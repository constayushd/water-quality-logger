"""Dissolved-oxygen transmitter, Modbus RTU, IEEE-754 float registers.

Registers 0x0000..0x0005 hold three big-endian 32-bit floats:
    0-1  saturation (%)
    2-3  dissolved oxygen (mg/L)
    4-5  temperature (degC)
"""

from __future__ import annotations

import struct

from .base import Channel, Sensor, register
from . import modbus


@register("do_rs485")
class DissolvedOxygenRS485(Sensor):
    CHANNELS = (
        Channel("mg_l", "mg/L", 2),
        Channel("saturation_pct", "%", 2),
        Channel("temp_c", "degC", 2),
    )

    def __init__(self, name, port="/dev/ttyUSB0", slave_id=1, baudrate=4800,
                 timeout=2.0, word_swap=False, **kwargs):
        super().__init__(name, **kwargs)
        self.slave_id = slave_id
        self.word_swap = word_swap
        self._bus = modbus.get_bus(port, baudrate=baudrate, timeout=timeout)

    def open(self):
        self._bus.acquire()

    def close(self):
        self._bus.release()

    def _f32(self, chunk: bytes) -> float:
        # Some transmitters store the low word first. word_swap: true fixes
        # readings that come out as nan or absurd exponents.
        if self.word_swap:
            chunk = chunk[2:4] + chunk[0:2]
        return struct.unpack(">f", chunk)[0]

    def read(self):
        data = self._bus.read_holding_registers(self.slave_id, 0x0000, 6)
        return {
            "saturation_pct": self._f32(data[0:4]),
            "mg_l": self._f32(data[4:8]),
            "temp_c": self._f32(data[8:12]),
        }
