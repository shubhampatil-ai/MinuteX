#include "Task_Upload.h"
#include "Config.h"
#include "Task_Recording.h"
#include "Service_BleProtocol.h"
#include <WiFi.h>
#include <SD.h>
#include <vector>

extern BleProtocol g_bleProtocol;   // for ensureAdvertising() between upload items (defined in ESP_32.ino)
extern void ledSetUploadBlinking(bool on);   // Task_Led — drives the green upload blink

namespace {
UploadTaskDeps g_deps;
QueueHandle_t g_inbox = nullptr;
AwsUploader g_uploader;
PendingQueue g_pendingQueue;
TaskHandle_t g_handle = nullptr;

unsigned long g_lastPendingRetryMs = 0;

// Mirrors original processPendingQueue(): attempts every queued entry,
// removes successes from SD, rewrites pending.txt with only the
// still-failing entries. Calls ensureAdvertising() between attempts so a
// backlog of back-to-back HTTP calls doesn't starve BLE.
int processPendingQueue() {
  auto entries = g_pendingQueue.readAll();
  int successCount = 0;
  std::vector<String> remaining;

  for (auto& e : entries) {
    bool ok = g_uploader.uploadFile(e.filename, e.meetingId, e.timestamp);
    g_deps.stateMgr->setError(!ok);   // matches original uploadFile() setting ledErrorFlag on every call
    if (!ok) {
      remaining.push_back(e.filename + "|" + e.meetingId + "|" + e.timestamp);
    } else {
#if LOCAL_UPLOAD_TEST
      // TEST ONLY - Local upload benchmark: the recording is KEPT on SD so the
      // identical file can be re-uploaded to S3 for the A/B comparison. A
      // different file has a different size and SD layout and would invalidate
      // the result. The queue entry is still cleared (it is not pushed to
      // `remaining`), so this does not re-upload in a loop.
      Serial.printf("[SD-AUDIT] TEST MODE: keeping %s on SD (benchmark, not deleted)\n",
                    e.filename.c_str());
      successCount++;
#else
      SDDriver::Lock lock(*g_deps.sd);
      // Deletion audit: every SD.remove() of a recording is logged so a
      // vanished file can be attributed to (or cleared of) this path.
      Serial.printf("[SD-AUDIT] UploadTask/pendingQueue removing %s (upload succeeded)\n",
                    e.filename.c_str());
      SD.remove(e.filename);
      successCount++;
#endif
    }
    g_bleProtocol.ensureAdvertising();
    vTaskDelay(1);   // yield between queued items so a large backlog can't starve the scheduler
  }
  g_pendingQueue.rewrite(remaining);
  return successCount;
}

// Mirrors original runUploadCycle() exactly.
void runUploadCycle(bool haveCurrent) {
  bool pushedToAws = false;

  if (haveCurrent) {
    const RecordingResult& rec = getLastRecordingResult();
    const AudioConfig& cfg = g_deps.configMgr->get();

    if (!cfg.uploadEnabled) {
      g_pendingQueue.add(rec.filename, rec.meetingId, rec.timestamp);
      Serial.println("[INFO] Upload disabled — file kept on SD");
    } else if (WiFi.status() == WL_CONNECTED) {
      ledSetUploadBlinking(true);
      bool ok = g_uploader.uploadFile(rec.filename, rec.meetingId, rec.timestamp);
      g_deps.stateMgr->setError(!ok);   // matches original uploadFile() setting ledErrorFlag directly
      if (!ok) {
        g_pendingQueue.add(rec.filename, rec.meetingId, rec.timestamp);
      } else {
#if LOCAL_UPLOAD_TEST
        // TEST ONLY - Local upload benchmark: keep the .wav on SD so the same
        // file can be replayed against S3 with LOCAL_UPLOAD_TEST 0. It is not
        // queued either, so it will not upload again on its own.
        Serial.printf("[SD-AUDIT] TEST MODE: keeping %s on SD (benchmark, not deleted)\n",
                      rec.filename.c_str());
        pushedToAws = true;
#else
        SDDriver::Lock lock(*g_deps.sd);
        Serial.printf("[SD-AUDIT] UploadTask/current removing %s (upload succeeded)\n",
                      rec.filename.c_str());
        SD.remove(rec.filename);
        pushedToAws = true;
#endif
      }
      g_bleProtocol.ensureAdvertising();  // the HTTP PUT above can starve BLE — recover before the queue loop
      int pendingOk = processPendingQueue();
      if (pendingOk > 0) pushedToAws = true;
      ledSetUploadBlinking(false);
    } else {
      g_pendingQueue.add(rec.filename, rec.meetingId, rec.timestamp);
      Serial.println("[INFO] No WiFi — queued instantly, will auto-upload when WiFi returns");
    }
  } else {
    // Pure pending-queue retry (no just-finished recording waiting).
    Serial.println("[INFO] WiFi available — retrying pending uploads...");
    ledSetUploadBlinking(true);
    int ok = processPendingQueue();
    ledSetUploadBlinking(false);
    if (ok > 0) pushedToAws = true;
  }

  g_deps.stateMgr->setPendingCount(g_pendingQueue.count());

  if (pushedToAws) {
    g_deps.stateMgr->setAppState(AppState::UPLOAD_OK);
    Serial.println(haveCurrent ? "[STATE] Upload confirmed on AWS — solid green 5s."
                                : "[STATE] Pending upload(s) confirmed on AWS — solid green 5s.");
  } else if (haveCurrent) {
    g_deps.stateMgr->setAppState(AppState::IDLE);
    Serial.println("[STATE] Back to idle.");
  }
}

void handleEvent(const Event& evt) {
  switch (evt.type) {
    case EventType::RECORDING_SAVED:
      g_deps.stateMgr->setAppState(AppState::UPLOADING);
      runUploadCycle(true);
      break;
    case EventType::UPLOAD_RETRY_DUE:
      if (g_deps.stateMgr->getAppState() == AppState::IDLE) {
        runUploadCycle(false);
      }
      break;
    default:
      break;
  }
}

void uploadTaskTrampoline(void* /*param*/) {
  for (;;) {
    // Periodic pending-queue retry. The original firmware called
    // retryPendingUploadsIfDue() from EVERY loop() iteration (orig line
    // 1407) in addition to the WiFi-connect edge; the throttle lives
    // inside the function (PENDING_RETRY_INTERVAL_MS = 30s), so calling it
    // on this task's 200ms tick reproduces the original cadence exactly.
    // Without this, a failed upload would sit in pending.txt untouched
    // until the next WiFi *reconnect* — never retried while the link
    // stayed up.
    requestPendingRetryIfDue();

    Event evt;
    if (xQueueReceive(g_inbox, &evt, pdMS_TO_TICKS(200)) == pdTRUE) {
      handleEvent(evt);
      // Extra yield after handling any event: if the SD card is in a bad
      // state (e.g. right after an abrupt reset), SD.open()/SD.exists()
      // calls inside AwsUploader/PendingQueue can each take far longer
      // than usual, and their SDDriver::Lock sections don't have their own
      // internal yields the way the network calls in Service_AwsUploader
      // do. This guarantees the scheduler gets a slice back on core 0
      // right after any upload-cycle attempt, however long it took.
      vTaskDelay(1);
    }
    g_deps.taskMgr->heartbeat(g_handle);
  }
}
}  // namespace

