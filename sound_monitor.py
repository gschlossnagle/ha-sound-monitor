#!/usr/bin/env python3
"""
Sound level monitor for Home Assistant.

Captures audio continuously, computes mean and max dBFS per 1-minute window,
and publishes to MQTT with Home Assistant auto-discovery.

Install dependencies:
    pip3 install -r requirements.txt

Configure:
    cp config.yaml.example config.yaml
    # then edit config.yaml

List available audio devices:
    python3 -c "import sounddevice; print(sounddevice.query_devices())"
"""

import argparse
import json
import logging
import queue
import time
from pathlib import Path

import numpy as np
import paho.mqtt.client as mqtt
import sounddevice as sd
import yaml

from event_detection import ClipRecorder, EventDetector

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sound_monitor")

REQUIRED_CONFIG_KEYS = {
    "mqtt": ["broker", "port", "user", "password"],
    "device": ["name", "id"],
    "audio": ["sample_rate", "channels", "chunk_seconds", "device"],
}

# Optional sections — merged over these defaults if present in config.yaml.
DETECTION_DEFAULTS = {
    "enabled": True,
    "frame_seconds": 0.01,
    "threshold_db": 15.0,
    "refractory_seconds": 0.2,
    "baseline_window_seconds": 30.0,
    "min_trigger_dbfs": -70.0,
}
CLIPS_DEFAULTS = {
    "enabled": True,
    "directory": "clips",
    "pre_seconds": 1.0,
    "post_seconds": 2.0,
    "max_clips": 200,
}


def load_config(path: Path) -> dict:
    """Load and validate the YAML config file."""
    if not path.exists():
        raise SystemExit(
            f"Config file not found: {path}\n"
            f"Copy config.yaml.example to {path.name} and fill in your values."
        )

    with path.open() as f:
        config = yaml.safe_load(f)

    for section, keys in REQUIRED_CONFIG_KEYS.items():
        if section not in config:
            raise SystemExit(f"Config missing section: {section}")
        for key in keys:
            if key not in config[section]:
                raise SystemExit(f"Config missing key: {section}.{key}")

    if "interval_seconds" not in config:
        raise SystemExit("Config missing key: interval_seconds")

    return config


def rms_to_dbfs(rms: float) -> float:
    """Convert linear RMS amplitude [0, 1] to dBFS."""
    return 20.0 * np.log10(max(rms, 1e-10))


