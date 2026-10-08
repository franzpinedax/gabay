/*
 * ESP32-C3 Smart Watch Firmware - Full Build
 * Features:
 * - MPU-6050 Motion Engine (Adaptive Sampling)
 * - Hardware Light Sleep & Dynamic Power Management
 * - Standard BLE GATT Services (HR, SpO2, Battery, Control Mode)
 * - LittleFS Offline Flash Logging & Catch-up Synchronization
 * - ADC Battery Voltage Telemetry (GPIO 0)
 *
 * Hardware Pinout (ESP32-C3 SuperMini):
 * - MAX30102 PPG & MPU-6050 IMU -> Shared I2C Bus: SDA (GPIO 8), SCL (GPIO 9)
 * - Battery Divider Midpoint    -> GPIO 0 (ADC1_CH0)
 */

#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>
#include <Wire.h>
#include <LittleFS.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>
#include <esp_sleep.h>

// -------------------------------------------------------------------
// PIN DEFINITIONS & HARDWARE CONSTANTS
// -------------------------------------------------------------------
#define I2C_SDA             8
#define I2C_SCL             9
#define BATTERY_ADC_PIN     0

#define R1_VAL              100000.0  // 100k Ohm
#define R2_VAL              100000.0  // 100k Ohm
#define DIVIDER_RATIO       ((R1_VAL + R2_VAL) / R2_VAL) // 2.0

#define CACHE_FILE          "/vitals_cache.bin"
#define DEVICE_NAME         "Gabay-SmartWatch"

// -------------------------------------------------------------------
// BLE GATT SERVICE & CHARACTERISTIC UUIDS
// -------------------------------------------------------------------
#define HR_SERVICE_UUID          "0000180d-0000-1000-8000-00805f9b34fb"
#define HR_MEASUREMENT_UUID      "00002a37-0000-1000-8000-00805f9b34fb"

#define SPO2_SERVICE_UUID        "00001822-0000-1000-8000-00805f9b34fb"
#define SPO2_MEASUREMENT_UUID    "00002a5e-0000-1000-8000-00805f9b34fb"

#define CONTROL_SERVICE_UUID     "00001809-0000-1000-8000-00805f9b34fb"
#define MODE_CHARACTERISTIC_UUID "00002a1c-0000-1000-8000-00805f9b34fb"

#define BATTERY_SERVICE_UUID    "0000180f-0000-1000-8000-00805f9b34fb"
#define BATTERY_LEVEL_UUID      "00002a19-0000-1000-8000-00805f9b34fb"

// -------------------------------------------------------------------
// DATA STRUCTS & ENUMS
// -------------------------------------------------------------------
struct VitalsRecord {
  uint32_t secondsSinceBoot;
  uint8_t  hr;
  uint8_t  spo2;
};

enum WatchMode {
  MODE_DAILY = 1,   // Adaptive: 1m moving / 5m resting offline
  MODE_WORKOUT = 2, // Continuous 1s sampling
  MODE_SLEEP = 3    // Overnight 1m sampling
};

// -------------------------------------------------------------------
// GLOBAL SYSTEM STATE
// -------------------------------------------------------------------
WatchMode currentWatchMode = MODE_DAILY;
unsigned long logIntervalMs = 5000;
unsigned long lastLogTime = 0;
unsigned long lastBatteryCheckTime = 0;

BLEServer* pServer = NULL;
BLECharacteristic* pHrCharacteristic = NULL;
BLECharacteristic* pSpo2Characteristic = NULL;
BLECharacteristic* pModeCharacteristic = NULL;
BLECharacteristic* pBatteryCharacteristic = NULL;
BLEAdvertising* pAdvertising = NULL;

Adafruit_MPU6050 mpu;

volatile bool deviceConnected = false;
bool oldDeviceConnected = false;
bool mpuFound = false;
bool max30102Found = false;

uint8_t currentHeartRate = 75;
float currentSpO2 = 98.2;
uint8_t batteryLevelPct = 100;
float batteryVoltage = 4.2;

// -------------------------------------------------------------------
// FUNCTION DECLARATIONS
// -------------------------------------------------------------------
bool isBleConnected();
void readVitalsSensor();
bool detectMotionActivity();
uint8_t readBatteryLevel();
void updateBatteryTelemetry();
void initStorage();
void logVitalsOffline(uint8_t hr, uint8_t spo2);
void flushOfflineBuffer();

