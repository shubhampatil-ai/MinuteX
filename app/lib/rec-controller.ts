// lib/rec-controller.ts — the recording state machine. ONE per app, outside React.
//
// WHY IT LIVES OUTSIDE THE REACT TREE
// Before this, record-phone.tsx owned the recorder via useAudioRecorder() and
// tracked its state in useState. Two consequences, both bad:
//   1. Leaving the screen unmounted the hook. The recorder object was released,
//      so navigating away from the recording screen ended the recording — and
//      "the UI must reflect the ACTUAL native state when the user comes back"
//      was unanswerable, because there was no state to come back to.
//   2. React state was treated as the truth. It isn't: the OS pauses the
//      recorder behind our back (a call, a mic grab, backgrounding), and the
//      screen went on rendering "On the record" over a recorder that had
//      stopped capturing. The user's only signal that they'd lost the meeting
//      was a short file at the end.
// So the controller is a module singleton. Screens subscribe to it; they never
// own the recorder. It survives navigation, and every state it reports is
// derived from the native recorder's own status, not from what we last asked
// for.
//
// AUTHORITY MODEL — read this before changing anything below.
// `state` is OUR interpretation; `AudioRecorder.getStatus()` is the platform's
// fact. reconcile() runs on a timer and on every app-state change, compares the
// two, and when they disagree the NATIVE side wins: if the recorder says it is
// no longer recording and we thought it was, something interrupted us and we
// move to a PAUSED_BY_* state rather than keep lying to the UI. The one thing
// we hold onto across that is INTENT — whether the user asked for silence —
// because no platform API can tell us that, and it is the difference between a
// correct auto-resume and recording someone who thought they were off the
// record.
import { AppState, type AppStateStatus, Platform } from "react-native";
import {
  AudioModule,
  RecordingPresets,
  getRecordingPermissionsAsync,
  requestNotificationPermissionsAsync,
  requestRecordingPermissionsAsync,
  setAudioModeAsync,
  type AudioRecorder,
  type RecorderState,
  type RecordingInput,
  type RecordingOptions,
  type RecordingStatus,
} from "expo-audio";
import { File } from "expo-file-system";
import { recLog, getSessionLog } from "./rec-log";
import {
  hasAudioFocusDetection,
  getCallPhase,
  silenceRinger,
  restoreRinger,
  isInCall,
  onFocusChange,
  startFocusListening,
  stopFocusListening,
} from "./audio-focus";
import {
  CRITICAL_FREE_BYTES,
  LOW_FREE_BYTES_WARN,
  MIN_FREE_BYTES_TO_START,
  concatSegments,
  createSession,
  ensureDir,
  fileSize,
  freeBytes,
  hasUsableAudio,
  isInvoluntaryPause,
  isPaused,
  reconcileDuration,
  saveSession,
  segmentSeconds,
  type InterruptionReason,
  type RecSession,
  type RecState,
} from "./rec-store";

// ---------------------------------------------------------------------------
// Recording format
// ---------------------------------------------------------------------------

// The HIGH_QUALITY preset, with the two things it does not give us:
//  * directory: 'document' — the preset writes to `cache`, which the OS may
//    purge. Audio waiting for wifi is precisely what gets purged. See the
//    rationale in lib/rec-store.ts.
//  * isMeteringEnabled — the live waveform needs a real input level. Without
//    it the waveform is decorative, and a decorative waveform on a recorder
//    that has silently stopped is actively misleading.
//
// Everything else stays at the preset's values on purpose: AAC in an .m4a
// container is ~1 MB/min, which is the difference between a 3-hour meeting
// being a 180 MB upload and a 1.8 GB one, and the backend already accepts m4a.
const REC_OPTIONS: RecordingOptions = {
  ...RecordingPresets.HIGH_QUALITY,
  isMeteringEnabled: true,
  directory: "document",
  android: {
    ...RecordingPresets.HIGH_QUALITY.android,
    // ADTS rather than the preset's MPEG-4, so that a recording interrupted by
    // a phone call can be reassembled.
    //
    // An interruption forces a NEW recorder and therefore a new file (a
    // MediaRecorder whose mic was taken keeps encoding silence forever — see
    // attemptResume). Those pieces have to become one file, because the upload
    // pipeline and the backend both take exactly one file per recording.
    //
    // MPEG-4 (.m4a) cannot be joined without a muxer: each file carries its own
    // moov atom, so byte-concatenation yields something players read as just
    // the first piece — audio silently lost, which is the failure this whole
    // change exists to prevent. ADTS-AAC is a self-framing stream of packets
    // with no container-level index, so appending the bytes of one to another
    // produces a valid, complete file. Same AAC audio, same ~1 MB/min, and the
    // backend already accepts 'aac' (see UPLOAD_FORMATS in lambda-userapi).
    //
    // iOS keeps the preset's .m4a: AVAudioSession interruptions do not
    // invalidate the recorder the way losing the Android mic does, so it
    // records to a single file and never needs joining.
    extension: ".aac",
    outputFormat: "aac_adts",
    audioEncoder: "aac",
  },
};

/**
 * Container per platform — see REC_OPTIONS.android for why they differ.
 * Used as the upload's `format` key, so it must match the real bytes.
 */
export const REC_FORMAT = Platform.OS === "android" ? "aac" : "m4a";

// The AudioRecorder CLASS off the native module.
//
// Hoisted into a local so the constructor call below reads as an ordinary
// `new Ctor(...)`. `AudioModule` is a native module instance, not an ES
// namespace, but eslint-plugin-import's `import/namespace` rule can't tell the
// difference and reports `AudioModule.AudioRecorder` as a missing namespace
// member. The property is genuinely there at runtime — it is exactly what
// useAudioRecorder() constructs (see ExpoAudio.js) — so the indirection is
// only about giving the linter something it can reason about.
const AudioRecorderCtor = (AudioModule as unknown as {
  AudioRecorder: new (options: Record<string, unknown>) => AudioRecorder;
}).AudioRecorder;

/**
 * Flatten RecordingOptions into the per-platform shape native expects.
 *
 * expo-audio patches `prepareToRecordAsync` to run its own
 * createRecordingOptions() over whatever you pass, but the AudioRecorder
 * CONSTRUCTOR is not patched (see ExpoAudio.js: only the prototype method is
 * wrapped) — useAudioRecorder() calls createRecordingOptions itself before
 * `new AudioModule.AudioRecorder(...)`. Since we construct the recorder
 * directly instead of through the hook, we have to do the same transform, or
 * native receives a nested `{ios: {...}, android: {...}}` object and silently
 * falls back to defaults — including the cache directory we are specifically
 * trying to avoid.
 *
 * Mirrors expo-audio/build/utils/options.js. Kept deliberately small and
 * literal so a change upstream is easy to diff against.
 */
function platformRecordingOptions(o: RecordingOptions): Record<string, unknown> {
  const common = {
    extension: o.extension,
    sampleRate: o.sampleRate,
    numberOfChannels: o.numberOfChannels,
    bitRate: o.bitRate,
    isMeteringEnabled: o.isMeteringEnabled ?? false,
    directory: o.directory,
  };
  if (Platform.OS === "ios") return { ...common, ...o.ios };
  if (Platform.OS === "android") return { ...common, ...o.android };
  return { ...common, ...o.web };
}

// ---------------------------------------------------------------------------
// Public snapshot — what screens render from
// ---------------------------------------------------------------------------

export type MicPermission = "unknown" | "granted" | "denied" | "blocked";

