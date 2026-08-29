// lib/__tests__/notification-center.test.mjs — the notification centre's
// pure logic, and the source-level rules that keep it coherent.
//
// Run:  node --test lib/__tests__/notification-center.test.mjs
//
// WHAT THIS PINS, AND WHY EACH PART IS A TEST RATHER THAN JUST CODE.
//
//   1. DEEP LINKING. destinationFor() is the map from a notification to a
//      screen. Getting it wrong is invisible in a diff and only shows up as a
//      tap that lands on the wrong thing — or worse, on a plausible-looking
//      WRONG meeting. The interesting cases are not the happy ones: an empty
//      entity id, and an entity type this build does not know (which a NEWER
//      backend can legitimately send), must both resolve to "not actionable"
//      rather than to a guess.
//
//   2. GROUPING + RELATIVE TIME. Both are calendar logic, and calendar logic
//      is where off-by-one-day bugs live. "Yesterday" is a DAY boundary, not
//      an elapsed-hours window — 20:00 last night is Yesterday at 09:00 even
//      though it is 13 hours old — and an empty group must never render a
//      header with nothing under it.
//
//   3. SOURCE-LEVEL LINT. Two architectural rules that nothing else enforces:
//      no screen may fetch notification state for itself (the badge and the
//      list would disagree the moment either marked something read), and the
//      notification engine must never be coupled to Gmail. Same approach, and
//      same reasoning, as gmail-integration.test.mjs and
//      keyboard-avoidance.test.mjs.
//
// The modules under test are TypeScript/TSX, so the pure logic is mirrored
// here rather than imported — matching the convention in task-insights.test.mjs
// and mom-model.test.mjs (node --test runs .mjs with no transpile step).
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const ROOT = new URL("../../", import.meta.url).pathname
  .replace(/^\/([A-Za-z]:)/, "$1");

// --- mirrored from lib/notification-center.tsx -----------------------------

