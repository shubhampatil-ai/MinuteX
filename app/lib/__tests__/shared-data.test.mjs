// lib/__tests__/shared-data.test.mjs — data more than one screen needs is
// read ONCE (local-first P1, step 1).
//
// Run:  node --test lib/__tests__/shared-data.test.mjs
//
// Two kinds of check:
//   * BEHAVIOUR, on the real TanStack engine (@tanstack/query-core is plain
//     JS) configured with the app's own defaults — so "three /me requests
//     become one" is measured, not assumed.
//   * WIRING, from the source, for the React Native files node cannot load.
import { describe, it, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { QueryClient, QueryObserver, onlineManager } from "@tanstack/query-core";
import {
  CACHE_MAX_AGE_MS, FRESH_FOR_MS, HIGHLIGHTS_FRESH_MS, PERSISTED_ROOTS, hasReadableHighlights,
  highlightsKey, meKey, recordingsKey, retryDelay, shouldPersist, shouldRetry,
  unfilteredTasksKey, unreadCountKey, LINK_CONFIG_FRESH_MS, WEBHOOK_STATE_FRESH_MS,
  linkConfigKey, webhookMeetingKey, isAboutMeeting, patchMeetingInList, withoutMeeting,
} from "../offline-cache-rules.ts";
import { mapLimit } from "../request-guard.ts";

const ROOT = new URL("../../", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const read = (p) => readFileSync(join(ROOT, p), "utf8");

/** A client with the SAME defaults as lib/query-client.tsx. */
function appClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: FRESH_FOR_MS, gcTime: CACHE_MAX_AGE_MS, retry: shouldRetry, retryDelay,
        networkMode: "offlineFirst", refetchOnWindowFocus: false,
      },
    },
  });
}

/** A fake endpoint: counts calls, answers after `ms`. */
function endpoint(value, ms = 20) {
  const fn = async () => { fn.calls += 1; await new Promise((r) => setTimeout(r, ms)); return value(); };
  fn.calls = 0;
  return fn;
}

describe("cache keys", () => {
  it("/me is per account; the badge is per account", () => {
    assert.deepEqual(meKey("u-1"), ["me", "u-1"]);
    assert.deepEqual(unreadCountKey("u-1"), ["unread-count", "u-1"]);
    assert.notDeepEqual(meKey("u-1"), meKey("u-2"));
  });

  it("an unreadable account never shares a key with a real one", () => {
    assert.deepEqual(meKey(""), ["me", "unknown"]);
    assert.notDeepEqual(meKey(""), meKey("u-1"));
  });

  it("the profile is saved to the phone; the badge never is", () => {
    assert.ok(PERSISTED_ROOTS.has("me"));
    assert.ok(!PERSISTED_ROOTS.has("unread-count"));
    const ok = { status: "success" };
    assert.equal(shouldPersist({ queryKey: meKey("u-1"), state: ok }), true);
    assert.equal(shouldPersist({ queryKey: unreadCountKey("u-1"), state: ok }), false);
  });
});

