// Every SF Symbol the app renders must have an Android (Material Symbol)
// mapping in lib/icons.tsx. `ListRow` / `IconCircle` take a plain string, so
// a missing mapping type-checks and silently draws an EMPTY icon on Android.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

const ROOT = join(import.meta.dirname, "..", "..");
const MAP = new Set([...readFileSync(join(ROOT, "lib/icons.tsx"), "utf8")
  .matchAll(/^\s*"([^"]+)":\s*"[^"]*"/gm)].map((m) => m[1]));

function files(dir) {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n);
    if (statSync(p).isDirectory()) return n === "__tests__" || n === "node_modules" ? [] : files(p);
    return p.endsWith(".tsx") ? [p] : [];
  });
}

describe("Android icons", () => {
  it("every icon name used in the app has an Android glyph", () => {
    const missing = [];
    for (const f of [...files(join(ROOT, "src")), ...files(join(ROOT, "lib"))]) {
      const src = readFileSync(f, "utf8");
      // icon="x.y" / name="x.y" props and { icon: "x.y" } entries. Dotted SF
      // Symbol names only: route names (name="index") are not icons.
      for (const m of src.matchAll(/\b(?:icon|name)(?:=\{?|:\s*)"([a-z][a-z0-9]*(?:\.[a-z0-9]+)+)"/g)) {
        if (!MAP.has(m[1])) missing.push(`${m[1]} (${f.slice(ROOT.length + 1)})`);
      }
    }
    assert.deepEqual(missing, [], "add these to MAP in lib/icons.tsx");
  });
});
