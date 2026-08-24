/*
  Main.cpp — the entire former ESP_32.ino, verbatim.

  Why a .cpp and not the .ino: this project builds two ways —
    - Arduino IDE (stock libs, lwIP snd_buf 5744)
    - PlatformIO / pioarduino (custom_sdkconfig libs, snd_buf 65535 — see
      platformio.ini)
  PlatformIO's .ino->.cpp converter (ConvertInoToCpp/_gcc_preprocess) failed on
  this sketch, and the conversion is pure overhead anyway: the file already
  included <Arduino.h> and defines only setup()/loop() with no
  auto-prototyping needs. So the code lives here as a normal translation unit,
  ESP_32.ino remains as a comment-only stub (the IDE requires a .ino named
  after the folder), and platformio.ini excludes *.ino from the build.

  ESP32-S3 Dual-Mic WAV Recorder + S3 Uploader — v1.7.4 (BLE + WiFi provisioning)
  Board: ESP32-S3 N16R8, Arduino-ESP32 core 3.x (IDF 5.x)
  Mics: 2x INMP441 | Storage: SD via SPI | Status: WS2812 RGB LED
  Control: 2 buttons + serial commands + BLE

  ARCHITECTURE (v2.0 — modular FreeRTOS rewrite):
  This firmware was refactored from a single-file loop()-driven monolith
  into a modular multi-task FreeRTOS architecture. EVERY externally visible
  behavior is byte-for-byte preserved: same BLE UUIDs/commands/JSON, same
  WiFi provisioning, same AWS presign/upload flow, same SD/WAV format, same
  serial commands, same LED behavior, same pending-upload queue, same
  battery monitoring. The mobile app requires zero changes.

  Layout (flat files, Arduino-IDE compilable — no subfolders, since the
  Arduino IDE does not compile .cpp files in sketch subdirectories):
    Config.h / Types.h              shared constants + value types
    Core_*                          StateManager, EventBus, ConfigManager, TaskManager
    Driver_*                        I2S, SD, Battery, RGB — hardware primitives only
    Service_*                       Recorder, AwsUploader, PendingQueue, BleProtocol, WifiProvisioning
    Task_*                          RecordingTask, BleTask, WifiTask, UploadTask, LedTask, MonitorTask
    SerialConsole / ButtonIsr        the two remaining "input source" adapters

  Task ownership (single owner per hardware resource — see architecture
  migration plan):
    RecordingTask  -> I2S, WAV, SD writes            (highest priority, core 1)
    UploadTask     -> HTTP/S3, pending queue, SD reads (core 0)
    BleTask        -> BLE server/advertising/notify    (core 1)
    WifiTask       -> WiFi radio                        (core 0)
    LedTask        -> RGB LED                           (core 1)
    MonitorTask    -> battery ADC, health/heartbeat logging (core 1)
    StateManager   -> appState (sole mutator; everyone else requests transitions)
  SD card is the one resource genuinely shared by two tasks (Recording
  writes, Upload reads) — Driver_SD serializes access with a mutex rather
  than pretending a single owner is possible, matching how the original
  firmware already shared the card across recording/upload/pending-queue
  code paths.

  Communication: FreeRTOS EventBus (pub/sub over per-task queues) replaces
  the original's volatile bool startFlag/stopFlag/uploadRequested globals.

  BUILD SETTINGS (Tools menu):
    PSRAM: OPI PSRAM          <- required for fast uploads
    Partition Scheme: choose one with larger APP if you hit size limits
    (BLE + WiFi + TLS is big; "8M with spiffs" / "16M Flash" schemes work)

  LED: RED=recording, RED blink=saving, GREEN blink=uploading,
       GREEN solid 5s=uploaded, BLUE=idle+WiFi, OFF=idle no WiFi.

  Serial commands: help, show, start, stop, shift, fmt, ch, rate, raw,
    upload, save, defaults, wifi <ssid>|<pass>, wificlear
*/

#include <Arduino.h>
#include <SPI.h>
#include <SD.h>
#include <WiFi.h>
#include <Preferences.h>
#include <Ticker.h>
#include <time.h>

#include "Config.h"
#include "Types.h"
#include "Core_EventBus.h"
#include "Core_StateManager.h"
#include "Core_ConfigManager.h"
#include "Core_TaskManager.h"
#include "Driver_I2S.h"
#include "Driver_SD.h"
#include "Driver_Battery.h"
#include "Driver_RGB.h"
#include "Service_BleProtocol.h"
#include "Task_Recording.h"
#include "Task_Ble.h"
#include "Task_Wifi.h"
#include "Task_Upload.h"
#include "Task_Led.h"
#include "Task_Monitor.h"
#include "SerialConsole.h"
#include "ButtonIsr.h"

