# System Stats over MQTT Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish `sound_monitor`'s own CPU%/memory plus board health (core voltage, CPU temp, under-voltage, swap %, load average, SD card free %) as Home Assistant MQTT sensors, on an independent thread decoupled from the audio pipeline.

**Architecture:** A new dependency-light module `system_stats.py` holds two pure, unit-tested parsers (`parse_throttled`, `parse_vcgencmd_value`) plus a `SystemStats` dataclass and a `collect_stats()` live collector (psutil + `vcgencmd` subprocess calls, not unit-tested — same convention as `run_stream()`). `sound_monitor.py` wires this into a new background thread, its own MQTT discovery function, and a new optional `system:` config section.

**Tech Stack:** Python 3.11+, `psutil` (new dependency), stdlib `subprocess`/`re`/`os`/`threading`, pytest.

**Reference:** `docs/superpowers/specs/2026-07-15-system-stats-mqtt-design.md`

---

### Task 1: Pure parsers in `system_stats.py`

**Files:**
- Create: `system_stats.py`
- Create: `tests/test_system_stats.py`

These two parsers must import cleanly without `psutil` installed — that dependency isn't added until Task 2, and even then it must stay a *local* import inside `collect_stats()` (see Task 2) so this module's pure functions stay testable in any environment, exactly like `event_detection.py` stays testable without `sounddevice`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_system_stats.py`:

```python
"""Tests for system_stats's pure parsing functions -- no psutil or
subprocess calls exercised here, matching the hardware-independent test
philosophy already used for event_detection.py."""

from system_stats import parse_throttled, parse_vcgencmd_value


class TestParseThrottled:
    def test_clean_state_is_not_under_voltage(self):
        assert parse_throttled("throttled=0x0\n") == {"under_voltage_now": False}

    def test_under_voltage_bit_set(self):
        assert parse_throttled("throttled=0x1\n") == {"under_voltage_now": True}

    def test_under_voltage_bit_set_among_others(self):
        # bit 0 (now) and bit 16 (since boot) both set
        assert parse_throttled("throttled=0x50001\n") == {"under_voltage_now": True}

    def test_other_bits_alone_are_not_under_voltage(self):
        # bit 2 (currently throttled) set, bit 0 clear
        assert parse_throttled("throttled=0x4\n") == {"under_voltage_now": False}

    def test_malformed_input_defaults_to_false(self):
        assert parse_throttled("garbage") == {"under_voltage_now": False}

    def test_empty_input_defaults_to_false(self):
        assert parse_throttled("") == {"under_voltage_now": False}


class TestParseVcgencmdValue:
    def test_parses_volts(self):
        assert parse_vcgencmd_value("volt=1.3500V\n") == 1.35

    def test_parses_temp(self):
        assert parse_vcgencmd_value("temp=43.3'C\n") == 43.3

    def test_malformed_input_returns_none(self):
        assert parse_vcgencmd_value("garbage") is None

    def test_empty_input_returns_none(self):
        assert parse_vcgencmd_value("") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_system_stats.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'system_stats'`

- [ ] **Step 3: Write the implementation**

Create `system_stats.py`:

```python
"""
System-level health stats (CPU/memory/voltage/temperature) for MQTT
publishing.

Deliberately keeps its parsing functions free of psutil/subprocess calls
so they can be unit-tested with canned strings on any machine, matching
the same pure-logic/live-I/O split used in event_detection.py.
"""

import logging
import os
import re
import subprocess
from dataclasses import dataclass

log = logging.getLogger("sound_monitor.system_stats")

_VALUE_RE = re.compile(r"=\s*(-?\d+(?:\.\d+)?)")
_THROTTLED_RE = re.compile(r"=\s*(0x[0-9a-fA-F]+)")


def parse_throttled(output: str) -> dict[str, bool]:
    """Parse `vcgencmd get_throttled` output (e.g. "throttled=0x50005\\n")
    into named booleans. Only bit 0 (under-voltage right now) is decoded
    today. Malformed/empty input yields under_voltage_now=False rather
    than raising.
    """
    match = _THROTTLED_RE.search(output)
    if not match:
        return {"under_voltage_now": False}
    value = int(match.group(1), 16)
    return {"under_voltage_now": bool(value & 0x1)}


