/*
 * ESP32-C3 Smart Watch Firmware - WRIST PPG + MOTION IDLE (Arduino core 3.x)
 * - MAX30102 worn on the WRIST: auto LED gain, band-pass beat detection,
 *   motion-gated HR / SpO2
 * - MPU-6050: tilt/motion detection -> IDLE when still for IDLE_TIMEOUT_MS
 * - BLE always reachable (no light sleep)
 * - LittleFS offline logging + catch-up sync on reconnect
 *
 * Pinout (ESP32-C3 SuperMini):
 * - I2C: SDA = GPIO 8, SCL = GPIO 9 (MAX30102 + MPU-6050)
 * - Battery divider midpoint -> GPIO 0
 *
 * Required library: "SparkFun MAX3010x Pulse and Proximity Sensor Library"
 */

#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>
#include <Wire.h>
#include <LittleFS.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include "MAX30105.h"

// ---------------- PINS / CONSTANTS ----------------
#define I2C_SDA             9
#define I2C_SCL             8
#define BATTERY_ADC_PIN     0
#define DIVIDER_RATIO       2.0f

#define MPU_ADDR            0x68

#define CACHE_FILE          "/vitals_cache.bin"
#define MAX_CACHE_BYTES     (64 * 1024)
#define DEVICE_NAME         "Gabay-SmartWatch"

// ---------------- DEBUG ----------------
// 1 = stream "IR,SIG,BEAT" to the Serial Plotter for tuning (disable normal use)
#define DEBUG_PPG           0

// ---------------- IDLE / MOTION TUNING ----------------
#define IDLE_TIMEOUT_MS     (5UL * 60UL * 1000UL)  // no motion for 5 min -> IDLE (use 30000UL to test)
#define MOTION_POLL_MS      100                    // IMU poll period
#define TILT_DELTA_MS2      0.6f                   // change vs. reference = movement (idle logic)
#define BLANK_DELTA_MS2     1.5f                   // sample-to-sample change = strong motion (PPG gating)
#define MOTION_BLANK_MS     1500                   // ignore PPG this long after strong motion
#define ACTIVE_WINDOW_MS    60000UL                // "moving" = motion within last 60 s
#define IDLE_NOTIFY_MS      10000UL                // keep-alive zeros to the app while idle + connected

// ---------------- WRIST PPG TUNING ----------------
#define SAMPLE_RATE_HZ      100                    // 400 sps / 4 averaging
#define SAMPLE_MS           (1000 / SAMPLE_RATE_HZ)
#define ADC_RANGE           8192
#define LED_DEFAULT         0x4F                   // starting LED current
#define LED_MIN             0x1F
#define LED_MAX             0xBF                   // ~37 mA peak; raise only if needed (power!)
#define IR_TARGET_LOW       50000                  // raise LED below this DC level
#define IR_TARGET_HIGH      180000                 // lower LED above this DC level
#define IR_TARGET_MID       100000
#define GAIN_CHECK_SAMPLES  200                    // re-evaluate LED level every 2 s
#define SKIN_IR_MIN         15000                  // below this = sensor not on skin
#define SETTLE_SAMPLES      200                    // 2 s settle after contact / LED change
#define MIN_PEAK_PI         0.0002f                // min pulse size relative to DC (wrist ~0.1-0.5 %)
#define REFRACTORY_MS       300                    // no two beats closer than this
#define RATE_SIZE           7                      // beats in the median
#define SPO2_ENABLED        1                      // red/IR SpO2 on a wrist is experimental
#define SPO2_WINDOW         300                    // 3 s window per SpO2 estimate
#define SPO2_HIST           3
#define SESSION_TIMEOUT_MS  30000

// ---------------- BLE UUIDS ----------------
#define HR_SERVICE_UUID          "0000180d-0000-1000-8000-00805f9b34fb"
#define HR_MEASUREMENT_UUID      "00002a37-0000-1000-8000-00805f9b34fb"
#define SPO2_SERVICE_UUID        "00001822-0000-1000-8000-00805f9b34fb"
#define SPO2_MEASUREMENT_UUID    "00002a5e-0000-1000-8000-00805f9b34fb"
#define BATTERY_SERVICE_UUID     "0000180f-0000-1000-8000-00805f9b34fb"
#define BATTERY_LEVEL_UUID       "00002a19-0000-1000-8000-00805f9b34fb"
// These UUIDs match BleWatchManager.kt in the Android bridge.
#define CONTROL_SERVICE_UUID     "00001809-0000-1000-8000-00805f9b34fb"
#define MODE_CHARACTERISTIC_UUID "00002a1c-0000-1000-8000-00805f9b34fb"

