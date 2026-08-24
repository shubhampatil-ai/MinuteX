#include "SerialConsole.h"
#include "Config.h"
#include "Task_Recording.h"
#include "Task_Wifi.h"
#include "Task_Upload.h"   // printPendingRecordings() for the `pending` command
#include "Service_BleProtocol.h"
#include <WiFi.h>

extern BleProtocol g_bleProtocol;   // defined in ESP_32.ino

namespace {
SerialConsoleDeps g_deps;
char g_lineBuf[96];
size_t g_lineLen = 0;

void printHelp() {
  Serial.println(F("[HELP] Commands:"));
  Serial.println(F("  start | stop            record control"));
  Serial.println(F("  show                    print config + state"));
  Serial.println(F("  pending                 list queued (not yet uploaded) recordings"));
  Serial.println(F("  benchsd <filename>      benchmark sequential SD read speed"));
  Serial.println(F("  sdinfo                  SD driver/SPI/card parameters"));
  Serial.println(F("  sdbench <filename>      open/read/close timing + buffer size sweep"));
  Serial.println(F("  wifi <ssid>|<pass>      save + connect a WiFi network"));
  Serial.println(F("  wificlear               forget all saved networks"));
  Serial.println(F("  shift <11..16>          32->16 shift"));
  Serial.println(F("  fmt <i2s|msb>           slot standard"));
  Serial.println(F("  ch <stereo|left|right>  channel mode"));
  Serial.println(F("  rate <hz>               8000/16000/22050/32000/44100"));
  Serial.println(F("  raw <on|off>            raw sample hex dump"));
  Serial.println(F("  upload <on|off>         WiFi upload after save"));
  Serial.println(F("  save | defaults         persist / reset config"));
}

void printConfig() {
  const AudioConfig& cfg = g_deps.configMgr->get();
  StateSnapshot s = g_deps.stateMgr->getSnapshot();

  Serial.printf("[CONFIG] rate=%uHz  shift=%u  fmt=%s  ch=%s  raw=%s  upload=%s\n",
                (unsigned)cfg.sampleRate, (unsigned)cfg.shiftBits, fmtName(cfg.fmt),
                chName(cfg.chMode), cfg.rawDebug ? "on" : "off", cfg.uploadEnabled ? "on" : "off");
  Serial.printf("[CONFIG] state=%s\n", AppStateToDebugString(s.appState));
  Serial.printf("[CONFIG] wifi=%s\n",
                s.wifiConnected ? ("connected " + s.wifiIp).c_str() : "not connected");

  WifiProvisioning& prov = getWifiProvisioning();
  String nets = "saved wifi:";
  if (prov.networkCount() == 0) nets += " (none — provision via BLE or 'wifi ssid|pass')";
  Serial.println("[CONFIG] " + nets);

  Serial.printf("[CONFIG] ble=%s%s\n", g_bleProtocol.deviceName().c_str(),
                g_bleProtocol.isClientConnected() ? " (app connected)" : " (advertising)");
  Serial.printf("[CONFIG] psram=%s\n",
                psramFound() ? (String(ESP.getFreePsram() / 1024) + " KB free").c_str() : "NOT FOUND (enable OPI PSRAM!)");
}

void handleCommand(String cmd) {
  cmd.trim();
  if (cmd.length() == 0) return;

  // Preserve case for wifi credentials; lowercase only the verb.
  int sp = cmd.indexOf(' ');
  String verb = (sp < 0) ? cmd : cmd.substring(0, sp);
  String arg  = (sp < 0) ? ""  : cmd.substring(sp + 1);
  verb.toLowerCase();
  arg.trim();

  AudioConfig& cfg = g_deps.configMgr->mutableRef();

  if (verb == "start") {
    Serial.println("[DEBUG] Serial: start");
    g_deps.bus->publish(Event(EventType::CMD_START));
  } else if (verb == "stop") {
    Serial.println("[DEBUG] Serial: stop");
    g_deps.bus->publish(Event(EventType::CMD_STOP));
  } else if (verb == "help") {
    printHelp();
  } else if (verb == "show") {
    printConfig();
  } else if (verb == "pending") {
    printPendingRecordings();   // read-only listing; see Task_Upload.h
  } else if (verb == "benchsd") {
    if (arg.length() == 0) {
      Serial.println("[WARN] Format: benchsd <filename>   (e.g. benchsd /rec_1785405103.wav)");
      Serial.println("[WARN] Use 'pending' to list queued recordings and their names");
    } else {
      benchmarkSdRead(arg);
    }
  } else if (verb == "sdinfo") {
    sdPrintInfo();
  } else if (verb == "sdbench") {
    if (arg.length() == 0) {
      Serial.println("[WARN] Format: sdbench <filename>   (open/read/close timing + buffer sweep)");
    } else {
      sdRunBenchmark(arg);
    }
  } else if (verb == "save") {
    g_deps.configMgr->save();
  } else if (verb == "wifi") {
    int sep = arg.indexOf('|');
    if (sep > 0) {
      String ssid = arg.substring(0, sep);
      String pass = arg.substring(sep + 1);
      requestSetWifi(g_deps.bus, ssid, pass);
    } else {
      Serial.println("[WARN] Format: wifi <ssid>|<password>");
    }
  } else if (verb == "wificlear") {
    g_deps.bus->publish(Event(EventType::CMD_CLEAR_WIFI));
  } else if (verb == "defaults") {
    AudioConfig d;
    bool needReinit = (d.sampleRate != cfg.sampleRate || d.fmt != cfg.fmt || d.chMode != cfg.chMode);
    cfg = d;
    if (needReinit) applyAudioSettingsIfIdle("defaults");
    Serial.println("[CONFIG] Defaults restored (use 'save' to persist)");
    printConfig();
  } else if (verb == "shift") {
    int v = arg.toInt();
    if (v >= 11 && v <= 16) {
      cfg.shiftBits = (uint8_t)v;
      Serial.printf("[CONFIG] shift=%d%s\n", v, v < 16 ? (" (+" + String((16 - v) * 6) + "dB gain)").c_str() : " (unity)");
    } else {
      Serial.println("[WARN] shift must be 11..16");
    }
  } else if (verb == "fmt") {
    arg.toLowerCase();
    if (arg == "i2s" || arg == "msb") {
      uint8_t nf = (arg == "msb") ? FMT_MSB : FMT_I2S;
      if (nf != cfg.fmt) {
        uint8_t prev = cfg.fmt;
        cfg.fmt = nf;
        if (!applyAudioSettingsIfIdle("fmt")) cfg.fmt = prev;
      }
      Serial.printf("[CONFIG] fmt=%s\n", fmtName(cfg.fmt));
    } else {
      Serial.println("[WARN] fmt must be i2s or msb");
    }
  } else if (verb == "ch") {
    arg.toLowerCase();
    uint8_t nc;
    if (arg == "stereo") nc = CH_STEREO;
    else if (arg == "left") nc = CH_LEFT;
    else if (arg == "right") nc = CH_RIGHT;
    else { Serial.println("[WARN] ch must be stereo, left, or right"); return; }
    if (nc != cfg.chMode) {
      uint8_t prev = cfg.chMode;
      cfg.chMode = nc;
      if (!applyAudioSettingsIfIdle("ch")) cfg.chMode = prev;
    }
    Serial.printf("[CONFIG] ch=%s\n", chName(cfg.chMode));
  } else if (verb == "rate") {
    uint32_t r = (uint32_t)arg.toInt();
    if (r == 8000 || r == 16000 || r == 22050 || r == 32000 || r == 44100) {
      if (r != cfg.sampleRate) {
        uint32_t prev = cfg.sampleRate;
        cfg.sampleRate = r;
        if (!applyAudioSettingsIfIdle("rate")) cfg.sampleRate = prev;
      }
      Serial.printf("[CONFIG] rate=%uHz\n", (unsigned)cfg.sampleRate);
    } else {
      Serial.println("[WARN] rate must be 8000/16000/22050/32000/44100");
    }
  } else if (verb == "raw") {
    arg.toLowerCase();
    if (arg == "on")  { cfg.rawDebug = true;  Serial.println("[CONFIG] raw dump ON"); }
    else if (arg == "off") { cfg.rawDebug = false; Serial.println("[CONFIG] raw dump OFF"); }
    else Serial.println("[WARN] raw must be on or off");
  } else if (verb == "upload") {
    arg.toLowerCase();
    if (arg == "on")  { cfg.uploadEnabled = true;  Serial.println("[CONFIG] upload ON"); }
    else if (arg == "off") { cfg.uploadEnabled = false; Serial.println("[CONFIG] upload OFF (files stay on SD, queued)"); }
    else Serial.println("[WARN] upload must be on or off");
  } else {
    Serial.println("[WARN] Unknown command: " + verb + " (type 'help')");
  }
}
}  // namespace

void serialConsoleBegin(const SerialConsoleDeps& deps) {
  g_deps = deps;
}

void serialConsolePoll() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (g_lineLen > 0) {
        g_lineBuf[g_lineLen] = '\0';
        handleCommand(String(g_lineBuf));
        g_lineLen = 0;
      }
    } else if (g_lineLen < sizeof(g_lineBuf) - 1) {
      g_lineBuf[g_lineLen++] = c;
    } else {
      g_lineLen = 0;
      Serial.println("[WARN] Serial line too long, discarded");
    }
  }
}
