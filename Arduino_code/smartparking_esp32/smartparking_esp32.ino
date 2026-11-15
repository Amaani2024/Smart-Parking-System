/*
  Smart Parking - ESP32
  - 4 IR slot sensors            -> /sensors/IR1..IR4  ("detected" / "not detected")
  - DHT22 temperature + humidity -> /sensors/temperature, /sensors/humidity
  - Flame (fire) sensor          -> /sensors/fire ("safe" / "detected")
  - Water pump (via relay)       -> /sensors/pump ("ON" / "OFF"); fire turns it on LOCALLY, no internet needed
  - Entry + exit gates (servos)  -> commands from /gate/entry/command and /gate/exit/command
  - Each gate has an obstacle sensor: the gate never closes while a car is in the way.

  Libraries (Library Manager): "Firebase ESP Client" (Mobizt), "DHT sensor library" (Adafruit),
                               "ESP32Servo" (Kevin Harrington)
  Board: ESP32 Dev Module.   NOT compiled by the author - check pins and angles on your wiring.
*/

#include <WiFi.h>
#include <Firebase_ESP_Client.h>
#include <DHT.h>
#include <ESP32Servo.h>

#include "addons/TokenHelper.h"
#include "addons/RTDBHelper.h"

// ---------------- SETTINGS (change here) ----------------
#define WIFI_SSID     "ESP32TEST"
#define WIFI_PASSWORD "YOUR_WIFI_PASSWORD"
#define API_KEY       "AIzaSyBAU3NptLvX-_tgE7r0_JrZALQJGSuRrEc"
#define DATABASE_URL  "https://smartparkingsystem-ccb6e-default-rtdb.asia-southeast1.firebasedatabase.app/"

// Pins
#define DHTPIN        12
#define DHTTYPE       DHT22
const int IR_PINS[4]  = {5, 18, 19, 4};      // slot sensors, LOW = car present
#define FLAME_DO      27                      // flame sensor digital out, LOW = fire
#define PUMP_RELAY    25                      // relay -> water pump (use its own power supply)
#define PUMP_ACTIVE_HIGH true                 // set false if your relay board switches on LOW

#define ENTRY_SERVO   13
#define EXIT_SERVO    26
#define ENTRY_SENSOR  32                      // obstacle sensor at the entry gate, LOW = car in the way
#define EXIT_SENSOR   33                      // obstacle sensor at the exit gate

// Gate servo angles
#define ANGLE_CLOSED  0
#define ANGLE_OPEN    90

// Gate timing (ms)
#define CLEAR_MS          2000                // sensor must be clear this long before closing
#define NO_CAR_TIMEOUT    15000               // opened but no car ever seen -> close after this
#define MAX_OPEN_MS       40000               // hard limit while waiting for the car to pass

// Fire / pump timing (ms)
#define PUMP_MAX_RUN      30000               // run at most this long, then re-check
#define PUMP_AFTER_FIRE   5000                // keep running this long after fire clears
#define PUMP_COOLDOWN     10000

// ---------------- GLOBALS ----------------
FirebaseData fbdo;
FirebaseAuth auth;
FirebaseConfig config;
DHT dht(DHTPIN, DHTTYPE);

enum GateState { G_CLOSED, G_OPEN };

struct Gate {
  const char* name;       // "entry" / "exit"
  int servoPin;
  int sensorPin;
  Servo servo;
  GateState state;
  unsigned long openedAt;
  unsigned long clearSince;
  bool carSeen;
  bool closeRequested;
  bool statusDirty;       // needs upload
  bool resetCmdDirty;     // command must be set back to "none"
};

Gate gates[2] = {
  {"entry", ENTRY_SERVO, ENTRY_SENSOR},
  {"exit",  EXIT_SERVO,  EXIT_SENSOR}
};

String irLast[4] = {"", "", "", ""};
bool fireNow = false, fireLastUploaded = false, fireUploadedOnce = false;
String pumpMode = "auto";          // auto | on | off  (from /pump/command)
bool pumpOn = false, pumpLastUploaded = false, pumpUploadedOnce = false;
unsigned long pumpStartedAt = 0, pumpStoppedAt = 0, lastFireSeen = 0;

