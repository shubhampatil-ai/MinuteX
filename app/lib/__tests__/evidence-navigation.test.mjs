// lib/__tests__/evidence-navigation.test.mjs — "View in transcript", and the
// dashboard's source-meeting / needs-review surfaces.
//
// Run:  node --test lib/__tests__/evidence-navigation.test.mjs
//
// WHAT THIS PINS.
//
//   1. THE AFFORDANCE IS CONDITIONAL. "View in transcript" may appear only
//      when the BACKEND stored segment ids it validated against that meeting's
//      transcript. A button that scrolled nowhere is worse than no button, and
//      two legitimate cases have no ids: a task from before this feature, and
//      an extraction the model gave no usable reference for. Both must still
//      show the evidence QUOTE, which is the primary artefact.
//
//   2. BLOCK RESOLUTION. The transcript groups consecutive same-speaker
//      segments into one block, so an id does not map 1:1 to a rendered row —
//      it maps to the block that CONTAINS it. The block model has carried
//      `ids` since it was written for exactly this; these tests pin that the
//      resolution uses them rather than re-deriving position.
//
//   3. THE APP NEVER INVENTS OR REPAIRS A REFERENCE. Validation is the
//      backend's job (it is the only side that has the transcript). The app
//      renders what it is given and nothing else.
//
// The modules under test are TSX, so the pure logic is mirrored here rather
// than imported — the convention in task-insights.test.mjs and
// task-provenance.test.mjs (node --test runs .mjs with no transpile step).
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const ROOT = new URL("../../", import.meta.url).pathname
  .replace(/^\/([A-Za-z]:)/, "$1");

// --- mirrored from lib/transcript-view.tsx ---------------------------------

/** Consecutive same-speaker segments folded into one block, carrying the ids
 *  of every segment inside it. */
function buildSpeakerBlocks(segments) {
  const out = [];
  for (const s of segments ?? []) {
    const last = out[out.length - 1];
    if (last && last.speaker === s.speaker) {
      last.texts.push(s.text);
      last.end = s.end;
      if (s.id) last.ids.push(s.id);
    } else {
      out.push({
        speaker: s.speaker, texts: [s.text],
        start: s.start, end: s.end, ids: s.id ? [s.id] : [],
      });
    }
  }
  return out;
}

/** Which blocks the evidence highlight applies to. */
function highlightedBlocks(blocks, highlightIds) {
  const wanted = new Set((highlightIds ?? []).filter(Boolean));
  if (!wanted.size) return [];
  return blocks
    .map((b, i) => ({ b, i }))
    .filter(({ b }) => b.ids.some((id) => wanted.has(id)))
    .map(({ i }) => i);
}

/** Whether Task Details offers the affordance at all. */
function showsViewEvidence(task, recording) {
  const ids = task.ai_evidence_segment_ids ?? [];
  return ids.length > 0 && !!recording;
}

/** The route params the button pushes. */
function evidenceRoute(task, recording) {
  return {
    pathname: "/recording/[key]/transcript",
    params: {
      key: recording.audio_s3_key,
      evidence: (task.ai_evidence_segment_ids ?? []).join(","),
    },
  };
}

/** The transcript screen parsing that param back. */
function parseEvidenceParam(evidence) {
  return String(evidence ?? "").split(",").map((x) => x.trim()).filter(Boolean);
}

const withIds = (ids) => ({
  id: "t-1",
  source_type: "AI",
  ai_evidence: "I'll send the proposal tomorrow.",
  ai_evidence_segment_ids: ids,
});
const REC = { audio_s3_key: "recordings/u-1/mobile/abc_123.m4a" };

const SEGMENTS = [
  { id: "seg_0", speaker: "0", text: "Where are we?", start: 0, end: 2 },
  { id: "seg_1", speaker: "1", text: "I'll send it tomorrow.", start: 2, end: 5 },
  { id: "seg_2", speaker: "1", text: "Just need the numbers.", start: 5, end: 7 },
  { id: "seg_3", speaker: "0", text: "Good.", start: 7, end: 8 },
];

// ---------------------------------------------------------------------------
describe("View in transcript is conditional", () => {
  it("appears when validated ids exist", () => {
    assert.equal(showsViewEvidence(withIds(["seg_1"]), REC), true);
  });

  it("does NOT appear when the list is empty", () => {
    // The model gave no usable reference. The quote still renders; a button
    // that scrolled nowhere would teach the user not to trust the feature.
    assert.equal(showsViewEvidence(withIds([]), REC), false);
  });

  it("does NOT appear for a legacy task with no field at all", () => {
    const legacy = { id: "t-old", ai_evidence: "Something was said." };
    assert.equal(showsViewEvidence(legacy, REC), false);
  });

  it("does NOT appear when the source meeting is unavailable", () => {
    // The recording may be trashed, or simply not loaded yet. Navigating
    // without a key would land on a broken screen.
    assert.equal(showsViewEvidence(withIds(["seg_1"]), null), false);
  });

  it("does NOT appear for a manual task", () => {
    const manual = { id: "t-m", source_type: "MANUAL" };
    assert.equal(showsViewEvidence(manual, REC), false);
  });
});

