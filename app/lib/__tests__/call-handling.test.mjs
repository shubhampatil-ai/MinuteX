// lib/__tests__/call-handling.test.mjs — ring-vs-answer decision tests.
//
// Run:  node --test lib/__tests__/call-handling.test.mjs
//
// WHAT THIS PINS AND WHY
//
// A phone call reaches the recorder as an audio-focus loss. The problem is that
// a RINGING phone and an ANSWERED one raise the SAME AUDIOFOCUS_LOSS_TRANSIENT,
// so the focus event alone cannot tell them apart — and the correct response is
// opposite in each case:
//
//   ringing  -> KEEP RECORDING. Most rings are ignored or rejected, and the mic
//               is still ours. Pausing loses the seconds around the ring for
//               nothing.
//   answered -> PAUSE. The telephony stack now owns the mic, and on Android
//               MediaRecorder responds by writing silence rather than erroring,
//               so failing to pause means minutes of dead audio.
//
// The decision therefore rests on getCallPhase(), and these tests pin that
// decision table plus the three guards that depend on it. They test the RULES
// as pure functions of (focus change, call phase) rather than driving the real
// controller: the controller needs expo-audio, a live recorder and real timers,
// and a test that stubbed all three would be asserting against the stubs.
//
// The rules under test are mirrored here from lib/rec-controller.ts. If that
// file's logic changes, these must be updated in step — which is the point:
// they are a written-down statement of intended behaviour, and a change that
// breaks one should have to justify itself.
import { describe, it } from "node:test";
import assert from "node:assert/strict";

// --- the rules, as implemented in lib/rec-controller.ts --------------------

/** Should a focus-loss event pause the recording? (startFocusWatch) */
function shouldPauseOnFocusLoss(change, phase, state = "RECORDING") {
  if (state !== "RECORDING") return false;
  // The ring-aware branch: a transient loss while merely ringing is watched,
  // not acted on.
  if (change === "loss_transient" && phase === "ringing") return false;
  // Ducking never pauses a recording — nothing is playing and the mic is fine.
  if (change === "loss_transient_can_duck") return false;
  if (change === "gain" || change === "unknown") return false;
  return true;
}

/** What watchRingingCall does on each poll. */
function ringWatchAction(phase, elapsedMs, maxMs = 90_000) {
  if (phase === "in_call") return "pause";
  if (phase === "idle") return "stop_watching";
  if (elapsedMs > maxMs) return "stop_watching";
  return "keep_watching";
}

/** Is an auto-resume allowed right now? (attemptResume) */
function mayResume({ phase, inCall, focusLost, userWantsRecording }) {
  if (!userWantsRecording) return false;
  // Any live call activity blocks — including a ring, because an answer may
  // land a fraction of a second later and resuming into it produces exactly
  // the silent segment the guard exists to prevent.
  if (phase !== "idle" || inCall) return false;
  if (focusLost) return false;
  return true;
}

/** Should sustained digital silence pause the recording? (reconcile) */
function shouldPauseOnSilence(phase, ticks, threshold = 12) {
  if (ticks < threshold) return false;
  // A ringing phone is the one case where digital zero is not proof the mic is
  // gone for good — some ROMs mute capture for the ringtone and hand it back.
  if (phase === "ringing") return false;
  return true;
}

/**
 * Should the NATIVE capture layer's verdict pause the recording? (reconcile)
 *
 * Distinct from shouldPauseOnSilence above, and deliberately NOT subject to the
 * ringing exemption. The two differ in the strength of their evidence:
 *
 *   metering-based (above)  sampled twice a second across the bridge, over a
 *                           level that some ROMs legitimately zero while a
 *                           ringtone plays — so a ring earns the benefit of
 *                           the doubt.
 *   native flag (here)      every PCM frame inspected in the capture loop, and
 *                           only latched after ~2.5s of unbroken exact zero. A
 *                           ringing-but-unanswered phone does not produce that.
 *
 * Honouring the ring exemption here would mean an answered call whose ring the
 * platform still reports never pauses at all — the exact bug this replaced.
 * Only the WAV engine can supply the flag; AAC leaves it undefined, which must
 * read as "no opinion", never as "the mic is fine".
 */
function shouldPauseOnNativeVerdict(micUnavailable, phase, state = "RECORDING") {
  if (state !== "RECORDING") return false;
  if (micUnavailable !== true) return false;   // undefined = engine has no opinion
  return true;                                  // phase is deliberately ignored
}