describe("behaviour on the real query engine, with the app's defaults", () => {
  let client;
  beforeEach(() => { client = appClient(); onlineManager.setOnline(true); });
  afterEach(() => { client.clear(); onlineManager.setOnline(true); });

  it("start-up: the name gate, the Desk header and Profile asking together cost ONE /me", async () => {
    const getMe = endpoint(() => ({ user_id: "u-1", name: "A" }));
    const key = meKey("u-1");
    // The Desk header subscribes (useMe) while the gate and Profile fetch.
    const observer = new QueryObserver(client, { queryKey: key, queryFn: getMe });
    const unsub = observer.subscribe(() => {});
    const [a, b] = await Promise.all([
      client.fetchQuery({ queryKey: key, queryFn: getMe, retry: false }),
      client.fetchQuery({ queryKey: key, queryFn: getMe, retry: false }),
    ]);
    unsub();
    assert.equal(getMe.calls, 1);
    assert.equal(a.user_id, "u-1");
    assert.equal(b, a);
  });

  it("a fresh /me is reused: opening Profile within 30 s costs nothing", async () => {
    const getMe = endpoint(() => ({ user_id: "u-1" }));
    await client.fetchQuery({ queryKey: meKey("u-1"), queryFn: getMe, retry: false });
    await client.fetchQuery({ queryKey: meKey("u-1"), queryFn: getMe, retry: false });
    assert.equal(getMe.calls, 1);
  });

  it("a saved profile edit is what every screen reads next", async () => {
    const getMe = endpoint(() => ({ user_id: "u-1", name: "Old" }));
    await client.fetchQuery({ queryKey: meKey("u-1"), queryFn: getMe, retry: false });
    client.setQueryData(meKey("u-1"), { user_id: "u-1", name: "New" });   // saveMe
    const me = await client.fetchQuery({ queryKey: meKey("u-1"), queryFn: getMe, retry: false });
    assert.equal(me.name, "New");
    assert.equal(getMe.calls, 1);
  });

  it("the badge: requests in flight are shared, but a finished answer is never reused", async () => {
    let n = 5;
    const count = endpoint(() => n);
    const opts = { queryKey: unreadCountKey("u-1"), queryFn: count, staleTime: 0, retry: false };
    const [x, y] = await Promise.all([client.fetchQuery(opts), client.fetchQuery(opts)]);
    assert.equal(count.calls, 1, "the provider and the bell share one request");
    assert.equal(x, 5); assert.equal(y, 5);
    n = 0;   // the user read everything
    assert.equal(await client.fetchQuery(opts), 0, "no stale badge");
    assert.equal(count.calls, 2);
  });

  it("offline, a one-shot read fails at once instead of waiting to be back online", async () => {
    onlineManager.setOnline(false);
    const getMe = async () => { throw Object.assign(new Error("Network error"), { status: 0 }); };
    const started = Date.now();
    await assert.rejects(client.fetchQuery({ queryKey: meKey("u-1"), queryFn: getMe, retry: false }));
    assert.ok(Date.now() - started < 1000, "the name gate fails open promptly");
  });

  it("two accounts never share a cached profile", async () => {
    const one = endpoint(() => ({ user_id: "u-1" }));
    const two = endpoint(() => ({ user_id: "u-2" }));
    await client.fetchQuery({ queryKey: meKey("u-1"), queryFn: one, retry: false });
    const other = await client.fetchQuery({ queryKey: meKey("u-2"), queryFn: two, retry: false });
    assert.equal(other.user_id, "u-2");
    assert.equal(two.calls, 1);
  });

  it("the Desk and Profile share one meetings list", async () => {
    const getRecordings = endpoint(() => [{ audio_s3_key: "k1" }]);
    const key = recordingsKey({ userId: "u-1", workspaceId: "" }, "");
    const desk = new QueryObserver(client, { queryKey: key, queryFn: getRecordings });
    const profile = new QueryObserver(client, { queryKey: recordingsKey({ userId: "u-1", workspaceId: "" }, ""), queryFn: getRecordings });
    const u1 = desk.subscribe(() => {}); const u2 = profile.subscribe(() => {});
    await new Promise((r) => setTimeout(r, 60));
    u1(); u2();
    assert.equal(getRecordings.calls, 1);
  });
});