// ---------------- TYPES ----------------
struct VitalsRecord {
  uint32_t secondsSinceBoot;
  uint8_t  hr;
  uint8_t  spo2;
};

enum WatchMode {
  MODE_DAILY   = 1,  // Connected: 5s | Offline: 1m moving / 5m resting
  MODE_WORKOUT = 2,  // 1s continuous, NOT motion-gated (expect noisy readings)
  MODE_SLEEP   = 3   // 1m sampling (idle is disabled in this mode)
};

// ---------------- GLOBAL STATE ----------------
volatile WatchMode currentWatchMode = MODE_DAILY;
volatile bool modeChanged = false;

unsigned long lastSampleTime = 0;
unsigned long lastBatteryCheckTime = 0;
unsigned long lastSensorRetry = 0;
bool sensorsAwake = true;     // refers to the MAX30102 (the IMU stays on)

// ---- motion / idle ----
unsigned long lastMotionMs = 0;        // tilt / movement (idle logic)
unsigned long lastStrongMotionMs = 0;  // strong movement (PPG gating)
unsigned long lastMotionPoll = 0;
unsigned long lastIdleNotify = 0;
bool idleMode = false;
bool haveRef = false;
float refX = 0, refY = 0, refZ = 0;
float prevX = 0, prevY = 0, prevZ = 0;
bool ppgBlanked = false;

BLEServer* pServer = NULL;
BLECharacteristic* pHrCharacteristic = NULL;
BLECharacteristic* pSpo2Characteristic = NULL;
BLECharacteristic* pModeCharacteristic = NULL;
BLECharacteristic* pBatteryCharacteristic = NULL;
BLEAdvertising* pAdvertising = NULL;

Adafruit_MPU6050 mpu;
MAX30105 particleSensor;

volatile bool deviceConnected = false;
bool oldDeviceConnected = false;
bool mpuFound = false;
bool max30102Found = false;

uint8_t batteryLevelPct = 100;
float batteryVoltage = 4.2;

// ---- PPG algorithm state ----
uint8_t ledLevel = LED_DEFAULT;
uint32_t sampleIdx = 0;
bool skinContact = false;
uint16_t gainCounter = 0;

float dcRed = 0, dcIr = 0, dcBeat = 0;
float lp1 = 0, lp2 = 0;
float sigPrev1 = 0, sigPrev2 = 0;
float ampEnv = 0;

float rates[RATE_SIZE];
uint8_t rateSpot = 0;
uint8_t beatCount = 0;
bool haveLastBeat = false;
uint32_t lastBeatIdx = 0;
bool hrValid = false;
float bpm = 0;

double accRed = 0, accIr = 0;
uint16_t accN = 0;
uint16_t settleLeft = SETTLE_SAMPLES;
float spo2Hist[SPO2_HIST];
uint8_t spo2Spot = 0;
uint8_t spo2Count = 0;
bool spo2Valid = false;
float spo2Avg = 0;

// ---------------- FORWARD DECLARATIONS ----------------
bool isBleConnected();
bool initMax30102();
void applyLed();
bool adjustLed();
void resetAlgorithm();
void processSample(uint32_t red, uint32_t ir);
void pollSensor();
void runMeasurementSession(uint32_t timeoutMs);
void reportReading(bool connected);
void notifyIdle();
void updateMotion();
void pollMotionIfDue();
void updateIdleState();
bool isMovingRecently();
uint8_t readBatteryLevel();
void updateBatteryTelemetry();
void initStorage();
void logVitalsOffline(uint8_t hr, uint8_t spo2);
void flushOfflineBuffer();
void wakeSensors();
void sleepSensors();
unsigned long getSampleIntervalMs(bool connected);