void startUploadTask(UploadTaskDeps deps) {
  g_deps = deps;
  g_inbox = deps.bus->subscribe();
  g_uploader.begin(deps.sd);
  g_pendingQueue.begin(deps.sd);

  // 8KB, cut down from 24KB on measured evidence.
  //
  // The 24KB was speculative headroom "for future features". A field log then
  // showed the cost: [HEALTH] reported UploadTask stack unused(min)=20152 of
  // 24576 — i.e. ~4.4KB actually used and ~20KB of internal DRAM sitting idle
  // — while the TLS handshake on the very same task was failing to allocate,
  // leaving dram_free=11864 / largest=4596 and min-ever 1396 bytes. Uploads
  // died with HTTPC_ERROR_SEND_PAYLOAD_FAILED because lwIP could not get pbufs.
  //
  // Task stacks are internal DRAM, the exact pool mbedTLS and lwIP compete
  // for. 8KB leaves ~3.6KB headroom over the measured high-water mark and
  // returns ~16KB to the handshake. Re-check [HEALTH] after any change that
  // deepens the call stack on this task.
  xTaskCreatePinnedToCore(
      uploadTaskTrampoline, "UploadTask", 8192, nullptr,
      1 /*priority*/, &g_handle, 0 /*core*/);
  // 45s, up from 15s. A single client.write() can legitimately block for the
  // full 30s socket timeout, so a 15s heartbeat window guaranteed a burst of
  // "heartbeat stale" warnings on every slow or failing upload — noise that
  // buries the real diagnostics. 45s still catches a genuinely wedged task
  // while sitting above the longest legal blocking call on this path.
  deps.taskMgr->registerTask("UploadTask", g_handle, 45000);
}

