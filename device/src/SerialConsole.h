/*
  SerialConsole — serial command line handling, unchanged from the
  original: help, show, start, stop, shift, fmt, ch, rate, raw, upload,
  save, defaults, wifi <ssid>|<pass>, wificlear. Dispatches START/STOP/
  SET_WIFI/CLEAR_WIFI onto the same EventBus events BLE commands use, so
  both control paths converge on identical task-layer handling (matches
  the original's "physical buttons + BLE + serial all drive the same
  state" design intent).
*/
#pragma once

#include <Arduino.h>
#include "Core_EventBus.h"
#include "Core_StateManager.h"
#include "Core_ConfigManager.h"

struct SerialConsoleDeps {
  EventBus* bus;
  StateManager* stateMgr;
  ConfigManager* configMgr;
};

void serialConsoleBegin(const SerialConsoleDeps& deps);
void serialConsolePoll();   // call every loop() iteration; non-blocking
