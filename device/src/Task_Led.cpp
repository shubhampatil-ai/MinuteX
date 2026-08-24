#include "Task_Led.h"
#include "Config.h"

namespace {
LedTaskDeps g_deps;
QueueHandle_t g_inbox = nullptr;
TaskHandle_t g_handle = nullptr;

volatile bool g_uploadBlinking = false;
bool g_uploadBlinkOn = false;
unsigned long g_lastUploadBlinkMs = 0;

bool g_savingBlinkOn = false;
unsigned long g_lastSavingBlinkMs = 0;

unsigned long g_uploadOkStartMs = 0;

bool blinkPhase(unsigned long periodMs) {
  return (millis() / periodMs) % 2 == 0;
}

void setLed(uint8_t r, uint8_t g, uint8_t b) { g_deps.rgb->setColor(r, g, b); }
void ledOff()      { setLed(0, 0, 0); }
void ledRed()      { setLed(255, 0, 0); }
void ledBlue()     { setLed(0, 0, 255); }
void ledGreen()    { setLed(0, 255, 0); }
void ledAmber()    { setLed(255, 120, 0); }
void ledWhiteDim() { setLed(40, 40, 40); }

void updateLed() {
  StateSnapshot s = g_deps.stateMgr->getSnapshot();

  // Recording states take precedence — the user must always see they're live.
  if (s.appState == AppState::RECORDING || s.appState == AppState::STARTING) {
    ledRed();
    return;
  }
  if (s.appState == AppState::STOPPING || s.appState == AppState::SAVING) {
    unsigned long now = millis();
    if (now - g_lastSavingBlinkMs >= SAVING_BLINK_INTERVAL_MS) {
      g_savingBlinkOn = !g_savingBlinkOn;
      g_lastSavingBlinkMs = now;
    }
    if (g_savingBlinkOn) ledRed(); else ledOff();
    return;
  }
  if (s.appState == AppState::UPLOAD_OK) {
    if (millis() - g_uploadOkStartMs >= UPLOAD_OK_SOLID_MS) {
      g_deps.stateMgr->setAppState(AppState::IDLE);
    } else {
      ledGreen();
    }
    return;
  }
  // While uploading, the upload blink (driven below in the task loop) owns
  // the pixel — don't fight it here.
  if (s.appState == AppState::UPLOAD_PENDING || s.appState == AppState::UPLOADING) return;

  // ---- IDLE (and any other) state: reflect health/connectivity ----
  if (s.errorFlag) { if (blinkPhase(250)) ledAmber(); else ledOff(); return; }

  if (s.batteryPct <= LOW_BATT_PERCENT) { ledAmber(); return; }

  if (s.wifiConnected) { ledBlue(); return; }
  if (s.wifiConnState == WifiConnState::CONNECTING) {
    if (blinkPhase(400)) ledBlue(); else ledOff();
    return;
  }
  ledWhiteDim();
}

void handleEvent(const Event& evt) {
  switch (evt.type) {
    case EventType::STATE_CHANGED: {
      AppState s = g_deps.stateMgr->getAppState();
      if (s == AppState::UPLOAD_OK) g_uploadOkStartMs = millis();
      break;
    }
    default:
      break;
  }
}

void ledTaskTrampoline(void* /*param*/) {
  for (;;) {
    Event evt;
    while (xQueueReceive(g_inbox, &evt, 0) == pdTRUE) {
      handleEvent(evt);
    }

    if (g_uploadBlinking) {
      unsigned long now = millis();
      if (now - g_lastUploadBlinkMs >= UPLOAD_BLINK_MS) {
        g_lastUploadBlinkMs = now;
        g_uploadBlinkOn = !g_uploadBlinkOn;
        if (g_uploadBlinkOn) ledGreen(); else ledOff();
      }
    } else {
      updateLed();
    }

    g_deps.taskMgr->heartbeat(g_handle);
    vTaskDelay(pdMS_TO_TICKS(50));
  }
}
}  // namespace

void startLedTask(LedTaskDeps deps) {
  g_deps = deps;
  g_inbox = deps.bus->subscribe();
  deps.rgb->begin();

  xTaskCreatePinnedToCore(
      ledTaskTrampoline, "LedTask", 2048, nullptr,
      2 /*priority*/, &g_handle, 1 /*core*/);
  deps.taskMgr->registerTask("LedTask", g_handle, 5000);
}

void ledSetUploadBlinking(bool on) {
  g_uploadBlinking = on;
  if (!on) {
    g_deps.rgb->invalidateCache();
  } else {
    g_lastUploadBlinkMs = millis();
    g_uploadBlinkOn = false;
  }
}