void benchmarkSdRead(const String& filename) {
  if (g_deps.stateMgr == nullptr || g_deps.sd == nullptr) {
    Serial.println("[BENCH][SD] Upload subsystem not ready yet");
    return;
  }
  AppState st = g_deps.stateMgr->getAppState();
  if (st == AppState::UPLOAD_PENDING || st == AppState::UPLOADING) {
    Serial.println("[BENCH][SD] An upload is in progress (SD busy) — try again in a moment");
    return;
  }

  SDDriver::Lock lock(*g_deps.sd);
  File f = SD.open(filename, FILE_READ);
  if (!f) {
    Serial.printf("[BENCH][SD] Cannot open %s\n", filename.c_str());
    return;
  }
  const size_t size = f.size();

  // Same chunk size HTTPClient pulls per iteration when streaming a file, so
  // this reproduces the upload path's SD access pattern rather than an
  // idealised bulk read.
  static uint8_t buf[BENCH_SD_CHUNK_BYTES];
  size_t total = 0;
  const uint32_t t0 = millis();
  while (f.available()) {
    size_t n = f.read(buf, sizeof(buf));
    if (n == 0) break;
    total += n;
  }
  const uint32_t ms = millis() - t0;
  f.close();

  const float sec = ms / 1000.0f;
  const float mb = total / 1048576.0f;
  Serial.println(F("[BENCH][SD]"));
  Serial.printf("  File       : %s\n", filename.c_str());
  Serial.printf("  Size       : %.2f MB (%u bytes)\n", mb, (unsigned)total);
  Serial.printf("  Chunk      : %u bytes\n", (unsigned)BENCH_SD_CHUNK_BYTES);
  Serial.printf("  Read Time  : %.2f s\n", sec);
  Serial.printf("  Read Speed : %.2f MB/s (%.1f KB/s)\n",
                sec > 0.0f ? mb / sec : 0.0f,
                sec > 0.0f ? (total / 1024.0f) / sec : 0.0f);
  if (total != size) {
    Serial.printf("  [WARN] short read: %u of %u bytes\n",
                  (unsigned)total, (unsigned)size);
  }
}

void printPendingRecordings() {
  if (g_deps.stateMgr == nullptr || g_deps.sd == nullptr) {
    Serial.println("[PENDING] Upload subsystem not ready yet");
    return;
  }

  // uploadFile() holds the SD mutex for the entire transfer, including the
  // multi-second HTTPS PUT. Reading the queue mid-upload would therefore
  // block the serial console until that upload finished — report and bail
  // instead of appearing hung.
  AppState st = g_deps.stateMgr->getAppState();
  if (st == AppState::UPLOAD_PENDING || st == AppState::UPLOADING) {
    Serial.println("[PENDING] An upload is in progress (SD busy) — try again in a moment");
    return;
  }

  // readAll() acquires the SD lock itself, so it must NOT be called while
  // holding it — SDDriver's mutex is non-recursive and nesting deadlocks.
  // Entries whose .wav is missing from SD are skipped by readAll(), which
  // logs a [WARN] for each, so orphaned queue lines still surface here.
  auto entries = g_pendingQueue.readAll();

  if (entries.empty()) {
    Serial.println("[PENDING] No pending recordings");
    return;
  }

  Serial.printf("[PENDING] %u pending recording(s):\n", (unsigned)entries.size());
  uint32_t totalBytes = 0;
  {
    SDDriver::Lock lock(*g_deps.sd);
    for (auto& e : entries) {
      File f = SD.open(e.filename, FILE_READ);
      if (f) {
        uint32_t sz = f.size();
        f.close();
        totalBytes += sz;
        Serial.printf("[PENDING]   %-26s %9u bytes\n", e.filename.c_str(), (unsigned)sz);
      } else {
        Serial.printf("[PENDING]   %-26s (size unavailable — open failed)\n", e.filename.c_str());
      }
    }
  }
  Serial.printf("[PENDING] Total: %u recording(s), %u bytes\n",
                (unsigned)entries.size(), (unsigned)totalBytes);
}

void requestPendingRetryIfDue() {
  // WifiTask is created before UploadTask in setup() and can reach the
  // WiFi-connect edge (which calls this) before startUploadTask() has
  // populated g_deps — dereferencing it then would hard-fault at boot.
  // Nothing is pending that early anyway, so bailing out is correct.
  if (g_deps.stateMgr == nullptr || g_deps.configMgr == nullptr || g_deps.bus == nullptr) return;

  // Guard order matches the original retryPendingUploadsIfDue() exactly:
  // the IDLE check comes FIRST, before the throttle timestamp is consumed,
  // so a tick that arrives while recording/uploading doesn't burn the 30s
  // retry window.
  if (g_deps.stateMgr->getAppState() != AppState::IDLE) return;
  const AudioConfig& cfg = g_deps.configMgr->get();
  if (!cfg.uploadEnabled) return;
  if (millis() - g_lastPendingRetryMs < PENDING_RETRY_INTERVAL_MS) return;
  g_lastPendingRetryMs = millis();

  if (!g_pendingQueue.exists()) return;
  if (WiFi.status() != WL_CONNECTED) return;

  g_deps.bus->publish(Event(EventType::UPLOAD_RETRY_DUE));
}
