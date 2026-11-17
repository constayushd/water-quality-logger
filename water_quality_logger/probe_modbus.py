#!/usr/bin/env python3
"""
probe_modbus.py -- dump raw Modbus registers and show every plausible reading
of them, so you can work out a transmitter's real register map instead of
guessing.

It speaks Modbus RTU directly over pyserial, so it does not depend on the
project's own driver code being right -- which is the point: when a driver
disagrees with reality, you want something underneath it.

    # who is actually on this bus?
    python3 probe_modbus.py --port /dev/serial/by-id/usb-FTDI... --scan-slaves

    # dump 12 registers and interpret them every way
    python3 probe_modbus.py --port /dev/serial/by-id/usb-FTDI... --slave 1 --count 12

    # some transmitters use input registers (function 4) not holding (3)
    python3 probe_modbus.py --port ... --slave 1 --count 12 --scan-fc

    # watch one register pair change as you move the probe
    python3 probe_modbus.py --port ... --slave 1 --count 6 --watch

Reading the output: a value is almost certainly a float32 when the big-endian
or word-swapped column shows a physically sensible number while the integer
columns show nonsense. A register pair whose first word barely changes while
the second jumps around is a float32 being misread as two integers.
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from typing import List, Optional, Tuple


# --------------------------------------------------------------------------
def crc16(data: bytes) -> int:
    """Modbus RTU CRC-16 (polynomial 0xA001, reflected)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def build_request(slave: int, fc: int, base: int, count: int) -> bytes:
    frame = struct.pack(">BBHH", slave, fc, base, count)
    return frame + struct.pack("<H", crc16(frame))


def parse_response(raw: bytes, slave: int, fc: int, count: int) -> List[int]:
    """Return the registers, or raise with a readable reason."""
    if len(raw) < 5:
        raise IOError(f"short reply ({len(raw)} bytes): {raw.hex(' ') or '(nothing)'}")
    if crc16(raw[:-2]) != struct.unpack("<H", raw[-2:])[0]:
        raise IOError(f"bad CRC -- wrong baud rate, or two devices answering: {raw.hex(' ')}")
    if raw[0] != slave:
        raise IOError(f"reply from slave {raw[0]}, not {slave}")
    if raw[1] & 0x80:
        code = raw[2]
        meaning = {
            1: "illegal function -- try the other --fc",
            2: "illegal data address -- that register does not exist here",
            3: "illegal data value -- asked for too many registers",
            4: "device failure",
        }.get(code, f"exception code {code}")
        raise IOError(f"the device refused: {meaning}")
    if raw[1] != fc:
        raise IOError(f"reply says function {raw[1]}, asked for {fc}")
    nbytes = raw[2]
    if nbytes != count * 2:
        raise IOError(f"expected {count * 2} data bytes, got {nbytes}")
    return list(struct.unpack(">" + "H" * count, raw[3:3 + nbytes]))


def read_registers(ser: object, slave: int, fc: int, base: int, count: int,
                   timeout: float = 1.0) -> List[int]:
    req = build_request(slave, fc, base, count)
    ser.reset_input_buffer()                      # type: ignore[attr-defined]
    ser.write(req)                                # type: ignore[attr-defined]
    ser.flush()                                   # type: ignore[attr-defined]
    expect = 5 + count * 2
    deadline = time.time() + timeout
    raw = b""
    while time.time() < deadline and len(raw) < expect:
        chunk = ser.read(expect - len(raw))       # type: ignore[attr-defined]
        if not chunk:
            break
        raw += chunk
    return parse_response(raw, slave, fc, count)


# --------------------------------------------------------------------------
def f32(hi: int, lo: int, swap: bool = False) -> float:
    a, b = (lo, hi) if swap else (hi, lo)
    return struct.unpack(">f", struct.pack(">HH", a, b))[0]


def sane(value: float) -> bool:
    """Does this look like a real measurement rather than noise?"""
    return value == value and abs(value) != float("inf") and 0.0001 <= abs(value) <= 100000


