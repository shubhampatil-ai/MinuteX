// lib/__tests__/crm-sync-jobs.test.mjs — Phase 2D.4: async CRM sync job
// status/polling logic in the Organisation CRM Review block.
//
// Run:  node --test lib/__tests__/crm-sync-jobs.test.mjs
//
// SAME TWO-LEVEL APPROACH as this repo's other integration suites: pure
// logic mirrored and tested directly (meeting-crm-records.tsx is TSX, no
// transpile step here), plus source-level checks for the invariants that
// matter across the whole module, not just one call site.
//
//   1. LOGIC.
//      - jobStatusLabel's progression: queued -> syncing -> synced/failed/
//        reconnect required (spec section 21's exact wording).
//      - isCrmSyncJobTerminal / CRM_SYNC_JOB_TERMINAL_STATUSES agree with
//        each other (mirrored from lib/api.ts).
//      - The Push button's disabled rule: never re-enabled while a job is
//        non-terminal, so a user cannot queue a second push while one is
//        already running.
//      - The retry-vs-reconnect-vs-review action rule (spec section 14):
//        RECONNECT_REQUIRED never gets a generic Retry button; a FAILED job
//        with category "permanent" never gets one either; only a FAILED job
//        with a recoverable category gets Retry.
//
//   2. SOURCE-LEVEL LINT.
//      - The UI never assumes a successful enqueue means Salesforce
//        received the data — the per-operation result section is gated on
//        the job's OWN status having progressed past "queuing".
//      - pushMeetingCrm's return type carries no completed-push fields
//        (pushed/tasks/event) — only job_id/status — proving the async
//        contract is enforced at the type level, not just by convention.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const ROOT = new URL("../../", import.meta.url).pathname
  .replace(/^\/([A-Za-z]:)/, "$1");

// --- mirrored from lib/meeting-crm-records.tsx (jobStatusLabel) -----------

function jobStatusLabel(status) {
  switch (status) {
    case "queuing": return "CRM sync queued";
    case "PENDING": return "CRM sync queued";
    case "SYNCING": return "CRM syncing…";
    case "RETRYING": return "CRM sync retrying…";
    case "SYNCED": return "CRM synced";
    case "FAILED": return "CRM sync failed";
    case "RECONNECT_REQUIRED": return "Reconnect Salesforce required";
    default: return "";
  }
}

// --- mirrored from lib/api.ts ----------------------------------------------

const CRM_SYNC_JOB_TERMINAL_STATUSES = ["SYNCED", "FAILED", "RECONNECT_REQUIRED"];

function isCrmSyncJobTerminal(status) {
  return CRM_SYNC_JOB_TERMINAL_STATUSES.includes(status);
}

// --- mirrored EXACTLY from the Push button's disabled expression in
// OrgCrmReviewBlock (meeting-crm-records.tsx) — note "queuing" is
// deliberately excluded from the non-terminal check: while a job is
// merely queuing (the local optimistic state, before the first status
// poll lands), the button already reads "Queuing…" via its label, and
// `pushing` alone covers the disabled state for that brief window.

function pushDisabled(ready, pushing, job) {
  return !ready || pushing
    || (job !== null && job !== "queuing" && !isCrmSyncJobTerminal(job.status));
}

// --- mirrored from the action-button selection rule (spec section 14) -----

function actionFor(job) {
  if (job === "queuing" || job === null) return "push";
  if (job.status === "RECONNECT_REQUIRED") return "reconnect";
  if (job.status === "FAILED" && job.last_error_category === "permanent") return "review";
  if (job.status === "FAILED") return "retry";
  return "push";
}

describe("jobStatusLabel progression", () => {
  it("reports the exact spec-section-21 wording at each stage", () => {
    assert.equal(jobStatusLabel("queuing"), "CRM sync queued");
    assert.equal(jobStatusLabel("PENDING"), "CRM sync queued");
    assert.equal(jobStatusLabel("SYNCING"), "CRM syncing…");
    assert.equal(jobStatusLabel("SYNCED"), "CRM synced");
    assert.equal(jobStatusLabel("FAILED"), "CRM sync failed");
    assert.equal(jobStatusLabel("RECONNECT_REQUIRED"), "Reconnect Salesforce required");
  });

  it("queuing and PENDING read identically to the user", () => {
    // The local optimistic "queuing" state and the server's own PENDING
    // status are deliberately indistinguishable in the UI — see the
    // component's own comment on why.
    assert.equal(jobStatusLabel("queuing"), jobStatusLabel("PENDING"));
  });

  it("RETRYING has its own distinct wording, not reused from SYNCING", () => {
    assert.notEqual(jobStatusLabel("RETRYING"), jobStatusLabel("SYNCING"));
  });
});

describe("terminal status agreement", () => {
  it("SYNCED, FAILED and RECONNECT_REQUIRED are terminal", () => {
    for (const s of ["SYNCED", "FAILED", "RECONNECT_REQUIRED"]) {
      assert.equal(isCrmSyncJobTerminal(s), true, s);
    }
  });

  it("PENDING, SYNCING and RETRYING are NOT terminal", () => {
    for (const s of ["PENDING", "SYNCING", "RETRYING"]) {
      assert.equal(isCrmSyncJobTerminal(s), false, s);
    }
  });
});

