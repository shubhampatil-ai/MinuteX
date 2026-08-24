/*
  Service_AwsUploader — presigned-URL handshake + PUT upload. Used
  exclusively by UploadTask. Mirrors the original requestPresignedUrl()/
  uploadFile() logic exactly: same endpoint, same header, same JSON field
  fallback order (url/uploadUrl/presignedUrl), same static 6MB PSRAM buffer
  strategy with SD-streaming fallback.
*/
#pragma once

#include <Arduino.h>
#include <SD.h>
#include "Driver_SD.h"
#include "Config.h"

// Live upload progress, published by the uploader and readable from any task
// (BleTask / SerialConsole / LedTask) to drive a UI. Copied out under a mutex
// via AwsUploader::progress(), so readers never see a torn struct.
//
// Deliberately plain data with no BLE/JSON coupling: the uploader must not
// know what renders it. Callers format it.
struct UploadProgress {
  bool     active      = false;   // true only while a PUT is in flight
  uint32_t totalBytes  = 0;
  uint32_t sentBytes   = 0;
  uint8_t  percent     = 0;
  uint32_t kbPerSec    = 0;       // instantaneous-ish average over the transfer
  uint32_t etaSeconds  = 0;       // 0 when not yet estimable
  uint8_t  attempt     = 0;       // 1-based; which retry this is
};

class AwsUploader {
public:
  // Reserves the static PSRAM upload buffer once (idempotent). Logs the
  // same messages as the original setup() block.
  void begin(SDDriver* sd);

  // Full upload of one SD file: presign -> PUT. Returns true on HTTP
  // 200/201. On failure, the caller (UploadTask) is responsible for
  // queuing it via PendingQueue, exactly like the original uploadFile()
  // caller did.
  bool uploadFile(const String& filename, const String& meetingId, const String& timestamp);

  // Thread-safe snapshot of the in-flight upload, for UI/BLE. Returns a
  // struct with active=false when no upload is running.
  UploadProgress progress() const;

  // Optional sink invoked when progress crosses the throttle thresholds
  // (UPLOAD_PROGRESS_MIN_PERCENT_DELTA / _MIN_INTERVAL_MS). Runs on
  // UploadTask, so the callback must not block — publish an event or set a
  // flag, do not do BLE I/O inline.
  using ProgressCallback = void (*)(const UploadProgress&);
  void setProgressCallback(ProgressCallback cb) { progressCb_ = cb; }

private:
  bool requestPresignedUrl(const String& meetingId, const String& timestamp,
                            String& outUrl, String& outKey);

#if UPLOAD_USE_DIRECT_TLS
  // Timing + socket statistics filled in by putFileDirect().
  //
  // msSdRead and msSocketWrite are measured independently rather than one
  // being derived from the other, because the loop also spends time in
  // vTaskDelay(1) and progress bookkeeping. Deriving socket time as
  // (transfer - sdRead) would silently fold that overhead into the socket
  // figure and overstate the network's share — which is exactly the number
  // this whole exercise exists to get right.
  struct PutTiming {
    uint32_t msConnect     = 0;   // TCP + TLS handshake
    uint32_t msHeaders     = 0;   // request line + headers write
    uint32_t msTransfer    = 0;   // whole payload loop, wall clock
    uint32_t msSdRead      = 0;   // summed time inside f.read()
    uint32_t msSocketWrite = 0;   // summed time inside client.write()
    uint32_t msResponse    = 0;   // last byte written -> status parsed

    // Socket write statistics (item 4).
    uint32_t writeCount    = 0;   // all write attempts, including the failed one
    uint32_t writeOkCount  = 0;   // writes that fully succeeded (avg is over these)
    uint32_t writeBytesMax = 0;
    uint32_t sentBytes     = 0;   // survives a failure, for retry diagnostics
    uint32_t sdBytesRead   = 0;   // bytes actually read from SD (throughput basis)

    // Stall / jitter detection. Averages cannot distinguish "uniformly slow"
    // from "fast with periodic multi-second parks", and those two have
    // completely different causes — the first is a rate limit (TLS cost, link
    // rate, sender loop), the second is a TCP event (retransmit timeout, zero
    // window, WiFi power-save beacon miss). These fields separate them.
    uint32_t msWriteMax       = 0;  // longest single client.write()
    uint32_t msSdReadMax      = 0;  // longest single f.read() — SD contention
    uint32_t writeStallCount  = 0;  // writes >= UPLOAD_STALL_THRESHOLD_MS
    uint32_t msWriteStallTotal= 0;  // summed time inside those stalled writes
    // Peak/trough tracked outside the bucket ring so they survive
    // bucketOverflow dropping the earliest windows.
    uint32_t peakBucketBytes  = 0;
    uint32_t minBucketBytes   = 0xFFFFFFFFUL;

    // DRAM state immediately after the TLS handshake - the peak-consumption
    // moment. When largestAfterHandshake falls near one MSS, lwIP cannot build
    // an outbound segment and writes stall until the socket timeout.
    uint32_t dramAfterHandshake    = 0;
    uint32_t largestAfterHandshake = 0;

    // Rolling throughput: bytes transferred within each
    // UPLOAD_THROUGHPUT_WINDOW_MS window. Fixed-size so nothing allocates
    // mid-transfer. bucketOverflow counts windows dropped past capacity.
    uint32_t buckets[UPLOAD_THROUGHPUT_MAX_BUCKETS] = {0};
    uint8_t  bucketCount    = 0;
    uint16_t bucketOverflow = 0;

    // WiFi link events observed during THIS attempt (item 3). Registered as a
    // scoped WiFi.onEvent handler for the duration of the PUT only, so
    // WifiTask's own polling behaviour is untouched.
    uint16_t wifiDropCount   = 0;
    uint32_t wifiOutageMs    = 0;   // summed time between drop and reconnect
    uint8_t  wifiLastReason  = 0;   // esp_wifi disconnect reason code

    // Captured at failure time (item 5). errno is read immediately after the
    // failing call, before any other libc call can overwrite it.
    int      failErrno     = 0;
    const char* tlsVersion = nullptr;
    const char* tlsCipher  = nullptr;
  };

  // One PUT attempt over a raw TLS socket, streaming the file from SD in
  // UPLOAD_CHUNK_BYTES units. Returns an HTTP status (>0) or a negative
  // HTTPC_ERROR_* code, matching what the retry loop already expects.
  //
  // Opens and closes the SD file itself, taking the SD mutex around each
  // individual chunk read rather than for the whole transfer.
  int putFileDirect(const String& filename, const String& host, uint16_t port,
                    const String& path, size_t fileSize, uint8_t attempt,
                    PutTiming* timing);
#endif

  // Updates progress_ under mutex and fires progressCb_ if the throttle
  // thresholds are met. Called once per chunk from the transfer loop.
  void reportProgress(size_t sent, size_t total, uint32_t elapsedMs,
                      uint8_t attempt, bool force);

  SDDriver* sd_ = nullptr;
  // Chunk staging buffer (direct path) or whole-file buffer (legacy path).
  // Allocated once in begin(); null if PSRAM unavailable.
  uint8_t* uploadBuf_ = nullptr;
  size_t   uploadBufSize_ = 0;

  // Progress state. Guarded by progressMutex_ because UploadTask writes it
  // while BleTask/SerialConsole may read it concurrently.
  mutable SemaphoreHandle_t progressMutex_ = nullptr;
  UploadProgress   progress_;
  ProgressCallback progressCb_ = nullptr;
  uint8_t  lastReportedPercent_ = 0;
  uint32_t lastReportMs_ = 0;
};
