// lib/push-logic.ts — the decisions behind OS push, with no native code.
//
// PURE (no React Native, no expo-notifications) so every rule is pinned by
// lib/__tests__/push-logic.test.mjs. lib/push.ts does the native work and
// asks this file what to do.

/** The payload the backend sends (cloud/shared/push_delivery.py). */
export const BRIEF_READY_TYPE = "brief_ready";

/** Must match push_delivery.ANDROID_CHANNEL_ID, or Android files the
 *  notification under a channel the user never configured. */
export const ANDROID_CHANNEL_ID = "briefs";

export type PushPermission =
  | "granted"
  | "undetermined"      // never asked: we may show our explanation, then ask
  | "denied"            // asked and refused, but the OS would ask again
  | "blocked"           // refused for good: only system Settings can fix it
  | "unavailable";      // no native module, simulator, Expo Go, no project id

/**
 * The OS permission, reduced to the five states the UI branches on.
 * `status`/`canAskAgain` are expo-notifications' NotificationPermissionsStatus.
 * iOS "provisional" counts as granted: notifications are delivered (quietly).
 */
export function classifyPermission(p: {
  status?: string;
  granted?: boolean;
  canAskAgain?: boolean;
  ios?: { status?: number } | null;
} | null | undefined): PushPermission {
  if (!p) return "unavailable";
  // IosAuthorizationStatus.PROVISIONAL = 3, EPHEMERAL = 4.
  const iosStatus = p.ios?.status;
  if (p.granted || p.status === "granted" || iosStatus === 3 || iosStatus === 4) return "granted";
  if (p.status === "undetermined") return "undetermined";
  return p.canAskAgain === false ? "blocked" : "denied";
}

/**
 * What tapping "turn on brief-ready notifications" should do next.
 *   explain   show MinuteX's own explanation, then the OS dialog
 *   request   show the OS dialog directly (Android re-ask after a soft deny)
 *   settings  the OS will not ask again — offer the system Settings screen
 *   register  permission is already there — just register with the server
 *   none      push cannot work on this build/device
 */
export function nextPermissionStep(p: PushPermission):
  "explain" | "request" | "settings" | "register" | "none" {
  switch (p) {
    case "granted": return "register";
    case "undetermined": return "explain";
    case "denied": return "request";
    case "blocked": return "settings";
    default: return "none";
  }
}

/**
 * Whether to offer the contextual "get notified when your brief is ready"
 * prompt without being asked. Once ever, only while the OS has never been
 * asked, and only where push can actually work. Asking on first launch, or
 * again after a "Not now", is the pattern that trains people to deny.
 */
export function shouldOfferPrompt(s: {
  alreadyOffered: boolean;
  permission: PushPermission;
}): boolean {
  return !s.alreadyOffered && s.permission === "undetermined";
}

/** The existing detail route a brief-ready push opens, or null. */
export type BriefTarget = { key: string; notificationId: string };

/**
 * Validate a notification's data and extract where to go. Anything that is
 * not a well-formed brief_ready payload opens nothing: a push is untrusted
 * input from the app's point of view, and a malformed one must not navigate
 * somewhere arbitrary. Ownership is re-checked by the detail screen's own API
 * read, so a valid key the user cannot access lands on its normal error state.
 */
export function briefTargetFrom(data: unknown): BriefTarget | null {
  if (!data || typeof data !== "object") return null;
  const d = data as Record<string, unknown>;
  if (d.type !== BRIEF_READY_TYPE) return null;
  const key = typeof d.audio_s3_key === "string" ? d.audio_s3_key.trim() : "";
  // Recording keys are S3 keys under recordings/ (or legacy device keys).
  // Bounded, no traversal, no scheme — it is only ever a route parameter.
  if (!key || key.length > 1024 || key.includes("..") || /^[a-z]+:/i.test(key)) return null;
  const notificationId = typeof d.notification_id === "string" ? d.notification_id : "";
  return { key, notificationId };
}

/** The expo-router href for a target — the SAME route the Desk list uses. */
export function briefHref(t: BriefTarget) {
  return { pathname: "/recording/[key]" as const, params: { key: t.key } };
}

/**
 * Foreground behaviour. A brief-ready push that arrives while the user is
 * ALREADY on that brief is redundant — the screen is about to show it — so
 * it is not presented. Anywhere else in the app it is shown as a banner
 * (silently: the user is looking at the phone), and it is always added to the
 * notification list so it is findable later.
 */
export function foregroundPresentation(data: unknown, currentPath: string): {
  banner: boolean; list: boolean; sound: boolean;
} {
  const t = briefTargetFrom(data);
  if (!t) return { banner: true, list: true, sound: false };
  const onThatBrief = decodePath(currentPath) === `/recording/${t.key}`;
  return { banner: !onThatBrief, list: !onThatBrief, sound: false };
}

function decodePath(p: string): string {
  try {
    return decodeURIComponent(p || "");
  } catch {
    return p || "";
  }
}

/**
 * Pending-tap bookkeeping. A tap can arrive before the app can navigate
 * (cold start, signed out, onboarding gate). The target is held, not lost,
 * and consumed exactly once when navigation is ready.
 */
export class PendingTap {
  private target: BriefTarget | null = null;
  private handled = new Set<string>();

  /** Offer a notification response. Returns true if it was new. */
  offer(responseId: string, data: unknown): boolean {
    if (!responseId || this.handled.has(responseId)) return false;
    const t = briefTargetFrom(data);
    this.handled.add(responseId);
    if (!t) return false;
    this.target = t;
    return true;
  }

  /** Take the target if navigation is ready; otherwise leave it pending. */
  take(ready: boolean): BriefTarget | null {
    if (!ready || !this.target) return null;
    const t = this.target;
    this.target = null;
    return t;
  }

  peek(): BriefTarget | null {
    return this.target;
  }
}
