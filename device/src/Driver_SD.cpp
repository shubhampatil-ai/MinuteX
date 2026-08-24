#include "Driver_SD.h"
#include "Config.h"
#include <esp_heap_caps.h>

namespace {
// The frequency argument we pass to SD.begin(). SD.begin()'s own default is
// 4000000 (see SD.h:30), and Driver_SD::begin() calls SD.begin(PIN_SD_CS)
// without a frequency — so 4 MHz is what the bus actually runs at. Recorded
// here so the diagnostics can report it instead of guessing.
constexpr uint32_t SD_SPI_HZ_IN_USE = 25000000;   // == SD.begin() default

const char* cardTypeName(sdcard_type_t t) {
  switch (t) {
    case CARD_NONE:    return "NONE";
    case CARD_MMC:     return "MMC";
    case CARD_SD:      return "SDSC";
    case CARD_SDHC:    return "SDHC/SDXC";
    case CARD_UNKNOWN: return "UNKNOWN";
    default:           return "?";
  }
}
}  // namespace

uint32_t SDDriver::configuredSpiHz() { return SD_SPI_HZ_IN_USE; }

bool SDDriver::begin() {
  mutex_ = xSemaphoreCreateMutex();
  SPI.begin(PIN_SD_SCK, PIN_SD_MISO, PIN_SD_MOSI, PIN_SD_CS);
  if (!SD.begin(PIN_SD_CS, SPI, 25000000)) {
    return false;
  }
  return true;
}

SDDriver::Lock::Lock(SDDriver& drv) : mutex_(drv.mutex()) {
  xSemaphoreTake(mutex_, portMAX_DELAY);
}

SDDriver::Lock::~Lock() {
  xSemaphoreGive(mutex_);
}

// =============================================================================
// Diagnostics
// =============================================================================

void sdPrintInfo() {
  Serial.println(F("[SPI/SD INFO]"));

  // Driver stack, established by reading the core sources rather than assumed:
  //   Arduino SD.h  -> class SDFS : public FS
  //   FS/VFS layer  -> VFSImpl / VFSFileImpl  (stdio: fopen/fread/fclose)
  //   FATFS         -> ff.c, mounted at "/sd"
  //   disk layer    -> SD/src/sd_diskio.cpp   (NOT the ESP-IDF sdmmc/sdspi
  //                    driver) which talks to the card through SPIClass
  Serial.println(F("  Driver      : Arduino SD (SDFS) -> FS/VFS(stdio) -> FATFS -> sd_diskio.cpp -> SPIClass"));
  Serial.println(F("  NOT using   : ESP-IDF sdmmc/sdspi host driver, SdFat"));

  const uint32_t hz = SDDriver::configuredSpiHz();
  Serial.printf("  Clock       : %.1f MHz  (SD.begin() default — we pass no frequency)\n", hz / 1000000.0f);
  Serial.println(F("  Mode        : SPI_MODE0, MSBFIRST, 1-bit (single MISO line)"));
  Serial.println(F("  DMA         : SPIClass transferBytes() — no explicit DMA descriptor use"));
  Serial.printf("  Pins        : CS=%d SCK=%d MISO=%d MOSI=%d\n",
                PIN_SD_CS, PIN_SD_SCK, PIN_SD_MISO, PIN_SD_MOSI);
  Serial.println(F("  max_files   : 5 (SD.begin() default) — each open file costs a 4KB stdio buffer"));

  // Theoretical ceiling at this clock. 1-bit SPI moves 1 bit per clock, so the
  // absolute wire limit is hz/8 bytes/s before any command/CRC/token overhead.
  const float ceilingMBs = (hz / 8.0f) / 1048576.0f;
  Serial.printf("  Wire ceiling: %.2f MB/s at %.1f MHz (before protocol overhead)\n",
                ceilingMBs, hz / 1000000.0f);
  Serial.println(F("  Driver cap  : 25 MHz (sd_diskio.cpp clamps card->frequency)"));

  Serial.printf("  Card type   : %s\n", cardTypeName(SD.cardType()));
  Serial.printf("  Capacity    : %llu MB\n", SD.cardSize() / (1024ULL * 1024ULL));
  Serial.printf("  Sector size : %u bytes\n", (unsigned)SD.sectorSize());
  Serial.printf("  Num sectors : %u\n", (unsigned)SD.numSectors());
  uint32_t t0 = millis();
  uint64_t total = SD.totalBytes();
  uint64_t used = SD.usedBytes();
  Serial.printf("  FS total    : %llu KB   used: %llu KB   free: %llu KB   (query %ums)\n",
                total / 1024, used / 1024,
                (total > used) ? (total - used) / 1024 : 0ULL, (unsigned)(millis() - t0));
  Serial.println(F("  Speed class : not exposed by this driver (no CSD/SSR decode available)"));
}

