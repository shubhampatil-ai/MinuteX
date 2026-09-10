// lib/__tests__/org-salesforce-integration.test.mjs — Phase 2D.2: the
// Organisation Salesforce card in the Integrations screen, and the RBAC/
// scope rules that govern it.
//
// Run:  node --test lib/__tests__/org-salesforce-integration.test.mjs
//
// SAME TWO-LEVEL APPROACH gmail-integration.test.mjs uses, for the same
// reason: the modules under test are TSX (no transpile step here), so the
// pure logic is mirrored and tested directly, and cross-cutting rules that
// matter more as an invariant than as one call site's behaviour are checked
// by reading the sources.
//
//   1. LOGIC.
//      - The Organisation Salesforce card's status wording/colour rule
//        (mirrored from OrgSalesforceCard in src/app/integrations/index.tsx).
//      - useWorkspace().can() — reads the server's own `capabilities` map
//        rather than re-deriving anything from `role`, so a capability
//        rename on the backend needs no matching change on the client.
//      - The Organisation Integrations Gmail card's status wording
//        (mirrored from OrgGmailCard) — same underlying per-user
//        Integration the Personal catalog already reads, never a second
//        connection.
//
//   2. SOURCE-LEVEL LINT.
//      - Organisation Salesforce management (connect/disconnect/config save)
//        is called ONLY from the org-scoped screens — never from anywhere
//        that could apply it against the wrong workspace.
//      - Personal Salesforce's own screens/calls are untouched by any
//        Organisation-scoped code path (the hard regression requirement).
//      - No source reads a refresh token, access token or client secret
//        (same invariant gmail-integration.test.mjs already pins, checked
//        again here because this phase added a second OAuth-adjacent
//        surface that could just as easily have leaked one).
//      - The /integrations screen renders Personal and Organisation as two
//        DISJOINT views: Organisation mode never shows the Personal
//        Salesforce card, Personal mode never shows the Organisation
//        Salesforce card, and Gmail is read from the SAME useGmail()/
//        useIntegrations() catalog in both — no second Gmail fetch, no
//        OrgGmailConnections-shaped type anywhere.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const ROOT = new URL("../../", import.meta.url).pathname
  .replace(/^\/([A-Za-z]:)/, "$1");

// --- mirrored from src/app/integrations/index.tsx (OrgSalesforceCard) ------

function orgSalesforceCardView(status, loading) {
  const connected = status?.connected === true;
  const label = loading ? "Checking…" : connected ? "Connected" : "Not connected";
  const colorToken = loading ? "textFaint" : connected ? "success" : "textFaint";
  const actionLabel = connected ? "Manage" : "Connect Salesforce";
  return { connected, label, colorToken, actionLabel };
}

// --- mirrored from lib/workspace-context.tsx (the `can` derivation) --------

function canDo(capabilities, capability) {
  return !!capabilities?.[capability];
}

// --- mirrored from OrgGmailCard in src/app/integrations/index.tsx ---------
// Reads the SAME Integration shape Personal's IntegrationCard reads (via
// useGmail() -> useIntegration("gmail") -> the one useIntegrations()
// catalog) — there is no separate "organisation Gmail" status type.

function orgGmailCardView(integration, loading) {
  const connected = integration?.status === "CONNECTED";
  const needsReauth = integration?.status === "REAUTH_REQUIRED"
    || integration?.status === "ERROR";
  const label = loading ? "Checking…"
    : connected ? "Connected"
      : needsReauth ? "Reconnect required" : "Not connected";
  const actionLabel = connected ? "Manage" : needsReauth ? "Reconnect Gmail" : "Connect Gmail";
  return { connected, needsReauth, label, actionLabel };
}

