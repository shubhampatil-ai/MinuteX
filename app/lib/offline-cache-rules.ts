// lib/offline-cache-rules.ts — the rules of the on-device cache (local-first,
// phase 1), kept free of React Native imports so node tests can pin them.
//
// What the cache is: the last good answer to a request, kept on the phone so
// a screen paints instantly and still shows something offline; the server is
// asked again in the background. What it is NOT: a source of truth. Every
// write still goes to the server first.
//
// Who may see it: cached data belongs to ONE account in ONE workspace. Every
// key starts with both, and signing out wipes the whole cache (see
// query-client.tsx), so the next person on this phone never sees it.

/** Bump to discard every cache written by an older app version. */
export const CACHE_BUSTER = "minutex-cache-v1";

/** How long a cached answer may be shown at all (then it is dropped). */
export const CACHE_MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;

/** How long an answer counts as fresh: no background refresh before this. */
export const FRESH_FOR_MS = 30 * 1000;

/** The storage key the persisted cache lives under. */
export const CACHE_STORAGE_KEY = "minutex.query-cache";

/** The account a token belongs to: the JWT's `sub`, or "" for anything
 *  that isn't a readable token. Used ONLY to partition the cache — never
 *  as proof of identity (the server checks the token itself). */
export function userIdFromToken(token: string | null | undefined): string {
  const part = String(token || "").split(".")[1];
  if (!part) return "";
  try {
    const b64 = part.replace(/-/g, "+").replace(/_/g, "/");
    const json = atob(b64 + "=".repeat((4 - (b64.length % 4)) % 4));
    const sub = JSON.parse(json)?.sub;
    return typeof sub === "string" ? sub : "";
  } catch {
    return "";
  }
}

/** Whose data a key holds. "" workspace = the personal workspace. */
export type CacheScope = { userId: string; workspaceId: string };

/** The Desk's meetings list. `whose` is the org filter ("" = mine/all). */
export function recordingsKey(scope: CacheScope, whose: string) {
  return ["recordings", scope.userId, scope.workspaceId || "personal", whose || ""] as const;
}

/** Retry a failed request? Only for failures that might go away: never for
 *  4xx (the answer will not change), except timeouts and rate limits. At
 *  most twice — the cached data stays on screen meanwhile. */
export function shouldRetry(failureCount: number, error: unknown): boolean {
  const status = Number((error as { status?: unknown })?.status) || 0;
  if (status >= 400 && status < 500 && status !== 408 && status !== 429) return false;
  return failureCount < 2;
}

/** 1s, 2s, 4s… capped at 15s. A throttled server (503/429) gets breathing
 *  room instead of an immediate second request. */
export function retryDelay(attempt: number): number {
  return Math.min(1000 * 2 ** attempt, 15_000);
}

/** Which cached answers are written to the phone: these roots, or a query
 *  that opts in with meta.persist — and only successful answers. Root-based
 *  because answers written from code (setQueryData) carry no meta. */
export const PERSISTED_ROOTS = new Set(["recordings", "meeting", "me", "highlights"]);

/** The signed-in user's profile (/me). Per account, not per workspace: the
 *  profile is the same in every workspace. Saved, so Profile works offline. */
export function meKey(userId: string) {
  return ["me", userId || "unknown"] as const;
}

/** One meeting's highlights (the dates it mentioned, for the Calendar). Per
 *  account and meeting; saved, and reused for HIGHLIGHTS_FRESH_MS.
 *
 *  WHY SO LONG. POST /recordings/ai/highlights generates with AI when nothing
 *  is stored, and does NOT store an empty result — so a meeting with no
 *  deadlines used to cost one AI call on every Calendar visit. Remembering
 *  the answer here (empty included) bounds that to once a day per phone. */
export function highlightsKey(userId: string, audioKey: string) {
  return ["highlights", userId || "unknown", audioKey] as const;
}

export const HIGHLIGHTS_FRESH_MS = 24 * 60 * 60 * 1000;

/** Only processed meetings have highlights to read; asking for an uploading
 *  or failed one is a wasted request (or a pointless AI call). */
