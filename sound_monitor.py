#!/usr/bin/env python3
"""
Sound level monitor for Home Assistant.

Captures audio continuously, computes Leq mean and max dBFS per 1-minute
window, and publishes to MQTT with Home Assistant auto-discovery.

Install dependencies:
    pip3 install sounddevice numpy paho-mqtt

List available audio devices:
    python3 -c "import sounddevice; print(sounddevice.query_devices())"
"""

import json
import logging
import time

import numpy as np
import paho.mqtt.client as mqtt
import sounddevice as sd

# ---------------------------------------------------------------------------
# Configuration — edit these
# ---------------------------------------------------------------------------
MQTT_BROKER   = "homeassistant.local"
MQTT_PORT     = 1883
MQTT_USER     = "mqtt_user"
MQTT_PASSWORD = "mqtt_password"

DEVICE_NAME   = "Living Room Sound Monitor"   # Friendly name shown in HA
DEVICE_ID     = "sound_monitor_living_room"   # Unique ID (no spaces)
TOPIC_BASE    = f"home/{DEVICE_ID}"

# Audio settings
# 16000 Hz is plenty for detecting pops/creaks and much lighter on a Pi Zero.
# 500 ms chunks reduce callback frequency, preventing input overflow.
SAMPLE_RATE      = 16000   # Hz
CHANNELS         = 1
CHUNK_SECONDS    = 0.5     # seconds per audio callback block
INTERVAL_SECONDS = 60      # publish every N seconds

# Watchdog: if no audio arrives for this many seconds, restart the stream.
WATCHDOG_SECONDS = INTERVAL_SECONDS * 2

# Optional: pin to a specific input device index (leave None for system default)
# Run: python3 -c "import sounddevice; print(sounddevice.query_devices())"
AUDIO_DEVICE = None
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sound_monitor")


def rms_to_dbfs(rms: float) -> float:
    """Convert linear RMS amplitude [0, 1] to dBFS."""
    return 20.0 * np.log10(max(rms, 1e-10))


def publish_discovery(client: mqtt.Client) -> None:
    """Publish MQTT auto-discovery messages so HA creates sensors automatically."""
    device_block = {
        "identifiers": [DEVICE_ID],
        "name": DEVICE_NAME,
        "model": "Raspberry Pi Sound Monitor",
        "manufacturer": "DIY",
    }

    metrics = {
        "mean_dbfs": {
            "name": f"{DEVICE_NAME} Mean dBFS",
            "icon": "mdi:microphone",
        },
        "max_dbfs": {
            "name": f"{DEVICE_NAME} Max dBFS",
            "icon": "mdi:microphone-plus",
        },
    }

    for key, meta in metrics.items():
        payload = {
            "name": meta["name"],
            "unique_id": f"{DEVICE_ID}_{key}",
            "state_topic": f"{TOPIC_BASE}/{key}",
            "unit_of_measurement": "dBFS",
            "icon": meta["icon"],
            "device": device_block,
            "expire_after": INTERVAL_SECONDS * 3,
        }
        client.publish(
            f"homeassistant/sensor/{DEVICE_ID}/{key}/config",
            json.dumps(payload),
            retain=True,
        )
        log.info("Published discovery for %s", key)


def run_stream(client: mqtt.Client) -> None:
    """Open the audio stream and publish until the watchdog or an exception fires."""
    chunk_size   = int(SAMPLE_RATE * CHUNK_SECONDS)
    audio_buffer: list[np.ndarray] = []
    window_start = time.monotonic()
    last_audio   = time.monotonic()

    def on_audio(indata: np.ndarray, frames: int, time_info, status) -> None:
        nonlocal last_audio
        if status:
            log.warning("Audio stream status: %s", status)
        audio_buffer.append(indata[:, 0].copy())
        last_audio = time.monotonic()

    log.info(
        "Opening audio stream  (device=%s, rate=%d Hz, chunk=%.1fs, interval=%ds)",
        AUDIO_DEVICE or "default",
        SAMPLE_RATE,
        CHUNK_SECONDS,
        INTERVAL_SECONDS,
    )

    with sd.InputStream(
        device=AUDIO_DEVICE,
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="float32",
        blocksize=chunk_size,
        callback=on_audio,
    ):
        while True:
            time.sleep(0.5)

            # Watchdog: restart stream if audio has gone silent unexpectedly
            if time.monotonic() - last_audio > WATCHDOG_SECONDS:
                log.warning("Watchdog: no audio for %ds — restarting stream.", WATCHDOG_SECONDS)
                audio_buffer.clear()
                return  # caller will reopen the stream

            elapsed = time.monotonic() - window_start
            if elapsed < INTERVAL_SECONDS:
                continue

            # --- Process the collected window ---
            if not audio_buffer:
                window_start = time.monotonic()
                continue

            all_samples = np.concatenate(audio_buffer)
            audio_buffer.clear()
            window_start = time.monotonic()

            # Compute per-chunk dBFS
            n_chunks = max(1, len(all_samples) // chunk_size)
            chunks   = np.array_split(all_samples, n_chunks)
            chunk_db = [
                rms_to_dbfs(float(np.sqrt(np.mean(c**2))))
                for c in chunks
                if len(c) > 0
            ]

            # Leq mean: average power in linear domain, then convert to dB
            mean_power = float(np.mean([10 ** (db / 20) for db in chunk_db]))
            mean_db = round(20 * np.log10(max(mean_power, 1e-10)), 1)
            max_db  = round(float(np.max(chunk_db)), 1)

            client.publish(f"{TOPIC_BASE}/mean_dbfs", mean_db)
            client.publish(f"{TOPIC_BASE}/max_dbfs",  max_db)
            log.info("Published  mean=%.1f dBFS  max=%.1f dBFS", mean_db, max_db)


def main() -> None:
    # --- MQTT setup ---
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=DEVICE_ID)
    client.username_pw_set(MQTT_USER, MQTT_PASSWORD)

    # Retry until the broker is reachable — handles slow DNS / boot ordering
    while True:
        try:
            client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
            break
        except Exception as exc:
            log.warning("MQTT connect failed (%s) — retrying in 10s", exc)
            time.sleep(10)

    client.loop_start()
    publish_discovery(client)

    # --- Stream loop with automatic restart ---
    while True:
        try:
            run_stream(client)
        except Exception as exc:
            log.error("Stream error: %s — restarting in 5s", exc)
            time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped.")
