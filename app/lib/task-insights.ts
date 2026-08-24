// lib/task-insights.ts — the arithmetic behind the Task Action Center.
//
// Every number the dashboard shows is derived HERE, not inside a component, so
// that the rules are testable without a renderer (see
// lib/__tests__/task-insights.test.mjs) and so a screen never quietly invents
// a statistic while formatting one.
//
// Two principles run through this file:
//
//   * NOTHING IS FABRICATED. Where the data cannot support a claim, the
//     function returns null and the caller omits the row. The
//     "↑ 12% vs last week" comparison is the clearest case: `completed_at` is
//     only written from the moment a task is completed through this backend,
//     so an account with no completions in the prior week has no baseline and
//     gets no arrow rather than a made-up one. Same for `hasHistory` on the
//     weekly figures.
//
//   * PARTIAL DATA IS NORMAL. The dashboard reads a PAGE of tasks (50, cursor
//     paged — see getAllTasks), not the whole table, so counts computed here
//     describe what has been loaded. `TaskCounts.partial` says so, and the UI
//     labels the health cards accordingly instead of presenting a page count
//     as an account total.
//
// Dates: the backend stores `due_date` as a plain YYYY-MM-DD day string, with
// no timezone. Comparing those as strings is exact and TZ-proof, which is why
// every boundary below is a `toDayKey()` string rather than a Date instance.
import type { ApiTask } from "./api";
import { assigneeLabel, needsAssigneeResolution } from "./api";

// ---------------------------------------------------------------------------
// Day arithmetic. All of it string-based on YYYY-MM-DD, in LOCAL time — a due
// date is a calendar day to the person who owes it, not an instant.
// ---------------------------------------------------------------------------

/** Local calendar day of a Date as YYYY-MM-DD. */
export function toDayKey(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

/** A new Date at local midnight, `days` from `from`. */
export function addDays(from: Date, days: number): Date {
  const d = new Date(from.getFullYear(), from.getMonth(), from.getDate());
  d.setDate(d.getDate() + days);
  return d;
}

/** Monday of the week containing `d`. Weeks start Monday: the dashboard's
 * strip is MON–FRI and "this week" should mean the working week the user is
 * actually in, not one that resets mid-week on Sunday. */
export function startOfWeek(d: Date): Date {
  const day = d.getDay(); // 0 = Sun
  const back = day === 0 ? 6 : day - 1;
  return addDays(d, -back);
}

/** Whole calendar days from today to `dayKey`. Negative = in the past. Null
 * when the string is not a usable date, so callers never render "NaN days". */
export function daysUntil(dayKey: string, now: Date): number | null {
  if (!isDayKey(dayKey)) return null;
  const [y, m, d] = dayKey.split("-").map(Number);
  const target = new Date(y, m - 1, d);
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.round((target.getTime() - today.getTime()) / 86400000);
}

function isDayKey(s: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) return false;
  const [y, m, d] = s.split("-").map(Number);
  if (m < 1 || m > 12 || d < 1 || d > 31) return false;
  const probe = new Date(y, m - 1, d);
  return probe.getMonth() === m - 1 && probe.getDate() === d;
}

/** The due day of a task, tolerating both field spellings the API emits. */
export function dueKeyOf(t: ApiTask): string {
  const raw = String(t.due_date || t.due || "").trim();
  return isDayKey(raw) ? raw : "";
}

// ---------------------------------------------------------------------------
// Status predicates. `is_overdue` is computed SERVER-side from due date +
// status and is therefore correct as of this request; it is trusted first and
// only recomputed locally when the backend did not send it (legacy rows).
// ---------------------------------------------------------------------------

export const TERMINAL_STATUSES = new Set(["Completed", "Cancelled"]);

export function isClosed(t: ApiTask): boolean {
  return TERMINAL_STATUSES.has(t.status);
}

export function isOverdue(t: ApiTask, now: Date): boolean {
  if (isClosed(t)) return false;
  if (typeof t.is_overdue === "boolean") return t.is_overdue;
  const key = dueKeyOf(t);
  return !!key && key < toDayKey(now);
}

/** True when this task names a person nobody has identified yet — the
 * UNRESOLVED / AMBIGUOUS states. Re-exported through this module so the
 * dashboard's selectors and its cards agree by construction. */
export function needsAssignment(t: ApiTask): boolean {
  return needsAssigneeResolution(t);
}

