#include "Core_ConfigManager.h"

void ConfigManager::begin() {
  prefs_.begin("recorder", false);
  cfg_.sampleRate    = prefs_.getUInt("rate", 16000);
  cfg_.shiftBits     = prefs_.getUChar("shift", 16);
  cfg_.fmt           = prefs_.getUChar("fmt", FMT_I2S);
  cfg_.chMode        = prefs_.getUChar("ch", CH_STEREO);
  cfg_.rawDebug      = prefs_.getBool("raw", false);
  cfg_.uploadEnabled = prefs_.getBool("upl", true);
  if (cfg_.shiftBits < 11 || cfg_.shiftBits > 16) cfg_.shiftBits = 16;
}

void ConfigManager::save() {
  prefs_.putUInt("rate", cfg_.sampleRate);
  prefs_.putUChar("shift", cfg_.shiftBits);
  prefs_.putUChar("fmt", cfg_.fmt);
  prefs_.putUChar("ch", cfg_.chMode);
  prefs_.putBool("raw", cfg_.rawDebug);
  prefs_.putBool("upl", cfg_.uploadEnabled);
  Serial.println("[CONFIG] Saved to NVS");
}

void ConfigManager::resetToDefaults() {
  cfg_ = AudioConfig();
}

uint32_t ConfigManager::nextRecNo() {
  uint32_t recNo = prefs_.getUInt("recno", 0) + 1;
  prefs_.putUInt("recno", recNo);
  return recNo;
}
