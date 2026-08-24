#include "Service_BleProtocol.h"
#include "Config.h"
#include "Task_Wifi.h"   // requestSetWifi()
#include <esp_mac.h>
#include <WiFi.h>

namespace {
BleProtocol* g_instance = nullptr;   // BLE callback classes need to reach the owning instance

class ServerCallbacks : public BLEServerCallbacks {
  void onConnect(BLEServer* s) override {
    if (g_instance) g_instance->onClientConnected();
  }
  void onDisconnect(BLEServer* s) override {
    if (g_instance) g_instance->onClientDisconnected();
    s->getAdvertising()->start();  // stay discoverable
  }
};

class ControlCallbacks : public BLECharacteristicCallbacks {
  void onWrite(BLECharacteristic* c) override {
    String v = String(c->getValue().c_str());
    if (v.length() > 0 && g_instance) g_instance->onControlWrite(v);
  }
};
}  // namespace

void BleProtocol::begin(EventBus* bus, StateManager* stateMgr) {
  bus_ = bus;
  stateMgr_ = stateMgr;
  g_instance = this;

  // Read the factory base MAC directly (esp_read_mac) rather than
  // WiFi.macAddress() — the latter returns all-zeros ("AI-Recorder-0000")
  // if WiFi hasn't fully initialized yet, which it hasn't at this point in
  // setup(). esp_read_mac works regardless of WiFi/BLE state.
  uint8_t mac[6] = {0};
  esp_read_mac(mac, ESP_MAC_WIFI_STA);
  char suffix[5];
  snprintf(suffix, sizeof(suffix), "%02X%02X", mac[4], mac[5]);
  deviceName_ = "AI-Recorder-" + String(suffix);

  BLEDevice::init(deviceName_.c_str());
  // Allow a larger ATT MTU so the status JSON (~150 bytes) fits in one
  // notification. Without this the default MTU (23) truncates it and the
  // app sees invalid JSON.
  BLEDevice::setMTU(BLE_ATT_MTU);
  server_ = BLEDevice::createServer();
  server_->setCallbacks(new ServerCallbacks());

  BLEService* svc = server_->createService(BLE_SERVICE_UUID);

  BLECharacteristic* controlChar = svc->createCharacteristic(
      BLE_CONTROL_UUID, BLECharacteristic::PROPERTY_WRITE);
  controlChar->setCallbacks(new ControlCallbacks());

  statusChar_ = svc->createCharacteristic(
      BLE_STATUS_UUID,
      BLECharacteristic::PROPERTY_READ | BLECharacteristic::PROPERTY_NOTIFY);
  statusChar_->addDescriptor(new BLE2902());   // enables notifications

  svc->start();
  BLEAdvertising* adv = BLEDevice::getAdvertising();
  adv->addServiceUUID(BLE_SERVICE_UUID);      // app filters on this UUID
  adv->setScanResponse(true);
  adv->start();
  Serial.printf("[BLE] Advertising as %s\n", deviceName_.c_str());
}

void BleProtocol::onClientConnected() {
  clientConnected_ = true;
  Serial.println("[BLE] App connected");
  pushStatus();   // greet the app with current state
}

void BleProtocol::onClientDisconnected() {
  clientConnected_ = false;
  Serial.println("[BLE] App disconnected — advertising again");
}

void BleProtocol::onControlWrite(const String& cmdIn) {
  String cmd = cmdIn;
  cmd.trim();
  Serial.printf("[BLE] Command: %s\n", cmd.c_str());

  if (cmd == "START") {
    bus_->publish(Event(EventType::CMD_START));
  } else if (cmd == "STOP") {
    bus_->publish(Event(EventType::CMD_STOP));
  } else if (cmd == "GET_STATUS" || cmd == "GET_INFO") {
    bus_->publish(Event(EventType::CMD_GET_STATUS));
  } else if (cmd.startsWith("SET_WIFI:")) {
    // Format: SET_WIFI:MyNetwork|myPassword
    String rest = cmd.substring(9);
    int sep = rest.indexOf('|');
    if (sep > 0) {
      String ssid = rest.substring(0, sep);
      String pass = rest.substring(sep + 1);
      requestSetWifi(bus_, ssid, pass);
    } else {
      Serial.println("[BLE] Bad SET_WIFI format (need SET_WIFI:ssid|pass)");
    }
  } else if (cmd == "CLEAR_WIFI") {
    bus_->publish(Event(EventType::CMD_CLEAR_WIFI));
  } else {
    Serial.printf("[BLE] Unknown BLE command: %s\n", cmd.c_str());
  }
}

void BleProtocol::pushStatus() {
  if (!statusChar_) return;
  StateSnapshot s = stateMgr_->getSnapshot();

  String json = "{";
  json += "\"rec\":"  + String(s.appState == AppState::RECORDING ? 1 : 0);
  json += ",\"state\":\"";
  json += AppStateToJsonString(s.appState);
  json += "\",\"wifi\":" + String(s.wifiConnected ? 1 : 0);
  json += ",\"wifi_state\":\"" + String(WifiConnStateToString(s.wifiConnState)) + "\"";
  json += ",\"wifi_ssid\":\""  + s.wifiSsid + "\"";
  json += ",\"ip\":\""   + (s.wifiConnected ? s.wifiIp : String("")) + "\"";
  json += ",\"batt\":"   + String(s.batteryPct);
  json += ",\"pend\":"   + String(s.pendingCount);
  json += ",\"fw\":\""   + String(FW_VERSION) + "\"";
  json += "}";
  statusChar_->setValue(json.c_str());
  if (clientConnected_) statusChar_->notify();
}

void BleProtocol::ensureAdvertising() {
  // Self-healing BLE advertising. If no app is connected, the device MUST
  // stay discoverable regardless of what else it's doing (recording, WiFi,
  // uploading pending files, etc.) — WiFi/BLE radio coexistence can
  // occasionally starve or silently drop advertising during heavy WiFi TX
  // (e.g. a large upload). Only ACTS if advertising has actually stopped.
  if (clientConnected_) return;
  BLEAdvertising* adv = BLEDevice::getAdvertising();
  if (!adv || adv->isAdvertising()) return;
  adv->start();
  Serial.println("[BLE] Advertising had stopped — restarted (watchdog)");
}
