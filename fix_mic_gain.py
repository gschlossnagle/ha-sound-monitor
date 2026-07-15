"""
Disable hardware AGC and pin capture gain before sound_monitor starts.

Run via ExecStartPre in sound_monitor.service. A mic's Auto Gain Control
(AGC) continuously re-scales analog gain, which means the same real sound
produces a different dBFS reading minute to minute — no calibration offset
(calibration.offset_db in config.yaml) can be valid unless gain is fixed
first. This script has no dependency on sounddevice or paho-mqtt so its
failure mode is independent of the main capture/publish pipeline.

Configure in config.yaml:
    audio:
      device: "ATR4697"            # substring matched against /proc/asound/cards
      capture_volume_percent: 100  # omit this key to skip the gain fix entirely
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

import yaml

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"

CARD_LINE_RE = re.compile(r"^\s*(\d+)\s+\[([^\]]*)\]:\s*(.*)$")


def parse_cards(cards_text: str) -> dict[int, str]:
    """Parse `/proc/asound/cards`-formatted text into {index: block_text}.

    Each card's block_text is its header line plus any indented
    continuation lines, joined together — long/marketing names (e.g.
    "Conference USB microphone ATR4697-USB") often live on the
    continuation line, not the short card id, so both must be searchable.
    """
    blocks: dict[int, list[str]] = {}
    current_index = None
    for line in cards_text.splitlines():
        m = CARD_LINE_RE.match(line)
        if m:
            current_index = int(m.group(1))
            blocks[current_index] = [m.group(2).strip(), m.group(3).strip()]
        elif current_index is not None and line.strip():
            blocks[current_index].append(line.strip())
    return {idx: " ".join(parts) for idx, parts in blocks.items()}


def find_card_index(cards_text: str, name_substring: str) -> int:
    """Return the single ALSA card index whose block contains name_substring
    (case-insensitive). Raises ValueError if zero or more than one match.
    """
    blocks = parse_cards(cards_text)
    needle = name_substring.lower()
    matches = [idx for idx, text in blocks.items() if needle in text.lower()]
    if not matches:
        raise ValueError(
            f"No ALSA card matched {name_substring!r} in:\n{cards_text}"
        )
    if len(matches) > 1:
        raise ValueError(
            f"{name_substring!r} matched multiple ALSA cards {matches} — "
            f"use a more specific substring in audio.device"
        )
    return matches[0]


def run_amixer(card_index: int, control: str, value: str) -> None:
    subprocess.run(
        ["amixer", "-c", str(card_index), "sset", control, value],
        check=True,
        capture_output=True,
        text=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    args = parser.parse_args()

    config = yaml.safe_load(args.config.read_text())
    audio_cfg = config.get("audio", {})
    volume_percent = audio_cfg.get("capture_volume_percent")

    if volume_percent is None:
        print("audio.capture_volume_percent not set — skipping AGC/gain fix")
        return 0

    device_substring = audio_cfg.get("device")
    if not device_substring:
        print(
            "audio.capture_volume_percent is set but audio.device is empty "
            "— cannot resolve which ALSA card to fix",
            file=sys.stderr,
        )
        return 1

    cards_text = Path("/proc/asound/cards").read_text()
    try:
        card_index = find_card_index(cards_text, device_substring)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        run_amixer(card_index, "Auto Gain Control,0", "off")
        run_amixer(card_index, "Mic,0", f"{volume_percent}%")
    except subprocess.CalledProcessError as exc:
        print(
            f"amixer failed on card {card_index}: {exc.stderr.strip()}",
            file=sys.stderr,
        )
        return 1
    except OSError as exc:
        print(f"Could not run amixer: {exc}", file=sys.stderr)
        return 1

    print(
        f"Card {card_index}: Auto Gain Control off, Mic capture volume "
        f"pinned to {volume_percent}%"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
