// lib/shared-data.ts — data more than one screen needs, read ONCE.
//
// WHY. Before this, every screen fetched for itself: a cold start asked the
// server for /me three times (the name gate, the workspace provider, the Desk
// header) and for the unread count twice, and the Desk and Profile each pulled
// the whole meetings list. These helpers put those reads behind the shared
// cache (lib/query-client.tsx): concurrent requests merge into one, and a
// fresh answer is reused instead of fetched again.
//
// RULES (docs/LOCAL_FIRST_AUDIT.md §3): AWS is the source of truth; the cache
// only holds what the server said. Every key carries the account (and, for
// lists, the workspace), and every sign-out wipes it.
//
// The fetch* functions use the queryClient singleton, so they work above the
// QueryProvider too (the root layout and NotificationsProvider sit there).
// The use* hooks are for screens below it.
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { useRouter } from "expo-router";
import {
  ApiError, clearToken, getActiveWorkspaceId, getAllTasks, getLinkConfig, getMe, getMeetingHighlights,
  getRecordings, getToken, getUnreadNotificationCount, getWebhookMeeting, updateMe,
  type ApiLinkConfig, type ApiTask, type MeetingHighlights, type RecordingSummary, type UserProfile,
} from "./api";
import { queryClient, useCacheScope } from "./query-client";
import {
  HIGHLIGHTS_FRESH_MS, LINK_CONFIG_FRESH_MS, WEBHOOK_STATE_FRESH_MS, highlightsKey, linkConfigKey,
  meKey, recordingsKey, unfilteredTasksKey, unreadCountKey, userIdFromToken, webhookMeetingKey,
  isAboutMeeting, patchMeetingInList, withoutMeeting,
  type CacheScope, type MeetingListPatch,
} from "./offline-cache-rules";

async function accountId(): Promise<string> {
  return userIdFromToken(await getToken());
}

/**
 * The signed-in user's profile. Reused while fresh (30 s), shared with any
 * request already in flight. ONE attempt, no retry: callers (the name gate
 * fails open) must see a failure promptly, not wait out a retry or an
 * offline pause.
 */
export async function fetchMe(): Promise<UserProfile> {
  return queryClient.fetchQuery({ queryKey: meKey(await accountId()), queryFn: getMe, retry: false });
}

/** The profile for a screen: the saved copy at once, refreshed when stale. */
export function useMe() {
  const scope = useCacheScope();
  return useQuery({
    queryKey: meKey(scope?.userId ?? ""),
    enabled: !!scope,
    queryFn: getMe,
  });
}

/** Save profile fields, then hand the server's answer to every screen. */
export async function saveMe(patch: { name?: string; avatar_url?: string }): Promise<UserProfile> {
  const user = await updateMe(patch);
  queryClient.setQueryData(meKey(await accountId()), user);
  return user;
}

/**
 * The unread notification count. Requests in flight are shared (the provider
 * and the bell ask together at start-up), but a finished answer is never
 * reused: marking notifications read changes it, and a cached number would
 * bring a read badge back.
 */
export async function fetchUnreadCount(): Promise<number> {
  return queryClient.fetchQuery({
    queryKey: unreadCountKey(await accountId()),
    queryFn: getUnreadNotificationCount,
    staleTime: 0,
    retry: false,
  });
}

/**
 * The meetings list for the current account and workspace. `whose` filters an
 * organisation's list by recorder ("" = the default list). The Desk and
 * Profile both read the default list, so they share one request and one
 * saved copy. A 401 signs out and returns to /login, as the Desk always did.
 */
export function useRecordings(whose: string) {
  const router = useRouter();
  const scope = useCacheScope();
  // STABLE across renders: screens put this key in useCallback/useFocusEffect
  // deps, and a fresh array every render would refetch on every render.
  const key = useMemo(() => (scope ? recordingsKey(scope, whose) : null), [scope, whose]);
  const query = useQuery<RecordingSummary[]>({
    queryKey: key ?? ["recordings", "no-account-yet"],
    enabled: !!key,
    meta: { persist: true },
    queryFn: async () => {
      try {
        // `whose` is only ever set on an organisation, and the server ignores
        // it on a personal workspace, so this is safe to pass unconditionally.
        return await getRecordings({ userId: whose });
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) { await clearToken(); router.replace("/login"); }
        throw e;
      }
    },
  });
  return { query, key };
}

/**
 * One meeting's highlights, remembered for a day (also when empty) and saved
 * on the phone. See highlightsKey for why: an empty answer used to cost one
 * AI call per meeting on every Calendar visit. A failure is not remembered,
 * so the next visit asks again.
 */
export async function fetchMeetingHighlights(audioKey: string): Promise<MeetingHighlights> {
  return queryClient.fetchQuery({
    queryKey: highlightsKey(await accountId(), audioKey),
    queryFn: async () => (await getMeetingHighlights(audioKey)).meeting_highlights,
    staleTime: HIGHLIGHTS_FRESH_MS,
    retry: false,
  });
}