def parse_vcgencmd_value(output: str) -> float | None:
    """Parse `vcgencmd measure_volts`/`measure_temp`-style output
    (e.g. "volt=1.3500V", "temp=43.3'C") into a float. Returns None on
    malformed/empty input rather than raising.
    """
    match = _VALUE_RE.search(output)
    if not match:
        return None
    return float(match.group(1))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_system_stats.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add system_stats.py tests/test_system_stats.py
git commit -m "feat: add system_stats module with vcgencmd output parsers"
```

---

### Task 2: `SystemStats` dataclass and live collector

**Files:**
- Modify: `system_stats.py`
- Modify: `requirements.txt`

`collect_stats()` is not unit-tested (it needs a real process, real `/proc`, and real `vcgencmd` — the same reason `run_stream()` in `sound_monitor.py` has no unit tests). Import `psutil` **locally inside `collect_stats()`**, not at module level, so Task 1's tests keep passing on any machine without `psutil` installed and the module's pure functions stay trivially testable.

- [ ] **Step 1: Add `psutil` to requirements**

Append to `requirements.txt`:

```
psutil>=5.9.0
```

- [ ] **Step 2: Add the dataclass and collector to `system_stats.py`**

Append to `system_stats.py` (after `parse_vcgencmd_value`):

```python
@dataclass
class SystemStats:
    """One snapshot of system health. Every field is independently
    best-effort: a single failed reading is None, not a raised exception."""

    sound_monitor_cpu_percent: float | None
    sound_monitor_mem_mb: float | None
    core_volts: float | None
    cpu_temp_c: float | None
    swap_percent: float | None
    load_avg_1m: float | None
    disk_free_percent: float | None
    under_voltage: bool | None


def _run_vcgencmd(*args: str) -> str | None:
    """Run `vcgencmd <args>`, returning stdout or None on any failure
    (binary missing, timeout, non-Pi host) -- logged, never raised."""
    try:
        result = subprocess.run(
            ["vcgencmd", *args],
            capture_output=True, text=True, timeout=5, check=True,
        )
        return result.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("vcgencmd %s failed: %s", " ".join(args), exc)
        return None


def collect_stats(pid: int) -> SystemStats:
    """Gather one snapshot of process + board health for ``pid``
    (the running sound_monitor process). Each reading fails independently
    and in isolation -- see the SystemStats docstring.
    """
    import psutil  # local: keeps this module importable without psutil

    cpu_percent = None
    mem_mb = None
    try:
        proc = psutil.Process(pid)
        # Blocking 1s call for a real (non-zero) reading -- fine here since
        # this runs on its own dedicated thread, not the audio-sensitive path.
        cpu_percent = proc.cpu_percent(interval=1.0)
        mem_mb = proc.memory_info().rss / 1_000_000
    except psutil.Error as exc:
        log.warning("Failed to read process stats: %s", exc)

    swap_percent = None
    try:
        swap_percent = psutil.swap_memory().percent
    except psutil.Error as exc:
        log.warning("Failed to read swap stats: %s", exc)

    disk_free_percent = None
    try:
        disk_free_percent = 100.0 - psutil.disk_usage("/").percent
    except OSError as exc:
        log.warning("Failed to read disk stats: %s", exc)

    load_avg_1m = None
    try:
        load_avg_1m = os.getloadavg()[0]
    except OSError as exc:
        log.warning("Failed to read load average: %s", exc)

    core_volts = None
    volts_out = _run_vcgencmd("measure_volts")
    if volts_out is not None:
        core_volts = parse_vcgencmd_value(volts_out)

    cpu_temp_c = None
    temp_out = _run_vcgencmd("measure_temp")
    if temp_out is not None:
        cpu_temp_c = parse_vcgencmd_value(temp_out)

    under_voltage = None
    throttled_out = _run_vcgencmd("get_throttled")
    if throttled_out is not None:
        under_voltage = parse_throttled(throttled_out)["under_voltage_now"]

    return SystemStats(
        sound_monitor_cpu_percent=cpu_percent,
        sound_monitor_mem_mb=mem_mb,
        core_volts=core_volts,
        cpu_temp_c=cpu_temp_c,
        swap_percent=swap_percent,
        load_avg_1m=load_avg_1m,
        disk_free_percent=disk_free_percent,
        under_voltage=under_voltage,
    )
