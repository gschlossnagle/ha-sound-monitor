# System stats over MQTT — design

## Context

While debugging why `sound_monitor` on the Pi Zero (`venkman`) periodically
stopped recording and the board itself rebooted, the diagnosis required SSH
access and manually running `vcgencmd`, `free -h`, `top`, and `journalctl` by
hand. The two numbers that actually cracked the case — the `sound_monitor`
process's own CPU time vs. uptime, and swap utilization — aren't visible from
Home Assistant at all today. This adds them (and related system health
metrics) as MQTT sensors so future incidents like this one are visible from
the HA dashboard without needing a terminal.

## Goals

- Expose `sound_monitor`'s own CPU% and memory usage as HA sensors — the
  process-specific numbers that resolved today's investigation.
- Expose board-level health: core voltage, CPU temperature, under-voltage
  flag, swap usage, load average, and SD card free space.
- Keep this collection fully decoupled from the audio capture pipeline, so a
  future audio-side stall doesn't also blind HA to system health — the
  opposite of what happened today.

## Non-goals

- Retrofitting `state_class`/other discovery improvements onto the existing
  `mean_dbfs`/`max_dbfs`/etc. sensors — out of scope for this change.
- Historical logging beyond what HA's own recorder/history already does.
- Exposing every `vcgencmd get_throttled` bit — only "under-voltage right
  now" (bit 0), per the design discussion. Other bits (frequency capping,
  soft temp limit, the "since boot" sticky variants) can be added later if a
  future incident calls for them.

## Architecture

### New module: `system_stats.py`

Mirrors the existing `event_detection.py` split: pure, hardware-independent
parsing functions that are fully unit-tested, plus a thin live-data collector
that isn't (matching how `event_detection.py`'s docstring explains its own
dependency-light design, and how `run_stream()` in `sound_monitor.py` is
never unit-tested because it needs real audio hardware).

```python
def parse_throttled(output: str) -> dict[str, bool]:
    """Parse `vcgencmd get_throttled` output (e.g. "throttled=0x50005\n")
    into named booleans. Only bit 0 (under-voltage right now) is exposed
    today; unknown/malformed input should not raise."""

def parse_vcgencmd_value(output: str) -> float:
    """Parse `vcgencmd measure_volts`/`measure_temp`-style output
    (e.g. "volt=1.3500V", "temp=43.3'C") into a float."""

@dataclass
class SystemStats:
    sound_monitor_cpu_percent: float | None
    sound_monitor_mem_mb: float | None
    core_volts: float | None
    cpu_temp_c: float | None
    swap_percent: float | None
    load_avg_1m: float | None
    disk_free_percent: float | None
    under_voltage: bool | None

def collect_stats(pid: int) -> SystemStats:
    """Live collector: psutil for process CPU%/RSS and system
    memory/swap/disk, os.getloadavg() for load, subprocess to vcgencmd for
    volts/temp/throttled. Each field is independently best-effort — one
    failing reading (e.g. vcgencmd missing) logs a warning and yields None
    for that field rather than aborting the whole cycle."""
```

`collect_stats()` calls `psutil.Process(pid).cpu_percent(interval=1.0)` (a
1s blocking call — acceptable since this runs in its own dedicated thread,
not the audio-sensitive path) for a properly primed, non-zero CPU% reading.

### Wiring into `sound_monitor.py`

A new `threading.Thread` (daemon, started in `main()` alongside the existing
audio stream loop) runs its own independent cycle:

```python
def run_system_stats(client, config, pid):
    interval = config["system"]["interval_seconds"]
    while True:
        stats = collect_stats(pid)
        publish stats to MQTT (home/<device_id>/<key>)
        time.sleep(interval)
```

This thread has no watchdog and no restart logic — if a single
`collect_stats()` call raises, the exception is caught and logged, and the
loop continues on the next tick. It is deliberately independent of
`run_stream()`'s health: the entire point is that system stats keep flowing
to HA even if the audio pipeline is wedged, restarting, or crash-looping.

### Config

New optional `system:` section in `config.yaml`, merged over defaults the
same way `detection:`/`clips:` already are:

```yaml
system:
  enabled: true
  interval_seconds: 60
```

### MQTT discovery

Same `device` block as the existing sensors (shared `identifiers`), so
everything groups under one HA device card. New entities:

| Key | HA type | Unit | Discovery extras |
|---|---|---|---|
| `sound_monitor_cpu_percent` | sensor | % | `state_class: measurement` |
| `sound_monitor_mem_mb` | sensor | MB | `state_class: measurement` |
| `core_volts` | sensor | V | `device_class: voltage`, `state_class: measurement` |
| `cpu_temp_c` | sensor | °C | `device_class: temperature`, `state_class: measurement` |
| `swap_percent` | sensor | % | `state_class: measurement` |
| `load_avg_1m` | sensor | — | `state_class: measurement` |
| `disk_free_percent` | sensor | % | `state_class: measurement` |
| `under_voltage` | binary_sensor | — | `device_class: problem` |

Published to `home/<device_id>/system/<key>` (a `system/` sub-path keeps
these visually and topically distinct from the per-minute audio metrics
already on `home/<device_id>/<key>`).

## Error handling

- Each field in `collect_stats()` is gathered independently; a failure
  reading any one (missing `vcgencmd`, a `psutil` exception, `/proc` unreadable)
  logs a warning and sets that field to `None` rather than aborting the
  cycle. `None` fields are simply not published that cycle (HA already
  handles a sensor not updating — same pattern as `baseline_dbfs` today,
  which is only published when non-`None`).
- The stats thread's outer loop catches and logs any exception per
  iteration and continues; it never exits and never signals `run_stream()`.

## Dependencies

Adds `psutil>=5.9.0` to `requirements.txt`. `vcgencmd` is still required
(unavoidable — no `psutil`/stdlib path exposes Broadcom firmware voltage,
temperature, or throttling state) but is invoked directly, not through a new
package.

## Testing

- `parse_throttled()`: unit tests covering a clean `0x0`, under-voltage-set
  (`0x1` / `0x50001`), and malformed/empty input.
- `parse_vcgencmd_value()`: unit tests covering `volt=1.3500V`,
  `temp=43.3'C`, and malformed/empty input.
- `collect_stats()` and the publishing thread are not unit-tested, matching
  the existing project convention that hardware-touching code (`run_stream()`,
  `sd.InputStream`) is verified by manual deployment rather than mocks.

## Documentation

`README.md`'s "Home Assistant" section gets a new subsection listing these
sensors, following the existing "Up to five sensors are created..." style.
`config.yaml.example` gets the new `system:` section with the same
commented-defaults style as `detection:`/`clips:`.