def publish_discovery(client: mqtt.Client, config: dict) -> None:
    """Publish MQTT auto-discovery messages so HA creates sensors automatically."""
    device_name = config["device"]["name"]
    device_id = config["device"]["id"]
    topic_base = f"home/{device_id}"
    interval_seconds = config["interval_seconds"]

    device_block = {
        "identifiers": [device_id],
        "name": device_name,
        "model": "Raspberry Pi Sound Monitor",
        "manufacturer": "DIY",
    }

    metrics = {
        "mean_dbfs": {
            "name": f"{device_name} Mean dBFS",
            "icon": "mdi:microphone",
            "unit": "dBFS",
        },
        "max_dbfs": {
            "name": f"{device_name} Max dBFS",
            "icon": "mdi:microphone-plus",
            "unit": "dBFS",
        },
        "events_per_minute": {
            "name": f"{device_name} Events Per Minute",
            "icon": "mdi:pulse",
            "unit": "events/min",
        },
        "last_event_peak": {
            "name": f"{device_name} Last Event Peak",
            "icon": "mdi:waveform",
            "unit": "dBFS",
            "state_topic": f"{topic_base}/event",
            "value_template": "{{ value_json.peak_dbfs }}",
            # A pop an hour ago is still the last event — never expire.
            "no_expire": True,
        },
    }

    for key, meta in metrics.items():
        payload = {
            "name": meta["name"],
            "unique_id": f"{device_id}_{key}",
            "state_topic": meta.get("state_topic", f"{topic_base}/{key}"),
            "unit_of_measurement": meta["unit"],
            "icon": meta["icon"],
            "device": device_block,
        }
        if "value_template" in meta:
            payload["value_template"] = meta["value_template"]
        if not meta.get("no_expire"):
            # Keep last value displayed until next update
            payload["expire_after"] = interval_seconds * 3
        client.publish(
            f"homeassistant/sensor/{device_id}/{key}/config",
            json.dumps(payload),
            retain=True,
        )
        log.info("Published discovery for %s", key)


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
    config = load_config(args.config)

    device_id = config["device"]["id"]
    topic_base = f"home/{device_id}"
    interval_seconds = config["interval_seconds"]
    sample_rate = config["audio"]["sample_rate"]
    channels = config["audio"]["channels"]
    chunk_seconds = config["audio"]["chunk_seconds"]
    audio_device = config["audio"]["device"]

    # --- MQTT setup ---
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=device_id)
    client.username_pw_set(config["mqtt"]["user"], config["mqtt"]["password"])
    client.connect(config["mqtt"]["broker"], config["mqtt"]["port"], keepalive=60)
    client.loop_start()

    publish_discovery(client, config)

    # --- Event detection setup ---
    det_cfg = {**DETECTION_DEFAULTS, **config.get("detection", {})}
    clips_cfg = {**CLIPS_DEFAULTS, **config.get("clips", {})}

    detector = None
    recorder = None
    if det_cfg["enabled"]:
        detector = EventDetector(
            sample_rate=sample_rate,
            frame_seconds=det_cfg["frame_seconds"],
            threshold_db=det_cfg["threshold_db"],
            refractory_seconds=det_cfg["refractory_seconds"],
            baseline_window_seconds=det_cfg["baseline_window_seconds"],
            min_trigger_dbfs=det_cfg["min_trigger_dbfs"],
        )
        if clips_cfg["enabled"]:
            recorder = ClipRecorder(
                sample_rate=sample_rate,
                directory=clips_cfg["directory"],
                pre_seconds=clips_cfg["pre_seconds"],
                post_seconds=clips_cfg["post_seconds"],
                max_clips=clips_cfg["max_clips"],
            )
        log.info(
            "Event detection on  (threshold=+%.0f dB, clips=%s)",
            det_cfg["threshold_db"],
            clips_cfg["directory"] if recorder else "off",
        )

    # --- Audio capture loop ---
    chunk_size = int(sample_rate * chunk_seconds)
    # Unbounded is safe: the 0.1s drain loop far outpaces the ~10 chunks/s
    # producer, and paho's loop_start() keeps client.publish() non-blocking.
    audio_queue: queue.Queue[np.ndarray] = queue.Queue()
    window_buffer: list[np.ndarray] = []
    window_start = time.monotonic()
    event_count = 0

    def on_audio(indata: np.ndarray, frames: int, time_info, status) -> None:
        if status:
            log.warning("Audio stream status: %s", status)
        audio_queue.put(indata[:, 0].copy())  # keep mono channel

    log.info(
        "Starting audio capture  (device=%s, rate=%d Hz, interval=%ds)",
        audio_device or "default",
        sample_rate,
        interval_seconds,
    )

    with sd.InputStream(
        device=audio_device,
        samplerate=sample_rate,
        channels=channels,
        dtype="float32",
        blocksize=chunk_size,
        callback=on_audio,
    ):
        while True:
            time.sleep(0.1)

            # Drain the queue: feed minute stats and the event detector.
            while True:
                try:
                    chunk = audio_queue.get_nowait()
                except queue.Empty:
                    break
                window_buffer.append(chunk)
                events = detector.process(chunk) if detector else []
                for ev in events:
                    event_count += 1
                    # peak_dbfs here is the trigger-instant value. The clip
                    # filename uses ev.peak_dbfs after refractory updates, so
                    # a louder follow-up frame can make the two disagree.
                    client.publish(
                        f"{topic_base}/event",
                        json.dumps({
                            "timestamp": ev.timestamp,
                            "peak_dbfs": round(ev.peak_dbfs, 1),
                            "baseline_dbfs": round(ev.baseline_dbfs, 1),
                            "over_baseline_db": round(ev.over_baseline_db, 1),
                        }),
                    )
                    log.info(
                        "Event  peak=%.1f dBFS  (+%.1f dB over baseline)",
                        ev.peak_dbfs, ev.over_baseline_db,
                    )
                if recorder:
                    recorder.process(chunk, events)

            elapsed = time.monotonic() - window_start
            if elapsed < interval_seconds:
                continue

            # --- Process the collected window ---
            if not window_buffer:
                window_start = time.monotonic()
                continue

            all_samples = np.concatenate(window_buffer)
            window_buffer.clear()
            window_start = time.monotonic()

            # Split into 100 ms chunks, compute per-chunk dBFS, then aggregate
            n_chunks = max(1, len(all_samples) // chunk_size)
            chunks   = np.array_split(all_samples, n_chunks)
            chunk_db = [
                rms_to_dbfs(float(np.sqrt(np.mean(c**2))))
                for c in chunks
                if len(c) > 0
            ]

            # Leq: average power in linear domain, then convert back to dB
            mean_power = float(np.mean([10 ** (db / 20) for db in chunk_db]))
            mean_db = round(20 * np.log10(max(mean_power, 1e-10)), 1)
            max_db  = round(float(np.max(chunk_db)), 1)

            # Normalize the interval's event count to a per-minute rate so the
            # "Events Per Minute" sensor stays truthful when interval_seconds
            # != 60. At the default 60 s interval this equals the raw count.
            events_per_min = round(event_count * 60 / interval_seconds, 1)

            client.publish(f"{topic_base}/mean_dbfs", mean_db)
            client.publish(f"{topic_base}/max_dbfs",  max_db)
            client.publish(f"{topic_base}/events_per_minute", events_per_min)
            log.info(
                "Published  mean=%.1f dBFS  max=%.1f dBFS  events=%d (%.1f/min)",
                mean_db, max_db, event_count, events_per_min,
            )
            event_count = 0


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped.")
