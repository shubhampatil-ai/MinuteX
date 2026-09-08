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
// THE RULE CHANGED ONCE ALREADY — read this before "simplifying" anything here.
//
// Round 1 (Android 14 and earlier): a screen was resized by the OS
// (adjustResize), so KeyboardAvoidingView had to add nothing —
// behavior={undefined} — while a Modal, its own window and never resized, had to
// shrink itself with behavior="height". This suite asserted exactly that.
//
// Round 2 (Android 15+, which is what Expo SDK 57 targets): edge-to-edge is
// ENFORCED and THE OS NO LONGER RESIZES THE WINDOW. RN 0.86's
// KeyboardAvoidingView.render() switches on `behavior` with no default branch,
// so behavior={undefined} renders a plain View that ignores the keyboard
// completely. The moment the window stopped shrinking, "undefined" stopped
// meaning "the OS handles it" and started meaning "nothing handles it".
//
// The Assistant chat composer was covered by the keyboard on Android 15 while
// every test here passed — because the suite was asserting the broken value.
// That is the failure this header exists to prevent repeating.
//
// Current rule: "padding" on BOTH platforms for a screen; "height" inside a
// Modal (unchanged — a modal was never resized by the OS on any version, which
// is why sheets kept working when screens broke).
//
// So this test reads the SOURCE of every screen and asserts:
//
//   1. app.json keeps softwareKeyboardLayoutMode = "resize". NOTE: on Android
//      15+ this is largely vestigial — the OS ignores the resize request under
//      enforced edge-to-edge. Kept because it still applies below API 35 and
//      does no harm; it is NOT what makes the current fixes work.
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
  // Handles the keyboard ITSELF, without KeyboardAvoidingView, on purpose.
  //
  // It is the app's one presentation: "modal" screen, so under
  // react-native-screens it renders in its own native container and
  // KeyboardAvoidingView's onLayout frame is container-relative while the
  // keyboard's screenY is window-relative. The two coordinate spaces disagree,
  // its computed inset collapses to ~0, and the composer stays under the
  // keyboard — silently, on a real Android 15 handset, with every check here
  // green. So it tracks endCoordinates.height and lifts the composer directly.
  //
  // Verified by ASSERTION below rather than trust: see "the Assistant screen
  // tracks the keyboard itself". Without that, this file passes the
  // HAS_AVOIDANCE regex purely on the word KeyboardAvoidingView appearing in
  // its explanatory comments, which is not a fix.
  ["src/app/recording/[key]/assistant.tsx", "tracks endCoordinates.height itself; see assertion below"],
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

  it('uses "padding" on Android for full SCREENS too', () => {
    // THIS ASSERTION USED TO DEMAND THE OPPOSITE, and the reversal is the point.
    //
    // It previously required behavior={undefined} on Android, on the theory
    // that MainActivity's adjustResize already resized the window so any
    // behavior would double-count the inset. That was true until it wasn't:
    // Expo SDK 57 targets Android 15+, where edge-to-edge is ENFORCED and the
    // OS no longer resizes the window for the keyboard.
    //
    // RN 0.86's KeyboardAvoidingView.render() switches on `behavior` with no
    // default branch, so undefined renders a plain View with NO keyboard
    // handling at all. Once the window stopped shrinking, "undefined" went from
    // "the OS handles it" to "nothing handles it" — the Assistant chat composer
    // sat under the keyboard on Android 15 with every test still green, because
    // the suite was asserting the broken value.
    //
    // "padding" applies paddingBottom from the keyboardDidShow event and needs
    // no window resize, so it is correct with or without edge-to-edge.
    const screen = ui.slice(
      ui.indexOf("export function KeyboardAware("),
      ui.indexOf("export function KeyboardAwareSheet")
    );
    assert.match(
      screen, /behavior="padding"/,
      'KeyboardAware must use behavior="padding" on both platforms. ' +
      "behavior={undefined} is inert on Android 15+ (edge-to-edge enforced, no " +
      "window resize), which silently removes keyboard avoidance from every " +
      "screen that uses this wrapper."
    );
    assert.doesNotMatch(
      screen, /: undefined/,
      "Android must not get behavior={undefined} — see above."
    );
  });

  it("no screen-level wrapper still uses the inert Android value", () => {
    // The wrapper above is shared, but four screens hand-rolled the same
    // KeyboardAvoidingView and all four had the same inert value. Catch any new
    // one rather than trusting everybody to use the wrapper.
    const offenders = [];
    for (const f of files) {
      const src = read(f);
      if (/"ios" \? "padding" : undefined/.test(src)) offenders.push(rel(f));
    }
    assert.deepEqual(
      offenders, [],
      'behavior={Platform.OS === "ios" ? "padding" : undefined} is inert on ' +
      "Android 15+ — RN renders a plain View and the OS no longer resizes the " +
      'window. Use behavior="padding" (screens) or <KeyboardAware>:\n  ' +
      offenders.join("\n  ")
    );
  });

  it("exports both wrappers and the scroll props", () => {
    for (const name of ["KeyboardAware", "KeyboardAwareSheet", "scrollFormProps"]) {
      assert.match(ui, new RegExp(`export (?:function|const) ${name}\\b`), name);
    }
  });
});

