// lib/mom-model.ts — pure edit operations on a structured MoM.
//
// WHY A SEPARATE MODULE. Every mutation the editor performs is expressed here
// as a pure function: take a Mom, return a new Mom. No React, no network, no
// expo — which is what lets lib/__tests__/mom-model.test.mjs verify the edit
// semantics directly, the same way task-insights.ts is kept pure so its
// arithmetic can be pinned (see that file's test header).
//
// TWO INVARIANTS EVERY OPERATION HERE MUST HOLD, because getting either wrong
// silently loses the user's work:
//
//   1. PROVENANCE. Editing AI text flips its source to `user_edited`; content
//      the user creates is `user_added`. The backend's merge reads exactly
//      these to decide what a regeneration may overwrite, so an operation
//      that forgets to mark an edit will have that edit thrown away the next
//      time the user taps Regenerate. `touch()` is the single place that
//      decision is made — no call site sets `source` by hand.
//
//   2. TOMBSTONES. Deleting anything records its id in `deleted_ids` rather
//      than just dropping it. An absence is indistinguishable from "an older
//      client didn't send this", so without the tombstone every regeneration
//      would resurrect the sections the user deliberately removed.
//
// Cells are keyed by COLUMN ID throughout, never by position — deleting a
// middle column would otherwise shift every later value one place left, which
// is the single most likely way for a table edit to corrupt data.
import type {
  Mom, MomColumn, MomField, MomListItem, MomRow, MomSection, MomSectionKind,
  MomSource,
} from "./api";

// ---------------------------------------------------------------------------
// Ids. Client-minted ids only ever name USER-ADDED content; everything the AI
// produced already arrived with a stable, content-derived id from the backend.
// A collision here would make the editor edit two rows at once, so the counter
// makes ids unique within a session and the prefix keeps them distinguishable
// from the backend's.
// ---------------------------------------------------------------------------
let seq = 0;
export function newId(prefix: string): string {
  seq += 1;
  return `${prefix}_u${Date.now().toString(36)}${seq.toString(36)}`;
}

/** Promote `ai` content to `user_edited`; leave user content as it is.
 * The ONE place provenance is decided — see invariant 1 above. */
function touch<T extends { source: MomSource }>(item: T): T {
  return item.source === "ai" ? { ...item, source: "user_edited" } : item;
}

function withSections(mom: Mom, sections: MomSection[]): Mom {
  return { ...mom, sections };
}

/** Record a deletion so a later regeneration cannot undo it. */
function tombstone(mom: Mom, ...ids: string[]): string[] {
  const next = mom.deleted_ids.slice();
  for (const id of ids) if (id && !next.includes(id)) next.push(id);
  return next;
}

function mapSection(
  mom: Mom, sectionId: string, fn: (s: MomSection) => MomSection
): Mom {
  return withSections(mom, mom.sections.map(
    (s) => (s.id === sectionId ? fn(s) : s)));
}

// ---------------------------------------------------------------------------
// Sections
// ---------------------------------------------------------------------------
export function addSection(
  mom: Mom, kind: MomSectionKind, title: string
): { mom: Mom; section: MomSection } {
  const base = {
    id: newId("s"),
    kind,
    title: title.trim() || "Untitled section",
    visible: true,
    // A section the user created has no catalogue role, which is exactly what
    // tells the backend's merge to leave it alone forever.
    source: "user_added" as const,
    role: "",
  };
  let section: MomSection;
  if (kind === "fields") {
    section = { ...base, fields: [] };
  } else if (kind === "table") {
    // A table with no columns can hold nothing and the backend drops its rows,
    // so a new one starts with two — enough to be useful, few enough to fit a
    // phone screen without horizontal scrolling.
    const columns: MomColumn[] = [
      { id: newId("c"), label: "Item", source: "user_added" },
      { id: newId("c"), label: "Owner", source: "user_added" },
    ];
    section = { ...base, columns, rows: [] };
  } else if (kind === "text") {
    section = { ...base, text: "" };
  } else {
    section = { ...base, items: [] };
  }
  return { mom: withSections(mom, [...mom.sections, section]), section };
}

