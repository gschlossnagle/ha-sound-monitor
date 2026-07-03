"""Tests for ClipLibrary — real temp dirs, fake clip files, no hardware."""

from datetime import datetime
from pathlib import Path

from clip_viewer import ClipInfo, ClipLibrary


def make_clip(directory: Path, name: str) -> Path:
    """Create a file with the given name; contents don't matter for these tests."""
    p = directory / name
    p.write_bytes(b"fake wav bytes")
    return p


class TestParsing:
    def test_lists_and_parses_valid_clip(self, tmp_path):
        make_clip(tmp_path, "20260702_103214_-18.3dBFS.wav")
        (clip,) = ClipLibrary(tmp_path).list_clips()
        assert clip.name == "20260702_103214_-18.3dBFS.wav"
        assert clip.timestamp == datetime(2026, 7, 2, 10, 32, 14)
        assert clip.peak_dbfs == -18.3

    def test_malformed_names_are_skipped(self, tmp_path):
        make_clip(tmp_path, "20260702_103214_-18.3dBFS.wav")
        make_clip(tmp_path, "notaclip.txt")
        make_clip(tmp_path, "backup.wav")
        make_clip(tmp_path, "20260702_bad_-1.0dBFS.wav")
        assert len(ClipLibrary(tmp_path).list_clips()) == 1

    def test_empty_directory_lists_nothing(self, tmp_path):
        assert ClipLibrary(tmp_path).list_clips() == []

    def test_missing_directory_lists_nothing(self, tmp_path):
        assert ClipLibrary(tmp_path / "does_not_exist").list_clips() == []


class TestOrdering:
    def test_newest_first(self, tmp_path):
        make_clip(tmp_path, "20260702_090000_-30.0dBFS.wav")
        make_clip(tmp_path, "20260702_170000_-10.0dBFS.wav")
        make_clip(tmp_path, "20260702_120000_-20.0dBFS.wav")
        names = [c.name for c in ClipLibrary(tmp_path).list_clips()]
        assert names == [
            "20260702_170000_-10.0dBFS.wav",
            "20260702_120000_-20.0dBFS.wav",
            "20260702_090000_-30.0dBFS.wav",
        ]


class TestPathSafety:
    def test_resolve_rejects_traversal_and_absolute(self, tmp_path):
        lib = ClipLibrary(tmp_path)
        assert lib.resolve("../../etc/passwd") is None
        assert lib.resolve("/etc/passwd") is None
        assert lib.resolve("sub/20260702_103214_-1.0dBFS.wav") is None

    def test_resolve_rejects_foreign_and_missing(self, tmp_path):
        make_clip(tmp_path, "backup.wav")
        lib = ClipLibrary(tmp_path)
        assert lib.resolve("backup.wav") is None                     # not a clip name
        assert lib.resolve("20260702_103214_-1.0dBFS.wav") is None    # no such file

    def test_resolve_accepts_real_clip(self, tmp_path):
        make_clip(tmp_path, "20260702_103214_-18.3dBFS.wav")
        p = ClipLibrary(tmp_path).resolve("20260702_103214_-18.3dBFS.wav")
        assert p is not None and p.name == "20260702_103214_-18.3dBFS.wav"


class TestDelete:
    def test_delete_removes_real_clip(self, tmp_path):
        make_clip(tmp_path, "20260702_103214_-18.3dBFS.wav")
        lib = ClipLibrary(tmp_path)
        assert lib.delete("20260702_103214_-18.3dBFS.wav") is True
        assert list(tmp_path.glob("*.wav")) == []

    def test_delete_refuses_foreign_file(self, tmp_path):
        foreign = make_clip(tmp_path, "backup.wav")
        assert ClipLibrary(tmp_path).delete("backup.wav") is False
        assert foreign.exists()

    def test_delete_refuses_traversal(self, tmp_path):
        outside = tmp_path.parent / "secret.txt"
        outside.write_text("secret")
        assert ClipLibrary(tmp_path).delete("../secret.txt") is False
        assert outside.exists()

    def test_delete_missing_returns_false(self, tmp_path):
        assert ClipLibrary(tmp_path).delete("20260702_103214_-1.0dBFS.wav") is False
