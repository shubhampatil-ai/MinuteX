// lib/__tests__/salesforce-link-flow.test.mjs — linking a meeting to
// Salesforce records (Contract v3): the pure UI logic.
//
// Run:  node --test lib/__tests__/salesforce-link-flow.test.mjs
//
// Pins: up to 5 records with exactly one primary (only objects that may be
// the main record), outputs offered per object with defaults from the
// primary, admin-locked outputs, "Property only" notes, what each record
// actually receives, task targets following the primary, the review
// lines, and the payload shape contract_v3.py expects.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import {
  LINK_OUTPUTS, MAX_LINKS, maxLinks, singleRecord, linkableObjectSummary, addLink, initialOutputs, linkProblem, normalizeOutputs,
  outputOptions, outputsForRecord, primaryOf, pruneTaskTargets, removeLink, reviewLines,
  sameRecord, setPrimary, taskTargetLabel, toLinkPayload, toggleOutput,
  OUTPUT_LABELS, documentNames, initialDocuments, sendableDocuments, sendableOutputs, toggleDocument,
} from "../salesforce-link-flow.ts";

const ALL = [...LINK_OUTPUTS];
// These tests describe the multi-record rules, so they set a limit of 5
// themselves; the real limit is the backend's (see the last test).
const CONFIG = {
  max_links: 5,
  objects: [
    { object: "Property__c", label: "Property", can_be_primary: true,
      outputs_allowed: ALL, outputs_default_on: ["overview", "mom", "tasks", "participants", "recording"],
      outputs_user_can_change: ALL },
    { object: "Lead", label: "Lead", can_be_primary: true,
      outputs_allowed: ["overview", "mom", "tasks", "participants", "recording"],
      outputs_default_on: ["overview", "mom", "tasks", "participants"],
      outputs_user_can_change: ["overview", "mom", "participants", "recording"] },
    { object: "Account", label: "Account", can_be_primary: false,
      outputs_allowed: ["overview", "participants"], outputs_default_on: ["overview"],
      outputs_user_can_change: ["overview", "participants"] },
  ],
};
const PROPERTY = { object: "Property__c", id: "a0X5g00000AbCdEAAZ", name: "Mumbai Heights" };
const LEAD = { object: "Lead", id: "00Q5g00000XyZabEAF", name: "Rahul Sharma" };
const ACCOUNT = { object: "Account", id: "0015g00000Acc0001A", name: "Sharma Family" };

function linked(...recs) {
  return recs.reduce((links, r) => addLink(links, r, CONFIG).links, []);
}
const opt = (options, output) => options.find((o) => o.output === output);

