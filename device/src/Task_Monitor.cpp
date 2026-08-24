#include "Task_Monitor.h"
#include "Config.h"

namespace {
MonitorTaskDeps g_deps;
TaskHandle_t g_handle = nullptr;

constexpr unsigned long BATTERY_POLL_INTERVAL_MS = 2000;
constexpr unsigned long HEALTH_LOG_INTERVAL_MS    = 30000;
constexpr unsigned long HEARTBEAT_CHECK_INTERVAL_MS = 5000;

unsigned long g_lastBatteryPollMs = 0;
unsigned long g_lastHealthLogMs = 0;
unsigned long g_lastHeartbeatCheckMs = 0;

// Soft watchdog: RecordingTask legitimately blocks for long stretches on
// i2s_channel_read() while actively recording (portMAX_DELAY is
// intentional — that's the original's exact blocking behavior, preserved
// on purpose). A hard esp_task_wdt panic-reset on that task would be a
// NEW failure mode the original firmware never had. Instead: log a
// once-per-stall warning per task so a genuinely wedged task (one that
// should be heartbeating every few seconds but isn't) is visible in the
// serial log for field diagnosis, without introducing surprise reboots.
void checkTaskHeartbeats() {
  unsigned long now = millis();
  for (size_t i = 0; i < g_deps.taskMgr->count(); i++) {
    const TaskHealthRecord& rec = g_deps.taskMgr->recordAt(i);
    if (rec.name == nullptr) continue;
    unsigned long age = now - rec.lastHeartbeatMs;
    if (age > rec.heartbeatTimeoutMs) {
      Serial.printf("[WARN] Task '%s' heartbeat stale (%lums, timeout=%lums)\n",
                    rec.name, age, (unsigned long)rec.heartbeatTimeoutMs);
    }
  }
}

void logHealth() {
  Serial.printf("[HEALTH] heap=%u KB free (min ever %u KB), psram=%s, uptime=%lus\n",
                (unsigned)(ESP.getFreeHeap() / 1024),
                (unsigned)(ESP.getMinFreeHeap() / 1024),
                psramFound() ? (String(ESP.getFreePsram() / 1024) + " KB free").c_str() : "N/A",
                (unsigned long)(millis() / 1000));

  // Per-task stack headroom. uxTaskGetStackHighWaterMark() returns the
  // MINIMUM free stack (in words) ever observed for that task, so
  // "unused" here is the worst case across the whole session — the amount
  // each stack can safely be trimmed by. Task stacks live in internal DRAM,
  // which is the exact resource HTTPS/mbedTLS competes for during an upload,
  // so right-sizing them from real measurements is how we buy back headroom
  // instead of guessing.
  for (size_t i = 0; i < g_deps.taskMgr->count(); i++) {
    const TaskHealthRecord& rec = g_deps.taskMgr->recordAt(i);
    if (rec.name == nullptr || rec.handle == nullptr) continue;
    UBaseType_t freeWords = uxTaskGetStackHighWaterMark(rec.handle);
    Serial.printf("[HEALTH]   %-14s stack unused(min): %u bytes\n",
                  rec.name, (unsigned)(freeWords * sizeof(StackType_t)));
  }
}

void monitorTaskTrampoline(void* /*param*/) {
  for (;;) {
    unsigned long now = millis();

    if (now - g_lastBatteryPollMs >= BATTERY_POLL_INTERVAL_MS) {
      g_lastBatteryPollMs = now;
      int pct = g_deps.battery->readPercent();
      StateSnapshot before = g_deps.stateMgr->getSnapshot();
      g_deps.stateMgr->setBatteryPercent(pct);
      if (before.batteryPct != pct) {
        g_deps.bus->publish(Event(EventType::BATTERY_UPDATED));
      }
    }

    if (now - g_lastHeartbeatCheckMs >= HEARTBEAT_CHECK_INTERVAL_MS) {
      g_lastHeartbeatCheckMs = now;
      checkTaskHeartbeats();
    }

    if (now - g_lastHealthLogMs >= HEALTH_LOG_INTERVAL_MS) {
      g_lastHealthLogMs = now;
      logHealth();
    }

    g_deps.taskMgr->heartbeat(g_handle);
    vTaskDelay(pdMS_TO_TICKS(500));
  }
}
}  // namespace

void startMonitorTask(MonitorTaskDeps deps) {
  g_deps = deps;
  // Intentionally does NOT subscribe to the EventBus: MonitorTask is purely
  // a producer (BATTERY_UPDATED) and reacts to no events. An unused
  // subscriber queue both wasted internal DRAM and, left undrained, made
  // every publish log a "queue full" warning once it filled.
  deps.battery->begin();

  xTaskCreatePinnedToCore(
      monitorTaskTrampoline, "MonitorTask", 2560, nullptr,
      2 /*priority*/, &g_handle, 1 /*core*/);
  deps.taskMgr->registerTask("MonitorTask", g_handle, 10000);
}
