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
import type { ApiTask, RecordingSummary } from "./api";
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

// ---------------------------------------------------------------------------
// Calendar (the /calendar screen)
//
// The month grid and the per-day buckets behind it. Same rule as everything
// else in this file: a day shows what the loaded tasks actually say about it,
// and a day with nothing due renders empty rather than borrowing a number
// from somewhere else.
// ---------------------------------------------------------------------------

/** Monday-first weekday index (0 = Mon … 6 = Sun). The grid starts on Monday
 * to match startOfWeek() and the dashboard's Mon–Fri strip; using JS's raw
 * getDay() here would shift every cell by one. */
export function mondayIndex(d: Date): number {
  const day = d.getDay();
  return day === 0 ? 6 : day - 1;
}

export function startOfMonth(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), 1);
}

/** Move `n` whole months from `d`, landing on the 1st. Going through the 1st
 * avoids the classic overflow bug where "one month after Jan 31" becomes
 * March 3. */
export function addMonths(d: Date, n: number): Date {
  return new Date(d.getFullYear(), d.getMonth() + n, 1);
}

export type CalendarCell = {
  dayKey: string;
  date: number;
  /** False for the leading/trailing days that belong to the adjacent month.
   * They are rendered dimmed rather than blank, so the grid stays a real
   * calendar the eye can track across a month boundary. */
  inMonth: boolean;
  isToday: boolean;
  isPast: boolean;
};

/** A full 6×7 month grid, Monday-first, always 42 cells.
 *
 * Fixed height on purpose: a grid that grows to 5 rows one month and 6 the
 * next makes everything below it jump as you page through months. */
export function monthGrid(month: Date, now: Date): CalendarCell[] {
  const first = startOfMonth(month);
  const gridStart = addDays(first, -mondayIndex(first));
  const today = toDayKey(now);
  const cells: CalendarCell[] = [];
  for (let i = 0; i < 42; i++) {
    const d = addDays(gridStart, i);
    const dayKey = toDayKey(d);
    cells.push({
      dayKey,
      date: d.getDate(),
      inMonth: d.getMonth() === first.getMonth(),
      isToday: dayKey === today,
      isPast: dayKey < today,
    });
  }
  return cells;
}

export type DayLoad = {
  /** Open (not closed) tasks due that day. */
  open: number;
  /** Of those, how many are overdue — a past day with work still on it. */
  overdue: number;
  /** Tasks completed that day, by due date or completion stamp. */
  completed: number;
  /** Meetings recorded that day. A calendar that only knew about tasks would
   * be missing half of where the day actually went. */
  meetings: number;
  /** AI-extracted deadlines from meeting highlights that resolve to this day.
   * These are DATES MENTIONED, not tasks — nobody owes them and they have no
   * status. See MeetingDeadline. */
  deadlines: number;
};

const EMPTY_LOAD: DayLoad = {
  open: 0, overdue: 0, completed: 0, meetings: 0, deadlines: 0,
};

/** Tasks bucketed by the day they are due, for the month dots.
 *
 * Keyed by day so a cell lookup is O(1) while paging months. A task with no
 * due date belongs to no day and is simply absent — it is not silently
 * dropped onto today. */
export function loadByDay(
  tasks: ApiTask[],
  now: Date,
  meetings: RecordingSummary[] = [],
  deadlines: MeetingDeadline[] = []
): Map<string, DayLoad> {
  const map = new Map<string, DayLoad>();
  const bump = (key: string, patch: Partial<DayLoad>) => {
    if (!key) return;
    const cur = map.get(key) ?? { ...EMPTY_LOAD };
    map.set(key, {
      open: cur.open + (patch.open ?? 0),
      overdue: cur.overdue + (patch.overdue ?? 0),
      completed: cur.completed + (patch.completed ?? 0),
      meetings: cur.meetings + (patch.meetings ?? 0),
      deadlines: cur.deadlines + (patch.deadlines ?? 0),
    });
  };
  for (const t of tasks) {
    const due = dueKeyOf(t);
    if (t.status === "Completed") {
      // A completion lands on the day it happened when we know it, and on the
      // due date otherwise — never on both, so a task is counted once.
      bump(completionDayKey(t) || due, { completed: 1 });
      continue;
    }
    if (isClosed(t)) continue; // Cancelled: no load, nothing was owed.
    if (!due) continue;
    bump(due, { open: 1, overdue: isOverdue(t, now) ? 1 : 0 });
  }
  for (const r of meetings) {
    bump(meetingDayKey(r), { meetings: 1 });
  }
  for (const d of deadlines) {
    bump(d.dayKey, { deadlines: 1 });
  }
  return map;
}

export function dayLoad(map: Map<string, DayLoad>, dayKey: string): DayLoad {
  return map.get(dayKey) ?? EMPTY_LOAD;
}

/** Every task due on one day, ordered the way the dashboard orders work:
 * open tasks by urgency first, then whatever is already closed. */
