#include "Task_Wifi.h"
#include "Config.h"
#include "Task_Upload.h"
#include <WiFi.h>
#if WIFI_DISABLE_POWER_SAVE
#include <esp_wifi.h>   // esp_wifi_get_ps() — read back what actually took effect
#endif

namespace {
WifiTaskDeps g_deps;
QueueHandle_t g_inbox = nullptr;
TaskHandle_t g_handle = nullptr;
WifiProvisioning g_wifiProv;

bool g_wifiConnecting = false;
unsigned long g_wifiAttemptStartMs = 0;
String g_wifiLastSsid = "";
WifiConnState g_wifiState = WifiConnState::IDLE;
unsigned long g_lastWifiKickMs = 0;
bool g_wasConnected = false;

// Credential staging slot for CMD_SET_WIFI (see requestSetWifi()). Producers
// (BLE / serial) write it under the mutex; WifiTask reads it when it handles
// the event. A single slot is sufficient — SET_WIFI is a deliberate user
// action, never a burst, and addNetwork() is idempotent for a repeated SSID.
SemaphoreHandle_t g_credMutex = nullptr;
char g_pendingSsid[33] = {0};
char g_pendingPass[65] = {0};

void publishWifiStatus() {
  bool connected = g_wifiProv.isConnected();
  String ip = connected ? WiFi.localIP().toString() : String("");
  g_deps.stateMgr->setWifiStatus(connected, g_wifiState, g_wifiLastSsid, ip);
}

// Kicks a non-blocking connection attempt at the next saved network.
void tryNextWifiNetwork() {
  if (g_wifiProv.networkCount() == 0) return;
  g_wifiLastSsid = g_wifiProv.tryNextNetwork();
  g_wifiConnecting = true;
  g_wifiAttemptStartMs = millis();
  g_wifiState = WifiConnState::CONNECTING;
  g_deps.stateMgr->setError(false);   // fresh attempt clears any prior error
  publishWifiStatus();                // tell the app we're connecting
}

void startWifiBackground() {
  if (g_wifiProv.networkCount() > 0) {
    tryNextWifiNetwork();
    Serial.println("[INFO] WiFi connecting in background (non-blocking)...");
  } else {
    Serial.println("[WIFI] No saved networks — provision via app (SET_WIFI) or serial: wifi <ssid>|<pass>");
  }
}

void wifiKeepAlive() {
  bool now = g_wifiProv.isConnected();
  if (now != g_wasConnected) {
    g_wasConnected = now;
    if (now) {
      Serial.printf("[WIFI] Connected: %s\n", WiFi.localIP().toString().c_str());
      g_wifiConnecting = false;
      g_wifiState = WifiConnState::CONNECTED;
#if WIFI_DISABLE_POWER_SAVE
      // Applied on EVERY connect edge, not once in setup(): the power-save
      // mode is part of the station's runtime config and does not reliably
      // survive a disconnect/reconnect cycle — and this firmware reconnects
      // often (round-robin across saved networks, 20s cooldown kicks). A
      // one-shot call would silently lapse on the first roam and the
      // measurement would drift back to the beacon-limited rate.
      //
      // The mode is read back rather than assumed: setSleep() returning true
      // only means the call was dispatched. esp_wifi_get_ps() is what proves
      // the radio is actually in the state the benchmark claims it is.
      WiFi.setSleep(false);
      wifi_ps_type_t ps = WIFI_PS_MAX_MODEM;
      const char* psName = "unknown";
      if (esp_wifi_get_ps(&ps) == ESP_OK) {
        psName = (ps == WIFI_PS_NONE)      ? "NONE (power save OFF)"
               : (ps == WIFI_PS_MIN_MODEM) ? "MIN_MODEM"
               : (ps == WIFI_PS_MAX_MODEM) ? "MAX_MODEM" : "other";
      }
      Serial.printf("[WIFI] Power save: %s   rssi=%d dBm  ch=%d%s\n",
                    psName, (int)WiFi.RSSI(), (int)WiFi.channel(),
                    (ps == WIFI_PS_NONE) ? "" : "   <-- NOT disabled, expect beacon-limited RTT");
#endif
      // Connect edge: drain any queued uploads immediately rather than
      // waiting for the 30s retry timer.
      if (g_deps.stateMgr->getAppState() == AppState::IDLE) requestPendingRetryIfDue();
    } else {
      g_wifiState = (g_wifiProv.networkCount() > 0) ? WifiConnState::CONNECTING : WifiConnState::IDLE;
    }
    publishWifiStatus();
  }

  // Per-attempt timeout: if a begin() has been in flight too long without
  // connecting, mark it failed and surface it. Then STOP and let the radio
  // idle until the next throttled kick.
  if (g_wifiConnecting && !now &&
      millis() - g_wifiAttemptStartMs >= WIFI_ATTEMPT_TIMEOUT_MS) {
    g_wifiConnecting = false;
    g_wifiState = WifiConnState::FAILED;
    g_deps.stateMgr->setError(true);
    WiFi.disconnect(true);   // release the radio between attempts (helps BLE)
    Serial.printf("[WIFI] Attempt timed out for '%s' (status=%d). Cooling down %us.\n",
                  g_wifiLastSsid.c_str(), (int)WiFi.status(), (unsigned)(WIFI_KICK_INTERVAL_MS / 1000));
    publishWifiStatus();
    g_lastWifiKickMs = millis();   // start cooldown; next kick is throttled
    return;
  }

  if (g_deps.stateMgr->getAppState() == AppState::RECORDING) return;
  if (now) return;
  if (g_wifiConnecting) return;    // an attempt is already in flight — wait it out
  if (g_wifiProv.networkCount() == 0) return;
  if (millis() - g_lastWifiKickMs < WIFI_KICK_INTERVAL_MS) return;
  g_lastWifiKickMs = millis();
  tryNextWifiNetwork();            // round-robin through saved networks
}

void handleEvent(const Event& evt) {
  switch (evt.type) {
    case EventType::CMD_SET_WIFI: {
      String ssid, pass;
      if (g_credMutex) {
        xSemaphoreTake(g_credMutex, portMAX_DELAY);
        ssid = String(g_pendingSsid);
        pass = String(g_pendingPass);
        xSemaphoreGive(g_credMutex);
      }
      if (ssid.length() == 0) {
        Serial.println("[WIFI] SET_WIFI ignored — no staged credentials");
        break;
      }
      g_wifiProv.addNetwork(ssid, pass);
      g_wifiProv.resetTryIndexBeforeSsid(ssid);   // connect to the new network right away
      tryNextWifiNetwork();
      publishWifiStatus();
      break;
    }
    case EventType::CMD_CLEAR_WIFI:
      g_wifiProv.clearNetworks();
      publishWifiStatus();
      break;
    default:
      break;
  }
}

void wifiTaskTrampoline(void* /*param*/) {
  for (;;) {
    Event evt;
    while (xQueueReceive(g_inbox, &evt, 0) == pdTRUE) {
      handleEvent(evt);
    }
    wifiKeepAlive();
    g_deps.taskMgr->heartbeat(g_handle);
    vTaskDelay(pdMS_TO_TICKS(100));
  }
}
}  // namespace

