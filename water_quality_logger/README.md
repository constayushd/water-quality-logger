# Water Quality Logger

Polls a dissolved-oxygen transmitter, a turbidity transmitter, and an analog
pH probe from a Raspberry Pi, and writes every reading into one CSV. Adding a
fourth sensor means writing one file and one config entry.

## Why it's structured this way

The three sensors have nothing in common electrically — two are Modbus RTU
over RS485, one is an analog voltage needing an external ADC. Rather than
three scripts and three CSVs, each sensor is a small driver class behind a
common interface, and a single runner polls them all into one timestamped row.

```
main.py              runner: reads config, polls, writes CSV
config.example.yaml  template, tracked in git
config.yaml          YOUR machine's wiring -- gitignored, made by setup
check_hardware.py    diagnostics: permissions, ports, I2C, config sanity
setup_ubuntu.sh      one-shot OS prep for Ubuntu on a Pi
wq-logger.service    systemd unit to run at boot
UBUNTU_SETUP.md      Ubuntu-specific setup and troubleshooting
calibrate_ph.py      interactive two-point pH calibration
csvlog.py            CSV output (wide or long format)
sensors/
  base.py            Sensor base class + driver registry
  modbus.py          shared Modbus RTU bus (handles two sensors on one port)
  ads1115.py         I2C ADC used by any analog sensor
  do_rs485.py        dissolved oxygen driver
  turbidity_rs485.py turbidity driver
  ph_analog.py       pH driver (PH-4502C via ADS1115)
  simulated.py       fake sensor: test without hardware, template for new ones
```

## Install

**On Ubuntu 22.04 (Raspberry Pi)** — see [UBUNTU_SETUP.md](UBUNTU_SETUP.md) for
the details, but the short version is:

```bash
chmod +x setup_ubuntu.sh
./setup_ubuntu.sh          # as your normal user, NOT with sudo
sudo reboot                # if it says one is needed
```

**On Raspberry Pi OS:**

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
sudo raspi-config                      # Interface Options -> enable I2C
sudo usermod -aG dialout,i2c $USER     # then log out and back in
cp config.example.yaml config.yaml
```

Either way, verify before wiring anything up:

```bash
python check_hardware.py    # checks permissions, ports, I2C, and your config
python main.py --list-types
```

## Hardware

### The two RS485 sensors

Both ship as Modbus slave ID 1 reading register 0x0000, so they **cannot share
one RS485 bus as delivered** — you'd get address collision and garbage. Either:

- give each one its own USB-RS485 adapter (the default in `config.yaml`), or
- re-address one sensor (turbidity → 2, say) and point both config entries at
  the same `port`. The bus layer locks the port and enforces the Modbus
  inter-frame gap, so sharing works correctly once addresses differ.

Use the stable device paths rather than `/dev/ttyUSB0`, which reshuffles on
reboot when two adapters are present:

```bash
ls -l /dev/serial/by-id/
```

### The pH probe — read this before wiring

The PH-4502C outputs an **analog voltage**, and the Pi has no ADC, so you need
an ADS1115 (~₹200) between them.

**The PH-4502C wants a 5 V supply, and its Po output can swing above 3.3 V.
Feeding that straight into a 3.3 V-powered ADS1115 will damage the ADC.**
Put a 2:1 divider on the signal:

```
PH-4502C V+  ──── Pi 5V (pin 2)
PH-4502C G   ──── Pi GND (pin 6)
PH-4502C Po  ──── 10k ──┬── ADS1115 A0
                        │
                       10k
                        │
                       GND

ADS1115 VDD  ──── Pi 3.3V (pin 1)
ADS1115 GND  ──── Pi GND
ADS1115 SCL  ──── Pi GPIO3 (pin 5)
ADS1115 SDA  ──── Pi GPIO2 (pin 3)
ADS1115 ADDR ──── GND        (sets I2C address 0x48)
```

Any two equal resistors work (10k is a good default). The divider halves the
signal, but that's absorbed into the calibration slope — you don't correct for
it anywhere in software.

Confirm the ADC is visible:

```bash
sudo apt install -y i2c-tools && i2cdetect -y 1     # expect 48
```

The `To` pin on the board is a temperature-probe input and is usually
unpopulated. Ignore it — the DO and turbidity sensors already report
temperature.

## Calibrate the pH probe

**pH readings are meaningless until you do this.** The two blue trimpots on
the board set an arbitrary offset and gain, so no two boards produce the same
voltage for the same pH, and the relationship drifts as the probe ages.

```bash
python calibrate_ph.py
```

It prompts for each buffer solution (pH 7.00 and pH 4.01 at minimum; add
10.01 to check linearity), samples the voltage, fits a straight line, and
saves `ph_calibration.json`.

Rinse the probe in distilled water between buffers and let each reading settle
for 30–60 seconds. An R² below about 0.99 across three points means the probe
is aged or fouled, or a buffer is contaminated.

Recalibrate every few weeks. **Never let the glass bulb dry out** — store it in
the KCl solution in its cap, not in distilled water, which leaches the bulb.

The raw ADC voltage is logged in its own `ph_volts` column regardless of
calibration state, so a missing or wrong calibration never loses you the
underlying data — you can always recompute pH from the CSV afterwards.

## Run

```bash
python main.py                    # until Ctrl+C
python main.py --once             # single reading, good for a wiring check
python main.py --duration 3600    # one hour
python main.py --format long      # tidy output instead of wide
```

Output goes to `data/sensor_log.csv`:

```
timestamp,do_mg_l,do_saturation_pct,do_temp_c,turbidity_ntu,turbidity_temp_c,ph_ph,ph_volts,status
2026-08-14T11:08:28,8.21,94.30,24.60,1.4,24.5,7.02,1.2481,ok
2026-08-14T11:08:30,8.19,94.10,24.60,,,7.01,1.2478,turbidity: no/short response
```

Units are written to `data/sensor_log_units.csv` so the header itself stays
easy to parse.

A sensor that fails leaves its columns blank and the reason in `status`; the
run continues, and that sensor's connection is reopened from scratch on the
next cycle. Unplug an adapter mid-run and it recovers when you plug it back
in. Every row is flushed to disk immediately, so a crash or yanked cable
costs you nothing.

### Run it unattended

`wq-logger.service` starts the logger at boot and restarts it if it dies.
Edit the `User` and the paths inside it first, then:

```bash
sudo cp wq-logger.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wq-logger
journalctl -u wq-logger -f
```

## Adding a sensor later

Copy `sensors/simulated.py`, then implement three things:

```python
from .base import Channel, Sensor, register

