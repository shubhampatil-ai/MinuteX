// lib/__tests__/participant-self-tag.test.mjs — recording that you were in a
// meeting, and the empty state that used to make a promise it could not keep.
//
// Run:  node --test lib/__tests__/participant-self-tag.test.mjs
//
// WHAT THIS PINS.
//
//   1. TWO EMPTY STATES, NOT ONE. The screen used to say "once it finishes
//      transcribing, each voice will appear here" for every empty speaker
//      list — including recordings that had ALREADY finished and found none.
//      That is a promise the app cannot keep, and it leaves the user waiting
//      for something that will never arrive. The processing case keeps that
//      copy; the finished case says so plainly and offers a way forward.
//
//   2. UNKNOWN STATUS MEANS "STILL PROCESSING". An older backend sends no
//      recording_status. Treating that as finished would make the app assert
//      "no speakers were identified" on a recording it knows nothing about,
//      so the fallback is the behaviour that already shipped.
//
//   3. ATTENDANCE IS NOT SPEECH. An attendance row must never occupy a
//      speaker slot in the UI's bySpeaker map — rendering one there would
//      show it as a voice in the transcript.
//
//   4. THE APP NEVER BUILDS THE SENTINEL. The reserved speaker_id is the
//      backend's business. The app sends `attendance_only` and reads back an
//      `attendance_only` boolean; it removes a row using the key the API gave
//      it. If this file ever needs to construct "self:" + id, the abstraction
//      has leaked.
//
// Source assertions rather than a render harness: these are TSX modules and
// node --test runs .mjs with no transpile step.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const ROOT = new URL("../../", import.meta.url).pathname
  .replace(/^\/([A-Za-z]:)/, "$1");

const read = (p) => readFileSync(join(ROOT, p), "utf8");

const API = read("lib/api.ts");
const SCREEN = read("src/app/recording/[key]/participants.tsx");

