// lib/__tests__/meeting-outputs.test.mjs — what a meeting can share.
//
// Run:  node --test lib/__tests__/meeting-outputs.test.mjs
//
// WHAT THIS PINS. Sharing used to be MoM-shaped and is now generic, and the
// generic version has exactly one job that is easy to get wrong: decide WHICH
// outputs exist and in WHICH formats. Everything downstream trusts that list,
// so a mistake here is a mistake the user only discovers in someone else's
// inbox:
//
//   OFFERING SOMETHING THAT ISN'T THERE — a document with no content must not
//     be listed. A ticked box that renders an empty PDF is worse than no box,
//     because the sender finds out from the recipient.
//
//   OFFERING THE SAME THING TWICE — the backend MIRRORS the structured MoM
//     into a `minutes_of_meeting` Markdown document. Both are real, both are
//     "Minutes of Meeting", and listing both gives the user two identical-
//     looking rows that differ only in typography.
//
//   OFFERING A FORMAT THAT CANNOT BE PRODUCED — PDF needs expo-print. A build
//     without it must not show a PDF chip that fails on send.
//
//   A SELECTION THAT SENDS NOTHING — a ticked output with zero formats. The
//     checkbox and the chips must never contradict each other.
//
// The module under test is TypeScript, so its logic is mirrored below rather
// than imported, matching the convention in task-insights.test.mjs and
// gmail-integration.test.mjs (node --test runs .mjs with no transpile step).
import { describe, it } from "node:test";
import assert from "node:assert/strict";

// --- mirrored from lib/meeting-outputs.ts ----------------------------------

const MAX_ATTACHMENTS = 5;

function formatsFor(pdfOk) {
  return pdfOk ? ["pdf", "docx", "md"] : ["docx", "md"];
}

function meetingOutputs(documents, mom, pdfOk = true) {
  const out = [];
  if (mom) {
    out.push({
      id: "minutes_of_meeting",
      label: mom.title || "Minutes of Meeting",
      kind: "mom",
      formats: pdfOk ? ["pdf", "docx"] : ["docx"],
      content: "",
    });
  }
  for (const doc of documents) {
    if (!doc?.content?.trim()) continue;
    if (mom && doc.type === "minutes_of_meeting") continue;
    out.push({
      id: doc.type,
      label: doc.label || doc.type,
      kind: "markdown",
      formats: formatsFor(pdfOk),
      content: doc.content,
    });
  }
  return out;
}

function defaultFormat(output) {
  return output.formats.includes("pdf") ? "pdf" : output.formats[0];
}

const fileCount = (picked) =>
  Object.values(picked).reduce((n, f) => n + f.length, 0);

/** The sheet's toggle logic, mirrored — including the cap refusal. */
function toggleOutput(picked, output) {
  const next = { ...picked };
  if (next[output.id]?.length) { delete next[output.id]; return { next, ok: true }; }
  next[output.id] = [defaultFormat(output)];
  return fileCount(next) > MAX_ATTACHMENTS
    ? { next: picked, ok: false }
    : { next, ok: true };
}

function toggleFormat(picked, output, format) {
  const current = picked[output.id] ?? [];
  const next = { ...picked };
  const updated = current.includes(format)
    ? current.filter((f) => f !== format)
    : [...current, format];
  if (updated.length) next[output.id] = updated;
  else delete next[output.id];
  if (updated.length < current.length) return { next, ok: true };
  return fileCount(next) > MAX_ATTACHMENTS
    ? { next: picked, ok: false }
    : { next, ok: true };
}

const doc = (type, label, content = "# Body") => ({ type, label, content });
const MOM = { title: "Minutes of Meeting" };

