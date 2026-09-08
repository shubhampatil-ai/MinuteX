// lib/rec-store.ts — the durable side of a recording session.
//
// WHY THIS EXISTS
// A recording is two things: bytes on disk, and the knowledge of what those
// bytes are. expo-audio gives us the first. Before this file, the second lived
// only in React state and a module-level array — so an app kill (OOM in the
// background, a crash, the user swiping the app away) left an orphan .m4a in
// the cache directory that nothing on earth knew about. The audio existed and
// was unreachable, which is indistinguishable from having lost it.
//
// So every session writes a small JSON sidecar next to its audio, updated at
// each transition. On launch, recoverSessions() reads the sidecars back and
// finishes what was interrupted. The sidecar is the source of truth for
// "does the user have unuploaded audio?", and it is what makes the answer
// survive process death.
//
// WHERE THE FILES LIVE — Paths.document, never Paths.cache.
// expo-audio defaults to the cache directory, which the OS is explicitly
// allowed to purge under storage pressure. A meeting recording waiting for
// wifi is exactly the kind of file that gets purged, and losing it is the
// worst failure this app has. `document` is the OS's "do not delete this"
// directory. We pay for it by having to clean up after ourselves, which
// pruneUploaded() does once the server has confirmed the bytes.
import { Directory, File, Paths } from "expo-file-system";
import { recLog } from "./rec-log";

// ---------------------------------------------------------------------------
// The state machine's vocabulary
// ---------------------------------------------------------------------------

// Recording state. The distinction that matters most is between the PAUSED_BY_*
// variants: PAUSED_BY_USER must never auto-resume (the user chose silence and
// resuming behind their back would record something they meant to keep off the
// record), while every other pause is involuntary and SHOULD resume the moment
// the cause clears. Collapsing these into one "paused" is the single most
// consequential bug this refactor fixes.
export type RecState =
  | "IDLE"
  | "RECORDING"
  | "PAUSED_BY_USER"
  | "PAUSED_BY_CALL"
  | "PAUSED_BY_AUDIO_INTERRUPTION"
  | "PAUSED_BY_MICROPHONE"
  | "FINALIZING"
  | "COMPLETED"
  | "ERROR";

/** Every pause state that the app entered on the user's behalf, not by request. */
export const INVOLUNTARY_PAUSES: RecState[] = [
  "PAUSED_BY_CALL",
  "PAUSED_BY_AUDIO_INTERRUPTION",
  "PAUSED_BY_MICROPHONE",
];

export function isPaused(s: RecState): boolean {
  return s === "PAUSED_BY_USER" || INVOLUNTARY_PAUSES.includes(s);
}

export function isInvoluntaryPause(s: RecState): boolean {
  return INVOLUNTARY_PAUSES.includes(s);
}

/** Whether the session is live enough that the audio file is still being written. */
export function isActive(s: RecState): boolean {
  return s === "RECORDING" || isPaused(s);
}

// Upload lifecycle, tracked separately from the recording state because the two
// are genuinely independent: a COMPLETED recording can sit at LOCAL for days
// (offline), and PROCESSING happens entirely server-side after the bytes land.
export type UploadState =
  | "LOCAL"         // on disk, nothing attempted yet (or waiting for network)
  | "UPLOADING"
  | "UPLOAD_FAILED"
  | "UPLOADED"      // server confirmed receipt — the local copy is now expendable
  | "PROCESSING"    // transcription/AI running server-side
  | "COMPLETED";

/** Why an involuntary pause happened, kept for the timeline + diagnostics. */
export type InterruptionReason =
  | "call"
  | "audio_interruption"
  | "microphone_unavailable"
  | "route_lost"
  | "media_services_reset"
  | "unknown";

export type Interruption = {
  reason: InterruptionReason;
  /** Epoch millis when the recording actually stopped capturing. */
  startedAt: number;
  /** Epoch millis when it resumed, or null while still interrupted. */
  endedAt: number | null;
  /** Whether the app resumed automatically (vs. the user tapping Resume). */
  autoResumed?: boolean;
};

