# The networking layer

The logger reads sensors and writes a CSV on the Pi. These files add the part
that gets those readings onto another machine — over the LAN, over the Pi's own
hotspot, or through an MQTT broker when the network will not let two devices
talk to each other.

Nothing here replaces the logger's sensor drivers. There is one sensor layer,
in `sensors/`, and one config, in `config.yaml`.

| File | Runs on | What it is |
|---|---|---|
| `pi_server.py` | Pi | Samples the sensors and serves them over HTTP: `/latest`, `/readings?since=N`, `/stream`, and a live page at `/`. |
| `wq_bridge.py` | Pi | Adapts `sensors/` + `config.yaml` to what `pi_server.py` expects. The reason there is no second sensor layer. |
| `pi_uplink.py` | Pi | Publishes readings to an MQTT broker, for when the PC cannot reach the Pi. Can take them from `pi_server.py` **or** tail the CSV `main.py` writes. |
| `wq_mqtt.py` | both | Broker settings, topics, paho version differences. |
| `pc_dashboard.py` | PC | Live plots and a CSV log, fed either by polling the Pi or by subscribing to the broker. |
| `scripts/` | Pi | Join a Wi-Fi network, or become one; a watchdog for the hotspot. |
| `systemd/` | Pi | Units so all of it starts at boot. |

## One rule: only one process owns the hardware

An I²C bus or an RS485 port cannot be opened twice. `main.py` and
`pi_server.py` both want to read the sensors, so **run one or the other**,
never both. That gives two ways to deploy:

### A. Keep `main.py` as the sampler, tail its CSV

Nothing about the logger changes. The uplink follows the file it is already
writing and publishes each new row.

```bash
python3 main.py -c config.yaml                      # as you already run it
python3 pi_uplink.py --from-csv data/sensor_log.csv --device-id pi-tank
```

Lowest risk, and the obvious choice if the logger is already working. It reads
both CSV layouts (`wide` and `long`), survives the file being rotated, and
starts from the end so it does not republish history (`--backfill-csv` if you
want it to).

What you give up: no live HTTP view on the Pi, and readings reach the PC one
CSV row at a time, so the dashboard is as fresh as `interval_seconds`.

### B. Let `pi_server.py` be the sampler

Stop `main.py`; `pi_server.py` reads the same drivers through `wq_bridge.py`.

```bash
python3 wq_bridge.py --once          # check the bridge can drive your config
python3 pi_server.py                 # samples + serves on :8000
python3 pi_uplink.py                 # optional, forwards to the broker
```

You get `/latest`, `/stream`, a browser page you can open from a phone at the
tank, an in-memory buffer so a reconnecting PC fills its gap, and `--spool` to
the SD card. You lose `main.py`'s CSV unless you keep writing one from the
dashboard side.

Start with A. Move to B when you want the live view.

## Which transport

| Situation | Use |
|---|---|
| Pi and PC on the same network, able to reach each other | `pc_dashboard.py --host <pi>` — see `01-pi-to-pc-over-lan.md` |
| No network at the site | The Pi becomes an access point — `02-field-deployment-hotspot.md` |
| Office or guest Wi-Fi that isolates clients | MQTT broker — `03-office-tank-deployment.md` |

`01` through `04` are the full guides; `04` covers the enclosure and wiring.

## Configuration

`config.yaml` — the logger's, unchanged — says which probes exist.
`wq_mqtt.ini` — copy from `wq_mqtt.ini.example` — says where the broker is.
Both are in `.gitignore`; only the `.example` files are committed.

## A note on the sensors

The logger's drivers are pH over analog (ADS1115), turbidity over RS485, and DO
over RS485. Earlier enclosure notes in `04` assumed three analog probes feeding
one ADS1115 — that is wrong for this hardware. The ADS1115 is needed only for
pH; turbidity and DO share a single RS485 bus, which means two wires daisy
chained between them, distinct slave IDs (already in `config.yaml`), and a 120 Ω
termination resistor at each end of the run. That is a simpler build than the
one those notes describe, not a harder one.
