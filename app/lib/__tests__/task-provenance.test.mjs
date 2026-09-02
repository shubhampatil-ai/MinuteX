// lib/__tests__/task-provenance.test.mjs — AI provenance in the task UI, and
// the dashboard filters built on top of it.
//
// Run:  node --test lib/__tests__/task-provenance.test.mjs
//
// WHAT THIS PINS.
//
//   1. THE REVIEW FLAG HAS ONE SOURCE. `needs_review` is computed on the
//      SERVER from resolution_status and now also covers a task whose
//      assignment the confidence gate withheld. If the app re-derived it, the
//      dashboard and the task screen could disagree about which tasks are safe
//      — the card would show a confident assignee for a row the detail screen
//      flags for review. So the rule is: trust the server's flag when it is
//      present, fall back to the local derivation only for an older backend.
//
//   2. CONFIDENCE IS A BAND, NEVER A NUMBER. The backend refuses 0.87 on
//      purpose (it is precision the model did not have), and the UI must not
//      reintroduce it by computing a percentage from the band. These tests pin
//      the label mapping and assert no numeric formatting exists.
//
//   3. AI AFFORDANCES ONLY ON AI TASKS. A manually typed task carries no
//      confidence and no evidence, and must not be dressed up with AI
//      metadata it does not have.
//
//   4. THE NEW FILTERS NARROW CORRECTLY. today/upcoming/others/needs are
//      client-side by necessity (see narrowLocally's comment for why each one
//      cannot be a server query), so their logic is mirrored and tested here.
//
// The modules under test are TypeScript/TSX, so the pure logic is mirrored
// rather than imported — the convention in task-insights.test.mjs and
// notification-center.test.mjs (node --test runs .mjs with no transpile step).
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const ROOT = new URL("../../", import.meta.url).pathname
  .replace(/^\/([A-Za-z]:)/, "$1");

// --- mirrored from lib/task-insights.ts ------------------------------------

/** The local fallback: a name was claimed but not confirmed. */
function needsAssigneeResolution(t) {
  return t.resolution_status === "UNRESOLVED" ||
         t.resolution_status === "AMBIGUOUS";
}

/** The shared helper. Server verdict first, local derivation as fallback. */
function needsAssignment(t) {
  if (typeof t.needs_review === "boolean") return t.needs_review;
  return needsAssigneeResolution(t);
}

// --- mirrored from src/app/task/[id].tsx -----------------------------------

function confidenceLabel(level) {
  return level ? level.charAt(0).toUpperCase() + level.slice(1) : "";
}

function confidenceTone(level) {
  switch (level) {
    case "high": return "success";
    case "medium": return "primary";
    case "low": return "warn";
    default: return "muted";
  }
}

// --- mirrored from src/app/tasks.tsx ---------------------------------------

const DAY = 24 * 60 * 60 * 1000;

function isDayKey(s) { return /^\d{4}-\d{2}-\d{2}$/.test(String(s || "")); }

function dueKeyOf(t) {
  const raw = String(t.due_date || t.due || "").trim();
  return isDayKey(raw) ? raw : "";
}

function daysUntil(dayKey, now) {
  if (!isDayKey(dayKey)) return null;
  const [y, m, d] = dayKey.split("-").map(Number);
  const target = new Date(y, m - 1, d).getTime();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  return Math.round((target - today) / DAY);
}

function isClosed(t) {
  return t.status === "Completed" || t.status === "Cancelled";
}

function narrowLocally(tasks, filter, now, myUserId) {
  switch (filter) {
    case "needs":
      return tasks.filter(needsAssignment);
    case "today":
      return tasks.filter((t) => !isClosed(t) && daysUntil(dueKeyOf(t), now) === 0);
    case "upcoming":
      return tasks.filter((t) => {
        if (isClosed(t)) return false;
        const d = daysUntil(dueKeyOf(t), now);
        return d !== null && d > 0;
      });
    case "others":
      return tasks.filter(
        (t) => !!t.assignee_user_id && t.assignee_user_id !== myUserId
      );
    default:
      return tasks;
  }
}

const task = (over = {}) => ({
  id: "t-1",
  task: "Send the proposal",
  status: "Open",
  due: "",
  due_date: "",
  source_type: "AI",
  resolution_status: "RESOLVED",
  assignee_user_id: "u-ravi",
  ai_confidence: "high",
  ai_evidence: "I'll send the proposal tomorrow.",
  ai_evidence_segment_ids: ["seg_3"],
  needs_review: false,
  ...over,
});

