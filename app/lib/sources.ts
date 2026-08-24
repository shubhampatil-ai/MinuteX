// lib/sources.ts — presentation vocabulary for recording sources & statuses.
// One place maps the backend enums (source: DEVICE|MOBILE|UPLOAD, status:
// uploading -> ... -> complete) to icons, labels and pill colors, so the
// timeline and the detail screen can never drift apart. Pure data + pure
// functions — no React, no network.
import type { IconName } from "./icons";
import type { RecordingSource, RecordingSummary } from "./api";

// ---------------------------------------------------------------------------
// Speakers — diarization labels -> human names.
//
// The transcript stores anonymous diarization labels ("0", "1", "agent"): the
// audio has no names in it. `speaker_names` on the recording maps those to
// people the user named. Resolution lives here, not in a screen, because the
// same mapping has to reach the transcript, the who-spoke bar, the participant
// list and action-item owners — four places that must never disagree.
// ---------------------------------------------------------------------------

/** Display name for a diarization label: the user's name, else "Speaker 0". */
export function speakerName(
  label: string,
  names?: Record<string, string> | null
): string {
  const key = normalizeSpeakerLabel(label);
  const given = names?.[key]?.trim();
  if (given) return given;
  // Bare numeric labels read as "Speaker 0"; named ids ("agent") stand alone.
  return /^\d+$/.test(key) ? `Speaker ${key}` : key || "Speaker";
}

/**
 * Reduce whatever a speaker is called back to its raw diarization label.
 *
 * Needed because the two producers disagree: `timestamps[].speaker` holds the
 * raw label ("0"), while Groq echoes the prompt's display form back in
 * `participants[].speaker` ("Speaker 0"). Both must key into the same map, or
 * naming someone in the transcript would leave the participants list stale.
 */
export function normalizeSpeakerLabel(label: string): string {
  const s = (label ?? "").trim();
  const m = /^speaker[\s_-]*(.+)$/i.exec(s);
  return (m ? m[1] : s).trim();
}

// ---------------------------------------------------------------------------
// Source — icon + label per recording source.
// ---------------------------------------------------------------------------
export const SOURCE_META: Record<RecordingSource, { icon: IconName; label: string }> = {
  DEVICE: { icon: "waveform", label: "MinuteX Device" },
  MOBILE: { icon: "iphone", label: "Phone" },
  UPLOAD: { icon: "folder", label: "Uploaded File" },
};

// The backend derives `source` for legacy rows, but rows fetched by an old
// backend build won't have it — derive the same way from the S3 key here so
// the UI is correct against either backend version.
export function deriveSource(
  r: Pick<RecordingSummary, "source" | "audio_s3_key">
): RecordingSource {
  if (r.source && SOURCE_META[r.source]) return r.source;
  const parts = (r.audio_s3_key || "").split("/");
  if (parts.length === 4 && parts[0] === "recordings") {
    if (parts[2] === "mobile") return "MOBILE";
    if (parts[2] === "uploads") return "UPLOAD";
  }
  return "DEVICE";
}

export function sourceMeta(r: Pick<RecordingSummary, "source" | "audio_s3_key">) {
  return SOURCE_META[deriveSource(r)];
}

// ---------------------------------------------------------------------------
// Status — the processing lifecycle, collapsed into three UI kinds.
// "ready" renders quiet (a check), "processing" renders a warn pill with the
// step name, "failed" renders a danger pill.
// ---------------------------------------------------------------------------
export type StatusKind = "ready" | "processing" | "failed";

const STATUS_LABELS: Record<string, { label: string; kind: StatusKind }> = {
  uploading: { label: "Uploading", kind: "processing" },
  uploaded: { label: "Queued", kind: "processing" },
  transcribing: { label: "Transcribing", kind: "processing" },
  generating_ai: { label: "Generating AI", kind: "processing" },
  complete: { label: "Completed", kind: "ready" },
  // Legacy: transcript stored, AI analysis failed — still viewable.
  transcribed: { label: "Completed", kind: "ready" },
  failed: { label: "Failed", kind: "failed" },
};

export function statusMeta(status: string | undefined | null): { label: string; kind: StatusKind } {
  // Unknown/blank statuses come from rows written before the lifecycle
  // existed — those were only ever written once fully processed.
  return STATUS_LABELS[status || ""] ?? { label: "Completed", kind: "ready" };
}

export function isReady(status: string | undefined | null): boolean {
  return statusMeta(status).kind === "ready";
}

// ---------------------------------------------------------------------------
// Duration — DynamoDB Decimals serialize as strings; render "m:ss" / "h:mm:ss".
// ---------------------------------------------------------------------------
export function durationSeconds(d: number | string | null | undefined): number | null {
  if (d == null || d === "") return null;
  const n = Number(d);
  return Number.isFinite(n) && n > 0 ? n : null;
}

export function fmtDuration(d: number | string | null | undefined): string {
  const total = durationSeconds(d);
  if (total == null) return "";
  const sec = Math.round(total);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
}

// ---------------------------------------------------------------------------
// Devices
// ---------------------------------------------------------------------------

/** Relative "last seen" for a device.
 *
 * Devices.last_seen is a unix INT when the device presign touches it but an
 * ISO string from older writes, so accept both — a bare `new Date(value)` on
 * the int would render 1970.
 */
export function lastSeenLabel(v: string | number | null | undefined): string {
  if (v == null || v === "") return "Never";
  const d = typeof v === "number" ? new Date(v * 1000) : new Date(v);
  if (isNaN(d.getTime())) return String(v);
  const mins = Math.round((Date.now() - d.getTime()) / 60000);
  if (mins < 1) return "Just now";
  if (mins < 60) return `${mins} min ago`;
  if (mins < 60 * 24) return `${Math.round(mins / 60)} h ago`;
  return d.toLocaleDateString();
}