export type RecSnapshot = {
  state: RecState;
  /** Seconds of audio actually captured (pauses excluded). */
  seconds: number;
  /** Live input level 0..1, or null when not metering. */
  level: number | null;
  permission: MicPermission;
  /** User-facing message for the current state, when there is something to say. */
  message: string | null;
  /** User-facing error, when state is ERROR or a recoverable problem occurred. */
  error: string | null;
  /** The session id, so a screen can follow it into the upload queue. */
  sessionId: string | null;
  /** Current input route name ("Built-in Microphone", a headset, …). */
  inputName: string | null;
  /** Free space is running out — the UI shows a warning band. */
  lowStorage: boolean;
  /** True while an operation is in flight; disables the controls. */
  busy: boolean;
};

const IDLE_SNAPSHOT: RecSnapshot = {
  state: "IDLE",
  seconds: 0,
  level: null,
  permission: "unknown",
  message: null,
  error: null,
  sessionId: null,
  inputName: null,
  lowStorage: false,
  busy: false,
};

// ---------------------------------------------------------------------------
// Controller internals
// ---------------------------------------------------------------------------

let recorder: AudioRecorder | null = null;
let session: RecSession | null = null;
let snapshot: RecSnapshot = IDLE_SNAPSHOT;
let permission: MicPermission = "unknown";
let busy = false;
let level: number | null = null;
let inputName: string | null = null;
let lowStorage = false;
let lastError: string | null = null;

/**
 * The user's INTENT, held separately from `state`.
 *
 * The platform can move us out of RECORDING at any moment and cannot tell us
 * why. This flag is the only record of whether the user wanted to be recording
 * when that happened, and therefore the only basis for deciding whether to
 * resume. It is set true by start()/resume() and false ONLY by the user's own
 * pause()/stop() — never by an interruption handler.
 */
let userWantsRecording = false;

const listeners = new Set<() => void>();
let ticker: ReturnType<typeof setInterval> | null = null;
let statusSub: { remove(): void } | null = null;
let appStateSub: { remove(): void } | null = null;
let focusSub: (() => void) | null = null;

// How often to poll the native recorder. 500ms matches what the old screen
// used for its timer, and is frequent enough that an interruption shows up in
// the UI within half a second while costing nothing measurable.
const TICK_MS = 500;

function emit() {
  const s = session;
  snapshot = {
    state: s?.state ?? "IDLE",
    seconds: s ? Math.round(segmentSeconds(s)) : 0,
    level,
    permission,
    message: messageFor(s?.state ?? "IDLE"),
    error: lastError,
    sessionId: s?.id ?? null,
    inputName,
    lowStorage,
    busy,
  };
  for (const l of listeners) l();
}

export function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => { listeners.delete(cb); };
}

export function getSnapshot(): RecSnapshot {
  return snapshot;
}

/** The live session record, for the upload hand-off. */
export function getSession(): RecSession | null {
  return session;
}

function messageFor(state: RecState): string | null {
  switch (state) {
    case "RECORDING": return "Recording";
    case "PAUSED_BY_USER": return "Paused";
    case "PAUSED_BY_CALL": return "Paused because of a call";
    case "PAUSED_BY_AUDIO_INTERRUPTION": return "Paused — another app took the audio";
    case "PAUSED_BY_MICROPHONE": return "Microphone unavailable";
    case "FINALIZING": return "Saving…";
    case "COMPLETED": return "Saved";
    case "ERROR": return null; // the error text itself is the message
    default: return null;
  }
}

// ---------------------------------------------------------------------------
// State transitions — the ONLY place `session.state` changes
// ---------------------------------------------------------------------------

function transition(next: RecState, reason?: InterruptionReason, note?: string) {
  if (!session) return;
  const prev = session.state;
  if (prev === next) return;

  const now = Date.now();
  let s: RecSession = { ...session, state: next };

  // Leaving RECORDING closes the open segment. Entering RECORDING opens one.
  // Doing this here — rather than at each call site — is what keeps the
  // segment list a faithful record of captured audio no matter which path
  // (user, interruption, recovery) caused the change.
  if (prev === "RECORDING" && next !== "RECORDING") {
    s.segments = s.segments.map((seg) =>
      seg.endedAt == null ? { ...seg, endedAt: now } : seg
    );
  }
  if (next === "RECORDING" && prev !== "RECORDING") {
    s.segments = [...s.segments, { startedAt: now, endedAt: null }];
  }

  // An involuntary pause opens an interruption record; leaving one closes it.
  if (isInvoluntaryPause(next) && !isInvoluntaryPause(prev)) {
    s.interruptions = [
      ...s.interruptions,
      { reason: reason ?? "unknown", startedAt: now, endedAt: null },
    ];
  }
  if (isInvoluntaryPause(prev) && !isInvoluntaryPause(next)) {
    s.interruptions = s.interruptions.map((i) =>
      i.endedAt == null
        ? { ...i, endedAt: now, autoResumed: next === "RECORDING" }
        : i
    );
  }

  s.duration = Math.round(segmentSeconds(s, now));
  session = s;
  saveSession(s);

  recLog("state.transition", { from: prev, to: next, reason, note }, s.id);
  switch (next) {
    case "RECORDING":
      recLog(prev === "IDLE" ? "recording.started" : "recording.resumed", {
        seconds: s.duration,
        auto: prev !== "PAUSED_BY_USER" && isPaused(prev),
      }, s.id);
      break;
    case "PAUSED_BY_USER":
      recLog("recording.paused", { by: "user", seconds: s.duration }, s.id);
      break;
    case "PAUSED_BY_CALL":
      recLog("call.interrupted", { seconds: s.duration }, s.id);
      recLog("recording.paused", { by: "call", seconds: s.duration }, s.id);
      break;
    case "PAUSED_BY_AUDIO_INTERRUPTION":
      recLog("mic.interrupted", { reason, seconds: s.duration }, s.id);
      recLog("recording.paused", { by: "audio_interruption", seconds: s.duration }, s.id);
      break;
    case "PAUSED_BY_MICROPHONE":
      recLog("mic.unavailable", { reason, seconds: s.duration }, s.id);
      recLog("recording.paused", { by: "microphone", seconds: s.duration }, s.id);
      break;
    case "COMPLETED":
      recLog("recording.stopped", { seconds: s.duration, size: s.size }, s.id);
      break;
    case "ERROR":
      recLog("recording.error", { note, seconds: s.duration }, s.id);
      break;
  }
  emit();
}

// ---------------------------------------------------------------------------
// Permissions
// ---------------------------------------------------------------------------

/**
 * Read the CURRENT permission without prompting. Call this on screen focus so
 * a permission revoked in Settings while the app was backgrounded is reflected
 * immediately rather than at the next failed record attempt.
 */
export async function refreshPermission(): Promise<MicPermission> {
  try {
    const res = await getRecordingPermissionsAsync();
    // `canAskAgain === false` on a denied permission is the OS saying the
    // dialog will never appear again — the only route left is Settings, and a
    // UI that offers "Allow" instead of "Open settings" there is a dead end.
    permission = res.granted
      ? "granted"
      : res.canAskAgain === false ? "blocked" : "denied";
  } catch {
    permission = "unknown";
  }
  emit();
  return permission;
}

export async function requestPermission(): Promise<MicPermission> {
  try {
    const res = await requestRecordingPermissionsAsync();
    permission = res.granted
      ? "granted"
      : res.canAskAgain === false ? "blocked" : "denied";
  } catch {
    permission = "denied";
  }
  recLog("mic.permission", { result: permission });
  emit();
  return permission;
}

