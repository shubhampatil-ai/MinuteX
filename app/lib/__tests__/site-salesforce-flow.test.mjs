// lib/__tests__/site-salesforce-flow.test.mjs — the NEW Salesforce Site UI
// (delivery_type = salesforce_site, behind EXPO_PUBLIC_SITE_SALESFORCE=1).
//
// Run:  node --test lib/__tests__/site-salesforce-flow.test.mjs
//
// TWO LEVELS, like push-logic.test.mjs / gmail-integration.test.mjs:
//
//   1. LOGIC — lib/site-salesforce-flow.ts is imported directly (pure TS, no
//      React Native): Site URL entry, health check, connection states, object
//      search, People Sync counts, pushing the meeting's EXISTING tasks
//      (state, default selection, Salesforce User suggestion, shared record,
//      readiness, due date), meeting push status, and the Site / object /
//      record / permission error wording.
//
//   2. SOURCE-LEVEL — invariants that matter across files, checked in the
//      source because the screens are TSX with no renderer here:
//      tasks are pushed from their meeting, never typed in again; Push
//      Meeting and Push tasks stay separate actions; the meeting block renders only behind the
//      flag; no credential UI in the Site flow; buttons are disabled while
//      an operation runs; the removed OAuth Salesforce files stay gone.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import {
  RECEIVER_PATH, SITE_ERROR_MESSAGES, normalizeSiteUrl, displaySiteUrl, siteErrorMessage, siteStage, STAGE_LABELS,
  healthLine, peopleSummary, filterObjects, taskRelatable, isIsoDate, meetingPushLine, isInFlight,
  pushedRowFor, salesforceStateOf, defaultPushSelection, suggestSalesforceUser, sharedRelationship,
  pushProblem, salesforceDue, objectGroups, relationKindLabel, relationSummary, relationLines,
  hasRelation, relationSourceNote, salesforceBadge, salesforceTypeTag, shouldSearchLive, mergePeople, peopleTabLabel, taskRelationOf, objectUseLabel,
  meetingRecordLine, taskTargetLines, visibleObjectSections, pushButtonLabel, skippedLine,
  orgDisplayName,
} from "../site-salesforce-flow.ts";
import {
  invalidateSiteSalesforce, subscribeSiteSalesforce, siteSalesforceVersion, completedRuns,
} from "../site-salesforce-events.ts";

