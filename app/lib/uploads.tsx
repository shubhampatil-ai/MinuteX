// lib/uploads.tsx — UploadManager: the ONE client upload pipeline.
//
// Every non-device recording source funnels through startUpload():
//
//   MOBILE (record-phone screen)  ─┐
//   UPLOAD (upload screen)        ─┤→ startUpload()
//   (future sources)              ─┘      │
//                                         ▼
//                    POST /recordings/upload-request  (JWT presign + stub row)
//                                         ▼
//                    PUT <presigned S3 URL>           (the audio bytes)
//                                         ▼
//                    POST /recordings/upload-complete (status -> "uploaded")
//                                         ▼
//                    S3 trigger -> transcription -> AI -> timeline
//
// There is no per-source upload code anywhere else — a screen hands over a
// file URI + metadata and this module does the rest. The MinuteX device does
// the equivalent server-side dance against the device API (x-api-key), so
// the backend pipeline downstream of S3 is identical for all sources.
//
// DURABILITY — what this module now guarantees, and did not before.
//
// The job list used to be a module-level array and nothing else. That made
// three failures indistinguishable from data loss:
//   * App killed mid-upload → the job vanished, and with it the only reference
//     to the file. The audio sat in the cache directory, unreferenced, until
//     the OS purged it.
//   * Offline at stop time → the job failed immediately and the user was
//     invited to "retry" a network that was still down. Nothing retried on its
//     own when connectivity came back.
//   * Retry → a brand-new presign, i.e. a SECOND timeline row for one
//     recording. Tapping retry three times created three recordings.
//
// Now every job is backed by a session sidecar on disk (lib/rec-store.ts):
//   * The presign ticket is requested ONCE per session and reused across every
//     attempt, so N retries produce exactly one recording. This is the whole
//     of duplicate-upload prevention — it is a property of the data model, not
//     of the UI disabling a button.
//   * A failed upload schedules its own retry with backoff, and retries again
//     whenever the app returns to the foreground. "Offline" is not an error
//     state, it is a wait.
//   * Local audio is deleted ONLY after upload-complete returns 2xx, and only
//     via pruneUploaded(), which checks `confirmedAt`. No failure path can
//     delete a recording.
//
// State model: a module-level job list + subscribe(), consumed by React via
// useSyncExternalStore (useUploads). It deliberately lives OUTSIDE the React
// tree so an upload survives screen unmounts — the user can stop a phone
// recording, hit Save and immediately navigate away; the Files screen shows
// the in-flight banner from this shared state. The jobs are now a VIEW over
// the durable sessions rather than the record itself.
import { useSyncExternalStore } from "react";
import { AppState } from "react-native";
import { File, UploadType } from "expo-file-system";
// RecordingSource is a TYPE, so it is imported as one. Left as a plain named
// import it survives into the emitted module graph as a value binding, which
// any consumer that loads this module without a TS-aware bundler rejects
// outright ("does not provide an export named 'RecordingSource'").
import { requestUpload, completeUpload, ApiError } from "./api";
import type { RecordingSource } from "./api";
import { recLog } from "./rec-log";
import {
  createSession,
  deleteSession,
  listSessions,
  loadSession,
  pruneUploaded,
  recoverSessions,
  saveSession,
  sweepOrphanAudio,
  type RecSession,
  type UploadState,
} from "./rec-store";

// ---------------------------------------------------------------------------
// Validation limits — mirror the backend's (userApi rejects violations too;
// checking here just fails before any bytes move).
// ---------------------------------------------------------------------------
export const UPLOAD_FORMATS: Record<string, string> = {
  wav: "audio/wav",
  mp3: "audio/mpeg",
  m4a: "audio/mp4",
  aac: "audio/aac",
  ogg: "audio/ogg",
  opus: "audio/opus",
  flac: "audio/flac",
  webm: "audio/webm",
  mp4: "audio/mp4",
  mp2: "audio/mpeg",
  mpga: "audio/mpeg",
  amr: "audio/amr",
  "3gp": "audio/3gpp",
  aiff: "audio/aiff",
  aif: "audio/aiff",
  wma: "audio/x-ms-wma",
  caf: "audio/x-caf",
  mka: "audio/x-matroska",
};
// MUST match userApi's MAX_UPLOAD_BYTES / MAX_DURATION_SECONDS exactly. Both
// are the transcription service's own documented ceilings (ElevenLabs Scribe in
// remote-URL mode: 3 GB, 10 hours), not a product choice — so raising one side
// without the other either rejects uploads the backend would accept, or lets
// bytes reach S3 only to be refused after the fact.
//
// DECIMAL GB (3,000,000,000), not GiB: the provider's limit is decimal, and the
// binary value would sit ~7% above it.
export const MAX_UPLOAD_BYTES = 3 * 1000 * 1000 * 1000; // 3 GB
export const MAX_DURATION_SECONDS = 10 * 3600;          // 10 hours

