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
import os
import queue
import threading
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np
import paho.mqtt.client as mqtt
import sounddevice as sd
import yaml

from event_detection import ClipRecorder, EventDetector, enqueue_or_drop
from system_stats import collect_stats

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
    "max_storage_mb": 1000,
    "max_clip_seconds": 60.0,
}
SYSTEM_DEFAULTS = {
    "enabled": True,
    "interval_seconds": 60,
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


def _device_block(config: dict) -> dict:
    """The shared HA `device` block so every entity (audio + system
    sensors) groups under one device card."""
    return {
        "identifiers": [config["device"]["id"]],
        "name": config["device"]["name"],
        "model": "Raspberry Pi Sound Monitor",
        "manufacturer": "DIY",
    }


def publish_discovery(
    client: mqtt.Client, config: dict, detection_enabled: bool
) -> None:
    """Publish MQTT auto-discovery messages so HA creates sensors automatically.

    ``detection_enabled`` gates the detector-derived sensors: the L90
    ``baseline_dbfs`` floor is only meaningful (and only ever published)
    when the event detector is running.
    """
    device_name = config["device"]["name"]
    device_id = config["device"]["id"]
    topic_base = f"home/{device_id}"
    interval_seconds = config["interval_seconds"]

    device_block = _device_block(config)

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

    if detection_enabled:
        # L90 ambient floor (10th-percentile level over the detector's rolling
        # window). Unlike Mean dBFS (an energy average that loud pops drag up),
        # this reads the quiet floor and stays put during transients.
        metrics["baseline_dbfs"] = {
            "name": f"{device_name} Baseline dBFS",
            "icon": "mdi:microphone-minus",
            "unit": "dBFS",
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


def publish_system_discovery(client: mqtt.Client, config: dict) -> None:
    """Publish MQTT discovery for system-health sensors: this process's own
    CPU%/memory, plus board-level voltage/temperature/swap/load/disk. Runs
    independently of ``detection_enabled`` -- these sensors don't depend on
    the event detector.
    """
    device_name = config["device"]["name"]
    device_id = config["device"]["id"]
    topic_base = f"home/{device_id}/system"
    device_block = _device_block(config)

    sensors = {
        "sound_monitor_cpu_percent": {
            "name": f"{device_name} CPU %",
            "unit": "%",
            "icon": "mdi:chip",
        },
        "sound_monitor_mem_mb": {
            "name": f"{device_name} Memory MB",
            "unit": "MB",
            "icon": "mdi:memory",
        },
        "core_volts": {
            "name": f"{device_name} Core Voltage",
            "unit": "V",
            "icon": "mdi:flash",
            "device_class": "voltage",
        },
        "cpu_temp_c": {
            "name": f"{device_name} CPU Temp",
            "unit": "°C",
            "icon": "mdi:thermometer",
            "device_class": "temperature",
        },
        "swap_percent": {
            "name": f"{device_name} Swap Used %",
            "unit": "%",
            "icon": "mdi:swap-horizontal",
        },
        "load_avg_1m": {
            "name": f"{device_name} Load Average (1m)",
            "unit": "",
            "icon": "mdi:speedometer",
        },
        "disk_free_percent": {
            "name": f"{device_name} SD Card Free %",
            "unit": "%",
            "icon": "mdi:sd",
        },
    }

    for key, meta in sensors.items():
        payload = {
            "name": meta["name"],
            "unique_id": f"{device_id}_{key}",
            "state_topic": f"{topic_base}/{key}",
            "unit_of_measurement": meta["unit"],
            "icon": meta["icon"],
            "state_class": "measurement",
            "device": device_block,
        }
        if "device_class" in meta:
            payload["device_class"] = meta["device_class"]
        client.publish(
            f"homeassistant/sensor/{device_id}/{key}/config",
            json.dumps(payload),
            retain=True,
        )
        log.info("Published discovery for %s", key)

    binary_payload = {
        "name": f"{device_name} Under-Voltage",
        "unique_id": f"{device_id}_under_voltage",
        "state_topic": f"{topic_base}/under_voltage",
        "device_class": "problem",
        "payload_on": "ON",
        "payload_off": "OFF",
        "device": device_block,
    }
    client.publish(
        f"homeassistant/binary_sensor/{device_id}/under_voltage/config",
        json.dumps(binary_payload),
        retain=True,
    )
    log.info("Published discovery for under_voltage")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Path to config YAML file (default: {DEFAULT_CONFIG_PATH})",
    )
    return parser.parse_args()


def run_stream(
    client: mqtt.Client,
    config: dict,
    detector: EventDetector | None,
    recorder: ClipRecorder | None,
) -> None:
    """Open the audio stream and process it until the watchdog trips.

    Returns when no audio has arrived for ``interval_seconds * 2`` (a wedged
    stream — USB glitch, device reset) so the caller can reopen it, and lets
    stream errors propagate for the same restart handling. The per-minute
    window stats and event counter are local, so a restart simply begins a
    fresh minute; the passed-in detector/recorder keep their state (L90
    history, clip pre-roll) across restarts.
    """
    device_id = config["device"]["id"]
    topic_base = f"home/{device_id}"
    interval_seconds = config["interval_seconds"]
    sample_rate = config["audio"]["sample_rate"]
    channels = config["audio"]["channels"]
    chunk_seconds = config["audio"]["chunk_seconds"]
    audio_device = config["audio"]["device"]

    chunk_size = int(sample_rate * chunk_seconds)
    # If no audio arrives for two intervals, the stream has wedged — return so
    # the caller reopens it rather than publishing silence forever.
    watchdog_seconds = interval_seconds * 2

    # Bounded so a stalled consumer (CPU contention, a slow disk write) can't
    # grow the queue without limit — on a Pi Zero's ~426 MB that turns into
    # swap-thrashing rather than a few dropped chunks. 60s of headroom is
    # generous versus the normal drain rate but caps backlog memory at a
    # few MB regardless of how long a stall lasts.
    queue_capacity = max(1, int(60 / chunk_seconds))
    audio_queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=queue_capacity)
    window_buffer: list[np.ndarray] = []
    window_start = time.monotonic()
    last_audio = time.monotonic()
    event_count = 0

    def on_audio(indata: np.ndarray, frames: int, time_info, status) -> None:
        nonlocal last_audio
        if status:
            log.warning("Audio stream status: %s", status)
        enqueue_or_drop(audio_queue, indata[:, 0].copy(), log)  # keep mono channel
        last_audio = time.monotonic()

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

            # Watchdog: if the stream has gone silent, bail so the caller
            # reopens it.
            if time.monotonic() - last_audio > watchdog_seconds:
                log.warning(
                    "Watchdog: no audio for %ds — reopening stream",
                    watchdog_seconds,
                )
                return

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

            # Split into per-chunk dBFS, then aggregate
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

            # L90 ambient floor from the detector (None during the first
            # second of warmup, or when detection is disabled).
            baseline_db = detector.baseline_dbfs if detector else None

            client.publish(f"{topic_base}/mean_dbfs", mean_db)
            client.publish(f"{topic_base}/max_dbfs",  max_db)
            client.publish(f"{topic_base}/events_per_minute", events_per_min)
            if baseline_db is not None:
                client.publish(f"{topic_base}/baseline_dbfs", round(baseline_db, 1))
            log.info(
                "Published  mean=%.1f  max=%.1f  baseline=%s dBFS  events=%d (%.1f/min)",
                mean_db, max_db,
                f"{baseline_db:.1f}" if baseline_db is not None else "n/a",
                event_count, events_per_min,
            )
            event_count = 0


