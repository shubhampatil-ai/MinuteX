// lib/__tests__/keyboard-avoidance.test.mjs — every input stays visible.
//
// Run:  node --test lib/__tests__/keyboard-avoidance.test.mjs
//
// WHY A TEST AND NOT JUST A FIX
//
// "The keyboard covers what I'm typing" was true across most of the app. A fix
// without a test regresses the moment someone adds the next form, because
// nothing about a missing KeyboardAvoidingView looks wrong in a diff — it only
// looks wrong on a handset, which is exactly where we cannot run CI.
//
// The rule that actually mattered, learned the hard way: ANDROID NEEDS THE
// OPPOSITE BEHAVIOR IN A MODAL THAN ON A SCREEN. A screen is resized by the OS
// (MainActivity is adjustResize), so KeyboardAvoidingView must add nothing. A
// Modal is a SEPARATE WINDOW that never gets resized, so it must shrink itself
// with behavior="height". Applying the screen rule to a sheet — which is what a
// first pass at this did — leaves the sheet's input under the keyboard, and a
// bottom-anchored sheet is the worst case rather than an edge case.
//
// So this test reads the SOURCE of every screen and asserts:
//
//   1. app.json keeps softwareKeyboardLayoutMode = "resize" (belt-and-braces:
//      Expo's template also writes adjustResize onto MainActivity, verified by
//      decoding the built APK's manifest).
//   2. Every file that renders a text input is either wrapped in keyboard
//      avoidance itself, or delegates its inputs to a shared component that is.
//   3. Every ScrollView that contains an input sets
//      keyboardShouldPersistTaps="handled" — without it the first tap on Save
//      while the keyboard is up only dismisses the keyboard, so the button
//      appears broken.
//   4. The two wrappers use the RIGHT behavior for their context (see above).
//
// It is a source-level lint rather than a render test on purpose: rendering
// these screens needs expo-router, expo-audio, native modules and a device
// keyboard, and a test that mocked all four would be asserting against mocks.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";