/**
 * Ask for notification permission on Android 13+, once, without blocking.
 *
 * Uses expo-audio's own requestNotificationPermissionsAsync rather than
 * PermissionsAndroid: it is the same POST_NOTIFICATIONS permission, but asking
 * through the audio module keeps this file's native dependencies to the one it
 * already owns (lib/notifications.ts covers the BLE pairing flow's separate
 * needs). The Platform guard is load-bearing, not defensive — expo-audio
 * THROWS on iOS ("only available on Android"), so calling it unconditionally
 * would turn every iOS recording start into a caught-and-logged failure.
 *
 * Returns whether it was granted, but callers treat that as advisory — see
 * start(). A refusal degrades background recording, it does not prevent
 * recording.
 */
async function ensureNotificationPermission(): Promise<boolean> {
  if (Platform.OS !== "android") return true;
  try {
    const res = await requestNotificationPermissionsAsync();
    if (!res.granted) {
      recLog("mic.permission", {
        note: "notifications declined; background recording may be limited",
      });
    }
    return res.granted;
  } catch {
    // Older dev clients may not expose it. Not worth failing over.
    return false;
  }
}

// ---------------------------------------------------------------------------
// Audio session
// ---------------------------------------------------------------------------

/**
 * Configure the audio session for BACKGROUND recording.
 *
 * Every field here is load-bearing:
 *
 *  allowsRecording           iOS: selects the .playAndRecord category. Without
 *                            it there is no input route at all.
 *  allowsBackgroundRecording THE critical one, and the bug this audit found.
 *                            expo-audio's OnActivityEntersBackground /
 *                            OnAppEntersBackground handlers PAUSE every
 *                            recorder unless this is true (AudioModule.kt:296,
 *                            AudioModule.swift:103). The app.json plugin flag
 *                            `enableBackgroundRecording` only adds the manifest
 *                            entries and the UIBackgroundModes key — it does
 *                            NOT set this runtime field. So before this change,
 *                            locking the screen silently paused the recording
 *                            while the UI kept saying "On the record".
 *                            On Android it is also what makes AudioRecorder set
 *                            `useForegroundService`, which starts the
 *                            microphone foreground service — the only way
 *                            Android permits background mic capture at all.
 *  interruptionMode          'doNotMix' takes exclusive audio focus. On Android
 *                            this is REQUIRED to receive focus-loss callbacks:
 *                            the expo-audio docs are explicit that under
 *                            'mixWithOthers' the app "won't receive audio focus
 *                            loss callbacks (for example, during phone calls)".
 *                            Without focus loss we cannot detect a call, and
 *                            call-detection is a hard requirement here.
 *  playsInSilentMode         Recording must not depend on the ringer switch.
 *
 * ORDERING MATTERS: this must run BEFORE prepareToRecordAsync(), because
 * AudioRecorder.prepare() reads `useForegroundService` to decide whether to
 * bind the recording service (AudioRecorder.kt:95). Setting the mode after
 * prepare leaves the service unbound, and record() then throws
 * AudioRecordingServiceException.
 */
async function configureSession(): Promise<void> {
  await setAudioModeAsync({
    allowsRecording: true,
    allowsBackgroundRecording: true,
    playsInSilentMode: true,
    interruptionMode: "doNotMix",
    shouldRouteThroughEarpiece: false,
  });
}

/**
 * Release the exclusive audio session once we are done with it.
 *
 * Holding 'doNotMix' focus after a recording ends would keep other apps'
 * audio suppressed — the user stops recording and their music stays dead.
 * Failure here is non-fatal: the recording is already saved.
 */
async function releaseSession(): Promise<void> {
  try {
    await setAudioModeAsync({
      allowsRecording: false,
      allowsBackgroundRecording: false,
      playsInSilentMode: true,
      interruptionMode: "mixWithOthers",
    });
  } catch {
    // Nothing the user can act on, and the audio is safe.
  }
}

// ---------------------------------------------------------------------------
// Route / input tracking
// ---------------------------------------------------------------------------

/**
 * Note the active input and report a change.
 *
 * expo-audio exposes no route-change EVENT to JS (RecordingEvents carries only
 * recordingStatusUpdate), so a poll is the only mechanism available. That is
 * enough for the requirement that matters: we must never silently produce an
 * empty recording after a Bluetooth headset disappears. Detecting the swap
 * within a tick lets us verify the recorder is still capturing and surface it
 * if it isn't.
 */
function pollInput(): void {
  if (!recorder) return;
  try {
    const current = recorder.getCurrentInput?.() as unknown;
    // getCurrentInput is typed as returning a Promise but resolves
    // synchronously on both platforms in SDK 57; handle either shape rather
    // than depending on which one this version does.
    if (current && typeof (current as Promise<RecordingInput>).then === "function") {
      (current as Promise<RecordingInput>)
        .then((inp) => noteInput(inp?.name ?? null))
        .catch(() => { /* route unavailable — the tick's own checks cover it */ });
    } else {
      noteInput((current as RecordingInput | null)?.name ?? null);
    }
  } catch {
    // Some devices throw when no input is available at all. That case is
    // caught by the recording-stalled check in reconcile().
  }
}

function noteInput(name: string | null): void {
  if (name === inputName) return;
  const from = inputName;
  inputName = name;
  if (from !== null) {
    recLog("route.changed", { from, to: name }, session?.id);
  }
  emit();
}

/** Available microphone inputs, for the input picker. */
export function listInputs(): RecordingInput[] {
  try {
    return recorder?.getAvailableInputs?.() ?? [];
  } catch {
    return [];
  }
}

/**
 * Switch input mid-recording.
 *
 * Allowed while live because the alternative — making the user stop and start
 * to move to a headset — costs them the meeting. The recorder keeps writing to
 * the same file across the swap.
 */
export async function selectInput(uid: string): Promise<boolean> {
  try {
    recorder?.setInput(uid);
    pollInput();
    recLog("route.changed", { to: uid, by: "user" }, session?.id);
    return true;
  } catch (e: any) {
    lastError = "Couldn't switch microphone: " + (e?.message ?? "unknown");
    emit();
    return false;
  }
}

// ---------------------------------------------------------------------------
// Reconciliation — native truth vs. our belief
// ---------------------------------------------------------------------------

// How long the recorder may report "not recording" while we believe it is,
// before we treat it as a real interruption. A single tick of disagreement is
// normal around a pause/resume round-trip; two consecutive ticks is not.
const STALL_TICKS = 2;
let stallCount = 0;

// -- Silence detection ------------------------------------------------------
//
// The SECOND line of defence, and the one that catches what the first misses.
//
// On Android, `isRecording` is a plain boolean we set ourselves and
// durationMillis is wall-clock arithmetic, so when the telephony stack takes
// the microphone NOTHING in the recorder's status changes — it just encodes
// silence. That is the "timer running, nothing recorded" bug. The stall check
// above literally cannot see it.
//
// What DOES change is metering: expo-audio's getAudioRecorderLevels() returns
// maxAmplitude, and a stolen mic reads exactly 0, which it reports as -160 dB
// (see AudioRecorder.kt). So a run of pinned-floor samples means we are writing
// silence, whatever the status claims.
//
// Why a long threshold: a real room genuinely goes quiet, and pausing a
// meeting recording because nobody spoke for two seconds would be far worse
// than the bug. -160 dB is not "quiet", though — it is a hardware zero, which
// a live mic essentially never returns (even a silent room has a noise floor
// around -60 to -50 dB). We still require a sustained run before acting, so
// that a single dropped buffer or a metering hiccup can't trigger it.
const SILENCE_DB_FLOOR = -159;      // -160 exactly, with room for float noise
const SILENCE_TICKS = 12;           // 12 * 500ms = 6s of true digital zero
let silenceCount = 0;

// Set when a focus-loss event tells us we've been interrupted. Kept separate
// from the pause state because it also gates auto-resume: focus GAIN is the
// signal that the interruption is over, and it is far more reliable than
// probing the recorder.
let focusLost = false;

