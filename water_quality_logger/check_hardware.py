#!/usr/bin/env python3
"""Check that this machine can actually talk to the sensors.

Run this before main.py whenever something isn't working, or after any
rewiring. It checks the things that go wrong in practice -- missing groups,
brltty eating the serial adapters, I2C not enabled, ports named differently
than config.yaml expects -- and tells you the fix for each.

    python check_hardware.py

Everything here is read-only. It changes nothing.
"""

from __future__ import annotations

import glob
import grp
import os
import pwd
import shutil
import subprocess
import sys

GREEN, YELLOW, RED, BOLD, OFF = "\033[32m", "\033[33m", "\033[31m", "\033[1m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = YELLOW = RED = BOLD = OFF = ""

problems: list[str] = []


def section(title):
    print(f"\n{BOLD}{title}{OFF}")


def ok(message):
    print(f"  {GREEN}ok{OFF}    {message}")


def warn(message, fix=None):
    print(f"  {YELLOW}warn{OFF}  {message}")
    if fix:
        print(f"        fix: {fix}")


def bad(message, fix=None):
    print(f"  {RED}fail{OFF}  {message}")
    if fix:
        print(f"        fix: {fix}")
    problems.append(message)


def run(command):
    try:
        return subprocess.run(command, capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except Exception:
        return ""


# ---------------------------------------------------------------- system
def check_system():
    section("System")
    try:
        with open("/etc/os-release") as fh:
            pretty = next((line.split("=", 1)[1].strip().strip('"')
                           for line in fh if line.startswith("PRETTY_NAME=")), "unknown")
    except OSError:
        pretty = "unknown"
    print(f"  {pretty}  |  python {sys.version.split()[0]}  |  user {pwd.getpwuid(os.getuid()).pw_name}")

    if sys.prefix == sys.base_prefix:
        warn("not running inside the virtualenv",
             "source .venv/bin/activate")
    else:
        ok(f"virtualenv active ({sys.prefix})")


def check_packages():
    section("Python packages")
    for module, package in (("serial", "pyserial"),
                            ("yaml", "PyYAML"),
                            ("smbus2", "smbus2")):
        try:
            __import__(module)
            ok(f"{package}")
        except ImportError:
            bad(f"{package} missing", "pip install -r requirements.txt")


def check_groups():
    section("Permissions")
    user = pwd.getpwuid(os.getuid()).pw_name
    active = {grp.getgrgid(g).gr_name for g in os.getgroups()}
    for group, why in (("dialout", "serial ports"), ("i2c", "the I2C bus")):
        try:
            grp.getgrnam(group)
        except KeyError:
            warn(f"group '{group}' does not exist on this system")
            continue
        if group in active:
            ok(f"in group {group} ({why})")
        else:
            member = user in grp.getgrnam(group).gr_mem
            if member:
                bad(f"in group {group} but not active in this session",
                    "log out and back in, or reboot")
            else:
                bad(f"not in group {group} -- cannot open {why}",
                    f"sudo usermod -aG {group} {user}   then log out and back in")


# ---------------------------------------------------------------- serial
def check_brltty():
    section("brltty (Ubuntu steals USB-serial adapters)")
    installed = run(["dpkg", "-l", "brltty"])
    if installed and any(line.startswith("ii") for line in installed.splitlines()):
        bad("brltty is installed -- it claims CH340/CP210x adapters and makes "
            "/dev/ttyUSB* vanish seconds after plug-in",
            "sudo apt remove -y brltty   then unplug and replug the adapters")
    else:
        ok("not installed")


def check_serial_ports():
    section("Serial ports")
    by_id = sorted(glob.glob("/dev/serial/by-id/*"))
    tty_usb = sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))

    if not tty_usb:
        bad("no USB serial adapters found",
            "check they're plugged in; if they appear then vanish, remove brltty")
        return

    for device in tty_usb:
        readable = os.access(device, os.R_OK | os.W_OK)
        if readable:
            ok(f"{device} (readable and writable)")
        else:
            bad(f"{device} exists but you cannot open it",
                "you're not in the dialout group yet, or haven't logged back in")

    if by_id:
        print(f"\n  {BOLD}Stable paths -- use these in config.yaml:{OFF}")
        for link in by_id:
            target = os.path.realpath(link)
            print(f"    {link}")
            print(f"      -> {os.path.basename(target)}")
        print("  (/dev/ttyUSB0 and USB1 swap on reboot; the by-id paths don't)")
    else:
        warn("no /dev/serial/by-id entries -- you'll have to use /dev/ttyUSB*, "
             "which can reorder on reboot")


