// lib/push-bridge.tsx — where OS push meets navigation and the in-app centre.
//
// Mounted ONCE, in the root layout, inside NotificationsProvider. Renders
// nothing. Four jobs:
//
//   1. TAP → BRIEF. A tapped "Brief ready" opens the EXISTING detail route
//      (/recording/[key]) — the same screen the Desk list opens. Covers all
//      three app states:
//        closed      getLastNotificationResponse() on mount (cold start)
//        background  the response listener
//        open        the response listener
//   2. NOTHING IS LOST WHILE THE APP CANNOT NAVIGATE. A cold-start tap usually
//      arrives before the navigator is mounted, often while the auth gate is
//      still deciding, sometimes on the login screen. The target is held in a
//      PendingTap and consumed only when `ready` (signed in, past onboarding,
//      in the app proper) and the root navigator exists.
//   3. ONE NOTIFICATION, NOT TWO. Tapping a push marks the in-app row it
//      delivered as read (data.notification_id), and a push arriving while the
//      app is open refreshes the bell — so the OS notification and the centre
//      never disagree about what the user has seen.
//   4. KEEP THE REGISTRATION FRESH. Every entry into the app re-registers this
//      installation (token rotation, last_seen_at, and — server-side — the
//      opportunistic receipt check). The server keeps the user's switch.
import { useCallback, useEffect, useRef } from "react";
import { usePathname, useRootNavigationState, useRouter } from "expo-router";
import { useNotifications } from "./notification-center";
import { Notifications, configureForegroundHandler, onDevicePushToken, syncThisDevice } from "./push";
import { PendingTap, briefHref } from "./push-logic";

export function PushBridge({ ready }: { ready: boolean }) {
  const router = useRouter();
  const pathname = usePathname();
  const navState = useRootNavigationState();
  const { refreshCount, markRead } = useNotifications();

  const pathRef = useRef(pathname);
  useEffect(() => { pathRef.current = pathname; }, [pathname]);

  const canNavigate = ready && !!navState?.key;
  const canNavigateRef = useRef(canNavigate);
  useEffect(() => { canNavigateRef.current = canNavigate; }, [canNavigate]);

  const pending = useRef<PendingTap | null>(null);
  const tapQueue = useCallback(() => {
    if (!pending.current) pending.current = new PendingTap();
    return pending.current;
  }, []);

  const drain = useCallback(() => {
    const target = tapQueue().take(canNavigateRef.current);
    if (!target) return;
    router.push(briefHref(target));
    if (target.notificationId) {
      markRead(target.notificationId).catch(() => { /* the bell re-syncs on focus */ });
    }
    // So the same response is not replayed as a "cold start" tap on the next
    // mount of this component.
    try { Notifications?.clearLastNotificationResponse(); } catch { /* older module */ }
  }, [router, markRead, tapQueue]);

  // (3, foreground) presentation rule + bell refresh.
  useEffect(() => {
    if (!Notifications) return;
    configureForegroundHandler(() => pathRef.current);
    const received = Notifications.addNotificationReceivedListener(() => {
      refreshCount().catch(() => { /* best-effort */ });
    });
    return () => received.remove();
  }, [refreshCount]);

  // (1) taps, from every app state.
  useEffect(() => {
    if (!Notifications) return;
    const N = Notifications;
    const handle = (r: import("expo-notifications").NotificationResponse | null | undefined) => {
      if (!r || r.actionIdentifier !== N.DEFAULT_ACTION_IDENTIFIER) return;
      if (tapQueue().offer(r.notification.request.identifier, r.notification.request.content.data)) {
        drain();
      }
    };
    try { handle(N.getLastNotificationResponse()); } catch { /* none */ }
    const sub = N.addNotificationResponseReceivedListener(handle);
    return () => sub.remove();
  }, [drain, tapQueue]);

  // (2) a held tap goes the moment navigation becomes possible.
  useEffect(() => {
    if (canNavigate) drain();
  }, [canNavigate, drain]);

  // (4) registration refresh on entry, and whenever the OS rotates the token.
  useEffect(() => {
    if (!ready || !Notifications) return;
    void syncThisDevice().catch(() => { /* offline or server down: backed off (push.ts) */ });
    // Only a token not seen before syncs: iOS re-reports the SAME token after
    // every fetch, and reacting to that was the registration loop.
    const sub = Notifications.addPushTokenListener((t) => {
      void onDevicePushToken(t?.data).catch(() => { /* same */ });
    });
    return () => sub.remove();
  }, [ready]);

  return null;
}