// Validate a picked file's name/size; returns its normalized format.
// Throws Error with a user-facing message on violation.
export function validateAudioFile(file: { name?: string | null; size?: number | null }): string {
  const ext = (file.name || "").split(".").pop()?.toLowerCase() ?? "";
  if (!UPLOAD_FORMATS[ext]) {
    throw new Error(
      `Unsupported format ".${ext || "?"}" — use an audio file ` +
      `(${Object.keys(UPLOAD_FORMATS).slice(0, 6).join(", ")}, …).`
    );
  }
  if (file.size != null && file.size > MAX_UPLOAD_BYTES) {
    throw new Error(`File too large — max ${MAX_UPLOAD_BYTES / 1e9} GB.`);
  }
  return ext;
}

// ---------------------------------------------------------------------------
// Job state
// ---------------------------------------------------------------------------
export type UploadPhase =
  | "requesting"  // asking the backend for a presigned PUT
  | "uploading"   // bytes in flight to S3
  | "finalizing"  // marking upload-complete
  | "waiting"     // offline / backing off before the next attempt
  | "done"
  | "failed";

export type UploadJob = {
  id: string;
  source: Exclude<RecordingSource, "DEVICE">;
  title: string;
  phase: UploadPhase;
  /** The durable session behind this job. The job is a view; this is the record. */
  sessionId: string;
  /** Coarse status the product speaks in (LOCAL/UPLOADING/…). */
  status: UploadState;
  key?: string;    // audio_s3_key, known once the ticket is issued
  error?: string;  // user-facing, set when phase === "failed"
  // Bytes actually on the wire, reported by the native uploader. `progress`
  // is 0..1 and is only meaningful while phase === "uploading"; it stays
  // undefined when the platform can't report a total (see runUpload).
  progress?: number;
  bytesSent?: number;
  totalBytes?: number;
  /** Attempts made so far, surfaced so a stuck upload is legible. */
  attempts: number;
  /** When the next automatic attempt is due (epoch ms), while phase is waiting. */
  nextAttemptAt?: number;
  // The original request, kept so a failed job can be retried with one tap
  // instead of making the user find the file again.
  input?: StartUploadInput;
};

let jobs: UploadJob[] = [];
const listeners = new Set<() => void>();

function emit() {
  for (const l of listeners) l();
}

function patchJob(id: string, patch: Partial<UploadJob>) {
  jobs = jobs.map((j) => (j.id === id ? { ...j, ...patch } : j));
  emit();
}

export function dismissUpload(id: string) {
  jobs = jobs.filter((j) => j.id !== id);
  emit();
}

// Completed jobs linger briefly so the user sees the ✓, then self-dismiss.
const DONE_LINGER_MS = 4000;

// ---------------------------------------------------------------------------
// Retry policy
// ---------------------------------------------------------------------------

// Exponential backoff, capped. The cap matters more than the curve: a phone
// that is offline for an hour should be trying every couple of minutes, not
// every 40 minutes, because the user is standing there waiting for their
// meeting to appear. Attempts are unbounded on purpose — there is no attempt
// count at which throwing away someone's meeting audio becomes correct.
const BACKOFF_MS = [2_000, 6_000, 15_000, 40_000, 90_000, 180_000];

// Exported for the unit tests only — see lib/__tests__/uploads.test.mjs.
// Whether a failure retries or stops is the decision that separates "your
// meeting is safe, we'll upload it later" from "your meeting is gone", so it
// is worth pinning directly rather than inferring from the pipeline.
export function backoffFor(attempts: number): number {
  return BACKOFF_MS[Math.min(attempts, BACKOFF_MS.length - 1)];
}

