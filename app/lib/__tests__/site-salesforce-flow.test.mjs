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
  RECEIVER_PATH, normalizeSiteUrl, displaySiteUrl, siteErrorMessage, siteStage, STAGE_LABELS,
  healthLine, peopleSummary, filterObjects, taskRelatable, isIsoDate, meetingPushLine, isInFlight,
  pushedRowFor, salesforceStateOf, defaultPushSelection, suggestSalesforceUser, sharedRelationship,
  pushProblem, salesforceDue, objectGroups, relationKindLabel, relationSummary, relationLines,
  hasRelation, relationSourceNote, salesforceBadge, peopleTabLabel, taskRelationOf, objectUseLabel,
  meetingRecordLine, taskTargetLines, visibleObjectSections,
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
    assert.equal(bad.detail, "Salesforce Site is unavailable. Check the Site URL and try again.");
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
      assert.equal(siteErrorMessage(code), "Salesforce Site is unavailable. Check the Site URL and try again.");
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
const API = read("lib/api.ts");
const SITE_API = API.slice(API.indexOf("// Salesforce Site — org connection (NEW architecture)"));
const SITE_FILES = [
  "src/app/site-salesforce/index.tsx", "src/app/site-salesforce/push-tasks.tsx",
  "src/app/site-salesforce/objects.tsx", "src/app/site-salesforce/fields.tsx",
  "src/app/site-salesforce/mappings.tsx", "src/app/site-salesforce/mapping-edit.tsx",
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
  it("renders the Site block only when SITE_SALESFORCE_ENABLED", () => {
    const uses = MEETING.match(/<SiteSalesforceMeetingBlock/g) || [];
    assert.equal(uses.length, 1);
    assert.match(MEETING, /\{SITE_SALESFORCE_ENABLED && rec\?\.workspace_id[\s\S]{0,200}<SiteSalesforceMeetingBlock/);
  });

  it("carries no trace of the removed OAuth CRM blocks", () => {
    assert.doesNotMatch(MEETING, /OrgCrmReviewBlock|CrmRecordsBlock|meeting-crm-records/);
  });
});

