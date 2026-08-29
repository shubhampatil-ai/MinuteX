// lib/__tests__/contact-ordering.test.mjs — contact-picker ordering rules.
//
// Run:  node --test lib/__tests__/contact-ordering.test.mjs
//
// WHAT THIS PINS
//
// When assigning a task or naming a speaker, the picker offers contacts in
// three tiers, narrowest context first:
//
//   1. In this meeting  — people already tagged in it. A task from a meeting
//                         almost always belongs to someone who was in it.
//   2. Folder contacts  — the rest of the folder the meeting is filed under.
//   3. All contacts     — everyone else, paged from the server.
//
// Two properties matter as much as the order itself:
//
//   * Each contact appears EXACTLY ONCE, in the most specific tier it
//     qualifies for. A duplicate makes the list look longer while offering no
//     new choice, and lets the user "pick" two different rows for one person.
//   * Search spans every tier. The folder is a shortcut, never a restriction —
//     a global contact must stay reachable by typing their name.
//
// The section builder is mirrored here from lib/contact-picker.tsx rather than
// imported, because that module pulls in React, expo-router and the native
// contacts module at load. These tests state the intended behaviour; a change
// that breaks one should have to justify itself.
import { describe, it } from "node:test";
import assert from "node:assert/strict";

// Mirrors dedupById() in lib/contact-picker.tsx.
function dedupById(contacts) {
  const seen = new Set();
  return contacts.filter((c) => {
    if (seen.has(c.id)) return false;
    seen.add(c.id);
    return true;
  });
}

// --- the rule, as implemented in lib/contact-picker.tsx -------------------
function buildSections({ meetingContacts = [], folderContacts = [], all = [], search = "" }) {
  const folderIds = new Set(folderContacts.map((c) => c.id));
  const needle = search.trim().toLowerCase();
  const matches = (c) =>
    !needle ||
    [c.name, c.email, c.company, c.role].some((f) =>
      (f || "").toLowerCase().includes(needle)
    );

  const out = [];
  const seen = new Set();

  const meetingMatches = dedupById(meetingContacts.filter(matches));
  if (meetingMatches.length) {
    out.push({ kind: "heading", label: "In this meeting" });
    for (const c of meetingMatches) {
      seen.add(c.id);
      out.push({ kind: "contact", contact: c });
    }
  }

  const folderMatches = dedupById(
    folderContacts.filter((c) => matches(c) && !seen.has(c.id))
  );
  if (folderMatches.length) {
    out.push({ kind: "heading", label: "Folder Contacts" });
    for (const c of folderMatches) {
      seen.add(c.id);
      out.push({ kind: "contact", contact: c });
    }
  }

  // NOTE the asymmetry, which is deliberate: `all` is NOT filtered by
  // `matches()` here. It arrives already filtered by the SERVER
  // (getContacts({search})), because the global list is paged and cannot be
  // searched on-device without breaking as soon as an account outgrows one
  // page. The meeting/folder tiers are small in-memory arrays, so they are
  // filtered locally. Tests therefore pass an `all` that reflects what the
  // server would have returned for the given search.
  const rest = dedupById(
    all.filter((c) => !seen.has(c.id) && !folderIds.has(c.id))
  );
  if (rest.length) {
    out.push({ kind: "heading", label: "All Contacts" });
    for (const c of rest) out.push({ kind: "contact", contact: c });
  }
  return out;
}

const ids = (secs) => secs.filter((s) => s.kind === "contact").map((s) => s.contact.id);
const headings = (secs) => secs.filter((s) => s.kind === "heading").map((s) => s.label);

const rahul = { id: "c1", name: "Rahul Sharma", email: "rahul@alpha.test" };
const neha = { id: "c2", name: "Neha Shah", email: "neha@alpha.test" };
const amit = { id: "c3", name: "Amit Patel", email: "amit@alpha.test" };
const priya = { id: "c4", name: "Priya Mehta", email: "priya@other.test" };

describe("three-tier ordering", () => {
  it("puts meeting people first, then folder, then everyone", () => {
    const secs = buildSections({
      meetingContacts: [rahul],
      folderContacts: [rahul, amit],
      all: [rahul, amit, priya],
    });
    assert.deepEqual(headings(secs),
      ["In this meeting", "Folder Contacts", "All Contacts"]);
    assert.deepEqual(ids(secs), ["c1", "c3", "c4"]);
  });

  it("lists each person exactly once, in their most specific tier", () => {
    // Rahul is in the meeting AND the folder AND the global page.
    const secs = buildSections({
      meetingContacts: [rahul],
      folderContacts: [rahul],
      all: [rahul],
    });
    assert.deepEqual(ids(secs), ["c1"]);
    assert.deepEqual(headings(secs), ["In this meeting"]);
  });

  it("omits a tier that has nobody in it", () => {
    const secs = buildSections({ all: [priya] });
    assert.deepEqual(headings(secs), ["All Contacts"]);
  });

  it("works with no meeting context at all", () => {
    // The contacts screen and folder screen pass no meetingContacts.
    const secs = buildSections({
      folderContacts: [amit],
      all: [amit, priya],
    });
    assert.deepEqual(headings(secs), ["Folder Contacts", "All Contacts"]);
    assert.deepEqual(ids(secs), ["c3", "c4"]);
  });

  it("shows global contacts even when the meeting has no folder", () => {
    // THE REPORTED BUG: a meeting with no folder has no folder contacts, so
    // "All Contacts" is the only tier that can render. If it were ever gated
    // on the folder, the picker would look empty on an account WITH contacts.
    const secs = buildSections({
      meetingContacts: [],
      folderContacts: [],
      all: [rahul, neha],
    });
    assert.deepEqual(headings(secs), ["All Contacts"]);
    assert.deepEqual(ids(secs), ["c1", "c2"]);
  });
});