/**
 * Compare our state against the recorder's, and believe the recorder.
 *
 * This is what makes "the UI always reflects the actual native recording
 * state" true rather than aspirational. Three things are checked every tick:
 *
 *  1. Did the recorder stop capturing while we thought it was recording?
 *     That is an interruption nobody told us about — the most common real-world
 *     cause being another app grabbing the mic, or Android's focus loss. We
 *     classify it as best we can and move to the matching PAUSED_BY_* state.
 *  2. Has an involuntary pause cleared? If the recorder is capturing again on
 *     its own, or the audio session is free again, resume — but ONLY if the
 *     user still wants to be recording.
 *  3. Is the disk filling up?
 */
/**
 * Enter an involuntary pause and STOP THE ENCODER.
 *
 * The critical difference from just calling transition(): on Android a
 * MediaRecorder that has lost the mic keeps writing silence, so merely
 * relabelling our state leaves the file growing with nothing in it. We have to
 * actually pause the encoder, and on resume we must build a NEW recorder
 * against a NEW file — a MediaRecorder whose input died stays bound to the dead
 * input and produces silence forever after, which is why the old code kept
 * recording nothing even after the call ended.
 *
 * Safe to call repeatedly; it no-ops unless we are RECORDING.
 */
async function enterInterruption(
  reason: InterruptionReason,
  note: string
): Promise<void> {
  if (!session || session.state !== "RECORDING") return;

  // Label a call as a call when we can tell, so the UI can be specific instead
  // of vague. Best-effort: see isInCall()'s contract.
  const state: RecState =
    reason === "call" || isInCall() ? "PAUSED_BY_CALL" : "PAUSED_BY_AUDIO_INTERRUPTION";
  const labelled: InterruptionReason =
    state === "PAUSED_BY_CALL" ? "call" : reason;

  transition(state, labelled, note);
  silenceCount = 0;
  stallCount = 0;

  // Close the current segment's file. pause() is preferred over stop() because
  // it keeps the container open and the already-encoded audio intact; if the
  // platform refuses (common once the mic is gone), fall through and let the
  // resume path rebuild from scratch — the bytes already flushed are still on
  // disk either way.
  try {
    recorder?.pause();
    recLog("recording.paused", { by: "encoder", reason: labelled }, session.id);
  } catch (e: any) {
    recLog("recording.error", {
      phase: "pause-on-interruption",
      message: String(e?.message ?? e),
    }, session.id);
  }
}

function reconcile(): void {
  if (!session || !recorder) return;
  const s = session;

  let status: RecorderState | null = null;
  try {
    status = recorder.getStatus();
  } catch {
    status = null;
  }

  if (status?.metering != null) {
    // Metering is dBFS (roughly -160..0). Map to 0..1 over the top 60 dB,
    // which is the band a room's speech actually occupies.
    const db = status.metering;
    level = Math.max(0, Math.min(1, (db + 60) / 60));
  }

  // A media-services reset invalidates the recorder outright: the platform
  // tells us the object must be re-prepared. Nothing can be salvaged from it
  // in place, so we finalize what we have rather than pretend to continue.
  if (status?.mediaServicesDidReset) {
    recLog("mic.interrupted", { reason: "media_services_reset" }, s.id);
    transition("PAUSED_BY_MICROPHONE", "media_services_reset");
    void salvageAfterReset("media_services_reset");
    return;
  }

  if (s.state === "RECORDING") {
    if (status && !status.isRecording) {
      stallCount += 1;
      if (stallCount >= STALL_TICKS) {
        stallCount = 0;
        // The recorder itself says it stopped. This is the honest case: iOS
        // raises it on a real AVAudioSession interruption. On Android it means
        // something called stop() outside our flow, since the flag is ours.
        //
        // We cannot ask the platform "was that a phone call?" without
        // READ_PHONE_STATE, which we deliberately do not request. isInCall()
        // reads AudioManager.mode instead — no permission, best-effort — so
        // when it says a call is up we can label it accurately, and otherwise
        // we report the generic interruption rather than guessing.
        void enterInterruption(
          Platform.OS === "ios" ? "call" : "audio_interruption",
          "native recorder stopped while state was RECORDING"
        );
      }
    } else {
      stallCount = 0;

      // The mic-stolen case the status flags cannot show us. A sustained run of
      // hardware-zero metering means the encoder is writing silence — the
      // recording is already being lost, so treat it exactly like any other
      // interruption: pause, preserve, and resume onto a fresh recorder.
      if (status?.metering != null && status.metering <= SILENCE_DB_FLOOR) {
        silenceCount += 1;
        if (silenceCount >= SILENCE_TICKS) {
          // A RINGING phone is the one case where sustained digital zero is not
          // proof the mic is gone for good: some ROMs mute the capture stream
          // for the ringtone and hand it straight back if the call is
          // dismissed. Pausing here would defeat the ring-aware handling above
          // — the recording would stop on the ring after all, just six seconds
          // later and labelled as a dead mic.
          //
          // So while a ring is up we hold: watchRingingCall is already polling
          // and will pause the moment it is ANSWERED. The counter is left
          // standing rather than reset, so if the ring resolves and the mic is
          // genuinely dead, the very next tick trips this without waiting out
          // another six seconds.
          if (getCallPhase() === "ringing") {
            return;
          }
          silenceCount = 0;
          recLog("mic.interrupted", {
            reason: "digital_silence",
            seconds: Math.round(segmentSeconds(s)),
            note: "metering pinned at hardware zero — mic taken",
          }, s.id);
          void enterInterruption(
            "microphone_unavailable",
            "metering pinned at -160dB: encoder writing silence"
          );
        }
      } else {
        silenceCount = 0;
      }
    }
    checkStorage();
    return;
  }

  // An involuntary pause: try to get back on the record.
  if (isInvoluntaryPause(s.state) && userWantsRecording) {
    if (status?.isRecording) {
      // The platform resumed us by itself (iOS does this on some interruption
      // ends). Just record the fact.
      transition("RECORDING", undefined, "native recorder resumed itself");
      return;
    }
    void attemptResume();
  }
}

function checkStorage(): void {
  const free = freeBytes();
  if (free == null) return;
  const nowLow = free < LOW_FREE_BYTES_WARN;
  if (nowLow !== lowStorage) {
    lowStorage = nowLow;
    if (nowLow) recLog("storage.low", { freeBytes: free }, session?.id);
    emit();
  }
  // Below the critical line, finalize rather than risk a truncated container.
  // Stopping with a good file beats recording until the encoder fails and the
  // moov atom never gets written — that would cost the WHOLE recording, not
  // just the tail.
  if (free < CRITICAL_FREE_BYTES && session?.state === "RECORDING") {
    recLog("storage.low", { freeBytes: free, action: "auto-finalize" }, session.id);
    lastError =
      "Storage is almost full — the recording was saved so it isn't lost.";
    void stop();
  }
}

// ---------------------------------------------------------------------------
// Resume / restart
// ---------------------------------------------------------------------------

// Back off between resume attempts: an interruption that is still active will
// refuse every call, and hammering it wastes battery for the whole call's
// duration. These are tick counts, not ms.
let resumeCooldown = 0;