/**
 * The native zero-run detector itself (WavRecorder.captureLoop).
 *
 * `peak` is the peak ABSOLUTE 16-bit sample of one read() buffer, so 0 means
 * every sample in it was exactly 0x0000. Returns whether the latch has tripped.
 */
function makeZeroRunDetector({
  zeroRunMillis = 2500,
  zeroToleranceSamples = 0,
  byteRate = 16000 * 2,        // 16 kHz, mono, 16-bit
} = {}) {
  const limit = zeroRunMillis > 0
    ? Math.floor((byteRate * zeroRunMillis) / 1000)
    : 0;
  let zeroRunBytes = 0;
  let micUnavailable = false;
  return {
    limit,
    feed(peak, nBytes) {
      if (limit > 0 && peak <= zeroToleranceSamples) {
        zeroRunBytes += nBytes;
        if (!micUnavailable && zeroRunBytes >= limit) micUnavailable = true;
      } else {
        // Any real signal breaks the run. The latch is NOT cleared: only
        // building a new recorder (or resume()) does that.
        zeroRunBytes = 0;
      }
      return micUnavailable;
    },
    get tripped() { return micUnavailable; },
  };
}

// --- tests ----------------------------------------------------------------

describe("focus loss: ring vs answered call", () => {
  it("does NOT pause while the phone is merely ringing", () => {
    assert.equal(shouldPauseOnFocusLoss("loss_transient", "ringing"), false);
  });

  it("DOES pause once the call is answered", () => {
    assert.equal(shouldPauseOnFocusLoss("loss_transient", "in_call"), true);
  });

  it("pauses on a transient loss that is not a call at all", () => {
    // Another app grabbed the mic. No call, so nothing to wait for.
    assert.equal(shouldPauseOnFocusLoss("loss_transient", "idle"), true);
  });

  it("pauses on a permanent loss even while ringing", () => {
    // AUDIOFOCUS_LOSS means the mic is gone for good; the ring is incidental
    // and waiting for it to resolve would just delay the inevitable.
    assert.equal(shouldPauseOnFocusLoss("loss", "ringing"), true);
  });

  it("never pauses for ducking", () => {
    // A notification chime must not stop a meeting.
    for (const phase of ["idle", "ringing", "in_call"]) {
      assert.equal(
        shouldPauseOnFocusLoss("loss_transient_can_duck", phase), false, phase);
    }
  });

  it("ignores focus events when not recording", () => {
    assert.equal(
      shouldPauseOnFocusLoss("loss_transient", "in_call", "PAUSED_BY_USER"),
      false);
  });

  it("falls back to pausing when the phase is unknowable", () => {
    // getCallPhase() returns "idle" when the native module is absent (iOS,
    // Expo Go, an old dev client). The old behaviour — pause on transient
    // loss — must still hold there, so nothing regresses on those builds.
    assert.equal(shouldPauseOnFocusLoss("loss_transient", "idle"), true);
  });
});

