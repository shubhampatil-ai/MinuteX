#include "Service_AwsUploader.h"
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <esp_heap_caps.h>
#include <esp_wifi.h>
#include <string.h>   // strncpy/strcmp for the memory-attribution ledger
#include <errno.h>    // errno captured on socket-write failure (retry diagnostics)

namespace {

// ===========================================================================
// Diagnostics (ENABLE_UPLOAD_PROFILING in Config.h)
//
// Everything in this block is measurement only. No upload decision, retry,
// buffer choice, timeout, header, or task parameter is influenced by it.
// With the flag at 0, the profiling wrapper is not constructed and the
// transfer path is identical to the un-instrumented version.
// ===========================================================================

// Decodes HTTPClient's negative return codes to their symbolic names, so a
// log line says what failed instead of just how.
const char* httpErrName(int code) {
  switch (code) {
    case HTTPC_ERROR_CONNECTION_REFUSED:  return "HTTPC_ERROR_CONNECTION_REFUSED";
    case HTTPC_ERROR_SEND_HEADER_FAILED:  return "HTTPC_ERROR_SEND_HEADER_FAILED";
    case HTTPC_ERROR_SEND_PAYLOAD_FAILED: return "HTTPC_ERROR_SEND_PAYLOAD_FAILED";
    case HTTPC_ERROR_NOT_CONNECTED:       return "HTTPC_ERROR_NOT_CONNECTED";
    case HTTPC_ERROR_CONNECTION_LOST:     return "HTTPC_ERROR_CONNECTION_LOST";
    case HTTPC_ERROR_NO_STREAM:           return "HTTPC_ERROR_NO_STREAM";
    case HTTPC_ERROR_NO_HTTP_SERVER:      return "HTTPC_ERROR_NO_HTTP_SERVER";
    case HTTPC_ERROR_TOO_LESS_RAM:        return "HTTPC_ERROR_TOO_LESS_RAM";
    case HTTPC_ERROR_ENCODING:            return "HTTPC_ERROR_ENCODING";
    case HTTPC_ERROR_STREAM_WRITE:        return "HTTPC_ERROR_STREAM_WRITE";
    case HTTPC_ERROR_READ_TIMEOUT:        return "HTTPC_ERROR_READ_TIMEOUT";
    default: return (code > 0) ? "(HTTP status from server)" : "(unknown client error)";
  }
}

// Running baseline for allocation deltas, so each checkpoint can attribute
// "who consumed what" rather than just reporting an absolute level.
uint32_t g_memPrevFree = 0;

// ---------------------------------------------------------------------------
// WiFi link-event tracking, active only for the duration of one PUT.
//
// WifiTask polls WiFi.status() rather than using events, so a drop+reconnect
// that completes between two polls is invisible to it — and invisible in the
// upload log, where it shows up only as an unexplained -1/-3. These counters
// are fed by a scoped WiFi.onEvent handler registered around the transfer and
// removed immediately after, so WifiTask's own behaviour is unchanged.
//
// volatile + plain integers: the handler runs on the Arduino event task, so
// this is cross-task access. Each field is written by exactly one side
// (handler writes, uploader reads at the end), and a torn read of a counter
// would at worst misreport a diagnostic — not worth a mutex on the event path.
// ---------------------------------------------------------------------------
volatile uint16_t g_wifiDropCount    = 0;
volatile uint32_t g_wifiOutageMs     = 0;
volatile uint32_t g_wifiDropStartMs  = 0;
volatile uint8_t  g_wifiLastReason   = 0;
volatile bool     g_wifiTrackActive  = false;
// Handle returned by WiFi.onEvent(); removeEvent() has no overload taking the
// two-arg callback pointer, so the handle is the only way to deregister.
network_event_handle_t g_wifiEvtHandle = 0;


void uploadWifiEventHandler(WiFiEvent_t event, WiFiEventInfo_t info) {
  if (!g_wifiTrackActive) return;
  switch (event) {
    case ARDUINO_EVENT_WIFI_STA_DISCONNECTED:
      g_wifiDropCount++;
      g_wifiLastReason = info.wifi_sta_disconnected.reason;
      g_wifiDropStartMs = millis();
      Serial.printf("[WIFI-EVENT] disconnected during upload (reason=%u)\n",
                    (unsigned)info.wifi_sta_disconnected.reason);
      break;
    case ARDUINO_EVENT_WIFI_STA_GOT_IP:
      if (g_wifiDropStartMs != 0) {
        const uint32_t outage = millis() - g_wifiDropStartMs;
        g_wifiOutageMs += outage;
        g_wifiDropStartMs = 0;
        Serial.printf("[WIFI-EVENT] reconnected after %.1f s\n", outage / 1000.0f);
      }
      break;
    default:
      break;
  }
}

// RAII arm/disarm for the link tracker. The transfer has five exit paths
// (open failure, SD read failure, socket write failure, normal completion,
// and any future one), and a handler left registered would keep firing
// against a dead upload. A guard makes the deregistration structural
// rather than something each new early-return has to remember.
struct WifiTrackGuard {
  WifiTrackGuard() {
    g_wifiDropCount = 0;
    g_wifiOutageMs = 0;
    g_wifiDropStartMs = 0;
    g_wifiLastReason = 0;
    g_wifiTrackActive = true;
    g_wifiEvtHandle = WiFi.onEvent(uploadWifiEventHandler);
  }
  ~WifiTrackGuard() { disarm(); }
  // Idempotent: the normal path calls this early to snapshot counters
  // before the report, and the destructor then no-ops.
  void disarm() {
    if (!g_wifiTrackActive && !g_wifiEvtHandle) return;
    g_wifiTrackActive = false;
    if (g_wifiEvtHandle) { WiFi.removeEvent(g_wifiEvtHandle); g_wifiEvtHandle = 0; }
  }
};

// Attribution ledger. Printing per-checkpoint deltas tells you the shape of
// the curve but leaves "which object is the biggest consumer" to be worked out
// by eye across a scrolling log. These three fields record the single worst
// drop of the run so the answer can be stated outright in the summary.
//
// Only the largest CONSUMER is tracked (negative delta); frees are ignored,
// since the question is what creates the pressure, not what relieves it.
int32_t  g_memWorstDelta = 0;
char     g_memWorstTag[24] = "(none)";
uint32_t g_memRunStartFree = 0;   // internal DRAM at the run's first checkpoint

void logMemSnapshot(const char* tag) {
#if ENABLE_UPLOAD_PROFILING
  // Total free heap alone is misleading: mbedTLS needs a few LARGE contiguous
  // blocks, so a fragmented heap can fail an allocation while still
  // reporting plenty free. largest_int is what actually predicts failure.
  const uint32_t freeInt    = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
  const uint32_t largestInt = heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL);
  // Whole-heap free (internal + any heap-capable PSRAM) alongside the
  // internal-only figure: when the two diverge, an allocation that "should"
  // have fit went to PSRAM instead of DRAM, which changes what the delta means.
  const uint32_t freeTotal  = ESP.getFreeHeap();
  // Internal-only low-water mark. ESP.getMinFreeHeap() is whole-heap and so
  // can be dominated by PSRAM movement; MALLOC_CAP_INTERNAL is the number that
  // actually corresponds to the 292-byte floor being investigated.
  const uint32_t minEverInt = heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL);

  const bool firstOfRun = (g_memPrevFree == 0);
  const int32_t delta = firstOfRun ? 0 : (int32_t)freeInt - (int32_t)g_memPrevFree;
  g_memPrevFree = freeInt;
  if (firstOfRun) g_memRunStartFree = freeInt;

  if (delta < g_memWorstDelta) {
    g_memWorstDelta = delta;
    strncpy(g_memWorstTag, tag, sizeof(g_memWorstTag) - 1);
    g_memWorstTag[sizeof(g_memWorstTag) - 1] = '\0';
  }

  Serial.printf("[MEM] %-20s dram_free=%6u  largest=%6u  delta=%+7d  heap_free=%7u  "
                "dram_min_ever=%6u  psram=%u\n",
                tag, (unsigned)freeInt, (unsigned)largestInt, (int)delta,
                (unsigned)freeTotal, (unsigned)minEverInt,
                (unsigned)ESP.getFreePsram());
