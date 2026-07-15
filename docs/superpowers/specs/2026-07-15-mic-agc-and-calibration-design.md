# Design: Fixed mic gain (AGC off) + dB calibration offset

## Problem

`sound_monitor.py` publishes dBFS (decibels relative to full scale) — a
relative measure whose absolute value depends entirely on the mic's capture
gain (README.md's own "What the values mean" section says this explicitly).
Two things are needed to turn these readings into something comparable to a
real-world dB SPL reference:

1. **Fixed gain.** The mic (a USB "ATR4697-USB" conference mic) has hardware
   Auto Gain Control (AGC) that continuously re-scales the analog gain to
   keep levels centered. With AGC active, the same real sound pressure
   produces a different dBFS reading minute to minute, so no fixed offset
   can ever be valid.
2. **A calibration offset.** Once gain is fixed and reproducible, the user
   can point a reference SPL meter at a steady sound source, compare it to
   the published dBFS reading, and derive a constant `offset_db` such that
   `dBFS + offset_db ≈ dB SPL`.

An earlier attempt to disable AGC via `amixer` failed. Investigation in this
session found the actual cause: the command targeted ALSA card 1, which is
the Raspberry Pi's onboard HDMI audio output (`vc4-hdmi`), not the USB mic.
The mic is card 0 (`ATR4697USB`) and exposes:

```
$ amixer -c 0 scontrols
Simple mixer control 'Mic',0
Simple mixer control 'Auto Gain Control',0

$ amixer -c 0 controls
numid=2,iface=MIXER,name='Mic Capture Switch'
numid=3,iface=MIXER,name='Mic Capture Volume'
numid=4,iface=MIXER,name='Auto Gain Control'
numid=1,iface=PCM,name='Capture Channel Map'
```

The working commands are:
```
amixer -c 0 sset 'Auto Gain Control',0 off
amixer -c 0 sset 'Mic',0 <N>%
```

## Goals

- Disable AGC and pin capture gain to a known, reproducible level,
  automatically, on every service start — surviving reboots, SD card
  rebuilds, and USB card-index churn (card numbering isn't stable across
  boots if other USB audio devices are present).
- Let the user supply a calibration offset (in dB) derived from their own
  independent SPL-meter comparison, applied to every *absolute* dBFS value
  published to Home Assistant.
- Leave the event-detection math (L90 baseline, `threshold_db`,
  `min_trigger_dbfs`) and clip filenames working in raw dBFS, untouched —
  these are internal/relative and recalibrating them adds risk for no
  benefit.
- Remain fully backward compatible: omitting the new config sections
  reproduces exactly today's behavior (no `amixer` calls, no offset, unit
  stays `"dBFS"`).

## Non-goals

- Auto-discovering the calibration offset (e.g. via a companion reference
  sensor). The user supplies a single manually-derived constant.
- Recalibrating existing saved clips or historical MQTT/HA data.
- Supporting mics without an `'Auto Gain Control'` simple-mixer control —
  the gain-fix script fails loudly rather than silently doing nothing when
  `audio.capture_volume_percent` is set but the expected controls aren't
  found on the resolved card.

## Design

### 1. `fix_mic_gain.py` (new file, repo root)

A small standalone script, run via `ExecStartPre` before `sound_monitor.py`
starts. Deliberately has **no** dependency on `sounddevice` or
`paho-mqtt` — only `pyyaml` and the stdlib — so its failure mode is
independent of the main capture/publish pipeline.

Behavior:

1. Load `config.yaml` (same path convention as `sound_monitor.py`:
   `--config` flag, default `config.yaml` next to the script).
2. Read `audio.device` (the existing substring, e.g. `"ATR4697"`) and the
   new `audio.capture_volume_percent`.
3. If `capture_volume_percent` is absent/`null`: log a line and exit 0
   (no-op — today's behavior for anyone who hasn't opted in).
4. Otherwise, parse `/proc/asound/cards` to find the card whose name/long
   name contains the `audio.device` substring (mirrors how `sounddevice`
   already resolves the same substring, so there is one source of truth
   for "which card is the mic"). Exit non-zero with a clear message if no
   match or more than one match is found.