```

- [ ] **Step 3: Verify Task 1's tests still pass without `psutil` installed**

Run: `python3 -m pytest tests/test_system_stats.py -v`
Expected: 10 passed (this confirms the local-import placement in Step 2 kept the module importable without `psutil` on this machine)

- [ ] **Step 4: Syntax-check the whole file**

Run: `python3 -m py_compile system_stats.py`
Expected: no output (success)

- [ ] **Step 5: Commit**

```bash
git add system_stats.py requirements.txt
git commit -m "feat: add SystemStats collector (psutil + vcgencmd)"
```

---

### Task 3: Extract `_device_block()` in `sound_monitor.py`

**Files:**
- Modify: `sound_monitor.py:111-116` (inside `publish_discovery`)

Small, targeted extraction so Task 4's new discovery function doesn't duplicate the device-metadata dict. `publish_discovery` has no existing unit tests (it needs a real `mqtt.Client`), so this refactor doesn't reduce any test coverage.

- [ ] **Step 1: Add the helper and use it in `publish_discovery`**

In `sound_monitor.py`, replace:

```python
    device_block = {
        "identifiers": [device_id],
        "name": device_name,
        "model": "Raspberry Pi Sound Monitor",
        "manufacturer": "DIY",
    }
```

(currently at `sound_monitor.py:111-116`, inside `publish_discovery`) with a call to a new module-level helper. First add the helper directly above `publish_discovery` (which starts at line 97):

```python
def _device_block(config: dict) -> dict:
    """The shared HA `device` block so every entity (audio + system
    sensors) groups under one device card."""
    return {
        "identifiers": [config["device"]["id"]],
        "name": config["device"]["name"],
        "model": "Raspberry Pi Sound Monitor",
        "manufacturer": "DIY",
    }


```

Then change the body of `publish_discovery` so the `device_block = {...}` literal becomes:

```python
    device_block = _device_block(config)
```

- [ ] **Step 2: Syntax-check**

Run: `python3 -m py_compile sound_monitor.py`
Expected: no output (success)

- [ ] **Step 3: Commit**

```bash
git add sound_monitor.py
git commit -m "refactor: extract _device_block() for reuse by system-stats discovery"
```

---

### Task 4: Wire system stats into `sound_monitor.py`

**Files:**
- Modify: `sound_monitor.py`

- [ ] **Step 1: Add the imports and `SYSTEM_DEFAULTS`**

Replace the top of `sound_monitor.py` (currently lines 19-31):

```python
import argparse
import json
import logging
import queue
import time
from pathlib import Path

import numpy as np
import paho.mqtt.client as mqtt
import sounddevice as sd
import yaml

from event_detection import ClipRecorder, EventDetector, enqueue_or_drop
```

with:

```python
import argparse
import json
import logging
import os
import queue
import threading
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np
import paho.mqtt.client as mqtt
import sounddevice as sd
import yaml

