# Running on Ubuntu 22.04 LTS (Raspberry Pi)

Ubuntu Server on a Pi differs from Raspberry Pi OS in four ways that break
this project if you don't handle them. `setup_ubuntu.sh` handles all four;
this document explains what it's doing and why, so you can fix things by hand
when something doesn't match.

| | Raspberry Pi OS | Ubuntu 22.04 |
|---|---|---|
| Enable I2C | `raspi-config` | edit `/boot/firmware/config.txt` |
| Boot config path | `/boot/config.txt` | `/boot/firmware/config.txt` |
| USB-serial adapters | work immediately | **`brltty` claims them** |
| `i2c` group + udev rule | preinstalled | often absent |
| Default user | `pi` | `ubuntu` (yours is `murallab`) |

## Quickstart

```bash
git clone https://github.com/constayushd/water-quality-logger.git
cd water-quality-logger
chmod +x setup_ubuntu.sh
./setup_ubuntu.sh          # as your normal user, NOT with sudo
sudo reboot                # it will tell you if this is needed
```

After the reboot:

```bash
cd water-quality-logger
source .venv/bin/activate
python check_hardware.py   # tells you exactly what's still wrong
nano config.yaml           # paste in the real device paths it printed
python main.py --once
```

`check_hardware.py` is the thing to reach for whenever something misbehaves.
It checks group membership, brltty, serial ports, the I2C bus, an ADS1115
scan, and whether the ports named in your config actually exist — and prints
the fix command for each failure. It changes nothing, so it's always safe.

## The four gotchas, individually

### 1. brltty eats your RS485 adapters

Ubuntu ships a braille-display driver that claims CH340 and CP210x chips —
the chips inside cheap USB-RS485 adapters. The symptom is `/dev/ttyUSB0`
appearing for a second after plug-in, then vanishing.

```bash
sudo apt remove -y brltty
# then unplug and replug the adapters
ls -l /dev/serial/by-id/
```

### 2. No raspi-config, and a different boot path

```bash
sudo nano /boot/firmware/config.txt     # note: /boot/firmware, not /boot
```

Add `dtparam=i2c_arm=on`, save, reboot. Then confirm:

```bash
ls /dev/i2c-*        # expect /dev/i2c-1
i2cdetect -y 1       # expect 48 for the ADS1115
```

If `/dev/i2c-1` is missing after a reboot, the module isn't loading:
`sudo modprobe i2c-dev` and add `i2c-dev` to `/etc/modules`.

### 3. Group membership and the missing i2c udev rule

```bash
sudo usermod -aG dialout,i2c $USER
```

**Log out and back in** — group changes don't apply to your current session,
which is why `groups` can list `dialout` while opening the port still fails.

Ubuntu often lacks the udev rule that gives the `i2c` group access to the bus.
`setup_ubuntu.sh` creates `/etc/udev/rules.d/99-i2c.rules` for you.

### 4. Use a virtualenv

The setup script creates `.venv` and installs into it. Activate it every time:

```bash
source .venv/bin/activate
```

Not strictly required on 22.04, but it future-proofs you against PEP 668,
which makes system-wide `pip install` fail on 23.04 and later.

## config.yaml is not in git

Device paths differ between your Pi and your laptop, so a tracked
`config.yaml` would conflict on every `git pull`. The repo holds
`config.example.yaml`; each machine keeps its own untracked `config.yaml`,
created by the setup script.

Practically: on the Pi, set real by-id paths. On your laptop, set every
sensor to `type: simulated` so you can develop with no hardware attached.
Both machines pull the same code without ever fighting over config.

Everything in the example ships as `enabled: false` except DO. Turn sensors
on one at a time as you wire them — debugging three unfamiliar wiring jobs at
once is how people conclude the code is broken.

## Run it at boot

```bash
sudo cp wq-logger.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wq-logger
journalctl -u wq-logger -f
```

The unit is preset for user `murallab` and `/home/murallab/water-quality-logger`.
Edit `User`, `Group`, `WorkingDirectory` and `ExecStart` if yours differ. Note
`ExecStart` must point at `.venv/bin/python`, not `/usr/bin/python3` — the
dependencies only exist inside the venv.

## Editing from your laptop

Install the **Remote - SSH** extension in VS Code and connect to
`murallab@<pi-ip>`. You get your laptop's editor operating on the Pi's
filesystem, with a terminal on the Pi. Edit and run in the same place; no
copying files back and forth.

If SSH closes immediately without asking for a password, Ubuntu's cloud image
has disabled password auth. Put your laptop's public key into
`~/.ssh/authorized_keys` on the Pi, or re-enable passwords:

```bash
echo "PasswordAuthentication yes" | sudo tee /etc/ssh/sshd_config.d/99-passwords.conf
sudo systemctl restart ssh
```

The Pi's IP changes when its DHCP lease renews. `hostname -I` on the Pi shows
the current one; `ssh murallab@pi-robot.local` often works too.

## Ubuntu-specific troubleshooting

| Symptom | Cause |
|---|---|
| `/dev/ttyUSB0` appears then disappears | brltty — remove it |
| `Permission denied: '/dev/ttyUSB0'` | not in `dialout`, or haven't logged back in since |
| `No such file or directory: '/dev/i2c-1'` | I2C not enabled in `/boot/firmware/config.txt` |
| `i2cdetect` shows nothing | wrong wiring, or ADS1115 unpowered |
| CRC errors for ~30s after plugging in | ModemManager probing the adapter — the setup script masks it |
| `externally-managed-environment` from pip | activate the venv first |
| SSH connection closed immediately | password auth disabled; use a key |
| `git pull` conflicts on config.yaml | it's tracked from an old commit — `git rm --cached config.yaml` |
