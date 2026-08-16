"""CSV output, in either of two shapes.

wide  -- one row per cycle, one column per channel. Easiest to plot and to
         open in Excel. Its weakness: the header changes whenever you add a
         sensor, so the writer rotates the old file out of the way instead of
         appending rows with a mismatched shape.

long  -- one row per reading: timestamp, sensor, channel, value, unit. The
         header never changes, so a single file survives you adding sensors
         forever. Pivot it in pandas when you want the wide view.
"""

from __future__ import annotations

import csv
import os
import shutil
from datetime import datetime

LONG_HEADER = ["timestamp", "sensor", "channel", "value", "unit"]


class CsvLogger:
    def __init__(self, path: str, sensors, fmt: str = "wide"):
        if fmt not in ("wide", "long"):
            raise ValueError("format must be 'wide' or 'long'")
        self.path = path
        self.fmt = fmt
        self.sensors = sensors
        self.header = self._build_header()
        self._fh = None
        self._writer = None

    def _build_header(self):
        if self.fmt == "long":
            return list(LONG_HEADER)
        header = ["timestamp"]
        for sensor in self.sensors:
            header.extend(sensor.columns)
        header.append("status")
        return header

    def _rotate_if_schema_changed(self):
        """If an existing file has a different header, move it aside."""
        if not os.path.exists(self.path) or os.path.getsize(self.path) == 0:
            return
        with open(self.path, newline="") as fh:
            existing = next(csv.reader(fh), None)
        if existing == self.header:
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        root, ext = os.path.splitext(self.path)
        archived = f"{root}.{stamp}{ext or '.csv'}"
        shutil.move(self.path, archived)
        print(
            f"[csv] sensor set changed since the last run; "
            f"previous data preserved as {archived}"
        )

    def open(self):
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        self._rotate_if_schema_changed()
        is_new = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
        self._fh = open(self.path, "a", newline="")
        self._writer = csv.writer(self._fh)
        if is_new:
            self._writer.writerow(self.header)
            self._fh.flush()
        if self.fmt == "wide":
            self._write_units_sidecar()

    def _write_units_sidecar(self):
        """Units live beside the CSV so the header stays machine-friendly."""
        root, _ = os.path.splitext(self.path)
        with open(f"{root}_units.csv", "w", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(["column", "unit"])
            for sensor in self.sensors:
                for channel in sensor.CHANNELS:
                    writer.writerow([f"{sensor.name}_{channel.key}", channel.unit])

    def write_cycle(self, timestamp: str, results: dict, status: str):
        """results maps sensor name -> reading dict (or None if the read failed)."""
        if self.fmt == "wide":
            row = [timestamp]
            for sensor in self.sensors:
                row.extend(sensor.format_row(results.get(sensor.name)))
            row.append(status)
            self._writer.writerow(row)
        else:
            for sensor in self.sensors:
                values = results.get(sensor.name)
                if values is None:
                    continue
                for channel in sensor.CHANNELS:
                    value = values.get(channel.key)
                    if value is None:
                        continue
                    self._writer.writerow([
                        timestamp,
                        sensor.name,
                        channel.key,
                        f"{value:.{channel.decimals}f}",
                        channel.unit,
                    ])
        self._fh.flush()   # a yanked cable shouldn't cost buffered rows

    def close(self):
        if self._fh is not None:
            self._fh.close()
            self._fh = None
