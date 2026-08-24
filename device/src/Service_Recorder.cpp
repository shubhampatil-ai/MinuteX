#include "Service_Recorder.h"
#include "Config.h"
#include <esp_heap_caps.h>

void Recorder::begin(I2SDriver* i2s, SDDriver* sd, ConfigManager* configMgr) {
  i2s_ = i2s;
  sd_ = sd;
  configMgr_ = configMgr;
}

bool Recorder::applyAudioSettings(const char* what) {
  if (!i2s_->begin(configMgr_->get())) {
    Serial.printf("[ERROR] I2S reinit failed after settings change (%s)\n", what);
    return false;
  }
  return true;
}

// Card capacity check. usedBytes() walks the FAT, so it can take a while on a
// large card — it is timed here so its own cost is visible and can't be
// mistaken for a write stall. A full card is a prime suspect for writes that
// report success (into cache) but never commit a directory entry.
void Recorder::logCardSpace(const char* tag) {
#if ENABLE_RECORDING_PROFILING
  uint32_t t0 = millis();
  uint64_t total = SD.totalBytes();
  uint64_t used = SD.usedBytes();
  uint32_t ms = millis() - t0;
  Serial.printf("[REC-DIAG] %-10s card total=%llu KB  used=%llu KB  free=%llu KB  (query %ums)\n",
                tag, total / 1024, used / 1024,
                (total > used) ? (total - used) / 1024 : 0ULL, (unsigned)ms);
#else
  (void)tag;
#endif
}

void Recorder::writeWavHeaderPlaceholder(File& f, uint8_t channels, uint32_t sampleRate) {
  uint8_t header[44] = {0};
  memcpy(header, "RIFF", 4);
  memcpy(header + 8, "WAVE", 4);
  memcpy(header + 12, "fmt ", 4);
  uint32_t subchunk1Size = 16;
  uint16_t audioFormat = 1;
  uint16_t numChannels = channels;
  uint16_t bitsPerSample = 16;
  uint32_t byteRate = sampleRate * numChannels * (bitsPerSample / 8);
  uint16_t blockAlign = numChannels * (bitsPerSample / 8);

  memcpy(header + 16, &subchunk1Size, 4);
  memcpy(header + 20, &audioFormat, 2);
  memcpy(header + 22, &numChannels, 2);
  memcpy(header + 24, &sampleRate, 4);
  memcpy(header + 28, &byteRate, 4);
  memcpy(header + 32, &blockAlign, 2);
  memcpy(header + 34, &bitsPerSample, 2);
  memcpy(header + 36, "data", 4);
  f.write(header, 44);
}

void Recorder::finalizeWavHeader(File& f, uint32_t dataSize) {
  uint32_t riffSize = 36 + dataSize;

  bool seek1 = f.seek(4);
  size_t w1 = f.write((uint8_t*)&riffSize, 4);
  bool seek2 = f.seek(40);
  size_t w2 = f.write((uint8_t*)&dataSize, 4);

  uint32_t tFlush0 = millis();
  f.flush();
  uint32_t msFlush = millis() - tFlush0;

#if ENABLE_RECORDING_PROFILING
  // seek/write failures here would corrupt the header but should NOT make the
  // file vanish — recording which of these fails separates "bad header" from
  // "file never committed".
  Serial.printf("[REC-DIAG] finalize: seek(4)=%d write=%u/4  seek(40)=%d write=%u/4  "
                "flush=%ums  writeError=%d  size_before_close=%u\n",
                seek1 ? 1 : 0, (unsigned)w1, seek2 ? 1 : 0, (unsigned)w2,
                (unsigned)msFlush, (int)f.getWriteError(), (unsigned)f.size());
#else
  (void)seek1; (void)w1; (void)seek2; (void)w2; (void)msFlush;
#endif
}