async function attemptResume(): Promise<void> {
  if (resumeCooldown > 0) { resumeCooldown -= 1; return; }
  if (!recorder || !session || busy) return;
  if (!userWantsRecording) return;

  // Don't even try while the interruption is demonstrably still up. Without
  // this we'd rebuild the recorder against a mic the call still owns, get a
  // recorder that "starts" but captures silence, and burn a file segment per
  // attempt for the whole length of the call.
  // Any live call activity blocks a resume — including a phone that is merely
  // RINGING. A ring means an answer may land a fraction of a second from now,
  // and resuming into that would rebuild the recorder against a mic the call is
  // about to take, producing exactly the silent segment this guard exists to
  // prevent. Waiting costs nothing: if the ring is dismissed, the next tick
  // resumes normally.
  if (getCallPhase() !== "idle" || isInCall()) { resumeCooldown = 4; return; }

  // On Android, focus loss is the interruption signal and focus GAIN is the
  // only trustworthy all-clear. Probing the recorder can't tell us: a rebuilt
  // recorder reports isRecording === true even when the mic is still stolen.
  if (hasAudioFocusDetection && focusLost) { resumeCooldown = 4; return; }

  busy = true;
  try {
    await configureSession();

    // REBUILD, don't just record() again.
    //
    // This is the fix for "nothing recorded after the call ended". A
    // MediaRecorder whose input was taken by the telephony stack stays bound
    // to that dead input for the rest of its life — calling record() on it
    // returns cleanly, sets isRecording, advances durationMillis, and writes
    // pure silence. The only way back to real audio is a new recorder, which
    // means a new output file.
    const previousUri = session.fileUri ?? recorder.uri ?? null;
    try {
      // Close the old container so its bytes are a valid, playable file. It
      // may already be dead — either way we keep what it flushed.
      await recorder.stop();
    } catch {
      // Expected when the recorder is in a broken state. The partial file is
      // still on disk and still listed on the session.
    }
    releaseRecorder();

    const r = new AudioRecorderCtor(platformRecordingOptions(REC_OPTIONS));
    attachStatusListener(r);
    await r.prepareToRecordAsync(REC_OPTIONS);
    r.record();
    const st = r.getStatus();
    if (!st.isRecording) throw new Error("recorder did not start");

    // Verify we're capturing SOUND, not just running. A recorder that starts
    // against a still-busy mic reports success and meters at hardware zero;
    // accepting that would recreate the original bug one segment later. Give
    // the first buffers a moment to land before reading the level.
    await new Promise((res) => setTimeout(res, 350));
    const lvl = r.getStatus().metering;
    if (lvl != null && lvl <= SILENCE_DB_FLOOR) {
      // Still silent — the interruption hasn't really ended. Throw away this
      // attempt's file so we don't accumulate empty segments, and back off.
      try { await r.stop(); } catch { /* nothing to salvage */ }
      const deadUri = r.uri;
      releaseRecorder();
      recorder = null;
      if (deadUri) { try { new File(deadUri).delete(); } catch { /* best effort */ } }
      throw new Error("resumed recorder is capturing silence");
    }

    recorder = r;
    const newUri = st.url ?? r.uri ?? null;

    // Keep BOTH files on the session, in capture order.
    const extras = [...(session.extraFiles ?? [])];
    if (previousUri && newUri && previousUri !== newUri) {
      // The already-captured audio moves into extraFiles and the live file
      // becomes fileUri, so fileUri is always the one being written to.
      extras.push(previousUri);
    }
    session = {
      ...session,
      fileUri: newUri,
      extraFiles: extras.length ? extras : undefined,
    };
    saveSession(session);
    if (newUri) {
      recLog("file.created", { uri: newUri, note: "new segment after interruption" }, session.id);
    }

    resumeCooldown = 0;
    silenceCount = 0;
    stallCount = 0;
    transition("RECORDING", undefined, "auto-resumed after interruption");
    recLog("interruption.ended", {
      autoResumed: true,
      segments: (session.extraFiles?.length ?? 0) + 1,
    }, session.id);
  } catch {
    // Still interrupted. Wait longer before the next try — 4 ticks is 2s,
    // which for a phone call is the right order of magnitude. Deliberately not
    // logged: this fires every retry for the whole length of a call and would
    // evict the events that matter from the ring buffer.
    resumeCooldown = 4;
  } finally {
    busy = false;
  }
}

/**
 * Rebuild the recorder after it has been invalidated (media services reset).
 *
 * The audio already on disk is NOT touched: we stop the dead recorder to close
 * its file cleanly, then continue the session with a new file. The session
 * keeps both — see `extraFiles` — so nothing recorded before the reset is lost.
 */
async function salvageAfterReset(reason: InterruptionReason): Promise<void> {
  if (!session) return;
  const id = session.id;
  recLog("recovery.attempted", { reason, note: "recorder invalidated" }, id);

  // Keep whatever the encoder flushed. finalize() does the rest: it stops the
  // (already broken) recorder, measures the file, and lands on COMPLETED if
  // there is real audio or ERROR if there is not.
  //
  // We do NOT continue the session into a new file, even though the
  // interruption path now can (see attemptResume + concatSegments). The
  // difference is that a media-services reset means the media daemon itself
  // crashed: the whole subsystem is in an unknown state, not just our recorder.
  // Rebuilding against it tends to fail again immediately, and each attempt
  // would add another segment. Finalizing keeps the audio captured so far and
  // reports honestly, which beats a retry loop that produces fragments.
  //
  // This is iOS-only in practice — mediaServicesDidReset has no Android
  // counterpart — and rare even there.
  const done = await finalize("interrupted");
  recLog("recovery.result", {
    state: done?.state,
    seconds: done?.duration,
    size: done?.size,
  }, id);

  if (done?.state === "COMPLETED") {
    lastError =
      "The microphone was reset by the system, so the recording was saved " +
      "at that point. Start a new recording to keep going.";
    emit();
  }
}

// ---------------------------------------------------------------------------
// App lifecycle
// ---------------------------------------------------------------------------

/**
 * Re-check state when the app comes back to the foreground.
 *
 * Two reasons this matters even though the recorder is supposed to keep
 * running in the background:
 *  * A permission revoked in Settings arrives with no event at all.
 *  * If the background recording DID get killed (an OEM aggressively
 *    reclaiming, or the foreground service failing to start), the only place
 *    we can notice is here.
 */
function onAppState(next: AppStateStatus): void {
  if (!session) return;
  recLog("state.transition", { appState: next, recState: session.state }, session.id);
  if (next === "active") {
    void refreshPermission().then((p) => {
      if (p !== "granted" && session && session.state === "RECORDING") {
        // The mic was taken away while we were backgrounded. The file so far
        // is fine; stop cleanly rather than hold a dead recorder.
        lastError = "Microphone access was turned off, so the recording was saved.";
        transition("PAUSED_BY_MICROPHONE", "microphone_unavailable");
        void stop();
      }
    });
    reconcile();
  }
}

function startTicker(): void {
  if (ticker) return;
  ticker = setInterval(() => {
    reconcile();
    pollInput();
    emit();
  }, TICK_MS);
  if (!appStateSub) {
    appStateSub = AppState.addEventListener("change", onAppState);
  }
  startFocusWatch();
}

function stopTicker(): void {
  if (ticker) { clearInterval(ticker); ticker = null; }
  if (appStateSub) { appStateSub.remove(); appStateSub = null; }
  stopFocusWatch();
  stopRingWatch();
  // Put the ringer back. Deliberately here rather than in stop(): this runs on
  // EVERY path that ends a recording — user stop, error, salvage, teardown — so
  // there is no route that leaves the user's phone silent afterwards. It is a
  // no-op when nothing was silenced, and its failure can never block finalizing
  // a recording (see restoreRinger).
  void restoreRinger().then((ok) => {
    if (ok) recLog("ringer.restored", undefined, session?.id);
  });
}

// ---------------------------------------------------------------------------
// Audio focus — the primary interruption signal on Android
// ---------------------------------------------------------------------------

