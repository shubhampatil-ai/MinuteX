// lib/__tests__/salesforce-link-sheet.test.mjs — the Link to Salesforce
// sheet, checked at source level (rendering needs native modules).
//
// Run:  node --test lib/__tests__/salesforce-link-sheet.test.mjs
//
// Pins: it's behind its own flag and mounted nowhere yet; every decision
// comes from salesforce-link-flow (the sheet keeps no rules of its own, and
// no copy of the record limit); a limit of one shows a single record picker;
// record ids are never rendered; the sheet follows the app's keyboard rules.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const ROOT = new URL("../../", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const read = (rel) => readFileSync(join(ROOT, rel), "utf8");
const sheet = read("lib/salesforce-link-sheet.tsx");

function sources(dir, out = []) {
  for (const name of readdirSync(dir)) {
    if (name === "node_modules" || name === "__tests__") continue;
    const full = join(dir, name);
    if (statSync(full).isDirectory()) sources(full, out);
    else if (/\.(ts|tsx)$/.test(name)) out.push(full);
  }
  return out;
}

describe("Link to Salesforce sheet", () => {
  it("is off unless the build turns it on, and mounted only behind that flag", () => {
    assert.match(sheet, /SALESFORCE_LINK_ENABLED = process\.env\.EXPO_PUBLIC_SALESFORCE_LINK === "1"/);
    const users = [...sources(join(ROOT, "lib")), ...sources(join(ROOT, "src"))]
      .filter((f) => !f.endsWith("salesforce-link-sheet.tsx"))
      .filter((f) => /SalesforceLinkSheet/.test(readFileSync(f, "utf8")))
      .map((f) => f.replace(/\\/g, "/").split("/app/").pop());
    assert.deepEqual(users, ["lib/salesforce-link-card.tsx"]);
    const block = read("lib/webhook-meeting.tsx");
    // With the flag on, the old meeting type + record card never renders.
    assert.ok(block.includes("if (SALESFORCE_LINK_ENABLED) {\n    if (linkConfig?.supported) {")
              || block.includes("if (SALESFORCE_LINK_ENABLED) {\r\n    if (linkConfig?.supported) {"));
    assert.ok(block.includes("setLinkConfigError(webhookError(e))"));
    assert.ok(block.includes('label="Try again"'));
    const eas = JSON.parse(read("eas.json"));
    for (const [name, profile] of Object.entries(eas.build)) {
      assert.notEqual(profile.env?.EXPO_PUBLIC_SALESFORCE_LINK, "1", name);
    }
  });

  it("takes every decision from salesforce-link-flow", () => {
    for (const fn of ["addLink", "removeLink", "setPrimary", "outputOptions", "initialOutputs",
                      "toggleOutput", "linkProblem", "reviewLines", "toLinkPayload",
                      "taskTargetLabel", "pruneTaskTargets", "singleRecord", "maxLinks"]) {
      assert.match(sheet, new RegExp(`\\b${fn}\\(`), fn);
    }
    assert.doesNotMatch(sheet, /MAX_LINKS/, "the limit comes from the configuration");
    assert.doesNotMatch(sheet, /\b[0-9]\s*records?\b/, "no hard-coded counts in the copy");
  });

  it("a limit of one is a single record picker", () => {
    assert.match(sheet, /single \? "RECORD" : `RECORDS · \$\{links\.length\} OF \$\{max\}`/);
    assert.match(sheet, /\{single \? null : \(/, "no main-record star with one record");
    assert.match(sheet, /Search to change the record…/);
  });

  it("never renders a Salesforce record id", () => {
    assert.doesNotMatch(sheet, />\s*\{\s*(l|h|r|target)\??\.id\s*\}/);
    assert.match(sheet, /\{l\.name\}/);
    assert.match(sheet, /\{h\.name\}/);
  });

  it("follows the keyboard rules for a sheet", () => {
    assert.match(sheet, /<Modal[\s\S]*<KeyboardAwareSheet>/);
    assert.match(sheet, /<ScrollView[^>]*keyboardShouldPersistTaps="handled"/);
  });

  it("handles empty, loading, error and save-failure states", () => {
    assert.match(sheet, /Search for the \{single \? "record" : "records"\} this meeting is about\./);
    assert.match(sheet, /No records match/);
    assert.match(sheet, /Couldn't search Salesforce\. Try again\./);
    assert.match(sheet, /Couldn't search Salesforce: \$\{msg\}/, "shows the server's reason");
    assert.match(sheet, /Couldn't link the meeting\. Try again\./);
    assert.match(sheet, /That&apos;s the most records one meeting can be linked to\./);
    assert.match(sheet, /disabled=\{!!problem\}/);
  });

  it("is three short steps with Back / Next always at the bottom", () => {
    assert.match(sheet, /\["Which records\?", "What to send\?", "Check and link"\]/);
    assert.match(sheet, /Step \{step\} of 3/);
    assert.match(sheet, /setStep\(1\); setShowTasks\(false\);/, "every opening starts at step 1");
    assert.match(sheet, /const canNext = step === 1 \? links\.length > 0 && !problem : true;/,
      "no step 2 without a valid record set");
    assert.match(sheet, /label="Back"/);
    assert.match(sheet, /label="Next"/);
  });

  it("shows records as cards with MAIN, Make main and Remove in words", () => {
    assert.match(sheet, />MAIN</);
    assert.match(sheet, />Make main</);
    assert.match(sheet, />Remove</);
    assert.doesNotMatch(sheet, /★|☆/, "no star symbols");
  });

  it("groups outputs as switches, says when the admin fixed one, and folds tasks into one line", () => {
    assert.match(sheet, /<Switch /);
    assert.match(sheet, /title: "Meeting"[\s\S]*title: "People"[\s\S]*title: "Tasks"/);
    assert.match(sheet, /"Set by your admin"/);
    assert.match(sheet, /disabled = option\.locked \|\| !option\.available/);
    assert.match(sheet, /All \$\{tasks\.length\}/);
    assert.match(sheet, /"Choose per task"/);
  });
});

describe("Salesforce link card on the meeting", () => {
  const card = read("lib/salesforce-link-card.tsx");

  it("saves the links, then pushes only the first time", () => {
    assert.match(card, /await setMeetingLinks\(meetingKey, payload\)/);
    assert.match(card, /if \(!job\?\.delivered && !isDeliveryInFlight\(job\)\) await pushWebhookMeeting\(meetingKey\)/);
  });

  it("loads tasks and people only when the sheet opens, and tolerates failures", () => {
    assert.match(card, /getTasks\(meetingKey\)\.catch\(\(\) => \[\]\)/);
    assert.match(card, /getParticipants\(meetingKey\)\.catch\(\(\) => null\)/);
  });

  it("shows records by name and object label, never by id", () => {
    assert.match(card, /\{l\.name\}/);
    assert.doesNotMatch(card, />\s*\{\s*l\.id\s*\}/);
  });
});

describe("Salesforce login (integration user)", () => {
  const card = read("lib/salesforce-login-card.tsx");
  const screen = read("src/app/salesforce/index.tsx");

  it("is behind its own flag, off in every build", () => {
    // Defined in webhook-ui (the Integrations card reads it), re-exported by the card.
    assert.match(read("lib/webhook-ui.tsx"), /SALESFORCE_OAUTH_ENABLED = process\.env\.EXPO_PUBLIC_SALESFORCE_OAUTH === "1"/);
    assert.match(card, /export \{ SALESFORCE_OAUTH_ENABLED \} from "\.\/webhook-ui"/);
    assert.match(screen, /SALESFORCE_OAUTH_ENABLED && activeId \? \(/);
    const eas = JSON.parse(read("eas.json"));
    for (const [name, profile] of Object.entries(eas.build)) {
      assert.notEqual(profile.env?.EXPO_PUBLIC_SALESFORCE_OAUTH, "1", name);
    }
  });

  it("opens Salesforce through an auth session and reads the outcome from the return link", () => {
    assert.match(card, /WebBrowser\.openAuthSessionAsync\(url, integrationReturnUrl\(\)\)/);
    for (const reason of ["denied", "expired", "not_allowed", "no_refresh_token", "exchange_failed"]) {
      assert.ok(card.includes(`  ${reason}: "`), reason);
    }
  });

  it("never handles a token on the device", () => {
    assert.doesNotMatch(card, /access_token|\.refresh_token\b/);
    assert.doesNotMatch(card, /store\.set|AsyncStorage|SecureStore/);
  });

  it("only owners and managers see Connect", () => {
    assert.match(card, /\{admin \? \(/);
    assert.match(screen, /<SalesforceLoginCard workspaceId=\{activeId\} admin=\{admin\}/);
  });
});

describe("MoM PDF at Link", () => {
  const card = read("lib/salesforce-link-card.tsx");

  it("is uploaded before the links are saved, only when the MoM is included", () => {
    const at = card.indexOf("await attachMomPdf()");
    assert.ok(at > 0 && at < card.indexOf("await setMeetingLinks(meetingKey, payload)"));
    assert.ok(card.includes('payload.included_outputs.includes("mom") ? await attachMomPdf() : ""'));
  });

  it("reuses the app's own PDF, and never blocks the link", () => {
    assert.ok(card.includes("await uploadMomPdf(meetingKey, res.mom)"));
    assert.ok(card.includes("so the MoM wasn't sent"));
    assert.ok(!card.includes("minutes text was sent"), "the MoM is never sent as text");
  });

  it("is sent again after every MoM change, so Salesforce never keeps an old PDF", () => {
    const editor = read("lib/mom-editor.tsx");
    assert.equal(editor.match(/void refreshSalesforceMomPdf\(recordingKey, /g)?.length, 3,
                 "save, regenerate and reset");
    const helper = read("lib/salesforce-pdfs.ts");
    assert.match(helper, /if \(!links\?\.included_outputs\.includes\("mom"\)\) return;/);
    assert.match(helper, /momPdfAttachment\(mom, /);
  });
});

describe("half set up: login connected, receiver missing", () => {
  const card = read("lib/salesforce-login-card.tsx");
  const screen = read("src/app/salesforce/index.tsx");
  const block = read("lib/webhook-meeting.tsx");

  it("the login card offers Finish setup when the receiver isn't set up", () => {
    assert.match(screen, /receiverReady=\{endpoint \? endpoint\.configured && endpoint\.enabled : undefined\}/);
    assert.match(screen, /onChanged=\{\(\) => \{ void load\(\); \}\}/);
    assert.match(card, /receiverReady === false \? \(/);
    assert.ok(card.includes("Setup isn&apos;t finished, so meetings can&apos;t be linked or sent yet."));
    assert.match(card, /<Button label="Finish setup" onPress=\{\(\) => \{ void finishSetup\(\); \}\}/);
    assert.ok(card.includes("await setupSalesforceReceiver(workspaceId)"));
  });

  it("every one-click setup outcome is worded, and a login without setup isn't called done", () => {
    for (const status of ["ready", "no_receiver", "not_allowed", "manual", "failed"]) {
      assert.ok(card.includes(`  ${status}: "`), status);
    }
    assert.ok(card.includes("return { ok: false, message: `Signed in to Salesforce. ${SETUP_COPY[setup] ?? SETUP_COPY.failed}` };"));
  });

  it("the meeting says so instead of showing nothing, only when the login is connected", () => {
    assert.match(block, /setLoginOnly\(!!c\.connected\)/);
    assert.match(block, /if \(blockMode === "none"\) \{\s*if \(!loginOnly\) return null;/);
    assert.ok(block.includes("Salesforce isn&apos;t fully set up for this workspace"));
    assert.match(block, /can\("manage_integrations"\) \? \(\s*<Button label="Finish Salesforce setup"/);
  });
});

describe("generated documents as PDFs", () => {
  const helper = read("lib/salesforce-pdfs.ts");
  const ctx = read("lib/meeting-context.tsx");
  const card = read("lib/salesforce-link-card.tsx");

  it("makes only the PDFs the server lists as missing or outdated", () => {
    assert.ok(helper.includes("links?.stale_documents ?? []"));
    assert.ok(helper.includes('!links?.included_outputs.includes("documents")'));
    assert.ok(helper.includes("documentPdfAttachment(doc.content"));
    assert.ok(!/content_base64: doc\.content|textAttachment/.test(helper), "never the text");
  });

  it("runs at Link and whenever the meeting's documents change", () => {
    assert.ok(card.includes('if (payload.included_outputs.includes("documents")) await refreshSalesforceDocumentPdfs(meetingKey);'));
    assert.ok(ctx.includes("if (docsFingerprint) void refreshSalesforceDocumentPdfs(meetingKey, meetingTitle);"));
  });

  it("keeps each file within the Salesforce limit", () => {
    assert.ok(helper.includes("SALESFORCE_MAX_FILE_BYTES = 2 * 1024 * 1024"));
  });
});

describe("Install MinuteX in Salesforce (from the app)", () => {
  const card = read("lib/salesforce-login-card.tsx");

  it("is shown to owners and managers only, and only when a version is configured", () => {
    assert.ok(card.includes("{admin && install ? ("));
    assert.ok(card.includes("const install = conn?.install ?? null;"));
  });

  it("opens Salesforce's own install page; a beta never offers production", () => {
    // The phone's browser: the in-app one re-showed the old sign-in page on Android.
    assert.ok(card.includes("Linking.openURL(install.sandbox_url)"));
    assert.ok(!/openBrowserAsync\(install\./.test(card));
    assert.ok(card.includes('label="Copy install link"'));
    assert.match(card, /\{install\.released \? \(\s*<Button label="Install in production"/);
    assert.ok(card.includes("it installs in sandboxes and Developer Edition orgs only"));
  });
});
