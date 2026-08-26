// lib/__tests__/transcript-view.test.mjs — speaker blocks, tap-to-seek
// alignment, segment ids and talk time.
//
// Run:  node --test lib/__tests__/transcript-view.test.mjs
//
// WHY THIS FILE EXISTS. Segment ids were added to transcript segments so the
// AI's evidence references have something to point at, and the transcript
// moved from a tab to its own screen. Both changes touch the ONE thing in the
// transcript that fails silently and expensively:
//
//   TAP-TO-SEEK depends on positional alignment. The app groups consecutive
//   same-speaker segments into blocks and seeks on the FIRST segment's
//   `start`. Nothing validates that number at runtime — a wrong one just
//   plays the wrong moment, which reads as a broken player rather than as a
//   data bug. So every property that seek depends on is pinned here:
//   ordering, grouping boundaries, and start/end passthrough.
//
//   IDs MUST BE ADDITIVE. They are derived server-side from position and
//   carried through the block builder, but NOTHING about grouping, ordering or
//   timing may depend on them — a legacy transcript has no ids at all and must
//   render and seek identically.
//
// The functions under test are pure and mirrored below rather than imported,
// matching the convention in mom-model.test.mjs and task-insights.test.mjs.
import test from "node:test";
import assert from "node:assert/strict";

// ---------------------------------------------------------------------------
// MIRRORS of lib/transcript-view.tsx. Keep in sync.
// ---------------------------------------------------------------------------
function buildSpeakerBlocks(segs) {
  const blocks = [];
  for (const s of segs ?? []) {
    const last = blocks[blocks.length - 1];
    if (last && last.speaker === s.speaker) {
      last.texts.push(s.text);
      last.end = Number(s.end) || last.end;
      if (s.id) last.ids.push(s.id);
    } else {
      blocks.push({
        speaker: s.speaker,
        start: Number(s.start) || 0,
        end: Number(s.end) || 0,
        texts: [s.text],
        ids: s.id ? [s.id] : [],
      });
    }
  }
  return blocks;
}

function buildSpeakerColors(segs, speakers) {
  const map = new Map();
  for (const seg of segs ?? []) {
    if (!map.has(seg.speaker)) map.set(seg.speaker, speakers[map.size % speakers.length]);
  }
  return map;
}