#else
  (void)tag;
#endif
}

// Reseeds the delta chain only (next snapshot reports delta=0). Used at a
// phase boundary where an absolute re-anchor is wanted without discarding the
// attribution ledger accumulated so far.
void logMemBaseline() {
#if ENABLE_UPLOAD_PROFILING
  g_memPrevFree = 0;
#endif
}

// Full reset: delta chain AND the attribution ledger. Called once per upload
// so the reported worst consumer belongs to that upload alone.
void logMemResetRun() {
#if ENABLE_UPLOAD_PROFILING
  g_memPrevFree = 0;
  g_memWorstDelta = 0;
  strncpy(g_memWorstTag, "(none)", sizeof(g_memWorstTag));
#endif
}

// Names the single largest internal-DRAM consumer of this upload, mapping the
// checkpoint tag to the component that runs between the previous checkpoint
// and this one. That mapping is what turns a delta into an attribution.
const char* memWorstComponent() {
#if ENABLE_UPLOAD_PROFILING
  const char* t = g_memWorstTag;
  if (strcmp(t, "WiFiClientSecure") == 0) return "WiFiClientSecure object (pre-TLS)";
  if (strcmp(t, "setInsecure") == 0)      return "mbedTLS config / cert store";
  if (strcmp(t, "HTTPClient ctor") == 0)  return "HTTPClient object";
  if (strcmp(t, "http.begin()") == 0)     return "HTTPClient URL/String state";
  if (strcmp(t, "addHeader") == 0)        return "HTTP header Strings";
  if (strcmp(t, "before PUT") == 0)       return "pre-transfer setup";
  // The PUT is where mbedTLS actually handshakes and allocates its record
  // buffers, and where lwIP opens the socket's send/recv buffers — by far the
  // most likely winner, and the reason this whole investigation exists.
  if (strcmp(t, "after PUT") == 0)        return "TLS handshake + record buffers + lwIP socket buffers";
  if (strcmp(t, "after open") == 0)       return "SD File object + stdio buffer";
  if (strcmp(t, "after presign") == 0)    return "presign TLS session (transient)";
  return t;
#else
  return "(profiling disabled)";
#endif
}

#if ENABLE_UPLOAD_PROFILING
const char* phyModeName() {
  wifi_phy_mode_t mode;
  if (esp_wifi_sta_get_negotiated_phymode(&mode) != ESP_OK) return "unknown";
  switch (mode) {
    case WIFI_PHY_MODE_LR:   return "LR";
    case WIFI_PHY_MODE_11B:  return "11b";
    case WIFI_PHY_MODE_11G:  return "11g";
    case WIFI_PHY_MODE_HT20: return "11n-HT20";
    case WIFI_PHY_MODE_HT40: return "11n-HT40";
    case WIFI_PHY_MODE_HE20: return "11ax-HE20";
    default:                 return "other";
  }
}

void logWifiInfo() {
  Serial.printf("[WIFI-DIAG] rssi=%d dBm  channel=%d  phy=%s  status=%d  ip=%s\n",
                (int)WiFi.RSSI(), (int)WiFi.channel(), phyModeName(),
                (int)WiFi.status(), WiFi.localIP().toString().c_str());
}
#endif  // ENABLE_UPLOAD_PROFILING

#if UPLOAD_USE_DIRECT_TLS
// ===========================================================================
// Direct-TLS PUT path
//
// Replaces HTTPClient for the upload only (the presign GET still uses it —
// that request is small and latency-bound, so HTTPClient's overhead is
// irrelevant there and its response parsing is worth keeping).
//
// The reason for bypassing HTTPClient: its Stream path drains the payload in
// HTTP_TCP_TX_BUFFER_SIZE (1460 B) units — a hardcoded #define in the core,
// not settable from sketch code. Every 1460 bytes costs a readBytes() call and
// a _client->write() call, and 1460 is below the 4096 that the TLS layer
// fragments to anyway, so it splits records smaller than necessary.
//
// The chunk is UPLOAD_CHUNK_BYTES = 32KB. send_ssl_data() re-fragments any
// write to 4096 internally, so the chunk size does not change the wire pattern
// — it governs how often the loop pays its fixed per-iteration cost, the
// biggest component of which is the SD mutex acquire/release. See Config.h for
// why lowering it to 4KB was tried and reverted.
//
// What this does NOT fix: lwIP's send window is CONFIG_LWIP_TCP_SND_BUF_DEFAULT
// = 5744 bytes, so a write still blocks until ACKs drain it. Chunk size governs
// per-call overhead, not the RTT stall itself.
// ===========================================================================

// Parsed components of an https:// URL. S3 presigned URLs are long (the query
// string carries the signature) but always fit this shape.
struct ParsedUrl {
  String host;
  uint16_t port = 443;
  String path;      // path + query, i.e. everything after the host
  bool ok = false;
};

// TEST ONLY - Local upload benchmark
// One alias so the transfer loop below is compiled verbatim for both
// transports. WiFiClient and WiFiClientSecure share the NetworkClient base,
// so connect(host,port,timeout) / write() / available() / connected() /
// readStringUntil() / stop() all have identical signatures and semantics —
// the only member that does not exist on the plain client is setInsecure(),
// which is the sole call site guarded below.
#if LOCAL_UPLOAD_TEST
using UploadClient = WiFiClient;
constexpr const char* kTransportName = "plain WiFiClient / HTTP (LOCAL_UPLOAD_TEST)";
#else
using UploadClient = WiFiClientSecure;
constexpr const char* kTransportName = "direct WiFiClientSecure / HTTPS";
#endif