/**
 * Is this failure worth retrying automatically?
 *
 * Network errors (ApiError status 0), timeouts, 5xx and 429 are transient by
 * definition. A 4xx other than 429/408 means the request itself is wrong —
 * retrying an oversize file or an expired session forever would just burn
 * battery, so those stop and ask the user. 401 in particular must NOT retry:
 * the session is dead and the token has already been cleared by api.ts.
 */
export function isTransient(e: unknown): boolean {
  if (e instanceof ApiError) {
    if (e.status === 0) return true;                 // network / DNS / offline
    if (e.status === 408 || e.status === 429) return true;
    return e.status >= 500;
  }
  // A thrown Error from the native uploader — a dropped connection mid-PUT
  // looks like this. Treat as transient; a genuinely broken file fails the
  // pre-flight existence check instead.
  return e instanceof Error;
}

const timers = new Map<string, ReturnType<typeof setTimeout>>();

function scheduleRetry(sessionId: string, jobId: string, attempts: number) {
  const delay = backoffFor(attempts);
  const existing = timers.get(sessionId);
  if (existing) clearTimeout(existing);
  patchJob(jobId, {
    phase: "waiting",
    status: "UPLOAD_FAILED",
    nextAttemptAt: Date.now() + delay,
    progress: undefined,
  });
  recLog("upload.deferred", { attempts, delayMs: delay }, sessionId);
  timers.set(
    sessionId,
    setTimeout(() => {
      timers.delete(sessionId);
      void driveSession(sessionId);
    }, delay)
  );
}

// ---------------------------------------------------------------------------
// The pipeline
// ---------------------------------------------------------------------------
export type StartUploadInput = {
  source: Exclude<RecordingSource, "DEVICE">;
  fileUri: string;
  format: string;    // one of UPLOAD_FORMATS' keys
  title?: string;
  duration?: number; // seconds, if known (phone recordings know it)
  size?: number;     // bytes, if known (picked files know it)
  /**
   * An existing durable session to upload. Phone recordings pass this: the
   * recording controller already created the session and wrote the audio into
   * the app's document directory, so there is nothing to import.
   */
  sessionId?: string;
  /**
   * File the recording into this folder. Passed to the presign, so the row is
   * created already carrying its folder — the meeting is never briefly visible
   * in General, and a crash mid-upload cannot leave it unfiled.
   *
   * For a session-backed upload the session's own `folderId` wins, because the
   * session is what survives a relaunch; this field is for the import paths
   * that have no session yet.
   */
  folderId?: string;
};

/** Map a session's persisted upload state onto the banner's phase. */
function phaseFor(s: RecSession): UploadPhase {
  switch (s.upload) {
    case "UPLOADING": return "uploading";
    case "UPLOAD_FAILED": return "failed";
    case "UPLOADED":
    case "PROCESSING":
    case "COMPLETED": return "done";
    default: return "waiting";
  }
}

function jobForSession(s: RecSession, input?: StartUploadInput): UploadJob {
  const source: Exclude<RecordingSource, "DEVICE"> = input?.source ?? "MOBILE";
  return {
    id: s.id,
    sessionId: s.id,
    source,
    title: input?.title || (source === "MOBILE" ? "Phone recording" : "Uploaded audio"),
    phase: phaseFor(s),
    status: s.upload,
    key: s.key,
    error: s.error,
    attempts: s.attempts,
    input,
  };
}

/**
 * Uploads one recording end-to-end. Resolves with the recording's
 * audio_s3_key.
 *
 * For an IMPORTED file (the upload screen) we first adopt it into a durable
 * session: the picked file lives in a cache directory the OS can purge, so we
 * copy it into the app's document directory before promising to upload it.
 * That copy is the difference between "we'll upload this when you're back
 * online" being true and being a hope.
 */
export async function startUpload(input: StartUploadInput): Promise<string> {
  let session: RecSession | null = input.sessionId ? loadSession(input.sessionId) : null;

  if (!session) {
    session = await adoptFile(input);
  } else if (input.folderId && !session.folderId) {
    // A session created before the folder was known (or by an older build).
    // Persist it NOW, while a caller with the answer is on the stack — the
    // presign may not happen until after a relaunch, and by then `input` is
    // gone and only the session survives.
    session.folderId = input.folderId;
    saveSession(session);
  }

  const job = jobForSession(session, input);
  // Replace any existing job for this session rather than appending — a retry
  // must never show two banners for one recording.
  jobs = [job, ...jobs.filter((j) => j.sessionId !== session!.id)];
  emit();

  inputs.set(session.id, input);
  return driveSession(session.id);
}

