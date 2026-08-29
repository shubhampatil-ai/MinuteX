// lib/__tests__/task-search-collapse.test.mjs — narrowing and truncating the
// attention list.
//
// Run:  node --test lib/__tests__/task-search-collapse.test.mjs
//
// WHAT THIS PINS. The task dashboard now shows only the TOP few tasks and
// hides the rest behind "Show all", with a search box above it. Both of those
// can lie in ways that are hard to notice:
//
//   * TRUNCATION MUST COME LAST. If the list is cut to five BEFORE ranking or
//     searching, the five rows on screen are "the first five that loaded",
//     not "the five that matter" — and "Show all 23" would count the wrong
//     set. The order filter -> search -> rank -> truncate is the whole
//     correctness story of this feature.
//   * SEARCH MUST NOT HIDE MATCHES. Someone who typed a query has already
//     narrowed the list; collapsing their results behind a second tap answers
//     a question with part of the answer.
//   * SEARCH SCOPE IS THE LOADED SET. There is no server-side text search on
//     GET /tasks, so the query runs over whatever paging has fetched. The
//     screen states that; these tests pin the matching itself, including the
//     assignee shapes a person would actually search by (a resolved contact
//     name, an unresolved AI name, a speaker label).
//
// The subject is TypeScript inside a React screen, so the two pure functions
// are mirrored here rather than imported — the convention in
// task-insights.test.mjs and contact-ordering.test.mjs, for the same reason:
// `node --test` runs .mjs with no transpile step.
import { describe, it } from "node:test";
import assert from "node:assert/strict";

// --- mirrored from src/app/tasks.tsx ---------------------------------------

const COLLAPSED_ROWS = 5;

function matchesQuery(tasks, query) {
  const q = query.trim().toLowerCase();
  if (!q) return tasks;
  return tasks.filter((t) => {
    const haystack = [
      t.title || t.task || "",
      t.description || "",
      t.assignee?.name || "",
      t.assignee_name || "",
      t.assignee_name_legacy || "",
      t.speaker_name || "",
    ];
    return haystack.some((h) => h.toLowerCase().includes(q));
  });
}

/** The truncation decision, exactly as the screen computes it. */
function truncate(matched, { expanded, query }) {
  const truncated = !expanded && !query && matched.length > COLLAPSED_ROWS;
  return {
    truncated,
    visible: truncated ? matched.slice(0, COLLAPSED_ROWS) : matched,
  };
}

// --- helpers ---------------------------------------------------------------

let seq = 0;
function task(over = {}) {
  seq += 1;
  return {
    id: `t-${seq}`,
    task: `Task ${seq}`,
    due: "",
    priority: "Medium",
    status: "Open",
    assignee: null,
    notified_via: [],
    created_at: "",
    updated_at: "",
    from_action_item: false,
    ...over,
  };
}

function many(n) {
  return Array.from({ length: n }, (_, i) =>
    task({ id: `m-${i}`, task: `Item ${i}` })
  );
}

// ===========================================================================
describe("matchesQuery", () => {
  it("returns everything for an empty or whitespace query", () => {
    const list = many(3);
    assert.equal(matchesQuery(list, "").length, 3);
    assert.equal(matchesQuery(list, "   ").length, 3);
  });

  it("matches the task text case-insensitively", () => {
    const list = [
      task({ task: "Send the proposal" }),
      task({ task: "Book the venue" }),
    ];
    assert.equal(matchesQuery(list, "PROPOSAL").length, 1);
    assert.equal(matchesQuery(list, "proposal")[0].task, "Send the proposal");
  });

  it("prefers title over the legacy task field when both exist", () => {
    // `title` is the first-class field; `task` is the backward-compatible
    // alias. Searching must read the one the card actually renders.
    const list = [task({ title: "Renew the licence", task: "stale text" })];
    assert.equal(matchesQuery(list, "renew").length, 1);
    assert.equal(matchesQuery(list, "stale").length, 0);
  });

  it("matches a resolved contact assignee", () => {
    const list = [
      task({ assignee: { name: "Rahul Sharma", source: "manual" } }),
      task({ assignee: { name: "Priya Nair", source: "manual" } }),
    ];
    assert.equal(matchesQuery(list, "rahul").length, 1);
  });

  it("matches an UNRESOLVED AI name", () => {
    // The whole point of the three-state assignee: a name the AI heard is
    // still what a person would type to find the task.
    const list = [task({ assignee_name_legacy: "Rahul" })];
    assert.equal(matchesQuery(list, "rahul").length, 1);
  });

  it("matches a speaker label and its resolved name", () => {
    const unnamed = [task({ speaker_name: "Speaker 2" })];
    assert.equal(matchesQuery(unnamed, "speaker 2").length, 1);
    const named = [task({ speaker_name: "Siddhesh Gawade" })];
    assert.equal(matchesQuery(named, "siddhesh").length, 1);
  });

  it("matches the description", () => {
    const list = [task({ description: "needs the signed NDA first" })];
    assert.equal(matchesQuery(list, "nda").length, 1);
  });

  it("returns nothing when there is no match, rather than everything", () => {
    // A search that silently falls back to the full list is worse than an
    // empty result: it reads as "here it is".
    assert.equal(matchesQuery(many(4), "zzzz").length, 0);
  });

  it("tolerates missing optional fields", () => {
    const bare = { id: "x", task: "", assignee: null };
    assert.doesNotThrow(() => matchesQuery([bare], "anything"));
    assert.equal(matchesQuery([bare], "anything").length, 0);
  });
});