// ---------------- BLE CALLBACKS ----------------
class WatchServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer* server) {
    deviceConnected = true;
    Serial.println("[BLE] Phone connected.");
  }

  void onDisconnect(BLEServer* server) {
    deviceConnected = false;
    Serial.println("[BLE] Phone disconnected -> offline logging.");
  }
};

class WatchModeCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic* characteristic) override {
    String rxValue = characteristic->getValue();
    if (rxValue.length() > 0) {
      uint8_t mode = static_cast<uint8_t>(rxValue[0]);
      if (mode >= MODE_DAILY && mode <= MODE_SLEEP) {
        currentWatchMode = static_cast<WatchMode>(mode);
        modeChanged = true;
        characteristic->setValue(&mode, 1);
        Serial.printf("[BLE] Watch mode set via app to %u (%s).\n",
                      mode,
                      mode == MODE_WORKOUT ? "WORKOUT" : mode == MODE_SLEEP ? "SLEEP" : "DAILY");
      } else {
        Serial.printf("[BLE] Invalid mode value received: %u\n", mode);
      }
    }
  }
};

bool isBleConnected() {
  return deviceConnected;
}

// ---------------- MOTION / IDLE ENGINE ----------------
void updateMotion() {
  if (!mpuFound) {
    lastMotionMs = millis();   // no IMU -> never go idle, never gate PPG
    return;
  }

  sensors_event_t a, g, temp;
  mpu.getEvent(&a, &g, &temp);
  float ax = a.acceleration.x, ay = a.acceleration.y, az = a.acceleration.z;

  if (!haveRef) {
    refX = prevX = ax;
    refY = prevY = ay;
    refZ = prevZ = az;
    haveRef = true;
    lastMotionMs = millis();
    return;
  }

  // Tilt / movement relative to a reference (used for idle)
  float dx = ax - refX, dy = ay - refY, dz = az - refZ;
  if (sqrtf(dx * dx + dy * dy + dz * dz) > TILT_DELTA_MS2) {
    lastMotionMs = millis();
    refX = ax; refY = ay; refZ = az;
  }

  // Sample-to-sample jerk (used to blank the PPG signal)
  float jx = ax - prevX, jy = ay - prevY, jz = az - prevZ;
  if (sqrtf(jx * jx + jy * jy + jz * jz) > BLANK_DELTA_MS2) {
    lastStrongMotionMs = millis();
    lastMotionMs = millis();
  }
  prevX = ax; prevY = ay; prevZ = az;
}

void pollMotionIfDue() {
  if (millis() - lastMotionPoll >= MOTION_POLL_MS) {
    lastMotionPoll = millis();
    updateMotion();
  }
}

bool isMovingRecently() {
  return (millis() - lastMotionMs) < ACTIVE_WINDOW_MS;
}

void updateIdleState() {
  bool idleAllowed = (currentWatchMode != MODE_SLEEP);
  unsigned long stillMs = millis() - lastMotionMs;

  if (!idleMode && idleAllowed && stillMs >= IDLE_TIMEOUT_MS) {
    idleMode = true;
    sleepSensors();
    lastIdleNotify = 0;
    Serial.println("[Idle] No motion -> IDLE (PPG off, waiting for movement).");
  } else if (idleMode && (!idleAllowed || stillMs < IDLE_TIMEOUT_MS)) {
    idleMode = false;
    lastSampleTime = 0;
    Serial.println("[Idle] Motion detected -> ACTIVE.");
  }
}

void notifyIdle() {
  uint8_t hrBuffer[2] = {0x00, 0};
  pHrCharacteristic->setValue(hrBuffer, 2);
  pHrCharacteristic->notify();

  uint8_t zero = 0;
  pSpo2Characteristic->setValue(&zero, 1);
  pSpo2Characteristic->notify();
}

// ---------------- MAX30102 SETUP ----------------
void applyLed() {
  particleSensor.setPulseAmplitudeRed(ledLevel);
  particleSensor.setPulseAmplitudeIR(ledLevel);
}

bool initMax30102() {
  if (!particleSensor.begin(Wire, I2C_SPEED_FAST)) return false;
  // LED level, avg 4, mode 2 = RED + IR, 400 sps, 411 us pulse, ADC range
  particleSensor.setup(ledLevel, 4, 2, 400, 411, ADC_RANGE);
  particleSensor.clearFIFO();
  resetAlgorithm();
  return true;
}

