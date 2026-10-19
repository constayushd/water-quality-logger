# Fitting the build into the enclosure

Notes from the photo of the current build. The box itself is the right choice —
plastic (so it does not block the Wi-Fi), moulded mounting bosses in the base,
glands already fitted. The problem is not space. It is that nothing is mounted,
the power distribution is sized for a drone, and three separate sensor boards
are doing a job one board can do.

Worked through in order of payoff.

---

## 1. Replace the XT60 distribution board with a terminal block

This is the single biggest space win and it costs almost nothing.

That black board with eight yellow XT60 connectors is a multirotor power
distribution board rated for tens of amps. The whole system here draws **under
1.5 A**. It is occupying roughly a fifth of the usable floor area to do a job a
₹30 part does better.

Replace it, and the chain of XT60s and butt splices, with:

```
battery ── XT30 ──[ fuse 3 A ]──[ switch ]──► buck 5 V ──┬──► Pi
                                                          └──► sensor board
```

- **XT30 instead of XT60** — same current headroom for this load, a third of the volume.
- **One 2-pole or 4-pole screw terminal block** for the 5 V distribution instead of soldered splices. Changing a wire later becomes a screwdriver job.
- **Shorten everything.** Those silicone leads are 150–200 mm serving 60 mm gaps. Loose wire is the main reason a box looks full when it is not.

> **Fit a fuse.** I do not see one in the photo. A Li-ion pack with no fuse, in a
> sealed plastic box, with loose wiring, is the one genuinely dangerous thing in
> this build. An inline 3 A automotive blade fuse holder costs about ₹40 and goes
> immediately at the battery positive — before the switch, before everything.

## 2. One ADS1115 instead of three sensor boards

pH, turbidity and TDS all produce an **analog voltage**. The Pi has no analog
input, which is why each of those modules has its own interface board.

A single **ADS1115** (4-channel, 16-bit, I²C) reads all three, with a spare
channel left over:

| Channel | Probe |
|---|---|
| A0 | pH board output |
| A1 | turbidity output |
| A2 | TDS output |
| A3 | spare — temperature, or battery voltage |

What this removes: two interface boards, the breadboard, and most of the jumper
wires. What reaches the Pi is **four wires total** — 3V3, GND, SDA, SCL — and
I²C is addressable, so the DS3231 clock module from the field guide shares the
same four wires. No extra pins.

The pH board still has to stay (its job is buffering a very high impedance
electrode, which the ADS1115 cannot do). The turbidity and TDS modules are often
just a voltage divider and an op-amp and can usually go straight to the ADC —
check your specific modules' output pins first.

16-bit also beats the 10-bit ADC most of those boards assume, so your pH
resolution improves as a side effect.

## 3. Get rid of the breadboard

A breadboard in a box that will be carried to water is a list of future
intermittent faults — contacts loosen with vibration and corrode in humidity,
and the failure looks like a drifting sensor, not a loose wire, so you will
debug the wrong thing for an evening.

You said you would rather not solder but can. This is where it pays:

**A perfboard "HAT"** — a 40-pin stacking header, the ADS1115 and DS3231
soldered down, and 3-pole screw terminals at the edge for each probe. One
afternoon, about ₹200, and it removes the breadboard, the jumper nest and the
mounting problem in one move, because the board then sits on the Pi's own
standoffs.

If you want zero soldering: a **screw-terminal HAT** plus pre-made modules works
and is only slightly bulkier, but every connection is still a screw that can
back out — torque them properly and add a dab of nail varnish as thread-lock.

## 4. Mount everything

Nothing in the photo is fastened. In a box that gets carried, loose boards
short against each other and wires fatigue at the joints.

The base has moulded bosses. Use them:

- Cut a **3 mm acrylic or FR4 plate** to the inner floor dimensions, drill it to match the bosses, and mount it on 5 mm standoffs.
- Mount every board **on the plate** with M2.5 nylon standoffs — Pi, sensor board, buck converter.
- Lift the plate off the floor so condensation runs underneath rather than through your electronics.

Then zip-tie or use adhesive cable clips so no wire crosses the box unsupported.

## 5. Separate the noisy half from the quiet half

Right now the buck converter, the battery leads and the analog sensor wiring are
all intermingled. A switching regulator is a radio transmitter you did not ask
for, and it degrades two things at once: your analog readings, and the Wi-Fi.

Divide the floor into zones:

```
 ┌──────────────────────────────────────────────┐
 │  POWER                    │  Pi              │  ← Pi's antenna corner
 │  fuse, switch, buck,      │  (antenna edge   │     points at this wall,
 │  terminal block           │   toward wall)   │     away from the buck
 │───────────────────────────┤                  │
 │  ANALOG                   │                  │
 │  pH board, ADS1115,       │                  │
 │  probe terminals          │                  │
 └──────────────────────────────────────────────┘
      probe glands this side        maintenance side
```