const ROOT = new URL("../../", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");

function walk(dir, out = []) {
  for (const name of readdirSync(dir)) {
    // The vendored Expo template is not our code.
    if (name === "node_modules" || name === "example" || name === "__tests__") continue;
    const full = join(dir, name);
    if (statSync(full).isDirectory()) walk(full, out);
    else if (/\.tsx$/.test(name)) out.push(full);
  }
  return out;
}

const files = [...walk(join(ROOT, "src")), ...walk(join(ROOT, "lib"))];
const read = (f) => readFileSync(f, "utf8");
const rel = (f) => relative(ROOT, f).replace(/\\/g, "/");

// A file "renders an input" if it uses a raw TextInput, or one of our own
// input components. SearchBar counts: a search field at the bottom of a list is
// just as coverable as a form field.
const RENDERS_INPUT = /<TextInput|<TextField|<SearchBar/;

// Keyboard avoidance is satisfied by wrapping in KeyboardAvoidingView directly,
// or by our shared wrappers, which are the same thing with the platform rules
// already decided.
const HAS_AVOIDANCE = /KeyboardAvoidingView|KeyboardAware(?:Sheet)?[\s>]/;

// Files whose inputs live entirely inside a shared component that IS wrapped.
// Listing them explicitly (rather than pattern-matching) keeps the exemption
// honest — adding a file here is a deliberate claim that has to be true.
const DELEGATES = new Map([
  // Its only input is <SearchBar> feeding the ContactPicker sheet, which is
  // itself keyboard-aware; the list scrolls under a top-anchored search field.
  ["src/app/contacts.tsx", "search field is top-anchored; picker sheet is wrapped"],
  // Same: a top-anchored search over a list. Nothing to cover.
  ["src/app/(tabs)/index.tsx", "top-anchored search over a list"],
  // The picker/sheet components own every input on these screens.
  ["src/app/folders.tsx", "inputs live in FolderSheet"],
  ["src/app/folder/[id].tsx", "inputs live in ContactPicker"],
  // Its search field is rendered by TaskSearchBar at the TOP of the list
  // header, so the keyboard opens below it and cannot cover it — the same
  // top-anchored-search-over-a-list case as contacts.tsx and the home tab.
  ["src/app/tasks.tsx", "top-anchored search over a list"],
  ["src/app/task/[id].tsx", "inputs live in ContactPicker"],
  ["src/app/recording/[key]/participants.tsx", "inputs live in ContactPicker"],
  ["src/app/recording/[key]/task/[taskId]/assign.tsx", "inputs live in ContactPicker"],
  ["lib/ui.tsx", "defines the input primitives and the wrappers themselves"],
  // TaskSearchBar is the only input here and it is top-anchored inside the
  // task list's header (see src/app/tasks.tsx) — there is nothing below it
  // for the keyboard to cover.
  ["lib/task-action-center.tsx", "top-anchored search rendered in a list header"],
  // The transcript's search field is the first thing in the scroll view, above
  // every speaker block, so the keyboard opens BELOW it and cannot cover it —
  // the same top-anchored-search-over-a-list case as contacts.tsx and the home
  // tab. The screen that renders it (recording/[key]/transcript.tsx) wraps its
  // rename Modal in KeyboardAwareSheet, which is the input that genuinely
  // needed covering.
  ["lib/transcript-view.tsx", "top-anchored search over the transcript list"],
]);

describe("android keyboard layout mode", () => {
  it('is "resize", not "pan"', () => {
    const app = JSON.parse(read(join(ROOT, "app.json")));
    const mode = app.expo?.android?.softwareKeyboardLayoutMode;
    assert.equal(
      mode, "resize",
      'android.softwareKeyboardLayoutMode must be "resize". Under "pan" the OS ' +
      "pans the window instead of resizing it, so KeyboardAvoidingView and " +
      "ScrollView get no inset and every input in the app can be covered."
    );
  });
});

describe("every screen with an input handles the keyboard", () => {
  const offenders = [];
  for (const f of files) {
    const src = read(f);
    if (!RENDERS_INPUT.test(src)) continue;
    if (HAS_AVOIDANCE.test(src)) continue;
    if (DELEGATES.has(rel(f))) continue;
    offenders.push(rel(f));
  }

  it("has no unprotected input screens", () => {
    assert.deepEqual(
      offenders, [],
      "These files render a text input with no keyboard avoidance and no " +
      "entry in DELEGATES:\n  " + offenders.join("\n  ") +
      "\n\nWrap the screen in <KeyboardAware> (or <KeyboardAwareSheet> inside " +
      "a Modal) from lib/ui.tsx, or add it to DELEGATES with a reason if its " +
      "inputs genuinely live in a wrapped child."
    );
  });

  it("every DELEGATES entry is still a real file", () => {
    // Stops the exemption list rotting into a place where dead entries hide
    // genuinely unprotected screens.
    const known = new Set(files.map(rel));
    const stale = [...DELEGATES.keys()].filter((f) => !known.has(f));
    assert.deepEqual(stale, [], `DELEGATES lists files that no longer exist: ${stale}`);
  });
});

describe("scrollable forms let the first tap through", () => {
  const offenders = [];
  for (const f of files) {
    const src = read(f);
    if (!RENDERS_INPUT.test(src)) continue;
    if (!/<ScrollView/.test(src)) continue;
    // Either spelled out, or supplied by the shared scrollFormProps spread.
    if (/keyboardShouldPersistTaps|scrollFormProps/.test(src)) continue;
    offenders.push(rel(f));
  }

  it("sets keyboardShouldPersistTaps on scrollable forms", () => {
    assert.deepEqual(
      offenders, [],
      "A ScrollView containing an input needs " +
      'keyboardShouldPersistTaps="handled" (or {...scrollFormProps}), or the ' +
      "first tap on a button while the keyboard is up is swallowed dismissing " +
      "the keyboard:\n  " + offenders.join("\n  ")
    );
  });
});

describe("the shared wrappers use the right platform behavior", () => {
  const ui = read(join(ROOT, "lib", "ui.tsx"));

  it('uses "padding" on iOS', () => {
    // Same on both wrappers; iOS never resizes the window for the keyboard.
    assert.match(ui, /Platform\.OS === "ios" \? "padding"/);
  });

  it('uses "height" on Android for MODAL sheets', () => {
    // THE BUG THIS TEST WAS WRITTEN FOR, after getting it wrong once.
    //
    // A React Native Modal on Android is its own window and does NOT inherit
    // MainActivity's adjustResize, so the OS never resizes it and
    // behavior={undefined} does nothing — the keyboard just covers the sheet.
    // A bottom-anchored sheet is exactly where the keyboard appears, so this is
    // the worst case, not an edge case. "height" makes KeyboardAvoidingView
    // shrink its own frame instead of waiting for a resize that never comes.
    const sheet = ui.slice(ui.indexOf("export function KeyboardAwareSheet"));
    assert.match(
      sheet.slice(0, 600), /Platform\.OS === "ios" \? "padding" : "height"/,
      'KeyboardAwareSheet must use "height" on Android — a Modal is a separate ' +
      "window that the OS does not resize."
    );
  });

  it("leaves Android behavior undefined for full SCREENS", () => {
    // The opposite of the rule above, and both must hold. A normal screen IS
    // resized by the OS (MainActivity is adjustResize), so adding a behavior
    // double-counts the inset and leaves a keyboard-sized gap.
    const screen = ui.slice(
      ui.indexOf("export function KeyboardAware("),
      ui.indexOf("export function KeyboardAwareSheet")
    );
    assert.match(screen, /Platform\.OS === "ios" \? "padding" : undefined/);
  });

  it("exports both wrappers and the scroll props", () => {
    for (const name of ["KeyboardAware", "KeyboardAwareSheet", "scrollFormProps"]) {
      assert.match(ui, new RegExp(`export (?:function|const) ${name}\\b`), name);
    }
  });
});
