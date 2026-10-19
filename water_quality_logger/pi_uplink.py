#!/usr/bin/env python3
"""
pi_uplink.py -- runs on the Pi, next to pi_server.py. Takes the readings the
server is already collecting and pushes them out to an MQTT broker, so the PC
can see them without being able to reach the Pi directly.

This is the path for a network that will not let two devices talk to each other
-- an office guest Wi-Fi, for instance. Both the Pi and the PC make *outbound*
connections to the broker, which is something every network allows.

    python3 pi_uplink.py                    # settings from wq_mqtt.ini
    python3 pi_uplink.py --broker abc.s1.eu.hivemq.cloud \
                         --mqtt-user wq_pi --mqtt-pass secret
    python3 pi_uplink.py --websockets       # if port 8883 is blocked
    python3 pi_uplink.py --test             # connect, publish one, exit

It does not read the sensors itself -- pi_server.py does that. This process
just forwards, which means the local dashboard at http://<pi>:8000/ keeps
working, the spool file still records everything, and if the uplink dies the
readings are not lost.

Readings are batched (default: one message a minute) because waking the Wi-Fi
radio is what costs battery, not the bytes.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import wq_mqtt

STATE_FILE = os.environ.get("WQ_UPLINK_STATE", "wq_uplink_state.json")


def log(msg: str) -> None:
    sys.stdout.write(f"{datetime.now().strftime('%H:%M:%S')} {msg}\n")
    sys.stdout.flush()


# --------------------------------------------------------------------------
# Reading from the local server
# --------------------------------------------------------------------------
class LocalServer:
    """Source 1: pi_server.py's HTTP API on this same Pi."""

    kind = "http"

    def __init__(self, base: str, token: Optional[str], timeout: float = 10.0) -> None:
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def describe(self) -> str:
        return self.base

    def get(self, path: str, **params: Any) -> Dict[str, Any]:
        if self.token:
            params["token"] = self.token
        url = self.base + path + ("?" + urlencode(params) if params else "")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(url, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode())

    def poll(self, state: Dict[str, Any]) -> List[Dict[str, Any]]:
        cursor = int(state.get("since", 0))
        payload = self.get("/readings", since=cursor)
        if int(payload.get("seq", 0)) < cursor:        # pi_server restarted
            log("local server restarted - resyncing")
            cursor = 0
            payload = self.get("/readings", since=0)
        out = []
        for reading in payload.get("readings", []):
            cursor = max(cursor, int(reading["seq"]))
            out.append(reading)
        state["since"] = cursor
        return out


