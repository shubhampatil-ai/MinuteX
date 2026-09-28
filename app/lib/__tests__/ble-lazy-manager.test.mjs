// lib/__tests__/ble-lazy-manager.test.mjs — importing lib/ble.ts must never
// construct a BleManager.
//
// Run:  node --test lib/__tests__/ble-lazy-manager.test.mjs
//
// WHY THIS EXISTS
//
// A real failure. lib/ble.ts held `const manager = new BleManager()` at module
// scope. react-native-ble-plx is a NATIVE module, so that constructor throws
// "Your JavaScript code tried to access a native module that doesn't exist"
// anywhere the native side isn't linked — Expo Go above all.
//
// Because the call sat at module scope, the throw happened on IMPORT. And the
// import chain is:
//
//     src/app/(tabs)/_layout.tsx -> lib/device-context.tsx
//       -> lib/device/index.ts -> lib/device/esp32-adapter.ts -> lib/ble.ts
//
// so merely loading the tab layout blew up. expo-router then could not read a
// default export off the module it failed to evaluate, which is why the real
// symptom was not "Bluetooth is broken" but an app that would not boot at all:
// "Cannot read property 'ErrorBoundary' of undefined", plus a "Route is
// missing the required default export" warning for EVERY screen in the app,
// including screens that have nothing to do with Bluetooth.
//
// That is the trap this test guards. The damage is wildly out of proportion to
// the cause, and the error message points at expo-router rather than at the one
// line responsible. The fix is that the manager is built lazily, on first real
// use, so the module is safe to import everywhere and BLE reports itself
// unavailable (isBleAvailable) instead of taking the process down.
//
// The assertions are made against the SOURCE rather than by importing it,
// because importing a .ts file that pulls in a native module is the very thing
// that cannot be done here — the same convention as
// native-config-committed.test.mjs.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const SRC = readFileSync(new URL("../ble.ts", import.meta.url), "utf8");

// Strip comments so prose describing the old bug can't satisfy or trip a check.
const CODE = SRC.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");

describe("lib/ble.ts is safe to import without the native module", () => {
  it("never constructs a BleManager at module scope", () => {
    // Module scope == column 0. A construction inside a function is indented,
    // and that is exactly the distinction that matters here.
    const topLevel = CODE.split(/\r?\n/).filter((l) => /^\S/.test(l));
    const offender = topLevel.find((l) => /new BleManager\s*\(/.test(l));
    assert.equal(
      offender,
      undefined,
      `BleManager is constructed at module scope (\`${offender}\`). This throws on ` +
        `import in Expo Go and takes down the whole route tree. Construct it lazily.`
    );
  });

  it("constructs the manager exactly once, inside a guarded accessor", () => {
    const constructions = CODE.match(/new BleManager\s*\(/g) ?? [];
    assert.equal(constructions.length, 1, "expected a single construction site");

    // That one site must be wrapped in try/catch — an unguarded lazy call just
    // relocates the crash to the first screen that touches Bluetooth.
    const site = CODE.indexOf("new BleManager");
    const before = CODE.slice(0, site);
    const openTry = before.lastIndexOf("try");
    const closeFn = before.lastIndexOf("function ");
    assert.ok(
      openTry > closeFn,
      "the BleManager construction must sit inside a try/catch in its accessor"
    );
  });

  it("exposes isBleAvailable so callers can degrade instead of crashing", () => {
    assert.match(CODE, /export function isBleAvailable\s*\(\s*\)\s*:\s*boolean/);
  });

  it("has no default export of the manager", () => {
    // `export default manager` would re-introduce eager evaluation for any
    // importer, defeating the lazy accessor.
    assert.doesNotMatch(CODE, /export\s+default\s+manager/);
  });

  it("routes every manager use through the lazy accessors", () => {
    // A bare `manager.` identifier means something slipped past getManager()/
    // requireManager() and would be a ReferenceError at runtime.
    const bare = CODE.split(/\r?\n/).filter((l) => /(?<![A-Za-z])manager\.\w/.test(l));
    assert.deepEqual(bare, [], "found manager.* uses outside the lazy accessors");
  });
});