describe("native zero-run detection (WAV engine)", () => {
  // One read() is ~1/4 second of audio: readBufferBytes = oneSecond / 4.
  const BYTE_RATE = 16000 * 2;
  const BUF = BYTE_RATE / 4;
  /** Feed `count` buffers all at peak `peak`. */
  const feed = (d, peak, count) => {
    for (let i = 0; i < count; i++) d.feed(peak, BUF);
    return d.tripped;
  };

  it("threshold is exactly 2.5 seconds of audio", () => {
    const d = makeZeroRunDetector();
    assert.equal(d.limit, 80_000);
    assert.equal(d.limit / BYTE_RATE, 2.5);
  });

  it("detects a mic taken by a call", () => {
    // The failure this exists for: read() keeps succeeding with zero buffers.
    assert.equal(feed(makeZeroRunDetector(), 0, 40), true);
  });

  it("does not trip one buffer before the threshold", () => {
    const d = makeZeroRunDetector();
    assert.equal(feed(d, 0, 9), false, "2.25s must not be enough");
    assert.equal(d.feed(0, BUF), true, "the 10th buffer reaches 2.5s");
  });

  // The false-positive requirement: legitimate quiet must never trip this.
  it("never trips on a quiet room with a real noise floor", () => {
    for (const floor of [1, 2, 3, 10, 30]) {
      assert.equal(
        feed(makeZeroRunDetector(), floor, 240),   // a full minute
        false,
        `peak ±${floor} is a live mic, not a stolen one`
      );
    }
  });

  it("never trips on speech separated by long pauses", () => {
    const d = makeZeroRunDetector();
    // 2s of digital zero, then a word, repeated. Each word resets the run, so
    // the threshold is never reached even though most of the audio is silent.
    for (let i = 0; i < 30; i++) {
      feed(d, 0, 8);        // 2.0s — just under
      feed(d, 5000, 2);     // a word
    }
    assert.equal(d.tripped, false);
  });

  it("tolerates a start-up or route-switch gap", () => {
    // Some ROMs deliver a few hundred ms of zeros before the first real frames,
    // and a device switch does the same. Both are shorter than the threshold.
    const d = makeZeroRunDetector();
    feed(d, 0, 6);          // 1.5s of warm-up zeros
    feed(d, 900, 20);       // real audio arrives
    assert.equal(d.tripped, false);
  });

  it("stays latched once tripped, even if zeros stop", () => {
    // The controller must roll to a NEW segment; a flag that cleared itself
    // would let a recorder still bound to a stolen input look recovered.
    const d = makeZeroRunDetector();
    feed(d, 0, 12);
    assert.equal(d.tripped, true);
    feed(d, 4000, 20);
    assert.equal(d.tripped, true, "only a new recorder clears the latch");
  });

  it("is configurable: zeroRunMillis changes the threshold", () => {
    const fast = makeZeroRunDetector({ zeroRunMillis: 1000 });
    assert.equal(feed(fast, 0, 4), true, "1s of zeros trips a 1000ms detector");
    const slow = makeZeroRunDetector({ zeroRunMillis: 5000 });
    assert.equal(feed(slow, 0, 12), false, "3s must not trip a 5000ms detector");
  });

  it("is configurable: zero tolerance covers ROMs that emit ±1", () => {
    // A few ROMs hand back a DC-corrected stream at a constant ±1 rather than
    // true zero. Strict mode ignores it; tolerance 1 catches it.
    assert.equal(feed(makeZeroRunDetector(), 1, 40), false);
    assert.equal(
      feed(makeZeroRunDetector({ zeroToleranceSamples: 1 }), 1, 40), true);
  });

  it("can be disabled entirely", () => {
    const off = makeZeroRunDetector({ zeroRunMillis: 0 });
    assert.equal(feed(off, 0, 400), false, "100s of zeros, detector disabled");
  });

  it("threshold follows the format, not a hard-coded byte count", () => {
    // 48 kHz stereo has 6x the byte rate; 2.5s must still mean 2.5s.
    const hi = makeZeroRunDetector({ byteRate: 48000 * 2 * 2 });
    assert.equal(hi.limit / (48000 * 2 * 2), 2.5);
  });
});

describe("the native verdict overrides the ring exemption", () => {
  it("pauses on the native flag even while the phone reports ringing", () => {
    // The bug being fixed: an answered call on a ROM that still reports
    // MODE_RINGTONE previously never paused, because the ring exemption
    // suppressed the only detector that could see it.
    assert.equal(shouldPauseOnNativeVerdict(true, "ringing"), true);
    assert.equal(shouldPauseOnSilence("ringing", 20), false,
      "the metering path still defers to the ring, by design");
  });

  it("pauses regardless of what the call phase claims", () => {
    for (const phase of ["idle", "ringing", "in_call"]) {
      assert.equal(shouldPauseOnNativeVerdict(true, phase), true, phase);
    }
  });

  it("treats an absent flag as no opinion, not as a healthy mic", () => {
    // The AAC engine cannot see frames and never sets this.
    assert.equal(shouldPauseOnNativeVerdict(undefined, "in_call"), false);
    assert.equal(shouldPauseOnNativeVerdict(false, "in_call"), false);
    // ...and the metering detector still covers that engine.
    assert.equal(shouldPauseOnSilence("in_call", 12), true);
  });

  it("ignores the flag when not recording", () => {
    assert.equal(
      shouldPauseOnNativeVerdict(true, "in_call", "PAUSED_BY_USER"), false);
  });
});