bool Recorder::start() {
  const AudioConfig& cfg = configMgr_->get();
  recChannels_   = channelCount(cfg.chMode);
  recSampleRate_ = cfg.sampleRate;

  if (timeIsSynced()) {
    currentTimestamp_ = String((unsigned long)time(nullptr));
  } else {
    uint32_t recNo = configMgr_->nextRecNo();
    currentTimestamp_ = "n" + String(recNo) + "-" + String(millis());
  }
  currentMeetingId_ = "meeting-" + currentTimestamp_;
  currentFilename_  = "/rec_" + currentTimestamp_ + ".wav";

  SDDriver::Lock lock(*sd_);

  logCardSpace("at start");

#if ENABLE_RECORDING_PROFILING
  // FILE_WRITE is "w" (truncate), so an existing file with this name would be
  // silently destroyed. Timestamp-derived names collide if two recordings
  // start within the same second (NTP path) — worth knowing about explicitly.
  if (SD.exists(currentFilename_)) {
    Serial.printf("[REC-DIAG] WARNING: %s already exists and will be TRUNCATED "
                  "(filename collision)\n", currentFilename_.c_str());
  }
#endif

  uint32_t tOpen0 = millis();
  wavFile_ = SD.open(currentFilename_, FILE_WRITE);
  uint32_t msOpen = millis() - tOpen0;
  if (!wavFile_) {
    Serial.printf("[ERROR] Failed to open file for recording: %s (open took %ums)\n",
                  currentFilename_.c_str(), (unsigned)msOpen);
    return false;
  }
#if ENABLE_RECORDING_PROFILING
  // Does the directory entry exist the moment after open? If this is already
  // false, the file never gets created at all and everything downstream is a
  // consequence rather than the cause.
  Serial.printf("[REC-DIAG] open ok: %s  (%ums)  exists_now=%d  name='%s'\n",
                currentFilename_.c_str(), (unsigned)msOpen,
                SD.exists(currentFilename_) ? 1 : 0, wavFile_.name());
#endif

  writeWavHeaderPlaceholder(wavFile_, recChannels_, recSampleRate_);
  dataBytesWritten_ = 0;
  chunkCounter_ = 0;

  // Reset write-path counters for this recording.
  writeCalls_ = 0;
  bytesRequested_ = 0;
  bytesAccepted_ = 0;
  partialWrites_ = 0;
  firstPartialChunk_ = 0;
  maxWriteMs_ = 0;
  totalWriteMs_ = 0;
  recStartMs_ = millis();
  wavFile_.clearWriteError();
  Serial.printf("[STATE] Recording -> %s (%uHz, %uch, shift=%u)\n",
                currentFilename_.c_str(), (unsigned)recSampleRate_,
                (unsigned)recChannels_, (unsigned)cfg.shiftBits);
  return true;
}

