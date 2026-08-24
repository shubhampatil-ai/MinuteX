/*
  Task_Ble — exclusive owner of the BLE server, notifications, advertising,
  and reconnect watchdog. No recording/WiFi/upload logic runs here — BLE
  command parsing (Service_BleProtocol::onControlWrite) only ever publishes
  EventBus events; this task's own loop just pushes status on STATE_CHANGED/
  WIFI_STATE_CHANGED/BATTERY_UPDATED and re-arms advertising, matching the
  original's ensureBleAdvertising() call from every loop() iteration.
*/
#pragma once

#include <Arduino.h>
#include "Core_EventBus.h"
#include "Core_StateManager.h"
#include "Core_TaskManager.h"
#include "Service_BleProtocol.h"

struct BleTaskDeps {
  EventBus* bus;
  StateManager* stateMgr;
  TaskManager* taskMgr;
  BleProtocol* ble;
};

void startBleTask(BleTaskDeps deps);