/** The resolver: entity_type + entity_id -> a route, or null. */
function destinationFor(n) {
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

function groupNotifications(items, now = new Date()) {
  const startOfToday = new Date(
    now.getFullYear(), now.getMonth(), now.getDate()
  ).getTime();
  const startOfYesterday = startOfToday - 24 * 60 * 60 * 1000;

  const today = [], yesterday = [], earlier = [];
  for (const n of items) {
    const t = Date.parse(n.created_at);
    if (!Number.isFinite(t)) earlier.push(n);
    else if (t >= startOfToday) today.push(n);
    else if (t >= startOfYesterday) yesterday.push(n);
    else earlier.push(n);
  }
  const groups = [];
  if (today.length) groups.push({ title: "Today", items: today });
  if (yesterday.length) groups.push({ title: "Yesterday", items: yesterday });
  if (earlier.length) groups.push({ title: "Earlier", items: earlier });
  return groups;
}

function relativeTime(iso, now = new Date()) {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "";
  const diffMs = now.getTime() - t;
  if (diffMs < 0) return "Just now";
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

/** An ISO timestamp `days` before / `hours` before the given LOCAL moment.
 *  Used instead of fixed UTC literals so these tests assert the calendar rule
 *  rather than the test machine's timezone offset. */
function localIso(base, { days = 0, hours = 0, minutes = 0 } = {}) {
  const d = new Date(
    base.getFullYear(), base.getMonth(), base.getDate() - days,
    base.getHours() - hours, base.getMinutes() - minutes
  );
  return d.toISOString();
}

/** Local midday on a fixed calendar day — a stable "now" with room on either
 *  side of it, so no case sits on a day boundary by accident. */
const NOW = new Date(2026, 7, 29, 12, 0, 0);

const notification = (over = {}) => ({
  notification_id: "n-1",
  type: "TASK_ASSIGNED",
  title: "New task assigned to you",
  message: "Complete API integration",
  priority: "HIGH",
  entity_type: "task",
  entity_id: "task-1",
  is_read: false,
  read_at: "",
  created_at: "2026-08-29T10:00:00Z",
  metadata: {},
  channels: ["IN_APP"],
  ...over,
});

// ---------------------------------------------------------------------------
describe("deep linking", () => {
  it("opens a task notification on Task Details", () => {
    assert.deepEqual(destinationFor(notification()), {
      pathname: "/task/[id]",
      params: { id: "task-1" },
    });
  });

  it("opens a meeting notification on the meeting", () => {
    const key = "recordings/u-1/mobile/abc_123.m4a";
    assert.deepEqual(
      destinationFor(notification({ entity_type: "meeting", entity_id: key })),
      { pathname: "/recording/[key]", params: { key } }
    );
  });

  it("opens a document notification on its MEETING", () => {
    // Documents are read inside the meeting workspace and are not
    // independently addressable, so the entity_id on those rows IS the
    // recording key. Routing them anywhere else would be a dead tap.
    const key = "recordings/u-1/mobile/abc_123.m4a";
    assert.deepEqual(
      destinationFor(notification({ entity_type: "document", entity_id: key })),
      { pathname: "/recording/[key]", params: { key } }
    );
  });

  it("every task notification type resolves to the task screen", () => {
    for (const type of ["TASK_ASSIGNED", "TASK_REASSIGNED", "TASK_DUE_TODAY",
                        "TASK_OVERDUE", "AI_ACTION_REQUIRED"]) {
      const to = destinationFor(notification({ type, entity_type: "task" }));
      assert.equal(to?.pathname, "/task/[id]", type);
    }
  });

  it("is not actionable when the entity id is missing", () => {
    // Better a row that does not navigate than one that navigates nowhere.
    assert.equal(destinationFor(notification({ entity_id: "" })), null);
    assert.equal(destinationFor(notification({ entity_id: "   " })), null);
  });

  it("is not actionable for an entity type this build does not know", () => {
    // A NEWER backend can legitimately send one. It must degrade to a
    // non-tappable row, never to a guessed destination.
    assert.equal(destinationFor(notification({ entity_type: "invoice" })), null);
  });

  it("resolves an unknown TYPE as long as the entity is known", () => {
    // The resolver keys off entity_type, not type — which is exactly what
    // lets an older build render a newer backend's notification correctly.
    const to = destinationFor(
      notification({ type: "SOMETHING_NEW", entity_type: "task" })
    );
    assert.deepEqual(to, { pathname: "/task/[id]", params: { id: "task-1" } });
  });
});

describe("grouping", () => {
  const now = NOW;

  it("buckets into Today / Yesterday / Earlier", () => {
    const groups = groupNotifications([
      notification({ notification_id: "a", created_at: localIso(now, { hours: 3 }) }),
      notification({ notification_id: "b", created_at: localIso(now, { days: 1 }) }),
      notification({ notification_id: "c", created_at: localIso(now, { days: 28 }) }),
    ], now);
    assert.deepEqual(groups.map((g) => g.title),
                     ["Today", "Yesterday", "Earlier"]);
    assert.deepEqual(groups.map((g) => g.items.length), [1, 1, 1]);
  });

  it("omits empty groups entirely", () => {
    // A header with nothing under it looks like a rendering bug.
    const groups = groupNotifications(
      [notification({ created_at: localIso(now, { hours: 3 }) })], now);
    assert.deepEqual(groups.map((g) => g.title), ["Today"]);
  });

  it("returns nothing for an empty inbox", () => {
    assert.deepEqual(groupNotifications([], now), []);
  });

  it("treats Yesterday as a DAY, not as 24 elapsed hours", () => {
    // 22:00 last night is 14 hours ago but belongs under Yesterday.
    const lateLastNight = new Date(
      now.getFullYear(), now.getMonth(), now.getDate() - 1, 22, 0);
    const groups = groupNotifications(
      [notification({ created_at: lateLastNight.toISOString() })], now);
    assert.deepEqual(groups.map((g) => g.title), ["Yesterday"]);
  });

  it("preserves the API's newest-first order within a group", () => {
    // Re-sorting here would let the list disagree with the pagination cursor.
    const groups = groupNotifications([
      notification({ notification_id: "newer", created_at: localIso(now, { hours: 1 }) }),
      notification({ notification_id: "older", created_at: localIso(now, { hours: 4 }) }),
    ], now);
    assert.deepEqual(groups[0].items.map((n) => n.notification_id),
                     ["newer", "older"]);
  });

  it("files an unparseable timestamp under Earlier rather than dropping it", () => {
    // The notification is real even if its clock string is odd; hiding it
    // would be worse than filing it conservatively.
    const groups = groupNotifications(
      [notification({ created_at: "not-a-date" })], now);
    assert.deepEqual(groups.map((g) => g.title), ["Earlier"]);
  });
});

describe("relative time", () => {
  const now = NOW;

  it("renders minutes, hours, yesterday and a date", () => {
    assert.equal(relativeTime(localIso(now, { minutes: 10 }), now), "10 min ago");
    assert.equal(relativeTime(localIso(now, { hours: 2 }), now), "2 hr ago");
    // 26 hours back is both "more than a day" and the previous local day.
    assert.equal(relativeTime(localIso(now, { hours: 26 }), now), "Yesterday");
    assert.match(relativeTime(localIso(now, { days: 28 }), now), /Aug|Jul/);
  });

  it("says Just now under a minute", () => {
    assert.equal(relativeTime(localIso(now, { minutes: 0 }), now), "Just now");
  });

  it("never renders a future timestamp as negative", () => {
    // Device clock skew is real; "in -3 min" is not something to ship.
    assert.equal(relativeTime(localIso(now, { minutes: -5 }), now), "Just now");
  });

  it("returns empty rather than NaN for an unparseable timestamp", () => {
    assert.equal(relativeTime("nonsense", now), "");
  });
});

// ---------------------------------------------------------------------------
// SOURCE-LEVEL LINT.
// ---------------------------------------------------------------------------
function sourceFiles() {
  const out = [];
  for (const dir of [join(ROOT, "lib"), join(ROOT, "src")]) {
    const walk = (d) => {
      for (const entry of readdirSync(d)) {
        if (entry === "node_modules" || entry === "__tests__") continue;
        const full = join(d, entry);
        if (statSync(full).isDirectory()) walk(full);
        else if (/\.(ts|tsx)$/.test(entry)) out.push(full);
      }
    };
    walk(dir);
  }
  return out;
}

describe("architecture", () => {
  it("no screen fetches notification state for itself", () => {
    // The badge lives in the header and the list lives on its own screen. If
    // either fetched its own copy they would disagree the moment one of them
    // marked something read — which is exactly the bug the context prevents.
    // Only the context and the API client may touch these calls.
    const allowed = new Set([
      join("lib", "notification-center.tsx"),
      join("lib", "api.ts"),
    ]);
    const calls = /\b(getNotifications|getUnreadNotificationCount|markNotificationRead|markAllNotificationsRead)\s*\(/;

    const offenders = sourceFiles()
      .filter((f) => !allowed.has(relative(ROOT, f)))
      .filter((f) => calls.test(readFileSync(f, "utf8")))
      .map((f) => relative(ROOT, f));

    assert.deepEqual(
      offenders, [],
      `these must read notification state through useNotifications(): ${offenders.join(", ")}`
    );
  });

  it("the notification centre is not coupled to Gmail", () => {
    // The architectural constraint: notifications are IN-APP, and Gmail stays
    // a separate, user-triggered communication integration. Nothing enforces
    // that except this — and by the time someone "helpfully" wires an email
    // into the centre, the coupling would be load-bearing.
    const src = readFileSync(join(ROOT, "lib", "notification-center.tsx"), "utf8")
      + readFileSync(join(ROOT, "lib", "notification-bell.tsx"), "utf8")
      + readFileSync(join(ROOT, "src", "app", "notifications.tsx"), "utf8");

    for (const forbidden of [/\bgmail\b/i, /sendMeetingEmail/, /sendTaskEmail/,
                             /sendGmailEmail/, /useGmail/, /whatsapp/i]) {
      assert.ok(!forbidden.test(src),
                `the notification centre must not reference ${forbidden}`);
    }
  });

  it("the bell and the centre agree on where notifications live", () => {
    // One route. A second path would silently create a second entry point
    // that the badge does not track.
    const bell = readFileSync(join(ROOT, "lib", "notification-bell.tsx"), "utf8");
    assert.match(bell, /router\.push\(\s*["'`]\/notifications["'`]/);
  });

  it("the notifications screen is registered in the root layout", () => {
    // An unregistered route renders without the app's header styling and
    // cannot be pushed to by name.
    const layout = readFileSync(join(ROOT, "src", "app", "_layout.tsx"), "utf8");
    assert.match(layout, /Stack\.Screen\s+name="notifications"/);
    assert.match(layout, /<NotificationsProvider>/);
  });

  it("the OS permission helper is left alone", () => {
    // lib/notifications.ts is the Android POST_NOTIFICATIONS permission and
    // has nothing to do with the in-app inbox. Keeping them separate is why
    // the new module is named notification-center.
    const perms = readFileSync(join(ROOT, "lib", "notifications.ts"), "utf8");
    assert.ok(!/getNotifications|unread|NotificationsProvider/.test(perms),
              "lib/notifications.ts must stay the OS permission helper only");
  });
});