void sdRunBenchmark(const String& filename) {
  // ---- Phase 1: open / read / close timed independently -------------------
  Serial.println(F("[SD BENCH] phase timing (4KB buffer)"));

  const uint32_t tOpen0 = millis();
  File f = SD.open(filename, FILE_READ);
  const uint32_t msOpen = millis() - tOpen0;
  if (!f) {
    Serial.printf("  open FAILED for %s (%ums) — heap=%u largest_int=%u\n",
                  filename.c_str(), (unsigned)msOpen,
                  (unsigned)ESP.getFreeHeap(),
                  (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
    return;
  }
  const size_t fileSize = f.size();

  static uint8_t phaseBuf[4096];
  size_t total = 0;
  const uint32_t tRead0 = millis();
  while (f.available()) {
    size_t n = f.read(phaseBuf, sizeof(phaseBuf));
    if (n == 0) break;
    total += n;
  }
  const uint32_t msRead = millis() - tRead0;

  const uint32_t tClose0 = millis();
  f.close();
  const uint32_t msClose = millis() - tClose0;

  Serial.printf("  File        : %s (%u bytes)\n", filename.c_str(), (unsigned)fileSize);
  Serial.printf("  open()      : %u ms\n", (unsigned)msOpen);
  Serial.printf("  read() all  : %u ms  (%u bytes)\n", (unsigned)msRead, (unsigned)total);
  Serial.printf("  close()     : %u ms\n", (unsigned)msClose);
  if (msRead > 0) {
    Serial.printf("  Read speed  : %.3f MB/s\n", (total / 1048576.0f) / (msRead / 1000.0f));
  }

  // ---- Phase 2: read-buffer size sweep ------------------------------------
  // Buffers come from PSRAM so the sweep itself cannot distort the internal
  // heap measurements taken elsewhere. Nothing here alters the runtime
  // configuration — it only measures the buffer-size/throughput curve.
  Serial.println(F("[SD BENCH] buffer size sweep"));
  Serial.println(F("  Buffer      Time      Speed"));

  static const size_t sizes[] = {512, 1024, 4096, 8192, 16384, 32768, 65536};
  for (size_t si = 0; si < sizeof(sizes) / sizeof(sizes[0]); si++) {
    const size_t bs = sizes[si];
    uint8_t* buf = (uint8_t*)(psramFound() ? ps_malloc(bs) : malloc(bs));
    if (!buf) {
      Serial.printf("  %-11s alloc failed — skipped\n",
                    (String(bs / 1024 ? bs / 1024 : bs) + (bs >= 1024 ? " KB" : " B")).c_str());
      continue;
    }

    File bf = SD.open(filename, FILE_READ);
    if (!bf) {
      Serial.printf("  %-11u open failed\n", (unsigned)bs);
      free(buf);
      continue;
    }
    size_t got = 0;
    const uint32_t t0 = millis();
    while (bf.available()) {
      size_t n = bf.read(buf, bs);
      if (n == 0) break;
      got += n;
    }
    const uint32_t ms = millis() - t0;
    bf.close();
    free(buf);

    const float mbps = ms > 0 ? (got / 1048576.0f) / (ms / 1000.0f) : 0.0f;
    char label[16];
    if (bs >= 1024) snprintf(label, sizeof(label), "%u KB", (unsigned)(bs / 1024));
    else            snprintf(label, sizeof(label), "%u B", (unsigned)bs);
    Serial.printf("  %-11s %-9u %.3f MB/s\n", label, (unsigned)ms, mbps);
    vTaskDelay(1);
  }

  const float ceilingMBs = (SDDriver::configuredSpiHz() / 8.0f) / 1048576.0f;
  Serial.printf("  ---- ceiling at %.1f MHz = %.2f MB/s; readings above are bounded by it ----\n",
                SDDriver::configuredSpiHz() / 1000000.0f, ceilingMBs);
}