describe("records", () => {
  it("the first record that may be the main one becomes primary", () => {
    assert.equal(primaryOf(linked(PROPERTY, LEAD)).name, "Mumbai Heights");
    // An Account can't be the main record here, so the Lead after it is.
    assert.equal(primaryOf(linked(ACCOUNT, LEAD)).name, "Rahul Sharma");
  });

  it("refuses duplicates (15 vs 18 characters), unknown objects and a sixth record", () => {
    const links = linked(PROPERTY);
    assert.match(addLink(links, { ...PROPERTY, id: PROPERTY.id.slice(0, 15) }, CONFIG).error, /already linked/);
    assert.match(addLink(links, { object: "Case", id: "5005g00000Cas0001A", name: "X" }, CONFIG).error,
      /can't be linked/);
    const max = maxLinks(CONFIG);
    let many = [];
    for (let i = 0; i < max; i++) {
      many = addLink(many, { object: "Lead", id: `00Q5g00000XyZa${i}EAF`, name: `L${i}` }, CONFIG).links;
    }
    assert.equal(many.length, max);
    assert.match(addLink(many, LEAD, CONFIG).error, new RegExp(`at most ${max} record`));
    assert.ok(sameRecord("a0X5g00000AbCdE", "a0X5g00000AbCdEAAZ"));
  });

  it("setPrimary only for objects that may be the main record; removing the primary moves it", () => {
    let links = linked(PROPERTY, LEAD, ACCOUNT);
    links = setPrimary(links, LEAD.id, CONFIG);
    assert.equal(primaryOf(links).name, "Rahul Sharma");
    assert.equal(primaryOf(setPrimary(links, ACCOUNT.id, CONFIG)).name, "Rahul Sharma");
    links = removeLink(links, LEAD.id, CONFIG);
    assert.equal(primaryOf(links).name, "Mumbai Heights");
    assert.equal(links.filter((l) => l.primary).length, 1);
  });

  it("says why Link can't be pressed", () => {
    assert.match(linkProblem([], CONFIG), /at least one/);
    assert.equal(linkProblem(linked(PROPERTY), CONFIG), null);
    assert.match(linkProblem(linked(ACCOUNT), CONFIG), /main record/);
    assert.match(linkProblem(linked(PROPERTY), { objects: [] }), /can't be linked any more/);
  });
});

describe("outputs", () => {
  it("offers what any linked object allows, with defaults from the primary", () => {
    const options = outputOptions(linked(PROPERTY, LEAD), CONFIG);
    assert.equal(opt(options, "participant_contributions").offered, true);
    assert.equal(opt(options, "participant_contributions").note, "Property only");
    assert.equal(opt(options, "overview").note, "");
    assert.equal(opt(options, "recording").defaultOn, true);       // the Property's default
    assert.deepEqual(initialOutputs(options), ["overview", "mom", "tasks", "participants", "recording"]);
  });

  it("defaults follow the new primary", () => {
    const links = setPrimary(linked(PROPERTY, LEAD), LEAD.id, CONFIG);
    assert.equal(opt(outputOptions(links, CONFIG), "recording").defaultOn, false);  // the Lead's default
  });

  it("an output nobody allows isn't offered; one nobody may change is locked", () => {
    const options = outputOptions(linked(LEAD), CONFIG);
    assert.equal(opt(options, "transcript").offered, false);
    assert.equal(opt(options, "tasks").locked, true);
    assert.deepEqual(toggleOutput(["tasks"], "tasks", options), ["tasks"]);
    assert.deepEqual(toggleOutput([], "transcript", options), []);
  });

  it("what each person said needs the participants", () => {
    const options = outputOptions(linked(PROPERTY), CONFIG);
    assert.deepEqual(toggleOutput([], "participant_contributions", options),
      ["participants", "participant_contributions"]);
    assert.deepEqual(toggleOutput(["participants", "participant_contributions"], "participants", options), []);
    assert.deepEqual(normalizeOutputs(["participant_contributions"]), []);
  });

  it("what the meeting doesn't have yet can't be ticked, and says how to make it", () => {
    const has = { mom: false, documents: [] };
    const options = outputOptions(linked(PROPERTY), CONFIG, has);
    assert.equal(opt(options, "mom").available, false);
    assert.match(opt(options, "mom").missing, /Generate the MoM/);
    assert.match(opt(options, "documents").missing, /Create one/);
    assert.equal(opt(options, "overview").available, true);
    assert.ok(!initialOutputs(options).includes("mom"), "not ticked by default");
    assert.deepEqual(toggleOutput([], "mom", options), []);
    assert.deepEqual(sendableOutputs(["overview", "mom"], options), ["overview"]);
    // Not known (the load failed): nothing is held back, as before.
    assert.equal(opt(outputOptions(linked(PROPERTY), CONFIG, null), "mom").available, true);
  });

  it("documents go one by one, under their own names", () => {
    const docs = [{ type: "follow_up_email", label: "Follow-up Email" },
                  { type: "executive_summary", label: "Executive Summary" }];
    assert.deepEqual(sendableDocuments({
      minutes_of_meeting: { label: "MoM", content: "x" }, empty: { label: "E", content: " " },
      follow_up_email: { label: "Follow-up Email", content: "Hi" },
      executive_summary: { label: "Executive Summary", content: "#" },
    }), [docs[1], docs[0]]);
    const options = outputOptions(linked(PROPERTY), CONFIG, { mom: true, documents: docs });
    assert.deepEqual(initialDocuments(docs, null, ["documents"]), ["follow_up_email", "executive_summary"]);
    assert.deepEqual(initialDocuments(docs, ["executive_summary", "gone"], ["documents"]), ["executive_summary"]);
    assert.deepEqual(initialDocuments(docs, null, ["overview"]), []);
    let s = toggleDocument(["overview"], [], "executive_summary", options);
    assert.deepEqual(s, { outputs: ["overview", "documents"], documents: ["executive_summary"] });
    s = toggleDocument(s.outputs, s.documents, "executive_summary", options);
    assert.deepEqual(s, { outputs: ["overview"], documents: [] });
    assert.deepEqual(documentNames(docs, ["executive_summary"]), ["Executive Summary"]);
    assert.doesNotMatch(Object.values(OUTPUT_LABELS).join(" "), /generated/i);
    const r = reviewLines({ links: linked(PROPERTY), selected: ["documents"], config: CONFIG,
                            taskCount: 0, participantCount: 0,
                            documentNames: ["Executive Summary"] });
    assert.equal(r.summary, "1 meeting · Executive Summary");
    assert.equal(r.perRecord[0].outputs, "Executive Summary (PDF)");
    assert.deepEqual(toLinkPayload(linked(PROPERTY), ["documents"], {}, ["executive_summary"])
      .included_documents, ["executive_summary"]);
    assert.equal("included_documents" in toLinkPayload(linked(PROPERTY), ["documents"], {}), false);
  });

  it("each record receives only what its own object allows", () => {
    const [prop, lead] = linked(PROPERTY, LEAD);
    const chosen = ["overview", "participants", "participant_contributions", "recording"];
    assert.deepEqual(outputsForRecord(prop, chosen, CONFIG), chosen);
    assert.deepEqual(outputsForRecord(lead, chosen, CONFIG), ["overview", "participants", "recording"]);
  });
});

describe("tasks, review and payload", () => {
  it("tasks follow the main record unless pointed elsewhere", () => {
    const links = linked(PROPERTY, LEAD);
    assert.deepEqual(taskTargetLabel(null, links), { text: "Mumbai Heights (main record)", followsPrimary: true });
    assert.deepEqual(taskTargetLabel({ ...LEAD }, links), { text: "Rahul Sharma", followsPrimary: false });
    const pruned = pruneTaskTargets({ t1: { ...LEAD }, t2: null }, LEAD.id);
    assert.deepEqual(pruned, { t1: null, t2: null });
  });

  it("review lines say what will be added and what each record gets", () => {
    const links = linked(PROPERTY, LEAD);
    const r = reviewLines({ links, selected: ["overview", "mom", "tasks", "participants"],
                            config: CONFIG, taskCount: 3, participantCount: 1 });
    assert.equal(r.summary, "1 meeting · 3 tasks · MoM PDF · 1 participant");
    assert.deepEqual(r.perRecord.map((p) => p.name), ["Mumbai Heights (main)", "Rahul Sharma"]);
    assert.equal(r.perRecord[1].label, "Lead");
    const none = reviewLines({ links: linked(ACCOUNT), selected: ["mom"], config: CONFIG,
                               taskCount: 0, participantCount: 0 });
    assert.equal(none.perRecord[0].outputs, "Meeting details only");
  });

  it("the payload matches contract_v3", () => {
    const links = linked(PROPERTY, LEAD);
    const payload = toLinkPayload(links, ["tasks", "overview", "participant_contributions"],
                                  { tsk_1: null, tsk_2: { ...LEAD } });
    assert.deepEqual(payload.links.map((l) => [l.object, l.primary]), [["Property__c", true], ["Lead", false]]);
    assert.deepEqual(payload.included_outputs, ["overview", "tasks"]);
    assert.deepEqual(payload.task_relations, { tsk_2: { object: "Lead", id: LEAD.id } });
  });

  it("the limit comes from the backend; a limit of one means pick-to-replace", () => {
    assert.equal(maxLinks({ objects: [] }), MAX_LINKS);              // not loaded yet
    assert.equal(maxLinks({ ...CONFIG, max_links: 3 }), 3);
    const one = { ...CONFIG, max_links: 1 };
    assert.ok(singleRecord(one));
    const first = addLink([], PROPERTY, one).links;
    const replaced = addLink(first, LEAD, one);
    assert.equal(replaced.error, undefined);
    assert.deepEqual(replaced.links.map((l) => [l.name, l.primary]), [["Rahul Sharma", true]]);
    assert.match(linkProblem([...first, { ...LEAD, primary: false }], one), /at most 1 record\./);
  });

  it("the output names match the backend contract", async () => {
    const { readFileSync } = await import("node:fs");
    const py = readFileSync(new URL("../../../cloud/shared/contract_v3.py", import.meta.url), "utf8");
    for (const o of LINK_OUTPUTS) assert.match(py, new RegExp(`"${o}"`), o);
    // The backend owns the limit and hands it to the app; the app keeps no
    // copy of the number, only a strict fallback.
    assert.match(py, /^MAX_LINKS = \d+$/m);
    assert.match(py, /"max_links": MAX_LINKS/);
    const src = readFileSync(new URL("../salesforce-link-flow.ts", import.meta.url), "utf8");
    assert.equal((src.match(/MAX_LINKS\b/g) || []).length, 3, "MAX_LINKS is only the fallback");
  });
});

describe("settings screen: records users can link to", () => {
  it("describes each object in plain words, in output order", () => {
    const s = linkableObjectSummary({
      label: "Property", can_be_primary: true,
      outputs_allowed: ["recording", "overview", "transcript", "mom"],
      outputs_default_on: ["mom", "overview"] });
    assert.equal(s.role, "Can be the main record");
    assert.equal(s.sends, "Overview & highlights, MoM (PDF), Transcript, Recording link");
    assert.equal(s.onByDefault, "Overview & highlights, MoM (PDF)");
    const none = linkableObjectSummary({ label: "Account", can_be_primary: false,
                                         outputs_allowed: [], outputs_default_on: [] });
    assert.equal(none.role, "Can be linked, not the main record");
    assert.equal(none.sends, "Meeting details only");
  });

  it("the screen shows linkable objects and no longer shows meeting types", async () => {
    const { readFileSync } = await import("node:fs");
    const screen = readFileSync(new URL("../../src/app/salesforce/index.tsx", import.meta.url), "utf8");
    assert.ok(screen.includes("<SectionRule>Records users can link to</SectionRule>"));
    assert.ok(!screen.includes("meeting_types"), "meeting types are not shown");
    assert.ok(!screen.includes("<SectionRule>Meeting types</SectionRule>"));
    assert.ok(screen.includes("doesn&apos;t support linking yet"));
  });
});