unsigned long tPoll = 0, tSensors = 0, tHeartbeat = 0;

// ---------------- HELPERS ----------------
String irStatus(int v) { return (v == LOW) ? "detected" : "not detected"; }

void setPump(bool on) {
  pumpOn = on;
  digitalWrite(PUMP_RELAY, (on == PUMP_ACTIVE_HIGH) ? HIGH : LOW);
  if (on) pumpStartedAt = millis(); else pumpStoppedAt = millis();
}

void openGate(Gate &g) {
  g.servo.write(ANGLE_OPEN);
  g.state = G_OPEN;
  g.openedAt = millis();
  g.clearSince = 0;
  g.carSeen = false;
  g.closeRequested = false;
  g.statusDirty = true;
  g.resetCmdDirty = true;
  Serial.printf("[%s] OPEN\n", g.name);
}

void closeGate(Gate &g) {
  g.servo.write(ANGLE_CLOSED);
  g.state = G_CLOSED;
  g.statusDirty = true;
  Serial.printf("[%s] CLOSED\n", g.name);
}

// Runs every loop: keeps gates safe without waiting for the internet.
void updateGate(Gate &g) {
  if (g.state != G_OPEN) return;
  unsigned long now = millis();
  bool blocked = (digitalRead(g.sensorPin) == LOW);

  if (blocked) {
    g.carSeen = true;
    g.clearSince = 0;
  } else if (g.clearSince == 0) {
    g.clearSince = now;
  }

  bool carPassed = g.carSeen && !blocked && g.clearSince && (now - g.clearSince >= CLEAR_MS);
  bool noCar     = !g.carSeen && (now - g.openedAt >= NO_CAR_TIMEOUT);
  bool manual    = g.closeRequested && !blocked && g.clearSince && (now - g.clearSince >= 500);
  bool timedOut  = (now - g.openedAt >= MAX_OPEN_MS) && !blocked;

  if (carPassed || noCar || manual || timedOut) closeGate(g);
}

void updateFirePump() {
  fireNow = (digitalRead(FLAME_DO) == LOW);
  unsigned long now = millis();
  if (fireNow) lastFireSeen = now;

  // Safety: if fire is detected, a manual OFF is ignored and the pump runs in auto mode.
  String mode = (pumpMode == "off" && fireNow) ? String("auto") : pumpMode;

  bool want;
  if (mode == "on") {
    want = true;
  } else if (mode == "off") {
    want = false;
  } else {
    bool recentFire = fireNow || (lastFireSeen && now - lastFireSeen < PUMP_AFTER_FIRE);
    want = recentFire;
    if (pumpOn && now - pumpStartedAt > PUMP_MAX_RUN) want = false;               // max run
    if (!pumpOn && pumpStoppedAt && now - pumpStoppedAt < PUMP_COOLDOWN) want = false;
  }
  if (want != pumpOn) setPump(want);
}

// ---------------- FIREBASE ----------------
void pollCommands() {
  if (!Firebase.ready()) return;

  if (Firebase.RTDB.getJSON(&fbdo, "/gate")) {
    FirebaseJson &json = fbdo.jsonObject();
    FirebaseJsonData d;
    for (int i = 0; i < 2; i++) {
      String path = String(gates[i].name) + "/command";
      if (json.get(d, path) && d.success) {
        String cmd = d.stringValue;
        if (cmd == "open") openGate(gates[i]);
        else if (cmd == "close" && gates[i].state == G_OPEN) {
          gates[i].closeRequested = true;
          gates[i].resetCmdDirty = true;
        } else if (cmd == "close") {
          gates[i].resetCmdDirty = true;
        }
      }
    }
  }

  if (Firebase.RTDB.getString(&fbdo, "/pump/command")) {
    String m = fbdo.stringData();
    if (m == "on" || m == "off" || m == "auto") pumpMode = m;
  }
}

