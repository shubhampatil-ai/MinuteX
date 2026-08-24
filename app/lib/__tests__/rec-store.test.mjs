// lib/__tests__/rec-store.test.mjs — logic tests for the recording state model.
//
// Run:  node --test lib/__tests__/
//
// WHAT THIS COVERS, AND WHY IT'S WORTH TESTING IN ISOLATION
// The recording pipeline's correctness lives in decisions that are pure
// functions of a session record: is this pause the user's or the platform's,
// how long was actually captured across pauses, does this session have real
// audio, may we delete the local file yet. Those are precisely the decisions a
// device test can only exercise by accident, and the ones where a wrong answer
// silently loses someone's meeting. So they are pulled out and pinned here.
//
// The parts that genuinely require a device — whether Android's foreground
// service keeps the mic alive with the screen locked, whether iOS resumes
// after a real phone call — are NOT simulated. Faking them would produce a
// green suite that says nothing about the platform behaviour, so they are
// listed as manual tests in the audit instead.
//
// expo-file-system is stubbed with an in-memory filesystem via a module mock:
// there is no native module in a bare `node --test` run, and the point here is
// the decision logic, not expo's file API.
import { describe, it, before, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { register } from "node:module";

// ---------------------------------------------------------------------------
// In-memory expo-file-system stub
// ---------------------------------------------------------------------------
// Shared with the loader hook through a global, because the hook runs on its
// own thread and can only pass strings — so the stub module SOURCE is what
// gets injected, and it reaches back into this global for the actual store.
const fsState = {
  files: new Map(),   // uri -> { text, size, lastModified }
  dirs: new Set(),
  available: 10 * 1024 * 1024 * 1024,
  total: 64 * 1024 * 1024 * 1024,
};
globalThis.__FS__ = fsState;

const STUB = `
const S = globalThis.__FS__;
const DOC = "file:///doc";
// Join path segments without mangling the "file:///" scheme prefix. A naive
// slash-collapse eats the triple slash and turns file:///doc into file://doc,
// which then fails to match any seeded URI — so the scheme is split off,
// collapsed separately, and put back.
function join(...parts) {
  const raw = parts
    .map((p) => (typeof p === "string" ? p : p.uri))
    .filter(Boolean)
    .join("/");
  const m = /^([a-z]+:\\/\\/)(.*)$/i.exec(raw);
  if (!m) return raw.replace(/\\/{2,}/g, "/");
  return m[1] + m[2].replace(/\\/{2,}/g, "/");
}
export class Directory {
  constructor(...uris) { this.uri = join(...uris); }
  get exists() { return S.dirs.has(this.uri); }
  get name() { return this.uri.split("/").pop(); }
  create() { S.dirs.add(this.uri); }
  list() {
    const out = [];
    for (const uri of S.files.keys()) {
      const parent = uri.slice(0, uri.lastIndexOf("/"));
      if (parent === this.uri) out.push(new File(uri));
    }
    return out;
  }
}
export class File {
  constructor(...uris) { this.uri = join(...uris); }
  get name() { return this.uri.split("/").pop(); }
  get exists() { return S.files.has(this.uri); }
  get size() { return S.files.get(this.uri)?.size ?? 0; }
  get lastModified() { return S.files.get(this.uri)?.lastModified ?? null; }
  get md5() { return null; }
  write(content) {
    S.files.set(this.uri, {
      text: String(content),
      size: Buffer.byteLength(String(content)),
      lastModified: S.now ?? 1000,
    });
  }
  textSync() {
    const f = S.files.get(this.uri);
    if (!f) throw new Error("ENOENT " + this.uri);
    return f.text;
  }
  delete() { S.files.delete(this.uri); }
  async copy(dest) {
    const f = S.files.get(this.uri);
    if (!f) throw new Error("ENOENT " + this.uri);
    S.files.set(dest.uri, { ...f });
  }
  create() {
    if (!S.files.has(this.uri)) {
      S.files.set(this.uri, { text: "", bytes: [], size: 0, lastModified: S.now ?? 1000 });
    }
  }
  // Byte streams, for concatSegments(). \`bytes\` holds the raw payload so the
  // test can assert the JOINED ORDER, which is the whole point of the merge.
  readableStream() {
    const f = S.files.get(this.uri);
    if (!f) throw new Error("ENOENT " + this.uri);
    const chunks = f.bytes ? [Uint8Array.from(f.bytes)] : [Buffer.from(f.text)];
    let i = 0;
    return new ReadableStream({
      pull(c) {
        if (i < chunks.length && chunks[i].length) c.enqueue(chunks[i++]);
        else c.close();
      },
    });
  }
  writableStream() {
    const uri = this.uri;
    if (S.failWritesTo && S.failWritesTo === uri) {
      return new WritableStream({ write() { throw new Error("disk full"); } });
    }
    return new WritableStream({
      write(chunk) {
        const cur = S.files.get(uri) ?? { text: "", bytes: [], size: 0, lastModified: 1000 };
        const bytes = [...(cur.bytes ?? []), ...chunk];
        S.files.set(uri, {
          text: Buffer.from(bytes).toString(),
          bytes,
          size: bytes.length,
          lastModified: S.now ?? 1000,
        });
      },
    });
  }
}
export const Paths = {
  get document() { return new Directory(DOC); },
  get cache() { return new Directory("file:///cache"); },
  get availableDiskSpace() { return S.available; },
  get totalDiskSpace() { return S.total; },
};
export const UploadType = { BINARY_CONTENT: 0, MULTIPART: 1 };
`;

// A tiny resolver/loader with two jobs:
//
//  1. Serve the in-memory stub for "expo-file-system". There is no native
//     module in a bare `node --test` run.
//  2. Append ".ts" to extensionless relative imports. The app's source is
//     bundled by Metro, which resolves `./rec-log` to `./rec-log.ts` for us;
//     Node's ESM resolver requires the real extension. Rewriting the specifier
//     here is what lets the production source run untouched under the test —
//     the alternative, adding extensions throughout the app, would be changing
//     shipping code to suit a test.
//
// Node 24 strips TypeScript types natively, so nothing else is needed.
const LOADER = `
import { existsSync } from "node:fs";
const STUB = ${JSON.stringify(STUB)};
export async function resolve(specifier, context, next) {
  if (specifier === "expo-file-system") {
    return { url: "stub:expo-file-system", shortCircuit: true };
  }
  if (specifier.startsWith(".") && !/\\.[a-z]+$/.test(specifier)) {
    const candidate = new URL(specifier + ".ts", context.parentURL);
    if (existsSync(candidate)) {
      return next(specifier + ".ts", context);
    }
  }
  return next(specifier, context);
}
export async function load(url, context, next) {
  if (url === "stub:expo-file-system") {
    return { format: "module", source: STUB, shortCircuit: true };
  }
  return next(url, context);
}
`;

register(`data:text/javascript,${encodeURIComponent(LOADER)}`);

// Metro injects __DEV__ into every module; plain Node does not, and the app's
// dev-only logging branches reference it at call time.
globalThis.__DEV__ = false;

let store;

before(async () => {
  // new URL(...) against import.meta.url is already a file: URL — running it
  // back through pathToFileURL would double the Windows drive letter.
  store = await import(new URL("../rec-store.ts", import.meta.url).href);
});

beforeEach(() => {
  fsState.files.clear();
  fsState.dirs.clear();
  fsState.available = 10 * 1024 * 1024 * 1024;
  fsState.now = 1000;
});

/** A session with real-looking audio on disk. */
function seed(overrides = {}) {
  const s = {
    id: "s1",
    createdAt: 1000,
    state: "COMPLETED",
    upload: "LOCAL",
    fileUri: "file:///doc/minutex-recordings/s1.m4a",
    format: "m4a",
    duration: 60,
    size: 1_000_000,
    segments: [{ startedAt: 0, endedAt: 60_000 }],
    interruptions: [],
    attempts: 0,
    ...overrides,
  };
  if (s.fileUri) {
    fsState.files.set(s.fileUri, {
      text: "", size: s.size ?? 1_000_000, lastModified: 5000,
    });
  }
  return s;
}

// ---------------------------------------------------------------------------

describe("pause classification", () => {
  it("separates the user's pause from every involuntary one", () => {
    // THE distinction the whole auto-resume rule rests on. If PAUSED_BY_USER
    // ever tests true here, MinuteX would resume recording someone who
    // deliberately went off the record.
    assert.equal(store.isInvoluntaryPause("PAUSED_BY_USER"), false);
    for (const s of [
      "PAUSED_BY_CALL",
      "PAUSED_BY_AUDIO_INTERRUPTION",
      "PAUSED_BY_MICROPHONE",
    ]) {
      assert.equal(store.isInvoluntaryPause(s), true, s);
    }
  });

  it("counts every pause variant as paused, and nothing else", () => {
    for (const s of [
      "PAUSED_BY_USER", "PAUSED_BY_CALL",
      "PAUSED_BY_AUDIO_INTERRUPTION", "PAUSED_BY_MICROPHONE",
    ]) {
      assert.equal(store.isPaused(s), true, s);
    }
    for (const s of ["IDLE", "RECORDING", "FINALIZING", "COMPLETED", "ERROR"]) {
      assert.equal(store.isPaused(s), false, s);
    }
  });

  it("treats recording and any pause as an active session", () => {
    // Drives "is the audio file still open" and the navigation guard that lets
    // the user leave the screen without ending the recording.
    assert.equal(store.isActive("RECORDING"), true);
    assert.equal(store.isActive("PAUSED_BY_CALL"), true);
    assert.equal(store.isActive("PAUSED_BY_USER"), true);
    assert.equal(store.isActive("COMPLETED"), false);
    assert.equal(store.isActive("IDLE"), false);
  });
});

describe("duration accounting", () => {
  it("excludes paused stretches from the captured total", () => {
    // 10s recorded, 60s paused, 5s recorded = 15s of audio, not 75s of
    // wall clock. Getting this wrong is what makes a final duration disagree
    // with the audio the user actually gets.
    const s = seed({
      segments: [
        { startedAt: 0, endedAt: 10_000 },
        { startedAt: 70_000, endedAt: 75_000 },
      ],
    });
    assert.equal(store.segmentSeconds(s), 15);
  });

  it("counts an open segment up to now", () => {
    const s = seed({ segments: [{ startedAt: 1_000, endedAt: null }] });
    assert.equal(store.segmentSeconds(s, 9_000), 8);
  });

  it("prefers the recorder's own duration when the two agree", () => {
    // The recorder counts encoded audio, which is more accurate than our
    // wall-clock segments — so it wins whenever it is plausible.
    const s = seed({ segments: [{ startedAt: 0, endedAt: 60_000 }] });
    assert.equal(store.reconcileDuration(s, 61.5), 62);
  });

  it("rejects a recorder duration that contradicts the segments", () => {
    // Android accumulates `now - startTime` and can report a paused stretch as
    // recorded. 600s claimed against 60s of segments is not framing drift, so
    // the segment sum is the safer answer.
    const s = seed({ segments: [{ startedAt: 0, endedAt: 60_000 }] });
    assert.equal(store.reconcileDuration(s, 600), 60);
  });

  it("falls back to segments when the recorder reports nothing", () => {
    // A failed stop() often leaves durationMillis at 0. The audio is still
    // there, so reporting 0 seconds would misdescribe a real recording.
    const s = seed({ segments: [{ startedAt: 0, endedAt: 42_000 }] });
    for (const bogus of [null, undefined, 0, NaN, -5]) {
      assert.equal(store.reconcileDuration(s, bogus), 42, String(bogus));
    }
  });
});

describe("usable-audio detection", () => {
  it("rejects a header-only container", () => {
    // An .m4a that never got audio is a few hundred bytes and decodes to
    // silence. Uploading it would bill a transcription for nothing and show
    // the user an empty meeting.
    const s = seed({ size: 400 });
    fsState.files.set(s.fileUri, { text: "", size: 400, lastModified: 1 });
    assert.equal(store.hasUsableAudio(s), false);
  });

  it("accepts a real recording", () => {
    assert.equal(store.hasUsableAudio(seed({ size: 900_000 })), true);
  });

  it("rejects a session whose file is gone", () => {
    const s = seed();
    fsState.files.delete(s.fileUri);
    assert.equal(store.hasUsableAudio(s), false);
  });
});

describe("crash recovery", () => {
  it("finalizes a recording the app died during, keeping the audio", () => {
    // The core crash case: state says RECORDING, there is no live recorder,
    // and the audio on disk is whatever the encoder flushed. It must come back
    // as an uploadable COMPLETED recording, not an error over good audio.
    store.saveSession(seed({
      state: "RECORDING",
      upload: "LOCAL",
      segments: [{ startedAt: 0, endedAt: null }],
    }));
    const { interrupted, pendingUpload } = store.recoverSessions();
    assert.equal(interrupted.length, 1);
    assert.equal(pendingUpload.length, 0);
    assert.equal(interrupted[0].state, "COMPLETED");
    assert.equal(interrupted[0].upload, "LOCAL");
    // The open segment was closed, so a duration exists.
    assert.equal(interrupted[0].segments[0].endedAt !== null, true);
    assert.ok(interrupted[0].duration >= 0);
  });

  it("closes an open interruption when recovering", () => {
    store.saveSession(seed({
      state: "PAUSED_BY_CALL",
      segments: [{ startedAt: 0, endedAt: 3_000 }],
      interruptions: [{ reason: "call", startedAt: 3_000, endedAt: null }],
    }));
    const { interrupted } = store.recoverSessions();
    assert.equal(interrupted.length, 1);
    assert.equal(interrupted[0].interruptions[0].endedAt !== null, true);
  });

  it("reports an interrupted session with no audio as an error", () => {
    // Nothing to recover — say so, rather than create a 0-byte recording the
    // user will find later and not understand.
    const s = seed({ state: "RECORDING", size: 100 });
    fsState.files.set(s.fileUri, { text: "", size: 100, lastModified: 1 });
    store.saveSession(s);
    const { interrupted } = store.recoverSessions();
    assert.equal(interrupted.length, 0);
    assert.equal(store.loadSession("s1").state, "ERROR");
  });

  it("re-queues a finalized recording that never uploaded", () => {
    // The ordinary offline case: recorded, stopped, no network.
    store.saveSession(seed({ state: "COMPLETED", upload: "LOCAL" }));
    const { pendingUpload } = store.recoverSessions();
    assert.equal(pendingUpload.length, 1);
    assert.equal(pendingUpload[0].id, "s1");
  });

  it("resets a session caught mid-upload so it retries", () => {
    // UPLOADING is a lie after a restart — there is no live task. It has to
    // become retryable or the recording is stranded forever.
    store.saveSession(seed({ state: "COMPLETED", upload: "UPLOADING" }));
    const { pendingUpload } = store.recoverSessions();
    assert.equal(pendingUpload.length, 1);
    assert.equal(pendingUpload[0].upload, "LOCAL");
  });

  it("leaves confirmed sessions alone", () => {
    store.saveSession(seed({ upload: "PROCESSING", confirmedAt: 123 }));
    const { interrupted, pendingUpload } = store.recoverSessions();
    assert.equal(interrupted.length + pendingUpload.length, 0);
  });

  it("flags a finalized session whose audio has vanished", () => {
    const s = seed({ state: "COMPLETED", upload: "LOCAL" });
    store.saveSession(s);
    fsState.files.delete(s.fileUri);
    const { pendingUpload } = store.recoverSessions();
    assert.equal(pendingUpload.length, 0);
    assert.equal(store.loadSession("s1").state, "ERROR");
  });
});

describe("orphan audio sweep", () => {
  it("adopts audio whose sidecar was lost", () => {
    // A sidecar truncated by a kill leaves audio that listSessions() cannot
    // see. Intact audio is worth rescuing even with poorer metadata.
    store.ensureDir();
    fsState.files.set("file:///doc/minutex-recordings/orphan.m4a", {
      text: "", size: 2_000_000, lastModified: 7777,
    });
    const adopted = store.sweepOrphanAudio();
    assert.equal(adopted.length, 1);
    assert.equal(adopted[0].id, "orphan");
    assert.equal(adopted[0].format, "m4a");
    assert.equal(adopted[0].state, "COMPLETED");
    assert.equal(adopted[0].upload, "LOCAL");
    // And it is now a real session on disk.
    assert.equal(store.loadSession("orphan") !== null, true);
  });

  it("discards a header-only orphan instead of surfacing an empty recording", () => {
    store.ensureDir();
    fsState.files.set("file:///doc/minutex-recordings/tiny.m4a", {
      text: "", size: 300, lastModified: 1,
    });
    assert.equal(store.sweepOrphanAudio().length, 0);
    assert.equal(fsState.files.has("file:///doc/minutex-recordings/tiny.m4a"), false);
  });

  it("ignores audio that already has a session", () => {
    store.saveSession(seed());
    assert.equal(store.sweepOrphanAudio().length, 0);
  });
});

describe("pruning — the only thing allowed to delete audio", () => {
  it("deletes audio the server confirmed", () => {
    store.saveSession(seed({ upload: "PROCESSING", confirmedAt: 999 }));
    const freed = store.pruneUploaded();
    assert.equal(freed, 1_000_000);
    assert.equal(fsState.files.has("file:///doc/minutex-recordings/s1.m4a"), false);
  });

  it("keeps audio that failed to upload", () => {
    // The requirement stated most plainly: a temporary failure must not delete
    // an already-created recording.
    const s = seed({ upload: "UPLOAD_FAILED", error: "network" });
    store.saveSession(s);
    assert.equal(store.pruneUploaded(), 0);
    assert.equal(fsState.files.has(s.fileUri), true);
  });

  it("keeps audio marked uploaded but never confirmed", () => {
    // Without confirmedAt there is no proof the server has the bytes, and a
    // status field alone is not proof.
    store.saveSession(seed({ upload: "UPLOADED", confirmedAt: undefined }));
    assert.equal(store.pruneUploaded(), 0);
    assert.equal(fsState.files.has("file:///doc/minutex-recordings/s1.m4a"), true);
  });

  it("keeps local audio that hasn't been uploaded at all", () => {
    store.saveSession(seed({ upload: "LOCAL" }));
    assert.equal(store.pruneUploaded(), 0);
  });
});

describe("storage headroom", () => {
  it("reads available space", () => {
    fsState.available = 123_456;
    assert.equal(store.freeBytes(), 123_456);
  });

  it("orders the thresholds so each one can actually fire", () => {
    // start > warn > critical. If these ever cross, either the warning never
    // shows or recording refuses to start while claiming there is room.
    assert.ok(store.MIN_FREE_BYTES_TO_START > store.LOW_FREE_BYTES_WARN);
    assert.ok(store.LOW_FREE_BYTES_WARN > store.CRITICAL_FREE_BYTES);
    assert.ok(store.CRITICAL_FREE_BYTES > 0);
  });
});

describe("session identity", () => {
  it("mints unique, filename-safe ids", () => {
    const ids = new Set();
    for (let i = 0; i < 500; i++) ids.add(store.newSessionId());
    assert.equal(ids.size, 500);
    for (const id of ids) assert.match(id, /^[a-z0-9-]+$/);
  });

  it("persists and reloads a session unchanged", () => {
    const s = seed({ interruptions: [{ reason: "call", startedAt: 1, endedAt: 2 }] });
    store.saveSession(s);
    assert.deepEqual(store.loadSession("s1"), s);
  });

  it("lists sessions newest first", () => {
    store.saveSession(seed({ id: "old", createdAt: 100, fileUri: null }));
    store.saveSession(seed({ id: "new", createdAt: 900, fileUri: null }));
    assert.deepEqual(store.listSessions().map((s) => s.id), ["new", "old"]);
  });

  it("survives a corrupt sidecar without losing the readable ones", () => {
    // A kill mid-write leaves unparseable JSON. It must not take out the whole
    // listing — the other recordings are still perfectly good.
    store.saveSession(seed({ id: "good", fileUri: null }));
    store.ensureDir();
    fsState.files.set("file:///doc/minutex-recordings/bad.json", {
      text: "{not json", size: 9, lastModified: 1,
    });
    assert.deepEqual(store.listSessions().map((s) => s.id), ["good"]);
  });

  it("deletes both the sidecar and the audio", () => {
    const s = seed();
    store.saveSession(s);
    store.deleteSession("s1", "m4a");
    assert.equal(store.loadSession("s1"), null);
    assert.equal(fsState.files.has(s.fileUri), false);
  });
});

// ---------------------------------------------------------------------------

describe("segment joining after an interruption", () => {
  // WHY THIS MATTERS MOST
  // A phone call forces a new recorder and therefore a second audio file (a
  // MediaRecorder that lost the mic encodes silence forever after, so it can't
  // be reused). Those pieces have to come back as ONE file, in order, because
  // the upload pipeline carries one file per recording. Every assertion here
  // is about not losing the second half of somebody's meeting.
  beforeEach(() => {
    fsState.files.clear();
    fsState.dirs.clear();
    delete fsState.failWritesTo;
    store.ensureDir();
  });

  /** A session record with NO filesystem side effects (unlike seed()). */
  function sess(overrides = {}) {
    return {
      id: "s1", createdAt: 1000, state: "COMPLETED", upload: "LOCAL",
      fileUri: null, format: "aac", duration: 60, size: null,
      segments: [], interruptions: [], attempts: 0, ...overrides,
    };
  }

  /** Write a file whose bytes are a recognisable marker, so order is checkable. */
  function part(uri, marker, len = 64) {
    const bytes = Array.from({ length: len }, () => marker);
    fsState.files.set(uri, {
      text: Buffer.from(bytes).toString(), bytes, size: len, lastModified: 5000,
    });
    return uri;
  }

  it("returns the single file untouched when there was no interruption", async () => {
    const s = sess({ format: "aac", fileUri: "file:///doc/minutex-recordings/s1.aac" });
    const r = await store.concatSegments(s);
    assert.equal(r.merged, false);
    assert.equal(r.uri, s.fileUri);
  });

  it("joins the pieces in CAPTURE order, earliest first", async () => {
    const a = part("file:///doc/minutex-recordings/s1.aac", 0xAA);
    const b = part("file:///doc/minutex-recordings/s1-2.aac", 0xBB);
    // extraFiles holds the EARLIER audio; fileUri is always the live one.
    const s = sess({ format: "aac", fileUri: b, extraFiles: [a] });

    const r = await store.concatSegments(s);
    assert.equal(r.merged, true);
    const out = fsState.files.get(r.uri);
    assert.equal(out.size, 128, "both parts present");
    // Order is the assertion: the pre-call audio must come first.
    assert.equal(out.bytes[0], 0xAA);
    assert.equal(out.bytes[127], 0xBB);
  });

  it("joins three pieces — two interruptions in one meeting", async () => {
    const a = part("file:///doc/minutex-recordings/s1.aac", 0x11, 10);
    const b = part("file:///doc/minutex-recordings/s1-2.aac", 0x22, 10);
    const c = part("file:///doc/minutex-recordings/s1-3.aac", 0x33, 10);
    const s = sess({ format: "aac", fileUri: c, extraFiles: [a, b] });

    const r = await store.concatSegments(s);
    assert.equal(r.merged, true);
    const out = fsState.files.get(r.uri);
    assert.equal(out.size, 30);
    assert.deepEqual(
      [out.bytes[0], out.bytes[10], out.bytes[20]],
      [0x11, 0x22, 0x33],
      "all three in order"
    );
  });

  it("REFUSES to byte-join m4a, because that would silently truncate", async () => {
    // MPEG-4 keeps a moov atom per file: concatenating two of them yields
    // something players read as only the first. Falling back to the longest
    // single part is worse audio but HONEST audio — and it's flagged.
    const a = part("file:///doc/minutex-recordings/s1.m4a", 0xAA, 100);
    const b = part("file:///doc/minutex-recordings/s1-2.m4a", 0xBB, 40);
    const s = sess({ format: "m4a", fileUri: b, extraFiles: [a] });

    const r = await store.concatSegments(s);
    assert.equal(r.merged, false);
    assert.ok(r.error, "reports why it declined");
    assert.equal(r.uri, a, "falls back to the LONGEST part");
  });

  it("never deletes the originals when the join fails", async () => {
    // A failed merge must not be how a recording gets lost.
    const a = part("file:///doc/minutex-recordings/s1.aac", 0xAA, 100);
    const b = part("file:///doc/minutex-recordings/s1-2.aac", 0xBB, 30);
    const s = sess({ format: "aac", fileUri: b, extraFiles: [a] });
    fsState.failWritesTo = "file:///doc/minutex-recordings/s1-merged.aac";

    const r = await store.concatSegments(s);
    assert.equal(r.merged, false);
    assert.equal(fsState.files.has(a), true, "part A survives");
    assert.equal(fsState.files.has(b), true, "part B survives");
    assert.equal(r.uri, a, "hands back the biggest surviving part");
  });

  it("skips a zero-byte part rather than failing the whole join", async () => {
    const a = part("file:///doc/minutex-recordings/s1.aac", 0xAA, 50);
    const empty = "file:///doc/minutex-recordings/s1-2.aac";
    fsState.files.set(empty, { text: "", bytes: [], size: 0, lastModified: 1 });
    const c = part("file:///doc/minutex-recordings/s1-3.aac", 0xCC, 50);
    const s = sess({ format: "aac", fileUri: c, extraFiles: [a, empty] });

    const r = await store.concatSegments(s);
    assert.equal(r.merged, true);
    assert.equal(fsState.files.get(r.uri).size, 100);
  });

  it("tolerates a part that vanished from disk", async () => {
    const a = part("file:///doc/minutex-recordings/s1.aac", 0xAA, 40);
    const gone = "file:///doc/minutex-recordings/s1-gone.aac";
    const c = part("file:///doc/minutex-recordings/s1-3.aac", 0xCC, 40);
    const s = sess({ format: "aac", fileUri: c, extraFiles: [a, gone] });

    const r = await store.concatSegments(s);
    assert.equal(r.merged, true, "still produces the audio it does have");
    assert.equal(fsState.files.get(r.uri).size, 80);
  });
});

describe("orphan sweep vs. merged files", () => {
  beforeEach(() => {
    fsState.files.clear();
    fsState.dirs.clear();
    store.ensureDir();
  });

  it("does not adopt a merged file as a second recording", () => {
    // concatSegments writes {id}-merged.aac next to the parts. Adopting it
    // would show the user the same meeting twice and upload it twice.
    const s = seed({ id: "s1", format: "aac", fileUri: "file:///doc/minutex-recordings/s1.aac" });
    store.saveSession(s);
    fsState.files.set("file:///doc/minutex-recordings/s1-merged.aac", {
      text: "", bytes: [], size: 500_000, lastModified: 6000,
    });

    const adopted = store.sweepOrphanAudio();
    assert.deepEqual(adopted.map((a) => a.id), [], "nothing adopted");
  });

  it("still rescues a merged file whose session record is gone", () => {
    // The crash-safety case: if the sidecar was lost, the audio must not be.
    fsState.files.set("file:///doc/minutex-recordings/lost-merged.aac", {
      text: "", bytes: [], size: 500_000, lastModified: 6000,
    });
    const adopted = store.sweepOrphanAudio();
    assert.equal(adopted.length, 1);
    assert.equal(adopted[0].format, "aac");
  });
});
