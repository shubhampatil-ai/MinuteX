// lib/salesforce-hooks.ts — React hooks for the Salesforce screens.
import { useEffect, useRef, useState } from "react";
import { AppState } from "react-native";
import { SalesforceDataKind, subscribeSalesforce } from "./salesforce-events";

/** A number that changes whenever any of `kinds` is invalidated
 *  (salesforce-events.ts). Put it in a load callback's dependencies and the
 *  screen reloads the moment another screen's change lands — no stale list,
 *  no pull-to-refresh needed. */
export function useSalesforceVersion(...kinds: SalesforceDataKind[]): number {
  const [version, setVersion] = useState(0);
  const key = kinds.join(",");
  useEffect(() => subscribeSalesforce(key.split(",") as SalesforceDataKind[],
    () => setVersion((v) => v + 1)), [key]);
  return version;
}

/** Calls `tick` every `ms` while `active`; stops as soon as it is not.
 *  Ticks are skipped while the app is in the background: a screen left open
 *  must not keep asking the server every few seconds. */
export function usePolling(active: boolean, tick: () => void, ms = 3000) {
  const ref = useRef(tick);
  useEffect(() => { ref.current = tick; });
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => { if (AppState.currentState === "active") ref.current(); }, ms);
    return () => clearInterval(id);
  }, [active, ms]);
}
