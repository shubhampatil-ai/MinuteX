package expo.modules.minutexaudiorecorder

// The AudioRecord capture engine: raw PCM off the microphone, streamed straight
// to a WAV segment on disk.
//
// WHY AudioRecord AND NOT MediaRecorder. MediaRecorder (what expo-audio uses)
// cannot emit PCM at all on Android — its encoder list is aac/he_aac/aac_eld/
// amr, with no PCM option — so a real WAV is only reachable by reading frames
// ourselves. It also means we own everything MediaRecorder normally handles:
// buffer sizing, the read loop, threading, and the container.
//
// THREADING. The read loop runs on its own daemon Thread, never on the RN/UI
// thread: AudioRecord.read() with READ_BLOCKING parks until the buffer fills,
// which on the JS thread would freeze the app. State shared with the module is
// @Volatile or guarded by `lock`, because start/stop/pause arrive from the JS
// thread while the loop is mid-read.
//
// MEMORY. One fixed read buffer, one BufferedOutputStream. Nothing scales with
// recording length — a 4-hour session allocates exactly what a 5-second one
// does. PCM is never accumulated, and never crosses the JS bridge.

import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import java.io.BufferedOutputStream
import java.io.File
import java.io.FileOutputStream

/**
 * Capture states the JS side can observe. Mirrors the union in
 * modules/minutex-audio-recorder/src/MinutexAudioRecorder.ts — keep the two in
 * step.
 *
 * Note these describe the NATIVE ENGINE only. The app-level interruption
 * policy (what a focus loss means, whether to auto-resume) stays in
 * lib/rec-controller.ts, which already owns that state machine; duplicating it
 * here would create two sources of truth about one recording.
 */
enum class CaptureState {
  IDLE, RECORDING, PAUSED, STOPPING, STOPPED, ERROR;

  val wire: String
    get() = name.lowercase()
}

/** Tunable capture parameters. Defaults are the transcription target format. */
data class RecorderConfig(
  val sampleRate: Int = 16000,
  val channels: Int = 1,
  val bitsPerSample: Int = 16,
  /**
   * MediaRecorder.AudioSource. VOICE_RECOGNITION by default: it is the standard
   * source for speech capture and, unlike MIC, most OEMs leave its signal
   * unprocessed by AGC/noise suppression tuned for calls. That matters because
   * the point of this engine is to feed a transcription/diarization benchmark,
   * and aggressive OEM processing is exactly the kind of variable that makes
   * a WAV-vs-AAC comparison meaningless. Falls back to MIC if unavailable.
   */
  val audioSource: Int = MediaRecorder.AudioSource.VOICE_RECOGNITION,
)

/** A finalized segment on disk. */
data class SegmentResult(
  val file: File,
  val pcmBytes: Long,
  val durationSeconds: Double,
)

/**
 * Owns one AudioRecord and the file it is writing.
 *
 * Lifecycle: [start] -> ([pause]/[resume])* -> [stop]. One instance handles one
 * segment; an interruption that kills the mic is handled by discarding this
 * recorder and constructing another, which is what produces a new segment file
 * (the same rebuild strategy lib/rec-controller.ts already uses for AAC).
 */
