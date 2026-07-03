# Clip Viewer — Design

**Date:** 2026-07-02
**Status:** Approved (design), pending implementation plan

## Goal

Give the user an easy, browse-in-place way to review the event WAV clips that
`ClipRecorder` saves to `clips/` on the Raspberry Pi — from any device on the
home LAN — without copying files around first. Reviewing clips is the whole
point of saving them (ground-truth for false-positive tuning), and today they
just accumulate on the Pi with no path to the user's ears.

## Decisions (from brainstorming)

- **Deployment:** a separate standalone script shipped with its own always-on
  systemd service (`clip_viewer.service`), independent of the capture process.
  A viewer bug can never affect the audio capture loop.
- **Network exposure:** LAN-open, no auth — bind `0.0.0.0`, same trust model as
  the user's existing Home Assistant setup.
- **Actions:** list + inline play + download, **plus a per-clip delete** (with a
  browser `confirm()`), so junk/false-positive clips can be pruned during review.
- **HTTP stack:** Python stdlib `http.server` — no new dependency. This is a
  one-page app with three routes; the codebase is deliberately stdlib-preferred
  (only numpy/sounddevice/paho/yaml are external), and Flask would be the one
  heavy dependency the project otherwise doesn't need.

## Architecture

Two new files, mirroring the existing `event_detection.py` + `*.service` pattern.

### `clip_viewer.py`

Split into pure logic and a thin HTTP shell so the logic is unit-testable
off-hardware (exactly like `EventDetector`/`ClipRecorder`):

- **`ClipLibrary`** — pure logic, **no HTTP imports**:
  - `list_clips() -> list[ClipInfo]` — scans `directory` for `*dBFS.wav`, parses
    each filename, returns newest-first.
  - filename parsing: `YYYYmmdd_HHMMSS_<peak>dBFS.wav` → a `ClipInfo` with
    `name` (the raw filename), `timestamp` (`datetime`), and `peak_dbfs` (float).
    Files that don't match the pattern are skipped (not an error).
  - `resolve(name) -> Path | None` — safely resolves a request-supplied name
    against `directory`; returns the path only if it stays inside `directory`
    AND matches `*dBFS.wav`, else `None`. Used by both serve and delete.
  - `delete(name) -> bool` — resolves via `resolve()`; unlinks and returns True,
    or returns False if the name is unsafe/foreign/missing.
- **`ClipInfo`** — small dataclass: `name: str`, `timestamp: datetime`,
  `peak_dbfs: float`.
- **`make_handler(library) -> type[BaseHTTPRequestHandler]`** — factory that
  closes over the `ClipLibrary` and returns a handler class implementing the
  three routes below.
- **`main()`** — parse `--config` (default alongside the script, like
  `sound_monitor.py`), `load_config()`, build the `ClipLibrary` from
  `clips.directory`, and serve on `viewer.host:viewer.port` via
  `http.server.HTTPServer`. Logs the bound address at startup.

### `clip_viewer.service`

systemd unit mirroring `sound_monitor.service`:
`User=pi`, `WorkingDirectory=/home/pi`,
`ExecStart=/usr/bin/python3 /home/pi/clip_viewer.py`, `Restart=always`,
journal logging, `WantedBy=multi-user.target`.

## Routes

All handled by the single handler from `make_handler`.

- **`GET /`** — auto-generated, fully self-contained HTML (inline CSS + JS, no
  external assets or CDN). Clips newest-first; each row shows:
  - human-readable **time** and **peak dBFS** (from the parsed filename),
  - an inline `<audio controls src="/clips/<name>">` player,
  - a **download** link (`/clips/<name>`, `download` attribute),
  - a **Delete** button → JS `confirm()` → `POST /delete`.
  - Empty state: "No clips yet."
- **`GET /clips/<name>`** — streams the WAV with `Content-Type: audio/wav`.
  Resolves `<name>` via `ClipLibrary.resolve()`; `404` if it returns `None`.
