// lib/__tests__/wav-format.test.mjs — WAV header and merge rules.
//
// Run:  node --test lib/__tests__/wav-format.test.mjs
//
// WHAT THIS PINS AND WHY
//
// The experimental recorder writes WAV itself, so every byte of the container
// is our responsibility — AudioRecord hands back raw PCM and nothing validates
// it downstream until a human notices a recording will not play.
//
// Two failures are specifically worth pinning because both produce a file that
// looks fine:
//
//   1. An unpatched header. A segment is opened with a placeholder header whose
//      sizes are zero. If finalize never runs, the file IS a valid WAV that
//      declares zero audio — players open it and show 0:00, and a transcription
//      of it comes back empty. Size on disk looks correct, so no size check
//      catches it.
//
//   2. A byte-concatenated merge. Appending two WAVs leaves a 44-byte header in
//      the middle of the PCM (an audible click) and the leading header still
//      declares only the first segment length, so every player stops early and
//      the rest is invisible. This is the exact trap lib/rec-store.ts
//      concatSegments() refuses for non-AAC formats.
//
// These are pure functions of bytes, so they are tested here in JS rather than
// through the Kotlin module: the RULES are what matter and they are mirrored
// from WavFile.kt / WavMerger.kt. If those change, these must change with them
// — which is the point.
import { describe, it } from "node:test";
import assert from "node:assert/strict";

const HEADER_BYTES = 44;

// --- the rules, mirrored from modules/.../WavFile.kt -----------------------

function buildWavHeader({ sampleRate, channels, bitsPerSample }, pcmBytes) {
  const b = Buffer.alloc(HEADER_BYTES);
  const bytesPerFrame = channels * (bitsPerSample / 8);
  b.write("RIFF", 0, "ascii");
  b.writeUInt32LE(36 + pcmBytes, 4);
  b.write("WAVE", 8, "ascii");
  b.write("fmt ", 12, "ascii");
  b.writeUInt32LE(16, 16);
  b.writeUInt16LE(1, 20);                          // PCM
  b.writeUInt16LE(channels, 22);
  b.writeUInt32LE(sampleRate, 24);
  b.writeUInt32LE(sampleRate * bytesPerFrame, 28); // byte rate
  b.writeUInt16LE(bytesPerFrame, 32);              // block align
  b.writeUInt16LE(bitsPerSample, 34);
  b.write("data", 36, "ascii");
  b.writeUInt32LE(pcmBytes, 40);
  return b;
}

/** Mirrors readWavInfo(): walks chunks, repairs an impossible data size. */
function readWavInfo(buf) {
  if (buf.length < HEADER_BYTES) return null;
  if (buf.toString("ascii", 0, 4) !== "RIFF") return null;
  if (buf.toString("ascii", 8, 12) !== "WAVE") return null;

  let pos = 12;
  let fmt = null;
  while (pos + 8 <= buf.length) {
    const id = buf.toString("ascii", pos, pos + 4);
    const size = buf.readUInt32LE(pos + 4);
    const payload = pos + 8;
    if (id === "fmt ") {
      if (buf.readUInt16LE(payload) !== 1) return null; // non-PCM
      fmt = {
        channels: buf.readUInt16LE(payload + 2),
        sampleRate: buf.readUInt32LE(payload + 4),
        bitsPerSample: buf.readUInt16LE(payload + 14),
      };
    } else if (id === "data") {
      const available = buf.length - payload;
      const dataBytes = size <= 0 || size > available ? available : size;
      if (!fmt) return null;
      return { format: fmt, dataOffset: payload, dataBytes };
    }
    if (size <= 0) break;
    pos = payload + size + (size & 1);
  }
  return null;
}

function finalizeWavHeader(buf, pcmBytes) {
  buf.writeUInt32LE(36 + pcmBytes, 4);
  buf.writeUInt32LE(pcmBytes, 40);
  return buf;
}

