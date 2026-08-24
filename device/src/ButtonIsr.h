/*
  ButtonIsr — physical START/STOP button interrupt handlers. Mirrors the
  original's debounce logic (250ms) exactly. Since publishing directly to
  an EventBus queue from ISR context requires the FromISR queue API (not
  the blocking xQueueSend used elsewhere), button presses are captured as
  a minimal ISR-safe counter and drained by a tiny task-context poll that
  publishes the real CMD_START/CMD_STOP events — this keeps ISR work to
  the bare minimum (IRAM-resident, no queue/heap calls) while still
  routing through the same EventBus all other command sources use.
*/
#pragma once

#include <Arduino.h>
#include "Core_EventBus.h"

void buttonIsrBegin(EventBus* bus);
void buttonIsrPoll();   // call every loop() iteration; publishes any pending press