def run_system_stats(
    client: mqtt.Client, config: dict, interval_seconds: float
) -> None:
    """Collect and publish system-health stats on its own cadence,
    independent of the audio pipeline. A failure in one cycle is logged
    and the loop continues -- this must keep running even if run_stream()
    is wedged or crash-looping, so HA never goes dark on system health at
    exactly the moment it's most needed.
    """
    device_id = config["device"]["id"]
    topic_base = f"home/{device_id}/system"
    pid = os.getpid()

    while True:
        try:
            stats = collect_stats(pid)
            for key, value in asdict(stats).items():
                if value is None:
                    continue
                if key == "under_voltage":
                    client.publish(f"{topic_base}/{key}", "ON" if value else "OFF")
                else:
                    client.publish(f"{topic_base}/{key}", round(value, 1))
            log.info(
                "System stats  cpu=%s%%  mem=%sMB  volts=%sV  temp=%s°C  "
                "swap=%s%%  load=%s  disk_free=%s%%  under_voltage=%s",
                stats.sound_monitor_cpu_percent, stats.sound_monitor_mem_mb,
                stats.core_volts, stats.cpu_temp_c, stats.swap_percent,
                stats.load_avg_1m, stats.disk_free_percent, stats.under_voltage,
            )
        except Exception as exc:
            log.error("System stats cycle failed: %s", exc)
        time.sleep(interval_seconds)


def main() -> None:
    args = parse_args()
    config = load_config(args.config)

    device_id = config["device"]["id"]
    sample_rate = config["audio"]["sample_rate"]

    # --- MQTT setup ---
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=device_id)
    client.username_pw_set(config["mqtt"]["user"], config["mqtt"]["password"])

    # Retry until the broker is reachable — at boot, MQTT/DNS may not be up yet.
    while True:
        try:
            client.connect(config["mqtt"]["broker"], config["mqtt"]["port"],
                           keepalive=60)
            break
        except Exception as exc:
            log.warning("MQTT connect failed (%s) — retrying in 10s", exc)
            time.sleep(10)

    client.loop_start()

    # --- Event detection setup ---
    # Resolve detection config before discovery so we know whether to
    # advertise the detector-derived baseline_dbfs sensor.
    det_cfg = {**DETECTION_DEFAULTS, **config.get("detection", {})}
    clips_cfg = {**CLIPS_DEFAULTS, **config.get("clips", {})}

    publish_discovery(client, config, det_cfg["enabled"])

    # --- System health stats (independent of the audio pipeline) ---
    sys_cfg = {**SYSTEM_DEFAULTS, **config.get("system", {})}
    if sys_cfg["enabled"]:
        publish_system_discovery(client, config)
        threading.Thread(
            target=run_system_stats,
            args=(client, config, sys_cfg["interval_seconds"]),
            daemon=True,
        ).start()
        log.info(
            "System stats on  (interval=%ds)", sys_cfg["interval_seconds"]
        )

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
                max_storage_mb=clips_cfg["max_storage_mb"],
                max_clip_seconds=clips_cfg["max_clip_seconds"],
            )
        log.info(
            "Event detection on  (threshold=+%.0f dB, clips=%s)",
            det_cfg["threshold_db"],
            clips_cfg["directory"] if recorder else "off",
        )

    # --- Stream loop with automatic restart ---
    # run_stream returns on a watchdog trip and raises on stream errors; either
    # way we reopen it. The detector/recorder persist across restarts.
    while True:
        try:
            run_stream(client, config, detector, recorder)
        except Exception as exc:
            log.error("Stream error: %s — reopening in 5s", exc)
            time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped.")