void uploadGates() {
  for (int i = 0; i < 2; i++) {
    if (gates[i].statusDirty) {
      String base = String("/gate/") + gates[i].name;
      Firebase.RTDB.setString(&fbdo, base + "/status", gates[i].state == G_OPEN ? "open" : "closed");
      gates[i].statusDirty = false;
    }
    if (gates[i].resetCmdDirty) {
      String base = String("/gate/") + gates[i].name;
      Firebase.RTDB.setString(&fbdo, base + "/command", "none");
      gates[i].resetCmdDirty = false;
    }
  }
}

void uploadSensors(bool all) {
  if (!Firebase.ready()) return;

  // IR slots: only when changed
  for (int i = 0; i < 4; i++) {
    String s = irStatus(digitalRead(IR_PINS[i]));
    if (s != irLast[i]) {
      if (Firebase.RTDB.setString(&fbdo, "/sensors/IR" + String(i + 1), s)) irLast[i] = s;
    }
  }

  // fire + pump: when changed
  if (!fireUploadedOnce || fireNow != fireLastUploaded) {
    if (Firebase.RTDB.setString(&fbdo, "/sensors/fire", fireNow ? "detected" : "safe")) {
      fireLastUploaded = fireNow;
      fireUploadedOnce = true;
    }
  }
  if (!pumpUploadedOnce || pumpOn != pumpLastUploaded) {
    if (Firebase.RTDB.setString(&fbdo, "/sensors/pump", pumpOn ? "ON" : "OFF")) {
      pumpLastUploaded = pumpOn;
      pumpUploadedOnce = true;
    }
  }

  // slow values: temperature / humidity / heartbeat (a DHT error no longer blocks anything else)
  if (all) {
    float h = dht.readHumidity();
    float t = dht.readTemperature();
    if (!isnan(h) && !isnan(t)) {
      Firebase.RTDB.setFloat(&fbdo, "/sensors/temperature", t);
      Firebase.RTDB.setFloat(&fbdo, "/sensors/humidity", h);
    } else {
      Serial.println("DHT read failed");
    }
    Firebase.RTDB.setTimestamp(&fbdo, "/sensors/lastSeen");
  }
}

// ---------------- SETUP / LOOP ----------------
void setup() {
  Serial.begin(115200);
  dht.begin();

  for (int i = 0; i < 4; i++) pinMode(IR_PINS[i], INPUT);
  pinMode(FLAME_DO, INPUT);
  pinMode(ENTRY_SENSOR, INPUT);
  pinMode(EXIT_SENSOR, INPUT);
  pinMode(PUMP_RELAY, OUTPUT);
  setPump(false);

  for (int i = 0; i < 2; i++) {
    gates[i].servo.setPeriodHertz(50);
    gates[i].servo.attach(gates[i].servoPin, 500, 2400);
    gates[i].servo.write(ANGLE_CLOSED);
    gates[i].state = G_CLOSED;
    gates[i].statusDirty = true;
    gates[i].resetCmdDirty = true;
  }

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Connecting to WiFi");
  while (WiFi.status() != WL_CONNECTED) { delay(500); Serial.print("."); }
  Serial.println("\nWiFi connected, IP: " + WiFi.localIP().toString());

  config.api_key = API_KEY;
  config.database_url = DATABASE_URL;
  config.token_status_callback = tokenStatusCallback;
  if (Firebase.signUp(&config, &auth, "", "")) Serial.println("Firebase sign-up OK");
  else Serial.printf("Sign-up failed: %s\n", config.signer.signupError.message.c_str());
  Firebase.begin(&config, &auth);
  Firebase.reconnectWiFi(true);
}

void loop() {
  unsigned long now = millis();

  // time-critical, every pass
  updateFirePump();
  updateGate(gates[0]);
  updateGate(gates[1]);

  // network work, spaced out
  if (now - tPoll >= 800) {
    tPoll = now;
    pollCommands();
  }
  if (now - tSensors >= 400) {
    tSensors = now;
    bool slow = (now - tHeartbeat >= 5000);
    if (slow) tHeartbeat = now;
    uploadSensors(slow);
    uploadGates();
  }
  delay(20);
}