- **`POST /delete`** — form body `name=<file>`; calls `ClipLibrary.delete()`,
  then `303`-redirects back to `/`. A refused delete still redirects to `/`
  (the clip simply remains).

### Path safety (critical)

`<name>` in `GET /clips/<name>` and `POST /delete` is attacker-controlled input.
Every access goes through `ClipLibrary.resolve()`, which:
1. rejects names containing path separators / `..` before touching the FS,
2. resolves the candidate and confirms `directory` is a parent of the result
   (`Path.resolve()` comparison),
3. confirms the result matches `*dBFS.wav`.

No traversal is possible, and delete can only ever remove the viewer's own
clip files — never `config.yaml`, source, or anything outside `clips/`.

## Config

Reuse the existing `config.yaml` so the viewer and recorder never disagree on
where clips live.

- **Clip directory:** reuse the existing **`clips.directory`** key (no new key).
- **New optional `viewer:` section**, merged over defaults with the established
  `{**VIEWER_DEFAULTS, **config.get("viewer", {})}` pattern:
  ```yaml
  viewer:
    enabled: true       # false → main() logs and exits without binding
    host: 0.0.0.0       # LAN-open
    port: 8099
  ```
  `VIEWER_DEFAULTS = {"enabled": True, "host": "0.0.0.0", "port": 8099}`.
- `viewer.enabled` is **honored** (same as `detection.enabled`/`clips.enabled`):
  if false, `main()` logs a message and exits without binding a socket, so the
  viewer can be turned off via config even while `clip_viewer.service` is
  installed.
- `viewer:` is **optional** — `load_config()` and `REQUIRED_CONFIG_KEYS` are
  unchanged; a config without a `viewer:` section uses the defaults.
- `--config` CLI flag, same default resolution as `sound_monitor.py`.
- If `clips.enabled` is false (no clips being written), the viewer still runs
  and shows the empty state.

## Testing

`tests/test_clip_viewer.py` exercises `ClipLibrary` against real temp dirs with
real (tiny) WAV files:

- filename parsing: a valid `YYYYmmdd_HHMMSS_<peak>dBFS.wav` yields the right
  `timestamp` + `peak_dbfs`; malformed names (`foo.wav`, `notaclip.txt`) are
  skipped, not errored.
- ordering: `list_clips()` returns newest-first.
- `resolve()` / `delete()` path safety: `../../etc/passwd`, an absolute path,
  and a name with a leading `/` all return `None` / `False`; a foreign
  `backup.wav` in the dir is refused; a real `*dBFS.wav` resolves and deletes.
- `delete()` of a nonexistent name returns `False` without raising.

The HTTP handler is thin glue over `ClipLibrary`; like `sound_monitor.py`'s
audio→MQTT seam, it's left to the manual Pi smoke test rather than unit tests.

## Docs

- README: a short **"Reviewing clips"** section — the viewer, its
  `viewer:` config, and the `clip_viewer.service` install steps (mirroring the
  existing systemd section). Include the low-tech `rsync -av pi@host:~/clips/ ./clips/`
  one-liner as an alternative for bulk/offline archiving.
- Project-structure tree gains `clip_viewer.py` and `clip_viewer.service`.

## Out of scope (YAGNI)

- Pagination — `clips.max_clips` (default 200) caps the set to one comfortable page.
- A "keep/flag" folder for curated examples.
- Auth, HTTPS, and reverse-proxy config.
- Waveform/spectrogram visualization.

## Manual verification (post-implementation, on the Pi)

Install and start `clip_viewer.service`; from a Mac/phone browser open
`http://<pi>:8099/`. Confirm: saved clips list newest-first with correct
time/peak, inline players play the audio, download works, and Delete removes a
clip (and it's gone after refresh). Confirm a crafted `/clips/../config.yaml`
request 404s.
