// lib/__tests__/tag-self.test.mjs — identifying YOURSELF in your own meeting.
//
// Run:  node --test lib/__tests__/tag-self.test.mjs
//
// THE GAP THIS CLOSES. Every contact in MinuteX is someone in your address
// book, and the speaker picker offered exactly three routes: an existing
// contact, a phone-book import, or typing a new person in by hand. None of
// them is "me". So the user who recorded the meeting — very often Speaker 0 —
// could only tag themselves by hand-typing their own name AND their own email
// into Create New Contact.
//
// That email is not cosmetic. The backend links a contact to a MinuteX account
// by looking the address up in the Users table (_resolve_minutex_user), and
// `assignee_user_id` is written ONLY from that link. So a typo produced a
// contact that looks right, tags the speaker correctly, and can never be
// notified — a silent failure whose symptom (missing notifications) shows up
// nowhere near its cause (a mistyped address weeks earlier).
//
// WHAT THIS PINS.
//
//   1. THE SHORTCUT EXISTS, AND IS OPT-IN. `allowSelf` is a prop, not a
//      default: most pickers ask "who is this OTHER person?", where offering
//      yourself is noise. The participants screen opts in because that is the
//      screen where the answer is often you.
//
//   2. THE EMAIL COMES FROM THE AUTHENTICATED PROFILE. Never typed. This is
//      the whole point — it is what makes the account link reliable rather
//      than a thing the user can get subtly wrong.
//
//   3. IT REUSES, NEVER DUPLICATES. createContact returns the EXISTING
//      contact when a strong identifier matches, so tapping this twice (or
//      after having typed yourself in once) converges on one row.
//
//   4. NO EMAIL, NO SHORTCUT. An account with no address cannot be linked, so
//      the button does not appear rather than creating a self-contact that can
//      never receive anything.
//
// Source assertions rather than a render harness: these are TSX modules and
// node --test runs .mjs with no transpile step.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";

const ROOT = new URL("../../", import.meta.url).pathname
  .replace(/^\/([A-Za-z]:)/, "$1");

const read = (p) => readFileSync(join(ROOT, p), "utf8");

const PICKER = read("lib/contact-picker.tsx");
const PARTICIPANTS = read("src/app/recording/[key]/participants.tsx");

describe("the picker can offer the signed-in user", () => {
  it("exposes allowSelf as an opt-in prop", () => {
    assert.match(
      PICKER, /allowSelf\?:\s*boolean/,
      "allowSelf must be a declared prop"
    );
    assert.match(
      PICKER, /allowSelf = false/,
      "it must default OFF — most pickers ask about someone else"
    );
  });

  it("loads the profile only when asked, and only while open", () => {
    // A picker that never offers the shortcut must not pay for it.
    assert.match(
      PICKER, /if \(!allowSelf \|\| !visible \|\| me\) return;/,
      "the profile fetch must be gated on allowSelf, visibility, and cache"
    );
  });

  it("does not offer the shortcut to an account with no email", () => {
    // Without an address there is nothing for _resolve_minutex_user to match,
    // so the contact could never be notified. Better absent than broken.
    assert.match(
      PICKER, /if \(alive && u\.email\) setMe\(/,
      "an account with no email must not get the shortcut"
    );
  });

  it("renders the button only when both conditions hold", () => {
    assert.match(
      PICKER, /\{allowSelf && !!me && \(/,
      "the button needs the opt-in AND a loaded profile"
    );
    assert.match(
      PICKER, /label=\{`That’s me \(\$\{me\.name\}\)`\}/,
      "the button should name the user, so it is obvious who it will pick"
    );
  });
});

describe("picking yourself produces a linkable contact", () => {
  it("takes the email from the profile, never from typed input", () => {
    const fn = PICKER.slice(
      PICKER.indexOf("const pickSelf"),
      PICKER.indexOf("// Ask for permission and load the device list")
    );
    assert.ok(fn.length > 0, "pickSelf must exist");
    assert.match(fn, /email: me\.email/,
      "the email must come from the authenticated profile");
    assert.match(fn, /name: me\.name/);
    // The hand-entry fields must not leak into this path.
    assert.doesNotMatch(fn, /newEmail|newName|newPhone/,
      "pickSelf must not read the create-new form fields");
  });

  it("goes through the ordinary create path, which dedupes by email", () => {
    // "Me" has to BE a contact — a contact is the unit of assignment
    // everywhere — and createContact answers with the EXISTING row when a
    // strong identifier already matches, so this converges rather than
    // accumulating self-rows.
    const fn = PICKER.slice(
      PICKER.indexOf("const pickSelf"),
      PICKER.indexOf("// Ask for permission and load the device list")
    );
    assert.match(fn, /await createContact\(\{/);
    assert.match(fn, /onPick\(contact\)/, "it must select the contact it made");
  });

  it("files the contact into the folder when there is one", () => {
    const fn = PICKER.slice(
      PICKER.indexOf("const pickSelf"),
      PICKER.indexOf("// Ask for permission and load the device list")
    );
    assert.match(fn, /folder_id: folderId \|\| undefined/);
  });

  it("surfaces a failure instead of silently doing nothing", () => {
    const fn = PICKER.slice(
      PICKER.indexOf("const pickSelf"),
      PICKER.indexOf("// Ask for permission and load the device list")
    );
    assert.match(fn, /setCreateError\(/, "a failed create must be visible");
  });
});

describe("the speaker screen offers it", () => {
  it("participants opts in", () => {
    // This is THE screen where the answer is often the user: you are almost
    // always in your own meeting.
    assert.match(
      PARTICIPANTS, /allowSelf/,
      "the speaker picker must offer the shortcut"
    );
  });
});