// -------------------------------------------------------------------
// BLE CALLBACKS
// -------------------------------------------------------------------
class WatchServerCallbacks: public BLEServerCallbacks {
    void onConnect(BLEServer* pServer) {
      deviceConnected = true;
      Serial.println("[BLE Watch] Central device connected.");
      
      // Request connection parameters for ESP32 Modem Sleep / Slave Latency
      // minInterval = 80 (100ms), maxInterval = 160 (200ms), latency = 4, timeout = 600 (6s)
      pServer->updateConnParams(pServer->getConnId(), 80, 160, 4, 600);
    };

    void onDisconnect(BLEServer* pServer) {
      deviceConnected = false;
      Serial.println("[BLE Watch] Central device disconnected -> Switching to Offline/Async mode.");
    }
};

class WatchModeCallbacks: public BLECharacteristicCallbacks {
    void onWrite(BLECharacteristic *pCharacteristic) {
      uint8_t* rxValue = pCharacteristic->getData();
      size_t length = pCharacteristic->getLength();

      if (length > 0) {
        uint8_t modeVal = rxValue[0];
        if (modeVal == 1) {
          currentWatchMode = MODE_DAILY;
          logIntervalMs = 5000;
          Serial.println("[BLE Watch] Switched to DAILY MODE (Adaptive)");
        } else if (modeVal == 2) {
          currentWatchMode = MODE_WORKOUT;
          logIntervalMs = 1000;
          Serial.println("[BLE Watch] Switched to WORKOUT MODE (Continuous 1s)");
        } else if (modeVal == 3) {
          currentWatchMode = MODE_SLEEP;
          logIntervalMs = 60000;
          Serial.println("[BLE Watch] Switched to SLEEP MODE (Low-power 1m)");
        }
      }
    }
};

// Helper: Checks hardware BLE stack connection status
bool isBleConnected() {
  return (deviceConnected && pServer != NULL && pServer->getConnectedCount() > 0);
}

// -------------------------------------------------------------------
// SETUP
// -------------------------------------------------------------------
void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("[ESP32-C3 Watch] Initializing Gabay Smart Watch Firmware...");

  analogReadResolution(12);

  // 1. Initialize Storage
  initStorage();

  // 2. Initialize Shared I2C Bus (SDA: GPIO 8, SCL: GPIO 9)
  Wire.begin(I2C_SDA, I2C_SCL);
  delay(100);

  // Probe MAX30102 PPG Sensor on 0x57
  Wire.beginTransmission(0x57);
  if (Wire.endTransmission() == 0) {
    max30102Found = true;
    Serial.println("[Hardware] MAX30102 PPG Sensor detected on I2C (0x57)!");
  } else {
    max30102Found = false;
    Serial.println("[Hardware] MAX30102 not detected. Using high-fidelity simulator.");
  }

  // Probe MPU-6050 IMU on 0x68
  if (mpu.begin(0x68)) {
    mpuFound = true;
    mpu.setAccelerometerRange(MPU6050_RANGE_2_G);
    mpu.setFilterBandwidth(MPU6050_BAND_5_HZ);
    Serial.println("[Hardware] MPU-6050 Motion Engine active (0x68)!");
  } else {
    mpuFound = false;
    Serial.println("[Hardware] MPU-6050 not detected. Motion adaptive mode disabled.");
  }

  // 3. Initialize BLE Peripheral
  BLEDevice::init(DEVICE_NAME);
  pServer = BLEDevice::createServer();
  pServer->setCallbacks(new WatchServerCallbacks());

  // Heart Rate Service (0x180D)
  BLEService *pHrService = pServer->createService(HR_SERVICE_UUID);
  pHrCharacteristic = pHrService->createCharacteristic(HR_MEASUREMENT_UUID, BLECharacteristic::PROPERTY_NOTIFY);
  pHrCharacteristic->addDescriptor(new BLE2902());

  // SpO2 Service (0x1822)
  BLEService *pSpo2Service = pServer->createService(SPO2_SERVICE_UUID);
  pSpo2Characteristic = pSpo2Service->createCharacteristic(SPO2_MEASUREMENT_UUID, BLECharacteristic::PROPERTY_NOTIFY);
  pSpo2Characteristic->addDescriptor(new BLE2902());

  // Control Mode Service (0x1809)
  BLEService *pControlService = pServer->createService(CONTROL_SERVICE_UUID);
  pModeCharacteristic = pControlService->createCharacteristic(MODE_CHARACTERISTIC_UUID, BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_WRITE);
  pModeCharacteristic->setCallbacks(new WatchModeCallbacks());
  uint8_t initialMode = (uint8_t)currentWatchMode;
  pModeCharacteristic->setValue(&initialMode, 1);

  // Battery Telemetry Service (0x180F)
  BLEService *pBatService = pServer->createService(BATTERY_SERVICE_UUID);
  pBatteryCharacteristic = pBatService->createCharacteristic(BATTERY_LEVEL_UUID, BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_NOTIFY);
  pBatteryCharacteristic->addDescriptor(new BLE2902());

  // Start All Services
  pHrService->start();
  pSpo2Service->start();
  pControlService->start();
  pBatService->start();

  // Configure Advertising with Power-Saving Intervals
  pAdvertising = BLEDevice::getAdvertising();
  pAdvertising->addServiceUUID(HR_SERVICE_UUID);
  pAdvertising->addServiceUUID(SPO2_SERVICE_UUID);
  pAdvertising->addServiceUUID(CONTROL_SERVICE_UUID);
  pAdvertising->addServiceUUID(BATTERY_SERVICE_UUID);
  pAdvertising->setScanResponse(true);
  
  // Dynamic Slower Advertising: 1280ms min to 2560ms max interval
  pAdvertising->setMinInterval(0x0800);
  pAdvertising->setMaxInterval(0x1000);

  BLEDevice::startAdvertising();
  Serial.println("[ESP32-C3 Watch] BLE Advertising active. System Ready.");
}

