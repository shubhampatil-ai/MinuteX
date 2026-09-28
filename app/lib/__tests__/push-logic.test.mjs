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
