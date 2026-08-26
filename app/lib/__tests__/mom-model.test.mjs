// lib/__tests__/mom-model.test.mjs — the MoM editor's edit semantics.
//
// Run:  node --test lib/__tests__/mom-model.test.mjs
//
// WHAT THIS PINS. Every one of these operations runs while the user is
// editing a document they intend to send to other people, and two of them can
// silently destroy work if they are wrong:
//
//   PROVENANCE — editing AI text must flip its source to `user_edited`. The
//     BACKEND reads that field to decide what a regeneration may overwrite
//     (see mom_schema.merge_generated), so an operation that forgets to mark
//     an edit does not fail here — it fails LATER, by having the user's
//     wording thrown away the next time they tap Regenerate. That is why
//     nearly every test below asserts on `source` as well as on text.
//
//   TOMBSTONES — deleting anything must record its id in `deleted_ids`.
//     An absence is indistinguishable from "an older client didn't send
//     this", so without the tombstone a regeneration resurrects the sections
//     the user deliberately removed.
//
// Also pinned: cells are keyed by COLUMN ID, so deleting a middle column
// cannot shift every later value one place left, and the last column of a
// table cannot be deleted (a table with no columns holds no data, and the
// backend drops its rows on the next save).
//
// The module under test is deliberately pure — no React, no expo, no network
// — precisely so it can be exercised directly. It is TypeScript, so the
// functions are mirrored below rather than imported, matching the convention
// in task-insights.test.mjs and contact-ordering.test.mjs (which mirror their
// subjects for the same reason: `node --test` runs the .mjs suite with no
// transpile step).
import { describe, it } from "node:test";
import assert from "node:assert/strict";

// --- mirrored from lib/mom-model.ts ----------------------------------------

let seq = 0;
function newId(prefix) {
  seq += 1;
  return `${prefix}_u${Date.now().toString(36)}${seq.toString(36)}`;
}

function touch(item) {
  return item.source === "ai" ? { ...item, source: "user_edited" } : item;
}

function withSections(mom, sections) {
  return { ...mom, sections };
}

function tombstone(mom, ...ids) {
  const next = mom.deleted_ids.slice();
  for (const id of ids) if (id && !next.includes(id)) next.push(id);
  return next;
}

function mapSection(mom, sectionId, fn) {
  return withSections(mom, mom.sections.map((s) => (s.id === sectionId ? fn(s) : s)));
}

function addSection(mom, kind, title) {
  const base = {
    id: newId("s"),
    kind,
    title: title.trim() || "Untitled section",
    visible: true,
    source: "user_added",
    role: "",
  };
  let section;
  if (kind === "fields") section = { ...base, fields: [] };
  else if (kind === "table") {
    section = {
      ...base,
      columns: [
        { id: newId("c"), label: "Item", source: "user_added" },
        { id: newId("c"), label: "Owner", source: "user_added" },
      ],
      rows: [],
    };
  } else if (kind === "text") section = { ...base, text: "" };
  else section = { ...base, items: [] };
  return { mom: withSections(mom, [...mom.sections, section]), section };
}

function renameSection(mom, sectionId, title) {
  const trimmed = title.trim();
  if (!trimmed) return mom;
  return mapSection(mom, sectionId, (s) => touch({ ...s, title: trimmed }));
}

function deleteSection(mom, sectionId) {
  return {
    ...mom,
    sections: mom.sections.filter((s) => s.id !== sectionId),
    deleted_ids: tombstone(mom, sectionId),
  };
}

function toggleSectionVisible(mom, sectionId) {
  return mapSection(mom, sectionId, (s) => ({ ...s, visible: !s.visible }));
}

function moveSection(mom, sectionId, delta) {
  const from = mom.sections.findIndex((s) => s.id === sectionId);
  if (from < 0) return mom;
  const to = from + delta;
  if (to < 0 || to >= mom.sections.length) return mom;
  const sections = mom.sections.slice();
  const [moved] = sections.splice(from, 1);
  sections.splice(to, 0, moved);
  return withSections(mom, sections);
}

function addField(mom, sectionId, label = "", value = "") {
  const field = { id: newId("f"), label, value, visible: true, source: "user_added" };
  return mapSection(mom, sectionId, (s) => ({ ...s, fields: [...(s.fields ?? []), field] }));
}