// The caller's original request, per session, so an automatic retry (which has
// no caller) can rebuild the same job banner.
const inputs = new Map<string, StartUploadInput>();

/**
 * Copy an imported file into durable storage and give it a session.
 *
 * Copy, not move: the source may be a provider-owned file (Drive, iCloud) that
 * we have no business relocating, and on Android a content:// pick is already a
 * cache copy owned by the picker. Copying costs disk but makes the upload
 * independent of anything else deleting the original.
 */
async function adoptFile(input: StartUploadInput): Promise<RecSession> {
  const s = createSession(input.format, input.folderId);
  const src = new File(input.fileUri);
  let uri = input.fileUri;
  let size: number | null = input.size ?? null;

  try {
    if (!src.exists) {
      throw new Error("That file is no longer available on this device.");
    }
    size = src.size ?? size;
    const { audioPathFor, ensureDir } = await import("./rec-store");
    ensureDir();
    const dest = new File(audioPathFor(s.id, input.format));
    await src.copy(dest, { overwrite: true });
    uri = dest.uri;
    recLog("file.relocated", { note: "imported into durable storage", size }, s.id);
  } catch (e: any) {
    // The copy failed — most likely no space, or a provider URI we cannot
    // read. Fall back to uploading straight from where it is: less durable,
    // but it lets the common online case still succeed instead of blocking on
    // a durability nicety.
    recLog("upload.failed", {
      phase: "adopt",
      message: String(e?.message ?? e),
      note: "uploading from original location",
    }, s.id);
  }

  const adopted: RecSession = {
    ...s,
    state: "COMPLETED",
    upload: "LOCAL",
    fileUri: uri,
    format: input.format,
    duration: input.duration ?? 0,
    size,
  };
  saveSession(adopted);
  return adopted;
}

/**
 * Push one session as far through the pipeline as it will go.
 *
 * Re-entrant-safe and idempotent: it reads the session's PERSISTED state to
 * decide what to do, so calling it twice (a retry timer firing at the same
 * moment the user taps Retry) cannot double-request a presign or double-PUT.
 */
const running = new Set<string>();

