# Testing the experimental WAV recorder

## Build

The module is native code, so Metro reload is not enough — and EAS builds from
**git**, not the working tree. Commit first or the build will not contain it
(`lib/__tests__/native-config-committed.test.mjs` fails deliberately until you
do).

```bash
cd app
git add modules/ lib/ src/ && git commit -m "Add experimental native WAV recorder"

# Local build (fastest iteration; needs Android SDK + JDK 17)
npx expo prebuild --platform android --clean
npx expo run:android

# or EAS
npx eas build --profile development --platform android
```

Then:

```bash
npx expo start --dev-client
npm test          # 133 pass; native-config guard clears once committed
npx tsc --noEmit  # errors under example/ are pre-existing scaffolding
```

## Enabling WAV

WAV is the default on Android in this build (`DEFAULT_ENGINE` in
`lib/rec-engine.ts`). Toggle per device in **Profile → Settings → High-quality
WAV recording**. The change applies to the **next** recording — an in-flight one
keeps the engine it started with.

To verify which engine ran, check the session sidecar:

```bash
adb shell run-as com.hp.airecorder \
  cat files/minutex-recordings/<id>.json | python -m json.tool | grep -E 'engine|format'
```

Expect `"engine": "wav"`, `"format": "wav"`.

## Pulling files

```bash
adb shell run-as com.hp.airecorder ls -la files/minutex-recordings/
adb exec-out run-as com.hp.airecorder cat files/minutex-recordings/<id>_final.wav > final.wav
```

Inspect with `ffprobe`. Every check below assumes it is available.

```bash
ffprobe -v error -show_entries \
  stream=codec_name,sample_rate,channels,bits_per_sample,duration \
  -of default=noprint_wrappers=1 final.wav
```

Expected: `pcm_s16le`, `16000`, `1`, `16`.

---

## A. Normal recording (2 min)

1. Record ~2 minutes of speech, stop, let it upload.

**Expect:** one `{id}_final.wav`, no `_segment_` files left behind.
**Inspect:** `ffprobe` shows `pcm_s16le / 16000 / 1`; duration within ~1s of the
UI timer; size ≈ `duration × 32000` bytes (±44).
**Fail if:** duration is 0:00 (header never patched), size is 44 bytes, or
`codec_name` is anything but `pcm_s16le`.

## B. 5 minutes

**Expect:** ≈ 9.6 MB. Waveform moves throughout.
**Inspect:** play the last 30s — audio must be present at the end, not silence.
**Fail if:** the file stops early, or the tail is silent (a dropped read loop).

## C. 30 minutes

**Expect:** ≈ 57.6 MB.
**Inspect:** memory during the run —

```bash
adb shell dumpsys meminfo com.hp.airecorder | grep -E "TOTAL PSS|Native Heap"
```

Sample at 1, 15 and 30 minutes. **Native heap must be flat** — that is the whole
point of streaming writes.
**Fail if:** PSS climbs steadily with duration.

## D. 1 hour

**Expect:** ≈ 115 MB, flat memory, no thermal throttling.
**Inspect:** `adb shell dumpsys battery` before/after; spot-check audio at 0:30,
30:00 and 59:30.

## E. 3–4 hours

**Expect:** ≈ 346 MB / 461 MB, well under the 3 GB ceiling.
**Inspect:** free space before starting (needs headroom for segments *and* the
merged copy — worst case ~2× peak during merge). Confirm the merge completes and
`_segment_` files are gone afterwards.
**Fail if:** the merge fails for lack of space — the fallback keeps the longest
segment and sets a `warning`, which is correct behaviour but a partial recording.

## F. Phone call interruption

1. Start recording, speak ~30s.
2. Call the device from another phone. **Answer it.** Talk ~20s. Hang up.
3. Keep recording ~30s. Stop.

**Expect:** UI shows "Paused because of a call", then auto-resumes. Final file
contains segments 1 and 3 merged — the call itself is *not* recorded.
**Inspect:**

```bash
ffprobe -v error -show_entries format=duration -of csv=p=0 final.wav
```

Duration ≈ 60s (the two speech stretches), **not** 80s.

```bash
# exactly one RIFF marker — the defining property of a correct merge
grep -abo RIFF final.wav | wc -l     # must be 1
```

Play across the seam: audible join, no click, no truncation.
**Fail if:** duration stops at 30s (byte-join bug), more than one `RIFF` marker
appears, or the post-call audio is silent.

## G. Ringing but not answered

Call the device and let it ring out **without** answering.

**Expect:** recording **continues** — a ring does not take the mic. Ring-aware
handling is shared with the AAC path (`getCallPhase()`).
**Fail if:** it pauses on the ring, or the ringtone is audible (the ringer should
be silenced when Do Not Disturb access was granted).

