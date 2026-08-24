/*
  Config.h — fixed configuration: pins, protocol constants, timing, and
  product identity. Values are copied verbatim from the pre-refactor
  ESP_32.ino v1.7.4 monolith — DO NOT change any value here without also
  updating the corresponding mobile app / backend contract.
*/
#pragma once

#include <Arduino.h>
#include "driver/i2s_std.h"

// ---------------- Fixed config ----------------
static const char* const FW_VERSION       = "1.7.4";
// Backend handshake endpoint (deployed HTTP API, ap-south-1 / Mumbai).
//
// Moved from eu-north-1 (Stockholm) on 2026-08-01. Upload throughput on this
// device is (lwIP send buffer / RTT) = 5744 bytes / RTT, so the region IS the
// throughput: measured TCP-connect RTT fell from 167-224 ms to 19 ms, which
// lifts the ceiling from ~26 KB/s to ~250 KB/s. See MIGRATION_ap-south-1.md.
//
// The old Stockholm stack is still running and can be reverted to by restoring
// this one line (its bucket, tables and Lambdas were left untouched).
static const char* const PRESIGN_ENDPOINT = "https://q87zfn5vyj.execute-api.ap-south-1.amazonaws.com/get-upload-url";
// Device API key — backend maps key -> deviceId.
static const char* const DEVICE_API_KEY   = "dk_live_QxTf0_URUY9UQrOs1SiAzNd7jN8giqgX6HjlDZ6h-S0";

// BLE UUIDs (fixed for this product — the app filters on SERVICE_UUID)
#define BLE_SERVICE_UUID  "5f47a3c0-1e0b-4f8a-9a1d-8f0c2e7b4d10"
#define BLE_CONTROL_UUID  "5f47a3c1-1e0b-4f8a-9a1d-8f0c2e7b4d10"   // write
#define BLE_STATUS_UUID   "5f47a3c2-1e0b-4f8a-9a1d-8f0c2e7b4d10"   // read+notify
constexpr uint16_t BLE_ATT_MTU = 185;

// mic
constexpr gpio_num_t PIN_I2S_WS  = GPIO_NUM_7;
constexpr gpio_num_t PIN_I2S_SCK = GPIO_NUM_4;
constexpr gpio_num_t PIN_I2S_SD  = GPIO_NUM_15;

// sd card
constexpr int PIN_SD_CS   = 10;
constexpr int PIN_SD_MOSI = 11;
constexpr int PIN_SD_SCK  = 12;
constexpr int PIN_SD_MISO = 13;

// buttons
constexpr int PIN_BTN_START = 8;
constexpr int PIN_BTN_STOP  = 14;
constexpr int PIN_BATT_ADC  = 1;

// status LED — onboard WS2812. The ESP32-S3 Arduino variant ALREADY defines
// PIN_RGB_LED (as a macro = 48) in pins_arduino.h, so we must NOT redefine it
// here — doing so is a compile error. We only provide a fallback for boards
// whose variant doesn't define it.
#ifndef PIN_RGB_LED
#define PIN_RGB_LED 48
#endif

// dont change
constexpr uint32_t I2S_DMA_DESC_NUM  = 6;
constexpr uint32_t I2S_DMA_FRAME_NUM = 512;
constexpr float    LOW_BATT_VOLTAGE  = 3.3f;

constexpr unsigned long DEBOUNCE_MS               = 250;
constexpr unsigned long SAVING_BLINK_INTERVAL_MS  = 400;
constexpr unsigned long SAVING_DURATION_MS        = 1000;
constexpr unsigned long UPLOAD_OK_SOLID_MS        = 5000;
constexpr unsigned long UPLOAD_BLINK_MS           = 300;
constexpr unsigned long PENDING_RETRY_INTERVAL_MS = 30000;
constexpr unsigned long WIFI_KICK_INTERVAL_MS      = 20000;
constexpr uint32_t      RAW_DEBUG_EVERY_N_CHUNKS  = 50;
constexpr int           MAX_WIFI_NETWORKS         = 3;
constexpr unsigned long WIFI_ATTEMPT_TIMEOUT_MS   = 12000;  // per-attempt cap
constexpr int            LOW_BATT_PERCENT         = 15;      // amber warning threshold

