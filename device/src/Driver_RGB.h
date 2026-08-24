/*
  Driver_RGB — raw WS2812 pixel primitive. Exclusively owned by LedTask; no
  other module may call setColor() directly. Color *meaning* (what red vs
  blink vs amber represents) lives in Task_Led, not here.
*/
#pragma once

#include <Arduino.h>
#include <Adafruit_NeoPixel.h>
#include "Config.h"

class RGBDriver {
public:
  void begin() {
    rgb_.begin();
    rgb_.setBrightness(60);
    setColor(0, 0, 0);
  }

  void setColor(uint8_t r, uint8_t g, uint8_t b) {
    uint32_t c = rgb_.Color(r, g, b);
    if (c == lastColor_) return;
    lastColor_ = c;
    rgb_.setPixelColor(0, c);
    rgb_.show();
  }

  // Forces the next setColor() call to actually write, even if the color
  // value is unchanged. Used after periods where something else drove the
  // pixel state externally (matches original lastLedColor reset points).
  void invalidateCache() { lastColor_ = 0xFFFFFFFF; }

private:
  Adafruit_NeoPixel rgb_{1, PIN_RGB_LED, NEO_GRB + NEO_KHZ800};
  uint32_t lastColor_ = 0xFFFFFFFF;
};