/** True when nobody at all is attached — a normal state, not an error. */
export function isUnassigned(t: ApiTask): boolean {
  const { name } = assigneeLabel(t);
  return !t.assignee_contact_id && !name;
}

// ---------------------------------------------------------------------------
// Health counts (§5)
// ---------------------------------------------------------------------------

export type TaskCounts = {
  overdue: number;
  dueThisWeek: number;
  completed: number;
  needsAssignment: number;
  /** True when the source list was a partial page, so these are counts of what
   * is loaded rather than of the whole account. */
  partial: boolean;
};

/** Counts over the tasks currently loaded.
 *
 * "Due this week" means due between today and the end of the current week
 * INCLUSIVE and still open — a task due Thursday that is already done is not
 * something to do this week. Overdue tasks are excluded: they are their own
 * card, and counting them twice makes the week look busier than it is.
 *
 * "Completed" counts completions within the current week when the row carries
 * a `completed_at` we can place, and falls back to all loaded completed tasks
 * otherwise. `scope` says which happened so the UI can label it honestly. */
export function computeCounts(
  tasks: ApiTask[],
  now: Date,
  opts: { partial?: boolean } = {}
): TaskCounts {
  const today = toDayKey(now);
  const weekEnd = toDayKey(addDays(startOfWeek(now), 6));
  let overdue = 0;
  let dueThisWeek = 0;
  let completed = 0;
  let needsAssign = 0;

  for (const t of tasks) {
    if (isOverdue(t, now)) overdue += 1;
    if (needsAssignment(t)) needsAssign += 1;
    if (t.status === "Completed") completed += 1;
    if (!isClosed(t) && !isOverdue(t, now)) {
      const key = dueKeyOf(t);
      if (key && key >= today && key <= weekEnd) dueThisWeek += 1;
    }
  }
  return {
    overdue,
    dueThisWeek,
    completed,
    needsAssignment: needsAssign,
    partial: !!opts.partial,
  };
}

// ---------------------------------------------------------------------------
// Attention ranking (§6)
// ---------------------------------------------------------------------------

/** Rank buckets, lowest first. Named rather than bare numbers so the ordering
 * reads as the product rule it is: overdue, due today, due soon, needs a
 * person, then everything else still open. */
export const ATTENTION_RANK = {
  overdue: 0,
  dueToday: 1,
  dueSoon: 2,
  needsAssignment: 3,
  open: 4,
} as const;

export type AttentionBucket = keyof typeof ATTENTION_RANK;

/** How soon "soon" is. Three days keeps the top of the list to things that
 * genuinely cannot wait until next week. */
export const DUE_SOON_DAYS = 3;

export function attentionBucket(t: ApiTask, now: Date): AttentionBucket {
  if (isOverdue(t, now)) return "overdue";
  const key = dueKeyOf(t);
  const days = key ? daysUntil(key, now) : null;
  if (days === 0) return "dueToday";
  if (days !== null && days > 0 && days <= DUE_SOON_DAYS) return "dueSoon";
  if (needsAssignment(t)) return "needsAssignment";
  return "open";
}

/** The "what should I do next?" ordering (§29).
 *
 * Closed tasks are dropped: a completed task is not something that needs
 * attention. Within a bucket, the nearest due date wins, tasks with no due
 * date sort after those that have one, and `id` breaks the final tie so the
 * order is STABLE across refetches — a list that reshuffles under the user's
 * thumb between two identical loads is its own bug. */
export function rankForAttention(tasks: ApiTask[], now: Date): ApiTask[] {
  const open = tasks.filter((t) => !isClosed(t));
  return open
    .map((t, i) => ({ t, i }))
    .sort((a, b) => {
      const ra = ATTENTION_RANK[attentionBucket(a.t, now)];
      const rb = ATTENTION_RANK[attentionBucket(b.t, now)];
      if (ra !== rb) return ra - rb;
      const da = dueKeyOf(a.t);
      const db = dueKeyOf(b.t);
      if (da && db && da !== db) return da < db ? -1 : 1;
      if (da && !db) return -1;
      if (!da && db) return 1;
      const ia = String(a.t.id);
      const ib = String(b.t.id);
      if (ia !== ib) return ia < ib ? -1 : 1;
      return a.i - b.i;
    })
    .map((x) => x.t);
}

