// lib/__tests__/webhook-flow.test.mjs — the Salesforce WEBHOOK UI
// (Webhook Contract v2) — the one Salesforce path; on in every build
// unless EXPO_PUBLIC_WEBHOOK_V2=0.
//
// Run:  node --test lib/__tests__/webhook-flow.test.mjs
//
// TWO LEVELS, like site-salesforce-flow.test.mjs:
//   1. LOGIC — lib/webhook-flow.ts imported directly: record field per
//      meeting type (Site Visit required / Internal hidden / Customer Call
//      optional), Push readiness, the delivery status wording (both FAILED
//      variants), configuration version wording, error codes.
//   2. SOURCE-LEVEL — invariants across files: the flag is read from env;
//      a record is never labelled by its Salesforce id; the signing secret is
//      never persisted on the device; the meeting screen shows ONE Salesforce
//      block; participant tagging is not part of the webhook block.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  canPush, canSendNow, configStatusLine, deliveryLine, historyLine, isDeliveryInFlight, meetingBlockMode,
  normalizeWebhookUrl, pushButtonLabel, recordDisplay, recordFieldState, recordLabel, salesforceCardStatus,
  selectedType, testLine, webhookErrorMessage,
} from "../webhook-flow.ts";

const ROOT = new URL("../../", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const read = (rel) => readFileSync(join(ROOT, rel), "utf8");

const SITE_VISIT = { id: "site_visit", name: "Site Visit",
  record_selection: { enabled: true, required: true, label: "Property" } };
const INTERNAL = { id: "internal_meeting", name: "Internal Meeting",
  record_selection: { enabled: false, required: false } };
const CUSTOMER_CALL = { id: "customer_call", name: "Customer Call",
  record_selection: { enabled: true, required: false } };
const CFG = { status: "ok", configuration_version: "17", fetched_at: "2026-09-29T00:00:00Z",
  meeting_types: [SITE_VISIT, INTERNAL, CUSTOMER_CALL] };
const MUMBAI = { id: "a0P123", display_name: "PV-2026-00452 — Mumbai Heights",
  secondary_text: "Property · Pune" };
const ctx = (meeting_type_id, record = null) => ({
  meeting_type_id, meeting_type_name: null, record });
const job = (delivery_status, extra = {}) => ({
  job_id: "whk_1", status: "", delivery_status, attempt: 2, max_attempts: 8,
  has_pending_changes: false, delivered: false, delivered_sequence: 0, last_delivered_at: "",
  lifecycle: "active", next_attempt_at: "", last_error_code: "", last_error_message: "",
  external: null, ...extra });

describe("record field per meeting type", () => {
  it("Site Visit requires, Internal hides, Customer Call is optional", () => {
    assert.equal(recordFieldState(SITE_VISIT), "required");
    assert.equal(recordFieldState(INTERNAL), "hidden");
    assert.equal(recordFieldState(CUSTOMER_CALL), "optional");
    assert.equal(recordFieldState(null), "hidden");
  });
  it("uses the customer's own label", () => {
    assert.equal(recordLabel(SITE_VISIT), "Property");
    assert.equal(recordLabel(CUSTOMER_CALL), "Salesforce record");
  });
  it("a type Salesforce removed is not selected", () => {
    assert.equal(selectedType(CFG, ctx("site_visit")).name, "Site Visit");
    assert.equal(selectedType(CFG, ctx("gone")), null);
  });
});

describe("a record reads as its display name, never its id", () => {
  it("title and secondary line only", () => {
    const d = recordDisplay(MUMBAI);
    assert.deepEqual(d, { title: "PV-2026-00452 — Mumbai Heights", sub: "Property · Pune" });
    assert.ok(!JSON.stringify(d).includes("a0P123"));
    assert.equal(recordDisplay(null), null);
  });
});

describe("Push readiness mirrors the server's checks", () => {
  it("refuses with a visible reason", () => {
    assert.match(canPush(null, ctx("site_visit"), null), /meeting types/);
    assert.match(canPush(CFG, ctx(null), null), /meeting type first/);
    assert.match(canPush(CFG, ctx("gone"), null), /no longer available/);
    assert.match(canPush(CFG, ctx("site_visit"), null), /Property first/);
    assert.match(canPush(CFG, ctx("site_visit", MUMBAI), job("delivering")), /Sending/);
    assert.match(canPush(CFG, ctx("site_visit", MUMBAI), job("failed")), /Retry/);
  });
  it("allows when the type's rules are met", () => {
    assert.equal(canPush(CFG, ctx("site_visit", MUMBAI), null), null);
    assert.equal(canPush(CFG, ctx("internal_meeting"), null), null);
    assert.equal(canPush(CFG, ctx("customer_call"), null), null);
    assert.equal(canPush(CFG, ctx("customer_call"), job("delivered", { delivered: true })), null);
  });
  it("labels a delivered meeting's push as an update", () => {
    assert.equal(pushButtonLabel(null), "Push to Salesforce");
    assert.equal(pushButtonLabel(job("delivered", { delivered: true })), "Send update now");
  });
});

describe("delivery status wording", () => {
  it("covers every status", () => {
    assert.equal(deliveryLine(null).title, "Not sent to Salesforce");
    assert.equal(deliveryLine(job("queued")).tone, "busy");
    assert.equal(deliveryLine(job("update_pending")).title, "Update pending");
    assert.equal(deliveryLine(job("delivering")).title, "Sending…");
    assert.equal(deliveryLine(job("retrying")).title, "Retrying (2/8)");
    const ok = deliveryLine(job("delivered"));
    assert.equal(ok.title, "Delivered");
    assert.match(ok.detail, /automatically/);
  });
  it("tells the two FAILED cases apart", () => {
    const plain = deliveryLine(job("failed"));
    const pending = deliveryLine(job("failed", { has_pending_changes: true }));
    assert.equal(plain.title, "Failed");
    assert.match(plain.detail, /No changes since/);
    assert.equal(pending.title, "Failed — changes pending");
    assert.match(pending.detail, /changed after the failed delivery/);
    assert.notEqual(plain.title, pending.title);
  });
  it("polls only while in flight", () => {
    for (const s of ["queued", "update_pending", "delivering", "retrying"]) {
      assert.ok(isDeliveryInFlight(job(s)), s);
    }
    for (const s of ["delivered", "failed"]) assert.ok(!isDeliveryInFlight(job(s)), s);
  });
});

describe("configuration wording carries the version", () => {
  it("ok, stale, error, never", () => {
    assert.equal(configStatusLine(CFG), "3 meeting types (version 17)");
    assert.match(configStatusLine({ ...CFG, status: "stale" }), /version 17.*last good list/);
    assert.match(configStatusLine({ ...CFG, status: "error", meeting_types: [] }), /Couldn't load/);
    assert.match(configStatusLine(null), /not loaded/);
    assert.match(testLine({ ok: true, at: "t", configuration_version: "17", meeting_type_count: 3 }),
      /version 17, 3 meeting types/);
    assert.match(testLine({ ok: false, at: "t", code: "bad_signature", configuration_version: "" }),
      /signing secret/);
    assert.equal(testLine({}), "Not tested yet.");
  });
});

describe("errors and URL entry", () => {
  it("maps codes, falls back to the server's message", () => {
    assert.match(webhookErrorMessage("site_mode_active"), /earlier Salesforce connection/);
    assert.equal(webhookErrorMessage("unheard_of", "Server said so."), "Server said so.");
  });
  it("normalizes the address lightly (the server does the real SSRF check)", () => {
    assert.deepEqual(normalizeWebhookUrl(" https://x.example.com/minutex/v2/ "),
                     { url: "https://x.example.com/minutex/v2" });
    assert.ok("error" in normalizeWebhookUrl("http://x.example.com"));
    assert.ok("error" in normalizeWebhookUrl("https://x.example.com/?a=1"));
    assert.ok("error" in normalizeWebhookUrl(""));
  });
});

describe("Integrations card status", () => {
  const receiver = { mode: "webhook", endpoint: { configured: true, enabled: true } };
  const noReceiver = { mode: "none", endpoint: { configured: false, enabled: false } };
  const LOGIN = { connected: true, status: "connected" };
  const NO_LOGIN = { connected: false, status: "not_connected" };

  it("a disconnected login is NOT connected, even though the receiver row stays", () => {
    assert.equal(salesforceCardStatus(receiver, NO_LOGIN), "not_connected");
  });

  it("is connected only with both the login and the receiver", () => {
    assert.equal(salesforceCardStatus(receiver, LOGIN), "connected");
    assert.equal(salesforceCardStatus(noReceiver, LOGIN), "setup_needed");
    assert.equal(salesforceCardStatus({ mode: "site", endpoint: { configured: false, enabled: false } }, LOGIN),
                 "setup_needed");
  });

  it("a login Salesforce no longer accepts asks to reconnect", () => {
    assert.equal(salesforceCardStatus(receiver, { connected: false, status: "reconnect_needed" }), "reconnect");
  });

  it("sending switched off is paused, not connected", () => {
    assert.equal(salesforceCardStatus({ mode: "webhook", endpoint: { configured: true, enabled: false } }, LOGIN),
                 "paused");
  });

  it("a build without the login reads the receiver alone", () => {
    assert.equal(salesforceCardStatus(receiver, "not_used"), "connected");
    assert.equal(salesforceCardStatus(noReceiver, "not_used"), "not_connected");
  });

  it("the card reads the login in a login build", () => {
    const ui = read("lib/webhook-ui.tsx");
    assert.ok(ui.includes('SALESFORCE_OAUTH_ENABLED ? getSalesforceOrgConnection(activeId) : Promise.resolve("not_used" as const)'));
    assert.ok(ui.includes("setStatus(salesforceCardStatus(r, login));"));
  });
});

describe("Salesforce screen: no address field, no People section", () => {
  const screen = read("src/app/salesforce/index.tsx");
  it("is removed", () => {
    assert.ok(!screen.includes(">Salesforce address<"));
    assert.ok(!screen.includes("<SectionRule>People</SectionRule>"));
    assert.ok(!screen.includes("startSitePeopleSync"));
    assert.ok(!screen.includes("saveWebhook"));
  });
});

describe("source-level invariants", () => {
  const ui = read("lib/webhook-ui.tsx");
  const block = read("lib/webhook-meeting.tsx");
  const pickers = read("lib/webhook-pickers.tsx");
  const screen = read("src/app/salesforce/index.tsx");
  const meeting = read("src/app/recording/[key]/index.tsx");

  it("the webhook is on in every build; the package UI is hidden in every build", () => {
    // A kill switch ("0"), not an opt-in: production shows the webhook.
    assert.match(ui, /process\.env\.EXPO_PUBLIC_WEBHOOK_V2 !== "0"/);
    const eas = JSON.parse(read("eas.json"));
    for (const [name, profile] of Object.entries(eas.build)) {
      assert.notEqual(profile.env?.EXPO_PUBLIC_WEBHOOK_V2, "0", `${name} disables the webhook`);
      assert.notEqual(profile.env?.EXPO_PUBLIC_SITE_SALESFORCE, "1", `${name} shows the package UI`);
    }
  });

  it("to a person it is simply \"Salesforce\": no webhook or package wording on screen", () => {
    // Visible strings only: JSX text, and string literals passed as labels,
    // titles, alerts and messages. Code identifiers and API codes may say
    // "webhook"; nothing the user reads may, and the retired package is
    // never mentioned.
    const stripComments = (src) => src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
    const visible = (raw) => { const src = stripComments(raw); return [
      ...src.matchAll(/>([^<>{}]+)</g),
      ...src.matchAll(/(?:label|title|placeholder)=\{?"([^"]+)"/g),
      ...src.matchAll(/Alert\.alert\(\s*"([^"]+)",\s*"([^"]+)"/g),
      ...src.matchAll(/set(?:Notice|Error)\("([^"]+)"/g),
    ].flatMap((m) => m.slice(1)).filter(Boolean); };
    const flow = read("lib/webhook-flow.ts");
    const messages = [...flow.matchAll(/^\s+\w+: "([^"]+)",$/gm)].map((m) => m[1]);
    for (const text of [...visible(ui), ...visible(block), ...visible(pickers), ...visible(screen), ...messages]) {
      assert.doesNotMatch(text, /webhook|package/i, `user-visible text says: ${text}`);
    }
    assert.match(ui, /<Text style=\{st\.name\}>Salesforce<\/Text>/);
    assert.match(screen, /title: "Salesforce" \}/);
    assert.match(ui, /router\.push\("\/salesforce" as never\)/);
  });

  it("internal testing builds show Salesforce as 'Coming soon' with nothing to press", () => {
    assert.match(ui, /EXPO_PUBLIC_SALESFORCE_COMING_SOON === "1"/);
    const eas = JSON.parse(read("eas.json"));
    for (const name of ["preview", "sideload"]) {
      assert.equal(eas.build[name].env?.EXPO_PUBLIC_SALESFORCE_COMING_SOON, "1", name);
    }
    for (const name of ["development", "production", "production-apk"]) {
      assert.notEqual(eas.build[name].env?.EXPO_PUBLIC_SALESFORCE_COMING_SOON, "1", name);
    }
    // The card: a plain label, and the Connect/Manage button is not rendered.
    assert.match(ui, /SALESFORCE_COMING_SOON \? \([\s\S]*?Coming soon/);
    assert.match(ui, /!SALESFORCE_COMING_SOON && \(connected \|\| admin\)/);
    // The screen, even by direct link: nothing to press. Meetings: what was
    // already sent stays visible, read-only.
    assert.match(screen, /if \(SALESFORCE_COMING_SOON\) \{[\s\S]*?Salesforce is coming soon/);
    assert.match(meeting, /readOnly=\{SALESFORCE_COMING_SOON\}/);
  });

  it("the contact picker offers Salesforce people for webhook workspaces too", () => {
    const picker = read("lib/contact-picker.tsx");
    assert.match(picker, /SITE_SALESFORCE_ENABLED \|\| WEBHOOK_V2_ENABLED/);
  });

  it("the meeting shows Salesforce only when connected, or when it was already sent", () => {
    const sent = { delivered: true, last_delivered_at: "2026-09-29T10:00:00Z" };
    const live = { mode: "webhook", endpoint: { enabled: true }, job: null };
    assert.equal(meetingBlockMode(live, false), "live");
    // Coming-soon build: never interactive, even when connected.
    assert.equal(meetingBlockMode(live, true), "none");
    assert.equal(meetingBlockMode({ ...live, job: sent }, true), "history");
    // Disconnected (or never set up): history if sent, otherwise nothing.
    for (const mode of ["none", "site", "webhook"]) {
      const off = { mode, endpoint: { enabled: false }, job: null };
      assert.equal(meetingBlockMode(off, false), "none", mode);
      assert.equal(meetingBlockMode({ ...off, job: sent }, false), "history", mode);
      assert.equal(meetingBlockMode({ ...off, job: { delivered: false } }, false), "none", mode);
      assert.equal(meetingBlockMode({ ...off, earlier_record: { name: "x", url: "", delivered_at: "" } },
        false), "history", mode);
    }
    assert.match(historyLine(sent), /^Sent to Salesforce on /);
    assert.equal(historyLine(null, { name: "x", url: "", delivered_at: "" }), "Sent to Salesforce");
    assert.equal(historyLine({ delivered: false }), "Sent to Salesforce");
    // No connect pointer and no buttons on the history card.
    assert.ok(!/isn't connected for this workspace/.test(block));
    // The ONLY setup pointer: login connected but receiver missing
    // ("half set up"), for owners/managers — never for an unconnected
    // workspace, never on the history card.
    assert.equal(block.match(/router\.push\("\/salesforce"/g)?.length ?? 0, 1);
    const half = block.slice(block.indexOf('if (blockMode === "none") {'), block.indexOf('if (blockMode === "history")'));
    assert.match(half, /if \(!loginOnly\) return null;[\s\S]*router\.push\("\/salesforce"/);
    const history = block.slice(block.indexOf('blockMode === "history"'), block.indexOf("// Linked to records (Contract v3)"));
    assert.ok(history.length > 100);
    assert.ok(!/<Button\b/.test(history), "the history card offers something to press");
    assert.match(history, /Open in Salesforce/);
  });

  it("records are rendered by display name, never by id", () => {
    for (const src of [block, pickers]) {
      // Text children only: `key={r.id}` is a React key, not something shown.
      assert.ok(!/>\s*\{\s*(r|record|rec|context\.record)\??\.id\s*\}/.test(src),
        "a record id is rendered as text");
      assert.ok(!/record_id\s*\}/.test(src));
    }
    assert.match(pickers, /r\.display_name/);
    assert.match(block, /record\.title/);
  });

  it("the signing secret is never persisted on the device", () => {
    for (const src of [screen, ui, block]) {
      assert.ok(!/(store|AsyncStorage|SecureStore)\.(set|setItem|setItemAsync)\([^)]*secret/i.test(src));
      assert.ok(!/from "\.\.\/\.\.\/\.\.\/lib\/storage"|from "\.\/storage"/.test(src));
    }
    assert.match(screen, /useState\(""\)[^\n]*\n?/);
    assert.match(screen, /you won&apos;t see this again/);
  });

  it("one Salesforce on screen: the package card and block are removed, not flagged", () => {
    const integrations = read("src/app/integrations/index.tsx");
    assert.doesNotMatch(integrations, /SalesforceCard|SITE_SALESFORCE_ENABLED|site-salesforce/);
    assert.equal((integrations.match(/<WebhookIntegrationCard \/>/g) || []).length, 2, "personal + organisation");
    assert.doesNotMatch(meeting, /SiteSalesforceMeetingBlock|SITE_SALESFORCE_ENABLED/);
    assert.equal((meeting.match(/<WebhookMeetingBlock/g) || []).length, 1);
    assert.match(block, /if \(!enabled \|\| off\) return null;/);
    assert.doesNotMatch(block, /fallback/);
    assert.match(meeting, /enabled=\{WEBHOOK_V2_ENABLED\}/);
  });

  it("participant tagging is not part of the webhook block", () => {
    assert.ok(!/ContactPicker|site-participants|person_key/.test(block));
  });

  it("the record search input lives in a keyboard-aware sheet", () => {
    assert.match(pickers, /<KeyboardAwareSheet>[\s\S]*<SearchBar/);
    assert.match(screen, /<KeyboardAware /);
  });
});

describe("Send now", () => {
  const job = (delivery_status, delivered = true) => ({ delivery_status, delivered });

  it("can be pressed while a delivery waits, not while one is being sent", () => {
    for (const st of ["queued", "update_pending", "retrying", "delivered"]) {
      assert.equal(canSendNow(job(st)), true, st);
    }
    assert.equal(canSendNow(job("delivering")), false);
    assert.equal(canSendNow(job("failed")), false, "a failure has its own Retry");
    assert.equal(canSendNow(null), true);
  });

  it("says what pressing it does", () => {
    assert.equal(pushButtonLabel(job("update_pending")), "Send update now");
    assert.equal(pushButtonLabel(job("retrying")), "Try again now");
    assert.equal(pushButtonLabel(null), "Push to Salesforce");
  });

  it("the link card uses it", () => {
    const card = readFileSync(join(new URL("../../", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1"),
                                   "lib/salesforce-link-card.tsx"), "utf8");
    assert.ok(card.includes('disabled={!!busy || !canSendNow(job)}'));
  });
});
