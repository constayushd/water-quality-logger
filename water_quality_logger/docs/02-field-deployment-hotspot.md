# Going wireless: the Pi as its own access point

The Ethernet version works. This is the same software with the cable removed —
the Pi broadcasts its own Wi-Fi network, you walk up with the laptop, join it,
and the dashboard runs exactly as before.

```
   at the water                                    you, within ~30 m
 ┌──────────────────────────┐                   ┌────────────────────────┐
 │ Raspberry Pi             │  SSID: WQ-FIELD   │ laptop                 │
 │  pH / turbidity / TDS    │ ((( ·  ·  · )))   │  joins the Pi's Wi-Fi  │
 │  pi_server.py  :8000     │ ───────────────►  │  gets 192.168.4.x      │
 │  access point 192.168.4.1│   WPA2            │  pc_dashboard.py       │
 └──────────────────────────┘                   └────────────────────────┘
        no router, no internet, no SIM — the Pi *is* the network
```

**Nothing in `pi_server.py`, `sensors.py` or `pc_dashboard.py` changes.** They
were always talking over IP; this only changes which network that IP lives on.

## What you gain and what you give up

| | |
|---|---|
| ✅ Works anywhere | A pond, a tank, a treatment plant — no infrastructure at all. |
| ✅ Nothing to configure on site | The Pi brings its network up at boot, before you arrive. |
| ✅ Private | Only someone with the password and within radio range can connect. |
| ❌ You must be nearby | ~30–50 m with the Pi's internal antenna, less through walls. |
| ❌ No internet on the Pi | Which means no NTP — see "The clock problem" below. |
| ❌ One radio, one job | While it is an access point the Pi cannot also be a Wi-Fi client, so you lose SSH over your home/campus network. You SSH to `192.168.4.1` after joining its network instead. |

If you need readings *without* being on site, that is the MQTT broker design in
`pi-to-desktop-communication.md` plus a 4G dongle or a phone hotspot — the two
approaches can coexist, see the last section.

---

## 1. Set it up over the Ethernet cable, before you go

Do this now, while the cable still works. Copy the script to the Pi and run it:

```bash
scp setup_hotspot.sh pi@raspberrypi.local:~
ssh pi@raspberrypi.local
chmod +x setup_hotspot.sh
sudo ./setup_hotspot.sh --ssid WQ-FIELD --pass 'pick-something-real' --country IN
```

It configures NetworkManager to run `wlan0` as a WPA2 access point on channel 6,
gives the Pi the fixed address `192.168.4.1`, hands out addresses to whoever
joins, turns Wi-Fi power saving off (a common cause of dropouts), and marks the
connection to come up at every boot.

> **The moment it finishes, the Pi leaves your current Wi-Fi.** Keep the Ethernet
> cable plugged in until you have tested the hotspot, so you always have a way
> back in. `sudo ./setup_hotspot.sh --revert` undoes everything.

Check it:

```bash
sudo ./setup_hotspot.sh --status
```

## 2. Make the sensor server start by itself

A headless Pi has nobody to type `python3 pi_server.py` after a power cut:

```bash
sudo cp wq-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wq-server
systemctl status wq-server
```

Then the watchdog, which checks once a minute that the access point is up and
the server is listening, and repairs both if not:

```bash
sudo cp wq-ap-watchdog.sh /usr/local/bin/
sudo chmod +x /usr/local/bin/wq-ap-watchdog.sh
sudo cp wq-ap-watchdog.service wq-ap-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now wq-ap-watchdog.timer
journalctl -t wq-ap-watchdog -f
```

After three failed repairs in a row it reloads the Wi-Fi driver, which clears
the firmware wedge the Pi's `brcmfmac` chip occasionally gets into — the failure
mode where the AP is "up" but nothing can associate with it.

## 3. Connect the laptop

Unplug the Ethernet, reboot the Pi, and from the laptop:

1. Join the Wi-Fi network **WQ-FIELD** with the password you set.
2. Windows will warn "no internet access" — that is correct and expected. Tell
   it to stay connected. (On a phone, turn mobile data off if you want the
   browser to use the Pi's network.)
3. Then:

```bash
ping 192.168.4.1
python3 pc_dashboard.py --host 192.168.4.1 --clock pc
```

or just open **http://192.168.4.1:8000/** in any browser — including a phone's,
which is the quickest way to check the sensors while standing at the water.

`raspberrypi.local` also works over the hotspot, so `--host raspberrypi.local`
is fine if you prefer the name.

---

## The clock problem

This is the one that quietly ruins a dataset, so it is worth two minutes.

A Pi has no battery-backed clock. With internet it asks an NTP server at boot
and is accurate to milliseconds. **As an isolated access point it has no
internet**, so at every boot it resumes from whatever `fake-hwclock` saved at
the last clean shutdown — and after a power cut, from a time that may be hours
or days stale. Every reading it timestamps is then wrong, and your CSV is wrong
in a way that is hard to notice later.

Two fixes, use either or both:

**Correct on the laptop (free, already done).** `--clock pc` measures the offset
between the two machines and rewrites each timestamp using your laptop's clock,
which *is* NTP-synced. The Pi's original value is kept in the `pi_timestamp`
column so nothing is lost:

```bash
python3 pc_dashboard.py --host 192.168.4.1 --clock pc
```

The dashboard warns you on connect if the offset is more than a few seconds.
The limitation: it only fixes readings while you are connected. Anything in the
Pi's `--spool` file from before you arrived still carries the Pi's own idea of
the time.

**Fix it at the source (≈ ₹100, 5 minutes).** A DS3231 RTC module on the Pi's
I²C pins keeps time for years on a coin cell:

```bash
sudo raspi-config        # Interface Options → I2C → enable
echo "dtoverlay=i2c-rtc,ds3231" | sudo tee -a /boot/firmware/config.txt
sudo reboot
# after reboot, with the clock still correct from the Ethernet session:
sudo hwclock -w          # write system time into the module
sudo hwclock -r          # read it back
```

From then on the Pi boots with the right time even after days without power.
For a deployment that logs unattended, this is the right answer.

Whenever you are on site you can also just push the laptop's time across:

```bash
ssh pi@192.168.4.1 "sudo date -s '$(date -u '+%Y-%m-%d %H:%M:%S')' && sudo hwclock -w"
```

---

## Range, placement and power

**Radio.** Stay on 2.4 GHz (the script does). It travels roughly twice as far as
5 GHz and through more material. Expect 30–50 m outdoors with line of sight, and
much less through concrete or around a corner. A Pi 4/5's internal antenna is
adequate; a Pi with a U.FL connector and a small external antenna roughly
doubles useful range.

**Enclosure.** A metal box is a Faraday cage — it will cut the signal to almost
nothing. Use a plastic/ABS IP65 box, and keep the Pi's antenna edge (the corner
away from the USB ports) facing out and away from the metal of any tank or rail.
Raising the Pi half a metre off wet ground helps more than most people expect.

