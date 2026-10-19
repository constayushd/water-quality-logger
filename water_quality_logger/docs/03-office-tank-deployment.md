# Deploying at the office water tank

Your PC is on an SSID called **Guest** — 10.1.28.196, gateway 10.1.16.1, so the
network is at least a /20 and the office hands out addresses across a wide
range. The DNS list includes 8.8.8.8, so that network does reach the internet.

Two things follow from that, and they decide the whole deployment.

**A network named "Guest" is usually built to stop devices talking to each
other.** Client isolation is the main reason a guest SSID exists. If it is on,
your PC cannot reach the Pi no matter which address it has, and no amount of
configuration on either machine changes that — the access point drops the
frames. So the direct path you have been using over Ethernet may simply not be
available here. Ten minutes of testing tells you.

**The internet works, which gives you a guaranteed fallback.** If the two
cannot talk directly, both can still talk *outward* to a broker in the middle.
That is the MQTT path, and it is now built: `pi_uplink.py` on the Pi,
`pc_dashboard.py --mqtt` on the PC, same plots and same CSV.

```
  DIRECT (try first)                    VIA BROKER (always works)

  Pi ─────────────► PC                  Pi ──┐                 ┌──► PC
     one hop on Guest                        └──► broker ──────┘
     blocked by client isolation             both connect outward, TLS 8883
```

---

## Step 1 — put the Pi on the Guest network (10 minutes, at your desk)

Do this over the Ethernet cable, before the Pi goes anywhere:

```bash
scp join_wifi.sh pi@raspberrypi.local:~
ssh pi@raspberrypi.local
chmod +x join_wifi.sh
sudo ./join_wifi.sh --ssid Guest --pass 'the-wifi-password'
```

It joins the network, turns Wi-Fi power saving off, makes it reconnect at boot,
and then prints the Pi's address plus whether the gateway, the internet and DNS
are reachable. Write the address down.

> If the Pi previously ran as a hotspot, this disables that profile — one radio
> cannot both be a network and join one.

## Step 2 — find out which path you get

From the PC, with the Pi's address from step 1:

```powershell
ping 10.1.x.x
Test-NetConnection 10.1.x.x -Port 8000      # some networks block ping but not TCP
```

Or let the dashboard sweep the range the office uses:

```bash
python3 pc_dashboard.py --scan 10.1.16.0/20
```

**If it replies** — the direct path works. Use it:

```bash
python3 pc_dashboard.py --host 10.1.x.x --clock pc
```

Note the Pi's address will change when its DHCP lease renews, so `--scan` is how
you find it again. Even so, consider setting the MQTT path up anyway: the office
could enable isolation at any point, and you will not be there when it happens.

**If it times out** — client isolation, as expected on a guest SSID. Go to
step 3. This is not something you can fix from the Pi.

## Step 3 — the broker path

### Get a broker (10 minutes, free)

1. Sign up at **console.hivemq.cloud** and create a free *Serverless* cluster.
2. Copy the cluster URL (`abc123….s1.eu.hivemq.cloud`), port **8883**.
3. Under *Access Management* create **two** credentials, so the Pi cannot read
   anything it should not:

   | User | Permission | Topic |
   |---|---|---|
   | `wq_pi` | publish + subscribe | `wq/#` |
   | `wq_desk` | subscribe | `wq/#` |

Your traffic is tiny — three sensors batched once a minute is a few thousand
messages a day, far inside the free tier.

### On the Pi

```bash
pip3 install paho-mqtt              # or: sudo apt install python3-paho-mqtt
cp wq_mqtt.ini.example wq_mqtt.ini
nano wq_mqtt.ini                    # host, username=wq_pi, password, device_id=pi-tank
chmod 600 wq_mqtt.ini

python3 pi_uplink.py --test         # connects, publishes once, tells you what happened
```

If `--test` times out, the office blocks outbound 8883. Tunnel it inside
ordinary web traffic instead, which essentially every network allows:

```bash
python3 pi_uplink.py --test --websockets      # port 8884
```

Then make it permanent, alongside the sensor server:

```bash
sudo cp wq-server.service wq-uplink.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wq-server wq-uplink
journalctl -u wq-uplink -f
```

### On the PC

```bash
pip install paho-mqtt matplotlib
copy wq_mqtt.ini.example wq_mqtt.ini     # username=wq_desk this time
python3 pc_dashboard.py --mqtt --session officepc --clock pc
```

`--session officepc` is worth using. It tells the broker to keep a queue for
this PC, so readings taken overnight while the dashboard was closed arrive the
next morning instead of being lost. I tested this: 40 seconds of readings taken
with the dashboard shut were delivered intact on reconnect, with no gap and no
duplicates.

### What the uplink handles

- **Batching.** One message a minute by default rather than one every five
  seconds. Waking the Wi-Fi radio is what costs battery, not the bytes.
- **Outages.** With the broker unreachable it keeps queueing; when the link
  returns it sends everything it held. Tested across a 63-second outage —
  the CSV came out continuous, no gap, no duplicates.
- **Restarts.** A cursor on disk means a restarted uplink does not re-send what
  it already sent, and a restarted `pi_server.py` is detected and resynced.
