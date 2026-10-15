// lib/__tests__/salesforce-people.test.mjs — Salesforce people in the app:
// badges, tags, live search merged with the People Store, the
// "this changed" signals, and the pickers that use them.
//
// Run:  node --test lib/__tests__/salesforce-people.test.mjs
//
// TWO LEVELS, like push-logic.test.mjs:
//   1. LOGIC — lib/salesforce-people.ts and lib/salesforce-events.ts are
//      imported directly (pure TS, no React Native).
//   2. SOURCE-LEVEL — the pickers and Contacts screen are TSX with no
//      renderer here, so their wiring is checked in the source.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import {
  mergePeople, peopleTabLabel, salesforceBadge, salesforceTypeTag, shouldSearchLive,
} from "../salesforce-people.ts";
import {
  invalidateSalesforce, salesforceVersion, subscribeSalesforce,
} from "../salesforce-events.ts";

const ROOT = new URL("../../", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const read = (rel) => readFileSync(join(ROOT, rel), "utf8");

const CONTACTS = read("src/app/contacts.tsx");
const CONTACT_PICKER = read("lib/contact-picker.tsx");
const TAGGING = read("lib/speaker-tagging.tsx");
const PARTICIPANTS = read("src/app/recording/[key]/participants.tsx");
const MEETING = read("src/app/recording/[key]/index.tsx");

describe("People: Contacts and Users, with counts, never stale", () => {
  it("labels people tabs with counts once known", () => {
    assert.equal(peopleTabLabel("contact", { contacts: 23, users: 4 }), "Contacts (23)");
    assert.equal(peopleTabLabel("user", { contacts: 23, users: 4 }), "Users (4)");
    assert.equal(peopleTabLabel("user", null), "Users");
  });
  it("badges contacts linked to Salesforce", () => {
    assert.equal(salesforceBadge({ people_provider: "salesforce", people_type: "contact" }), "Salesforce Contact");
    assert.equal(salesforceBadge({ people_provider: "salesforce", people_type: "user" }), "Salesforce User");
    assert.equal(salesforceBadge({ people_provider: "salesforce", people_type: "lead" }), "Salesforce Lead");
    assert.equal(salesforceBadge({}), "");
  });
  it("Contacts shows a Salesforce segment with Contacts/Users from the People Store", () => {
    assert.match(CONTACTS, /searchSitePeoplePage\(activeId, \{ q: debounced, type: sfType/);
    assert.match(CONTACTS, /peopleTabLabel\("contact", sfCounts\)/);
    assert.match(CONTACTS, /peopleTabLabel\("user", sfCounts\)/);
    assert.match(CONTACTS, /useSalesforceVersion\("people"/, "reloads when people change");
  });
  it("every choose-a-person sheet offers Salesforce people and resolves them to a contact", () => {
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
  it("tagging refreshes the meeting's Salesforce relationship", () => {
    // Every tag write (speaker, attendee, clear) lives in the shared module.
    assert.equal((TAGGING.match(/invalidateSalesforce\("relation"\)/g) || []).length, 3);
  });
});

describe("Invalidation bus", () => {
  it("notifies only subscribers of the changed kinds and bumps versions", () => {
    const seen = [];
    const off = subscribeSalesforce(["people"], () => seen.push("people"));
    const before = salesforceVersion("people");
    invalidateSalesforce("connection");
    assert.deepEqual(seen, []);
    invalidateSalesforce("people", "relation");
    assert.deepEqual(seen, ["people"]);
    assert.equal(salesforceVersion("people"), before + 1);
    off();
    invalidateSalesforce("people");
    assert.deepEqual(seen, ["people"], "unsubscribed");
  });
});

describe("live Salesforce people search in the picker", () => {
  it("labels Leads as well as Contacts and Users", () => {
    assert.equal(salesforceTypeTag("lead"), "SF LEAD");
    assert.equal(salesforceTypeTag("user"), "SF USER");
    assert.equal(salesforceTypeTag("contact"), "SF CONTACT");
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
    assert.match(CONTACT_PICKER, /shouldSearchLive\(debounced\)\s*\?\s*searchLivePeople\(/);
    assert.match(CONTACT_PICKER, /searchLivePeople\([^)]*\)\.catch\(\(\) => \[\]/);
    assert.match(CONTACT_PICKER, /livePeopleKeys\.has\(p\.person_key\)/);
    assert.match(CONTACT_PICKER, /fromSalesforceNow \? p : undefined/);
    assert.doesNotMatch(CONTACT_PICKER, /"SF CONTACT"/);
  });
});

describe("people on the meeting's linked Salesforce record", () => {
  it("are suggested right after this meeting's people, only while browsing", () => {
    assert.match(CONTACT_PICKER, /getRelatedPeople\(meetingKey\)[\s\S]{0,120}\.catch\(\(\) => \{ if \(alive\) setRelatedPeople\(\[\]\)/);
    assert.match(CONTACT_PICKER, /const relatedTier: Section\[\] = needle \? \[\] :/);
    const order = ["In this meeting", "On this meeting's Salesforce record", "Your organisation"];
    const at = order.map((label) => CONTACT_PICKER.indexOf(`"${label}"`));
    assert.ok(at.every((i) => i > 0) && at[0] < at[1] && at[1] < at[2], at.join(","));
    // A suggested person may not be saved yet, so the pick sends them.
    assert.match(CONTACT_PICKER, /relatedPeople\.some\(\(r\) => r\.person_key === p\.person_key\)/);
  });

  it("both speaker pickers pass the meeting", () => {
    assert.match(TAGGING, /meetingKey=\{tagging\.recordingKey\}/);
    assert.match(PARTICIPANTS, /meetingKey=\{recordingKey\}/);
  });
});

describe("The retired Salesforce Site path is gone", () => {
  it("its screens and libraries no longer exist", () => {
    for (const f of ["src/app/site-salesforce/index.tsx", "src/app/site-salesforce/connect.tsx",
                     "src/app/site-salesforce/push-tasks.tsx", "lib/site-salesforce-meeting.tsx",
                     "lib/site-salesforce-ui.tsx", "lib/site-salesforce-flow.ts",
                     "lib/site-salesforce-pickers.tsx", "lib/site-salesforce-events.ts"]) {
      assert.equal(existsSync(join(ROOT, f)), false, f);
    }
  });

  it("the API client calls no Site route", () => {
    assert.doesNotMatch(read("lib/api.ts"), /\/site-salesforce|\/site-delivery/);
  });
});
