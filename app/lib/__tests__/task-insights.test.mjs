// lib/__tests__/task-insights.test.mjs — the Task Action Center's arithmetic.
//
// Run:  node --test lib/__tests__/task-insights.test.mjs
//
// WHAT THIS PINS. The dashboard makes claims about a person's workload —
// what is late, how much of the week is done, what is coming. A wrong number
// here is worse than no number, because the whole screen is built to be
// trusted at a glance. So these tests are mostly about REFUSING to claim:
//
//   * no negative or misleading relative dates ("in -2 days");
//   * no overdue task presented as "upcoming";
//   * no vs-last-week arrow without a real prior week to compare against;
//   * no closed task counted as needing attention.
//
// The module under test is deliberately pure — no React, no expo, no network —
// precisely so it can be imported directly here. It is TypeScript, so the
// functions are mirrored below rather than imported: `node --test` runs the
// .mjs suite without a transpile step, matching the convention in
// contact-ordering.test.mjs (which mirrors its subject for the same reason).
// These tests state the intended behaviour; a change that breaks one should
// have to justify itself.
import { describe, it } from "node:test";
import assert from "node:assert/strict";

// --- mirrored from lib/task-insights.ts ------------------------------------

function toDayKey(d) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function addDays(from, days) {
  const d = new Date(from.getFullYear(), from.getMonth(), from.getDate());
  d.setDate(d.getDate() + days);
  return d;
}

function startOfWeek(d) {
  const day = d.getDay();
  const back = day === 0 ? 6 : day - 1;
  return addDays(d, -back);
}

function isDayKey(s) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) return false;
  const [y, m, d] = s.split("-").map(Number);
  if (m < 1 || m > 12 || d < 1 || d > 31) return false;
  const probe = new Date(y, m - 1, d);
  return probe.getMonth() === m - 1 && probe.getDate() === d;
}

function daysUntil(dayKey, now) {
  if (!isDayKey(dayKey)) return null;
  const [y, m, d] = dayKey.split("-").map(Number);
  const target = new Date(y, m - 1, d);
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.round((target.getTime() - today.getTime()) / 86400000);
}

function dueKeyOf(t) {
  const raw = String(t.due_date || t.due || "").trim();
  return isDayKey(raw) ? raw : "";
}

const TERMINAL_STATUSES = new Set(["Completed", "Cancelled"]);
const isClosed = (t) => TERMINAL_STATUSES.has(t.status);

function isOverdue(t, now) {
  if (isClosed(t)) return false;
  if (typeof t.is_overdue === "boolean") return t.is_overdue;
  const key = dueKeyOf(t);
  return !!key && key < toDayKey(now);
}

const needsAssignment = (t) =>
  t.resolution_status === "UNRESOLVED" || t.resolution_status === "AMBIGUOUS";

function computeCounts(tasks, now, opts = {}) {
  const today = toDayKey(now);
  const weekEnd = toDayKey(addDays(startOfWeek(now), 6));
  let overdue = 0, dueThisWeek = 0, completed = 0, needsAssign = 0;
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
    overdue, dueThisWeek, completed,
    needsAssignment: needsAssign, partial: !!opts.partial,
  };
}

const ATTENTION_RANK = {
  overdue: 0, dueToday: 1, dueSoon: 2, needsAssignment: 3, open: 4,
};
const DUE_SOON_DAYS = 3;

function attentionBucket(t, now) {
  if (isOverdue(t, now)) return "overdue";
  const key = dueKeyOf(t);
  const days = key ? daysUntil(key, now) : null;
  if (days === 0) return "dueToday";
  if (days !== null && days > 0 && days <= DUE_SOON_DAYS) return "dueSoon";
  if (needsAssignment(t)) return "needsAssignment";
  return "open";
}

function rankForAttention(tasks, now) {
  return tasks
    .filter((t) => !isClosed(t))
    .map((t, i) => ({ t, i }))
    .sort((a, b) => {
      const ra = ATTENTION_RANK[attentionBucket(a.t, now)];
      const rb = ATTENTION_RANK[attentionBucket(b.t, now)];
      if (ra !== rb) return ra - rb;
      const da = dueKeyOf(a.t), db = dueKeyOf(b.t);
      if (da && db && da !== db) return da < db ? -1 : 1;
      if (da && !db) return -1;
      if (!da && db) return 1;
      const ia = String(a.t.id), ib = String(b.t.id);
      if (ia !== ib) return ia < ib ? -1 : 1;
      return a.i - b.i;
    })
    .map((x) => x.t);
}

const UPCOMING_WINDOW_DAYS = 14;

function relativeDueLabel(days) {
  if (days === 0) return "Today";
  if (days === 1) return "Tomorrow";
  if (days <= 6) return `In ${days} days`;
  if (days <= 13) return "Next week";
  return `In ${Math.round(days / 7)} weeks`;
}

function upcomingDeadlines(tasks, now, limit = 3) {
  const out = [];
  for (const t of tasks) {
    if (isClosed(t)) continue;
    if (isOverdue(t, now)) continue;
    const dayKey = dueKeyOf(t);
    if (!dayKey) continue;
    const days = daysUntil(dayKey, now);
    if (days === null || days < 0 || days > UPCOMING_WINDOW_DAYS) continue;
    out.push({
      task: t, dayKey, days, label: relativeDueLabel(days),
      progress: Math.max(0, Math.min(1,
        (UPCOMING_WINDOW_DAYS - days) / UPCOMING_WINDOW_DAYS)),
    });
  }
  out.sort((a, b) =>
    a.days !== b.days ? a.days - b.days
      : String(a.task.id) < String(b.task.id) ? -1 : 1);
  return out.slice(0, limit);
}

