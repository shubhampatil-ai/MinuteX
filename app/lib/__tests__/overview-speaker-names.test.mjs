// lib/__tests__/overview-speaker-names.test.mjs — "Speaker 0" in AI prose
// follows a rename.
//
// Run:  node --test lib/__tests__/overview-speaker-names.test.mjs
//
// WHY THIS FILE EXISTS. Speaker identity is split in two on purpose: the id
// ("0") is stable and stored, the display name is mutable metadata resolved
// when something renders. That is why renaming a speaker instantly updates the
// transcript, the participants list, the Attendees table and task owners —
// each of them holds an id and calls speakerName() on it.
//
// The dynamic Overview is the one output that cannot work that way, because it
// is not structured data: it is PROSE the model wrote, and the id is spelled
// out inside a sentence ("Speaker 0 raised the timeline") rather than being
// referenced. Nothing could re-resolve it, so a renamed speaker kept reading
// "Speaker 0" in the Overview while every other surface updated. This is the
// join that closes that gap.
//
// THE TEST THAT MATTERS MOST is the two-digit boundary. Without the trailing
// word guard, "Speaker 1" matches inside "Speaker 12" and the prose becomes
// "Amit2 agreed" — one person's words attributed to another, which is the
// failure mode the whole id/name split exists to prevent.
//
// The function under test is pure and mirrored below rather than imported,
// matching the convention in transcript-view.test.mjs and mom-model.test.mjs.
// Its backend twin is speaker_identity.substitute_speaker_names, exercised by
// cloud/tests/test_speaker_identity.py — the two must agree, or the same
// meeting would resolve in the MoM and the shared page but not in the app.
import test from "node:test";
import assert from "node:assert/strict";

// ---------------------------------------------------------------------------
// MIRROR of lib/sources.ts substituteSpeakerNames. Keep in sync.
// ---------------------------------------------------------------------------
const PROSE_LABEL_RE = /\bspeaker[\s_-]*(\d+)(?!\w)/gi;

function substituteSpeakerNames(text, names) {
  const body = text ?? "";
  if (!body || !names) return body;
  return body.replace(PROSE_LABEL_RE, (whole, digits) => {
    const given = names[digits]?.trim() || names[`Speaker ${digits}`]?.trim();
    return given || whole;
  });
}

// ---------------------------------------------------------------------------

test("a named speaker is replaced", () => {
  assert.equal(
    substituteSpeakerNames("Speaker 0 raised the timeline", { 0: "Rahul" }),
    "Rahul raised the timeline"
  );
});

test("an unnamed speaker keeps the label the transcript shows", () => {
  assert.equal(
    substituteSpeakerNames("Speaker 3 agreed", { 0: "Rahul" }),
    "Speaker 3 agreed"
  );
});

test("every occurrence and every speaker is resolved", () => {
  assert.equal(
    substituteSpeakerNames(
      "Speaker 0 proposed it; Speaker 1 agreed with Speaker 0.",
      { 0: "Rahul", 1: "Amit" }
    ),
    "Rahul proposed it; Amit agreed with Rahul."
  );
});

test("the spellings the model actually emits all resolve", () => {
  for (const written of ["Speaker 0", "speaker 0", "speaker_0", "Speaker-0",
                         "SPEAKER 0"]) {
    assert.equal(
      substituteSpeakerNames(`${written} spoke`, { 0: "Rahul" }),
      "Rahul spoke",
      written
    );
  }
});

test("a two-digit label is not matched by a one-digit speaker", () => {
  // Without the (?!\w) guard this becomes "Amit2 agreed" — a stray digit and,
  // worse, the wrong person credited.
  assert.equal(
    substituteSpeakerNames("Speaker 12 agreed", { 1: "Amit" }),
    "Speaker 12 agreed"
  );
  assert.equal(
    substituteSpeakerNames("Speaker 12 agreed", { 12: "Neha" }),
    "Neha agreed"
  );
});

test("a word label is left alone", () => {
  // "agent" renders unprefixed, so there is no "Speaker agent" in the text —
  // and replacing the bare word would corrupt prose that merely used it.
  assert.equal(
    substituteSpeakerNames("The agent escalated it", { agent: "Rahul" }),
    "The agent escalated it"
  );
});

test("a name already in the prose is never rewritten", () => {
  // The model sometimes writes the NAME it read, because the transcript it was
  // given has names substituted into the lines. There is no label to find, and
  // guessing which speaker "Rahul" meant would risk misattribution.
  assert.equal(
    substituteSpeakerNames("Rahul raised the timeline", { 0: "Amit" }),
    "Rahul raised the timeline"
  );
});

test("a rename re-resolves from the label, not from the previous name", () => {
  // Why the STORED prose must stay canonical: every render starts from
  // "Speaker 0", so a second rename lands rather than freezing on the first.
  const stored = "Speaker 0 owns the rollout";
  assert.equal(substituteSpeakerNames(stored, { 0: "Rahul" }),
               "Rahul owns the rollout");
  assert.equal(substituteSpeakerNames(stored, { 0: "Amit" }),
               "Amit owns the rollout");
});

test("a legacy display-form key still resolves", () => {
  // A speaker_names map written before the API canonicalized keys on write.
  assert.equal(
    substituteSpeakerNames("Speaker 0 spoke", { "Speaker 0": "Rahul" }),
    "Rahul spoke"
  );
});

test("empty and missing inputs are safe", () => {
  assert.equal(substituteSpeakerNames("", { 0: "Rahul" }), "");
  assert.equal(substituteSpeakerNames(null, { 0: "Rahul" }), "");
  assert.equal(substituteSpeakerNames("Speaker 0 spoke", null),
               "Speaker 0 spoke");
  assert.equal(substituteSpeakerNames("Speaker 0 spoke", {}),
               "Speaker 0 spoke");
});