void Recorder::captureChunk() {
  static int32_t i2sBuffer[I2S_DMA_FRAME_NUM * 2];
  static int16_t pcmBuffer[I2S_DMA_FRAME_NUM * 2];

  size_t bytesRead = 0;
  esp_err_t res = i2s_->read(i2sBuffer, sizeof(i2sBuffer), &bytesRead);
  if (res != ESP_OK || bytesRead == 0) {
    Serial.printf("[WARN] i2s read returned no data (err=%d)\n", (int)res);
    return;
  }

  int samplesRead = bytesRead / sizeof(int32_t);
  chunkCounter_++;

  const AudioConfig& cfg = configMgr_->get();
  if (cfg.rawDebug && (chunkCounter_ % RAW_DEBUG_EVERY_N_CHUNKS == 1)) {
    Serial.printf("[DEBUG] raw: %08X %08X %08X %08X %08X %08X\n",
                  (unsigned)i2sBuffer[0], (unsigned)i2sBuffer[1], (unsigned)i2sBuffer[2],
                  (unsigned)i2sBuffer[3], (unsigned)i2sBuffer[4], (unsigned)i2sBuffer[5]);
  }

  const uint8_t shift = cfg.shiftBits;
  for (int i = 0; i < samplesRead; i++) {
    int32_t v = i2sBuffer[i] >> shift;
    if (v > 32767) v = 32767;
    else if (v < -32768) v = -32768;
    pcmBuffer[i] = (int16_t)v;
  }

  size_t bytesToWrite = samplesRead * sizeof(int16_t);
  SDDriver::Lock lock(*sd_);

  uint32_t tw0 = millis();
  size_t written = wavFile_.write((uint8_t*)pcmBuffer, bytesToWrite);
  uint32_t twMs = millis() - tw0;

#if ENABLE_RECORDING_PROFILING
  writeCalls_++;
  bytesRequested_ += bytesToWrite;
  bytesAccepted_  += written;
  totalWriteMs_   += twMs;
  if (twMs > maxWriteMs_) maxWriteMs_ = twMs;
#endif

  if (written != bytesToWrite) {
#if ENABLE_RECORDING_PROFILING
    // Every partial write is logged immediately with full context — this is
    // the anomaly being hunted, so it is never aggregated away.
    partialWrites_++;
    if (firstPartialChunk_ == 0) firstPartialChunk_ = chunkCounter_;
    Serial.printf("[REC-DIAG] PARTIAL WRITE @chunk=%u t=%ums  requested=%u got=%u  "
                  "writeError=%d  pos=%u  accepted_total=%u\n",
                  (unsigned)chunkCounter_, (unsigned)(millis() - recStartMs_),
                  (unsigned)bytesToWrite, (unsigned)written,
                  (int)wavFile_.getWriteError(),
                  (unsigned)wavFile_.position(), (unsigned)bytesAccepted_);
#endif
    Serial.println("[ERROR] SD write incomplete — possible full card or write failure");
  }
  dataBytesWritten_ += written;

#if ENABLE_RECORDING_PROFILING
  if (chunkCounter_ % RECORDING_PROFILE_EVERY_N_CHUNKS == 0) {
    Serial.printf("[REC-DIAG] chunk=%u t=%ums  accepted=%u/%u bytes  partials=%u  "
                  "write_ms(avg/max)=%u/%u  writeError=%d  heap=%u\n",
                  (unsigned)chunkCounter_, (unsigned)(millis() - recStartMs_),
                  (unsigned)bytesAccepted_, (unsigned)bytesRequested_,
                  (unsigned)partialWrites_,
                  (unsigned)(writeCalls_ ? totalWriteMs_ / writeCalls_ : 0),
                  (unsigned)maxWriteMs_,
                  (int)wavFile_.getWriteError(),
                  (unsigned)ESP.getFreeHeap());
  }
#endif
}