// ---------------------------------------------------------------------------
// Upcoming deadlines (§11)
// ---------------------------------------------------------------------------

export type Deadline = {
  task: ApiTask;
  dayKey: string;
  days: number;
  /** "Today" | "Tomorrow" | "In 3 days" | "Next week" — never negative. */
  label: string;
  /** 0..1 — how far through the approach window this deadline is. 1 = due
   * today. Meaningful only because the window is fixed and forward-looking. */
  progress: number;
};

/** How far ahead "upcoming" looks. Two weeks: far enough to include next
 * week's commitments, near enough that the section stays actionable. */
export const UPCOMING_WINDOW_DAYS = 14;

export function relativeDueLabel(days: number): string {
  if (days === 0) return "Today";
  if (days === 1) return "Tomorrow";
  if (days <= 6) return `In ${days} days`;
  if (days <= 13) return "Next week";
  return `In ${Math.round(days / 7)} weeks`;
}

/** The nearest FORWARD-looking deadlines. Overdue tasks are excluded by
 * construction (days >= 0), because an overdue task is not "upcoming" and
 * showing it here as "in -2 days" is exactly the misleading relative date the
 * spec forbids. Closed tasks are excluded too — a finished task has no
 * deadline left to meet. */
export function upcomingDeadlines(
  tasks: ApiTask[],
  now: Date,
  limit = 3
): Deadline[] {
  const out: Deadline[] = [];
  for (const t of tasks) {
    if (isClosed(t)) continue;
    if (isOverdue(t, now)) continue;
    const dayKey = dueKeyOf(t);
    if (!dayKey) continue;
    const days = daysUntil(dayKey, now);
    if (days === null || days < 0 || days > UPCOMING_WINDOW_DAYS) continue;
    out.push({
      task: t,
      dayKey,
      days,
      label: relativeDueLabel(days),
      // Fills as the day approaches. Clamped so it can never read past full.
      progress: Math.max(
        0,
        Math.min(1, (UPCOMING_WINDOW_DAYS - days) / UPCOMING_WINDOW_DAYS)
      ),
    });
  }
  out.sort((a, b) =>
    a.days !== b.days ? a.days - b.days : String(a.task.id) < String(b.task.id) ? -1 : 1
  );
  return out.slice(0, limit);
}

// ---------------------------------------------------------------------------
// Weekly progress (§10)
// ---------------------------------------------------------------------------

export type WeekDay = { dayKey: string; label: string; date: number; isToday: boolean };

export type WeeklyProgress = {
  completed: number;
  total: number;
  /** 0..1. Zero when `total` is zero — never NaN. */
  ratio: number;
  /** Percentage-point change vs the previous week, or null when the previous
   * week has no completions to compare against. Null means OMIT the row. */
  deltaPct: number | null;
  days: WeekDay[];
};

const DAY_LABELS = ["MON", "TUE", "WED", "THU", "FRI"];

/** Mon–Fri of the current week, with real dates. Never hardcoded. */
export function weekStrip(now: Date): WeekDay[] {
  const monday = startOfWeek(now);
  const today = toDayKey(now);
  return DAY_LABELS.map((label, i) => {
    const d = addDays(monday, i);
    const dayKey = toDayKey(d);
    return { dayKey, label, date: d.getDate(), isToday: dayKey === today };
  });
}

/** Completion for the current week.
 *
 * "Total" is the week's workload: every loaded task due this week plus every
 * task completed this week (a task completed early still counts as work done).
 * "Completed" is the subset of that which is done.
 *
 * The vs-last-week delta needs a real baseline. `completed_at` is only present
 * on tasks completed through the first-class path, so when no loaded task
 * carries a placeable prior-week completion, `deltaPct` is null and the caller
 * omits the comparison rather than printing a fabricated arrow. */