// ---------------------------------------------------------------------------
describe("review flag has one source of truth", () => {
  it("trusts the server's needs_review when present", () => {
    // Even though resolution_status says RESOLVED, the server is the
    // authority — this is the shape a gated task could take after a partial
    // client-side update, and the app must not out-vote the backend.
    assert.equal(needsAssignment(task({ needs_review: true })), true);
    assert.equal(
      needsAssignment(task({ resolution_status: "UNRESOLVED", needs_review: false })),
      false
    );
  });

  it("falls back to the local derivation for an older backend", () => {
    const legacy = task({ resolution_status: "UNRESOLVED" });
    delete legacy.needs_review;
    assert.equal(needsAssignment(legacy), true);

    const ok = task({ resolution_status: "RESOLVED" });
    delete ok.needs_review;
    assert.equal(needsAssignment(ok), false);
  });

  it("does not treat an unassigned task as needing review", () => {
    // NONE is a normal outcome. Treating it as an open question would fill
    // the review queue with work nobody ever claimed.
    assert.equal(
      needsAssignment(task({ resolution_status: "NONE", needs_review: false })),
      false
    );
  });
});

describe("confidence display", () => {
  it("renders the band as a capitalised word", () => {
    assert.equal(confidenceLabel("high"), "High");
    assert.equal(confidenceLabel("medium"), "Medium");
    assert.equal(confidenceLabel("low"), "Low");
  });

  it("renders nothing when the model made no claim", () => {
    // An unscored task must show no badge at all rather than "Unknown",
    // which would read as a judgement the model never made.
    assert.equal(confidenceLabel(""), "");
    assert.equal(confidenceLabel(undefined), "");
  });

  it("gives low confidence a WARN tone, not a danger tone", () => {
    // A low-confidence extraction is a thing to check, not a failure — the
    // task itself may be perfectly real even when its owner is unclear.
    assert.equal(confidenceTone("low"), "warn");
    assert.equal(confidenceTone("high"), "success");
    assert.equal(confidenceTone(""), "muted");
  });
});

describe("dashboard filters", () => {
  const now = new Date(2026, 7, 29, 12, 0, 0);
  const day = (offset) => {
    const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() + offset);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  };

  const set = [
    task({ id: "today", due_date: day(0) }),
    task({ id: "tomorrow", due_date: day(1) }),
    task({ id: "past", due_date: day(-3) }),
    task({ id: "nodue", due_date: "" }),
    task({ id: "done-today", due_date: day(0), status: "Completed" }),
    task({ id: "review", needs_review: true, resolution_status: "UNRESOLVED" }),
    task({ id: "theirs", assignee_user_id: "u-someone" }),
    task({ id: "unassigned", assignee_user_id: "" }),
  ];

  const ids = (f) => narrowLocally(set, f, now, "u-ravi").map((t) => t.id).sort();

  it("today shows only tasks due today and still open", () => {
    assert.deepEqual(ids("today"), ["today"]);
  });

  it("upcoming excludes today, the past and undated tasks", () => {
    assert.deepEqual(ids("upcoming"), ["tomorrow"]);
  });

  it("needs review uses the shared server-driven flag", () => {
    assert.deepEqual(ids("needs"), ["review"]);
  });

  it("assigned to others excludes me AND excludes unassigned", () => {
    // Unassigned is not "assigned to others" — it is assigned to nobody, and
    // lumping the two together would make this filter a dumping ground.
    assert.deepEqual(ids("others"), ["theirs"]);
  });

  it("all returns everything untouched", () => {
    assert.equal(narrowLocally(set, "all", now, "u-ravi").length, set.length);
  });
});

// ---------------------------------------------------------------------------
// SOURCE-LEVEL LINT — the rules nothing else enforces.
// ---------------------------------------------------------------------------
describe("architecture", () => {
  const detail = readFileSync(join(ROOT, "src", "app", "task", "[id].tsx"), "utf8");
  const insights = readFileSync(join(ROOT, "lib", "task-insights.ts"), "utf8");
  const card = readFileSync(join(ROOT, "lib", "task-action-center.tsx"), "utf8");

  it("the app never turns a confidence band into a number", () => {
    // The backend refuses 0.87 precisely because it is precision the model did
    // not have. Recomputing a percentage here would smuggle it back in.
    for (const forbidden of [/confidence\s*\*\s*100/, /toFixed\(\s*\d*\s*\)[^;]*confidence/i,
                             /Math\.round\([^)]*confidence/i, /%.*ai_confidence/]) {
      assert.ok(!forbidden.test(detail), `must not compute a numeric confidence: ${forbidden}`);
    }
  });

  it("the review flag is read from the server, not re-derived", () => {
    assert.match(insights, /needs_review/,
      "needsAssignment must consult the server's needs_review");
  });

  it("the task card uses the shared helper rather than its own rule", () => {
    // Calling needsAssigneeResolution directly here would miss a gated task,
    // and the card would show a confident assignee for a row the detail
    // screen flags for review.
    assert.match(card, /needsPerson = needsAssignment\(task\)/);
  });

  it("the detail screen shows AI metadata only for AI tasks", () => {
    // A manually typed task has no confidence and no evidence, and must not
    // be dressed up with metadata it does not have.
    assert.match(detail, /task\.source_type === "AI" && !!task\.ai_confidence/);
  });

  it("the detail screen surfaces evidence and the review state", () => {
    assert.match(detail, /task\.ai_evidence/);
    assert.match(detail, /task\.needs_review/);
  });
});