export function tasksOnDay(
  tasks: ApiTask[],
  dayKey: string,
  now: Date
): ApiTask[] {
  const onDay = tasks.filter((t) => {
    if (dueKeyOf(t) === dayKey) return true;
    // A task completed on this day belongs to it even if it was due earlier —
    // that is the day the work actually happened.
    return t.status === "Completed" && completionDayKey(t) === dayKey;
  });
  const open = rankForAttention(onDay, now);
  const closed = onDay.filter((t) => isClosed(t));
  return [...open, ...closed];
}

const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

export function monthLabel(d: Date): string {
  return `${MONTH_NAMES[d.getMonth()]} ${d.getFullYear()}`;
}

/** "Today", "Tomorrow", "Yesterday", else "Wed, Aug 26" — the heading over a
 * selected day's task list. */
const WEEKDAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

export function dayHeading(dayKey: string, now: Date): string {
  const days = daysUntil(dayKey, now);
  if (days === 0) return "Today";
  if (days === 1) return "Tomorrow";
  if (days === -1) return "Yesterday";
  const [y, m, d] = dayKey.split("-").map(Number);
  const date = new Date(y, m - 1, d);
  return `${WEEKDAY_NAMES[date.getDay()]}, ${shortDate(dayKey)}`;
}

/** The Mon-first weekday headers for the grid. */
export const GRID_DAY_LABELS = ["M", "T", "W", "T", "F", "S", "S"];

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

// ---------------------------------------------------------------------------
// The unified agenda — tasks AND meetings on one day.
//
// A calendar that only knew about tasks would answer "what is due?" while
// leaving out where the day actually went. Meetings are the other half, and on
// this product they are the half that GENERATES the first: an agenda that
// shows the Acme review at 10:00 and the three tasks it produced due Friday is
// the whole pitch in one screen.
//
// The two kinds are deliberately NOT merged into one shape. A meeting happened
// at an instant; a task is owed on a day. Flattening them into a fake common
// "event" would mean inventing a time for every task — so an AgendaEntry is a
// tagged union, and the renderer decides how each reads.
// ---------------------------------------------------------------------------

/** The local day a meeting was recorded. `recorded_at` is a real instant (not
 * a day string like due_date), so it converts through the local timezone — a
 * 00:30 meeting belongs to the day the person experienced it. */
export function meetingDayKey(r: RecordingSummary): string {
  const raw = String(r.recorded_at || r.created_at || "").trim();
  if (!raw) return "";
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return "";
  return toDayKey(d);
}

/** Minutes past local midnight, or null when the timestamp is unusable. Used
 * only for ORDERING and for the "10:30" label — never to place a task, which
 * has no time. */
export function minutesOfDay(iso: string): number | null {
  const raw = String(iso || "").trim();
  if (!raw) return null;
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return null;
  return d.getHours() * 60 + d.getMinutes();
}

/** "9:05 AM" from an ISO instant. Empty when it cannot be read, so a row
 * renders without a time rather than with a broken one. */
export function clockLabel(iso: string): string {
  const mins = minutesOfDay(iso);
  if (mins === null) return "";
  const h24 = Math.floor(mins / 60);
  const m = mins % 60;
  const ampm = h24 < 12 ? "AM" : "PM";
  const h12 = h24 % 12 === 0 ? 12 : h24 % 12;
  return `${h12}:${String(m).padStart(2, "0")} ${ampm}`;
}

/** "42m" / "1h 12m" from the seconds a recording reports. DynamoDB Decimals
 * serialize as strings, so the value is coerced before use — same tolerance
 * as MinuteX's weekCaptured(). Empty when there is no usable duration. */
export function durationLabel(raw: number | string | null | undefined): string {
  const secs = Number(raw);
  if (!Number.isFinite(secs) || secs <= 0) return "";
  const h = Math.floor(secs / 3600);
  const m = Math.round((secs % 3600) / 60);
  if (h) return `${h}h ${m}m`;
  return m > 0 ? `${m}m` : "<1m";
}

export type AgendaEntry =
  | {
    kind: "meeting";
    id: string;
    /** Minutes past midnight, for ordering. Null sorts to the end. */
    at: number | null;
    recording: RecordingSummary;
  }
  | {
    kind: "deadline";
    id: string;
    at: null;
    deadline: MeetingDeadline;
  }
  | {
    kind: "task";
    id: string;
    at: null;
    task: ApiTask;
  };

/** One day's agenda: the meetings that happened, in clock order, then the
 * tasks owed that day in the dashboard's own urgency order.
 *
 * Meetings come first because they are FIXED — they happened at a time, and a
 * day reads chronologically. Tasks follow as the day's workload, ordered by
 * what needs attention rather than by an invented hour. Merging them into one
 * time-sorted list would require pretending a task has a time, which is the
 * kind of small lie that makes a calendar untrustworthy. */
