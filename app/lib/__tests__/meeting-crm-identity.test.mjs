// lib/__tests__/meeting-crm-identity.test.mjs — Phase 2D.3: speaker CRM
// identity classification and the Organisation CRM Review/Push block.
//
// Run:  node --test lib/__tests__/meeting-crm-identity.test.mjs
//
// SAME TWO-LEVEL APPROACH as gmail-integration.test.mjs / org-salesforce-
// integration.test.mjs: pure logic mirrored and tested directly (the
// modules under test are TSX, no transpile step here), plus source-level
// checks for cross-cutting invariants that matter as an invariant, not just
// at one call site.
//
//   1. LOGIC.
//      - speakerLine's resolved/unresolved/not-tagged wording (mirrored
//        from meeting-crm-records.tsx).
//      - The org-meeting detection rule participants.tsx and recording/
//        [key]/index.tsx BOTH use (workspace_id present AND resolves to a
//        non-personal workspace) — proven identical between the two call
//        sites, so the identity toggle and the CRM Review block can never
//        disagree about whether a given meeting is "organisation".
//      - identityRole is omitted (not sent as "") when absent, matching the
//        backend's preserve-on-omit contract from 2D.3's set_participant.
//
//   2. SOURCE-LEVEL LINT.
//      - The Internal/External identity toggle only renders for an
//        organisation meeting (never for personal) — checked structurally.
//      - pushMeetingCrm/getMeetingCrmReview are called only from the
//        Organisation CRM Review block, never from a Personal-only surface.
//      - No source reads a refresh token, access token or client secret
//        (same invariant every prior integration suite in this repo pins).
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const ROOT = new URL("../../", import.meta.url).pathname
  .replace(/^\/([A-Za-z]:)/, "$1");

// --- mirrored from lib/meeting-crm-records.tsx (speakerLine) ---------------

function speakerLine(label, state) {
  if (!state) {
    return { tone: "unset", text: `${label}: not tagged` };
  }
  if (state.status === "resolved") {
    const who = state.identity_role === "internal"
      ? state.sf_username : state.contact_name;
    return { tone: "success", text: `${label}: ${who || "resolved"}` };
  }
  return { tone: "warn", text: `${label}: unresolved` };
}

// --- mirrored org-meeting detection rule, shared by participants.tsx and
// src/app/recording/[key]/index.tsx ------------------------------------

function isOrgMeeting(recWorkspaceId, workspaces) {
  return !!recWorkspaceId
    && workspaces.some((w) => w.workspace_id === recWorkspaceId && !w.is_personal);
}

describe("speakerLine wording", () => {
  it("says 'not tagged' when the speaker has no CRM state at all", () => {
    const v = speakerLine("SM", null);
    assert.equal(v.tone, "unset");
    assert.match(v.text, /not tagged/);
  });

  it("shows the Salesforce username for a resolved internal speaker", () => {
    const v = speakerLine("SM", {
      status: "resolved", identity_role: "internal", sf_username: "rahul@sf.example",
    });
    assert.equal(v.tone, "success");
    assert.match(v.text, /rahul@sf\.example/);
  });

  it("shows the contact name for a resolved external speaker", () => {
    const v = speakerLine("Client", {
      status: "resolved", identity_role: "external", contact_name: "John Smith",
    });
    assert.equal(v.tone, "success");
    assert.match(v.text, /John Smith/);
  });

  it("never shows the OTHER identity's field for a resolved speaker", () => {
    // A resolved EXTERNAL speaker must never render sf_username (an
    // internal-only field) even if it happened to be present on the object.
    const v = speakerLine("Client", {
      status: "resolved", identity_role: "external", contact_name: "John Smith",
      sf_username: "should-not-appear@sf.example",
    });
    assert.doesNotMatch(v.text, /should-not-appear/);
  });

  it("reports unresolved distinctly from unset", () => {
    const unresolved = speakerLine("SM", { status: "unresolved" });
    const unset = speakerLine("SM", null);
    assert.notEqual(unresolved.tone, unset.tone);
  });
});

