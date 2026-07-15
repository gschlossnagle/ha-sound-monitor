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