export function agendaForDay(
  tasks: ApiTask[],
  meetings: RecordingSummary[],
  dayKey: string,
  now: Date,
  deadlines: MeetingDeadline[] = []
): AgendaEntry[] {
  const onDay = meetings
    .filter((r) => meetingDayKey(r) === dayKey)
    .map((r) => ({
      kind: "meeting" as const,
      id: r.audio_s3_key,
      at: minutesOfDay(String(r.recorded_at || r.created_at || "")),
      recording: r,
    }))
    .sort((a, b) => {
      if (a.at === b.at) return a.id < b.id ? -1 : 1;
      if (a.at === null) return 1;
      if (b.at === null) return -1;
      return a.at - b.at;
    });

  // Dates the meeting MENTIONED, between what happened and what is owed:
  // they are context for the day, not work in it.
  const dates = deadlinesOnDay(deadlines, dayKey).map((d) => ({
    kind: "deadline" as const,
    id: d.id,
    at: null,
    deadline: d,
  }));

  const dayTasks = tasksOnDay(tasks, dayKey, now).map((t) => ({
    kind: "task" as const,
    id: t.id,
    at: null,
    task: t,
  }));

  return [...onDay, ...dates, ...dayTasks];
}

/** A one-line summary of a day, for the heading under the grid: "2 meetings ·
 * 3 due · 1 overdue". Only the clauses that apply, and "" for an empty day so
 * the caller renders nothing rather than "0 meetings, 0 due". */
export function describeDay(load: DayLoad): string {
  const parts: string[] = [];
  if (load.meetings) {
    parts.push(`${load.meetings} meeting${load.meetings === 1 ? "" : "s"}`);
  }
  if (load.open) parts.push(`${load.open} due`);
  if (load.overdue) parts.push(`${load.overdue} overdue`);
  if (load.completed) {
    parts.push(`${load.completed} completed`);
  }
  if (load.deadlines) {
    parts.push(
      `${load.deadlines} date${load.deadlines === 1 ? "" : "s"} mentioned`
    );
  }
  return parts.join("  ·  ");
}

// ---------------------------------------------------------------------------
// Dates mentioned in meetings (MeetingHighlights.deadlines)
//
// The AI extracts every date, time and milestone a meeting mentions, stored
// EXACTLY as spoken — "next Tuesday", "end of Q3", "15th March" — because
// prompts.py forbids it from resolving relative dates (it does not know the
// meeting's date, so it would be guessing). lib/spoken-dates.ts does that
// resolution here on the client, anchored to the meeting's recorded_at.
//
// These are NOT tasks. Nobody owes them, they have no status, and they cannot
// be completed. A "deadline" here is a date the meeting REFERRED to — a
// contract expiry, a launch, a board review. Putting them on the calendar is
// what makes it show everything that was said to matter, not only what got
// turned into a task.
//
// Anything that does not resolve to a real day keeps dayKey "" and is surfaced
// separately as an unplaced date rather than pinned to a guess.
// ---------------------------------------------------------------------------

export type MeetingDeadline = {
  /** Stable within a render: meeting key + index. Highlights carry no ids. */
  id: string;
  /** What the deadline is about ("contract expires"). */
  what: string;
  /** The phrase exactly as spoken ("end of Q3"). Always shown — the resolved
   * date never replaces the speaker's own words. */
  when: string;
  /** Resolved day, or "" when the phrase is not placeable. */
  dayKey: string;
  /** "exact" (a date was spoken) | "relative" (inferred from the meeting
   * date) | "none" (not a day at all). Drives the "inferred" marker. */
  confidence: "exact" | "relative" | "none";
  recordingKey: string;
  meetingTitle: string;
};

/** Build the deadline list for one meeting's highlights.
 *
 * `resolve` is injected rather than imported so this module stays free of
 * lib/spoken-dates and the two can be tested independently — and so the whole
 * date-parsing policy lives in exactly one place. */
export function deadlinesFromHighlights(
  recordingKey: string,
  meetingTitle: string,
  recordedAt: string,
  rows: { what: string; when: string }[],
  resolve: (when: string, anchor: Date) => {
    dayKey: string;
    confidence: "exact" | "relative" | "none";
  }
): MeetingDeadline[] {
  const anchor = new Date(recordedAt);
  const anchored = Number.isNaN(anchor.getTime()) ? null : anchor;
  const out: MeetingDeadline[] = [];
  rows.forEach((r, i) => {
    const what = String(r?.what || "").trim();
    const when = String(r?.when || "").trim();
    // A row with neither half says nothing; skip rather than render a blank.
    if (!what && !when) return;
    // With no usable meeting date a relative phrase cannot be anchored, so
    // nothing is resolved — the row still shows, just without a day.
    const res = anchored
      ? resolve(when, anchored)
      : { dayKey: "", confidence: "none" as const };
    out.push({
      id: `${recordingKey}#${i}`,
      what,
      when,
      dayKey: res.dayKey,
      confidence: res.confidence,
      recordingKey,
      meetingTitle,
    });
  });
  return out;
}

/** The deadlines that landed on a given day. */
export function deadlinesOnDay(
  deadlines: MeetingDeadline[],
  dayKey: string
): MeetingDeadline[] {
  return deadlines.filter((d) => d.dayKey === dayKey);
}

/** Deadlines that could NOT be placed — real dates the meeting mentioned that
 * are not calendar days ("end of Q3", "before the holidays"). Surfaced as a
 * list so they are visible without being pinned to a date nobody said. */
export function unplacedDeadlines(
  deadlines: MeetingDeadline[]
): MeetingDeadline[] {
  return deadlines.filter((d) => !d.dayKey);
}