# --------------------------------------------------------------------------
class CsvSource:
    """Source 2: follow the CSV that water-quality-logger's main.py writes.

    Use this when the logger is already running and owning the hardware. Two
    processes cannot both open the same I2C bus or RS485 port, so rather than
    sampling a second time, this tails the file the logger is already writing
    and publishes each new row. The logger needs no changes at all.

    Handles both of its formats:
        wide  timestamp, <sensor>_<channel>, ..., status
        long  timestamp, sensor, channel, value, unit
    """

    kind = "csv"

    def __init__(self, path: str, device_id: str, backfill: bool = False) -> None:
        self.path = path
        self.device_id = device_id
        self.backfill = backfill
        self.header: List[str] = []
        self.fmt = "wide"

    def describe(self) -> str:
        return f"{self.path} (csv)"

    # ---- header ----------------------------------------------------------
    def _read_header(self, fh: Any) -> None:
        fh.seek(0)
        first = fh.readline()
        if not first:
            self.header = []
            return
        self.header = next(csv.reader([first.rstrip("\n")]))
        lowered = [h.strip().lower() for h in self.header]
        self.fmt = "long" if lowered[:4] == ["timestamp", "sensor", "channel", "value"] \
            else "wide"

    # ---- rows ------------------------------------------------------------
    def _rows_to_readings(self, rows: List[List[str]], seq: int) -> Tuple[List[Dict[str, Any]], int]:
        out: List[Dict[str, Any]] = []
        if self.fmt == "long":
            # Group consecutive rows sharing a timestamp+sensor into one reading.
            grouped: Dict[Tuple[str, str], Dict[str, float]] = {}
            order: List[Tuple[str, str]] = []
            for row in rows:
                if len(row) < 4:
                    continue
                ts_text, sensor, channel, value = row[0], row[1], row[2], row[3]
                try:
                    number = float(value)
                except ValueError:
                    continue
                key = (ts_text, sensor)
                if key not in grouped:
                    grouped[key] = {}
                    order.append(key)
                grouped[key][channel] = number
            for ts_text, sensor in order:
                seq += 1
                out.append(self._reading(seq, sensor, ts_text, grouped[(ts_text, sensor)]))
            return out, seq

        # wide
        for row in rows:
            if not row or len(row) < 2:
                continue
            ts_text = row[0]
            by_sensor: Dict[str, Dict[str, float]] = {}
            for name, cell in zip(self.header[1:], row[1:]):
                low = name.strip().lower()
                if low in ("status", "") or cell == "":
                    continue
                try:
                    number = float(cell)
                except ValueError:
                    continue
                sensor, _, channel = name.partition("_")
                by_sensor.setdefault(sensor or name, {})[channel or name] = number
            for sensor, metrics in by_sensor.items():
                seq += 1
                out.append(self._reading(seq, sensor, ts_text, metrics))
        return out, seq

    def _reading(self, seq: int, source: str, ts_text: str,
                 metrics: Dict[str, float]) -> Dict[str, Any]:
        ts = _parse_time(ts_text)
        return {
            "schema": wq_mqtt.SCHEMA,
            "seq": seq,
            "device_id": self.device_id,
            "source": source,
            "ts": round(ts, 3),
            "ts_iso": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
            "metrics": metrics,
        }

    # ---- polling ---------------------------------------------------------
    def poll(self, state: Dict[str, Any]) -> List[Dict[str, Any]]:
        try:
            stat = os.stat(self.path)
        except FileNotFoundError:
            return []

        offset = int(state.get("offset", -1))
        known_inode = state.get("inode")
        seq = int(state.get("seq", 0))

        with open(self.path, "r", encoding="utf-8", errors="replace", newline="") as fh:
            self._read_header(fh)
            if not self.header:
                return []

            fresh = offset < 0
            rotated = known_inode is not None and known_inode != stat.st_ino
            truncated = offset > stat.st_size
            if rotated or truncated:
                log(f"{self.path} was rotated or truncated - following the new file")
                offset = fh.tell()           # just past the header
            elif fresh:
                # Start from the end, so a long existing log is not republished.
                offset = fh.tell() if self.backfill else stat.st_size

            fh.seek(offset)
            chunk = fh.read()
            consumed = offset + len(chunk)

        # Only whole lines; a partial last line waits for the next poll.
        if "\n" not in chunk:
            state.update(offset=offset, inode=stat.st_ino, seq=seq)
            return []
        complete, _, remainder = chunk.rpartition("\n")
        consumed -= len(remainder)

        rows = [r for r in csv.reader(complete.splitlines()) if r]
        rows = [r for r in rows if r[0].strip().lower() != "timestamp"]
        readings, seq = self._rows_to_readings(rows, seq)
        state.update(offset=consumed, inode=stat.st_ino, seq=seq)
        return readings


_TIME_FORMATS = (
    "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S", "%d-%m-%Y %H:%M:%S",
)


def _parse_time(text: str) -> float:
    text = text.strip()
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        pass
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    try:
        return float(text)          # already an epoch
    except ValueError:
        return time.time()          # unparseable: stamp it as now


# --------------------------------------------------------------------------
def load_cursor(device_id: str) -> Dict[str, Any]:
    """Where this uplink had got to, so a restart does not re-send everything."""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            saved = json.load(fh).get(device_id, {})
    except Exception:  # noqa: BLE001 - no state yet, or it is corrupt
        return {}
    if isinstance(saved, int):          # written by an older version
        return {"since": saved}
    return dict(saved) if isinstance(saved, dict) else {}


def save_cursor(device_id: str, cursor: Dict[str, Any]) -> None:
    state: Dict[str, Any] = {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            state = json.load(fh)
    except Exception:  # noqa: BLE001
        pass
    state[device_id] = cursor
    tmp = STATE_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, STATE_FILE)
    except OSError as exc:
        log(f"! could not save the cursor: {exc}")


