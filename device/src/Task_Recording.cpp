#include "Task_Recording.h"
#include "Config.h"

namespace {
RecordingTaskImpl g_recordingTask;
TaskHandle_t g_handle = nullptr;

enum class LocalPhase { IDLE, RECORDING, SAVING };
LocalPhase g_phase = LocalPhase::IDLE;
RecordingResult g_pendingResult;
}  // namespace

void RecordingTaskImpl::begin(const RecordingTaskDeps& deps) {
  deps_ = deps;
  inbox_ = deps_.bus->subscribe();
  recorder_.begin(deps_.i2s, deps_.sd, deps_.configMgr);

  if (!deps_.i2s->begin(deps_.configMgr->get())) {
    Serial.println("[ERROR] I2S init failed — halting");
    while (true) delay(1000);
  }
}

bool RecordingTaskImpl::applyAudioSettingsIfIdle(const char* what) {
  if (deps_.stateMgr->getAppState() != AppState::IDLE) {
    Serial.printf("[WARN] %s change requires IDLE state — stop recording first\n", what);
    return false;
  }
  return recorder_.applyAudioSettings(what);
}

void RecordingTaskImpl::handleEvent(const Event& evt) {
  switch (evt.type) {
    case EventType::CMD_START: {
      AppState s = deps_.stateMgr->getAppState();
      if (s == AppState::IDLE) {
        deps_.stateMgr->setAppState(AppState::STARTING);
        if (recorder_.start()) {
          g_phase = LocalPhase::RECORDING;
          deps_.stateMgr->setAppState(AppState::RECORDING);
        } else {
          deps_.stateMgr->setAppState(AppState::IDLE);
        }
      } else {
        Serial.println("[WARN] Start ignored — already recording or busy");
      }
      break;
    }
    case EventType::CMD_STOP: {
      if (deps_.stateMgr->getAppState() == AppState::RECORDING) {
        deps_.stateMgr->setAppState(AppState::STOPPING);
        g_pendingResult = recorder_.stop();
        g_phase = LocalPhase::SAVING;
        savingStateEnteredMs_ = millis();
        deps_.stateMgr->setAppState(AppState::SAVING);
      } else {
        Serial.println("[WARN] Stop ignored — not currently recording");
      }
      break;
    }
    default:
      break;   // not our event
  }
}

void RecordingTaskImpl::run() {
  Event evt;
  // Drain any pending commands without blocking the recording chunk loop.
  while (xQueueReceive(inbox_, &evt, 0) == pdTRUE) {
    handleEvent(evt);
  }

  switch (g_phase) {
    case LocalPhase::RECORDING:
      recorder_.captureChunk();
      break;

    case LocalPhase::SAVING:
      if (millis() - savingStateEnteredMs_ >= SAVING_DURATION_MS) {
        deps_.stateMgr->setAppState(AppState::UPLOAD_PENDING);
        Serial.println("[STATE] Saving complete, checking upload");
        g_phase = LocalPhase::IDLE;

        Event handoff(EventType::RECORDING_SAVED);
        // Filenames/ids travel via a shared static (single in-flight
        // recording at a time, same invariant the original relied on:
        // a new recording can't start until appState == IDLE again).
        deps_.bus->publish(handoff);
      }
      break;

    case LocalPhase::IDLE:
    default:
      break;
  }

  deps_.taskMgr->heartbeat(g_handle);
}

// Exposes the just-finished recording's metadata to UploadTask without a
// second queue: UploadTask reads this immediately upon receiving
// RECORDING_SAVED, before RecordingTask could possibly start a new
// recording (appState is not IDLE again until UPLOAD_OK/failure path
// completes), matching the original's single-in-flight-file invariant.
const RecordingResult& getLastRecordingResult() { return g_pendingResult; }

static void recordingTaskTrampoline(void* /*param*/) {
  for (;;) {
    g_recordingTask.run();
    // No fixed delay while RECORDING: recorder_.captureChunk() blocks on
    // i2s_channel_read() with portMAX_DELAY, matching the original's tight
    // loop() cadence. When IDLE/SAVING, yield briefly so this task doesn't
    // spin the core.
    //
    // KNOWN ISSUE (deliberately left unfixed for now): when the DMA ring
    // already has data, that read returns immediately and this task
    // (priority 4, core 1) spins without yielding — measured starving
    // LedTask and MonitorTask for ~25s during a long recording. A 1-tick
    // yield per chunk fixes it at ~3% overhead, but that is a behavioural
    // change and the current goal is measurement against an unmodified
    // baseline.
    if (g_phase != LocalPhase::RECORDING) {
      vTaskDelay(pdMS_TO_TICKS(20));
    }
  }
}

void startRecordingTask(RecordingTaskDeps deps) {
  g_recordingTask.begin(deps);
  // 8192, matching ARDUINO_LOOP_STACK_SIZE — the stack the pre-refactor
  // v1.6.2 monolith ran every SD operation on from loop(), and which worked.
  //
  // ROOT CAUSE OF THE DATA-LOSS BUG this fixes: FATFS here is built with
  // CONFIG_FATFS_LFN_STACK (FF_USE_LFN 2) and FF_MAX_LFN 255, so every
  // long-filename path operation allocates a (FF_MAX_LFN+1)*2 = 512 byte
  // working buffer ON THE CALLING TASK'S STACK (ffconf.h:166), plus UTF-8
  // conversion on top. Our filenames (/rec_<epoch>.wav) are long filenames.
  //
  // At 4096 this task measured only 1,332 bytes free at its minimum. That is
  // not enough for the 512-byte LFN buffer plus the f_close -> f_sync ->
  // directory-update chain, so the directory entry silently failed to commit:
  // audio data reached the clusters (card showed GB used) but stat() failed,
  // making File::size() return its stale 0, SD.exists() return false, and
  // f_readdir yield an empty root — a recording reporting >1MB written that
  // did not exist on the card. File::close() returns void, so the failure
  // surfaced nowhere.
  xTaskCreatePinnedToCore(
      recordingTaskTrampoline, "RecordingTask", 8192, nullptr,
      4 /*priority*/, &g_handle, 1 /*core*/);
  deps.taskMgr->registerTask("RecordingTask", g_handle, 5000);
}

bool applyAudioSettingsIfIdle(const char* what) {
  return g_recordingTask.applyAudioSettingsIfIdle(what);
}
