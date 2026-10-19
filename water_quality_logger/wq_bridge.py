#!/usr/bin/env python3
"""
wq_bridge.py -- lets pi_server.py drive the water-quality-logger sensor drivers.

The logger repo already has the better sensor layer: a driver registry, lazy
opening, per-driver calibration, RS485 and ADS1115 support. There is no reason
to have a second one. This module adapts it to the small interface pi_server.py
expects, so the network layer sits on top of the drivers you already have.

    pi_server.py  ──imports──►  wq_bridge  ──uses──►  sensors/  (the logger's
                                                                 driver package)

It reads the same config.yaml the logger uses, so there is one place that says
which probes exist and how they are wired.

    python3 wq_bridge.py                 # list what the config would start
    python3 wq_bridge.py --once          # read each sensor once and print it
    WQ_CONFIG=config.yaml python3 pi_server.py

Important: only one process may own the hardware. An I2C bus or an RS485 port
cannot be opened twice. If the logger's own main.py is running, do not also run
pi_server.py with this bridge -- use `pi_uplink.py --from-csv` instead, which
follows the CSV the logger is already writing.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterator, List, Optional

# --------------------------------------------------------------------------
# Same knobs pi_server.py expects from the standalone sensors.py
# --------------------------------------------------------------------------
SIMULATE = os.environ.get("WQ_SIMULATE", "").strip().lower() in ("1", "true", "yes")
EXCLUSIVE_SAMPLING = True
SETTLE_S = 0.3
LOCK_TIMEOUT_S = 10.0

_sample_lock = threading.Lock()


@contextlib.contextmanager
def exclusive() -> Iterator[None]:
    """One probe at a time. An RS485 bus shared by two sensors requires this;
    a pH electrode next to a current-driving probe benefits from it."""
    if not EXCLUSIVE_SAMPLING:
        yield
        return
    acquired = _sample_lock.acquire(timeout=LOCK_TIMEOUT_S)
    try:
        yield
        if acquired:
            time.sleep(SETTLE_S)
    finally:
        if acquired:
            _sample_lock.release()


@dataclass
class Sensor:
    """What pi_server.py iterates over."""
    name: str
    fn: Callable[[], Dict[str, float]]
    interval: float = 5.0


SENSORS: List[Sensor] = []


# --------------------------------------------------------------------------
class ManagedDriver:
    """Opens the driver on first use and reopens it after a failure.

    The same recovery behaviour as the logger's own ManagedSensor, so a probe
    that is unplugged and plugged back in starts working again without a
    restart.
    """

    def __init__(self, driver: Any, name: str) -> None:
        self.driver = driver
        self.name = name
        self.is_open = False

    def read(self) -> Dict[str, float]:
        try:
            if not self.is_open:
                opener = getattr(self.driver, "open", None)
                if callable(opener):
                    opener()
                self.is_open = True
            values = self.driver.read()
        except Exception:
            self.close()
            raise
        return {k: float(v) for k, v in dict(values).items() if v is not None}

    def close(self) -> None:
        self.is_open = False
        closer = getattr(self.driver, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001 - close must never raise
                pass


# --------------------------------------------------------------------------
def _load_yaml(path: str) -> Dict[str, Any]:
    try:
        import yaml
    except ImportError:
        raise SystemExit("PyYAML is not installed.   pip3 install pyyaml")
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load(config_path: Optional[str] = None,
         interval: Optional[float] = None,
         simulate: Optional[bool] = None) -> List[Sensor]:
    """Build SENSORS from the logger's config.yaml. Returns the list and also
    assigns it to the module-level SENSORS that pi_server.py reads."""
    global SENSORS, SIMULATE

    if simulate is not None:
        SIMULATE = simulate

    path = config_path or os.environ.get("WQ_CONFIG", "config.yaml")
    config = _load_yaml(path)

    default_interval = float(
        interval
        if interval is not None
        else (config.get("logging") or {}).get("interval_seconds", 5.0)
    )

    try:
        import sensors as drivers          # the logger's driver package
    except ImportError as exc:
        raise SystemExit(
            f"Cannot import the logger's sensor package ({exc}).\n"
            "  Run this from the directory that contains sensors/, or add that\n"
            "  directory to PYTHONPATH."
        )

    built: List[Sensor] = []
    for entry in config.get("sensors") or []:
        if not entry.get("enabled", False):
            continue
        name = entry.get("name") or entry.get("type")
        type_name = "simulated" if SIMULATE else entry.get("type")
        params = {k: v for k, v in entry.items()
                  if k not in ("name", "type", "enabled", "interval_seconds")}
        if SIMULATE:
            params = {}
        try:
            driver = drivers.create(type_name, name, params)
        except Exception as exc:  # noqa: BLE001
            print(f"! skipping {name}: cannot create '{type_name}': "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        managed = ManagedDriver(driver, name)
        built.append(Sensor(
            name=name,
            fn=managed.read,
            interval=float(entry.get("interval_seconds", default_interval)),
        ))

    if not built:
        raise SystemExit(
            f"No enabled sensors in {path}.\n"
            "  Set enabled: true on at least one entry, or run with --simulate."
        )

    SENSORS = built
    return built


# Build on import so `import wq_bridge as sensors` behaves like the standalone
# module. A failure here is not fatal: pi_server.py can call load() itself with
# an explicit path and report the problem properly.
if os.environ.get("WQ_BRIDGE_AUTOLOAD", "1") != "0":
    try:
        load()
    except SystemExit as exc:
        print(f"wq_bridge: {exc}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"wq_bridge: could not build sensors: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Check that the logger's drivers can be driven by pi_server.py.")
    ap.add_argument("-c", "--config", default=None, help="config.yaml (default: $WQ_CONFIG)")
    ap.add_argument("--once", action="store_true", help="read each sensor once")
    ap.add_argument("--loop", action="store_true", help="keep reading until Ctrl-C")
    ap.add_argument("--simulate", action="store_true", help="use the simulated driver")
    args = ap.parse_args()

    load(args.config, simulate=args.simulate or None)
    print(f"{len(SENSORS)} sensor(s) from "
          f"{args.config or os.environ.get('WQ_CONFIG', 'config.yaml')}:")
    for s in SENSORS:
        print(f"  {s.name:<12} every {s.interval:g}s")

    if not (args.once or args.loop):
        print("\nAdd --once to actually read them.")
        return 0

    try:
        while True:
            print(time.strftime("%H:%M:%S"))
            for s in SENSORS:
                try:
                    with exclusive():
                        values = s.fn()
                    print("  " + s.name.ljust(12)
                          + ", ".join(f"{k}={v}" for k, v in values.items()))
                except Exception as exc:  # noqa: BLE001
                    print(f"  {s.name:<12} FAILED: {type(exc).__name__}: {exc}")
            if not args.loop:
                break
            time.sleep(min(s.interval for s in SENSORS))
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
