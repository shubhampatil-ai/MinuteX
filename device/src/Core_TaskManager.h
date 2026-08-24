/*
  Core_TaskManager — generic task registration/bookkeeping so MonitorTask
  can watch every task's health (stack high-water mark + last-heartbeat
  timestamp) without each task needing bespoke monitoring code, and so
  future tasks (OTA, VAD, ...) register themselves the same way existing
  ones do.

  This does NOT replace xTaskCreate — tasks still create themselves however
  they need (pinned core, stack size, priority). They just call
  TaskManager::registerTask() once at startup and TaskManager::heartbeat()
  periodically from their own loop.
*/
#pragma once

#include <Arduino.h>

constexpr size_t TASK_MANAGER_MAX_TASKS = 12;

struct TaskHealthRecord {
  const char* name = nullptr;
  TaskHandle_t handle = nullptr;
  volatile uint32_t lastHeartbeatMs = 0;
  uint32_t heartbeatTimeoutMs = 10000;   // if a task misses this, MonitorTask flags it
};

class TaskManager {
public:
  void begin();

  // Called once by each task right after xTaskCreate*(), from setup context.
  void registerTask(const char* name, TaskHandle_t handle, uint32_t heartbeatTimeoutMs = 10000);

  // Called by each managed task periodically from within its own loop.
  void heartbeat(TaskHandle_t self);

  size_t count() const { return count_; }
  const TaskHealthRecord& recordAt(size_t i) const { return records_[i]; }

private:
  TaskHealthRecord records_[TASK_MANAGER_MAX_TASKS];
  size_t count_ = 0;
  SemaphoreHandle_t mutex_ = nullptr;
};