describe("the attendance API is distinct from speaker mapping", () => {
  it("exposes tagAttendee, which sends no speaker_id", () => {
    assert.match(API, /export async function tagAttendee\(/);
    const fn = API.slice(
      API.indexOf("export async function tagAttendee"),
      API.indexOf("// -- Cross-meeting tasks")
    );
    assert.match(fn, /attendance_only: true/,
      "the call must declare itself as attendance");
    assert.doesNotMatch(fn, /speaker_id/,
      "attendance must not claim a speaker label");
  });

  it("participants carry an attendance_only flag", () => {
    assert.match(
      API, /attendance_only\?:\s*boolean/,
      "the app reads a boolean, not a parsed sentinel"
    );
  });

  it("the app never constructs the reserved speaker id", () => {
    // The storage shape is the backend's business. If this fails, the
    // abstraction has leaked into the client.
    assert.doesNotMatch(API, /["'`]self:/,
      "api.ts must not build the sentinel");
    assert.doesNotMatch(SCREEN, /["'`]self:/,
      "the screen must not build the sentinel");
  });

  it("reports the recording status so empty can be read correctly", () => {
    assert.match(API, /recording_status\?:/);
  });
});

describe("the empty state tells the truth", () => {
  it("treats an unknown status as still processing", () => {
    assert.match(
      SCREEN, /if \(!recordingStatus\) return true;/,
      "an older backend must keep the behaviour it already had"
    );
    assert.match(
      SCREEN, /\["complete", "transcribed", "failed"\]\.includes\(recordingStatus\)/,
      "finished states must be named explicitly"
    );
  });

  it("only the processing case promises voices will appear", () => {
    const start = SCREEN.indexOf("stillProcessing ? (");
    assert.ok(start > 0, "the branch must exist");
    const branch = SCREEN.slice(start, start + 900);
    // The promise belongs to the processing half only.
    const promiseAt = branch.indexOf("each voice will appear here");
    const finishedAt = branch.indexOf("No speakers were identified");
    assert.ok(promiseAt > 0, "processing copy must keep the promise");
    assert.ok(finishedAt > 0, "the finished case must have its own copy");
    assert.ok(promiseAt < finishedAt,
      "the promise must sit in the processing branch, before the finished one");
  });

  it("offers the self-tag when processing finished with no speakers", () => {
    assert.match(
      SCREEN, /label="I was in this meeting"/,
      "the finished-with-no-speakers state must offer a way forward"
    );
  });

  it("offers it when speakers DO exist too", () => {
    // Attending without speaking is not only a no-diarization case.
    assert.match(
      SCREEN, /\{speakers\.length > 0 && \(\s*<Button\s*\n\s*label="I was in this meeting"/,
      "someone can attend a well-diarized meeting without saying anything"
    );
  });
});

describe("mapping a speaker syncs every surface that shows the name", () => {
  // THE BUG. Mapping a speaker rewrites the meeting's `speaker_names`
  // SERVER-SIDE. The transcript, the overview summary and generated documents
  // all render that map through the meeting context — but this screen writes
  // through the participants API, not through the context, so nothing told the
  // provider its copy was stale. The name only appeared after navigating away
  // and back, which remounts the provider and refetches.
  const CONTEXT = read("lib/meeting-context.tsx");

  it("the context exposes a speaker-name resync", () => {
    assert.match(
      CONTEXT, /syncSpeakerNames:\s*\(\) => Promise<void>/,
      "the context must expose a resync for writes made elsewhere"
    );
    assert.match(CONTEXT, /const syncSpeakerNames = useCallback\(/);
  });

  it("it refetches the recording AND the things derived from the map", () => {
    const fn = CONTEXT.slice(
      CONTEXT.indexOf("const syncSpeakerNames = useCallback("),
      CONTEXT.indexOf("// UPSERT by type, not a blind prepend")
    );
    assert.ok(fn.length > 0);
    // `rec` is what the transcript and summary read.
    assert.match(fn, /setRec\(data\)/);
    // Documents bake their prose in, so they need the staleness re-check.
    assert.match(fn, /refreshDocumentStatus\(\)/);
    // Tasks resolve their assignee from the map that just changed.
    assert.match(fn, /refreshTaskAssignees\(\)/);
  });

  it("it is published on the context value and its deps", () => {
    const published = CONTEXT.match(/^\s*syncSpeakerNames,$/gm);
    assert.equal(published?.length, 2,
      "syncSpeakerNames must appear in the value AND the dependency list");
  });

  it("the participants screen calls it after a mapping change", () => {
    assert.match(
      // Destructuring may pull in other fields alongside it (e.g. `rec`,
      // added in Phase 2D.3 for organisation-meeting detection) — the
      // invariant this pins is that syncSpeakerNames comes FROM the
      // context, not the exact shape of the destructure.
      SCREEN, /const \{[^}]*\bsyncSpeakerNames\b[^}]*\} = useMeeting\(\)/,
      "the screen must take the resync from the context"
    );
    assert.match(
      SCREEN, /await syncSpeakerNames\(\);/,
      "a speaker mapping must sync the context"
    );
  });

  it("attendance changes do NOT trigger it", () => {
    // Attendance never writes speaker_names, so resyncing would be a wasted
    // fetch — and would bump nothing, since no rendered name changed.
    const calls = (SCREEN.match(/await syncSpeakerNames\(\);/g) || []).length;
    assert.equal(calls, 1,
      "only the speaker-mapping path should resync");
  });
});

describe("attendance never renders as a voice", () => {
  it("attendance rows are kept out of the speaker map", () => {
    assert.match(
      SCREEN, /if \(!p\.attendance_only\) map\[p\.speaker_id\] = p;/,
      "an attendance row must never occupy a speaker slot"
    );
  });

  it("they are listed separately and labelled as not speaking", () => {
    assert.match(SCREEN, /Also attended/);
    assert.match(
      SCREEN, /Attended — did not speak/,
      "the distinction must be visible, not implied"
    );
  });

  it("the picker offers the user themselves", () => {
    // Reuses the existing contact rather than making them type an email.
    const start = SCREEN.indexOf('title="Who was in this meeting?"');
    assert.ok(start > 0, "the attendance picker must exist");
    const block = SCREEN.slice(Math.max(0, start - 400), start);
    assert.match(block, /allowSelf/,
      "the attendance picker must offer That's me");
  });
});
