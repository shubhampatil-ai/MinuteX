// lib/notification-center.tsx — the in-app notification centre's state, and
// the ONE place a notification is turned into a destination.
//
// NAMED notification-center, NOT notifications, on purpose: lib/notifications.ts
// already exists and is something else entirely (the Android 13+
// POST_NOTIFICATIONS permission, extracted from the BLE module). That file is
// about the OS letting us show a system notification; this one is about
// MinuteX's own in-app inbox. They are unrelated, and merging them would put
// a permission prompt and an API client in one module.
//
// TWO RESPONSIBILITIES.
//
// 1. NotificationsProvider / useNotifications() — the shared state behind the
//    bell. It is a context for the same reason lib/integrations.tsx is one:
//    the badge lives in the header and the list lives on its own screen, and
//    if each fetched its own count they would disagree the moment one of them
//    marked something read. One source, one count, updated in one place.
//
//    The count is what most of the app reads, so the provider is deliberately
//    cheap: it loads the COUNT on mount (a dedicated endpoint that does not
//    ship notification bodies) and only fetches the list when the centre asks
//    for it.
//
// 2. destinationFor() — the deep-link resolver. A notification carries
//    entity_type + entity_id, and this maps that pair to an existing Expo
//    Router route. It is a pure function so the mapping can be tested without
//    a navigator, and it lives here rather than in the screen so a new
//    notification type is wired in one place.
//
// WHAT THIS FILE DOES NOT DO. It does not decide whether the user may open
// the entity. Tapping navigates to a screen that fetches the entity through
// the normal authenticated route, and THAT route enforces ownership — a
// notification id is never an authorization token. If the underlying task or
// meeting is gone or not theirs, the destination screen shows its own
// not-found state, exactly as it does when reached any other way.
import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef,
  useState,
} from "react";
import {
  AppNotification,
  getNotifications,
  getUnreadNotificationCount,
  markAllNotificationsRead,
  markNotificationRead,
} from "./api";

// One page. Matches the backend default; the centre pages with "Load more"
// rather than loading a user's whole history (which is the requirement's
// performance rule, and also just what a long-lived inbox needs).
export const NOTIFICATIONS_PAGE_SIZE = 20;

// ---------------------------------------------------------------------------
// Deep linking.
// ---------------------------------------------------------------------------
export type NotificationDestination =
  | { pathname: "/task/[id]"; params: { id: string } }
  | { pathname: "/recording/[key]"; params: { key: string } };

/**
 * Where tapping this notification should go, or null when it is not
 * actionable.
 *
 * Driven by entity_type rather than by the notification TYPE, which is what
 * keeps this from growing a branch per type: every task notification opens the
 * task, every meeting notification opens the meeting. A type the app does not
 * recognize still resolves correctly as long as its entity does — which is
 * what lets an older build render a newer backend's notification without a
 * dead tap.
 *
 * `document` maps to the MEETING deliberately: documents are not
 * independently addressable in MinuteX — they are read inside the meeting
 * workspace — so the entity_id on those rows IS the recording key. The
 * document type travels in metadata for the screen to scroll to.
 *
 * Returns null (not a fallback route) for an empty or unknown entity: sending
 * someone to a plausible-looking wrong screen is worse than a row that simply
 * does not navigate.
 */
export function destinationFor(
  n: Pick<AppNotification, "entity_type" | "entity_id">
): NotificationDestination | null {
  const id = (n.entity_id ?? "").trim();
  if (!id) return null;
  switch (n.entity_type) {
    case "task":
      return { pathname: "/task/[id]", params: { id } };
    case "meeting":
    case "document":
      return { pathname: "/recording/[key]", params: { key: id } };
    default:
      return null;
  }
}

/** True when tapping this notification goes somewhere. Used to decide whether
 *  a row renders as pressable — a non-actionable row must not look tappable. */
export function isActionable(n: AppNotification): boolean {
  return destinationFor(n) !== null;
}

// ---------------------------------------------------------------------------
// State.
// ---------------------------------------------------------------------------
type NotificationsState = {
  unreadCount: number;
  notifications: AppNotification[];
  /** First load of the LIST (the centre's skeleton). The badge does not use
   *  this — it must never make the header flicker on every refresh. */
  loading: boolean;
  loadingMore: boolean;
  error: string;
  hasMore: boolean;
  /** Re-read the badge count only. Cheap; safe to call on focus. */
  refreshCount: () => Promise<void>;
  /** Load page one of the list (and the count with it). */
  refresh: () => Promise<void>;
  loadMore: () => Promise<void>;
  markRead: (notificationId: string) => Promise<void>;
  markAllRead: () => Promise<void>;
};

const NotificationsContext = createContext<NotificationsState | null>(null);