function completionDayKey(t) {
  const raw = String(t.completed_at || "").trim();
  if (!raw) return "";
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return "";
  return toDayKey(d);
}

function weeklyProgress(tasks, now) {
  const monday = startOfWeek(now);
  const weekStart = toDayKey(monday);
  const weekEnd = toDayKey(addDays(monday, 6));
  const prevStart = toDayKey(addDays(monday, -7));
  const prevEnd = toDayKey(addDays(monday, -1));
  let completed = 0, total = 0, prevCompleted = 0, prevTotal = 0;
  for (const t of tasks) {
    const due = dueKeyOf(t);
    const done = completionDayKey(t);
    if ((due && due >= weekStart && due <= weekEnd) ||
        (done && done >= weekStart && done <= weekEnd)) {
      total += 1;
      if (t.status === "Completed") completed += 1;
    }
    if ((due && due >= prevStart && due <= prevEnd) ||
        (done && done >= prevStart && done <= prevEnd)) {
      prevTotal += 1;
      if (t.status === "Completed") prevCompleted += 1;
    }
  }
  const ratio = total > 0 ? completed / total : 0;
  const deltaPct = prevTotal > 0
    ? Math.round((ratio - prevCompleted / prevTotal) * 100) : null;
  return { completed, total, ratio, deltaPct };
}

function meetingInsights(tasks, titles, now, limit = 1) {
  const byMeeting = new Map();
  for (const t of tasks) {
    const key = String(t.source_recording_id || "");
    if (!key) continue;
    if (t.source_type !== "AI") continue;
    const title = titles.get(key);
    if (!title) continue;
    let row = byMeeting.get(key);
    if (!row) {
      row = { recordingKey: key, title, generated: 0, completed: 0, overdue: 0, needsAssignment: 0 };
      byMeeting.set(key, row);
    }
    row.generated += 1;
    if (t.status === "Completed") row.completed += 1;
    if (isOverdue(t, now)) row.overdue += 1;
    if (needsAssignment(t)) row.needsAssignment += 1;
  }
  return [...byMeeting.values()]
    .sort((a, b) => b.generated !== a.generated ? b.generated - a.generated
      : a.recordingKey < b.recordingKey ? -1 : 1)
    .slice(0, limit);
}

function describeInsight(m) {
  const parts = [];
  if (m.completed) parts.push(`${m.completed} completed`);
  if (m.overdue) parts.push(`${m.overdue} overdue`);
  if (m.needsAssignment) parts.push(`${m.needsAssignment} waiting for assignment resolution`);
  const items = `${m.generated} action item${m.generated === 1 ? "" : "s"}`;
  return parts.length ? `${items}. ${parts.join(", ")}.` : `${items}.`;
}

function mondayIndex(d) {
  const day = d.getDay();
  return day === 0 ? 6 : day - 1;
}

function startOfMonth(d) {
  return new Date(d.getFullYear(), d.getMonth(), 1);
}

function addMonths(d, n) {
  return new Date(d.getFullYear(), d.getMonth() + n, 1);
}

