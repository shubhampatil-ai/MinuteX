#include "ButtonIsr.h"
#include "Config.h"

namespace {
EventBus* g_bus = nullptr;
volatile bool g_startFlag = false;
volatile bool g_stopFlag = false;
volatile unsigned long g_lastStartIsrMs = 0;
volatile unsigned long g_lastStopIsrMs = 0;

void IRAM_ATTR onStartPress() {
  unsigned long now = millis();
  if (now - g_lastStartIsrMs > DEBOUNCE_MS) { g_startFlag = true; g_lastStartIsrMs = now; }
}
void IRAM_ATTR onStopPress() {
  unsigned long now = millis();
  if (now - g_lastStopIsrMs > DEBOUNCE_MS) { g_stopFlag = true; g_lastStopIsrMs = now; }
}
}  // namespace

void buttonIsrBegin(EventBus* bus) {
  g_bus = bus;
  pinMode(PIN_BTN_START, INPUT_PULLUP);
  pinMode(PIN_BTN_STOP, INPUT_PULLUP);
  attachInterrupt(digitalPinToInterrupt(PIN_BTN_START), onStartPress, FALLING);
  attachInterrupt(digitalPinToInterrupt(PIN_BTN_STOP), onStopPress, FALLING);
}

void buttonIsrPoll() {
  if (g_startFlag) {
    g_startFlag = false;
    g_bus->publish(Event(EventType::CMD_START));
  }
  if (g_stopFlag) {
    g_stopFlag = false;
    g_bus->publish(Event(EventType::CMD_STOP));
  }
}