function fmtTs(sec) {
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

/** Mirror of the Speakers tab's talk-time derivation (index.tsx). */
function talkTimeBySpeaker(segs) {
  const totals = new Map();
  for (const seg of segs ?? []) {
    const span = (Number(seg.end) || 0) - (Number(seg.start) || 0);
    if (span > 0) totals.set(seg.speaker, (totals.get(seg.speaker) ?? 0) + span);
  }
  return totals;
}

/** Mirror of fmtTalkTime (lib/meeting-summary.tsx). */
function fmtTalkTime(seconds) {
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))} sec`;
  return `${Math.round(seconds / 60)} min`;
}

// A diarized transcript WITH derived ids, as the backend now serves it.
const WITH_IDS = [
  { id: "seg_0", speaker: "0", start: 0, end: 4.2, text: "The quote is 4.2 lakh." },
  { id: "seg_1", speaker: "0", start: 4.2, end: 7.5, text: "Rakesh will revise it." },
  { id: "seg_2", speaker: "1", start: 7.5, end: 11, text: "That is over budget." },
  { id: "seg_3", speaker: "0", start: 11, end: 14.5, text: "Understood." },
];

// The SAME transcript as an older backend served it — no ids at all.
const LEGACY = WITH_IDS.map(({ id, ...rest }) => rest);

// ===========================================================================
// 1. GROUPING AND ORDER — what tap-to-seek rests on
// ===========================================================================
test("consecutive same-speaker segments group; a speaker change splits", () => {
  const blocks = buildSpeakerBlocks(WITH_IDS);
  assert.equal(blocks.length, 3);
  assert.deepEqual(blocks.map((b) => b.speaker), ["0", "1", "0"]);
  // The two adjacent Speaker 0 turns became one block...
  assert.deepEqual(blocks[0].texts,
                   ["The quote is 4.2 lakh.", "Rakesh will revise it."]);
  // ...and the LATER Speaker 0 turn is its own block, not folded back into the
  // first. Folding non-adjacent turns would reorder the meeting.
  assert.deepEqual(blocks[2].texts, ["Understood."]);
});

test("block order is transcript order, never sorted", () => {
  // Deliberately out-of-order timestamps: the transcript's own sequence wins.
  // Sorting here would reorder what was said.
  const blocks = buildSpeakerBlocks([
    { speaker: "0", start: 90, end: 95, text: "later" },
    { speaker: "1", start: 10, end: 15, text: "earlier" },
  ]);
  assert.deepEqual(blocks.map((b) => b.texts[0]), ["later", "earlier"]);
});

test("a block's start is the FIRST segment's start — the seek target", () => {
  const blocks = buildSpeakerBlocks(WITH_IDS);
  assert.equal(blocks[0].start, 0);      // not 4.2 (the second segment)
  assert.equal(blocks[1].start, 7.5);
  assert.equal(blocks[2].start, 11);
});

test("a block's end extends to the LAST segment it absorbed", () => {
  // `end` drives the now-playing highlight; stopping at the first segment's end
  // would un-highlight a block mid-sentence.
  const blocks = buildSpeakerBlocks(WITH_IDS);
  assert.equal(blocks[0].end, 7.5);
});

test("string timings coerce to numbers, and junk degrades to 0 not NaN", () => {
  // start/end cross the wire as JSON and have historically arrived as strings.
  // NaN would break both seek and the >= / < highlight comparison silently.
  const blocks = buildSpeakerBlocks([
    { speaker: "0", start: "1.8", end: "6.4", text: "a" },
    { speaker: "1", start: null, end: undefined, text: "b" },
  ]);
  assert.equal(blocks[0].start, 1.8);
  assert.equal(blocks[0].end, 6.4);
  assert.equal(blocks[1].start, 0);
  assert.equal(blocks[1].end, 0);
});

// ===========================================================================
// 2. SEGMENT IDS ARE ADDITIVE — a legacy transcript behaves identically
// ===========================================================================
test("ids change nothing about grouping, order or timing", () => {
  const withIds = buildSpeakerBlocks(WITH_IDS);
  const legacy = buildSpeakerBlocks(LEGACY);

  assert.equal(withIds.length, legacy.length);
  for (let i = 0; i < withIds.length; i++) {
    assert.equal(withIds[i].speaker, legacy[i].speaker);
    assert.equal(withIds[i].start, legacy[i].start);
    assert.equal(withIds[i].end, legacy[i].end);
    assert.deepEqual(withIds[i].texts, legacy[i].texts);
  }
});

test("a block carries the ids of the segments folded into it", () => {
  const blocks = buildSpeakerBlocks(WITH_IDS);
  assert.deepEqual(blocks[0].ids, ["seg_0", "seg_1"]);
  assert.deepEqual(blocks[1].ids, ["seg_2"]);
  assert.deepEqual(blocks[2].ids, ["seg_3"]);
});

test("a legacy transcript yields empty id lists, never undefined", () => {
  // A consumer resolving an evidence reference iterates these; undefined would
  // throw on a recording from the back catalogue.
  for (const b of buildSpeakerBlocks(LEGACY)) {
    assert.deepEqual(b.ids, []);
  }
});

test("a partially-migrated transcript keeps only the ids it has", () => {
  const blocks = buildSpeakerBlocks([
    { id: "seg_0", speaker: "0", start: 0, end: 1, text: "a" },
    { speaker: "0", start: 1, end: 2, text: "b" },   // no id
  ]);
  assert.equal(blocks.length, 1);
  assert.deepEqual(blocks[0].ids, ["seg_0"]);
  assert.deepEqual(blocks[0].texts, ["a", "b"]);
});

test("an evidence reference resolves to the block containing it", () => {
  // This is what segment ids are FOR: "seg_3" must find the third block, whose
  // start (11) is where the player should seek.
  const blocks = buildSpeakerBlocks(WITH_IDS);
  const found = blocks.find((b) => b.ids.includes("seg_3"));
  assert.equal(found.start, 11);
  // An id the transcript does not contain resolves to nothing, rather than to
  // the wrong moment.
  assert.equal(blocks.find((b) => b.ids.includes("seg_999")), undefined);
});

// ===========================================================================
// 3. EMPTY AND MISSING INPUT
// ===========================================================================
test("no timestamps yields no blocks, not a throw", () => {
  for (const empty of [undefined, null, []]) {
    assert.deepEqual(buildSpeakerBlocks(empty), []);
  }
});

// ===========================================================================
// 4. SPEAKER COLOURS — stable, first-appearance order
// ===========================================================================
test("colours are assigned in first-appearance order and reused per speaker", () => {
  const palette = ["red", "green", "blue"];
  const colors = buildSpeakerColors(WITH_IDS, palette);
  assert.equal(colors.get("0"), "red");
  assert.equal(colors.get("1"), "green");
  // Speaker 0 speaks again later and must keep its FIRST colour.
  assert.equal(colors.size, 2);
});

test("more speakers than palette entries wraps rather than running out", () => {
  const segs = ["0", "1", "2", "3"].map((speaker, i) =>
    ({ speaker, start: i, end: i + 1, text: "x" }));
  const colors = buildSpeakerColors(segs, ["red", "green"]);
  assert.equal(colors.get("0"), "red");
  assert.equal(colors.get("1"), "green");
  assert.equal(colors.get("2"), "red");
  assert.equal(colors.get("3"), "green");
});

// ===========================================================================
// 5. TIMESTAMP FORMATTING — the transcript's tabular column
// ===========================================================================
test("timestamps are zero-padded on both halves", () => {
  // The column is fixed-width tabular; an unpadded minute makes it ragged.
  assert.equal(fmtTs(0), "00:00");
  assert.equal(fmtTs(9), "00:09");
  assert.equal(fmtTs(65), "01:05");
  assert.equal(fmtTs(3599), "59:59");
  // Fractional seconds floor rather than round up past the segment start.
  assert.equal(fmtTs(11.9), "00:11");
});

// ===========================================================================
// 6. TALK TIME — derived on the client from data already present
// ===========================================================================
test("talk time sums every segment span per speaker", () => {
  const totals = talkTimeBySpeaker(WITH_IDS);
  // Speaker 0: (4.2-0) + (7.5-4.2) + (14.5-11) = 11.0
  assert.equal(Math.round(totals.get("0") * 10) / 10, 11);
  // Speaker 1: 11 - 7.5 = 3.5
  assert.equal(Math.round(totals.get("1") * 10) / 10, 3.5);
});

test("a speaker with no measurable span is absent, not zero", () => {
  // Absent means the row omits the figure. A stored 0 would render as "0 min",
  // which reads as "this person said nothing" — a claim the data cannot make.
  const totals = talkTimeBySpeaker([
    { speaker: "0", start: 0, end: 5, text: "a" },
    { speaker: "1", start: 5, end: 5, text: "b" },   // zero-length
    { speaker: "2", start: 9, end: 7, text: "c" },   // negative: bad data
  ]);
  assert.equal(totals.has("0"), true);
  assert.equal(totals.has("1"), false);
  assert.equal(totals.has("2"), false);
});

test("talk time works on a legacy transcript with no ids", () => {
  assert.deepEqual([...talkTimeBySpeaker(LEGACY).keys()], ["0", "1"]);
});

test("talk time renders coarsely, never as false precision", () => {
  assert.equal(fmtTalkTime(707), "12 min");
  // The sub-minute boundary is tested on RAW seconds, not rounded minutes:
  // rounding first reported 45 seconds as "1 min", overstating a short
  // contribution by a third. Caught by this test.
  assert.equal(fmtTalkTime(45), "45 sec");
  assert.equal(fmtTalkTime(59), "59 sec");
  assert.equal(fmtTalkTime(60), "1 min");
  // Under a second still reads as "1 sec" rather than "0 sec".
  assert.equal(fmtTalkTime(0.4), "1 sec");
});