// Keep the DC level inside the sensor's sweet spot (strap tightness / skin differ)
bool adjustLed() {
  float dcMax = fmaxf(dcIr, dcRed);
  float scale;

  if (dcMax > IR_TARGET_HIGH)      scale = IR_TARGET_MID / dcMax;
  else if (dcIr < IR_TARGET_LOW)   scale = IR_TARGET_MID / dcIr;
  else                             return false;

  int nl = (int)lroundf(ledLevel * scale);
  nl = constrain(nl, LED_MIN, LED_MAX);
  if (nl == ledLevel) return false;

  ledLevel = (uint8_t)nl;
  applyLed();
  particleSensor.clearFIFO();
  resetAlgorithm();
  Serial.printf("[PPG] LED level -> 0x%02X (DC IR %.0f)\n", ledLevel, dcIr);
  return true;
}

// ---------------- PPG ALGORITHM ----------------
void resetAlgorithm() {
  rateSpot = 0;
  beatCount = 0;
  haveLastBeat = false;
  hrValid = false;
  bpm = 0;

  accRed = accIr = 0;
  accN = 0;
  settleLeft = SETTLE_SAMPLES;
  gainCounter = 0;
  spo2Spot = 0;
  spo2Count = 0;
  spo2Valid = false;
  spo2Avg = 0;

  lp1 = lp2 = 0;
  sigPrev1 = sigPrev2 = 0;
  ampEnv = 0;
}

static float medianRate() {
  float tmp[RATE_SIZE];
  for (uint8_t i = 0; i < beatCount; i++) tmp[i] = rates[i];
  for (uint8_t i = 1; i < beatCount; i++) {
    float key = tmp[i];
    int j = i - 1;
    while (j >= 0 && tmp[j] > key) { tmp[j + 1] = tmp[j]; j--; }
    tmp[j + 1] = key;
  }
  return tmp[beatCount / 2];
}