class WavRecorder(
  private val config: RecorderConfig,
  private val outputFile: File,
) {
  private val lock = Any()

  @Volatile private var state: CaptureState = CaptureState.IDLE
  @Volatile private var pcmBytes: Long = 0L
  @Volatile private var lastError: String? = null

  /**
   * Peak absolute sample of the most recent buffer, normalised 0..1.
   *
   * Exposed because the existing controller detects a dead microphone by
   * watching the input level (SILENCE_DB_FLOOR in lib/rec-controller.ts). With
   * MediaRecorder that came from getMaxAmplitude(); AudioRecord has no
   * equivalent, so we compute it while the frames are already in hand.
   */
  @Volatile private var peakLevel: Float = 0f

  private var record: AudioRecord? = null
  private var out: BufferedOutputStream? = null
  private var thread: Thread? = null
  private var readBufferBytes: Int = 0

  val format = WavFormat(config.sampleRate, config.channels, config.bitsPerSample)

  fun currentState(): CaptureState = state
  fun bytesWritten(): Long = pcmBytes
  fun level(): Float = peakLevel
  fun error(): String? = lastError
  fun durationSeconds(): Double = format.durationOf(pcmBytes)

  /**
   * Open the mic and begin writing. Throws [RecorderException] with a specific
   * message on every failure path so the JS side can surface something true
   * rather than a generic "recording failed".
   */
  fun start() {
    synchronized(lock) {
      if (state == CaptureState.RECORDING || state == CaptureState.PAUSED) {
        // Idempotent: a double-tap must not open a second AudioRecord on the
        // same file, which would interleave two writers into one stream.
        return
      }

      val channelConfig = when (config.channels) {
        1 -> AudioFormat.CHANNEL_IN_MONO
        2 -> AudioFormat.CHANNEL_IN_STEREO
        else -> throw RecorderException("unsupported channel count: ${config.channels}")
      }
      if (config.bitsPerSample != 16) {
        // The WAV writer emits integer PCM; 8-bit and float would need a
        // different fmt chunk and a different level calculation.
        throw RecorderException("unsupported bit depth: ${config.bitsPerSample}")
      }
      val encoding = AudioFormat.ENCODING_PCM_16BIT

      val minBuffer = AudioRecord.getMinBufferSize(config.sampleRate, channelConfig, encoding)
      if (minBuffer == AudioRecord.ERROR || minBuffer == AudioRecord.ERROR_BAD_VALUE) {
        throw RecorderException(
          "the device cannot record at ${config.sampleRate} Hz / " +
            "${config.channels} channel(s) / ${config.bitsPerSample}-bit"
        )
      }

      // Deliberately larger than the minimum. The minimum is the point at
      // which overrun begins if the reader is ever late, and this reader shares
      // a CPU with the whole app; a GC pause or a busy main thread at the
      // minimum size drops frames, which is unrecoverable audio loss. 4x the
      // minimum, floored at ~1s of audio, buys slack for a few hundred ms of
      // scheduling delay at negligible memory cost.
      val oneSecond = config.sampleRate * format.bytesPerFrame
      val bufferBytes = maxOf(minBuffer * 4, oneSecond)

      // Read a fraction of the ring buffer per call so the loop keeps draining
      // well ahead of the hardware writer rather than racing it.
      readBufferBytes = maxOf(minBuffer, oneSecond / 4)

      val rec = try {
        AudioRecord(config.audioSource, config.sampleRate, channelConfig, encoding, bufferBytes)
      } catch (e: IllegalArgumentException) {
        throw RecorderException("could not open the microphone: ${e.message}")
      } catch (e: SecurityException) {
        // No RECORD_AUDIO. Reported distinctly so JS can route to the existing
        // permission flow instead of showing a generic error.
        throw RecorderException("microphone permission denied", permissionDenied = true)
      }

      if (rec.state != AudioRecord.STATE_INITIALIZED) {
        rec.release()
        // Almost always the permission being absent, or another app holding
        // the mic exclusively.
        throw RecorderException(
          "the microphone is unavailable — it may be in use by another app",
          micUnavailable = true
        )
      }

      // Header placeholder. Sizes are zero until the segment closes; writing
      // it now means the file is a structurally recognisable WAV from the first
      // byte, which is what lets a segment orphaned by a crash still be read
      // and merged (readWavInfo repairs the size from the file length).
      val stream = try {
        BufferedOutputStream(FileOutputStream(outputFile), COPY_CHUNK)
      } catch (e: Exception) {
        rec.release()
        throw RecorderException("could not create the recording file: ${e.message}")
      }
      try {
        stream.write(buildWavHeader(format, 0L))
      } catch (e: Exception) {
        try { stream.close() } catch (_: Exception) {}
        rec.release()
        throw RecorderException("could not write to the recording file: ${e.message}")
      }

      try {
        rec.startRecording()
      } catch (e: IllegalStateException) {
        try { stream.close() } catch (_: Exception) {}
        rec.release()
        throw RecorderException("the microphone could not be started: ${e.message}")
      }

      if (rec.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
        try { stream.close() } catch (_: Exception) {}
        rec.release()
        throw RecorderException("the microphone did not start", micUnavailable = true)
      }

      record = rec
      out = stream
      pcmBytes = 0L
      peakLevel = 0f
      lastError = null
      state = CaptureState.RECORDING

      val t = Thread({ captureLoop() }, "minutex-wav-capture")
      t.isDaemon = true           // must never keep the process alive
      thread = t
      t.start()
    }
  }

  /**
   * Stop draining without tearing down the mic.
   *
   * PCM already written stays written. Note the mic is NOT released: the
   * recording is still ours, which is the behaviour the existing controller
   * expects from a user pause (it resumes into the same file).
   */
  fun pause() {
    synchronized(lock) {
      if (state != CaptureState.RECORDING) return
      state = CaptureState.PAUSED
      try {
        record?.stop()
      } catch (e: IllegalStateException) {
        // Already stopped by the system. The loop sees PAUSED and idles.
      }
    }
  }

  /** Resume into the SAME segment file. Throws if the mic cannot be restarted. */
  fun resume() {
    synchronized(lock) {
      if (state != CaptureState.PAUSED) return
      val rec = record ?: throw RecorderException("the recorder was already released")
      try {
        rec.startRecording()
      } catch (e: IllegalStateException) {
        throw RecorderException("could not restart the microphone: ${e.message}")
      }
      if (rec.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
        throw RecorderException("the microphone did not restart", micUnavailable = true)
      }
      state = CaptureState.RECORDING
    }
  }

  /**
   * Close the segment: stop the loop, patch the header, release everything.
   *
   * Safe to call from any state and safe to call twice — the second call
   * returns the same measurements. Returns null only when no header was ever
   * written, i.e. there is no file to speak of.
   */
  fun stop(): SegmentResult? {
    val worker: Thread?
    synchronized(lock) {
      if (state == CaptureState.STOPPED) {
        return if (outputFile.exists()) {
          SegmentResult(outputFile, pcmBytes, durationSeconds())
        } else null
      }
      state = CaptureState.STOPPING
      worker = thread
    }

    // Join OUTSIDE the lock: the loop takes the same lock to check state, so
    // joining while holding it would deadlock. A blocked read returns within
    // one buffer (~250 ms at these settings), so the timeout is generous.
    try {
      worker?.join(3000)
    } catch (e: InterruptedException) {
      Thread.currentThread().interrupt()
    }

    synchronized(lock) {
      try {
        record?.let { rec ->
          if (rec.recordingState == AudioRecord.RECORDSTATE_RECORDING) {
            try { rec.stop() } catch (_: IllegalStateException) {}
          }
          rec.release()          // must happen or the mic stays held process-wide
        }
      } catch (e: Exception) {
        // Nothing actionable; the file below is what matters.
      } finally {
        record = null
      }

      try {
        out?.flush()
        out?.close()
      } catch (e: Exception) {
        lastError = "could not flush the recording file: ${e.message}"
      } finally {
        out = null
      }

      thread = null
      state = CaptureState.STOPPED
    }

    if (!outputFile.exists()) return null

    // Now the true payload size is known, so the placeholder becomes a correct
    // header. Until this runs the file declares zero-length audio.
    finalizeWavHeader(outputFile, pcmBytes)

    return SegmentResult(outputFile, pcmBytes, durationSeconds())
  }

  /**
   * The read loop. Runs until stop() flips state to STOPPING.
   *
   * Partial reads are normal, not exceptional: read() returns whatever is
   * available, so the return value is always treated as a length rather than
   * assumed to fill the buffer.
   */
  private fun captureLoop() {
    val buffer = ByteArray(readBufferBytes)

    while (true) {
      val current = state
      if (current == CaptureState.STOPPING || current == CaptureState.STOPPED ||
        current == CaptureState.ERROR
      ) break

      if (current == CaptureState.PAUSED) {
        // Idle cheaply instead of spinning. Short enough that resume() is
        // responsive, long enough not to burn battery for hours.
        try { Thread.sleep(50) } catch (e: InterruptedException) { break }
        continue
      }

      val rec = record ?: break
      val n = try {
        rec.read(buffer, 0, buffer.size)
      } catch (e: Exception) {
        fail("microphone read failed: ${e.message}")
        break
      }

      if (n > 0) {
        try {
          out?.write(buffer, 0, n)
          pcmBytes += n
          peakLevel = peakOf(buffer, n)
        } catch (e: Exception) {
          // Disk full, or the file went away. Stop rather than spin writing
          // into a stream that cannot accept bytes — and keep what landed.
          fail("could not write audio to disk: ${e.message}")
          break
        }
        continue
      }

      when (n) {
        0 -> {
          // No data ready. Happens around a pause or a device switch; not an
          // error, so yield and retry.
          try { Thread.sleep(10) } catch (e: InterruptedException) { break }
        }
        AudioRecord.ERROR_INVALID_OPERATION -> {
          fail("the microphone was stopped unexpectedly")
          break
        }
        AudioRecord.ERROR_BAD_VALUE -> {
          fail("the microphone returned an invalid read")
          break
        }
        AudioRecord.ERROR_DEAD_OBJECT -> {
          // The audio server died or the mic was taken for good. The segment on
          // disk is intact; the controller rebuilds into a new one.
          fail("the microphone became unavailable")
          break
        }
        else -> {
          fail("microphone read error ($n)")
          break
        }
      }
    }
  }

  private fun fail(message: String) {
    synchronized(lock) {
      // Do not overwrite a stop already in progress: an error raised while
      // finalizing is not what the caller needs to hear about.
      if (state == CaptureState.STOPPING || state == CaptureState.STOPPED) return
      lastError = message
      state = CaptureState.ERROR
    }
  }

  /** Peak magnitude of a 16-bit LE buffer, normalised to 0..1. */
  private fun peakOf(buffer: ByteArray, length: Int): Float {
    var peak = 0
    var i = 0
    // Step 2 bytes per sample; ignore a trailing odd byte (never happens with
    // frame-aligned reads, but a truncated read must not index past `length`).
    while (i + 1 < length) {
      val sample = (buffer[i].toInt() and 0xFF) or (buffer[i + 1].toInt() shl 8)
      val abs = if (sample < 0) -sample else sample
      if (abs > peak) peak = abs
      i += 2
    }
    return (peak / 32768f).coerceIn(0f, 1f)
  }

  private companion object {
    /** Output stream buffer. One allocation, independent of duration. */
    const val COPY_CHUNK = 64 * 1024
  }
}

/**
 * A capture failure with enough structure for JS to react appropriately.
 *
 * The flags exist so the TS wrapper can map a permission problem onto the
 * existing permission flow and a mic conflict onto the existing interruption
 * handling, instead of every failure becoming one opaque string.
 */
class RecorderException(
  message: String,
  val permissionDenied: Boolean = false,
  val micUnavailable: Boolean = false,
) : Exception(message)
