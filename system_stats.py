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
