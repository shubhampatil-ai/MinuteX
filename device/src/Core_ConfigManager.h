/*
  Core_ConfigManager — owns the "recorder" NVS namespace (audio config +
  recording sequence number). Mirrors the original loadConfig()/saveConfig()
  exactly, including default values and the shiftBits sanity clamp.

  Preferences (NVS) already serializes its own flash access internally, so
  a single shared instance accessed from one task (RecordingTask, which is
  the only reader/writer of AudioConfig at runtime) needs no extra mutex.
  Serial/BLE config commands are dispatched into RecordingTask via the
  EventBus in the tasks layer, keeping ConfigManager single-threaded in
  practice.
*/
#pragma once

#include <Arduino.h>
#include <Preferences.h>
#include "Types.h"

class ConfigManager {
public:
  void begin();

  const AudioConfig& get() const { return cfg_; }
  AudioConfig& mutableRef() { return cfg_; }

  void save();
  void resetToDefaults();

  // Recording-number fallback sequence, used when NTP time isn't synced yet
  // (matches original prefs.getUInt("recno", 0) + 1 behavior).
  uint32_t nextRecNo();

private:
  Preferences prefs_;
  AudioConfig cfg_;
};
