"""A fake sensor that invents plausible numbers.

Two uses:

1. Test the logger, the CSV shape, and your analysis scripts on a laptop with
   no hardware attached. Set `fail_every` to prove your failure handling works.

2. Copy this file as the starting point for a real driver. A driver is just a
   class with CHANNELS and a read() method -- everything else (CSV columns,
   error recovery, config wiring) is handled for you.
"""

from __future__ import annotations

import math
import random
import time

from .base import Channel, Sensor, register


@register("simulated")
class SimulatedSensor(Sensor):
    # One Channel per value you produce. The key becomes the CSV column
    # suffix, so this sensor named "test" yields test_value and test_temp_c.
    CHANNELS = (
        Channel("value", "arb", 3),
        Channel("temp_c", "degC", 1),
    )

    def __init__(self, name, centre=7.0, swing=1.0, noise=0.02,
                 period_seconds=60.0, fail_every=0, **kwargs):
        super().__init__(name, **kwargs)
        self.centre = centre
        self.swing = swing
        self.noise = noise
        self.period = period_seconds
        self.fail_every = fail_every
        self._count = 0

    def open(self):
        """Acquire hardware here in a real driver. Raise if unavailable."""

    def read(self):
        self._count += 1
        if self.fail_every and self._count % self.fail_every == 0:
            raise IOError("simulated dropout")
        phase = 2 * math.pi * (time.time() % self.period) / self.period
        return {
            "value": self.centre + self.swing * math.sin(phase)
                     + random.uniform(-self.noise, self.noise),
            "temp_c": 25.0 + 2.0 * math.sin(phase / 3),
        }

    def close(self):
        """Release hardware here. Must not raise."""
