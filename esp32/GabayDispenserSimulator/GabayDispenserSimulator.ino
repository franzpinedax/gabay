#include <WiFi.h>
#include <HTTPClient.h>
#include <PubSubClient.h>
#include <ArduinoJson.h>

// Update these values for your network and Gabay installation.
const char* WIFI_SSID = "YOUR_WIFI_NAME";
const char* WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";
const char* MQTT_HOST = "192.168.100.86";
const uint16_t MQTT_PORT = 1883;
const char* GABAY_HOST = "192.168.100.86";
const uint16_t GABAY_PORT = 8000;
const char* PATIENT_ID = "franzpineda8249";

// Optional simulation hardware. The dispense action is also printed to Serial.
const uint8_t ALARM_PIN = 25;
const uint8_t STATUS_LED_PIN = 2;

WiFiClient wifiClient;
PubSubClient mqttClient(wifiClient);
String commandTopic;

void connectWifi() {
  Serial.print("Connecting to Wi-Fi");
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println();
  Serial.print("Wi-Fi connected: ");
  Serial.println(WiFi.localIP());
}

void confirmDispensed(long eventId) {
  HTTPClient http;
  String url = String("http://") + GABAY_HOST + ":" + GABAY_PORT +
               "/dispense-events/" + eventId + "/dispensed";

  http.begin(url);
  http.addHeader("Content-Type", "application/json");
  int status = http.POST("{}");
  Serial.print("Gabay dispense confirmation HTTP status: ");
  Serial.println(status);
  if (status >= 200 && status < 300) {
    Serial.println("Gabay recorded the dispense successfully.");
  } else {
    Serial.println("Gabay did not confirm the dispense.");
  }
  http.end();
}

void simulateDispense(long eventId, const char* medName, const char* dosage) {
  Serial.println();
  Serial.println("=== DISPENSER COMMAND RECEIVED ===");
  Serial.print("Event: ");
  Serial.println(eventId);
  Serial.print("Medication: ");
  Serial.println(medName);
  Serial.print("Dosage: ");
  Serial.println(dosage);
  Serial.println("Alarm: ON");

  digitalWrite(STATUS_LED_PIN, HIGH);
  digitalWrite(ALARM_PIN, HIGH);
  delay(3000);
  digitalWrite(ALARM_PIN, LOW);

  Serial.println("Simulated servo/carousel dispense: COMPLETE");
  digitalWrite(STATUS_LED_PIN, LOW);
  confirmDispensed(eventId);
}

void mqttMessageReceived(char* topic, byte* payload, unsigned int length) {
  StaticJsonDocument<512> command;
  DeserializationError error = deserializeJson(command, payload, length);
  if (error) {
    Serial.print("Invalid dispenser command JSON: ");
    Serial.println(error.c_str());
    return;
  }

  const char* action = command["command"] | "";
  if (strcmp(action, "dispense") != 0) {
    Serial.print("Ignoring unsupported command: ");
    Serial.println(action);
    return;
  }

  long eventId = command["event_id"] | 0;
  const char* medName = command["med_name"] | "Medication";
  const char* dosage = command["dosage"] | "";
  if (eventId <= 0) {
    Serial.println("Ignoring dispense command without a valid event_id.");
    return;
  }

  simulateDispense(eventId, medName, dosage);
}

void reconnectMqtt() {
  while (!mqttClient.connected()) {
    String clientId = String("GabayDispenser-") + String((uint32_t)ESP.getEfuseMac(), HEX);
    Serial.print("Connecting to MQTT...");
    if (mqttClient.connect(clientId.c_str())) {
      Serial.println("connected");
      mqttClient.subscribe(commandTopic.c_str(), 1);
      Serial.print("Subscribed to: ");
      Serial.println(commandTopic);
    } else {
      Serial.print("MQTT failed, state=");
      Serial.println(mqttClient.state());
      delay(3000);
    }
  }
}

void setup() {
  Serial.begin(115200);
  pinMode(ALARM_PIN, OUTPUT);
  pinMode(STATUS_LED_PIN, OUTPUT);
  digitalWrite(ALARM_PIN, LOW);
  digitalWrite(STATUS_LED_PIN, LOW);

  commandTopic = String("gabay/patients/") + PATIENT_ID + "/dispenser/command";
  connectWifi();
  mqttClient.setServer(MQTT_HOST, MQTT_PORT);
  mqttClient.setCallback(mqttMessageReceived);
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    connectWifi();
  }
  if (!mqttClient.connected()) {
    reconnectMqtt();
  }
  mqttClient.loop();
}
