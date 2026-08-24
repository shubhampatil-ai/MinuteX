/*
  Service_PendingQueue — /pending.txt management. Used exclusively by
  UploadTask. Mirrors the original addToPendingQueue()/pendingQueueExists()/
  pendingCount() logic and file format (filename|meetingId|timestamp per
  line) exactly, so an existing /pending.txt from a prior firmware version
  keeps working unchanged.
*/
#pragma once

#include <Arduino.h>
#include <SD.h>
#include <vector>
#include "Driver_SD.h"

class PendingQueue {
public:
  void begin(SDDriver* sd) { sd_ = sd; }

  void add(const String& filename, const String& meetingId, const String& timestamp);
  bool exists();
  int  count();

  // Reads out all queued entries as raw "filename|meetingId|timestamp" lines,
  // for UploadTask to iterate + retry. Returns entries still pending after
  // the caller-provided attempt function decides per-line success; the
  // caller passes a lambda-like callable via function pointer + context to
  // keep this header dependency-light (avoids <functional> heap use on a
  // hot path).
  //
  // Simpler contract used by UploadTask: retrieve all lines, let the caller
  // attempt uploads, then call rewrite() with the lines that should remain.
  struct Entry {
    String filename;
    String meetingId;
    String timestamp;
  };

  // Returns parsed entries (skips malformed / SD-missing lines, matching the
  // original's behavior of dropping missing-file entries silently from the
  // rewritten queue).
  std::vector<Entry> readAll();

  // Overwrites pending.txt with exactly these remaining entries (as raw
  // "filename|meetingId|timestamp" lines), matching original's
  // remove-all-then-rewrite-remaining behavior.
  void rewrite(const std::vector<String>& remainingLines);

private:
  SDDriver* sd_ = nullptr;
};
