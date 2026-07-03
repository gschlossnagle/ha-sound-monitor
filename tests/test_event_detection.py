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


class TestRefractoryAndPeak:
    def test_impulses_within_refractory_count_as_one_event(self):
        det = make_detector()  # default refractory 0.2 s
        audio = add_impulse(quiet(5.0), at_second=2.5)
        audio = add_impulse(audio, at_second=2.55)  # 50 ms later
        assert len(run_detector(det, audio)) == 1

    def test_impulses_outside_refractory_count_separately(self):
        det = make_detector()
        audio = add_impulse(quiet(5.0), at_second=2.5)
        audio = add_impulse(audio, at_second=3.2)  # 700 ms later
        assert len(run_detector(det, audio)) == 2

    def test_peak_updated_by_louder_frame_during_refractory(self):
        det = make_detector()
        audio = add_impulse(quiet(5.0), at_second=2.5, amplitude=0.3)
        audio = add_impulse(audio, at_second=2.55, amplitude=0.8)
        (ev,) = run_detector(det, audio)
        # 0.3-amplitude alone lands near -13 dBFS; the 0.8 follow-up
        # should have raised the recorded peak well above that.
        assert ev.peak_dbfs > -10.0


class TestSustainedNoise:
    def test_step_change_in_level_does_not_storm(self):
        """A vacuum turning on is one onset, not events every 0.2 s.

        The 1 s median gate should cut off retriggers within ~1 s of
        onset even though the 30 s L90 baseline adapts much more slowly.
        """
        det = make_detector()
        audio = np.concatenate([
            quiet(3.0),
            quiet(10.0, level=0.1, seed=1),  # step up to ~-20 dBFS
        ])
        events = run_detector(det, audio)
        # Onset may fire a few times before the median catches up,
        # but nothing after the first ~1 s of loud audio.
        assert 1 <= len(events) <= 5


class TestChunkBoundaries:
    def test_impulse_detected_regardless_of_chunk_size(self):
        """The same audio fed in ragged chunks yields the same single event.

        process() buffers residual samples across calls, so an impulse
        straddling a chunk boundary must still fire exactly once.
        """
        audio = add_impulse(quiet(5.0), at_second=2.5)

        det_big = make_detector()
        big = run_detector(det_big, audio)  # clean 100 ms chunks

        det_ragged = make_detector()
        ragged = []
        for i in range(0, len(audio), 37):  # odd size, never frame-aligned
            ragged.extend(det_ragged.process(audio[i:i + 37]))

        assert len(big) == 1
        assert len(ragged) == 1
        assert ragged[0].peak_dbfs == pytest.approx(big[0].peak_dbfs, abs=1.0)

    def test_empty_input_is_safe(self):
        det = make_detector()
        assert det.process(np.empty(0, dtype=np.float32)) == []


import wave

from event_detection import ClipRecorder, Event