# --------------------------------------------------------------------------
class Uplink:
    def __init__(self, cfg: wq_mqtt.MqttConfig, server: LocalServer,
                 batch_seconds: float, max_batch: int, heartbeat: float) -> None:
        self.cfg = cfg
        self.server = server
        self.batch_seconds = batch_seconds
        self.max_batch = max_batch
        self.heartbeat = heartbeat
        self.stop = threading.Event()
        self.connected = threading.Event()
        self.sent = 0
        self.messages = 0

        client_id = f"wq-pi-{cfg.device_id}-{socket.gethostname()[:12]}"
        self.client, _ = wq_mqtt.make_client(cfg, client_id, clean_session=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        # If this process dies or the Pi drops off, the broker publishes this
        # for us, so the dashboard shows the Pi as offline instead of just
        # going quiet.
        self.client.will_set(
            cfg.status_topic(),
            json.dumps({"schema": wq_mqtt.SCHEMA, "device_id": cfg.device_id,
                        "state": "offline", "reason": "last will"}),
            qos=1, retain=True)

    # ---- callbacks -------------------------------------------------------
    def _on_connect(self, client: Any, userdata: Any, flags: Any, rc: int) -> None:
        if rc == 0:
            self.connected.set()
            log(f"connected to {self.cfg.describe()}")
            self._publish_status("online")
        else:
            self.connected.clear()
            log(f"! {wq_mqtt.explain_rc(rc)}")

    def _on_disconnect(self, client: Any, userdata: Any, rc: int, *a: Any) -> None:
        self.connected.clear()
        if not self.stop.is_set():
            log("! disconnected from the broker - retrying")

    # ---- publishing ------------------------------------------------------
    def _publish_status(self, state: str, extra: Optional[Dict[str, Any]] = None) -> None:
        payload = {
            "schema": wq_mqtt.SCHEMA,
            "device_id": self.cfg.device_id,
            "state": state,
            "now": round(time.time(), 3),      # lets the PC correct the Pi's clock
            "sent": self.sent,
        }
        if extra:
            payload.update(extra)
        self.client.publish(self.cfg.status_topic(),
                            json.dumps(payload, separators=(",", ":")),
                            qos=1, retain=True)

    def _publish_batch(self, readings: List[Dict[str, Any]]) -> bool:
        payload = {
            "schema": wq_mqtt.SCHEMA,
            "device_id": self.cfg.device_id,
            "sent_at": round(time.time(), 3),
            "count": len(readings),
            "readings": readings,
        }
        info = self.client.publish(self.cfg.data_topic(),
                                   json.dumps(payload, separators=(",", ":")),
                                   qos=1, retain=False)
        try:
            info.wait_for_publish(timeout=30)
        except Exception:  # noqa: BLE001 - paho 1.x raises if not connected
            return False
        return bool(info.is_published())

    # ---- main loop -------------------------------------------------------
    def run(self) -> int:
        self.cfg.check()
        log(f"device_id={self.cfg.device_id}  source={self.server.describe()}  "
            f"batch={self.batch_seconds:.0f}s")
        try:
            self.client.connect_async(self.cfg.host, self.cfg.port, keepalive=60)
        except Exception as exc:  # noqa: BLE001
            log(f"! {type(exc).__name__}: {exc}")
            return 1
        self.client.loop_start()

        cursor = load_cursor(self.cfg.device_id)
        pending: List[Dict[str, Any]] = []
        last_send = time.time()
        last_beat = 0.0
        warned_local = False

        while not self.stop.is_set():
            # 1. collect whatever the source has produced since last time
            try:
                pending.extend(self.server.poll(cursor))
                warned_local = False
            except Exception as exc:  # noqa: BLE001
                if not warned_local:
                    warned_local = True
                    log(f"! cannot read {self.server.describe()}: "
                        f"{type(exc).__name__}: {exc}")
                    if self.server.kind == "http":
                        log("  is pi_server.py running? readings are still being spooled")
                    else:
                        log("  is the logger running and writing to that file?")

            # 2. send when the batch is full or the timer is up
            due = (time.time() - last_send) >= self.batch_seconds
            if pending and (due or len(pending) >= self.max_batch):
                if self.connected.is_set():
                    chunk = pending[:self.max_batch]
                    if self._publish_batch(chunk):
                        self.sent += len(chunk)
                        self.messages += 1
                        pending = pending[len(chunk):]
                        last_send = time.time()
                        save_cursor(self.cfg.device_id, cursor)
                        log(f"sent {len(chunk)} reading(s)  "
                            f"[{self.sent} total, {len(pending)} waiting]")
                    else:
                        log("! publish not confirmed - will retry")
                        last_send = time.time()
                else:
                    # Not connected: hold on to them. pi_server's ring buffer and
                    # spool file are the real safety net, this is just the queue.
                    if len(pending) > 20000:
                        dropped = len(pending) - 20000
                        pending = pending[-20000:]
                        log(f"! offline too long - dropped {dropped} oldest from the "
                            f"send queue (they are still in the Pi's spool file)")

            # 3. heartbeat, so the dashboard knows the Pi is alive between batches
            if self.connected.is_set() and time.time() - last_beat >= self.heartbeat:
                last_beat = time.time()
                self._publish_status("online", {"queued": len(pending), "seq": cursor})

            self.stop.wait(1.0)

        log("stopping")
        if self.connected.is_set():
            self._publish_status("offline", {"reason": "clean shutdown"})
            time.sleep(0.5)
        self.client.loop_stop()
        try:
            self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        return 0

    # ---- one-shot check --------------------------------------------------
    def test(self) -> int:
        self.cfg.check()
        log(f"connecting to {self.cfg.describe()} ...")
        try:
            self.client.connect(self.cfg.host, self.cfg.port, keepalive=30)
        except Exception as exc:  # noqa: BLE001
            log(f"FAILED: {type(exc).__name__}: {exc}")
            log("  if this is a timeout, the network may block this port - "
                "try --websockets")
            return 1
        self.client.loop_start()
        if not self.connected.wait(timeout=20):
            log("FAILED: connected to the port but the broker did not accept us")
            self.client.loop_stop()
            return 1

        try:
            if self.server.kind == "http":
                readings = list(self.server.get("/latest").get("latest", {}).values())
            else:
                was = self.server.backfill
                self.server.backfill = True          # so --test sees existing rows
                try:
                    readings = self.server.poll({})[-5:]
                finally:
                    self.server.backfill = was
            if not readings:
                raise RuntimeError("the source has produced nothing yet")
        except Exception as exc:  # noqa: BLE001
            log(f"! no local server ({type(exc).__name__}) - sending a marker instead")
            readings = [{
                "schema": wq_mqtt.SCHEMA, "seq": 0, "device_id": self.cfg.device_id,
                "source": "uplink_test", "ts": round(time.time(), 3),
                "ts_iso": datetime.now().astimezone().isoformat(),
                "metrics": {"test": 1.0},
            }]
        ok = self._publish_batch(readings) if readings else False
        log(f"published {len(readings)} reading(s): {'ok' if ok else 'NOT confirmed'}")
        log(f"topic: {self.cfg.data_topic()}")
        log("on the PC:  python3 pc_dashboard.py --mqtt")
        self._publish_status("offline", {"reason": "test finished"})
        time.sleep(0.5)
        self.stop.set()            # so the disconnect below is not reported as a fault
        self.client.loop_stop()
        self.client.disconnect()
        return 0 if ok else 1


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Forward readings from the local sensor server to an MQTT broker.")
    ap.add_argument("--from", dest="source", default="http://127.0.0.1:8000",
                    help="the local pi_server.py (default http://127.0.0.1:8000)")
    ap.add_argument("--token", default=os.environ.get("WQ_TOKEN"),
                    help="pi_server.py's --token, if it has one")
    ap.add_argument("--batch", type=float, default=60.0,
                    help="seconds between messages; bigger saves battery (default 60)")
    ap.add_argument("--max-batch", type=int, default=200,
                    help="most readings in one message (default 200)")
    ap.add_argument("--heartbeat", type=float, default=30.0,
                    help="seconds between status messages (default 30)")
    ap.add_argument("--from-csv", dest="from_csv", metavar="FILE",
                    help="instead of the HTTP server, follow a CSV that another "
                         "logger is writing (water-quality-logger's main.py, say). "
                         "Use this when that logger already owns the hardware -- "
                         "two processes cannot share an I2C bus or a serial port.")
    ap.add_argument("--backfill-csv", action="store_true",
                    help="with --from-csv, also publish rows already in the file "
                         "(default: start from the end)")
    ap.add_argument("--test", action="store_true",
                    help="connect, publish the latest readings once, exit")
    wq_mqtt.add_arguments(ap, side="pi")
    args = ap.parse_args()

    cfg = wq_mqtt.config_from_args(args)
    server: Any
    if args.from_csv:
        server = CsvSource(args.from_csv, cfg.device_id, backfill=args.backfill_csv)
    else:
        server = LocalServer(args.source, args.token)
    uplink = Uplink(cfg, server, args.batch, args.max_batch, args.heartbeat)

    if args.test:
        return uplink.test()

    def shutdown(signum: int, frame: Any) -> None:
        uplink.stop.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    return uplink.run()


if __name__ == "__main__":
    sys.exit(main())
