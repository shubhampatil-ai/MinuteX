// lib/__tests__/audio-level.test.mjs — the live waveform's signal path.
//
// Run:  node --test lib/__tests__/audio-level.test.mjs
//
// WHY THIS FILE EXISTS. The recorder's waveform is the user's only live
// evidence that the microphone is hearing them. It used to animate to
// random targets, which meant it kept dancing over a mic that had
// silently stopped. The replacement is driven by the real input level, and
// every rule that turns a dB reading into a bar height lives in
// lib/audio-level.ts — pure, so it is imported and tested here directly (Node
// strips the types) rather than mirrored.
//
// The last block is a static guard: no screen or waveform module may bring
// random or seeded bar heights back.
import { describe, it } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import {
  LEVEL_CEIL_DB, LEVEL_FLOOR_DB, LevelHistory, LevelSmoother,
  dbfsToLevel, linearToDbfs, stepWaveform,
} from "../audio-level.ts";

const ROOT = new URL("../../", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const read = (p) => readFileSync(join(ROOT, p), "utf8");

describe("dbfsToLevel — dB to bar height", () => {
  it("silence is flat: hardware zero, no reading, and room tone all map to 0", () => {
    assert.equal(dbfsToLevel(-160), 0);
    assert.equal(dbfsToLevel(null), 0);
    assert.equal(dbfsToLevel(undefined), 0);
    assert.equal(dbfsToLevel(Number.NaN), 0);
    assert.equal(dbfsToLevel(-58), 0, "a quiet room's noise floor draws nothing");
    assert.equal(dbfsToLevel(LEVEL_FLOOR_DB), 0);
  });

  it("is monotonic: louder is never shorter", () => {
    let prev = -1;
    for (let db = -70; db <= 0; db += 1) {
      const v = dbfsToLevel(db);
      assert.ok(v >= prev, `${db} dB gave ${v}, below ${prev}`);
      prev = v;
    }
  });

  it("quiet < normal speech < loud speech, all visibly distinct", () => {
    const quiet = dbfsToLevel(-40);
    const normal = dbfsToLevel(-25);
    const loud = dbfsToLevel(-12);
    assert.ok(quiet > 0 && quiet < 0.35, `quiet speech ${quiet}`);
    assert.ok(normal > 0.4 && normal < 0.75, `normal speech ${normal}`);
    assert.ok(loud > 0.75, `loud speech ${loud}`);
    assert.ok(normal - quiet > 0.2 && loud - normal > 0.1);
  });

  it("clamps at the ceiling rather than overflowing the bar", () => {
    assert.equal(dbfsToLevel(LEVEL_CEIL_DB), 1);
    assert.equal(dbfsToLevel(0), 1);
    assert.equal(dbfsToLevel(12), 1);
  });

  it("treats a degenerate window as silence, never Infinity", () => {
    assert.equal(dbfsToLevel(-20, -10, -10), 0);
    assert.equal(dbfsToLevel(-20, -5, -10), 0);
  });
});

describe("linearToDbfs", () => {
  it("full scale is 0 dB, half is about -6 dB", () => {
    assert.equal(linearToDbfs(1), 0);
    assert.ok(Math.abs(linearToDbfs(0.5) - -6.02) < 0.01);
  });
  it("zero / invalid is the no-signal floor, which the level map reads as silence", () => {
    for (const v of [0, -1, null, undefined, Number.NaN]) {
      assert.equal(linearToDbfs(v), -160);
      assert.equal(dbfsToLevel(linearToDbfs(v)), 0);
    }
  });
});

describe("LevelSmoother — steady, not jittery", () => {
  it("rises quickly on a speech onset", () => {
    const s = new LevelSmoother();
    const first = s.next(0.8);
    const second = s.next(0.8);
    assert.ok(first >= 0.45, `first sample only reached ${first}`);
    assert.ok(second > first && second <= 0.8);
  });

  it("falls gradually between syllables instead of collapsing", () => {
    const s = new LevelSmoother();
    for (let i = 0; i < 10; i++) s.next(0.8);
    const after = s.next(0);
    assert.ok(after > 0.5, `one silent sample dropped the bar to ${after}`);
  });

  it("settles to exactly zero in silence, so the waveform goes flat", () => {
    const s = new LevelSmoother();
    for (let i = 0; i < 10; i++) s.next(1);
    let v = 1;
    for (let i = 0; i < 60; i++) v = s.next(0);
    assert.equal(v, 0);
  });

  it("damps alternating input — its output swings far less than its input", () => {
    const s = new LevelSmoother();
    const out = [];
    for (let i = 0; i < 40; i++) out.push(s.next(i % 2 ? 0.2 : 0.7));
    const tail = out.slice(20);
    const swing = Math.max(...tail) - Math.min(...tail);
    assert.ok(swing < 0.5 * 0.7, `swing ${swing} is not smoothed`);
  });

  it("clamps garbage input into range", () => {
    const s = new LevelSmoother();
    for (const bad of [Number.NaN, Infinity, -5, 9]) {
      const v = s.next(bad);
      assert.ok(v >= 0 && v <= 1, `${bad} -> ${v}`);
    }
  });

  it("is deterministic: the same input produces the same output", () => {
    const run = () => {
      const s = new LevelSmoother();
      return [0.1, 0.5, 0.9, 0.2, 0, 0.6].map((x) => s.next(x));
    };
    assert.deepEqual(run(), run());
  });
});

describe("LevelHistory — fixed-size ring, oldest → newest", () => {
  it("marks unfilled slots -1 and keeps newest last", () => {
    const h = new LevelHistory(4);
    assert.deepEqual(h.toArray(), [-1, -1, -1, -1]);
    h.push(0.1); h.push(0.2);
    assert.deepEqual(h.toArray(), [-1, -1, 0.1, 0.2]);
  });

  it("drops the oldest once full, so memory never grows", () => {
    const h = new LevelHistory(3);
    for (const v of [1, 2, 3, 4, 5]) h.push(v);
    assert.deepEqual(h.toArray(), [3, 4, 5]);
    assert.equal(h.toArray().length, 3);
    assert.equal(h.count(), 3);
  });

  it("clear() returns to an empty field", () => {
    const h = new LevelHistory(2);
    h.push(1); h.clear();
    assert.deepEqual(h.toArray(), [-1, -1]);
  });

  it("rejects a nonsensical size", () => {
    assert.throws(() => new LevelHistory(0));
    assert.throws(() => new LevelHistory(2.5));
  });
});

describe("stepWaveform — the whole pipeline", () => {
  it("a silent recording draws a flat line of recorded-silence (0), not placeholders", () => {
    const s = new LevelSmoother();
    const h = new LevelHistory(5);
    let bars = [];
    for (let i = 0; i < 5; i++) bars = stepWaveform(-160, s, h);
    assert.deepEqual(bars, [0, 0, 0, 0, 0]);
  });

  it("speech after silence appears at the newest (right-hand) end", () => {
    const s = new LevelSmoother();
    const h = new LevelHistory(4);
    for (let i = 0; i < 3; i++) stepWaveform(-160, s, h);
    const bars = stepWaveform(-20, s, h);
    assert.equal(bars[0], 0);
    assert.ok(bars[3] > 0.3, `newest bar ${bars[3]}`);
  });
});

// ---------------------------------------------------------------------------
// Static guards
// ---------------------------------------------------------------------------
describe("no fake waveform data", () => {
  const files = [
    "lib/waveform.tsx",
    "lib/use-input-level.ts",
    "lib/audio-level.ts",
    "src/app/record-phone.tsx",
    "src/app/record.tsx",
    "src/app/(tabs)/index.tsx",
    "lib/audio-player.tsx",
  ];

  it("nothing on the waveform path uses Math.random", () => {
    for (const f of files) {
      assert.doesNotMatch(read(f), /Math\.random/, `${f} uses Math.random`);
    }
  });

  it("the seeded and random waveform components are gone", () => {
    const src = read("lib/waveform.tsx");
    assert.doesNotMatch(src, /export function (Waveform|LiveWaveform)\b/);
    assert.doesNotMatch(src, /seededHeights/);
    for (const f of files) {
      assert.doesNotMatch(read(f), /<(Live)?Waveform\b/, `${f} still renders a fake waveform`);
    }
  });

  it("the phone recorder draws levels from the controller, not a timer of its own", () => {
    assert.match(read("lib/use-input-level.ts"), /readInputLevel/);
    assert.match(read("src/app/record-phone.tsx"), /useWaveformLevels/);
  });

  it("the device recorder draws no waveform — no audio reaches the phone", () => {
    const src = read("src/app/record.tsx");
    assert.doesNotMatch(src, /LevelWaveform|useWaveformLevels/);
  });
});

describe("the silence detector still sees the whole tick", () => {
  // On Android AAC, metering is getMaxAmplitude(): peak since the LAST read,
  // reset on read. The waveform's faster reads are folded into a window that
  // reconcile() consumes, so mic-theft detection judges the loudest reading
  // over its full tick — never just the sliver since the waveform last read.
  const ctrl = read("lib/rec-controller.ts");

  it("reconcile consumes the metering window before judging silence", () => {
    assert.match(ctrl, /const meterDb = takeMeterWindow\(status\?\.metering\)/);
    assert.match(ctrl, /meterDb != null && meterDb <= SILENCE_DB_FLOOR/);
    assert.doesNotMatch(ctrl, /status\?\.metering != null && status\.metering <= SILENCE_DB_FLOOR/);
  });

  it("readInputLevel folds every peak it reads into the window", () => {
    assert.match(ctrl, /export function readInputLevel\(\)/);
    assert.match(ctrl, /meterWindowMaxDb = meterWindowMaxDb == null \? peak : Math\.max\(meterWindowMaxDb, peak\)/);
  });

  it("readInputLevel only reads while RECORDING", () => {
    assert.match(ctrl, /session\.state !== "RECORDING"[^\n]*return null/);
  });

  // The fold itself, mirrored: max of the window and the tick's own read.
  const take = (win, cur) => (win == null ? cur : cur == null || !Number.isFinite(cur) ? win : Math.max(win, cur));
  it("a stolen mic (every read -160) still reads as hardware zero", () => {
    assert.equal(take(-160, -160), -160);
  });
  it("any real signal in the window keeps the tick out of the silence run", () => {
    assert.equal(take(-42, -160), -42);
  });
  it("with no waveform mounted the tick reads exactly as before", () => {
    assert.equal(take(null, -37), -37);
  });
});