// ===========================================================================
// TEST ONLY - Local upload benchmark
//
// Temporary diagnostic switch. Swaps ONLY the transport of the payload PUT:
//
//   1 -> plain WiFiClient / HTTP / local laptop server (benchmark_server/)
//   0 -> production WiFiClientSecure / HTTPS / presigned AWS S3   (DEFAULT)
//
// Everything else is byte-for-byte the production path: same SD open, same
// chunked read loop, same UPLOAD_CHUNK_BYTES, same progress reporting, same
// timing/memory/WiFi instrumentation, same retry policy. The only differences
// under the flag are (a) the socket class, (b) the presign round-trip is
// skipped because there is nothing to sign, and (c) Host: carries the port.
//
// Running the same file through both settings isolates the bottleneck:
//   local ~= S3        -> ESP32 firmware / WiFi / lwIP (TLS and S3 exonerated)
//   local >> S3        -> TLS cost and/or Internet RTT to S3
//
// TO REVERT: set this to 0. Nothing else needs touching.
// ===========================================================================
#define LOCAL_UPLOAD_TEST 0

#if LOCAL_UPLOAD_TEST
// TEST ONLY - Local upload benchmark: destination of the diagnostic PUT.
// Set LOCAL_UPLOAD_HOST to the Windows laptop's LAN IP (`ipconfig` -> IPv4
// Address of the adapter on the same WiFi network as the ESP32).
//
// This is a DHCP address and it DOES move — it was .3 earlier in the same
// session it is now .193. A stale value here surfaces as
// "[ERROR] connect failed to <ip>:8000", which looks identical to a firewall
// block. Re-check against the server's own "Reachable :" banner before every
// flash, or give the laptop a static lease on the router.
static const char* const LOCAL_UPLOAD_HOST = "192.168.1.3";
constexpr uint16_t       LOCAL_UPLOAD_PORT = 8000;
// Base path. The recording's filename is appended, so the server stores it
// under its real name (e.g. PUT /upload/rec_1785405103.wav).
static const char* const LOCAL_UPLOAD_PATH = "/upload";
#endif  // LOCAL_UPLOAD_TEST

// ===========================================================================
// Wi-Fi power save (throughput experiment — Step 1 of the bottleneck plan)
//
//   1 -> esp_wifi_set_ps(WIFI_PS_NONE) on every connect edge
//   0 -> ESP-IDF default (WIFI_PS_MIN_MODEM)
//
// WHY: with modem sleep on, the radio parks between DTIM beacons and only
// wakes to service traffic on the beacon boundary. Measured effect on this
// device: ICMP replies of 958 ms then 32-64 ms, and a median inter-read gap at
// the receiving server of 60-110 ms — one beacon interval. Since TCP
// throughput here is (lwIP send buffer / RTT) = 5744 / RTT, that latency IS
// the bottleneck on a LAN: 5744 / 0.110 = 52 KB/s, which is what was measured.
//
// EXPECTED: large gain on the LAN (RTT 110ms -> ~5ms). This mattered far less
// when S3 was in eu-north-1, because ~200 ms of Internet RTT swamped the beacon
// latency. Now that the bucket is in ap-south-1 (RTT ~19 ms) the beacon wait is
// the DOMINANT term, so disabling power save is worth more than it used to be.
// See FINDINGS.md and MIGRATION_ap-south-1.md.
//
// COST: this is NOT free on a battery device. WIFI_PS_NONE keeps the receive
// chain powered continuously, roughly 4x the average idle current of
// MIN_MODEM. Acceptable while benchmarking; for production, prefer scoping it
// to the upload window (disable on entry to uploadFile(), restore on exit)
// rather than leaving it on for the whole session.
// ===========================================================================
#define WIFI_DISABLE_POWER_SAVE 1