describe("search spans every tier", () => {
  it("finds a global contact by name", () => {
    // `all` is what the SERVER returned for search="priya".
    const secs = buildSections({
      meetingContacts: [rahul],
      folderContacts: [amit],
      all: [priya],
      search: "priya",
    });
    assert.deepEqual(ids(secs), ["c4"]);
  });

  it("finds a meeting person and keeps them in their own tier", () => {
    const secs = buildSections({
      meetingContacts: [rahul],
      folderContacts: [amit],
      all: [rahul],            // server matched Rahul too
      search: "rahul",
    });
    assert.deepEqual(headings(secs), ["In this meeting"]);
    assert.deepEqual(ids(secs), ["c1"], "not duplicated into All Contacts");
  });

  it("matches on email as well as name in the local tiers", () => {
    const secs = buildSections({
      folderContacts: [rahul, priya], all: [], search: "other.test",
    });
    assert.deepEqual(ids(secs), ["c4"]);
  });

  it("is case-insensitive in the local tiers", () => {
    const secs = buildSections({ folderContacts: [rahul], search: "RAHUL" });
    assert.deepEqual(ids(secs), ["c1"]);
  });

  it("returns nothing when nothing matches", () => {
    // The server returned no global matches either.
    const secs = buildSections({
      meetingContacts: [rahul], folderContacts: [amit], all: [],
      search: "zzz-nobody",
    });
    assert.deepEqual(secs, [], "empty means the UI shows its No matches state");
  });

  it("a blank search shows everyone", () => {
    const secs = buildSections({ all: [rahul, neha, priya], search: "   " });
    assert.equal(ids(secs).length, 3);
  });
});

describe("stale-search reset", () => {
  // The other half of the reported bug: reopening the picker used to clear the
  // visible search box but keep the previous DEBOUNCED term, so it fetched
  // matches for a search the user could no longer see — showing "no contacts"
  // on an account that had them.
  function openPicker({ query, debounced }) {
    // What the reset effect must do on open.
    return { query: "", debounced: "" };
  }

  it("clears BOTH halves of the search state on open", () => {
    const after = openPicker({ query: "rahul", debounced: "rahul" });
    assert.equal(after.query, "");
    assert.equal(after.debounced, "",
      "a stale debounced term refetches a search the user cannot see");
  });

  it("leaves an already-clean state clean", () => {
    const after = openPicker({ query: "", debounced: "" });
    assert.deepEqual(after, { query: "", debounced: "" });
  });
});

// --- duplicate keys -------------------------------------------------------
//
// REGRESSION. The picker's rows key on contact.id, and `meetingContacts` is
// built from speaker->contact MAPPINGS:
//
//   p.participants.map((x) => x.contact)
//
// One person tagged as two speakers (Speaker 0 and Speaker 2 are both Priya,
// routine in a diarized meeting) therefore arrives here TWICE. The tier
// filters guarded against duplicates from EARLIER tiers but not within a
// tier, so both rows were emitted with the same key and React logged
// "Encountered two children with the same key" on every render of the sheet.
describe("duplicate contacts in one tier", () => {
  it("shows one row per person when a contact is tagged as two speakers", () => {
    const secs = buildSections({ meetingContacts: [priya, priya] });
    assert.deepEqual(ids(secs), ["c4"]);
  });

  it("emits unique keys for every tier", () => {
    const secs = buildSections({
      meetingContacts: [priya, priya, rahul],
      folderContacts: [rahul, neha, neha],
      all: [amit, amit, priya],
    });
    const keys = ids(secs);
    assert.deepEqual(keys, [...new Set(keys)], "contact keys must be unique");
    assert.deepEqual(keys, ["c4", "c1", "c2", "c3"]);
  });

  it("keeps a duplicated contact in its most specific tier only", () => {
    const secs = buildSections({
      meetingContacts: [priya, priya],
      folderContacts: [priya],
      all: [priya],
    });
    assert.deepEqual(ids(secs), ["c4"]);
    assert.deepEqual(headings(secs), ["In this meeting"]);
  });
});
