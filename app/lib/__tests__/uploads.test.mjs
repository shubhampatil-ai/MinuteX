// lib/__tests__/uploads.test.mjs — retry-classification and backoff tests.
//
// Run:  node --test lib/__tests__/uploads.test.mjs
//
// WHY THESE TWO FUNCTIONS
// isTransient() decides whether a failed upload waits and tries again, or
// stops and asks the user. Get it wrong in one direction and a phone that is
// simply offline reports the meeting as failed; wrong in the other and the app
// retries an oversize file or a dead session forever, burning battery for a
// result that can never change. It is a pure function of the error, so it is
// exactly the thing to pin in a unit test.
//
// The pipeline around it (presign reuse, sidecar updates, pruning) is covered
// by rec-store.test.mjs at the data-model level and by the manual device tests
// end-to-end; simulating expo's native uploader here would test the mock.
import { describe, it, before } from "node:test";
import assert from "node:assert/strict";
import { register } from "node:module";

// react-native and expo-file-system have no meaning outside a device runtime,
// and lib/uploads.tsx pulls both in at module load (AppState listener, File).
// Stubbed to the smallest surface that lets the module evaluate.
const RN_STUB = `
export const AppState = { addEventListener: () => ({ remove() {} }) };
export const Platform = { OS: "android" };
`;
const FS_STUB = `
export class File { constructor() {} get exists() { return false; } get size() { return 0; } }
export class Directory { constructor() {} get exists() { return false; } create() {} list() { return []; } }
export const Paths = { document: {}, cache: {}, availableDiskSpace: 1e10, totalDiskSpace: 1e11 };
export const UploadType = { BINARY_CONTENT: 0, MULTIPART: 1 };
`;
const REACT_STUB = `
export function useSyncExternalStore(sub, get) { return get(); }
export default { useSyncExternalStore };
`;

const LOADER = `
import { existsSync } from "node:fs";
const STUBS = {
  "react-native": ${JSON.stringify(RN_STUB)},
  "expo-file-system": ${JSON.stringify(FS_STUB)},
  "react": ${JSON.stringify(REACT_STUB)},
};
export async function resolve(specifier, context, next) {
  if (STUBS[specifier]) return { url: "stub:" + specifier, shortCircuit: true };
  // Metro resolves extensionless relative imports; Node's ESM resolver needs
  // the real extension. Try .ts then .tsx so app source runs unmodified.
  if (specifier.startsWith(".") && !/\\.[a-z]+$/.test(specifier)) {
    for (const ext of [".ts", ".tsx"]) {
      if (existsSync(new URL(specifier + ext, context.parentURL))) {
        return next(specifier + ext, context);
      }
    }
  }
  return next(specifier, context);
}
export async function load(url, context, next) {
  if (url.startsWith("stub:")) {
    return { format: "module", source: STUBS[url.slice(5)], shortCircuit: true };
  }
  // Node's type-stripping handles .ts but refuses .tsx outright. lib/uploads.tsx
  // carries the .tsx extension for consistency with the other lib/ modules that
  // export components, but contains no JSX of its own — so it is served here as
  // plain TypeScript rather than being renamed to suit the test runner.
  if (url.endsWith(".tsx")) {
    const r = await next(url, { ...context, format: "module-typescript" });
    return { ...r, format: "module-typescript" };
  }
  return next(url, context);
}
`;

register(`data:text/javascript,${encodeURIComponent(LOADER)}`);

// Metro injects __DEV__ into every module; plain Node does not, and the app's
// dev-only logging branches reference it at call time.
globalThis.__DEV__ = false;

let uploads, api;

before(async () => {
  uploads = await import(new URL("../uploads.tsx", import.meta.url).href);
  api = await import(new URL("../api.ts", import.meta.url).href);
});

