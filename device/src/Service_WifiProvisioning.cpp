#include "Service_WifiProvisioning.h"

void WifiProvisioning::begin() {
  prefs_.begin("wifinet", false);
  loadNetworks();
}

void WifiProvisioning::loadNetworks() {
  count_ = prefs_.getInt("count", 0);
  if (count_ > MAX_WIFI_NETWORKS) count_ = MAX_WIFI_NETWORKS;
  for (int i = 0; i < count_; i++) {
    ssid_[i] = prefs_.getString(("s" + String(i)).c_str(), "");
    pass_[i] = prefs_.getString(("p" + String(i)).c_str(), "");
  }
  Serial.printf("[WIFI] %d saved network(s) loaded\n", count_);
}

void WifiProvisioning::persist() {
  prefs_.putInt("count", count_);
  for (int i = 0; i < count_; i++) {
    prefs_.putString(("s" + String(i)).c_str(), ssid_[i]);
    prefs_.putString(("p" + String(i)).c_str(), pass_[i]);
  }
}

void WifiProvisioning::addNetwork(const String& ssid, const String& pass) {
  for (int i = 0; i < count_; i++) {
    if (ssid_[i] == ssid) {
      pass_[i] = pass;
      persist();
      Serial.printf("[WIFI] Updated password for saved network: %s\n", ssid.c_str());
      return;
    }
  }
  if (count_ < MAX_WIFI_NETWORKS) {
    ssid_[count_] = ssid;
    pass_[count_] = pass;
    count_++;
  } else {
    for (int i = 1; i < MAX_WIFI_NETWORKS; i++) {   // evict oldest
      ssid_[i - 1] = ssid_[i];
      pass_[i - 1] = pass_[i];
    }
    ssid_[MAX_WIFI_NETWORKS - 1] = ssid;
    pass_[MAX_WIFI_NETWORKS - 1] = pass;
  }
  persist();
  Serial.printf("[WIFI] Saved network: %s (%d/%d)\n", ssid.c_str(), count_, MAX_WIFI_NETWORKS);
}

void WifiProvisioning::clearNetworks() {
  prefs_.clear();
  count_ = 0;
  WiFi.disconnect();
  Serial.println("[WIFI] All saved networks cleared");
}

void WifiProvisioning::initMode() {
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.setAutoReconnect(true);
  configTime(0, 0, "pool.ntp.org", "time.google.com");
}

void WifiProvisioning::resetTryIndexBeforeSsid(const String& ssid) {
  tryIndex_ = -1;
  for (int i = 0; i < count_; i++)
    if (ssid_[i] == ssid) tryIndex_ = i - 1;
}

String WifiProvisioning::tryNextNetwork() {
  if (count_ == 0) return "";
  tryIndex_ = (tryIndex_ + 1) % count_;
  String ssid = ssid_[tryIndex_];
  Serial.printf("[WIFI] Trying saved network: %s\n", ssid.c_str());
  WiFi.disconnect();
  WiFi.begin(ssid_[tryIndex_].c_str(), pass_[tryIndex_].c_str());
  return ssid;
}