/**
 * GET /tasks with no filter, `limit` rows. Requests in flight are shared and
 * nothing is reused afterwards (task lists change with every edit, and the
 * screens re-read on focus): the Tasks screen's list and its summary cards
 * asked for exactly this at the same moment.
 */
export async function fetchUnfilteredTasks(
  limit: number,
): Promise<{ tasks: ApiTask[]; next_cursor: string }> {
  // Scoped exactly as request() scopes the call: the token's account and the
  // active-workspace header. Resolved here rather than taken from a hook, so
  // a screen's first load does not wait for (or re-run on) the hook's scope.
  const scope: CacheScope = { userId: await accountId(), workspaceId: (await getActiveWorkspaceId()) || "" };
  return queryClient.fetchQuery({
    queryKey: unfilteredTasksKey(scope, limit),
    queryFn: () => getAllTasks({ limit }),
    staleTime: 0,
    retry: false,
  });
}

/** What the meeting's Salesforce block reads: the state, or "off" when the
 *  server says this person/workspace has no Salesforce section. */
export type WebhookMeetingRead =
  | { off: false; meeting: Awaited<ReturnType<typeof getWebhookMeeting>> }
  | { off: true };

/** The answers that mean "no Salesforce section here" rather than a failure. */
export function isWebhookOff(e: unknown): boolean {
  return e instanceof ApiError
    && (e.code === "webhook_disabled" || e.status === 404 || e.status === 403);
}

/**
 * A meeting's Salesforce state. Reused for 15 s, so switching tabs and back
 * does not re-read it; `force` (the delivery poll, after a push or a change)
 * always asks, still joining a request in flight. "Off" is remembered like
 * any answer, so a workspace without Salesforce is not asked on every visit.
 */
export async function fetchWebhookMeeting(
  meetingKey: string, opts: { force?: boolean } = {},
): Promise<WebhookMeetingRead> {
  return queryClient.fetchQuery<WebhookMeetingRead>({
    queryKey: webhookMeetingKey(await accountId(), meetingKey),
    queryFn: async () => {
      try {
        return { off: false, meeting: await getWebhookMeeting(meetingKey) };
      } catch (e) {
        if (isWebhookOff(e)) return { off: true };
        throw e;
      }
    },
    staleTime: opts.force ? 0 : WEBHOOK_STATE_FRESH_MS,
    retry: false,
  });
}

/** A workspace's Salesforce linking setup, reused for 5 minutes; `force` is
 *  the block's "Try again". */
export async function fetchLinkConfig(
  workspaceId: string, opts: { force?: boolean } = {},
): Promise<ApiLinkConfig> {
  return queryClient.fetchQuery({
    queryKey: linkConfigKey(await accountId(), workspaceId),
    queryFn: () => getLinkConfig(workspaceId),
    staleTime: opts.force ? 0 : LINK_CONFIG_FRESH_MS,
    retry: false,
  });
}

// ---------------------------------------------------------------------------
// AFTER A CHANGE — call these only once the server has confirmed it. They
// keep every saved copy of a meeting in step, which is what lets the Desk
// trust a fresh copy instead of re-reading the list on every visit.
// ---------------------------------------------------------------------------

/** A confirmed change to a meeting's title or status: every meetings list
 *  and the saved meeting copy show it at once. */
export function applyMeetingChange(audioKey: string, patch: MeetingListPatch): void {
  queryClient.setQueriesData<RecordingSummary[]>(
    { queryKey: ["recordings"] }, (list) => patchMeetingInList(list, audioKey, patch));
  queryClient.setQueriesData<{ rec: RecordingSummary }>(
    { queryKey: ["meeting"], predicate: (q) => isAboutMeeting(q.queryKey, audioKey) },
    (saved) => (saved ? { ...saved, rec: { ...saved.rec, ...patch } } : saved));
}

/** A confirmed move to Trash: gone from every meetings list. */
export function dropMeetingFromLists(audioKey: string): void {
  queryClient.setQueriesData<RecordingSummary[]>(
    { queryKey: ["recordings"] }, (list) => withoutMeeting(list, audioKey));
}

/** A confirmed PERMANENT delete: every saved answer about the meeting leaves
 *  the phone too (its detail, highlights, Salesforce state). */
export function forgetMeeting(audioKey: string): void {
  dropMeetingFromLists(audioKey);
  queryClient.removeQueries({ predicate: (q) => isAboutMeeting(q.queryKey, audioKey) });
}

/** The meeting's highlights are about to change (it is being reprocessed):
 *  drop the saved ones so the Calendar asks again. */
export function forgetMeetingHighlights(audioKey: string): void {
  queryClient.removeQueries({ queryKey: ["highlights"], predicate: (q) => isAboutMeeting(q.queryKey, audioKey) });
}

/** Something changed that a list cannot patch locally (a restore brings back
 *  a row; a finished brief changes several fields): mark every meetings list
 *  stale. A screen showing one reloads it now; the rest on next view. */
export function refreshMeetingLists(): Promise<void> {
  return queryClient.invalidateQueries({ queryKey: ["recordings"] });
}
