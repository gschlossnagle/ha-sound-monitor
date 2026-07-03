# Clip Viewer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A small always-on web UI (`clip_viewer.py`) that serves the `clips/` directory so the user can list, play, download, and delete event WAV clips from any device on the LAN.

**Architecture:** A new standalone `clip_viewer.py` with a pure-logic `ClipLibrary` (list/parse/resolve/delete, no HTTP imports — unit-testable off-hardware) and a thin `BaseHTTPRequestHandler` built by `make_handler(library)`. It reads the SAME `config.yaml` as the recorder (reusing `clips.directory`, adding an optional `viewer:` section) but deliberately does NOT import `sound_monitor` — so it stays independent of the audio stack (numpy/sounddevice/paho) and runs anywhere. Shipped with a `clip_viewer.service` systemd unit mirroring `sound_monitor.service`.

**Tech Stack:** Python 3.11+ stdlib only (`http.server`, `re`, `datetime`, `html`, `urllib.parse`, `wave` in tests) + `PyYAML` (already a dep). No new runtime dependency. pytest for tests.

**Spec:** `docs/superpowers/specs/2026-07-02-clip-viewer-design.md`

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `clip_viewer.py` | Create | `ClipInfo`, `ClipLibrary` (logic), `make_handler`/`render_index` (HTTP), `main` |
| `tests/test_clip_viewer.py` | Create | Unit tests for `ClipLibrary` (parsing, ordering, path safety, delete) |
| `clip_viewer.service` | Create | systemd unit, mirrors `sound_monitor.service` |
| `config.yaml.example` | Modify | New optional `viewer:` section |
| `config.yaml` | Modify (local, gitignored) | Same `viewer:` section so local runs work |
| `README.md` | Modify | "Reviewing clips" section, viewer config rows, project tree, rsync alt |

`ClipLibrary` never imports `http.server`; the handler is thin glue over it. `clip_viewer.py` never imports `sound_monitor` or the audio libs.

---

### Task 1: ClipLibrary — parse, list, resolve, delete

**Files:**
- Create: `clip_viewer.py` (module setup + `ClipInfo` + `ClipLibrary` only)
- Create: `tests/test_clip_viewer.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_clip_viewer.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_clip_viewer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'clip_viewer'`

- [ ] **Step 3: Write the implementation**

Create `clip_viewer.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_clip_viewer.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add clip_viewer.py tests/test_clip_viewer.py
git commit -m "Add ClipLibrary: parse/list/resolve/delete event clips"
```

---

### Task 2: HTTP handler, index page, and main()

**Files:**
- Modify: `clip_viewer.py` (add imports, `VIEWER_DEFAULTS`, `DEFAULT_CONFIG_PATH`, `PAGE_TEMPLATE`, `render_index`, `make_handler`, `load_viewer_config`, `parse_args`, `main`, `__main__` guard)

No new unit tests (the handler is thin glue). Verification is a real local end-to-end smoke run — `clip_viewer.py` has no audio deps, so it runs on any machine.

- [ ] **Step 1: Add imports**

At the top of `clip_viewer.py`, extend the import block (add these; keep the existing `logging`/`re`/`dataclass`/`datetime`/`Path`):

```python
import argparse
import html
import logging
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import yaml
```

- [ ] **Step 2: Add module-level config constants**

Immediately after the `log = logging.getLogger("clip_viewer")` line, add:

```python
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"

# Optional viewer config — merged over these defaults if a `viewer:` section
# is present in config.yaml.
VIEWER_DEFAULTS = {
    "enabled": True,
    "host": "0.0.0.0",   # 0.0.0.0 = LAN-reachable; 127.0.0.1 = Pi only
    "port": 8099,
}
```

- [ ] **Step 3: Add the page template and renderer**

Append to `clip_viewer.py` (after the `ClipLibrary` class):