const ROOT = new URL("../../", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const read = (rel) => readFileSync(join(ROOT, rel), "utf8");

const SITE = "https://acme.my.salesforce-sites.com";

// ===========================================================================
describe("Site URL connection", () => {
  it("adds the receiver path to a bare Site URL", () => {
    assert.deepEqual(normalizeSiteUrl(SITE), { url: SITE + RECEIVER_PATH, error: "" });
    assert.equal(normalizeSiteUrl(`${SITE}/minutex/`).url, `${SITE}/minutex${RECEIVER_PATH}`);
    assert.equal(normalizeSiteUrl("  acme.my.salesforce-sites.com  ").url, SITE + RECEIVER_PATH);
    assert.equal(normalizeSiteUrl("https://ACME.my.salesforce-sites.com").url, SITE + RECEIVER_PATH);
  });

  it("keeps a full receiver URL as it is (no doubled path)", () => {
    const full = `${SITE}/minutex${RECEIVER_PATH}`;
    assert.equal(normalizeSiteUrl(full).url, full);
    assert.equal(normalizeSiteUrl(full.slice(0, -1)).url, full);
    assert.equal(normalizeSiteUrl(`${SITE}/services/apexrest`).url, SITE + RECEIVER_PATH);
  });

  it("refuses what the backend would refuse, with a reason", () => {
    for (const bad of ["", "   ", "http://acme.my.salesforce-sites.com", `${SITE}/?x=1`, `${SITE}/#a`, "https://"]) {
      const out = normalizeSiteUrl(bad);
      assert.equal(out.url, "", bad);
      assert.ok(out.error, bad);
    }
  });

  it("shows the Site URL back without the Apex REST path", () => {
    assert.equal(displaySiteUrl(`${SITE}/minutex${RECEIVER_PATH}`), `${SITE}/minutex`);
    assert.equal(displaySiteUrl(""), "");
    assert.equal(displaySiteUrl(undefined), "");
  });
});

describe("Health check", () => {
  it("ok -> Connected; failure -> Connection failed + a clear reason", () => {
    assert.deepEqual(healthLine({ ok: true }), { ok: true, title: "Connected", detail: "" });
    const bad = healthLine({ ok: false, code: "receiver_unavailable", message: "HTTP 503; will retry." });
    assert.equal(bad.title, "Connection failed");
    assert.equal(bad.detail, "Salesforce isn't reachable right now. Try again in a moment.");
    assert.equal(healthLine(null), null, "no check run yet -> nothing shown");
  });

  it("never shows more than the server's user-facing message", () => {
    const out = healthLine({ ok: false, code: "some_new_code", message: "The receiver refused." });
    assert.equal(out.detail, "The receiver refused.");
    assert.equal(healthLine({ ok: false }).detail, "Something went wrong. Try again.");
  });
});

describe("Connection states", () => {
  it("walks Not connected -> Connecting -> Connected -> Discovering -> Syncing -> Ready", () => {
    assert.equal(siteStage({ connected: false }), "not_connected");
    assert.equal(siteStage({ connected: false, connecting: true }), "connecting");
    assert.equal(siteStage({ connected: true }), "connected");
    assert.equal(siteStage({ connected: true, discoveryStatus: "SYNCING" }), "discovering");
    assert.equal(siteStage({ connected: true, discoveryStatus: "SYNCED", peopleStatus: "PENDING" }),
      "syncing_people");
    assert.equal(siteStage({ connected: true, discoveryStatus: "SYNCED", peopleStatus: "SYNCED" }), "ready");
    assert.equal(siteStage({ connected: true, discoveryStatus: "FAILED", peopleStatus: "SYNCED" }), "connected");
    for (const k of Object.keys(STAGE_LABELS)) assert.ok(STAGE_LABELS[k]);
  });

  it("in-flight means queued, running or retrying — nothing else", () => {
    for (const s of ["PENDING", "SYNCING", "RETRYING"]) assert.equal(isInFlight(s), true, s);
    for (const s of ["SYNCED", "FAILED", "", undefined]) assert.equal(isInFlight(s), false, String(s));
  });
});

describe("People Sync", () => {
  it("reports the total and the Users / Contacts split", () => {
    assert.deepEqual(peopleSummary({ users: 25, contacts: 100, received: 125, skipped: 0 }),
      { total: 125, users: 25, contacts: 100 });
  });

  it("never invents a number the backend did not report", () => {
    assert.deepEqual(peopleSummary({ received: 130, skipped: 5 }), { total: 125, users: null, contacts: null });
    assert.deepEqual(peopleSummary(undefined), { total: null, users: null, contacts: null });
  });
});

// ===========================================================================
const OBJECTS = [
  { api_name: "Site_Visit__c", label: "Site Visit", is_custom: true, queryable: true, task_relation: "what" },
  { api_name: "Booking__c", label: "Booking", is_custom: true, queryable: true, task_relation: "" },
  { api_name: "Lead", label: "Lead", is_custom: false, queryable: true, task_relation: "who" },
  { api_name: "Opportunity", label: "Opportunity", is_custom: false, queryable: false, task_relation: "what" },
];

describe("Object discovery and search", () => {
  it("searches label and API name, case-insensitively", () => {
    assert.deepEqual(filterObjects(OBJECTS, "visit").map((o) => o.api_name), ["Site_Visit__c"]);
    assert.deepEqual(filterObjects(OBJECTS, "SITE_VISIT").map((o) => o.api_name), ["Site_Visit__c"]);
    assert.equal(filterObjects(OBJECTS, "").length, OBJECTS.length);
    assert.deepEqual(filterObjects(OBJECTS, "zzz"), [], "empty state, not an error");
  });

  it("offers for Tasks only objects Tasks can relate to AND whose records can be searched", () => {
    assert.deepEqual(filterObjects(OBJECTS, "", "tasks").map((o) => o.api_name), ["Site_Visit__c", "Lead"]);
    assert.equal(taskRelatable(OBJECTS[1]), false, "activities disabled");
    assert.equal(taskRelatable(OBJECTS[3]), false, "not queryable");
    assert.deepEqual(filterObjects(OBJECTS, "", "custom").map((o) => o.api_name), ["Site_Visit__c", "Booking__c"]);
  });
});

// ===========================================================================
const MX_TASKS = [
  { id: "t1", task: "Send the quotation", status: "Open", assignee: { name: "Shubham", email: "Rep@Acme.com" },
    due: "Friday", due_date_normalized: "2026-09-25" },
  { id: "t2", task: "Book a site visit", status: "Completed", assignee: null, due: "next week", due_date_normalized: "" },
  { id: "t3", task: "Old idea", status: "Cancelled", assignee: null, due: "", due_date_normalized: "" },
  { id: "t4", task: "Share floor plan", status: "Open", assignee: { name: "Priya", email: "priya@x.com" },
    due: "2026-10-01", due_date_normalized: "" },
];
const PEOPLE = [
  { person_key: "salesforce#user#005A", email: "rep@acme.com", status: "active" },
  { person_key: "salesforce#user#005B", email: "priya@x.com", status: "inactive" },
];
const PUSHED = [
  { minutex_task_id: "t2", sync_status: "SYNCED", assignee_person_key: "salesforce#user#005Z",
    related_object_type: "Site_Visit__c", related_record_id: "a01...125", related_record_name: "SV-00125" },
];

describe("Pushing the meeting's existing tasks", () => {
  it("shows each task's Salesforce state", () => {
    assert.equal(salesforceStateOf("t1", PUSHED).label, "Not in Salesforce");
    assert.equal(salesforceStateOf("t2", PUSHED).label, "In Salesforce");
    assert.equal(salesforceStateOf("t2", [{ ...PUSHED[0], sync_status: "RETRYING" }]).key, "sending");
    assert.equal(salesforceStateOf("t2", [{ ...PUSHED[0], sync_status: "FAILED" }]).key, "failed");
    assert.equal(pushedRowFor("t2", PUSHED).related_record_name, "SV-00125");
  });

  it("ticks every task not yet in Salesforce, and never a cancelled one", () => {
    assert.deepEqual(defaultPushSelection(MX_TASKS, PUSHED), ["t1", "t4"]);
    assert.deepEqual(defaultPushSelection(MX_TASKS, [{ ...PUSHED[0], sync_status: "FAILED" }]), ["t1", "t2", "t4"]);
    assert.deepEqual(defaultPushSelection(MX_TASKS, PUSHED, "t2"), ["t2"], "the task the person opened");
    assert.deepEqual(defaultPushSelection(MX_TASKS, PUSHED, "missing"), []);
  });

  it("suggests a Salesforce User only on a fact", () => {
    assert.deepEqual(suggestSalesforceUser(MX_TASKS[0], PEOPLE, PUSHED),
      { personKey: "salesforce#user#005A", reason: "email" }, "exact email, case-insensitive");
    assert.deepEqual(suggestSalesforceUser(MX_TASKS[1], PEOPLE, PUSHED),
      { personKey: "salesforce#user#005Z", reason: "previous" }, "who it went to last time");
    assert.deepEqual(suggestSalesforceUser(MX_TASKS[3], PEOPLE, PUSHED),
      { personKey: "", reason: "" }, "an inactive User is never suggested");
    assert.deepEqual(suggestSalesforceUser({ id: "t9", task: "x", status: "Open",
      assignee: { name: "Shubham" } }, PEOPLE, []), { personKey: "", reason: "" }, "a name alone is not proof");
  });

  it("reuses the record the meeting's tasks were already pushed onto", () => {
    assert.deepEqual(sharedRelationship(PUSHED),
      { objectApiName: "Site_Visit__c", recordId: "a01...125", recordName: "SV-00125" });
    assert.equal(sharedRelationship([]), null);
    assert.equal(sharedRelationship([...PUSHED, { ...PUSHED[0], minutex_task_id: "t1",
      related_record_id: "a01...999" }]), null, "records disagree -> the person chooses");
  });

  it("by default needs only tasks and a Salesforce User each: the record comes from the meeting", () => {
    const inherit = { selected: ["t1", "t4"], objectApiName: "", recordId: "", meetingRelated: true,
                      assigneeFor: () => "salesforce#user#005A" };
    assert.equal(pushProblem(inherit), "", "no object or record to pick");
    assert.match(pushProblem({ ...inherit, selected: [] }), /at least one task/);
    assert.match(pushProblem({ ...inherit, meetingRelated: false }),
      /Tag a Salesforce Contact in this meeting, or change the related Salesforce record/);
    assert.equal(pushProblem({ ...inherit, assigneeFor: (id) => (id === "t1" ? "k" : "") }),
      "Choose a Salesforce User for 1 task.");
  });

  it("Change related Salesforce record: an object needs its record", () => {
    const base = { selected: ["t1"], objectApiName: "Site_Visit__c", recordId: "a01",
                   assigneeFor: () => "k" };
    assert.equal(pushProblem(base), "", "an explicit record needs no meeting relationship");
    assert.match(pushProblem({ ...base, recordId: "" }), /record/);
  });

  it("sends the resolved due date, never a guessed one", () => {
    assert.deepEqual(salesforceDue(MX_TASKS[0]), { date: "2026-09-25", spoken: "" });
    assert.deepEqual(salesforceDue(MX_TASKS[1]), { date: "", spoken: "next week" });
    assert.deepEqual(salesforceDue(MX_TASKS[3]), { date: "2026-10-01", spoken: "" });
    assert.ok(isIsoDate("2026-09-28"));
    for (const bad of ["2026-02-30", "28/09/2026", "tomorrow"]) assert.equal(isIsoDate(bad), false, bad);
  });
});

describe("Errors a tester can act on", () => {
  it("Site, object, record and permission errors use the agreed wording", () => {
    for (const code of ["timeout", "network_error", "receiver_unavailable"]) {
      assert.equal(siteErrorMessage(code), "Salesforce isn't reachable right now. Try again in a moment.");
    }
    for (const code of ["mapping_invalid", "unknown_object", "invalid_object"]) {
      assert.equal(siteErrorMessage(code), "This Salesforce object is no longer available. Refresh Salesforce metadata.");
    }
    assert.equal(siteErrorMessage("invalid_record"), "The selected Salesforce record could not be found.");
    for (const code of ["salesforce_permission_denied", "permission_denied", "forbidden"]) {
      assert.equal(siteErrorMessage(code), "You do not have permission to create a Task for this Salesforce record.");
    }
  });

  it("unknown codes keep the server's user-facing message", () => {
    assert.equal(siteErrorMessage("brand_new", "Salesforce said no."), "Salesforce said no.");
    assert.equal(siteErrorMessage(undefined, "x"), "x");
  });
});

describe("Push Meeting status", () => {
  it("not pushed / pushing / pushed / failed", () => {
    assert.deepEqual(meetingPushLine(null), { tone: "none", text: "Not pushed yet" });
    assert.equal(meetingPushLine({ status: "PENDING" }).tone, "pending");
    assert.equal(meetingPushLine({ status: "RETRYING" }).text, "Retrying…");
    assert.deepEqual(meetingPushLine({ status: "SYNCED" }), { tone: "success", text: "Pushed to Salesforce" });
    const failed = meetingPushLine({ status: "FAILED", last_error_code: "transcript_too_large" });
    assert.equal(failed.tone, "error");
    assert.match(failed.text, /131,072/);
  });
});

// ===========================================================================
// Source-level invariants
// ===========================================================================
const PUSH = read("src/app/site-salesforce/push-tasks.tsx");
const INDEX = read("src/app/site-salesforce/index.tsx");
const BLOCK = read("lib/site-salesforce-meeting.tsx");
const MEETING = read("src/app/recording/[key]/index.tsx");
const TAGGING = read("lib/speaker-tagging.tsx");
const API = read("lib/api.ts");
const SITE_API = API.slice(API.indexOf("// Salesforce Site — org connection (NEW architecture)"));
const SITE_FILES = [
  "src/app/site-salesforce/index.tsx", "src/app/site-salesforce/push-tasks.tsx",
  "lib/site-salesforce-meeting.tsx", "lib/site-salesforce-pickers.tsx",
  "lib/site-salesforce-ui.tsx", "lib/site-salesforce-flow.ts",
];
const code = (src) => src.replace(/\/\/.*$/gm, "").replace(/\/\*[\s\S]*?\*\//g, "");

describe("Tasks are pushed from their meeting, not typed in again", () => {
  it("the push screen reads the meeting's own tasks and sends only the MinuteX task id + choices", () => {
    assert.match(PUSH, /getTasks\(meetingKey\)/);
    assert.match(PUSH, /pushTaskToSalesforce\(ws, \{\s*minutex_task_id: id, assignee_person_key: owner\.person_key,/);
    assert.doesNotMatch(code(PUSH), /subject:|TextField/, "no retyping of the task's content");
    assert.match(PUSH, /!meetingKey\) \{[\s\S]{0,300}Open a meeting first/);
  });

  it("the ad-hoc Create Task form is gone", () => {
    assert.equal(existsSync(join(ROOT, "src/app/site-salesforce/new-task.tsx")), false);
    for (const f of SITE_FILES) assert.doesNotMatch(code(read(f)), /site-salesforce\/new-task/, f);
    assert.doesNotMatch(SITE_API, /createSiteSalesforceTask|updateSiteSalesforceTask/);
  });

  it("the meeting block opens the push screen with this meeting's key, title and workspace", () => {
    assert.match(BLOCK, /pathname: "\/site-salesforce\/push-tasks"/);
    assert.match(BLOCK, /meeting: meetingKey, title, workspace: workspaceId/);
    assert.match(BLOCK, /openPush\(t\.minutex_task_id \|\| undefined\)/, "tap a pushed task -> push it again");
  });
});

describe("Push Meeting and Push tasks are separate actions", () => {
  it("the block pushes the meeting via the meeting-delivery route only", () => {
    assert.match(BLOCK, /pushSiteMeeting\(meetingKey\)/);
    assert.doesNotMatch(code(BLOCK), /pushTaskToSalesforce/);
  });

  it("the push screen never pushes the meeting", () => {
    assert.doesNotMatch(code(PUSH), /pushSiteMeeting|retrySiteMeeting/);
  });
});

describe("Meeting screen: only behind the flag, nothing else changed", () => {
  it("the retired package block is not rendered on the meeting screen", () => {
    // Salesforce is ONE connection now (the webhook; see webhook-flow.test).
    // The package's block is removed from the screen, not hidden behind a
    // flag, so no build setting or stale bundle can show a second Salesforce.
    const uses = MEETING.match(/<SiteSalesforceMeetingBlock/g) || [];
    assert.equal(uses.length, 0);
  });

  it("carries no trace of the removed OAuth CRM blocks", () => {
    assert.doesNotMatch(MEETING, /OrgCrmReviewBlock|CrmRecordsBlock|meeting-crm-records/);
  });
});

describe("No Salesforce credentials in the Site flow", () => {
  it("no client id/secret, token, OAuth or External Client App UI", () => {
    const banned = /client_secret|client_id|consumer key|consumer secret|External Client App|access_token|refresh_token|WebBrowser|openAuthSession/i;
    for (const f of SITE_FILES) assert.doesNotMatch(code(read(f)), banned, f);
    assert.doesNotMatch(code(SITE_API), /client_secret|client_id/, "the Site API section");
  });

  it("no secret, link or package id passes through the app: Salesforce pairs with MinuteX directly", () => {
    const CONNECT = read("src/app/site-salesforce/connect.tsx");
    for (const src of [code(INDEX), code(CONNECT), code(read("src/app/integrations/index.tsx"))]) {
      assert.doesNotMatch(src, /shared_secret|secureTextEntry|TextField|mxc1|packageId|04t/,
        "nothing technical is typed into or shown by the app");
    }
    for (const f of SITE_FILES) {
      assert.doesNotMatch(code(read(f)), /AsyncStorage|SecureStore/, `${f}: nothing Salesforce is persisted on the device`);
    }
  });

  it("reconnecting goes through the same install flow", () => {
    assert.match(INDEX, /Reinstall or reconnect/);
    assert.match(INDEX, /router\.push\("\/site-salesforce\/connect"/);
  });
});

describe("Buttons are disabled while an operation runs", () => {
  it("every action button on the connection screen honours busy state", () => {
    const buttons = INDEX.match(/<Button[\s\S]*?\/>/g) || [];
    assert.ok(buttons.length >= 6);
    for (const b of buttons) assert.match(b, /disabled=\{[^}]*anyBusy/, b.slice(0, 60));
  });

  it("the push screen disables Push until ready and while a push is running", () => {
    assert.match(PUSH, /disabled=\{!!problem \|\| pushing \|\| inFlight\}/);
    assert.match(PUSH, /disabled=\{pushing\}/, "checkboxes lock while pushing");
    assert.match(BLOCK, /disabled=\{busy \|\| pushing \|\| !endpointEnabled \|\| !canPush\}/);
  });
});

describe("Loading and empty states", () => {
  it("each list says what to do when it is empty", () => {
    assert.match(PUSH, /No tasks in this meeting/);
    const pickers = read("lib/site-salesforce-pickers.tsx");
    assert.match(pickers, /No synced Salesforce Users match/);
    assert.match(pickers, /records match/);
    assert.match(pickers, /refresh them in Settings → Integrations →/);
  });

  it("the meeting block shows nothing while loading and a retry when loading fails", () => {
    assert.match(BLOCK, /if \(!loaded\) return null;/);
    assert.match(BLOCK, /Couldn't load Salesforce\./);
  });
});

// ===========================================================================
// Phase 2: People, filtered objects, the meeting relationship, inheritance
// ===========================================================================
const PICKERS = read("lib/site-salesforce-pickers.tsx");
const CONTACTS = read("src/app/contacts.tsx");
const CONTACT_PICKER = read("lib/contact-picker.tsx");
const PARTICIPANTS = read("src/app/recording/[key]/participants.tsx");

describe("Objects: grouped, labelled, no API names", () => {
  const OFFERED = [
    { api_name: "Account", label: "Account", is_custom: false, task_relation: "what", queryable: true },
    { api_name: "Contact", label: "Contact", is_custom: false, task_relation: "who", queryable: true },
    { api_name: "Site_Visit__c", label: "Site Visit", label_plural: "Site Visits", is_custom: true,
      task_relation: "what", queryable: true },
  ];
  it("groups standard objects before custom objects and drops empty groups", () => {
    assert.deepEqual(objectGroups(OFFERED).map((g) => [g.title, g.data.map((o) => o.label)]),
      [["Standard objects", ["Account", "Contact"]], ["Custom objects", ["Site Visit"]]]);
    assert.deepEqual(objectGroups(OFFERED.slice(0, 1)).map((g) => g.title), ["Standard objects"]);
    assert.deepEqual(objectGroups([]), []);
  });
  it("filters standard / custom and searches the plural label", () => {
    assert.deepEqual(filterObjects(OFFERED, "", "standard").map((o) => o.label), ["Account", "Contact"]);
    assert.deepEqual(filterObjects(OFFERED, "", "custom").map((o) => o.label), ["Site Visit"]);
    assert.deepEqual(filterObjects(OFFERED, "visits").map((o) => o.label), ["Site Visit"]);
  });
  it("says how a Task relates in words, not WhoId/WhatId", () => {
    assert.equal(relationKindLabel({ task_relation: "who" }), "Person");
    assert.equal(relationKindLabel({ task_relation: "what" }), "Record");
  });
  it("no screen or picker renders an object API name, a record id or WhoId/WhatId", () => {
    for (const [name, src] of [["pickers", PICKERS], ["block", BLOCK], ["push", PUSH]]) {
      const ui = code(src);
      // Rendered text only: `key={o.api_name}` is a React key, not text.
      assert.doesNotMatch(ui, /(?<!=)\{\s*(o|obj|chosen)\??\.api_name\s*\}/, `${name}: API name shown`);
      assert.doesNotMatch(ui, /(?<!=)\{\s*r\.id\s*\}|salesforce_task_id\}|related_record_id \}/, `${name}: an id shown`);
      assert.doesNotMatch(ui, /WhoId|WhatId/, `${name}: Task field names shown`);
    }
    assert.doesNotMatch(code(INDEX), /label="Org ID"/, "no org id row");
  });
});

describe("The meeting's Salesforce relationship", () => {
  const AUTO = { who: { object_label: "Contact", name: "Asha Rao", source: "tagged_contact" },
                 what: null, automatic: true };
  const BOTH = { who: { object_label: "Contact", name: "Asha Rao", source: "tagged_contact" },
                 what: { object_label: "Opportunity", name: "Big Deal", source: "manual" }, automatic: true };
  it("reads as names: Salesforce: Asha Rao (· Big Deal)", () => {
    assert.equal(relationSummary(AUTO), "Asha Rao");
    assert.equal(relationSummary(BOTH), "Asha Rao · Big Deal");
    assert.equal(relationSummary(null), "");
    assert.deepEqual(relationLines(BOTH), [{ label: "Contact", name: "Asha Rao" },
                                           { label: "Opportunity", name: "Big Deal" }]);
    assert.equal(hasRelation(null), false);
    assert.equal(hasRelation(AUTO), true);
  });
  it("explains where it came from", () => {
    assert.match(relationSourceNote(AUTO), /tagged in this meeting/);
    assert.match(relationSourceNote({ ...AUTO, automatic: false }), /Chosen/);
    assert.match(relationSourceNote(null), /Tag a synced Salesforce Contact/);
  });
  it("the meeting card is Destination -> Record -> Push, with nothing technical", () => {
    const ui = code(BLOCK);
    assert.match(ui, />Destination</);
    assert.match(ui, />Record</);
    assert.match(ui, /setSiteMeetingDestination\(workspaceId, meetingKey,/);
    assert.match(ui, /searchSiteDestinationRecords\(workspaceId, destination\.destination_id/);
    assert.match(ui, /Search Salesforce destinations/);
    assert.match(ui, /✓ Synced to Salesforce/);
    assert.match(ui, /Change destination/);
    assert.match(ui, /Open in Salesforce/);
    for (const banned of ["always creates", "Linked to", "Chosen for this meeting", "Related To",
                          "MinuteX Meeting", "link to an existing record", "Change linked record",
                          "api_name", "Apex", "record_id}"]) {
      assert.ok(!ui.includes(banned), `meeting card must not say "${banned}"`);
    }
    assert.doesNotMatch(ui, /<ObjectPicker/, "no object/API picker on the meeting card");
  });
  it("the push screen inherits by default and pickers appear only after Change related Salesforce record", () => {
    const ui = code(PUSH);
    assert.match(ui, /getSiteMeetingRelation\(ws, meetingKey\)/);
    assert.match(ui, /Change related Salesforce record/);
    assert.ok(ui.indexOf("<ObjectPicker") > ui.indexOf("!changing ?"), "ObjectPicker only when changing");
    assert.match(ui, /\.\.\.\(override \? \{ object_api_name/, "object/record sent only for an override");
    assert.doesNotMatch(ui, /sharedRelationship/, "the meeting relationship, not a guess from past pushes");
  });
});

describe("People: Contacts and Users, with counts, never stale", () => {
  it("labels people tabs with counts once known", () => {
    assert.equal(peopleTabLabel("contact", { contacts: 23, users: 4 }), "Contacts (23)");
    assert.equal(peopleTabLabel("user", { contacts: 23, users: 4 }), "Users (4)");
    assert.equal(peopleTabLabel("user", null), "Users");
  });
  it("badges contacts linked to Salesforce", () => {
    assert.equal(salesforceBadge({ people_provider: "salesforce", people_type: "contact" }), "Salesforce Contact");
    assert.equal(salesforceBadge({ people_provider: "salesforce", people_type: "user" }), "Salesforce User");
    assert.equal(salesforceBadge({}), "");
  });
  it("Contacts shows a Salesforce segment with Contacts/Users from the People Store", () => {
    assert.match(CONTACTS, /searchSitePeoplePage\(activeId, \{ q: debounced, type: sfType/);
    assert.match(CONTACTS, /peopleTabLabel\("contact", sfCounts\)/);
    assert.match(CONTACTS, /peopleTabLabel\("user", sfCounts\)/);
    assert.match(CONTACTS, /useSiteSalesforceVersion\("people"/, "reloads when a sync finishes");
  });
  it("every choose-a-person sheet offers synced Salesforce people and resolves them to a contact", () => {
    assert.match(CONTACT_PICKER, /From Salesforce/);
    assert.match(CONTACT_PICKER, /contactForSitePerson\(\s*sfWorkspace, p\.person_key/);
    assert.match(CONTACT_PICKER, /onPick\(contact\)/);
    // Speaker tagging on every surface goes through the shared module, whose
    // picker is scoped to the MEETING's workspace.
    assert.match(TAGGING, /salesforceWorkspaceId=\{tagging\.workspaceId\}/);
    assert.match(TAGGING, /workspaceId: rec\?\.workspace_id/);
    assert.match(PARTICIPANTS, /<SpeakerContactPicker/);
    assert.match(MEETING, /<SpeakerTagSheet/);
    // The attendance picker on Participants is its own, and scoped the same.
    assert.match(PARTICIPANTS, /salesforceWorkspaceId=\{workspaceId\}/);
  });
  it("a finished sync/connect/disconnect is broadcast, and tagging refreshes the relationship", () => {
    assert.match(INDEX, /completedRuns\(lastRun\.current, next\)/);
    assert.match(read("src/app/site-salesforce/connect.tsx"),
      /invalidateSiteSalesforce\("connection", "objects", "people", "relation"\)/, "connecting");
    assert.match(INDEX, /invalidateSiteSalesforce\("connection", "objects", "people", "relation"\)/);
    // Every tag write (speaker, attendee, clear) lives in the shared module.
    assert.equal((TAGGING.match(/invalidateSiteSalesforce\("relation"\)/g) || []).length, 3);
  });
});

describe("Custom objects without activities: meetings yes, Tasks no", () => {
  it("labels what each object can be used for", () => {
    assert.match(objectUseLabel({ task_relation: "what" }), /meetings and Tasks/);
    assert.match(objectUseLabel({ task_relation: "" }), /Meetings only .*Allow Activities/);
  });
  it("Tasks carry only the sides Salesforce allows, and name the rest", () => {
    const rel = { who: { object_label: "Contact", name: "Asha Rao" },
                  what: { object_label: "Site Visit", name: "SV-1", tasks_supported: false },
                  automatic: true };
    const { relation, skipped } = taskRelationOf(rel);
    assert.equal(relationSummary(relation), "Asha Rao");
    assert.deepEqual(skipped, ["Site Visit"]);
    assert.equal(hasRelation(taskRelationOf({ what: rel.what }).relation), false);
    assert.deepEqual(taskRelationOf(null), { relation: null, skipped: [] });
  });
  it("the meeting card offers every object; Task pickers only Task-capable ones", () => {
    assert.doesNotMatch(BLOCK, /<ObjectPicker/, "the meeting card uses destinations, not raw objects");
    assert.match(PUSH, /<ObjectPicker workspaceId=\{ws\} value=\{obj\} capability="task"/);
    assert.match(PICKERS, /scope: "visible", capability/);
    assert.match(PICKERS, /scope: "all", capability, q: term/);
  });
});

describe("Pushing to a destination record", () => {
  const PUSHED = { object_label: "MinuteX Meeting", name: "Client Discussion",
                   destination: { name: "Site Visit", record_name: "Mumbai Tower A" } };
  it("names where the meeting is, in the customer's words", () => {
    assert.equal(meetingRecordLine(PUSHED), "Site Visit · Mumbai Tower A");
    assert.equal(meetingRecordLine({ object_label: "MinuteX Meeting", name: "x" }), "This meeting in Salesforce");
    assert.equal(meetingRecordLine(null), "");
  });
  it("Push to Salesforce first, then Update Salesforce; a failure offers Try again", () => {
    assert.equal(pushButtonLabel({ pushed: false, failed: false }), "Push to Salesforce");
    assert.equal(pushButtonLabel({ pushed: true, failed: false }), "Update Salesforce");
    assert.equal(pushButtonLabel({ pushed: true, failed: true }), "Try again");
  });
  it("says which mapped fields were not updated, and why, in plain words", () => {
    assert.equal(skippedLine([{ field: "Transcript", reason: "no_access" }, { field: "Visit Date", reason: "no_value" }]),
      "Not updated: Transcript (no access)");
    assert.equal(skippedLine([]), "");
  });
  it("Task targets: Related To = the destination record, Name = the Contact; an override replaces one side", () => {
    assert.deepEqual(taskTargetLines({ meetingRecord: PUSHED, who: { name: "Asha Rao" } }), [
      { label: "Related To", name: "Site Visit · Mumbai Tower A" },
      { label: "Name", name: "Asha Rao" }]);
    assert.deepEqual(taskTargetLines({ meetingRecord: PUSHED, who: { name: "Asha Rao" },
      override: { relation: "what", objectApiName: "Account", name: "Acme" } }), [
      { label: "Related To", name: "Acme" }, { label: "Name", name: "Asha Rao" }]);
    assert.deepEqual(taskTargetLines({ meetingRecord: PUSHED, who: null,
      override: { relation: "who", objectApiName: "Lead", name: "Lead One" } }),
      [{ label: "Name", name: "Lead One" }], "a Lead cannot have a Related To");
  });
  it("tasks wait for the meeting unless the destination record takes Tasks", () => {
    assert.match(pushProblem({ selected: ["t1"], objectApiName: "", recordId: "", meetingPushed: false,
                               assigneeFor: () => "k" }), /Push the meeting to Salesforce first/);
    assert.match(BLOCK, /disabled=\{!tasksReady\}/);
    assert.match(BLOCK, /!!chosenDest\?\.task_supported \|\| !!meetingRecord/);
    assert.match(PUSH, /relation\.what\?\.tasks_supported !== false/);
    assert.match(PUSH, /Each Salesforce Task is attached to/);
  });
});

describe("Integrations: Salesforce is a normal card", () => {
  const INTEGRATIONS = read("src/app/integrations/index.tsx");
  const CONNECT = read("src/app/site-salesforce/connect.tsx");
  it("the package card is retired: Integrations shows the ONE Salesforce card", () => {
    // Salesforce is one connection now (webhook-flow.test.mjs pins its card).
    // The package card is removed from the screen, not hidden behind a flag.
    assert.doesNotMatch(INTEGRATIONS, /function SalesforceCard\(\)|<SalesforceCard \/>/);
    assert.doesNotMatch(INTEGRATIONS, /SiteSalesforceEntry|Salesforce \(Site\)|Install MinuteX in Salesforce/);
    assert.equal((INTEGRATIONS.match(/<WebhookIntegrationCard \/>/g) || []).length, 2, "personal and organisation views");
  });
  it("installing is a few taps; nothing technical on the way", () => {
    for (const src of [code(INTEGRATIONS), code(CONNECT), code(BLOCK)]) {
      for (const banned of ["Site URL", "Apex", "receiver", "api_name", "Org ID", "sf project"]) {
        assert.ok(!src.includes(banned), `must not show "${banned}"`);
      }
    }
    assert.match(CONNECT, /Install MinuteX in Salesforce/);
    assert.match(CONNECT, /✓ MinuteX installed/);
    assert.match(CONNECT, />Connect Salesforce|label="Connect Salesforce"/);
    // Objects and people start syncing server-side when the install connects.
    assert.match(CONNECT, /finishSiteSalesforceInstall\(/);
    assert.match(CONNECT, /can\("manage_integrations"\)/);
  });
});

describe("Object catalog: recommended, added, then search", () => {
  it("groups the short list as Recommended then Added for this workspace", () => {
    const objs = [{ api_name: "Account", label: "Account", recommended: true },
                  { api_name: "Property__c", label: "Property", is_custom: true, enabled: true }];
    assert.deepEqual(visibleObjectSections(objs).map((g) => [g.title, g.data.map((o) => o.label)]),
      [["Recommended Salesforce records", ["Account"]], ["Added for this workspace", ["Property"]]]);
    assert.deepEqual(visibleObjectSections([]), []);
  });
  it("the picker loads the short list and searches the whole catalog, with every state handled", () => {
    assert.match(PICKERS, /FIND ANOTHER SALESFORCE OBJECT/);
    assert.match(PICKERS, /Tap to retry/, "error state with retry");
    assert.match(PICKERS, /No Salesforce objects yet/, "empty state");
    assert.match(PICKERS, /No Salesforce object matches/, "empty search");
    assert.match(PICKERS, /<ActivityIndicator/, "loading state");
    assert.match(PICKERS, /meetings only/, "a meetings-only object says so in the meeting picker");
  });
  it("Settings shows where meetings go, not Salesforce's objects, fields or Task templates", () => {
    for (const gone of ["objects", "fields", "mappings", "mapping-edit"]) {
      assert.ok(!existsSync(join(ROOT, `src/app/site-salesforce/${gone}.tsx`)), `${gone} screen removed`);
    }
    const ui = code(INDEX);
    assert.doesNotMatch(ui, /Salesforce objects|Task templates|site-salesforce\/(objects|mappings)/);
    assert.match(ui, /<SectionRule>Where meetings go<\/SectionRule>/);
    // Refreshing (new fields, picklist values) stays, for admins, under Advanced.
    const advancedAt = ui.indexOf("{advanced ? (");
    assert.ok(ui.indexOf('"Refresh from Salesforce"') > advancedAt && advancedAt > 0);
  });
});

describe("Install MinuteX from the app", () => {
  const CONNECT = read("src/app/site-salesforce/connect.tsx");
  const CARD = read("src/app/integrations/index.tsx");
  it("Install -> Salesforce -> back -> ✓ installed -> Connect", () => {
    assert.match(CONNECT, /startSiteSalesforceInstall\(activeId, sandbox\)/);
    assert.match(CONNECT, /WebBrowser\.openBrowserAsync\(url\)/, "the install page opens in the browser");
    assert.match(CONNECT, /Your setup code/);
    assert.match(CONNECT, /Waiting for Salesforce/);
    assert.match(CONNECT, /status === "installed"/);
    assert.match(CONNECT, /finishSiteSalesforceInstall\(activeId, allowOrgChange\)/);
    assert.match(CONNECT, /router\.replace\("\/site-salesforce"/, "then Manage: people, destinations");
  });
  it("re-checks on return, while waiting, and handles cancel, failure, expiry and the wrong org", () => {
    assert.match(CONNECT, /AppState\.addEventListener\("change"/, "back from Salesforce -> check again");
    assert.match(CONNECT, /usePolling\(install\?\.status === "waiting"/);
    assert.match(CONNECT, /Open Salesforce again/, "cancelled or not finished");
    assert.match(CONNECT, /Installation didn&apos;t finish\?/, "failed installation");
    assert.match(CONNECT, /Your last setup code expired/, "expired");
    assert.match(CONNECT, /e\.code === "org_mismatch"/, "wrong Salesforce org");
    assert.match(CONNECT, /Use a different Salesforce org\?/);
    assert.match(CONNECT, /My Salesforce org is a sandbox/);
  });
  it("the Integrations screen no longer leads into the package install", () => {
    // The install screens remain in the codebase (retired, unreachable from
    // Integrations) until they are deleted with the rest of the package path.
    assert.doesNotMatch(CARD, /site-salesforce\/connect|Install MinuteX in Salesforce|Continue setup/);
  });
  it("disconnecting is a visible button, not hidden under Advanced", () => {
    const disconnectAt = INDEX.indexOf('label="Disconnect Salesforce"');
    const advancedAt = INDEX.indexOf("{advanced ? (");
    assert.ok(disconnectAt > 0 && disconnectAt < advancedAt, "Disconnect is before (outside) Advanced");
  });
  it("friendly wording for every install outcome", () => {
    for (const c of ["install_unavailable", "install_not_finished", "install_expired", "invalid_setup_code"]) {
      assert.ok(SITE_ERROR_MESSAGES[c], c);
    }
  });
});

describe("Invalidation bus", () => {
  it("notifies only subscribers of the changed kinds and bumps versions", () => {
    const seen = [];
    const off = subscribeSiteSalesforce(["people"], () => seen.push("people"));
    const before = siteSalesforceVersion("people");
    invalidateSiteSalesforce("objects");
    assert.deepEqual(seen, []);
    invalidateSiteSalesforce("people", "relation");
    assert.deepEqual(seen, ["people"]);
    assert.equal(siteSalesforceVersion("people"), before + 1);
    off();
    invalidateSiteSalesforce("people");
    assert.deepEqual(seen, ["people"], "unsubscribed");
  });
  it("announces a run once, when it goes from running to SYNCED", () => {
    assert.deepEqual(completedRuns({ people: "SYNCING" }, { people: "SYNCED" }), ["people"]);
    assert.deepEqual(completedRuns({ discovery: "PENDING" }, { discovery: "SYNCED" }), ["objects"]);
    assert.deepEqual(completedRuns({ people: "SYNCED" }, { people: "SYNCED" }), [], "not on every poll");
    assert.deepEqual(completedRuns({ people: "SYNCING" }, { people: "FAILED" }), []);
  });
});

describe("The old OAuth Salesforce integration is gone", () => {
  it("its screens and client libraries no longer exist", () => {
    for (const f of ["src/app/salesforce.tsx", "src/app/org-salesforce.tsx",
                     "src/app/salesforce-config.tsx", "src/app/org-salesforce-config.tsx",
                     "src/app/crm-connected.tsx", "lib/salesforce.ts",
                     "lib/salesforce-install.tsx", "lib/meeting-crm-records.tsx"]) {
      assert.equal(existsSync(join(ROOT, f)), false, f);
    }
  });

  it("the API client has no OAuth Salesforce calls left", () => {
    assert.doesNotMatch(API, /\/crm\/salesforce|getOrgSalesforceStatus|getSalesforceAuthorizeUrl|SALESFORCE_RECONNECT_CODE|crm_records/);
  });

  it("the Site flow never calls the old OAuth Salesforce API", () => {
    for (const f of SITE_FILES) {
      assert.doesNotMatch(code(read(f)), /getOrgSalesforceStatus|lib\/salesforce"|from "\.\/salesforce"|crm\/salesforce/, f);
    }
  });
});

describe("The connected org's name", () => {
  it("never reads as 'Connected to: None'", () => {
    assert.equal(orgDisplayName("Acme Realty"), "Acme Realty");
    for (const empty of ["None", "none", "null", "", "  ", undefined, null]) {
      assert.equal(orgDisplayName(empty), "", String(empty));
    }
    for (const src of [INDEX, read("src/app/integrations/index.tsx"), read("src/app/site-salesforce/connect.tsx")]) {
      assert.doesNotMatch(code(src), /\{\s*(conn|install)\??\.org_name\s*(\|\||\})/, "raw org_name is never rendered");
    }
    assert.match(INDEX, /"Connected to Salesforce"/);
  });
});

describe("One Salesforce record per meeting (destination write mode)", () => {
  const src = code(BLOCK);
  it("choosing a per-meeting destination saves it at once: no record to pick", () => {
    assert.match(src, /d\.write_mode === "meeting_record"\) \{ void chooseRecord\(d, null\)/);
    assert.match(src, /: \{ destination_id: dest\.destination_id \}\)/, "no record id is sent");
    assert.match(BLOCK, /Salesforce keeps one \{destForRecord\.name\} for this meeting and updates it each time you push\./);
    assert.match(src, /destForRecord\.write_mode !== "meeting_record" \? \(/, "the record list never opens for it");
  });
  it("after the push it counts as synced to the record Salesforce created", () => {
    assert.match(src, /\(perMeeting \|\| placed\.record_name === chosen\?\.record_name\)/);
  });
  it("the API allows a destination without a record", () => {
    assert.match(read("lib/api.ts"), /change: \{ destination_id: string; record_id\?: string; record_name\?: string \}/);
    assert.match(read("lib/api.ts"), /write_mode\?: "update_record" \| "meeting_record";/);
  });
});

describe("live Salesforce people search in the picker", () => {
  it("labels Leads as well as Contacts and Users", () => {
    assert.equal(salesforceTypeTag("lead"), "SF LEAD");
    assert.equal(salesforceTypeTag("user"), "SF USER");
    assert.equal(salesforceTypeTag("contact"), "SF CONTACT");
    assert.equal(salesforceBadge({ people_provider: "salesforce", people_type: "lead" }), "Salesforce Lead");
  });

  it("searches live only from 2 characters", () => {
    assert.equal(shouldSearchLive(""), false);
    assert.equal(shouldSearchLive(" r "), false);
    assert.equal(shouldSearchLive("ra"), true);
  });

  it("puts live results first and never shows a person twice", () => {
    const live = [{ person_key: "a" }, { person_key: "b" }];
    const stored = [{ person_key: "b" }, { person_key: "c" }];
    assert.deepEqual(mergePeople(live, stored).map((p) => p.person_key), ["a", "b", "c"]);
  });

  it("the picker searches live, falls back to stored people, and saves only the picked person", () => {
    const picker = read("lib/contact-picker.tsx");
    assert.match(picker, /shouldSearchLive\(debounced\)\s*\?\s*searchLivePeople\(/);
    assert.match(picker, /searchLivePeople\([^)]*\)\.catch\(\(\) => \[\]/);
    assert.match(picker, /livePeopleKeys\.has\(p\.person_key\)/);
    assert.match(picker, /fromSalesforceNow \? p : undefined/);
    assert.doesNotMatch(picker, /"SF CONTACT"/);
  });
});

describe("people on the meeting's linked Salesforce record", () => {
  it("are suggested right after this meeting's people, only while browsing", () => {
    const picker = read("lib/contact-picker.tsx");
    assert.match(picker, /getRelatedPeople\(meetingKey\)[\s\S]{0,120}\.catch\(\(\) => \{ if \(alive\) setRelatedPeople\(\[\]\)/);
    assert.match(picker, /const relatedTier: Section\[\] = needle \? \[\] :/);
    const order = ["In this meeting", "On this meeting's Salesforce record", "Your organisation"];
    const at = order.map((label) => picker.indexOf(`"${label}"`));
    assert.ok(at.every((i) => i > 0) && at[0] < at[1] && at[1] < at[2], at.join(","));
    // A suggested person may not be saved yet, so the pick sends them.
    assert.match(picker, /relatedPeople\.some\(\(r\) => r\.person_key === p\.person_key\)/);
  });

  it("both speaker pickers pass the meeting", () => {
    assert.match(read("lib/speaker-tagging.tsx"), /meetingKey=\{tagging\.recordingKey\}/);
    assert.match(read("src/app/recording/[key]/participants.tsx"), /meetingKey=\{recordingKey\}/);
  });
});
