#include "Service_PendingQueue.h"

void PendingQueue::add(const String& filename, const String& meetingId, const String& timestamp) {
  SDDriver::Lock lock(*sd_);
  File f = SD.open("/pending.txt", FILE_APPEND);
  if (f) {
    f.println(filename + "|" + meetingId + "|" + timestamp);
    f.close();
    Serial.printf("[INFO] Queued for later upload: %s\n", filename.c_str());
  } else {
    Serial.println("[ERROR] Could not open pending.txt to queue file");
  }
}

bool PendingQueue::exists() {
  SDDriver::Lock lock(*sd_);
  return SD.exists("/pending.txt");
}

int PendingQueue::count() {
  SDDriver::Lock lock(*sd_);
  if (!SD.exists("/pending.txt")) return 0;
  File f = SD.open("/pending.txt", FILE_READ);
  if (!f) return 0;
  int n = 0;
  while (f.available()) {
    String line = f.readStringUntil('\n');
    line.trim();
    if (line.length() > 0) n++;
  }
  f.close();
  return n;
}

std::vector<PendingQueue::Entry> PendingQueue::readAll() {
  std::vector<Entry> out;
  SDDriver::Lock lock(*sd_);
  if (!SD.exists("/pending.txt")) return out;
  File f = SD.open("/pending.txt", FILE_READ);
  if (!f) return out;

  while (f.available()) {
    String line = f.readStringUntil('\n');
    line.trim();
    if (line.length() == 0) continue;

    int p1 = line.indexOf('|');
    int p2 = line.indexOf('|', p1 + 1);
    if (p1 < 0 || p2 < 0) {
      Serial.printf("[WARN] Skipping malformed pending line: %s\n", line.c_str());
      continue;
    }
    Entry e;
    e.filename  = line.substring(0, p1);
    e.meetingId = line.substring(p1 + 1, p2);
    e.timestamp = line.substring(p2 + 1);

    if (!SD.exists(e.filename)) {
      Serial.printf("[WARN] Pending entry dropped (file missing on SD): %s\n", e.filename.c_str());
      continue;
    }
    out.push_back(e);
  }
  f.close();
  return out;
}

void PendingQueue::rewrite(const std::vector<String>& remainingLines) {
  SDDriver::Lock lock(*sd_);
  Serial.printf("[SD-AUDIT] PendingQueue rewriting /pending.txt (%u entries kept)\n",
                (unsigned)remainingLines.size());
  SD.remove("/pending.txt");
  for (const auto& l : remainingLines) {
    File af = SD.open("/pending.txt", FILE_APPEND);
    if (af) { af.println(l); af.close(); }
  }
}
