package expo.modules.minutexaudiorecorder

// WAV reading/writing for the experimental PCM recorder.
//
// AudioRecord hands back raw PCM with no container, so everything about the
// file format is ours to get right. The two operations here are deliberately
// separated from the capture loop (WavRecorder) and from the module surface
// (MinutexAudioRecorderModule) so that the byte-level format rules live in one
// auditable place.
//
// WHY NOT JUST RENAME .pcm TO .wav: a headerless PCM file is not playable by
// anything the pipeline touches — not a phone player, not ffprobe, and not the
// transcription provider, which sniffs the container. The header is what makes
// the bytes self-describing.

import java.io.File
import java.io.RandomAccessFile

/** Canonical 44-byte RIFF/WAVE PCM header length. */
const val WAV_HEADER_BYTES = 44

/** PCM format tag in the fmt chunk. 1 = uncompressed integer PCM. */
private const val WAVE_FORMAT_PCM = 1

/**
 * The audio parameters a WAV file was written with. Compared between segments
 * before a merge — joining files whose rate or channel count differ would
 * change playback speed or interleaving, which is silent corruption rather
 * than an error, so it must be refused.
 */
data class WavFormat(
  val sampleRate: Int,
  val channels: Int,
  val bitsPerSample: Int,
) {
  val bytesPerFrame: Int get() = channels * (bitsPerSample / 8)
  val byteRate: Int get() = sampleRate * bytesPerFrame

  /** Seconds of audio for a given PCM payload size. */
  fun durationOf(pcmBytes: Long): Double =
    if (byteRate <= 0) 0.0 else pcmBytes.toDouble() / byteRate.toDouble()
}

/**
 * Build a 44-byte canonical WAV header.
 *
 * `pcmBytes` may be 0 when writing the placeholder at the start of a segment —
 * the real sizes are patched in by [finalizeWavHeader] once capture ends and
 * the true payload length is known. Writing a placeholder first (rather than
 * buffering audio to learn the size) is what keeps memory flat for a 4-hour
 * recording.
 *
 * All multi-byte fields are little-endian, which is what RIFF requires.
 */
fun buildWavHeader(format: WavFormat, pcmBytes: Long): ByteArray {
  val header = ByteArray(WAV_HEADER_BYTES)
  var i = 0

  fun ascii(s: String) { for (c in s) header[i++] = c.code.toByte() }
  fun le32(v: Long) {
    header[i++] = (v and 0xFF).toByte()
    header[i++] = ((v shr 8) and 0xFF).toByte()
    header[i++] = ((v shr 16) and 0xFF).toByte()
    header[i++] = ((v shr 24) and 0xFF).toByte()
  }
  fun le16(v: Int) {
    header[i++] = (v and 0xFF).toByte()
    header[i++] = ((v shr 8) and 0xFF).toByte()
  }

  // RIFF chunk descriptor. The size field covers everything AFTER it, i.e.
  // the 36 bytes of remaining header plus the payload.
  ascii("RIFF")
  le32(36L + pcmBytes)
  ascii("WAVE")

  // fmt subchunk — 16 bytes for PCM.
  ascii("fmt ")
  le32(16L)
  le16(WAVE_FORMAT_PCM)
  le16(format.channels)
  le32(format.sampleRate.toLong())
  le32(format.byteRate.toLong())
  le16(format.bytesPerFrame)          // block align
  le16(format.bitsPerSample)

  // data subchunk — the payload follows immediately.
  ascii("data")
  le32(pcmBytes)

  return header
}

/**
 * Patch the two size fields of an already-written header in place.
 *
 * Called when a segment closes. Seeking to the two offsets is far cheaper than
 * rewriting the file, and it is the reason a segment can be finalized in
 * constant time regardless of how long it is.
 *
 * Returns false if the file is too short to hold a header — meaning capture
 * died before the placeholder was written, so there is nothing to patch.
 */
fun finalizeWavHeader(file: File, pcmBytes: Long): Boolean {
  if (!file.exists() || file.length() < WAV_HEADER_BYTES) return false
  RandomAccessFile(file, "rw").use { raf ->
    fun writeLe32At(offset: Long, v: Long) {
      raf.seek(offset)
      raf.write(
        byteArrayOf(
          (v and 0xFF).toByte(),
          ((v shr 8) and 0xFF).toByte(),
          ((v shr 16) and 0xFF).toByte(),
          ((v shr 24) and 0xFF).toByte(),
        )
      )
    }
    writeLe32At(4L, 36L + pcmBytes)   // RIFF size
    writeLe32At(40L, pcmBytes)        // data size
  }
  return true
}

