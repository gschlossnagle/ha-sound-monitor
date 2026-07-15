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
- Publishes the detector's **L90 baseline** (the ambient floor, exceeded 90%
  of the time) as its own sensor — a spike-immune companion to Mean dBFS,
  since a percentile ignores the loud transients an energy average would absorb
- Optionally saves a WAV clip (1 s before + 2 s after) of every event to
  `clips/` for ground-truth review. A flurry of pops close together is kept in
  a single clip that extends to 2 s past the last pop (bounded by
  `max_clip_seconds`) and is named after the loudest pop it contains

## Hardware

- Any Raspberry Pi (Zero 2W, 3, 4, 5 all work)
- Any USB microphone (~$5–15). A directional mic pointed at the floor or wall where pops occur gives better isolation.

## Setup

### 1. Install dependencies

Raspberry Pi OS marks the system Python as "externally managed" (PEP 668),
so `pip3 install` system-wide is refused. Install into a virtual environment
instead:

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

The rest of this guide calls `venv/bin/python` directly so nothing depends on
having the venv activated. If you'd rather activate it, run
`source venv/bin/activate` and then plain `python`/`pip` work as usual.

### 2. Find your microphone's device index

```bash
venv/bin/python -c "import sounddevice; print(sounddevice.query_devices())"
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
| `clips.pre_seconds` | Audio kept before the first pop (default: `1.0`) |
| `clips.post_seconds` | Audio kept after the *last* pop in a flurry (default: `2.0`) |
| `clips.max_clip_seconds` | Ceiling on one clip's post-roll so a long flurry can't record forever; `0` disables (default: `60`) |
| `clips.max_clips` | Oldest clips beyond this count are deleted; `0` keeps all (default: `200`) |
| `clips.max_storage_mb` | Oldest clips are deleted to keep the `clips/` dir under this size in MB; `0` disables (default: `1000` ≈ 1 GB) |
| `viewer.enabled` | Run the clip-review web UI (default: `true`) |
| `viewer.host` | Bind address — `0.0.0.0` for LAN, `127.0.0.1` for Pi-only (default: `0.0.0.0`) |
| `viewer.port` | Port for the clip viewer (default: `8099`) |

The `detection:` and `clips:` sections are optional — omit them entirely and
the defaults above apply.

### 4. Test it

```bash
venv/bin/python sound_monitor.py
```

By default the script looks for `config.yaml` next to the script. To use a
config file elsewhere:

```bash
venv/bin/python sound_monitor.py --config /path/to/config.yaml
```

You should see a log line every minute like:
```
10:32:00  INFO      Published  mean=-51.3  max=-34.7  baseline=-54.8 dBFS  events=0 (0.0/min)
```
(`baseline` shows `n/a` for the first minute while the detector warms up, or
whenever detection is disabled.)

and a line for each detected event as it happens:
```
10:32:14  INFO      Event  peak=-18.3 dBFS  (+41.2 dB over baseline)
10:32:14  INFO      Saved clip clips/20260702_103214_-18.3dBFS.wav
```

### 5. Install as a systemd service

```bash
# Copy the app files (event_detection.py is imported by sound_monitor.py,
# and requirements.txt is needed to build the venv on the Pi)
cp sound_monitor.py event_detection.py requirements.txt config.yaml /home/pi/

# Build the venv at its final location — a venv is not relocatable, so it
# must be created on the Pi, in the directory the service will run from
python3 -m venv /home/pi/venv
/home/pi/venv/bin/pip install -r /home/pi/requirements.txt

# Install and start the service
sudo cp sound_monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sound_monitor

