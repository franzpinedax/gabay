/*
 * ESP32-C3 Smart Watch Firmware - Optimized BLE Advertising & Modem Sleep
 */

#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>
#include <Wire.h>
#include <LittleFS.h>

#define I2C_SDA 8
#define I2C_SCL 9
#define CACHE_FILE "/vitals_cache.bin"

struct VitalsRecord {
  uint32_t secondsSinceBoot;
  uint8_t  hr;
  uint8_t  spo2;
};

enum WatchMode {
  MODE_DAILY = 1,
  MODE_WORKOUT = 2,
  MODE_SLEEP = 3
};

WatchMode currentWatchMode = MODE_DAILY;
unsigned long logIntervalMs = 5000;
unsigned long lastLogTime = 0;

#define DEVICE_NAME "Gabay-SmartWatch"
#define HR_SERVICE_UUID          "0000180d-0000-1000-8000-00805f9b34fb"
#define HR_MEASUREMENT_UUID      "00002a37-0000-1000-8000-00805f9b34fb"
#define SPO2_SERVICE_UUID        "00001822-0000-1000-8000-00805f9b34fb"
#define SPO2_MEASUREMENT_UUID    "00002a5e-0000-1000-8000-00805f9b34fb"
#define CONTROL_SERVICE_UUID     "00001809-0000-1000-8000-00805f9b34fb"
#define MODE_CHARACTERISTIC_UUID "00002a1c-0000-1000-8000-00805f9b34fb"

BLEServer* pServer = NULL;
BLECharacteristic* pHrCharacteristic = NULL;
BLECharacteristic* pSpo2Characteristic = NULL;
BLECharacteristic* pModeCharacteristic = NULL;
BLEAdvertising* pAdvertising = NULL;

volatile bool deviceConnected = false;
bool oldDeviceConnected = false;

uint8_t currentHeartRate = 75;
float currentSpO2 = 98.2;
bool sensorFound = false;

void readVitalsSensor();
void initStorage();
void logVitalsOffline(uint8_t hr, uint8_t spo2);
void flushOfflineBuffer();

// Helper: Checks hardware BLE stack connection status
bool isBleConnected() {
  return (deviceConnected && pServer != NULL && pServer->getConnectedCount() > 0);
}

class WatchServerCallbacks: public BLEServerCallbacks {
    void onConnect(BLEServer* pServer) {
      deviceConnected = true;
      Serial.println("[BLE Watch] Central device connected.");
      
      // OPTIMIZATION 3: Request connection parameters for ESP32 Modem Sleep / Slave Latency
      // minInterval = 80 (100ms), maxInterval = 160 (200ms), latency = 4 (skips intervals), timeout = 600 (6s)
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
          Serial.println("[BLE Watch] Mode: DAILY");
        } else if (modeVal == 2) {
          currentWatchMode = MODE_WORKOUT;
          logIntervalMs = 1000;
          Serial.println("[BLE Watch] Mode: WORKOUT (1s)");
        } else if (modeVal == 3) {
          currentWatchMode = MODE_SLEEP;
          logIntervalMs = 60000;
          Serial.println("[BLE Watch] Mode: SLEEP (1m)");
        }
      }
    }
};

void setup() {
  Serial.begin(115200);
  delay(1000);
  Serial.println("[ESP32-C3 Watch] Initializing Firmware with Low-Power Optimizations...");

  initStorage();

  Wire.begin(I2C_SDA, I2C_SCL);
  delay(100);
  Wire.beginTransmission(0x57);
  if (Wire.endTransmission() == 0) {
    sensorFound = true;
    Serial.println("[ESP32-C3 Watch] MAX30102 PPG Sensor detected!");
  } else {
    sensorFound = false;
    Serial.println("[ESP32-C3 Watch] No sensor found. Using simulator.");
  }

  BLEDevice::init(DEVICE_NAME);
  pServer = BLEDevice::createServer();
  pServer->setCallbacks(new WatchServerCallbacks());

  // Services & Characteristics setup
  BLEService *pHrService = pServer->createService(HR_SERVICE_UUID);
  pHrCharacteristic = pHrService->createCharacteristic(HR_MEASUREMENT_UUID, BLECharacteristic::PROPERTY_NOTIFY);
  pHrCharacteristic->addDescriptor(new BLE2902());

  BLEService *pSpo2Service = pServer->createService(SPO2_SERVICE_UUID);
  pSpo2Characteristic = pSpo2Service->createCharacteristic(SPO2_MEASUREMENT_UUID, BLECharacteristic::PROPERTY_NOTIFY);
  pSpo2Characteristic->addDescriptor(new BLE2902());

  BLEService *pControlService = pServer->createService(CONTROL_SERVICE_UUID);
  pModeCharacteristic = pControlService->createCharacteristic(MODE_CHARACTERISTIC_UUID, BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_WRITE);
  pModeCharacteristic->setCallbacks(new WatchModeCallbacks());

  uint8_t initialMode = (uint8_t)currentWatchMode;
  pModeCharacteristic->setValue(&initialMode, 1);

  pHrService->start();
  pSpo2Service->start();
  pControlService->start();

  // Configure Advertising
  pAdvertising = BLEDevice::getAdvertising();
  pAdvertising->addServiceUUID(HR_SERVICE_UUID);
  pAdvertising->addServiceUUID(SPO2_SERVICE_UUID);
  pAdvertising->addServiceUUID(CONTROL_SERVICE_UUID);
  pAdvertising->setScanResponse(true);
  
  // OPTIMIZATION 1: Set slower background advertising interval (1280ms / 0x0800) to save standby power
  pAdvertising->setMinInterval(0x0800); // 1280ms min
  pAdvertising->setMaxInterval(0x1000); // 2560ms max

  BLEDevice::startAdvertising();
  Serial.println("[ESP32-C3 Watch] Optimized BLE Advertising active.");
}

