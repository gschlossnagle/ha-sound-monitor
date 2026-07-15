# Mic AGC Fix + dB Calibration Offset Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Disable the USB mic's hardware Auto Gain Control and pin its capture gain to a known level on every service start, and let the user apply a manually-derived dB offset so published dBFS sensors approximate real-world dB SPL.

**Architecture:** A new standalone script (`fix_mic_gain.py`) runs as `ExecStartPre` before `sound_monitor.py` starts, resolving the mic's ALSA card by name and disabling AGC via `amixer`. A new pure module (`calibration.py`) holds the one-line offset arithmetic so it's unit-testable without pulling in `sound_monitor.py`'s hardware imports (`sounddevice`, `paho-mqtt`) — the same "hardware-independent module" pattern already used by `event_detection.py` and `clip_viewer.py`. `sound_monitor.py` wires a new optional `calibration:` config section through to the handful of publish call sites that emit absolute dBFS values.

**Tech Stack:** Python 3, PyYAML, pytest (existing project stack — no new dependencies).

---

## File Structure

**Create:**
- `fix_mic_gain.py` — resolves the mic's ALSA card by name, disables AGC, pins capture volume. Depends only on PyYAML + stdlib (no `sounddevice`/`paho-mqtt`), so it fails independently of the main capture pipeline.
- `calibration.py` — one pure function, `apply_offset()`. Zero dependencies. Imported by `sound_monitor.py`.
- `tests/test_fix_mic_gain.py` — tests for the pure `/proc/asound/cards` parsing logic.
- `tests/test_calibration.py` — tests for `apply_offset()`.

**Modify:**
- `sound_monitor.py` — new `CALIBRATION_DEFAULTS`, offset wired through `main()` → `publish_discovery()` / `run_stream()`, offset applied at the absolute-dBFS publish points.
- `sound_monitor.service` — new `ExecStartPre=` line.
- `config.yaml.example` — new `audio.capture_volume_percent` and `calibration.offset_db` keys, documented inline.
- `README.md` — new config-table rows, a "Calibrating" section, a Troubleshooting row, updated Project Structure tree, updated systemd file-copy list.

