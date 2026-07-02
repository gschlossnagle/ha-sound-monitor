# ha-sound-monitor

Continuously monitors ambient sound levels and publishes **mean and max dBFS** per minute to Home Assistant via MQTT. Designed to track thermal expansion events (pops and creaks) in a house by capturing the characteristic pattern of brief, loud transients against a quiet baseline.

## How it works

- Captures audio from a USB microphone in 100 ms chunks
- Every 60 seconds, computes:
  - **Mean dBFS** — average loudness over the minute (ambient baseline)
  - **Max dBFS** — loudest single chunk (captures transient pops)
- Publishes both values to MQTT with Home Assistant auto-discovery
- A derived **Pop Index** sensor (`max − mean`) flags minutes with likely pop/creak events

## Hardware

- Any Raspberry Pi (Zero 2W, 3, 4, 5 all work)
- Any USB microphone (~$5–15). A directional mic pointed at the floor or wall where pops occur gives better isolation.

## Setup

### 1. Install dependencies

```bash
pip3 install -r requirements.txt
```

### 2. Find your microphone's device index

```bash
python3 -c "import sounddevice; print(sounddevice.query_devices())"
```

### 3. Configure

Copy the example config and fill in your values:

```bash
cp config.yaml.example config.yaml
```

`config.yaml` is gitignored since it holds MQTT credentials — never commit it.

| Key | Description |
|---|---|
| `mqtt.broker` | IP address of your MQTT broker (usually your HA host) |
| `mqtt.port` | MQTT broker port (default: `1883`) |
| `mqtt.user` / `mqtt.password` | Credentials for your Mosquitto broker |
| `device.name` | Friendly name shown in HA (e.g. `"Bedroom Sound Monitor"`) |
| `device.id` | Unique slug (no spaces) used for MQTT topics and entity IDs |
| `audio.sample_rate` | Sample rate in Hz (default: `44100`) |
| `audio.channels` | Number of input channels (default: `1`) |
| `audio.chunk_seconds` | Size of each audio chunk fed into the buffer (default: `0.1`) |
| `audio.device` | Device index from step 2, or `null` for system default |
| `interval_seconds` | Reporting interval in seconds (default: `60`) |

### 4. Test it

```bash
python3 sound_monitor.py
```

By default the script looks for `config.yaml` next to the script. To use a
config file elsewhere:

```bash
python3 sound_monitor.py --config /path/to/config.yaml
```

You should see a log line every minute like:
```
10:32:00  INFO      Published  mean=-51.3 dBFS  max=-34.7 dBFS
```

### 5. Install as a systemd service

```bash
# Copy files
cp sound_monitor.py config.yaml /home/pi/
sudo cp sound_monitor.service /etc/systemd/system/

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable --now sound_monitor

# Check status
sudo journalctl -u sound_monitor -f
```

## Home Assistant

Sensors appear automatically via MQTT discovery as soon as the script connects — no manual YAML needed for the core sensors.

The file `ha/sound_monitor.yaml` contains optional extras:

- **Pop Index template sensor** — `max_dbfs − mean_dbfs`; values above ~15–20 dB suggest a transient event in that minute
- **History graph dashboard card** — paste into a dashboard to visualise 24 hours of data
- **Automation** — optional notification when a large pop is detected

### What the values mean

dBFS (decibels relative to full scale) is always ≤ 0. The absolute values depend on your mic's gain; what matters is the relative pattern:

| Situation | Mean | Max | Pop Index |
|---|---|---|---|
| Quiet room, no events | −55 dBFS | −50 dBFS | ~5 dB |
| Background noise (HVAC, etc.) | −45 dBFS | −40 dBFS | ~5 dB |
| Single loud pop | −55 dBFS | −25 dBFS | ~30 dB |

## Project structure

```
ha-sound-monitor/
├── sound_monitor.py       # Main capture + MQTT publish script
├── sound_monitor.service  # systemd unit for auto-start on boot
├── config.yaml.example    # Config template — copy to config.yaml and edit
├── config.yaml            # Your local config (gitignored, holds credentials)
├── requirements.txt
├── ha/
│   └── sound_monitor.yaml # Optional HA template sensor, dashboard card, automation
└── README.md
```

## License

MIT