```python
PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sound Monitor — Clips</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 720px;
         margin: 1.5rem auto; padding: 0 1rem; }
  h1 { font-size: 1.3rem; }
  .clip { border: 1px solid #ccc; border-radius: 8px;
          padding: 0.75rem; margin: 0.75rem 0; }
  .meta { display: flex; justify-content: space-between;
          font-variant-numeric: tabular-nums; }
  .peak { font-weight: 600; }
  audio { width: 100%; margin: 0.5rem 0; }
  .actions { display: flex; gap: 1rem; align-items: center; }
  .empty { color: #666; }
  button { cursor: pointer; }
</style>
</head>
<body>
<h1>Sound Monitor — Clips (__COUNT__)</h1>
__BODY__
</body>
</html>
"""


def render_index(clips: list[ClipInfo]) -> str:
    """Build the full HTML index page for the given clips."""
    rows = []
    for c in clips:
        url = "/clips/" + urllib.parse.quote(c.name)
        when = c.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        rows.append(
            '<div class="clip">'
            f'<div class="meta"><span class="when">{html.escape(when)}</span>'
            f'<span class="peak">{c.peak_dbfs:.1f} dBFS</span></div>'
            f'<audio controls preload="none" src="{html.escape(url)}"></audio>'
            '<div class="actions">'
            f'<a href="{html.escape(url)}" download>Download</a>'
            '<form method="post" action="/delete" '
            "onsubmit=\"return confirm('Delete this clip?');\">"
            f'<input type="hidden" name="name" value="{html.escape(c.name)}">'
            '<button type="submit">Delete</button>'
            "</form></div></div>"
        )
    body = "\n".join(rows) if rows else '<p class="empty">No clips yet.</p>'
    return PAGE_TEMPLATE.replace("__COUNT__", str(len(clips))).replace(
        "__BODY__", body
    )
```

- [ ] **Step 4: Add the request handler factory**

Append to `clip_viewer.py`:

```python
def make_handler(library: ClipLibrary) -> type[BaseHTTPRequestHandler]:
    """Build a request handler bound to the given ClipLibrary."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = urllib.parse.urlparse(self.path).path
            if path == "/":
                self._send_html(render_index(library.list_clips()))
            elif path.startswith("/clips/"):
                name = urllib.parse.unquote(path[len("/clips/"):])
                self._serve_clip(name)
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            if urllib.parse.urlparse(self.path).path != "/delete":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", 0))
            params = urllib.parse.parse_qs(self.rfile.read(length).decode())
            library.delete((params.get("name") or [""])[0])
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()

        def _serve_clip(self, name: str) -> None:
            path = library.resolve(name)
            if path is None:
                self.send_error(404)
                return
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _send_html(self, markup: str) -> None:
            body = markup.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args) -> None:
            log.info("%s %s", self.address_string(), fmt % args)

    return Handler
```

- [ ] **Step 5: Add config loading, arg parsing, and main()**

Append to `clip_viewer.py`:

```python
def load_viewer_config(path: Path) -> dict:
    """Read the shared config.yaml for the clips dir and viewer settings."""
    if not path.exists():
        raise SystemExit(
            f"Config file not found: {path}\n"
            f"The viewer reads the same {path.name} as sound_monitor.py."
        )
    with path.open() as f:
        config = yaml.safe_load(f) or {}
    clips_directory = (config.get("clips") or {}).get("directory", "clips")
    viewer = {**VIEWER_DEFAULTS, **(config.get("viewer") or {})}
    return {"clips_directory": clips_directory, "viewer": viewer}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to config YAML file (default: {DEFAULT_CONFIG_PATH})",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_viewer_config(args.config)
    vcfg = config["viewer"]
    if not vcfg["enabled"]:
        log.info("Viewer disabled (viewer.enabled=false); exiting.")
        return
    library = ClipLibrary(config["clips_directory"])
    server = HTTPServer((vcfg["host"], vcfg["port"]), make_handler(library))
    log.info(
        "Serving clips from %s  at http://%s:%d/",
        library.directory, vcfg["host"], vcfg["port"],
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Verify — compile, unit tests, and a real HTTP smoke run**

Run:

```bash
python3 -m py_compile clip_viewer.py && python3 -m pytest tests/test_clip_viewer.py -q && python3 - <<'EOF'
import threading, urllib.request, urllib.parse, urllib.error, wave, tempfile, pathlib
from http.server import HTTPServer
import clip_viewer