**Not modified:** `event_detection.py` (detection math stays in raw dBFS — confirmed in the design spec), `config.yaml` (local, gitignored — the user edits it themselves after reviewing `config.yaml.example`'s new keys).

---

### Task 1: `calibration.py` — pure offset arithmetic

**Files:**
- Create: `calibration.py`
- Test: `tests/test_calibration.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_calibration.py
"""Tests for calibration — pure arithmetic, no hardware."""

from calibration import apply_offset


class TestApplyOffset:
    def test_zero_offset_is_a_no_op_besides_rounding(self):
        assert apply_offset(-51.34, 0.0) == -51.3

    def test_positive_offset_shifts_value_up(self):
        assert apply_offset(-51.3, 12.5) == -38.8

    def test_negative_offset_shifts_value_down(self):
        assert apply_offset(-40.0, -5.0) == -45.0

    def test_rounds_to_one_decimal_place(self):
        assert apply_offset(-51.26, 0.04) == -51.2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `venv/bin/pytest tests/test_calibration.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'calibration'`

- [ ] **Step 3: Write minimal implementation**

```python
# calibration.py
"""Applies a fixed dB calibration offset to absolute dBFS readings.

No dependencies beyond the stdlib, so it's importable (and testable)
anywhere sound_monitor.py's hardware stack (sounddevice, paho-mqtt) isn't
installed — the same pattern event_detection.py and clip_viewer.py use.
"""


def apply_offset(value_db: float, offset_db: float) -> float:
    """Add a calibration offset to an absolute dBFS value.

    offset_db is a constant the user derives by comparing a published
    dBFS reading against a reference SPL meter (see README's
    "Calibrating" section) — it is only valid for the fixed capture gain
    that reading was taken at. Rounded to 1 decimal place, matching every
    other dBFS value this project publishes.
    """
    return round(value_db + offset_db, 1)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `venv/bin/pytest tests/test_calibration.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add calibration.py tests/test_calibration.py
git commit -m "feat: add calibration.apply_offset for dB SPL calibration"
```

---

### Task 2: `fix_mic_gain.py` — card resolution (pure logic, TDD)

**Files:**
- Create: `fix_mic_gain.py` (this task writes only the parsing/resolution half; Task 3 adds the CLI/amixer half)
- Test: `tests/test_fix_mic_gain.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_fix_mic_gain.py
"""Tests for fix_mic_gain's card-resolution logic — pure text parsing, no hardware."""

import pytest

from fix_mic_gain import find_card_index

CARDS_TEXT = """\
 0 [ATR4697USB     ]: USB-Audio - ATR4697-USB
                      Conference USB microphone ATR4697-USB at usb-20980000.usb-1, full speed
 1 [vc4hdmi        ]: vc4-hdmi - vc4-hdmi
                      vc4-hdmi
"""

AMBIGUOUS_CARDS_TEXT = """\
 0 [USBMic0        ]: USB-Audio - Generic USB Audio
                      USB Audio Device at usb-0000:00:00.0-1, full speed
 1 [USBMic1        ]: USB-Audio - Generic USB Audio
                      USB Audio Device at usb-0000:00:00.0-2, full speed
"""


class TestFindCardIndex:
    def test_matches_short_id(self):
        assert find_card_index(CARDS_TEXT, "ATR4697") == 0

    def test_matches_long_name_on_continuation_line(self):
        assert find_card_index(CARDS_TEXT, "Conference USB microphone") == 0

    def test_is_case_insensitive(self):
        assert find_card_index(CARDS_TEXT, "atr4697") == 0

    def test_matches_other_card(self):
        assert find_card_index(CARDS_TEXT, "vc4-hdmi") == 1

    def test_no_match_raises(self):
        with pytest.raises(ValueError, match="No ALSA card matched"):
            find_card_index(CARDS_TEXT, "nonexistent")

    def test_ambiguous_match_raises(self):
        with pytest.raises(ValueError, match="matched multiple"):
            find_card_index(AMBIGUOUS_CARDS_TEXT, "USB Audio")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `venv/bin/pytest tests/test_fix_mic_gain.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'fix_mic_gain'`

- [ ] **Step 3: Write the minimal implementation**

```python
# fix_mic_gain.py
"""
Disable hardware AGC and pin capture gain before sound_monitor starts.

Run via ExecStartPre in sound_monitor.service. A mic's Auto Gain Control
(AGC) continuously re-scales analog gain, which means the same real sound
produces a different dBFS reading minute to minute — no calibration offset
(calibration.offset_db in config.yaml) can be valid unless gain is fixed
first. This script has no dependency on sounddevice or paho-mqtt so its
failure mode is independent of the main capture/publish pipeline.

Configure in config.yaml:
    audio:
      device: "ATR4697"            # substring matched against /proc/asound/cards
      capture_volume_percent: 100  # omit this key to skip the gain fix entirely
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"

CARD_LINE_RE = re.compile(r"^\s*(\d+)\s+\[([^\]]*)\]:\s*(.*)$")


def parse_cards(cards_text: str) -> dict[int, str]:
    """Parse `/proc/asound/cards`-formatted text into {index: block_text}.

    Each card's block_text is its header line plus any indented
    continuation lines, joined together — long/marketing names (e.g.
    "Conference USB microphone ATR4697-USB") often live on the
    continuation line, not the short card id, so both must be searchable.
    """
    blocks: dict[int, list[str]] = {}
    current_index = None
    for line in cards_text.splitlines():
        m = CARD_LINE_RE.match(line)
        if m:
            current_index = int(m.group(1))
            blocks[current_index] = [m.group(2).strip(), m.group(3).strip()]
        elif current_index is not None and line.strip():
            blocks[current_index].append(line.strip())
    return {idx: " ".join(parts) for idx, parts in blocks.items()}


def find_card_index(cards_text: str, name_substring: str) -> int:
    """Return the single ALSA card index whose block contains name_substring
    (case-insensitive). Raises ValueError if zero or more than one match.
    """
    blocks = parse_cards(cards_text)
    needle = name_substring.lower()
    matches = [idx for idx, text in blocks.items() if needle in text.lower()]
    if not matches:
        raise ValueError(
            f"No ALSA card matched {name_substring!r} in:\n{cards_text}"
        )
    if len(matches) > 1:
        raise ValueError(
            f"{name_substring!r} matched multiple ALSA cards {matches} — "
            f"use a more specific substring in audio.device"
        )
    return matches[0]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `venv/bin/pytest tests/test_fix_mic_gain.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add fix_mic_gain.py tests/test_fix_mic_gain.py
git commit -m "feat: add fix_mic_gain card-resolution logic"
```

---

### Task 3: `fix_mic_gain.py` — CLI and amixer wiring

**Files:**
- Modify: `fix_mic_gain.py` (append to the file created in Task 2)

This half shells out to `amixer` and reads real hardware state (`/proc/asound/cards`), so it isn't unit-testable in CI — it's verified manually against the Pi in Task 3's final step, per the design spec's Testing section.

- [ ] **Step 1: Append the amixer-running and CLI code**

```python
# Append to fix_mic_gain.py, below find_card_index()


def run_amixer(card_index: int, control: str, value: str) -> None:
    subprocess.run(
        ["amixer", "-c", str(card_index), "sset", control, value],
        check=True,
        capture_output=True,
        text=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    args = parser.parse_args()

    config = yaml.safe_load(args.config.read_text())
    audio_cfg = config.get("audio", {})
    volume_percent = audio_cfg.get("capture_volume_percent")

    if volume_percent is None:
        print("audio.capture_volume_percent not set — skipping AGC/gain fix")
        return 0

    device_substring = audio_cfg.get("device")
    if not device_substring:
        print(
            "audio.capture_volume_percent is set but audio.device is empty "
            "— cannot resolve which ALSA card to fix",
            file=sys.stderr,
        )
        return 1

    cards_text = Path("/proc/asound/cards").read_text()
    try:
        card_index = find_card_index(cards_text, device_substring)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        run_amixer(card_index, "Auto Gain Control,0", "off")
        run_amixer(card_index, "Mic,0", f"{volume_percent}%")
    except subprocess.CalledProcessError as exc:
        print(
            f"amixer failed on card {card_index}: {exc.stderr.strip()}",
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        print(f"Could not run amixer: {exc}", file=sys.stderr)
        return 1

    print(
        f"Card {card_index}: Auto Gain Control off, Mic capture volume "
        f"pinned to {volume_percent}%"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run the full local test suite to confirm nothing broke**

Run: `venv/bin/pytest tests/ -v`
Expected: All tests PASS (the new CLI code has no importable side effects that
would break existing tests — `argparse`/`subprocess`/`sys` are stdlib).

- [ ] **Step 3: Commit**

```bash
git add fix_mic_gain.py
git commit -m "feat: add fix_mic_gain CLI to disable AGC and pin capture gain"
```

- [ ] **Step 4: Manually verify on the Pi (not automatable — no ALSA hardware in CI)**

Copy `fix_mic_gain.py` to `/home/pi/` alongside the other project files, add
`capture_volume_percent: 100` under `audio:` in the Pi's `config.yaml`
(`audio.device` is already set to `"ATR4697"` per the existing setup), then run:

```bash
venv/bin/python fix_mic_gain.py
amixer -c 0 sget 'Auto Gain Control',0   # expect: [off]
amixer -c 0 sget 'Mic',0                 # expect: [100%]
```

Expected output from the script itself:
```
Card 0: Auto Gain Control off, Mic capture volume pinned to 100%
```

---

### Task 4: `sound_monitor.py` — imports and calibration defaults

**Files:**
- Modify: `sound_monitor.py:19-56` (imports and defaults)

This task only adds new, unused-so-far declarations — it can't break anything
that imports or calls into `sound_monitor.py` today. Tasks 5 and 6 give the
existing `publish_discovery()`/`run_stream()` call sites in `main()` a
default-valued new parameter (safe on their own), and Task 7 is the one that
actually rewires `main()` to pass real values through — deferring that
wiring to last means every intermediate commit stays runnable.

- [ ] **Step 1: Add the import and defaults dict**

In `sound_monitor.py`, add the import next to the existing `event_detection` import (currently line 31):

```python
from calibration import apply_offset
from event_detection import ClipRecorder, EventDetector, enqueue_or_drop
```

Add `CALIBRATION_DEFAULTS` next to `CLIPS_DEFAULTS` (currently lines 57-65):

```python
CALIBRATION_DEFAULTS = {
    "offset_db": 0.0,
}
```

- [ ] **Step 2: Run the full test suite to confirm nothing broke**

Run: `venv/bin/pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 3: Commit**

```bash
git add sound_monitor.py
git commit -m "feat: add calibration import and CALIBRATION_DEFAULTS"
```

---

### Task 5: Apply the offset in `publish_discovery()`

**Files:**
- Modify: `sound_monitor.py:97-153` (`publish_discovery`)

- [ ] **Step 1: Update the function signature and metrics dict**

Change:

```python
def publish_discovery(
    client: mqtt.Client, config: dict, detection_enabled: bool
) -> None:
```

to:

```python
def publish_discovery(
    client: mqtt.Client, config: dict, detection_enabled: bool,
    offset_db: float = 0.0,
) -> None:
```

Inside the function, right after `interval_seconds = config["interval_seconds"]`, add:

```python
    # Once a calibration offset is configured, published values are no
    # longer raw dBFS — relabel the affected sensors so HA shows the truth.
    absolute_unit = "dB SPL" if offset_db else "dBFS"
```

Then change every `"unit": "dBFS"` for `mean_dbfs`, `max_dbfs`, `last_event_peak`,
and `baseline_dbfs` (four call sites — NOT `events_per_minute`, which stays
`"events/min"`) to `"unit": absolute_unit`. The full `metrics` block becomes:

```python
    metrics = {
        "mean_dbfs": {
            "name": f"{device_name} Mean dBFS",
            "icon": "mdi:microphone",
            "unit": absolute_unit,
        },
        "max_dbfs": {
            "name": f"{device_name} Max dBFS",
            "icon": "mdi:microphone-plus",
            "unit": absolute_unit,
        },
        "events_per_minute": {
            "name": f"{device_name} Events Per Minute",
            "icon": "mdi:pulse",
            "unit": "events/min",
        },
        "last_event_peak": {
            "name": f"{device_name} Last Event Peak",
            "icon": "mdi:waveform",
            "unit": absolute_unit,
            "state_topic": f"{topic_base}/event",
            "value_template": "{{ value_json.peak_dbfs }}",
            # A pop an hour ago is still the last event — never expire.
            "no_expire": True,
        },
    }

    if detection_enabled:
        # L90 ambient floor (10th-percentile level over the detector's rolling
        # window). Unlike Mean dBFS (an energy average that loud pops drag up),
        # this reads the quiet floor and stays put during transients.
        metrics["baseline_dbfs"] = {
            "name": f"{device_name} Baseline dBFS",
            "icon": "mdi:microphone-minus",
            "unit": absolute_unit,
        }
```

- [ ] **Step 2: Run the full test suite**

Run: `venv/bin/pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 3: Commit**

```bash
git add sound_monitor.py
git commit -m "feat: relabel dBFS sensors as dB SPL when calibration offset is set"
```

---

### Task 6: Apply the offset in `run_stream()`

**Files:**
- Modify: `sound_monitor.py:188-338` (`run_stream`)

- [ ] **Step 1: Update the function signature**

Change:

```python
def run_stream(
    client: mqtt.Client,
    config: dict,
    detector: EventDetector | None,
    recorder: ClipRecorder | None,
) -> None:
```

to:

```python
def run_stream(
    client: mqtt.Client,
    config: dict,
    detector: EventDetector | None,
    recorder: ClipRecorder | None,
    offset_db: float = 0.0,
) -> None:
```

- [ ] **Step 2: Apply the offset to the per-event publish block**

Change:

```python
                    client.publish(
                        f"{topic_base}/event",
                        json.dumps({
                            "timestamp": ev.timestamp,
                            "peak_dbfs": round(ev.peak_dbfs, 1),
                            "baseline_dbfs": round(ev.baseline_dbfs, 1),
                            "over_baseline_db": round(ev.over_baseline_db, 1),
                        }),
                    )
                    log.info(
                        "Event  peak=%.1f dBFS  (+%.1f dB over baseline)",
                        ev.peak_dbfs, ev.over_baseline_db,
                    )
```

to:

```python
                    client.publish(
                        f"{topic_base}/event",
                        json.dumps({
                            "timestamp": ev.timestamp,
                            "peak_dbfs": apply_offset(ev.peak_dbfs, offset_db),
                            "baseline_dbfs": apply_offset(ev.baseline_dbfs, offset_db),
                            # A delta (peak - baseline): the offset cancels out
                            # identically, so it's deliberately NOT applied here.
                            "over_baseline_db": round(ev.over_baseline_db, 1),
                        }),
                    )
                    log.info(
                        "Event  peak=%.1f dBFS  (+%.1f dB over baseline)",
                        apply_offset(ev.peak_dbfs, offset_db), ev.over_baseline_db,
                    )
```

Note `detector.process(chunk)` just above this block (feeding `EventDetector`)
is untouched — it keeps receiving raw audio and computing raw dBFS
internally, exactly as before. Only the already-computed `ev.peak_dbfs` /
`ev.baseline_dbfs` values get the offset applied at the point of publishing.

- [ ] **Step 3: Apply the offset to the per-minute summary block**

Change:

```python
            # Leq: average power in linear domain, then convert back to dB
            mean_power = float(np.mean([10 ** (db / 20) for db in chunk_db]))
            mean_db = round(20 * np.log10(max(mean_power, 1e-10)), 1)
            max_db  = round(float(np.max(chunk_db)), 1)
```

to:

```python
            # Leq: average power in linear domain, then convert back to dB
            mean_power = float(np.mean([10 ** (db / 20) for db in chunk_db]))
            mean_db = apply_offset(20 * np.log10(max(mean_power, 1e-10)), offset_db)
            max_db  = apply_offset(float(np.max(chunk_db)), offset_db)
```

Change:

```python
            # L90 ambient floor from the detector (None during the first
            # second of warmup, or when detection is disabled).
            baseline_db = detector.baseline_dbfs if detector else None

            client.publish(f"{topic_base}/mean_dbfs", mean_db)
            client.publish(f"{topic_base}/max_dbfs",  max_db)
            client.publish(f"{topic_base}/events_per_minute", events_per_min)
            if baseline_db is not None:
                client.publish(f"{topic_base}/baseline_dbfs", round(baseline_db, 1))
```

to:

```python
            # L90 ambient floor from the detector (None during the first
            # second of warmup, or when detection is disabled).
            baseline_db = detector.baseline_dbfs if detector else None
            if baseline_db is not None:
                baseline_db = apply_offset(baseline_db, offset_db)

            client.publish(f"{topic_base}/mean_dbfs", mean_db)
            client.publish(f"{topic_base}/max_dbfs",  max_db)
            client.publish(f"{topic_base}/events_per_minute", events_per_min)
            if baseline_db is not None:
                client.publish(f"{topic_base}/baseline_dbfs", baseline_db)
```

(The trailing `log.info("Published  mean=%.1f  max=%.1f  baseline=%s dBFS ...", mean_db, max_db, ...)`
line needs no changes — `mean_db`/`max_db`/`baseline_db` are already the
calibrated values by the time it runs, so log and MQTT stay consistent
automatically.)

- [ ] **Step 4: Run the full test suite**

Run: `venv/bin/pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add sound_monitor.py
git commit -m "feat: apply calibration offset to published dBFS values"
```

---

### Task 7: Wire `calibration:` config into `main()`

**Files:**
- Modify: `sound_monitor.py:367-370` and `:404` (`main()`, config merge + call sites)

Both `publish_discovery()` (Task 5) and `run_stream()` (Task 6) already
accept `offset_db` with a `0.0` default, so this task is the last step:
compute the real value from config and pass it through both call sites.

- [ ] **Step 1: Merge the config section and pass it through**

In `main()`, change:

```python
    det_cfg = {**DETECTION_DEFAULTS, **config.get("detection", {})}
    clips_cfg = {**CLIPS_DEFAULTS, **config.get("clips", {})}

    publish_discovery(client, config, det_cfg["enabled"])
```

to:

```python
    det_cfg = {**DETECTION_DEFAULTS, **config.get("detection", {})}
    clips_cfg = {**CLIPS_DEFAULTS, **config.get("clips", {})}
    cal_cfg = {**CALIBRATION_DEFAULTS, **config.get("calibration", {})}

    publish_discovery(client, config, det_cfg["enabled"], cal_cfg["offset_db"])
```

And change:

```python
        try:
            run_stream(client, config, detector, recorder)
```

to:

```python
        try:
            run_stream(client, config, detector, recorder, cal_cfg["offset_db"])
```

- [ ] **Step 2: Run the full test suite**

Run: `venv/bin/pytest tests/ -v`
Expected: All tests PASS

- [ ] **Step 3: Commit**

```bash
git add sound_monitor.py
git commit -m "feat: wire calibration.offset_db config through main()"
```

---

### Task 8: `sound_monitor.service` — wire in the gain fix

**Files:**
- Modify: `sound_monitor.service`

- [ ] **Step 1: Add `ExecStartPre=`**

Change:

```
ExecStart=/home/pi/venv/bin/python /home/pi/sound_monitor.py
```

to:

```
# Disables hardware AGC and pins capture gain before every start — see
# fix_mic_gain.py. A no-op if audio.capture_volume_percent isn't set in
# config.yaml. Runs with the same User/venv as ExecStart below.
ExecStartPre=/home/pi/venv/bin/python /home/pi/fix_mic_gain.py
ExecStart=/home/pi/venv/bin/python /home/pi/sound_monitor.py
```

- [ ] **Step 2: Commit**

```bash
git add sound_monitor.service
git commit -m "feat: run fix_mic_gain.py as ExecStartPre in sound_monitor.service"
```

---

### Task 9: `config.yaml.example` — document the new keys

**Files:**
- Modify: `config.yaml.example`

- [ ] **Step 1: Add `capture_volume_percent` to the `audio:` section**

Change:

```yaml
audio:
  sample_rate: 44100     # Hz — most USB mics support this
  channels: 1
  chunk_seconds: 0.1     # 100 ms chunks fed into the buffer
  # Pin to a specific input device index, or leave null for system default.
  # Run: python3 -c "import sounddevice; print(sounddevice.query_devices())"
  # to find your device index.
  device: null
```

to:

```yaml
audio:
  sample_rate: 44100     # Hz — most USB mics support this
  channels: 1
  chunk_seconds: 0.1     # 100 ms chunks fed into the buffer
  # Pin to a specific input device index, or leave null for system default.
  # Run: python3 -c "import sounddevice; print(sounddevice.query_devices())"
  # to find your device index.
  device: null
  # Optional. If set, fix_mic_gain.py (run automatically via ExecStartPre)
  # disables the mic's hardware Auto Gain Control and pins its capture
  # volume to this percentage on every service start. Required for
  # calibration.offset_db below to stay valid — AGC constantly re-scales
  # gain, so a fixed dB offset only makes sense once gain is fixed too.
  # Omit this key to skip the gain fix entirely (today's behavior).
  capture_volume_percent: 100
```

- [ ] **Step 2: Add the `calibration:` section**

Add after the `clips:` section (before `viewer:`):

```yaml
# Calibration — converts published dBFS values to approximate real-world
# dB SPL. Optional; omit entirely to keep today's raw-dBFS behavior.
calibration:
  # Add this many dB to every published *absolute* level (Mean/Max/Baseline
  # dBFS, event peak/baseline). Derive it by comparing a Mean dBFS reading
  # against a reference SPL meter pointed at the same steady sound — see
  # README's "Calibrating" section. Only valid for the audio.capture_volume_percent
  # above; re-derive this number if that value ever changes.
  offset_db: 0.0
```

- [ ] **Step 3: Commit**

```bash
git add config.yaml.example
git commit -m "docs: document capture_volume_percent and calibration.offset_db"
```

---

### Task 10: `README.md` — full documentation pass

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add two rows to the "All options" table**

In the table under "### 3. Configure" (currently ending with the `viewer.port`
row), add after the `audio.device` row:

```markdown
| `audio.capture_volume_percent` | Optional. Pins the mic's capture gain to this percentage and disables hardware AGC on every service start (via `fix_mic_gain.py`, run as `ExecStartPre`). Omit to skip the gain fix. |
```

and after the `clips.max_storage_mb` row, before the `viewer.enabled` row:

```markdown
| `calibration.offset_db` | Optional (default: `0.0`). Added to every published absolute dBFS value (Mean/Max/Baseline dBFS, event peak/baseline) to approximate real-world dB SPL. See "Calibrating" below. |
```

- [ ] **Step 2: Add a "Calibrating" section**

Add a new section after "### What the values mean" (still under "## Home
Assistant"):

```markdown
### Calibrating to real-world dB SPL

dBFS values depend on the mic's gain, so out of the box they're only
comparable to themselves — useful for spotting relative pop/creak patterns,
but not directly comparable to a spec sheet or another sensor. To convert
readings to approximate dB SPL:

1. Set `audio.capture_volume_percent` in `config.yaml` (e.g. `100`) and
   restart the service. This disables the mic's hardware AGC and pins its
   gain — required before any calibration offset can stay valid, since AGC
   would otherwise keep changing the gain out from under it.
2. Play or generate a steady sound (e.g. a phone's pink-noise generator) at
   a fixed distance from the mic, and let a reference SPL meter and the
   **Mean dBFS** HA sensor both settle on a reading.
3. Compute `offset_db = reference_spl_reading - mean_dbfs_reading`. For
   example, a meter reading 62 dB SPL while Mean dBFS reads -48.3 gives
   `offset_db = 62 - (-48.3) = 110.3`.
4. Set `calibration.offset_db` to that value in `config.yaml` and restart
   the service. All absolute-level sensors (Mean, Max, Baseline dBFS, and
   the event peak) now read in dB SPL, and their HA unit label switches
   from `dBFS` to `dB SPL` automatically.

If you ever change `audio.capture_volume_percent`, the mic's gain changes
and `offset_db` must be re-derived — repeat steps 2–3.
```

- [ ] **Step 3: Add a Troubleshooting row**

Add a row to the existing Troubleshooting table:

```markdown
| `sound_monitor` fails to start; `journalctl` shows a `fix_mic_gain.py` error before it | `ExecStartPre` failed — either no ALSA card matched `audio.device`, or the card doesn't expose `'Auto Gain Control'`/`'Mic'` simple-mixer controls | Run `amixer -c <idx> scontrols` for the card `arecord -l` shows as your mic to confirm the exact control names; if the mic genuinely has no AGC control, remove `audio.capture_volume_percent` from `config.yaml` to skip the gain fix |
```

- [ ] **Step 4: Update the Project Structure tree**

Change:

```
ha-sound-monitor/
├── sound_monitor.py       # Main capture + MQTT publish script
├── event_detection.py     # EventDetector + ClipRecorder (no hardware deps)
├── clip_viewer.py         # LAN web UI for reviewing saved clips
```

to:

```
ha-sound-monitor/
├── sound_monitor.py       # Main capture + MQTT publish script
├── event_detection.py     # EventDetector + ClipRecorder (no hardware deps)
├── calibration.py         # dB offset arithmetic (no hardware deps)
├── fix_mic_gain.py        # Disables AGC + pins capture gain (ExecStartPre)
├── clip_viewer.py         # LAN web UI for reviewing saved clips
```

- [ ] **Step 5: Update the systemd install file-copy list**

Change:

```markdown
```bash
# Copy the app files (event_detection.py is imported by sound_monitor.py,
# and requirements.txt is needed to build the venv on the Pi)
cp sound_monitor.py event_detection.py requirements.txt config.yaml /home/pi/
```
```

to:

```markdown
```bash
# Copy the app files (event_detection.py and calibration.py are imported
# by sound_monitor.py, fix_mic_gain.py runs as ExecStartPre, and
# requirements.txt is needed to build the venv on the Pi)
cp sound_monitor.py event_detection.py calibration.py fix_mic_gain.py \
   requirements.txt config.yaml /home/pi/
```
```

- [ ] **Step 6: Commit**

```bash
git add README.md
git commit -m "docs: document AGC fix and dB calibration in README"
```

---

### Task 11: Final verification

**Files:** none (verification only)

- [ ] **Step 1: Run the full test suite one more time**

Run: `venv/bin/pytest tests/ -v`
Expected: All tests PASS, including the 4 new `test_calibration.py` tests
and 6 new `test_fix_mic_gain.py` tests.

- [ ] **Step 2: Confirm backward compatibility by grepping for the defaults**

Run: `grep -n "CALIBRATION_DEFAULTS\|offset_db: 0.0" sound_monitor.py calibration.py`
Expected: shows `CALIBRATION_DEFAULTS = {"offset_db": 0.0}` — confirms that
any `config.yaml` without a `calibration:` section publishes byte-identical
values to before this change (`apply_offset(x, 0.0) == round(x, 1)`, same
rounding as the pre-change code).

- [ ] **Step 3: Confirm `git log` shows one commit per task**

Run: `git log --oneline -12`
Expected: 10 feature/docs commits from Tasks 1–10 (Task 11 has no commit — it's
verification-only), each with a clear, single-purpose message.

---

## Deferred to the user (not part of this plan)

- Editing the live `config.yaml` on the Pi (gitignored, holds credentials —
  the user copies the new keys from `config.yaml.example` themselves).
- Actually performing the SPL-meter comparison and deriving a real
  `offset_db` value — this plan wires up the *mechanism*; the number itself
  requires physical measurement the user does independently, per the
  original request.