describe("ring watch resolves all three outcomes", () => {
  it("pauses the moment the call is answered", () => {
    assert.equal(ringWatchAction("in_call", 1_200), "pause");
  });

  it("stops watching when the ring is rejected or missed", () => {
    assert.equal(ringWatchAction("idle", 4_000), "stop_watching");
  });

  it("keeps watching while it is still ringing", () => {
    assert.equal(ringWatchAction("ringing", 4_000), "keep_watching");
  });

  it("gives up rather than polling forever", () => {
    // A poller that outlived the ring would keep a timer alive for the rest of
    // a multi-hour recording.
    assert.equal(ringWatchAction("ringing", 90_001), "stop_watching");
  });

  it("answering still wins past the timeout", () => {
    // Order matters: the answer check runs before the deadline check, so a
    // late answer pauses rather than being dropped.
    assert.equal(ringWatchAction("in_call", 120_000), "pause");
  });
});

describe("auto-resume is blocked until the call is really over", () => {
  const base = { phase: "idle", inCall: false, focusLost: false,
                 userWantsRecording: true };

  it("resumes when everything is clear", () => {
    assert.equal(mayResume(base), true);
  });

  it("does not resume while still in the call", () => {
    assert.equal(mayResume({ ...base, phase: "in_call", inCall: true }), false);
  });

  it("does not resume while a NEW call is ringing", () => {
    // The window this closes: pausing for call A, A ends, B starts ringing.
    // Resuming into B's imminent answer would rebuild the recorder against a
    // mic that is about to be taken.
    assert.equal(mayResume({ ...base, phase: "ringing" }), false);
  });

  it("does not resume while focus is still lost", () => {
    assert.equal(mayResume({ ...base, focusLost: true }), false);
  });

  it("does not resume a recording the user paused themselves", () => {
    assert.equal(mayResume({ ...base, userWantsRecording: false }), false);
  });
});

describe("silence detection defers to the ring", () => {
  it("pauses on sustained digital silence when no call is involved", () => {
    assert.equal(shouldPauseOnSilence("idle", 12), true);
  });

  it("does NOT pause on digital silence while ringing", () => {
    // Otherwise a ROM that mutes capture for the ringtone would defeat the
    // ring-aware handling — the recording would stop on the ring after all,
    // six seconds later and mislabelled as a dead mic.
    assert.equal(shouldPauseOnSilence("ringing", 12), false);
    assert.equal(shouldPauseOnSilence("ringing", 999), false);
  });

  it("still pauses on digital silence once a call is answered", () => {
    assert.equal(shouldPauseOnSilence("in_call", 12), true);
  });

  it("needs a sustained run, not one quiet tick", () => {
    // A real room goes quiet. Pausing a meeting because nobody spoke would be
    // far worse than the bug this detector exists for.
    assert.equal(shouldPauseOnSilence("idle", 1), false);
    assert.equal(shouldPauseOnSilence("idle", 11), false);
  });
});

describe("ringer restore is safe on every path", () => {
  // Mirrors the native module's contract: previousRingerMode is only set when
  // we actually replaced a mode, and restore clears it.
  function makeRinger(initial) {
    let mode = initial;
    let previous = null;
    return {
      get mode() { return mode; },
      silence(granted) {
        if (!granted) return false;
        if (mode !== "silent") {
          if (previous === null) previous = mode;
          mode = "silent";
        }
        return true;
      },
      restore() {
        if (previous === null) return true;   // nothing to undo
        mode = previous;
        previous = null;
        return true;
      },
    };
  }

  it("restores the exact mode the user had", () => {
    const r = makeRinger("vibrate");
    r.silence(true);
    assert.equal(r.mode, "silent");
    r.restore();
    assert.equal(r.mode, "vibrate");
  });

  it("is a no-op when permission was refused", () => {
    const r = makeRinger("normal");
    assert.equal(r.silence(false), false);
    r.restore();
    assert.equal(r.mode, "normal", "must not change a mode we never took");
  });

  it("double-silencing does not lose the original mode", () => {
    const r = makeRinger("normal");
    r.silence(true);
    r.silence(true);          // e.g. a resume after an interruption
    r.restore();
    assert.equal(r.mode, "normal");
  });

  it("restoring twice is harmless", () => {
    const r = makeRinger("vibrate");
    r.silence(true);
    r.restore();
    r.restore();              // stopTicker can run more than once
    assert.equal(r.mode, "vibrate");
  });

  it("leaves an already-silent phone alone", () => {
    const r = makeRinger("silent");
    r.silence(true);
    r.restore();
    assert.equal(r.mode, "silent", "never un-silences a phone the user muted");
  });
});
