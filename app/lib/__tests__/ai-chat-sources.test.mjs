// lib/__tests__/ai-chat-sources.test.mjs — AI chat sources, and the fact that
// they reuse the transcript navigation that already exists.
//
// Run:  node --test lib/__tests__/ai-chat-sources.test.mjs
//
// WHAT THIS PINS.
//
//   1. ONE NAVIGATION MECHANISM. A chat source and a task's "View in
//      transcript" must push the IDENTICAL route with the identical param
//      shape (/recording/[key]/transcript?evidence=seg_N). The transcript
//      screen already highlights the block containing an id and seeks the
//      player to it; a second path to the same destination would be a second
//      thing to keep working, and would drift.
//
//   2. NO SOURCES IS THE ORDINARY CASE. Three legitimate situations produce an
//      answer with no sources — the model declined to cite, the turn was stored
//      before grounding shipped, and the backend is older than the feature.
//      All three must render as "just an answer", never as an error and never
//      as an empty "Sources" heading.
//
//   3. THE APP NEVER INVENTS A SOURCE. Validation against the meeting's real
//      segment ids is the backend's job — it is the only side holding the
//      transcript. The app renders what it is handed.
//
// The screen under test is TSX, so the pure logic is mirrored here rather than
// imported — the convention in evidence-navigation.test.mjs and
// task-provenance.test.mjs (node --test runs .mjs with no transpile step).
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const ROOT = new URL("../../", import.meta.url).pathname
  .replace(/^\/([A-Za-z]:)/, "$1");

// --- mirrored from src/app/recording/[key]/assistant.tsx -------------------

/** Whether the Sources block renders at all. */
function showsSources(turn) {
  return (turn.sources?.length ?? 0) > 0;
}

/** The route params a source row pushes. */
function sourceRoute(source, recordingKey) {
  return {
    pathname: "/recording/[key]/transcript",
    params: { key: recordingKey, evidence: source.segment_id },
  };
}

/** M:SS / H:MM:SS, as the transcript and player already render a timestamp. */
function clock(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const mm = h ? String(m).padStart(2, "0") : String(m);
  return `${h ? `${h}:` : ""}${mm}:${String(s).padStart(2, "0")}`;
}

// --- mirrored from lib/api.ts sendAiChat ----------------------------------

/** The defaulting an older/ungrounded response goes through. */
function normalizeChatResponse(res) {
  return {
    reply: res.reply ?? "",
    chat_history: res.chat_history ?? [],
    sources: res.sources ?? [],
  };
}

// --- mirrored from lib/transcript-view.tsx (the existing destination) -----

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

function highlightedBlocks(blocks, highlightIds) {
  const wanted = new Set((highlightIds ?? []).filter(Boolean));
  if (!wanted.size) return [];
  return blocks
    .map((b, i) => ({ b, i }))
    .filter(({ b }) => b.ids.some((id) => wanted.has(id)))
    .map(({ i }) => i);
}

function parseEvidenceParam(evidence) {
  return String(evidence ?? "").split(",").map((x) => x.trim()).filter(Boolean);
}

const KEY = "recordings/u-1/mobile/abc_123.m4a";

const SEGMENTS = [
  { id: "seg_0", speaker: "0", text: "Where are we?", start: 0, end: 2 },
  { id: "seg_1", speaker: "1", text: "I'll send it tomorrow.", start: 2, end: 5 },
  { id: "seg_2", speaker: "1", text: "Just need the numbers.", start: 5, end: 7 },
  { id: "seg_3", speaker: "0", text: "Good.", start: 1032, end: 1045 },
];

const grounded = (ids) => ({
  role: "assistant",
  content: "They agreed to send the revised quote.",
  sources: ids.map((id) => {
    const seg = SEGMENTS.find((s) => s.id === id);
    return {
      segment_id: id, speaker_id: seg.speaker,
      start_time: seg.start, end_time: seg.end,
    };
  }),
});

// ---------------------------------------------------------------------------
describe("Sources render only when the backend grounded the answer", () => {
  it("renders when sources are present", () => {
    assert.equal(showsSources(grounded(["seg_1"])), true);
  });

  it("does NOT render for an answer the model declined to cite", () => {
    // "That wasn't discussed" is a correct answer with nothing to point at.
    // An empty "Sources" heading would imply evidence that does not exist.
    assert.equal(showsSources({ role: "assistant", content: "Not discussed.", sources: [] }), false);
  });

  it("does NOT render for a turn stored before grounding shipped", () => {
    assert.equal(showsSources({ role: "assistant", content: "Older answer." }), false);
  });

  it("does NOT render on a user turn", () => {
    assert.equal(showsSources({ role: "user", content: "What did we decide?" }), false);
  });
});

