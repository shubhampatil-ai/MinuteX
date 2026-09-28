// lib/site-salesforce-events.ts — "this Salesforce data changed" signals for
// the Salesforce Site screens (delivery_type = salesforce_site).
//
// WHY. People, discovered objects and a meeting's Salesforce relationship are
// read by several screens at once (the Salesforce settings screen, Contacts,
// every "choose a person" sheet, the meeting card, the push screen). A sync
// finishing on one screen must show on the others immediately — not on the
// next focus, not after a manual pull-to-refresh, and never as a stale list.
// Each screen subscribes to the kinds it renders and reloads when one bumps.
//
// PURE (no React Native), so lib/__tests__/site-salesforce-flow.test.mjs pins
// it. The React hook lives in site-salesforce-ui.tsx.

export type SiteSalesforceDataKind = "people" | "objects" | "connection" | "relation";

const versions: Record<SiteSalesforceDataKind, number> = {
  people: 0, objects: 0, connection: 0, relation: 0,
};
const listeners = new Set<(kinds: SiteSalesforceDataKind[]) => void>();

/** Mark data as changed; every subscriber of those kinds reloads. */
export function invalidateSiteSalesforce(...kinds: SiteSalesforceDataKind[]): void {
  if (!kinds.length) return;
  kinds.forEach((k) => { versions[k] += 1; });
  listeners.forEach((fn) => fn(kinds));
}

export function siteSalesforceVersion(kind: SiteSalesforceDataKind): number {
  return versions[kind];
}

/** Call `fn` whenever any of `kinds` is invalidated. Returns the unsubscribe. */
export function subscribeSiteSalesforce(
  kinds: SiteSalesforceDataKind[], fn: () => void,
): () => void {
  const wanted = new Set(kinds);
  const listener = (changed: SiteSalesforceDataKind[]) => {
    if (changed.some((k) => wanted.has(k))) fn();
  };
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

/** Which data a status change means is now fresh: a people sync or a
 *  discovery that was running and has just finished. Compared across polls,
 *  so it fires once per completed run, never on every poll. */
export function completedRuns(
  prev: { people?: string; discovery?: string },
  next: { people?: string; discovery?: string },
): SiteSalesforceDataKind[] {
  const running = (s?: string) => ["PENDING", "SYNCING", "RETRYING"].includes(s || "");
  const out: SiteSalesforceDataKind[] = [];
  if (running(prev.people) && next.people === "SYNCED") out.push("people");
  if (running(prev.discovery) && next.discovery === "SYNCED") out.push("objects");
  return out;
}