void processSample(uint32_t red, uint32_t ir) {
  sampleIdx++;

  // ---- skin contact (with hysteresis) ----
  float contactLimit = skinContact ? (SKIN_IR_MIN * 0.8f) : (float)SKIN_IR_MIN;
  if ((float)ir < contactLimit) {
    if (skinContact) {
      skinContact = false;
      resetAlgorithm();
      if (ledLevel != LED_DEFAULT) {
        ledLevel = LED_DEFAULT;
        applyLed();
      }
    }
    return;
  }
  if (!skinContact) {
    skinContact = true;
    resetAlgorithm();
  }

  // ---- settle: track levels directly, no filtering yet ----
  if (settleLeft > 0) {
    settleLeft--;
    dcRed = (float)red;
    dcIr = (float)ir;
    dcBeat = (float)ir;
    lp1 = lp2 = 0;
    sigPrev1 = sigPrev2 = 0;
    ampEnv = 0;
    return;
  }

  // ---- baselines ----
  dcRed  += 0.02f * ((float)red - dcRed);
  dcIr   += 0.02f * ((float)ir  - dcIr);
  dcBeat += 0.03f * ((float)ir  - dcBeat);     // ~0.5 Hz high-pass

  // ---- band-pass (~0.5 - 4 Hz). Inverted: IR drops when blood volume rises.
  float hp = dcBeat - (float)ir;
  lp1 += 0.25f * (hp - lp1);
  lp2 += 0.25f * (lp1 - lp2);
  float sig = lp2;

  bool blanked = ppgBlanked;

  // ---- adaptive threshold ----
  if (!blanked) {
    float absSig = fabsf(sig);
    if (absSig > ampEnv) ampEnv = absSig;
    else ampEnv *= 0.998f;                      // ~3.5 s half-life
  }
  float thr = fmaxf(0.5f * ampEnv, MIN_PEAK_PI * dcIr);

  // ---- peak (beat) detection ----
  bool beat = false;
  if (!blanked && sigPrev1 > sigPrev2 && sigPrev1 >= sig && sigPrev1 > thr) {
    uint32_t peakIdx = sampleIdx - 1;
    if (!haveLastBeat || (peakIdx - lastBeatIdx) * SAMPLE_MS >= REFRACTORY_MS) {
      beat = true;
      if (haveLastBeat) {
        uint32_t dtMs = (peakIdx - lastBeatIdx) * SAMPLE_MS;
        if (dtMs >= 333 && dtMs <= 1500) {      // 40 - 180 BPM
          rates[rateSpot] = 60000.0f / (float)dtMs;
          rateSpot = (rateSpot + 1) % RATE_SIZE;
          if (beatCount < RATE_SIZE) beatCount++;
        }
      }
      haveLastBeat = true;
      lastBeatIdx = peakIdx;

      if (beatCount >= 4) {
        bpm = medianRate();
        hrValid = true;
      }
    }
  }
  sigPrev2 = sigPrev1;
  sigPrev1 = sig;

#if DEBUG_PPG
  Serial.printf("IR:%u,SIG:%.1f,THR:%.1f,BEAT:%d\n", (unsigned)ir, sig, thr, beat ? 200 : 0);
#endif

  // No beat for 5 s -> reading is stale
  if (haveLastBeat && (sampleIdx - lastBeatIdx) * SAMPLE_MS > 5000) {
    beatCount = 0;
    rateSpot = 0;
    haveLastBeat = false;
    hrValid = false;
  }

  // ---- LED gain control ----
  if (++gainCounter >= GAIN_CHECK_SAMPLES) {
    gainCounter = 0;
    if (adjustLed()) return;
  }

  // ---- SpO2 (ratio of ratios) ----
  if (blanked) {                                 // motion contaminates the window
    accRed = accIr = 0;
    accN = 0;
    return;
  }

  float acRed = (float)red - dcRed;
  float acIr  = (float)ir  - dcIr;
  accRed += (double)acRed * acRed;
  accIr  += (double)acIr * acIr;
  accN++;

  if (accN >= SPO2_WINDOW) {
    float rmsRed = sqrtf((float)(accRed / accN));
    float rmsIr  = sqrtf((float)(accIr / accN));
    accRed = accIr = 0;
    accN = 0;

    float acdcRed = rmsRed / dcRed;
    float acdcIr  = rmsIr / dcIr;

    if (acdcIr > 0.0001f && acdcIr < 0.03f && acdcRed > 0.0f) {
      float R = acdcRed / acdcIr;
      float s = -45.060f * R * R + 30.354f * R + 94.845f;
      if (s > 100.0f && s < 104.0f) s = 100.0f;
      if (s >= 70.0f && s <= 100.0f) {
        spo2Hist[spo2Spot] = s;
        spo2Spot = (spo2Spot + 1) % SPO2_HIST;
        if (spo2Count < SPO2_HIST) spo2Count++;

        float sum = 0;
        for (uint8_t i = 0; i < spo2Count; i++) sum += spo2Hist[i];
        spo2Avg = sum / spo2Count;
        spo2Valid = (spo2Count >= 2);
      }
    }
  }
}

void pollSensor() {
  if (!max30102Found || !sensorsAwake) return;

  // Workout mode accepts noisy data; other modes pause during strong motion
  ppgBlanked = (currentWatchMode != MODE_WORKOUT) &&
               (millis() - lastStrongMotionMs < MOTION_BLANK_MS);

  particleSensor.check();
  while (particleSensor.available()) {
    uint32_t red = particleSensor.getFIFORed();
    uint32_t ir  = particleSensor.getFIFOIR();
    particleSensor.nextSample();
    processSample(red, ir);
  }
}

void runMeasurementSession(uint32_t timeoutMs) {
  wakeSensors();
  unsigned long start = millis();
  Serial.println("[Sensor] Measuring... keep the watch snug and your arm still.");

  while (millis() - start < timeoutMs) {
    pollMotionIfDue();
    pollSensor();
    bool spo2Done = (!SPO2_ENABLED) || spo2Valid;
    if (hrValid && spo2Done) break;
    if (!skinContact && millis() - start > 3000) break;   // watch isn't being worn
    delay(5);
  }
}

