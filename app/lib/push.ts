// lib/push.ts — OS push notifications for this installation.
//
// WHAT THIS OWNS. Getting this device an Expo push token, telling the MinuteX
// backend about it (lib/api.ts registerPushToken), the brief-ready switch for
// this device, the Android channel, and the foreground presentation rule.
// Tap routing lives in lib/push-bridge.tsx; every decision lives in
// lib/push-logic.ts, which is pure and tested.
//
// WHAT IT DOES NOT DO. It never sends a notification. Sending happens only on
// the backend (cloud/shared/push_delivery.py), after the brief is persisted.
//
// OLD BINARIES. expo-notifications calls requireNativeModule at import time,
// so importing it into a build that predates this feature would crash on
// launch. It is therefore required lazily, and only after
// requireOptionalNativeModule confirms the native module is in the binary —
// the same guard lib/audio-focus.ts uses. Without it every function here
// reports "unavailable" and the app behaves exactly as before.
import { Alert, Platform } from "react-native";
import Constants from "expo-constants";
import * as Device from "expo-device";
import { requireOptionalNativeModule } from "expo-modules-core";
import { ApiError, onSignOut, registerPushToken, unregisterPushToken, type PushTokenState } from "./api";
import { APP_VERSION } from "./app-info";
import { store } from "./storage";
import {
  ANDROID_CHANNEL_ID, classifyPermission, foregroundPresentation, isNewDeviceToken,
  nextRegistrationFailure, registrationAllowed, shouldOfferPrompt, shouldRegister,
  type PushPermission, type RegistrationFailure,
} from "./push-logic";

type NotificationsModule = typeof import("expo-notifications");

function load(): NotificationsModule | null {
  if (Platform.OS === "web") return null;
  if (!requireOptionalNativeModule("ExpoPushTokenManager")) return null;
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    return require("expo-notifications") as NotificationsModule;
  } catch {
    return null;
  }
}

export const Notifications: NotificationsModule | null = load();

const INSTALLATION_KEY = "minutex.push.installationId";
const ENABLED_KEY = "minutex.push.briefReady";          // cached server value
const PROMPTED_KEY = "minutex.push.promptOffered";      // contextual prompt, once
const REGISTERED_KEY = "minutex.push.lastRegistered";   // {token, at}: see shouldRegister
const FAILED_KEY = "minutex.push.lastFailure";          // {failures, retryAt}: see registrationBackoffMs

/** Why push cannot work here, or null when it can. Shown in Settings. */
export function pushUnavailableReason(): string | null {
  if (!Notifications) {
    return Platform.OS === "web"
      ? "Not available on the web."
      : "Needs the latest app update.";
  }
  if (!Device.isDevice) return "Needs a physical device.";
  if (!projectId()) return "Not configured for this build.";
  return null;
}

function projectId(): string {
  // The EAS project id app.json already carries (extra.eas.projectId). Read
  // from the BUILD's config, so a fork or a staging project never has to edit
  // this file.
  const fromConfig = (Constants.expoConfig?.extra as { eas?: { projectId?: string } } | undefined)
    ?.eas?.projectId;
  return fromConfig || Constants.easConfig?.projectId || "";
}

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------
let channelReady = false;

/**
 * Android 8+ needs a channel before anything is shown, and on Android 13+ the
 * POST_NOTIFICATIONS prompt only appears once a channel exists — so this runs
 * before any permission request. DEFAULT importance: it makes a sound and
 * appears in the shade, but does not pop over whatever the user is doing (that
 * is HIGH, reserved for calls and alarms).
 */
export async function ensureAndroidChannel(): Promise<void> {
  if (!Notifications || Platform.OS !== "android" || channelReady) return;
  await Notifications.setNotificationChannelAsync(ANDROID_CHANNEL_ID, {
    name: "Briefs",
    description: "When a meeting brief is ready to review",
    importance: Notifications.AndroidImportance.DEFAULT,
  });
  channelReady = true;
}

/**
 * How a push is presented while the app is OPEN. `currentPath` is read at the
 * moment the push arrives (see lib/push-bridge.tsx), so a brief-ready push for
 * the brief already on screen is not shown on top of it.
 */
export function configureForegroundHandler(currentPath: () => string): void {
  if (!Notifications) return;
  Notifications.setNotificationHandler({
    handleNotification: async (n) => {
      const f = foregroundPresentation(n.request.content.data, currentPath());
      return {
        shouldShowBanner: f.banner,
        shouldShowList: f.list,
        shouldPlaySound: f.sound,
        shouldSetBadge: false,
      };
    },
  });
}

// ---------------------------------------------------------------------------
// Permission
// ---------------------------------------------------------------------------
export async function getPushPermission(): Promise<PushPermission> {
  if (!Notifications || pushUnavailableReason()) return "unavailable";
  try {
    return classifyPermission(await Notifications.getPermissionsAsync());
  } catch {
    return "unavailable";
  }
}

