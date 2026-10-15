// lib/audio-level.ts — turning a microphone's input level into waveform data.
//
// PURE. No React, no React Native, no native modules — so every rule that
// decides how tall a bar is can be pinned by a plain node test
// (lib/__tests__/audio-level.test.mjs), and the renderer (lib/waveform.tsx)
// never has to be trusted with signal processing.
//
// The pipeline, in order:
//
//   native input level (dBFS)          rec-controller.readInputLevel()
//        │  RMS where the engine can compute it (WAV: every PCM frame),
//        │  otherwise the platform's own meter (AAC: MediaRecorder peak on
//        │  Android, AVAudioRecorder average power on iOS)
//        ▼
//   dbfsToLevel()        dB → 0..1 on a perceptual (log) scale, noise-gated
//        ▼
//   LevelSmoother        fast attack / slow release — speech onsets land
//        │               immediately, but a bar does not collapse between
//        │               syllables, which is what reads as "jitter"
//        ▼
//   LevelHistory         fixed-size ring: the last N smoothed samples,
//        │               oldest → newest
//        ▼
//   <LevelWaveform>      draws the array. Knows nothing about audio.
//
// WHAT THIS DELIBERATELY IS NOT: a spectrum. Neither engine exposes
// frequency-domain data, and replacing the recorder to get an FFT would put
// the most hardened code in the app at risk for a visual. An amplitude
// envelope is also the honest answer to the only question a recorder's
// waveform has to answer: "is it hearing me, and how loudly?"

/**
 * The dBFS window mapped onto 0..1.
 *
 * FLOOR: a silent room on a phone mic meters around -60 to -50 dBFS. Setting
 * the floor at -50 means room tone renders as (near) nothing, so silence
 * looks like silence — not a restless carpet of small bars.
 *
 * CEIL: -6 dBFS is already very loud for a meeting (someone leaning into the
 * phone). Mapping full scale to the top would leave normal speech in the
 * bottom third; this puts conversational speech (~-30..-18 dBFS RMS) across
 * the middle of the range, where the difference between quiet and loud
 * speech is actually visible.
 */
export const LEVEL_FLOOR_DB = -50;
export const LEVEL_CEIL_DB = -6;

/**
 * Below this (post-mapping) level the bar is drawn as silence. Stops the last
 * flickers of room tone from showing as a baseline that never settles.
 */
export const LEVEL_GATE = 0.04;

/** The engines report exactly -160 dB for a hardware zero / no reading. */
const NO_SIGNAL_DB = -159;

/**
 * Map a dBFS reading to a display level in [0, 1].
 *
 * dB is already a logarithmic (perceptual) scale, so a linear map over the
 * window above is what makes "twice as loud" look like a similar step at any
 * volume. Non-finite and hardware-zero readings are silence, never an error.
 */
export function dbfsToLevel(
  db: number | null | undefined,
  floor = LEVEL_FLOOR_DB,
  ceil = LEVEL_CEIL_DB,
): number {
  if (db == null || !Number.isFinite(db) || db <= NO_SIGNAL_DB) return 0;
  if (ceil <= floor) return 0;
  const v = (db - floor) / (ceil - floor);
  if (v <= LEVEL_GATE) return 0;
  return v >= 1 ? 1 : v;
}

/**
 * Linear RMS amplitude (0..1, where 1 is full-scale) to dBFS. Exposed for the
 * engines that report linear levels; 0 maps to the "no signal" value so it
 * flows through dbfsToLevel as silence.
 */
export function linearToDbfs(amplitude: number | null | undefined): number {
  if (amplitude == null || !Number.isFinite(amplitude) || amplitude <= 0) return -160;
  const db = 20 * Math.log10(Math.min(1, amplitude));
  return db < -160 ? -160 : db;
}

/**
 * Asymmetric exponential smoothing.
 *
 * `attack` is the weight given to a RISING sample, `release` to a falling one.
 * High attack (0.6) means a word shows up within one or two samples; low
 * release (0.18) means the envelope decays over a few hundred milliseconds
 * instead of snapping to zero between syllables. Symmetric smoothing forces a
 * bad trade: either laggy onsets or a flickering tail.
 *
 * Values are clamped to [0, 1] on the way in, so a bad reading cannot push
 * the state outside the range the renderer expects.
 */
export class LevelSmoother {
  private value = 0;
  private readonly attack: number;
  private readonly release: number;

  // Plain fields rather than constructor parameter properties: this module is
  // imported by a node test through Node's type stripping, which accepts only
  // erasable TypeScript syntax.
  constructor(attack = 0.6, release = 0.18) {
    this.attack = attack;
    this.release = release;
  }

  next(sample: number): number {
    const s = Number.isFinite(sample) ? Math.max(0, Math.min(1, sample)) : 0;
    const k = s > this.value ? this.attack : this.release;
    this.value += (s - this.value) * k;
    // Snap tiny tails to zero so silence settles to exactly flat instead of
    // asymptotically approaching it forever.
    if (this.value < 0.005) this.value = 0;
    return this.value;
  }

  current(): number {
    return this.value;
  }

  reset(): void {
    this.value = 0;
  }
}

/**
 * The last `size` samples, oldest first. A fixed-size ring, so a three-hour
 * recording costs exactly as much memory as a three-second one.
 */
export class LevelHistory {
  readonly size: number;
  private readonly buf: number[];
  private head = 0;
  private filled = 0;

  constructor(size: number) {
    if (!Number.isInteger(size) || size <= 0) throw new Error("LevelHistory size must be a positive integer");
    this.size = size;
    this.buf = new Array<number>(size).fill(0);
  }

  push(v: number): void {
    this.buf[this.head] = v;
    this.head = (this.head + 1) % this.size;
    if (this.filled < this.size) this.filled += 1;
  }

  /**
   * Oldest → newest, always exactly `size` long. Slots not yet filled are
   * `-1`, which the renderer draws as an empty placeholder dot — distinct from
   * a genuine 0 (silence that was actually recorded).
   */
  toArray(): number[] {
    const out = new Array<number>(this.size);
    const empty = this.size - this.filled;
    for (let i = 0; i < empty; i++) out[i] = -1;
    for (let i = 0; i < this.filled; i++) {
      out[empty + i] = this.buf[(this.head - this.filled + i + this.size) % this.size];
    }
    return out;
  }

  count(): number {
    return this.filled;
  }

  clear(): void {
    this.buf.fill(0);
    this.head = 0;
    this.filled = 0;
  }
}

/**
 * One step of the whole pipeline: raw dB in, the updated history out.
 * Kept as a free function so the hook and the tests drive exactly the same
 * code path.
 */
export function stepWaveform(
  db: number | null | undefined,
  smoother: LevelSmoother,
  history: LevelHistory,
): number[] {
  history.push(smoother.next(dbfsToLevel(db)));
  return history.toArray();
}