/** Mirrors mergeWavSegments(): strip headers, one new header, PCM back to back. */
function mergeWavSegments(segments) {
  const parsed = [];
  let ref = null;
  let skipped = 0;
  for (const seg of segments) {
    const info = readWavInfo(seg);
    if (!info || info.dataBytes <= 0) { skipped++; continue; }
    if (ref === null) {
      ref = info.format;
    } else if (
      info.format.sampleRate !== ref.sampleRate ||
      info.format.channels !== ref.channels ||
      info.format.bitsPerSample !== ref.bitsPerSample
    ) {
      skipped++; continue;
    }
    parsed.push({ seg, info });
  }
  if (!ref || parsed.length === 0) return { ok: false, skipped };

  const total = parsed.reduce((n, p) => n + p.info.dataBytes, 0);
  const out = Buffer.concat([
    buildWavHeader(ref, total),
    ...parsed.map(({ seg, info }) =>
      seg.subarray(info.dataOffset, info.dataOffset + info.dataBytes)
    ),
  ]);
  return { ok: true, buf: out, merged: parsed.length, skipped };
}

// --- helpers ---------------------------------------------------------------

const FMT = { sampleRate: 16000, channels: 1, bitsPerSample: 16 };

/** A segment carrying `n` bytes of recognisable PCM filled with `fill`. */
function segment(n, fill) {
  const pcm = Buffer.alloc(n, fill);
  return Buffer.concat([buildWavHeader(FMT, n), pcm]);
}

// --- tests -----------------------------------------------------------------

describe("WAV header", () => {
  it("writes a canonical 44-byte RIFF/WAVE PCM header", () => {
    const h = buildWavHeader(FMT, 32000);
    assert.equal(h.length, 44);
    assert.equal(h.toString("ascii", 0, 4), "RIFF");
    assert.equal(h.toString("ascii", 8, 12), "WAVE");
    assert.equal(h.toString("ascii", 12, 16), "fmt ");
    assert.equal(h.toString("ascii", 36, 40), "data");
    assert.equal(h.readUInt16LE(20), 1, "format tag must be 1 (PCM)");
    assert.equal(h.readUInt16LE(22), 1, "mono");
    assert.equal(h.readUInt32LE(24), 16000, "sample rate");
    assert.equal(h.readUInt16LE(34), 16, "bits per sample");
  });

  it("derives byte rate and block align from the format", () => {
    const h = buildWavHeader(FMT, 0);
    // 16000 Hz * 1 ch * 2 bytes = 32000 B/s — the ~1.92 MB/min figure.
    assert.equal(h.readUInt32LE(28), 32000, "byte rate");
    assert.equal(h.readUInt16LE(32), 2, "block align");
  });

  it("sets RIFF size to payload + 36, not payload + 44", () => {
    // Off-by-eight here is the classic WAV bug: the RIFF size counts everything
    // after the size field itself, so 36 + payload.
    const h = buildWavHeader(FMT, 1000);
    assert.equal(h.readUInt32LE(4), 1036);
    assert.equal(h.readUInt32LE(40), 1000);
  });

  it("patches both size fields when a segment is finalized", () => {
    const f = Buffer.concat([buildWavHeader(FMT, 0), Buffer.alloc(6400, 7)]);
    assert.equal(f.readUInt32LE(40), 0, "placeholder starts at zero");
    finalizeWavHeader(f, 6400);
    assert.equal(f.readUInt32LE(40), 6400, "data size patched");
    assert.equal(f.readUInt32LE(4), 6436, "RIFF size patched");
  });

  it("reports duration from the PCM byte count", () => {
    // 32000 B/s, so 320000 bytes is exactly 10 seconds. Deriving duration from
    // bytes rather than wall clock is what makes it correct across a pause.
    const info = readWavInfo(segment(320000, 1));
    const byteRate = info.format.sampleRate * info.format.channels *
      (info.format.bitsPerSample / 8);
    assert.equal(info.dataBytes / byteRate, 10);
  });
});

