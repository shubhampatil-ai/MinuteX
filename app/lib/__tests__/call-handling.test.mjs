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