from event_detection import ClipRecorder, EventDetector, enqueue_or_drop
from system_stats import collect_stats
```

(`os`, `threading`, and `dataclasses.asdict` are new stdlib imports, grouped with the existing stdlib block to match this file's existing import style; `system_stats` joins the local-imports group.)

Add `SYSTEM_DEFAULTS` right after `CLIPS_DEFAULTS` (`sound_monitor.py:57-65`):

```python
SYSTEM_DEFAULTS = {
    "enabled": True,
    "interval_seconds": 60,
}
```

- [ ] **Step 2: Add `publish_system_discovery()`**

Add this new function directly after `publish_discovery` (which ends at `sound_monitor.py:174`, right before `def parse_args()`):

```python
def publish_system_discovery(client: mqtt.Client, config: dict) -> None:
    """Publish MQTT discovery for system-health sensors: this process's own
    CPU%/memory, plus board-level voltage/temperature/swap/load/disk. Runs
    independently of ``detection_enabled`` -- these sensors don't depend on
    the event detector.
    """
    device_name = config["device"]["name"]
    device_id = config["device"]["id"]
    topic_base = f"home/{device_id}/system"
    device_block = _device_block(config)

    sensors = {
        "sound_monitor_cpu_percent": {
            "name": f"{device_name} CPU %",
            "unit": "%",
            "icon": "mdi:chip",
        },
        "sound_monitor_mem_mb": {
            "name": f"{device_name} Memory MB",
            "unit": "MB",
            "icon": "mdi:memory",
        },
        "core_volts": {
            "name": f"{device_name} Core Voltage",
            "unit": "V",
            "icon": "mdi:flash",
            "device_class": "voltage",
        },
        "cpu_temp_c": {
            "name": f"{device_name} CPU Temp",
            "unit": "°C",
            "icon": "mdi:thermometer",
            "device_class": "temperature",
        },
        "swap_percent": {
            "name": f"{device_name} Swap Used %",
            "unit": "%",
            "icon": "mdi:swap-horizontal",
        },
        "load_avg_1m": {
            "name": f"{device_name} Load Average (1m)",
            "unit": "",
            "icon": "mdi:speedometer",
        },
        "disk_free_percent": {
            "name": f"{device_name} SD Card Free %",
            "unit": "%",
            "icon": "mdi:sd",
        },
    }

    for key, meta in sensors.items():
        payload = {
            "name": meta["name"],
            "unique_id": f"{device_id}_{key}",
            "state_topic": f"{topic_base}/{key}",
            "unit_of_measurement": meta["unit"],
            "icon": meta["icon"],
            "state_class": "measurement",
            "device": device_block,
        }
        if "device_class" in meta:
            payload["device_class"] = meta["device_class"]
        client.publish(
            f"homeassistant/sensor/{device_id}/{key}/config",
            json.dumps(payload),
            retain=True,
        )
        log.info("Published discovery for %s", key)

    binary_payload = {
        "name": f"{device_name} Under-Voltage",
        "unique_id": f"{device_id}_under_voltage",
        "state_topic": f"{topic_base}/under_voltage",
        "device_class": "problem",
        "payload_on": "ON",
        "payload_off": "OFF",
        "device": device_block,
    }
    client.publish(
        f"homeassistant/binary_sensor/{device_id}/under_voltage/config",
        json.dumps(binary_payload),
        retain=True,
    )
    log.info("Published discovery for under_voltage")
```

- [ ] **Step 3: Add `run_system_stats()`**

Add this new function directly after `run_stream()` (which ends at `sound_monitor.py:338`, right before `def main()`):

```python
def run_system_stats(
    client: mqtt.Client, config: dict, interval_seconds: float
) -> None:
    """Collect and publish system-health stats on its own cadence,
    independent of the audio pipeline. A failure in one cycle is logged
    and the loop continues -- this must keep running even if run_stream()
    is wedged or crash-looping, so HA never goes dark on system health at
    exactly the moment it's most needed.
    """
    device_id = config["device"]["id"]
    topic_base = f"home/{device_id}/system"
    pid = os.getpid()

    while True:
        try:
            stats = collect_stats(pid)
            for key, value in asdict(stats).items():
                if value is None:
                    continue
                if key == "under_voltage":
                    client.publish(f"{topic_base}/{key}", "ON" if value else "OFF")
                else:
                    client.publish(f"{topic_base}/{key}", round(value, 1))
            log.info(
                "System stats  cpu=%s%%  mem=%sMB  volts=%sV  temp=%s°C  "
                "swap=%s%%  load=%s  disk_free=%s%%  under_voltage=%s",
                stats.sound_monitor_cpu_percent, stats.sound_monitor_mem_mb,
                stats.core_volts, stats.cpu_temp_c, stats.swap_percent,
                stats.load_avg_1m, stats.disk_free_percent, stats.under_voltage,
            )
        except Exception as exc:
            log.error("System stats cycle failed: %s", exc)
        time.sleep(interval_seconds)
```

- [ ] **Step 4: Start the thread in `main()`**

In `main()`, after the existing block that resolves `det_cfg`/`clips_cfg` and calls `publish_discovery` (`sound_monitor.py:367-370`):

```python
    det_cfg = {**DETECTION_DEFAULTS, **config.get("detection", {})}
    clips_cfg = {**CLIPS_DEFAULTS, **config.get("clips", {})}

    publish_discovery(client, config, det_cfg["enabled"])
