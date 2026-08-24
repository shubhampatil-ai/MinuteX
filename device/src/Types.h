/*
  Types.h — shared value types used across core/services/tasks:
  AudioConfig, the app state enum, the event-bus message format, and the
  WiFi provisioning record. Kept dependency-free (no task/queue handles)
  so every module can include it without pulling in FreeRTOS task code.
*/
#pragma once

#include <Arduino.h>

// ---------------- Tunable audio config ----------------
enum ChMode : uint8_t { CH_STEREO = 0, CH_LEFT = 1, CH_RIGHT = 2 };
enum FmtMode : uint8_t { FMT_I2S = 0, FMT_MSB = 1 };

struct AudioConfig {
  uint32_t sampleRate    = 16000;
  uint8_t  shiftBits     = 16;
  uint8_t  fmt           = FMT_I2S;
  uint8_t  chMode        = CH_STEREO;
  bool     rawDebug      = false;
  bool     uploadEnabled = true;
};

inline const char* fmtName(uint8_t f)  { return f == FMT_MSB ? "msb" : "i2s"; }
inline const char* chName(uint8_t c)   { return c == CH_LEFT ? "left" : (c == CH_RIGHT ? "right" : "stereo"); }
inline uint8_t channelCount(uint8_t c) { return c == CH_STEREO ? 2 : 1; }

// ---------------- Application state ----------------
// Expanded internal state machine (BOOT/STARTING/STOPPING/UPLOAD_PENDING are
// new transient states requested by the architecture spec). External BLE
// JSON exposure collapses these back to the original 5 strings via
// AppStateToJsonString() below, so the app/backend see zero change.
enum class AppState : uint8_t {
  BOOT,
  IDLE,
  STARTING,
  RECORDING,
  STOPPING,
  SAVING,
  UPLOAD_PENDING,   // == legacy UPLOAD_CHECK (about to hand off to UploadTask)
  UPLOADING,        // == legacy UPLOAD_CHECK (upload task actively working)
  UPLOAD_OK
};

inline const char* AppStateToJsonString(AppState s) {
  switch (s) {
    case AppState::RECORDING:      return "recording";
    case AppState::STOPPING:
    case AppState::SAVING:         return "saving";
    case AppState::UPLOAD_PENDING:
    case AppState::UPLOADING:      return "uploading";
    case AppState::UPLOAD_OK:      return "uploaded";
    case AppState::BOOT:
    case AppState::IDLE:
    case AppState::STARTING:
    default:                       return "idle";
  }
}

inline const char* AppStateToDebugString(AppState s) {
  switch (s) {
    case AppState::BOOT:            return "BOOT";
    case AppState::IDLE:            return "IDLE";
    case AppState::STARTING:        return "STARTING";
    case AppState::RECORDING:       return "RECORDING";
    case AppState::STOPPING:        return "STOPPING";
    case AppState::SAVING:          return "SAVING";
    case AppState::UPLOAD_PENDING:  return "UPLOAD_PENDING";
    case AppState::UPLOADING:       return "UPLOADING";
    case AppState::UPLOAD_OK:       return "UPLOAD_OK";
    default:                        return "UNKNOWN";
  }
}

// ---------------- WiFi status (surfaced in BLE status JSON + LED) ----------------
enum class WifiConnState : uint8_t { IDLE, CONNECTING, CONNECTED, FAILED };

inline const char* WifiConnStateToString(WifiConnState s) {
  switch (s) {
    case WifiConnState::CONNECTING: return "connecting";
    case WifiConnState::CONNECTED:  return "connected";
    case WifiConnState::FAILED:     return "failed";
    case WifiConnState::IDLE:
    default:                        return "idle";
  }
}

// ---------------- Event bus ----------------
// Small tagged-union style event. Kept POD + fixed-size so it can travel
// through a FreeRTOS queue by value (no heap, no pointers to freed memory).
enum class EventType : uint8_t {
  CMD_START,          // BLE or serial or button: begin recording
  CMD_STOP,           // BLE or serial or button: stop recording
  CMD_GET_STATUS,     // BLE: force a status push
  CMD_SET_WIFI,       // BLE or serial: provision a network (creds staged via requestSetWifi)
  CMD_CLEAR_WIFI,     // BLE or serial: forget all networks
  RECORDING_SAVED,    // RecordingTask -> UploadTask: a file is ready to upload
  UPLOAD_RETRY_DUE,   // internal timer -> UploadTask: check pending queue
  WIFI_STATE_CHANGED, // WifiTask -> StateManager/LedTask/UploadTask
  STATE_CHANGED,      // StateManager -> BleTask/LedTask: appState transitioned
  ERROR_RAISED,       // any task -> LedTask/StateManager: surface amber LED
  ERROR_CLEARED,      // any task -> LedTask: clear amber LED
  BATTERY_UPDATED,    // MonitorTask -> LedTask/BleTask: cached % changed
};

// Deliberately kept tiny (8 bytes). WiFi credentials are NOT inlined here:
// a 98-byte ssid+pass payload made every Event 104 bytes, and since EventBus
// copies each Event into every subscriber's 16-slot queue, that cost ~12KB
// of internal DRAM — memory the upload path needs for TLS/TCP buffers.
// CMD_SET_WIFI credentials now travel through a dedicated mutex-protected
// slot owned by WifiTask (see requestSetWifi() in Task_Wifi.h).
struct Event {
  EventType type;
  union {
    int32_t intVal;
  } payload;

  Event() : type(EventType::CMD_GET_STATUS) { payload.intVal = 0; }
  explicit Event(EventType t) : type(t) { payload.intVal = 0; }
};