function updateField(mom, sectionId, fieldId, patch) {
  return mapSection(mom, sectionId, (s) => ({
    ...s,
    fields: (s.fields ?? []).map((f) => (f.id === fieldId ? touch({ ...f, ...patch }) : f)),
  }));
}

function deleteField(mom, sectionId, fieldId) {
  return {
    ...mapSection(mom, sectionId, (s) => ({
      ...s, fields: (s.fields ?? []).filter((f) => f.id !== fieldId),
    })),
    deleted_ids: tombstone(mom, fieldId),
  };
}

function toggleFieldVisible(mom, sectionId, fieldId) {
  return mapSection(mom, sectionId, (s) => ({
    ...s,
    fields: (s.fields ?? []).map((f) => (f.id === fieldId ? { ...f, visible: !f.visible } : f)),
  }));
}

function updateText(mom, sectionId, text) {
  return mapSection(mom, sectionId, (s) => touch({ ...s, text }));
}

function addListItem(mom, sectionId, text = "") {
  const item = { id: newId("i"), text, visible: true, source: "user_added" };
  return mapSection(mom, sectionId, (s) => ({ ...s, items: [...(s.items ?? []), item] }));
}

function updateListItem(mom, sectionId, itemId, text) {
  return mapSection(mom, sectionId, (s) => ({
    ...s,
    items: (s.items ?? []).map((i) => (i.id === itemId ? touch({ ...i, text }) : i)),
  }));
}

function deleteListItem(mom, sectionId, itemId) {
  return {
    ...mapSection(mom, sectionId, (s) => ({
      ...s, items: (s.items ?? []).filter((i) => i.id !== itemId),
    })),
    deleted_ids: tombstone(mom, itemId),
  };
}

function addRow(mom, sectionId) {
  return mapSection(mom, sectionId, (s) => {
    const cells = {};
    for (const c of s.columns ?? []) cells[c.id] = "";
    return {
      ...s,
      rows: [...(s.rows ?? []),
             { id: newId("r"), cells, visible: true, source: "user_added" }],
    };
  });
}

function updateCell(mom, sectionId, rowId, columnId, value) {
  return mapSection(mom, sectionId, (s) => ({
    ...s,
    rows: (s.rows ?? []).map((r) => (r.id === rowId
      ? touch({ ...r, cells: { ...r.cells, [columnId]: value } })
      : r)),
  }));
}

function deleteRow(mom, sectionId, rowId) {
  return {
    ...mapSection(mom, sectionId, (s) => ({
      ...s, rows: (s.rows ?? []).filter((r) => r.id !== rowId),
    })),
    deleted_ids: tombstone(mom, rowId),
  };
}

function addColumn(mom, sectionId, label = "Column") {
  return mapSection(mom, sectionId, (s) => {
    const column = { id: newId("c"), label, source: "user_added" };
    const rows = (s.rows ?? []).map((r) => ({
      ...r, cells: { ...r.cells, [column.id]: "" },
    }));
    return { ...s, columns: [...(s.columns ?? []), column], rows };
  });
}

function renameColumn(mom, sectionId, columnId, label) {
  const trimmed = label.trim();
  if (!trimmed) return mom;
  return mapSection(mom, sectionId, (s) => ({
    ...s,
    columns: (s.columns ?? []).map(
      (c) => (c.id === columnId ? touch({ ...c, label: trimmed }) : c)),
  }));
}

function deleteColumn(mom, sectionId, columnId) {
  const section = mom.sections.find((s) => s.id === sectionId);
  if (!section || (section.columns ?? []).length <= 1) return mom;
  return {
    ...mapSection(mom, sectionId, (s) => ({
      ...s,
      columns: (s.columns ?? []).filter((c) => c.id !== columnId),
      rows: (s.rows ?? []).map((r) => {
        const cells = { ...r.cells };
        delete cells[columnId];
        return { ...r, cells };
      }),
    })),
    deleted_ids: tombstone(mom, columnId),
  };
}

function isSectionEmpty(section) {
  switch (section.kind) {
    case "fields":
      return !(section.fields ?? []).some((f) => f.visible && (f.label || f.value));
    case "table":
      return !(section.columns ?? []).length
        || !(section.rows ?? []).some((r) => r.visible);
    case "text":
      return !(section.text ?? "").trim();
    default:
      return !(section.items ?? []).some((i) => i.visible && i.text.trim());
  }
}

function visibleSections(mom) {
  return mom.sections.filter((s) => s.visible && !isSectionEmpty(s));
}