5. Run `amixer -c <idx> sset 'Auto Gain Control',0 off`. Exit non-zero
   (propagating amixer's stderr) if the control doesn't exist — this is
   the failure mode that must be loud, not swallowed, since a silent
   failure here means AGC stays on and any configured `offset_db` becomes
   silently wrong.
6. Run `amixer -c <idx> sset 'Mic',0 <capture_volume_percent>%`.
7. Print a summary line (card index, both amixer outcomes) so
   `journalctl -u sound_monitor` shows what happened on every start.

### 2. `sound_monitor.service` changes

Add:
```
ExecStartPre=/home/pi/venv/bin/python /home/pi/fix_mic_gain.py
```
directly above the existing `ExecStart=` line, using the same venv/path
convention already documented for `ExecStart`. A comment notes that, like
`ExecStart`, this path must be edited on non-Pi hosts.

If `ExecStartPre` fails, systemd does not start the main process — this is
correct: a fixed gain is a precondition, not a nice-to-have, for anyone
who has opted into `audio.capture_volume_percent`.

### 3. Config additions (`config.yaml.example` + `config.yaml`)

```yaml
audio:
  ...
  capture_volume_percent: 100   # optional. If set, fix_mic_gain.py disables
                                 # hardware AGC and pins capture gain to this
                                 # level on every service start. Omit to skip
                                 # the gain fix entirely (today's behavior).

calibration:
  offset_db: 12.5   # optional. Added to every published *absolute* dBFS
                     # value (Mean/Max/Baseline dBFS, event peak/baseline)
                     # to approximate real-world dB SPL. Only valid for the
                     # audio.capture_volume_percent above — re-derive this
                     # number if that value ever changes. Omit for today's
                     # raw-dBFS behavior.
```

Both sections are optional, following the existing `detection:`/`clips:`
pattern (`{**DEFAULTS, **config.get("section", {})}` merged at point of
use in `main()`). A `CALIBRATION_DEFAULTS = {"offset_db": 0.0}` dict is
added alongside `DETECTION_DEFAULTS`/`CLIPS_DEFAULTS`.

### 4. `sound_monitor.py` changes

- Load `cal_cfg = {**CALIBRATION_DEFAULTS, **config.get("calibration", {})}`
  in `main()`, alongside the existing `det_cfg`/`clips_cfg` lines, and pass
  `offset_db` down to `run_stream()` (same parameter-passing pattern
  already used for `detector`/`recorder`).
- In `run_stream()`, add `offset_db` once to `mean_db`, `max_db`, and
  `baseline_db` right after they're computed (lines ~315–325 today),
  *before* they're used for both the `client.publish(...)` calls and the
  `log.info("Published ...")` line — so log and MQTT are always
  consistent, and there's a single point of truth for "the published
  value."
- In the event-publish block (lines ~275–286 today), add `offset_db` to
  `ev.peak_dbfs` and `ev.baseline_dbfs` when building the JSON payload and
  the log line. `ev.over_baseline_db` is left untouched (it's
  `peak − baseline`; the offset cancels out identically, so adding it
  would be a no-op at best and a bug magnet if the two additions ever
  drifted apart).
- `detector.process(chunk)` continues to receive raw, unmodified chunks —
  no change to `event_detection.py` at all. This keeps L90 baseline
  computation, `threshold_db`, and `min_trigger_dbfs` exactly as tuned
  today.
- `ClipRecorder` / clip filenames: no change. Filenames keep embedding raw
  dBFS (e.g. `..._-18.3dBFS.wav`) since they're a local diagnostic artifact,
  never published to HA, and changing them adds risk for no benefit.

### 5. `publish_discovery()` changes

When `cal_cfg["offset_db"] != 0`, switch `unit_of_measurement` from
`"dBFS"` to `"dB SPL"` for `mean_dbfs`, `max_dbfs`, `baseline_dbfs`, and
`last_event_peak` (the fourth sensor already sources its value from the
event JSON's `peak_dbfs`, so it automatically reflects the offset — only
its discovery-time unit label needs updating). `events_per_minute` is
unaffected. This makes the HA UI honest about what's being displayed
without requiring a second parallel set of sensors.

### 6. Documentation (README.md)

- New rows in the "All options" table for `audio.capture_volume_percent`
  and `calibration.offset_db`.
- A new "Calibrating" subsection under Home Assistant (or its own
  top-level section) walking through: disable AGC (automatic once
  `capture_volume_percent` is set), play/measure a steady reference sound,
  compare the Mean dBFS sensor to a reference SPL meter reading, set
  `offset_db` to the difference, restart the service.
- New Troubleshooting row: `fix_mic_gain.py` exits non-zero / service
  won't start → cause (wrong card resolved, or mic lacks the expected
  simple-mixer controls — run `amixer -c <idx> scontrols` to check) → fix.
- Project Structure tree gains `fix_mic_gain.py`.
- Step 5 (systemd install) gains `fix_mic_gain.py` to the list of files
  copied to `/home/pi`.

## Testing

- `fix_mic_gain.py`'s card-resolution logic (substring match against
  `/proc/asound/cards`-shaped text) is pure and testable without hardware —
  add `tests/test_fix_mic_gain.py` covering: single match, no match,
  ambiguous multiple matches, and the no-op path when
  `capture_volume_percent` is absent.
- The `amixer` subprocess calls themselves are not unit-testable (no ALSA
  hardware in CI) — verified manually on the Pi per the design's own
  "Calibrating" doc section.
- `sound_monitor.py`'s offset application is a pure arithmetic change at
  well-defined points; existing tests (if any cover this file) plus a
  manual run against `config.yaml` with `calibration.offset_db` set
  confirms published values shift by exactly that offset while
  `over_baseline_db` does not.

## Open questions resolved during brainstorming

- **Persistence approach**: `ExecStartPre` in the systemd service
  (self-healing, name-based card resolution) — not `alsactl store`.
- **Offset scope**: applied to all absolute-level sensors (Mean/Max/
  Baseline dBFS, event peak/baseline), not just the per-minute summary,
  and not as parallel duplicate sensors.