#if !LOCAL_UPLOAD_TEST   // TEST ONLY - Local upload benchmark: no presigned URL to parse
ParsedUrl parseHttpsUrl(const String& url) {
  ParsedUrl p;
  if (!url.startsWith("https://")) {
    Serial.printf("[ERROR] Presigned URL is not https:// — refusing to upload: %.60s\n",
                  url.c_str());
    return p;   // ok stays false; plaintext upload of audio is never acceptable
  }
  const int hostStart = 8;                       // strlen("https://")
  const int pathStart = url.indexOf('/', hostStart);
  String hostPort = (pathStart < 0) ? url.substring(hostStart)
                                    : url.substring(hostStart, pathStart);
  p.path = (pathStart < 0) ? "/" : url.substring(pathStart);

  const int colon = hostPort.indexOf(':');
  if (colon >= 0) {
    p.host = hostPort.substring(0, colon);
    p.port = (uint16_t)hostPort.substring(colon + 1).toInt();
  } else {
    p.host = hostPort;
    p.port = 443;
  }
  p.ok = p.host.length() > 0;
  return p;
}
#endif  // !LOCAL_UPLOAD_TEST

// Reads the HTTP status line and drains headers up to the blank line.
// Returns the numeric status, or a negative HTTPClient-compatible code so the
// existing retry logic (which branches on code>0 vs code<0) keeps working
// unchanged.
//
// Deliberately does NOT read the response body: S3 returns an empty body on a
// successful PUT, and on error the status code alone drives our decision. The
// connection is closed by the caller either way.
int readHttpResponse(UploadClient& client, uint32_t timeoutMs) {   // TEST ONLY - Local upload benchmark: type is an alias, body unchanged
  const uint32_t deadline = millis() + timeoutMs;

  // Wait for the status line. client.available() can legitimately be 0 for a
  // while after the payload is sent while S3 processes the object.
  while (!client.available()) {
    if (millis() > deadline) {
      Serial.println("[ERROR] Timed out waiting for S3 response headers");
      return HTTPC_ERROR_READ_TIMEOUT;
    }
    if (!client.connected()) {
      Serial.println("[ERROR] Connection closed before any response was received");
      return HTTPC_ERROR_CONNECTION_LOST;
    }
    vTaskDelay(pdMS_TO_TICKS(10));
  }

  String statusLine = client.readStringUntil('\n');
  statusLine.trim();
  // Expected: "HTTP/1.1 200 OK"
  const int sp1 = statusLine.indexOf(' ');
  if (sp1 < 0) {
    Serial.printf("[ERROR] Malformed HTTP status line: %s\n", statusLine.c_str());
    return HTTPC_ERROR_NO_HTTP_SERVER;
  }
  const int sp2 = statusLine.indexOf(' ', sp1 + 1);
  const int status = (sp2 < 0) ? statusLine.substring(sp1 + 1).toInt()
                               : statusLine.substring(sp1 + 1, sp2).toInt();

  // Drain headers to the blank line so the socket is left in a clean state.
  while (client.connected() || client.available()) {
    if (millis() > deadline) break;
    if (!client.available()) { vTaskDelay(pdMS_TO_TICKS(5)); continue; }
    String line = client.readStringUntil('\n');
    line.trim();
    if (line.length() == 0) break;   // end of headers
  }
  return status;
}
#endif  // UPLOAD_USE_DIRECT_TLS

}  // namespace

void AwsUploader::begin(SDDriver* sd) {
  sd_ = sd;
  if (!progressMutex_) progressMutex_ = xSemaphoreCreateMutex();
  if (psramFound()) {
    // Checkpoint the 6MB buffer reservation. It is expected to land entirely
    // in PSRAM, so the interesting number here is the INTERNAL delta: if it is
    // non-zero, ps_malloc spilled bookkeeping (or the whole block) into DRAM,
    // which would make the buffer a hidden DRAM consumer rather than the
    // "free" PSRAM allocation it is assumed to be.
    logMemSnapshot("pre upload-buf");
#if UPLOAD_USE_DIRECT_TLS
    // Chunk staging buffer only - the whole-file buffer is gone. This frees
    // ~6MB of PSRAM and removes the requirement that a recording fit entirely
    // in RAM before it can be uploaded. Size is now bounded only by S3.
    uploadBufSize_ = UPLOAD_CHUNK_BYTES;
#else
    uploadBufSize_ = UPLOAD_BUF_MAX_BYTES;
#endif
    uploadBuf_ = (uint8_t*)ps_malloc(uploadBufSize_);
    logMemSnapshot("upload-buf alloc");
    if (uploadBuf_) {
      Serial.printf("[BOOT] Upload buffer reserved: %u KB (static, reused every upload)\n",
                    (unsigned)(uploadBufSize_ / 1024));
      // States the compiled-in chunk size explicitly. When four firmware
      // variants are flashed to compare 16/32/48/64 KB, the serial log is the
      // only way to tell which binary is actually running on the device.
      Serial.printf("[BOOT] Upload chunk size : %u KB%s\n",
                    (unsigned)(UPLOAD_CHUNK_BYTES / 1024),
                    (UPLOAD_CHUNK_BYTES == 32UL * 1024) ? " (default)" : " (overridden)");
    } else {
      Serial.printf("[ERROR] Failed to reserve %u KB upload buffer - uploads REFUSED\n",
                    (unsigned)(uploadBufSize_ / 1024));
    }
  }
}

UploadProgress AwsUploader::progress() const {
  UploadProgress copy;
  if (!progressMutex_) return copy;
  xSemaphoreTake(progressMutex_, portMAX_DELAY);
  copy = progress_;
  xSemaphoreGive(progressMutex_);
  return copy;
}

void AwsUploader::reportProgress(size_t sent, size_t total, uint32_t elapsedMs,
                                 uint8_t attempt, bool force) {
  const uint8_t pct = (total > 0) ? (uint8_t)((uint64_t)sent * 100ULL / total) : 0;

  // Throttle: emit only when the percent has moved far enough AND enough time
  // has passed. Requiring both keeps a fast upload from flooding BLE while
  // still ticking at least once a second on a slow one. `force` bypasses it
  // for the start/end edges, which a UI needs even if they violate the gap.
  const uint32_t now = millis();
  const bool pctReady  = (pct >= lastReportedPercent_ + UPLOAD_PROGRESS_MIN_PERCENT_DELTA);
  const bool timeReady = (now - lastReportMs_ >= UPLOAD_PROGRESS_MIN_INTERVAL_MS);
  if (!force && !(pctReady && timeReady)) return;

  lastReportedPercent_ = pct;
  lastReportMs_ = now;

  // Speed/ETA from the whole-transfer average rather than an instantaneous
  // sample: per-chunk timings on this path swing wildly (a 32KB write can
  // block for a full RTT), and a jittery ETA is worse than a smooth one.
  const uint32_t kbps = (elapsedMs > 0)
      ? (uint32_t)(((uint64_t)sent * 1000ULL) / elapsedMs / 1024ULL) : 0;
  const uint32_t remaining = (total > sent) ? (uint32_t)(total - sent) : 0;
  const uint32_t eta = (kbps > 0) ? (remaining / 1024U) / kbps : 0;

  UploadProgress snap;
  snap.active     = (sent < total);
  snap.totalBytes = (uint32_t)total;
  snap.sentBytes  = (uint32_t)sent;
  snap.percent    = pct;
  snap.kbPerSec   = kbps;
  snap.etaSeconds = eta;
  snap.attempt    = attempt;

  if (progressMutex_) {
    xSemaphoreTake(progressMutex_, portMAX_DELAY);
    progress_ = snap;
    xSemaphoreGive(progressMutex_);
  }

#if ENABLE_UPLOAD_PROFILING
  Serial.printf("[UPLOAD] %u%%  %u/%u KB  %u KB/s  ETA %us\n",
                (unsigned)pct, (unsigned)(sent / 1024), (unsigned)(total / 1024),
                (unsigned)kbps, (unsigned)eta);
#endif

  // Fired last, after progress_ is committed, so a callback that calls
  // progress() sees the value it was notified about.
  if (progressCb_) progressCb_(snap);
}

