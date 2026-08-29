// lib/__tests__/phone-contact-dedup.test.mjs — collapsing the same person.
//
// Run:  node --test lib/__tests__/phone-contact-dedup.test.mjs
//
// WHAT THIS PINS. A real device address book holds the SAME person more than
// once — one row from the Google account, one from the SIM, one from
// WhatsApp. expo-contacts returns all of them. Before this dedup the picker
// showed "Rahul Patil" three times, React warned about duplicate keys
// (identical name + email/phone produced identical keys), and the repeats ate
// the 30-row cap so real contacts fell off the list.
//
// The opposite failure matters just as much: two DIFFERENT people who share a
// name must stay two rows. Merging them here would be exactly the silent
// identity inference the backend's _match_contacts refuses to make — and it
// would end with one person's task assigned to the other. So the rule is:
// merge on a strong identifier, or on a name that nothing contradicts; never
// on a name whose sides carry conflicting identifiers.
//
// dedupePhoneContacts is module-private in lib/contacts.ts (it exists to serve
// searchPhoneContacts, not as API), and that module imports react-native — so
// it is mirrored below, matching the convention in task-insights.test.mjs.
import { describe, it } from "node:test";
import assert from "node:assert/strict";

// --- mirrored from lib/contacts.ts -----------------------------------------

function normPhone(raw) {
  const t = (raw ?? "").trim();
  if (!t) return "";
  const digits = t.replace(/\D/g, "");
  if (digits.length < 7) return "";
  return (t.startsWith("+") ? "+" : "") + digits;
}

function normEmail(raw) {
  return (raw ?? "").trim().toLowerCase();
}

function normName(raw) {
  return (raw ?? "").replace(/\s+/g, " ").trim().toLowerCase();
}

function dedupePhoneContacts(rows) {
  const out = [];
  const byEmail = new Map();
  const byPhone = new Map();
  const byName = new Map();

  for (const row of rows) {
    const email = normEmail(row.email);
    const phone = normPhone(row.phone);
    const name = normName(row.name);

    let at = -1;
    if (email && byEmail.has(email)) at = byEmail.get(email);
    else if (phone && byPhone.has(phone)) at = byPhone.get(phone);
    else {
      for (const i of byName.get(name) ?? []) {
        const kept = out[i];
        const keptEmail = normEmail(kept.email);
        const keptPhone = normPhone(kept.phone);
        const emailConflict = !!email && !!keptEmail && email !== keptEmail;
        const phoneConflict = !!phone && !!keptPhone && phone !== keptPhone;
        if (!emailConflict && !phoneConflict) { at = i; break; }
      }
    }

    if (at >= 0) {
      const kept = out[at];
      if (!kept.email && row.email) {
        kept.email = row.email;
        byEmail.set(normEmail(row.email), at);
      }
      if (!kept.phone && row.phone) {
        kept.phone = row.phone;
        const np = normPhone(row.phone);
        if (np) byPhone.set(np, at);
      }
      if (!kept.photoUri && row.photoUri) kept.photoUri = row.photoUri;
      continue;
    }

    const i = out.length;
    out.push({ ...row });
    if (email) byEmail.set(email, i);
    if (phone) byPhone.set(phone, i);
    byName.set(name, [...(byName.get(name) ?? []), i]);
  }
  return out;
}

// --- tests ------------------------------------------------------------------

describe("normPhone", () => {
  it("collapses separators so one number has one comparable form", () => {
    assert.equal(normPhone("+91 98765 43210"), "+919876543210");
    assert.equal(normPhone("+91-98765-43210"), "+919876543210");
    assert.equal(normPhone("+919876543210"), "+919876543210");
  });

  it("keeps a local number distinct from its international spelling", () => {
    // We have no country context, and guessing one would risk fusing two
    // people. Two rows the user can merge beats one wrongly merged row.
    assert.notEqual(normPhone("9876543210"), normPhone("+919876543210"));
  });

  it("rejects fragments too short to identify anyone", () => {
    assert.equal(normPhone("12345"), "");
    assert.equal(normPhone("x"), "");
    assert.equal(normPhone(undefined), "");
  });
});