function hasUserEdits(mom) {
  const edited = (s) => s.source !== "ai";
  return mom.sections.some((s) =>
    edited(s)
    || (s.fields ?? []).some(edited)
    || (s.rows ?? []).some(edited)
    || (s.items ?? []).some(edited)
    || (s.columns ?? []).some(edited))
    || mom.deleted_ids.length > 0;
}

// --- fixtures ---------------------------------------------------------------

function aiMom() {
  return {
    title: "Minutes of Meeting",
    subtitle: "Internal Task Portal",
    deleted_ids: [],
    mom_version: "1",
    generated_at: "", updated_at: "", transcript_fingerprint: "",
    speaker_mapping_version: 0,
    sections: [
      {
        id: "s_details", kind: "fields", title: "Meeting Details",
        visible: true, source: "ai", role: "meeting_details",
        fields: [
          { id: "f_date", label: "Date", value: "19 August 2026",
            visible: true, source: "ai" },
          { id: "f_time", label: "Time", value: "3:30 PM - 4:00 PM",
            visible: true, source: "ai" },
        ],
      },
      {
        id: "s_actions", kind: "table", title: "Action Items",
        visible: true, source: "ai", role: "action_items",
        columns: [
          { id: "c_no", label: "Sr. No", source: "ai" },
          { id: "c_task", label: "Action Item", source: "ai" },
          { id: "c_owner", label: "Owner", source: "ai" },
        ],
        rows: [
          { id: "r_0", visible: true, source: "ai",
            cells: { c_no: "1", c_task: "Complete OAuth", c_owner: "Rohan" } },
          { id: "r_1", visible: true, source: "ai",
            cells: { c_no: "2", c_task: "Write migration", c_owner: "Priya" } },
        ],
      },
      {
        id: "s_high", kind: "list", title: "Highlights",
        visible: true, source: "ai", role: "highlights",
        items: [
          { id: "i_0", text: "OAuth is nearly done", visible: true, source: "ai" },
          { id: "i_1", text: "Launch slips a week", visible: true, source: "ai" },
        ],
      },
      {
        id: "s_sum", kind: "text", title: "Summary",
        visible: true, source: "ai", role: "summary",
        text: "The team reviewed progress.",
      },
    ],
  };
}

const find = (mom, id) => mom.sections.find((s) => s.id === id);

// ===========================================================================
describe("provenance — what a regeneration is allowed to overwrite", () => {
  it("editing an AI field marks it user_edited", () => {
    const next = updateField(aiMom(), "s_details", "f_date", { value: "1 Jan 2027" });
    const field = find(next, "s_details").fields[0];
    assert.equal(field.value, "1 Jan 2027");
    assert.equal(field.source, "user_edited");
  });

  it("renaming a field marks it user_edited too", () => {
    // The LABEL is content: a user who renames "Time" to "Slot" must not have
    // it renamed back by the next generation.
    const next = updateField(aiMom(), "s_details", "f_time", { label: "Slot" });
    assert.equal(find(next, "s_details").fields[1].source, "user_edited");
  });

  it("editing AI prose marks the section user_edited", () => {
    const next = updateText(aiMom(), "s_sum", "My own summary");
    assert.equal(find(next, "s_sum").source, "user_edited");
  });

  it("editing one cell marks its row, and no other row", () => {
    const next = updateCell(aiMom(), "s_actions", "r_0", "c_owner", "Meera");
    const rows = find(next, "s_actions").rows;
    assert.equal(rows[0].source, "user_edited");
    assert.equal(rows[1].source, "ai", "an untouched row must stay refreshable");
  });

  it("editing a list item marks it, and no sibling", () => {
    const next = updateListItem(aiMom(), "s_high", "i_0", "Rewritten");
    const items = find(next, "s_high").items;
    assert.equal(items[0].source, "user_edited");
    assert.equal(items[1].source, "ai");
  });

  it("renaming a section marks it user_edited", () => {
    const next = renameSection(aiMom(), "s_actions", "Follow-ups");
    assert.equal(find(next, "s_actions").source, "user_edited");
  });

  it("user_added content is never downgraded to user_edited", () => {
    // It has no AI original to be an edit OF.
    let mom = addField(aiMom(), "s_details", "Client", "ABC");
    const id = find(mom, "s_details").fields.at(-1).id;
    mom = updateField(mom, "s_details", id, { value: "XYZ" });
    assert.equal(find(mom, "s_details").fields.at(-1).source, "user_added");
  });

  it("everything the user creates is user_added", () => {
    const { mom, section } = addSection(aiMom(), "list", "Client Notes");
    assert.equal(section.source, "user_added");
    assert.equal(section.role, "", "no catalogue role — a regeneration must leave it alone");
    let next = addListItem(mom, section.id, "A point");
    assert.equal(find(next, section.id).items[0].source, "user_added");
    next = addRow(addColumn(next, "s_actions"), "s_actions");
    assert.equal(find(next, "s_actions").rows.at(-1).source, "user_added");
  });

  it("hiding is not an edit to the text, but is still tracked", () => {
    // Visibility must survive a regeneration without freezing the content.
    const next = toggleFieldVisible(aiMom(), "s_details", "f_date");
    const field = find(next, "s_details").fields[0];
    assert.equal(field.visible, false);
    assert.equal(field.source, "ai");
  });

  it("hasUserEdits sees an untouched MoM as clean", () => {
    assert.equal(hasUserEdits(aiMom()), false);
    assert.equal(hasUserEdits(updateText(aiMom(), "s_sum", "mine")), true);
    assert.equal(hasUserEdits(deleteSection(aiMom(), "s_sum")), true);
  });
});

