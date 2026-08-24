package expo.modules.minutexaudiorecorder

// The JS surface of the experimental WAV recorder.
//
// DESIGN, mirroring modules/audio-focus: this module owns MECHANISM (open the
// mic, write frames, merge segments) and as little POLICY as possible. What an
// interruption means, whether to auto-resume, when to give up — all of that
// already lives in lib/rec-controller.ts and must not be duplicated here, or
// there would be two state machines disagreeing about one recording.
//
// The one piece of policy it does own is segment bookkeeping, because only this
// side knows how many bytes actually reached disk.
//
// EVERY function is safe to call in the wrong order. The JS side can be
// re-entered by a double-tap, an interruption arriving mid-finalize, or a
// screen remounting, so "start while recording" and "stop while stopping" have
// to be defined rather than undefined.

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import androidx.core.content.ContextCompat
import expo.modules.kotlin.exception.CodedException
import expo.modules.kotlin.modules.Module
import expo.modules.kotlin.modules.ModuleDefinition
import java.io.File

private const val EVENT_STATUS = "onRecordingStatus"

class MinutexAudioRecorderModule : Module() {

  private val context: Context
    get() = appContext.reactContext ?: throw IllegalStateException("No react context")

  /** The live recorder, or null when idle. Guarded by [lock]. */
  private var recorder: WavRecorder? = null

  /** Segments finalized so far, in capture order. */
  private val segments = mutableListOf<SegmentResult>()

  /** Where segments for the current recording are written. */
  private var recordingDir: File? = null
  private var recordingId: String? = null
  private var config = RecorderConfig()

  /** Whether the foreground service is up for this recording. */
  private var serviceRunning = false

  private val lock = Any()