// ---------------- Upload transfer path ----------------
// Selects between the two implementations in Service_AwsUploader.cpp:
//   1 = direct WiFiClientSecure PUT, chunked SD reads (no whole-file buffer)
//   0 = legacy HTTPClient + whole-file PSRAM buffer
// Kept switchable so the two can be A/B'd on the same hardware without a
// revert; the legacy path is unchanged and still compiles.
#define UPLOAD_USE_DIRECT_TLS 1

// TEST ONLY - Local upload benchmark: the diagnostic transport is implemented
// inside putFileDirect(), so the legacy HTTPClient path cannot serve it.
#if LOCAL_UPLOAD_TEST && !UPLOAD_USE_DIRECT_TLS
#error "LOCAL_UPLOAD_TEST requires UPLOAD_USE_DIRECT_TLS 1 (the legacy HTTPClient path is not instrumented for this benchmark)"
#endif

// Chunk size for the SD -> socket loop on the direct path. This is the unit
// of BOTH the SD read and the TLS write, and the SD mutex is held only for
// the read half of each iteration.
//
// 32KB. This was briefly lowered to 4KB to match send_ssl_data()'s internal
// max_write_chunk_size (esp32 core 3.3.10, ssl_client.cpp), on the theory that
// one application write should equal one mbedtls_ssl_write. Field logs showed
// that was a mistake on both counts:
//
//   - Throughput fell to 13-20 KB/s. The core re-fragments to 4096 either way,
//     so the smaller chunk changed nothing on the wire while multiplying the
//     per-chunk fixed cost (SD-mutex take/give, vTaskDelay(1), progress
//     bookkeeping) by 8x. A 6MB file needed 1019+ write calls.
//   - SD contention rose 8x. Each chunk is one SDDriver::Lock acquire/release,
//     so 4KB means ~1486 lock cycles per 6MB file instead of ~186. Every
//     release is an opening for RecordingTask to take the SPI bus mid-transfer,
//     and one such collision produced an EIO from sdReadBytes()'s 500ms data-
//     token timeout (which has no retry) that killed a 5-minute upload at 68%.
//
// 32KB keeps the SD lock held ~21ms per iteration at the measured ~1.5 MB/s
// read rate — long enough to amortize the overhead, short enough not to stall
// RecordingTask.
//
// BENCHMARKING: this is the ONLY place the chunk size is defined. Change it
// here and rebuild — nothing else in the firmware carries a literal chunk
// value. Suggested sweep: 16/32/48/64 KB.
//
// Note the buffer is PSRAM, so raising it does not consume internal DRAM.
#ifndef UPLOAD_CHUNK_BYTES
constexpr size_t UPLOAD_CHUNK_BYTES = 32UL * 1024;
#endif

// Progress-report throttle. A report is emitted only when BOTH the percent
// delta AND the time delta are exceeded, so a fast upload cannot spam BLE and
// a slow one still ticks. Item 2 of the optimization pass.
constexpr uint8_t  UPLOAD_PROGRESS_MIN_PERCENT_DELTA = 2;
constexpr uint32_t UPLOAD_PROGRESS_MIN_INTERVAL_MS   = 1000;

// Rolling-throughput window. The final average hides the shape of the
// transfer: a run that starts at 300 KB/s and collapses to 20 KB/s once the
// TCP window fills averages out to something that looks merely mediocre.
// Bucketing per-window bytes exposes that collapse.
//
// 5s balances resolution against log volume — at 20 KB/s a 5s bucket is ~100KB,
// so a multi-MB upload produces a readable dozen lines rather than hundreds.
constexpr uint32_t UPLOAD_THROUGHPUT_WINDOW_MS = 5000;

