#!/usr/bin/env python3
"""
pc_dashboard.py -- runs on the PC. Pulls readings from the Pi over Wi-Fi,
appends them to a CSV, and draws a live plot per metric.

    python3 pc_dashboard.py --host 10.1.58.62
    python3 pc_dashboard.py --host 10.1.58.62 --token secret123
    python3 pc_dashboard.py --host 10.1.58.62 --headless     # console + CSV only
    python3 pc_dashboard.py --host 10.1.58.62 --window 60    # 60 minutes on screen
    python3 pc_dashboard.py --host 10.1.58.62 --preview out.png   # one frame, then exit
    python3 pc_dashboard.py --host 10.1.58.62 --once         # print /latest and exit

Two ways in. By default the PC polls the Pi directly, which needs the two to be
able to reach each other. On a network that forbids that -- most office guest
Wi-Fi -- the Pi publishes to a broker instead and the PC subscribes:

    python3 pc_dashboard.py --mqtt                           # settings from wq_mqtt.ini
    python3 pc_dashboard.py --mqtt --broker abc.s1.eu.hivemq.cloud \
                            --mqtt-user wq_desk --mqtt-pass secret
    python3 pc_dashboard.py --mqtt --websockets              # if 8883 is blocked

Everything downstream -- plots, CSV, the online/offline header -- is the same
either way.

Needs matplotlib for the plots (pip install matplotlib). --headless and
--once need nothing beyond the standard library.

The CSV is long-form:

    timestamp, epoch, device_id, source, metric, value

so a fourth sensor never breaks the file. To get a wide table:

    import pandas as pd
    df = pd.read_csv("wq_log.csv", parse_dates=["timestamp"])
    wide = df.pivot_table(index="timestamp", columns="metric", values="value")
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import queue
import sys
import threading
import time
import socket
import urllib.error
import urllib.request
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple
from urllib.parse import urlencode

OFFLINE_AFTER = 45.0     # seconds without a reading before the header goes red


def say(msg: str) -> None:
    """One write per line, so the poller thread never splits a line in half."""
    try:
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()
    except BrokenPipeError:
        # Piped into head, or the terminal went away. Nothing to say about it.
        os._exit(0)


# --------------------------------------------------------------------------
# Talking to the Pi
# --------------------------------------------------------------------------
class PiClient:
    def __init__(self, host: str, port: int, token: Optional[str], timeout: float = 10.0):
        self.base = f"http://{host}:{port}"
        self.token = token
        self.timeout = timeout

    def get(self, path: str, **params: Any) -> Dict[str, Any]:
        if self.token:
            params["token"] = self.token
        url = self.base + path
        if params:
            url += "?" + urlencode(params)
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        # No proxy: the Pi is on the LAN, and a system proxy would break this.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode())


class Poller(threading.Thread):
    """Polls /readings?since=N and puts each new reading on a queue.

    Survives the Pi rebooting (sequence numbers restart), the Wi-Fi dropping,
    and the dashboard being started before the Pi.
    """

    def __init__(self, client: PiClient, out: "queue.Queue[Dict[str, Any]]",
                 interval: float, stop: threading.Event, backfill: bool = False,
                 clock: str = "pi") -> None:
        super().__init__(name="poller", daemon=True)
        self.client = client
        self.label = client.base
        self.out = out
        self.interval = interval
        self.stop = stop
        self.since = 0
        self._backfill = backfill
        self.connected = False
        self.last_error: Optional[str] = None
        self.last_ok: float = 0.0
        # A Pi with no internet has no NTP, so its clock can be minutes or years
        # out. clock="pc" rewrites each reading's timestamp using the measured
        # offset; the Pi's own value is kept as ts_pi.
        self.clock = clock
        self.offset = 0.0
        self._offset_at = 0.0
        self._warned_offset = False

    def _measure_offset(self) -> None:
        """Compare the Pi's clock with ours, allowing for the round trip."""
        t0 = time.time()
        health = self.client.get("/health")
        t1 = time.time()
        server_time = health.get("server_time")
        if server_time is None:
            return
        self.offset = (t0 + t1) / 2 - float(server_time)
        self._offset_at = t1
        if abs(self.offset) > 5 and not self._warned_offset:
            self._warned_offset = True
            direction = "behind" if self.offset > 0 else "ahead of"
            say(f"! the Pi's clock is {_duration(abs(self.offset))} {direction} this PC"
                + (" - timestamps are being corrected" if self.clock == "pc"
                   else " - run with --clock pc to correct the timestamps"))

    def _apply_clock(self, reading: Dict[str, Any]) -> Dict[str, Any]:
        if self.clock != "pc" or not self._offset_at:
            return reading
        reading["ts_pi"] = reading["ts"]
        reading["ts_pi_iso"] = reading.get("ts_iso")
        reading["ts"] = round(float(reading["ts"]) + self.offset, 3)
        reading["ts_iso"] = datetime.fromtimestamp(reading["ts"], timezone.utc).isoformat()
        reading["clock"] = "pc"
        return reading

    def run(self) -> None:
        delay = 1.0
        first = True
        while not self.stop.is_set():
            try:
                if first and not self._backfill:
                    # Start from "now": ask the Pi where it is, skip the history.
                    health = self.client.get("/health")
                    self.since = int(health.get("seq", 0))
                    first = False
                if time.time() - self._offset_at > 60:
                    self._measure_offset()
                payload = self.client.get("/readings", since=self.since)
                server_seq = int(payload.get("seq", 0))
                if server_seq < self.since:            # the Pi restarted
                    say("! Pi restarted (sequence reset) - resyncing")
                    self.since = 0
                    payload = self.client.get("/readings", since=0)
                for reading in payload.get("readings", []):
                    self.since = max(self.since, int(reading["seq"]))
                    self.out.put(self._apply_clock(reading))
                if not self.connected:
                    say(f"* connected to {self.client.base}")
                self.connected = True
                self.last_ok = time.time()
                self.last_error = None
                delay = 1.0
                first = False
            except Exception as exc:  # noqa: BLE001
                msg = _explain(exc)
                if self.connected or self.last_error != msg:
                    say(f"! {msg}  (retrying in {delay:.0f}s)")
                self.connected = False
                self.last_error = msg
                self.stop.wait(delay)
                delay = min(delay * 2, 30.0)          # exponential backoff, capped
                continue
            self.stop.wait(self.interval)