@register("ec_rs485")                    # the name you'll use in config.yaml
class ConductivityRS485(Sensor):
    CHANNELS = (Channel("us_cm", "uS/cm", 1),)   # one per value = one CSV column

    def __init__(self, name, port="/dev/ttyUSB2", slave_id=1, **kwargs):
        super().__init__(name, **kwargs)
        self.slave_id = slave_id
        self._bus = modbus.get_bus(port, baudrate=4800)

    def open(self):  self._bus.acquire()
    def close(self): self._bus.release()

    def read(self):
        data = self._bus.read_holding_registers(self.slave_id, 0x0000, 1)
        return {"us_cm": struct.unpack(">h", data)[0] / 10.0}
```

Then add it to `config.yaml`:

```yaml
  - name: ec
    type: ec_rs485
    port: /dev/ttyUSB2
    slave_id: 1
```

Nothing else changes. The package auto-imports every driver file, the CSV
header is rebuilt from whatever sensors are enabled, and `main.py` never needs
editing. For another analog sensor (ORP, for instance) reuse `ADS1115` on a
different `channel` — the ADC has four inputs.

On the next run the logger notices the header no longer matches the existing
CSV and renames the old file with a timestamp rather than appending
mismatched rows. If you'd rather keep appending to one file forever as sensors
come and go, use `format: long` — its header never changes.

## Ubuntu notes

What `setup_ubuntu.sh` does, in case you'd rather do it by hand or need to
debug it:

**Enabling I2C.** There's no `raspi-config`. Add `dtparam=i2c_arm=on` to
`/boot/firmware/config.txt` (that path is Ubuntu 22.04 and later; older
releases used `usercfg.txt`) and reboot. Check it worked with
`ls /dev/i2c-*` — you want `/dev/i2c-1` to exist.

**The venv is not optional.** Ubuntu 23.04 and later mark the system Python as
externally managed, so `pip install pyserial` fails with an
`externally-managed-environment` error. Use the venv the script creates. You
*can* force it with `pip install --break-system-packages`, but that risks
conflicting with apt-managed packages — not worth it here.

**I2C permissions.** Raspberry Pi OS ships a udev rule putting `/dev/i2c-*` in
the `i2c` group; Ubuntu often doesn't. The script creates the group, adds the
rule to `/etc/udev/rules.d/99-i2c.rules`, and adds you to `dialout` and `i2c`.
Group membership only applies to new logins, so reboot rather than wonder why
permissions still fail.

**ModemManager.** This is the one that produces baffling symptoms. Ubuntu runs
ModemManager, which probes every new USB serial device to see whether it's a
modem. Those probes push bytes onto your RS485 bus, so for roughly 30 seconds
after plugging in an adapter you get CRC errors and short reads that then
mysteriously stop. The script masks the service. To check it's gone:

```bash
systemctl is-enabled ModemManager     # expect: masked
```

**Ubuntu Server has no desktop autologin**, so if you want the logger running
after a power cut, use the systemd service rather than a terminal session.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `no/short response` | Wrong port, A/B swapped, sensor unpowered (needs 10–30 V), or wrong baud |
| `reply came from address N` | Two devices at the same slave ID on one bus |
| `CRC mismatch` | EMI on the RS485 line; use shielded cable, add a 120 Ω terminator |
| DO values are `nan` or `1e38` | Set `word_swap: true` on the DO sensor |
| `Permission denied` on `/dev/ttyUSB0` | Not in the `dialout` group yet, or haven't logged back in |
| pH column blank, volts populated | No calibration yet — run `calibrate_ph.py` |
| pH volts pinned near 0 or full scale | Divider wired wrong, or probe not seated in the BNC |
| pH jumps around by whole units | Probe needs cleaning, or ground loop between the 5 V board and the ADC |
| `i2cdetect` shows nothing | I2C not enabled, or SDA/SCL swapped |
| `externally-managed-environment` on pip | Ubuntu 23.04+; activate the venv first |
| CRC errors for ~30 s after plugging in USB | ModemManager probing the adapter — mask it |
| `No such file or directory: /dev/i2c-1` | `dtparam=i2c_arm=on` missing from `/boot/firmware/config.txt` |
| `ModuleNotFoundError: serial` under systemd | `ExecStart` points at `/usr/bin/python3` instead of the venv |

## Optional: temperature-compensated pH

The Nernst slope varies with temperature by about 0.2% per °C, so pH readings
drift as water temperature changes. Both RS485 sensors already report
temperature, so the correction is available to you — the simplest approach is
to log everything as-is and apply the correction during analysis, using the
`do_temp_c` column against the temperature of your calibration buffers. Doing
it in post keeps the raw record intact.
