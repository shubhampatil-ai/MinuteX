# minutex-audio-recorder

Experimental Android WAV recorder: `AudioRecord` → raw PCM → WAV segments → one
merged WAV, for benchmarking transcription and diarization accuracy against the
existing AAC path.

Android only. The existing `expo-audio` AAC recorder is untouched and remains
the only engine on iOS.

## Why it exists

`MediaRecorder` — what `expo-audio` uses — cannot emit PCM on Android at all.
Its encoder list is `aac / he_aac / aac_eld / amr` and its container list has no
WAV, so a real WAV file is only reachable by reading frames from `AudioRecord`
and writing the container ourselves.

`expo-audio` does ship an `AudioRecord` path (`AudioStream`), but it delivers
buffers to **JavaScript** and has no file writer. At 32 KB/s that is ~460 MB of
`ArrayBuffer`s over the bridge for a 4-hour recording, and any JS-thread stall
drops audio permanently. This module keeps PCM entirely native: nothing but
paths and small status objects crosses the bridge.

## Format

| | |
|---|---|
| Sample rate | 16,000 Hz (configurable) |
| Bit depth | 16-bit (fixed — the writer emits integer PCM) |
| Channels | 1, mono (configurable) |
| Encoding | PCM 16-bit little-endian |
| Container | WAV, canonical 44-byte RIFF/WAVE header |
| Audio source | `VOICE_RECOGNITION`, falling back to `MIC` |
| Bitrate | 32 KB/s ≈ 1.92 MB/min ≈ 115 MB/hour |

`VOICE_RECOGNITION` rather than `MIC` because most OEMs leave it free of the
AGC/noise-suppression tuned for voice calls. Aggressive OEM processing is
exactly the variable that would make a WAV-vs-AAC comparison meaningless.

## Files

| File | Role |
|---|---|
| `WavFile.kt` | Header build/patch, chunk-walking parser |
| `WavMerger.kt` | Streaming segment merge (256 KB buffer) |
| `WavRecorder.kt` | `AudioRecord` capture loop, one segment |
| `WavRecordingService.kt` | Microphone foreground service |
| `MinutexAudioRecorderModule.kt` | JS surface, segment bookkeeping |
| `src/MinutexAudioRecorder.ts` | TS wrapper, degrades to no-ops off-Android |

## Interruption recovery

A phone call takes the mic and `AudioRecord` starts returning zeros rather than
failing. Recovery is the same strategy the AAC path uses — a fresh recorder —
which means a fresh file:

```
{id}_segment_001.wav   before the call
{id}_segment_002.wav   after it
{id}_final.wav         merged at stop
```

**Segments are never byte-concatenated.** Appending WAVs leaves a 44-byte header
mid-stream (an audible click) and the leading header still declares only the
first segment's length, so players stop early and the rest is silently
invisible. `mergeWavSegments` instead reads each header, verifies the formats
agree, copies only the `data` payloads, and writes one new header describing the
combined length.

The merge streams through a fixed buffer, writes to `{name}.merging`, validates
the result, and only then renames into place — so a crash mid-merge leaves the
sources untouched. Sources are deleted **only** after the merged file exists and
re-validates.

## Memory

Flat regardless of duration. One read buffer (~1s of audio), one 64 KB output
stream, one 256 KB merge buffer. PCM is never accumulated in RAM or JS.

Expected sizes: 5 min ≈ 10 MB · 1 hr ≈ 115 MB · 3 hr ≈ 346 MB · 4 hr ≈ 461 MB.
The 3 GB upload ceiling is reached at roughly 26 hours.

## Background recording

The module starts a `foregroundServiceType="microphone"` service **before**
opening the mic. On Android 9+ a backgrounded app keeps reading successfully but
gets **silence**, and from Android 14 `startForeground` throws without the mic
service type.

`app.json` already declares `FOREGROUND_SERVICE`,
`FOREGROUND_SERVICE_MICROPHONE`, `POST_NOTIFICATIONS` and `RECORD_AUDIO`, so no
permission changes were needed. The service is declared in this module's own
`AndroidManifest.xml` and merges in at prebuild.

Its own notification channel (`minutex_wav_recording_channel`, `IMPORTANCE_LOW`,
sound and vibration off) keeps it distinct from `expo-audio`'s, and stops a
notification chime being captured by the mic announcing it.

**Not covered:** force-stop, OEM battery managers, and Doze on aggressive ROMs
can still kill the process. Segments already on disk survive — the header is
written up front, and the parser repairs an unpatched size from the file length,
so an orphaned segment is still readable and mergeable.

If `startForegroundService` is refused the recording proceeds foreground-only
rather than failing, matching how the AAC path treats a declined notification
permission.

## Permissions

Uses the existing flow in `lib/permissions.ts` / `rec-controller.ts`. Native
code only *checks* `RECORD_AUDIO` and returns `ERR_NO_MIC_PERMISSION`; it never
prompts.

## Error codes

| Code | Meaning |
|---|---|
| `ERR_NO_MIC_PERMISSION` | `RECORD_AUDIO` not granted |
| `ERR_MIC_UNAVAILABLE` | Another app holds the mic, or init failed |
| `ERR_BAD_ARGS` | Missing `directory` or `id` |
| `ERR_DIRECTORY` | Could not create the output directory |
| `ERR_NO_AUDIO` | Stopped with no segment holding audio |
| `ERR_RECORDER` | Unsupported rate/channels, disk full, write failure |

A recording is never reported successful when the WAV is incomplete:
`stopRecording` returns a `warning`, and `validateWavForUpload` re-checks the
header before the session is marked `COMPLETED`.

## Known limitations

- **Android only.** iOS keeps `expo-audio`/`.m4a` unchanged.
- **16-bit only.** 8-bit and float would need a different `fmt` chunk and a
  different level calculation.
- **Level is peak, not RMS.** Computed from frames already in hand and converted
  to dBFS so the existing silence detector works unchanged; it is not
  numerically identical to `MediaRecorder.getMaxAmplitude()`.
- **Requires a native rebuild.** Not available in Expo Go or any dev client
  built before this module existed. `isNativeWavAvailable` reports this and the
  engine falls back to AAC rather than failing.
- **A ~1s buffer means up to ~1s of tail audio** can be lost if the process is
  killed without `stop()` — the merge recovers everything already flushed.
