// lib/__tests__/gmail-integration.test.mjs — the Gmail-dependent UI rule.
//
// Run:  node --test lib/__tests__/gmail-integration.test.mjs
//
// WHAT THIS PINS, AND WHY IT IS A TEST RATHER THAN JUST A FIX.
//
// The rule is: a feature that needs Gmail must not be offered as a working
// action when Gmail is not connected — and the moment Gmail IS connected, it
// must become available without the user hunting for it. That rule is easy to
// state, easy to implement once, and very easy to break later: nothing about a
// screen that fetches its own Gmail status looks wrong in a diff. It only
// looks wrong on a handset, after a disconnect, on a screen that was already
// mounted — which is exactly where CI cannot go.
//
// So this suite works on two levels:
//
//   1. LOGIC. useIntegration's status derivation is mirrored below and tested
//      directly. It is the single definition of "usable" the whole app reads,
//      and the interesting cases are the ones that are NOT simply connected or
//      not: a revoked connection (still has a row, still has a token, must be
//      unusable) and an unknown state (must fail closed, never open).
//
//   2. SOURCE-LEVEL LINT. Every Gmail-dependent surface must go through the
//      shared context rather than fetching status itself, or two screens can
//      disagree about whether Gmail is connected. The lint reads the sources
//      and asserts nobody re-derives it locally. Same approach, and same
//      reasoning, as keyboard-avoidance.test.mjs.
//
// The modules under test are TypeScript/TSX, so the pure logic is mirrored
// here rather than imported — matching the convention in task-insights.test.mjs
// and mom-model.test.mjs (node --test runs .mjs with no transpile step).
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const ROOT = new URL("../../", import.meta.url).pathname
  .replace(/^\/([A-Za-z]:)/, "$1");

// --- mirrored from lib/integrations.tsx ------------------------------------

/** The derivation useIntegration performs. `usable` is the ONE thing a
 *  Gmail-dependent action is allowed to branch on. */
function viewFor(integrations, provider, loading = false) {
  const integration = integrations.find((i) => i.provider === provider) ?? null;
  const status = integration?.status ?? "NOT_CONNECTED";
  return {
    integration,
    usable: status === "CONNECTED",
    needsReauth: status === "REAUTH_REQUIRED",
    status,
    loading,
  };
}

/** The card's status wording — mirrored from statusLabel in
 *  src/app/integrations/index.tsx. */
function statusLabel(integration) {
  if (!integration.available && !integration.managed_elsewhere) {
    return "Coming soon";
  }
  switch (integration.status) {
    case "CONNECTED": return "Connected";
    case "REAUTH_REQUIRED": return "Reconnect required";
    case "ERROR": return "Needs attention";
    default: return "Not connected";
  }
}

function integration(over = {}) {
  return {
    provider: "gmail", name: "Gmail", category: "communication",
    description: "", available: true, managed_elsewhere: false,
    status: "NOT_CONNECTED", connected: false, account_identifier: "",
    account_name: "", scopes: [], connected_at: "", updated_at: "",
    message: "", ...over,
  };
}

// ---------------------------------------------------------------------------
describe("integration status derivation", () => {
  it("reports NOT_CONNECTED when the provider has no row", () => {
    const v = viewFor([], "gmail");
    assert.equal(v.status, "NOT_CONNECTED");
    assert.equal(v.usable, false);
    assert.equal(v.needsReauth, false);
    assert.equal(v.integration, null);
  });

  it("is usable only when CONNECTED", () => {
    const v = viewFor([integration({ status: "CONNECTED", connected: true })],
                      "gmail");
    assert.equal(v.usable, true);
    assert.equal(v.needsReauth, false);
  });

  it("is NOT usable when the connection needs reauth", () => {
    // The case a naive "do we have a connection row?" check gets wrong: the
    // row exists and holds a token, and the action must still be unavailable.
    const v = viewFor([integration({ status: "REAUTH_REQUIRED" })], "gmail");
    assert.equal(v.usable, false);
    assert.equal(v.needsReauth, true);
  });

  it("distinguishes needing reauth from never having connected", () => {
    // Both are unusable, but one should say "Reconnect" — telling a user who
    // already set this up that nothing they did was lost.
    const fresh = viewFor([], "gmail");
    const stale = viewFor([integration({ status: "REAUTH_REQUIRED" })], "gmail");
    assert.equal(fresh.usable, stale.usable);
    assert.notEqual(fresh.needsReauth, stale.needsReauth);
  });

  it("is not usable in the ERROR state", () => {
    const v = viewFor([integration({ status: "ERROR" })], "gmail");
    assert.equal(v.usable, false);
    assert.equal(v.needsReauth, false);
  });

  it("fails closed on a status it does not recognise", () => {
    // A newer backend introducing a state this build has never heard of must
    // not accidentally enable a send.
    const v = viewFor([integration({ status: "SOMETHING_NEW" })], "gmail");
    assert.equal(v.usable, false);
  });

  it("never confuses one provider with another", () => {
    const list = [
      integration({ provider: "gmail", status: "NOT_CONNECTED" }),
      integration({ provider: "whatsapp", status: "CONNECTED",
                    available: false }),
    ];
    assert.equal(viewFor(list, "gmail").usable, false);
  });

  it("ignores `connected` and reads `status`", () => {
    // Two fields that could disagree; one rule wins. A backend that set
    // connected:true alongside a dead status must not enable sending.
    const v = viewFor(
      [integration({ status: "REAUTH_REQUIRED", connected: true })], "gmail");
    assert.equal(v.usable, false);
  });
});