describe("retry classification", () => {
  it("retries a network failure", () => {
    // ApiError status 0 is what lib/api.ts throws when fetch itself fails —
    // offline, DNS, airplane mode. THE case that must never look like a
    // permanent failure, because the recording is fine and the user just
    // walked out of signal.
    assert.equal(uploads.isTransient(new api.ApiError(0, "Network error")), true);
  });

  it("retries server-side errors", () => {
    for (const status of [500, 502, 503, 504]) {
      assert.equal(
        uploads.isTransient(new api.ApiError(status, "server")),
        true,
        String(status)
      );
    }
  });

  it("retries throttling and timeouts", () => {
    assert.equal(uploads.isTransient(new api.ApiError(429, "slow down")), true);
    assert.equal(uploads.isTransient(new api.ApiError(408, "timeout")), true);
  });

  it("does NOT retry a dead session", () => {
    // api.ts has already cleared the token by this point; retrying would spin
    // on 401 forever and never tell the user to sign in again.
    assert.equal(uploads.isTransient(new api.ApiError(401, "unauthorized")), false);
  });

  it("does NOT retry a rejected request", () => {
    // 400/403/413 mean the request itself is wrong — an oversize file, a bad
    // signature. Retrying cannot change the outcome.
    for (const status of [400, 403, 404, 413, 422]) {
      assert.equal(
        uploads.isTransient(new api.ApiError(status, "rejected")),
        false,
        String(status)
      );
    }
  });

  it("retries a bare Error from the native uploader", () => {
    // A connection dropped mid-PUT surfaces as a plain Error. Treated as
    // transient — a genuinely unusable file is caught by the pre-flight
    // existence check instead, which throws before any attempt is made.
    assert.equal(uploads.isTransient(new Error("socket closed")), true);
  });

  it("does not retry a non-error value", () => {
    assert.equal(uploads.isTransient("nope"), false);
    assert.equal(uploads.isTransient(null), false);
    assert.equal(uploads.isTransient(undefined), false);
  });
});

describe("backoff", () => {
  it("grows with each attempt", () => {
    const delays = [0, 1, 2, 3, 4, 5].map(uploads.backoffFor);
    for (let i = 1; i < delays.length; i++) {
      assert.ok(delays[i] > delays[i - 1], `${delays[i - 1]} -> ${delays[i]}`);
    }
  });

  it("caps rather than growing without bound", () => {
    // An hour offline must not push the next attempt 40 minutes out — the
    // user is waiting for their meeting to appear.
    const cap = uploads.backoffFor(5);
    for (const n of [6, 20, 500]) {
      assert.equal(uploads.backoffFor(n), cap, String(n));
    }
    assert.ok(cap <= 5 * 60_000, "cap should stay within a few minutes");
  });

  it("starts quickly", () => {
    // The first retry covers a momentary blip and should be near-immediate.
    assert.ok(uploads.backoffFor(0) <= 3_000);
  });
});

describe("file validation", () => {
  it("accepts every format the backend accepts", () => {
    for (const ext of Object.keys(uploads.UPLOAD_FORMATS)) {
      assert.equal(uploads.validateAudioFile({ name: `a.${ext}`, size: 1000 }), ext);
    }
  });

  it("normalizes case", () => {
    assert.equal(uploads.validateAudioFile({ name: "MEETING.M4A", size: 10 }), "m4a");
  });

  it("rejects a non-audio file", () => {
    assert.throws(() => uploads.validateAudioFile({ name: "notes.pdf", size: 10 }), /Unsupported format/);
  });

  it("rejects a file with no extension", () => {
    assert.throws(() => uploads.validateAudioFile({ name: "recording", size: 10 }), /Unsupported format/);
  });

  it("rejects an oversize file before any bytes move", () => {
    assert.throws(
      () => uploads.validateAudioFile({ name: "big.wav", size: uploads.MAX_UPLOAD_BYTES + 1 }),
      /too large/
    );
  });

  it("accepts a file exactly at the limit", () => {
    // The backend's check is `>`, so the boundary value must pass here too or
    // the client would reject uploads the server would have taken.
    assert.equal(
      uploads.validateAudioFile({ name: "big.wav", size: uploads.MAX_UPLOAD_BYTES }),
      "wav"
    );
  });

  it("allows an unknown size", () => {
    // Some Android providers report no size. Not knowing is not a reason to
    // refuse — the backend checks again with the real byte count.
    assert.equal(uploads.validateAudioFile({ name: "a.mp3", size: null }), "mp3");
  });

  it("keeps the limits aligned with the transcription provider's", () => {
    assert.equal(uploads.MAX_UPLOAD_BYTES, 3 * 1000 * 1000 * 1000);
    assert.equal(uploads.MAX_DURATION_SECONDS, 36000);
  });
});
