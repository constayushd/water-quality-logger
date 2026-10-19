# Fetching sensor readings over Wi-Fi: Pi → PC on the same network

Both machines are on private `10.x` addresses, so if the network routes between
them the PC can pull readings straight from the Pi. No broker, no cloud, no port
forwarding — the Pi runs a small HTTP server, the PC polls it.

```
 Raspberry Pi                                     PC
 ┌────────────────────────┐                      ┌────────────────────────┐
 │ pH code      ─┐        │   HTTP :8000         │ live plots             │
 │ turbidity    ─┼ sensors│ ───────────────────► │ wq_log.csv             │
 │ TDS code     ─┘   .py  │   /readings?since=N  │ online/offline header   │
 │  pi_server.py          │   /latest /stream    │ pc_dashboard.py        │
 └────────────────────────┘                      └────────────────────────┘
```

> **The Pi's IP moves.** It has already been `10.1.58.62` and `10.1.23.222` —
> that is DHCP handing out a new lease each time it reconnects. Use the Pi's
> *name* rather than its address (see "When the IP changes" below) and you can
> stop tracking it.

## 0. First, prove the two can see each other (30 seconds)

This is the only step that can fail for reasons outside the code. From the PC:

```bash
ping raspberrypi.local        # or the Pi's current IP
```

If ping replies, you are fine. If it does not, check reachability on the actual
port once the server is running (some networks block ICMP but pass TCP):

```powershell
# Windows PowerShell
Test-NetConnection raspberrypi.local -Port 8000
```

```bash
# macOS / Linux
nc -vz raspberrypi.local 8000
```

If neither works, the two devices are on the same address range but not allowed
to talk — college and office Wi-Fi commonly enable *client isolation* (also
called AP isolation), and a PC on `10.1.22.x` and a Pi on `10.1.23.x` may be separate subnets.
Fixes, in order of preference:

1. Put both on a network you control — a phone hotspot, or your own router.
2. Have the Pi run its own hotspot and connect the PC to it.
3. Connect the Pi to the PC with an Ethernet cable (link-local addresses work).
4. Fall back to the MQTT broker design in `pi-to-desktop-communication.md`,
   which needs only outbound internet from each side and is immune to this.

## 1. On the Pi

```bash
mkdir -p ~/water-quality && cd ~/water-quality
# copy sensors.py, pi_server.py (and wq-server.service) here

python3 sensors.py --simulate     # sanity check, no hardware
python3 pi_server.py --simulate --port 8000
```

It prints the URL it is serving on. Check from the Pi itself:

```bash
curl http://localhost:8000/health
```

Then open `sensors.py` and put your three codes in — that is the only file you
edit. Each function returns a dict of numbers; every key becomes a plot panel
and a CSV column automatically. Then run for real:

```bash
python3 pi_server.py --port 8000 --spool ~/water-quality/wq_spool.jsonl
```

Useful flags:

| Flag | Effect |
|---|---|
| `--simulate` | fake data, no hardware needed |
| `--once` | read each sensor once, print the JSON, exit |
| `--token SECRET` | require the secret on every request |
| `--spool FILE` | also append every reading to the SD card |
| `--device-id pi-02` | name a second Pi (the PC handles both) |
| `--buffer 20000` | how many readings are held for a PC that reconnects |

Nothing to `pip install` — it is standard library only.

To start it at boot, see the header of `wq-server.service`.

## 2. On the PC

```bash
pip install matplotlib          # only needed for the plots
python3 pc_dashboard.py --host raspberrypi.local
```

A window opens with one panel per metric, the latest value marked in red, and a
header that is green while the Pi is reporting and red when it is not.
Everything is appended to `wq_log.csv`.

| Flag | Effect |
|---|---|
| `--headless` | console + CSV only, no window, no matplotlib |
| `--once` | print health and the latest values, then exit |
| `--window 60` | keep 60 minutes on screen (default 10) |
| `--backfill` | also pull readings the Pi took before you started |
| `--preview out.png` | save a single frame and exit |
| `--token SECRET` | must match the Pi's `--token` |

You can also just open **http://raspberrypi.local:8000/** in a browser on the PC for a
live tile view — no Python on the PC at all.

## Deploying without a network

To run this at an actual water site, where there is no router to join, the Pi
can broadcast its own Wi-Fi and the laptop connects straight to it. See
`FIELD-DEPLOYMENT.md` — the Python here does not change at all.

## When the IP changes

The Pi gets its address from DHCP, so it moves whenever it reconnects. Three
ways to stop caring, best first.

**1. Call it by name (no setup on the Pi).** Raspberry Pi OS runs Avahi, so the
Pi answers to `<hostname>.local` on the LAN. Windows 10+, macOS and most Linux
resolve that out of the box:

```bash
python3 pc_dashboard.py --host raspberrypi.local
```

Check the name on the Pi with `hostname` — if you renamed it, use that instead.
This follows the Pi across every new lease. It is now the default `--host`.

**2. Reserve the address on the router.** In the router's admin page, find the
Pi's MAC (`ip link show wlan0` on the Pi) under DHCP and bind it to a fixed
address. The Pi needs no changes and always lands on the same IP. Best option
on a network you control; not available on campus or office Wi-Fi.

**3. Find it again when it has moved.** If `.local` is blocked and you do not
control the router:

```bash
# on the Pi
hostname -I

# or, from the PC, knock on port 8000 across a range
python3 pc_dashboard.py --scan 10.1.23.0/24
python3 pc_dashboard.py --scan              # this PC's own /24
```

`--scan` prints the device_id and sensors of anything running `pi_server.py`,
then the exact command to start the dashboard against it.

## The CSV

Long-form, so adding a fourth sensor never breaks the file:

```
timestamp, epoch, device_id, source, metric, value
```

```python
import pandas as pd
df = pd.read_csv("wq_log.csv", parse_dates=["timestamp"])
wide = df.pivot_table(index="timestamp", columns="metric", values="value")
```

## What it handles

- **Wi-Fi drops.** The PC reconnects with exponential backoff (1 s → 30 s) and
  asks for everything it missed by sequence number, so the gap is filled from
  the Pi's buffer rather than lost.
- **PC closed overnight.** Readings stay in the Pi's ring buffer (5000 by
  default) and, with `--spool`, on the SD card as well.
- **Pi reboots.** The sequence numbers restart; the PC notices and resyncs.
- **Started in either order.** The PC waits for the Pi; the Pi does not care
  whether anyone is listening.
- **One bad probe.** Each sensor samples in its own thread — a probe that hangs
  or throws does not stop the other two. The error shows in `/health`.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `connection refused` | `pi_server.py` is not running, or it is on another port. |
| `no reply from the Pi` (timeout) | Client isolation or different subnets — see step 0. |
| `401 unauthorised` | The PC's `--token` does not match the Pi's. |
| Works on the Pi (`localhost`) but not from the PC | The server is bound to loopback only, or a firewall on the Pi. It binds `0.0.0.0` by default; check `sudo ufw status`. |
| The Pi's address changed | DHCP gave it a new lease. Use `raspberrypi.local` instead, or see "When the IP changes". |
| Header is green but no new points | A sensor function is throwing. Check `curl http://raspberrypi.local:8000/health` — failures are listed under `errors`. |
| Readings arrive in a burst | The PC was disconnected and is catching up. Normal. |

## Security note

This is plain HTTP on the LAN. That is fine on a network you trust. On shared
Wi-Fi, pass `--token` on both sides — it keeps casual snoopers out, though it
does not encrypt anything. If you need real confidentiality, use the MQTT-over-
TLS design in `pi-to-desktop-communication.md` instead.