**Channel.** Channel 6 is the default. If the site has other 2.4 GHz networks,
check what is busy and move to 1 or 11:

```bash
sudo iw dev wlan0 scan | grep -E 'SSID|freq'      # before the AP is up
sudo ./setup_hotspot.sh --channel 11 --ssid WQ-FIELD --pass '...'
```

**Power.** Running as an access point draws noticeably more than idling, and a
Pi that browns out mid-write can corrupt the SD card. Use the official supply or
a 5 V 3 A one; if the site's power is unreliable, a small USB UPS HAT with an
18650 cell is cheap insurance. Readings keep going into `wq_spool.jsonl` whether
or not anyone is connected, so an outage costs you the gap, not the dataset.

**SD card.** The spool file grows forever. At 3 sensors every 5 s it is roughly
80 MB a month — fine on a 32 GB card, but rotate it if the deployment is long:

```bash
# /etc/logrotate.d/wq-spool
/home/pi/water-quality/wq_spool.jsonl {
    weekly
    rotate 8
    compress
    copytruncate
    missingok
}
```

---

## Before you leave the site

A short list, because walking back is expensive:

- [ ] `sudo ./setup_hotspot.sh --status` — AP active, address is 192.168.4.1, something listening on 8000
- [ ] `systemctl is-enabled wq-server wq-ap-watchdog.timer` — both say `enabled`
- [ ] **Reboot the Pi and reconnect.** This is the test that matters: `sudo reboot`, wait 60 s, rejoin WQ-FIELD, `python3 pc_dashboard.py --host 192.168.4.1 --clock pc`. If it comes back by itself, it will come back after a power cut too.
- [ ] Real readings, not errors: `curl http://192.168.4.1:8000/health` → `"errors": {}`
- [ ] Walk to the far end of where you expect to stand and check the dashboard header is still green
- [ ] Clock is right: `ssh pi@192.168.4.1 date`
- [ ] Spool is being written: `ls -l ~/water-quality/wq_spool.jsonl`

## When you cannot reach it

| Symptom | What to try |
|---|---|
| The network WQ-FIELD does not appear | The Pi is off, or the radio is blocked. Power-cycle it; the watchdog recovers most software failures within a minute or two. |
| It appears but the laptop will not join | Wrong password, or the laptop remembered a bad profile — "forget" the network and rejoin. |
| Joined, but `ping 192.168.4.1` fails | The laptop got no DHCP lease. Check it has a `192.168.4.x` address (`ipconfig` / `ip a`); if it has a `169.254.x.x` one, rejoin. |
| Ping works, dashboard does not | `pi_server.py` is not running. `ssh pi@192.168.4.1`, then `systemctl status wq-server` and `journalctl -u wq-server -n 50`. |
| Everything is up but a metric is missing | That sensor function is throwing. `curl http://192.168.4.1:8000/health` lists it under `errors`. |
| Connection drops every few minutes | Power saving came back on, or you are at the edge of range. `iw wlan0 get power_save` should say `off`; move closer and re-test. |
| Nothing works and you need the data | Plug the Ethernet cable back in, or put the SD card in a reader: `wq_spool.jsonl` has every reading the Pi has taken. |

**The last-resort recovery**, worth doing before deployment: put a file named
`wq-rescue` on the SD card's boot partition and have the watchdog check for it —
or more simply, keep the Ethernet cable in the kit. The cable always works, and
`sudo ./setup_hotspot.sh --revert` puts the Pi back on normal Wi-Fi.

---

## Combining this with remote access

These are not exclusive. A sensible production setup is:

- **The hotspot**, always on, for walking up and checking the site in person.
- **The MQTT broker path** from `pi-to-desktop-communication.md`, for watching
  from anywhere — add a 4G USB dongle or a second Wi-Fi adapter so the Pi has an
  uplink while `wlan0` stays an access point. The publisher spools to disk when
  the uplink is down, so patchy coverage costs nothing.

With `ipv4.method shared` the Pi also shares any internet it has with whoever
joins its network — so when a dongle is plugged in, your laptop gets internet
through the Pi as well.
