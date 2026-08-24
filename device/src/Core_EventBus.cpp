#include "Core_EventBus.h"

void EventBus::begin() {
  regMutex_ = xSemaphoreCreateMutex();
}

QueueHandle_t EventBus::subscribe() {
  xSemaphoreTake(regMutex_, portMAX_DELAY);
  QueueHandle_t q = nullptr;
  if (subscriberCount_ < EVENT_BUS_MAX_SUBSCRIBERS) {
    q = xQueueCreate(EVENT_BUS_QUEUE_LEN, sizeof(Event));
    subscribers_[subscriberCount_++] = q;
  } else {
    Serial.println("[ERROR] EventBus: max subscribers exceeded");
  }
  xSemaphoreGive(regMutex_);
  return q;
}

void EventBus::publish(const Event& evt) {
  xSemaphoreTake(regMutex_, portMAX_DELAY);
  for (size_t i = 0; i < subscriberCount_; i++) {
    if (subscribers_[i] == nullptr) continue;
    if (xQueueSend(subscribers_[i], &evt, 0) != pdTRUE) {
      Serial.println("[WARN] EventBus: subscriber queue full, event dropped");
    }
  }
  xSemaphoreGive(regMutex_);
}
