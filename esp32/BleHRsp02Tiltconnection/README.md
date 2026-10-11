# ESP32-C3 PPG + tilt BLE watch

This sketch is for an ESP32-C3 with a MAX30102 PPG sensor and MPU-6050 tilt/motion sensor.

## Behavior

- Samples HR and SpO2 from the MAX30102 while active.
- Detects movement by comparing the MPU-6050 acceleration vector with its reference.
- After `IDLE_TIMEOUT_MS` without movement, calls `particleSensor.shutDown()` and sends no valid vitals.
- Keeps BLE connected and leaves the MPU-6050 in low-power accelerometer cycle mode so motion can wake the watch.
- On movement, wakes the MAX30102, resets the PPG algorithm, and measures immediately.
- Sends `HR=0` and `SpO2=0` as an idle marker. The Android bridge maps those values to `null`, so they are not published as real vitals over MQTT.

Change `IDLE_TIMEOUT_MS` near the top of the sketch to tune the timeout. For bench testing, use `30000UL` for 30 seconds.

## Wiring

ESP32-C3 SuperMini:

| Device | ESP32-C3 |
| --- | --- |
| MAX30102 SDA | GPIO 8 |
| MAX30102 SCL | GPIO 9 |
| MPU-6050 SDA | GPIO 8 |
| MPU-6050 SCL | GPIO 9 |
| Both VCC | 3.3 V |
| Both GND | GND |

The MPU-6050 must be at I2C address `0x68`. The battery divider midpoint is read on GPIO 0.

## Battery wiring and test

Use a resistor divider for a single-cell Li-ion/LiPo battery. With the default
`DIVIDER_RATIO 2.0`, equal-value resistors are expected, for example:

```text
Battery + ---- 100k resistor ---- GPIO 0 ---- 100k resistor ---- GND
```

Do not connect the raw battery positive terminal directly to GPIO 0. The ADC
pin must stay within the ESP32-C3 ADC input range. Confirm the divider with a
multimeter before powering the board.

The firmware measures the battery at boot and once per minute. The battery
percentage is an estimate for a Li-ion cell, not a fuel-gauge measurement.

## Serial Monitor output

Open the Arduino Serial Monitor at `115200` baud. Serial output is a debug
view from the ESP32; it is not sent directly to Android. Android receives the
BLE notifications described in the contract below.

Typical boot and connection output looks like this:

```text
[Watch] Initializing (wrist PPG + motion idle)...
[Storage] LittleFS ready.
[HW] MAX30102 ready.
[HW] MPU-6050 active (low-power motion mode).
[Battery] 4.08 V | 82%
[Watch] Advertising. System ready.
[BLE] Phone connected. Flushing flash cache...
[Sensor] Measuring... keep the watch snug and your arm still.
[BLE Tx] Mode:1 HR:76 SpO2:98% Bat:82%
```

After the timeout with no detected movement:

```text
[Idle] No motion -> IDLE (PPG off, waiting for movement).
[Battery] 4.02 V | 79%
```

While idle, the ESP32 sends HR `0` and SpO2 `0` as BLE keep-alive markers.
The Android app converts those markers to `null`, displays `--`, and does not
publish them as MQTT vitals. When the tilt sensor detects movement again:

```text
[Idle] Motion detected -> ACTIVE.
[Sensor] Measuring... keep the watch snug and your arm still.
[BLE Tx] Mode:1 HR:77 SpO2:97% Bat:79%
```

To test the battery path, temporarily set `IDLE_TIMEOUT_MS` to `30000UL`,
power the board from the battery, and compare the printed voltage with a
multimeter reading across the battery terminals. Restore the five-minute
timeout after testing.

## Android BLE contract

The sketch preserves the Android app's existing contract:

- Device name: `Gabay-SmartWatch`
- HR service/measurement: `0x180D` / `0x2A37`
- SpO2 service/measurement: `0x1822` / `0x2A5E`
- Battery service/level: `0x180F` / `0x2A19`
- Mode service/characteristic: `0x1809` / `0x2A1C`

HR notifications use the standard two-byte format `[0x00, bpm]`. SpO2 notifications use one byte containing the percentage. Mode commands are `1=DAILY`, `2=WORKOUT`, and `3=SLEEP`.

## Arduino IDE libraries

Install:

- Adafruit MPU6050
- Adafruit Unified Sensor
- SparkFun MAX3010x Pulse and Proximity Sensor Library

Use an ESP32-C3 board definition and open Serial Monitor at `115200` baud.

## Validation sequence

1. Upload with `IDLE_TIMEOUT_MS` temporarily set to `30000UL`.
2. Scan and connect from the Android app.
3. Confirm HR and SpO2 notifications while the watch is moving.
4. Keep the watch still for 30 seconds and confirm the Android values become `--`, with no zero-valued MQTT vitals.
5. Move or tilt the watch and confirm the MAX30102 wakes and a fresh reading appears.
6. Restore the timeout to five minutes for normal use.