  private fun hasMicPermission(): Boolean =
    ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) ==
      PackageManager.PERMISSION_GRANTED

  /**
   * Bytes across finalized segments plus the live one. This is what makes the
   * reported duration correct across an interruption — the live recorder only
   * knows about its own segment.
   */
  private fun totalPcmBytes(): Long =
    segments.sumOf { it.pcmBytes } + (recorder?.bytesWritten() ?: 0L)

  private fun totalDuration(): Double {
    val fmt = WavFormat(config.sampleRate, config.channels, config.bitsPerSample)
    return fmt.durationOf(totalPcmBytes())
  }

  private fun statusMap(): Map<String, Any?> {
    val rec = recorder
    val state = rec?.currentState() ?: CaptureState.IDLE
    return mapOf(
      "state" to state.wire,
      "isRecording" to (state == CaptureState.RECORDING),
      "durationSeconds" to totalDuration(),
      "sizeBytes" to (totalPcmBytes() + WAV_HEADER_BYTES),
      "level" to (rec?.level() ?: 0f),
      "segmentCount" to segments.size + (if (rec != null) 1 else 0),
      "sampleRate" to config.sampleRate,
      "channels" to config.channels,
      "bitDepth" to config.bitsPerSample,
      "error" to rec?.error(),
    )
  }

  override fun definition() = ModuleDefinition {
    Name("MinutexAudioRecorder")

    Events(EVENT_STATUS)

    /**
     * Whether this build can record WAV at all. Lets JS fall back to the AAC
     * engine without a try/catch around every call.
     */
    Function("isAvailable") { true }

    /**
     * Begin a recording.
     *
     * `directory` and `id` come from JS so the files land in the same document
     * directory the existing store owns (lib/rec-store.ts), rather than this
     * module inventing a location the rest of the app cannot find.
     */
    AsyncFunction("startRecording") { options: Map<String, Any?> ->
      synchronized(lock) {
        if (!hasMicPermission()) {
          throw CodedException("ERR_NO_MIC_PERMISSION", "Microphone permission has not been granted", null)
        }

        val existing = recorder
        if (existing != null &&
          (existing.currentState() == CaptureState.RECORDING ||
            existing.currentState() == CaptureState.PAUSED)
        ) {
          // Idempotent, matching start() in lib/rec-controller.ts.
          return@AsyncFunction statusMap()
        }

        val dirPath = options["directory"] as? String
          ?: throw CodedException("ERR_BAD_ARGS", "A `directory` is required", null)
        val id = options["id"] as? String
          ?: throw CodedException("ERR_BAD_ARGS", "An `id` is required", null)

        val dir = File(stripFileScheme(dirPath))
        if (!dir.exists() && !dir.mkdirs()) {
          throw CodedException("ERR_DIRECTORY", "Could not create $dir", null)
        }

        config = RecorderConfig(
          sampleRate = (options["sampleRate"] as? Number)?.toInt() ?: 16000,
          channels = (options["channels"] as? Number)?.toInt() ?: 1,
          bitsPerSample = (options["bitDepth"] as? Number)?.toInt() ?: 16,
        )

        segments.clear()
        recordingDir = dir
        recordingId = id

        // Foreground service BEFORE opening the mic. On Android 9+ a
        // background process reads zeros rather than failing, so the order
        // matters: start the service, then capture.
        serviceRunning = WavRecordingService.start(context)

        val segmentFile = File(dir, segmentName(id, 1))
        val rec = WavRecorder(config, segmentFile)
        try {
          rec.start()
        } catch (e: RecorderException) {
          if (serviceRunning) {
            WavRecordingService.stop(context)
            serviceRunning = false
          }
          throw CodedException(codeFor(e), e.message ?: "Could not start recording", e)
        }
        recorder = rec

        val status = statusMap()
        sendEvent(EVENT_STATUS, status)
        status
      }
    }

    /**
     * Close the current segment and open a new one — the interruption-recovery
     * primitive.
     *
     * Called by JS when the existing interruption machinery decides the mic has
     * come back (see attemptResume in lib/rec-controller.ts). A fresh
     * AudioRecord is the only way back to real audio after the telephony stack
     * has taken the input, exactly as on the AAC path.
     */
    AsyncFunction("rollSegment") {
      synchronized(lock) {
        val dir = recordingDir
        val id = recordingId
        if (dir == null || id == null) {
          throw CodedException("ERR_NOT_RECORDING", "No recording in progress", null)
        }

        // Close the old one first so its bytes are a complete, valid WAV even if
        // opening the replacement fails.
        recorder?.stop()?.let { if (it.pcmBytes > 0L) segments.add(it) }
        recorder = null

        val next = File(dir, segmentName(id, segments.size + 1))
        val rec = WavRecorder(config, next)
        try {
          rec.start()
        } catch (e: RecorderException) {
          // Segments so far are preserved and listed; the caller retries.
          throw CodedException(codeFor(e), e.message ?: "Could not open a new segment", e)
        }
        recorder = rec

        val status = statusMap()
        sendEvent(EVENT_STATUS, status)
        status
      }
    }

    AsyncFunction("pauseRecording") {
      synchronized(lock) {
        recorder?.pause()
        val status = statusMap()
        sendEvent(EVENT_STATUS, status)
        status
      }
    }

    AsyncFunction("resumeRecording") {
      synchronized(lock) {
        val rec = recorder ?: throw CodedException("ERR_NOT_RECORDING", "No recording to resume", null)
        try {
          rec.resume()
        } catch (e: RecorderException) {
          throw CodedException(codeFor(e), e.message ?: "Could not resume", e)
        }
        val status = statusMap()
        sendEvent(EVENT_STATUS, status)
        status
      }
    }

    /**
     * Finish: close the live segment, merge if there is more than one, and
     * return the single file to upload.
     *
     * Source segments are deleted ONLY after the merged file exists and
     * re-validates. A failed merge leaves every part on disk and says so, so a
     * bad merge can never be the reason a meeting is lost.
     */
    AsyncFunction("stopRecording") {
      synchronized(lock) {
        val dir = recordingDir
        val id = recordingId
        if (dir == null || id == null) {
          throw CodedException("ERR_NOT_RECORDING", "No recording in progress", null)
        }

        val liveError = recorder?.error()
        recorder?.stop()?.let { if (it.pcmBytes > 0L) segments.add(it) }
        recorder = null

        if (serviceRunning) {
          WavRecordingService.stop(context)
          serviceRunning = false
        }

        if (segments.isEmpty()) {
          recordingDir = null
          recordingId = null
          throw CodedException(
            "ERR_NO_AUDIO",
            liveError ?: "No audio was captured",
            null
          )
        }

        val fmt = WavFormat(config.sampleRate, config.channels, config.bitsPerSample)

        // One segment is already the finished article — merging it would copy
        // hundreds of MB to no purpose.
        if (segments.size == 1) {
          val only = segments[0]
          val result = mapOf(
            "uri" to fileUri(only.file),
            "durationSeconds" to only.durationSeconds,
            "sizeBytes" to only.file.length(),
            "sampleRate" to config.sampleRate,
            "channels" to config.channels,
            "bitDepth" to config.bitsPerSample,
            "format" to "wav",
            "segmentCount" to 1,
            "merged" to false,
            "segmentUris" to listOf(fileUri(only.file)),
            "warning" to liveError,
          )
          segments.clear()
          recordingDir = null
          recordingId = null
          sendEvent(EVENT_STATUS, statusMap())
          return@AsyncFunction result
        }

        val sources = segments.map { it.file }
        val target = File(dir, finalName(id))
        val merge = mergeWavSegments(sources, target)

        if (!merge.ok) {
          // Keep everything. The caller decides whether to upload the longest
          // part; nothing is deleted on this path.
          val kept = sources.map { fileUri(it) }
          val longest = sources.maxByOrNull { it.length() }
          val result = mapOf(
            "uri" to (longest?.let { fileUri(it) }),
            "durationSeconds" to (longest?.let { f ->
              readWavInfo(f)?.let { fmt.durationOf(it.dataBytes) } ?: 0.0
            } ?: 0.0),
            "sizeBytes" to (longest?.length() ?: 0L),
            "sampleRate" to config.sampleRate,
            "channels" to config.channels,
            "bitDepth" to config.bitsPerSample,
            "format" to "wav",
            "segmentCount" to sources.size,
            "merged" to false,
            "segmentUris" to kept,
            "warning" to (merge.error ?: "Segments could not be merged"),
          )
          segments.clear()
          recordingDir = null
          recordingId = null
          sendEvent(EVENT_STATUS, statusMap())
          return@AsyncFunction result
        }

        // Merged and validated — now the sources are redundant. Reclaiming the
        // space matters: a 3-hour recording keeps ~350 MB of segments alive.
        for (f in sources) {
          try { f.delete() } catch (e: Exception) { /* space, not correctness */ }
        }

        val result = mapOf(
          "uri" to fileUri(target),
          "durationSeconds" to merge.durationSeconds,
          "sizeBytes" to target.length(),
          "sampleRate" to config.sampleRate,
          "channels" to config.channels,
          "bitDepth" to config.bitsPerSample,
          "format" to "wav",
          "segmentCount" to merge.segmentsMerged,
          "merged" to true,
          "segmentUris" to emptyList<String>(),
          "warning" to merge.error,
        )
        segments.clear()
        recordingDir = null
        recordingId = null
        sendEvent(EVENT_STATUS, statusMap())
        result
      }
    }

    /** Synchronous, because the UI polls it on a timer like the AAC path does. */
    Function("getRecordingStatus") { statusMap() }

    Function("isRecording") {
      recorder?.currentState() == CaptureState.RECORDING
    }

    /**
     * Abandon a recording and delete its files. For the user discarding, and
     * for cleaning up after a failed start.
     */
    AsyncFunction("discardRecording") {
      synchronized(lock) {
        recorder?.stop()
        recorder = null
        if (serviceRunning) {
          WavRecordingService.stop(context)
          serviceRunning = false
        }
        for (s in segments) {
          try { s.file.delete() } catch (e: Exception) { /* best effort */ }
        }
        segments.clear()
        recordingDir = null
        recordingId = null
        sendEvent(EVENT_STATUS, statusMap())
        true
      }
    }

    /**
     * Validate a WAV on disk. Used by the tests and by the upload path to
     * refuse a structurally broken file before it is presigned.
     */
    Function("inspectWav") { uri: String ->
      val info = readWavInfo(File(stripFileScheme(uri)))
      if (info == null) {
        mapOf("valid" to false)
      } else {
        mapOf(
          "valid" to true,
          "sampleRate" to info.format.sampleRate,
          "channels" to info.format.channels,
          "bitDepth" to info.format.bitsPerSample,
          "dataBytes" to info.dataBytes,
          "durationSeconds" to info.format.durationOf(info.dataBytes),
        )
      }
    }

    OnDestroy {
      // Never leave the mic held or the notification stranded because the module
      // went away without a stop().
      synchronized(lock) {
        try { recorder?.stop() } catch (e: Exception) { /* shutting down */ }
        recorder = null
        if (serviceRunning) {
          WavRecordingService.stop(context)
          serviceRunning = false
        }
      }
    }
  }

  private fun codeFor(e: RecorderException): String = when {
    e.permissionDenied -> "ERR_NO_MIC_PERMISSION"
    e.micUnavailable -> "ERR_MIC_UNAVAILABLE"
    else -> "ERR_RECORDER"
  }

  private companion object {
    fun segmentName(id: String, index: Int): String =
      "%s_segment_%03d.wav".format(id, index)

    fun finalName(id: String): String = "${id}_final.wav"

    /** expo-file-system hands over file:// URIs; java.io.File needs a path. */
    fun stripFileScheme(s: String): String =
      if (s.startsWith("file://")) s.removePrefix("file://") else s

    fun fileUri(f: File): String = "file://${f.absolutePath}"
  }
}