#if UPLOAD_USE_DIRECT_TLS
int AwsUploader::putFileDirect(const String& filename, const String& host, uint16_t port,
                               const String& path, size_t fileSize, uint8_t attempt,
                               PutTiming* timing) {
  // --- TCP + TLS ----------------------------------------------------------
  // A fresh client per attempt. Hoisting it across attempts (as the legacy
  // path does) is pointless here because a failed PUT leaves the socket in an
  // indeterminate state — NetworkClientSecure::write() calls stop() internally
  // on error — so the next attempt must reconnect regardless.
  UploadClient client;   // TEST ONLY - Local upload benchmark: alias, see above
#if !LOCAL_UPLOAD_TEST
  client.setInsecure();
#endif
  // Inactivity limit for the payload write, not a total budget.
  //
  // MUST be set via connect(host, port, timeout_ms), NOT setTimeout():
  //   - NetworkClient/NetworkClientSecure do not override setTimeout(), so
  //     setTimeout(n) resolves to Stream::setTimeout(n) — which takes
  //     MILLISECONDS (not seconds), and writes Stream::_timeout.
  //   - NetworkClient re-declares its own `int _timeout` (NetworkClient.h:43),
  //     shadowing Stream::_timeout. write() reads the shadowing member, so
  //     setTimeout() cannot influence the socket at all: it left the
  //     constructor default of 30000 ms in place, which is the "TIMED OUT
  //     after 30002 ms" seen in the failure logs.
  //   - connect(host, port, timeout_ms) assigns NetworkClient::_timeout
  //     directly (NetworkClientSecure.cpp:129-132) and that value reaches
  //     ssl_client->socket_timeout, which is the deadline send_ssl_data()
  //     actually enforces between successive mbedtls_ssl_write() fragments.
  constexpr int32_t kSocketTimeoutMs = 90000;

  const uint32_t tConnect0 = millis();
  if (!client.connect(host.c_str(), port, kSocketTimeoutMs)) {
    Serial.printf("[ERROR] connect failed to %s:%u (%s)\n",
                  host.c_str(), (unsigned)port, kTransportName);
    return HTTPC_ERROR_CONNECTION_REFUSED;
  }
  timing->msConnect = millis() - tConnect0;
  // TLS version/ciphersuite are NOT reported: NetworkClientSecure keeps its
  // sslclient context protected, and the only public accessor
  // (getPeerCertificate) exposes the peer cert, not the negotiated suite.
  // Reaching them would mean patching the core again for a value that does
  // not affect the throughput question, so it is deliberately left out.
  timing->dramAfterHandshake = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
  timing->largestAfterHandshake = heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL);
  logMemSnapshot("after TLS connect");

  // --- Request line + headers --------------------------------------------
  // Built into one String and written once: separate print() calls would emit
  // a TLS record each, costing a handshake-sized overhead per header line.
  //
  // Host and Content-Length must match exactly what the presigned signature
  // covers. Content-Type is the only signed header the device sends (see the
  // Lambda's PutObjectCommand). Connection: close tells S3 not to keep the
  // socket alive, which is what we want — we tear it down after one PUT.
  const uint32_t tHdr0 = millis();
  String req;
  req.reserve(path.length() + host.length() + 160);
  req  = "PUT "; req += path; req += " HTTP/1.1\r\n";
  req += "Host: "; req += host;
#if LOCAL_UPLOAD_TEST
  // TEST ONLY - Local upload benchmark: RFC 9110 requires the port in Host
  // when it is not the scheme default. Production must NOT do this — S3's
  // presigned signature covers "Host: <bucket>.s3...amazonaws.com" with no
  // port, and appending one invalidates the signature (403 SignatureDoesNotMatch).
  req += ":"; req += String((uint32_t)port);
