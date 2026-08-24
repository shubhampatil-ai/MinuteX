/*
  Driver_I2S — thin wrapper over the ESP-IDF i2s_std driver for the two
  INMP441 mics. Owned exclusively by RecordingTask/Service_Recorder; no
  other module may call these functions.
*/
#pragma once

#include <Arduino.h>
#include "driver/i2s_std.h"
#include "Types.h"

class I2SDriver {
public:
  // (Re)initializes the I2S RX channel for the given audio config. Safe to
  // call again to change sample rate/format/channel mode — tears down any
  // existing channel first. Returns false and leaves the driver uninstalled
  // on failure.
  bool begin(const AudioConfig& cfg);

  void end();

  bool isInstalled() const { return installed_; }

  // Blocking read of one DMA-sized chunk (portMAX_DELAY like the original).
  // Returns ESP_OK/bytesRead via out params, matching i2s_channel_read().
  esp_err_t read(void* buf, size_t bufBytes, size_t* bytesRead);

private:
  i2s_chan_handle_t rxHandle_ = nullptr;
  bool installed_ = false;
};
