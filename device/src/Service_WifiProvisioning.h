/*
  Service_WifiProvisioning — owns the "wifinet" NVS namespace and the
  round-robin saved-network connect logic. Used exclusively by WifiTask.
  Mirrors the original loadWifiNetworks()/persistWifiNetworks()/
  addWifiNetwork()/clearWifiNetworks()/tryNextWifiNetwork() exactly,
  including the oldest-entry eviction policy when the 3-slot list is full.
*/
#pragma once

#include <Arduino.h>
#include <Preferences.h>
#include <WiFi.h>
#include "Config.h"

class WifiProvisioning {
public:
  void begin();

  void loadNetworks();
  void addNetwork(const String& ssid, const String& pass);
  void clearNetworks();

  int networkCount() const { return count_; }

  // Sets WIFI_STA mode + NTP config + setSleep(false)/autoReconnect(true).
  // Must be called before BLE init (matches original ordering rationale).
  void initMode();

  // Kicks a non-blocking WiFi.begin() at the next saved network
  // (round-robin). Returns the SSID it just attempted, or "" if no
  // networks are saved.
  String tryNextNetwork();

  // Resets the round-robin pointer so the next tryNextNetwork() call lands
  // on network index 0 (used after a fresh SET_WIFI provision, matching
  // original's wifiTryIndex = -1 reset + search-for-just-added-index dance).
  void resetTryIndexBeforeSsid(const String& ssid);

  bool isConnected() const { return WiFi.status() == WL_CONNECTED; }

private:
  Preferences prefs_;
  int count_ = 0;
  String ssid_[MAX_WIFI_NETWORKS];
  String pass_[MAX_WIFI_NETWORKS];
  int tryIndex_ = 0;

  void persist();
};