export function renameSection(mom: Mom, sectionId: string, title: string): Mom {
  const trimmed = title.trim();
  if (!trimmed) return mom;
  return mapSection(mom, sectionId, (s) => touch({ ...s, title: trimmed }));
}

export function deleteSection(mom: Mom, sectionId: string): Mom {
  return {
    ...mom,
    sections: mom.sections.filter((s) => s.id !== sectionId),
    deleted_ids: tombstone(mom, sectionId),
  };
}

/** Hide, rather than delete — the section stays in the structure so it can be
 * brought back, but renders in no document. */
export function toggleSectionVisible(mom: Mom, sectionId: string): Mom {
  return mapSection(mom, sectionId, (s) => ({ ...s, visible: !s.visible }));
}

/** Move a section one place up or down. Order IS list position — there is no
 * separate `order` field to keep in step. */
export function moveSection(mom: Mom, sectionId: string, delta: number): Mom {
  const from = mom.sections.findIndex((s) => s.id === sectionId);
  if (from < 0) return mom;
  const to = from + delta;
  if (to < 0 || to >= mom.sections.length) return mom;
  const sections = mom.sections.slice();
  const [moved] = sections.splice(from, 1);
  sections.splice(to, 0, moved);
  return withSections(mom, sections);
}

// ---------------------------------------------------------------------------
// Fields
// ---------------------------------------------------------------------------
export function addField(mom: Mom, sectionId: string, label = "", value = ""): Mom {
  const field: MomField = {
    id: newId("f"), label, value, visible: true, source: "user_added",
  };
  return mapSection(mom, sectionId, (s) => ({
    ...s, fields: [...(s.fields ?? []), field],
  }));
}

export function updateField(
  mom: Mom, sectionId: string, fieldId: string, patch: Partial<MomField>
): Mom {
  return mapSection(mom, sectionId, (s) => ({
    ...s,
    fields: (s.fields ?? []).map(
      (f) => (f.id === fieldId ? touch({ ...f, ...patch }) : f)),
  }));
}

export function deleteField(mom: Mom, sectionId: string, fieldId: string): Mom {
  return {
    ...mapSection(mom, sectionId, (s) => ({
      ...s, fields: (s.fields ?? []).filter((f) => f.id !== fieldId),
    })),
    deleted_ids: tombstone(mom, fieldId),
  };
}

export function toggleFieldVisible(
  mom: Mom, sectionId: string, fieldId: string
): Mom {
  return mapSection(mom, sectionId, (s) => ({
    ...s,
    fields: (s.fields ?? []).map(
      (f) => (f.id === fieldId ? { ...f, visible: !f.visible } : f)),
  }));
}

// ---------------------------------------------------------------------------
// Text and list sections
// ---------------------------------------------------------------------------
export function updateText(mom: Mom, sectionId: string, text: string): Mom {
  return mapSection(mom, sectionId, (s) => touch({ ...s, text }));
}

export function addListItem(mom: Mom, sectionId: string, text = ""): Mom {
  const item: MomListItem = {
    id: newId("i"), text, visible: true, source: "user_added",
  };
  return mapSection(mom, sectionId, (s) => ({
    ...s, items: [...(s.items ?? []), item],
  }));
}

export function updateListItem(
  mom: Mom, sectionId: string, itemId: string, text: string
): Mom {
  return mapSection(mom, sectionId, (s) => ({
    ...s,
    items: (s.items ?? []).map(
      (i) => (i.id === itemId ? touch({ ...i, text }) : i)),
  }));
}

export function deleteListItem(mom: Mom, sectionId: string, itemId: string): Mom {
  return {
    ...mapSection(mom, sectionId, (s) => ({
      ...s, items: (s.items ?? []).filter((i) => i.id !== itemId),
    })),
    deleted_ids: tombstone(mom, itemId),
  };
}