// A Modal containing an input must use MODAL behavior, not screen behavior.
//
// This is the check the original suite was missing, and it is exactly how the
// bug came back. The file-level assertions above are satisfied by ANY
// KeyboardAvoidingView anywhere in the file — so a screen that correctly wrapped
// itself in <KeyboardAware> passed while its bottom sheet, hundreds of lines
// further down, had no avoidance at all (salesforce-config's field picker,
// mom-editor's rename sheet) or had the SCREEN rule copied into it
// (mom-editor's add-section/row/columns sheets, meeting-crm-records, the task
// detail status picker). Both leave the sheet's input under the keyboard on
// Android, and a bottom-anchored sheet is the worst case, not an edge case.
//
// The rule: inside <Modal>, either use <KeyboardAwareSheet> or spell out
// behavior with "height" on Android. `undefined` is a screen-only answer.
describe("modals with inputs use modal keyboard behavior", () => {
  const offenders = [];

  // Comments discuss <Modal> in prose (the wrappers document exactly this
  // rule), and a commented-out block is not shipped markup. Blanking comments
  // to equal-length whitespace keeps every later index/line number honest.
  const blankComments = (src) =>
    src.replace(/\/\*[\s\S]*?\*\/|\/\/[^\n]*/g, (c) => c.replace(/[^\n]/g, " "));

  for (const f of files) {
    const src = blankComments(read(f));
    // ui.tsx defines the wrappers themselves and renders no modal of its own.
    if (rel(f) === "lib/ui.tsx") continue;

    for (const m of src.matchAll(/<Modal\b/g)) {
      // Walk from this <Modal to its matching close, counting nesting so a
      // sheet inside a sheet is attributed to the inner one.
      let depth = 0, end = src.length;
      const tag = /<Modal\b|<\/Modal>/g;
      tag.lastIndex = m.index;
      for (let t; (t = tag.exec(src)); ) {
        if (t[0] === "<Modal") depth++;
        else if (--depth === 0) { end = t.index; break; }
      }
      const body = src.slice(m.index, end);

      // Only a modal that actually contains an input can hide one. A modal that
      // delegates to a child component (e.g. <MomEditorScreen />) is skipped
      // here and checked where that component is defined.
      if (!RENDERS_INPUT.test(body)) continue;

      // The shared wrapper already encodes the right rule.
      if (/<KeyboardAwareSheet\b/.test(body)) continue;

      // Otherwise it must spell out "height" for Android.
      const kav = body.match(/<KeyboardAvoidingView[\s\S]{0,240}?>/);
      if (kav && /"ios" \? "padding" : "height"/.test(kav[0])) continue;

      const line = src.slice(0, m.index).split("\n").length;
      offenders.push(
        `${rel(f)}:${line} — ` +
        (kav ? "screen behavior (undefined) inside a Modal" : "no keyboard avoidance")
      );
    }
  }

  it("has no modal input using screen behavior", () => {
    assert.deepEqual(
      offenders, [],
      "A <Modal> containing a text input is its own window on Android and is " +
      "never resized by the OS, so it must shrink itself. Wrap it in " +
      "<KeyboardAwareSheet> from lib/ui.tsx:\n  " + offenders.join("\n  ")
    );
  });
});

// The Assistant screen tracks the keyboard itself — verify it really does.
//
// This screen is exempted from the wrapper check in DELEGATES, and an exemption
// without an assertion is just a hole: the file would satisfy the HAS_AVOIDANCE
// regex on the word "KeyboardAvoidingView" appearing in its comments alone, so
// deleting the actual fix would not fail anything.
//
// The mechanism it must keep: read endCoordinates.height from a keyboard show
// event, and apply it to the composer. Asserting on the mechanism rather than
// exact source lets the code be refactored, while still failing loudly if the
// keyboard handling is removed.
describe("the Assistant screen tracks the keyboard itself", () => {
  const f = "src/app/recording/[key]/assistant.tsx";
  const src = read(join(ROOT, f));

  it("listens for a keyboard show event", () => {
    assert.match(
      src, /Keyboard\.addListener/,
      `${f} must subscribe to keyboard events — it is exempt from the shared ` +
      "wrapper check precisely because it handles the keyboard itself."
    );
    // keyboardWillShow is iOS-only; Android must use keyboardDidShow or the
    // listener silently never fires and the composer is never lifted.
    assert.match(
      src, /keyboardDidShow/,
      `${f} must listen for "keyboardDidShow" — "keyboardWillShow" does not ` +
      "fire on Android, which is the platform this fix exists for."
    );
  });

  it("reads the keyboard height from the event", () => {
    assert.match(
      src, /endCoordinates[?.]*\.height/,
      `${f} must take its offset from endCoordinates.height. Deriving it from ` +
      "onLayout is what failed here: this screen is presentation: \"modal\", " +
      "so its frame and the keyboard's screenY are in different coordinate " +
      "spaces and the computed inset collapses to ~0."
    );
  });

  it("resets to zero when the keyboard hides", () => {
    // Without this the composer stays lifted after dismissal, leaving a
    // keyboard-sized gap above it.
    assert.match(
      src, /keyboardDidHide|keyboardWillHide/,
      `${f} must handle the hide event and reset the offset to 0.`
    );
  });

  it("applies the offset to the composer", () => {
    assert.match(
      src, /composerWrap,\s*\{\s*(?:margin|padding)Bottom:/,
      `${f} must apply the tracked height to composerWrap as margin/padding ` +
      "Bottom. Tracking the height without applying it lifts nothing."
    );
  });
});
