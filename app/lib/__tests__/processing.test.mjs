// lib/__tests__/processing.test.mjs — backend status → the steps a person sees.
//
// Run:  node --test lib/__tests__/processing.test.mjs
//
// The processing view replaced a bar that filled to a percentage computed
// from which fields happened to be non-empty. These pin the replacement to
// the REAL lifecycle (lib/api.ts RecordingStatus) and keep it honest: stages
// only, no invented steps, no percentage.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { isProcessingStatus, pipelineSteps, stageLabel } from "../processing.ts";

const ROOT = new URL("../../", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const states = (status, opts) => pipelineSteps(status, opts).map((s) => s.state);

describe("pipelineSteps", () => {
  it("has exactly the three stages the backend reports", () => {
    assert.deepEqual(pipelineSteps("transcribing").map((s) => s.key), ["upload", "transcribe", "brief"]);
  });

  it("follows the lifecycle in order", () => {
    assert.deepEqual(states("uploading"), ["active", "pending", "pending"]);
    assert.deepEqual(states("uploaded"), ["done", "active", "pending"]);
    assert.deepEqual(states("transcribing"), ["done", "active", "pending"]);
    assert.deepEqual(states("generating_ai"), ["done", "done", "active"]);
    assert.deepEqual(states("complete"), ["done", "done", "done"]);
  });

  it("'uploaded' reads as waiting for transcription, not as a step of its own", () => {
    const t = pipelineSteps("uploaded")[1];
    assert.equal(t.state, "active");
    assert.equal(t.detail, "Waiting to start");
  });

  it("marks the step that actually failed", () => {
    assert.deepEqual(states("failed"), ["done", "failed", "pending"]);
    assert.deepEqual(states("transcribed"), ["done", "done", "failed"]);
    assert.match(pipelineSteps("failed")[1].detail, /Transcription/);
  });

  it("treats blank / unknown status as complete, like statusMeta", () => {
    for (const s of ["", null, undefined, "something_new"]) {
      assert.deepEqual(states(s), ["done", "done", "done"]);
    }
  });

  it("says a stalled step is slow, on the active step only", () => {
    const steps = pipelineSteps("transcribing", { stuck: true });
    assert.match(steps[1].detail, /longer than usual/);
    assert.equal(steps[0].detail, undefined);
    assert.equal(steps[2].detail, undefined);
  });

  it("never carries a percentage", () => {
    for (const s of ["uploading", "uploaded", "transcribing", "generating_ai", "complete", "failed"]) {
      for (const step of pipelineSteps(s)) {
        assert.doesNotMatch(JSON.stringify(step), /%|progress|percent/i);
      }
    }
  });
});

describe("isProcessingStatus / stageLabel", () => {
  it("agrees with statusMeta's 'processing' kind", () => {
    for (const s of ["uploading", "uploaded", "transcribing", "generating_ai"]) assert.equal(isProcessingStatus(s), true);
    for (const s of ["complete", "transcribed", "failed", "", null]) assert.equal(isProcessingStatus(s), false);
  });
  it("names the current stage", () => {
    assert.equal(stageLabel("generating_ai"), "Writing brief");
    assert.equal(stageLabel("uploaded"), "Queued");
  });
});

describe("the screens use the real status", () => {
  const detail = readFileSync(join(ROOT, "src/app/recording/[key]/index.tsx"), "utf8");
  const desk = readFileSync(join(ROOT, "src/app/(tabs)/index.tsx"), "utf8");

  it("the brief screen drives ProgressSteps from rec.status", () => {
    assert.match(detail, /pipelineSteps\(rec\.status/);
    assert.match(detail, /<ProgressSteps/);
  });

  it("no fixed-width fake progress fill survives", () => {
    assert.doesNotMatch(desk, /width: "62%"/);
    assert.doesNotMatch(detail, /Math\.round\(\(stage \/ steps\.length\) \* 100\)/);
  });
});