RecordingResult Recorder::stop() {
  SDDriver::Lock lock(*sd_);

  // ---- Finalization chain, each step timestamped relative to stop() entry ----
  const uint32_t tStop0 = millis();

#if ENABLE_RECORDING_PROFILING
  const uint32_t sizeBeforeFinalize = wavFile_.size();
  const uint32_t posBeforeFinalize  = wavFile_.position();
  const int writeErrBeforeFinalize  = wavFile_.getWriteError();
  const bool openBeforeFinalize     = (bool)wavFile_;
  Serial.printf("[REC-DIAG] stop() entry t=+0ms  handle_open=%d  size=%u  pos=%u  writeError=%d\n",
                openBeforeFinalize ? 1 : 0, (unsigned)sizeBeforeFinalize,
                (unsigned)posBeforeFinalize, writeErrBeforeFinalize);
#endif

  finalizeWavHeader(wavFile_, dataBytesWritten_);
  const uint32_t tAfterFinalize = millis();

  const uint32_t tClose0 = millis();
  wavFile_.close();
  const uint32_t msClose = millis() - tClose0;

#if ENABLE_RECORDING_PROFILING
  // After close() the handle must evaluate false; if it doesn't, close did not
  // complete.
  Serial.printf("[REC-DIAG] close(): %ums  t=+%ums  handle_open_after=%d\n",
                (unsigned)msClose, (unsigned)(millis() - tStop0),
                ((bool)wavFile_) ? 1 : 0);
#endif

  uint32_t bytesPerSec = recSampleRate_ * recChannels_ * 2;
  float seconds = bytesPerSec ? (float)dataBytesWritten_ / bytesPerSec : 0.0f;
  Serial.printf("[STATE] Stopped. %s (%u bytes = %.1fs)\n",
                currentFilename_.c_str(), (unsigned)dataBytesWritten_, seconds);

#if ENABLE_RECORDING_PROFILING
  // Write-path summary for the whole recording.
  Serial.printf("[REC-DIAG] writes: calls=%u requested=%u accepted=%u partials=%u"
                " first_partial_chunk=%u write_ms(total/max)=%u/%u\n",
                (unsigned)writeCalls_, (unsigned)bytesRequested_,
                (unsigned)bytesAccepted_, (unsigned)partialWrites_,
                (unsigned)firstPartialChunk_,
                (unsigned)totalWriteMs_, (unsigned)maxWriteMs_);
  Serial.printf("[REC-DIAG] timing: finalize=%ums close=%ums wall=%ums audio=%.1fs\n",
                (unsigned)(tAfterFinalize - tStop0), (unsigned)msClose,
                (unsigned)(millis() - recStartMs_), seconds);
#endif

  // ---- Existence / size verification, immediately after close ----
  const uint32_t tExists0 = millis();
  const bool existsAfterClose = SD.exists(currentFilename_);
  const uint32_t msExists = millis() - tExists0;

#if ENABLE_RECORDING_PROFILING
  Serial.printf("[REC-DIAG] SD.exists(\"%s\") = %d   (%ums, t=+%ums)\n",
                currentFilename_.c_str(), existsAfterClose ? 1 : 0,
                (unsigned)msExists, (unsigned)(millis() - tStop0));
#endif

  const uint32_t expectedSize = dataBytesWritten_ + 44;   // 44-byte WAV header

  if (!existsAfterClose) {
    Serial.printf("[ERROR] DATA LOSS: %s reported %u bytes written but does not "
                  "exist on SD — recording lost\n",
                  currentFilename_.c_str(), (unsigned)dataBytesWritten_);
#if ENABLE_RECORDING_PROFILING
    Serial.printf("[REC-DIAG] at loss: heap=%u largest_int=%u min_ever=%u\n",
                  (unsigned)ESP.getFreeHeap(),
                  (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL),
                  (unsigned)ESP.getMinFreeHeap());
    logCardSpace("at loss");
    // Is the directory readable at all, or is the whole FS unhealthy? Listing
    // the root distinguishes "this one entry vanished" from "SD/FAT is broken".
    File root = SD.open("/");
    if (!root) {
      Serial.println("[REC-DIAG] root directory NOT openable — filesystem-level failure");
    } else {
      int n = 0;
      File e = root.openNextFile();
      while (e) {
        if (n < 12) Serial.printf("[REC-DIAG]   root entry: %-28s %u bytes\n",
                                  e.name(), (unsigned)e.size());
        n++;
        e.close();
        e = root.openNextFile();
      }
      root.close();
      Serial.printf("[REC-DIAG] root listing OK, %d entries total\n", n);
    }
#endif
  } else {
    // Reopen and compare the on-disk size against what we tracked internally.
    File chk = SD.open(currentFilename_, FILE_READ);
    if (!chk) {
      Serial.printf("[ERROR] %s exists per SD.exists() but cannot be reopened\n",
                    currentFilename_.c_str());
    } else {
      const uint32_t onDisk = chk.size();
      chk.close();
#if ENABLE_RECORDING_PROFILING
      Serial.printf("[REC-DIAG] reopen ok: on_disk=%u  expected=%u (data %u + 44)  delta=%d\n",
                    (unsigned)onDisk, (unsigned)expectedSize,
                    (unsigned)dataBytesWritten_, (int)((int32_t)onDisk - (int32_t)expectedSize));
      logCardSpace("after stop");
#endif
      if (onDisk < expectedSize) {
        Serial.printf("[ERROR] TRUNCATED: %s is %u bytes on SD but %u expected\n",
                      currentFilename_.c_str(), (unsigned)onDisk, (unsigned)expectedSize);
      }
    }
  }

  RecordingResult r;
  r.filename = currentFilename_;
  r.meetingId = currentMeetingId_;
  r.timestamp = currentTimestamp_;
  r.dataBytesWritten = dataBytesWritten_;
  return r;
}