// ---------------- Core singletons ----------------
// These are the only "global" objects in the firmware. Each is either a
// pure coordination primitive (EventBus, StateManager, TaskManager) with
// its own internal locking, or a single-owner driver/service instantiated
// once and handed by pointer to exactly the one task that owns it.
static EventBus       g_eventBus;
static StateManager   g_stateManager;
static ConfigManager  g_configManager;
static TaskManager    g_taskManager;

static I2SDriver      g_i2sDriver;
static SDDriver       g_sdDriver;
static BatteryDriver  g_batteryDriver;
static RGBDriver      g_rgbDriver;

BleProtocol g_bleProtocol;   // extern-referenced by Task_Upload.cpp / SerialConsole.cpp

void setup() {
  Serial.begin(115200);
  unsigned long serialWaitStart = millis();
  while (!Serial && millis() - serialWaitStart < 3000) delay(10);
  delay(200);
  Serial.printf("[BOOT] ESP32-S3 Dual-Mic Recorder v%s (BLE + WiFi provisioning)\n", FW_VERSION);
  Serial.println(psramFound()
      ? ("[BOOT] PSRAM detected: " + String(ESP.getPsramSize() / 1024) + " KB")
      : "[BOOT] PSRAM NOT FOUND — set Tools->PSRAM->OPI PSRAM");

  // ---- Core layer bring-up (must happen before any task starts) ----
  g_eventBus.begin();
  g_stateManager.begin(&g_eventBus);
  g_configManager.begin();
  g_taskManager.begin();
  g_stateManager.setAppState(AppState::BOOT);

  // ---- SD card (shared resource — mount once, before any task needs it) ----
  if (!g_sdDriver.begin()) {
    Serial.println("[ERROR] SD card mount failed — halting");
    while (true) delay(1000);
  }
  Serial.println("[INIT] SD card mounted");

  // ---- Buttons (ISR only sets a debounced flag; polled into EventBus below) ----
  buttonIsrBegin(&g_eventBus);

  // ---- Serial console ----
  SerialConsoleDeps consoleDeps{&g_eventBus, &g_stateManager, &g_configManager};
  serialConsoleBegin(consoleDeps);

  // ---- Tasks, in the same dependency order the original setup() used:
  //      I2S must be ready before recording can start (done inside
  //      RecordingTask::begin), WiFi mode before BLE claims the radio,
  //      BLE up before WiFi starts connecting (matches original ordering
  //      rationale for the setSleep(false) BLE-pairing fix). ----

  RecordingTaskDeps recDeps{&g_eventBus, &g_stateManager, &g_configManager, &g_taskManager, &g_i2sDriver, &g_sdDriver};
  startRecordingTask(recDeps);

  WifiTaskDeps wifiDeps{&g_eventBus, &g_stateManager, &g_taskManager};
  startWifiTask(wifiDeps);   // brings up WIFI_STA mode synchronously inside begin()

  g_bleProtocol.begin(&g_eventBus, &g_stateManager);
  BleTaskDeps bleDeps{&g_eventBus, &g_stateManager, &g_taskManager, &g_bleProtocol};
  startBleTask(bleDeps);

  kickWifiBackgroundConnect();   // matches original startWiFiBackground() call order (after BLE init)

  UploadTaskDeps uploadDeps{&g_eventBus, &g_stateManager, &g_configManager, &g_taskManager, &g_sdDriver};
  startUploadTask(uploadDeps);

  LedTaskDeps ledDeps{&g_eventBus, &g_stateManager, &g_taskManager, &g_rgbDriver};
  startLedTask(ledDeps);

  MonitorTaskDeps monDeps{&g_eventBus, &g_stateManager, &g_taskManager, &g_batteryDriver};
  startMonitorTask(monDeps);

  g_stateManager.setAppState(AppState::IDLE);

  Serial.println("[BOOT] Ready. 'help' for commands. Provision WiFi via app or: wifi <ssid>|<pass>");
}

void loop() {
  // loop() itself now does almost nothing — all real work lives on the
  // dedicated tasks above. It remains only as the home for the two input
  // sources that don't have a natural task of their own (serial line
  // reading and debounced button-flag draining), both of which just
  // publish EventBus events for the owning task to act on.
  buttonIsrPoll();
  serialConsolePoll();
  delay(10);
}
