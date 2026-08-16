#!/usr/bin/env python3
"""Two-point (or more) calibration for the analog pH probe.

You need buffer solutions -- pH 7.00 and pH 4.01 are the usual pair, and
adding pH 10.01 gives you a third point to check linearity against.

    python calibrate_ph.py

Rinse the probe in distilled water between buffers and let each reading
settle for 30-60 seconds before recording it. The result is a straight-line
fit, pH = slope * volts + intercept, saved to ph_calibration.json.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime

import yaml

from sensors.ads1115 import ADS1115, close_all


def load_ph_config(path="config.yaml"):
    """Pull the ADC settings out of config.yaml so both scripts agree."""
    defaults = {
        "i2c_bus": 1, "i2c_address": 0x48, "channel": 0, "gain": 4.096,
        "calibration_file": "ph_calibration.json",
    }
    try:
        with open(path) as fh:
            config = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return defaults
    for entry in config.get("sensors", []):
        if entry.get("type") == "ph_analog":
            defaults.update({k: v for k, v in entry.items()
                             if k in defaults})
            break
    return defaults


def linear_fit(points):
    """Least-squares fit of pH against volts. points = [(volts, ph), ...]"""
    n = len(points)
    mean_v = sum(v for v, _ in points) / n
    mean_p = sum(p for _, p in points) / n
    numerator = sum((v - mean_v) * (p - mean_p) for v, p in points)
    denominator = sum((v - mean_v) ** 2 for v, _ in points)
    if abs(denominator) < 1e-12:
        raise SystemExit(
            "All calibration voltages are identical. Check that the probe is "
            "connected to the board's BNC and that Po reaches the ADC."
        )
    slope = numerator / denominator
    intercept = mean_p - slope * mean_v
    return slope, intercept


def r_squared(points, slope, intercept):
    mean_p = sum(p for _, p in points) / len(points)
    ss_res = sum((p - (slope * v + intercept)) ** 2 for v, p in points)
    ss_tot = sum((p - mean_p) ** 2 for _, p in points)
    return 1.0 if ss_tot == 0 else 1 - ss_res / ss_tot


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-c", "--config", default="config.yaml")
    parser.add_argument("-s", "--samples", type=int, default=15,
                        help="conversions to median per calibration point")
    args = parser.parse_args()

    settings = load_ph_config(args.config)
    adc = ADS1115(
        bus=settings["i2c_bus"],
        address=int(settings["i2c_address"]),
        gain=settings["gain"],
    )
    channel = settings["channel"]

    print("pH calibration")
    print("-" * 50)
    print("Rinse the probe between buffers and let each reading settle for")
    print("30-60 seconds. Enter a blank pH value when you're done.\n")

    points = []
    try:
        while True:
            label = f"point {len(points) + 1}"
            answer = input(f"Buffer pH for {label} (blank to finish): ").strip()
            if not answer:
                break
            try:
                ph_value = float(answer)
            except ValueError:
                print("  Enter a number, e.g. 7.00")
                continue

            input("  Probe in the buffer and settled? Press Enter to sample...")
            volts = adc.read_voltage_median(channel, args.samples)
            print(f"  pH {ph_value:.2f}  ->  {volts:.4f} V\n")
            points.append((volts, ph_value))

        if len(points) < 2:
            raise SystemExit("Need at least two points. Nothing saved.")

        slope, intercept = linear_fit(points)
        fit_quality = r_squared(points, slope, intercept)

        print("-" * 50)
        print(f"pH = {slope:.4f} * V + {intercept:.4f}")
        print(f"R^2 = {fit_quality:.5f}")
        if fit_quality < 0.99 and len(points) > 2:
            print("  Warning: poor linearity. The probe may be aged or fouled,")
            print("  or a buffer may be contaminated.")
        if slope > 0:
            print("  Note: positive slope means voltage rises with pH. That is")
            print("  normal for some PH-4502C wiring, just check pH 4 and pH 7")
            print("  come back the right way round when you run main.py.")

        calibration = {
            "slope": slope,
            "intercept": intercept,
            "r_squared": fit_quality,
            "points": [{"volts": v, "ph": p} for v, p in points],
            "calibrated_at": datetime.now().isoformat(timespec="seconds"),
            "adc": {
                "bus": settings["i2c_bus"],
                "address": int(settings["i2c_address"]),
                "channel": channel,
                "gain": settings["gain"],
            },
        }
        out_path = settings["calibration_file"]
        with open(out_path, "w") as fh:
            json.dump(calibration, fh, indent=2)
        print(f"\nSaved to {out_path}")
        print("Recalibrate every few weeks, or whenever readings look off.")

    except KeyboardInterrupt:
        print("\nCancelled. Nothing saved.")
    finally:
        close_all()


if __name__ == "__main__":
    main()
