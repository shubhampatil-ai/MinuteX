// lib/__tests__/org-integrations.test.mjs — the Integrations screen's
// Organisation view and the capability/scope rules behind it.
//
// Run:  node --test lib/__tests__/org-integrations.test.mjs
//
// SAME TWO-LEVEL APPROACH gmail-integration.test.mjs uses: the modules under
// test are TSX (no transpile step here), so the pure logic is mirrored and
// tested directly, and cross-cutting rules are checked by reading the sources.
//
//   1. LOGIC.
//      - useWorkspace().can() — reads the server's own `capabilities` map
//        rather than re-deriving anything from `role`.
//      - The Organisation Integrations Gmail card's status wording
//        (mirrored from OrgGmailCard) — the same per-user Integration the
//        Personal catalog reads, never a second connection.
//
//   2. SOURCE-LEVEL LINT.
//      - No source reads a refresh token, access token or client secret.
//      - The /integrations screen renders Personal and Organisation as two
//        DISJOINT views, and Gmail is read from the SAME useGmail()/
//        useIntegrations() catalog in both.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const ROOT = new URL("../../", import.meta.url).pathname
  .replace(/^\/([A-Za-z]:)/, "$1");

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

describe("credential hygiene (source-level)", () => {
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
// the same catalog. Gmail must never grow an organisation-scoped twin.
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
  it("Organisation mode renders the Gmail card, and Salesforce only as the gated Site entry", () => {
    const { orgBlock } = splitBranches();
    assert.match(orgBlock, /<OrgGmailCard/);
    assert.doesNotMatch(orgBlock, /<IntegrationCard/,
      "Organisation mode must not render the generic Personal IntegrationCard");
    assert.match(orgBlock, /SITE_SALESFORCE_ENABLED \? \([\s\S]*?<SiteSalesforceEntry/);
  });

  it("the Personal (non-organisation) branch still maps every catalog category, unchanged", () => {
    const { personalBlock } = splitBranches();
    assert.match(personalBlock, /SECTIONS\.map/);
    assert.match(personalBlock, /<IntegrationCard/);
    assert.doesNotMatch(personalBlock, /<OrgGmailCard/,
      "Personal mode must not render the Organisation-scoped card");
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