# ------------------------------------------------------------------- i2c
def check_i2c():
    section("I2C bus (needed for the pH sensor)")
    buses = sorted(glob.glob("/dev/i2c-*"))
    if not buses:
        bad("no /dev/i2c-* devices -- I2C is not enabled",
            "add 'dtparam=i2c_arm=on' to /boot/firmware/config.txt and reboot")
        return
    ok(f"found {', '.join(buses)}")

    target = "/dev/i2c-1"
    if target not in buses:
        warn(f"{target} not present; the config assumes bus 1")
        return
    if not os.access(target, os.R_OK | os.W_OK):
        bad(f"{target} exists but you cannot open it",
            "sudo usermod -aG i2c $USER   then log out and back in")
        return

    try:
        from smbus2 import SMBus
    except ImportError:
        warn("smbus2 not installed, skipping the device scan")
        return

    found = []
    with SMBus(1) as bus:
        for address in range(0x03, 0x78):
            try:
                bus.read_byte(address)
                found.append(address)
            except OSError:
                pass

    if not found:
        bad("no I2C devices responded",
            "check SDA/SCL wiring and that the ADS1115 has 3.3V power")
    else:
        listed = ", ".join(f"0x{a:02x}" for a in found)
        ok(f"devices responding at {listed}")
        if any(a in (0x48, 0x49, 0x4A, 0x4B) for a in found):
            ok("an ADS1115 is present -- put its address in config.yaml")
        else:
            warn("nothing at 0x48-0x4b, so no ADS1115 seen",
                 "ADDR pin to GND gives 0x48")


# ---------------------------------------------------------------- config
def check_config():
    section("config.yaml")
    if not os.path.exists("config.yaml"):
        bad("config.yaml not found",
            "cp config.example.yaml config.yaml   then edit it")
        return
    try:
        import yaml
        with open("config.yaml") as fh:
            config = yaml.safe_load(fh) or {}
    except Exception as exc:
        bad(f"could not parse config.yaml: {exc}")
        return

    entries = config.get("sensors", [])
    if not entries:
        bad("no sensors listed in config.yaml")
        return

    for entry in entries:
        name = entry.get("name", "?")
        if not entry.get("enabled", True):
            print(f"  {'-':>4}  {name}: disabled, skipping")
            continue

        port = entry.get("port")
        if port:
            if os.path.exists(port):
                ok(f"{name}: {port} exists")
            else:
                bad(f"{name}: {port} does not exist",
                    "set it to one of the stable paths listed above, "
                    "or disable this sensor with enabled: false")

        if entry.get("type") == "ph_analog":
            cal = entry.get("calibration_file", "ph_calibration.json")
            if os.path.exists(cal):
                ok(f"{name}: calibration found ({cal})")
            else:
                warn(f"{name}: no calibration yet -- pH will log as blank, "
                     "raw volts still recorded",
                     "python calibrate_ph.py")

    slaves: dict[str, list] = {}
    for entry in entries:
        if entry.get("enabled", True) and entry.get("port"):
            slaves.setdefault(entry["port"], []).append(
                (entry.get("name"), entry.get("slave_id", 1)))
    for port, users in slaves.items():
        ids = [slave_id for _, slave_id in users]
        if len(ids) > 1 and len(set(ids)) != len(ids):
            bad(f"two sensors share {port} with the same slave_id "
                f"({', '.join(n for n, _ in users)})",
                "re-address one sensor, or move it to its own adapter")


def main():
    print(f"{BOLD}Water Quality Logger -- hardware check{OFF}")
    check_system()
    check_packages()
    check_groups()
    check_brltty()
    check_serial_ports()
    check_i2c()
    check_config()

    print()
    if problems:
        print(f"{RED}{BOLD}{len(problems)} problem(s) to fix:{OFF}")
        for problem in problems:
            print(f"  - {problem}")
        sys.exit(1)
    print(f"{GREEN}{BOLD}All checks passed.{OFF}  Try: python main.py --once")


if __name__ == "__main__":
    main()