/**
 * Subscribe to audio-focus changes for the life of the recording.
 *
 * This is the FIRST line of defence and the fast one: a phone call raises
 * AUDIOFOCUS_LOSS_TRANSIENT the moment it arrives, so we pause within a tick
 * instead of waiting out the silence threshold. The silence detector in
 * reconcile() stays as the backstop for OEMs that don't deliver focus events
 * reliably (and there are some), and for mic theft that doesn't involve focus
 * at all.
 *
 * No-ops on iOS, where AVAudioSession interruptions already reach us through
 * the recorder's own status.
 */
// A ring is being watched: poll the call phase until it resolves. Held so a
// second ring event does not start a second poller.
let ringWatch: ReturnType<typeof setInterval> | null = null;

// How often to check whether a ringing call got answered, and for how long to
// keep looking.
//
// 400ms is well inside human reaction time, so the gap between "answered" and
// "paused" is short enough that at most a fraction of a second of call audio
// can reach the file. The 90s ceiling is the give-up: a ring that has not
// resolved by then was almost certainly missed, and a poller that ran forever
// would keep a timer alive for the rest of the recording.
const RING_POLL_MS = 400;
const RING_WATCH_MAX_MS = 90_000;

/**
 * Watch a ringing call and pause ONLY if it is answered.
 *
 * This is the other half of the ring-aware focus handling: we did not pause on
 * the ring, so something has to notice the moment the mic is actually taken.
 * Three outcomes, all handled:
 *
 *   answered  -> the phase becomes "in_call" and we pause immediately, with
 *                the encoder stopped (see enterInterruption for why merely
 *                relabelling the state is not enough on Android).
 *   rejected  -> the phase returns to "idle" and we simply stop watching. The
 *                recording was never interrupted, which is the whole point.
 *   ambiguous -> we stop watching after RING_WATCH_MAX_MS. The existing
 *                silence detector still covers a mic that died without the
 *                mode ever reporting a call, so nothing is left unguarded.
 */
function watchRingingCall(): void {
  if (ringWatch) return;
  const startedAt = Date.now();

  ringWatch = setInterval(() => {
    // Stop watching the moment we are no longer recording — the user paused or
    // stopped, and there is nothing left to protect.
    if (!session || session.state !== "RECORDING") {
      stopRingWatch();
      return;
    }

    const phase = getCallPhase();

    if (phase === "in_call") {
      stopRingWatch();
      focusLost = true;
      recLog("call.answered", {
        note: "pausing — the call now holds the microphone",
        seconds: session.duration,
      }, session.id);
      void enterInterruption("call", "call answered");
      return;
    }

    if (phase === "idle") {
      // Rejected, missed, or hung up before connecting. Nothing was ever
      // interrupted.
      stopRingWatch();
      recLog("call.dismissed", {
        note: "ring ended without being answered — recording never paused",
      }, session?.id);
      return;
    }

    if (Date.now() - startedAt > RING_WATCH_MAX_MS) {
      stopRingWatch();
      recLog("call.dismissed", {
        note: "ring watch timed out; silence detection still active",
      }, session?.id);
    }
  }, RING_POLL_MS);
}

function stopRingWatch(): void {
  if (!ringWatch) return;
  clearInterval(ringWatch);
  ringWatch = null;
}

function startFocusWatch(): void {
  if (focusSub || !hasAudioFocusDetection) return;

  focusSub = onFocusChange((change) => {
    if (!session) return;

    switch (change) {
      case "loss":
      case "loss_transient": {
        if (session.state !== "RECORDING") {
          focusLost = true;
          return;
        }

        // A RINGING phone has not taken the microphone — only an ANSWERED call
        // does. Both raise AUDIOFOCUS_LOSS_TRANSIENT, so focus alone cannot
        // tell them apart, and pausing on the ring is the wrong call: most
        // rings are ignored or rejected, and pausing a meeting because someone's
        // phone rang loses the seconds around it for nothing.
        //
        // So on a ring we keep recording and start watching for the answer (see
        // watchRingingCall). If it is answered we pause then; if it is rejected
        // or missed, recording was never interrupted at all.
        //
        // Note focusLost is deliberately NOT set here. It gates auto-resume,
        // and we have not paused — setting it would make the next focus GAIN
        // look like the end of an interruption that never began.
        if (change === "loss_transient" && getCallPhase() === "ringing") {
          recLog("audio.focus", {
            change,
            note: "ringing — recording continues until answered",
          }, session.id);
          watchRingingCall();
          break;
        }

        focusLost = true;
        recLog("audio.focus", { change, note: "interruption began" }, session.id);
        // isInCall() inside enterInterruption picks the accurate label.
        void enterInterruption(
          change === "loss_transient" ? "call" : "audio_interruption",
          `audio focus ${change}`
        );
        break;
      }

      case "gain": {
        focusLost = false;
        if (!session || !isInvoluntaryPause(session.state)) return;
        recLog("audio.focus", { change, note: "interruption ended" }, session.id);
        // Clear the backoff so the next tick retries immediately rather than
        // sitting out a cooldown that was set while the call was still up.
        resumeCooldown = 0;
        break;
      }

      // Ducking is irrelevant to recording: nothing is being played, and the
      // mic is unaffected. Ignoring it avoids pausing a meeting for a
      // notification chime.
      case "loss_transient_can_duck":
      default:
        break;
    }
  });

  void startFocusListening().then((granted) => {
    recLog("audio.focus", {
      note: granted ? "focus acquired" : "focus request refused (recording anyway)",
    }, session?.id);
  });
}

function stopFocusWatch(): void {
  if (focusSub) { focusSub(); focusSub = null; }
  focusLost = false;
  void stopFocusListening();
}

/**
 * Detach the native recorder and drop our reference.
 *
 * release(), not remove(): AudioRecorder is a SharedObject and inherits
 * release() — the JS↔native detach — whereas remove() only exists on
 * AudioPlayer. Because we construct the recorder ourselves rather than through
 * useAudioRecorder(), nothing else will free it, and on Android the recorder's
 * sharedObjectDidRelease() is what unregisters it from the microphone
 * foreground service. Skipping this would leave the "Recording audio"
 * notification pinned after the recording ended.
 */
function releaseRecorder(): void {
  const r = recorder;
  recorder = null;
  if (!r) return;
  try {
    r.release();
  } catch {
    // Already detached, or the native side is gone (media services reset).
    // Either way there is nothing left to free.
  }
}

// ---------------------------------------------------------------------------
// The status listener — the recorder's own error channel
// ---------------------------------------------------------------------------

function attachStatusListener(r: AudioRecorder): void {
  statusSub?.remove();
  statusSub = r.addListener("recordingStatusUpdate", (st: RecordingStatus) => {
    if (!session) return;
    if (st.mediaServicesDidReset) {
      recLog("mic.interrupted", { reason: "media_services_reset", via: "event" }, session.id);
      transition("PAUSED_BY_MICROPHONE", "media_services_reset");
      void salvageAfterReset("media_services_reset");
      return;
    }
    if (st.hasError) {
      // MediaRecorder's onError path (server died, unknown failure). The file
      // is closed by the platform at this point; keep whatever landed.
      recLog("recording.error", { message: st.error ?? "unknown", via: "event" }, session.id);
      lastError = st.error || "The recorder stopped unexpectedly.";
      void finalize("error");
      return;
    }
    if (st.url && session.fileUri !== st.url) {
      session = { ...session, fileUri: st.url };
      saveSession(session);
      recLog("file.created", { uri: st.url }, session.id);
      emit();
    }
  });
}

// ---------------------------------------------------------------------------
// Public commands
// ---------------------------------------------------------------------------

