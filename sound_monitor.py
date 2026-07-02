#!/usr/bin/env python3
"""
Sound level monitor for Home Assistant.

Captures audio continuously, computes mean and max dBFS per 1-minute window,
and publishes to MQTT with Home Assistant auto-discovery.

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
MQTT_BROKER   = "192.168.1.x"      # IP of your HA / Mosquitto broker
MQTT_PORT     = 1883
MQTT_USER     = "mqtt_user"
MQTT_PASSWORD = "mqtt_password"

DEVICE_NAME   = "Living Room Sound Monitor"   # Friendly name shown in HA
DEVICE_ID     = "sound_monitor_living_room"   # Unique ID (no spaces)
TOPIC_BASE    = f"home/{DEVICE_ID}"

# Audio settings
SAMPLE_RATE      = 44100   # Hz — most USB mics support this
CHANNELS         = 1
CHUNK_SECONDS    = 0.1     # 100 ms chunks fed into the buffer
INTERVAL_SECONDS = 60      # Publish every N seconds

# Optional: pin to a specific input device index (leave None for system default)
# Run: python3 -c "import sounddevice; print(sounddevice.query_devices())"
# to find your device index.
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
            # Keep last value displayed until next update
            "expire_after": INTERVAL_SECONDS * 3,
        }
        client.publish(
            f"homeassistant/sensor/{DEVICE_ID}/{key}/config",
            json.dumps(payload),
            retain=True,
        )
        log.info("Published discovery for %s", key)


def main() -> None:
    # --- MQTT setup ---
    client = mqtt.Client(client_id=DEVICE_ID)
    client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    client.loop_start()

    publish_discovery(client)

    # --- Audio capture loop ---
    chunk_size   = int(SAMPLE_RATE * CHUNK_SECONDS)
    audio_buffer: list[np.ndarray] = []
    window_start = time.monotonic()

    def on_audio(indata: np.ndarray, frames: int, time_info, status) -> None:
        if status:
            log.warning("Audio stream status: %s", status)
        audio_buffer.append(indata[:, 0].copy())  # keep mono channel

    log.info(
        "Starting audio capture  (device=%s, rate=%d Hz, interval=%ds)",
        AUDIO_DEVICE or "default",
        SAMPLE_RATE,
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
            time.sleep(0.1)

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

            client.publish(f"{TOPIC_BASE}/mean_dbfs", mean_db)
            client.publish(f"{TOPIC_BASE}/max_dbfs",  max_db)
            log.info("Published  mean=%.1f dBFS  max=%.1f dBFS", mean_db, max_db)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Stopped.")
