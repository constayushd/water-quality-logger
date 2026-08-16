"""Minimal Modbus RTU master over pyserial, with shared-bus support.

Two sensors configured on the same serial port share a single Serial handle
and a lock, so their transactions can't interleave and corrupt each other.
Sensors on different ports get independent handles and run without contention.
"""

from __future__ import annotations

import threading
import time

import serial

_BUSES: dict[str, "SerialBus"] = {}
_BUSES_LOCK = threading.Lock()


def get_bus(port: str, baudrate: int = 4800, timeout: float = 2.0,
            parity: str = "N", stopbits: int = 1, bytesize: int = 8) -> "SerialBus":
    """Return the shared bus for `port`, creating it on first use."""
    with _BUSES_LOCK:
        bus = _BUSES.get(port)
        if bus is None:
            bus = SerialBus(port, baudrate, timeout, parity, stopbits, bytesize)
            _BUSES[port] = bus
        elif bus.baudrate != baudrate:
            raise ValueError(
                f"port {port} is already open at {bus.baudrate} baud; "
                f"cannot also use it at {baudrate} baud"
            )
        return bus


def close_all_buses() -> None:
    with _BUSES_LOCK:
        for bus in _BUSES.values():
            bus.close()
        _BUSES.clear()


def modbus_crc(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


class ModbusError(IOError):
    pass


class SerialBus:
    def __init__(self, port, baudrate, timeout, parity, stopbits, bytesize):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._parity = parity
        self._stopbits = stopbits
        self._bytesize = bytesize
        self._ser: serial.Serial | None = None
        self._lock = threading.RLock()
        self._users = 0
        # 3.5 character times is the Modbus RTU inter-frame gap; at low baud
        # rates this matters, and below 19200 the spec fixes it at ~1.75 ms.
        self._gap = max(0.00175, 3.5 * 11.0 / baudrate)
        self._last_activity = 0.0

    def acquire(self) -> None:
        """Open the port if needed and register one more user."""
        with self._lock:
            if self._ser is None:
                self._ser = serial.Serial(
                    port=self.port,
                    baudrate=self.baudrate,
                    bytesize=self._bytesize,
                    parity=self._parity,
                    stopbits=self._stopbits,
                    timeout=self.timeout,
                )
            self._users += 1

    def release(self) -> None:
        with self._lock:
            self._users = max(0, self._users - 1)
            if self._users == 0:
                self.close()

    def close(self) -> None:
        with self._lock:
            if self._ser is not None:
                try:
                    self._ser.close()
                except Exception:
                    pass
                self._ser = None

    def read_holding_registers(self, slave_id: int, address: int, count: int) -> bytes:
        """Function 0x03. Returns the raw data payload (2 bytes per register)."""
        request = bytes([
            slave_id,
            0x03,
            (address >> 8) & 0xFF,
            address & 0xFF,
            (count >> 8) & 0xFF,
            count & 0xFF,
        ])
        crc = modbus_crc(request)
        request += bytes([crc & 0xFF, (crc >> 8) & 0xFF])

        with self._lock:
            if self._ser is None:
                raise ModbusError(f"{self.port} is not open")

            # Respect the inter-frame silence before driving the line.
            idle = time.monotonic() - self._last_activity
            if idle < self._gap:
                time.sleep(self._gap - idle)

            self._ser.reset_input_buffer()
            self._ser.write(request)
            self._ser.flush()

            expected = 5 + count * 2
            response = self._ser.read(expected)
            self._last_activity = time.monotonic()

        if len(response) < 5:
            raise ModbusError(
                f"slave {slave_id}: no/short response ({response.hex() or 'empty'})"
            )
        if response[0] != slave_id:
            raise ModbusError(
                f"slave {slave_id}: reply came from address {response[0]} "
                f"({response.hex()}) -- address collision on the bus?"
            )
        if response[1] & 0x80:
            raise ModbusError(f"slave {slave_id}: exception response ({response.hex()})")
        if modbus_crc(response[:-2]) != (response[-1] << 8 | response[-2]):
            raise ModbusError(f"slave {slave_id}: CRC mismatch ({response.hex()})")

        return response[3:-2]