void startWifiTask(WifiTaskDeps deps) {
  g_deps = deps;
  g_inbox = deps.bus->subscribe();
  // Created before any producer can stage credentials: BleProtocol::begin()
  // and serialConsoleBegin() both run after startWifiTask() in setup().
  g_credMutex = xSemaphoreCreateMutex();
  g_wifiProv.begin();

  // STA mode first (matches original ordering: WiFi.mode() must run before
  // BLE claims radio config it needs), background connect kicked from
  // main after BLE is up.
  g_wifiProv.initMode();

  xTaskCreatePinnedToCore(
      wifiTaskTrampoline, "WifiTask", 4096, nullptr,
      1 /*priority*/, &g_handle, 0 /*core*/);
  deps.taskMgr->registerTask("WifiTask", g_handle, 15000);
}

WifiProvisioning& getWifiProvisioning() { return g_wifiProv; }

void requestSetWifi(EventBus* bus, const String& ssid, const String& pass) {
  if (bus == nullptr) return;
  if (g_credMutex) {
    xSemaphoreTake(g_credMutex, portMAX_DELAY);
    ssid.toCharArray(g_pendingSsid, sizeof(g_pendingSsid));
    pass.toCharArray(g_pendingPass, sizeof(g_pendingPass));
    xSemaphoreGive(g_credMutex);
  }
  bus->publish(Event(EventType::CMD_SET_WIFI));
}

void kickWifiBackgroundConnect() { startWifiBackground(); }