/**
 * A contiguous stretch of captured audio. A pause/resume cycle closes one
 * segment and opens the next.
 *
 * WHY TRACK THESE — the recorder's own duration counter is the only thing the
 * UI used to trust, and on Android it is wall-clock arithmetic
 * (`getAudioRecorderDurationMillis` accumulates `now - startTime`), which
 * drifts from the real encoded length across pause/resume. Summing the
 * segments gives us an independent figure to reconcile against, which is what
 * lets us assert the final duration matches the audio actually captured
 * instead of hoping it does.
 */
export type Segment = {
  startedAt: number;
  endedAt: number | null;
};

export type RecSession = {
  /** Stable id. Also the sidecar filename and the audio filename stem. */
  id: string;
  createdAt: number;
  state: RecState;
  upload: UploadState;
  /** file:// URI of the audio. Set once the recorder produces a file. */
  fileUri: string | null;
  /**
   * Additional audio files, when the recording had to continue into a new one.
   *
   * WHY THIS EXISTS. An interruption that takes the microphone (a phone call,
   * most often) leaves the MediaRecorder bound to a dead input: it keeps
   * "recording" but encodes only silence, and it never recovers — so resuming
   * onto the same recorder produced a file that was silent from the call
   * onward. The fix is to build a fresh recorder on resume, and a fresh
   * recorder means a fresh file, because MPEG-4 cannot be extended by
   * appending bytes to a finalized container.
   *
   * So a recording is `fileUri` plus these, in capture order. They are joined
   * at finalize (see concatSegments) into a single file for upload, which keeps
   * the one-file-per-recording contract the upload pipeline and the backend
   * both assume. If joining fails the parts are still all on disk and listed
   * here, so nothing is ever lost to a failed merge.
   */
  extraFiles?: string[];
  /** Container/extension, as the upload pipeline's `format` key. */
  format: string;
  /**
   * Which recording engine produced this session.
   *
   *   "aac"  expo-audio / MediaRecorder (the original path)
   *   "wav"  the experimental native AudioRecord recorder
   *
   * Persisted with the session because finalize and merge behaviour differ
   * between the two, and a session recovered from its sidecar after a relaunch
   * has to be finalized the way it was recorded — not the way the current
   * preference says. Absent on sessions written before the WAV engine existed,
   * which is why every read treats undefined as "aac".
   */
  engine?: "aac" | "wav";
  /** Best-known duration in seconds (see reconcileDuration). */
  duration: number;
  /** Bytes on disk at last check — for the low-storage + sanity checks. */
  size: number | null;
  segments: Segment[];
  interruptions: Interruption[];
  /** audio_s3_key, once the presign ticket is issued. */
  key?: string;
  /**
   * IDEMPOTENCY. The presign ticket is requested ONCE per session and reused
   * across every retry, so a session can never create two timeline rows for
   * one recording. See lib/uploads.tsx for why this is the linchpin of
   * duplicate-upload prevention.
   */
  uploadUrl?: string;
  uploadUrlExpiresAt?: number;
  /** Attempt counter, for backoff and for giving up gracefully. */
  attempts: number;
  /** Last user-facing error, when state is ERROR or upload is UPLOAD_FAILED. */
  error?: string;
  /** Set once the server confirms — the ONLY licence to delete local bytes. */
  confirmedAt?: number;
  /** Tail of the diagnostic log, persisted with the session. */
  log?: { t: number; event: string; data?: Record<string, unknown> }[];
};

// ---------------------------------------------------------------------------
// Locations
// ---------------------------------------------------------------------------

// One directory holding both the audio and the sidecars, inside `document` so
// the OS will not reclaim it. Named for what it is, so a user browsing files
// on Android sees something meaningful.
const DIR_NAME = "minutex-recordings";

function recordingsDir(): Directory {
  return new Directory(Paths.document, DIR_NAME);
}