/**
 * What a parsed WAV tells us: its format, and where its PCM payload lives.
 *
 * `dataOffset`/`dataBytes` are what the merger streams from, which is how the
 * merge drops every intermediate header instead of copying it into the middle
 * of the output.
 */
data class WavInfo(
  val format: WavFormat,
  val dataOffset: Long,
  val dataBytes: Long,
)

/**
 * Parse enough of a WAV to merge or validate it.
 *
 * Chunks are WALKED rather than assumed to sit at fixed offsets. A 44-byte
 * canonical layout is what we write ourselves, but a file that has been
 * through any other tool may carry LIST/fact/JUNK chunks before `data`, and
 * reading the payload from a hardcoded offset 44 would then treat chunk
 * headers as audio — a burst of noise at the seam. Walking costs nothing and
 * makes the merger tolerant of any conformant input, including the hardware
 * MinuteX recorder own WAVs.
 *
 * A truncated `data` size (0, or larger than the file) is repaired from the
 * real file length: that is exactly the shape of a segment whose header never
 * got patched because the app was killed mid-recording, and recovering its
 * audio is the whole point of segmenting.
 *
 * Returns null when the bytes are not a WAV we can read.
 */
fun readWavInfo(file: File): WavInfo? {
  if (!file.exists() || file.length() < WAV_HEADER_BYTES) return null

  RandomAccessFile(file, "r").use { raf ->
    val riff = ByteArray(4).also { raf.readFully(it) }
    if (String(riff, Charsets.US_ASCII) != "RIFF") return null
    raf.skipBytes(4) // RIFF size — untrusted; the file length is authoritative
    val wave = ByteArray(4).also { raf.readFully(it) }
    if (String(wave, Charsets.US_ASCII) != "WAVE") return null

    var sampleRate = 0
    var channels = 0
    var bits = 0
    var dataOffset = -1L
    var dataBytes = 0L
    val fileLen = raf.length()

    // Walk subchunks: 4-byte id, 4-byte little-endian size, then payload.
    while (raf.filePointer + 8 <= fileLen) {
      val id = ByteArray(4).also { raf.readFully(it) }
      val idStr = String(id, Charsets.US_ASCII)
      val size = readLe32(raf)
      val payloadAt = raf.filePointer

      if (idStr == "fmt ") {
        val audioFormat = readLe16(raf)
        channels = readLe16(raf)
        sampleRate = readLe32(raf).toInt()
        readLe32(raf)               // byte rate — derived, not trusted
        readLe16(raf)               // block align — derived, not trusted
        bits = readLe16(raf)
        // Only integer PCM can be concatenated as raw frames. Anything else
        // (IEEE float, ADPCM, a WAV-wrapped compressed codec) would need
        // decoding, so refuse rather than produce noise.
        if (audioFormat != WAVE_FORMAT_PCM) return null
      } else if (idStr == "data") {
        dataOffset = payloadAt
        val available = fileLen - payloadAt
        // Trust the smaller of declared/available, and fall back to
        // available when the declared size is absent or impossible.
        dataBytes = when {
          size <= 0L -> available
          size > available -> available
          else -> size
        }
        break                       // payload found; no need to walk on
      }

      // Chunks are word-aligned: an odd size is followed by a pad byte.
      if (size <= 0L) break         // malformed; refuse to spin
      val next = payloadAt + size + (size and 1L)
      if (next >= fileLen) break
      raf.seek(next)
    }

    if (sampleRate <= 0 || channels <= 0 || bits <= 0 || dataOffset < 0) return null
    return WavInfo(WavFormat(sampleRate, channels, bits), dataOffset, dataBytes)
  }
}

private fun readLe32(raf: RandomAccessFile): Long {
  val b = ByteArray(4).also { raf.readFully(it) }
  return (b[0].toLong() and 0xFF) or
    ((b[1].toLong() and 0xFF) shl 8) or
    ((b[2].toLong() and 0xFF) shl 16) or
    ((b[3].toLong() and 0xFF) shl 24)
}

private fun readLe16(raf: RandomAccessFile): Int {
  val b = ByteArray(2).also { raf.readFully(it) }
  return (b[0].toInt() and 0xFF) or ((b[1].toInt() and 0xFF) shl 8)
}