describe("No Salesforce credentials in the Site flow", () => {
  it("no client id/secret, token, OAuth or External Client App UI", () => {
    const banned = /client_secret|client_id|consumer key|consumer secret|External Client App|access_token|refresh_token|shared_secret|WebBrowser|openAuthSession/i;
    for (const f of SITE_FILES) assert.doesNotMatch(code(read(f)), banned, f);
    assert.doesNotMatch(code(SITE_API), /shared_secret|client_secret|client_id/, "the Site API section");
  });

  it("connect saves only the Site URL and auth mode, then pins the org", () => {
    assert.match(INDEX, /saveSiteDelivery\(activeId, \{\s*url, auth_mode: endpoint\?\.auth_mode \|\| "none", enabled: true,/);
    assert.match(INDEX, /connectSiteSalesforce\(activeId, \{ allow_org_change: allowOrgChange \}\)/);
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
    assert.match(BLOCK, /disabled=\{busy \|\| pushing \|\| !endpointEnabled\}/);
  });
});

describe("Loading and empty states", () => {
  it("each list says what to do when it is empty", () => {
    assert.match(PUSH, /No tasks in this meeting/);
    assert.match(read("src/app/site-salesforce/objects.tsx"), /Discover Salesforce Objects/);
    const pickers = read("lib/site-salesforce-pickers.tsx");
    assert.match(pickers, /No synced Salesforce Users match/);
    assert.match(pickers, /records match/);
    assert.match(pickers, /Discover Salesforce Objects first/);
  });

  it("the meeting block shows nothing while loading and a retry when loading fails", () => {
    assert.match(BLOCK, /if \(!loaded\) return null;/);
    assert.match(BLOCK, /Could not load the Salesforce status\./);
  });
});

// ===========================================================================
// Phase 2: People, filtered objects, the meeting relationship, inheritance
// ===========================================================================
const PICKERS = read("lib/site-salesforce-pickers.tsx");
const OBJECTS_SCREEN = read("src/app/site-salesforce/objects.tsx");
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
    for (const [name, src] of [["pickers", PICKERS], ["objects", OBJECTS_SCREEN], ["block", BLOCK],
                               ["push", PUSH], ["fields", read("src/app/site-salesforce/fields.tsx")]]) {
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
  it("the block shows the link first and hides the pickers behind Link to a Salesforce record", () => {
    assert.match(BLOCK, /Linked to: \$\{summary\}/);
    assert.match(BLOCK, /always creates a new MinuteX Meeting record/, "says what the picker is for");
    const before = BLOCK.slice(0, BLOCK.indexOf("Link to a Salesforce record"));
    assert.ok(before.length > 0, "the Change link exists");
    const ui = code(BLOCK);
    const pickerAt = ui.indexOf("<ObjectPicker");
    const changingAt = ui.indexOf("!changing ?");
    assert.ok(changingAt > 0 && pickerAt > changingAt, "ObjectPicker only in the changing branch");
    assert.match(BLOCK, /setSiteMeetingRelation\(workspaceId, meetingKey, change\)/);
    assert.match(BLOCK, /reset: "who"/);
    assert.match(BLOCK, /reset: "what"/);
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
    assert.match(CONTACT_PICKER, /contactForSitePerson\(sfWorkspace, p\.person_key\)/);
    assert.match(CONTACT_PICKER, /onPick\(contact\)/);
    assert.match(PARTICIPANTS, /salesforceWorkspaceId=\{rec\?\.workspace_id\}/);
    assert.match(MEETING, /salesforceWorkspaceId=\{rec\?\.workspace_id\}/);
  });
  it("a finished sync/connect/disconnect is broadcast, and tagging refreshes the relationship", () => {
    assert.match(INDEX, /completedRuns\(lastRun\.current, next\)/);
    assert.match(INDEX, /invalidateSiteSalesforce\("connection", "objects", "relation"\)/);
    assert.match(INDEX, /invalidateSiteSalesforce\("connection", "objects", "people", "relation"\)/);
    assert.match(PARTICIPANTS, /invalidateSiteSalesforce\("relation"\)/);
    assert.match(MEETING, /invalidateSiteSalesforce\("relation"\)/);
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
    assert.match(BLOCK, /<ObjectPicker workspaceId=\{workspaceId\} value=\{obj\} onChange=\{setObj\} capability="meeting" \/>/);
    assert.match(PUSH, /<ObjectPicker workspaceId=\{ws\} value=\{obj\} capability="task"/);
    assert.match(PICKERS, /scope: "visible", capability/);
    assert.match(PICKERS, /scope: "all", capability, q: term/);
  });
});

describe("Tasks are attached to the pushed meeting record", () => {
  const REC = { object_label: "MinuteX Meeting", name: "Client Discussion" };
  it("names where the meeting is in Salesforce", () => {
    assert.equal(meetingRecordLine(REC), "MinuteX Meeting · Client Discussion");
    assert.equal(meetingRecordLine(null), "");
  });
  it("Related To = the meeting record, Name = the Contact; an override replaces one side", () => {
    assert.deepEqual(taskTargetLines({ meetingRecord: REC, who: { name: "Asha Rao" } }), [
      { label: "Related To", name: "MinuteX Meeting · Client Discussion" },
      { label: "Name", name: "Asha Rao" }]);
    assert.deepEqual(taskTargetLines({ meetingRecord: REC, who: { name: "Asha Rao" },
      override: { relation: "what", objectApiName: "Account", name: "Acme" } }), [
      { label: "Related To", name: "Acme" }, { label: "Name", name: "Asha Rao" }]);
    assert.deepEqual(taskTargetLines({ meetingRecord: REC, who: null,
      override: { relation: "who", objectApiName: "Lead", name: "Lead One" } }),
      [{ label: "Name", name: "Lead One" }], "a Lead cannot have a Related To");
  });
  it("nothing can be pushed until the meeting is", () => {
    assert.match(pushProblem({ selected: ["t1"], objectApiName: "", recordId: "", meetingPushed: false,
                               assigneeFor: () => "k" }), /Push the meeting to Salesforce first/);
  });
  it("the card shows where the meeting went and gates Push tasks on it", () => {
    assert.match(BLOCK, /IN SALESFORCE/);
    assert.match(BLOCK, /meetingRecordLine\(meetingRecord\)/);
    assert.match(BLOCK, /Open in Salesforce/);
    assert.match(BLOCK, /disabled=\{!conn\?\.connected \|\| !conn\.task_permissions\?\.createable \|\| !meetingRecord\}/);
    assert.match(BLOCK, /Push the meeting first/);
  });
  it("the push screen blocks without the meeting record and shows the target", () => {
    assert.match(PUSH, /relation\?\.meeting_record/);
    assert.match(PUSH, /Push the meeting first/);
    assert.match(PUSH, /Each Salesforce Task is attached to/);
    assert.match(PUSH, /meetingPushed: !!meetingRecord/);
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
  it("the objects screen adds from search and removes (managers) — persisted for the workspace", () => {
    assert.match(OBJECTS_SCREEN, /setSiteSalesforceObjectPrefs\(activeId, add \? \{ enable: \[o\.api_name\] \} : \{ disable: \[o\.api_name\] \}\)/);
    assert.match(OBJECTS_SCREEN, /canRemove = can\("manage_integrations"\)/);
    assert.match(OBJECTS_SCREEN, /scope: "all", q: term/);
    assert.match(OBJECTS_SCREEN, /invalidateSiteSalesforce\("objects"\)/);
    assert.match(OBJECTS_SCREEN, /label="Meetings"/);
    assert.match(OBJECTS_SCREEN, /label="Tasks"/);
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