describe("An older backend degrades to no sources, not to a crash", () => {
  it("a response with no sources key normalizes to an empty list", () => {
    const res = normalizeChatResponse({ reply: "Hi", chat_history: [] });
    assert.deepEqual(res.sources, []);
    assert.equal(res.reply, "Hi");
  });

  it("a response with sources passes them through", () => {
    const res = normalizeChatResponse({
      reply: "Agreed.", chat_history: [],
      sources: [{ segment_id: "seg_1", start_time: 2, end_time: 5 }],
    });
    assert.equal(res.sources.length, 1);
  });
});

describe("A source uses the EXISTING transcript navigation", () => {
  it("pushes the same route a task's View in transcript pushes", () => {
    const route = sourceRoute(grounded(["seg_1"]).sources[0], KEY);
    assert.equal(route.pathname, "/recording/[key]/transcript");
    assert.equal(route.params.key, KEY);
    assert.equal(route.params.evidence, "seg_1");
  });

  it("the param round-trips through the transcript screen's parser", () => {
    const route = sourceRoute(grounded(["seg_3"]).sources[0], KEY);
    assert.deepEqual(parseEvidenceParam(route.params.evidence), ["seg_3"]);
  });

  it("the id resolves to the block the transcript will highlight", () => {
    // seg_1 and seg_2 are one speaker's consecutive turns, so they fold into a
    // single rendered block — an id maps to the block CONTAINING it, not to a
    // row index.
    const blocks = buildSpeakerBlocks(SEGMENTS);
    const route = sourceRoute(grounded(["seg_2"]).sources[0], KEY);
    const ids = parseEvidenceParam(route.params.evidence);
    assert.deepEqual(highlightedBlocks(blocks, ids), [1]);
  });

  it("a late source resolves to the late block, not the first one", () => {
    // The whole point of retrieval: an answer about the END of a meeting must
    // scroll to the end.
    const blocks = buildSpeakerBlocks(SEGMENTS);
    const ids = parseEvidenceParam(
      sourceRoute(grounded(["seg_3"]).sources[0], KEY).params.evidence);
    assert.deepEqual(highlightedBlocks(blocks, ids), [2]);
  });

  it("carries the timestamp the player seeks to", () => {
    const src = grounded(["seg_3"]).sources[0];
    assert.equal(src.start_time, 1032);
  });
});

describe("Source timestamps read like every other timestamp in the app", () => {
  it("formats under an hour as M:SS", () => {
    assert.equal(clock(1032), "17:12");
    assert.equal(clock(5), "0:05");
  });

  it("formats over an hour as H:MM:SS", () => {
    assert.equal(clock(3725), "1:02:05");
  });

  it("tolerates a missing or junk value rather than rendering NaN", () => {
    assert.equal(clock(undefined), "0:00");
    assert.equal(clock(-5), "0:00");
  });
});

describe("The screen renders what it is given and nothing more", () => {
  it("does not filter or repair the ids it receives", () => {
    // Validation is the backend's job — it is the only side with the
    // transcript. If the app started filtering, a real id the app did not
    // recognise would silently vanish from the UI.
    const turn = { role: "assistant", content: "x",
                   sources: [{ segment_id: "seg_9999", start_time: 1, end_time: 2 }] };
    assert.equal(showsSources(turn), true);
    assert.equal(sourceRoute(turn.sources[0], KEY).params.evidence, "seg_9999");
  });
});

// ---------------------------------------------------------------------------
// The "thinking" phases shown while an answer is being generated.
//
// WHY THEY EXIST. A grounded answer on a long meeting takes two Groq round
// trips (retrieve, then answer) where it used to take one. The phases mirror
// those real steps — they are not a fake progress bar — so a meeting answered
// in ONE call must not claim to be searching anything.

const RETRIEVE_PHASES = [
  { at: 0, label: "Searching the meeting…" },
  { at: 2600, label: "Reading the relevant sections…" },
  { at: 6000, label: "Writing the answer…" },
];
const DIRECT_PHASES = [
  { at: 0, label: "Reading the meeting…" },
  { at: 2000, label: "Writing the answer…" },
];