describe("Organisation Salesforce card status derivation", () => {
  it("reports Not connected with no status yet", () => {
    const v = orgSalesforceCardView(null, false);
    assert.equal(v.connected, false);
    assert.equal(v.label, "Not connected");
    assert.equal(v.actionLabel, "Connect Salesforce");
  });

  it("reports Checking while the status request is in flight", () => {
    const v = orgSalesforceCardView(null, true);
    assert.equal(v.label, "Checking…");
  });

  it("reports Connected once the workspace has a connection", () => {
    const v = orgSalesforceCardView({ connected: true }, false);
    assert.equal(v.connected, true);
    assert.equal(v.label, "Connected");
    assert.equal(v.colorToken, "success");
    assert.equal(v.actionLabel, "Manage");
  });

  it("never reports Connected while still loading, even with a stale status", () => {
    // Guards against a stale previous fetch's status leaking through the
    // loading state of a NEW workspace's fetch after a workspace switch.
    const v = orgSalesforceCardView({ connected: true }, true);
    assert.equal(v.label, "Checking…");
  });

  it("Loading and Not-connected use the same neutral colour, Connected does not", () => {
    const loading = orgSalesforceCardView(null, true);
    const notConnected = orgSalesforceCardView(null, false);
    const connected = orgSalesforceCardView({ connected: true }, false);
    assert.equal(loading.colorToken, notConnected.colorToken);
    assert.notEqual(connected.colorToken, notConnected.colorToken);
  });
});

describe("workspace capability check (`can`)", () => {
  it("is false with no capabilities map at all", () => {
    assert.equal(canDo(undefined, "manage_integrations"), false);
  });

  it("is false when the capability is present but false", () => {
    assert.equal(canDo({ manage_integrations: false }, "manage_integrations"), false);
  });

  it("is true only when the server says so", () => {
    assert.equal(canDo({ manage_integrations: true }, "manage_integrations"), true);
  });

  it("never guesses a capability the server never sent", () => {
    // A capability name typo'd or renamed on the backend must fail CLOSED
    // (no Connect/Disconnect button), never open.
    assert.equal(canDo({ manage_settings: true }, "manage_integrations"), false);
  });
});

