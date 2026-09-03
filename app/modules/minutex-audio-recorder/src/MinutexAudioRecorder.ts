// modules/minutex-audio-recorder/src/MinutexAudioRecorder.ts — TS wrapper for
// the experimental native WAV recorder.
//
// Mirrors the shape of lib/audio-focus.ts: requireOptionalNativeModule so that
// every export degrades to a defined no-op when the native side is absent. That
// matters for three real cases — iOS (this module is Android-only), Expo Go,
// and a dev client built before this module existed. None of them should crash;
// they should simply report unavailable so the caller stays on the AAC engine.
//
// PCM never crosses this boundary. The native side writes frames straight to
// disk and this wrapper exchanges only paths and small status objects — at
// 32 KB/s a 4-hour recording would be ~460 MB of ArrayBuffers over the bridge,
// which is precisely the design this avoids.

import { Platform } from "react-native";
import { requireOptionalNativeModule } from "expo-modules-core";

/**
 * Native capture state. Mirrors CaptureState in WavRecorder.kt.
 *
 * This is the ENGINE state, not the app recording state. The app-level machine
 * (RecState in lib/rec-store.ts) has richer states — PAUSED_BY_CALL and the
 * rest — and remains the single source of truth about a recording; these only
 * describe what the microphone is doing.
 */
export type WavCaptureState =
  | "idle"
  | "recording"
  | "paused"
  | "stopping"
  | "stopped"
  | "error";

export type WavRecorderOptions = {
  /** Directory to write segments into. Accepts a file:// URI or a plain path. */
  directory: string;
  /** Recording id — the filename stem, so segments match the session. */
  id: string;
  /** @default 16000 */
  sampleRate?: number;
  /** @default 1 */
  channels?: number;
  /** @default 16 */
  bitDepth?: number;
};

export type WavRecorderStatus = {
  state: WavCaptureState;
  isRecording: boolean;
  /** Audio actually written, derived from PCM byte count — not wall clock. */
  durationSeconds: number;
  sizeBytes: number;
  /** Peak level 0..1 of the last buffer. Feeds the silence detector. */
  level: number;
  /**
   * The capture layer has seen enough consecutive digital silence to conclude
   * the microphone was taken (a call, or another app grabbing it exclusively).
   *
   * This is the authoritative interruption signal for the WAV engine. Android's
   * AudioRecord.read() does not fail when the telephony stack takes the mic —
   * it returns full buffers of zeros indefinitely — so neither `state` nor
   * `isRecording` nor `error` can reveal it. Only the frames can.
   *
   * Latched: once true it stays true for the life of the recorder object, so
   * the controller must roll to a new segment rather than wait for it to clear.
   * Absent on builds predating this field, hence optional — treat undefined as
   * false and fall back to the JS-side level check.
   */
  micUnavailable?: boolean;
  segmentCount: number;
  sampleRate: number;
  channels: number;
  bitDepth: number;
  error: string | null;
};

export type WavRecordingResult = {
  /** file:// URI of the single file to upload. */
  uri: string | null;
  durationSeconds: number;
  sizeBytes: number;
  sampleRate: number;
  channels: number;
  bitDepth: number;
  format: "wav";
  segmentCount: number;
  /** Whether segments were merged. False when there was only one. */
  merged: boolean;
  /** Non-empty only when a merge FAILED — the parts still on disk. */
  segmentUris: string[];
  /** Set when the recording completed but imperfectly. Surface it, do not hide it. */
  warning: string | null;
};

export type WavInspection =
  | { valid: false }
  | {
      valid: true;
      sampleRate: number;
      channels: number;
      bitDepth: number;
      dataBytes: number;
      durationSeconds: number;
    };

type NativeModuleShape = {
  isAvailable(): boolean;
  startRecording(options: {
    directory: string;
    id: string;
    sampleRate?: number;
    channels?: number;
    bitDepth?: number;
  }): Promise<WavRecorderStatus>;
  rollSegment(): Promise<WavRecorderStatus>;
  pauseRecording(): Promise<WavRecorderStatus>;
  resumeRecording(): Promise<WavRecorderStatus>;
  stopRecording(): Promise<WavRecordingResult>;
  getRecordingStatus(): WavRecorderStatus;
  isRecording(): boolean;
  discardRecording(): Promise<boolean>;
  inspectWav(uri: string): WavInspection;
  addListener(
    event: "onRecordingStatus",
    cb: (status: WavRecorderStatus) => void
  ): { remove(): void };
};

const Native = requireOptionalNativeModule<NativeModuleShape>("MinutexAudioRecorder");

/** Android-only, and only in a dev/production build that bundled this module. */
export const isNativeWavAvailable: boolean =
  Platform.OS === "android" && Native != null;

const IDLE_STATUS: WavRecorderStatus = {
  state: "idle",
  isRecording: false,
  durationSeconds: 0,
  sizeBytes: 0,
  level: 0,
  segmentCount: 0,
  sampleRate: 16000,
  channels: 1,
  bitDepth: 16,
  error: null,
};

/**
 * Why an explicit error rather than a silent no-op: start() failing quietly
 * would leave the app believing it is recording. Every other call is safe to
 * no-op, but this one must be loud.
 */
function requireNative(): NativeModuleShape {
  if (!Native) {
    throw new Error(
      Platform.OS === "android"
        ? "Native WAV recording is not in this build — rebuild the dev client."
        : "Native WAV recording is Android-only."
    );
  }
  return Native;
}

export async function startWavRecording(
  options: WavRecorderOptions
): Promise<WavRecorderStatus> {
  return requireNative().startRecording({
    directory: options.directory,
    id: options.id,
    sampleRate: options.sampleRate ?? 16000,
    channels: options.channels ?? 1,
    bitDepth: options.bitDepth ?? 16,
  });
}

/**
 * Close the current segment and open the next — the interruption-recovery
 * primitive, called when the mic is believed to have come back.
 */
export async function rollWavSegment(): Promise<WavRecorderStatus> {
  return requireNative().rollSegment();
}

export async function pauseWavRecording(): Promise<WavRecorderStatus> {
  if (!Native) return IDLE_STATUS;
  return Native.pauseRecording();
}

export async function resumeWavRecording(): Promise<WavRecorderStatus> {
  return requireNative().resumeRecording();
}

export async function stopWavRecording(): Promise<WavRecordingResult> {
  return requireNative().stopRecording();
}

export function getWavRecordingStatus(): WavRecorderStatus {
  if (!Native) return IDLE_STATUS;
  try {
    return Native.getRecordingStatus();
  } catch {
    return IDLE_STATUS;
  }
}

export function isWavRecording(): boolean {
  if (!Native) return false;
  try {
    return Native.isRecording();
  } catch {
    return false;
  }
}

export async function discardWavRecording(): Promise<boolean> {
  if (!Native) return false;
  try {
    return await Native.discardRecording();
  } catch {
    return false;
  }
}

/** Structural check on a WAV. Returns `{valid:false}` rather than throwing. */
export function inspectWav(uri: string): WavInspection {
  if (!Native) return { valid: false };
  try {
    return Native.inspectWav(uri);
  } catch {
    return { valid: false };
  }
}

export function addWavStatusListener(
  cb: (status: WavRecorderStatus) => void
): { remove(): void } {
  if (!Native) return { remove() {} };
  return Native.addListener("onRecordingStatus", cb);
}