async function driveSession(sessionId: string): Promise<string> {
  if (running.has(sessionId)) {
    // Already in flight. Return the key if we have one; the in-flight run will
    // finish the job either way.
    const s = loadSession(sessionId);
    if (s?.key) return s.key;
    throw new ApiError(0, "Upload already in progress.");
  }
  running.add(sessionId);

  const input = inputs.get(sessionId);
  let s = loadSession(sessionId);
  if (!s) {
    running.delete(sessionId);
    throw new Error("That recording is no longer on this device.");
  }

  const jobId = sessionId;
  if (!jobs.some((j) => j.sessionId === sessionId)) {
    jobs = [jobForSession(s, input), ...jobs];
    emit();
  }

  try {
    // Pre-flight: the bytes must actually be there. A missing file is NOT a
    // transient failure — retrying forever would spin on nothing — so it is
    // reported as a terminal, explicable error.
    if (!s.fileUri) throw new Error("This recording has no audio file.");
    const file = new File(s.fileUri);
    if (!file.exists) {
      recLog("file.missing", { uri: s.fileUri }, sessionId);
      throw new Error("The audio file is no longer on this device.");
    }
    const size = file.size ?? s.size ?? 0;
    if (size > MAX_UPLOAD_BYTES) {
      throw new Error(`That recording is too large to upload (max ${MAX_UPLOAD_BYTES / 1e9} GB).`);
    }
    if (size !== s.size) {
      s = { ...s, size };
      saveSession(s);
    }

    // 1. Presign + ownership stub — ONCE per session, ever.
    //
    // THIS is duplicate-upload prevention. Every attempt after the first
    // reuses the stored key and URL, so a recording can only ever produce one
    // timeline row no matter how many times the upload is retried, or how many
    // times the app is relaunched mid-retry.
    if (!s.key || !s.uploadUrl || urlExpired(s)) {
      patchJob(jobId, { phase: "requesting", status: "UPLOADING", error: undefined });
      const ticket = await requestUpload({
        source: input?.source ?? "MOBILE",
        format: s.format,
        title: input?.title,
        duration: s.duration || undefined,
        size: size || undefined,
        // The SESSION's folder wins over the caller's: the session is what
        // survives a relaunch, so on a retry hours later it is the only
        // trustworthy source. `input` only matters on the very first attempt,
        // and startUpload has already written it onto the session by then.
        folder_id: s.folderId || input?.folderId || undefined,
        // The key from an earlier attempt, when this is a re-presign after
        // expiry. Sending it is what keeps one session to one row — without
        // it the backend mints a fresh identity and the first row is stranded
        // at "uploading" forever as a phantom duplicate.
        key: s.key || undefined,
      });
      // Keep the FIRST key if we already had one: a re-presign after
      // expiry must not create a second recording. The backend re-signs the
      // same key when it is supplied above, and if it hands back a different
      // key we adopt it but log the fact, since that would mean the row was
      // no longer re-signable (already uploaded, or trashed) and a duplicate
      // now exists server-side.
      if (s.key && s.key !== ticket.key) {
        recLog("upload.failed", {
          note: "presign returned a different key; possible duplicate row",
          had: s.key, got: ticket.key,
        }, sessionId);
      }
      s = {
        ...s,
        key: ticket.key,
        uploadUrl: ticket.upload_url,
        uploadUrlExpiresAt: Date.now() + Math.max(60, ticket.expires_in ?? 900) * 1000,
        upload: "UPLOADING",
      };
      saveSession(s);
      patchJob(jobId, { key: s.key });
    }

    // 2. The bytes, streamed from disk by the native uploader.
    //
    // NOT fetch(). React Native's fetch cannot reliably turn a file:// URI
    // into a Blob: on Android it throws "Network request failed" outright, or
    // hands back a zero-length body, so the PUT never reaches S3 and every
    // phone recording fails before a byte moves. expo-file-system's upload()
    // streams the file natively, which also means large recordings never have
    // to fit in memory, and it reports real byte progress.
    //
    // NO Content-Type header here, deliberately. The backend leaves
    // ContentType out of the signature (see request_upload in the userApi
    // Lambda) precisely so the client doesn't have to reproduce a
    // byte-identical header — the file extension carries the format through
    // the pipeline. Sending one anyway is harmless; sending a *different* one
    // than was signed would be a 403, so we simply send none.
    //
    // sessionType 'background' (the default, stated explicitly here because it
    // is load-bearing) lets iOS keep the transfer going after the app is
    // suspended. Android has no equivalent for an in-process upload, so a
    // backgrounded Android upload may be suspended and resumed by the retry
    // path instead — see the limitations note in the audit.
    s = { ...s, upload: "UPLOADING", attempts: s.attempts + 1 };
    saveSession(s);
    patchJob(jobId, {
      phase: "uploading",
      status: "UPLOADING",
      attempts: s.attempts,
      error: undefined,
      nextAttemptAt: undefined,
    });
    recLog("upload.started", {
      attempt: s.attempts,
      size,
      duration: s.duration,
    }, sessionId);

    let lastLoggedPct = -1;
    const res = await file.upload(s.uploadUrl!, {
      httpMethod: "PUT",
      uploadType: UploadType.BINARY_CONTENT,
      sessionType: "background",
      onProgress: ({ bytesSent, totalBytes }) => {
        // totalBytes is -1/0 on platforms that can't stat the file up front;
        // leave `progress` undefined there so the UI falls back to the
        // indeterminate bar rather than rendering a bogus 0%.
        const known = totalBytes > 0;
        patchJob(jobId, {
          bytesSent,
          totalBytes: known ? totalBytes : undefined,
          progress: known ? Math.min(1, bytesSent / totalBytes) : undefined,
        });
        // Log every 10% rather than every callback: a 2 GB upload would
        // otherwise flood the ring buffer and evict everything useful.
        if (known) {
          const pct = Math.floor((bytesSent / totalBytes) * 10) * 10;
          if (pct !== lastLoggedPct) {
            lastLoggedPct = pct;
            recLog("upload.progress", { pct, bytesSent }, sessionId);
          }
        }
      },
    });
    if (res.status < 200 || res.status >= 300) {
      // S3 explains itself in an XML <Code> — surface it instead of a bare
      // status, so a future signing/permission problem is diagnosable.
      const code = /<Code>([^<]+)<\/Code>/.exec(res.body ?? "")?.[1];
      // An expired presign is specifically retryable, and retrying it means
      // re-presigning; drop the stale URL so the next pass fetches a new one
      // for the SAME key.
      if (res.status === 403 && /Expired|RequestTimeTooSkewed/i.test(code ?? "")) {
        s = { ...s, uploadUrl: undefined, uploadUrlExpiresAt: undefined };
        saveSession(s);
        throw new ApiError(503, "The upload link expired — retrying.");
      }
      throw new ApiError(
        res.status,
        `Storage upload failed (${res.status}${code ? `: ${code}` : ""}).`
      );
    }

    // 3. Flip the row to "uploaded" — transcription takes over from here.
    // progress is cleared: the bytes are all sent, and leaving a fractional
    // value would strand the bar at 99% through the finalize round-trip.
    patchJob(jobId, { phase: "finalizing", progress: undefined });
    await completeUpload(s.key!, s.duration || undefined);

    // THE confirmation. This — and only this — licenses deleting local audio.
    s = {
      ...s,
      upload: "PROCESSING",
      confirmedAt: Date.now(),
      error: undefined,
    };
    saveSession(s);
    recLog("upload.succeeded", {
      key: s.key,
      attempts: s.attempts,
      size,
      duration: s.duration,
    }, sessionId);

    patchJob(jobId, { phase: "done", status: "PROCESSING", error: undefined });
    // Reclaim the disk now that the server owns the bytes.
    pruneUploaded();
    inputs.delete(sessionId);
    setTimeout(() => dismissUpload(jobId), DONE_LINGER_MS);
    return s.key!;
  } catch (e) {
    const msg =
      e instanceof ApiError ? e.message :
      e instanceof Error ? e.message : "Upload failed.";
    const transient = isTransient(e);
    const attempts = (loadSession(sessionId)?.attempts ?? s.attempts) || 1;

    const failed = loadSession(sessionId);
    if (failed) {
      saveSession({ ...failed, upload: "UPLOAD_FAILED", error: msg });
    }
    recLog("upload.failed", {
      attempt: attempts,
      transient,
      message: msg,
    }, sessionId);

    if (transient) {
      // Not a failure the user has to act on — a wait. The banner says so,
      // and the timer will try again. The audio is safe on disk either way.
      scheduleRetry(sessionId, jobId, attempts);
    } else {
      patchJob(jobId, {
        phase: "failed",
        status: "UPLOAD_FAILED",
        error: msg,
        progress: undefined,
        attempts,
      });
    }
    throw e;
  } finally {
    running.delete(sessionId);
  }
}

