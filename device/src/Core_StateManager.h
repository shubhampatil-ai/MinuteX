/*
  Core_StateManager — the ONLY module allowed to mutate AppState. Every
  other module requests a transition via requestTransition() or reads a
  consistent snapshot via getSnapshot(). Replaces the original firmware's
  `volatile State appState` global.

  Also tracks the WiFi status fields that used to be loose globals
  (wifiConnState/wifiLastSsid/ip/rssi) and the battery percent + pending
  count + error flag needed to build the BLE status JSON — centralizing
  everything the original sendBleStatus() read from scattered globals.

  Transitions publish EventType::STATE_CHANGED on the EventBus so BleTask
  (status notify) and LedTask (pixel update) react without polling.
*/
#pragma once

#include <Arduino.h>
#include "Types.h"
#include "Core_EventBus.h"

struct StateSnapshot {
  AppState      appState      = AppState::BOOT;
  bool          wifiConnected = false;
  WifiConnState wifiConnState = WifiConnState::IDLE;
  String        wifiSsid      = "";
  String        wifiIp        = "";
  int           batteryPct    = 100;
  int           pendingCount  = 0;
  bool          errorFlag     = false;
};

class StateManager {
public:
  void begin(EventBus* bus);

  // ---- Mutators (StateManager is the only writer of the underlying state) ----
  void setAppState(AppState s);
  void setWifiStatus(bool connected, WifiConnState connState, const String& ssid, const String& ip);
  void setBatteryPercent(int pct);
  void setPendingCount(int count);
  void setError(bool active);

  // ---- Readers (safe from any task; copies out under mutex) ----
  StateSnapshot getSnapshot() const;
  AppState getAppState() const;

private:
  void publishStateChanged();

  mutable SemaphoreHandle_t mutex_ = nullptr;
  StateSnapshot state_;
  EventBus* bus_ = nullptr;
};