## H. Multiple interruptions

Three answered calls during one recording.

**Expect:** four segments merged into one file; `segmentCount: 4` in the log.
**Inspect:** duration = sum of the four speech stretches; still exactly one
`RIFF`; all four stretches audible in order.

## I. App backgrounding

Start recording, press Home, use another app for 5 minutes, return, stop.

**Expect:** the "MinuteX is recording" notification stays visible throughout;
audio from the backgrounded period is **real speech, not silence**.
**Inspect:** play the middle of the file. This is the check the foreground
service exists for.
**Fail if:** the middle is digital silence — the service did not hold. Confirm
with `adb shell dumpsys activity services | grep -i wavrecording`.

## J. Screen locked

As I, but lock the screen.

**Expect:** recording continues. On aggressive OEM ROMs (Xiaomi, Oppo, Huawei)
it may still be killed — a documented limitation, not a code bug. Check whether
battery optimisation is disabled for the app before filing anything.

## K. Low storage

Fill the device to under ~200 MB free, then record.

**Expect:** start is refused with a clear message (`MIN_FREE_BYTES_TO_START`).
If space runs out mid-recording, it auto-saves rather than dying — bytes already
written stay valid.
**Fail if:** it crashes, or reports success with a truncated file.

## L. Upload and transcription

**Expect:** presign with `format: "wav"` → `Content-Type: audio/wav` → S3 key
ending `.wav`, transcription completes.
**Inspect:** the backend needs **no changes** — `wav` is already in
`UPLOAD_FORMATS` (`cloud/functions/userapi/lambda_function.py`) and `AUDIO_EXTS`
(`cloud/functions/transcribe/lambda_function.py`).
**Fail if:** presign 400s on the format — that would mean `formatForEngine()`
and the real bytes disagree.

## M. AAC regression (do not skip)

Toggle WAV **off**, then re-run A, F and L.

**Expect:** identical behaviour to before this change — `.aac`, ADTS byte-join
via `concatSegments`, `format: "aac"`.
**Fail if:** anything differs. The AAC path must be untouched.

## N. iOS regression

Build iOS and record.

**Expect:** `.m4a`, no WAV toggle visible in Settings, no native module loaded.
`isNativeWavAvailable === false`.

---

# Audio quality benchmark

The comparison this module exists for. **Do not claim WAV is better until these
numbers say so** — plausible reasoning about bitrate is not evidence.

## Method

Same room, same speakers, same content, back to back. Ideally record the *same*
meeting twice on two devices, or the same played-back reference audio.

| Arm | Engine | Format |
|---|---|---|
| Control | `expo-audio` | AAC 128 kbps, 44.1 kHz stereo, ~1 MB/min |
| Test | native | PCM 16 kHz mono, ~1.92 MB/min |

Note the sample-rate confound: the control is 44.1 kHz stereo and the test is
16 kHz mono, so a difference could come from **either** the codec or the rate.
To isolate the codec, add a third arm — AAC forced to 16 kHz mono via
`REC_OPTIONS` — before concluding anything about compression.

Aim for ≥ 5 sessions of ≥ 20 minutes with ≥ 3 speakers, including deliberate
crosstalk and background noise.

## Measure

**Transcription** — WER against a human reference: substitutions + deletions +
insertions ÷ reference words. Track deletions separately; missed speech matters
more than a misspelling for meeting notes.

**Diarization** — DER, plus speaker-count accuracy, switch precision/recall at
turn boundaries, and behaviour on overlapping speech.

**Cost** — upload wall-clock on wifi and LTE; peak/mean PSS
(`dumpsys meminfo`); CPU (`adb shell top -p $(adb shell pidof com.hp.airecorder)`);
battery drop per hour (`dumpsys batterystats`).

**Robustness** — interruption recovery success rate over ≥ 10 answered calls per
arm, and how much audio each arm loses per interruption.

## Record results as

| Metric | AAC | WAV 16k | Δ |
|---|---|---|---|
| WER % | | | |
| Deletion rate % | | | |
| DER % | | | |
| Speaker-count accuracy | | | |
| MB per hour | ~60 | ~115 | +92% |
| Upload time (LTE, 1 hr) | | | |
| Peak PSS (MB) | | | |
| Battery %/hour | | | |
| Interruption recovery | | | |

## Deciding

Adopt WAV only if transcription/diarization gains are **material** — a WER
improvement inside run-to-run variance does not justify ~2× the bytes. Run each
arm at least twice to estimate that variance before comparing arms.

If gains are marginal, the cheaper win is likely AAC at 16 kHz mono: most of the
size benefit, most of any rate benefit, and it keeps the proven recorder. If
they are material, flip `DEFAULT_ENGINE` deliberately and revisit the cellular
upload story at ~115 MB/hour.
