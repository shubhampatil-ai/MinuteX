// lib/profile-gate.ts — one signal: "this user now has a name".
//
// WHY THIS EXISTS. The profile gate lives in RootContent (_layout.tsx) and
// asks /me ONCE per sign-in, deliberately: it is a network call, and putting
// it on `segments` would fire a request on every tab switch. The comment
// there said the local flip after saving a name was enough — but `needsName`
// is private state in RootContent and the onboarding screen had no way to
// reach it. So saving a name left the flag stuck at `true`, the gate bounced
// the user straight back to the gate, and the only way into the app was to
// kill and relaunch it (a cold start re-runs the /me check).
//
// A module-level listener is the smallest thing that closes that loop: no new
// provider, no context threaded through screens that do not care, and no
// extra /me call. The screen that satisfies the gate announces it; the gate
// listens.
//
// NOT app state, and never read as truth. It is an invalidation ping — the
// server remains the only authority on whether a name is set, and a cold
// start still asks /me exactly as before.
type Listener = () => void;

const listeners = new Set<Listener>();

/** Subscribe to "a name was just saved". Returns an unsubscribe function. */
export function onProfileNameSaved(fn: Listener): () => void {
  listeners.add(fn);
  return () => { listeners.delete(fn); };
}

/** Announce that this user now has a name. Safe to call when nobody listens. */
export function notifyProfileNameSaved(): void {
  // Copied before iterating: a listener that unsubscribes itself while the
  // set is being walked would otherwise mutate it mid-iteration.
  for (const fn of Array.from(listeners)) {
    try {
      fn();
    } catch {
      // One bad listener must not stop the others, and must never surface as
      // a failure of the save that triggered it.
    }
  }
}