void reportReading(bool connected) {
  bool hrOk   = max30102Found && skinContact && hrValid;
  bool spo2Ok = SPO2_ENABLED && max30102Found && skinContact && spo2Valid;
  uint8_t hr   = hrOk   ? (uint8_t)lroundf(bpm)     : 0;
  uint8_t spo2 = spo2Ok ? (uint8_t)lroundf(spo2Avg) : 0;

  if (connected) {
    uint8_t hrBuffer[2] = {0x00, hr};
    pHrCharacteristic->setValue(hrBuffer, 2);
    pHrCharacteristic->notify();

    pSpo2Characteristic->setValue(&spo2, 1);
    pSpo2Characteristic->notify();

#if !DEBUG_PPG
    if (!max30102Found)      Serial.println("[Sensor] MAX30102 not found - check wiring.");
    else if (!skinContact)   Serial.println("[Sensor] No skin contact (off-wrist or too loose).");
    else if (!hrOk)          Serial.println("[Sensor] Acquiring / motion - no valid reading yet.");
    else Serial.printf("[BLE Tx] Mode:%d HR:%d SpO2:%d%% Bat:%d%%\n",
                       (int)currentWatchMode, hr, spo2, batteryLevelPct);
#endif
  } else {
    if (hrOk) {
      logVitalsOffline(hr, spo2);
    } else {
      Serial.println("[Sensor] No valid reading - nothing logged.");
    }
  }
}

// ---------------- SENSOR POWER MANAGEMENT ----------------
void sleepSensors() {
  if (!sensorsAwake) return;
  if (max30102Found) particleSensor.shutDown();
  sensorsAwake = false;
}

void wakeSensors() {
  if (sensorsAwake) return;
  if (max30102Found) {
    particleSensor.wakeUp();
    particleSensor.clearFIFO();
  }
  resetAlgorithm();
  skinContact = false;
  delay(50);
  sensorsAwake = true;
}

unsigned long getSampleIntervalMs(bool connected) {
  switch (currentWatchMode) {
    case MODE_WORKOUT: return 1000UL;
    case MODE_SLEEP:   return 60000UL;
    case MODE_DAILY:
    default:
      if (connected) return 5000UL;
      return isMovingRecently() ? 60000UL : 300000UL;
  }
}

// ---------------- SETUP ----------------
void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("[Watch] Initializing (wrist PPG + motion idle)...");

  setCpuFrequencyMhz(80);
  analogReadResolution(12);
  initStorage();

  Wire.begin(I2C_SDA, I2C_SCL);
  delay(100);

  max30102Found = initMax30102();
  Serial.println(max30102Found ? "[HW] MAX30102 ready."
                               : "[HW] MAX30102 NOT found - check wiring/power. Will retry.");

  if (mpu.begin(MPU_ADDR)) {
    mpuFound = true;
    mpu.setAccelerometerRange(MPU6050_RANGE_2_G);
    mpu.setFilterBandwidth(MPU6050_BAND_21_HZ);
    // Low-power accelerometer-only mode (gyro + temp off, 20 Hz samples)
    mpu.setGyroStandby(true, true, true);
    mpu.setTemperatureStandby(true);
    mpu.setCycleRate(MPU6050_CYCLE_20_HZ);
    mpu.enableCycle(true);
    Serial.println("[HW] MPU-6050 active (low-power motion mode).");
  } else {
    Serial.println("[HW] MPU-6050 not detected. Idle + motion gating disabled.");
  }
  lastMotionMs = millis();

  // ---- BLE ----
  BLEDevice::init(DEVICE_NAME);
  pServer = BLEDevice::createServer();
  pServer->setCallbacks(new WatchServerCallbacks());

  BLEService* pHrService = pServer->createService(HR_SERVICE_UUID);
  pHrCharacteristic = pHrService->createCharacteristic(
      HR_MEASUREMENT_UUID, BLECharacteristic::PROPERTY_NOTIFY);
  pHrCharacteristic->addDescriptor(new BLE2902());

  BLEService* pSpo2Service = pServer->createService(SPO2_SERVICE_UUID);
  pSpo2Characteristic = pSpo2Service->createCharacteristic(
      SPO2_MEASUREMENT_UUID, BLECharacteristic::PROPERTY_NOTIFY);
  pSpo2Characteristic->addDescriptor(new BLE2902());

  BLEService* pControlService = pServer->createService(CONTROL_SERVICE_UUID);
  pModeCharacteristic = pControlService->createCharacteristic(
      MODE_CHARACTERISTIC_UUID,
      BLECharacteristic::PROPERTY_READ |
      BLECharacteristic::PROPERTY_WRITE |
      BLECharacteristic::PROPERTY_WRITE_NR |
      BLECharacteristic::PROPERTY_NOTIFY
  );
  pModeCharacteristic->addDescriptor(new BLE2902());
  pModeCharacteristic->setCallbacks(new WatchModeCallbacks());
  uint8_t initialMode = (uint8_t)currentWatchMode;
  pModeCharacteristic->setValue(&initialMode, 1);

  BLEService* pBatService = pServer->createService(BATTERY_SERVICE_UUID);
  pBatteryCharacteristic = pBatService->createCharacteristic(
      BATTERY_LEVEL_UUID,
      BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_NOTIFY);
  pBatteryCharacteristic->addDescriptor(new BLE2902());

  pHrService->start();
  pSpo2Service->start();
  pControlService->start();
  pBatService->start();

  pAdvertising = BLEDevice::getAdvertising();
  pAdvertising->addServiceUUID(BLEUUID((uint16_t)0x180D));
  pAdvertising->setScanResponse(true);
  pAdvertising->setMinInterval(0x0320);  // 500 ms
  pAdvertising->setMaxInterval(0x0640);  // 1 s
  BLEDevice::startAdvertising();

  updateBatteryTelemetry();
  Serial.println("[Watch] Advertising. System ready.");
}

