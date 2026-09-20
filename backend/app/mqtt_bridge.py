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

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

import analytics

MQTT_BROKER_HOST = "localhost"   # Use localhost if broker is on the same machine
MQTT_BROKER_PORT = 1883
MQTT_TOPIC_FILTER = "gabay/patients/+/vitals"  # '+' matches any single topic level (the patient id)

logger = logging.getLogger("mqtt_bridge")
_client = None


def _on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        client.subscribe(MQTT_TOPIC_FILTER)
        print(f"✅ MQTT Connected to {MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}")
        print(f"📡 Subscribed to: {MQTT_TOPIC_FILTER}")
        logger.info(f"MQTT connected to {MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}, subscribed to {MQTT_TOPIC_FILTER}")
    else:
        print(f"❌ MQTT Connection failed with code {reason_code}")
        logger.warning(f"MQTT connect failed: {reason_code}")

def _on_message(client, userdata, msg):
    try:
        # Expected topic shape: gabay/patients/<patient_id>/vitals
        parts = msg.topic.split("/")
        if len(parts) != 4:
            raise ValueError(f"Unexpected topic shape: {msg.topic}")
        patient_id = parts[2]

        payload = json.loads(msg.payload.decode("utf-8"))
        print(f"📥 Received MQTT Vitals for {patient_id}: {payload}")

        analytics.record_vital(
            patient_id,
            hr=payload.get("hr"),
            spo2=payload.get("spo2"),
            temp=payload.get("temp"),
        )
        logger.info(f"Ingested MQTT vital for {patient_id}: {payload}")
    except Exception as e:
        # A malformed message from one bad publish should never crash
        # the whole ingestion pipeline — log it and move on.
        logger.error(f"Failed to process MQTT message on {msg.topic!r}: {e}")


def start():
    """Called once from main.py's startup event. Runs the MQTT network
    loop in its own background thread (paho-mqtt's loop_start()), so
    it doesn't block or need to be awoken inside FastAPI's asyncio loop."""
    global _client
    _client = mqtt.Client(CallbackAPIVersion.VERSION2)
    _client.on_connect = _on_connect
    _client.on_message = _on_message
    try:
        _client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, keepalive=60)
        _client.loop_start()
    except Exception as e:
        logger.warning(
            f"Could not connect to MQTT broker at {MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}: {e}. "
            "MQTT ingestion is disabled — HTTP POST /patients/{id}/vitals still works normally."
        )


def stop():
    if _client:
        _client.loop_stop()
        _client.disconnect()
