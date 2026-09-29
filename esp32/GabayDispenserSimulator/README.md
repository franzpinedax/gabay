# Gabay ESP32 dispenser simulator

This sketch listens for Gabay's scheduled dispense commands and simulates the
dispenser with a three-second alarm and a status LED. It then confirms the
dispense to Gabay over HTTP.

## Arduino IDE libraries

Install these libraries before compiling:

- PubSubClient
- ArduinoJson

The ESP32 Wi-Fi and HTTP libraries are included with the ESP32 Arduino core.

## Configure

Edit the constants at the top of
[GabayDispenserSimulator.ino](./GabayDispenserSimulator.ino):

- `WIFI_SSID` and `WIFI_PASSWORD`
- `MQTT_HOST` — the PC running Mosquitto
- `GABAY_HOST` — the PC running Gabay's FastAPI backend
- `PATIENT_ID` — must match the scheduled patient

The default topics are:

```text
gabay/patients/franzpineda8249/dispenser/command
```

The default confirmation request is:

```text
POST http://192.168.100.86:8000/dispense-events/<event_id>/dispensed
```

Open Serial Monitor at `115200` baud. When Gabay publishes a command, the
sketch prints the medication, sounds the alarm pin for three seconds, turns on
the status LED, and sends the confirmation request.

For a real dispenser, replace the `delay(3000)` simulation in
`simulateDispense()` with the servo/carousel movement and keep the
`confirmDispensed(eventId)` call only after the mechanism has completed.
