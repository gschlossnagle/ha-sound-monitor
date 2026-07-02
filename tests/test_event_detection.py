"""Tests for event_detection — all audio is synthetic, no hardware needed."""

import numpy as np
import pytest

from event_detection import EventDetector

SR = 44100
CHUNK = int(SR * 0.1)  # 100 ms chunks, matching production callback size


def quiet(seconds: float, level: float = 0.001, seed: int = 0) -> np.ndarray:
    """Gaussian noise at ~-60 dBFS RMS (level=0.001)."""
    rng = np.random.default_rng(seed)
    return (rng.standard_normal(int(SR * seconds)) * level).astype(np.float32)


def add_impulse(samples: np.ndarray, at_second: float,
                amplitude: float = 0.5, length: float = 0.005) -> np.ndarray:
    """Overwrite a short segment with a constant-amplitude burst (a 'pop')."""
    out = samples.copy()
    start = int(SR * at_second)
    out[start:start + int(SR * length)] = amplitude
    return out


def run_detector(det: EventDetector, samples: np.ndarray) -> list:
    """Feed samples through in production-sized chunks, collect all events."""
    events = []
    for i in range(0, len(samples) - CHUNK + 1, CHUNK):
        events.extend(det.process(samples[i:i + CHUNK]))
    return events


def make_detector(**kwargs) -> EventDetector:
    return EventDetector(sample_rate=SR, **kwargs)


class TestBasicTriggering:
    def test_quiet_noise_produces_no_events(self):
        det = make_detector()
        events = run_detector(det, quiet(5.0))
        assert events == []

    def test_single_impulse_produces_one_event(self):
        det = make_detector()
        audio = add_impulse(quiet(5.0), at_second=2.5)
        events = run_detector(det, audio)
        assert len(events) == 1

    def test_event_reports_peak_and_baseline(self):
        det = make_detector()
        audio = add_impulse(quiet(5.0), at_second=2.5, amplitude=0.5)
        (ev,) = run_detector(det, audio)
        # 0.5 amplitude over part of a 10 ms frame lands around -9 dBFS
        assert -15.0 < ev.peak_dbfs < -3.0
        # baseline should be near the -60 dBFS noise floor
        assert -65.0 < ev.baseline_dbfs < -55.0
        assert ev.over_baseline_db == pytest.approx(
            ev.peak_dbfs - ev.baseline_dbfs)

    def test_no_trigger_during_first_second_warmup(self):
        det = make_detector()
        audio = add_impulse(quiet(0.9), at_second=0.5)
        assert run_detector(det, audio) == []

    def test_min_trigger_floor_suppresses_blips_in_silence(self):
        det = make_detector()  # default min_trigger_dbfs=-70
        audio = add_impulse(
            np.zeros(int(SR * 3.0), dtype=np.float32),
            at_second=1.5, amplitude=0.0002)  # ~-77 dBFS, below floor
        assert run_detector(det, audio) == []
