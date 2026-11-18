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
from typing import Any, Deque, Dict, List, Optional, Tuple
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
    """Samples one probe on its own schedule, and takes orders while it runs.

    The interval can be changed, the probe paused and resumed, and a reading
    taken immediately, all without restarting the process -- which matters
    once the Pi is deployed and the only way in is a phone on the hotspot.
    """

    def __init__(self, sensor: "sensors.Sensor", store: Store, stop: threading.Event) -> None:
        super().__init__(name=f"sensor-{sensor.name}", daemon=True)
        self.sensor = sensor
        self.store = store
        self.stop = stop
        self.interval = float(sensor.interval)
        self.paused = False
        self.reads = 0
        self.failures = 0
        self.last_ts = 0.0
        self._wake = threading.Event()       # set to cut the sleep short
        self._force = False                  # a one-off read, even when paused
        self._flag_lock = threading.Lock()

    # ---- orders ----------------------------------------------------------
    def sample_now(self) -> None:
        with self._flag_lock:
            self._force = True
        self._wake.set()

    def set_interval(self, seconds: float) -> None:
        self.interval = max(0.2, float(seconds))
        self._wake.set()

    def set_paused(self, paused: bool) -> None:
        self.paused = bool(paused)
        self._wake.set()

    def status(self) -> Dict[str, Any]:
        return {
            "name": self.sensor.name,
            "interval_s": round(self.interval, 3),
            "paused": self.paused,
            "reads": self.reads,
            "failures": self.failures,
            "last_ts": round(self.last_ts, 3) if self.last_ts else None,
            "age_s": round(time.time() - self.last_ts, 1) if self.last_ts else None,
        }

    # ---- loop ------------------------------------------------------------
    def _read_once(self) -> None:
        try:
            # Probes take turns: see EXCLUSIVE_SAMPLING in sensors.py.
            with sensors.exclusive():
                values = self.sensor.fn()
            if not isinstance(values, dict) or not values:
                raise ValueError(f"{self.sensor.name} returned {values!r}, expected a dict")
            self.store.add(self.sensor.name, values)
            self.reads += 1
            self.last_ts = time.time()
        except Exception as exc:  # noqa: BLE001 - never let one probe kill the rest
            msg = f"{type(exc).__name__}: {exc}"
            self.failures += 1
            self.store.add_error(self.sensor.name, msg)
            log(f"[{self.sensor.name}] read failed: {msg}")

    def run(self) -> None:
        while not self.stop.is_set():
            # Clear first, so an order arriving during the read below is not
            # swallowed -- it simply makes the next wait return immediately.
            self._wake.clear()
            with self._flag_lock:
                forced, self._force = self._force, False
            if not self.paused or forced:
                self._read_once()
            if self.stop.is_set():
                break
            self._wake.wait(max(0.05, self.interval))


def log(msg: str) -> None:
    print(f"{datetime.now().strftime('%H:%M:%S')} {msg}", flush=True)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