describe("tombstones — a deletion must survive a regeneration", () => {
  it("deleting a section records its id", () => {
    const next = deleteSection(aiMom(), "s_high");
    assert.equal(find(next, "s_high"), undefined);
    assert.ok(next.deleted_ids.includes("s_high"));
  });

  it("deleting a field, row, item and column each record an id", () => {
    let mom = deleteField(aiMom(), "s_details", "f_date");
    mom = deleteRow(mom, "s_actions", "r_0");
    mom = deleteListItem(mom, "s_high", "i_1");
    mom = deleteColumn(mom, "s_actions", "c_owner");
    for (const id of ["f_date", "r_0", "i_1", "c_owner"]) {
      assert.ok(mom.deleted_ids.includes(id), `${id} not tombstoned`);
    }
  });

  it("deleting twice does not duplicate the tombstone", () => {
    let mom = deleteSection(aiMom(), "s_high");
    mom = deleteSection(mom, "s_high");
    assert.equal(mom.deleted_ids.filter((id) => id === "s_high").length, 1);
  });
});

describe("tables", () => {
  it("adding a column gives every existing row a blank cell", () => {
    // A ragged row would render short in the document.
    const next = addColumn(aiMom(), "s_actions", "Deadline");
    const section = find(next, "s_actions");
    const added = section.columns.at(-1);
    assert.equal(added.label, "Deadline");
    for (const row of section.rows) {
      assert.equal(row.cells[added.id], "");
    }
  });

  it("deleting a middle column does not shift the other cells", () => {
    // THE regression cell-keying prevents. With position-keyed cells, removing
    // "Action Item" would move every Owner one place left.
    const before = find(aiMom(), "s_actions");
    const owners = before.rows.map((r) => r.cells.c_owner);
    const next = deleteColumn(aiMom(), "s_actions", "c_task");
    const section = find(next, "s_actions");
    assert.deepEqual(section.columns.map((c) => c.id), ["c_no", "c_owner"]);
    assert.deepEqual(section.rows.map((r) => r.cells.c_owner), owners);
    for (const row of section.rows) {
      assert.equal("c_task" in row.cells, false, "orphaned cell left behind");
    }
  });

  it("the last column cannot be deleted", () => {
    // A table with no columns holds no data and the backend drops its rows —
    // so allowing this would quietly destroy the section's content.
    let mom = deleteColumn(aiMom(), "s_actions", "c_task");
    mom = deleteColumn(mom, "s_actions", "c_owner");
    const before = find(mom, "s_actions").columns.length;
    assert.equal(before, 1);
    const after = deleteColumn(mom, "s_actions", "c_no");
    assert.equal(find(after, "s_actions").columns.length, 1);
    assert.equal(after.deleted_ids.includes("c_no"), false);
  });

  it("a new row has a cell for every column", () => {
    const next = addRow(addColumn(aiMom(), "s_actions", "Deadline"), "s_actions");
    const section = find(next, "s_actions");
    const row = section.rows.at(-1);
    assert.deepEqual(Object.keys(row.cells).sort(),
                     section.columns.map((c) => c.id).sort());
  });

  it("renaming a column keeps every cell attached to it", () => {
    const next = renameColumn(aiMom(), "s_actions", "c_owner", "Responsible");
    const section = find(next, "s_actions");
    assert.equal(section.columns[2].label, "Responsible");
    assert.equal(section.rows[0].cells.c_owner, "Rohan");
  });

  it("a blank rename is ignored rather than blanking the header", () => {
    const next = renameColumn(aiMom(), "s_actions", "c_owner", "   ");
    assert.equal(find(next, "s_actions").columns[2].label, "Owner");
  });
});