#endif
  req += "\r\n";
  req += "Content-Type: audio/wav\r\n";
  req += "Content-Length: "; req += String((uint32_t)fileSize); req += "\r\n";
  req += "Connection: close\r\n\r\n";

  if (client.write((const uint8_t*)req.c_str(), req.length()) != req.length()) {
    Serial.println("[ERROR] Failed to write request headers");
    client.stop();
    return HTTPC_ERROR_SEND_HEADER_FAILED;
  }
  timing->msHeaders = millis() - tHdr0;

  // --- Payload: chunked SD read -> socket write ---------------------------
  // The SD mutex is taken and released around EACH chunk read, so
  // RecordingTask can interleave its own SD writes between our chunks instead
  // of waiting for the whole transfer.
  // Arm WiFi link tracking for this attempt only (auto-disarms on any exit).
  WifiTrackGuard wifiGuard;

  const uint32_t tXfer0 = millis();
  size_t sent = 0;
  uint32_t lastFragMs = 0;
  // Rolling-throughput accumulation. bucketStartMs anchors the current window;
  // bucketBytes counts bytes written inside it.
  uint32_t bucketStartMs = tXfer0;
  uint32_t bucketBytes = 0;

  {
    // The File handle itself lives outside the per-chunk lock. That is safe
    // because only this task touches this handle, and the Arduino SD/FATFS
    // layer serializes actual card access through the driver; the mutex here
    // protects against RecordingTask and this task issuing overlapping
    // operations, which only happens during the read call itself.
    File f;
    {
      SDDriver::Lock lock(*sd_);
      f = SD.open(filename, FILE_READ);
    }
    if (!f) {
      Serial.printf("[ERROR] Cannot reopen file for chunked upload: %s\n", filename.c_str());
      client.stop();
      return HTTPC_ERROR_NO_STREAM;
    }

    while (sent < fileSize) {
      const size_t want = ((fileSize - sent) < uploadBufSize_) ? (fileSize - sent)
                                                               : uploadBufSize_;
      // ---- SD read (mutex held only here) ----
      const uint32_t tRd0 = millis();
      size_t got;
      {
        SDDriver::Lock lock(*sd_);
        got = f.read(uploadBuf_, want);
      }
      const uint32_t msRd = millis() - tRd0;
      timing->msSdRead += msRd;
      timing->sdBytesRead += got;
      if (msRd > timing->msSdReadMax) timing->msSdReadMax = msRd;

      if (got == 0) {
        timing->failErrno = errno;
        timing->sentBytes = sent;

        Serial.println("\n========== SD READ FAILED ==========");
        Serial.printf("sent             : %u\n", (unsigned)sent);
        Serial.printf("fileSize         : %u\n", (unsigned)fileSize);
        Serial.printf("requestedToRead  : %u\n", (unsigned)want);
        Serial.printf("got              : %u\n", (unsigned)got);
        Serial.printf("errno            : %d\n", errno);
        Serial.printf("file.size()      : %u\n", (unsigned)f.size());
        Serial.printf("file.position()  : %u\n", (unsigned)f.position());
        Serial.printf("file.available() : %u\n", (unsigned)f.available());
        Serial.printf("file.name()      : %s\n", f.name());

        Serial.printf("f bool           : %d\n", (bool)f);

        Serial.println("====================================\n");

        Serial.printf("[ERROR] SD read returned 0 at offset %u/%u\n",
                      (unsigned)sent, (unsigned)fileSize);

        f.close();

        timing->wifiDropCount = g_wifiDropCount;
        timing->wifiOutageMs = g_wifiOutageMs;
        timing->wifiLastReason = g_wifiLastReason;

        client.stop();

        return HTTPC_ERROR_NO_STREAM;
      }

      // ---- Socket write (timed independently of the SD read) ----
      // write() returns short only on error for NetworkClientSecure (it loops
      // internally in send_ssl_data until the full buffer is sent or it fails),
      // so a mismatch here is a genuine failure, not backpressure.
      const uint32_t tWr0 = millis();
      const size_t wrote = client.write(uploadBuf_, got);
      const uint32_t msWr = millis() - tWr0;
      timing->msSocketWrite += msWr;
      timing->writeCount++;
      if (wrote == got) timing->writeOkCount++;
      if (got > timing->writeBytesMax) timing->writeBytesMax = (uint32_t)got;

      // Stall accounting. A write that parks well past the nominal per-chunk
      // time is a TCP event, not a rate limit — logged inline (not just
      // aggregated) so the stall can be correlated against the [FRAG] heap
      // trace and the server-side per-second table by wall-clock offset.
      if (msWr > timing->msWriteMax) timing->msWriteMax = msWr;
      if (msWr >= UPLOAD_STALL_THRESHOLD_MS) {
        timing->writeStallCount++;
        timing->msWriteStallTotal += msWr;
#if ENABLE_UPLOAD_PROFILING
        Serial.printf("[STALL] t=%6.1fs  write of %uKB blocked %u ms  (offset %uKB)\n",
                      (millis() - tXfer0) / 1000.0f, (unsigned)(got / 1024),
                      (unsigned)msWr, (unsigned)(sent / 1024));
#endif
      }

      if (wrote != got) {
        timing->failErrno = errno;   // captured before any other libc call
        timing->sentBytes = sent;
        Serial.printf("[ERROR] Socket write short: %u/%u at offset %u/%u\n",
                      (unsigned)wrote, (unsigned)got, (unsigned)sent, (unsigned)fileSize);
        f.close();
        timing->wifiDropCount = g_wifiDropCount;
        timing->wifiOutageMs = g_wifiOutageMs;
        timing->wifiLastReason = g_wifiLastReason;
        client.stop();
        return HTTPC_ERROR_SEND_PAYLOAD_FAILED;
      }
      sent += got;
      timing->sentBytes = sent;

      // ---- rolling throughput bucketing ----
      bucketBytes += got;
      const uint32_t nowB = millis();
      if (nowB - bucketStartMs >= UPLOAD_THROUGHPUT_WINDOW_MS) {
        // Peak/trough recorded here rather than derived from the retained
        // ring, so a run long enough to overflow it still reports the true
        // extremes of the whole transfer.
        if (bucketBytes > timing->peakBucketBytes) timing->peakBucketBytes = bucketBytes;
        if (bucketBytes < timing->minBucketBytes)  timing->minBucketBytes  = bucketBytes;
        if (timing->bucketCount < UPLOAD_THROUGHPUT_MAX_BUCKETS) {
          timing->buckets[timing->bucketCount++] = bucketBytes;
        } else {
          // Keep the LAST N windows: a throughput collapse shows up at the end,
          // so dropping the oldest preserves the interesting part.
          memmove(&timing->buckets[0], &timing->buckets[1],
                  sizeof(timing->buckets[0]) * (UPLOAD_THROUGHPUT_MAX_BUCKETS - 1));
          timing->buckets[UPLOAD_THROUGHPUT_MAX_BUCKETS - 1] = bucketBytes;
          timing->bucketOverflow++;
        }
        bucketBytes = 0;
        bucketStartMs = nowB;
      }

      reportProgress(sent, fileSize, millis() - tXfer0, attempt, false);

#if ENABLE_UPLOAD_PROFILING
      const uint32_t now = millis();
      if (now - lastFragMs >= PROFILE_FRAG_SAMPLE_MS) {
        lastFragMs = now;
        Serial.printf("[FRAG] t=%5.1fs  dram_free=%6u  largest=%6u  sent=%uKB\n",
                      (now - tXfer0) / 1000.0f,
                      (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                      (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL),
                      (unsigned)(sent / 1024));
      }
#endif
      // Yield between chunks so RecordingTask (higher priority, other core)
      // and the idle task get scheduled. Without this a long upload can starve
      // the watchdog's idle-task check.
      vTaskDelay(1);
    }
    f.close();
  }
  // Flush the final partial window so the last (often most telling) seconds
  // are not silently discarded.
  if (bucketBytes > 0 && timing->bucketCount < UPLOAD_THROUGHPUT_MAX_BUCKETS) {
    timing->buckets[timing->bucketCount++] = bucketBytes;
  }

  wifiGuard.disarm();   // stop the handler before snapshotting its counters
  timing->wifiDropCount  = g_wifiDropCount;
  timing->wifiOutageMs   = g_wifiOutageMs;
  timing->wifiLastReason = g_wifiLastReason;

  timing->msTransfer = millis() - tXfer0;

  // --- Response -----------------------------------------------------------
  const uint32_t tResp0 = millis();
  const int status = readHttpResponse(client, 90000);
  timing->msResponse = millis() - tResp0;

  client.stop();
  return status;
}
#endif  // UPLOAD_USE_DIRECT_TLS