def _duration(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    if seconds < 172800:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} days"


def _explain(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 401:
            return "401 unauthorised - wrong or missing --token"
        return f"HTTP {exc.code} from the Pi"
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", exc)
        text = str(reason)
        if "refused" in text.lower():
            return "connection refused - is pi_server.py running on the Pi?"
        if "timed out" in text.lower() or "timeout" in text.lower():
            return ("no reply from the Pi - it is on a different subnet, asleep, "
                    "or the Wi-Fi blocks client-to-client traffic")
        if "not known" in text.lower():
            return "host not found - check the --host address"
        return f"network error: {text}"
    return f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------
# The other way in: subscribe to a broker instead of polling the Pi
# --------------------------------------------------------------------------
class MqttSource(threading.Thread):
    """Receives readings a Pi has published to an MQTT broker.

    Used when the Pi and the PC cannot reach each other -- a guest Wi-Fi with
    client isolation, or two different networks entirely. Both ends connect
    outward to the broker, so neither needs to accept an incoming connection.

    Presents the same interface as Poller, so the plots, the CSV and the
    header do not know the difference.
    """

    def __init__(self, cfg: Any, out: "queue.Queue[Dict[str, Any]]",
                 stop: threading.Event, devices: Optional[List[str]] = None,
                 clock: str = "pi", session_id: Optional[str] = None) -> None:
        super().__init__(name="mqtt", daemon=True)
        import wq_mqtt  # imported late so the HTTP path needs no paho

        self.cfg = cfg
        self.label = cfg.describe()
        self.out = out
        self.stop = stop
        self.devices = set(devices) if devices else None
        self.clock = clock
        self.connected = False
        self.last_error: Optional[str] = None
        self.offset = 0.0
        self._have_offset = False
        self._warned_offset = False
        self.states: Dict[str, str] = {}
        # QoS 1 is "at least once", so a reconnect can redeliver. Remember what
        # we have already seen, bounded so it cannot grow forever.
        self._seen: "deque[Tuple[str, int]]" = deque(maxlen=20000)
        self._seen_set: set = set()

        # With a session id the broker keeps a queue for this PC while the
        # dashboard is closed and delivers it on the next start -- so an
        # overnight run is not lost because nobody was watching. Without one,
        # each run is fresh and only sees what arrives while it is open.
        if session_id:
            client_id = f"wq-pc-{session_id}"
            self.label += f" (session {session_id})"
        else:
            client_id = f"wq-pc-{socket.gethostname()[:12]}-{os.getpid()}"
        self.client, _ = wq_mqtt.make_client(cfg, client_id,
                                             clean_session=not session_id)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self._explain_rc = wq_mqtt.explain_rc

    # ---- callbacks -------------------------------------------------------
    def _on_connect(self, client: Any, userdata: Any, flags: Any, rc: int) -> None:
        if rc == 0:
            self.connected = True
            self.last_error = None
            client.subscribe([(self.cfg.subscribe_data(), 1),
                              (self.cfg.subscribe_status(), 1)])
            say(f"* subscribed to {self.cfg.base_topic}/+/data on {self.cfg.host}")
        else:
            self.connected = False
            self.last_error = self._explain_rc(rc)
            say(f"! {self.last_error}")

    def _on_disconnect(self, client: Any, userdata: Any, rc: int, *a: Any) -> None:
        self.connected = False
        if not self.stop.is_set():
            self.last_error = "disconnected from the broker"
            say("! disconnected from the broker - retrying")

    def _on_message(self, client: Any, userdata: Any, msg: Any) -> None:
        try:
            payload = json.loads(msg.payload.decode())
        except Exception:  # noqa: BLE001 - someone else's message on our topic
            return
        device = payload.get("device_id") or msg.topic.split("/")[-2:-1][0]
        if self.devices and device not in self.devices:
            return

        if msg.topic.endswith("/status"):
            # A retained status is whatever the Pi last said, possibly days ago,
            # so its clock reading is useless for measuring the offset.
            self._on_status(device, payload, retained=bool(getattr(msg, "retain", 0)))
            return

        for reading in payload.get("readings", []) or []:
            key = (str(reading.get("device_id", device)), int(reading.get("seq", -1)))
            if key in self._seen_set:
                continue                      # a redelivery, not a new reading
            if len(self._seen) == self._seen.maxlen:
                self._seen_set.discard(self._seen[0])
            self._seen.append(key)
            self._seen_set.add(key)
            self.out.put(self._apply_clock(reading))

    def _on_status(self, device: str, payload: Dict[str, Any],
                   retained: bool = False) -> None:
        state = payload.get("state", "?")
        if self.states.get(device) != state:
            self.states[device] = state
            say(f"* {device} is {state}" + (" (last known)" if retained else ""))
        now = payload.get("now")
        if now is not None and not retained:
            self.offset = time.time() - float(now)
            self._have_offset = True
            if abs(self.offset) > 5 and not self._warned_offset:
                self._warned_offset = True
                direction = "behind" if self.offset > 0 else "ahead of"
                say(f"! {device}'s clock is {_duration(abs(self.offset))} "
                    f"{direction} this PC"
                    + (" - timestamps are being corrected" if self.clock == "pc"
                       else " - run with --clock pc to correct the timestamps"))

    def _apply_clock(self, reading: Dict[str, Any]) -> Dict[str, Any]:
        if self.clock != "pc" or not self._have_offset:
            return reading
        reading["ts_pi"] = reading["ts"]
        reading["ts_pi_iso"] = reading.get("ts_iso")
        reading["ts"] = round(float(reading["ts"]) + self.offset, 3)
        reading["ts_iso"] = datetime.fromtimestamp(reading["ts"], timezone.utc).isoformat()
        reading["clock"] = "pc"
        return reading

    # ---- thread ----------------------------------------------------------
    def run(self) -> None:
        self.cfg.check()
        try:
            self.client.connect_async(self.cfg.host, self.cfg.port, keepalive=60)
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            say(f"! {self.last_error}")
            return
        self.client.loop_start()
        say(f"connecting to {self.label} ...")
        self.stop.wait()
        self.client.loop_stop()
        try:
            self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# Finding the Pi again after DHCP has moved it
# --------------------------------------------------------------------------
def local_ip() -> str:
    """This machine's address on the LAN (no traffic is actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def scan(networks: List[str], port: int, token: Optional[str],
         timeout: float = 0.5, workers: int = 200) -> List[Tuple[str, Dict[str, Any]]]:
    """Knock on <port> across the given networks and report anything that
    answers /health like pi_server.py. Use when DHCP has moved the Pi."""
    import concurrent.futures
    import ipaddress

    if not networks:
        networks = [f"{local_ip()}/24"]

    targets: List[str] = []
    for net in networks:
        try:
            targets.extend(str(ip) for ip in ipaddress.ip_network(net, strict=False).hosts())
        except ValueError as exc:
            say(f"! skipping {net}: {exc}")
    if len(targets) > 8192:
        say(f"! {len(targets)} addresses is too many; narrow the range (e.g. 10.1.23.0/24)")
        return []

    say(f"scanning {len(targets)} addresses on port {port} ...")

    def probe(ip: str) -> Optional[Tuple[str, Dict[str, Any]]]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            if sock.connect_ex((ip, port)) != 0:
                return None
        finally:
            sock.close()
        try:
            health = PiClient(ip, port, token, timeout=3.0).get("/health")
            if health.get("ok") and "device_id" in health:
                return ip, health
        except Exception:  # noqa: BLE001 - something else is on that port
            pass
        return None

    found: List[Tuple[str, Dict[str, Any]]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for result in pool.map(probe, targets):
            if result:
                ip, health = result
                names = ", ".join(s["name"] for s in health.get("sensors", []))
                say(f"  found {health['device_id']} at {ip}:{port}  [{names}]")
                found.append(result)
    if not found:
        say("  nothing found. The Pi may be on another subnet, or the server is not running.")
    return found


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------
class CsvLog:
    HEADER = ["timestamp", "epoch", "device_id", "source", "metric", "value",
              "pi_timestamp"]

    def __init__(self, path: str) -> None:
        self.path = path
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        # An existing file written before pi_timestamp existed keeps its six
        # columns, so appending never produces a ragged CSV.
        self.columns = len(self.HEADER)
        if not new:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    first = fh.readline()
                self.columns = max(6, len(next(csv.reader([first]))))
            except Exception:  # noqa: BLE001
                self.columns = 6
        self.fh = open(path, "a", newline="", encoding="utf-8")
        self.writer = csv.writer(self.fh)
        if new:
            self.writer.writerow(self.HEADER)
            self.fh.flush()

    def write(self, reading: Dict[str, Any]) -> None:
        iso = reading.get("ts_iso") or datetime.fromtimestamp(
            reading["ts"], timezone.utc).isoformat()
        for metric, value in reading["metrics"].items():
            row = [iso, reading["ts"], reading["device_id"],
                   reading["source"], metric, value]
            if self.columns > 6:
                # Blank unless --clock pc rewrote the timestamp.
                row.append(reading.get("ts_pi_iso", ""))
            self.writer.writerow(row)
        self.fh.flush()

    def close(self) -> None:
        try:
            self.fh.close()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# Series held for plotting
# --------------------------------------------------------------------------
class Series:
    def __init__(self, window_minutes: float, cap: int = 20000) -> None:
        self.window = window_minutes * 60.0
        self.data: Dict[str, Tuple[Deque[float], Deque[float]]] = defaultdict(
            lambda: (deque(maxlen=cap), deque(maxlen=cap)))
        self.source_of: Dict[str, str] = {}
        self.last_ts = 0.0

    def add(self, reading: Dict[str, Any]) -> bool:
        fresh_metric = False
        for metric, value in reading["metrics"].items():
            if metric not in self.data:
                fresh_metric = True
            xs, ys = self.data[metric]
            xs.append(reading["ts"])
            ys.append(float(value))
            self.source_of[metric] = reading["source"]
        self.last_ts = max(self.last_ts, float(reading["ts"]))
        return fresh_metric

    def trim(self) -> None:
        cutoff = time.time() - self.window
        for xs, ys in self.data.values():
            while xs and xs[0] < cutoff:
                xs.popleft()
                ys.popleft()

    def metrics(self) -> List[str]:
        return sorted(self.data)


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------
def run_once(client: PiClient) -> int:
    health = client.get("/health")
    latest = client.get("/latest")
    print(json.dumps({"health": health, "latest": latest["latest"]}, indent=2))
    return 0


def run_headless(poller_q: "queue.Queue[Dict[str, Any]]", log: Optional[CsvLog],
                 stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            r = poller_q.get(timeout=0.5)
        except queue.Empty:
            continue
        if log:
            log.write(r)
        values = "  ".join(f"{k}={v:g}" for k, v in r["metrics"].items())
        say(f"{r['ts_iso'][11:19]}Z  {r['device_id']:<6} {r['source']:<11} {values}")


def run_plot(poller: Poller, poller_q: "queue.Queue[Dict[str, Any]]",
             log: Optional[CsvLog], series: Series, stop: threading.Event,
             preview: Optional[str], refresh: float) -> int:
    try:
        import matplotlib
        if preview:
            matplotlib.use("Agg")
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib is not installed. Either:\n"
              "    pip install matplotlib\n"
              "or run with --headless for console + CSV only.", file=sys.stderr)
        return 2

    fig = plt.figure(figsize=(11, 7))
    try:
        fig.canvas.manager.set_window_title("Water quality - live")
    except Exception:  # noqa: BLE001 - not every backend has a window
        pass
    axes: Dict[str, Any] = {}
    header = fig.suptitle("waiting for the Pi...", fontsize=12, color="#b00020")
    if not preview:
        plt.ion()
        plt.show(block=False)

    def rebuild() -> None:
        """Lay the figure out again -- one panel per metric seen so far."""
        nonlocal axes, header
        fig.clf()
        metrics = series.metrics()
        cols = 1 if len(metrics) <= 3 else 2
        rows = max(1, (len(metrics) + cols - 1) // cols)
        axes = {}
        for i, m in enumerate(metrics):
            axes[m] = fig.add_subplot(rows, cols, i + 1)
        header = fig.suptitle("", fontsize=12)
        fig.subplots_adjust(top=0.90, hspace=0.5, wspace=0.25)

    deadline = time.time() + 20.0
    try:
        while not stop.is_set():
            drained = 0
            while drained < 5000:
                try:
                    r = poller_q.get_nowait()
                except queue.Empty:
                    break
                drained += 1
                if log:
                    log.write(r)
                series.add(r)

            series.trim()
            if set(axes) != set(series.metrics()):
                rebuild()

            for m, ax in axes.items():
                xs, ys = series.data[m]
                ax.cla()
                ax.grid(alpha=0.25)
                if xs:
                    times = [datetime.fromtimestamp(t) for t in xs]
                    ax.plot(times, list(ys), lw=1.4, color="#1f6feb")
                    ax.plot(times[-1], ys[-1], "o", ms=5, color="#d1242f")
                    ax.set_title(f"{m} = {ys[-1]:g}   ({series.source_of.get(m, '')})",
                                 fontsize=10)
                else:
                    ax.set_title(m, fontsize=10)
                ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=5))
                ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
                for label in ax.get_xticklabels():
                    label.set_fontsize(8)

            age = time.time() - series.last_ts if series.last_ts else 1e9
            if poller.connected and age < OFFLINE_AFTER:
                header.set_text(f"{poller.label}  -  online   "
                                f"(last reading {age:.0f}s ago)")
                header.set_color("#0a7f2e")
            else:
                why = poller.last_error or f"no reading for {age:.0f}s"
                header.set_text(f"{poller.label}  -  OFFLINE   ({why})")
                header.set_color("#b00020")

            fig.canvas.draw_idle()
            if preview:
                if series.last_ts or time.time() > deadline:
                    fig.savefig(preview, dpi=130, bbox_inches="tight")
                    print(f"wrote {preview}")
                    return 0
                time.sleep(1.0)
            else:
                fig.canvas.flush_events()
                if not plt.fignum_exists(fig.number):
                    return 0
                time.sleep(refresh)
    except KeyboardInterrupt:
        pass
    return 0


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Live water-quality dashboard, pulled from the Pi.")
    ap.add_argument("--host", default=os.environ.get("WQ_HOST", "raspberrypi.local"),
                    help="the Pi's address or hostname (default: raspberrypi.local, "
                         "which follows the Pi when DHCP changes its IP)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("WQ_PORT", 8000)))
    ap.add_argument("--token", default=os.environ.get("WQ_TOKEN"))
    ap.add_argument("--csv", default="wq_log.csv", help="CSV to append to ('' to disable)")
    ap.add_argument("--window", type=float, default=10.0, help="minutes kept on screen")
    ap.add_argument("--poll", type=float, default=1.0, help="seconds between polls")
    ap.add_argument("--refresh", type=float, default=0.5, help="seconds between redraws")
    ap.add_argument("--backfill", action="store_true",
                    help="also fetch readings the Pi took before this started")
    ap.add_argument("--headless", action="store_true", help="console + CSV, no window")
    ap.add_argument("--preview", metavar="PNG", help="save one frame and exit (for SSH)")
    ap.add_argument("--once", action="store_true", help="print health + latest, then exit")
    ap.add_argument("--clock", choices=("pi", "pc"), default="pi",
                    help="whose clock timestamps the readings. 'pc' corrects the "
                         "Pi's time against this laptop's, which matters when the "
                         "Pi runs as a hotspot with no internet and so no NTP "
                         "(the Pi's own value is kept in the pi_timestamp column)")
    ap.add_argument("--scan", nargs="*", metavar="NETWORK",
                    help="find the Pi: probe these networks (e.g. 10.1.23.0/24), "
                         "or this PC's own /24 if none given, then exit")
    ap.add_argument("--mqtt", action="store_true",
                    help="receive readings from an MQTT broker instead of polling "
                         "the Pi directly -- the path for a network that will not "
                         "let the two machines talk to each other")
    ap.add_argument("--devices", nargs="+", metavar="ID",
                    help="with --mqtt, only show these device_ids")
    ap.add_argument("--session", metavar="NAME",
                    help="with --mqtt, a durable session name for this PC. The "
                         "broker then holds readings while the dashboard is "
                         "closed and delivers them next time, so an overnight "
                         "run is not lost. Give each PC its own name.")
    try:
        import wq_mqtt as _wq_mqtt
        _wq_mqtt.add_arguments(ap, side="pc")
    except ImportError:
        pass
    args = ap.parse_args()

    if args.scan is not None:
        found = scan(args.scan, args.port, args.token)
        if found:
            say(f"\nrun:  python3 pc_dashboard.py --host {found[0][0]}")
        return 0 if found else 1

    if args.once and not args.mqtt:
        try:
            return run_once(PiClient(args.host, args.port, args.token))
        except Exception as exc:  # noqa: BLE001
            print(_explain(exc), file=sys.stderr)
            return 1

    stop = threading.Event()
    q: "queue.Queue[Dict[str, Any]]" = queue.Queue()

    poller: Any
    if args.mqtt:
        import wq_mqtt
        cfg = wq_mqtt.config_from_args(args)
        cfg.check()
        poller = MqttSource(cfg, q, stop, devices=args.devices, clock=args.clock,
                            session_id=args.session)
    else:
        poller = Poller(PiClient(args.host, args.port, args.token), q, args.poll,
                        stop, backfill=args.backfill, clock=args.clock)
    poller.start()

    log = CsvLog(args.csv) if args.csv else None
    if log:
        say(f"logging to {os.path.abspath(log.path)}")

    try:
        if args.headless:
            run_headless(q, log, stop)
            return 0
        series = Series(args.window)
        return run_plot(poller, q, log, series, stop, args.preview, args.refresh)
    except KeyboardInterrupt:
        return 0
    finally:
        stop.set()
        if log:
            log.close()


if __name__ == "__main__":
    sys.exit(main())