describe("WAV parsing", () => {
  it("rejects bytes that are not RIFF/WAVE", () => {
    assert.equal(readWavInfo(Buffer.alloc(100)), null);
    const aac = Buffer.alloc(100); aac.write("ADTS", 0, "ascii");
    assert.equal(readWavInfo(aac), null);
  });

  it("rejects non-PCM WAVs, which cannot be frame-concatenated", () => {
    const h = buildWavHeader(FMT, 100);
    h.writeUInt16LE(3, 20);                     // IEEE float
    assert.equal(readWavInfo(Buffer.concat([h, Buffer.alloc(100)])), null);
  });

  it("recovers audio from a segment whose header was never patched", () => {
    // The crash-mid-recording shape: placeholder header, real PCM behind it.
    // Trusting the declared 0 would discard the whole segment.
    const orphan = Buffer.concat([buildWavHeader(FMT, 0), Buffer.alloc(64000, 3)]);
    const info = readWavInfo(orphan);
    assert.equal(info.dataBytes, 64000, "size repaired from file length");
  });

  it("clamps a data size that overruns the file", () => {
    const truncated = Buffer.concat([buildWavHeader(FMT, 999999), Buffer.alloc(500, 1)]);
    assert.equal(readWavInfo(truncated).dataBytes, 500);
  });

  it("finds data past an extra chunk instead of assuming offset 44", () => {
    // A LIST chunk before data. Reading from a hardcoded 44 would treat the
    // chunk header as audio — a burst of noise at the start.
    const list = Buffer.alloc(8 + 10);
    list.write("LIST", 0, "ascii");
    list.writeUInt32LE(10, 4);
    const pcm = Buffer.alloc(3200, 9);
    const data = Buffer.alloc(8);
    data.write("data", 0, "ascii");
    data.writeUInt32LE(pcm.length, 4);

    const head = buildWavHeader(FMT, pcm.length).subarray(0, 36); // through fmt
    const file = Buffer.concat([head, list, data, pcm]);
    const info = readWavInfo(file);
    assert.equal(info.dataBytes, 3200);
    assert.equal(file[info.dataOffset], 9, "payload starts at real audio");
  });
});

describe("WAV merge", () => {
  it("produces one header followed by every segment PCM", () => {
    const segs = [segment(1000, 0xaa), segment(2000, 0xbb), segment(3000, 0xcc)];
    const { ok, buf, merged } = mergeWavSegments(segs);
    assert.ok(ok);
    assert.equal(merged, 3);
    assert.equal(buf.length, HEADER_BYTES + 6000, "header once, all PCM");
    assert.equal(buf.readUInt32LE(40), 6000, "data size covers the total");
  });

  it("leaves no WAV header inside the merged audio", () => {
    // The defining property. A byte-concatenated file would contain "RIFF"
    // again at offset 44 + 1000; a correct merge contains it exactly once.
    const { buf } = mergeWavSegments([segment(1000, 1), segment(1000, 2)]);
    let found = 0;
    for (let i = 0; i + 4 <= buf.length; i++) {
      if (buf.toString("ascii", i, i + 4) === "RIFF") found++;
    }
    assert.equal(found, 1, "exactly one RIFF marker, at the start");
  });

  it("preserves segment order and every byte of audio", () => {
    const { buf } = mergeWavSegments([segment(100, 0x11), segment(100, 0x22)]);
    const body = buf.subarray(HEADER_BYTES);
    assert.ok(body.subarray(0, 100).every((b) => b === 0x11), "first segment first");
    assert.ok(body.subarray(100, 200).every((b) => b === 0x22), "second follows");
  });

  it("declares a duration covering ALL segments", () => {
    // The byte-concatenation failure mode: a player stops at the first
    // segment length and the rest is silently lost.
    const segs = [segment(32000, 1), segment(32000, 2), segment(32000, 3)];
    const { buf } = mergeWavSegments(segs);
    const info = readWavInfo(buf);
    const byteRate = 16000 * 2;
    assert.equal(info.dataBytes / byteRate, 3, "3 seconds, not 1");
  });

  it("skips a segment whose format disagrees rather than corrupting output", () => {
    // A 44.1 kHz segment mixed into 16 kHz would play at the wrong speed —
    // corruption that sounds like audio, so it must be dropped, not joined.
    const odd = (() => {
      const f = { sampleRate: 44100, channels: 1, bitsPerSample: 16 };
      return Buffer.concat([buildWavHeader(f, 1000), Buffer.alloc(1000, 5)]);
    })();
    const { ok, buf, merged, skipped } = mergeWavSegments([
      segment(1000, 1), odd, segment(1000, 2),
    ]);
    assert.ok(ok);
    assert.equal(merged, 2);
    assert.equal(skipped, 1);
    assert.equal(buf.readUInt32LE(24), 16000, "keeps the reference rate");
    assert.equal(buf.readUInt32LE(40), 2000, "only compatible audio included");
  });

  it("keeps going when one segment is unreadable", () => {
    // Losing one damaged segment beats losing the meeting.
    const { ok, merged, skipped } = mergeWavSegments([
      segment(500, 1), Buffer.alloc(20), segment(500, 2),
    ]);
    assert.ok(ok);
    assert.equal(merged, 2);
    assert.equal(skipped, 1);
  });

  it("fails clearly when nothing is readable", () => {
    assert.equal(mergeWavSegments([Buffer.alloc(10), Buffer.alloc(12)]).ok, false);
    assert.equal(mergeWavSegments([]).ok, false);
  });

  it("merges a single segment to a byte-identical payload", () => {
    const one = segment(4096, 0x5a);
    const { buf } = mergeWavSegments([one]);
    assert.deepEqual(buf.subarray(HEADER_BYTES), one.subarray(HEADER_BYTES));
  });
});

