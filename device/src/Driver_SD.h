/*
  Driver_SD — gateway to the shared SD/SPI bus. The physical SD card is used
  by both RecordingTask (WAV writes) and UploadTask (reads for upload,
  pending-queue read/write), so unlike I2S/LED/battery it cannot have a
  single task owner. This driver is the sole gateway: every access goes
  through a mutex so concurrent tasks never collide on the SPI bus, but the
  business logic that decides *what* to read/write stays in
  Service_Recorder / Service_AwsUploader / Service_PendingQueue.
*/
#pragma once

#include <Arduino.h>
#include <SD.h>

class SDDriver {
public:
  bool begin();

  // RAII-ish mutex guard for a block of SD operations. Use like:
  //   { SDDriver::Lock lock(sd); ... File f = SD.open(...); ... }
  class Lock {
  public:
    explicit Lock(SDDriver& drv);
    ~Lock();
    Lock(const Lock&) = delete;
    Lock& operator=(const Lock&) = delete;
  private:
    SemaphoreHandle_t mutex_;
  };

  SemaphoreHandle_t mutex() const { return mutex_; }

  // The SPI clock actually passed to SD.begin(). Reported by the `sdinfo`
  // diagnostic so the measured throughput can be compared against the
  // theoretical ceiling of this clock rather than against a guess.
  static uint32_t configuredSpiHz();

private:
  SemaphoreHandle_t mutex_ = nullptr;
};

// ---- Diagnostics (read-only; no behaviour change) ----------------------------
// `sdinfo`  — driver stack, SPI parameters, card type/capacity/sector size.
// `sdbench` — open/read/close timed separately, then a read-buffer size sweep
//             (512B..64KB) so the buffer-vs-throughput curve is measured
//             rather than assumed. Neither changes the runtime configuration.
void sdPrintInfo();
void sdRunBenchmark(const String& filename);