describe("Organisation Integrations Gmail card status derivation", () => {
  it("reports Not connected with no Gmail integration yet", () => {
    const v = orgGmailCardView(null, false);
    assert.equal(v.connected, false);
    assert.equal(v.label, "Not connected");
    assert.equal(v.actionLabel, "Connect Gmail");
  });

  it("reports Connected once CONNECTED, with a Manage action", () => {
    const v = orgGmailCardView({ status: "CONNECTED" }, false);
    assert.equal(v.connected, true);
    assert.equal(v.label, "Connected");
    assert.equal(v.actionLabel, "Manage");
  });

  it("reports Reconnect required distinctly from Not connected", () => {
    const reauth = orgGmailCardView({ status: "REAUTH_REQUIRED" }, false);
    const never = orgGmailCardView(null, false);
    assert.equal(reauth.needsReauth, true);
    assert.notEqual(reauth.label, never.label);
    assert.equal(reauth.actionLabel, "Reconnect Gmail");
  });

  it("ERROR status also reads as needing reconnection, not silently connected", () => {
    const v = orgGmailCardView({ status: "ERROR" }, false);
    assert.equal(v.connected, false);
    assert.equal(v.needsReauth, true);
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

describe("Organisation Salesforce scope isolation (source-level)", () => {
  it("getOrgSalesforceAuthorizeUrl is called only from the org-scoped connect flow", () => {
    const offenders = sources()
      .filter((f) => f.path !== "lib/api.ts" && f.path !== "src/app/org-salesforce.tsx")
      .filter((f) => /\bgetOrgSalesforceAuthorizeUrl\s*\(/.test(f.text))
      .map((f) => f.path);
    assert.deepEqual(offenders, []);
  });

  it("disconnectOrgSalesforce is called only from the org-scoped manage screen", () => {
    const offenders = sources()
      .filter((f) => f.path !== "lib/api.ts" && f.path !== "src/app/org-salesforce.tsx")
      .filter((f) => /\bdisconnectOrgSalesforce\s*\(/.test(f.text))
      .map((f) => f.path);
    assert.deepEqual(offenders, []);
  });

  it("saveOrgSalesforceConfig is called only from the org-scoped config screen", () => {
    const offenders = sources()
      .filter((f) => f.path !== "lib/api.ts" && f.path !== "src/app/org-salesforce-config.tsx")
      .filter((f) => /\bsaveOrgSalesforceConfig\s*\(/.test(f.text))
      .map((f) => f.path);
    assert.deepEqual(offenders, []);
  });

  it("Personal Salesforce's own screen never calls an Organisation-scoped function", () => {
    // The hard regression requirement, enforced structurally: salesforce.tsx
    // and salesforce-config.tsx (Personal, Phase 1) must not have grown any
    // reference to the Organisation-scoped API surface added in Phase 2D.
    const ORG_SCOPED = /\b(getOrgSalesforce\w+|saveOrgSalesforceConfig|disconnectOrgSalesforce)\s*\(/;
    for (const path of ["src/app/salesforce.tsx", "src/app/salesforce-config.tsx"]) {
      const text = readFileSync(join(ROOT, path), "utf8");
      assert.doesNotMatch(text, ORG_SCOPED,
        `${path} must not call any Organisation-scoped Salesforce function`);
    }
  });

  it("the org-scoped screens never call a Personal-only Salesforce function", () => {
    // The inverse check: the workspace-scoped screens must not accidentally
    // fall back to reading/writing the PERSONAL connection.
    const PERSONAL_ONLY =
      /\b(getSalesforceStatus|getSalesforceConfig|saveSalesforceConfig|disconnectSalesforce|getSalesforceAuthorizeUrl|getSalesforceObjects|getSalesforceFields)\s*\(/;
    for (const path of ["src/app/org-salesforce.tsx", "src/app/org-salesforce-config.tsx"]) {
      const text = readFileSync(join(ROOT, path), "utf8");
      assert.doesNotMatch(text, PERSONAL_ONLY,
        `${path} must not call any Personal-only Salesforce function`);
    }
  });

  it("the org-scoped screens gate mutating actions on a capability check", () => {
    // RBAC must be visible in the source as a real branch, not just present
    // in a comment — display gating only, but it must actually exist.
    for (const path of ["src/app/org-salesforce.tsx", "src/app/org-salesforce-config.tsx"]) {
      const text = readFileSync(join(ROOT, path), "utf8");
      assert.match(text, /can\(\s*["']manage_integrations["']\s*\)/,
        `${path} must gate on the manage_integrations capability`);
    }
  });

  it("no source reads a refresh token, access token or client secret", () => {
    const FORBIDDEN = /\b(refresh_token|access_token|client_secret)\b/;
    const offenders = sources()
      .filter((f) => FORBIDDEN.test(f.text))
      .map((f) => f.path);
    assert.deepEqual(offenders, []);
  });
});

// ---------------------------------------------------------------------------
// Connected Apps (Personal) vs Organisation Integrations: two disjoint
// views on ONE screen (src/app/integrations/index.tsx), not two copies of
// the same catalog. See that file's own module comment for the full
// rationale — showing Personal Salesforce and Organisation Salesforce
// together (as this screen briefly did) was itself the confusion being
// fixed here, and Gmail must never grow an organisation-scoped twin.
// ---------------------------------------------------------------------------
describe("Connected Apps vs Organisation Integrations scope split (source-level)", () => {
  const screen = () => readFileSync(join(ROOT, "src/app/integrations/index.tsx"), "utf8");
  // .replace strips a trailing \r left by .split("\n") on a CRLF file —
  // without it every line-equality check below fails on Windows checkouts.
  const screenLines = () => screen().split("\n").map((l) => l.replace(/\r$/, ""));

  // Splits the render body into its two ternary branches by INDENTATION,
  // not just the next "(" ... ")" pair — the org branch has its own nested
  // `loadingBody ? ( ... ) : ( ... )` ternary at deeper indentation, which
  // a naive "first ) : ( wins" regex matches instead of the outer split.
  // The outer `{isOrganisation ? (` / `) : (` / closing `)}` are indented
  // exactly 6 spaces in this file; every nested ternary inside either
  // branch is indented deeper. Anchoring on exact indentation (after
  // normalizing CRLF) is what makes this robust to further nested
  // ternaries either branch may grow.
  function splitBranches() {
    const lines = screenLines();
    const startIdx = lines.findIndex((l) => l.trim() === "{isOrganisation ? (");
    assert.ok(startIdx >= 0, "{isOrganisation ? ( line not found");
    const indent = lines[startIdx].match(/^(\s*)/)[1];
    const midIdx = lines.findIndex(
      (l, i) => i > startIdx && l === `${indent}) : (`
    );
    assert.ok(midIdx > startIdx, "outer ) : ( split not found at matching indentation");
    const endIdx = lines.findIndex(
      (l, i) => i > midIdx && l === `${indent})}`
    );
    assert.ok(endIdx > midIdx, "outer closing )} not found at matching indentation");
    return {
      orgBlock: lines.slice(startIdx + 1, midIdx).join("\n"),
      personalBlock: lines.slice(midIdx + 1, endIdx).join("\n"),
    };
  }

  it("Organisation mode never renders the Personal catalog's SECTIONS.map loop", () => {
    const { orgBlock } = splitBranches();
    assert.doesNotMatch(orgBlock, /SECTIONS\.map/,
      "Organisation mode must not render the Personal per-category catalog loop");
  });

  it("Organisation mode renders exactly one OrgSalesforceCard and one OrgGmailCard, nothing else provider-shaped", () => {
    const { orgBlock } = splitBranches();
    assert.match(orgBlock, /<OrgSalesforceCard/);
    assert.match(orgBlock, /<OrgGmailCard/);
    assert.doesNotMatch(orgBlock, /<IntegrationCard/,
      "Organisation mode must not render the generic Personal IntegrationCard");
  });

  it("the Personal (non-organisation) branch still maps every catalog category, unchanged", () => {
    const { personalBlock } = splitBranches();
    assert.match(personalBlock, /SECTIONS\.map/);
    assert.match(personalBlock, /<IntegrationCard/);
    assert.doesNotMatch(personalBlock, /<OrgSalesforceCard|<OrgGmailCard/,
      "Personal mode must not render either Organisation-scoped card");
  });

  it("OrgGmailCard reads useGmail()/useIntegration, never a second Gmail fetch", () => {
    const text = screen();
    assert.match(text, /useGmail\(\)/);
    // No org-scoped Gmail status function exists anywhere to call — the
    // absence of this name is itself the assertion that no such API was
    // introduced.
    assert.doesNotMatch(text, /getOrgGmail|OrgGmailStatus|OrgGmailConnection/i);
  });

  it("no source anywhere defines an organisation-scoped Gmail DATA type or connection concept", () => {
    // Mirrors the backend invariant (Integrations table is keyed by
    // user_id only, no workspace_id) at the frontend: nothing here should
    // name a workspace-scoped Gmail CONNECTION/STATUS/TABLE concept, since
    // one must never be introduced without an explicit architecture
    // decision. This deliberately does NOT forbid "OrgGmailCard" — that is
    // a presentational component reading the existing per-user Gmail
    // integration inside an organisation-scoped SCREEN, not a new Gmail
    // data concept; the distinction is exactly what this test must not
    // blur, so it names the specific forbidden identifiers rather than any
    // string containing "OrgGmail".
    const FORBIDDEN = /\bOrgGmailConnection|OrgGmailStatus|OrgGmailToken|org_gmail_connections?\b/i;
    const offenders = sources()
      .filter((f) => FORBIDDEN.test(f.text))
      .map((f) => f.path);
    assert.deepEqual(offenders, []);
  });

  it("Organisation Salesforce card copy says organisation-owned, not personal", () => {
    const text = screen();
    assert.match(text, /Organisation connection/);
    assert.match(text, /Managed by organisation/);
  });

  it("Organisation Gmail card copy makes clear it is the user's own account", () => {
    const text = screen();
    assert.match(text, /Your Gmail account/);
    assert.doesNotMatch(text, /Organisation Gmail\b/,
      "must never imply a shared organisation mailbox by naming it that way");
  });
});

describe("logo fallback (integration-logos.tsx registry)", () => {
  it("has a real mark for every provider the spec named", () => {
    const text = readFileSync(join(ROOT, "lib/integration-logos.tsx"), "utf8");
    const registryMatch = text.match(/const LOGOS[^{]*\{([\s\S]*?)\n\};/);
    assert.ok(registryMatch, "LOGOS registry not found");
    const registry = registryMatch[1];
    for (const provider of ["salesforce", "gmail", "google_calendar", "outlook"]) {
      assert.match(registry, new RegExp(`\\b${provider}:`),
        `no logo registered for provider "${provider}"`);
    }
  });

  it("falls back to a neutral icon for an unknown provider, never a crash", () => {
    const text = readFileSync(join(ROOT, "lib/integration-logos.tsx"), "utf8");
    assert.match(text, /if \(!Logo\)/,
      "IntegrationLogo must branch on a missing registry entry");
  });
});

describe("the Organisation Salesforce status shape never carries a credential", () => {
  it("OrgSalesforceStatus type declares no token field", () => {
    const text = readFileSync(join(ROOT, "lib/api.ts"), "utf8");
    const typeMatch = text.match(/export type OrgSalesforceStatus = \{([\s\S]*?)\n\};/);
    assert.ok(typeMatch, "OrgSalesforceStatus type not found");
    assert.doesNotMatch(typeMatch[1], /token/i);
  });
});
