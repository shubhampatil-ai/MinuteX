/*
  Task_Upload — owns HTTP/S3/pending-queue decisions exclusively. Mirrors
  the original background uploadTaskFn()/runUploadCycle() 1:1: handles the
  just-finished recording (on RECORDING_SAVED) plus periodic pending-queue
  retries (on UPLOAD_RETRY_DUE), with identical LED-blink-during-upload,
  BLE-advertising-recovery-between-items, and pending.txt rewrite behavior.

  Pinned to core 0 — long (multi-minute) HTTP PUTs must never share a core
  with BLE (core 1), matching the original's documented rationale.
*/
#pragma once

#include <Arduino.h>
#include "Core_EventBus.h"
#include "Core_StateManager.h"
#include "Core_ConfigManager.h"
#include "Core_TaskManager.h"
#include "Service_AwsUploader.h"
#include "Service_PendingQueue.h"
#include "Driver_SD.h"

struct UploadTaskDeps {
  EventBus* bus;
  StateManager* stateMgr;
  ConfigManager* configMgr;
  TaskManager* taskMgr;
  SDDriver* sd;
};

void startUploadTask(UploadTaskDeps deps);

// Called from WifiTask (on WiFi connect edge) and from the periodic
// serial-driven "retryPendingUploadsIfDue" cadence — publishes
// UPLOAD_RETRY_DUE onto the bus if the same gating conditions the original
// retryPendingUploadsIfDue() checked are satisfied.
void requestPendingRetryIfDue();

// Read-only diagnostic listing for the `pending` serial command. Lives here
// because Task_Upload owns the PendingQueue instance; SerialConsole calls
// this rather than reaching into the queue itself. Touches SD only through
// the same SDDriver mutex every other reader uses, and never mutates
// pending.txt or influences upload scheduling.
void printPendingRecordings();

// Standalone sequential-read benchmark for the `benchsd <filename>` serial
// command. Reads the whole file in BENCH_SD_CHUNK_BYTES chunks (the same size
// HTTPClient pulls when streaming) and reports bytes, elapsed time, and MB/s.
// Read-only; does not touch the pending queue or upload logic.
void benchmarkSdRead(const String& filename);