describe("dedupePhoneContacts", () => {
  it("merges the Google + SIM copies of one person", () => {
    const out = dedupePhoneContacts([
      { id: "1", name: "Rahul Patil", email: "rahul@x.com" },
      { id: "2", name: "Rahul Patil", email: "rahul@x.com" },
    ]);
    assert.equal(out.length, 1);
    assert.equal(out[0].id, "1");
  });

  it("merges on phone even when the saved formatting differs", () => {
    const out = dedupePhoneContacts([
      { id: "1", name: "Rahul Patil", phone: "+91 98765 43210" },
      { id: "2", name: "Rahul P", phone: "+919876543210" },
    ]);
    assert.equal(out.length, 1);
    assert.equal(out[0].name, "Rahul Patil", "first row's name wins");
  });

  it("completes a person from two half-filled rows", () => {
    // The SIM row has only a number, the Google row only an email. One
    // complete person is more useful than two partial ones.
    const out = dedupePhoneContacts([
      { id: "1", name: "Anita Sharma", phone: "+919999999999" },
      { id: "2", name: "Anita Sharma", email: "anita@x.com" },
    ]);
    assert.equal(out.length, 1);
    assert.equal(out[0].phone, "+919999999999");
    assert.equal(out[0].email, "anita@x.com");
  });

  it("keeps two different people who happen to share a name", () => {
    // The whole point: conflicting strong identifiers mean these are not the
    // same person, however identical the name looks.
    const out = dedupePhoneContacts([
      { id: "1", name: "Rahul Sharma", email: "rahul.s@alpha.com" },
      { id: "2", name: "Rahul Sharma", email: "rahul.s@beta.com" },
    ]);
    assert.equal(out.length, 2);
  });

  it("keeps two same-name people with different phones", () => {
    const out = dedupePhoneContacts([
      { id: "1", name: "Rahul Sharma", phone: "+919876543210" },
      { id: "2", name: "Rahul Sharma", phone: "+919111111111" },
    ]);
    assert.equal(out.length, 2);
  });

  it("does not let a merged-in identifier fuse a third, distinct person", () => {
    // Row 2 merges into row 1 and contributes an email. Row 3 shares the name
    // but carries a different email — it must still stand alone.
    const out = dedupePhoneContacts([
      { id: "1", name: "Rahul Sharma", phone: "+919876543210" },
      { id: "2", name: "Rahul Sharma", email: "rahul@alpha.com" },
      { id: "3", name: "Rahul Sharma", email: "rahul@beta.com" },
    ]);
    assert.equal(out.length, 2);
    assert.equal(out[0].email, "rahul@alpha.com");
    assert.equal(out[1].id, "3");
  });

  it("treats name case and spacing as the same person", () => {
    const out = dedupePhoneContacts([
      { id: "1", name: "Rahul  Patil" },
      { id: "2", name: "rahul patil" },
    ]);
    assert.equal(out.length, 1);
  });

  it("leaves genuinely distinct people alone", () => {
    const out = dedupePhoneContacts([
      { id: "1", name: "Rahul Patil" },
      { id: "2", name: "Anita Sharma" },
      { id: "3", name: "Ritesh More" },
    ]);
    assert.equal(out.length, 3);
  });

  it("produces unique ids, which is what the list key needs", () => {
    const out = dedupePhoneContacts([
      { id: "1", name: "Rahul Patil", email: "rahul@x.com" },
      { id: "2", name: "Rahul Patil", email: "rahul@x.com" },
      { id: "3", name: "Anita Sharma" },
    ]);
    assert.equal(new Set(out.map((c) => c.id)).size, out.length);
  });
});

describe("photo carried across a merge", () => {
  // Only ONE of a person's address-book rows usually holds their picture —
  // typically the Google/WhatsApp one, not the SIM. Before the merge carried
  // it, the person kept whichever row happened to come first and so lost the
  // photo whenever that was the photo-less one.
  it("keeps the photo when the photo-less row comes first", () => {
    const out = dedupePhoneContacts([
      { id: "sim", name: "Rahul Patil", phone: "+91 98765 43210" },
      { id: "goog", name: "Rahul Patil", phone: "+919876543210",
        photoUri: "content://contacts/photo/7" },
    ]);
    assert.equal(out.length, 1);
    assert.equal(out[0].photoUri, "content://contacts/photo/7");
  });

  it("keeps the FIRST photo when both rows have one", () => {
    // Same rule the other fields follow: the first (usually richest) source
    // stays authoritative rather than being overwritten by a later row.
    const out = dedupePhoneContacts([
      { id: "a", name: "Rahul Patil", email: "r@x.com", photoUri: "uri-a" },
      { id: "b", name: "Rahul Patil", email: "r@x.com", photoUri: "uri-b" },
    ]);
    assert.equal(out.length, 1);
    assert.equal(out[0].photoUri, "uri-a");
  });

  it("does not invent a photo for someone who has none", () => {
    const out = dedupePhoneContacts([
      { id: "1", name: "Rahul Patil", phone: "+919876543210" },
      { id: "2", name: "Rahul Patil", phone: "+919876543210" },
    ]);
    assert.equal(out.length, 1);
    assert.equal(out[0].photoUri, undefined);
  });

  it("never moves a photo between two DIFFERENT people who share a name", () => {
    // The failure this guards against is showing one person's face on
    // another's row — the visual twin of a misassigned task.
    const out = dedupePhoneContacts([
      { id: "1", name: "Rahul Sharma", email: "rahul@alpha.com",
        photoUri: "face-of-alpha" },
      { id: "2", name: "Rahul Sharma", email: "rahul@beta.com" },
    ]);
    assert.equal(out.length, 2);
    assert.equal(out[0].photoUri, "face-of-alpha");
    assert.equal(out[1].photoUri, undefined);
  });
});
