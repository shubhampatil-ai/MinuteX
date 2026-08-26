// lib/__tests__/meeting-overview.test.mjs — the dynamic Overview's rendering
// decisions.
//
// Run:  node --test lib/__tests__/meeting-overview.test.mjs
//
// WHAT THIS PINS, and why each one matters:
//
//   NO FIXED SECTIONS. The whole point of the dynamic overview is that the
//     MODEL picks the sections — titles, count, order — per meeting. The
//     renderer must therefore be title-blind: it renders whatever arrived, in
//     the order it arrived. A regression here does not crash; it quietly
//     reintroduces the template the feature exists to remove, or silently
//     drops any section title nobody anticipated. Neither fails loudly, so
//     they are pinned here.
//
//   EMPTINESS. A heading with nothing under it reads to a user as a finding
//     ("Risks: —" looks like "risks were considered"). The backend already
//     drops empty sections; this is the client half of the same rule, for a
//     stale row or an older backend.
//
//   KIND FOLLOWS CONTENT. The model mislabels `kind` routinely. A section
//     that says "text" but carries only items must still render its items —
//     otherwise the card renders blank and the meeting silently loses a
//     section.
//
//   LEGACY. A recording analysed before the overview shipped has no sections
//     at all, only the old `summary`/`highlights`. hasOverview() is what the
//     screen branches on, so it must be false for every shape that has
//     nothing renderable — including a section list that is non-empty but all
//     padding.
//
// The functions under test are pure and mirrored below rather than imported,
// matching the convention in mom-model.test.mjs and task-insights.test.mjs
// (`node --test` runs .mjs with no TypeScript/JSX transform, and the module
// itself is a .tsx component file).
import test from "node:test";
import assert from "node:assert/strict";

// ---------------------------------------------------------------------------
// MIRRORS of lib/meeting-overview.tsx's pure logic. Keep in sync.
// ---------------------------------------------------------------------------
const KIND_TEXT = "text";
const KIND_LIST = "list";

/** Mirror of hasContent(). */
function hasContent(s) {
  if (!s?.title?.trim()) return false;
  return !!s.content?.trim() || !!s.items?.some((i) => i?.trim());
}

/** Mirror of hasOverview(). */
function hasOverview(overview) {
  return (overview?.sections ?? []).some(hasContent);
}

/** Mirror of MeetingOverviewView's section selection — what actually renders,
 *  in render order. */
function renderedSections(overview) {
  return (overview?.sections ?? []).filter(hasContent);
}

/** Mirror of OverviewSectionCard's kind decision. */
function renderKind(section) {
  const text = (section.content ?? "").trim();
  const items = (section.items ?? []).map((i) => (i ?? "").trim()).filter(Boolean);
  return items.length > 0 && !text ? KIND_LIST : KIND_TEXT;
}

/** Mirror of the list key: stable id, falling back to position. Never the
 *  title — two sections can share one, and a duplicate React key silently
 *  drops a card. */
function renderKey(section, index) {
  return section.id || `section_${index}`;
}

const section = (over = {}) => ({
  id: "section_0", title: "Pricing", kind: KIND_TEXT, content: "Quoted 4.2 lakh.",
  items: [], source: "ai", evidence_segment_ids: [], ...over,
});

// ===========================================================================
// 1. THE DYNAMIC CONTRACT — no title is special, order is the AI's
// ===========================================================================
test("renders whatever sections arrived, in the order they arrived", () => {
  // Two meetings with nothing in common. Both must render fully — the
  // renderer has no opinion about either set of titles.
  const technical = {
    sections: [
      section({ id: "s0", title: "Project Context" }),
      section({ id: "s1", title: "Technical Architecture" }),
      section({ id: "s2", title: "Panel Feedback" }),
      section({ id: "s3", title: "Hardware Concerns" }),
    ],
  };
  const sales = {
    sections: [
      section({ id: "s0", title: "Customer Requirements" }),
      section({ id: "s1", title: "Pain Points" }),
      section({ id: "s2", title: "Pricing Discussion" }),
      section({ id: "s3", title: "Objections" }),
    ],
  };

  assert.deepEqual(renderedSections(technical).map((s) => s.title), [
    "Project Context", "Technical Architecture", "Panel Feedback",
    "Hardware Concerns",
  ]);
  assert.deepEqual(renderedSections(sales).map((s) => s.title), [
    "Customer Requirements", "Pain Points", "Pricing Discussion", "Objections",
  ]);
});

test("an unfamiliar section title renders like any other", () => {
  // The failure this guards: a renderer with a title switch drops anything not
  // in the switch. A title nobody has ever seen must still render.
  const overview = {
    sections: [section({ title: "Rebar Tolerance Dispute" })],
  };
  assert.equal(renderedSections(overview).length, 1);
  assert.equal(renderedSections(overview)[0].title, "Rebar Tolerance Dispute");
});

test("no section count is imposed on either end", () => {
  // One substantive section is a complete overview for a single-topic meeting;
  // nine is fine for a dense one. The renderer must not pad or truncate.
  const one = { sections: [section()] };
  assert.equal(renderedSections(one).length, 1);

  const many = {
    sections: Array.from({ length: 9 }, (_, i) =>
      section({ id: `s${i}`, title: `Topic ${i}`, content: `c${i}` })),
  };
  assert.equal(renderedSections(many).length, 9);
});