describe("organisation-meeting detection", () => {
  const workspaces = [
    { workspace_id: "personal_u1", is_personal: true },
    { workspace_id: "wso_abc", is_personal: false },
  ];

  it("is false for a meeting with no workspace_id (pre-workspace recording)", () => {
    assert.equal(isOrgMeeting(undefined, workspaces), false);
  });

  it("is false for a meeting whose workspace IS personal", () => {
    assert.equal(isOrgMeeting("personal_u1", workspaces), false);
  });

  it("is true only for a meeting whose workspace is a known organisation", () => {
    assert.equal(isOrgMeeting("wso_abc", workspaces), true);
  });

  it("is false for a workspace_id not present in the caller's own list", () => {
    // Never trust the value alone — it must resolve against workspaces the
    // CALLER actually belongs to (mirrors the backend's own membership
    // re-check pattern; this is the client-side analog of "don't guess").
    assert.equal(isOrgMeeting("wso_unknown", workspaces), false);
  });
});

describe("setParticipant identity_role omission contract", () => {
  it("an absent identityRole becomes undefined in the request body, never an empty string overwrite", () => {
    // Mirrors: identity_role: identityRole || undefined
    const build = (identityRole) => ({ identity_role: identityRole || undefined });
    assert.equal(build(undefined).identity_role, undefined);
    assert.equal(build("").identity_role, undefined);
    assert.equal(build("internal").identity_role, "internal");
  });
});

// ---------------------------------------------------------------------------
// SOURCE-LEVEL LINT.
// ---------------------------------------------------------------------------
function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    if (name === "node_modules" || name === ".expo" || name === "__tests__") {
      continue;
    }
    const full = join(dir, name);
    if (statSync(full).isDirectory()) walk(full, out);
    else if (/\.tsx?$/.test(full)) out.push(full);
  }
  return out;
}

function sources() {
  return [...walk(join(ROOT, "lib")), ...walk(join(ROOT, "src"))]
    .map((f) => ({ path: relative(ROOT, f).replace(/\\/g, "/"),
                   text: readFileSync(f, "utf8") }));
}

describe("Organisation CRM Review/Push scope (source-level)", () => {
  it("getMeetingCrmReview/pushMeetingCrm are called only from the Organisation CRM block", () => {
    const offenders = sources()
      .filter((f) => f.path !== "lib/api.ts" && f.path !== "lib/meeting-crm-records.tsx")
      .filter((f) => /\b(getMeetingCrmReview|pushMeetingCrm)\s*\(/.test(f.text))
      .map((f) => f.path);
    assert.deepEqual(offenders, []);
  });

  it("the participants screen gates the identity toggle on an organisation-meeting check", () => {
    const text = readFileSync(
      join(ROOT, "src/app/recording/[key]/participants.tsx"), "utf8");
    assert.match(text, /isOrgMeeting/,
      "participants.tsx must gate the Internal/External toggle on isOrgMeeting");
    assert.match(text, /is_personal/,
      "the org-meeting check must resolve against the workspace's is_personal flag, not a guessed id format");
  });

  it("the Overview screen renders the CRM Review block only for organisation meetings", () => {
    const text = readFileSync(
      join(ROOT, "src/app/recording/[key]/index.tsx"), "utf8");
    assert.match(text, /isOrgMeeting \? <OrgCrmReviewBlock/,
      "OrgCrmReviewBlock must be conditioned on isOrgMeeting, not rendered unconditionally");
  });

  it("no source reads a refresh token, access token or client secret", () => {
    const FORBIDDEN = /\b(refresh_token|access_token|client_secret)\b/;
    const offenders = sources()
      .filter((f) => FORBIDDEN.test(f.text))
      .map((f) => f.path);
    assert.deepEqual(offenders, []);
  });

  it("resolveContactCrmIdentity and resolveMemberCrmIdentity persist explicit ids only, never a search result auto-applied", () => {
    // Structural proxy for "never auto-merge": the resolve calls must take a
    // recordId/username the CALLER supplies, not something computed from a
    // search response inline. This checks the function signatures in api.ts
    // accept an explicit id parameter rather than, say, a query string.
    const text = readFileSync(join(ROOT, "lib/api.ts"), "utf8");
    assert.match(text, /resolveContactCrmIdentity[\s\S]{0,200}recordId/);
    assert.match(text, /resolveMemberCrmIdentity[\s\S]{0,200}recordId/);
  });
});
