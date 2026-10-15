// lib/__tests__/request-guard.test.mjs — timeout, circuit breaker, dev call
// counter, and "no session, no call".
//
// Run:  node --test lib/__tests__/request-guard.test.mjs
//
// The rules are imported directly (lib/request-guard.ts is pure). request()
// itself lives in lib/api.ts, which imports native storage, so its wiring is
// pinned from the source — the same way offline-cache.test.mjs does.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  BREAKER_MAX_OPEN_MS, BREAKER_OPEN_MS, BREAKER_THRESHOLD, BREAKER_WINDOW_MS,
  CALL_COUNTER_WARN_AT, CircuitBreaker, REQUEST_TIMEOUT_MS, isOverloadStatus,
  refusedOffline, retryAfterMs, routeKey, summarizeCalls,
} from "../request-guard.ts";

const ROOT = new URL("../../", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const read = (p) => readFileSync(join(ROOT, p), "utf8");
const T0 = 1_800_000_000_000;
const R = "POST /me/push-tokens";

/** Fail `n` calls on `key` at time t (each must be let through first). */
function fail(b, key, t, n = 1, retryAfter = null) {
  for (let i = 0; i < n; i++) {
    assert.equal(b.check(key, t).open, false, "call should have been let through");
    b.record(key, t, "overload", retryAfter);
  }
}

describe("route keys", () => {
  it("drop ids, encoded recording keys, tokens and the query", () => {
    assert.equal(routeKey("get", "/me"), "GET /me");
    assert.equal(routeKey("POST", "/me/push-tokens"), "POST /me/push-tokens");
    assert.equal(routeKey("DELETE", "/me/push-tokens/inst-0a1b2c"), "DELETE /me/push-tokens/:id");
    assert.equal(routeKey("GET", `/recordings/${encodeURIComponent("recordings/u-1/m.m4a")}`),
      "GET /recordings/:id");
    assert.equal(routeKey("GET", "/recordings/ai/tasks/rec%2Fkey"), "GET /recordings/ai/tasks/:id");
    assert.equal(routeKey("GET", "/recordings?user_id=u-9"), "GET /recordings");
    assert.equal(routeKey("POST", "/workspace-invitations/abcdefghijklmnopqrstuvwxyz/accept"),
      "POST /workspace-invitations/:id/accept");
    assert.equal(routeKey("GET", "/notifications/unread-count"), "GET /notifications/unread-count");
  });
});

describe("what counts as overload", () => {
  it("429, 502, 503, 504 — not client errors, not a plain 500", () => {
    for (const s of [429, 502, 503, 504]) assert.equal(isOverloadStatus(s), true, String(s));
    for (const s of [200, 400, 401, 403, 404, 409, 500]) assert.equal(isOverloadStatus(s), false, String(s));
  });

  it("reads Retry-After as seconds or an HTTP date", () => {
    assert.equal(retryAfterMs("120", T0), 120_000);
    assert.equal(retryAfterMs("1.5", T0), 1500);
    assert.equal(retryAfterMs(new Date(T0 + 90_000).toUTCString(), T0), 90_000);
    assert.equal(retryAfterMs(new Date(T0 - 5_000).toUTCString(), T0), 0);
    assert.equal(retryAfterMs(null, T0), null);
    assert.equal(retryAfterMs("soon", T0), null);
  });
});

describe("circuit breaker", () => {
  it("stays closed below the threshold", () => {
    const b = new CircuitBreaker();
    fail(b, R, T0, BREAKER_THRESHOLD - 1);
    assert.equal(b.check(R, T0).open, false);
  });

  it("opens after THRESHOLD overloads inside the window, and refuses without the network", () => {
    const b = new CircuitBreaker();
    fail(b, R, T0, BREAKER_THRESHOLD);
    const g = b.check(R, T0 + 1);
    assert.equal(g.open, true);
    assert.equal(g.retryInMs, BREAKER_OPEN_MS - 1);
  });

  it("forgets overloads older than the window", () => {
    const b = new CircuitBreaker();
    fail(b, R, T0, BREAKER_THRESHOLD - 1);
    fail(b, R, T0 + BREAKER_WINDOW_MS + 1, 1);
    assert.equal(b.isOpen(R, T0 + BREAKER_WINDOW_MS + 2), false);
  });

  it("is per route: one broken endpoint does not block the others", () => {
    const b = new CircuitBreaker();
    fail(b, R, T0, BREAKER_THRESHOLD);
    assert.equal(b.check(R, T0).open, true);
    assert.equal(b.check("GET /recordings", T0).open, false);
  });

  it("a success resets the count", () => {
    const b = new CircuitBreaker();
    fail(b, R, T0, BREAKER_THRESHOLD - 1);
    b.check(R, T0); b.record(R, T0, "ok");
    fail(b, R, T0, BREAKER_THRESHOLD - 1);
    assert.equal(b.check(R, T0).open, false);
  });

  it("offline and plain 500s neither open nor close it", () => {
    const b = new CircuitBreaker();
    for (let i = 0; i < 10; i++) { b.check(R, T0); b.record(R, T0, "neutral"); }
    assert.equal(b.check(R, T0).open, false);
    fail(b, R, T0, BREAKER_THRESHOLD);
    assert.equal(b.isOpen(R, T0), true);
  });

  it("after the open time, lets exactly ONE trial through", () => {
    const b = new CircuitBreaker();
    fail(b, R, T0, BREAKER_THRESHOLD);
    const later = T0 + BREAKER_OPEN_MS;
    assert.equal(b.check(R, later).open, false, "the trial");
    assert.equal(b.check(R, later).open, true, "others wait for the trial");
  });

  it("a successful trial closes it", () => {
    const b = new CircuitBreaker();
    fail(b, R, T0, BREAKER_THRESHOLD);
    const later = T0 + BREAKER_OPEN_MS;
    b.check(R, later); b.record(R, later, "ok");
    assert.equal(b.check(R, later).open, false);
    assert.equal(b.check(R, later).open, false);
  });

  it("a failed trial re-opens it for twice as long, never beyond the max", () => {
    const b = new CircuitBreaker();
    fail(b, R, T0, BREAKER_THRESHOLD);
    let t = T0 + BREAKER_OPEN_MS;
    let expected = BREAKER_OPEN_MS;
    for (let i = 0; i < 10; i++) {
      assert.equal(b.check(R, t).open, false, `trial ${i}`);
      b.record(R, t, "overload");
      expected = Math.min(expected * 2, BREAKER_MAX_OPEN_MS);
      assert.equal(b.check(R, t).retryInMs, expected);
      t += expected;
    }
  });

  it("a neutral trial (offline) releases the trial slot", () => {
    const b = new CircuitBreaker();
    fail(b, R, T0, BREAKER_THRESHOLD);
    const later = T0 + BREAKER_OPEN_MS;
    b.check(R, later); b.record(R, later, "neutral");
    assert.equal(b.check(R, later).open, false, "a new trial may go");
  });

  it("honours a longer Retry-After, capped at the max", () => {
    const b = new CircuitBreaker();
    fail(b, R, T0, BREAKER_THRESHOLD - 1);
    fail(b, R, T0, 1, 120_000);
    assert.equal(b.check(R, T0).retryInMs, 120_000);
    const c = new CircuitBreaker();
    fail(c, R, T0, BREAKER_THRESHOLD - 1);
    fail(c, R, T0, 1, 60 * 60_000);
    assert.equal(c.check(R, T0).retryInMs, BREAKER_MAX_OPEN_MS);
  });

  it("turns the 8 Oct flood (200 calls/s, all 503) into a few calls a minute", () => {
    const b = new CircuitBreaker();
    let sent = 0;
    for (let t = T0; t < T0 + 10 * 60_000; t += 5) {
      if (!b.check(R, t).open) { sent += 1; b.record(R, t, "overload"); }
    }
    // Without the breaker: 120,000 requests in 10 minutes.
    assert.ok(sent <= 10, `sent=${sent}`);
  });
});

describe("timeout", () => {
  it("is just above API Gateway's 30 s limit, so no valid answer is cut short", () => {
    assert.ok(REQUEST_TIMEOUT_MS > 30_000 && REQUEST_TIMEOUT_MS <= 40_000);
  });
});

describe("development call counter", () => {
  it("summarises busiest first and flags probable loops", () => {
    const counts = new Map([["GET /me", 3], [R, CALL_COUNTER_WARN_AT + 1], ["GET /recordings", 1]]);
    const { line, hot } = summarizeCalls(counts);
    assert.equal(line, `${CALL_COUNTER_WARN_AT + 1}× ${R} | 3× GET /me | 1× GET /recordings`);
    assert.deepEqual(hot, [`${CALL_COUNTER_WARN_AT + 1}× ${R}`]);
  });

  it("normal traffic is not flagged", () => {
    assert.deepEqual(summarizeCalls(new Map([["GET /me", CALL_COUNTER_WARN_AT]])).hot, []);
  });
});

describe("request() wiring (lib/api.ts)", () => {
  const api = read("lib/api.ts");
  const body = api.slice(api.indexOf("async function request<T>"));
  const fn = body.slice(0, body.indexOf("\n}\n"));

  it("refuses an authenticated call with no token BEFORE the network, without clearToken", () => {
    const guard = fn.indexOf('if (!token) throw new ApiError(401');
    assert.ok(guard > 0, "no-token guard present");
    assert.ok(guard < fn.indexOf("fetch("), "before fetch");
    const line = fn.slice(guard, fn.indexOf("\n", guard));
    assert.doesNotMatch(line, /clearToken/);
    assert.match(line, /"no_session"/);
  });

  it("checks the breaker before fetch, with a timeout signal on the fetch", () => {
    assert.ok(fn.indexOf("breaker.check(") < fn.indexOf("fetch("));
    assert.match(fn, /signal: controller\.signal/);
    assert.match(fn, /setTimeout\(\(\) => controller\.abort\(\), REQUEST_TIMEOUT_MS\)/);
    assert.match(fn, /finally \{\s*clearTimeout\(timer\);/);
  });

  it("reports every outcome to the breaker: timeout, offline, and the status", () => {
    assert.match(fn, /aborted\) \{\s*breaker\.record\(route, Date\.now\(\), "overload"\)/);
    assert.match(fn, /breaker\.record\(route, Date\.now\(\), "neutral"\);\s*throw new ApiError\(0/);
    assert.match(fn, /isOverloadStatus\(resp\.status\) \? "overload"/);
    assert.match(fn, /retryAfterMs\(resp\.headers\.get\("retry-after"\)/);
  });

  it("a real 401 from the server still ends the session", () => {
    assert.match(fn, /resp\.status === 401 && auth && sessionExpiryOn401\) await clearToken\(\)/);
  });

  it("the call counter is development-only", () => {
    assert.match(api, /const COUNT_CALLS = typeof __DEV__ !== "undefined" && __DEV__;/);
    assert.match(api, /function countCall\(route: string\): void \{\s*if \(!COUNT_CALLS\) return;/);
  });

  it("saveToken announces the sign-in; one failing listener cannot block it", () => {
    const save = api.slice(api.indexOf("export async function saveToken"));
    const b = save.slice(0, save.indexOf("\n}\n"));
    assert.ok(b.indexOf("store.setItemAsync(TOKEN_KEY") < b.indexOf("signInHandlers"), "token saved first");
    assert.match(b, /try \{ await fn\(\); \} catch/);
  });
});

describe("providers above the auth gate make no call while signed out", () => {
  for (const [file, call] of [["lib/integrations.tsx", "refresh"], ["lib/notification-center.tsx", "refreshCount"]]) {
    it(`${file}: loads only with a token, again on sign-in, and clears on sign-out`, () => {
      const src = read(file);
      assert.match(src, new RegExp(`getToken\\(\\)\\.then\\(\\(t\\) => \\{ if \\(t\\) void ${call}\\(\\)`));
      assert.match(src, new RegExp(`onSignIn\\(\\(\\) => \\{ void ${call}\\(\\); \\}\\)`));
      assert.match(src, /const offOut = onSignOut\(/);
      assert.match(src, /return \(\) => \{ offIn\(\); offOut\(\); \};/);
      assert.doesNotMatch(src, new RegExp(`useEffect\\(\\(\\) => \\{ ${call}\\(\\); \\}`), "no unconditional mount fetch");
    });
  }
});

describe("offline: edits need a connection (no queue, no guessing)", () => {
  it("refuses writes only when the phone is KNOWN to be offline", () => {
    for (const m of ["POST", "PUT", "PATCH", "DELETE", "post"]) {
      assert.equal(refusedOffline(m, true), true, m);
      assert.equal(refusedOffline(m, false), false, m);
    }
  });

  it("never refuses a read: the cache answers it", () => {
    assert.equal(refusedOffline("GET", true), false);
    assert.equal(refusedOffline("", true), false, "a missing method is a GET");
  });

  const api = read("lib/api.ts");
  const fn = api.slice(api.indexOf("async function request<T>"));

  it("request() refuses before the network and before the breaker, with one message", () => {
    const at = fn.indexOf("if (refusedOffline(method, knownOffline))");
    assert.ok(at > 0);
    assert.ok(at < fn.indexOf("fetch(") && at < fn.indexOf("breaker.check("));
    assert.match(fn, /throw new ApiError\(0, OFFLINE_WRITE_MESSAGE, "offline"\)/);
    assert.match(api, /export const OFFLINE_WRITE_MESSAGE = "You're offline\. This change needs a connection\.";/);
  });

  it("the flag starts false and only NetInfo sets it (api.ts imports no native module)", () => {
    assert.match(api, /let knownOffline = false;/);
    assert.doesNotMatch(api, /(from|require\()\s*["']@react-native-community\/netinfo["']/);
    const qc = read("lib/query-client.tsx");
    assert.match(qc, /const online = s\.isConnected !== false;\s*setKnownOffline\(!online\);\s*setOnline\(online\);/);
  });

  it("one banner for the whole app, drawn over the screens, never blocking a tap", () => {
    const layout = read("src/app/_layout.tsx");
    assert.ok(layout.indexOf("<OfflineBanner />") > layout.indexOf("</Stack>"));
    const banner = read("lib/offline-banner.tsx");
    assert.match(banner, /if \(online\) return null;/);
    assert.match(banner, /pointerEvents="none"/);
    assert.match(banner, /changes need a connection/);
  });

  it("the Desk's Move to Trash waits for the server before touching the saved list", () => {
    const desk = read("src/app/(tabs)/index.tsx");
    const body = desk.slice(desk.indexOf("const confirmDelete = useCallback"));
    const handler = body.slice(0, body.indexOf("}, []);"));
    assert.ok(handler.indexOf("await trashRecording(key);") < handler.indexOf("dropMeetingFromLists(key);"));
    assert.doesNotMatch(handler, /setQueryData/, "no optimistic removal");
  });
});