/** Show the OS dialog. Callers show MinuteX's own explanation FIRST. */
export async function requestPushPermission(): Promise<PushPermission> {
  if (!Notifications || pushUnavailableReason()) return "unavailable";
  try {
    await ensureAndroidChannel();
    return classifyPermission(await Notifications.requestPermissionsAsync({
      ios: { allowAlert: true, allowSound: true, allowBadge: false },
    }));
  } catch {
    return "unavailable";
  }
}

export async function wasPromptOffered(): Promise<boolean> {
  try {
    return (await store.getItemAsync(PROMPTED_KEY)) === "1";
  } catch {
    return true;   // storage unreadable: err on the side of not nagging
  }
}

export async function markPromptOffered(): Promise<void> {
  try { await store.setItemAsync(PROMPTED_KEY, "1"); } catch { /* best-effort */ }
}

// ---------------------------------------------------------------------------
// Registration
// ---------------------------------------------------------------------------
async function installationId(): Promise<string> {
  const existing = await store.getItemAsync(INSTALLATION_KEY);
  if (existing) return existing;
  // Random, app-generated, never derived from hardware ids. It identifies
  // THIS INSTALL so a rotated push token replaces its predecessor server-side.
  const bytes = Array.from({ length: 16 }, () => Math.floor(Math.random() * 256));
  const id = "inst-" + bytes.map((b) => b.toString(16).padStart(2, "0")).join("");
  await store.setItemAsync(INSTALLATION_KEY, id);
  return id;
}

/** The last server answer for this device's switch, for a first paint that
 *  does not wait on the network. null = unknown. */
export async function cachedBriefReadyEnabled(): Promise<boolean | null> {
  try {
    const v = await store.getItemAsync(ENABLED_KEY);
    return v === "1" ? true : v === "0" ? false : null;
  } catch {
    return null;
  }
}

async function cacheEnabled(v: boolean) {
  try { await store.setItemAsync(ENABLED_KEY, v ? "1" : "0"); } catch { /* best-effort */ }
}

/**
 * Register (or refresh) this installation with the backend.
 *
 * Needs permission to be granted: without it there is nothing to deliver to,
 * so nothing is registered. `enabled` omitted = keep the server's value (the
 * every-app-start refresh); a boolean = the user just flipped the switch.
 *
 * Throws on a backend error so the Settings toggle can show it; the silent
 * app-start refresh catches and ignores.
 */
export async function syncThisDevice(opts: { enabled?: boolean } = {}): Promise<PushTokenState | null> {
  // ONE sync at a time: a silent refresh arriving while one is running joins
  // it. The Settings switch (an explicit `enabled`) waits for it, then runs.
  if (opts.enabled === undefined && syncInFlight) return syncInFlight;
  if (syncInFlight) { try { await syncInFlight; } catch { /* its own caller saw it */ } }
  const run = syncOnce(opts);
  syncInFlight = run;
  try { return await run; } finally { if (syncInFlight === run) syncInFlight = null; }
}

let syncInFlight: Promise<PushTokenState | null> | null = null;

// EVERY sign-out (also a 401's clearToken, which skips unregisterThisDevice):
// the next account on this phone must register even with the same token, and
// must not inherit the previous account's backoff.
onSignOut(async () => {
  failureMemo = null;
  lastDeviceToken = "";
  try { await store.deleteItemAsync(REGISTERED_KEY); } catch { /* best-effort */ }
  try { await store.deleteItemAsync(FAILED_KEY); } catch { /* best-effort */ }
});

async function lastRegistered(): Promise<{ token: string; at: number } | null> {
  try {
    const v = JSON.parse((await store.getItemAsync(REGISTERED_KEY)) || "null");
    return v && typeof v.token === "string" && typeof v.at === "number" ? v : null;
  } catch {
    return null;
  }
}

// The backoff lives in memory AND in the store: memory so it holds even when
// the store is unavailable (lib/storage.ts), the store so a relaunch does not
// start the count again.
let failureMemo: RegistrationFailure | null = null;

async function lastFailure(): Promise<RegistrationFailure | null> {
  if (failureMemo) return failureMemo;
  try {
    const v = JSON.parse((await store.getItemAsync(FAILED_KEY)) || "null");
    failureMemo = v && typeof v.failures === "number" && typeof v.retryAt === "number" ? v : null;
  } catch {
    failureMemo = null;
  }
  return failureMemo;
}

async function recordFailure(): Promise<void> {
  failureMemo = nextRegistrationFailure(await lastFailure(), Date.now(), Math.random());
  try { await store.setItemAsync(FAILED_KEY, JSON.stringify(failureMemo)); } catch { /* memory still holds */ }
}

async function clearFailure(): Promise<void> {
  failureMemo = null;
  try { await store.deleteItemAsync(FAILED_KEY); } catch { /* best-effort */ }
}

