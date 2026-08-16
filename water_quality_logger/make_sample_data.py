#!/usr/bin/env python3
"""Generate a sample CSV with the exact columns the real logger produces.

Useful for building plots and analysis scripts before the hardware is wired,
or for testing changes to the CSV format without a Pi.

    python make_sample_data.py                     # 30 minutes of readings
    python make_sample_data.py --rows 500 --format long

The numbers are invented but plausible: dissolved oxygen falling slowly as a
tank warms, turbidity spiking when something is disturbed, pH drifting a
little. Two turbidity dropouts are included so you can see how failures
appear in the file.
"""

from __future__ import annotations

import argparse
import math
import random
from datetime import datetime, timedelta

from csvlog import CsvLogger
from sensors.do_rs485 import DissolvedOxygenRS485
from sensors.ph_analog import PhAnalog
from sensors.turbidity_rs485 import TurbidityRS485


class _Stub:
    """Mimics a configured sensor closely enough for CsvLogger's schema."""

    def __init__(self, name, real_class):
        self.name = name
        self.CHANNELS = real_class.CHANNELS
        self.TYPE_NAME = real_class.TYPE_NAME
        self._real = real_class

    @property
    def columns(self):
        return [f"{self.name}_{ch.key}" for ch in self.CHANNELS]

    def format_row(self, values):
        cells = []
        for ch in self.CHANNELS:
            value = None if values is None else values.get(ch.key)
            cells.append("" if value is None else f"{value:.{ch.decimals}f}")
        return cells


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-o", "--output", default="data/sample_sensor_log.csv")
    parser.add_argument("-n", "--rows", type=int, default=900)
    parser.add_argument("-i", "--interval", type=float, default=2.0)
    parser.add_argument("-f", "--format", choices=("wide", "long"), default="wide")
    args = parser.parse_args()

    sensors = [
        _Stub("do", DissolvedOxygenRS485),
        _Stub("turbidity", TurbidityRS485),
        _Stub("ph", PhAnalog),
    ]

    logger = CsvLogger(args.output, sensors, fmt=args.format)
    logger.open()

    start = datetime.now() - timedelta(seconds=args.rows * args.interval)
    random.seed(20260814)

    # Two stretches where the turbidity sensor drops off the bus.
    dropouts = set(range(int(args.rows * 0.30), int(args.rows * 0.30) + 4))
    dropouts |= set(range(int(args.rows * 0.72), int(args.rows * 0.72) + 7))

    for i in range(args.rows):
        timestamp = (start + timedelta(seconds=i * args.interval)) \
            .isoformat(timespec="seconds")
        progress = i / max(1, args.rows - 1)

        # Water warms over the run; DO falls as it does.
        temperature = 24.0 + 2.4 * progress + random.gauss(0, 0.04)
        oxygen = 8.6 - 1.1 * progress + 0.12 * math.sin(i / 40) + random.gauss(0, 0.03)
        saturation = oxygen / (14.6 - 0.36 * temperature + 0.004 * temperature ** 2) * 100

        # Turbidity sits low with an occasional disturbance.
        turbidity = 1.3 + 0.4 * math.sin(i / 55) + random.gauss(0, 0.08)
        if 0.55 < progress < 0.60:
            turbidity += 14 * math.exp(-((progress - 0.565) * 180) ** 2)

        # pH drifts slightly downward as CO2 accumulates.
        ph_value = 7.42 - 0.28 * progress + 0.02 * math.sin(i / 30) + random.gauss(0, 0.008)
        volts = (ph_value - 7.0) / -5.7 + 1.248     # inverse of a typical calibration

        results = {
            "do": {"mg_l": oxygen, "saturation_pct": saturation, "temp_c": temperature},
            "turbidity": None if i in dropouts else {
                "ntu": max(0.0, turbidity),
                "temp_c": temperature + random.gauss(0, 0.06),
            },
            "ph": {"ph": ph_value, "volts": volts},
        }
        status = "ok" if i not in dropouts else \
            "turbidity: no/short response (empty)"

        logger.write_cycle(timestamp, results, status)

    logger.close()
    print(f"Wrote {args.rows} rows to {args.output}")


if __name__ == "__main__":
    main()
