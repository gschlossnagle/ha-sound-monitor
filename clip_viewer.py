#!/usr/bin/env python3
"""
Web UI for reviewing sound-monitor event clips.

Serves the clips/ directory as a simple LAN page: list newest-first, play
inline, download, and delete. Reads the same config.yaml as sound_monitor.py
(reusing clips.directory) but imports none of the audio stack, so it runs
anywhere.

Run:
    python3 clip_viewer.py [--config config.yaml]
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("clip_viewer")

# Matches ClipRecorder's output: YYYYmmdd_HHMMSS_<peak>dBFS.wav (peak may be
# negative and fractional). Anchored + no path separators allowed, so a
# traversal or absolute path can never match — this is the primary path guard.
_CLIP_RE = re.compile(r"^(\d{8}_\d{6})_(-?\d+(?:\.\d+)?)dBFS\.wav$")


@dataclass
class ClipInfo:
    """A saved event clip, parsed from its filename."""

    name: str            # raw filename, e.g. "20260702_103214_-18.3dBFS.wav"
    timestamp: datetime  # parsed from the YYYYmmdd_HHMMSS prefix
    peak_dbfs: float     # parsed from the <peak>dBFS portion


def _parse_clip(name: str) -> ClipInfo | None:
    """Parse a clip filename, or None if it doesn't match the expected shape."""
    m = _CLIP_RE.match(name)
    if not m:
        return None
    try:
        ts = datetime.strptime(m.group(1), "%Y%m%d_%H%M%S")
    except ValueError:
        return None
    return ClipInfo(name=name, timestamp=ts, peak_dbfs=float(m.group(2)))


class ClipLibrary:
    """Read/parse/delete clips in a directory. No HTTP knowledge."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def list_clips(self) -> list[ClipInfo]:
        """All parseable clips, newest first."""
        if not self.directory.is_dir():
            return []
        clips = [
            info
            for path in self.directory.glob("*dBFS.wav")
            if (info := _parse_clip(path.name)) is not None
        ]
        # Filenames start with YYYYmmdd_HHMMSS, so lexical sort == chronological.
        clips.sort(key=lambda c: c.name, reverse=True)
        return clips

    def resolve(self, name: str) -> Path | None:
        """Safely map a request-supplied name to a real clip path, or None.

        Returns a path only if `name` is a valid clip filename that exists
        inside `directory`. Rejects traversal, absolute paths, foreign files,
        and missing files.
        """
        if not _CLIP_RE.match(name):
            return None
        base = self.directory.resolve()
        candidate = (base / name).resolve()
        if candidate.parent != base:   # belt-and-suspenders vs the regex guard
            return None
        if not candidate.is_file():
            return None
        return candidate

    def delete(self, name: str) -> bool:
        """Delete a clip by name; True on success, False if unsafe/missing."""
        path = self.resolve(name)
        if path is None:
            return False
        path.unlink()
        return True