bool AwsUploader::requestPresignedUrl(const String& meetingId, const String& timestamp,
                                       String& outUrl, String& outKey) {
  String endpoint = String(PRESIGN_ENDPOINT) +
                    "?meetingId=" + meetingId +
                    "&timestamp=" + timestamp;

  // Explicit client rather than http.begin(url): that overload builds and
  // tears down its own NetworkClientSecure internally (and routes through the
  // library's deprecated-API path). setInsecure() matches the existing trust
  // posture exactly — begin(url) on an https:// URL already resolves to
  // TLSTraits(nullptr), whose verify() calls setInsecure() itself. This is a
  // lifetime change, NOT a security change.
  WiFiClientSecure client;
  client.setInsecure();

  HTTPClient http;
  http.begin(client, endpoint);
  http.setTimeout(20000);
  http.addHeader("x-api-key", DEVICE_API_KEY);

  int code = http.GET();
  vTaskDelay(1);   // yield after the blocking GET so the scheduler can service other tasks/idle
  if (code == 200) {
    String payload = http.getString();

    auto jsonString = [&payload](const char* name) -> String {
      String needle = "\"" + String(name) + "\"";
      int nIdx = payload.indexOf(needle);
      if (nIdx < 0) return "";
      int cIdx = payload.indexOf(':', nIdx + needle.length());
      if (cIdx < 0) return "";
      int s = payload.indexOf('"', cIdx + 1);
      if (s < 0) return "";
      s++;
      int e = payload.indexOf('"', s);
      if (e < 0) return "";
      return payload.substring(s, e);
    };

    outUrl = jsonString("url");
    if (outUrl.length() == 0) outUrl = jsonString("uploadUrl");
    if (outUrl.length() == 0) outUrl = jsonString("presignedUrl");
    outUrl.replace("\\/", "/");
    outKey = jsonString("key");

    if (outUrl.length() == 0) {
      Serial.printf("[ERROR] Presign returned 200 but no url field found. Raw body: %s\n", payload.c_str());
    }
    http.end();
    return outUrl.length() > 0;
  }

  String errBody = (code > 0) ? http.getString() : String("(no response — client/TLS error)");
  Serial.printf("[ERROR] Presign failed, HTTP code: %d  body: %s\n", code, errBody.c_str());
  http.end();
  return false;
}

bool AwsUploader::uploadFile(const String& filename, const String& meetingId, const String& timestamp) {
  const uint32_t tUploadStart = millis();
  logMemResetRun();
  logMemSnapshot("task entry");

  if (!uploadBuf_) {
    Serial.println("[ERROR] No upload staging buffer (PSRAM unavailable) - leaving file queued.");
    return false;
  }

  // ---- Stat the file. The SD lock is held only for open+size+close. ----
  size_t fileSize = 0;
  uint32_t msOpen = 0;
  {
    SDDriver::Lock sdLock(*sd_);
    const uint32_t tOpen0 = millis();
    File f = SD.open(filename, FILE_READ);
    msOpen = millis() - tOpen0;
    if (!f) {
      Serial.printf("[ERROR] Cannot open file for upload: %s\n", filename.c_str());
      return false;
    }
    fileSize = f.size();
    f.close();
  }
  if (fileSize == 0) {
    Serial.printf("[ERROR] File is empty, refusing to upload: %s\n", filename.c_str());
    return false;
  }
  Serial.printf("[INFO] Uploading %s (%u bytes), RSSI %d dBm\n",
                filename.c_str(), (unsigned)fileSize, (int)WiFi.RSSI());
  logMemSnapshot("after stat");

#if ENABLE_UPLOAD_PROFILING
  logWifiInfo();
#endif

  ParsedUrl url;
  uint32_t msPresign = 0;

#if LOCAL_UPLOAD_TEST
  // =========================================================================
  // TEST ONLY - Local upload benchmark
  //
  // The presign round-trip is skipped: there is no signature to obtain for a
  // local server, and including it would add a variable HTTPS handshake to a
  // measurement whose entire purpose is to isolate the payload transport.
  // msPresign therefore reports 0 in this mode, which is correct and is what
  // makes the two report cards directly comparable on "Payload transfer".
  //
  // meetingId/timestamp are unused here — they only ever fed the presign
  // query string. The file's own name carries the identity to the server.
  // =========================================================================
  (void)meetingId;
  (void)timestamp;
  url.host = LOCAL_UPLOAD_HOST;
  url.port = LOCAL_UPLOAD_PORT;
  url.path = String(LOCAL_UPLOAD_PATH);
  if (filename.length() > 0 && filename[0] != '/') url.path += '/';
  url.path += filename;
  url.ok = true;
  Serial.printf("[TEST] LOCAL_UPLOAD_TEST=1 -> http://%s:%u%s  (plain HTTP, no TLS, no presign)\n",
                url.host.c_str(), (unsigned)url.port, url.path.c_str());
#else
  String presignedUrl, s3Key;
  const uint32_t tPresign0 = millis();
  bool presignOk = requestPresignedUrl(meetingId, timestamp, presignedUrl, s3Key);
  msPresign = millis() - tPresign0;
  if (!presignOk) return false;
  Serial.printf("[INFO] Presigned OK, S3 key: %s\n", s3Key.c_str());
  logMemSnapshot("after presign");

  url = parseHttpsUrl(presignedUrl);
  if (!url.ok) return false;
#endif

  struct ExitProbe { ~ExitProbe() { logMemSnapshot("task exit"); } } exitProbe;

  // Retry policy is UNCHANGED: negative codes (no HTTP response at all) are
  // retried; any real HTTP status from S3 is final.
  constexpr int MAX_PUT_ATTEMPTS = 3;
  int code = 0;
  PutTiming timing;
  size_t largestAtStart = 0, largestAtEnd = 0;

  // Progress start edge: forced, so a UI shows 0% immediately rather than
  // waiting for the first throttle window to open.
  lastReportedPercent_ = 0;
  lastReportMs_ = 0;
  reportProgress(0, fileSize, 0, 1, true);

  for (int attempt = 1; attempt <= MAX_PUT_ATTEMPTS; attempt++) {
    largestAtStart = heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL);
    // Each attempt restarts the byte counter, so the throttle must reset too
    // or a retry would appear to jump backwards and emit nothing.
    lastReportedPercent_ = 0;
    timing = PutTiming();

    const uint32_t tSend0 = millis();
    code = putFileDirect(filename, url.host, url.port, url.path, fileSize,
                         (uint8_t)attempt, &timing);
    const uint32_t tSend1 = millis();

    largestAtEnd = heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL);
    Serial.printf("[INFO] PUT attempt %d/%d finished in %.1fs, HTTP code: %d %s (direct-tls)\n",
                  attempt, MAX_PUT_ATTEMPTS, (tSend1 - tSend0) / 1000.0f, code,
                  httpErrName(code));
    logMemSnapshot("after PUT");

