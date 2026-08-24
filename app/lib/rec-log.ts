// lib/rec-log.ts — diagnostic ring buffer for the recording + upload pipeline.
//
// Recording bugs are almost never reproducible on demand: they happen when a
// call lands at the wrong moment, when the OS reclaims the mic, when the app
// is killed in the background. By the time the user reports it, the console is
// long gone. So every state transition writes a structured line HERE, in a
// bounded in-memory ring, and the last N lines travel with the recording as
// part of its session record (lib/rec-store.ts persists them).
//
// PRIVACY — this log carries pipeline facts ONLY: state names, reasons,
// durations, byte counts, file URIs. It never carries audio, transcript text,
// meeting content, titles, or auth tokens. Anything user-authored is
// deliberately absent; `note` fields are written by us, never by the user.
// That's what makes it safe to persist to disk and to attach to a support
// report.

// The vocabulary. A closed union rather than free-form strings so a typo can't
// silently create an event nobody is looking for, and so the diagnostics screen
// can filter by kind.
export type RecEvent =
  | "session.created"
  | "recording.started"
  | "recording.paused"
  | "recording.resumed"
  | "recording.stopped"
  | "recording.error"
  | "file.created"
  | "file.finalized"
  | "file.missing"
  | "file.relocated"
  | "route.changed"
  // Android audio-focus change. The primary interruption signal there: a call
  // raises AUDIOFOCUS_LOSS_TRANSIENT, and focus GAIN is the all-clear that
  // gates auto-resume. See lib/audio-focus.ts.
  | "audio.focus"
  | "mic.interrupted"
  | "mic.unavailable"
  | "mic.permission"
  | "call.interrupted"
  // A ring was detected and recording deliberately CONTINUED; then either the
  // call was answered (call.answered -> we pause) or it went away without
  // being answered (call.dismissed -> we never paused). Logged separately from
  // call.interrupted so a support trace shows which of the three a given ring
  // actually was. See watchRingingCall in lib/rec-controller.ts.
  | "call.answered"
  | "call.dismissed"
  // Ringer silenced for the duration of a recording, and restored after, so an
  // incoming call's ringtone/vibration is not captured. Includes the refusal
  // case (no Do Not Disturb access), which is not an error.
  | "ringer.silenced"
  | "ringer.restored"
  | "interruption.began"
  | "interruption.ended"
  | "storage.low"
  | "upload.started"
  | "upload.progress"
  | "upload.failed"
  | "upload.succeeded"
  | "upload.deferred"
  | "recovery.attempted"
  | "recovery.result"
  | "state.transition";

export type RecLogLine = {
  /** Epoch millis. Absolute so lines from different sessions can be merged. */
  t: number;
  event: RecEvent;
  /** The recording session this line belongs to, when there is one. */
  sessionId?: string;
  /** Structured, non-sensitive detail. See the privacy note above. */
  data?: Record<string, unknown>;
};

// Bounded so a 6-hour recording can't grow the log without limit. At one line
// per state change plus a throttled progress line, 500 covers a very long
// session many times over; the oldest lines are the least interesting.
const MAX_LINES = 500;

let lines: RecLogLine[] = [];
const listeners = new Set<() => void>();

function emit() {
  for (const l of listeners) l();
}

/**
 * Record one pipeline event.
 *
 * Never throws: a logging failure must not be able to break a recording, so
 * the whole body is defensive. Callers treat this as fire-and-forget.
 */
export function recLog(
  event: RecEvent,
  data?: Record<string, unknown>,
  sessionId?: string
): void {
  try {
    const line: RecLogLine = { t: Date.now(), event, sessionId, data };
    lines = lines.length >= MAX_LINES
      ? [...lines.slice(lines.length - MAX_LINES + 1), line]
      : [...lines, line];
    if (__DEV__) {
      // One grep-able prefix so a device log can be filtered to just this
      // pipeline: `adb logcat | grep '\[rec\]'`.
      console.log(
        `[rec] ${event}${sessionId ? ` (${sessionId.slice(0, 8)})` : ""}`,
        data ?? ""
      );
    }
    emit();
  } catch {
    // Deliberately silent — see above.
  }
}

/** The whole buffer, oldest first. For the diagnostics view / a support dump. */
export function getRecLog(): RecLogLine[] {
  return lines;
}

/** Just this session's lines — what gets persisted alongside the session. */
export function getSessionLog(sessionId: string): RecLogLine[] {
  return lines.filter((l) => l.sessionId === sessionId);
}

/** Render the log as plain text, for sharing in a support report. */
export function formatRecLog(subset: RecLogLine[] = lines): string {
  return subset
    .map((l) => {
      const ts = new Date(l.t).toISOString();
      const sid = l.sessionId ? ` [${l.sessionId.slice(0, 8)}]` : "";
      const d = l.data && Object.keys(l.data).length
        ? ` ${JSON.stringify(l.data)}`
        : "";
      return `${ts}${sid} ${l.event}${d}`;
    })
    .join("\n");
}

export function subscribeRecLog(cb: () => void): () => void {
  listeners.add(cb);
  return () => { listeners.delete(cb); };
}

/** Test/support hook — drops every buffered line. */
export function clearRecLog(): void {
  lines = [];
  emit();
}