/** Create the recordings directory if it isn't there yet. Idempotent. */
export function ensureDir(): Directory {
  const dir = recordingsDir();
  try {
    if (!dir.exists) dir.create({ intermediates: true });
  } catch {
    // A create race with another call is harmless — the directory exists
    // either way, which is all the caller needs.
  }
  return dir;
}

/** Absolute path the audio for `id` should live at. */
export function audioPathFor(id: string, format: string): string {
  return new File(recordingsDir(), `${id}.${format}`).uri;
}

function sidecarFor(id: string): File {
  return new File(recordingsDir(), `${id}.json`);
}

// ---------------------------------------------------------------------------
// Sidecar read/write
// ---------------------------------------------------------------------------

/**
 * Persist a session record. Synchronous on purpose.
 *
 * The whole value of the sidecar is that it is on disk BEFORE the thing it
 * describes can go wrong. An async write can be interrupted between the state
 * change and the flush — which is exactly the window a crash-recovery record
 * has to survive. expo-file-system's `write` is synchronous and the payload is
 * well under a kilobyte, so this costs nothing measurable even called on every
 * transition.
 */
export function saveSession(s: RecSession): void {
  try {
    ensureDir();
    const f = sidecarFor(s.id);
    f.write(JSON.stringify(s));
  } catch (e: any) {
    // A failed sidecar write must not stop a recording — the audio matters
    // more than the bookkeeping. Log it so recovery gaps are explicable.
    recLog("recording.error", {
      note: "sidecar write failed",
      message: String(e?.message ?? e),
    }, s.id);
  }
}

export function loadSession(id: string): RecSession | null {
  try {
    const f = sidecarFor(id);
    if (!f.exists) return null;
    return JSON.parse(f.textSync()) as RecSession;
  } catch {
    return null;
  }
}

/** Every session on disk, newest first. */
export function listSessions(): RecSession[] {
  try {
    const dir = recordingsDir();
    if (!dir.exists) return [];
    const out: RecSession[] = [];
    for (const entry of dir.list()) {
      if (!(entry instanceof File)) continue;
      if (!entry.name.endsWith(".json")) continue;
      try {
        const s = JSON.parse(entry.textSync()) as RecSession;
        if (s && typeof s.id === "string") out.push(s);
      } catch {
        // A truncated sidecar (killed mid-write) is unreadable but the AUDIO
        // beside it may be fine. Skip the record; sweepOrphanAudio() below is
        // what gives that audio a second chance.
      }
    }
    return out.sort((a, b) => b.createdAt - a.createdAt);
  } catch {
    return [];
  }
}

/** Remove a session's sidecar AND its audio. Only for confirmed/discarded work. */
export function deleteSession(id: string, format?: string): void {
  const s = format ? null : loadSession(id);
  const fmt = format ?? s?.format ?? "m4a";
  // The WAV engine names its output `{id}_final.wav` and its segments
  // `{id}_segment_NNN.wav` rather than `{id}.{fmt}`, because the uploadable
  // file is the MERGE of several. They are found by listing rather than by
  // reconstructing names, since the segment count is not known here — and
  // missing one would leave audio behind for the orphan sweep to re-adopt as a
  // phantom recording of the same meeting.
  const extra: File[] = [];
  if (fmt === "wav") {
    try {
      for (const entry of recordingsDir().list()) {
        if (!(entry instanceof File)) continue;
        if (entry.name === `${id}_final.wav` || entry.name.startsWith(`${id}_segment_`)) {
          extra.push(entry);
        }
      }
    } catch {
      // Directory unreadable — the named deletes below still run.
    }
  }
  for (const f of [sidecarFor(id), new File(recordingsDir(), `${id}.${fmt}`), ...extra]) {
    try {
      if (f.exists) f.delete();
    } catch {
      // Best effort. A leftover file is a storage nuisance, not a data loss,
      // and pruneUploaded() will try again next launch.
    }
  }
}

// ---------------------------------------------------------------------------
// Duration accounting
// ---------------------------------------------------------------------------

