#include "Core_TaskManager.h"

void TaskManager::begin() {
  mutex_ = xSemaphoreCreateMutex();
}

void TaskManager::registerTask(const char* name, TaskHandle_t handle, uint32_t heartbeatTimeoutMs) {
  xSemaphoreTake(mutex_, portMAX_DELAY);
  if (count_ < TASK_MANAGER_MAX_TASKS) {
    records_[count_].name = name;
    records_[count_].handle = handle;
    records_[count_].lastHeartbeatMs = millis();
    records_[count_].heartbeatTimeoutMs = heartbeatTimeoutMs;
    count_++;
  } else {
    Serial.println("[ERROR] TaskManager: max tasks exceeded");
  }
  xSemaphoreGive(mutex_);
}

void TaskManager::heartbeat(TaskHandle_t self) {
  xSemaphoreTake(mutex_, portMAX_DELAY);
  for (size_t i = 0; i < count_; i++) {
    if (records_[i].handle == self) {
      records_[i].lastHeartbeatMs = millis();
      break;
    }
  }
  xSemaphoreGive(mutex_);
}