describe("integration card status wording", () => {
  it("says Coming soon for a provider that is not available", () => {
    assert.equal(
      statusLabel(integration({ provider: "whatsapp", available: false })),
      "Coming soon");
  });

  it("does not say Coming soon for Salesforce, which really works", () => {
    // It is connected through its own screen, not this flow — dressing it as
    // "Coming soon" would be a lie to anyone who already linked an org.
    assert.equal(
      statusLabel(integration({
        provider: "salesforce", available: false, managed_elsewhere: true,
        status: "NOT_CONNECTED" })),
      "Not connected");
  });

  it("reports each connected state distinctly", () => {
    assert.equal(statusLabel(integration({ status: "CONNECTED" })), "Connected");
    assert.equal(statusLabel(integration({ status: "REAUTH_REQUIRED" })),
                 "Reconnect required");
    assert.equal(statusLabel(integration({ status: "ERROR" })),
                 "Needs attention");
    assert.equal(statusLabel(integration({ status: "NOT_CONNECTED" })),
                 "Not connected");
  });
});

// ---------------------------------------------------------------------------
// SOURCE-LEVEL LINT — nobody re-derives Gmail status locally.
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

describe("Gmail status has exactly one source of truth", () => {
  it("only lib/integrations.tsx calls the raw status endpoints", () => {
    // getIntegrations / getIntegration are the network reads. If a screen
    // calls one directly it holds its own copy of the answer, and a
    // disconnect elsewhere will not reach it — the exact bug the shared
    // context exists to prevent.
    const offenders = sources()
      .filter((f) => f.path !== "lib/api.ts" && f.path !== "lib/integrations.tsx")
      .filter((f) => /\b(getIntegrations|getIntegration)\s*\(/.test(f.text))
      .map((f) => f.path);
    assert.deepEqual(offenders, []);
  });

  it("only lib/integrations.tsx calls disconnectIntegration directly", () => {
    // Disconnecting must go through the context so every screen re-reads.
    const offenders = sources()
      .filter((f) => f.path !== "lib/api.ts" && f.path !== "lib/integrations.tsx")
      .filter((f) => /\bdisconnectIntegration\s*\(/.test(f.text))
      .map((f) => f.path);
    assert.deepEqual(offenders, []);
  });

  it("every screen that sends Gmail mail also gates on Gmail status", () => {
    // A surface that can call a send API must know whether it is allowed to
    // offer that. Either it reads the context itself, or it delegates to a
    // component that does (GmailShareRow / GmailTaskButton).
    // Named precisely rather than matching a bare "sendEmail", because
    // lib/contacts.ts exports its own sendEmail() — a mailto: deep link that
    // predates this feature, needs no Gmail connection, and hands off to
    // whatever mail app the phone has. Flagging that (and the Notify screen
    // using it) would be a false positive, so the Gmail entry points are
    // listed by name. Adding a new Gmail send API means adding it here.
    const SEND_CALLS = /\b(sendMeetingEmail|sendTaskEmail|sendGmailEmail)\s*\(/;
    const GATED = /useGmail\(|useIntegration\(|GmailShareRow|GmailTaskButton/;
    const offenders = sources()
      .filter((f) => f.path !== "lib/api.ts")
      .filter((f) => SEND_CALLS.test(f.text) && !GATED.test(f.text))
      .map((f) => f.path);
    assert.deepEqual(offenders, []);
  });

  it("the Gmail entry points render a prompt instead of a dead action", () => {
    // The requirement allows either hiding or disabling-with-an-explanation.
    // What it does NOT allow is an enabled-looking control that fails. Both
    // components must therefore branch on `usable` before rendering an
    // actionable row, and must name the fix.
    for (const path of ["lib/gmail-share.tsx", "lib/gmail-task-share.tsx"]) {
      const text = readFileSync(join(ROOT, path), "utf8");
      assert.match(text, /if \(!usable\)/,
                   `${path} must branch on usable`);
      assert.match(text, /Connect Gmail/,
                   `${path} must tell the user how to fix it`);
      assert.match(text, /Reconnect/,
                   `${path} must distinguish reconnecting from connecting`);
    }
  });
});

describe("the app never handles provider credentials", () => {
  it("no source reads a refresh token, access token or client secret", () => {
    // The app talks only to the MinuteX backend. A field name like this
    // appearing in app code means someone started routing a credential
    // through the client, which is the thing the whole server-side exchange
    // exists to avoid.
    const FORBIDDEN = /\b(refresh_token|access_token|client_secret)\b/;
    const offenders = sources()
      .filter((f) => FORBIDDEN.test(f.text))
      .map((f) => f.path);
    assert.deepEqual(offenders, []);
  });
});