/** Sum of the closed + open segments, in seconds. */
export function segmentSeconds(s: RecSession, now = Date.now()): number {
  let ms = 0;
  for (const seg of s.segments) {
    ms += (seg.endedAt ?? now) - seg.startedAt;
  }
  return Math.max(0, ms / 1000);
}

/**
 * Settle on a final duration, preferring the recorder's own figure but
 * refusing an obviously-wrong one.
 *
 * The recorder is authoritative when it agrees with our segment arithmetic,
 * because it counts encoded audio rather than wall clock. When the two
 * disagree badly — the recorder reporting a paused stretch as recorded, or
 * reporting zero after a failed stop — the segment sum is the safer answer.
 * TOLERANCE is generous: small differences are just encoder framing and
 * pause latency, not a bug worth overriding a good number for.
 */
const DURATION_TOLERANCE_S = 5;

export function reconcileDuration(
  s: RecSession,
  recorderSeconds: number | null | undefined
): number {
  const fromSegments = segmentSeconds(s);
  if (recorderSeconds == null || !Number.isFinite(recorderSeconds) || recorderSeconds <= 0) {
    return Math.round(fromSegments);
  }
  const drift = Math.abs(recorderSeconds - fromSegments);
  if (drift <= DURATION_TOLERANCE_S || fromSegments <= 0) {
    return Math.round(recorderSeconds);
  }
  recLog("state.transition", {
    note: "duration drift, using segment sum",
    recorderSeconds: Math.round(recorderSeconds),
    segmentSeconds: Math.round(fromSegments),
  }, s.id);
  return Math.round(fromSegments);
}

// ---------------------------------------------------------------------------
// Storage headroom
// ---------------------------------------------------------------------------

// Refuse to START a recording with less than this free. AAC at the
// HIGH_QUALITY preset is ~1 MB/min, so 250 MB is roughly four hours of
// headroom — enough that a normal meeting cannot run the disk dry, while
// still letting someone with a nearly-full phone record at all.
export const MIN_FREE_BYTES_TO_START = 250 * 1024 * 1024;

// While recording, warn at this level. Lower than the start threshold on
// purpose: stopping a meeting in progress is itself a loss, so we only
// escalate when the disk is genuinely nearly gone.
export const LOW_FREE_BYTES_WARN = 80 * 1024 * 1024;

// Below this, keeping the file open risks a truncated container. Finalize
// while there is still room to write the moov atom.
export const CRITICAL_FREE_BYTES = 25 * 1024 * 1024;