export function NotificationsProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const [unreadCount, setUnreadCount] = useState(0);
  const [notifications, setNotifications] = useState<AppNotification[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState("");
  const [cursor, setCursor] = useState("");

  // Guards a setState after unmount, and makes a slow response that resolves
  // after a newer one harmless. Same pattern as IntegrationsProvider.
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

  const refreshCount = useCallback(async () => {
    try {
      const count = await getUnreadNotificationCount();
      if (alive.current) setUnreadCount(count);
    } catch {
      // Deliberately silent. The badge is ambient: a failed count must not
      // raise an error banner over whatever screen the user is actually on.
      // The previous number stays, which is stale rather than wrong-looking.
    }
  }, []);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const page = await getNotifications({ limit: NOTIFICATIONS_PAGE_SIZE });
      if (!alive.current) return;
      setNotifications(page.notifications);
      setCursor(page.next_cursor);
      // The first page carries the count, so opening the centre costs one
      // request rather than two.
      if (typeof page.unread_count === "number") {
        setUnreadCount(page.unread_count);
      }
    } catch (e: any) {
      if (alive.current) setError(e?.message ?? "Couldn’t load notifications.");
    } finally {
      if (alive.current) setLoading(false);
    }
  }, []);

  const loadMore = useCallback(async () => {
    if (!cursor || loadingMore) return;
    setLoadingMore(true);
    try {
      const page = await getNotifications({
        limit: NOTIFICATIONS_PAGE_SIZE,
        cursor,
      });
      if (!alive.current) return;
      // Concatenated, and de-duplicated by id: a notification created between
      // two page reads shifts the window, and without this the same row could
      // appear twice.
      setNotifications((prev) => {
        const seen = new Set(prev.map((n) => n.notification_id));
        return [
          ...prev,
          ...page.notifications.filter((n) => !seen.has(n.notification_id)),
        ];
      });
      setCursor(page.next_cursor);
    } catch (e: any) {
      if (alive.current) setError(e?.message ?? "Couldn’t load more.");
    } finally {
      if (alive.current) setLoadingMore(false);
    }
  }, [cursor, loadingMore]);

  const markRead = useCallback(async (notificationId: string) => {
    // Optimistic, because this runs while the user is being navigated away to
    // the entity — waiting for the round trip would leave the row looking
    // unread for the whole transition. The server is authoritative and its
    // real count replaces the guess below.
    let wasUnread = false;
    setNotifications((prev) =>
      prev.map((n) => {
        if (n.notification_id !== notificationId || n.is_read) return n;
        wasUnread = true;
        return { ...n, is_read: true };
      })
    );
    if (wasUnread) setUnreadCount((c) => Math.max(0, c - 1));

    try {
      const res = await markNotificationRead(notificationId);
      if (alive.current) setUnreadCount(res.unread_count);
    } catch {
      // Re-read rather than rolling back to a guess: the optimistic update may
      // still be correct (another device may have marked it), and the count
      // endpoint settles it either way.
      await refreshCount();
    }
  }, [refreshCount]);

  const markAllRead = useCallback(async () => {
    const snapshot = notifications;
    setNotifications((prev) => prev.map((n) => ({ ...n, is_read: true })));
    setUnreadCount(0);
    try {
      const res = await markAllNotificationsRead();
      if (!alive.current) return;
      setUnreadCount(res.unread_count);
      // A backlog bigger than one call's ceiling leaves rows unread. Rather
      // than looping blindly, re-read the list so what the user sees matches
      // the server — and the count above already tells them work remains.
      if (res.remaining > 0) await refresh();
    } catch (e: any) {
      if (!alive.current) return;
      // A failed mark-all is worth SAYING, unlike a failed count: the user
      // pressed a button and it did not happen. Restore what was really there.
      setNotifications(snapshot);
      setError(e?.message ?? "Couldn’t mark all as read.");
      await refreshCount();
    }
  }, [notifications, refresh, refreshCount]);

  // The badge is loaded once at mount. It is refreshed on focus by the screens
  // that show it, rather than polled here — a timer in a context would keep
  // firing on every screen in the app for a number most of them never show.
  useEffect(() => { refreshCount(); }, [refreshCount]);

  const value = useMemo<NotificationsState>(
    () => ({
      unreadCount, notifications, loading, loadingMore, error,
      hasMore: !!cursor,
      refreshCount, refresh, loadMore, markRead, markAllRead,
    }),
    [unreadCount, notifications, loading, loadingMore, error, cursor,
     refreshCount, refresh, loadMore, markRead, markAllRead]
  );

  return (
    <NotificationsContext.Provider value={value}>
      {children}
    </NotificationsContext.Provider>
  );
}

