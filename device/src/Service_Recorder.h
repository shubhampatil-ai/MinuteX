/*
  Service_Recorder — WAV file lifecycle + I2S chunk capture. Used
  exclusively by RecordingTask. Mirrors the original firmware's
  startRecording()/recordChunk()/enterSavingState()/finalizeWavHeader()
  logic 1:1, including the filename scheme (/rec_<timestamp>.wav) and the
  NTP-synced-vs-fallback timestamp logic.
*/
#pragma once

#include <Arduino.h>
#include <SD.h>
#include "Types.h"
#include "Driver_I2S.h"
#include "Driver_SD.h"
#include "Core_ConfigManager.h"

struct RecordingResult {
  String filename;
  String meetingId;
  String timestamp;
  uint32_t dataBytesWritten;
};

class Recorder {
public:
  void begin(I2SDriver* i2s, SDDriver* sd, ConfigManager* configMgr);

  // Re-applies audio settings to I2S (only valid when not recording).
  // Mirrors original applyAudioSettings(): returns false on I2S reinit failure.
  bool applyAudioSettings(const char* what);

  // Starts a new WAV file on SD using the current AudioConfig. Returns
  // false on SD open failure (mirrors original startRecording()).
  bool start();

  // Reads one I2S DMA chunk, shifts/clamps to 16-bit PCM, writes to the
  // open WAV file. Call repeatedly while RECORDING.
  void captureChunk();

  // Finalizes the WAV header + closes the file. Call once on STOP.
  // Returns the completed recording's metadata for hand-off to UploadTask.
  RecordingResult stop();

private:
  I2SDriver* i2s_ = nullptr;
  SDDriver* sd_ = nullptr;
  ConfigManager* configMgr_ = nullptr;

  File wavFile_;
  String currentFilename_;
  String currentMeetingId_;
  String currentTimestamp_;
  uint32_t dataBytesWritten_ = 0;
  uint8_t recChannels_ = 2;
  uint32_t recSampleRate_ = 16000;
  uint32_t chunkCounter_ = 0;

  // ---- Write-path instrumentation (diagnostics only; no behaviour change) ----
  // Aggregates so individual writes need not be logged. Reset in start().
  uint32_t writeCalls_        = 0;   // total write() invocations
  uint32_t bytesRequested_    = 0;   // sum of requested lengths
  uint32_t bytesAccepted_     = 0;   // sum of write() return values
  uint32_t partialWrites_     = 0;   // count where return != requested
  uint32_t firstPartialChunk_ = 0;   // chunk index of the first short write
  uint32_t maxWriteMs_        = 0;   // slowest single write
  uint32_t totalWriteMs_      = 0;   // cumulative time inside write()
  uint32_t recStartMs_        = 0;   // millis() when start() opened the file

  bool timeIsSynced() const { return time(nullptr) > 1700000000; }
  void writeWavHeaderPlaceholder(File& f, uint8_t channels, uint32_t sampleRate);
  void finalizeWavHeader(File& f, uint32_t dataSize);
  void logCardSpace(const char* tag);
};
