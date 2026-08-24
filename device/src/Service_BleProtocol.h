/*
  Service_BleProtocol — BLE GATT server setup + status JSON builder. Used
  exclusively by BleTask. Mirrors the original initBLE()/sendBleStatus()/
  ServerCallbacks/ControlCallbacks exactly: same device name derivation
  (esp_read_mac), same service/characteristic UUIDs, same MTU, same JSON
  field set/order/types.

  Command parsing (ControlCallbacks::onWrite) publishes an EventBus event
  instead of setting volatile flags directly or calling business logic
  in-callback — BLE callbacks run on the NimBLE/Bluedroid host task, so
  keeping them to "parse + publish" only avoids doing recording/WiFi work
  on that task, per the "no recording logic inside BLE callbacks" rule.
*/
#pragma once

#include <Arduino.h>
#include <BLEDevice.h>
#include <BLEServer.h>
#include <BLEUtils.h>
#include <BLE2902.h>
#include "Types.h"
#include "Core_EventBus.h"
#include "Core_StateManager.h"

class BleProtocol {
public:
  void begin(EventBus* bus, StateManager* stateMgr);

  // Builds the status JSON from the current StateSnapshot and pushes it to
  // the STATUS characteristic (setValue always; notify() only if a client
  // is connected) — exactly matching original sendBleStatus() semantics.
  void pushStatus();

  bool isClientConnected() const { return clientConnected_; }
  const String& deviceName() const { return deviceName_; }

  // Called by ServerCallbacks/ControlCallbacks (friends) — kept public so
  // the nested callback classes (defined in the .cpp) can reach back into
  // the owning BleProtocol instance via a stored pointer.
  void onClientConnected();
  void onClientDisconnected();
  void onControlWrite(const String& cmd);

  void ensureAdvertising();

private:
  EventBus* bus_ = nullptr;
  StateManager* stateMgr_ = nullptr;
  BLEServer* server_ = nullptr;
  BLECharacteristic* statusChar_ = nullptr;
  String deviceName_;
  volatile bool clientConnected_ = false;
};
