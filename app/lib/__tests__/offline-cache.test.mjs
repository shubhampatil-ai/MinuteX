// lib/__tests__/offline-cache.test.mjs — the on-device cache (local-first,
// phase 1): its rules, and how the Desk and sign-out use it.
//
// Run:  node --test lib/__tests__/offline-cache.test.mjs
//
// Pins: cache keys carry the account and the workspace; a token's account
// is read from `sub` and anything unreadable is ""; no retry on 4xx (except
// timeouts and rate limits), at most twice, with growing waits; only
// opted-in successful answers are saved; signing out wipes the cache; the
// Desk reads its list through the cache, says when it is offline, and never
// starts a second request on focus.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  CACHE_MAX_AGE_MS, MAX_SAVED_MEETINGS, meetingKey, meetingsToEvict, offlineMeetingCopy,
  recordingsKey, retryDelay, savedAtLabel, shouldPersist, shouldRetry, userIdFromToken,
  ALWAYS_KEEP_ROOTS, PERSIST_BUDGET_CHARS, PERSIST_MAX_ENTRY_CHARS, fitToBudget,
} from "../offline-cache-rules.ts";
import { QueryClient, dehydrate, hydrate } from "@tanstack/query-core";

const ROOT = new URL("../../", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const read = (rel) => readFileSync(join(ROOT, rel), "utf8");

function jwt(payload) {
  const b64u = (o) => Buffer.from(JSON.stringify(o)).toString("base64url");
  return `${b64u({ alg: "HS256" })}.${b64u(payload)}.sig`;
}

describe("cache rules", () => {
  it("reads the account from the token, and nothing from junk", () => {
    assert.equal(userIdFromToken(jwt({ sub: "user-1", exp: 1 })), "user-1");
    for (const bad of [null, undefined, "", "abc", "a.!!!.c", jwt({ sub: 5 }), jwt({})]) {
      assert.equal(userIdFromToken(bad), "", String(bad));
    }
  });

  it("keys carry the account, the workspace and the filter", () => {
    assert.deepEqual(recordingsKey({ userId: "u1", workspaceId: "" }, ""),
                     ["recordings", "u1", "personal", ""]);
    assert.deepEqual(recordingsKey({ userId: "u1", workspaceId: "wso_1" }, "u2"),
                     ["recordings", "u1", "wso_1", "u2"]);
    assert.notDeepEqual(recordingsKey({ userId: "u1", workspaceId: "" }, ""),
                        recordingsKey({ userId: "u2", workspaceId: "" }, ""));
  });

  it("retries only what might go away, at most twice, waiting longer each time", () => {
    for (const status of [400, 401, 403, 404, 409, 422]) {
      assert.equal(shouldRetry(0, { status }), false, String(status));
    }
    for (const status of [408, 429, 500, 503, 0]) {
      assert.equal(shouldRetry(0, { status }), true, String(status));
    }
    assert.equal(shouldRetry(2, { status: 503 }), false);
    assert.deepEqual([0, 1, 2, 10].map(retryDelay), [1000, 2000, 4000, 15000]);
  });

  it("saves only the meetings list, meetings and opted-in answers — and only successes", () => {
    const ok = { status: "success" };
    assert.equal(shouldPersist({ queryKey: ["recordings", "u"], state: ok }), true);
    assert.equal(shouldPersist({ queryKey: ["meeting", "u", "personal", "k"], state: ok }), true);
    assert.equal(shouldPersist({ queryKey: ["other"], meta: { persist: true }, state: ok }), true);
    assert.equal(shouldPersist({ queryKey: ["other"], state: ok }), false);
    assert.equal(shouldPersist({ queryKey: ["meeting"], state: { status: "error" } }), false);
  });

  it("a saved meeting leaves out the transcript, the audio link and the chat", () => {
    const copy = offlineMeetingCopy({
      title: "QBR", transcript: "long text", timestamps: [{ start: 0 }],
      audio_url: "https://s3/presigned", chat_history: [{ q: 1 }], overview: { a: 1 },
    });
    assert.deepEqual(
      [copy.transcript, copy.timestamps, copy.audio_url, copy.chat_history, copy.transcript_withheld],
      ["", [], null, null, true]);
    assert.deepEqual(copy.overview, { a: 1 });
    assert.equal(offlineMeetingCopy({ transcript: "" }).transcript_withheld, false);
  });

  it("meeting keys carry the account and workspace; only the newest meetings are kept", () => {
    assert.deepEqual(meetingKey({ userId: "u1", workspaceId: "" }, "k"), ["meeting", "u1", "personal", "k"]);
    const saved = Array.from({ length: MAX_SAVED_MEETINGS + 3 }, (_, i) => ({ id: i, dataUpdatedAt: i }));
    assert.deepEqual(meetingsToEvict(saved).map((x) => x.id), [2, 1, 0]);
    assert.deepEqual(meetingsToEvict(saved.slice(0, 5)), []);
  });

  it("labels when a saved answer was fetched", () => {
    assert.equal(savedAtLabel(0), "");
    assert.equal(savedAtLabel(new Date(2026, 9, 8, 9, 5).getTime()), "saved 8 Oct, 09:05");
  });

  it("keeps saved answers a week at most", () => {
    assert.equal(CACHE_MAX_AGE_MS, 7 * 24 * 3600 * 1000);
  });
});

describe("sign-out wipes the cache", () => {
  const api = read("lib/api.ts");
  const qc = read("lib/query-client.tsx");

  it("every clearToken() runs the sign-out handlers, and a failure never blocks it", () => {
    const body = api.slice(api.indexOf("export async function clearToken"));
    assert.match(body.slice(0, 400), /for \(const fn of signOutHandlers\) \{\s*try \{ await fn\(\); \} catch/);
  });

  it("the cache registers its wipe, which clears memory and the phone", () => {
    assert.match(qc, /onSignOut\(wipeQueryCache\)/);
    assert.match(qc, /queryClient\.clear\(\);\s*await persister\?\.removeClient\(\);/);
  });

  it("api.ts never imports the cache (no native module in the API layer)", () => {
    assert.doesNotMatch(api, /from "\.\/query-client"/);
  });

  it("native storage and network modules are loaded defensively", () => {
    assert.match(qc, /require\("@react-native-async-storage\/async-storage"\)/);
    assert.match(qc, /require\("@react-native-community\/netinfo"\)/);
    assert.doesNotMatch(qc, /^import .*"@react-native-async-storage\//m);
    assert.doesNotMatch(qc, /^import .*"@react-native-community\/netinfo"/m);
  });

  it("is mounted for every screen", () => {
    const layout = read("src/app/_layout.tsx");
    assert.ok(layout.indexOf("<QueryProvider>") > layout.indexOf("<WorkspaceProvider>"));
    assert.ok(layout.indexOf("</QueryProvider>") < layout.indexOf("</WorkspaceProvider>"));
  });
});

describe("the Desk reads through the cache", () => {
  const desk = read("src/app/(tabs)/index.tsx");
  const shared = read("lib/shared-data.ts");

  it("keys its list on the account + workspace + filter and saves it (via lib/shared-data.ts)", () => {
    assert.match(desk, /const \{ query: list, key: listKey \} = useRecordings\(whose\);/);
    assert.match(shared, /useMemo\(\(\) => \(scope \? recordingsKey\(scope, whose\) : null\), \[scope, whose\]\)/);
    assert.match(shared, /meta: \{ persist: true \}/);
    assert.match(shared, /enabled: !!key/);
  });

  it("joins a request in flight instead of starting a second one", () => {
    assert.match(desk, /refetchQueries\(\{ queryKey: listKey, exact: true \}, \{ cancelRefetch: false \}\)/);
  });

  it("says when it is offline, with or without a saved list", () => {
    assert.match(desk, /You're offline\. Your briefs will appear when you're back online\./);
    assert.match(desk, /`Offline · \$\{savedAtLabel\(list\.dataUpdatedAt\)\}`/);
  });

  it("still signs out on a 401", () => {
    assert.match(shared, /e\.status === 401\) \{ await clearToken\(\); router\.replace\("\/login"\); \}/);
  });
});

describe("the meeting reads through the cache", () => {
  const ctx = read("lib/meeting-context.tsx");
  const screen = read("src/app/recording/[key]/index.tsx");
  const transcript = read("src/app/recording/[key]/transcript.tsx");

  it("paints the saved copy first, then saves the server's answer without the transcript", () => {
    const load = ctx.slice(ctx.indexOf("const load = useCallback"));
    assert.ok(load.indexOf("getQueryData<SavedMeeting>(key)") < load.indexOf("await getRecording(meetingKey)"));
    assert.match(load, /rec: offlineMeetingCopy\(data\)/);
    assert.match(load, /meetingsToEvict\(all\)/);
  });

  it("offline keeps the saved copy with a note; a deleted meeting drops it", () => {
    assert.match(ctx, /setSavedNote\(`Offline · \$\{savedAtLabel\(/);
    assert.match(ctx, /e\.status === 404\) \{\s*if \(key\) queryClient\.removeQueries\(\{ queryKey: key, exact: true \}\);/);
  });

  it("a withheld transcript keeps the tabs, and the Transcript tab says why it's missing", () => {
    assert.match(screen, /const hasTranscript = !!rec\.transcript \|\| !!rec\.transcript_withheld;/);
    assert.match(screen, /\{savedNote\}/);
    assert.match(transcript, /The transcript isn't saved on this phone\. Connect to the internet to read it\./);
  });
});

describe("storage budget: the saved cache can never outgrow one Android value", () => {
  const q = (root, id, chars, at) => ({
    queryKey: [root, "u-1", id], queryHash: `${root}-${id}`,
    state: { data: "x".repeat(chars), dataUpdatedAt: at, status: "success" },
  });
  const saved = (queries) => ({ timestamp: 1, buster: "b", clientState: { mutations: [], queries } });
  const size = (c) => c.clientState.queries.reduce((n, x) => n + JSON.stringify(x).length, 0);

  it("stays under Android's ~2 MB per value even at 2 bytes per character", () => {
    assert.ok(PERSIST_BUDGET_CHARS * 2 <= 2 * 1024 * 1024);
    assert.ok(PERSIST_MAX_ENTRY_CHARS < PERSIST_BUDGET_CHARS);
    assert.deepEqual([...ALWAYS_KEEP_ROOTS].sort(), ["me", "recordings"]);
  });

  it("under budget: nothing is touched (same object)", () => {
    const c = saved([q("me", "a", 100, 1), q("meeting", "m1", 1000, 2)]);
    const r = fitToBudget(c);
    assert.equal(r.dropped, 0);
    assert.equal(r.client, c);
  });

  it("over budget: profile and lists kept, then newest meetings first", () => {
    const c = saved([
      q("recordings", "list", 50_000, 1), q("me", "me", 1_000, 1),
      q("meeting", "old", 400_000, 10), q("meeting", "mid", 400_000, 20), q("meeting", "new", 400_000, 30),
    ]);
    const r = fitToBudget(c, 1_000_000, 500_000);
    const ids = r.client.clientState.queries.map((x) => x.queryKey[2]);
    assert.deepEqual(ids, ["list", "me", "new", "mid"]);
    assert.equal(r.dropped, 1);
    assert.ok(size(r.client) <= 1_000_000);
  });

  it("one oversized answer is skipped, smaller older ones still fit", () => {
    const c = saved([q("meeting", "huge", 300_000, 99), q("meeting", "small", 1_000, 1)]);
    const r = fitToBudget(c);
    assert.deepEqual(r.client.clientState.queries.map((x) => x.queryKey[2]), ["small"]);
  });

  it("the lists are kept even when large: offline the Desk must show something", () => {
    const c = saved([q("recordings", "list", 600_000, 1), q("meeting", "m", 100, 2)]);
    const r = fitToBudget(c, 1_000_000, 256_000);
    assert.ok(r.client.clientState.queries.some((x) => x.queryKey[0] === "recordings"));
  });

  it("20 maximum-size meetings (400 KB rows) no longer produce an unsavable value", () => {
    const qs = [q("recordings", "list", 60_000, 1), q("me", "me", 1_000, 1)];
    for (let i = 0; i < 20; i++) qs.push(q("meeting", `m${i}`, 200_000, i + 2));
    const r = fitToBudget(saved(qs));
    assert.ok(JSON.stringify(r.client).length <= PERSIST_BUDGET_CHARS + 1_000);
    assert.ok(r.client.clientState.queries.some((x) => x.queryKey[2] === "m19"), "the newest is kept");
    assert.ok(r.dropped > 0);
  });

  it("round trip on the real engine: capped save, restored offline launch still has the Desk", () => {
    const live = new QueryClient();
    live.setQueryData(["recordings", "u-1", "personal", ""], [{ audio_s3_key: "a", title: "T" }]);
    live.setQueryData(["me", "u-1"], { user_id: "u-1" });
    for (let i = 0; i < 12; i++) {
      live.setQueryData(["meeting", "u-1", "personal", `m${i}`], { rec: { notes: "y".repeat(150_000) } });
    }
    const persisted = { timestamp: Date.now(), buster: "b", clientState: dehydrate(live) };
    const json = JSON.stringify(fitToBudget(persisted).client);
    assert.ok(json.length <= PERSIST_BUDGET_CHARS + 2_000, `saved ${json.length} chars`);
    const restored = new QueryClient();
    hydrate(restored, JSON.parse(json).clientState);
    assert.equal(restored.getQueryData(["recordings", "u-1", "personal", ""])[0].title, "T");
    assert.equal(restored.getQueryData(["me", "u-1"]).user_id, "u-1");
    live.clear(); restored.clear();
  });

  it("every save goes through the budget", () => {
    const qc = read("lib/query-client.tsx");
    assert.match(qc, /serialize: \(client\) => \{\s*const \{ client: fitted, dropped \} = fitToBudget\(client\);/);
    assert.match(qc, /return JSON\.stringify\(fitted\);/);
  });
});
