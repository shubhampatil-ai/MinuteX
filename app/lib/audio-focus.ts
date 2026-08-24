// lib/audio-focus.ts — Android audio-focus signal for the recorder.
//
// Wraps the local `modules/audio-focus` native module. See its Kotlin source
// for why it has to exist: expo-audio's focus listener only drives players,
// never the recorder, so on Android nothing reacts to a call taking the mic —
// and MediaRecorder responds to losing the mic by encoding silence rather than
// erroring, so polling getStatus() cannot see it.
//
// On iOS this is all inert by design: AVAudioSession delivers real interruption
// notifications and expo-audio surfaces them through the recorder's own status
// (mediaServicesDidReset) and by actually stopping capture, which the existing
// reconcile() already detects. Nothing here is needed there.

import { Platform } from "react-native";
import { requireOptionalNativeModule } from "expo-modules-core";

export type FocusChange =
  | "loss"
  | "loss_transient"
  | "loss_transient_can_duck"
  | "gain"
  | "unknown";

/**
 * Where a call is in its lifecycle, as far as AudioManager.mode can tell.
 *
 * The distinction that matters: a RINGING phone has not taken the microphone,
 * an ANSWERED one has. Both raise the same audio-focus loss, so focus alone
 * cannot separate them — this can.
 */
export type CallPhase = "idle" | "ringing" | "in_call";

type AudioFocusNativeModule = {
  startListening(): Promise<boolean>;
  stopListening(): Promise<boolean>;
  isInCall(): boolean;
  getCallPhase(): CallPhase;
  canSilenceRinger(): boolean;
  openSilenceRingerSettings(): Promise<boolean>;
  silenceRinger(): Promise<boolean>;
  restoreRinger(): Promise<boolean>;
  addListener(
    event: "onAudioFocusChange",
    cb: (e: { change: FocusChange; raw: number }) => void
  ): { remove(): void };
};

// requireOptionalNativeModule returns null rather than throwing when the module
// isn't in the binary. That matters for two real cases: iOS (the module is
// android-only) and any JS-only context such as Expo Go or a stale dev client
// built before this module existed. Every export below degrades to a no-op so
// the recorder still works — just without focus-based interruption detection.
const Native = requireOptionalNativeModule<AudioFocusNativeModule>("AudioFocus");

/** Whether real focus detection is available on this build. */
export const hasAudioFocusDetection = Platform.OS === "android" && Native != null;

export async function startFocusListening(): Promise<boolean> {
  if (!Native) return false;
  try {
    return await Native.startListening();
  } catch {
    return false;
  }
}

export async function stopFocusListening(): Promise<void> {
  if (!Native) return;
  try {
    await Native.stopListening();
  } catch {
    // Abandoning focus is best-effort; failing to release it must never
    // propagate into the stop path and block finalizing a recording.
  }
}

/**
 * Best-effort "is a call up right now".
 *
 * Reads AudioManager.mode, which needs no permission. Used ONLY to label the
 * pause reason (call vs. generic audio interruption) — never to decide whether
 * to pause, because some OEMs and VoIP apps don't set it reliably. Returns
 * false when unavailable, which degrades the label, not the behaviour.
 */
export function isInCall(): boolean {
  if (!Native) return false;
  try {
    return Native.isInCall();
  } catch {
    return false;
  }
}

export function onFocusChange(
  cb: (change: FocusChange) => void
): () => void {
  if (!Native) return () => {};
  try {
    const sub = Native.addListener("onAudioFocusChange", (e) => cb(e.change));
    return () => sub.remove();
  } catch {
    return () => {};
  }
}


/**
 * Where any active call currently is: idle / ringing / in_call.
 *
 * Returns "idle" when the native module is unavailable (iOS, Expo Go, an older
 * dev client). That is the RIGHT fallback: "idle" means the ring-aware logic
 * makes no claim, so the caller falls back to its existing focus-and-silence
 * detection rather than trusting a guess.
 */
export function getCallPhase(): CallPhase {
  if (!Native?.getCallPhase) return "idle";
  try {
    return Native.getCallPhase();
  } catch {
    return "idle";
  }
}

/** Whether we currently hold the Do Not Disturb access needed to silence the
 * ringer. False on iOS and on any build without the module. */
export function canSilenceRinger(): boolean {
  if (!Native?.canSilenceRinger) return false;
  try {
    return Native.canSilenceRinger();
  } catch {
    return false;
  }
}

/** Open the system DND-access screen. Resolves false if it cannot be opened
 * (some OEM ROMs omit it) so callers can say something honest. */
export async function openSilenceRingerSettings(): Promise<boolean> {
  if (!Native?.openSilenceRingerSettings) return false;
  try {
    return await Native.openSilenceRingerSettings();
  } catch {
    return false;
  }
}

/**
 * Silence the ringer for the duration of a recording, so an incoming call's
 * ringtone or vibration is not captured.
 *
 * Resolves false when it could not be done (no permission, iOS, refused by the
 * ROM). A false is NOT an error the caller should surface as a failure: the
 * recording proceeds regardless, and ring-aware pausing already prevents an
 * unanswered call from truncating the audio.
 */
export async function silenceRinger(): Promise<boolean> {
  if (!Native?.silenceRinger) return false;
  try {
    return await Native.silenceRinger();
  } catch {
    return false;
  }
}

/**
 * Restore the ringer to whatever it was before we silenced it.
 *
 * Idempotent and a no-op when nothing was silenced, so it is safe to call on
 * every stop path — including recovery paths where this process never silenced
 * anything.
 */
export async function restoreRinger(): Promise<boolean> {
  if (!Native?.restoreRinger) return false;
  try {
    return await Native.restoreRinger();
  } catch {
    // Best-effort by design: failing to restore the ringer must never
    // propagate into the stop path and block finalizing a recording.
    return false;
  }
}