export function hasReadableHighlights(status: string | null | undefined): boolean {
  return status === "complete" || status === "transcribed";
}

/** The Tasks screen's unfiltered read, shared by its list and its summary
 *  cards (they asked for the same thing at two page sizes). In flight only. */
export function unfilteredTasksKey(scope: CacheScope, limit: number) {
  return ["tasks-unfiltered", scope.userId, scope.workspaceId || "personal", limit] as const;
}

/** A meeting's Salesforce state (the block on its Overview tab). Memory only.
 *  The block is unmounted on every tab switch, so without this each return
 *  to Overview re-read it; reused for WEBHOOK_STATE_FRESH_MS, and always
 *  re-read while a delivery is in flight or after the user changes it. */
export function webhookMeetingKey(userId: string, meetingKey: string) {
  return ["webhook-meeting", userId || "unknown", meetingKey] as const;
}

export const WEBHOOK_STATE_FRESH_MS = 15 * 1000;

/** A workspace's Salesforce linking setup. Changes only when an admin edits
 *  the receiver, so it is reused for LINK_CONFIG_FRESH_MS. Memory only. */
export function linkConfigKey(userId: string, workspaceId: string) {
  return ["link-config", userId || "unknown", workspaceId] as const;
}

export const LINK_CONFIG_FRESH_MS = 5 * 60 * 1000;

// ---------------------------------------------------------------------------
// AFTER A CHANGE. Applied only once the server has confirmed it (AWS is the
// source of truth; nothing here guesses), so every saved copy of a meeting —
// each meetings list, the saved meeting — says what the server now says, and
// screens can trust a copy that is still fresh instead of re-reading it.
// ---------------------------------------------------------------------------

/** The meeting fields a confirmed change may set in the saved copies: the
 *  ones the Desk list shows. Anything else is left to the next read. */
export type MeetingListPatch = { title?: string; status?: string };

/** `list` with one meeting's fields replaced; the same array (no re-render)
 *  when that meeting is not in it. */
export function patchMeetingInList<T extends { audio_s3_key: string }>(
  list: T[] | undefined, audioKey: string, patch: MeetingListPatch,
): T[] | undefined {
  if (!list || !list.some((r) => r.audio_s3_key === audioKey)) return list;
  return list.map((r) => (r.audio_s3_key === audioKey ? { ...r, ...patch } : r));
}

/** `list` without one meeting; the same array when it is not in it. */
export function withoutMeeting<T extends { audio_s3_key: string }>(
  list: T[] | undefined, audioKey: string,
): T[] | undefined {
  if (!list || !list.some((r) => r.audio_s3_key === audioKey)) return list;
  return list.filter((r) => r.audio_s3_key !== audioKey);
}

/** The per-meeting cache roots (each key ends with the meeting's key). */
export const PER_MEETING_ROOTS = new Set(["meeting", "highlights", "webhook-meeting"]);

/** Is this cached answer about that one meeting? (Permanent delete removes
 *  every such answer from the phone.) */
export function isAboutMeeting(queryKey: readonly unknown[], audioKey: string): boolean {
  return PER_MEETING_ROOTS.has(String(queryKey[0] ?? "")) && queryKey[queryKey.length - 1] === audioKey;
}

/** The notification badge. Per account (the server counts per user). NOT
 *  saved and never served from cache: only requests in flight are shared, so
 *  the badge cannot show a number the user has already read away. */
export function unreadCountKey(userId: string) {
  return ["unread-count", userId || "unknown"] as const;
}

export function shouldPersist(query: {
  queryKey?: readonly unknown[]; meta?: Record<string, unknown>; state: { status: string };
}): boolean {
  if (query.state.status !== "success") return false;
  return query.meta?.persist === true || PERSISTED_ROOTS.has(String(query.queryKey?.[0] ?? ""));
}

// ---------------------------------------------------------------------------
// STORAGE BUDGET. The whole saved cache is ONE AsyncStorage value. Android
// fails a single value over ~2 MB (and a whole store over ~6 MB) — silently,
// behind the storage wrapper — and then NOTHING is saved. Measured 2026-10-12:
// the heaviest account's 20 saved meetings are ~0.2 MB, but a meeting row can
// reach 400 KB, so the write is capped instead of trusted.
// ---------------------------------------------------------------------------