// Number of rolling-throughput buckets retained for the end-of-upload table.
// Fixed-size and stack-allocated: a std::vector here would allocate from the
// heap during the transfer, which is the one thing this whole investigation
// is trying to keep clean. Overflow past this count keeps the LAST N windows
// (the interesting end of a collapse), and the report says how many it dropped.
constexpr size_t UPLOAD_THROUGHPUT_MAX_BUCKETS = 24;

// A single socket write taking longer than this is counted as a STALL rather
// than merely slow throughput.
//
// This threshold was USELESS against eu-north-1 and is meaningful against
// ap-south-1, which is worth understanding before re-tuning it. Throughput is
// (lwIP send buffer / RTT) = 5744 / RTT, so the nominal time for one 32KB
// write is 32768 / that rate:
//
//   eu-north-1, RTT ~210 ms -> ~26 KB/s  -> ~1260 ms per 32KB write
//   ap-south-1, RTT ~19 ms  -> ~250 KB/s -> ~130 ms per 32KB write
//
// At 26 KB/s no write could POSSIBLY finish inside 250 ms, so the counter read
// "57 of 57 stalled" on every upload — true by arithmetic, diagnostic of
// nothing (see FINDINGS.md, "Caveats"). At 250 KB/s the nominal write is
// ~130 ms, so 250 ms sits at ~2x nominal and once again separates "waiting for
// ACKs" from "waiting for a retransmit timer".
//
// If the region or the send buffer changes again, recompute: this wants to be
// roughly 2x the nominal per-chunk time, never below it.
constexpr uint32_t UPLOAD_STALL_THRESHOLD_MS = 250;

// Legacy whole-file buffer cap (UPLOAD_USE_DIRECT_TLS == 0 only).
constexpr size_t UPLOAD_BUF_MAX_BYTES = 6UL * 1024 * 1024;  // ~26 min @16kHz/16-bit/stereo

// ---------------- Upload profiling (diagnostics only) ----------------
// Set to 0 to compile out ALL upload instrumentation: phase timings, 64KB
// progress logging, per-phase memory snapshots, WiFi info, and the summary
// report. When 0 the upload path is byte-for-byte the un-instrumented
// version — the profiling wrapper stream is not even constructed.
#define ENABLE_UPLOAD_PROFILING 1

// How often the transfer progress line is emitted. Must stay large enough
// that logging itself doesn't perturb the measurement being taken.
constexpr size_t PROFILE_PROGRESS_INTERVAL_BYTES = 64UL * 1024;

// Fragmentation-trace sampling period during the PUT. 250ms rather than 1s:
// the heap can collapse from healthy to unusable inside a single TLS record
// exchange, and a 1s sampler can step straight over that transition. At ~20KB/s
// this is roughly every 5KB transferred, so the log stays readable.
constexpr uint32_t PROFILE_FRAG_SAMPLE_MS = 250;

// Read chunk used by the standalone `benchsd` command. Matches
// HTTP_TCP_TX_BUFFER_SIZE (1460) so the benchmark reproduces the same access
// pattern HTTPClient uses when streaming a file from SD.
constexpr size_t BENCH_SD_CHUNK_BYTES = 1460;

// ---------------- Recording profiling (diagnostics only) ----------------
// Instruments the WAV write/flush/close/verify path. Set to 0 to compile out.
// Chases: a recording that reports >1MB written but leaves no file on SD.
#define ENABLE_RECORDING_PROFILING 1

// A partial write is ALWAYS logged immediately (that is the anomaly being
// hunted). This controls the cadence of the periodic *aggregate* progress
// line, in chunks — one chunk is ~32ms of audio at 16kHz stereo, so 100
// chunks is roughly every 3.2 seconds. Logging every individual write would
// be ~2200 lines per 70s recording and would itself perturb SD timing.
constexpr uint32_t RECORDING_PROFILE_EVERY_N_CHUNKS = 100;
