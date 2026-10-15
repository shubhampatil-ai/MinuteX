// lib/use-input-level.ts — samples the live input level into waveform data.
//
// The middle layer of three, and the only one with a clock:
//
//   rec-controller.readInputLevel()   level PROVIDER — what the mic hears (dBFS)
//   useWaveformLevels()  ← this file  samples it, runs lib/audio-level.ts,
//                                     publishes the history to a shared value
//   <LevelWaveform>                   RENDERER — draws whatever it is given
//
// Why a Reanimated shared value rather than React state: the history changes
// ~14 times a second for the length of a meeting. Pushing that through
// setState would re-render the whole recorder screen at display rate. A shared
// value is read by each bar's animated style on the UI thread, so a new sample
// costs one JS→UI write and no React render at all.
//
// COST CONTROL. The timer runs only while ALL of these hold:
//   * the caller says capture is live (`active`) — paused/finalizing = no timer
//   * the app is in the foreground — a locked phone draws nothing, so sampling
//     for it would be pure battery spend over a three-hour meeting
// The recorder itself never depends on this hook: unmount it, or never mount
// it, and recording, silence detection and upload behave exactly as before.
import { useEffect, useRef } from "react";
import { AppState } from "react-native";
import { useSharedValue, type SharedValue } from "react-native-reanimated";
import { LevelHistory, LevelSmoother, stepWaveform } from "./audio-level";
import { MOTION } from "./motion";
import { readInputLevel } from "./rec-controller";

export function useWaveformLevels({
  active, bars, resetKey, read = readInputLevel, intervalMs = MOTION.waveformSampleMs,
}: {
  /** Sample only while audio is genuinely being captured. */
  active: boolean;
  /** Number of bars = history length. */
  bars: number;
  /** Changing this (e.g. a new session id) clears the history. */
  resetKey?: string | null;
  /** Level provider, injectable for tests/previews. Returns dBFS or null. */
  read?: () => number | null;
  intervalMs?: number;
}): SharedValue<number[]> {
  const history = useRef<LevelHistory | null>(null);
  const smoother = useRef(new LevelSmoother());
  const levels = useSharedValue<number[]>(new Array(bars).fill(-1));

  // A new recording (or a different bar count) starts from an empty field,
  // not from the tail of the previous one.
  useEffect(() => {
    history.current = new LevelHistory(bars);
    smoother.current.reset();
    levels.set(history.current.toArray());
  }, [bars, resetKey, levels]);

  useEffect(() => {
    if (!active) {
      // Keep what was captured on screen (a paused recorder shows its
      // history, frozen); just let the envelope fall so resuming does not
      // begin with a stale spike.
      smoother.current.reset();
      return;
    }
    let timer: ReturnType<typeof setInterval> | null = null;
    const tick = () => {
      const h = history.current;
      if (!h) return;
      levels.set(stepWaveform(read(), smoother.current, h));
    };
    const start = () => {
      if (timer == null) timer = setInterval(tick, intervalMs);
    };
    const halt = () => {
      if (timer != null) { clearInterval(timer); timer = null; }
    };
    if (AppState.currentState === "active") start();
    const sub = AppState.addEventListener("change", (s) => {
      if (s === "active") start(); else halt();
    });
    return () => { halt(); sub.remove(); };
  }, [active, intervalMs, read, levels]);

  return levels;
}
