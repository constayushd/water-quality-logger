"""Analog pH probe (PH-4502C board) read through an ADS1115 ADC.

The board outputs a voltage on its Po pin. Converting that to pH needs a
two-point calibration, because the two trimpots on the board set an arbitrary
offset and gain -- no two boards agree, and the numbers drift as the probe
ages. Run calibrate_ph.py to produce the calibration file.

The raw voltage is logged in its own column whether or not a calibration
exists, so a bad or missing calibration never loses you the underlying data:
you can always recompute pH from the CSV afterwards.
"""

from __future__ import annotations

import json
import os

from .base import Channel, Sensor, register
from .ads1115 import ADS1115


@register("ph_analog")
class PhAnalog(Sensor):
    CHANNELS = (
        Channel("ph", "pH", 2),
        Channel("volts", "V", 4),
    )

    def __init__(self, name, i2c_bus=1, i2c_address=0x48, channel=0,
                 gain=4.096, samples=5, calibration_file="ph_calibration.json",
                 temp_source=None, **kwargs):
        super().__init__(name, **kwargs)
        self.channel = channel
        self.samples = samples
        self.calibration_file = calibration_file
        # Name of another sensor's channel to use for temperature compensation
        # later, e.g. "do_temp_c". Recorded for reference; not applied here.
        self.temp_source = temp_source
        self._adc = ADS1115(bus=i2c_bus, address=int(i2c_address), gain=gain)
        self._slope: float | None = None
        self._intercept: float | None = None
        self._warned = False

    def open(self):
        self._load_calibration()
        self._adc.read_voltage(self.channel)   # fail fast if I2C is misconfigured

    def _load_calibration(self):
        if not os.path.exists(self.calibration_file):
            if not self._warned:
                print(
                    f"[{self.name}] no calibration at {self.calibration_file} -- "
                    "logging raw volts only. Run: python calibrate_ph.py"
                )
                self._warned = True
            return
        with open(self.calibration_file) as fh:
            cal = json.load(fh)
        self._slope = float(cal["slope"])
        self._intercept = float(cal["intercept"])
        print(
            f"[{self.name}] calibration loaded: "
            f"pH = {self._slope:.4f} * V + {self._intercept:.4f}"
        )

    def read(self):
        volts = self._adc.read_voltage_median(self.channel, self.samples)
        ph = None
        if self._slope is not None:
            ph = self._slope * volts + self._intercept
        return {"volts": volts, "ph": ph}