- Power section in one corner, analog section away from it, probe cables entering through the glands nearest the analog section.
- Run probe wiring **along the edges**, never across the buck converter.
- A **100 nF ceramic + 10 µF electrolytic** across the ADS1115 supply pins, right at the chip, costs ₹5 and visibly cleans up the readings.
- Keep the Pi's antenna corner (the edge opposite the USB ports) facing an outside wall, not buried behind the battery.

## 6. Pi 4 or Pi Zero 2 W

You asked for the trade-off, so:

| | **Pi 4** (what you have) | **Pi Zero 2 W** |
|---|---|---|
| Footprint | 85 × 56 mm | 65 × 30 mm — about 40% of the area |
| Draw, running this | ~700–900 mA at 5 V | ~250–350 mA |
| Battery life, same pack | baseline | roughly **2.5× longer** |
| Heat in a sealed box | noticeable, wants a heatsink | barely warm |
| Running the hotspot + server | effortless | still effortless — this workload is tiny |
| Ethernet | yes, useful for recovery | no (USB adapter only) |
| Cost | already owned | ~₹1,500 |

**My read:** the workload is three analog reads and an HTTP server — the Zero 2 W
is not a compromise here, it is correctly sized, and the battery gain matters
more than anything else on this list for a field deployment.

But you already own the Pi 4, and it fits. So: **do items 1–5 with the Pi 4
first.** They are what is actually making the box look full, and they apply
unchanged to either board. If, once it is tidy, battery life is the thing
limiting your deployment, swap to the Zero 2 W then — the software and the
sensor board carry across with no changes.

If you do keep the Pi 4: add a small heatsink, and power it through the **GPIO
pins (5 V on 2 or 4, GND on 6)** from the buck rather than a USB-C cable. That
removes a bulky right-angle cable and a connector that works loose. Make sure
the buck is set to 5.1 V before connecting it — the GPIO route bypasses the Pi's
input protection, so get this right with a multimeter first, with the Pi
unplugged.

## 7. Two things the photo does not show but the water will

**Condensation.** A sealed box that warms in the day and cools at night will
rain on the inside. Fit a **Gore-Tex vent plug** (M12, ~₹150) in one spare
gland hole — it equalises pressure and lets vapour out while keeping the IP
rating, and drop a silica gel sachet in. This is the difference between an
enclosure that survives a season and one that corrodes in three weeks.

**Probe cross-talk.** A TDS probe pushes current through the water; a pH probe
measures a tiny voltage across that same water. In one tank on a shared ground,
the TDS drive lands as noise on your pH reading. Two fixes, use both:

- *Software, already done:* `sensors.py` now has `EXCLUSIVE_SAMPLING = True`, so
  the three reads take turns with a short settle between them instead of
  overlapping. It is on by default; a probe that hangs can no longer block the
  others either.
- *Hardware:* power the TDS probe through a small N-channel MOSFET on a GPIO pin
  and energise it only while reading. `sensors.py` has a `powered(pin)` helper
  for this. It removes the interference at source and roughly doubles the life
  of a cheap EC electrode, which corrodes whenever it is energised in water.

---

## Parts list

Everything that replaces something currently in the box:

| Part | Why | Approx. |
|---|---|---|
| ADS1115 module | replaces 2 interface boards + breadboard | ₹150 |
| Perfboard + 40-pin stacking header | the sensor board | ₹80 |
| 3-pole screw terminals × 4 | probe and power connections | ₹60 |
| Inline blade fuse holder + 3 A fuse | **safety** | ₹40 |
| Panel-mount rocker or toggle switch | stop unplugging XT60s | ₹40 |
| XT30 pairs × 2 | replaces XT60 chain | ₹60 |
| M2.5 nylon standoff kit | mounting | ₹100 |
| 3 mm acrylic sheet, cut to floor | mounting plate | ₹100 |
| Gore-Tex M12 vent plug | condensation | ₹150 |
| Silica gel sachets | condensation | ₹20 |
| DS3231 RTC module | the clock problem, from the field guide | ₹100 |
| 2N7000 / IRLZ44N MOSFET + 10 kΩ | TDS power gating | ₹20 |
| Heatsink (if keeping the Pi 4) | heat in a sealed box | ₹50 |
| *Optional:* Pi Zero 2 W | space and battery life | ₹1,500 |

Under ₹1,000 without the Zero.

## Order of work

1. Fuse and switch — before anything else, and do not skip it.
2. Strip out the XT60 board and the butt splices; rewire with XT30 and a terminal block, shortest sensible lengths.
3. Build the perfboard HAT (ADS1115 + DS3231 + screw terminals); test it on the bench with the Pi before it goes in the box.
4. Cut the mounting plate, fit standoffs, mount everything in zones.
5. Re-run `python3 sensors.py` on the bench, then `python3 pi_server.py --once`, before closing the lid.
6. Fit the vent plug and silica gel, close it, and do the reboot test from the field guide.

Photograph the inside before you close it. You will want it in six months.
