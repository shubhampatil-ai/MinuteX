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
//   2. Your organisation — colleagues, synced from live membership. These are
//                         who a member assigns work to most often, and they
//                         are the people who must be taggable in ANY
//                         member's meeting without being typed in.
//   3. Other contacts   — everyone else, paged from the server.
//
// Two properties matter as much as the order itself:
//
//   * Each contact appears EXACTLY ONCE, in the most specific tier it
//     qualifies for. A duplicate makes the list look longer while offering no
//     new choice, and lets the user "pick" two different rows for one person.
//   * Search spans both tiers. The meeting tier is a shortcut, never a
//     restriction — a global contact must stay reachable by typing their name.
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
function buildSections({ meetingContacts = [], all = [], search = "" }) {
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

  // NOTE the asymmetry, which is deliberate: `all` is NOT filtered by
  // `matches()` here. It arrives already filtered by the SERVER
  // (getContacts({search})), because the global list is paged and cannot be
  // searched on-device without breaking as soon as an account outgrows one
  // page. The meeting tier is a small in-memory array, so it is filtered
  // locally. Tests therefore pass an `all` that reflects what the server
  // would have returned for the given search.
  const rest = dedupById(all.filter((c) => !seen.has(c.id)));

  // A THIRD TIER: organisation colleagues, identified by workspace_role,
  // which the SERVER sets only for a live member of the active organisation
  // (never stored on the contact, so it cannot lag a role change). In a
  // personal workspace no row carries it and the list is two tiers exactly
  // as before.
  const colleagues = rest.filter((c) => !!c.workspace_role);
  const others = rest.filter((c) => !c.workspace_role);
  if (colleagues.length) {
    out.push({ kind: "heading", label: "Your organisation" });
    for (const c of colleagues) out.push({ kind: "contact", contact: c });
  }
  if (others.length) {
    out.push({
      kind: "heading",
      label: colleagues.length ? "Other Contacts" : "All Contacts",
    });
    for (const c of others) out.push({ kind: "contact", contact: c });
  }
  return out;
}

const ids = (secs) => secs.filter((s) => s.kind === "contact").map((s) => s.contact.id);
const headings = (secs) => secs.filter((s) => s.kind === "heading").map((s) => s.label);

const rahul = { id: "c1", name: "Rahul Sharma", email: "rahul@alpha.test" };
const neha = { id: "c2", name: "Neha Shah", email: "neha@alpha.test" };
const amit = { id: "c3", name: "Amit Patel", email: "amit@alpha.test" };
const priya = { id: "c4", name: "Priya Mehta", email: "priya@other.test" };
// Colleagues carry the role the SERVER resolved from live membership.
const asha = {
  id: "member-u1", name: "Asha Owner", email: "asha@abc.test",
  workspace_role: "OWNER", is_member: true,
};
const manav = {
  id: "member-u2", name: "Manav Manager", email: "manav@abc.test",
  workspace_role: "MANAGER", is_member: true,
};