export function freeBytes(): number | null {
  try {
    const n = Paths.availableDiskSpace;
    return typeof n === "number" && Number.isFinite(n) ? n : null;
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------------------
// Recovery
// ---------------------------------------------------------------------------

export type Recovery = {
  /** Sessions the app died during — audio on disk, never finalized. */
  interrupted: RecSession[];
  /** Sessions finalized but never confirmed by the server. */
  pendingUpload: RecSession[];
};

/**
 * Read the sidecars and sort out what needs finishing. Called once at launch.
 *
 * A session in an ACTIVE state (RECORDING or any PAUSED_BY_*) with a sidecar
 * on disk means the process died mid-recording: there is no live recorder to
 * ask, so whatever the encoder managed to flush is what we have. We move it to
 * COMPLETED rather than ERROR, because the honest description of that file is
 * "a recording that ends abruptly", not "a failure" — and a short recording
 * the user can still upload beats a scary error over usable audio.
 */
export function recoverSessions(): Recovery {
  const interrupted: RecSession[] = [];
  const pendingUpload: RecSession[] = [];

  for (const s of listSessions()) {
    // Already done and confirmed — nothing to recover; pruning handles it.
    if (s.upload === "COMPLETED" || s.upload === "UPLOADED" || s.upload === "PROCESSING") {
      continue;
    }

    const audioOk = hasUsableAudio(s);

    if (isActive(s.state)) {
      recLog("recovery.attempted", {
        was: s.state,
        hasAudio: audioOk,
        segments: s.segments.length,
      }, s.id);

      // Close the open segment at the last moment we know the app was alive.
      // We can't know exactly when the process died, so we use the sidecar's
      // own last-write time via the audio file's mtime — the closest honest
      // bound available.
      const closedAt = lastWriteMillis(s) ?? Date.now();
      const segments = s.segments.map((seg) =>
        seg.endedAt == null ? { ...seg, endedAt: closedAt } : seg
      );
      const openInterruption = s.interruptions.map((i) =>
        i.endedAt == null ? { ...i, endedAt: closedAt } : i
      );

      const recovered: RecSession = {
        ...s,
        segments,
        interruptions: openInterruption,
        state: audioOk ? "COMPLETED" : "ERROR",
        upload: audioOk ? "LOCAL" : s.upload,
        duration: Math.round(segmentSeconds({ ...s, segments }, closedAt)),
        size: fileSize(s),
        error: audioOk ? undefined : "Recording was interrupted before any audio was saved.",
      };
      saveSession(recovered);
      recLog("recovery.result", {
        state: recovered.state,
        duration: recovered.duration,
        size: recovered.size,
      }, s.id);
      if (audioOk) interrupted.push(recovered);
      continue;
    }

    // Finalized but the bytes never made it (or never started). This is the
    // ordinary offline case as well as the crash-during-upload case.
    if (s.state === "COMPLETED" || s.state === "FINALIZING") {
      if (!audioOk) {
        // The sidecar promises audio that isn't there — the cache-purge
        // scenario, or a delete that raced. Nothing to upload; mark it so the
        // UI can explain rather than retrying forever.
        const dead: RecSession = {
          ...s,
          state: "ERROR",
          error: "The audio file is no longer on this device.",
        };
        saveSession(dead);
        recLog("file.missing", { uri: s.fileUri }, s.id);
        continue;
      }
      // A session caught mid-PUT reads as UPLOADING; it isn't, there is no
      // live task any more. Reset it to a retryable state.
      const reset: RecSession =
        s.upload === "UPLOADING"
          ? { ...s, upload: "LOCAL", state: "COMPLETED" }
          : { ...s, state: "COMPLETED" };
      if (reset.upload !== s.upload) saveSession(reset);
      pendingUpload.push(reset);
    }
  }

  return { interrupted, pendingUpload };
}

/** Does this session's audio exist and hold more than a container header? */
export function hasUsableAudio(s: RecSession): boolean {
  const size = fileSize(s);
  // An .m4a with only its header is a few hundred bytes and decodes to
  // nothing. 8 KB is comfortably above that and below any real recording.
  return size != null && size > 8 * 1024;
}

export function fileSize(s: RecSession): number | null {
  if (!s.fileUri) return null;
  try {
    const f = new File(s.fileUri);
    return f.exists ? f.size : null;
  } catch {
    return null;
  }
}

/**
 * Join a multi-segment recording into one file, in capture order.
 *
 * Only reachable when an interruption forced a new recorder mid-recording (see
 * `extraFiles`). Returns the URI to upload — the merged file when joining
 * worked, or the longest single segment when it did not.
 *
 * WHY BYTE CONCATENATION IS VALID HERE: Android records ADTS-AAC, a stream of
 * self-describing frames with no container index, so the concatenation of two
 * such files is itself a well-formed ADTS file. This would NOT be true of the
 * .m4a the iOS side uses — MPEG-4 keeps a moov atom per file — which is exactly
 * why REC_OPTIONS switches the Android container. Guarded below so a future
 * format change can't silently start corrupting recordings.
 *
 * NOTHING IS DELETED on failure. The parts stay on disk and stay listed on the
 * session, because a failed merge must never be the reason a meeting is lost.
 */
export async function concatSegments(s: RecSession): Promise<{
  uri: string | null;
  merged: boolean;
  error?: string;
}> {
  const parts = [...(s.extraFiles ?? []), s.fileUri].filter(
    (u): u is string => typeof u === "string" && u.length > 0
  );
  if (parts.length <= 1) return { uri: s.fileUri, merged: false };

  // Byte-joining is only sound for self-framing streams. Refuse otherwise and
  // fall back rather than write a file that looks fine and plays short.
  if (s.format !== "aac") {
    recLog("recording.error", {
      phase: "concat",
      note: `refusing to byte-join .${s.format} — not a self-framing container`,
      parts: parts.length,
    }, s.id);
    return {
      uri: longestPart(parts),
      merged: false,
      error: `Cannot join .${s.format} segments`,
    };
  }

  const target = new File(ensureDir(), `${s.id}-merged.aac`);
  try {
    // Stream rather than buffer: a 3-hour recording is ~180 MB and reading the
    // parts into memory to join them would risk an OOM on exactly the long
    // recordings this feature is meant to protect.
    if (target.exists) target.delete();
    target.create();
    const sink = target.writableStream().getWriter();
    let written = 0;
    try {
      for (const uri of parts) {
        const part = new File(uri);
        if (!part.exists || part.size === 0) continue;
        const reader = part.readableStream().getReader();
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          await sink.write(value);
          written += value.byteLength;
        }
      }
    } finally {
      await sink.close();
    }

    if (written === 0) throw new Error("no bytes written");

    recLog("file.finalized", {
      note: "segments joined",
      parts: parts.length,
      bytes: written,
      uri: target.uri,
    }, s.id);
    return { uri: target.uri, merged: true };
  } catch (e: any) {
    // Leave every original part untouched; hand back the biggest one so the
    // user still gets the bulk of the meeting.
    try { if (target.exists) target.delete(); } catch { /* best effort */ }
    const message = String(e?.message ?? e);
    recLog("recording.error", {
      phase: "concat",
      message,
      parts: parts.length,
      note: "originals kept",
    }, s.id);
    return { uri: longestPart(parts), merged: false, error: message };
  }
}

/** The biggest surviving part — the best single-file fallback after a failed join. */
function longestPart(parts: string[]): string | null {
  let best: string | null = null;
  let bestSize = -1;
  for (const uri of parts) {
    try {
      const f = new File(uri);
      if (f.exists && f.size > bestSize) { bestSize = f.size; best = uri; }
    } catch { /* skip unreadable parts */ }
  }
  return best;
}

function lastWriteMillis(s: RecSession): number | null {
  if (!s.fileUri) return null;
  try {
    const f = new File(s.fileUri);
    return f.exists ? f.lastModified ?? null : null;
  } catch {
    return null;
  }
}

/**
 * Delete local audio for sessions the SERVER has confirmed, and only those.
 *
 * `confirmedAt` is set exactly once, by the upload pipeline, after
 * upload-complete returns 2xx. Nothing else may delete audio. This is the
 * rule that makes "a temporary failure must not delete a recording" true by
 * construction rather than by everyone remembering.
 */
export function pruneUploaded(): number {
  let freed = 0;
  for (const s of listSessions()) {
    const confirmed =
      !!s.confirmedAt && (s.upload === "UPLOADED" || s.upload === "PROCESSING" || s.upload === "COMPLETED");
    if (!confirmed) continue;
    const size = fileSize(s) ?? 0;
    deleteSession(s.id, s.format);
    freed += size;
  }
  if (freed > 0) recLog("file.finalized", { note: "pruned confirmed audio", freed });
  return freed;
}

/**
 * Audio in the recordings directory with no readable sidecar.
 *
 * This is the last safety net: a sidecar truncated by a kill leaves audio that
 * listSessions() cannot see. Rather than let it sit forever, we synthesize a
 * minimal session for it so it shows up as recoverable. The metadata is poorer
 * (no segments, so duration comes from the file's own mtime span) but the
 * audio is intact, and intact audio is the thing worth saving.
 */
export function sweepOrphanAudio(): RecSession[] {
  const adopted: RecSession[] = [];
  try {
    const dir = recordingsDir();
    if (!dir.exists) return adopted;
    const known = new Set(listSessions().map((s) => s.id));
    for (const entry of dir.list()) {
      if (!(entry instanceof File)) continue;
      if (entry.name.endsWith(".json")) continue;
      const dot = entry.name.lastIndexOf(".");
      if (dot <= 0) continue;
      let id = entry.name.slice(0, dot);
      const format = entry.name.slice(dot + 1);

      // WAV-engine filenames are `{id}_final.wav` and `{id}_segment_NNN.wav`,
      // so the stem is NOT the session id. Left unmapped, a rescued
      // `abc_final.wav` would be adopted under the id "abc_final" — a phantom
      // recording sitting next to the real session, and one that a later
      // deleteSession("abc") could never clean up.
      //
      // Segments are skipped outright: they are only ever intermediate. If the
      // merge succeeded they were deleted; if it failed the parent session
      // still lists them and finalize already chose the longest as a fallback.
      // Adopting one would show the user a fragment as though it were the
      // meeting.
      if (format === "wav") {
        const seg = id.match(/^(.+)_segment_\d+$/);
        if (seg) {
          // Parent gone. The audio is real, so rescue it under the parent id
          // rather than the segment name — but only the FIRST such segment, or
          // several orphans of one recording would each claim the same id and
          // overwrite each other's sidecar.
          id = seg[1];
        } else {
          const fin = id.match(/^(.+)_final$/);
          if (fin) id = fin[1];
        }
      }

      if (known.has(id)) continue;
      // A "-merged" file belongs to a session that already exists (concatSegments
      // writes it), so adopting it would create a phantom SECOND recording of
      // the same meeting. If its parent session is gone the sweep still rescues
      // the audio via the parent id below.
      if (id.endsWith("-merged")) {
        const parent = id.slice(0, -"-merged".length);
        if (known.has(parent)) continue;
      }
      if (entry.size <= 8 * 1024) {
        // Header-only leftover: nothing to rescue, and leaving it would show
        // the user a 0-second "recording". Reclaim the space instead.
        try { entry.delete(); } catch { /* best effort */ }
        continue;
      }
      const created = entry.lastModified ?? Date.now();
      const s: RecSession = {
        id,
        createdAt: created,
        state: "COMPLETED",
        upload: "LOCAL",
        fileUri: entry.uri,
        format,
        // Unknown — the pipeline treats 0 as "let the server measure it",
        // which the transcription step does anyway.
        duration: 0,
        size: entry.size,
        segments: [],
        interruptions: [],
        attempts: 0,
      };
      saveSession(s);
      // Mark it known immediately. Without this, a second orphan mapping to the
      // same id (two segments of one abandoned WAV recording) would overwrite
      // the sidecar just written and the first rescue would be lost.
      known.add(id);
      adopted.push(s);
      recLog("recovery.result", {
        note: "adopted orphan audio",
        size: entry.size,
        format,
      }, id);
    }
  } catch {
    // Never let a sweep failure block launch.
  }
  return adopted;
}

// ---------------------------------------------------------------------------
// Session construction
// ---------------------------------------------------------------------------

let counter = 0;

/** Collision-resistant, sortable, filename-safe. */
export function newSessionId(): string {
  counter = (counter + 1) % 1000;
  return [
    Date.now().toString(36),
    counter.toString(36).padStart(2, "0"),
    Math.random().toString(36).slice(2, 8),
  ].join("-");
}

export function createSession(format = "m4a"): RecSession {
  const s: RecSession = {
    id: newSessionId(),
    createdAt: Date.now(),
    state: "IDLE",
    upload: "LOCAL",
    fileUri: null,
    format,
    duration: 0,
    size: null,
    segments: [],
    interruptions: [],
    attempts: 0,
  };
  ensureDir();
  saveSession(s);
  recLog("session.created", { format }, s.id);
  return s;
}