describe("Push button disabled rule", () => {
  it("is enabled when ready and no job exists yet", () => {
    assert.equal(pushDisabled(true, false, null), false);
  });

  it("is disabled while not ready, regardless of job state", () => {
    assert.equal(pushDisabled(false, false, null), true);
  });

  it("is disabled while a push is in flight (local pushing state)", () => {
    assert.equal(pushDisabled(true, true, null), true);
  });

  it("is NOT disabled by the non-terminal check while a job is queuing — the label alone communicates state", () => {
    // "queuing" is excluded from the job!==null&&job!=="queuing" guard by
    // design; only the separate `pushing` flag (covering the brief window
    // before the first poll lands) actually disables it.
    assert.equal(pushDisabled(true, false, "queuing"), false);
    assert.equal(pushDisabled(true, true, "queuing"), true);
  });

  it("is disabled while a job is PENDING/SYNCING/RETRYING — never a second push", () => {
    for (const status of ["PENDING", "SYNCING", "RETRYING"]) {
      assert.equal(pushDisabled(true, false, { status }), true, status);
    }
  });

  it("is re-enabled once the job reaches a terminal status", () => {
    for (const status of ["SYNCED", "FAILED", "RECONNECT_REQUIRED"]) {
      assert.equal(pushDisabled(true, false, { status }), false, status);
    }
  });
});

describe("action button selection (spec section 14 — no generic Retry for every failure)", () => {
  it("offers Push with no job or a queuing one", () => {
    assert.equal(actionFor(null), "push");
    assert.equal(actionFor("queuing"), "push");
  });

  it("offers Reconnect for RECONNECT_REQUIRED, never a generic Retry", () => {
    assert.equal(actionFor({ status: "RECONNECT_REQUIRED" }), "reconnect");
  });

  it("offers Review (not Retry) for a permanent FAILED job", () => {
    assert.equal(
      actionFor({ status: "FAILED", last_error_category: "permanent" }),
      "review"
    );
  });

  it("offers Retry for a FAILED job with a recoverable category", () => {
    for (const category of ["transient", "permission", "validation", ""]) {
      assert.equal(
        actionFor({ status: "FAILED", last_error_category: category }),
        "retry",
        category
      );
    }
  });

  it("offers Push again once SYNCED", () => {
    assert.equal(actionFor({ status: "SYNCED" }), "push");
  });
});

// ---------------------------------------------------------------------------
// SOURCE-LEVEL LINT.
// ---------------------------------------------------------------------------
describe("async push contract (source-level)", () => {
  it("pushMeetingCrm's return type carries only job_id/status, never a completed-push shape", () => {
    const text = readFileSync(join(ROOT, "lib/api.ts"), "utf8");
    const fnMatch = text.match(
      /export async function pushMeetingCrm\([\s\S]{0,200}?\): Promise<\{([\s\S]*?)\}>/
    );
    assert.ok(fnMatch, "pushMeetingCrm signature not found");
    const returnShape = fnMatch[1];
    assert.match(returnShape, /job_id/);
    assert.match(returnShape, /status/);
    // The OLD (Phase 2D.3) synchronous shape must not have crept back in —
    // that would mean the function is once again returning completed-push
    // data instead of a job pointer.
    assert.doesNotMatch(returnShape, /pushed|push_errors|tasks|event:/);
  });

  it("OrgCrmReviewBlock gates per-operation result rendering on the job having progressed past queuing", () => {
    const text = readFileSync(join(ROOT, "lib/meeting-crm-records.tsx"), "utf8");
    assert.match(text, /job !== "queuing" && job\.result\?\.pushed/,
      "per-operation results must not render while the job is still queuing");
  });

  it("the component polls getCrmSyncJobStatus rather than trusting the enqueue response", () => {
    const text = readFileSync(join(ROOT, "lib/meeting-crm-records.tsx"), "utf8");
    assert.match(text, /getCrmSyncJobStatus/);
    assert.match(text, /setInterval/,
      "the block must actually poll, not just fetch once");
  });

  it("RECONNECT_REQUIRED never renders a generic Retry button", () => {
    const text = readFileSync(join(ROOT, "lib/meeting-crm-records.tsx"), "utf8");
    const actionsRowMatch = text.match(
      /<View style=\{st\.actionsRow\}>([\s\S]*?)<\/View>\r?\n\s*<\/Card>/
    );
    assert.ok(actionsRowMatch, "actionsRow block not found");
    const block = actionsRowMatch[1];

    // The ternary chain must test RECONNECT_REQUIRED before the generic
    // FAILED+Retry branch, and the RECONNECT_REQUIRED branch's own JSX
    // (up to the next "? (" branch boundary) must not contain a Retry
    // button.
    const reconnectIdx = block.indexOf('status === "RECONNECT_REQUIRED"');
    const permanentIdx = block.indexOf('last_error_category === "permanent"');
    const genericFailedIdx = block.lastIndexOf('job?.status === "FAILED"');
    assert.ok(reconnectIdx >= 0, "RECONNECT_REQUIRED branch not found");
    assert.ok(permanentIdx > reconnectIdx,
      "permanent-category branch must be checked after RECONNECT_REQUIRED");
    assert.ok(genericFailedIdx > permanentIdx,
      "generic FAILED+Retry branch must be the last, most general check");

    const reconnectBranch = block.slice(reconnectIdx, permanentIdx);
    assert.doesNotMatch(reconnectBranch, /"Retry"/);
  });
});