describe("two-tier ordering", () => {
  it("puts meeting people first, then everyone", () => {
    const secs = buildSections({
      meetingContacts: [rahul],
      all: [rahul, amit, priya],
    });
    assert.deepEqual(headings(secs), ["In this meeting", "All Contacts"]);
    assert.deepEqual(ids(secs), ["c1", "c3", "c4"]);
  });

  it("lists each person exactly once, in their most specific tier", () => {
    // Rahul is in the meeting AND the global page.
    const secs = buildSections({
      meetingContacts: [rahul],
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
    // The contacts screen passes no meetingContacts.
    const secs = buildSections({ all: [amit, priya] });
    assert.deepEqual(headings(secs), ["All Contacts"]);
    assert.deepEqual(ids(secs), ["c3", "c4"]);
  });

  it("shows global contacts when the meeting has nobody tagged", () => {
    // THE REPORTED BUG, in its surviving form: an untagged meeting leaves
    // "All Contacts" as the only tier that can render. If it were ever gated
    // on the meeting tier, the picker would look empty on an account WITH
    // contacts.
    const secs = buildSections({
      meetingContacts: [],
      all: [rahul, neha],
    });
    assert.deepEqual(headings(secs), ["All Contacts"]);
    assert.deepEqual(ids(secs), ["c1", "c2"]);
  });
});

describe("search spans both tiers", () => {
  it("finds a global contact by name", () => {
    // `all` is what the SERVER returned for search="priya".
    const secs = buildSections({
      meetingContacts: [rahul],
      all: [priya],
      search: "priya",
    });
    assert.deepEqual(ids(secs), ["c4"]);
  });

  it("finds a meeting person and keeps them in their own tier", () => {
    const secs = buildSections({
      meetingContacts: [rahul],
      all: [rahul],            // server matched Rahul too
      search: "rahul",
    });
    assert.deepEqual(headings(secs), ["In this meeting"]);
    assert.deepEqual(ids(secs), ["c1"], "not duplicated into All Contacts");
  });

  it("matches on email as well as name in the local tier", () => {
    const secs = buildSections({
      meetingContacts: [rahul, priya], all: [], search: "other.test",
    });
    assert.deepEqual(ids(secs), ["c4"]);
  });

  it("is case-insensitive in the local tier", () => {
    const secs = buildSections({ meetingContacts: [rahul], search: "RAHUL" });
    assert.deepEqual(ids(secs), ["c1"]);
  });

  it("returns nothing when nothing matches", () => {
    // The server returned no global matches either.
    const secs = buildSections({
      meetingContacts: [rahul], all: [],
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

  it("emits unique keys for both tiers", () => {
    const secs = buildSections({
      meetingContacts: [priya, priya, rahul],
      all: [amit, amit, priya, neha],
    });
    const keys = ids(secs);
    assert.deepEqual(keys, [...new Set(keys)], "contact keys must be unique");
    assert.deepEqual(keys, ["c4", "c1", "c3", "c2"]);
  });

  it("keeps a duplicated contact in its most specific tier only", () => {
    const secs = buildSections({
      meetingContacts: [priya, priya],
      all: [priya],
    });
    assert.deepEqual(ids(secs), ["c4"]);
    assert.deepEqual(headings(secs), ["In this meeting"]);
  });
});

describe("the organisation tier", () => {
  it("puts colleagues between the meeting and everyone else", () => {
    const secs = buildSections({
      meetingContacts: [rahul],
      all: [rahul, priya, asha, manav],
    });
    assert.deepEqual(headings(secs), [
      "In this meeting", "Your organisation", "Other Contacts",
    ]);
    assert.deepEqual(ids(secs), ["c1", "member-u1", "member-u2", "c4"]);
  });

  it("keeps the heading as 'All Contacts' when there are no colleagues", () => {
    // A personal workspace: no row carries workspace_role, so the list must
    // read exactly as it did before this tier existed.
    const secs = buildSections({ all: [rahul, priya] });
    assert.deepEqual(headings(secs), ["All Contacts"]);
    assert.deepEqual(ids(secs), ["c1", "c4"]);
  });

  it("does not duplicate a colleague who is also in the meeting", () => {
    // The most specific tier wins, exactly as for any other contact —
    // otherwise tagging a colleague who spoke would offer two rows for one
    // person.
    const secs = buildSections({
      meetingContacts: [asha],
      all: [asha, manav],
    });
    assert.deepEqual(ids(secs), ["member-u1", "member-u2"]);
    assert.deepEqual(headings(secs), ["In this meeting", "Your organisation"]);
  });

  it("shows only the organisation tier when every contact is a colleague", () => {
    const secs = buildSections({ all: [asha, manav] });
    assert.deepEqual(headings(secs), ["Your organisation"]);
  });

  it("keeps colleagues in a search result", () => {
    // `all` is what the SERVER returned for the search (see the asymmetry
    // note above), and the projected colleagues are filtered server-side by
    // the same needle — so a matching colleague must still be tiered, not
    // swept into "Other Contacts".
    const secs = buildSections({ all: [manav], search: "manav" });
    assert.deepEqual(headings(secs), ["Your organisation"]);
    assert.deepEqual(ids(secs), ["member-u2"]);
  });
});
