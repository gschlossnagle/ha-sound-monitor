"""
Sound event detection and clip capture.

Deliberately imports neither sounddevice nor paho-mqtt so it can be
unit-tested on machines without audio hardware.
"""

import logging
import time
import wave
from collections import deque
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger("sound_monitor.events")


@dataclass
class Event:
    """A detected transient sound event."""

    timestamp: float       # unix time at trigger
    peak_dbfs: float       # loudest 10 ms frame; mutated in place while the
                           # event is still live during its refractory period
    baseline_dbfs: float   # L90 baseline at trigger time

    @property
    def over_baseline_db(self) -> float:
        return self.peak_dbfs - self.baseline_dbfs


class EventDetector:
    """
    Detects impulsive sound events against an adaptive noise baseline.

    A frame triggers an event when its RMS dBFS exceeds BOTH:
      - the L90 baseline (10th percentile of the last
        ``baseline_window_seconds`` of frames) by ``threshold_db``, and
      - the median of the last 1 s of frames by ``threshold_db``.

    The second gate stops sustained noise (vacuum, HVAC onset) from
    producing an event storm while the slow L90 adapts. A refractory
    period after each trigger prevents one pop being counted repeatedly;
    during it the event's peak is updated if louder frames arrive.
    """

    def __init__(
        self,
        sample_rate: int,
        frame_seconds: float = 0.01,
        threshold_db: float = 15.0,
        refractory_seconds: float = 0.2,
        baseline_window_seconds: float = 30.0,
        min_trigger_dbfs: float = -70.0,
        clock=time.time,
    ) -> None:
        self.frame_size = int(sample_rate * frame_seconds)
        self.threshold_db = threshold_db
        self.refractory_frames = int(refractory_seconds / frame_seconds)
        self.min_trigger_dbfs = min_trigger_dbfs
        self._min_history = int(1.0 / frame_seconds)  # 1 s warmup
        self._history: deque[float] = deque(
            maxlen=int(baseline_window_seconds / frame_seconds))
        self._recent: deque[float] = deque(maxlen=self._min_history)
        self._residual = np.empty(0, dtype=np.float32)
        self._cooldown = 0
        self._current_event: Event | None = None
        self._clock = clock

    @property
    def baseline_dbfs(self) -> float | None:
        """L90 (level exceeded 90% of the time); None until 1 s of history."""
        if len(self._history) < self._min_history:
            return None
        return float(np.percentile(self._history, 10))

    def process(self, samples: np.ndarray) -> list[Event]:
        """Feed raw float32 samples; return events triggered in this batch."""
        events: list[Event] = []
        data = np.concatenate([self._residual, samples])
        n_frames = len(data) // self.frame_size
        for i in range(n_frames):
            frame = data[i * self.frame_size:(i + 1) * self.frame_size]
            rms = float(np.sqrt(np.mean(frame ** 2)))
            dbfs = 20.0 * np.log10(max(rms, 1e-10))
            self._step(dbfs, events)
        self._residual = data[n_frames * self.frame_size:]
        return events

    def _step(self, dbfs: float, events: list[Event]) -> None:
        baseline = self.baseline_dbfs
        recent = (
            float(np.median(self._recent))
            if len(self._recent) == self._recent.maxlen
            else None
        )
        if self._cooldown > 0:
            self._cooldown -= 1
            if self._current_event and dbfs > self._current_event.peak_dbfs:
                self._current_event.peak_dbfs = dbfs
        # Trigger only when the frame clears ALL three gates: the slow L90
        # baseline (ambient floor), the 1 s median (blocks sustained-noise
        # onsets from storming while L90 catches up), and the absolute
        # min_trigger floor. Loosening any one silently weakens storm
        # suppression — see test_step_change_in_level_does_not_storm.
        elif (
            baseline is not None
            and recent is not None
            and dbfs >= self.min_trigger_dbfs
            and dbfs > baseline + self.threshold_db
            and dbfs > recent + self.threshold_db
        ):
            self._current_event = Event(self._clock(), dbfs, baseline)
            events.append(self._current_event)
            self._cooldown = self.refractory_frames
        self._history.append(dbfs)
        self._recent.append(dbfs)


class ClipRecorder:
    """
    Keeps a rolling pre-buffer of audio; when an event triggers, captures
    pre_seconds before + post_seconds after into a 16-bit mono WAV named
    ``YYYYmmdd_HHMMSS_<peak>dBFS.wav``. Events arriving while a clip is
    already recording are absorbed into that clip. Oldest clips beyond
    ``max_clips`` are deleted after each write (0 disables pruning).
    """

    def __init__(
        self,
        sample_rate: int,
        directory: str | Path,
        pre_seconds: float = 1.0,
        post_seconds: float = 2.0,
        max_clips: int = 200,
    ) -> None:
        self.sample_rate = sample_rate
        self.directory = Path(directory)
        self.pre_samples = int(sample_rate * pre_seconds)
        self.post_samples = int(sample_rate * post_seconds)
        self.max_clips = max_clips
        self._pre: deque[np.ndarray] = deque()
        self._pre_total = 0
        self._pre_snapshot = np.empty(0, dtype=np.float32)
        self._post: list[np.ndarray] = []
        self._post_total = 0
        self._event: Event | None = None

    def process(self, samples: np.ndarray,
                events: list[Event]) -> Path | None:
        """Feed one chunk plus any events it triggered; returns the clip
        path when a recording completes, else None."""
        written = None
        if self._event is None and events:
            self._event = events[0]
            if self._pre:
                self._pre_snapshot = np.concatenate(
                    list(self._pre))[-self.pre_samples:]
            else:
                self._pre_snapshot = np.empty(0, dtype=np.float32)

        if self._event is not None:
            # The chunk containing the trigger is the start of the post-roll.
            self._post.append(samples)
            self._post_total += len(samples)
            if self._post_total >= self.post_samples:
                written = self._write()
                self._event = None
                self._post = []
                self._post_total = 0

        self._pre.append(samples)
        self._pre_total += len(samples)
        while self._pre and (
                self._pre_total - len(self._pre[0]) >= self.pre_samples):
            self._pre_total -= len(self._pre.popleft())
        return written

    def _write(self) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S",
                           time.localtime(self._event.timestamp))
        path = self.directory / f"{ts}_{self._event.peak_dbfs:.1f}dBFS.wav"
        post = np.concatenate(self._post)[:self.post_samples]
        data = np.concatenate([self._pre_snapshot, post])
        pcm = (np.clip(data, -1.0, 1.0) * 32767).astype(np.int16)
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.sample_rate)
            w.writeframes(pcm.tobytes())
        log.info("Saved clip %s", path)
        self._prune()
        return path

    def _prune(self) -> None:
        if not self.max_clips:
            return
        clips = sorted(self.directory.glob("*.wav"))
        for old in clips[:-self.max_clips]:
            old.unlink()