# Check status
sudo journalctl -u sound_monitor -f
```

The unit ships with `User=pi` and paths under `/home/pi`. **On any other host
(different username or home directory) edit `User=`, `WorkingDirectory=`, and
the venv path in `ExecStart=` in `/etc/systemd/system/sound_monitor.service`
to match, then `sudo systemctl daemon-reload`** — a mismatch there is what
produces a `status=217/USER` failure at startup.

## Home Assistant

Sensors appear automatically via MQTT discovery as soon as the script connects — no manual YAML needed for the core sensors.

Up to five sensors are created: **Mean dBFS**, **Max dBFS**, **Events Per
Minute**, **Last Event Peak**, and — when detection is enabled — **Baseline
dBFS** (the L90 ambient floor). Baseline dBFS is the spike-immune counterpart
to Mean dBFS: a quiet room reads the same on it whether or not pops occurred
that minute, so comparing the two (or watching Max rise above Baseline) is a
clean way to spot transient activity. Each event is also published as JSON to
`home/<device_id>/event` (`timestamp`, `peak_dbfs`, `baseline_dbfs`,
`over_baseline_db`) for automations that want per-event triggers.

Independent of the audio pipeline, `sound_monitor` also publishes its own
resource usage and board health every `system.interval_seconds` (default
60s): **CPU %** and **Memory MB** (the `sound_monitor` process itself),
**Core Voltage**, **CPU Temp**, **Swap Used %**, **Load Average (1m)**,
**SD Card Free %**, and a binary **Under-Voltage** sensor. These publish on
their own thread, decoupled from the audio watchdog, so they keep
reporting even if the audio stream itself is stalled or restarting.
Requires `vcgencmd` (present on Raspberry Pi OS); set `system.enabled:
false` in `config.yaml` if running this off a Pi.

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

Run it on the Pi (uses the same venv as the monitor):

```bash
venv/bin/python clip_viewer.py
```

then open `http://<pi-address>:8099/` in a browser.

To keep it always available, install it as a service (alongside the main one).
It reuses the monitor's venv and `/home/pi` deploy directory, so there are no
extra dependencies to install:

```bash
cp clip_viewer.py /home/pi/
sudo cp clip_viewer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now clip_viewer
```

It reads the same `config.yaml` as the monitor (using `clips.directory`), so
no extra setup is needed beyond the optional `viewer:` section. (As with the
main service, adjust `User=`/paths in the unit for non-Pi hosts.)

Prefer to pull clips off the Pi instead of browsing in place? A plain rsync
works too:

```bash
rsync -av pi@<pi-address>:~/clips/ ./clips/
```

## Troubleshooting

Deploying to a real Pi surfaces a predictable sequence of host-specific
issues. These are the ones you're most likely to hit, in roughly the order
they appear, with the symptom in the journal and the fix.

| Symptom (in `journalctl -u sound_monitor`) | Cause | Fix |
|---|---|---|
| `status=217/USER`, "Failed to determine user credentials" | The unit's `User=` doesn't exist on this host (it ships as `pi`) | Edit `User=`, `WorkingDirectory=`, and the `ExecStart=` venv path in `/etc/systemd/system/*.service` to match your host, then `sudo systemctl daemon-reload` |
| `status=203/EXEC` | `ExecStart=` points at a venv/python path that doesn't exist | Confirm the venv was created at that exact path and `ExecStart` matches it |
| `error: externally-managed-environment` on `pip install` | Pi OS (PEP 668) forbids system-wide pip | Install into a venv: `python3 -m venv venv && venv/bin/pip install -r requirements.txt` |
| `ModuleNotFoundError: No module named 'yaml'` / `'sounddevice'` | Service is running system Python, not the venv | Re-copy the updated `.service` (its `ExecStart` uses `/home/pi/venv/bin/python`), `daemon-reload`, restart |
| `PortAudioError: Error querying device -1` | No default input device — `audio.device: null` finds nothing under a headless service (no sound-server session) | Pin the mic explicitly, e.g. `audio.device: "ATR4697"` (a substring of its name) or `"hw:1,0"` |
| `arecord -l` as the service user says "no soundcards found" | The service user isn't in the `audio` group, so `/dev/snd/*` is unreadable | `sudo usermod -aG audio <user>` then restart the service (no reboot needed) |
| `arecord -l` shows the card but `Subdevices: 0/1`, and `query_devices()` omits it | The mic is busy — another process (often a desktop PipeWire/PulseAudio session) holds it open | `sudo fuser -v /dev/snd/pcmC*c` to find the holder and stop it; re-check for `1/1` |
| `Unknown key 'StartLimitIntervalSec' in section [Service], ignoring` | `StartLimit*` keys were placed under `[Service]` | Move them to `[Unit]` (fixed in the shipped unit as of the current version) |

To confirm the mic a service will actually use, always enumerate **as the
service user**, not your login shell — the two can differ:

```bash
sudo -u pi arecord -l                                              # hardware capture devices
sudo -u pi /home/pi/venv/bin/python -c "import sounddevice as sd; print(sd.query_devices())"
```

For the clip viewer specifically: if the page won't load, first check whether
anything is even listening — `sudo ss -ltnp | grep 8099`. Nothing there means
the service failed before binding (check `journalctl -u clip_viewer`), not a
network/firewall problem.

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
├── docs/                  # Design specs and implementation plans
├── ha/
│   └── sound_monitor.yaml # Optional HA template sensor, dashboard card, automation
└── README.md
```

## License

MIT
