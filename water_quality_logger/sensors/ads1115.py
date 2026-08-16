"""Minimal ADS1115 16-bit ADC driver over I2C (smbus2 only, no Blinka).

Single-shot conversions, one channel at a time. Shared between any drivers
that need analog inputs -- pH now, ORP or an analog EC probe later.
"""

from __future__ import annotations

import threading
import time

try:
    from smbus2 import SMBus
except ImportError:  # pragma: no cover - clearer message than a raw traceback
    SMBus = None

_REG_CONVERSION = 0x00
_REG_CONFIG = 0x01

# Programmable gain amplifier: full-scale volts -> config bits
_PGA = {6.144: 0, 4.096: 1, 2.048: 2, 1.024: 3, 0.512: 4, 0.256: 5}
# Single-ended input multiplexer settings for AIN0..AIN3
_MUX = {0: 0b100, 1: 0b101, 2: 0b110, 3: 0b111}
# Samples per second -> config bits
_DATA_RATE = {8: 0, 16: 1, 32: 2, 64: 3, 128: 4, 250: 5, 475: 6, 860: 7}

_bus_lock = threading.Lock()
_open_buses: dict[int, "SMBus"] = {}


def _get_smbus(bus_number: int):
    if SMBus is None:
        raise RuntimeError(
            "smbus2 is not installed. Run: pip install smbus2 "
            "(and enable I2C with raspi-config)"
        )
    with _bus_lock:
        if bus_number not in _open_buses:
            _open_buses[bus_number] = SMBus(bus_number)
        return _open_buses[bus_number]


def close_all():
    with _bus_lock:
        for bus in _open_buses.values():
            try:
                bus.close()
            except Exception:
                pass
        _open_buses.clear()


class ADS1115:
    def __init__(self, bus: int = 1, address: int = 0x48,
                 gain: float = 4.096, data_rate: int = 128):
        if gain not in _PGA:
            raise ValueError(f"gain must be one of {sorted(_PGA)}")
        if data_rate not in _DATA_RATE:
            raise ValueError(f"data_rate must be one of {sorted(_DATA_RATE)}")
        self.bus_number = bus
        self.address = address
        self.gain = gain
        self.data_rate = data_rate
        self._lock = threading.Lock()

    def read_voltage(self, channel: int) -> float:
        """Single-ended conversion on AIN<channel>, returned in volts."""
        if channel not in _MUX:
            raise ValueError("channel must be 0-3")

        config = (
            (1 << 15)                          # start a single conversion
            | (_MUX[channel] << 12)
            | (_PGA[self.gain] << 9)
            | (1 << 8)                         # single-shot mode
            | (_DATA_RATE[self.data_rate] << 5)
            | 0x0003                           # comparator disabled
        )

        smbus = _get_smbus(self.bus_number)
        with self._lock:
            smbus.write_i2c_block_data(
                self.address, _REG_CONFIG, [(config >> 8) & 0xFF, config & 0xFF]
            )

            # Wait one conversion period, then poll the ready bit.
            time.sleep(1.0 / self.data_rate + 0.001)
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline:
                hi, lo = smbus.read_i2c_block_data(self.address, _REG_CONFIG, 2)
                if (hi << 8 | lo) & 0x8000:    # OS bit set == conversion done
                    break
                time.sleep(0.001)
            else:
                raise IOError(
                    f"ADS1115 at 0x{self.address:02x} did not finish a conversion"
                )

            hi, lo = smbus.read_i2c_block_data(self.address, _REG_CONVERSION, 2)

        raw = hi << 8 | lo
        if raw & 0x8000:                        # two's complement
            raw -= 1 << 16
        return raw * self.gain / 32768.0

    def read_voltage_median(self, channel: int, samples: int = 5) -> float:
        """Median of several conversions -- rejects the odd spike from EMI."""
        readings = sorted(self.read_voltage(channel) for _ in range(max(1, samples)))
        return readings[len(readings) // 2]