d = pathlib.Path(tempfile.mkdtemp()) / "clips"
d.mkdir()
name = "20260702_103214_-18.3dBFS.wav"
with wave.open(str(d / name), "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100)
    w.writeframes(b"\x00\x00" * 4410)

lib = clip_viewer.ClipLibrary(d)
srv = HTTPServer(("127.0.0.1", 0), clip_viewer.make_handler(lib))
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = "http://127.0.0.1:%d" % srv.server_address[1]

# GET / lists the clip with its parsed time + peak
page = urllib.request.urlopen(base + "/").read().decode()
assert name in page and "-18.3 dBFS" in page, "index missing clip"

# GET /clips/<name> serves the WAV
resp = urllib.request.urlopen(base + "/clips/" + urllib.parse.quote(name))
assert resp.status == 200 and resp.headers["Content-Type"] == "audio/wav"

# GET a traversal name is rejected (404)
try:
    urllib.request.urlopen(base + "/clips/" + urllib.parse.quote("../config.yaml"))
    raise SystemExit("traversal not blocked!")
except urllib.error.HTTPError as e:
    assert e.code == 404, e.code

# POST /delete removes it (303 -> follows to GET /)
req = urllib.request.Request(
    base + "/delete",
    data=urllib.parse.urlencode({"name": name}).encode(),
    method="POST",
)
assert urllib.request.urlopen(req).status == 200
assert not (d / name).exists(), "clip was not deleted"

srv.shutdown()
print("SMOKE OK")
EOF
```

Expected: `11 passed` then `SMOKE OK`, no traceback/assertion error.

- [ ] **Step 7: Commit**

```bash
git add clip_viewer.py
git commit -m "Add HTTP handler, index page, and main() to clip viewer"
```

---

### Task 3: viewer config section

**Files:**
- Modify: `config.yaml.example`
- Modify: `config.yaml` (local, gitignored — keep valid, do NOT commit)

- [ ] **Step 1: Append the viewer section to `config.yaml.example`**

```yaml

# Clip viewer — a small LAN web UI (clip_viewer.py) for reviewing saved
# clips: list newest-first, play inline, download, delete. Optional; the
# viewer reads clips.directory above to find them.
viewer:
  enabled: true       # false -> clip_viewer.py exits without serving
  host: 0.0.0.0       # 0.0.0.0 = reachable on your LAN; 127.0.0.1 = Pi only
  port: 8099
```

Append the same `viewer:` section to the local `config.yaml` (gitignored).

- [ ] **Step 2: Verify both configs still parse**

Run: `python3 -c "import yaml; yaml.safe_load(open('config.yaml.example')); yaml.safe_load(open('config.yaml')); print('YAML OK')"`
Expected: `YAML OK`

- [ ] **Step 3: Verify the viewer reads the new section**

Run:

```bash
python3 - <<'EOF'
import clip_viewer, pathlib
cfg = clip_viewer.load_viewer_config(pathlib.Path("config.yaml.example"))
print("clips_directory:", cfg["clips_directory"])
print("viewer:", cfg["viewer"])
assert cfg["viewer"]["port"] == 8099 and cfg["viewer"]["enabled"] is True
print("CONFIG OK")
EOF
```

Expected: prints the dir + viewer dict and `CONFIG OK`.

- [ ] **Step 4: Commit** (example only — `config.yaml` is gitignored)

```bash
git add config.yaml.example
git commit -m "Add viewer config section to example"
```

---

### Task 4: systemd service unit

**Files:**
- Create: `clip_viewer.service`

- [ ] **Step 1: Create `clip_viewer.service`**

```ini
[Unit]
Description=Sound Monitor Clip Viewer (web UI)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi
ExecStart=/usr/bin/python3 /home/pi/clip_viewer.py
Restart=always
RestartSec=10

# Keep stdout/stderr in the journal:
StandardOutput=journal
StandardError=journal
SyslogIdentifier=clip_viewer

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Sanity-check the unit is well-formed**

Run: `grep -q "ExecStart=/usr/bin/python3 /home/pi/clip_viewer.py" clip_viewer.service && grep -q "WorkingDirectory=/home/pi" clip_viewer.service && echo UNIT OK`
Expected: `UNIT OK`

- [ ] **Step 3: Commit**

```bash
git add clip_viewer.service
git commit -m "Add clip_viewer systemd service unit"
```

---

### Task 5: Documentation

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add viewer rows to the config-keys table**

In the "3. Configure" table, after the `clips.max_clips` row, add:

```markdown
| `viewer.enabled` | Run the clip-review web UI (default: `true`) |
| `viewer.host` | Bind address — `0.0.0.0` for LAN, `127.0.0.1` for Pi-only (default: `0.0.0.0`) |
| `viewer.port` | Port for the clip viewer (default: `8099`) |
```

- [ ] **Step 2: Add a "Reviewing clips" section**

Insert immediately before the "## Project structure" heading:

```markdown
## Reviewing clips

When clip capture is enabled, `clip_viewer.py` serves the saved WAVs as a
simple web page so you can review them from any device on your LAN — list
newest-first, play inline, download, or delete false positives.

Run it on the Pi:

```bash
python3 clip_viewer.py
```

then open `http://<pi-address>:8099/` in a browser.

To keep it always available, install it as a service (alongside the main one):

```bash
cp clip_viewer.py /home/pi/
sudo cp clip_viewer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now clip_viewer
```

It reads the same `config.yaml` as the monitor (using `clips.directory`), so
no extra setup is needed beyond the optional `viewer:` section.

Prefer to pull clips off the Pi instead of browsing in place? A plain rsync
works too:

```bash
rsync -av pi@<pi-address>:~/clips/ ./clips/
```
```

- [ ] **Step 3: Update the project-structure tree**

Replace the tree block so it includes the viewer files:

```markdown
ha-sound-monitor/
├── sound_monitor.py       # Main capture + MQTT publish script
├── event_detection.py     # EventDetector + ClipRecorder (no hardware deps)
├── clip_viewer.py         # LAN web UI for reviewing saved clips
├── sound_monitor.service  # systemd unit for the capture service
├── clip_viewer.service    # systemd unit for the clip viewer
├── config.yaml.example    # Config template — copy to config.yaml and edit
├── config.yaml            # Your local config (gitignored, holds credentials)
├── requirements.txt
├── requirements-dev.txt   # pytest, for running the test suite
├── tests/
│   ├── test_event_detection.py
│   └── test_clip_viewer.py
├── clips/                 # Saved event WAVs (gitignored)
├── ha/
│   └── sound_monitor.yaml # Optional HA template sensor, dashboard card, automation
└── README.md
```

- [ ] **Step 4: Verify the README renders the fenced blocks correctly**

Run: `grep -q "## Reviewing clips" README.md && grep -q "clip_viewer.service" README.md && echo README OK`
Expected: `README OK`

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "Document the clip viewer and rsync alternative"
```

---

## Self-Review Notes

- **Spec coverage:** standalone script + service ✓ (Tasks 1/2/4); LAN-open no-auth bind ✓ (Task 2 `VIEWER_DEFAULTS`/`main`); list+play+download+delete ✓ (Task 2 `render_index`/handler); stdlib `http.server`, no new dep ✓; `ClipLibrary` pure/no-HTTP ✓ (Task 1); filename parsing ✓; path-safety via anchored regex + parent check ✓ (Task 1 `resolve`, tested); reuse `clips.directory` + optional `viewer:` merged over defaults ✓ (Tasks 2/3); `viewer.enabled=false` exits ✓ (Task 2 `main`); does NOT import `sound_monitor`/audio libs ✓; tests on `ClipLibrary` ✓ (Task 1); README + tree + rsync ✓ (Task 5).
- **Type consistency:** `ClipInfo(name, timestamp, peak_dbfs)` used identically in Tasks 1–2 and tests. `ClipLibrary.list_clips() -> list[ClipInfo]`, `.resolve(name) -> Path | None`, `.delete(name) -> bool`, `.directory` attribute — all consistent across handler, main, and tests. `make_handler(library) -> type[BaseHTTPRequestHandler]`; `render_index(list[ClipInfo]) -> str`; `load_viewer_config(Path) -> {"clips_directory", "viewer"}`. Route paths (`/`, `/clips/<name>`, `/delete`) and the `name` form field match between `render_index` and the handler.
- **Deliberate simplifications (YAGNI, per spec):** whole-file `read_bytes()` for serving clips (a few-hundred-KB WAV on a LAN is fine — no streaming); count-based page (no pagination, bounded by `clips.max_clips`); handler left to the local/Pi smoke run rather than unit tests.
```
