package expo.modules.minutexaudiorecorder

// Merge WAV segments into one valid WAV.
//
// WHY A MERGER EXISTS AT ALL: an interruption that takes the microphone forces
// a brand new AudioRecord, and a new AudioRecord means a new file. The upload
// pipeline and the backend both assume exactly one file per recording, so the
// pieces have to become one.
//
// WHY NOT BYTE CONCATENATION. This is the trap the AAC path gets away with and
// WAV does not. Appending segment_002.wav to segment_001.wav leaves a 44-byte
// RIFF header sitting in the middle of the audio stream — players decode it as
// a click, and the first header still declares only the first segment length,
// so every player stops at the original duration and the rest is silently
// invisible. The existing lib/rec-store.ts concatSegments() guards against
// this explicitly (it refuses any format that is not `aac`), which is why the
// WAV engine routes through here instead.
//
// The correct operation is: read each header, verify the formats agree, copy
// only the PCM payloads back to back, and write ONE new header describing the
// combined length.
//
// MEMORY: payloads are streamed through a fixed 256 KB buffer. A 4-hour
// recording is ~460 MB of PCM and must never be held in RAM, so nothing here
// ever allocates proportional to the audio length.

import java.io.File
import java.io.FileOutputStream
import java.io.RandomAccessFile

/** Streaming copy buffer. Large enough to keep IO efficient, small and fixed. */
private const val COPY_BUFFER_BYTES = 256 * 1024

/** Outcome of a merge attempt. `error` is set only when `ok` is false. */
data class MergeResult(
  val ok: Boolean,
  val pcmBytes: Long,
  val durationSeconds: Double,
  val segmentsMerged: Int,
  val error: String? = null,
)

/**
 * Merge [segments] (in capture order) into [target].
 *
 * Segments that cannot be read, or whose format disagrees with the first
 * readable one, are SKIPPED rather than failing the whole merge — losing one
 * damaged segment beats losing the entire meeting. The count of what actually
 * made it in comes back as `segmentsMerged`, so the caller can report honestly
 * when it is fewer than expected.
 *
 * The target is written to a temporary file and only renamed into place once it
 * is complete and re-validated. That ordering is what makes it safe for the
 * caller to delete the source segments afterwards: a crash mid-merge leaves the
 * sources untouched and no half-file masquerading as the final recording.
 */
fun mergeWavSegments(segments: List<File>, target: File): MergeResult {
  if (segments.isEmpty()) {
    return MergeResult(false, 0L, 0.0, 0, "no segments to merge")
  }

  // Pass 1: read every header. Cheap (a few hundred bytes each) and it tells us
  // the total payload size up front, so the final header can be written
  // correctly on the first pass instead of patched afterwards.
  val usable = ArrayList<Pair<File, WavInfo>>(segments.size)
  var reference: WavFormat? = null
  var skipped = 0

  for (seg in segments) {
    val info = readWavInfo(seg)
    if (info == null || info.dataBytes <= 0L) {
      skipped++
      continue
    }
    val ref = reference
    if (ref == null) {
      reference = info.format
    } else if (info.format != ref) {
      // Mismatched rate/channels/depth. Concatenating these would play at the
      // wrong speed or swap channels — corruption that looks like audio, so it
      // must be refused rather than accepted.
      skipped++
      continue
    }
    usable.add(seg to info)
  }

  val format = reference
  if (format == null || usable.isEmpty()) {
    return MergeResult(false, 0L, 0.0, 0, "no readable WAV segments")
  }

  val totalPcm = usable.sumOf { it.second.dataBytes }
  if (totalPcm <= 0L) {
    return MergeResult(false, 0L, 0.0, 0, "segments contained no audio")
  }

  // Write beside the target so the rename is on the same filesystem (a rename
  // across mount points is not atomic and can silently fall back to a copy).
  val tmp = File(target.parentFile, target.name + ".merging")
  if (tmp.exists()) tmp.delete()

  var written = 0L
  try {
    FileOutputStream(tmp).use { out ->
      out.write(buildWavHeader(format, totalPcm))

      val buf = ByteArray(COPY_BUFFER_BYTES)
      for ((file, info) in usable) {
        RandomAccessFile(file, "r").use { raf ->
          raf.seek(info.dataOffset)
          var remaining = info.dataBytes
          while (remaining > 0L) {
            val want = if (remaining < buf.size) remaining.toInt() else buf.size
            val n = raf.read(buf, 0, want)
            if (n <= 0) break            // short file; keep what we copied
            out.write(buf, 0, n)
            written += n
            remaining -= n
          }
        }
      }
      // Push to the OS before we claim success. Without this a merge can be
      // reported complete while bytes are still buffered, and a crash in the
      // window that follows would leave a truncated "final" file.
      out.flush()
      out.fd.sync()
    }
  } catch (e: Exception) {
    tmp.delete()
    return MergeResult(false, 0L, 0.0, 0, "merge write failed: ${e.message}")
  }

  // A short read anywhere means the header we wrote overstates the payload.
  // Patch it down to what actually landed rather than shipping a file whose
  // declared length exceeds its contents.
  if (written != totalPcm) {
    finalizeWavHeader(tmp, written)
  }

  // Re-validate before committing. This is the check that stops a structurally
  // broken file from ever being handed to the upload pipeline.
  val check = readWavInfo(tmp)
  if (check == null || check.dataBytes <= 0L) {
    tmp.delete()
    return MergeResult(false, 0L, 0.0, 0, "merged file failed validation")
  }

  if (target.exists()) target.delete()
  if (!tmp.renameTo(target)) {
    tmp.delete()
    return MergeResult(false, 0L, 0.0, 0, "could not move merged file into place")
  }

  return MergeResult(
    ok = true,
    pcmBytes = written,
    durationSeconds = format.durationOf(written),
    segmentsMerged = usable.size,
    error = if (skipped > 0) "skipped $skipped unreadable segment(s)" else null,
  )
}