export type StartResult =
  | { ok: true; sessionId: string }
  | { ok: false; error: string; needsPermission?: boolean; needsSettings?: boolean };

/**
 * Begin a recording.
 *
 * Order is deliberate and each step gates the next: permission, then storage,
 * then audio session, then prepare, then record. A failure at any point leaves
 * NO half-session behind — the sidecar is only written once the recorder is
 * actually running, so recovery can never see a phantom.
 */
export async function start(): Promise<StartResult> {
  if (busy) return { ok: false, error: "Busy — try again." };
  if (session && (session.state === "RECORDING" || isPaused(session.state))) {
    // Already recording. Idempotent rather than an error: a double-tap on the
    // record button must not start a second recording.
    return { ok: true, sessionId: session.id };
  }

  busy = true;
  lastError = null;
  emit();

  try {
    // 1. Permission. Ask only if we haven't been permanently refused.
    let p = await refreshPermission();
    if (p !== "granted") {
      if (p === "blocked") {
        return {
          ok: false,
          needsSettings: true,
          error: "Microphone access is off. Turn it on in Settings to record.",
        };
      }
      p = await requestPermission();
      if (p !== "granted") {
        return {
          ok: false,
          needsPermission: true,
          needsSettings: p === "blocked",
          error: p === "blocked"
            ? "Microphone access is off. Turn it on in Settings to record."
            : "MinuteX needs the microphone to record this meeting.",
        };
      }
    }

    // 2. Notification permission — Android's price of admission for
    // background recording.
    //
    // The microphone foreground service posts an ongoing notification, and on
    // Android 13+ POST_NOTIFICATIONS is a runtime permission. Without it the
    // service can still start, but the user gets no persistent indicator that
    // MinuteX is listening — and on several OEM builds startForeground() is
    // refused outright, which surfaces as AudioRecordingServiceException from
    // record(). Asking here, before the recorder exists, turns a mystifying
    // start failure into an ordinary permission prompt.
    //
    // Deliberately NOT fatal if declined: a recording that only works while
    // the app is in the foreground is far better than no recording. The
    // consequence is described honestly in the audit's Android limitations.
    await ensureNotificationPermission();

    // 3. Storage. Refusing up front is far kinder than dying at minute 40.
    const free = freeBytes();
    if (free != null && free < MIN_FREE_BYTES_TO_START) {
      recLog("storage.low", { freeBytes: free, action: "refused to start" });
      return {
        ok: false,
        error: `Not enough free space to record (${Math.round(free / 1e6)} MB left). Free some space and try again.`,
      };
    }

    // 4. The audio session, BEFORE prepare — see configureSession().
    await configureSession();

    // 5. A session record and a recorder writing into our own directory.
    ensureDir();
    const s = createSession(REC_FORMAT);
    // Constructed directly (not via useAudioRecorder) so the recorder outlives
    // any screen — see the header. That means doing the options transform
    // ourselves; prepareToRecordAsync is patched upstream and takes the
    // un-flattened form.
    const r = new AudioRecorderCtor(platformRecordingOptions(REC_OPTIONS));
    attachStatusListener(r);
    await r.prepareToRecordAsync(REC_OPTIONS);

    recorder = r;
    session = s;
    userWantsRecording = true;
    stallCount = 0;
    resumeCooldown = 0;

    r.record();
    const st = r.getStatus();
    if (!st.isRecording) {
      throw new Error("the recorder did not start");
    }

    session = { ...s, fileUri: st.url ?? r.uri ?? null };
    saveSession(session);
    if (session.fileUri) recLog("file.created", { uri: session.fileUri }, session.id);

    transition("RECORDING");
    pollInput();
    startTicker();

    // Silence the ringer so an incoming call's ringtone or vibration is not
    // captured. Fire-and-forget on purpose: this is a nicety, and the recording
    // must not wait on it or fail because of it. A refusal (no Do Not Disturb
    // access) is logged and ignored — ring-aware pausing already stops an
    // unanswered call from truncating the audio, so the recording is still
    // correct, just potentially noisier.
    void silenceRinger().then((ok) => {
      recLog("ringer.silenced", {
        ok,
        note: ok ? "ringer muted for this recording"
                 : "no Do Not Disturb access — ringer left as-is",
      }, session?.id);
    });

    return { ok: true, sessionId: session.id };
  } catch (e: any) {
    const msg = String(e?.message ?? e);
    recLog("recording.error", { phase: "start", message: msg });
    // Tear down cleanly so the next attempt starts from a known state, and
    // drop the session record — there is no audio to recover.
    releaseRecorder();
    recorder = null;
    session = null;
    userWantsRecording = false;
    stopTicker();
    return { ok: false, error: friendlyStartError(msg) };
  } finally {
    busy = false;
    emit();
  }
}

function friendlyStartError(msg: string): string {
  const m = msg.toLowerCase();
  if (m.includes("service")) {
    // AudioRecordingServiceException — the foreground service could not start.
    // On Android 14+ this is what a missing FOREGROUND_SERVICE_MICROPHONE
    // permission or a denied notification permission looks like.
    return (
      "Background recording couldn't start. Allow MinuteX to show " +
      "notifications, then try again."
    );
  }
  if (m.includes("permission")) {
    return "MinuteX needs the microphone to record this meeting.";
  }
  if (m.includes("space") || m.includes("storage")) {
    return "Not enough free space to record.";
  }
  return "Couldn't start recording: " + msg;
}

/** The USER asking to pause. Sets intent false — nothing will auto-resume. */
export function pauseByUser(): void {
  if (!recorder || !session || session.state !== "RECORDING") return;
  userWantsRecording = false;
  try {
    recorder.pause();
  } catch (e: any) {
    recLog("recording.error", { phase: "pause", message: String(e?.message ?? e) }, session.id);
  }
  // Transition regardless of whether pause() threw: the user asked for silence
  // and the UI must show it. The next reconcile() tick verifies the native
  // side actually stopped and escalates if it didn't.
  transition("PAUSED_BY_USER");
}

/** The USER asking to resume, from any paused state. */
export async function resumeByUser(): Promise<void> {
  if (!recorder || !session || !isPaused(session.state)) return;
  const wasInvoluntary = isInvoluntaryPause(session.state);
  userWantsRecording = true;

  // Resuming out of an INTERRUPTION is not the same operation as resuming out
  // of the user's own pause.
  //
  // After a user pause the recorder is healthy and record() genuinely resumes
  // it. After an interruption took the microphone the recorder is bound to a
  // dead input and record() succeeds while capturing pure silence — the
  // original bug. So delegate to attemptResume(), which rebuilds the recorder
  // onto a fresh segment and refuses to accept a resume that meters silent.
  if (wasInvoluntary) {
    // NOTE: deliberately not setting `busy` around this call — attemptResume()
    // bails out early when busy is set, so doing so here would make the user's
    // tap silently do nothing. attemptResume() manages the flag itself.
    resumeCooldown = 0;
    // Clear the cached focus-loss latch: the user is telling us the
    // interruption is over, and on some OEMs the GAIN callback never arrives,
    // which would otherwise leave the resume permanently gated.
    focusLost = false;
    await attemptResume();
    if (session && isInvoluntaryPause(session.state)) {
      // attemptResume() declined — the interruption is still in force.
      lastError = isInCall()
        ? "Can't resume while a call is in progress — it'll continue by itself when the call ends."
        : "Can't resume yet — something else is using the microphone.";
      emit();
    }
    return;
  }

  busy = true;
  emit();
  try {
    await configureSession();
    recorder.record();
    const st = recorder.getStatus();
    if (!st.isRecording) throw new Error("recorder did not resume");
    resumeCooldown = 0;
    silenceCount = 0;
    transition("RECORDING", undefined, "resumed by user");
  } catch (e: any) {
    lastError = "Can't resume yet — something else is using the microphone.";
    recLog("recording.error", { phase: "resume", message: String(e?.message ?? e) }, session.id);
  } finally {
    busy = false;
    emit();
  }
}

