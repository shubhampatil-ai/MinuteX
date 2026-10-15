// lib/__tests__/push-logic.test.mjs — permission flow, tap routing, foreground.
//
// Run:  node --test lib/__tests__/push-logic.test.mjs
//
// The rules that decide whether MinuteX asks for notification permission,
// where a tapped "Brief ready" goes, and whether a push that arrives while
// the app is open is shown. Imported directly (Node strips the types).
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  ANDROID_CHANNEL_ID, PendingTap, briefHref, briefTargetFrom, classifyPermission,
  foregroundPresentation, nextPermissionStep, shouldOfferPrompt,
  REGISTRATION_REFRESH_MS, shouldRegister,
  REGISTRATION_BACKOFF_BASE_MS, REGISTRATION_BACKOFF_MAX_MS, isNewDeviceToken,
  nextRegistrationFailure, registrationAllowed, registrationBackoffMs,
} from "../push-logic.ts";

const ROOT = new URL("../../", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const read = (p) => readFileSync(join(ROOT, p), "utf8");
const KEY = "recordings/u-1/mobile/mobile-abc_1754300000.m4a";
const DATA = { type: "brief_ready", audio_s3_key: KEY, notification_id: "n-1" };

describe("permission", () => {
  it("classifies the OS states the UI branches on", () => {
    assert.equal(classifyPermission({ status: "granted", granted: true }), "granted");
    assert.equal(classifyPermission({ status: "undetermined", canAskAgain: true }), "undetermined");
    assert.equal(classifyPermission({ status: "denied", canAskAgain: true }), "denied");
    assert.equal(classifyPermission({ status: "denied", canAskAgain: false }), "blocked");
    assert.equal(classifyPermission(null), "unavailable");
  });

  it("iOS provisional delivery counts as granted", () => {
    assert.equal(classifyPermission({ status: "denied", ios: { status: 3 } }), "granted");
  });

  it("explains before the first OS dialog, and never re-asks a hard deny", () => {
    assert.equal(nextPermissionStep("undetermined"), "explain");
    assert.equal(nextPermissionStep("denied"), "request");
    assert.equal(nextPermissionStep("blocked"), "settings");
    assert.equal(nextPermissionStep("granted"), "register");
    assert.equal(nextPermissionStep("unavailable"), "none");
  });

  it("offers the contextual prompt once, and only before the OS has been asked", () => {
    assert.equal(shouldOfferPrompt({ alreadyOffered: false, permission: "undetermined" }), true);
    assert.equal(shouldOfferPrompt({ alreadyOffered: true, permission: "undetermined" }), false);
    for (const p of ["granted", "denied", "blocked", "unavailable"]) {
      assert.equal(shouldOfferPrompt({ alreadyOffered: false, permission: p }), false, p);
    }
  });
});

describe("tap → the existing brief route", () => {
  it("opens /recording/[key] with the recording key", () => {
    const t = briefTargetFrom(DATA);
    assert.deepEqual(t, { key: KEY, notificationId: "n-1" });
    assert.deepEqual(briefHref(t), { pathname: "/recording/[key]", params: { key: KEY } });
  });

  it("ignores anything that is not a well-formed brief_ready payload", () => {
    for (const bad of [
      null, "x", {}, { type: "other", audio_s3_key: KEY },
      { type: "brief_ready" }, { type: "brief_ready", audio_s3_key: "" },
      { type: "brief_ready", audio_s3_key: "../../etc" },
      { type: "brief_ready", audio_s3_key: "https://evil.example/x" },
      { type: "brief_ready", audio_s3_key: 42 },
    ]) {
      assert.equal(briefTargetFrom(bad), null, JSON.stringify(bad));
    }
  });

  it("holds a tap until navigation is ready, then hands it over exactly once", () => {
    const p = new PendingTap();
    assert.equal(p.offer("resp-1", DATA), true);
    assert.equal(p.take(false), null, "not ready: still pending");
    assert.deepEqual(p.take(true), { key: KEY, notificationId: "n-1" });
    assert.equal(p.take(true), null, "consumed");
  });

  it("the same response (cold start + listener) is handled once", () => {
    const p = new PendingTap();
    assert.equal(p.offer("resp-1", DATA), true);
    p.take(true);
    assert.equal(p.offer("resp-1", DATA), false);
    assert.equal(p.take(true), null);
  });
});

describe("foreground", () => {
  it("does not present a push for the brief the user is already on", () => {
    const f = foregroundPresentation(DATA, `/recording/${encodeURIComponent(KEY)}`);
    assert.deepEqual(f, { banner: false, list: false, sound: false });
  });

  it("shows it silently anywhere else", () => {
    assert.deepEqual(foregroundPresentation(DATA, "/"), { banner: true, list: true, sound: false });
  });
});

describe("wiring", () => {
  const push = read("lib/push.ts");
  const layout = read("src/app/_layout.tsx");

  it("the Android channel matches the backend", () => {
    assert.equal(ANDROID_CHANNEL_ID, "briefs");
    assert.match(read("../cloud/shared/push_delivery.py"), /ANDROID_CHANNEL_ID = "briefs"/);
  });

  it("expo-notifications is loaded behind a native-module check (old binaries must not crash)", () => {
    assert.match(push, /requireOptionalNativeModule\("ExpoPushTokenManager"\)/);
    assert.doesNotMatch(push, /^import .* from "expo-notifications"/m);
  });

  it("the project id comes from the build's Expo config, never a literal", () => {
    assert.match(push, /projectId/);
    assert.doesNotMatch(push, /1540993d-e521-42ff-b54a-7adb4ea808cc/);
  });

  it("the root layout mounts the bridge and gates it on a signed-in, onboarded user", () => {
    assert.match(layout, /<PushBridge\b/);
    assert.match(layout, /<PushBridge ready=\{pushReady\} \/>/);
    assert.match(layout, /const pushReady = authed && needsName === false/);
    // ...and NOT while the auth/onboarding gate could still replace the stack.
    assert.match(layout, /!PUBLIC_SEGMENTS\.includes\(segments\[0\]/);
  });

  it("the Settings toggle drives the server-side preference", () => {
    const settings = read("src/app/settings.tsx");
    assert.match(settings, /Brief-ready notifications/);
    assert.match(settings, /setBriefReadyEnabled/);
  });

  it("sign-out removes this installation's token", () => {
    assert.match(read("src/app/(tabs)/profile.tsx"), /unregisterThisDevice\(\)/);
  });
});

describe("registration refresh never loops", () => {
  const T = "ExponentPushToken[abc]";
  const NOW = 1_800_000_000_000;

  it("the Settings switch always registers", () => {
    assert.equal(shouldRegister({ explicit: true, token: T, last: { token: T, at: NOW }, now: NOW }), true);
  });

  it("a new or first token registers", () => {
    assert.equal(shouldRegister({ explicit: false, token: T, last: null, now: NOW }), true);
    assert.equal(shouldRegister({ explicit: false, token: T, last: { token: "old", at: NOW }, now: NOW }), true);
  });

  it("the same token is not re-sent until the refresh interval has passed", () => {
    const last = { token: T, at: NOW };
    assert.equal(shouldRegister({ explicit: false, token: T, last, now: NOW + 1000 }), false);
    assert.equal(shouldRegister({ explicit: false, token: T, last, now: NOW + REGISTRATION_REFRESH_MS }), true);
  });

  it("push.ts runs one sync at a time, checks before posting, and forgets on every sign-out", () => {
    const push = readFileSync(join(ROOT, "lib/push.ts"), "utf8");
    assert.match(push, /if \(opts\.enabled === undefined && syncInFlight\) return syncInFlight;/);
    const body = push.slice(push.indexOf("async function syncOnce"));
    assert.ok(body.indexOf("shouldRegister(") > 0
              && body.indexOf("shouldRegister(") < body.indexOf("registerPushToken("),
              "the check comes before the POST");
    const signOut = push.slice(push.indexOf("onSignOut(async () => {"));
    const handler = signOut.slice(0, signOut.indexOf("});"));
    assert.match(handler, /store\.deleteItemAsync\(REGISTERED_KEY\)/);
    assert.match(handler, /store\.deleteItemAsync\(FAILED_KEY\)/, "the backoff does not pass to the next account");
    assert.match(handler, /lastDeviceToken = ""/);
  });
});

describe("failed registration backs off (bounded, jittered)", () => {
  const NOW = 1_800_000_000_000;

  it("doubles from 1 minute, half fixed and half random", () => {
    assert.equal(registrationBackoffMs(1, 0), REGISTRATION_BACKOFF_BASE_MS / 2);
    assert.equal(registrationBackoffMs(1, 0.999999), REGISTRATION_BACKOFF_BASE_MS);
    assert.equal(registrationBackoffMs(2, 0), REGISTRATION_BACKOFF_BASE_MS);
    assert.equal(registrationBackoffMs(3, 0), 2 * REGISTRATION_BACKOFF_BASE_MS);
  });

  it("never waits longer than the cap, however many failures", () => {
    for (const n of [10, 20, 50, 1000, Number.MAX_SAFE_INTEGER]) {
      assert.ok(registrationBackoffMs(n, 1) <= REGISTRATION_BACKOFF_MAX_MS, String(n));
      assert.ok(registrationBackoffMs(n, 0) >= REGISTRATION_BACKOFF_MAX_MS / 2, String(n));
    }
  });

  it("treats nonsense input as the first failure, and clamps the random", () => {
    assert.equal(registrationBackoffMs(0, 0), REGISTRATION_BACKOFF_BASE_MS / 2);
    assert.equal(registrationBackoffMs(-3, -1), REGISTRATION_BACKOFF_BASE_MS / 2);
    assert.equal(registrationBackoffMs(1, 5), REGISTRATION_BACKOFF_BASE_MS);
  });

  it("counts consecutive failures and schedules the retry", () => {
    const f1 = nextRegistrationFailure(null, NOW, 0);
    assert.deepEqual(f1, { failures: 1, retryAt: NOW + REGISTRATION_BACKOFF_BASE_MS / 2 });
    const f2 = nextRegistrationFailure(f1, NOW, 0);
    assert.equal(f2.failures, 2);
    assert.equal(f2.retryAt, NOW + REGISTRATION_BACKOFF_BASE_MS);
  });

  it("a silent sync waits for the retry time; the Settings switch never waits", () => {
    const failure = { failures: 1, retryAt: NOW + 60_000 };
    assert.equal(registrationAllowed({ explicit: false, failure, now: NOW }), false);
    assert.equal(registrationAllowed({ explicit: false, failure, now: NOW + 60_000 }), true);
    assert.equal(registrationAllowed({ explicit: true, failure, now: NOW }), true);
    assert.equal(registrationAllowed({ explicit: false, failure: null, now: NOW }), true);
  });

  it("a clock set far back cannot block registration for longer than the cap", () => {
    const failure = { failures: 1, retryAt: NOW + REGISTRATION_BACKOFF_MAX_MS * 10 };
    assert.equal(registrationAllowed({ explicit: false, failure, now: NOW }), true);
  });

  it("a 10-minute outage costs at most a handful of attempts, not thousands", () => {
    // Simulate the worst case: the OS fires a token event every 10 ms (the
    // old loop rate) and every POST fails. Count the POSTs allowed.
    let failure = null, attempts = 0;
    for (let t = NOW; t < NOW + 10 * 60_000; t += 10) {
      if (registrationAllowed({ explicit: false, failure, now: t })) {
        attempts += 1;
        failure = nextRegistrationFailure(failure, t, 0.5);
      }
    }
    assert.ok(attempts <= 5, `attempts=${attempts}`);
  });

  it("push.ts checks the backoff BEFORE fetching the token, and records every failure but a 401", () => {
    const push = readFileSync(join(ROOT, "lib/push.ts"), "utf8");
    const body = push.slice(push.indexOf("async function syncOnce"));
    const allowed = body.indexOf("registrationAllowed(");
    assert.ok(allowed > 0 && allowed < body.indexOf("getExpoPushTokenAsync("), "backoff gates the token fetch");
    assert.match(body, /catch \(e\) \{\s*\/\/[\s\S]*?if \(!\(e instanceof ApiError && e\.status === 401\)\) await recordFailure\(\);\s*throw e;/);
    assert.ok(body.indexOf("await clearFailure()") > body.indexOf("registerPushToken("), "success resets the backoff");
  });
});

describe("a repeated device token does not start a sync", () => {
  it("only a token not seen before is news", () => {
    assert.equal(isNewDeviceToken("", "dev-token-1"), true);
    assert.equal(isNewDeviceToken("dev-token-1", "dev-token-1"), false);
    assert.equal(isNewDeviceToken("dev-token-1", "dev-token-2"), true);
  });

  it("an unreadable token is treated as news (still rate-limited downstream)", () => {
    assert.equal(isNewDeviceToken("dev-token-1", undefined), true);
    assert.equal(isNewDeviceToken("dev-token-1", { not: "a string" }), true);
  });

  it("the token listener goes through onDevicePushToken, never straight to syncThisDevice", () => {
    const bridge = readFileSync(join(ROOT, "lib/push-bridge.tsx"), "utf8");
    const listener = bridge.slice(bridge.indexOf("addPushTokenListener("));
    const cb = listener.slice(0, listener.indexOf("});"));
    assert.match(cb, /onDevicePushToken\(t\?\.data\)/);
    assert.doesNotMatch(cb, /syncThisDevice\(/);
    const push = readFileSync(join(ROOT, "lib/push.ts"), "utf8");
    const fn = push.slice(push.indexOf("export function onDevicePushToken"));
    assert.ok(fn.indexOf("isNewDeviceToken(") < fn.indexOf("syncThisDevice("), "dedupe before sync");
  });
});
