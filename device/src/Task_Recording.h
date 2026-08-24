/*
  Task_Recording — highest-priority task. Exclusive owner of I2S, WAV
  writing, and SD recording (Service_Recorder). Never blocks on BLE/WiFi/
  network. Responds to CMD_START/CMD_STOP via its EventBus subscriber
  queue instead of the original's volatile startFlag/stopFlag.

  Also handles the "physical button" edge, since debounced button ISRs
  publish the same CMD_START/CMD_STOP events onto the bus (see
  ButtonIsr.h) — RecordingTask doesn't care whether a command came from
  BLE, serial, or a button; all three converge on the same event type,
  preserving the original two-way sync behavior.

  Runs the same guard logic as the original loop(): START only accepted
  from IDLE, STOP only accepted from RECORDING. On successful stop, hands
  the finished recording off to UploadTask by publishing RECORDING_SAVED
  (this replaces the original's appState==SAVING/UPLOAD_CHECK timed
  handoff, but preserves the same 1-second SAVING blink window before
  the handoff — see .cpp for the exact timing match).
*/
#pragma once

#include <Arduino.h>
#include "Core_EventBus.h"
#include "Core_StateManager.h"
#include "Core_ConfigManager.h"
#include "Core_TaskManager.h"
#include "Service_Recorder.h"
#include "Driver_I2S.h"
#include "Driver_SD.h"

struct RecordingTaskDeps {
  EventBus* bus;
  StateManager* stateMgr;
  ConfigManager* configMgr;
  TaskManager* taskMgr;
  I2SDriver* i2s;
  SDDriver* sd;
};

class RecordingTaskImpl {
public:
  void begin(const RecordingTaskDeps& deps);
  void run();   // task loop body, called forever from the FreeRTOS task trampoline

  // Serial-console / config-command entry points (RecordingTask is the
  // sole owner of AudioConfig application, matching the original's
  // "IDLE-only" guard in applyAudioSettings()).
  bool applyAudioSettingsIfIdle(const char* what);

private:
  RecordingTaskDeps deps_;
  QueueHandle_t inbox_ = nullptr;
  Recorder recorder_;

  unsigned long savingStateEnteredMs_ = 0;

  void handleEvent(const Event& evt);
};

void startRecordingTask(RecordingTaskDeps deps);

// Cross-task accessors (called from Task_Upload / Task_Ble / serial console).
const RecordingResult& getLastRecordingResult();
bool applyAudioSettingsIfIdle(const char* what);