def show(regs: List[int], base: int) -> None:
    print("\n  per register")
    print("    addr   hex     uint16    int16")
    for i, r in enumerate(regs):
        signed = r - 0x10000 if r > 0x7FFF else r
        print(f"    {base + i:>4}  0x{r:04X}  {r:>7}  {signed:>7}")

    print("\n  as 32-bit floats (pairs of registers)")
    print("    addrs        big-endian        word-swapped")
    for i in range(0, len(regs) - 1, 2):
        be = f32(regs[i], regs[i + 1])
        ws = f32(regs[i], regs[i + 1], swap=True)
        mark_be = "  <-- plausible" if sane(be) else ""
        mark_ws = "  <-- plausible" if sane(ws) else ""
        print(f"    {base + i:>3}-{base + i + 1:<3}  {be:>16.4f}{mark_be:<16}"
              f"  {ws:>16.4f}{mark_ws}")

    print("\n  as 32-bit floats, offset by one register")
    for i in range(1, len(regs) - 1, 2):
        be = f32(regs[i], regs[i + 1])
        ws = f32(regs[i], regs[i + 1], swap=True)
        print(f"    {base + i:>3}-{base + i + 1:<3}  {be:>16.4f}                  {ws:>16.4f}")

    print("\n  as integers with common scale factors")
    print("    addr      /1        /10       /100      /1000")
    for i, r in enumerate(regs):
        s = r - 0x10000 if r > 0x7FFF else r
        print(f"    {base + i:>4}  {s:>8}  {s/10:>9.2f}  {s/100:>9.3f}  {s/1000:>9.4f}")


# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="Dump and decode raw Modbus registers.")
    ap.add_argument("--port", required=True, help="e.g. /dev/serial/by-id/usb-FTDI...")
    ap.add_argument("--baud", type=int, default=4800)
    ap.add_argument("--slave", type=int, default=1)
    ap.add_argument("--base", type=int, default=0, help="first register (default 0)")
    ap.add_argument("--count", type=int, default=12, help="how many to read (default 12)")
    ap.add_argument("--fc", type=int, default=3, choices=(3, 4),
                    help="3 = holding registers, 4 = input registers")
    ap.add_argument("--scan-fc", action="store_true", help="try both function codes")
    ap.add_argument("--scan-slaves", action="store_true",
                    help="find which addresses answer on this bus (1-16)")
    ap.add_argument("--watch", action="store_true",
                    help="re-read every second; move the probe and watch what changes")
    ap.add_argument("--timeout", type=float, default=1.0)
    args = ap.parse_args()

    try:
        import serial
    except ImportError:
        print("pyserial is not installed:  pip3 install pyserial", file=sys.stderr)
        return 2

    try:
        ser = serial.Serial(args.port, args.baud, timeout=args.timeout)
    except Exception as exc:  # noqa: BLE001
        print(f"could not open {args.port}: {exc}", file=sys.stderr)
        return 1

    print(f"port {args.port}  @ {args.baud} baud")

    with ser:
        time.sleep(0.2)

        if args.scan_slaves:
            print("\nscanning addresses 1-16 (reading 2 registers from each)")
            found = []
            for slave in range(1, 17):
                for fc in (3, 4):
                    try:
                        regs = read_registers(ser, slave, fc, args.base, 2, args.timeout)
                        print(f"  slave {slave:>2}  fc {fc}  -> {[hex(r) for r in regs]}")
                        found.append((slave, fc))
                    except IOError:
                        pass
                    except Exception as exc:  # noqa: BLE001
                        print(f"  slave {slave:>2}  fc {fc}  -> {type(exc).__name__}")
            if not found:
                print("  nothing answered. Check: A/B swapped, wrong baud rate, "
                      "no power to the transmitter, or the wrong adapter.")
            return 0 if found else 1

        codes = (3, 4) if args.scan_fc else (args.fc,)
        ok = False
        while True:
            for fc in codes:
                label = "holding" if fc == 3 else "input"
                try:
                    regs = read_registers(ser, args.slave, fc, args.base,
                                          args.count, args.timeout)
                except Exception as exc:  # noqa: BLE001
                    print(f"\nfc {fc} ({label} registers): {exc}")
                    continue
                ok = True
                print(f"\nfc {fc} ({label} registers) "
                      f"{args.base}..{args.base + args.count - 1} from slave {args.slave}")
                if all(r == 0 for r in regs):
                    print("  every register is zero -- the device is answering but has "
                          "nothing to report.\n  Usually: no probe attached to the "
                          "transmitter, or the wrong register block.")
                show(regs, args.base)
            if not args.watch:
                break
            time.sleep(1.0)

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