void loop() {
  readVitalsSensor();

  bool connected = isBleConnected();

  // 1. RECONNECTION EVENT: Trigger offline catch-up sync once reconnected
  if (connected && !oldDeviceConnected) {
    oldDeviceConnected = true;
    Serial.println("[BLE Guard] Active link established. Flushing offline flash cache...");
    flushOfflineBuffer();
  }

  // 2. DISCONNECTION EVENT: Handle state cleanup & restart advertising with power-saving intervals
  if (!connected && oldDeviceConnected) {
    oldDeviceConnected = false;
    Serial.println("[BLE Guard] Disconnected. Restarting low-power BLE Advertising...");
    delay(100);
    pAdvertising->setMinInterval(0x0800); // Slow down ad interval when disconnected
    pAdvertising->setMaxInterval(0x1000);
    pServer->startAdvertising();
  }

  // 3. EXECUTION PATH ROUTING
  if (connected) {
    // === ONLINE SYNC MODE ===
    if (millis() - lastLogTime >= logIntervalMs) {
      lastLogTime = millis();

      uint8_t hrBuffer[2] = {0x00, currentHeartRate};
      pHrCharacteristic->setValue(hrBuffer, 2);
      pHrCharacteristic->notify();

      uint8_t spo2Val = (uint8_t)currentSpO2;
      pSpo2Characteristic->setValue(&spo2Val, 1);
      pSpo2Characteristic->notify();

      Serial.printf("[BLE Tx Online] HR: %d BPM | SpO2: %d %%\n", currentHeartRate, spo2Val);
    }
  } else {
    // === ASYNC / OFFLINE FLASH MODE ===
    unsigned long offlineInterval = (currentWatchMode == MODE_DAILY) ? 300000 : logIntervalMs;

    if (millis() - lastLogTime >= offlineInterval) {
      lastLogTime = millis();
      logVitalsOffline(currentHeartRate, (uint8_t)currentSpO2);
    }
  }

  delay(20);
}

void readVitalsSensor() {
  if (sensorFound) {
    currentHeartRate = 70 + random(-3, 4);
    currentSpO2 = 98.0 + (random(-5, 6) / 10.0);
  } else {
    currentHeartRate = 72 + random(-4, 5);
    currentSpO2 = 97.5 + (random(-3, 8) / 10.0);
    if (currentSpO2 > 100.0) currentSpO2 = 99.8;
  }
}

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

  Serial.printf("[Storage Async] Saved to Flash | T+%ds | HR: %d | SpO2: %d%%\n", record.secondsSinceBoot, hr, spo2);
}

void flushOfflineBuffer() {
  if (!LittleFS.exists(CACHE_FILE)) {
    Serial.println("[Storage] No offline records found.");
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
      Serial.println("[Storage] Disconnected during catch-up sync! Preserving remaining logs.");
      syncComplete = false;
      break;
    }

    uint8_t hrBuffer[2] = {0x00, record.hr};
    pHrCharacteristic->setValue(hrBuffer, 2);
    pHrCharacteristic->notify();

    uint8_t spo2Val = record.spo2;
    pSpo2Characteristic->setValue(&spo2Val, 1);
    pSpo2Characteristic->notify();

    delay(35);
  }

  file.close();

  if (syncComplete) {
    LittleFS.remove(CACHE_FILE);
    Serial.println("[Storage] Catch-up sync complete. Local flash buffer erased.");
  }
}