export function useNotifications(): NotificationsState {
  const ctx = useContext(NotificationsContext);
  if (!ctx) {
    throw new Error(
      "useNotifications must be used inside NotificationsProvider"
    );
  }
  return ctx;
}

// ---------------------------------------------------------------------------
// Presentation helpers — shared by the bell and the list so an icon or a
// colour role is defined once.
// ---------------------------------------------------------------------------

/** The icon for a notification, chosen by TYPE (not entity), because the icon
 *  is what tells "ready" apart from "failed" at a glance. Unknown types get
 *  the neutral bell rather than nothing. */
export function iconForNotification(type: string): string {
  switch (type) {
    case "MEETING_PROCESSING_COMPLETED":
    case "AI_OUTPUT_READY":
      return "checkmark.circle.fill";
    case "MEETING_PROCESSING_FAILED":
      return "exclamationmark.triangle.fill";
    case "AI_ACTION_REQUIRED":
      return "sparkles";
    case "TASK_ASSIGNED":
    case "TASK_REASSIGNED":
      return "checkmark.circle";
    case "TASK_DUE_TODAY":
      return "calendar.badge.clock";
    case "TASK_OVERDUE":
      return "exclamationmark.triangle.fill";
    case "MEETING_DOCUMENT_READY":
      return "doc.text.fill";
    case "MEETING_OUTPUT_SHARED":
      return "square.and.arrow.up";
    default:
      return "bell.fill";
  }
}

/** Which theme colour role a notification reads in. Returns a KEY into the
 *  palette rather than a colour, so light/dark both work and the theme stays
 *  the only place colours are defined. */
export type NotificationTone = "danger" | "warn" | "success" | "accent" | "primary";

export function toneForNotification(type: string): NotificationTone {
  switch (type) {
    case "MEETING_PROCESSING_FAILED":
    case "TASK_OVERDUE":
      return "danger";
    case "TASK_DUE_TODAY":
      return "warn";
    case "MEETING_PROCESSING_COMPLETED":
      return "success";
    case "AI_ACTION_REQUIRED":
    case "AI_OUTPUT_READY":
      return "accent";
    default:
      return "primary";
  }
}

// ---------------------------------------------------------------------------
// Grouping + relative time.
// ---------------------------------------------------------------------------
export type NotificationGroup = {
  title: "Today" | "Yesterday" | "Earlier";
  items: AppNotification[];
};

/**
 * Bucket a page into Today / Yesterday / Earlier.
 *
 * Compared on the LOCAL calendar day, not on elapsed hours: "yesterday" means
 * the day before today to a person, so something from 20:00 last night is
 * Yesterday at 09:00 even though it is only 13 hours old. Empty groups are
 * dropped so the list never renders a header with nothing under it.
 *
 * Order within a group is preserved — the API already returns newest first,
 * and re-sorting here would let the list disagree with the pagination cursor.
 */
export function groupNotifications(
  items: AppNotification[],
  now: Date = new Date()
): NotificationGroup[] {
  const startOfToday = new Date(
    now.getFullYear(), now.getMonth(), now.getDate()
  ).getTime();
  const startOfYesterday = startOfToday - 24 * 60 * 60 * 1000;

  const today: AppNotification[] = [];
  const yesterday: AppNotification[] = [];
  const earlier: AppNotification[] = [];

  for (const n of items) {
    const t = Date.parse(n.created_at);
    // An unparseable timestamp goes to Earlier rather than crashing or being
    // dropped: the notification is real even if its clock string is odd, and
    // silently hiding it would be worse than filing it conservatively.
    if (!Number.isFinite(t)) earlier.push(n);
    else if (t >= startOfToday) today.push(n);
    else if (t >= startOfYesterday) yesterday.push(n);
    else earlier.push(n);
  }

  const groups: NotificationGroup[] = [];
  if (today.length) groups.push({ title: "Today", items: today });
  if (yesterday.length) groups.push({ title: "Yesterday", items: yesterday });
  if (earlier.length) groups.push({ title: "Earlier", items: earlier });
  return groups;
}

/** "10 min ago" / "2 hr ago" / "Yesterday" / "12 Aug". Short by design — it
 *  sits at the end of a row and must never wrap. */
export function relativeTime(iso: string, now: Date = new Date()): string {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "";
  const diffMs = now.getTime() - t;
  if (diffMs < 0) return "Just now";        // clock skew, not the future
  const mins = Math.floor(diffMs / 60000);
  if (mins < 1) return "Just now";
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs} hr ago`;
  const then = new Date(t);
  const startOfToday = new Date(
    now.getFullYear(), now.getMonth(), now.getDate()
  ).getTime();
  if (t >= startOfToday - 24 * 60 * 60 * 1000) return "Yesterday";
  return then.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}
