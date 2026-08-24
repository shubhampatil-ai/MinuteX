/*
  Task_Monitor — health monitoring: battery percent (owns BatteryDriver
  exclusively, matching the original's checkBatteryAndSleepIfLow() call
  site being the only ADC reader), free-heap/PSRAM logging, and a
  per-task heartbeat watchdog (new capability — the original had no task
  liveness check at all beyond the temporary loopHeartbeat() debug print).

  This task ties into the ESP-IDF Task Watchdog Timer (esp_task_wdt) so a
  genuinely wedged task (not just a slow one) triggers a real reset
  instead of silently hanging forever — the "graceful restart" /
  "automatic recovery" requirement from the architecture spec.
*/
#pragma once

#include <Arduino.h>
#include "Core_EventBus.h"
#include "Core_StateManager.h"
#include "Core_TaskManager.h"
#include "Driver_Battery.h"

struct MonitorTaskDeps {
  EventBus* bus;
  StateManager* stateMgr;
  TaskManager* taskMgr;
  BatteryDriver* battery;
};

void startMonitorTask(MonitorTaskDeps deps);
