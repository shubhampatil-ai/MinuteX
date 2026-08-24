// lib/__tests__/native-config-committed.test.mjs — native config must be
// committed, not just saved.
//
// Run:  node --test lib/__tests__/native-config-committed.test.mjs
//
// WHY THIS EXISTS
//
// A real failure, not a hypothetical. The keyboard-covers-inputs bug came down
// to one line in app.json (softwareKeyboardLayoutMode: "pan" -> "resize"). The
// line was fixed on disk, the JS work around it was fixed, everything typechecked
// and every test passed — and the bug survived two more builds, because:
//
//   1. app.json properties that land in AndroidManifest.xml only take effect in
//      a NEW NATIVE BUILD. No Metro reload, no EAS Update, no OTA.
//   2. EAS Build builds from GIT, not the working tree. An uncommitted fix is
//      invisible to it.
//
// So the edit sat in the working tree while EAS rebuilt the same old commit
// twice, and the reported symptom never changed. "It's fixed locally and the
// tests pass" was true and useless at the same time.
//
// This test closes that gap: if a file whose contents get compiled into the
// binary has uncommitted changes, it fails and says so. It is the cheapest
// possible reminder that for these files, `git commit` is part of the fix
// rather than paperwork afterwards.
//
// It is deliberately NOT a check that a build exists or is current — that would
// be flaky and would fail on every ordinary edit. It only asserts the thing a
// developer can actually be wrong about silently: having native changes that no
// build could ever have seen.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { join } from "node:path";

const ROOT = new URL("../../", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");

// Files that are compiled INTO the app binary. A change to any of these cannot
// reach a device except through a fresh native build.
//
// app.json          -> AndroidManifest.xml / Info.plist (permissions, keyboard
//                      mode, plugins). The file this test was written for.
// modules/**        -> local native modules (Kotlin/Swift).
// plugins/**        -> config plugins, which rewrite native project files.
// package.json      -> a new native dependency needs a rebuild to be linked.
const NATIVE_PATHS = ["app.json", "modules", "plugins", "package.json"];

function git(args) {
  try {
    return execFileSync("git", args, { cwd: ROOT, encoding: "utf8" }).trim();
  } catch {
    return null;   // not a git checkout, or git unavailable
  }
}

describe("native config is committed", () => {
  const insideRepo = git(["rev-parse", "--is-inside-work-tree"]) === "true";

  it("has no uncommitted changes to files that need a native build", (t) => {
    if (!insideRepo) {
      // A tarball/CI checkout with no git history has nothing to assert about.
      t.skip("not a git working tree");
      return;
    }

    const present = NATIVE_PATHS.filter((p) => existsSync(join(ROOT, p)));
    const dirty = git(["status", "--porcelain", "--", ...present]);
    if (dirty === null) {
      t.skip("git status unavailable");
      return;
    }

    const files = dirty
      .split("\n")
      .map((l) => l.trim())
      .filter(Boolean)
      // Ignore build scratch that git happens to see inside modules/.
      .filter((l) => !/\.gradle\/|\/build\/|node_modules\//.test(l));

    assert.deepEqual(
      files, [],
      "These files are compiled into the app binary and have uncommitted " +
      "changes:\n  " + files.join("\n  ") +
      "\n\nEAS Build builds from GIT, so an uncommitted change here cannot " +
      "reach a device no matter how many times you rebuild. This is exactly " +
      "how the keyboard fix survived two builds unchanged.\n" +
      "Commit these, THEN run: eas build --profile development\n" +
      "Afterwards confirm the build picked them up:\n" +
      "  eas build:list --platform android --limit 1   # Commit must match HEAD"
    );
  });

  it("keeps the keyboard mode that the JS fixes depend on", () => {
    // Belt and braces with keyboard-avoidance.test.mjs: that one reads the
    // working tree, this one reads what is COMMITTED — i.e. what a build would
    // actually contain.
    const committed = git(["show", "HEAD:app.json"]);
    if (committed === null) return;   // no HEAD yet
    const mode = JSON.parse(committed).expo?.android?.softwareKeyboardLayoutMode;
    assert.equal(
      mode, "resize",
      'The COMMITTED app.json must have softwareKeyboardLayoutMode "resize". ' +
      'Under "pan" the OS pans the window instead of resizing it, so every ' +
      "KeyboardAvoidingView in the app is inert and inputs get covered."
    );
  });
});