#if ENABLE_UPLOAD_PROFILING
    const uint32_t msTotal = millis() - tUploadStart;
    const float xferSec = timing.msTransfer / 1000.0f;
    const float kbps = xferSec > 0.0f ? (fileSize / 1024.0f) / xferSec : 0.0f;
    // Throughput is computed from bytes that ACTUALLY MOVED, not fileSize.
    // Dividing the whole file size by the time spent moving one chunk produced
    // absurd figures on a failed upload (a real log printed "252.29 MB/s" for
    // SD after reading a single 32KB chunk).
    const uint32_t movedBytes = timing.sentBytes;
    const float sdSec = timing.msSdRead / 1000.0f;
    const float sdMbps = (sdSec > 0.0f && timing.sdBytesRead > 0)
                         ? (timing.sdBytesRead / 1048576.0f) / sdSec : 0.0f;
    const float sockSec = timing.msSocketWrite / 1000.0f;
    const float sockMbps = sockSec > 0.0f ? (movedBytes / 1048576.0f) / sockSec : 0.0f;
    // Whatever the payload loop spent outside the two measured calls: the
    // per-chunk vTaskDelay(1), progress bookkeeping, and FRAG sampling.
    const uint32_t msAccounted = timing.msSdRead + timing.msSocketWrite;
    const uint32_t msOverhead = (timing.msTransfer > msAccounted)
                                ? (timing.msTransfer - msAccounted) : 0;
    // Average over SUCCESSFUL writes only. writeCount also counts the failed
    // write, which would otherwise drag the average toward zero.
    const uint32_t avgWrite = timing.writeOkCount ? (movedBytes / timing.writeOkCount) : 0;

    // Verdict. msTransfer is 0 when the loop never completed an iteration (the
    // failure mode where a single write blocks until the socket timeout), so
    // the classification is anchored on the total time actually accounted for,
    // not on msTransfer.
    const uint32_t msBasis = msAccounted > 0 ? msAccounted : timing.msTransfer;
    const char* verdict = "indeterminate";
    if (code < 0 && movedBytes == 0 && timing.msSocketWrite > 0) {
      // Nothing moved and the socket burned the whole window: a stalled write,
      // not a slow one. With DRAM this low the cause is almost always that
      // lwIP could not allocate pbufs for the outbound segment.
      verdict = "STALLED (socket blocked, 0 bytes sent - check DRAM/pbufs)";
    } else if (msBasis > 0) {
      if (timing.msSocketWrite * 2 > msBasis)      verdict = "SOCKET-BOUND (network/lwIP/TLS)";
      else if (timing.msSdRead * 2 > msBasis)      verdict = "SD-BOUND (card/SPI)";
      else if (msOverhead * 2 > msBasis)           verdict = "OVERHEAD-BOUND (yield/scheduling)";
      else                                         verdict = "MIXED (no single dominant phase)";
    }

    Serial.println(F("================================"));
    Serial.println(F("UPLOAD ANALYSIS REPORT"));
    Serial.println(F("================================"));
    Serial.printf("Transfer path      : %s, %u KB chunks\n",
                  kTransportName, (unsigned)(uploadBufSize_ / 1024));
    Serial.printf("Destination        : %s:%u%.60s\n",
                  url.host.c_str(), (unsigned)url.port, url.path.c_str());
    Serial.printf("File Size          : %u bytes\n", (unsigned)fileSize);
    Serial.printf("Open+stat          : %u ms\n", (unsigned)msOpen);
    Serial.printf("Presigned Request  : %u ms\n", (unsigned)msPresign);
    Serial.printf("TCP+TLS connect    : %u ms\n", (unsigned)timing.msConnect);
    Serial.printf("Headers            : %u ms\n", (unsigned)timing.msHeaders);
    Serial.printf("Payload transfer   : %.2f s\n", xferSec);
    Serial.printf("  of which SD read : %u ms  (%.2f MB/s)\n",
                  (unsigned)timing.msSdRead, sdMbps);
    Serial.printf("  of which socket  : %u ms  (%.2f MB/s)\n",
                  (unsigned)timing.msSocketWrite, sockMbps);
    Serial.printf("  loop overhead    : %u ms  (yield + bookkeeping)\n",
                  (unsigned)msOverhead);
    Serial.printf("Response wait      : %u ms\n", (unsigned)timing.msResponse);
    Serial.printf("Total              : %.2f s\n", msTotal / 1000.0f);
    Serial.printf("Average Speed      : %.1f KB/s\n", kbps);
    Serial.printf("HTTP Code          : %d  %s\n", code, httpErrName(code));
    Serial.printf("WiFi               : status=%d  rssi=%d dBm  ch=%d  phy=%s\n",
                  (int)WiFi.status(), (int)WiFi.RSSI(), (int)WiFi.channel(), phyModeName());
    Serial.printf("Largest Block      : %u -> %u bytes\n",
                  (unsigned)largestAtStart, (unsigned)largestAtEnd);
    Serial.printf("DRAM min ever      : %u bytes (internal-only)\n",
                  (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL));
    Serial.printf("Largest Consumer   : %s\n", memWorstComponent());
    Serial.printf("Free PSRAM         : %u KB\n", (unsigned)(ESP.getFreePsram() / 1024));
    Serial.println(F("---- socket ----"));
    Serial.printf("Socket writes      : %u calls\n", (unsigned)timing.writeCount);
    Serial.printf("Avg / max write    : %u / %u bytes\n",
                  (unsigned)avgWrite, (unsigned)timing.writeBytesMax);
    Serial.printf("Chunk size (cfg)   : %u bytes\n", (unsigned)UPLOAD_CHUNK_BYTES);
    Serial.printf("TLS version        : %s\n",
                  timing.tlsVersion ? timing.tlsVersion : "(unknown)");
    Serial.printf("TLS cipher         : %s\n",
                  timing.tlsCipher ? timing.tlsCipher : "(unknown)");
    // Compiled-in lwIP TCP parameters. These constants come from the sdkconfig
    // the NETWORK LIBS were built with, so this block is the ground truth for
    // which build is running: the stock Arduino-IDE libs print 5744, a
    // platformio.ini custom_sdkconfig build prints 65535. Throughput is
    // bounded by (snd_buf / RTT) — see benchmark_server/FINDINGS.md.
    Serial.printf("lwIP snd buf (cfg) : %d bytes%s\n",
                  (int)CONFIG_LWIP_TCP_SND_BUF_DEFAULT,
                  (CONFIG_LWIP_TCP_SND_BUF_DEFAULT <= 8192)
                    ? "   <-- SMALL: this is the upload ceiling (stock IDE libs)"
                    : "   (tuned libs)");
    Serial.printf("lwIP TCP wnd (cfg) : %d bytes\n", (int)CONFIG_LWIP_TCP_WND_DEFAULT);
#ifdef CONFIG_LWIP_WND_SCALE
    Serial.printf("TCP window scaling : on (rcv_scale=%d)\n", (int)CONFIG_LWIP_TCP_RCV_SCALE);
#else
    // Deliberately off: scaling enlarges the window WE advertise (receive
    // direction); upload throughput is bounded by OUR send buffer instead.
    Serial.println(F("TCP window scaling : off (deliberate — no upload benefit)"));