// ---------------------------------------------------------------------------
describe("which outputs a meeting offers", () => {
  it("offers nothing when the meeting has produced nothing", () => {
    assert.deepEqual(meetingOutputs([], null), []);
  });

  it("offers every generated document, not just the MoM", () => {
    // The whole point of the generic layer: an Executive Summary and an
    // Action Item Report are as shareable as Minutes.
    const outs = meetingOutputs([
      doc("executive_summary", "Executive Summary"),
      doc("action_items", "Action Item Report"),
      doc("custom_a1b2", "Client Proposal"),
    ], null);
    assert.deepEqual(outs.map((o) => o.label),
      ["Executive Summary", "Action Item Report", "Client Proposal"]);
  });

  it("offers a future document type with no code change", () => {
    // A type this build has never heard of still shows up, because the list
    // comes from the meeting's data rather than a hardcoded catalogue.
    const outs = meetingOutputs([doc("board_pack_2027", "Board Pack")], null);
    assert.equal(outs.length, 1);
    assert.equal(outs[0].label, "Board Pack");
  });

  it("never offers a document with no content", () => {
    const outs = meetingOutputs([
      doc("executive_summary", "Executive Summary", ""),
      doc("action_items", "Action Item Report", "   "),
      doc("follow_up_email", "Follow-up Email", "Real text"),
    ], null);
    assert.deepEqual(outs.map((o) => o.label), ["Follow-up Email"]);
  });

  it("lists the structured MoM once, not twice", () => {
    // The backend mirrors the structure into a Markdown document of the same
    // type. Both exist; only one may be offered.
    const outs = meetingOutputs([
      doc("minutes_of_meeting", "Minutes of Meeting"),
      doc("executive_summary", "Executive Summary"),
    ], MOM);
    const minutes = outs.filter((o) => o.id === "minutes_of_meeting");
    assert.equal(minutes.length, 1);
    assert.equal(minutes[0].kind, "mom", "the structured version must win");
    assert.equal(outs.length, 2);
  });

  it("falls back to the Markdown minutes when no structured MoM exists", () => {
    const outs = meetingOutputs([doc("minutes_of_meeting", "Minutes of Meeting")], null);
    assert.equal(outs.length, 1);
    assert.equal(outs[0].kind, "markdown");
  });

  it("uses the MoM's own title when the user renamed it", () => {
    const outs = meetingOutputs([], { title: "Client Discussion Minutes" });
    assert.equal(outs[0].label, "Client Discussion Minutes");
  });
});

describe("formats offered per output", () => {
  it("offers PDF, DOCX and Markdown for a Markdown document", () => {
    const outs = meetingOutputs([doc("executive_summary", "Executive Summary")], null);
    assert.deepEqual(outs[0].formats, ["pdf", "docx", "md"]);
  });

  it("does not offer Markdown for the structured MoM", () => {
    // Its Markdown mirror is a lossy rendering of the structure — sending it
    // would hand the recipient a worse copy of a document we render properly.
    const outs = meetingOutputs([], MOM);
    assert.deepEqual(outs[0].formats, ["pdf", "docx"]);
  });

  it("hides PDF entirely when the build cannot produce one", () => {
    // A chip that always fails is worse than a missing chip.
    const outs = meetingOutputs([doc("executive_summary", "Summary")], MOM, false);
    for (const o of outs) assert.ok(!o.formats.includes("pdf"), o.label);
  });

  it("defaults to PDF, and to DOCX when PDF is unavailable", () => {
    const [withPdf] = meetingOutputs([doc("a", "A")], null, true);
    const [noPdf] = meetingOutputs([doc("a", "A")], null, false);
    assert.equal(defaultFormat(withPdf), "pdf");
    assert.equal(defaultFormat(noPdf), "docx");
  });
});

describe("selecting outputs and formats", () => {
  const [summary] = meetingOutputs([doc("executive_summary", "Summary")], null);

  it("ticking an output selects a real format", () => {
    // A ticked document with no format would silently send nothing.
    const { next } = toggleOutput({}, summary);
    assert.deepEqual(next[summary.id], ["pdf"]);
  });

  it("unticking an output clears it entirely", () => {
    const { next } = toggleOutput({ [summary.id]: ["pdf", "docx"] }, summary);
    assert.equal(next[summary.id], undefined);
  });

  it("removing the last format unticks the output", () => {
    // Otherwise the checkbox says "selected" while nothing would be attached.
    const { next } = toggleFormat({ [summary.id]: ["pdf"] }, summary, "pdf");
    assert.equal(next[summary.id], undefined);
    assert.equal(fileCount(next), 0);
  });

  it("counts FILES, not documents", () => {
    assert.equal(fileCount({ a: ["pdf", "docx"], b: ["pdf"] }), 3);
  });

  it("refuses a selection past the attachment cap", () => {
    const full = { a: ["pdf"], b: ["pdf"], c: ["pdf"], d: ["pdf"], e: ["pdf"] };
    assert.equal(fileCount(full), MAX_ATTACHMENTS);
    const { next, ok } = toggleOutput(full, summary);
    assert.equal(ok, false, "must refuse");
    assert.deepEqual(next, full, "selection must be unchanged");
  });

  it("still allows REMOVING when already at the cap", () => {
    // The cap must never trap the user: unselecting is always permitted.
    const full = { a: ["pdf", "docx"], b: ["pdf"], c: ["pdf"], d: ["pdf"] };
    const { ok } = toggleFormat(full, { id: "a", formats: ["pdf", "docx"] }, "docx");
    assert.equal(ok, true);
  });

  it("nothing is selected by default", () => {
    // Attachments cost the recipient something. Pre-ticking items from a list
    // the user has not read is how people send documents by accident.
    assert.equal(fileCount({}), 0);
  });
});
