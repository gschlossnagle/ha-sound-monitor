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