// ---------------------------------------------------------------------------
// Tables
// ---------------------------------------------------------------------------
export function addRow(mom: Mom, sectionId: string): Mom {
  return mapSection(mom, sectionId, (s) => {
    const columns = s.columns ?? [];
    const cells: Record<string, string> = {};
    for (const c of columns) cells[c.id] = "";
    const row: MomRow = {
      id: newId("r"), cells, visible: true, source: "user_added",
    };
    return { ...s, rows: [...(s.rows ?? []), row] };
  });
}

export function updateCell(
  mom: Mom, sectionId: string, rowId: string, columnId: string, value: string
): Mom {
  return mapSection(mom, sectionId, (s) => ({
    ...s,
    rows: (s.rows ?? []).map((r) => (r.id === rowId
      ? touch({ ...r, cells: { ...r.cells, [columnId]: value } })
      : r)),
  }));
}

export function deleteRow(mom: Mom, sectionId: string, rowId: string): Mom {
  return {
    ...mapSection(mom, sectionId, (s) => ({
      ...s, rows: (s.rows ?? []).filter((r) => r.id !== rowId),
    })),
    deleted_ids: tombstone(mom, rowId),
  };
}

export function addColumn(mom: Mom, sectionId: string, label = "Column"): Mom {
  return mapSection(mom, sectionId, (s) => {
    const column: MomColumn = {
      id: newId("c"), label, source: "user_added",
    };
    // Every existing row gains a blank cell for the new column, so the table
    // is never ragged — the renderer would otherwise emit a short row.
    const rows = (s.rows ?? []).map(
      (r) => ({ ...r, cells: { ...r.cells, [column.id]: "" } }));
    return { ...s, columns: [...(s.columns ?? []), column], rows };
  });
}

export function renameColumn(
  mom: Mom, sectionId: string, columnId: string, label: string
): Mom {
  const trimmed = label.trim();
  if (!trimmed) return mom;
  return mapSection(mom, sectionId, (s) => ({
    ...s,
    columns: (s.columns ?? []).map(
      (c) => (c.id === columnId ? touch({ ...c, label: trimmed }) : c)),
  }));
}

/**
 * Delete a column and every cell under it.
 *
 * Refuses to remove the last column: a table with no columns can hold no
 * data, and the backend drops its rows on the next save — so allowing it here
 * would quietly destroy the section's content rather than emptying one column.
 */
export function deleteColumn(mom: Mom, sectionId: string, columnId: string): Mom {
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

// ---------------------------------------------------------------------------
// Derived helpers the editor and preview both need.
// ---------------------------------------------------------------------------

/** Sections that will actually appear in the document. */
export function visibleSections(mom: Mom): MomSection[] {
  return mom.sections.filter((s) => s.visible && !isSectionEmpty(s));
}

/** True when a section would render nothing — an empty heading reads as a
 * mistake, so both the preview and the backend's renderer skip these. */
export function isSectionEmpty(section: MomSection): boolean {
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

/** A one-line description of what a section holds, for the section list. */
export function sectionSummary(section: MomSection): string {
  const n = (xs: unknown[] | undefined) => (xs ?? []).length;
  switch (section.kind) {
    case "fields": {
      const count = n(section.fields);
      return `${count} field${count === 1 ? "" : "s"}`;
    }
    case "table": {
      const rows = n(section.rows);
      const cols = n(section.columns);
      return `${rows} row${rows === 1 ? "" : "s"} · ${cols} column${cols === 1 ? "" : "s"}`;
    }
    case "text": {
      const words = (section.text ?? "").trim().split(/\s+/).filter(Boolean).length;
      return `${words} word${words === 1 ? "" : "s"}`;
    }
    default: {
      const count = n(section.items);
      return `${count} item${count === 1 ? "" : "s"}`;
    }
  }
}

/** True when anything in the MoM carries a user edit — drives the "edited"
 * badge and the warning before a regeneration. */
export function hasUserEdits(mom: Mom): boolean {
  const edited = (s: { source: MomSource }) => s.source !== "ai";
  return mom.sections.some((s) =>
    edited(s)
    || (s.fields ?? []).some(edited)
    || (s.rows ?? []).some(edited)
    || (s.items ?? []).some(edited)
    || (s.columns ?? []).some(edited))
    || mom.deleted_ids.length > 0;
}