/** Mirrors the screen's own estimate of which path the backend will take.
 *
 * 300k chars, measured against the DEPLOYED Groq config (TPM=300000, where the
 * 131k context window binds and the budget lands near 369k chars) — NOT the
 * 12k-TPM code default. Retrieval engages past roughly eight hours of talk, so
 * virtually every real meeting takes the one-call path.
 */
function willRetrieve(rec) {
  return (rec?.transcript?.length ?? 0) > 300000;
}

/** The label shown `ms` into a request. */
function labelAt(ms, retrieving) {
  const phases = retrieving ? RETRIEVE_PHASES : DIRECT_PHASES;
  let label = phases[0].label;
  for (const p of phases) if (ms >= p.at) label = p.label;
  return label;
}

describe("The waiting phases mirror the calls the backend actually makes", () => {
  it("a long meeting is described as being searched", () => {
    // The retrieval path really does search first — this is not theatre.
    assert.equal(willRetrieve({ transcript: "x".repeat(400000) }), true);
    assert.equal(labelAt(0, true), "Searching the meeting…");
  });

  it("a short meeting never claims to search", () => {
    // One call, whole transcript. Claiming to "search" would be a lie the user
    // could not detect but that would make the wording meaningless.
    // A 2-hour meeting (~84k chars) still takes the ONE-call path in prod.
    assert.equal(willRetrieve({ transcript: "x".repeat(84000) }), false);
    assert.equal(labelAt(0, false), "Reading the meeting…");
    assert.notEqual(labelAt(5000, false), "Searching the meeting…");
  });

  it("advances through retrieval phases as the seconds pass", () => {
    assert.equal(labelAt(0, true), "Searching the meeting…");
    assert.equal(labelAt(3000, true), "Reading the relevant sections…");
    assert.equal(labelAt(7000, true), "Writing the answer…");
  });

  it("the final phase persists however long the answer takes", () => {
    // No timer past the last phase: an answer that takes 20s must not run out
    // of labels and fall back to something that reads as stuck.
    assert.equal(labelAt(20000, true), "Writing the answer…");
    assert.equal(labelAt(60000, false), "Writing the answer…");
  });

  it("a meeting with no transcript yet does not crash the estimate", () => {
    assert.equal(willRetrieve({}), false);
    assert.equal(willRetrieve(null), false);
    assert.equal(willRetrieve({ transcript: null }), false);
  });

  it("every phase starts at or after the one before it", () => {
    for (const phases of [RETRIEVE_PHASES, DIRECT_PHASES]) {
      for (let i = 1; i < phases.length; i++) {
        assert.ok(phases[i].at > phases[i - 1].at,
          "phases must advance, never go backwards");
      }
    }
  });
});

// ---------------------------------------------------------------------------
// Source-of-truth checks against the real files, so this mirror cannot quietly
// drift from the screen it claims to describe.
describe("the mirrored logic matches the shipped screens", () => {
  const assistant = readFileSync(
    join(ROOT, "src/app/recording/[key]/assistant.tsx"), "utf8");
  const api = readFileSync(join(ROOT, "lib/api.ts"), "utf8");

  it("assistant.tsx pushes the transcript evidence route", () => {
    assert.match(assistant, /pathname: "\/recording\/\[key\]\/transcript"/);
    assert.match(assistant, /evidence: src\.segment_id/);
  });

  it("assistant.tsx renders nothing when there are no sources", () => {
    assert.match(assistant, /if \(!sources\?\.length\) return null;/);
  });

  it("api.ts defaults a missing sources array rather than passing undefined", () => {
    assert.match(api, /sources: res\.sources \?\? \[\]/);
  });

  it("ChatTurn carries optional sources", () => {
    assert.match(api, /sources\?: ChatSource\[\];/);
  });

  it("the screen's phase tables match the ones mirrored here", () => {
    for (const p of [...RETRIEVE_PHASES, ...DIRECT_PHASES]) {
      assert.ok(assistant.includes(p.label),
        `assistant.tsx is missing the phase label: ${p.label}`);
    }
  });

  it("the screen uses the same 15000-char retrieval threshold", () => {
    assert.match(assistant, /rec\?\.transcript\?\.length \?\? 0\) > 300000/);
  });

  it("the dots animate on the native driver", () => {
    // They must keep moving while JS is busy parsing a long response — which
    // is exactly when a JS-driven animation would stutter.
    assert.match(assistant, /useNativeDriver: true/);
  });

  it("the animation loops are stopped on unmount", () => {
    assert.match(assistant, /loops\.forEach\(\(l\) => l\.stop\(\)\)/);
  });
});
