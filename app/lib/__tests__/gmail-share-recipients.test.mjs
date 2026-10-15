// lib/__tests__/gmail-share-recipients.test.mjs — sending meeting outputs to
// people who were NOT in the meeting.
//
// Run:  node --test lib/__tests__/gmail-share-recipients.test.mjs
//
// WHAT THIS PINS.
//
//   1. THE SHARE SHEET CAN ADD ANY CONTACT. Participants are pre-selected; any
//      other contact is added deliberately through the shared ContactPicker —
//      not a second picker grown inside the sheet.
//
//   2. EVERYONE ADDED HAS AN EMAIL. The picker runs in `requireEmail` mode: no-
//      email rows are disabled, the create form refuses a blank address, a
//      phone contact without one goes to the create form, and an existing
//      match with no address gets the typed one filled in — only into a gap.
//
//   3. NOTHING ELSE CHANGES. The send still carries contact_id only (the
//      backend resolves the address), the cap mirrors the backend's, and the
//      other pickers keep email optional.
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

const SHARE = read("lib/gmail-share.tsx");
const PICKER = read("lib/contact-picker.tsx");
const EMAIL_PY = read("../cloud/shared/email_message.py");

const between = (src, start, end) =>
  src.slice(src.indexOf(start), src.indexOf(end, src.indexOf(start)));

describe("the share sheet adds recipients from contacts", () => {
  const sheet = between(SHARE, "export function GmailShareSheet", "export function GmailShareRow");

  it("renders the shared ContactPicker, in requireEmail mode", () => {
    assert.match(SHARE, /import \{ ContactPicker \} from "\.\/contact-picker"/);
    assert.match(sheet, /<ContactPicker[\s\S]*?onPick=\{addContact\}[\s\S]*?requireEmail[\s\S]*?\/>/);
  });

  it("offers an explicit 'Add from contacts' action", () => {
    assert.match(sheet, /onPress=\{\(\) => setPickerOpen\(true\)\}/);
    assert.match(sheet, /Add from contacts/);
  });

  it("still sends contact_id only, for participants and added people alike", () => {
    assert.match(sheet,
      /recipients: chosenRecipients\.map\(\(r\) => \(\{ contact_id: r\.contact_id \}\)\)/);
    assert.match(sheet, /const allRecipients = \[\.\.\.recipients, \.\.\.extras\]/);
    assert.match(sheet, /allRecipients\.filter\(\(r\) => selected\[r\.contact_id\]\)/);
  });

  it("starts every open from the meeting's participants only", () => {
    const load = between(sheet, "const load = useCallback", "useEffect(");
    assert.match(load, /setExtras\(\[\]\)/);
  });

  it("refuses a contact with no email and never lists someone twice", () => {
    const add = between(sheet, "const addContact", "const onSend");
    assert.match(add, /if \(!email\)[\s\S]*?return;/);
    assert.match(add, /r\.contact_id === c\.id \|\| normEmail\(r\.email\) === email/);
  });

  it("mirrors the backend's recipient cap", () => {
    const py = EMAIL_PY.match(/^MAX_RECIPIENTS = (\d+)/m);
    const ts = SHARE.match(/export const MAX_RECIPIENTS = (\d+);/);
    assert.ok(py && ts, "both constants must exist");
    assert.equal(ts[1], py[1]);
  });
});

describe("the picker's requireEmail mode", () => {
  it("is an opt-in prop, off by default", () => {
    assert.match(PICKER, /requireEmail\?: boolean;/);
    assert.match(PICKER, /requireEmail = false/);
  });

  it("disables people without an email instead of hiding them", () => {
    const rows = between(PICKER, "const renderRow", "return (\n    <Modal");
    assert.equal((rows.match(/const noEmail = requireEmail && !hasEmail\(/g) || []).length, 2,
      "both the Salesforce row and the contact row");
    assert.equal((rows.match(/disabled=\{[^}]*noEmail\}/g) || []).length, 2);
    assert.match(rows, /No email address/);
  });

  it("will not create a contact without an email", () => {
    const submit = between(PICKER, "const submitNew", "const pickSelf");
    assert.match(submit, /if \(requireEmail && !email\)[\s\S]*?return;/);
  });

  it("routes a phone contact without an email to the create form", () => {
    const imp = between(PICKER, "const importFromPhone", "const renderRow");
    assert.match(imp,
      /if \(requireEmail && !hasEmail\(pick\.email\)\)[\s\S]*?setCreating\(true\);\s*return;/);
    // The redirect happens BEFORE anything is uploaded or created.
    assert.ok(imp.indexOf("setCreating(true)") < imp.indexOf("uploadPhoneContactPhoto"));
  });

  it("fills a missing email on an existing match — and only a missing one", () => {
    const ensure = between(PICKER, "const ensureEmail", "const submitNew");
    assert.match(ensure,
      /if \(!requireEmail \|\| hasEmail\(contact\.email\) \|\| !typedEmail\) return contact;/);
    assert.match(ensure, /updateContact\(contact\.id, \{ email: typedEmail \}\)/);
    assert.match(between(PICKER, "const submitNew", "const pickSelf"), /await ensureEmail\(/);
    assert.match(between(PICKER, "const importFromPhone", "const renderRow"), /await ensureEmail\(/);
  });

  it("no other picker turns it on", () => {
    for (const p of [
      "lib/add-task-sheet.tsx", "lib/quick-add-task-sheet.tsx", "lib/speaker-tagging.tsx",
      "src/app/contacts.tsx", "src/app/task/[id].tsx",
      "src/app/recording/[key]/participants.tsx",
      "src/app/recording/[key]/task/[taskId]/assign.tsx",
    ]) {
      assert.doesNotMatch(read(p), /requireEmail/, `${p} keeps email optional`);
    }
  });
});