async function syncOnce(opts: { enabled?: boolean }): Promise<PushTokenState | null> {
  if (!Notifications || pushUnavailableReason()) return null;
  if ((await getPushPermission()) !== "granted") return null;
  const explicit = opts.enabled !== undefined;
  // Checked BEFORE the token fetch: while backing off, a silent sync does
  // nothing at all — no Expo token call (which is itself a network call, and
  // on iOS re-fires the token listener), no POST.
  if (!registrationAllowed({ explicit, failure: await lastFailure(), now: Date.now() })) return null;
  let state: PushTokenState;
  let token: string;
  try {
    await ensureAndroidChannel();
    token = (await Notifications.getExpoPushTokenAsync({ projectId: projectId() })).data;
    // Fetching the token above can itself fire addPushTokenListener on iOS;
    // without this check that re-entered here forever (see shouldRegister).
    if (!shouldRegister({ explicit, token, last: await lastRegistered(), now: Date.now() })) {
      return null;
    }
    state = await registerPushToken({
      token,
      installation_id: await installationId(),
      platform: Platform.OS === "ios" ? "ios" : "android",
      app_version: APP_VERSION,
      ...(opts.enabled === undefined ? {} : { enabled: opts.enabled }),
    });
  } catch (e) {
    // A 401 has already signed the user out (request() → clearToken), which
    // wiped this state; recording a failure now would hand the backoff to
    // whoever signs in next.
    if (!(e instanceof ApiError && e.status === 401)) await recordFailure();
    throw e;
  }
  await clearFailure();
  await cacheEnabled(state.enabled);
  try { await store.setItemAsync(REGISTERED_KEY, JSON.stringify({ token, at: Date.now() })); } catch { /* best-effort */ }
  return state;
}

// The device token the OS last reported to the listener in this run.
let lastDeviceToken = "";

/**
 * addPushTokenListener's handler. Syncs only for a token not seen before in
 * this run (see isNewDeviceToken): the OS re-reporting the SAME token after
 * our own getExpoPushTokenAsync is not news, and acting on it was the loop.
 */
export function onDevicePushToken(reported: unknown): Promise<PushTokenState | null> {
  if (!isNewDeviceToken(lastDeviceToken, reported)) return Promise.resolve(null);
  if (typeof reported === "string") lastDeviceToken = reported;
  return syncThisDevice();
}

/**
 * The Settings switch. Turning it OFF keeps the token but tells the server not
 * to send; turning it ON registers with enabled=true. The caller has already
 * handled permission (see nextPermissionStep).
 */
export async function setBriefReadyEnabled(enabled: boolean): Promise<PushTokenState | null> {
  return syncThisDevice({ enabled });
}

/**
 * Sign-out: stop this device receiving the account's notifications. Must run
 * while the JWT is still valid (before clearToken). Best-effort — a failure
 * is not a reason to keep someone signed in; the next account to sign in on
 * this device takes the token over server-side anyway.
 */
export async function unregisterThisDevice(): Promise<void> {
  try {
    const id = await store.getItemAsync(INSTALLATION_KEY);
    if (id) await unregisterPushToken(id);
  } catch {
    /* see above */
  }
  try { await store.deleteItemAsync(ENABLED_KEY); } catch { /* best-effort */ }
  // The next account on this phone must register even with the same token.
  try { await store.deleteItemAsync(REGISTERED_KEY); } catch { /* best-effort */ }
}

// ---------------------------------------------------------------------------
// The contextual prompt
// ---------------------------------------------------------------------------
/** MinuteX's own explanation, shown BEFORE the OS dialog (Settings uses it too). */
export const BRIEF_READY_PROMPT = {
  title: "Get notified when your meeting brief is ready.",
  body: "MinuteX will send one notification when a brief has been written — nothing else.",
  enable: "Enable notifications",
  later: "Not now",
} as const;

/**
 * Offer brief-ready notifications ONCE, at the moment they become relevant:
 * the user's first recording has just uploaded, and a brief is minutes away.
 * Never on first launch, never again after "Not now" (Settings remains the way
 * back), and never when the OS has already been asked — shouldOfferPrompt.
 */
export async function offerBriefReadyPrompt(): Promise<void> {
  if (await wasPromptOffered()) return;
  const perm = await getPushPermission();
  if (!shouldOfferPrompt({ alreadyOffered: false, permission: perm })) {
    // Already decided one way or the other (or impossible here): never offer.
    if (perm !== "unavailable") await markPromptOffered();
    return;
  }
  // Recorded BEFORE the dialog, so a crash or force-quit on it cannot turn
  // "once" into "every upload".
  await markPromptOffered();
  Alert.alert(BRIEF_READY_PROMPT.title, BRIEF_READY_PROMPT.body, [
    { text: BRIEF_READY_PROMPT.later, style: "cancel" },
    {
      text: BRIEF_READY_PROMPT.enable,
      onPress: () => {
        void (async () => {
          if ((await requestPushPermission()) === "granted") {
            await setBriefReadyEnabled(true).catch(() => { /* Settings can retry */ });
          }
        })();
      },
    },
  ]);
}