```

add immediately after `publish_discovery(client, config, det_cfg["enabled"])`:

```python

    # --- System health stats (independent of the audio pipeline) ---
    sys_cfg = {**SYSTEM_DEFAULTS, **config.get("system", {})}
    if sys_cfg["enabled"]:
        publish_system_discovery(client, config)
        threading.Thread(
            target=run_system_stats,
            args=(client, config, sys_cfg["interval_seconds"]),
            daemon=True,
        ).start()
        log.info(
            "System stats on  (interval=%ds)", sys_cfg["interval_seconds"]
        )
```

- [ ] **Step 5: Syntax-check**

Run: `python3 -m py_compile sound_monitor.py`
Expected: no output (success)

- [ ] **Step 6: Run the full test suite**

Run: `python3 -m pytest tests/ -v`
Expected: all tests pass (this file still can't be imported directly without `sounddevice`/`paho-mqtt` installed, so this step confirms nothing in `event_detection.py`/`system_stats.py` regressed, not that `sound_monitor.py` itself was exercised)

- [ ] **Step 7: Commit**

```bash
git add sound_monitor.py
git commit -m "feat: publish system-health sensors on an independent MQTT thread"
```

---

### Task 5: Config example and README

**Files:**
- Modify: `config.yaml.example`
- Modify: `README.md`

- [ ] **Step 1: Add the `system:` section to `config.yaml.example`**

Add at the end of `config.yaml.example` (after the existing `viewer:` section):

```yaml

# System health — publishes this process's own CPU/memory usage plus
# board health (voltage, temperature, swap, load, disk free) as HA
# sensors, on its own schedule independent of the audio pipeline. Requires
# `vcgencmd` (present on Raspberry Pi OS); set enabled: false if running
# this off a Pi. Optional: delete this section to disable; missing keys
# fall back to the defaults shown.
system:
  enabled: true
  interval_seconds: 60   # how often to publish system health sensors
```

- [ ] **Step 2: Document the new sensors in `README.md`**

In the `## Home Assistant` section, directly after the existing paragraph that ends "...a clean way to spot transient activity. Each event is also published as JSON to `home/<device_id>/event` ... for automations that want per-event triggers." and before the "Note: the event JSON..." paragraph, insert:

```markdown
Independent of the audio pipeline, `sound_monitor` also publishes its own
resource usage and board health every `system.interval_seconds` (default
60s): **CPU %** and **Memory MB** (the `sound_monitor` process itself),
**Core Voltage**, **CPU Temp**, **Swap Used %**, **Load Average (1m)**,
**SD Card Free %**, and a binary **Under-Voltage** sensor. These publish on
their own thread, decoupled from the audio watchdog, so they keep
reporting even if the audio stream itself is stalled or restarting.
Requires `vcgencmd` (present on Raspberry Pi OS); set `system.enabled:
false` in `config.yaml` if running this off a Pi.
```

- [ ] **Step 3: Commit**

```bash
git add config.yaml.example README.md
git commit -m "docs: document system-health sensors and config.yaml.example section"
```

---

### Task 6: Deploy and verify on the Pi

**Files:** none (manual verification — this repo's `sound_monitor.py` has never been unit-testable end-to-end since it needs real audio hardware; see README's existing Troubleshooting section for the same convention)

- [ ] **Step 1: Deploy**

On the Pi (`venkman`):

```bash
cd /home/pi/ha-sound-monitor   # or wherever this repo is checked out
git pull
/home/pi/venv/bin/pip install -r requirements.txt   # picks up psutil
sudo systemctl restart sound_monitor
```

- [ ] **Step 2: Confirm the new sensors publish**

```bash
journalctl -u sound_monitor -f
```

Expected within the first `interval_seconds` (60s default): a `System stats  cpu=...` log line, and no `System stats cycle failed` errors.

- [ ] **Step 3: Confirm the sensors appear in Home Assistant**

In HA, check the device card for this sound monitor (Settings → Devices & Services → MQTT → find the device) for: CPU %, Memory MB, Core Voltage, CPU Temp, Swap Used %, Load Average (1m), SD Card Free %, and Under-Voltage (binary).

- [ ] **Step 4: Sanity-check values against a manual reading**

```bash
vcgencmd measure_volts
vcgencmd measure_temp
free -h
```

Compare against the HA sensor values published in the same minute — they should match (within rounding).