INDEX_HTML = """<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Water quality monitoring - {device}</title>
<style>
 :root{{color-scheme:dark}}
 *{{box-sizing:border-box}}
 body{{font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;margin:0;
   padding:20px 16px calc(28px + env(safe-area-inset-bottom,0px));
   background:#0f1115;color:#e6e8ee}}
 .wrap{{max-width:900px;margin:0 auto}}
 h1{{font-size:18px;margin:0 0 4px}}
 .sub{{color:#8a90a0;font-size:13px;margin-bottom:18px}}
 .dot{{display:inline-block;width:8px;height:8px;border-radius:50%;background:#555;
   margin-right:6px;vertical-align:middle}}
 .dot.on{{background:#2ea043}} .dot.off{{background:#d1242f}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}}
 .card{{background:#181b22;border:1px solid #262a34;border-radius:10px;padding:13px}}
 .k{{color:#8a90a0;font-size:11px;text-transform:uppercase;letter-spacing:.05em}}
 .v{{font-size:26px;font-variant-numeric:tabular-nums;margin-top:5px}}
 .s{{color:#5c6272;font-size:11px;margin-top:5px}}
 h2{{font-size:13px;color:#8a90a0;text-transform:uppercase;letter-spacing:.05em;
   margin:26px 0 10px;font-weight:600}}
 .row{{background:#181b22;border:1px solid #262a34;border-radius:10px;padding:12px;
   margin-bottom:9px;display:flex;flex-wrap:wrap;gap:9px;align-items:center}}
 .row .name{{font-weight:600;flex:1 1 110px;min-width:110px}}
 .row .meta{{color:#5c6272;font-size:12px;flex:1 1 100%;order:9}}
 button{{font:inherit;font-size:14px;padding:10px 14px;min-height:42px;border-radius:8px;
   border:1px solid #30363d;background:#21262d;color:#e6e8ee;cursor:pointer}}
 button:active{{background:#2d333b}}
 button.p{{background:#1f6feb;border-color:#1f6feb}}
 button.warn{{background:#3d2226;border-color:#5c2b31}}
 input[type=number]{{font:inherit;width:78px;padding:9px;border-radius:8px;
   border:1px solid #30363d;background:#0d1117;color:#e6e8ee}}
 .paused{{opacity:.55}}
 .lbl{{color:#8a90a0;font-size:13px}}
 .bar{{display:flex;gap:9px;flex-wrap:wrap;margin-bottom:14px}}
 #toast{{position:fixed;left:50%;transform:translateX(-50%);
   bottom:calc(18px + env(safe-area-inset-bottom,0px));background:#21262d;
   border:1px solid #30363d;border-radius:8px;padding:11px 16px;font-size:14px;
   max-width:92vw;opacity:0;transition:opacity .2s;pointer-events:none}}
 #toast.show{{opacity:1}}
 a{{color:#58a6ff}}
</style>
<div class="wrap">
<h1>Water quality monitoring &mdash; {device}</h1>
<div class="sub"><span class="dot" id="dot"></span><span id="sub">connecting&hellip;</span></div>

<div class="grid" id="grid"></div>

<h2>Controls</h2>
<div class="bar">
  <button class="p" onclick="cmd({{action:'sample',source:'all'}})">Sample all now</button>
  <button onclick="cmd({{action:'pause',source:'all'}})">Pause all</button>
  <button onclick="cmd({{action:'resume',source:'all'}})">Resume all</button>
  <button id="dl">Download log</button>
</div>
<div id="sensors"></div>
</div>
<div id="toast"></div>
<script>
const q = new URLSearchParams(location.search);
const token = q.get("token");
const tok = token ? "?token=" + encodeURIComponent(token) : "";
const cards = {{}};

function toast(msg, bad) {{
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.style.borderColor = bad ? "#5c2b31" : "#30363d";
  t.classList.add("show");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 2600);
}}

function put(metric, value, source, iso) {{
  // Key by source AND metric: two probes can both report temp_c, and one
  // must not overwrite the other's tile.
  const key = source + "/" + metric;
  let c = cards[key];
  if (!c) {{
    c = document.createElement("div"); c.className = "card";
    c.innerHTML = '<div class="k"></div><div class="v"></div><div class="s"></div>';
    c.querySelector(".k").textContent = metric;
    document.getElementById("grid").appendChild(c); cards[key] = c;
  }}
  c.querySelector(".v").textContent = (Math.round(value * 1000) / 1000).toFixed(2);
  c.querySelector(".s").textContent = source + " \u00b7 " + iso.slice(11, 19) + "Z";
}}

async function cmd(body) {{
  try {{
    const r = await fetch("/cmd" + tok, {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify(body)
    }});
    const d = await r.json();
    if (d.ok) {{ toast(d.result); render(d.sensors); }}
    else toast(d.error || "failed", true);
  }} catch (e) {{ toast("no reply from the Pi", true); }}
}}

function render(list) {{
  const host = document.getElementById("sensors");
  host.innerHTML = "";
  for (const s of list) {{
    const d = document.createElement("div");
    d.className = "row" + (s.paused ? " paused" : "");
    const age = s.age_s == null ? "no reading yet"
      : s.age_s.toFixed(0) + "s ago \u00b7 " + s.reads + " reads"
        + (s.failures ? " \u00b7 " + s.failures + " failed" : "");
    d.innerHTML =
      '<div class="name"></div>' +
      '<button data-a="sample">Sample</button>' +
      '<button data-a="toggle"></button>' +
      '<span class="lbl">every</span>' +
      '<input type="number" min="0.2" step="0.5" value="' + s.interval_s + '">' +
      '<span class="lbl">s</span>' +
      '<button data-a="interval">Set</button>' +
      '<div class="meta"></div>';
    d.querySelector(".name").textContent = s.name;
    d.querySelector(".meta").textContent = age;
    const toggle = d.querySelector('[data-a=toggle]');
    toggle.textContent = s.paused ? "Resume" : "Pause";
    if (s.paused) toggle.className = "warn";
    d.querySelector('[data-a=sample]').onclick = () => cmd({{action:"sample", source:s.name}});
    toggle.onclick = () => cmd({{action: s.paused ? "resume" : "pause", source: s.name}});
    d.querySelector('[data-a=interval]').onclick = () =>
      cmd({{action:"interval", source:s.name, seconds: d.querySelector("input").value}});
    host.appendChild(d);
  }}
}}

async function refresh() {{
  try {{
    const d = await (await fetch("/sensors" + tok)).json();
    if (d.sensors) render(d.sensors);
    document.getElementById("dl").style.display = d.spool ? "" : "none";
  }} catch (e) {{}}
}}
document.getElementById("dl").onclick = () => location.href = "/spool" + tok;

const es = new EventSource("/stream" + tok);
es.onopen = () => {{
  document.getElementById("sub").textContent = "live";
  document.getElementById("dot").className = "dot on";
}};
es.onerror = () => {{
  document.getElementById("sub").textContent = "disconnected - retrying";
  document.getElementById("dot").className = "dot off";
}};
es.onmessage = (e) => {{
  const r = JSON.parse(e.data);
  for (const [m, v] of Object.entries(r.metrics)) put(m, v, r.source, r.ts_iso);
}};

fetch("/latest" + tok).then(r => r.json()).then(d => {{
  for (const r of Object.values(d.latest || {{}}))
    for (const [m, v] of Object.entries(r.metrics)) put(m, v, r.source, r.ts_iso);
}});
refresh();
setInterval(refresh, 5000);
</script>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "wq-pi/1.0"
    protocol_version = "HTTP/1.1"
    store: Store = None       # type: ignore[assignment]
    token: Optional[str] = None
    threads: Dict[str, "SensorThread"] = {}
    allow_commands: bool = True
    spool_path: Optional[str] = None

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
        # Never cache the page. Editing dashboard.html and getting the old one
        # back from the browser's cache wastes more time than the bytes save.
        self.send_header("Cache-Control", "no-store, must-revalidate")
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
        elif path == "/sensors":
            self._json({"ok": True, "commands": self.allow_commands,
                        "spool": bool(self.spool_path),
                        "sensors": [t.status() for t in self.threads.values()]})
        elif path == "/spool":
            self._send_spool()
        elif path == "/cmd":
            # Handy from a phone or curl without a JSON body:
            #   /cmd?action=sample&source=ph
            self._command({k: v[0] for k, v in params.items() if k != "token"})
        elif path == "/":
            self._html(self._page())
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

    # ---- commands --------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if not self._authorised(params):
            self._json({"ok": False, "error": "unauthorised"}, 401)
            return
        if (parsed.path.rstrip("/") or "/") != "/cmd":
            self._json({"ok": False, "error": "not found"}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode() if length else "{}"
            payload = json.loads(body or "{}")
        except Exception:  # noqa: BLE001
            self._json({"ok": False, "error": "body is not JSON"}, 400)
            return
        self._command(payload if isinstance(payload, dict) else {})

    def _command(self, args: Dict[str, Any]) -> None:
        """Act on a live instruction.

        Deliberately small: the things worth doing while standing at the tank
        with a phone, and nothing that can wedge the process. Restarting
        services, reading logs and editing files is Cockpit's job.
        """
        if not self.allow_commands:
            self._json({"ok": False,
                        "error": "commands are disabled (--no-commands)"}, 403)
            return

        action = str(args.get("action", "")).lower()
        source = args.get("source")
        targets = list(self.threads.values())
        if source and source != "all":
            if source not in self.threads:
                self._json({"ok": False, "error": f"no sensor named {source!r}",
                            "known": sorted(self.threads)}, 404)
                return
            targets = [self.threads[source]]
        names = ", ".join(t.sensor.name for t in targets)

        if action == "sample":
            for t in targets:
                t.sample_now()
            result = f"sampling {names} now"
        elif action == "interval":
            raw = args.get("seconds", args.get("value"))
            try:
                seconds = float(raw)
            except (TypeError, ValueError):
                self._json({"ok": False, "error": "interval needs seconds=<number>"}, 400)
                return
            for t in targets:
                t.set_interval(seconds)
            result = f"{names}: interval set to {seconds:g}s"
        elif action in ("pause", "resume"):
            for t in targets:
                t.set_paused(action == "pause")
            result = f"{action}d {names}"
        else:
            self._json({"ok": False, "error": f"unknown action {action!r}",
                        "actions": ["sample", "interval", "pause", "resume"]}, 400)
            return

        log(f"command: {result}")
        self._json({"ok": True, "result": result,
                    "sensors": [t.status() for t in self.threads.values()]})

    # ---- the page -------------------------------------------------------
    _page_cache: Tuple[float, str] = (0.0, "")

    def _page(self) -> str:
        """Serve dashboard.html from disk if it is there, else the built-in page.

        Keeping the dashboard in its own file means it can be edited, diffed and
        reviewed as HTML rather than as a Python string, and reloaded without
        restarting the sampling loop.
        """
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            return INDEX_HTML.format(device=self.store.device_id)
        cached_at, cached = Handler._page_cache
        if cached and cached_at == mtime:
            return cached
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            log(f"! could not read dashboard.html ({exc}) - using the built-in page")
            return INDEX_HTML.format(device=self.store.device_id)
        Handler._page_cache = (mtime, text)
        return text

    def _send_spool(self) -> None:
        """Hand over the on-disk record, so the data can come home from the
        tank without SSH."""
        path = self.spool_path
        if not path or not os.path.exists(path):
            self._json({"ok": False,
                        "error": "no spool file (start with --spool FILE)"}, 404)
            return
        try:
            size = os.path.getsize(path)
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition",
                             f'attachment; filename="{os.path.basename(path)}"')
            self.end_headers()
            sent = 0
            with open(path, "rb") as fh:
                while sent < size:
                    chunk = fh.read(min(65536, size - sent))
                    if not chunk:
                        break
                    # The file keeps growing while we send; only ever write the
                    # number of bytes promised in Content-Length.
                    self.wfile.write(chunk)
                    sent += len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

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
    ap.add_argument("--no-commands", action="store_true",
                    help="serve readings but refuse sample/pause/interval commands")
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
    threads: Dict[str, SensorThread] = {}
    for s in sensors.SENSORS:
        t = SensorThread(s, store, stop)
        threads[s.name] = t
        t.start()

    Handler.store = store
    Handler.token = args.token
    Handler.threads = threads
    Handler.allow_commands = not args.no_commands
    Handler.spool_path = args.spool
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True

    ip = lan_ip()
    suffix = f"?token={args.token}" if args.token else ""
    log(f"device_id={args.device_id}  simulate={sensors.SIMULATE}  "
        f"auth={'on' if args.token else 'off'}  "
        f"commands={'on' if Handler.allow_commands else 'off'}")
    log(f"serving on http://{ip}:{args.port}/{suffix}")
    log(f"  PC:  python3 pc_dashboard.py --host {ip}" + (f" --token {args.token}" if args.token else ""))
    log("Ctrl-C to stop")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("stopping")
    finally:
        stop.set()
        for t in threads.values():
            t.sample_now()          # wake the sleepers so they exit promptly
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