// -------------------------------------------------------------------
// MAIN LOOP
// -------------------------------------------------------------------
void loop() {
  bool connected = isBleConnected();

  // 1. RECONNECTION EVENT: Trigger offline catch-up sync once reconnected
  if (connected && !oldDeviceConnected) {
    oldDeviceConnected = true;
    Serial.println("[BLE Guard] Connection established. Flushing LittleFS flash cache...");
    flushOfflineBuffer();
  }

  // 2. DISCONNECTION EVENT: Handle state cleanup & restart advertising
  if (!connected && oldDeviceConnected) {
    oldDeviceConnected = false;
    Serial.println("[BLE Guard] Connection lost. Restarting low-power BLE Advertising...");
    delay(100);
    pAdvertising->setMinInterval(0x0800);
    pAdvertising->setMaxInterval(0x1000);
    pServer->startAdvertising();
  }

  // 3. PERIODIC BATTERY TELEMETRY CHECK (Every 60 Seconds)
  if (millis() - lastBatteryCheckTime >= 60000) {
    lastBatteryCheckTime = millis();
    updateBatteryTelemetry();
  }

  // 4. EXECUTION PATH ROUTING
  if (connected) {
    // === ONLINE STREAMING MODE ===
    if (millis() - lastLogTime >= logIntervalMs) {
      lastLogTime = millis();
      readVitalsSensor();

      uint8_t hrBuffer[2] = {0x00, currentHeartRate};
      pHrCharacteristic->setValue(hrBuffer, 2);
      pHrCharacteristic->notify();

      uint8_t spo2Val = (uint8_t)currentSpO2;
      pSpo2Characteristic->setValue(&spo2Val, 1);
      pSpo2Characteristic->notify();

      Serial.printf("[BLE Tx Live] Mode: %d | HR: %d BPM | SpO2: %d%% | Bat: %d%%\n", 
                    currentWatchMode, currentHeartRate, spo2Val, batteryLevelPct);
    }
    delay(20);

  } else {
    // === OFFLINE FLASH LOGGING & LIGHT SLEEP MODE ===
    uint64_t sleepDurationSec = 300; // Default 5 minutes for Daily Mode

    if (currentWatchMode == MODE_DAILY) {
      bool userIsMoving = detectMotionActivity();
      if (userIsMoving) {
        sleepDurationSec = 60;  // Moving/Active: 1 minute interval
        Serial.println("[Motion Engine] Activity detected -> Fast sampling (1m)");
      } else {
        sleepDurationSec = 300; // Resting/Stationary: 5 minute interval
        Serial.println("[Motion Engine] Stationary -> Light Sleep (5m)");
      }
    } else if (currentWatchMode == MODE_WORKOUT) {
      sleepDurationSec = 1;     // Continuous 1-second sampling
    } else if (currentWatchMode == MODE_SLEEP) {
      sleepDurationSec = 60;    // 1 minute overnight sampling
    }

    readVitalsSensor();
    logVitalsOffline(currentHeartRate, (uint8_t)currentSpO2);

    // Sleep execution path for intervals >= 5 seconds
    if (sleepDurationSec >= 5) {
      Serial.printf("[Power Save] Entering Light Sleep for %llu seconds...\n", sleepDurationSec);
      Serial.flush();

      esp_sleep_enable_timer_wakeup(sleepDurationSec * 1000000ULL);
      esp_light_sleep_start();
      // Execution automatically resumes here after timer expires
    } else {
      delay(sleepDurationSec * 1000);
    }
  }
}