// ===========================================================================
// 2. EMPTINESS — no heading without something under it
// ===========================================================================
test("a section with no content is not rendered", () => {
  const overview = {
    sections: [
      section({ id: "s0", title: "Real", content: "something" }),
      section({ id: "s1", title: "Padded", content: "", items: [] }),
      section({ id: "s2", title: "Whitespace", content: "   ", items: ["  "] }),
    ],
  };
  assert.deepEqual(renderedSections(overview).map((s) => s.title), ["Real"]);
});

test("a section with no title is not rendered", () => {
  // Orphan text with no heading has nowhere to go in the layout.
  const overview = {
    sections: [section({ title: "", content: "orphaned" })],
  };
  assert.deepEqual(renderedSections(overview), []);
});

test("a list section with items but no prose still renders", () => {
  const overview = {
    sections: [section({ content: "", items: ["a", "b"], kind: KIND_LIST })],
  };
  assert.equal(renderedSections(overview).length, 1);
});

// ===========================================================================
// 3. KIND FOLLOWS CONTENT, not the model's label
// ===========================================================================
test("a section labelled text but carrying only items renders as a list", () => {
  assert.equal(
    renderKind(section({ kind: KIND_TEXT, content: "", items: ["only items"] })),
    KIND_LIST
  );
});

test("a section labelled list but carrying only prose renders as text", () => {
  assert.equal(
    renderKind(section({ kind: KIND_LIST, content: "only prose", items: [] })),
    KIND_TEXT
  );
});

test("a section with BOTH prose and items renders as text so neither is lost", () => {
  // The card shows the prose as a lead-in with the bullets beneath it. Choosing
  // "list" here would drop the prose entirely.
  assert.equal(
    renderKind(section({ content: "lead-in", items: ["a", "b"] })),
    KIND_TEXT
  );
});

test("a nonsense kind still renders by content", () => {
  assert.equal(renderKind(section({ kind: "sparkles", content: "prose" })),
               KIND_TEXT);
  assert.equal(
    renderKind(section({ kind: "sparkles", content: "", items: ["x"] })),
    KIND_LIST
  );
});

// ===========================================================================
// 4. KEYS — a duplicate React key silently drops a card
// ===========================================================================
test("sections are keyed by id, not by title", () => {
  const a = section({ id: "section_0", title: "Next Steps" });
  const b = section({ id: "section_1", title: "Next Steps" });
  assert.notEqual(renderKey(a, 0), renderKey(b, 1));
});

test("a section with no id falls back to its position", () => {
  assert.equal(renderKey(section({ id: "" }), 3), "section_3");
  assert.equal(renderKey(section({ id: undefined }), 0), "section_0");
});

// ===========================================================================
// 5. LEGACY AND MALFORMED — an old recording must not crash the screen
// ===========================================================================
test("hasOverview is false for every shape with nothing to render", () => {
  for (const empty of [
    undefined, null, {}, { sections: null }, { sections: [] },
    // Non-empty list, but every section is padding — the case a naive
    // `sections.length > 0` check gets wrong, showing an Overview tab with
    // nothing in it instead of falling back to the legacy summary.
    { sections: [section({ title: "Padded", content: "", items: [] })] },
    { sections: [section({ title: "", content: "orphan" })] },
  ]) {
    assert.equal(hasOverview(empty), false, JSON.stringify(empty));
  }
});

test("hasOverview is true as soon as one section is renderable", () => {
  const overview = {
    sections: [
      section({ id: "s0", title: "Padded", content: "", items: [] }),
      section({ id: "s1", title: "Real", content: "substance" }),
    ],
  };
  assert.equal(hasOverview(overview), true);
  // ...and only the renderable one is shown.
  assert.deepEqual(renderedSections(overview).map((s) => s.title), ["Real"]);
});

test("hasOverview and renderedSections always agree", () => {
  // These two are the screen's branch and the renderer's output. If they can
  // disagree, a meeting shows an empty Overview tab (hasOverview true, nothing
  // rendered) or hides a real one (false, sections present).
  for (const overview of [
    undefined, {}, { sections: [] },
    { sections: [section()] },
    { sections: [section({ title: "", content: "x" })] },
    { sections: [section({ content: "", items: [] })] },
    { sections: [section({ content: "", items: ["a"] })] },
  ]) {
    assert.equal(hasOverview(overview), renderedSections(overview).length > 0,
                 JSON.stringify(overview));
  }
});

test("malformed section fields do not throw", () => {
  // Straight from DynamoDB, possibly written by older code.
  for (const bad of [
    { sections: [{ title: "A" }] },                       // no content/items
    { sections: [{ title: "A", content: "x" }] },          // no items array
    { sections: [{ title: "A", items: ["x"] }] },           // no content
    { sections: [{ title: "A", content: "x", items: [null, undefined] }] },
  ]) {
    assert.doesNotThrow(() => renderedSections(bad));
    assert.doesNotThrow(() => hasOverview(bad));
    for (const s of bad.sections) assert.doesNotThrow(() => renderKind(s));
  }
});