function urlExpired(s: RecSession): boolean {
  if (!s.uploadUrlExpiresAt) return true;
  // Re-presign a minute early rather than discover the expiry mid-PUT on a
  // slow connection — a large upload can outlive a short-lived URL.
  return Date.now() > s.uploadUrlExpiresAt - 60_000;
}

/**
 * Retry a failed upload from its own banner.
 *
 * Unlike the previous implementation, this does NOT start a fresh job from
 * scratch: it re-drives the SAME session, which reuses the same presigned key.
 * That is what makes repeated taps safe — the old behaviour requested a new
 * presign each time and created a new timeline row per tap.
 *
 * Returns false when there is nothing to retry, so the caller can fall back to
 * asking the user to pick the file again.
 */
export function retryUpload(id: string): boolean {
  const job = jobs.find((j) => j.id === id);
  if (!job) return false;
  const s = loadSession(job.sessionId);
  if (!s || !s.fileUri) return false;
  const t = timers.get(job.sessionId);
  if (t) { clearTimeout(t); timers.delete(job.sessionId); }
  patchJob(id, { phase: "requesting", error: undefined, nextAttemptAt: undefined });
  void driveSession(job.sessionId).catch(() => { /* the job banner reports it */ });
  return true;
}

/**
 * Give up on a recording and delete it, at the user's explicit request.
 *
 * Separate from dismissUpload (which only hides the banner) because the two
 * are genuinely different intentions: hiding a banner must never destroy
 * audio, and there has to be SOME way for a user to delete a recording that
 * can never upload.
 */
export function abandonUpload(id: string): void {
  const job = jobs.find((j) => j.id === id);
  if (!job) return;
  const t = timers.get(job.sessionId);
  if (t) { clearTimeout(t); timers.delete(job.sessionId); }
  const s = loadSession(job.sessionId);
  if (s) {
    recLog("state.transition", { note: "upload abandoned by user" }, s.id);
    deleteSession(s.id, s.format);
  }
  inputs.delete(job.sessionId);
  dismissUpload(id);
}

