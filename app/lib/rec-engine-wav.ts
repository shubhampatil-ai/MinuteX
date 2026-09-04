// lib/rec-engine-wav.ts — adapts the native WAV recorder to the shape
// rec-controller.ts already drives.
//
// WHY AN ADAPTER RATHER THAN BRANCHES EVERYWHERE. rec-controller.ts is a 1700-
// line state machine that has been hardened against real interruptions: ring vs
// answer, digital-silence detection, stall counting, disk pressure, duration
// reconciliation. Threading `if (engine === "wav")` through all of it would
// double the number of paths every one of those behaviours has to be correct
// on, and the AAC path — the one that currently works — would be the thing put
// at risk.
//
// So the WAV engine is presented through the SAME small surface the controller
// already uses on the recorder object: getStatus() returning metering in dBFS
// and isRecording, plus stop/pause/release. The controller keeps one code path;
// only construction and finalize differ.
//
// The deliberate asymmetry: metering. The AAC path gets dBFS from
// MediaRecorder.getMaxAmplitude(); AudioRecord has no equivalent, so
// WavRecorder.kt computes a peak from the frames it is already holding and this
// adapter converts it to the same dBFS scale. That keeps the controller
// silence detector (SILENCE_DB_FLOOR) working unchanged, which is what makes
// mic-theft detection work for WAV too.

import {
  addWavStatusListener,
  discardWavRecording,
  getWavRecordingStatus,
  inspectWav,
  pauseWavRecording,
  resumeWavRecording,
  rollWavSegment,
  startWavRecording,
  stopWavRecording,
  type WavRecordingResult,
} from "../modules/minutex-audio-recorder/src/MinutexAudioRecorder";

/**
 * The subset of expo-audio RecorderState that rec-controller.ts actually
 * reads. Keeping this narrow is what lets one controller drive both engines.
 */
export type EngineStatus = {
  isRecording: boolean;
  /** dBFS, ~-160..0, matching expo-audio metering. */
  metering: number | null;
  /** Always false — Android has no mediaServicesDidReset. */
  mediaServicesDidReset: boolean;
  durationMillis: number | null;
  url: string | null;
  /**
   * The engine itself has concluded the microphone was taken.
   *
   * Only the WAV engine can report this: it sees the PCM frames, so it can tell
   * a zero-filled stream from a quiet room. The AAC path leaves it undefined
   * and keeps relying on the controller's metering-based detector, so treat
   * undefined as "no opinion", never as "the mic is fine".
   */
  micUnavailable?: boolean;
};

/** Floor used when the peak is exactly zero — log10(0) is undefined. */
const DIGITAL_SILENCE_DB = -160;

/**
 * Linear peak (0..1) to dBFS.
 *
 * A true digital zero must map to the floor rather than to a small negative
 * number, because SILENCE_DB_FLOOR in the controller is what detects a stolen
 * microphone. Getting this wrong in the safe-looking direction would silently
 * disable interruption detection for the WAV engine.
 */
function peakToDb(peak: number): number {
  if (!Number.isFinite(peak) || peak <= 0) return DIGITAL_SILENCE_DB;
  const db = 20 * Math.log10(peak);
  return db < DIGITAL_SILENCE_DB ? DIGITAL_SILENCE_DB : db;
}

/**
 * A live WAV recording, exposing the controller expected shape.
 *
 * Instances are cheap: all real state lives in the native module, which is the
 * single owner of the mic and the files. This wrapper holds no audio.
 */
export class WavEngineRecorder {
  /** file:// URI of the CURRENT segment, or the merged file once stopped. */
  uri: string | null = null;

  private stopped = false;
  private finalResult: WavRecordingResult | null = null;

  constructor(private readonly sessionId: string) {}

  static async begin(
    sessionId: string,
    directoryUri: string
  ): Promise<WavEngineRecorder> {
    const engine = new WavEngineRecorder(sessionId);
    await startWavRecording({
      directory: directoryUri,
      id: sessionId,
      sampleRate: 16000,
      channels: 1,
      bitDepth: 16,
    });
    engine.uri = null; // segment paths are internal until finalize
    return engine;
  }

  /** Segments captured so far, including the live one. For logging. */
  segmentCount(): number {
    return getWavRecordingStatus().segmentCount;
  }

  getStatus(): EngineStatus {
    const s = getWavRecordingStatus();
    return {
      isRecording: s.isRecording,
      metering: peakToDb(s.level),
      mediaServicesDidReset: false,
      durationMillis: s.durationSeconds * 1000,
      url: this.uri,
      micUnavailable: s.micUnavailable === true,
    };
  }

  /**
   * Interruption recovery. Unlike the AAC path — which must throw away the
   * whole recorder and build a new one — the native side can roll to a new
   * segment in place, keeping the mic handle logic in one place.
   *
   * Returns false when the mic is still unavailable, which is the signal the
   * controller retry/backoff already understands.
   */
  async rollSegment(): Promise<boolean> {
    try {
      await rollWavSegment();
      return true;
    } catch {
      return false;
    }
  }

  async pause(): Promise<void> {
    await pauseWavRecording();
  }

  /**
   * Resume into the SAME segment after a user pause.
   *
   * Distinct from rollSegment(): a user pause left the mic handle and the file
   * open, so there is nothing to merge. Only an interruption — which invalidates
   * the input — needs a new segment.
   */
  async resume(): Promise<void> {
    await resumeWavRecording();
  }

  /**
   * Finalize: merge segments and return the single uploadable file.
   *
   * Idempotent — the controller can reach finalize from four directions at
   * once (see the `finalizing` guard in rec-controller.ts), and a second call
   * must not throw or re-merge.
   */
  async stop(): Promise<WavRecordingResult | null> {
    if (this.stopped) return this.finalResult;
    this.stopped = true;
    try {
      const result = await stopWavRecording();
      this.finalResult = result;
      this.uri = result.uri;
      return result;
    } catch {
      // ERR_NO_AUDIO and friends. Nothing to upload; the controller reports it.
      this.finalResult = null;
      return null;
    }
  }

  /** Match the AAC path release() so teardown is symmetrical. */
  release(): void {
    // The native module owns the mic and frees it on stop()/OnDestroy. Nothing
    // to detach on the JS side, but the method must exist so the controller can
    // call it unconditionally.
  }

  async discard(): Promise<void> {
    this.stopped = true;
    await discardWavRecording();
  }

  addListener(
    _event: "recordingStatusUpdate",
    cb: (status: EngineStatus) => void
  ): { remove(): void } {
    return addWavStatusListener((s) => {
      cb({
        isRecording: s.isRecording,
        metering: peakToDb(s.level),
        mediaServicesDidReset: false,
        durationMillis: s.durationSeconds * 1000,
        url: this.uri,
        micUnavailable: s.micUnavailable === true,
      });
    });
  }
}

/**
 * Structural validation before upload.
 *
 * The controller already refuses to mark a session COMPLETED without usable
 * audio; for WAV we can be stricter than a size check, because a WAV whose
 * header never got patched declares zero-length audio and would transcribe as
 * an empty file. Checking the header catches that before presign.
 */
export function validateWavForUpload(uri: string): {
  ok: boolean;
  reason?: string;
  durationSeconds?: number;
} {
  const info = inspectWav(uri);
  if (!info.valid) return { ok: false, reason: "not a readable WAV file" };
  if (info.dataBytes <= 0) return { ok: false, reason: "the WAV contains no audio" };
  return { ok: true, durationSeconds: info.durationSeconds };
}