// ---------------- MAIN LOOP ----------------
void loop() {
  bool connected = isBleConnected();

  // Reconnect -> flush offline cache
  if (connected && !oldDeviceConnected) {
    oldDeviceConnected = true;
    delay(500);
    Serial.println("[BLE] Connected. Flushing flash cache...");
    flushOfflineBuffer();
    if (max30102Found && sensorsAwake) particleSensor.clearFIFO();
    resetAlgorithm();
  }

  // Disconnect -> restart advertising
  if (!connected && oldDeviceConnected) {
    oldDeviceConnected = false;
    delay(300);
    BLEDevice::startAdvertising();
    Serial.println("[BLE] Advertising restarted.");
  }

  // Battery every 60 s
  if (millis() - lastBatteryCheckTime >= 60000) {
    lastBatteryCheckTime = millis();
    updateBatteryTelemetry();
  }

  pollMotionIfDue();

  // A mode change from the app counts as activity
  if (modeChanged) {
    modeChanged = false;
    uint8_t m = (uint8_t)currentWatchMode;
    if (pModeCharacteristic) {
      pModeCharacteristic->setValue(&m, 1);
      if (connected) pModeCharacteristic->notify();
    }
    lastSampleTime = 0;
    lastMotionMs = millis();
    Serial.printf("[Watch Mode] Mode updated to %u (%s)\n",
                  m,
                  m == MODE_WORKOUT ? "WORKOUT" : m == MODE_SLEEP ? "SLEEP" : "DAILY");
  }

  updateIdleState();

  // ===== IDLE: PPG off, only IMU + BLE running =====
  if (idleMode) {
    sleepSensors();
    if (connected && millis() - lastIdleNotify >= IDLE_NOTIFY_MS) {
      lastIdleNotify = millis();
      notifyIdle();   // HR=0, SpO2=0 so the app can show "idle"
    }
    delay(connected ? 50 : 100);
    return;
  }

  // Retry the PPG sensor if it wasn't found at boot
  if (!max30102Found && millis() - lastSensorRetry >= 5000) {
    lastSensorRetry = millis();
    max30102Found = initMax30102();
    if (max30102Found) {
      sensorsAwake = true;
      Serial.println("[HW] MAX30102 detected.");
    }
  }

  unsigned long interval = getSampleIntervalMs(connected);
  bool continuous = (interval <= 5000);  // workout (1s) or connected daily (5s)

  if (continuous) {
    wakeSensors();
    pollSensor();

    if (lastSampleTime == 0 || millis() - lastSampleTime >= interval) {
      lastSampleTime = millis();
      reportReading(connected);
    }
    delay(10);

  } else {
    if (lastSampleTime == 0 || millis() - lastSampleTime >= interval) {
      runMeasurementSession(SESSION_TIMEOUT_MS);
      reportReading(connected);
      sleepSensors();
      lastSampleTime = millis();
    } else {
      sleepSensors();
      delay(connected ? 20 : 100);
    }
  }
}

