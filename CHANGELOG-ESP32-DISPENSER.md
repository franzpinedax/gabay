# ESP32 dispenser integration

## What changed

### Gabay backend

- Added MQTT publishing for scheduled dispense commands.
- The background scheduler checks pending medication events every 30 seconds.
- Each event is published once to:

  `gabay/patients/<patient_id>/dispenser/command`

- Added `command_sent_at` tracking to avoid duplicate commands.
- Added `POST /dispense-events/{event_id}/command` for immediate testing.
- Updated the dashboard's **Simulate Dispense** button to publish the MQTT
  command instead of pretending that the ESP32 already dispensed the dose.
- Restored Windows-safe terminal messages for MQTT connection and received
  vitals.
- MQTT broker host and port can be configured with `MQTT_BROKER_HOST` and
  `MQTT_BROKER_PORT`.

### Dashboard

- Manual dispenser testing now exercises the real MQTT command path.
- The ESP32 remains responsible for confirming actual mechanical actuation
  through `POST /dispense-events/{event_id}/dispensed`.

### Android Health Bridge

- Added an MQTT connection checker with visible error details.
- Fixed the vitals topic generation.
- Added medication reminder notifications using WorkManager.
- Added a high-priority notification channel and Android 13 notification
  permission handling.
- Built and installed a debug APK successfully.

### ESP32 simulator

- Added `esp32/GabayDispenserSimulator`.
- Connects to Wi-Fi and Mosquitto.
- Subscribes to the patient dispenser command topic.
- Simulates a three-second alarm and status LED.
- Sends the dispense confirmation HTTP request back to Gabay.

## ESP32 command contract

Subscribe to:

```text
gabay/patients/<patient_id>/dispenser/command
```

Example payload:

```json
{
  "command": "dispense",
  "event_id": 42,
  "med_name": "Metformin",
  "dosage": "500 mg",
  "scheduled_datetime": "2026-09-30T08:00:00",
  "alarm": true
}
```

After the motor/carousel finishes, call:

```text
POST http://<gabay-server-ip>:8000/dispense-events/42/dispensed
```

## Validation performed

- Backend Python syntax check passed.
- Frontend Vite production build passed.
- Android debug APK build passed.
- Android debug APK installed successfully on the connected device.
- Fresh backend verification returned `sent: true` from the manual MQTT command
  endpoint and logged the command publication.