def feed_recorder(rec: ClipRecorder, samples: np.ndarray,
                  event_at_chunk: int | None = None):
    """Feed chunks; inject an Event at the given chunk index. Returns paths."""
    paths = []
    for idx in range(len(samples) // CHUNK):
        chunk = samples[idx * CHUNK:(idx + 1) * CHUNK]
        events = ([Event(timestamp=1751500000.0 + idx,
                         peak_dbfs=-20.0, baseline_dbfs=-60.0)]
                  if idx == event_at_chunk else [])
        path = rec.process(chunk, events)
        if path:
            paths.append(path)
    return paths


class TestClipRecorder:
    def test_writes_wav_with_exact_pre_plus_post_duration(self, tmp_path):
        rec = ClipRecorder(sample_rate=SR, directory=tmp_path,
                           pre_seconds=1.0, post_seconds=2.0)
        audio = quiet(6.0)
        (path,) = feed_recorder(rec, audio, event_at_chunk=30)  # t=3.0 s
        with wave.open(str(path), "rb") as w:
            assert w.getnframes() == int(SR * 3.0)  # 1 s pre + 2 s post
            assert w.getframerate() == SR
            assert w.getnchannels() == 1
            assert w.getsampwidth() == 2  # 16-bit PCM

    def test_no_event_writes_nothing(self, tmp_path):
        rec = ClipRecorder(sample_rate=SR, directory=tmp_path)
        assert feed_recorder(rec, quiet(5.0)) == []
        assert list(tmp_path.glob("*.wav")) == []

    def test_event_during_active_recording_is_absorbed(self, tmp_path):
        rec = ClipRecorder(sample_rate=SR, directory=tmp_path,
                           pre_seconds=0.5, post_seconds=1.0)
        audio = quiet(6.0)
        paths = []
        for idx in range(len(audio) // CHUNK):
            chunk = audio[idx * CHUNK:(idx + 1) * CHUNK]
            # events at chunks 20 and 25: second falls inside first's post window
            events = ([Event(1751500000.0 + idx, -20.0, -60.0)]
                      if idx in (20, 25) else [])
            if (p := rec.process(chunk, events)):
                paths.append(p)
        assert len(paths) == 1

    def test_prunes_oldest_clips_beyond_max(self, tmp_path):
        rec = ClipRecorder(sample_rate=SR, directory=tmp_path,
                           pre_seconds=0.2, post_seconds=0.2, max_clips=2)
        audio = quiet(20.0)
        paths = []
        for idx in range(len(audio) // CHUNK):
            chunk = audio[idx * CHUNK:(idx + 1) * CHUNK]
            # well-separated events at chunks 50, 100, 150
            events = ([Event(1751500000.0 + idx, -20.0, -60.0)]
                      if idx in (50, 100, 150) else [])
            if (p := rec.process(chunk, events)):
                paths.append(p)
        assert len(paths) == 3
        remaining = sorted(tmp_path.glob("*.wav"))
        assert len(remaining) == 2
        assert paths[0] not in remaining  # oldest was pruned

    def test_pruning_ignores_unrelated_wav_files(self, tmp_path):
        # A foreign .wav with a lexically-early name must not be counted
        # toward max_clips nor deleted as the "oldest" clip.
        foreign = tmp_path / "00000000_backup.wav"
        foreign.write_bytes(b"not a real clip")
        rec = ClipRecorder(sample_rate=SR, directory=tmp_path,
                           pre_seconds=0.2, post_seconds=0.2, max_clips=2)
        audio = quiet(20.0)
        for idx in range(len(audio) // CHUNK):
            chunk = audio[idx * CHUNK:(idx + 1) * CHUNK]
            events = ([Event(1751500000.0 + idx, -20.0, -60.0)]
                      if idx in (50, 100, 150) else [])
            rec.process(chunk, events)
        assert foreign.exists()  # untouched by pruning
        assert len(list(tmp_path.glob("*dBFS.wav"))) == 2  # cap applies to ours


class TestFlurryExtension:
    """Extend-on-absorb: the post-roll re-arms from the most recent pop, so a
    flurry lands in one clip that runs post_seconds past the LAST pop, bounded
    by max_clip_seconds."""

    def _run(self, rec, audio, event_chunks, peaks=None):
        paths = []
        for idx in range(len(audio) // CHUNK):
            chunk = audio[idx * CHUNK:(idx + 1) * CHUNK]
            events = []
            if idx in event_chunks:
                peak = -20.0 if peaks is None else peaks[event_chunks.index(idx)]
                events = [Event(1751500000.0 + idx, peak, -60.0)]
            if (p := rec.process(chunk, events)):
                paths.append(p)
        return paths

    def test_single_pop_unaffected(self, tmp_path):
        rec = ClipRecorder(sample_rate=SR, directory=tmp_path,
                           pre_seconds=0.5, post_seconds=1.0)
        (path,) = self._run(rec, quiet(6.0), event_chunks=[20])
        with wave.open(str(path), "rb") as w:
            assert w.getnframes() == int(SR * 1.5)  # 0.5 pre + 1.0 post

    def test_absorbed_pop_extends_post_roll_past_last_pop(self, tmp_path):
        rec = ClipRecorder(sample_rate=SR, directory=tmp_path,
                           pre_seconds=0.5, post_seconds=1.0)
        # pops at chunk 20 (t=2.0) and chunk 25 (t=2.5), 0.5 s apart
        (path,) = self._run(rec, quiet(8.0), event_chunks=[20, 25])
        with wave.open(str(path), "rb") as w:
            # 0.5 pre + (0.5 gap + 1.0 tail from the LAST pop) = 2.0 s total
            assert w.getnframes() == int(SR * 2.0)

    def test_max_clip_seconds_caps_a_runaway_flurry(self, tmp_path):
        rec = ClipRecorder(sample_rate=SR, directory=tmp_path,
                           pre_seconds=0.2, post_seconds=1.0,
                           max_clip_seconds=1.0)
        # a pop every 2 chunks (0.2 s) keeps re-arming the post-roll; the cap
        # must force a write at 1.0 s of post-roll from the first pop.
        (path,) = self._run(rec, quiet(8.0),
                            event_chunks=[20, 22, 24, 26, 28])
        with wave.open(str(path), "rb") as w:
            assert w.getnframes() == int(SR * 1.2)  # 0.2 pre + 1.0 cap

    def test_clip_named_after_loudest_pop_in_flurry(self, tmp_path):
        rec = ClipRecorder(sample_rate=SR, directory=tmp_path,
                           pre_seconds=0.5, post_seconds=1.0)
        # second pop is louder; filename should reflect it, not the first
        (path,) = self._run(rec, quiet(8.0), event_chunks=[20, 25],
                            peaks=[-20.0, -5.0])
        assert "-5.0dBFS.wav" in path.name

    def test_max_clip_seconds_default_is_60s(self, tmp_path):
        rec = ClipRecorder(sample_rate=SR, directory=tmp_path)
        assert rec.max_post_samples == int(SR * 60.0)


class TestStorageCap:
    """Byte-size cap on the clips directory, evicting oldest-first."""

    def _make_clips(self, tmp_path, count, size):
        """Create `count` clip files of `size` bytes each, chronologically named
        oldest-first; returns the paths in that (ascending) order."""
        paths = []
        for i in range(count):
            p = tmp_path / f"2026070{i + 1}_120000_-10.0dBFS.wav"
            p.write_bytes(b"x" * size)
            paths.append(p)
        return paths

    def test_size_cap_evicts_oldest_until_under(self, tmp_path):
        rec = ClipRecorder(sample_rate=SR, directory=tmp_path, max_clips=0)
        rec.max_storage_bytes = 250
        paths = self._make_clips(tmp_path, count=4, size=100)  # 400 bytes total
        rec._prune()
        remaining = sorted(tmp_path.glob("*dBFS.wav"))
        assert sum(p.stat().st_size for p in remaining) <= 250
        assert remaining == paths[2:]  # kept newest 2, evicted oldest 2

    def test_size_cap_zero_disables(self, tmp_path):
        rec = ClipRecorder(sample_rate=SR, directory=tmp_path, max_clips=0)
        rec.max_storage_bytes = 0
        self._make_clips(tmp_path, count=5, size=1000)
        rec._prune()
        assert len(list(tmp_path.glob("*dBFS.wav"))) == 5

    def test_both_caps_evict_to_satisfy_both(self, tmp_path):
        # count cap alone would keep 3, but the tighter size cap keeps fewer
        rec = ClipRecorder(sample_rate=SR, directory=tmp_path, max_clips=3)
        rec.max_storage_bytes = 150  # only one 100-byte clip fits
        paths = self._make_clips(tmp_path, count=5, size=100)
        rec._prune()
        remaining = sorted(tmp_path.glob("*dBFS.wav"))
        assert remaining == paths[4:]  # kept newest 1

    def test_default_max_storage_is_1gb(self, tmp_path):
        rec = ClipRecorder(sample_rate=SR, directory=tmp_path)
        assert rec.max_storage_bytes == 1000 * 1_000_000

    def test_size_cap_ignores_foreign_wavs(self, tmp_path):
        rec = ClipRecorder(sample_rate=SR, directory=tmp_path, max_clips=0)
        rec.max_storage_bytes = 50
        foreign = tmp_path / "backup.wav"
        foreign.write_bytes(b"x" * 10_000)
        self._make_clips(tmp_path, count=1, size=10)
        rec._prune()
        assert foreign.exists()  # not counted, not deleted
        assert len(list(tmp_path.glob("*dBFS.wav"))) == 1