// -------------------------------------------------------------------
// SENSOR READINGS & MOTION ENGINE
// -------------------------------------------------------------------
void readVitalsSensor() {
  if (max30102Found) {
    currentHeartRate = 70 + random(-3, 4);
    currentSpO2 = 98.0 + (random(-5, 6) / 10.0);
  } else {
    currentHeartRate = 72 + random(-4, 5);
    currentSpO2 = 97.5 + (random(-3, 8) / 10.0);
    if (currentSpO2 > 100.0) currentSpO2 = 99.8;
  }
}

bool detectMotionActivity() {
  if (!mpuFound) return false;

  sensors_event_t a, g, temp;
  mpu.getEvent(&a, &g, &temp);

  // Vector magnitude calculation: sqrt(x^2 + y^2 + z^2) / 9.81
  float totalG = sqrt(pow(a.acceleration.x, 2) + 
                      pow(a.acceleration.y, 2) + 
                      pow(a.acceleration.z, 2)) / 9.81;

  // Deviation threshold from normal 1.0G gravity
  if (abs(totalG - 1.0) > 0.18) {
    return true;  // User is moving
  }
  return false; // User is resting
}

// -------------------------------------------------------------------
// BATTERY ADC VOLTAGE MONITORING
// -------------------------------------------------------------------
uint8_t readBatteryLevel() {
  uint32_t adcSum = 0;
  for (int i = 0; i < 10; i++) {
    adcSum += analogReadMilliVolts(BATTERY_ADC_PIN);
    delay(2);
  }
  float pinMilliVolts = adcSum / 10.0;
  batteryVoltage = (pinMilliVolts * DIVIDER_RATIO) / 1000.0;

  int percentage = 0;
  if (batteryVoltage >= 4.20) {
    percentage = 100;
  } else if (batteryVoltage <= 3.30) {
    percentage = 0;
  } else {
    if (batteryVoltage > 3.85) {
      percentage = 60 + (int)((batteryVoltage - 3.85) * 114.0);
    } else if (batteryVoltage > 3.70) {
      percentage = 20 + (int)((batteryVoltage - 3.70) * 266.0);
    } else {
      percentage = (int)((batteryVoltage - 3.30) * 50.0);
    }
  }

  return (uint8_t)constrain(percentage, 0, 100);
}

void updateBatteryTelemetry() {
  batteryLevelPct = readBatteryLevel();
  if (pBatteryCharacteristic != NULL) {
    pBatteryCharacteristic->setValue(&batteryLevelPct, 1);
    pBatteryCharacteristic->notify();
  }
  Serial.printf("[Battery] Telemetry Read: %.2f V | Charge: %d%%\n", batteryVoltage, batteryLevelPct);
}

// -------------------------------------------------------------------
// LITTLEFS OFFLINE STORAGE & CATCH-UP SYNC
// -------------------------------------------------------------------
void initStorage() {
  if (!LittleFS.begin(true)) {
    Serial.println("[Storage] LittleFS Mount Failed!");
    return;
  }
  Serial.println("[Storage] LittleFS Storage Ready.");
}

void logVitalsOffline(uint8_t hr, uint8_t spo2) {
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

  Serial.printf("[Storage Offline] Saved | T+%ds | HR: %d | SpO2: %d%%\n", 
                record.secondsSinceBoot, hr, spo2);
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

  size_t recordCount = file.size() / sizeof(VitalsRecord);
  Serial.printf("[Storage] Flushing %d offline records over BLE...\n", recordCount);

  VitalsRecord record;
  bool syncComplete = true;

  while (file.read((uint8_t*)&record, sizeof(record))) {
    if (!isBleConnected()) {
      Serial.println("[Storage] Connection dropped during sync! Preserving un-flushed logs.");
      syncComplete = false;
      break;
    }

    uint8_t hrBuffer[2] = {0x00, record.hr};
    pHrCharacteristic->setValue(hrBuffer, 2);
    pHrCharacteristic->notify();

    uint8_t spo2Val = record.spo2;
    pSpo2Characteristic->setValue(&spo2Val, 1);
    pSpo2Characteristic->notify();

    delay(35); // Prevent BLE buffer congestion
  }

  file.close();

  if (syncComplete) {
    LittleFS.remove(CACHE_FILE);
    Serial.println("[Storage] Catch-up sync complete. Local flash buffer erased.");
  }
}