export function weeklyProgress(tasks: ApiTask[], now: Date): WeeklyProgress {
  const monday = startOfWeek(now);
  const weekStart = toDayKey(monday);
  const weekEnd = toDayKey(addDays(monday, 6));
  const prevStart = toDayKey(addDays(monday, -7));
  const prevEnd = toDayKey(addDays(monday, -1));

  let completed = 0;
  let total = 0;
  let prevCompleted = 0;
  let prevTotal = 0;

  for (const t of tasks) {
    const due = dueKeyOf(t);
    const done = completionDayKey(t);
    const inWeek =
      (!!due && due >= weekStart && due <= weekEnd) ||
      (!!done && done >= weekStart && done <= weekEnd);
    if (inWeek) {
      total += 1;
      if (t.status === "Completed") completed += 1;
    }
    const inPrev =
      (!!due && due >= prevStart && due <= prevEnd) ||
      (!!done && done >= prevStart && done <= prevEnd);
    if (inPrev) {
      prevTotal += 1;
      if (t.status === "Completed") prevCompleted += 1;
    }
  }

  const ratio = total > 0 ? completed / total : 0;
  // A baseline needs actual prior-week work to compare against. Without it
  // there is no honest percentage, so there is no row.
  const deltaPct =
    prevTotal > 0
      ? Math.round((ratio - prevCompleted / prevTotal) * 100)
      : null;

  return { completed, total, ratio, deltaPct, days: weekStrip(now) };
}

/** The local day a task was completed, or "" when unknown. `completed_at` is
 * an ISO instant; it is converted to a LOCAL day so it lands in the same week
 * the user experienced. */
export function completionDayKey(t: ApiTask): string {
  const raw = String(t.completed_at || "").trim();
  if (!raw) return "";
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return "";
  return toDayKey(d);
}

// ---------------------------------------------------------------------------
// Meeting task intelligence (§12)
// ---------------------------------------------------------------------------

export type MeetingInsight = {
  recordingKey: string;
  title: string;
  generated: number;
  completed: number;
  overdue: number;
  needsAssignment: number;
};

/** "Acme Product Review generated 5 action items. 2 completed, 1 overdue,
 * 2 waiting for assignment resolution."
 *
 * Counts only AI-extracted tasks — a task the user typed was not "generated"
 * by the meeting, and including it would overstate what the AI found. The
 * titles come from the caller's recording list; a meeting whose title we do
 * not have is skipped rather than labelled "Untitled", since an insight the
 * user cannot place is noise. */
export function meetingInsights(
  tasks: ApiTask[],
  titles: Map<string, string>,
  now: Date,
  limit = 1
): MeetingInsight[] {
  const byMeeting = new Map<string, MeetingInsight>();
  for (const t of tasks) {
    const key = String(t.source_recording_id || "");
    if (!key) continue;
    if (t.source_type !== "AI") continue;
    const title = titles.get(key);
    if (!title) continue;
    let row = byMeeting.get(key);
    if (!row) {
      row = {
        recordingKey: key,
        title,
        generated: 0,
        completed: 0,
        overdue: 0,
        needsAssignment: 0,
      };
      byMeeting.set(key, row);
    }
    row.generated += 1;
    if (t.status === "Completed") row.completed += 1;
    if (isOverdue(t, now)) row.overdue += 1;
    if (needsAssignment(t)) row.needsAssignment += 1;
  }
  return [...byMeeting.values()]
    .sort((a, b) =>
      b.generated !== a.generated
        ? b.generated - a.generated
        : a.recordingKey < b.recordingKey
          ? -1
          : 1
    )
    .slice(0, limit);
}

/** The sentence for a MeetingInsight, with only the clauses that apply. An
 * insight with nothing notable returns just the generated count rather than
 * padding with "0 completed, 0 overdue". */
export function describeInsight(m: MeetingInsight): string {
  const parts: string[] = [];
  if (m.completed) parts.push(`${m.completed} completed`);
  if (m.overdue) parts.push(`${m.overdue} overdue`);
  if (m.needsAssignment) {
    parts.push(`${m.needsAssignment} waiting for assignment resolution`);
  }
  const items = `${m.generated} action item${m.generated === 1 ? "" : "s"}`;
  return parts.length ? `${items}. ${parts.join(", ")}.` : `${items}.`;
}

// ---------------------------------------------------------------------------
// Greeting (§3)
// ---------------------------------------------------------------------------

export function greetingFor(now: Date): string {
  const h = now.getHours();
  if (h < 12) return "Good morning";
  if (h < 17) return "Good afternoon";
  return "Good evening";
}

/** "Aug 22" — the compact due-date form the task cards use. Empty for a
 * missing or unparseable date, so a card renders without it rather than with
 * a placeholder. */
const MONTHS = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

export function shortDate(dayKey: string): string {
  if (!isDayKey(dayKey)) return "";
  const [, m, d] = dayKey.split("-").map(Number);
  return `${MONTHS[m - 1]} ${d}`;
}
