#!/usr/bin/env bash
#
# One-time setup for running this project on Ubuntu on a Raspberry Pi.
#
#   chmod +x setup_ubuntu.sh
#   ./setup_ubuntu.sh
#
# Safe to re-run: every step checks before it changes anything.
#
# Handles the four things Ubuntu does differently from Raspberry Pi OS:
#   1. no raspi-config, so I2C is enabled by editing the firmware config
#   2. PEP 668 blocks pip installing into the system Python, so we use a venv
#   3. the i2c group and its udev rule may not exist
#   4. ModemManager grabs USB serial adapters and corrupts Modbus traffic

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${PROJECT_DIR}/.venv"
NEEDS_REBOOT=0

info()  { echo -e "\033[1;34m==>\033[0m $*"; }
warn()  { echo -e "\033[1;33m!  \033[0m $*"; }
ok()    { echo -e "\033[1;32m ok\033[0m $*"; }

if [[ $EUID -eq 0 ]]; then
    echo "Run this as your normal user, not with sudo. It will prompt when needed."
    exit 1
fi

# --- 1. system packages ------------------------------------------------
info "Installing system packages"
sudo apt-get update -qq
sudo apt-get install -y python3-venv python3-pip python3-dev i2c-tools git
ok "packages installed"

# --- 2. enable I2C -----------------------------------------------------
# Ubuntu has no raspi-config. The firmware config lives in /boot/firmware
# on 22.04 and later; older releases used a different path.
CONFIG_TXT=""
for candidate in /boot/firmware/config.txt /boot/firmware/usercfg.txt /boot/config.txt; do
    if [[ -f "$candidate" ]]; then CONFIG_TXT="$candidate"; break; fi
done

if [[ -z "$CONFIG_TXT" ]]; then
    warn "Could not find the firmware config file. Enable I2C manually:"
    warn "  add 'dtparam=i2c_arm=on' to your boot config and reboot."
else
    info "Enabling I2C in ${CONFIG_TXT}"
    if grep -qE '^\s*dtparam=i2c_arm=on' "$CONFIG_TXT"; then
        ok "already enabled"
    else
        echo 'dtparam=i2c_arm=on' | sudo tee -a "$CONFIG_TXT" > /dev/null
        ok "added dtparam=i2c_arm=on"
        NEEDS_REBOOT=1
    fi
fi

# The i2c-dev module exposes /dev/i2c-1. Usually autoloaded; make it certain.
if ! grep -qE '^\s*i2c-dev' /etc/modules 2>/dev/null; then
    echo 'i2c-dev' | sudo tee -a /etc/modules > /dev/null
    ok "i2c-dev added to /etc/modules"
    NEEDS_REBOOT=1
fi
sudo modprobe i2c-dev 2>/dev/null || true

# --- 3. device permissions --------------------------------------------
info "Setting up device access"

if ! getent group i2c > /dev/null; then
    sudo groupadd -f i2c
    ok "created i2c group"
fi

# Raspberry Pi OS ships this rule; Ubuntu often doesn't.
UDEV_RULE=/etc/udev/rules.d/99-i2c.rules
if [[ ! -f "$UDEV_RULE" ]]; then
    echo 'KERNEL=="i2c-[0-9]*", GROUP="i2c", MODE="0660"' | sudo tee "$UDEV_RULE" > /dev/null
    sudo udevadm control --reload-rules 2>/dev/null || true
    ok "added i2c udev rule"
    NEEDS_REBOOT=1
fi

for grp in dialout i2c; do
    if id -nG "$USER" | tr ' ' '\n' | grep -qx "$grp"; then
        ok "already in group ${grp}"
    else
        sudo usermod -aG "$grp" "$USER"
        ok "added ${USER} to ${grp}"
        NEEDS_REBOOT=1
    fi
done

# --- 4a. remove brltty -------------------------------------------------
# Ubuntu ships a braille-display driver that claims CH340 and CP210x USB
# serial chips -- exactly the chips in cheap RS485 adapters. The symptom is
# /dev/ttyUSB0 appearing for a second after plug-in and then disappearing.
# This one costs people an afternoon; Raspberry Pi OS does not have it.
if dpkg -l brltty 2>/dev/null | grep -q '^ii'; then
    info "Removing brltty (it steals USB-serial adapters on Ubuntu)"
    sudo apt-get remove -y brltty
    ok "brltty removed -- unplug and replug your RS485 adapters"
else
    ok "brltty not installed"
fi

# --- 4. stop ModemManager touching the RS485 adapters ------------------
# ModemManager probes new USB serial devices looking for modems. Those probes
# inject bytes onto the bus and show up as CRC errors and short reads for the
# first ~30 seconds after plugging in an adapter.
if systemctl list-unit-files 2>/dev/null | grep -q '^ModemManager\.service'; then
    info "Disabling ModemManager (it interferes with USB-RS485 adapters)"
    sudo systemctl stop ModemManager 2>/dev/null || true
    sudo systemctl disable ModemManager 2>/dev/null || true
    sudo systemctl mask ModemManager 2>/dev/null || true
    ok "ModemManager masked"
else
    ok "ModemManager not present"
fi

# --- 5. Python environment --------------------------------------------
# Ubuntu 23.04+ marks the system Python as externally managed (PEP 668), so
# a plain 'pip install' fails. A venv is the clean way around it.
info "Creating virtual environment at ${VENV_DIR}"
if [[ ! -d "$VENV_DIR" ]]; then
    python3 -m venv "$VENV_DIR"
    ok "venv created"
else
    ok "venv already exists"
fi

"${VENV_DIR}/bin/pip" install --quiet --upgrade pip
"${VENV_DIR}/bin/pip" install --quiet -r "${PROJECT_DIR}/requirements.txt"
ok "Python dependencies installed"

# --- 6. local config ---------------------------------------------------
# config.yaml holds this machine's device paths, so it is deliberately not
# tracked in git. Each machine keeps its own, seeded from the example.
if [[ -f "${PROJECT_DIR}/config.yaml" ]]; then
    ok "config.yaml already exists (left untouched)"
else
    cp "${PROJECT_DIR}/config.example.yaml" "${PROJECT_DIR}/config.yaml"
    ok "created config.yaml -- edit it with your real device paths"
fi

# --- 7. verify ---------------------------------------------------------
echo
info "Checking the install"
if "${VENV_DIR}/bin/python" "${PROJECT_DIR}/main.py" --list-types > /dev/null 2>&1; then
    ok "drivers load correctly"
else
    warn "main.py --list-types failed. Check that the sensors/ folder is intact."
fi

echo
echo "----------------------------------------------------------------"
if [[ $NEEDS_REBOOT -eq 1 ]]; then
    echo "REBOOT REQUIRED before the I2C device and group changes take effect:"
    echo "    sudo reboot"
    echo
    echo "After rebooting:"
else
    echo "Setup complete. Next:"
fi
cat <<EOF
    python check_hardware.py     # verify wiring, permissions and ports
    nano config.yaml             # set your real device paths
    source .venv/bin/activate
    python calibrate_ph.py       # calibrate the pH probe
    python main.py --once        # single test reading
    python main.py               # log continuously
EOF
echo "----------------------------------------------------------------"
