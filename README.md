# ha-sound-monitor

Continuously monitors ambient sound levels and publishes **mean and max dBFS** per minute to Home Assistant via MQTT. Designed to track thermal expansion events (pops and creaks) in a house by capturing the characteristic pattern of brief, loud transients against a quiet baseline.

## How it works

- Captures audio from a USB microphone in 100 ms chunks
- Every 60 seconds, computes:
  - **Mean dBFS** — average loudness over the minute (ambient baseline)
  - **Max dBFS** — loudest single chunk (captures transient pops)
- Publishes both values to MQTT with Home Assistant auto-discovery
- A derived **Pop Index** sensor (`max − mean`) flags minutes with likely pop/creak events
- Detects discrete transient events on-device: each 10 ms frame is compared
  against a rolling L90 noise baseline, and frames exceeding it by a threshold
  trigger an event (with a refractory period so one pop counts once)
- Publishes each event immediately (peak dBFS, dB over baseline) plus an
  **events per minute** count alongside the minute stats
- Optionally saves a WAV clip (1 s before + 2 s after) of every event to
  `clips/` for ground-truth review

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
| `detection.enabled` | Enable on-device event detection (default: `true`) |
| `detection.frame_seconds` | Analysis frame size in seconds (default: `0.01` = 10 ms) |
| `detection.threshold_db` | dB above baseline required to trigger an event (default: `15`) |
| `detection.refractory_seconds` | Minimum gap between distinct events (default: `0.2`) |
| `detection.baseline_window_seconds` | Rolling window for the L90 baseline (default: `30`) |
| `detection.min_trigger_dbfs` | Absolute floor below which triggers are ignored (default: `-70`) |
| `clips.enabled` | Save a WAV clip per detected event (default: `true`) |
| `clips.directory` | Where clips are written, gitignored (default: `clips`) |
| `clips.pre_seconds` / `clips.post_seconds` | Audio kept around the trigger (default: `1.0` / `2.0`) |
| `clips.max_clips` | Oldest clips beyond this count are deleted; `0` keeps all (default: `200`) |
| `viewer.enabled` | Run the clip-review web UI (default: `true`) |
| `viewer.host` | Bind address — `0.0.0.0` for LAN, `127.0.0.1` for Pi-only (default: `0.0.0.0`) |
| `viewer.port` | Port for the clip viewer (default: `8099`) |

The `detection:` and `clips:` sections are optional — omit them entirely and
the defaults above apply.

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
10:32:00  INFO      Published  mean=-51.3 dBFS  max=-34.7 dBFS  events=0
```

and a line for each detected event as it happens:
```
10:32:14  INFO      Event  peak=-18.3 dBFS  (+41.2 dB over baseline)
10:32:14  INFO      Saved clip clips/20260702_103214_-18.3dBFS.wav
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

Four sensors are created: **Mean dBFS**, **Max dBFS**, **Events Per Minute**,
and **Last Event Peak**. Each event is also published as JSON to
`home/<device_id>/event` (`timestamp`, `peak_dbfs`, `baseline_dbfs`,
`over_baseline_db`) for automations that want per-event triggers.

Note: the event JSON and the **Last Event Peak** sensor report the peak at the
moment the event triggered. A saved clip's filename may show a slightly higher
peak, since a louder frame arriving within the refractory window updates the
clip's recorded peak but not the already-published event.

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

## Reviewing clips

When clip capture is enabled, `clip_viewer.py` serves the saved WAVs as a
simple web page so you can review them from any device on your LAN — list
newest-first, play inline, download, or delete false positives.

Run it on the Pi:

```bash
python3 clip_viewer.py
```

then open `http://<pi-address>:8099/` in a browser.

To keep it always available, install it as a service (alongside the main one):

```bash
cp clip_viewer.py /home/pi/
sudo cp clip_viewer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now clip_viewer
```

It reads the same `config.yaml` as the monitor (using `clips.directory`), so
no extra setup is needed beyond the optional `viewer:` section.

Prefer to pull clips off the Pi instead of browsing in place? A plain rsync
works too:

```bash
rsync -av pi@<pi-address>:~/clips/ ./clips/
```

## Project structure

```
ha-sound-monitor/
├── sound_monitor.py       # Main capture + MQTT publish script
├── event_detection.py     # EventDetector + ClipRecorder (no hardware deps)
├── clip_viewer.py         # LAN web UI for reviewing saved clips
├── sound_monitor.service  # systemd unit for the capture service
├── clip_viewer.service    # systemd unit for the clip viewer
├── config.yaml.example    # Config template — copy to config.yaml and edit
├── config.yaml            # Your local config (gitignored, holds credentials)
├── requirements.txt
├── requirements-dev.txt   # pytest, for running the test suite
├── tests/
│   ├── test_event_detection.py
│   └── test_clip_viewer.py
├── clips/                 # Saved event WAVs (gitignored)
├── ha/
│   └── sound_monitor.yaml # Optional HA template sensor, dashboard card, automation
└── README.md
```

## License

MIT