#endif
    // The handshake is the peak DRAM consumer; if largest-free after it drops
    // near/below one MSS, lwIP cannot build an outbound segment and writes
    // stall regardless of link quality.
    // Only meaningful once the socket actually came up. On a connect failure
    // these stay 0, and an unguarded "< 8192" test then reports a phantom
    // memory exhaustion for what is really an unreachable host — a false trail
    // that costs more time than the line saves.
    if (timing.dramAfterHandshake == 0) {
      Serial.println(F("DRAM after handshake: n/a (connect never completed)"));
    } else {
      Serial.printf("DRAM after handshake: free=%u  largest=%u%s\n",
                    (unsigned)timing.dramAfterHandshake,
                    (unsigned)timing.largestAfterHandshake,
                    (timing.largestAfterHandshake < 8192)
                      ? "   <-- TOO LOW, lwIP cannot allocate pbufs" : "");
    }
    // Rolling throughput. A flat column means the link is steady; a column
    // that starts high and decays means the TCP window filled and never
    // recovered, which is the signature of a send-buffer-bound transfer.
    if (timing.bucketCount > 0) {
      Serial.println(F("---- throughput over time ----"));
      if (timing.bucketOverflow > 0) {
        Serial.printf("(showing last %u windows; %u earlier window(s) dropped)\n",
                      (unsigned)timing.bucketCount, (unsigned)timing.bucketOverflow);
      }
      const uint32_t winSec = UPLOAD_THROUGHPUT_WINDOW_MS / 1000;
      for (uint8_t bi = 0; bi < timing.bucketCount; bi++) {
        const uint32_t fromS = (uint32_t)(bi + timing.bucketOverflow) * winSec;
        const uint32_t kbs = timing.buckets[bi] / 1024U / (winSec ? winSec : 1);
        Serial.printf("  %4u-%-4u s : %6u KB/s\n",
                      (unsigned)fromS, (unsigned)(fromS + winSec), (unsigned)kbs);
      }
    }
    // Stalls and jitter. The average speed cannot tell "uniformly slow" apart
    // from "fast with periodic parks", and the two point at different layers:
    //   many stalls, high max-write   -> TCP (retransmit/zero-window) or WiFi
    //   no stalls, flat peak==avg     -> a hard rate limit (TLS cost, link,
    //                                    or the sender loop itself)
    Serial.println(F("---- stalls & jitter ----"));
    Serial.printf("Max single write   : %u ms\n", (unsigned)timing.msWriteMax);
    Serial.printf("Max single SD read : %u ms\n", (unsigned)timing.msSdReadMax);
    Serial.printf("Stalled writes     : %u of %u  (>= %u ms)\n",
                  (unsigned)timing.writeStallCount, (unsigned)timing.writeCount,
                  (unsigned)UPLOAD_STALL_THRESHOLD_MS);
    Serial.printf("Time lost to stalls: %.2f s  (%.0f%% of transfer)\n",
                  timing.msWriteStallTotal / 1000.0f,
                  timing.msTransfer ? (100.0f * timing.msWriteStallTotal / timing.msTransfer) : 0.0f);
    if (timing.peakBucketBytes > 0) {
      const uint32_t winSec2 = UPLOAD_THROUGHPUT_WINDOW_MS / 1000;
      const uint32_t divSec  = winSec2 ? winSec2 : 1;
      const uint32_t peakKbs = timing.peakBucketBytes / 1024U / divSec;
      const uint32_t minKbs  = (timing.minBucketBytes == 0xFFFFFFFFUL)
                               ? 0 : timing.minBucketBytes / 1024U / divSec;
      Serial.printf("Peak / min window  : %u / %u KB/s\n",
                    (unsigned)peakKbs, (unsigned)minKbs);
      // Peak-to-average ratio is the single most diagnostic number here: a
      // link that can hit 300 KB/s in one window and averages 26 KB/s is not
      // bandwidth-limited, it is stalling.
      Serial.printf("Peak / avg ratio   : %.1fx%s\n",
                    kbps > 0.0f ? peakKbs / kbps : 0.0f,
                    (kbps > 0.0f && peakKbs > 2.0f * kbps)
                      ? "   <-- BURSTY, link can go faster than the average" : "");
    }
    Serial.println(F("---- wifi link ----"));
    Serial.printf("Drops during PUT   : %u\n", (unsigned)timing.wifiDropCount);
    if (timing.wifiDropCount > 0) {
      Serial.printf("Total outage       : %.1f s  (last reason=%u)\n",
                    timing.wifiOutageMs / 1000.0f, (unsigned)timing.wifiLastReason);
    }
    Serial.println(F("---- verdict ----"));
    Serial.printf("Bottleneck         : %s\n", verdict);
    Serial.println(F("================================"));
#endif

    if (code == 200 || code == 201) break;
    if (code > 0) break;   // real HTTP status from S3 - retrying won't help
    // Failure diagnostics (item 5): everything needed to tell a transient
    // network drop apart from a signature/permission problem, without
    // reproducing the failure.
    Serial.println(F("[UPLOAD FAILURE]"));
    Serial.printf("  File          : %s\n", filename.c_str());
    Serial.printf("  Bytes sent    : %u / %u  (%u%%)\n",
                  (unsigned)timing.sentBytes, (unsigned)fileSize,
                  (unsigned)(fileSize ? (uint64_t)timing.sentBytes * 100ULL / fileSize : 0));
    Serial.printf("  HTTP status   : %d  %s\n", code, httpErrName(code));
    Serial.printf("  errno         : %d (%s)\n",
                  timing.failErrno, timing.failErrno ? strerror(timing.failErrno) : "none");
    Serial.printf("  WiFi          : status=%d  rssi=%d dBm  ch=%d\n",
                  (int)WiFi.status(), (int)WiFi.RSSI(), (int)WiFi.channel());
    // Distinguishes "the link dropped underneath us" from "the link was fine
    // and the failure was TLS/heap/server" — the single most useful bit when
    // triaging an intermittent -1/-3.
    Serial.printf("  WiFi drops    : %u during this attempt%s\n",
                  (unsigned)timing.wifiDropCount,
                  timing.wifiDropCount ? "  <-- LINK LOSS, not a TLS/heap fault" : "");
    if (timing.wifiDropCount > 0) {
      Serial.printf("  Outage total  : %.1f s  (last disconnect reason=%u)\n",
                    timing.wifiOutageMs / 1000.0f, (unsigned)timing.wifiLastReason);
    }
    Serial.printf("  DRAM free     : %u  largest=%u\n",
                  (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                  (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
    Serial.printf("  Phase         : %s\n",
                  timing.msConnect == 0   ? "TCP/TLS connect"
                  : timing.msHeaders == 0 ? "header send"
                  : timing.sentBytes == 0 ? "first payload write"
                  : code == HTTPC_ERROR_READ_TIMEOUT ? "awaiting server response"
                                          : "mid-payload");
    if (attempt < MAX_PUT_ATTEMPTS) {
      Serial.printf("[WARN] PUT attempt %d failed (code=%d) - retrying\n", attempt, code);
      delay(500);
    }
  }

  // Final progress edge: forced so a UI lands on exactly 100% (or the
  // last real byte count on failure) instead of stalling at the last
  // throttled sample.
  reportProgress(timing.sentBytes, fileSize, timing.msTransfer,
                 (uint8_t)MAX_PUT_ATTEMPTS, true);
  if (progressMutex_) {
    xSemaphoreTake(progressMutex_, portMAX_DELAY);
    progress_.active = false;
    xSemaphoreGive(progressMutex_);
  }

  logMemSnapshot("after cleanup");
  if (code == 200 || code == 201) {
    Serial.printf("[INFO] Upload successful: %s\n", filename.c_str());
    return true;
  }
  Serial.printf("[ERROR] Upload failed, HTTP code: %d\n", code);
  return false;
}
