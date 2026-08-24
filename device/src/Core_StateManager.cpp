#include "Core_StateManager.h"

void StateManager::begin(EventBus* bus) {
  mutex_ = xSemaphoreCreateMutex();
  bus_ = bus;
}

void StateManager::setAppState(AppState s) {
  xSemaphoreTake(mutex_, portMAX_DELAY);
  bool changed = (state_.appState != s);
  state_.appState = s;
  xSemaphoreGive(mutex_);
  if (changed) publishStateChanged();
}

void StateManager::setWifiStatus(bool connected, WifiConnState connState, const String& ssid, const String& ip) {
  xSemaphoreTake(mutex_, portMAX_DELAY);
  state_.wifiConnected = connected;
  state_.wifiConnState = connState;
  state_.wifiSsid = ssid;
  state_.wifiIp = ip;
  xSemaphoreGive(mutex_);
  // Always publish (no dedup guard): every original call site
  // (tryNextWifiNetwork/connect-edge/disconnect-edge/attempt-timeout)
  // called sendBleStatus() unconditionally afterward, regardless of
  // whether the fields actually changed value.
  Event e(EventType::WIFI_STATE_CHANGED);
  bus_->publish(e);
}

void StateManager::setBatteryPercent(int pct) {
  xSemaphoreTake(mutex_, portMAX_DELAY);
  state_.batteryPct = pct;
  xSemaphoreGive(mutex_);
}

void StateManager::setPendingCount(int count) {
  xSemaphoreTake(mutex_, portMAX_DELAY);
  state_.pendingCount = count;
  xSemaphoreGive(mutex_);
}

void StateManager::setError(bool active) {
  xSemaphoreTake(mutex_, portMAX_DELAY);
  bool changed = (state_.errorFlag != active);
  state_.errorFlag = active;
  xSemaphoreGive(mutex_);
  if (changed) {
    Event e(active ? EventType::ERROR_RAISED : EventType::ERROR_CLEARED);
    bus_->publish(e);
  }
}

StateSnapshot StateManager::getSnapshot() const {
  xSemaphoreTake(mutex_, portMAX_DELAY);
  StateSnapshot copy = state_;
  xSemaphoreGive(mutex_);
  return copy;
}

AppState StateManager::getAppState() const {
  xSemaphoreTake(mutex_, portMAX_DELAY);
  AppState s = state_.appState;
  xSemaphoreGive(mutex_);
  return s;
}

void StateManager::publishStateChanged() {
  Event e(EventType::STATE_CHANGED);
  bus_->publish(e);
}
