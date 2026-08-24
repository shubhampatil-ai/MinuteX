/*
  Driver_Battery — raw ADC read + Li-ion percent curve. Owned by
  MonitorTask; other modules read the cached value via StateManager/
  EventBus, never the ADC directly.
*/
#pragma once

#include <Arduino.h>
#include "Config.h"

class BatteryDriver {
public:
  void begin() {}

  float readVoltage() const {
    int raw = analogRead(pinAdc_);
    float vAdc = (raw / 4095.0f) * 3.3f;
    return vAdc * 2.0f;
  }

  int readPercent() const {
    float v = readVoltage();   // rough Li-ion curve 3.3-4.2 V
    int p = (int)((v - 3.3f) / (4.2f - 3.3f) * 100.0f);
    if (p < 0) p = 0;
    if (p > 100) p = 100;
    return p;
  }

private:
  static constexpr int pinAdc_ = PIN_BATT_ADC;
};