// ---------------- BATTERY ----------------
uint8_t readBatteryLevel() {
  uint32_t adcSum = 0;
  for (int i = 0; i < 10; i++) {
    adcSum += analogReadMilliVolts(BATTERY_ADC_PIN);
    delay(2);
  }
  float pinMilliVolts = adcSum / 10.0;
  batteryVoltage = (pinMilliVolts * DIVIDER_RATIO) / 1000.0;

  int percentage;
  if (batteryVoltage >= 4.20)      percentage = 100;
  else if (batteryVoltage <= 3.30) percentage = 0;
  else if (batteryVoltage > 3.85)  percentage = 60 + (int)((batteryVoltage - 3.85) * 114.0);
  else if (batteryVoltage > 3.70)  percentage = 20 + (int)((batteryVoltage - 3.70) * 266.0);
  else                             percentage = (int)((batteryVoltage - 3.30) * 50.0);

  return (uint8_t)constrain(percentage, 0, 100);
}

void updateBatteryTelemetry() {
  batteryLevelPct = readBatteryLevel();
  if (pBatteryCharacteristic != NULL) {
    pBatteryCharacteristic->setValue(&batteryLevelPct, 1);
    if (isBleConnected()) pBatteryCharacteristic->notify();
  }
#if !DEBUG_PPG
  Serial.printf("[Battery] %.2f V | %d%%\n", batteryVoltage, batteryLevelPct);
#endif
}

// ---------------- LITTLEFS OFFLINE STORAGE ----------------
void initStorage() {
  if (!LittleFS.begin(true)) {
    Serial.println("[Storage] LittleFS mount failed!");
    return;
  }
  Serial.println("[Storage] LittleFS ready.");
}

void logVitalsOffline(uint8_t hr, uint8_t spo2) {
  if (LittleFS.exists(CACHE_FILE)) {
    File chk = LittleFS.open(CACHE_FILE, FILE_READ);
    size_t sz = chk ? chk.size() : 0;
    if (chk) chk.close();
    if (sz >= MAX_CACHE_BYTES) {
      Serial.println("[Storage] Cache full, clearing old data.");
      LittleFS.remove(CACHE_FILE);
    }
  }

  File file = LittleFS.open(CACHE_FILE, FILE_APPEND);
  if (!file) {
    Serial.println("[Storage] Error opening cache file!");
    return;
  }

  VitalsRecord record;
  record.secondsSinceBoot = millis() / 1000;
  record.hr = hr;
  record.spo2 = spo2;

  file.write((uint8_t*)&record, sizeof(record));
  file.close();

  Serial.printf("[Storage] Saved T+%us HR:%d SpO2:%d%%\n",
                (unsigned)record.secondsSinceBoot, hr, spo2);
}

void flushOfflineBuffer() {
  if (!LittleFS.exists(CACHE_FILE)) {
    Serial.println("[Storage] No offline records to sync.");
    return;
  }

  File file = LittleFS.open(CACHE_FILE, FILE_READ);
  if (!file || file.size() == 0) {
    if (file) file.close();
    return;
  }

  Serial.printf("[Storage] Flushing %u records...\n",
                (unsigned)(file.size() / sizeof(VitalsRecord)));

  VitalsRecord record;
  bool syncComplete = true;

  while (file.read((uint8_t*)&record, sizeof(record)) == sizeof(record)) {
    if (!isBleConnected()) {
      Serial.println("[Storage] Connection dropped mid-sync! Keeping logs.");
      syncComplete = false;
      break;
    }

    uint8_t hrBuffer[2] = {0x00, record.hr};
    pHrCharacteristic->setValue(hrBuffer, 2);
    pHrCharacteristic->notify();

    uint8_t spo2Val = record.spo2;
    pSpo2Characteristic->setValue(&spo2Val, 1);
    pSpo2Characteristic->notify();

    delay(30);
  }

  file.close();

  if (syncComplete) {
    LittleFS.remove(CACHE_FILE);
    Serial.println("[Storage] Sync complete. Cache erased.");
  }
}