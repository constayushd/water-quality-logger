#!/usr/bin/env python3
"""Poll every sensor listed in config.yaml and log them to one CSV.

    python main.py                       # run until Ctrl+C
    python main.py --duration 600        # run for 10 minutes
    python main.py --once                # single reading, useful for testing
    python main.py --format long         # tidy output instead of wide
    python main.py --list-types          # show available driver types
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime

import yaml

import sensors
from csvlog import CsvLogger
from sensors import ads1115, modbus


class ManagedSensor:
    """Wraps a driver with lazy open and automatic recovery after a failure."""

    def __init__(self, sensor):
        self.sensor = sensor
        self.is_open = False

    @property
    def name(self):
        return self.sensor.name

    def read(self):
        if not self.is_open:
            self.sensor.open()
            self.is_open = True
        return self.sensor.read()

    def mark_failed(self):
        """Drop the connection so the next cycle reopens it from scratch."""
        if self.is_open:
            try:
                self.sensor.close()
            except Exception:
                pass
            self.is_open = False

    def close(self):
        self.mark_failed()


def build_sensors(config):
    built = []
    for entry in config.get("sensors", []):
        if not entry.get("enabled", True):
            continue
        name = entry["name"]
        type_name = entry["type"]
        params = {k: v for k, v in entry.items()
                  if k not in ("name", "type", "enabled")}
        built.append(ManagedSensor(sensors.create(type_name, name, params)))
    if not built:
        sys.exit("No enabled sensors in the config. Nothing to log.")
    return built


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.add_argument("-o", "--output", help="CSV path (overrides config)")
    parser.add_argument("-i", "--interval", type=float,
                        help="seconds between cycles (overrides config)")
    parser.add_argument("-d", "--duration", type=float,
                        help="stop after this many seconds")
    parser.add_argument("-f", "--format", choices=("wide", "long"),
                        help="CSV shape (overrides config)")
    parser.add_argument("--once", action="store_true",
                        help="take a single reading and exit")
    parser.add_argument("--list-types", action="store_true",
                        help="list registered sensor types and exit")
    return parser.parse_args()


def main():
    args = parse_args()

    if args.list_types:
        print("Available sensor types:")
        for type_name in sensors.known_types():
            print(f"  {type_name}")
        return

    try:
        with open(args.config) as fh:
            config = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        message = f"Config file not found: {args.config}"
        if args.config == "config.yaml" and os.path.exists("config.example.yaml"):
            message += ("\n\nconfig.yaml is machine-specific and not in git. Create yours:"
                        "\n    cp config.example.yaml config.yaml"
                        "\n    python check_hardware.py    # shows your real device paths"
                        "\n    nano config.yaml")
        sys.exit(message)

    logging_config = config.get("logging", {})
    output = args.output or logging_config.get("output", "data/sensor_log.csv")
    interval = args.interval or float(logging_config.get("interval_seconds", 2.0))
    fmt = args.format or logging_config.get("format", "wide")

    managed = build_sensors(config)
    logger = CsvLogger(output, [m.sensor for m in managed], fmt=fmt)
    logger.open()

    print(f"Logging {len(managed)} sensor(s) to {output} every {interval:g}s "
          f"({fmt} format). Ctrl+C to stop.")
    for m in managed:
        print(f"  - {m.name} ({m.sensor.TYPE_NAME})")

    started = time.monotonic()
    try:
        while True:
            cycle_start = time.monotonic()
            timestamp = datetime.now().isoformat(timespec="seconds")
            results, errors = {}, []

            for m in managed:
                try:
                    results[m.name] = m.read()
                except Exception as exc:
                    results[m.name] = None
                    errors.append(f"{m.name}: {exc}")
                    m.mark_failed()

            status = "; ".join(errors) if errors else "ok"
            logger.write_cycle(timestamp, results, status)

            parts = []
            for m in managed:
                values = results.get(m.name)
                if values is None:
                    parts.append(f"{m.name}=FAIL")
                    continue
                shown = ", ".join(
                    f"{ch.key}={values[ch.key]:.{ch.decimals}f}"
                    for ch in m.sensor.CHANNELS
                    if values.get(ch.key) is not None
                )
                parts.append(f"{m.name}[{shown}]")
            print(f"{timestamp} | " + " | ".join(parts))
            for error in errors:
                print(f"    ! {error}")

            if args.once:
                break
            if args.duration and (time.monotonic() - started) >= args.duration:
                break

            time.sleep(max(0.0, interval - (time.monotonic() - cycle_start)))

    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        for m in managed:
            m.close()
        modbus.close_all_buses()
        ads1115.close_all()
        logger.close()
        print(f"Data written to {output}")


if __name__ == "__main__":
    main()
