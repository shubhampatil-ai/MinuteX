/*
  Task_Led — exclusive owner of the RGB LED. No other module calls
  RGBDriver directly. Mirrors the original updateLed() priority resolver
  exactly:
    recording/saving > error > uploading (blink) > low-batt >
    wifi-connecting > wifi-connected > idle(no wifi)
  and the original uploadBlinkTick()/Ticker-driven upload blink, now done
  via a simple time-check inside this task's own loop instead of a Ticker
  ISR callback (removes the ISR-context LED write entirely — a strictly
  safer primitive than the original's Ticker, with identical visible
  behavior).
*/
#pragma once

#include <Arduino.h>
#include "Core_EventBus.h"
#include "Core_StateManager.h"
#include "Core_TaskManager.h"
#include "Driver_RGB.h"

struct LedTaskDeps {
  EventBus* bus;
  StateManager* stateMgr;
  TaskManager* taskMgr;
  RGBDriver* rgb;
};

void startLedTask(LedTaskDeps deps);

// Called by UploadTask to start/stop the green upload blink, exactly
// matching the original's uploadBlinker.attach_ms(...)/detach() calls
// bracketing the upload work.
void ledSetUploadBlinking(bool on);
