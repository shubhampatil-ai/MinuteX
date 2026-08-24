#include "Task_Ble.h"

namespace {
BleTaskDeps g_deps;
QueueHandle_t g_inbox = nullptr;
TaskHandle_t g_handle = nullptr;

void handleEvent(const Event& evt) {
  switch (evt.type) {
    case EventType::STATE_CHANGED:
    case EventType::WIFI_STATE_CHANGED:
    case EventType::BATTERY_UPDATED:
    case EventType::CMD_GET_STATUS:
    case EventType::ERROR_RAISED:
    case EventType::ERROR_CLEARED:
      g_deps.ble->pushStatus();
      break;
    default:
      break;
  }
}

void bleTaskTrampoline(void* /*param*/) {
  for (;;) {
    Event evt;
    while (xQueueReceive(g_inbox, &evt, pdMS_TO_TICKS(50)) == pdTRUE) {
      handleEvent(evt);
    }
    g_deps.ble->ensureAdvertising();
    g_deps.taskMgr->heartbeat(g_handle);
  }
}
}  // namespace

void startBleTask(BleTaskDeps deps) {
  g_deps = deps;
  g_inbox = deps.bus->subscribe();

  xTaskCreatePinnedToCore(
      bleTaskTrampoline, "BleTask", 4096, nullptr,
      3 /*priority*/, &g_handle, 1 /*core*/);
  deps.taskMgr->registerTask("BleTask", g_handle, 5000);
}