describe("size expectations for long recordings", () => {
  // Pins the arithmetic the upload ceiling is judged against, so a future
  // format change (a rate bump, stereo) cannot quietly blow past 3 GB.
  const BYTES_PER_SEC = 16000 * 1 * 2;

  it("is ~1.92 MB per minute", () => {
    assert.equal(BYTES_PER_SEC * 60, 1_920_000);
  });

  it("keeps a 4-hour recording well under the 3 GB upload limit", () => {
    const fourHours = BYTES_PER_SEC * 60 * 60 * 4;
    assert.equal(fourHours, 460_800_000, "~461 MB");
    assert.ok(fourHours < 3_000_000_000);
  });

  it("only exceeds 3 GB past ~26 hours", () => {
    const hours = 3_000_000_000 / (BYTES_PER_SEC * 3600);
    assert.ok(hours > 26 && hours < 27, `got ${hours}`);
  });
});

describe("WAV filename mapping", () => {
  // The WAV engine writes `{id}_final.wav` and `{id}_segment_NNN.wav` rather
  // than `{id}.{format}`, which the store's id<->filename assumptions predate.
  // Two concrete bugs follow if the mapping is wrong, and both are silent:
  //
  //   * deleteSession("abc") deletes `abc.wav`, which does not exist, and
  //     leaves `abc_final.wav` on disk. The orphan sweep then re-adopts it as a
  //     separate recording of the same meeting.
  //   * sweepOrphanAudio() adopts `abc_final.wav` under the id "abc_final" — a
  //     phantom session that no deleteSession("abc") can ever clean up.
  //
  // Mirrored from the id-mapping rules in lib/rec-store.ts.
  function sessionIdFor(filename) {
    const dot = filename.lastIndexOf(".");
    if (dot <= 0) return null;
    let id = filename.slice(0, dot);
    const format = filename.slice(dot + 1);
    if (format === "wav") {
      const seg = id.match(/^(.+)_segment_\d+$/);
      if (seg) return seg[1];
      const fin = id.match(/^(.+)_final$/);
      if (fin) return fin[1];
    }
    return id;
  }

  it("maps a merged WAV back to its session id", () => {
    assert.equal(sessionIdFor("rec_123_final.wav"), "rec_123");
  });

  it("maps every segment back to the same session id", () => {
    assert.equal(sessionIdFor("rec_123_segment_001.wav"), "rec_123");
    assert.equal(sessionIdFor("rec_123_segment_017.wav"), "rec_123");
  });

  it("leaves AAC and m4a filenames alone", () => {
    assert.equal(sessionIdFor("rec_123.aac"), "rec_123");
    assert.equal(sessionIdFor("rec_123.m4a"), "rec_123");
  });

  it("does not strip _final from a non-WAV file", () => {
    // The suffix is only meaningful for the WAV engine; treating it as magic
    // everywhere would corrupt the id of an imported file named that way.
    assert.equal(sessionIdFor("holiday_final.m4a"), "holiday_final");
  });

  it("keeps an id that merely contains the word segment", () => {
    assert.equal(sessionIdFor("segment_notes.wav"), "segment_notes");
  });
});
