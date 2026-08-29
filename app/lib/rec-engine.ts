// lib/rec-engine.ts — which recording engine a recording uses.
//
// Two engines exist side by side so AAC and WAV can be compared on real
// meetings rather than argued about:
//
//   "aac"  expo-audio / MediaRecorder -> ADTS-AAC (the original, unchanged)
//   "wav"  modules/minutex-audio-recorder -> AudioRecord -> PCM WAV
//
// WHY A MODULE AND NOT A CONSTANT. The choice has to be readable
// SYNCHRONOUSLY: rec-controller.ts picks an engine inside start(), and the
// store here is expo-secure-store, which is async only. So the persisted value
// is loaded once at boot into a cached variable, and every read after that is
// synchronous against the cache. A read before the load completes falls back to
// the default, which is correct — the first recording of a cold start uses the
// default engine rather than blocking on storage.
//
// The engine is captured onto the session at start() and never re-read, so
// flipping the toggle mid-recording cannot change the format of a file that is
// already being written.

import { Platform } from "react-native";
import { store } from "./storage";
import { isNativeWavAvailable } from "../modules/minutex-audio-recorder/src/MinutexAudioRecorder";

export type RecEngine = "aac" | "wav";

const ENGINE_KEY = "minutex.recording.engine";

/**
 * The default engine.
 *
 * Set to "wav" at the explicit request of the project owner, overriding the
 * usual "keep the proven path as default" instinct. The consequences are real
 * and are the reason this constant is documented rather than inlined:
 *
 *   * Size. WAV at 16 kHz/16-bit/mono is ~1.92 MB/min vs ~1 MB/min for AAC —
 *     a 3-hour meeting is ~350 MB instead of ~180 MB. Still inside the 3 GB
 *     upload ceiling (about 26 hours of audio), but a much heavier upload on
 *     cellular.
 *   * Maturity. The AAC path has shipped and survived real interruptions. The
 *     WAV path is new code touching the microphone directly.
 *
 * Set this to "aac" to make the proven engine the default again — nothing else
 * needs to change.
 */
const DEFAULT_ENGINE: RecEngine = "wav";

/**
 * iOS has no native WAV engine (AudioRecord is Android-only) and the brief is
 * explicit that iOS must not change. So iOS is always AAC — this is a floor
 * that no stored preference can override.
 */
function coerce(engine: RecEngine): RecEngine {
  if (Platform.OS !== "android") return "aac";
  // A build that predates the native module, or Expo Go: asking for WAV would
  // throw at start(). Fall back rather than fail to record.
  if (engine === "wav" && !isNativeWavAvailable) return "aac";
  return engine;
}

let cached: RecEngine = coerce(DEFAULT_ENGINE);
let loaded = false;

/**
 * The engine to use for the NEXT recording. Synchronous by design — see the
 * header for why.
 */
export function getRecEngine(): RecEngine {
  return coerce(cached);
}

/** Whether the WAV engine can be offered on this build at all. */
export function canUseWavEngine(): boolean {
  return Platform.OS === "android" && isNativeWavAvailable;
}

/** Load the persisted preference. Call once, early. Safe to call twice. */
export async function loadRecEngine(): Promise<RecEngine> {
  if (loaded) return getRecEngine();
  try {
    const v = await store.getItemAsync(ENGINE_KEY);
    if (v === "aac" || v === "wav") cached = v;
  } catch {
    // Storage unavailable — the default stands.
  }
  loaded = true;
  return getRecEngine();
}

/** Persist a choice. Takes effect on the next recording, not the current one. */
export async function setRecEngine(engine: RecEngine): Promise<void> {
  cached = engine;
  loaded = true;
  try {
    await store.setItemAsync(ENGINE_KEY, engine);
  } catch {
    // In-memory only for this session; the cache above still honours it.
  }
}

/**
 * The upload `format` key for an engine — the value the presign is signed
 * with, so it must match the real bytes on disk.
 *
 * iOS records .m4a, Android AAC records ADTS .aac, and the WAV engine records
 * .wav. All three are already in UPLOAD_FORMATS on the backend, so no backend
 * change is needed for any of them.
 */
export function formatForEngine(engine: RecEngine): string {
  if (engine === "wav") return "wav";
  return Platform.OS === "android" ? "aac" : "m4a";
}
