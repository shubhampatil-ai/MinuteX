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
import { registerPushToken, unregisterPushToken, type PushTokenState } from "./api";
import { APP_VERSION } from "./app-info";
import { store } from "./storage";
import {
  ANDROID_CHANNEL_ID, classifyPermission, foregroundPresentation, shouldOfferPrompt,
  type PushPermission,
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
  if (!Notifications || pushUnavailableReason()) return null;
  if ((await getPushPermission()) !== "granted") return null;
  await ensureAndroidChannel();
  const { data: token } = await Notifications.getExpoPushTokenAsync({ projectId: projectId() });
  const state = await registerPushToken({
    token,
    installation_id: await installationId(),
    platform: Platform.OS === "ios" ? "ios" : "android",
    app_version: APP_VERSION,
    ...(opts.enabled === undefined ? {} : { enabled: opts.enabled }),
  });
  await cacheEnabled(state.enabled);
  return state;
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