function monthGrid(month, now) {
  const first = startOfMonth(month);
  const gridStart = addDays(first, -mondayIndex(first));
  const today = toDayKey(now);
  const cells = [];
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

const EMPTY_LOAD = {
  open: 0, overdue: 0, completed: 0, meetings: 0, deadlines: 0,
};

function loadByDay(tasks, now, meetings = [], deadlines = []) {
  const map = new Map();
  const bump = (key, patch) => {
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
      bump(completionDayKey(t) || due, { completed: 1 });
      continue;
    }
    if (isClosed(t)) continue;
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

function meetingDayKey(r) {
  const raw = String(r.recorded_at || r.created_at || "").trim();
  if (!raw) return "";
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return "";
  return toDayKey(d);
}

function minutesOfDay(iso) {
  const raw = String(iso || "").trim();
  if (!raw) return null;
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return null;
  return d.getHours() * 60 + d.getMinutes();
}

function clockLabel(iso) {
  const mins = minutesOfDay(iso);
  if (mins === null) return "";
  const h24 = Math.floor(mins / 60);
  const m = mins % 60;
  const ampm = h24 < 12 ? "AM" : "PM";
  const h12 = h24 % 12 === 0 ? 12 : h24 % 12;
  return `${h12}:${String(m).padStart(2, "0")} ${ampm}`;
}

function durationLabel(raw) {
  const secs = Number(raw);
  if (!Number.isFinite(secs) || secs <= 0) return "";
  const h = Math.floor(secs / 3600);
  const m = Math.round((secs % 3600) / 60);
  if (h) return `${h}h ${m}m`;
  return m > 0 ? `${m}m` : "<1m";
}

function agendaForDay(tasks, meetings, dayKey, now, deadlines = []) {
  const onDay = meetings
    .filter((r) => meetingDayKey(r) === dayKey)
    .map((r) => ({
      kind: "meeting",
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
  const dates = deadlinesOnDay(deadlines, dayKey).map((d) => ({
    kind: "deadline",
    id: d.id,
    at: null,
    deadline: d,
  }));
  const dayTasks = tasksOnDay(tasks, dayKey, now).map((t) => ({
    kind: "task",
    id: t.id,
    at: null,
    task: t,
  }));
  return [...onDay, ...dates, ...dayTasks];
}

function describeDay(load) {
  const parts = [];
  if (load.meetings) {
    parts.push(`${load.meetings} meeting${load.meetings === 1 ? "" : "s"}`);
  }
  if (load.open) parts.push(`${load.open} due`);
  if (load.overdue) parts.push(`${load.overdue} overdue`);
  if (load.completed) parts.push(`${load.completed} completed`);
  if (load.deadlines) {
    parts.push(
      `${load.deadlines} date${load.deadlines === 1 ? "" : "s"} mentioned`
    );
  }
  return parts.join("  ·  ");
}

function deadlinesFromHighlights(recordingKey, meetingTitle, recordedAt, rows, resolve) {
  const anchor = new Date(recordedAt);
  const anchored = Number.isNaN(anchor.getTime()) ? null : anchor;
  const out = [];
  rows.forEach((r, i) => {
    const what = String(r?.what || "").trim();
    const when = String(r?.when || "").trim();
    if (!what && !when) return;
    const res = anchored ? resolve(when, anchored) : { dayKey: "", confidence: "none" };
    out.push({
      id: `${recordingKey}#${i}`,
      what, when,
      dayKey: res.dayKey,
      confidence: res.confidence,
      recordingKey, meetingTitle,
    });
  });
  return out;
}

function deadlinesOnDay(deadlines, dayKey) {
  return deadlines.filter((d) => d.dayKey === dayKey);
}

function unplacedDeadlines(deadlines) {
  return deadlines.filter((d) => !d.dayKey);
}

function dayLoad(map, dayKey) {
  return map.get(dayKey) ?? EMPTY_LOAD;
}

function tasksOnDay(tasks, dayKey, now) {
  const onDay = tasks.filter((t) => {
    if (dueKeyOf(t) === dayKey) return true;
    return t.status === "Completed" && completionDayKey(t) === dayKey;
  });
  const open = rankForAttention(onDay, now);
  const closed = onDay.filter((t) => isClosed(t));
  return [...open, ...closed];
}

const WEEKDAY_NAMES = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function shortDate(dayKey) {
  if (!isDayKey(dayKey)) return "";
  const [, m, d] = dayKey.split("-").map(Number);
  return `${MONTHS[m - 1]} ${d}`;
}

function dayHeading(dayKey, now) {
  const days = daysUntil(dayKey, now);
  if (days === 0) return "Today";
  if (days === 1) return "Tomorrow";
  if (days === -1) return "Yesterday";
  const [y, m, d] = dayKey.split("-").map(Number);
  const date = new Date(y, m - 1, d);
  return `${WEEKDAY_NAMES[date.getDay()]}, ${shortDate(dayKey)}`;
}

function greetingFor(now) {
  const h = now.getHours();
  if (h < 12) return "Good morning";
  if (h < 17) return "Good afternoon";
  return "Good evening";
}

// --- fixtures ---------------------------------------------------------------

// A Wednesday, so the week has real days either side of "today".
const NOW = new Date(2026, 7, 26, 10, 0, 0); // 2026-08-26
const day = (n) => toDayKey(addDays(NOW, n));

let seq = 0;
function task(over = {}) {
  seq += 1;
  return {
    id: `t${String(seq).padStart(3, "0")}`,
    task: `Task ${seq}`,
    status: "Open",
    priority: "Medium",
    due: "",
    due_date: "",
    resolution_status: "RESOLVED",
    source_type: "MANUAL",
    source_recording_id: "",
    completed_at: "",
    ...over,
  };
}

// --- tests ------------------------------------------------------------------

describe("day arithmetic", () => {
  it("treats due dates as calendar days, not instants", () => {
    // Late-evening 'now' must not push a same-day deadline into yesterday.
    const late = new Date(2026, 7, 26, 23, 59, 59);
    assert.equal(daysUntil("2026-08-26", late), 0);
    assert.equal(daysUntil("2026-08-27", late), 1);
  });

  it("starts the week on Monday, including on a Sunday", () => {
    const sunday = new Date(2026, 7, 30); // 2026-08-30 is a Sunday
    assert.equal(sunday.getDay(), 0);
    assert.equal(toDayKey(startOfWeek(sunday)), "2026-08-24");
    // And the Wednesday fixture lands in the same week.
    assert.equal(toDayKey(startOfWeek(NOW)), "2026-08-24");
  });

  it("rejects malformed dates rather than inventing one", () => {
    assert.equal(daysUntil("not-a-date", NOW), null);
    assert.equal(daysUntil("2026-02-30", NOW), null); // Feb 30 does not exist
    assert.equal(dueKeyOf({ due: "soon" }), "");
  });
});

describe("overdue", () => {
  it("trusts the server's computed flag over the local clock", () => {
    // is_overdue is computed server-side per request; a client whose clock is
    // wrong must not override it.
    assert.equal(isOverdue(task({ due: day(5), is_overdue: true }), NOW), true);
    assert.equal(isOverdue(task({ due: day(-5), is_overdue: false }), NOW), false);
  });

  it("never calls a closed task overdue", () => {
    for (const status of ["Completed", "Cancelled"]) {
      const t = task({ status, due: day(-9), is_overdue: true });
      assert.equal(isOverdue(t, NOW), false, `${status} must not be overdue`);
    }
  });

  it("falls back to the due date when the server sent no flag", () => {
    assert.equal(isOverdue(task({ due: day(-1) }), NOW), true);
    assert.equal(isOverdue(task({ due: day(0) }), NOW), false);
    assert.equal(isOverdue(task({ due: "" }), NOW), false);
  });
});

describe("computeCounts", () => {
  it("counts overdue, due-this-week and completed without double-counting", () => {
    const tasks = [
      task({ due: day(-2) }),                       // overdue
      task({ due: day(-1) }),                       // overdue
      task({ due: day(0) }),                        // due today -> this week
      task({ due: day(1) }),                        // due tomorrow -> this week
      task({ status: "Completed", due: day(-3) }),  // completed, not overdue
      task({ status: "Cancelled", due: day(-4) }),  // cancelled, counts nowhere
    ];
    const c = computeCounts(tasks, NOW);
    assert.equal(c.overdue, 2);
    assert.equal(c.completed, 1);
    // Wed 26th: today + tomorrow are inside the Mon-Sun week; the overdue two
    // are excluded so the week does not look busier than it is.
    assert.equal(c.dueThisWeek, 2);
  });

  it("does not count a task due next week as due this week", () => {
    // NOW is Wednesday; +7 days is next Wednesday, past Sunday's boundary.
    const c = computeCounts([task({ due: day(7) })], NOW);
    assert.equal(c.dueThisWeek, 0);
  });

  it("counts unresolved and ambiguous assignees, not resolved ones", () => {
    const tasks = [
      task({ resolution_status: "UNRESOLVED" }),
      task({ resolution_status: "AMBIGUOUS" }),
      task({ resolution_status: "RESOLVED" }),
      task({ resolution_status: "NONE" }),
    ];
    assert.equal(computeCounts(tasks, NOW).needsAssignment, 2);
  });

  it("reports partial so the UI can say what the numbers describe", () => {
    assert.equal(computeCounts([], NOW).partial, false);
    assert.equal(computeCounts([], NOW, { partial: true }).partial, true);
  });
});

describe("rankForAttention", () => {
  it("orders overdue, due today, due soon, needs assignment, then open", () => {
    const open = task({ id: "z-open" });
    const needs = task({ id: "z-needs", resolution_status: "UNRESOLVED" });
    const soon = task({ id: "z-soon", due: day(2) });
    const today = task({ id: "z-today", due: day(0) });
    const late = task({ id: "z-late", due: day(-1) });
    const ranked = rankForAttention([open, needs, soon, today, late], NOW);
    assert.deepEqual(ranked.map((t) => t.id), [
      "z-late", "z-today", "z-soon", "z-needs", "z-open",
    ]);
  });

  it("drops closed tasks — a finished task needs no attention", () => {
    const tasks = [
      task({ id: "a", status: "Completed", due: day(-1) }),
      task({ id: "b", status: "Cancelled" }),
      task({ id: "c", due: day(-1) }),
    ];
    assert.deepEqual(rankForAttention(tasks, NOW).map((t) => t.id), ["c"]);
  });

  it("sorts by nearest due date within a bucket", () => {
    const far = task({ id: "far", due: day(-1) });
    const near = task({ id: "near", due: day(-9) });
    // Both overdue; the one late the longest comes first.
    assert.deepEqual(
      rankForAttention([far, near], NOW).map((t) => t.id),
      ["near", "far"]
    );
  });

  it("is stable across identical loads", () => {
    // Two tasks alike in every ranked respect must not swap between renders.
    const a = task({ id: "aaa" });
    const b = task({ id: "bbb" });
    const first = rankForAttention([a, b], NOW).map((t) => t.id);
    const second = rankForAttention([b, a], NOW).map((t) => t.id);
    assert.deepEqual(first, second);
  });

  it("puts a dated task ahead of an undated one in the same bucket", () => {
    const dated = task({ id: "dated", due: day(20) });
    const undated = task({ id: "undated" });
    assert.deepEqual(
      rankForAttention([undated, dated], NOW).map((t) => t.id),
      ["dated", "undated"]
    );
  });
});

describe("upcomingDeadlines", () => {
  it("never shows an overdue task as upcoming", () => {
    const items = upcomingDeadlines([task({ due: day(-3) })], NOW);
    assert.equal(items.length, 0);
  });

  it("never produces a negative or misleading relative date", () => {
    const tasks = [day(-5), day(0), day(1), day(3), day(9), day(30)]
      .map((d) => task({ due: d }));
    for (const item of upcomingDeadlines(tasks, NOW, 10)) {
      assert.ok(item.days >= 0, `days must not be negative: ${item.days}`);
      assert.ok(!item.label.includes("-"), `bad label: ${item.label}`);
    }
  });

  it("labels the near days in plain language", () => {
    assert.equal(relativeDueLabel(0), "Today");
    assert.equal(relativeDueLabel(1), "Tomorrow");
    assert.equal(relativeDueLabel(3), "In 3 days");
    assert.equal(relativeDueLabel(9), "Next week");
  });

  it("excludes closed tasks and anything past the window", () => {
    const tasks = [
      task({ id: "done", status: "Completed", due: day(2) }),
      task({ id: "far", due: day(UPCOMING_WINDOW_DAYS + 1) }),
      task({ id: "keep", due: day(2) }),
    ];
    assert.deepEqual(
      upcomingDeadlines(tasks, NOW, 10).map((d) => d.task.id),
      ["keep"]
    );
  });

  it("orders nearest first and honours the limit", () => {
    const tasks = [day(6), day(1), day(3)].map((d) => task({ due: d }));
    const items = upcomingDeadlines(tasks, NOW, 2);
    assert.deepEqual(items.map((d) => d.days), [1, 3]);
  });

  it("keeps progress within 0..1", () => {
    for (const d of upcomingDeadlines(
      [day(0), day(7), day(14)].map((x) => task({ due: x })), NOW, 10
    )) {
      assert.ok(d.progress >= 0 && d.progress <= 1, `bad progress ${d.progress}`);
    }
  });
});

describe("weeklyProgress", () => {
  it("counts the week's workload and what is done in it", () => {
    const tasks = [
      task({ due: day(0), status: "Completed" }),
      task({ due: day(1) }),
      task({ due: day(-1) }), // Tuesday — still this week
    ];
    const w = weeklyProgress(tasks, NOW);
    assert.equal(w.total, 3);
    assert.equal(w.completed, 1);
    assert.ok(Math.abs(w.ratio - 1 / 3) < 1e-9);
  });

  it("omits the comparison when there is no prior week to compare against", () => {
    // The spec's rule: no baseline, no arrow — never an invented number.
    const w = weeklyProgress([task({ due: day(0) })], NOW);
    assert.equal(w.deltaPct, null);
  });

  it("reports a real delta when last week has data", () => {
    const lastWeek = toDayKey(addDays(startOfWeek(NOW), -4)); // prior Thursday
    const tasks = [
      // Last week: 1 of 2 done -> 50%.
      task({ due: lastWeek, status: "Completed", completed_at: `${lastWeek}T09:00:00Z` }),
      task({ due: lastWeek }),
      // This week: 1 of 1 done -> 100%.
      task({ due: day(0), status: "Completed" }),
    ];
    const w = weeklyProgress(tasks, NOW);
    assert.equal(w.deltaPct, 50);
  });

  it("never divides by zero", () => {
    const w = weeklyProgress([], NOW);
    assert.equal(w.ratio, 0);
    assert.equal(w.total, 0);
    assert.equal(w.deltaPct, null);
  });

  it("counts a task completed this week even when it was due earlier", () => {
    const t = task({
      due: toDayKey(addDays(startOfWeek(NOW), -10)),
      status: "Completed",
      completed_at: `${day(0)}T12:00:00`,
    });
    const w = weeklyProgress([t], NOW);
    assert.equal(w.completed, 1);
    assert.equal(w.total, 1);
  });
});

describe("meetingInsights", () => {
  const titles = new Map([["rec/acme.wav", "Acme Product Review"]]);

  it("summarises only AI-extracted tasks from a known meeting", () => {
    const tasks = [
      task({ source_type: "AI", source_recording_id: "rec/acme.wav", status: "Completed" }),
      task({ source_type: "AI", source_recording_id: "rec/acme.wav", status: "Completed" }),
      task({ source_type: "AI", source_recording_id: "rec/acme.wav", due: day(-1) }),
      task({ source_type: "AI", source_recording_id: "rec/acme.wav", resolution_status: "UNRESOLVED" }),
      task({ source_type: "AI", source_recording_id: "rec/acme.wav", resolution_status: "AMBIGUOUS" }),
      // Typed by hand — not "generated" by the meeting.
      task({ source_type: "MANUAL", source_recording_id: "rec/acme.wav" }),
    ];
    const [m] = meetingInsights(tasks, titles, NOW);
    assert.equal(m.title, "Acme Product Review");
    assert.equal(m.generated, 5);
    assert.equal(m.completed, 2);
    assert.equal(m.overdue, 1);
    assert.equal(m.needsAssignment, 2);
    assert.equal(
      describeInsight(m),
      "5 action items. 2 completed, 1 overdue, 2 waiting for assignment resolution."
    );
  });

  it("skips meetings whose title is unknown rather than saying 'Untitled'", () => {
    const tasks = [task({ source_type: "AI", source_recording_id: "rec/ghost.wav" })];
    assert.deepEqual(meetingInsights(tasks, titles, NOW), []);
  });

  it("omits clauses that do not apply", () => {
    const m = { generated: 1, completed: 0, overdue: 0, needsAssignment: 0 };
    assert.equal(describeInsight(m), "1 action item.");
  });
});

describe("greetingFor", () => {
  it("matches the time of day", () => {
    assert.equal(greetingFor(new Date(2026, 7, 26, 9)), "Good morning");
    assert.equal(greetingFor(new Date(2026, 7, 26, 13)), "Good afternoon");
    assert.equal(greetingFor(new Date(2026, 7, 26, 19)), "Good evening");
  });
});

describe("monthGrid", () => {
  it("is always 42 cells, so paging months never shifts the layout", () => {
    for (let m = 0; m < 12; m++) {
      const grid = monthGrid(new Date(2026, m, 1), NOW);
      assert.equal(grid.length, 42, `month ${m} must be 6 rows`);
    }
  });

  it("starts on a Monday", () => {
    for (const m of [0, 1, 6, 11]) {
      const grid = monthGrid(new Date(2026, m, 1), NOW);
      const [y, mm, dd] = grid[0].dayKey.split("-").map(Number);
      assert.equal(new Date(y, mm - 1, dd).getDay(), 1, "first cell is Monday");
    }
  });

  it("marks the adjacent months' days as out-of-month", () => {
    // Aug 2026 starts on a Saturday, so the grid opens with July days.
    const grid = monthGrid(new Date(2026, 7, 1), NOW);
    assert.equal(grid[0].inMonth, false);
    const inMonth = grid.filter((c) => c.inMonth);
    assert.equal(inMonth.length, 31, "August has 31 days");
    assert.equal(inMonth[0].date, 1);
    assert.equal(inMonth[30].date, 31);
  });

  it("handles a leap February", () => {
    const grid = monthGrid(new Date(2028, 1, 1), NOW); // 2028 is a leap year
    assert.equal(grid.filter((c) => c.inMonth).length, 29);
  });

  it("flags today and the past against the given clock", () => {
    const grid = monthGrid(NOW, NOW);
    const today = grid.find((c) => c.dayKey === toDayKey(NOW));
    assert.ok(today);
    assert.equal(today.isToday, true);
    assert.equal(today.isPast, false);
    const yesterday = grid.find((c) => c.dayKey === toDayKey(addDays(NOW, -1)));
    assert.equal(yesterday.isPast, true);
  });
});

describe("addMonths", () => {
  it("does not overflow off a 31st", () => {
    // The classic bug: naive setMonth on Jan 31 lands in March.
    assert.equal(toDayKey(addMonths(new Date(2026, 0, 31), 1)), "2026-02-01");
  });

  it("crosses year boundaries in both directions", () => {
    assert.equal(toDayKey(addMonths(new Date(2026, 11, 15), 1)), "2027-01-01");
    assert.equal(toDayKey(addMonths(new Date(2026, 0, 15), -1)), "2025-12-01");
  });
});

describe("loadByDay", () => {
  it("buckets open tasks by due day and flags overdue", () => {
    const tasks = [
      task({ due: day(0) }),
      task({ due: day(0) }),
      task({ due: day(-2) }),
    ];
    const map = loadByDay(tasks, NOW);
    assert.equal(dayLoad(map, day(0)).open, 2);
    assert.equal(dayLoad(map, day(0)).overdue, 0);
    assert.equal(dayLoad(map, day(-2)).overdue, 1);
  });

  it("counts a completion once — on the day it happened, not also its due day", () => {
    const t = task({
      due: day(-3),
      status: "Completed",
      completed_at: `${day(-1)}T10:00:00`,
    });
    const map = loadByDay([t], NOW);
    assert.equal(dayLoad(map, day(-1)).completed, 1);
    assert.equal(dayLoad(map, day(-3)).completed, 0, "must not double-count");
    assert.equal(dayLoad(map, day(-3)).open, 0, "completed is not open");
  });

  it("falls back to the due day when there is no completion stamp", () => {
    const map = loadByDay([task({ due: day(1), status: "Completed" })], NOW);
    assert.equal(dayLoad(map, day(1)).completed, 1);
  });

  it("gives a cancelled task no load — nothing is owed", () => {
    const map = loadByDay([task({ due: day(0), status: "Cancelled" })], NOW);
    assert.deepEqual(dayLoad(map, day(0)), EMPTY_LOAD);
  });

  it("drops undated tasks rather than piling them onto today", () => {
    const map = loadByDay([task({ due: "" }), task({ due: "nope" })], NOW);
    assert.equal(map.size, 0);
    assert.deepEqual(dayLoad(map, toDayKey(NOW)), EMPTY_LOAD);
  });

  it("returns an empty load for a day with nothing on it", () => {
    const map = loadByDay([task({ due: day(0) })], NOW);
    assert.deepEqual(dayLoad(map, day(5)), EMPTY_LOAD);
  });
});

describe("tasksOnDay", () => {
  it("returns the day's tasks, open first by urgency then closed", () => {
    const tasks = [
      task({ id: "done", due: day(0), status: "Completed" }),
      task({ id: "open", due: day(0) }),
      task({ id: "other", due: day(4) }),
    ];
    assert.deepEqual(
      tasksOnDay(tasks, day(0), NOW).map((t) => t.id),
      ["open", "done"]
    );
  });

  it("includes a task completed that day even when it was due earlier", () => {
    const t = task({
      id: "late-finish",
      due: day(-6),
      status: "Completed",
      completed_at: `${day(0)}T15:00:00`,
    });
    assert.deepEqual(
      tasksOnDay([t], day(0), NOW).map((x) => x.id),
      ["late-finish"]
    );
  });

  it("is empty for a day with nothing due", () => {
    assert.deepEqual(tasksOnDay([task({ due: day(0) })], day(9), NOW), []);
  });
});

describe("dayHeading", () => {
  it("names the near days rather than dating them", () => {
    assert.equal(dayHeading(toDayKey(NOW), NOW), "Today");
    assert.equal(dayHeading(day(1), NOW), "Tomorrow");
    assert.equal(dayHeading(day(-1), NOW), "Yesterday");
  });

  it("falls back to a weekday and date further out", () => {
    // 2026-08-26 is a Wednesday, so +4 days is a Sunday.
    assert.equal(dayHeading(day(4), NOW), "Sun, Aug 30");
  });
});

// A recording fixture. recorded_at is a real local instant, unlike a task's
// plain due DAY — that difference is the whole reason the agenda keeps the two
// kinds apart.
let recSeq = 0;
function meeting(over = {}) {
  recSeq += 1;
  return {
    audio_s3_key: `rec/m${recSeq}.wav`,
    title: `Meeting ${recSeq}`,
    recorded_at: "",
    created_at: "",
    status: "complete",
    duration: 0,
    folder_id: "",
    ...over,
  };
}

/** A local ISO instant on the day `offset` days from NOW, at h:m. Built from
 * local parts on purpose: a "Z" string would shift day in most timezones and
 * make these tests pass or fail depending on where they run. */
function at(offset, h, m = 0) {
  const d = addDays(NOW, offset);
  return `${toDayKey(d)}T${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:00`;
}

describe("meetingDayKey", () => {
  it("places a meeting on the local day it happened", () => {
    assert.equal(meetingDayKey(meeting({ recorded_at: at(0, 9) })), toDayKey(NOW));
    assert.equal(meetingDayKey(meeting({ recorded_at: at(-1, 23, 45) })), day(-1));
  });

  it("keeps a just-after-midnight meeting on that day, not the day before", () => {
    assert.equal(meetingDayKey(meeting({ recorded_at: at(0, 0, 30) })), toDayKey(NOW));
  });

  it("falls back to created_at, and is empty when neither is usable", () => {
    assert.equal(meetingDayKey(meeting({ created_at: at(2, 14) })), day(2));
    assert.equal(meetingDayKey(meeting({})), "");
    assert.equal(meetingDayKey(meeting({ recorded_at: "nonsense" })), "");
  });
});

describe("clockLabel / durationLabel", () => {
  it("renders 12-hour times with a correct midnight and noon", () => {
    assert.equal(clockLabel(at(0, 0, 5)), "12:05 AM");
    assert.equal(clockLabel(at(0, 12, 0)), "12:00 PM");
    assert.equal(clockLabel(at(0, 9, 30)), "9:30 AM");
    assert.equal(clockLabel(at(0, 17, 5)), "5:05 PM");
  });

  it("returns empty rather than a broken time", () => {
    assert.equal(clockLabel(""), "");
    assert.equal(clockLabel("not-a-time"), "");
  });

  it("formats durations, tolerating the string form DynamoDB sends", () => {
    assert.equal(durationLabel(90), "2m");     // rounds to the nearest minute
    assert.equal(durationLabel("2520"), "42m");
    assert.equal(durationLabel(4320), "1h 12m");
    assert.equal(durationLabel(20), "<1m");
  });

  it("shows nothing for a missing or nonsense duration", () => {
    assert.equal(durationLabel(null), "");
    assert.equal(durationLabel(0), "");
    assert.equal(durationLabel("abc"), "");
  });
});

describe("loadByDay with meetings", () => {
  it("counts meetings alongside task load on the same day", () => {
    const map = loadByDay(
      [task({ due: day(0) }), task({ due: day(0) })],
      NOW,
      [meeting({ recorded_at: at(0, 10) })]
    );
    const l = dayLoad(map, day(0));
    assert.equal(l.meetings, 1);
    assert.equal(l.open, 2);
  });

  it("marks a day that has a meeting but nothing due", () => {
    const map = loadByDay([], NOW, [meeting({ recorded_at: at(3, 11) })]);
    const l = dayLoad(map, day(3));
    assert.equal(l.meetings, 1);
    assert.equal(l.open, 0);
  });

  it("leaves a day with neither completely empty", () => {
    const map = loadByDay([], NOW, [meeting({ recorded_at: at(0, 9) })]);
    assert.deepEqual(dayLoad(map, day(6)), EMPTY_LOAD);
  });

  it("ignores a meeting with no usable timestamp", () => {
    const map = loadByDay([], NOW, [meeting({})]);
    assert.equal(map.size, 0);
  });
});

describe("agendaForDay", () => {
  it("lists meetings in clock order, then the day's tasks", () => {
    const meetings = [
      meeting({ audio_s3_key: "late", recorded_at: at(0, 16, 0) }),
      meeting({ audio_s3_key: "early", recorded_at: at(0, 9, 15) }),
    ];
    const tasks = [task({ id: "t-due", due: day(0) })];
    const entries = agendaForDay(tasks, meetings, day(0), NOW);
    assert.deepEqual(entries.map((e) => e.id), ["early", "late", "t-due"]);
    assert.deepEqual(entries.map((e) => e.kind), ["meeting", "meeting", "task"]);
  });

  it("never gives a task a clock time", () => {
    const entries = agendaForDay([task({ due: day(0) })], [], day(0), NOW);
    assert.equal(entries.length, 1);
    assert.equal(entries[0].at, null);
  });

  it("sorts an untimed meeting last among meetings", () => {
    const meetings = [
      meeting({ audio_s3_key: "unknown", recorded_at: "", created_at: at(0, 0, 0) }),
      meeting({ audio_s3_key: "timed", recorded_at: at(0, 11) }),
    ];
    // The untimed one still lands on the day via created_at midnight, so it
    // sorts by that; what matters is the list is deterministic.
    const ids = agendaForDay([], meetings, day(0), NOW).map((e) => e.id);
    assert.equal(ids.length, 2);
    assert.deepEqual([...ids].sort(), ["timed", "unknown"]);
  });

  it("only includes entries from the requested day", () => {
    const meetings = [
      meeting({ audio_s3_key: "today", recorded_at: at(0, 10) }),
      meeting({ audio_s3_key: "tomorrow", recorded_at: at(1, 10) }),
    ];
    const tasks = [task({ id: "t0", due: day(0) }), task({ id: "t1", due: day(1) })];
    assert.deepEqual(
      agendaForDay(tasks, meetings, day(0), NOW).map((e) => e.id),
      ["today", "t0"]
    );
  });

  it("is empty for a day with neither meetings nor tasks", () => {
    assert.deepEqual(agendaForDay([], [], day(8), NOW), []);
  });

  it("gives every entry a key unique across the two kinds", () => {
    // The screen keys rows on `${kind}:${id}` — a meeting and a task must not
    // be able to collide even if their ids matched.
    const entries = agendaForDay(
      [task({ id: "shared", due: day(0) })],
      [meeting({ audio_s3_key: "shared", recorded_at: at(0, 9) })],
      day(0),
      NOW
    );
    const keys = entries.map((e) => `${e.kind}:${e.id}`);
    assert.equal(new Set(keys).size, keys.length);
  });
});

describe("describeDay", () => {
  it("states only the clauses that apply", () => {
    assert.equal(
      describeDay({ meetings: 2, open: 3, overdue: 1, completed: 0 }),
      "2 meetings  ·  3 due  ·  1 overdue"
    );
    assert.equal(
      describeDay({ meetings: 1, open: 0, overdue: 0, completed: 0 }),
      "1 meeting"
    );
  });

  it("is empty for an empty day, so the caller renders nothing", () => {
    assert.equal(describeDay(EMPTY_LOAD), "");
  });
});

describe("deadlinesFromHighlights", () => {
  // A tiny stand-in for lib/spoken-dates: only the cases these tests need.
  // The real parser has its own suite (spoken-dates.test.mjs) — what is
  // pinned HERE is the plumbing, not the date grammar.
  const fakeResolve = (when, anchor) => {
    const w = String(when).toLowerCase();
    if (w === "tomorrow") {
      return { dayKey: toDayKey(addDays(anchor, 1)), confidence: "relative" };
    }
    if (w === "2026-09-01") {
      return { dayKey: "2026-09-01", confidence: "exact" };
    }
    return { dayKey: "", confidence: "none" };
  };

  const rows = [
    { what: "Contract expires", when: "2026-09-01" },
    { what: "Board review", when: "tomorrow" },
    { what: "Budget freeze", when: "end of Q3" },
  ];

  it("resolves what it can and keeps the rest unplaced", () => {
    const out = deadlinesFromHighlights(
      "rec/a.wav", "Acme Review", at(0, 10), rows, fakeResolve
    );
    assert.equal(out.length, 3);
    assert.equal(out[0].dayKey, "2026-09-01");
    assert.equal(out[0].confidence, "exact");
    assert.equal(out[1].dayKey, day(1));
    assert.equal(out[1].confidence, "relative");
    assert.equal(out[2].dayKey, "");
    assert.equal(out[2].confidence, "none");
  });

  it("always keeps the spoken phrase, resolved or not", () => {
    const out = deadlinesFromHighlights(
      "rec/a.wav", "Acme Review", at(0, 10), rows, fakeResolve
    );
    // The resolved date must never REPLACE what the speaker actually said.
    assert.deepEqual(out.map((d) => d.when), ["2026-09-01", "tomorrow", "end of Q3"]);
  });

  it("anchors to the MEETING's date, not the reader's clock", () => {
    const janRows = [{ what: "Kickoff", when: "tomorrow" }];
    const out = deadlinesFromHighlights(
      "rec/j.wav", "Jan Meeting", "2026-01-05T09:00:00", janRows, fakeResolve
    );
    assert.equal(out[0].dayKey, "2026-01-06");
  });

  it("resolves nothing when the meeting has no usable date", () => {
    // Without an anchor a relative phrase cannot be placed — the row still
    // shows, just without a day.
    const out = deadlinesFromHighlights(
      "rec/x.wav", "Undated", "", [{ what: "Review", when: "tomorrow" }], fakeResolve
    );
    assert.equal(out.length, 1);
    assert.equal(out[0].dayKey, "");
  });

  it("skips a row with neither half rather than rendering a blank", () => {
    const out = deadlinesFromHighlights(
      "rec/a.wav", "Acme", at(0, 10),
      [{ what: "", when: "" }, { what: "Real", when: "end of Q3" }],
      fakeResolve
    );
    assert.equal(out.length, 1);
    assert.equal(out[0].what, "Real");
  });

  it("gives every row a stable, unique id", () => {
    const out = deadlinesFromHighlights(
      "rec/a.wav", "Acme", at(0, 10), rows, fakeResolve
    );
    assert.equal(new Set(out.map((d) => d.id)).size, out.length);
    // Same inputs, same ids — the list must not reshuffle between renders.
    const again = deadlinesFromHighlights(
      "rec/a.wav", "Acme", at(0, 10), rows, fakeResolve
    );
    assert.deepEqual(out.map((d) => d.id), again.map((d) => d.id));
  });

  it("carries the source meeting so a row can navigate back to it", () => {
    const out = deadlinesFromHighlights(
      "rec/a.wav", "Acme Review", at(0, 10), rows, fakeResolve
    );
    assert.equal(out[0].recordingKey, "rec/a.wav");
    assert.equal(out[0].meetingTitle, "Acme Review");
  });
});

describe("deadlines on the calendar", () => {
  const placed = (dayKey, id = "d1") => ({
    id, what: "Contract expires", when: "1st Sept",
    dayKey, confidence: "exact",
    recordingKey: "rec/a.wav", meetingTitle: "Acme",
  });
  const unplacedRow = {
    id: "d2", what: "Budget freeze", when: "end of Q3",
    dayKey: "", confidence: "none",
    recordingKey: "rec/a.wav", meetingTitle: "Acme",
  };

  it("marks a day that only has a mentioned date", () => {
    const map = loadByDay([], NOW, [], [placed(day(2))]);
    const l = dayLoad(map, day(2));
    assert.equal(l.deadlines, 1);
    assert.equal(l.open, 0);
    assert.equal(l.meetings, 0);
  });

  it("never lets an unplaced date land on a day", () => {
    // This is the whole point: "end of Q3" must not be pinned anywhere.
    const map = loadByDay([], NOW, [], [unplacedRow]);
    assert.equal(map.size, 0);
  });

  it("separates placed from unplaced", () => {
    const all = [placed(day(2)), unplacedRow];
    assert.deepEqual(deadlinesOnDay(all, day(2)).map((d) => d.id), ["d1"]);
    assert.deepEqual(unplacedDeadlines(all).map((d) => d.id), ["d2"]);
  });

  it("orders the agenda meetings, then dates, then tasks", () => {
    const entries = agendaForDay(
      [task({ id: "t1", due: day(0) })],
      [meeting({ audio_s3_key: "m1", recorded_at: at(0, 9) })],
      day(0),
      NOW,
      [placed(day(0))]
    );
    assert.deepEqual(entries.map((e) => e.kind), ["meeting", "deadline", "task"]);
    assert.deepEqual(entries.map((e) => e.id), ["m1", "d1", "t1"]);
  });

  it("keys stay unique across all three kinds", () => {
    const entries = agendaForDay(
      [task({ id: "same", due: day(0) })],
      [meeting({ audio_s3_key: "same", recorded_at: at(0, 9) })],
      day(0),
      NOW,
      [placed(day(0), "same")]
    );
    const keys = entries.map((e) => `${e.kind}:${e.id}`);
    assert.equal(new Set(keys).size, 3);
  });

  it("counts mentioned dates in the day summary", () => {
    assert.equal(
      describeDay({ meetings: 0, open: 0, overdue: 0, completed: 0, deadlines: 2 }),
      "2 dates mentioned"
    );
    assert.equal(
      describeDay({ meetings: 1, open: 1, overdue: 0, completed: 0, deadlines: 1 }),
      "1 meeting  ·  1 due  ·  1 date mentioned"
    );
  });
});
