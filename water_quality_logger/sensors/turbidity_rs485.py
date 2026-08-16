"""RS-ZD-N01 style turbidity transmitter, Modbus RTU.

Registers 0x0000..0x0001 hold two signed 16-bit integers, each scaled 10x:
    0  turbidity (NTU * 10)
    1  temperature (degC * 10)
"""

from __future__ import annotations

import struct

from .base import Channel, Sensor, register
from . import modbus


@register("turbidity_rs485")
class TurbidityRS485(Sensor):
    CHANNELS = (
        Channel("ntu", "NTU", 1),
        Channel("temp_c", "degC", 1),
    )

    def __init__(self, name, port="/dev/ttyUSB1", slave_id=1, baudrate=4800,
                 timeout=2.0, scale=10.0, **kwargs):
        super().__init__(name, **kwargs)
        self.slave_id = slave_id
        self.scale = scale
        self._bus = modbus.get_bus(port, baudrate=baudrate, timeout=timeout)

    def open(self):
        self._bus.acquire()

    def close(self):
        self._bus.release()

    def read(self):
        data = self._bus.read_holding_registers(self.slave_id, 0x0000, 2)
        turbidity, temperature = struct.unpack(">hh", data)
        return {
            "ntu": turbidity / self.scale,
            "temp_c": temperature / self.scale,
        }