- **Liveness.** A last-will message means the dashboard header goes red when
  the Pi dies, instead of just going quiet.
- **It does not read the sensors.** `pi_server.py` still does that, so the
  local web page still works, the spool file still records everything, and if
  the uplink fails you lose visibility, not data.

---

## The hard part: battery

You said there is no mains at the tank. This is now the thing that limits the
deployment, so here is the arithmetic rather than a reassurance.

Runtime ≈ pack watt-hours × 0.85 (converter losses) ÷ load watts.

| Battery | Pi 4 (~3.4 W) | Pi Zero 2 W (~1.3 W) |
|---|---|---|
| 3S 2200 mAh LiPo (24 Wh) | **6 h** | 16 h |
| 10,000 mAh power bank (37 Wh) | 9 h | 24 h |
| 20,000 mAh power bank (74 Wh) | 18 h | 48 h |
| 12 V 7 Ah sealed lead-acid (84 Wh) | 20 h | 55 h |
| 12 V 18 Ah sealed lead-acid (216 Wh) | 50 h | **6 days** |

The pack in your photo is in the first row. With the Pi 4 that is an afternoon,
not a deployment.

Three honest options:

1. **Find mains anyway.** An office tank is usually within extension-cord reach
   of a socket, and a 5 V 3 A adapter removes this entire section — no battery,
   no buck converter, no XT connectors, no swap schedule. If there is any way
   to get power there, this is worth an awkward conversation with facilities.
2. **Pi Zero 2 W plus a 12 V 18 Ah battery.** About six days per charge, so a
   weekly swap. This is the realistic battery-only build, and it is the strongest
   argument yet for the board swap from the enclosure notes.
3. **Hardware duty cycling.** A Pi cannot sleep; it idles at nearly full power
   whatever your sampling interval. Getting to weeks means physically cutting
   power between samples with a scheduler board (Witty Pi, PiJuice): wake every
   15 minutes, sample, publish, power down. Roughly 10× the runtime, at the cost
   of a much coarser dataset and a more complex build.

Before committing, **measure it**. A USB power meter (~₹300) inline for an hour
gives you the real number for your build, which beats my estimate.

Whatever you choose, `--batch 300` (one upload every five minutes) and
`interval=30` in `sensors.py` cost you very little data for a water tank, where
nothing changes in seconds.

---

## In and around the tank

**Get permission first.** This is shared office water, quite possibly drinking
water. Talk to facilities before anything goes in it, use probes rated for
potable water, and do not leave anything in the tank that could shed material
or come loose. Keep mains electricity, if you end up using it, properly away
from the water and on an RCD.

**Mounting the probes.** Fix them at a depth that stays submerged at the tank's
*lowest* normal level — a pH electrode that dries out is damaged, often
permanently. Keep them away from the inlet: incoming flow entrains air, and
bubbles on the optical path make turbidity readings jump. A rigid bracket beats
hanging them on their cables, which stresses the connectors.

**Keep water out of the box.** Mount the enclosure above the waterline, and put
a drip loop in every probe cable so water runs off the lowest point of the loop
instead of tracking down the cable into a gland. This single habit prevents
most flooded enclosures.

**Calibrate before you deploy, not after.** pH with 4/7/10 buffers, TDS against
a known solution. Write the date and the calibration constants in the project
notes. In a closed tank, biofilm builds up on the probes over weeks and makes
readings drift low and lazy — plan to clean and recalibrate monthly, and treat
a slow unexplained drift as fouling until proven otherwise.

---

## Weekly check, once it is running

- Dashboard header green, readings arriving: `python3 pc_dashboard.py --mqtt --session officepc`
- No sensor errors: look at the `errors` field in the status, or `curl http://<pi>:8000/health` if you can reach it
- Battery swapped or charged, if you went that route
- `journalctl -u wq-uplink -n 50` on the Pi for repeated reconnects — a lot of them usually means weak signal at the tank
- Spool file is growing but not unbounded: `ls -lh ~/water-quality/wq_spool.jsonl` (rotate it, see the enclosure notes)
- Probes cleaned, and recalibrated monthly

## If the readings stop

| Symptom | Where to look |
|---|---|
| Header red, "offline" from the last will | The Pi lost power or lost Wi-Fi. Battery first, then signal strength at the tank. |
| Header red, no status at all | The PC cannot reach the broker. Check the office network, then the broker's status page. |
| Readings arrive in bursts | The uplink was offline and is catching up. Normal. |
| One metric missing, others fine | That probe's function is throwing. `health` lists it under `errors`. |
| Everything up, values obviously wrong | Fouling or a drifting calibration. Clean and recalibrate before suspecting the code. |
| Nothing works and you need the data | Every reading is in `wq_spool.jsonl` on the Pi's SD card, whether or not it was ever uploaded. |

## Security note

These readings cross the internet to a third-party broker. TLS is on by default
and should stay on, the Pi's credential is scoped to `wq/#`, and
`wq_mqtt.ini` holds a password so `chmod 600` it and keep it out of git. Do not
use the public `test.mosquitto.org` for anything real — anyone can read it, and
anyone can inject fake readings into your dataset.
