/*
  Core_EventBus — simple fan-out pub/sub over FreeRTOS queues. Replaces the
  original firmware's volatile bool flags (startFlag/stopFlag/uploadRequested)
  with proper RTOS primitives, and gives future modules (OTA/VAD/MQTT) a way
  to plug in new event types and subscribers without touching existing
  publishers.

  Design: every subscriber gets its own bounded queue. publish() copies the
  (small, POD) Event into every currently-registered subscriber queue. This
  is O(subscribers) per publish, which is fine at the scale of this product
  (single-digit task count). If a subscriber's queue is full, the event is
  dropped for that subscriber only (matches "queue overflow handling" in the
  spec) and a warning is logged — publishers never block on a slow
  subscriber.
*/
#pragma once

#include <Arduino.h>
#include "Types.h"

constexpr size_t EVENT_BUS_MAX_SUBSCRIBERS = 8;
constexpr size_t EVENT_BUS_QUEUE_LEN       = 16;

class EventBus {
public:
  void begin();

  // Creates and registers a new subscriber queue, returns its handle.
  // Call once per task during setup, before the scheduler starts running
  // that task's loop.
  QueueHandle_t subscribe();

  // Copies `evt` into every subscriber queue (non-blocking; drops + logs on
  // a full queue rather than stalling the publisher).
  void publish(const Event& evt);

private:
  QueueHandle_t subscribers_[EVENT_BUS_MAX_SUBSCRIBERS] = {nullptr};
  size_t subscriberCount_ = 0;
  SemaphoreHandle_t regMutex_ = nullptr;
};