describe("wiring: who reads what", () => {
  const shared = read("lib/shared-data.ts");

  it("fetchMe is one-shot (no retry, no offline wait) and shared", () => {
    assert.match(shared, /fetchQuery\(\{ queryKey: meKey\(await accountId\(\)\), queryFn: getMe, retry: false \}\)/);
  });

  it("saveMe writes the server's answer into the shared profile", () => {
    const fn = shared.slice(shared.indexOf("export async function saveMe"));
    assert.ok(fn.indexOf("await updateMe(patch)") < fn.indexOf("setQueryData(meKey("));
  });

  it("the unread count is shared in flight only (staleTime 0) and one-shot", () => {
    const fn = shared.slice(shared.indexOf("export async function fetchUnreadCount"));
    assert.match(fn.slice(0, 400), /staleTime: 0,\s*retry: false,/);
  });

  it("the meetings-list key is stable across renders (no refetch-every-render loop)", () => {
    assert.match(shared, /const key = useMemo\(\(\) => \(scope \? recordingsKey\(scope, whose\) : null\), \[scope, whose\]\);/);
  });

  it("start-up reads /me through the shared copy: name gate, Desk, onboarding, Profile", () => {
    const layout = read("src/app/_layout.tsx");
    assert.match(layout, /const me = await fetchMe\(\);/);
    assert.doesNotMatch(layout, /\bgetMe\(/);
    const desk = read("src/app/(tabs)/index.tsx");
    assert.match(desk, /const me = useMe\(\)\.data \?\? null;/);
    assert.doesNotMatch(desk, /\bgetMe\b/);
    const onboarding = read("src/app/onboarding/name.tsx");
    assert.match(onboarding, /await fetchMe\(\)/);
    assert.doesNotMatch(onboarding, /\b(getMe|updateMe)\(/);
    const profile = read("src/app/(tabs)/profile.tsx");
    assert.match(profile, /await fetchMe\(\)/);
    assert.doesNotMatch(profile, /\b(getMe|updateMe|getRecordings)\(/);
    assert.match(profile, /useRecordings\(""\)/);
  });

  it("the workspace provider takes the user id from the token, /me only as a fallback", () => {
    const ws = read("lib/workspace-context.tsx");
    const fn = ws.slice(ws.indexOf("async function resolveUserId"));
    assert.ok(fn.indexOf("userIdFromToken(") < fn.indexOf("getMe()"));
    assert.equal((ws.match(/\bgetMe\(\)/g) || []).length, 1, "only the fallback");
  });

  it("no other file calls getMe/updateMe directly (the contact picker moves in its own step)", () => {
    const allowed = new Set([
      "lib/api.ts", "lib/shared-data.ts", "lib/workspace-context.tsx",
      "lib/contact-picker.tsx",
    ]);
    const files = [];
    const walk = (d) => {
      for (const e of readdirSync(d)) {
        const full = join(d, e);
        if (statSync(full).isDirectory()) { if (e !== "__tests__") walk(full); }
        else if (/\.(ts|tsx)$/.test(e)) files.push(full);
      }
    };
    walk(join(ROOT, "lib")); walk(join(ROOT, "src"));
    const offenders = files
      .map((f) => relative(ROOT, f).replace(/\\/g, "/"))
      .filter((f) => !allowed.has(f))
      .filter((f) => /\b(getMe|updateMe)\s*\(/.test(read(f)));
    assert.deepEqual(offenders, []);
  });

  it("only the notification centre reads the shared unread count", () => {
    const center = read("lib/notification-center.tsx");
    assert.match(center, /const count = await fetchUnreadCount\(\);/);
    assert.doesNotMatch(center, /getUnreadNotificationCount/);
    for (const f of ["lib/notification-bell.tsx", "lib/push-bridge.tsx", "src/app/notifications.tsx"]) {
      assert.doesNotMatch(read(f), /fetchUnreadCount/, f);
    }
  });
});

describe("polling only in the foreground", () => {
  it("the Desk's processing poll skips ticks while the app is in the background", () => {
    const desk = read("src/app/(tabs)/index.tsx");
    assert.match(desk, /setInterval\(\(\) => \{ if \(AppState\.currentState === "active"\) load\(\); \}, 20_000\)/);
  });

  it("the meeting's processing poll does too", () => {
    const ctx = read("lib/meeting-context.tsx");
    const poll = ctx.slice(ctx.indexOf("const t = setInterval(async () => {"));
    assert.ok(poll.indexOf('if (AppState.currentState !== "active") return;') < poll.indexOf("getRecording(meetingKey)"));
  });
});

describe("Calendar: highlights cost at most one request per meeting per day", () => {
  let client;
  beforeEach(() => { client = appClient(); onlineManager.setOnline(true); });
  afterEach(() => { client.clear(); });

  it("keys per account and meeting; saved to the phone for a day", () => {
    assert.deepEqual(highlightsKey("u-1", "rec/k1"), ["highlights", "u-1", "rec/k1"]);
    assert.ok(PERSISTED_ROOTS.has("highlights"));
    assert.equal(HIGHLIGHTS_FRESH_MS, 24 * 3600 * 1000);
  });

  it("only processed meetings are asked", () => {
    assert.equal(hasReadableHighlights("complete"), true);
    assert.equal(hasReadableHighlights("transcribed"), true);
    for (const s of ["uploading", "uploaded", "transcribing", "generating_ai", "failed", "", null, undefined]) {
      assert.equal(hasReadableHighlights(s), false, String(s));
    }
  });

  it("an EMPTY answer is remembered: 8 empty meetings, two visits = 8 AI calls, not 16", async () => {
    const ai = endpoint(() => ({ deadlines: [], decisions: [] }), 5);
    const visit = () => Promise.all(Array.from({ length: 8 }, (_, i) =>
      client.fetchQuery({ queryKey: highlightsKey("u-1", `k${i}`), queryFn: ai,
                          staleTime: HIGHLIGHTS_FRESH_MS, retry: false })));
    await visit();
    await visit();
    assert.equal(ai.calls, 8);
  });

  it("a FAILED answer is not remembered: the next visit asks again", async () => {
    let fail = true;
    const ai = async () => { ai.calls += 1; if (fail) throw new Error("503"); return { deadlines: [] }; };
    ai.calls = 0;
    const opts = { queryKey: highlightsKey("u-1", "k"), queryFn: ai, staleTime: HIGHLIGHTS_FRESH_MS, retry: false };
    await assert.rejects(client.fetchQuery(opts));
    fail = false;
    await client.fetchQuery(opts);
    assert.equal(ai.calls, 2);
  });
});

describe("bounded parallelism (mapLimit)", () => {
  it("never has more than `limit` calls in flight, and keeps input order", async () => {
    let inFlight = 0, peak = 0;
    const out = await mapLimit([5, 1, 4, 2, 3, 0, 6, 7, 8, 9, 10, 11], 3, async (ms, i) => {
      inFlight += 1; peak = Math.max(peak, inFlight);
      await new Promise((r) => setTimeout(r, ms * 3));
      inFlight -= 1;
      return i;
    });
    assert.equal(peak, 3);
    assert.deepEqual(out, [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]);
  });

  it("handles empty input and a limit above the item count", async () => {
    assert.deepEqual(await mapLimit([], 3, async () => 1), []);
    assert.deepEqual(await mapLimit([1, 2], 10, async (x) => x * 2), [2, 4]);
  });
});

describe("Tasks: list and summary cards share one unfiltered read", () => {
  let client;
  beforeEach(() => { client = appClient(); });
  afterEach(() => { client.clear(); });

  it("asked together = one request; asked again later = a fresh one (task lists change)", async () => {
    const tasks = endpoint(() => ({ tasks: [{ id: "t1" }], next_cursor: "" }));
    const key = unfilteredTasksKey({ userId: "u-1", workspaceId: "" }, 100);
    const opts = { queryKey: key, queryFn: tasks, staleTime: 0, retry: false };
    await Promise.all([client.fetchQuery(opts), client.fetchQuery(opts)]);
    assert.equal(tasks.calls, 1);
    await client.fetchQuery(opts);
    assert.equal(tasks.calls, 2);
  });

  it("is per account and workspace, and never saved to the phone", () => {
    assert.notDeepEqual(unfilteredTasksKey({ userId: "u-1", workspaceId: "" }, 100),
                        unfilteredTasksKey({ userId: "u-1", workspaceId: "ws-1" }, 100));
    assert.equal(shouldPersist({ queryKey: unfilteredTasksKey({ userId: "u", workspaceId: "" }, 100),
                                 state: { status: "success" } }), false);
  });
});

describe("wiring: Tasks and Calendar", () => {
  const cal = read("src/app/calendar.tsx");
  const tasks = read("src/app/tasks.tsx");
  const shared = read("lib/shared-data.ts");

  it("Calendar: shared meetings list, cached highlights, processed meetings only, 3 at a time", () => {
    assert.match(cal, /useRecordings\(""\)/);
    assert.doesNotMatch(cal, /(getRecordings|getMeetingHighlights)\(/);
    assert.match(cal, /\.filter\(\(r\) => hasReadableHighlights\(r\.status\)\)/);
    assert.match(cal, /mapLimit\(recent, HIGHLIGHT_CONCURRENCY,/);
    assert.match(cal, /const HIGHLIGHT_CONCURRENCY = 3;/);
    assert.match(cal, /await fetchMeetingHighlights\(r\.audio_s3_key\)/);
  });

  it("Calendar: highlights are not re-read on every focus", () => {
    const at = cal.search(/setNow\(new Date\(\)\);\s*load\(month\);/);
    assert.ok(at > 0, "the focus effect");
    const body = cal.slice(at, cal.indexOf("]);", at) + 3);
    assert.doesNotMatch(body, /highlight/i);
  });

  it("highlights are remembered for a day and failures are not", () => {
    const fn = shared.slice(shared.indexOf("export async function fetchMeetingHighlights"));
    assert.match(fn.slice(0, 500), /staleTime: HIGHLIGHTS_FRESH_MS,\s*retry: false,/);
  });

  it("Tasks: shared list, profile and unfiltered read; no direct duplicates", () => {
    assert.match(tasks, /useRecordings\(""\)/);
    assert.match(tasks, /fetchMe\(\)/);
    assert.doesNotMatch(tasks, /(getMe|getRecordings)\(/);
    assert.match(tasks, /Object\.keys\(filters\)\.length === 0\s*\? await fetchUnfilteredTasks\(INSIGHT_PAGE\)/);
    assert.match(tasks, /const res = await fetchUnfilteredTasks\(INSIGHT_PAGE\);\s*setInsightTasks/);
  });

  it("the unfiltered read is scoped like request() (token account + workspace header)", () => {
    const fn = shared.slice(shared.indexOf("export async function fetchUnfilteredTasks"));
    assert.match(fn, /userId: await accountId\(\), workspaceId: \(await getActiveWorkspaceId\(\)\) \|\| ""/);
    assert.match(fn.slice(0, 900), /staleTime: 0,\s*retry: false,/);
  });
});

describe("meeting screen: the Salesforce block survives tab switches", () => {
  let client;
  beforeEach(() => { client = appClient(); });
  afterEach(() => { client.clear(); });
  const read15 = (fn, force = false) => ({
    queryKey: webhookMeetingKey("u-1", "rec/k"), queryFn: fn,
    staleTime: force ? 0 : WEBHOOK_STATE_FRESH_MS, retry: false,
  });

  it("keys are per account and meeting / workspace, and are never saved to the phone", () => {
    assert.deepEqual(webhookMeetingKey("u-1", "k"), ["webhook-meeting", "u-1", "k"]);
    assert.deepEqual(linkConfigKey("u-1", "ws-1"), ["link-config", "u-1", "ws-1"]);
    const ok = { status: "success" };
    assert.equal(shouldPersist({ queryKey: webhookMeetingKey("u-1", "k"), state: ok }), false);
    assert.equal(shouldPersist({ queryKey: linkConfigKey("u-1", "w"), state: ok }), false);
  });

  it("Overview → Tasks → Overview within 15 s: one request, not two", async () => {
    const api = endpoint(() => ({ off: false, meeting: { mode: "live" } }));
    await client.fetchQuery(read15(api));   // first mount
    await client.fetchQuery(read15(api));   // remount after the tab switch
    assert.equal(api.calls, 1);
  });

  it("the delivery poll and every change always ask (force)", async () => {
    const api = endpoint(() => ({ off: false, meeting: { mode: "live" } }));
    await client.fetchQuery(read15(api));
    await client.fetchQuery(read15(api, true));
    await client.fetchQuery(read15(api, true));
    assert.equal(api.calls, 3);
  });

  it("a forced read joins a mount read already in flight", async () => {
    const api = endpoint(() => ({ off: false, meeting: {} }), 30);
    await Promise.all([client.fetchQuery(read15(api)), client.fetchQuery(read15(api, true))]);
    assert.equal(api.calls, 1);
  });

  it("'off' is remembered like any answer: no 404 on every Overview visit", async () => {
    const api = endpoint(() => ({ off: true }));
    await client.fetchQuery(read15(api));
    const again = await client.fetchQuery(read15(api));
    assert.equal(again.off, true);
    assert.equal(api.calls, 1);
  });

  it("the link setup is reused for 5 minutes; Try again always asks", async () => {
    const api = endpoint(() => ({ supported: true }));
    const opts = (force) => ({ queryKey: linkConfigKey("u-1", "ws-1"), queryFn: api,
                               staleTime: force ? 0 : LINK_CONFIG_FRESH_MS, retry: false });
    await client.fetchQuery(opts(false));
    await client.fetchQuery(opts(false));
    assert.equal(api.calls, 1);
    await client.fetchQuery(opts(true));
    assert.equal(api.calls, 2);
  });
});

describe("wiring: the Salesforce block and polling", () => {
  const block = read("lib/webhook-meeting.tsx");
  const shared = read("lib/shared-data.ts");
  const polling = read("lib/salesforce-hooks.ts");

  it("reads through the shared cache, never the API directly", () => {
    assert.doesNotMatch(block, /\b(getWebhookMeeting|getLinkConfig)\(/);
    assert.match(block, /await fetchWebhookMeeting\(meetingKey, \{ force \}\)/);
    assert.match(block, /fetchLinkConfig\(workspaceId, \{ force: linkConfigTry > 0 \}\)/);
  });

  it("only the mount reuses; poll, changes and onChanged force", () => {
    assert.match(block, /const load = useCallback\(async \(force = true\) =>/);
    assert.match(block, /useEffect\(\(\) => \{ void load\(false\); \}, \[load\]\);/);
    assert.match(block, /usePolling\(blockMode === "live" && isDeliveryInFlight\(state\?\.job\), \(\) => \{ void load\(\); \}, 3000\)/);
    assert.match(block, /onChanged=\{load\}/);
  });

  it("an 'off' answer still hides the section", () => {
    assert.match(block, /if \(read\.off\) \{ setOff\(true\); return; \}/);
    const fn = shared.slice(shared.indexOf("export function isWebhookOff"));
    assert.match(fn.slice(0, 300), /e\.code === "webhook_disabled" \|\| e\.status === 404 \|\| e\.status === 403/);
  });

  it("the org-connection check is keyed on whether state exists, not on the state object", () => {
    assert.match(block, /const hasState = !!state;/);
    assert.match(block, /\}, \[enabled, readOnly, workspaceId, hasState, blockMode\]\);/);
    assert.doesNotMatch(block, /\[enabled, readOnly, workspaceId, state, blockMode\]/);
  });

  it("usePolling skips ticks while the app is in the background", () => {
    assert.match(polling, /setInterval\(\(\) => \{ if \(AppState\.currentState === "active"\) ref\.current\(\); \}, ms\)/);
  });
});

describe("after a confirmed change, every saved copy agrees", () => {
  const A = { audio_s3_key: "rec/a", title: "Old", status: "complete" };
  const B = { audio_s3_key: "rec/b", title: "Other", status: "complete" };

  it("patches one meeting's title/status and leaves the rest", () => {
    const out = patchMeetingInList([A, B], "rec/a", { title: "New" });
    assert.deepEqual(out.map((r) => r.title), ["New", "Other"]);
    assert.equal(out[1], B, "untouched rows are the same objects");
  });

  it("returns the SAME array when the meeting isn't in it (no needless re-render)", () => {
    const list = [B];
    assert.equal(patchMeetingInList(list, "rec/a", { title: "x" }), list);
    assert.equal(withoutMeeting(list, "rec/a"), list);
    assert.equal(patchMeetingInList(undefined, "rec/a", { title: "x" }), undefined);
  });

  it("removes a trashed meeting", () => {
    assert.deepEqual(withoutMeeting([A, B], "rec/a"), [B]);
  });

  it("knows which saved answers are about one meeting", () => {
    assert.equal(isAboutMeeting(["meeting", "u-1", "personal", "rec/a"], "rec/a"), true);
    assert.equal(isAboutMeeting(highlightsKey("u-1", "rec/a"), "rec/a"), true);
    assert.equal(isAboutMeeting(webhookMeetingKey("u-1", "rec/a"), "rec/a"), true);
    assert.equal(isAboutMeeting(["meeting", "u-1", "personal", "rec/b"], "rec/a"), false);
    assert.equal(isAboutMeeting(recordingsKey({ userId: "u-1", workspaceId: "" }, ""), "rec/a"), false);
    assert.equal(isAboutMeeting(meKey("rec/a"), "rec/a"), false, "only per-meeting roots");
  });

  describe("on the real query engine", () => {
    let client;
    const desk = recordingsKey({ userId: "u-1", workspaceId: "" }, "");
    const filtered = recordingsKey({ userId: "u-1", workspaceId: "" }, "u-9");
    const saved = ["meeting", "u-1", "personal", "rec/a"];
    beforeEach(() => {
      client = appClient();
      client.setQueryData(desk, [A, B]);
      client.setQueryData(filtered, [A]);
      client.setQueryData(saved, { rec: { ...A, transcript: "" }, tasks: [] });
      client.setQueryData(highlightsKey("u-1", "rec/a"), { deadlines: [] });
      client.setQueryData(highlightsKey("u-1", "rec/b"), { deadlines: [] });
      client.setQueryData(webhookMeetingKey("u-1", "rec/a"), { off: true });
    });
    afterEach(() => { client.clear(); });

    it("a rename reaches every list and the saved meeting (applyMeetingChange)", () => {
      client.setQueriesData({ queryKey: ["recordings"] }, (l) => patchMeetingInList(l, "rec/a", { title: "New" }));
      client.setQueriesData({ queryKey: ["meeting"], predicate: (q) => isAboutMeeting(q.queryKey, "rec/a") },
        (sv) => (sv ? { ...sv, rec: { ...sv.rec, title: "New" } } : sv));
      assert.equal(client.getQueryData(desk)[0].title, "New");
      assert.equal(client.getQueryData(filtered)[0].title, "New");
      assert.equal(client.getQueryData(saved).rec.title, "New");
      assert.equal(client.getQueryData(desk)[1].title, "Other");
    });

    it("a permanent delete leaves nothing about that meeting on the phone (forgetMeeting)", () => {
      client.setQueriesData({ queryKey: ["recordings"] }, (l) => withoutMeeting(l, "rec/a"));
      client.removeQueries({ predicate: (q) => isAboutMeeting(q.queryKey, "rec/a") });
      assert.deepEqual(client.getQueryData(desk), [B]);
      assert.equal(client.getQueryData(saved), undefined);
      assert.equal(client.getQueryData(highlightsKey("u-1", "rec/a")), undefined);
      assert.equal(client.getQueryData(webhookMeetingKey("u-1", "rec/a")), undefined);
      assert.ok(client.getQueryData(highlightsKey("u-1", "rec/b")), "other meetings untouched");
    });

    it("Desk focus re-reads only a STALE list; pull-to-refresh always does", async () => {
      const api = endpoint(() => [A, B], 5);
      const observer = new QueryObserver(client, { queryKey: desk, queryFn: api });
      const unsub = observer.subscribe(() => {});
      await client.refetchQueries({ queryKey: desk, exact: true }, { cancelRefetch: false }); // a fresh read
      const before = api.calls;
      await client.refetchQueries({ queryKey: desk, exact: true, stale: true }, { cancelRefetch: false }); // focus
      assert.equal(api.calls, before, "fresh list: no request on focus");
      await client.invalidateQueries({ queryKey: ["recordings"] });   // restore / brief ready
      const afterInvalidate = api.calls;
      assert.ok(afterInvalidate > before, "an invalidated list on screen reloads at once");
      await client.refetchQueries({ queryKey: desk, exact: true }, { cancelRefetch: false }); // pull
      assert.equal(api.calls, afterInvalidate + 1);
      unsub();
    });
  });
});

describe("wiring: every change updates the saved copies AFTER the server confirms", () => {
  const after = (src, call, effect) => {
    const i = src.indexOf(call);
    assert.ok(i > 0, `${call} present`);
    const j = src.indexOf(effect, i);
    assert.ok(j > i && j - i < 600, `${effect} follows ${call}`);
  };

  it("rename (meeting screen)", () => {
    after(read("lib/meeting-context.tsx"), "await updateRecording(meetingKey, { title: trimmed })",
          "applyMeetingChange(meetingKey, { title: updated.title || trimmed })");
  });

  it("reprocess and Move to Trash (meeting screen)", () => {
    const screen = read("src/app/recording/[key]/index.tsx");
    after(screen, "await reprocessRecording(key);", 'applyMeetingChange(key, { status: "transcribing" });');
    after(screen, "await reprocessRecording(key);", "forgetMeetingHighlights(key);");
    after(screen, "await trashRecording(key);", "dropMeetingFromLists(key);");
  });

  it("restore and permanent delete (Trash)", () => {
    const trash = read("src/app/trash.tsx");
    after(trash, "await restoreRecording(key);", "void refreshMeetingLists();");
    after(trash, "await permanentlyDeleteRecording(key);", "forgetMeeting(key);");
  });

  it("a brief-ready push marks the lists stale", () => {
    const bridge = read("lib/push-bridge.tsx");
    assert.match(bridge, /if \(briefTargetFrom\(n\.request\.content\.data\)\) void refreshMeetingLists\(\);/);
  });

  it("Desk focus refreshes only a stale list; the forced load() is unchanged", () => {
    const desk = read("src/app/(tabs)/index.tsx");
    assert.match(desk, /refetchQueries\(\{ queryKey: listKey, exact: true, stale: true \}, \{ cancelRefetch: false \}\)/);
    assert.match(desk, /useFocusEffect\(useCallback\(\(\) => \{ refreshIfStale\(\); \}, \[refreshIfStale\]\)\);/);
    assert.doesNotMatch(desk, /useFocusEffect\(useCallback\(\(\) => \{ load\(\); \}, \[load\]\)\);/);
    assert.match(desk, /refetchQueries\(\{ queryKey: listKey, exact: true \}, \{ cancelRefetch: false \}\)/);
  });
});
