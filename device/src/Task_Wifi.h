/*
  Task_Wifi — exclusive owner of the WiFi radio (reconnect, provisioning,
  status, RSSI). Mirrors the original wifiKeepAlive()/tryNextWifiNetwork()/
  startWiFiBackground() logic exactly: same per-attempt timeout (12s), same
  cooldown between round-robin attempts (20s), same "don't touch WiFi while
  RECORDING" guard, same "drain pending uploads on connect edge" behavior.

  Pinned to core 0 alongside UploadTask, matching the original's shared
  rationale that radio-heavy work should stay off the BLE-serving core.
*/
#pragma once

#include <Arduino.h>
#include "Core_EventBus.h"
#include "Core_StateManager.h"
#include "Core_TaskManager.h"
#include "Service_WifiProvisioning.h"

struct WifiTaskDeps {
  EventBus* bus;
  StateManager* stateMgr;
  TaskManager* taskMgr;
};

void startWifiTask(WifiTaskDeps deps);

// Kicks the first background connection attempt. Called from main after
// BLE init, matching the original boot order: initWiFiMode() -> initBLE()
// -> startWiFiBackground().
void kickWifiBackgroundConnect();

// Stages WiFi credentials into a mutex-protected slot owned by WifiTask and
// publishes CMD_SET_WIFI. Called by BleProtocol (SET_WIFI:ssid|pass) and
// SerialConsole (wifi <ssid>|<pass>) instead of packing credentials into
// the Event itself — see the note on struct Event in Types.h for why.
void requestSetWifi(EventBus* bus, const String& ssid, const String& pass);

// Returns the shared WifiProvisioning instance so BleProtocol/serial
// console can call addNetwork()/clearNetworks() indirectly via events
// only — kept here in case a future module needs read-only info (e.g. an
// OTA task checking connectivity before starting a firmware download).
WifiProvisioning& getWifiProvisioning();