// ===========================================================================
describe("truncation", () => {
  it("shows every task when there are fewer than the cut", () => {
    const { truncated, visible } = truncate(many(3), {
      expanded: false,
      query: "",
    });
    assert.equal(truncated, false);
    assert.equal(visible.length, 3);
  });

  it("shows exactly the cut without truncating at the boundary", () => {
    // 5 of 5 is not "more" — an off-by-one here would render a "Show all 5"
    // control that reveals nothing.
    const { truncated, visible } = truncate(many(COLLAPSED_ROWS), {
      expanded: false,
      query: "",
    });
    assert.equal(truncated, false);
    assert.equal(visible.length, COLLAPSED_ROWS);
  });

  it("truncates to the cut once there is one more than fits", () => {
    const { truncated, visible } = truncate(many(COLLAPSED_ROWS + 1), {
      expanded: false,
      query: "",
    });
    assert.equal(truncated, true);
    assert.equal(visible.length, COLLAPSED_ROWS);
  });

  it("keeps the TOP of the list, which is the ranked end", () => {
    // Truncation slices from the front, so whatever rankForAttention put
    // first is what survives. This is why the screen truncates AFTER ranking.
    const ranked = many(10);
    const { visible } = truncate(ranked, { expanded: false, query: "" });
    assert.deepEqual(
      visible.map((t) => t.id),
      ["m-0", "m-1", "m-2", "m-3", "m-4"]
    );
  });

  it("expanding reveals the whole list", () => {
    const { truncated, visible } = truncate(many(23), {
      expanded: true,
      query: "",
    });
    assert.equal(truncated, false);
    assert.equal(visible.length, 23);
  });

  it("a search shows every match, never a truncated subset", () => {
    // Someone who typed a query has already narrowed the list themselves.
    const { truncated, visible } = truncate(many(20), {
      expanded: false,
      query: "item",
    });
    assert.equal(truncated, false);
    assert.equal(visible.length, 20);
  });
});

// ===========================================================================
describe("the pipeline order", () => {
  // filter -> search -> rank -> truncate. These pin that the count shown on
  // "Show all N" describes the SEARCHED set, not the raw loaded page.
  it("counts matches, not loaded tasks", () => {
    const list = [
      ...many(20),
      task({ task: "Send the proposal" }),
      task({ task: "Proposal follow-up" }),
    ];
    const matched = matchesQuery(list, "proposal");
    assert.equal(matched.length, 2);
    const { truncated, visible } = truncate(matched, {
      expanded: false,
      query: "proposal",
    });
    assert.equal(truncated, false);
    assert.equal(visible.length, 2);
  });

  it("truncating a searched set would hide matches — so it must not", () => {
    // 8 hits with a cut of 5: if truncation ran on search results, three
    // matching tasks would silently vanish.
    const list = Array.from({ length: 8 }, () =>
      task({ task: "proposal work" })
    );
    const matched = matchesQuery(list, "proposal");
    assert.equal(matched.length, 8);
    const { visible } = truncate(matched, {
      expanded: false,
      query: "proposal",
    });
    assert.equal(visible.length, 8);
  });

  it("clearing the search restores truncation", () => {
    const list = many(12);
    const cleared = truncate(matchesQuery(list, ""), {
      expanded: false,
      query: "",
    });
    assert.equal(cleared.truncated, true);
    assert.equal(cleared.visible.length, COLLAPSED_ROWS);
  });
});
