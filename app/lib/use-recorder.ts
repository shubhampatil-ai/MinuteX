// lib/use-recorder.ts — the React binding for the recording controller.
//
// Thin on purpose. All the logic lives in lib/rec-controller.ts, outside the
// component tree, because a recording has to survive navigation and the
// controller has to be able to act on an interruption whether or not any
// screen is mounted. This file exists only to let a screen READ that state and
// re-render, plus a couple of convenience selectors.
//
// useSyncExternalStore is the right primitive here rather than useState +
// useEffect: it subscribes without an extra render pass and, critically, reads
// the CURRENT snapshot on every render — so a screen that mounts mid-recording
// paints the live state on its first frame instead of flashing IDLE. That is
// exactly the "leave the screen and come back" requirement.
import { useSyncExternalStore } from "react";
import {
  getSnapshot,
  subscribe,
  type RecSnapshot,
} from "./rec-controller";

export function useRecorder(): RecSnapshot {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}

/** Just "is something being recorded right now" — for the global indicator. */
export function useIsRecording(): boolean {
  const snap = useRecorder();
  return (
    snap.state === "RECORDING" ||
    snap.state === "PAUSED_BY_USER" ||
    snap.state === "PAUSED_BY_CALL" ||
    snap.state === "PAUSED_BY_AUDIO_INTERRUPTION" ||
    snap.state === "PAUSED_BY_MICROPHONE"
  );
}
