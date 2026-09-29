"""
mqtt_bridge.py
Optional ingestion path alongside the HTTP POST /patients/{id}/vitals
endpoint: lets any MQTT-publishing source — e.g. your Android app
relaying Mi Band data — feed vitals into the exact same pipeline
(database write, blockchain log, alert check) without needing to
speak HTTP/REST at all.

Topic convention:  gabay/patients/<patient_id>/vitals
Payload (JSON):     {"hr": 78, "spo2": 97, "temp": 36.6}
                     (spo2/temp optional — same rule as the HTTP
                     endpoint, since a Mi Band relay may only have HR)

You need an actual MQTT BROKER for this to talk through — MQTTX is a
client/testing tool, not a broker. Recommended: run Mosquitto locally
(free, keeps patient vitals off the public internet). Point both your
Android app and MQTT_BROKER_HOST below at that same broker's address.
Do NOT use a public test broker (e.g. broker.emqx.io) for anything
beyond initial connectivity testing — those are visible to anyone.

This module is intentionally optional: if no broker is reachable at
startup, the backend logs a warning and keeps running normally — HTTP
ingestion is unaffected either way.
"""
import json
import logging
import os
from datetime import datetime, timezone, timedelta

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

import analytics

MQTT_BROKER_HOST = os.getenv("MQTT_BROKER_HOST", "192.168.100.86")
MQTT_BROKER_PORT = int(os.getenv("MQTT_BROKER_PORT", "1883"))
MQTT_TOPIC_FILTER = "gabay/patients/+/vitals"  # '+' matches any single topic level (the patient id)

logger = logging.getLogger("mqtt_bridge")
_client = None


def _on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        client.subscribe(MQTT_TOPIC_FILTER)
        print(f"MQTT Connected to {MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}")
        print(f"Subscribed to: {MQTT_TOPIC_FILTER}")
        logger.info(f"MQTT connected to {MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}, subscribed to {MQTT_TOPIC_FILTER}")
    else:
        logger.warning(f"MQTT connect failed: {reason_code}")

def _on_message(client, userdata, msg):
    try:
        # Expected topic shape: gabay/patients/<patient_id>/vitals
        parts = msg.topic.split("/")
        if len(parts) != 4:
            raise ValueError(f"Unexpected topic shape: {msg.topic}")
        patient_id = parts[2]

        payload = json.loads(msg.payload.decode("utf-8"))
        print(f"Received MQTT vitals for {patient_id}: {payload}")

        # Android's VitalsPayload uses camelCase names and reports epoch
        # milliseconds. Keep the original short names supported as well so
        # existing Arduino/MQTT publishers continue to work.
        hr = payload.get("hr", payload.get("heartRate"))
        spo2 = payload.get("spo2", payload.get("spO2"))
        temp = payload.get("temp", payload.get("temperature"))
        fall_detected = bool(payload.get("fall_detected", payload.get("fallDetected", False)))
        recorded_at = _payload_timestamp(payload)

        if hr is None and spo2 is None and temp is None:
            raise ValueError("Payload contains no supported vital values")

        analytics.record_vital(
            patient_id,
            hr=hr,
            spo2=spo2,
            temp=temp,
            fall_detected=fall_detected,
            recorded_at=recorded_at,
        )
        logger.info(f"Ingested MQTT vital for {patient_id}: {payload}")
    except Exception as e:
        # A malformed message from one bad publish should never crash
        # the whole ingestion pipeline — log it and move on.
        logger.error(f"Failed to process MQTT message on {msg.topic!r}: {e}")


def publish_due_commands():
    """Publish each due, uncommanded dose once for the ESP32 dispenser."""
    if _client is None or not _client.is_connected():
        return 0

    from database import get_conn

    now = datetime.now()
    window_start = (now - timedelta(minutes=1)).isoformat()
    window_end = (now + timedelta(minutes=1)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM dispense_events
               WHERE status='pending' AND command_sent_at IS NULL
               AND scheduled_datetime BETWEEN ? AND ?""",
            (window_start, window_end),
        ).fetchall()

        published = 0
        for row in rows:
            if publish_dispense_command(dict(row)):
                conn.execute(
                    "UPDATE dispense_events SET command_sent_at=? WHERE id=?",
                    (now.isoformat(), row["id"]),
                )
                published += 1
        conn.commit()
    return published


def publish_dispense_command(row):
    """Publish one dispense command and return whether MQTT accepted it."""
    if _client is None or not _client.is_connected():
        logger.warning("Cannot publish dispense command: MQTT is not connected")
        return False

    topic = f"gabay/patients/{row['patient_id']}/dispenser/command"
    payload = json.dumps({
        "command": "dispense",
        "event_id": row["id"],
        "med_name": row["med_name"],
        "dosage": row["dosage"],
        "scheduled_datetime": row["scheduled_datetime"],
        "alarm": True,
    })
    result = _client.publish(topic, payload, qos=1)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        logger.warning(f"Could not publish dispense command for event {row['id']}: {result.rc}")
        return False
    print(f"Dispense command sent for event {row['id']} ({row['med_name']})")
    return True


def _payload_timestamp(payload):
    """Convert Android epoch milliseconds to the ISO format used by SQLite."""
    timestamp = payload.get("timestamp")
    if timestamp is None:
        return None
    try:
        return datetime.fromtimestamp(float(timestamp) / 1000, timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        raise ValueError(f"Invalid timestamp {timestamp!r}") from exc


def start():
    """Called once from main.py's startup event. Runs the MQTT network
    loop in its own background thread (paho-mqtt's loop_start()), so
    it doesn't block or need to be awoken inside FastAPI's asyncio loop.
    connect_async keeps retrying if the broker starts after Gabay."""
    global _client
    _client = mqtt.Client(CallbackAPIVersion.VERSION2)
    _client.on_connect = _on_connect
    _client.on_message = _on_message
    _client.reconnect_delay_set(min_delay=1, max_delay=30)
    try:
        _client.loop_start()
        _client.connect_async(MQTT_BROKER_HOST, MQTT_BROKER_PORT, keepalive=60)
    except Exception as e:
        logger.warning(
            f"Could not connect to MQTT broker at {MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}: {e}. "
            "MQTT ingestion will keep retrying — HTTP POST /patients/{id}/vitals still works normally."
        )


def stop():
    if _client:
        _client.loop_stop()
        _client.disconnect()
