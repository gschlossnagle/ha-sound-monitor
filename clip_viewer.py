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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("clip_viewer")

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"

# Optional viewer config — merged over these defaults if a `viewer:` section
# is present in config.yaml.
VIEWER_DEFAULTS = {
    "enabled": True,
    "host": "0.0.0.0",   # 0.0.0.0 = LAN-reachable; 127.0.0.1 = Pi only
    "port": 8099,
}

# Matches ClipRecorder's output: YYYYmmdd_HHMMSS_<peak>dBFS.wav (peak may be
# negative and fractional). Anchored with \A...\Z (not $, which would match
# before a trailing newline) and no path separators allowed, so a traversal or
# absolute path can never match — this is the primary path guard.
_CLIP_RE = re.compile(r"\A(\d{8}_\d{6})_(-?\d+(?:\.\d+)?)dBFS\.wav\Z")


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
        try:
            path.unlink()
        except FileNotFoundError:
            # Raced with a concurrent prune by ClipRecorder — already gone.
            return False
        return True


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
            # LAN-open with no auth: any device on the network can POST here.
            # No CSRF token by design (there's no session to protect); the
            # mitigation is network trust, or binding host to 127.0.0.1.
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
            try:
                data = path.read_bytes()
            except (FileNotFoundError, OSError):
                # Raced with a concurrent prune by ClipRecorder — treat as gone.
                self.send_error(404)
                return
            # Whole file into memory, no HTTP Range support: fine for the small
            # (few-hundred-KB) WAVs these clips are; a refresh recovers from a race.
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