describe("section order and visibility", () => {
  it("moving a section reorders it", () => {
    const next = moveSection(aiMom(), "s_actions", -1);
    assert.deepEqual(next.sections.map((s) => s.id),
                     ["s_actions", "s_details", "s_high", "s_sum"]);
  });

  it("moving past either end is a no-op, not a crash", () => {
    const mom = aiMom();
    assert.deepEqual(moveSection(mom, "s_details", -1).sections.map((s) => s.id),
                     mom.sections.map((s) => s.id));
    assert.deepEqual(moveSection(mom, "s_sum", 1).sections.map((s) => s.id),
                     mom.sections.map((s) => s.id));
  });

  it("moving an unknown section is a no-op", () => {
    const mom = aiMom();
    assert.deepEqual(moveSection(mom, "nope", 1).sections, mom.sections);
  });

  it("a hidden section is excluded from the document", () => {
    const next = toggleSectionVisible(aiMom(), "s_sum");
    assert.equal(visibleSections(next).some((s) => s.id === "s_sum"), false);
    assert.ok(find(next, "s_sum"), "but it stays in the structure to unhide");
  });

  it("an empty section is excluded from the document", () => {
    // An empty "## Decisions" heading reads as a mistake, not as "none".
    const { mom, section } = addSection(aiMom(), "list", "Empty");
    assert.equal(visibleSections(mom).some((s) => s.id === section.id), false);
    const filled = addListItem(mom, section.id, "Now it has content");
    assert.ok(visibleSections(filled).some((s) => s.id === section.id));
  });

  it("a section whose only item is blank still counts as empty", () => {
    const { mom, section } = addSection(aiMom(), "list", "Blank");
    const withBlank = addListItem(mom, section.id, "   ");
    assert.equal(visibleSections(withBlank).some((s) => s.id === section.id), false);
  });
});

describe("immutability — an edit must not mutate what React already rendered", () => {
  it("no operation mutates its input", () => {
    const mom = aiMom();
    const snapshot = JSON.stringify(mom);
    updateField(mom, "s_details", "f_date", { value: "x" });
    updateCell(mom, "s_actions", "r_0", "c_owner", "x");
    updateListItem(mom, "s_high", "i_0", "x");
    updateText(mom, "s_sum", "x");
    deleteSection(mom, "s_sum");
    deleteRow(mom, "s_actions", "r_0");
    deleteColumn(mom, "s_actions", "c_owner");
    addColumn(mom, "s_actions", "New");
    addRow(mom, "s_actions");
    addSection(mom, "text", "New");
    moveSection(mom, "s_actions", -1);
    assert.equal(JSON.stringify(mom), snapshot);
  });

  it("untouched sections keep their identity so React can skip them", () => {
    const mom = aiMom();
    const next = updateText(mom, "s_sum", "changed");
    assert.equal(find(next, "s_details"), find(mom, "s_details"));
    assert.notEqual(find(next, "s_sum"), find(mom, "s_sum"));
  });
});

describe("new sections", () => {
  it("a new table starts with columns, so rows can hold something", () => {
    const { section } = addSection(aiMom(), "table", "Costs");
    assert.equal(section.columns.length, 2);
    assert.deepEqual(section.rows, []);
  });

  it("a blank title falls back rather than producing an unnamed section", () => {
    const { section } = addSection(aiMom(), "text", "   ");
    assert.equal(section.title, "Untitled section");
  });

  it("ids are unique across rapid additions", () => {
    // Two sections added in the same millisecond must not collide, or the
    // editor would edit both at once.
    let mom = aiMom();
    const ids = new Set();
    for (let i = 0; i < 50; i++) {
      const out = addSection(mom, "text", `S${i}`);
      mom = out.mom;
      ids.add(out.section.id);
    }
    assert.equal(ids.size, 50);
  });
});