describe("evidence route", () => {
  it("carries the meeting key and the ids", () => {
    const r = evidenceRoute(withIds(["seg_1"]), REC);
    assert.equal(r.pathname, "/recording/[key]/transcript");
    assert.equal(r.params.key, REC.audio_s3_key);
    assert.equal(r.params.evidence, "seg_1");
  });

  it("joins several ids and parses them back unchanged", () => {
    const r = evidenceRoute(withIds(["seg_1", "seg_2"]), REC);
    assert.equal(r.params.evidence, "seg_1,seg_2");
    assert.deepEqual(parseEvidenceParam(r.params.evidence), ["seg_1", "seg_2"]);
  });

  it("parses a missing or malformed param to nothing", () => {
    // The transcript screen is reachable by ordinary navigation with no
    // evidence at all — that must render exactly as it always did.
    assert.deepEqual(parseEvidenceParam(undefined), []);
    assert.deepEqual(parseEvidenceParam(""), []);
    assert.deepEqual(parseEvidenceParam(",,"), []);
    assert.deepEqual(parseEvidenceParam(" seg_1 , seg_2 "), ["seg_1", "seg_2"]);
  });
});

describe("resolving an id to a transcript block", () => {
  const blocks = buildSpeakerBlocks(SEGMENTS);

  it("groups consecutive same-speaker segments and keeps every id", () => {
    assert.equal(blocks.length, 3);
    assert.deepEqual(blocks[1].ids, ["seg_1", "seg_2"]);
  });

  it("highlights the block CONTAINING the id, not a positional row", () => {
    // seg_2 is the second segment of block 1 — resolving by position would
    // highlight the wrong block entirely.
    assert.deepEqual(highlightedBlocks(blocks, ["seg_2"]), [1]);
  });

  it("highlights one block when several of its ids are referenced", () => {
    assert.deepEqual(highlightedBlocks(blocks, ["seg_1", "seg_2"]), [1]);
  });

  it("highlights several blocks for evidence that spans them", () => {
    assert.deepEqual(highlightedBlocks(blocks, ["seg_0", "seg_3"]), [0, 2]);
  });

  it("highlights nothing for an id this meeting does not have", () => {
    // Should not happen (the backend validates), but the UI must degrade to
    // "no highlight" rather than to an exception.
    assert.deepEqual(highlightedBlocks(blocks, ["seg_99"]), []);
  });

  it("highlights nothing when no ids are supplied", () => {
    assert.deepEqual(highlightedBlocks(blocks, []), []);
    assert.deepEqual(highlightedBlocks(blocks, undefined), []);
  });

  it("survives segments with no ids at all (legacy transcripts)", () => {
    const plain = buildSpeakerBlocks(
      SEGMENTS.map(({ id, ...rest }) => rest));
    assert.equal(plain.length, 3);
    assert.deepEqual(highlightedBlocks(plain, ["seg_1"]), []);
  });
});

// ---------------------------------------------------------------------------
// SOURCE-LEVEL LINT.
// ---------------------------------------------------------------------------
describe("architecture", () => {
  const detail = readFileSync(join(ROOT, "src", "app", "task", "[id].tsx"), "utf8");
  const view = readFileSync(join(ROOT, "lib", "transcript-view.tsx"), "utf8");
  const screen = readFileSync(
    join(ROOT, "src", "app", "recording", "[key]", "transcript.tsx"), "utf8");
  const card = readFileSync(join(ROOT, "lib", "task-action-center.tsx"), "utf8");
  const dash = readFileSync(join(ROOT, "src", "app", "tasks.tsx"), "utf8");

  it("the button is gated on the ids actually existing", () => {
    assert.match(detail, /evidenceIds\.length > 0/);
  });

  it("the app never derives or repairs a segment id", () => {
    // Validation belongs to the backend — it is the only side holding the
    // transcript. An app-side "fix" would resurrect exactly the wrong-line
    // failure the backend validator exists to prevent.
    for (const forbidden of [/seg_\$\{/, /["'`]seg_["'`]\s*\+/, /parseInt\([^)]*seg/]) {
      assert.ok(!forbidden.test(detail), `must not construct ids: ${forbidden}`);
      assert.ok(!forbidden.test(view), `must not construct ids: ${forbidden}`);
    }
  });

  it("the transcript screen reads the evidence param and scrolls once", () => {
    assert.match(screen, /useLocalSearchParams/);
    assert.match(screen, /highlightIds=\{evidenceIds\}/);
    // Scrolling on every layout pass would fight the user for control of the
    // scroll position.
    assert.match(screen, /scrolled\.current/);
  });

  it("the transcript view keeps evidence and playback highlights distinct", () => {
    // They mean different things and can both be true at once; one must not
    // silently mask the other.
    assert.match(view, /isEvidence/);
    assert.match(view, /active \? C\.primarySoft/);
  });

  it("the dashboard shows a source meeting only when there is one", () => {
    assert.match(card, /meetingTitle \?/);
    assert.match(dash, /meetingTitles\.get/);
  });

  it("the needs-review count is tappable and maps to the real filter", () => {
    assert.match(card, /counts\.needsAssignment/);
    assert.match(dash, /key === "review"/);
  });
});
