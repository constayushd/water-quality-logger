#!/usr/bin/env python3
"""
pi_server.py -- runs on the Raspberry Pi. Samples the three sensors and
serves the readings over Wi-Fi on the local network.

    python3 pi_server.py                     # bind 0.0.0.0:8000
    python3 pi_server.py --simulate          # fake data, no hardware
    python3 pi_server.py --port 8000 --token secret123
    python3 pi_server.py --device-id pi-01

Endpoints (all JSON except /stream and /):

    GET /health                 is it alive, what is it sampling
    GET /latest                 the newest reading from each sensor
    GET /readings?since=N       every reading with seq > N  (this is what the
                                PC dashboard polls; N=0 gives the whole buffer)
    GET /readings?limit=100     the newest 100 readings
    GET /stream                 server-sent events, one per reading (for a
                                browser or any client that prefers push)
    GET /                       a plain live page you can open in a browser

If --token is given, every request needs it: either an "X-Auth-Token" header
or "?token=..." on the URL.

Standard library only -- nothing to pip install on the Pi.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import socket
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Deque, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

# Two ways to get the sensors, depending on which repo this is sitting in:
#   wq_bridge  -- inside water-quality-logger, driving its own driver package
#                 from its config.yaml (preferred: one sensor layer, not two)
#   sensors    -- the standalone sensors.py that ships beside this file
# Set WQ_SENSORS=sensors to force the standalone one.
_want = os.environ.get("WQ_SENSORS")
if _want == "sensors":
    import sensors
elif _want == "wq_bridge":
    import wq_bridge as sensors
else:
    try:
        import wq_bridge as sensors
    except ImportError:
        import sensors

SCHEMA = 1


# --------------------------------------------------------------------------
# Shared state: a ring buffer of readings plus the latest one per sensor
# --------------------------------------------------------------------------
class Store:
    def __init__(self, device_id: str, capacity: int = 5000,
                 spool_path: Optional[str] = None) -> None:
        self.device_id = device_id
        self.capacity = capacity
        self.spool_path = spool_path
        self._spool = open(spool_path, "a", encoding="utf-8") if spool_path else None
        self._lock = threading.Lock()
        self._buf: Deque[Dict[str, Any]] = deque(maxlen=capacity)
        self._latest: Dict[str, Dict[str, Any]] = {}
        self._errors: Dict[str, str] = {}
        self._seq = 0
        self.started = time.time()
        # One queue per connected /stream client.
        self._subs: List["queue.Queue[Dict[str, Any]]"] = []

    def add(self, source: str, metrics: Dict[str, float]) -> Dict[str, Any]:
        now = time.time()
        with self._lock:
            self._seq += 1
            reading = {
                "schema": SCHEMA,
                "seq": self._seq,
                "device_id": self.device_id,
                "source": source,
                "ts": round(now, 3),
                "ts_iso": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                "metrics": {k: float(v) for k, v in metrics.items()},
            }
            self._buf.append(reading)
            self._latest[source] = reading
            self._errors.pop(source, None)
            subs = list(self._subs)
            if self._spool is not None:
                # Every reading also lands on the SD card, so an overnight run
                # survives the PC being closed and the Pi rebooting.
                self._spool.write(json.dumps(reading, separators=(",", ":")) + "\n")
                self._spool.flush()
        for q in subs:
            try:
                q.put_nowait(reading)
            except queue.Full:
                pass
        return reading

    def add_error(self, source: str, message: str) -> None:
        with self._lock:
            self._errors[source] = message

    def since(self, seq: int, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        with self._lock:
            out = [r for r in self._buf if r["seq"] > seq]
        if limit is not None and len(out) > limit:
            out = out[-limit:]
        return out

    def newest(self, limit: int) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._buf)[-limit:]

    def latest(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return dict(self._latest)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "ok": True,
                "schema": SCHEMA,
                "device_id": self.device_id,
                "seq": self._seq,
                "buffered": len(self._buf),
                "capacity": self.capacity,
                "uptime_s": round(time.time() - self.started, 1),
                "server_time": round(time.time(), 3),
                "sensors": [
                    {"name": s.name, "interval_s": s.interval} for s in sensors.SENSORS
                ],
                "errors": dict(self._errors),
            }

    def subscribe(self) -> "queue.Queue[Dict[str, Any]]":
        q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=1000)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: "queue.Queue[Dict[str, Any]]") -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)


# --------------------------------------------------------------------------
# One sampling thread per sensor
# --------------------------------------------------------------------------
class SensorThread(threading.Thread):
    def __init__(self, sensor: "sensors.Sensor", store: Store, stop: threading.Event) -> None:
        super().__init__(name=f"sensor-{sensor.name}", daemon=True)
        self.sensor = sensor
        self.store = store
        self.stop = stop

    def run(self) -> None:
        next_at = time.time()
        while not self.stop.is_set():
            try:
                # Probes take turns: see EXCLUSIVE_SAMPLING in sensors.py.
                with sensors.exclusive():
                    values = self.sensor.fn()
                if not isinstance(values, dict) or not values:
                    raise ValueError(f"{self.sensor.name} returned {values!r}, expected a dict")
                self.store.add(self.sensor.name, values)
            except Exception as exc:  # noqa: BLE001 - never let one probe kill the rest
                msg = f"{type(exc).__name__}: {exc}"
                self.store.add_error(self.sensor.name, msg)
                log(f"[{self.sensor.name}] read failed: {msg}")
            next_at += self.sensor.interval
            delay = next_at - time.time()
            if delay < 0:                       # we fell behind; resync
                next_at = time.time()
                delay = 0.05
            self.stop.wait(delay)


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
INDEX_HTML = """<!doctype html><meta charset="utf-8">
<title>Water quality - {device}</title>
<style>
 body{{font:15px system-ui,sans-serif;margin:0;padding:24px;background:#0f1115;color:#e6e8ee}}
 h1{{font-size:18px;margin:0 0 4px}} .sub{{color:#8a90a0;font-size:13px;margin-bottom:20px}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px}}
 .card{{background:#181b22;border:1px solid #262a34;border-radius:10px;padding:14px}}
 .k{{color:#8a90a0;font-size:12px;text-transform:uppercase;letter-spacing:.04em}}
 .v{{font-size:28px;font-variant-numeric:tabular-nums;margin-top:6px}}
 .s{{color:#5c6272;font-size:11px;margin-top:6px}}
</style>
<h1>Water quality &mdash; {device}</h1>
<div class="sub" id="sub">connecting&hellip;</div>
<div class="grid" id="grid"></div>
<script>
const q = new URLSearchParams(location.search);
const tok = q.get("token") ? "?token=" + encodeURIComponent(q.get("token")) : "";
const cards = {{}};
function put(metric, value, source, iso) {{
  let c = cards[metric];
  if (!c) {{
    c = document.createElement("div"); c.className = "card";
    c.innerHTML = '<div class="k"></div><div class="v"></div><div class="s"></div>';
    c.querySelector(".k").textContent = metric;
    document.getElementById("grid").appendChild(c); cards[metric] = c;
  }}
  c.querySelector(".v").textContent = (Math.round(value * 1000) / 1000).toFixed(2);
  c.querySelector(".s").textContent = source + " \\u00b7 " + iso.slice(11, 19) + "Z";
}}
const es = new EventSource("/stream" + tok);
es.onopen = () => document.getElementById("sub").textContent = "live";
es.onerror = () => document.getElementById("sub").textContent = "disconnected - retrying";
es.onmessage = (e) => {{
  const r = JSON.parse(e.data);
  for (const [m, v] of Object.entries(r.metrics)) put(m, v, r.source, r.ts_iso);
}};
fetch("/latest" + tok).then(r => r.json()).then(d => {{
  for (const r of Object.values(d.latest || {{}}))
    for (const [m, v] of Object.entries(r.metrics)) put(m, v, r.source, r.ts_iso);
}});
</script>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "wq-pi/1.0"
    protocol_version = "HTTP/1.1"
    store: Store = None       # type: ignore[assignment]
    token: Optional[str] = None

    # ---- helpers ---------------------------------------------------------
    def _authorised(self, params: Dict[str, List[str]]) -> bool:
        if not self.token:
            return True
        supplied = self.headers.get("X-Auth-Token") or (params.get("token") or [None])[0]
        return supplied == self.token

    def _json(self, payload: Any, code: int = 200) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, text: str) -> None:
        body = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- routing ---------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"

        if not self._authorised(params):
            self._json({"ok": False, "error": "unauthorised"}, 401)
            return

        if path == "/health":
            self._json(self.store.snapshot())
        elif path == "/latest":
            self._json({"ok": True, "device_id": self.store.device_id,
                        "latest": self.store.latest()})
        elif path == "/readings":
            snap = self.store.snapshot()
            limit = _int(params.get("limit", [None])[0])
            if "since" in params:
                since = _int(params.get("since", ["0"])[0]) or 0
                rows = self.store.since(since, limit)
            else:
                rows = self.store.newest(limit or 200)
            self._json({"ok": True, "device_id": self.store.device_id,
                        "seq": snap["seq"], "count": len(rows), "readings": rows})
        elif path == "/stream":
            self._stream()
        elif path == "/":
            self._html(INDEX_HTML.format(device=self.store.device_id))
        else:
            self._json({"ok": False, "error": "not found",
                        "endpoints": ["/health", "/latest", "/readings", "/stream", "/"]}, 404)

    def _stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        q = self.store.subscribe()
        try:
            while True:
                try:
                    reading = q.get(timeout=15)
                    payload = json.dumps(reading, separators=(",", ":"))
                    self.wfile.write(f"data: {payload}\n\n".encode())
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")   # stops idle proxies timing out
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self.store.unsubscribe(q)

    def log_message(self, fmt: str, *args: Any) -> None:
        if os.environ.get("WQ_HTTP_LOG"):
            log("http " + fmt % args)


def _int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def lan_ip() -> str:
    """Best guess at this machine's address on the LAN (no traffic is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Serve water-quality readings over the LAN.")
    ap.add_argument("--host", default="0.0.0.0", help="interface to bind (default: all)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("WQ_PORT", 8000)))
    ap.add_argument("--device-id", default=os.environ.get("WQ_DEVICE_ID", "pi-01"))
    ap.add_argument("--token", default=os.environ.get("WQ_TOKEN"),
                    help="require this shared secret on every request")
    ap.add_argument("--buffer", type=int, default=5000,
                    help="readings kept in memory for late-joining clients")
    ap.add_argument("--spool", metavar="FILE", default=os.environ.get("WQ_SPOOL"),
                    help="also append every reading to this JSONL file on the Pi")
    ap.add_argument("--simulate", action="store_true", help="fake data, no hardware")
    ap.add_argument("--once", action="store_true",
                    help="read each sensor once, print the JSON, exit (no server)")
    args = ap.parse_args()

    if args.simulate:
        sensors.SIMULATE = True

    store = Store(args.device_id, capacity=args.buffer, spool_path=args.spool)

    if args.once:
        for s in sensors.SENSORS:
            try:
                print(json.dumps(store.add(s.name, s.fn()), indent=2))
            except Exception as exc:  # noqa: BLE001
                print(json.dumps({"source": s.name, "error": f"{type(exc).__name__}: {exc}"}))
        return 0

    stop = threading.Event()
    for s in sensors.SENSORS:
        SensorThread(s, store, stop).start()

    Handler.store = store
    Handler.token = args.token
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True

    ip = lan_ip()
    suffix = f"?token={args.token}" if args.token else ""
    log(f"device_id={args.device_id}  simulate={sensors.SIMULATE}  auth={'on' if args.token else 'off'}")
    log(f"serving on http://{ip}:{args.port}/{suffix}")
    log(f"  PC:  python3 pc_dashboard.py --host {ip}" + (f" --token {args.token}" if args.token else ""))
    log("Ctrl-C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("stopping")
    finally:
        stop.set()
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