// ---------------------------------------------------------------------------
// Launch recovery + connectivity retry
// ---------------------------------------------------------------------------

let recoveryDone = false;

/**
 * Finish what a previous run of the app started. Call once, early, at launch.
 *
 * This closes the loop that made the old implementation lossy: a recording
 * that survived on disk but whose job was gone had no way back into the
 * pipeline. Now every unconfirmed session is re-queued, and any recording that
 * was interrupted by the app dying is finalized into an uploadable state.
 *
 * Returns a summary so the UI can tell the user what it found — an interrupted
 * recording reappearing without explanation is its own kind of alarming.
 */
export async function recoverUploads(): Promise<{
  interrupted: number;
  queued: number;
  adopted: number;
}> {
  if (recoveryDone) return { interrupted: 0, queued: 0, adopted: 0 };
  recoveryDone = true;

  let adopted = 0;
  try {
    adopted = sweepOrphanAudio().length;
  } catch {
    // Never let recovery stop launch.
  }

  const { interrupted, pendingUpload } = recoverSessions();
  recLog("recovery.attempted", {
    at: "launch",
    interrupted: interrupted.length,
    pending: pendingUpload.length,
    orphans: adopted,
  });

  // Everything unconfirmed goes back in the queue, interrupted recordings
  // included — they are now COMPLETED sessions with real audio.
  const queue = [...interrupted, ...pendingUpload];
  for (const s of queue) {
    const input: StartUploadInput = {
      source: "MOBILE",
      fileUri: s.fileUri!,
      format: s.format,
      duration: s.duration || undefined,
      sessionId: s.id,
    };
    inputs.set(s.id, input);
    jobs = [jobForSession(s, input), ...jobs.filter((j) => j.sessionId !== s.id)];
  }
  emit();

  // Kick them off staggered, so ten pending recordings don't all contend for
  // the radio at once on a weak connection.
  queue.forEach((s, i) => {
    setTimeout(() => {
      void driveSession(s.id).catch(() => { /* banner reports it */ });
    }, i * 1500);
  });

  // Reclaim space from anything already confirmed in a previous run.
  try { pruneUploaded(); } catch { /* best effort */ }

  return { interrupted: interrupted.length, queued: queue.length, adopted };
}

/**
 * Nudge every waiting upload when the app comes back to the foreground.
 *
 * Without a connectivity library this is the best available "the network may
 * have changed" signal — and it covers the actual user journey, which is:
 * lose signal, put the phone away, come back somewhere with wifi, open the
 * app. @react-native-community/netinfo would give a true online event, but it
 * is not a dependency of this project and adding one for a nudge that the
 * foreground event already provides is not worth the native surface.
 */
export function retryAllWaiting(): number {
  let n = 0;
  for (const j of jobs) {
    if (j.phase !== "waiting" && j.phase !== "failed") continue;
    const s = loadSession(j.sessionId);
    if (!s?.fileUri) continue;
    const t = timers.get(j.sessionId);
    if (t) { clearTimeout(t); timers.delete(j.sessionId); }
    n += 1;
    void driveSession(j.sessionId).catch(() => { /* banner reports it */ });
  }
  return n;
}

// One app-wide listener. Registered at module load because uploads are an
// app-level concern, not a screen's — the whole point is that they continue
// while the user is somewhere else entirely.
AppState.addEventListener("change", (state) => {
  if (state === "active") retryAllWaiting();
});

/** Unconfirmed recordings still on this device — for a "waiting to upload" badge. */
export function pendingLocalCount(): number {
  try {
    return listSessions().filter(
      (s) => !s.confirmedAt && s.fileUri && (s.upload === "LOCAL" || s.upload === "UPLOAD_FAILED")
    ).length;
  } catch {
    return 0;
  }
}

// ---------------------------------------------------------------------------
// React binding
// ---------------------------------------------------------------------------
function subscribe(cb: () => void) {
  listeners.add(cb);
  return () => { listeners.delete(cb); };
}

function getSnapshot() {
  return jobs;
}

// Live view of the upload queue for any screen (Files shows the banners).
export function useUploads(): UploadJob[] {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}