/** The most the saved cache may hold, in characters. 1 M characters stays
 *  under Android's 2 MB per-value limit even at 2 bytes per character. */
export const PERSIST_BUDGET_CHARS = 1_000_000;

/** One saved answer larger than this is not kept (except the always-kept
 *  ones): one huge meeting must not push out twenty normal ones. */
export const PERSIST_MAX_ENTRY_CHARS = 256_000;

/** Kept whatever their size: who you are, and the meetings lists — without
 *  them an offline launch shows nothing at all. */
export const ALWAYS_KEEP_ROOTS = new Set(["me", "recordings"]);

type SavedQuery = { queryKey: readonly unknown[]; state: { dataUpdatedAt?: number } };

/**
 * The saved cache cut down to `budget` characters: the always-kept roots
 * first, then everything else newest first, skipping any single answer over
 * `entryMax`. Returns the client to save and how many answers were left out.
 */
export function fitToBudget<C extends { clientState: { queries: SavedQuery[] } }>(
  client: C, budget = PERSIST_BUDGET_CHARS, entryMax = PERSIST_MAX_ENTRY_CHARS,
): { client: C; dropped: number } {
  const all = client.clientState.queries.map((q) => ({ q, size: JSON.stringify(q).length }));
  const keep = (q: SavedQuery) => ALWAYS_KEEP_ROOTS.has(String(q.queryKey?.[0] ?? ""));
  const kept: SavedQuery[] = [];
  let total = 0;
  for (const e of all) if (keep(e.q)) { kept.push(e.q); total += e.size; }
  const rest = all.filter((e) => !keep(e.q))
    .sort((a, b) => (b.q.state.dataUpdatedAt ?? 0) - (a.q.state.dataUpdatedAt ?? 0));
  for (const e of rest) {
    if (e.size > entryMax || total + e.size > budget) continue;
    kept.push(e.q);
    total += e.size;
  }
  const dropped = all.length - kept.length;
  if (!dropped) return { client, dropped };
  return { client: { ...client, clientState: { ...client.clientState, queries: kept } }, dropped };
}

/** One meeting's saved copy (meeting detail + its tasks). */
export function meetingKey(scope: CacheScope, key: string) {
  return ["meeting", scope.userId, scope.workspaceId || "personal", key] as const;
}

/** How many meetings one account keeps on the phone (most recent first). */
export const MAX_SAVED_MEETINGS = 20;

/** The copy of a meeting that may be kept on the phone. The TRANSCRIPT is
 *  left out (it can be hundreds of thousands of characters, and the phone's
 *  store is small), as are the audio link (it expires) and the assistant
 *  chat. `transcript_withheld` records that one existed, so the screen still
 *  shows its tabs and the Transcript tab says it needs a connection. */
export function offlineMeetingCopy<T extends {
  transcript?: string | null; timestamps?: unknown[] | null;
  audio_url?: string | null; chat_history?: unknown[] | null;
}>(detail: T): T & { transcript_withheld: boolean } {
  return {
    ...detail,
    transcript: "",
    timestamps: [],
    audio_url: null,
    chat_history: null,
    transcript_withheld: !!detail.transcript,
  };
}

/** The saved meetings to drop so at most MAX_SAVED_MEETINGS remain, oldest
 *  first out. */
export function meetingsToEvict<Q extends { dataUpdatedAt: number }>(saved: Q[]): Q[] {
  return [...saved].sort((a, b) => b.dataUpdatedAt - a.dataUpdatedAt).slice(MAX_SAVED_MEETINGS);
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** When a saved answer was fetched, in the phone's local time:
 *  "saved 8 Oct, 10:42" ("" when unknown). No clock needed, so it is safe
 *  to compute during render. */
export function savedAtLabel(updatedAt: number): string {
  if (!updatedAt) return "";
  const d = new Date(updatedAt);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");
  return `saved ${d.getDate()} ${MONTHS[d.getMonth()]}, ${hh}:${mm}`;
}