/**
 * Stop and finalize. Returns the completed session, or null if there was
 * nothing usable.
 *
 * This is the moment the audio becomes a real artifact, so it is also where we
 * are strictest: the file is verified to exist and to be more than a container
 * header before the session is called COMPLETED. A session that reaches
 * COMPLETED is a promise that there is uploadable audio behind it.
 */
export async function stop(): Promise<RecSession | null> {
  if (!session) return null;
  if (session.state === "FINALIZING") return null;
  userWantsRecording = false;
  return finalize("user");
}

/**
 * Re-entrancy guard for finalize().
 *
 * finalize() can be reached from four directions at once: the user tapping
 * Stop, the recorder's error event, the low-storage auto-save, and the
 * media-services-reset salvage. Two concurrent runs would both call
 * recorder.stop() — the second throwing — and both write a session record,
 * with the loser's stale `duration` and `size` overwriting the winner's. The
 * state check in stop() is not enough on its own because the callers above do
 * not all go through stop(). A single in-flight promise, shared by every
 * caller, means whoever gets there first finalizes and everyone else awaits
 * the same answer.
 */
let finalizing: Promise<RecSession | null> | null = null;

function finalize(cause: "user" | "error" | "interrupted"): Promise<RecSession | null> {
  if (!session) return Promise.resolve(null);
  if (finalizing) return finalizing;
  finalizing = runFinalize(cause).finally(() => { finalizing = null; });
  return finalizing;
}

async function runFinalize(cause: "user" | "error" | "interrupted"): Promise<RecSession | null> {
  if (!session) return null;
  const s0 = session;
  busy = true;
  transition("FINALIZING", undefined, cause);

  // Prefer the recorder's own duration, cross-checked against our segments.
  let recorderSeconds: number | null = null;
  try {
    recorderSeconds = recorder?.getStatus()?.durationMillis != null
      ? (recorder.getStatus().durationMillis as number) / 1000
      : null;
  } catch {
    recorderSeconds = null;
  }

  try {
    try {
      await recorder?.stop();
    } catch (e: any) {
      // stop() throwing usually still leaves a playable file (MediaRecorder
      // finalizes on stop failure too), so we press on and let the file checks
      // below decide whether we actually have audio.
      recLog("recording.error", { phase: "stop", message: String(e?.message ?? e) }, s0.id);
    }

    let uri = session?.fileUri ?? recorder?.uri ?? null;

    // Join the pieces if an interruption split this recording across files.
    // Must happen AFTER the recorder has stopped (so the last segment's bytes
    // are all flushed) and BEFORE we measure size/duration, since the merged
    // file is what actually gets uploaded.
    if (session && (session.extraFiles?.length ?? 0) > 0) {
      const joined = await concatSegments({ ...session, fileUri: uri });
      if (joined.merged && joined.uri) {
        uri = joined.uri;
      } else if (joined.uri) {
        // Fell back to a single part. The recording is incomplete, so say so
        // rather than let it look whole.
        uri = joined.uri;
        lastError =
          "Part of this recording could not be joined after an interruption — " +
          "the longest piece was kept.";
      }
    }

    let final: RecSession = {
      ...(session as RecSession),
      fileUri: uri,
      state: "FINALIZING",
    };
    // Close the open segment before measuring.
    const now = Date.now();
    final.segments = final.segments.map((seg) =>
      seg.endedAt == null ? { ...seg, endedAt: now } : seg
    );
    final.interruptions = final.interruptions.map((i) =>
      i.endedAt == null ? { ...i, endedAt: now } : i
    );
    final.duration = reconcileDuration(final, recorderSeconds);
    final.size = fileSize(final);
    final.log = getSessionLog(s0.id).slice(-80).map((l) => ({
      t: l.t, event: l.event, data: l.data,
    }));

    session = final;

    if (!uri || !hasUsableAudio(final)) {
      // Nothing worth keeping. Say so plainly instead of creating an empty
      // recording the user will find later and not understand.
      session = {
        ...final,
        state: "ERROR",
        error: lastError ?? "No audio was captured — the recording was empty.",
      };
      saveSession(session);
      recLog("recording.error", {
        note: "no usable audio at finalize",
        uri,
        size: final.size,
      }, s0.id);
      const dead = session;
      await teardown();
      emit();
      return dead;
    }

    // Set COMPLETED directly rather than via transition().
    //
    // transition() exists to maintain the segment/interruption bookkeeping on
    // the way between live states, and that work is already done above — with
    // the measured duration and size folded in, which transition() does not
    // know about and would recompute from scratch. Going through it here would
    // either no-op (prev === next once state is assigned) or overwrite the
    // reconciled duration with the raw segment sum. So the write is explicit,
    // and the log line that transition() would have emitted is emitted here.
    session = { ...final, state: "COMPLETED", upload: "LOCAL" };
    saveSession(session);
    recLog("file.finalized", {
      uri,
      size: session.size,
      duration: session.duration,
      segments: session.segments.length,
      interruptions: session.interruptions.length,
    }, session.id);
    recLog("recording.stopped", {
      cause,
      seconds: session.duration,
      size: session.size,
    }, session.id);
    const done = session;
    await teardown();
    emit();
    return done;
  } finally {
    busy = false;
    emit();
  }
}

/**
 * Release the recorder and the audio session, keeping the session RECORD.
 *
 * `session` deliberately survives so the screen can show the completed state
 * and hand it to the uploader. Only reset() clears it.
 */
async function teardown(): Promise<void> {
  stopTicker();
  statusSub?.remove();
  statusSub = null;
  releaseRecorder();
  level = null;
  stallCount = 0;
  resumeCooldown = 0;
  await releaseSession();
}

/**
 * Discard the current recording, deleting its audio.
 *
 * The ONE path that destroys audio on purpose, and it exists only behind an
 * explicit user confirmation in the UI. Everything else preserves bytes.
 */
export async function discard(): Promise<void> {
  const s = session;
  if (!s) return;
  userWantsRecording = false;
  try { await recorder?.stop(); } catch { /* fine */ }
  await teardown();
  try {
    if (s.fileUri) {
      const f = new File(s.fileUri);
      if (f.exists) f.delete();
    }
  } catch { /* best effort */ }
  const { deleteSession } = await import("./rec-store");
  deleteSession(s.id, s.format);
  recLog("state.transition", { note: "discarded by user" }, s.id);
  session = null;
  lastError = null;
  emit();
}

/** Clear a finished/errored session so the screen returns to IDLE. */
export function reset(): void {
  if (session && (session.state === "RECORDING" || isPaused(session.state))) return;
  session = null;
  lastError = null;
  level = null;
  inputName = null;
  emit();
}

/** Dismiss just the error banner, keeping the session. */
export function clearError(): void {
  lastError = null;
  emit();
}

/**
 * Adopt a session recovered at launch, so the UI can show it.
 *
 * Used by the recovery path: an interrupted recording becomes visible and
 * uploadable rather than sitting invisibly on disk.
 */
export function adoptSession(s: RecSession): void {
  if (session && (session.state === "RECORDING" || isPaused(session.state))) return;
  session = s;
  emit();
}

/** Is a recording live right now? For the global "recording" indicator. */
export function isRecordingNow(): boolean {
  const st = session?.state;
  return st === "RECORDING" || (st != null && isPaused(st));
}
