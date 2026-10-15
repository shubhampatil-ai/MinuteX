// lib/salesforce-events.ts — "this Salesforce data changed" signals.
//
// WHY. Salesforce people and a meeting's Salesforce relationship are read by
// several screens at once (Contacts, every "choose a person" sheet, the
// meeting card). A change made on one screen must show on the others
// immediately — not on the next focus, not after a manual pull-to-refresh.
// Each screen subscribes to the kinds it renders and reloads when one bumps.
//
// PURE (no React Native), so lib/__tests__/salesforce-people.test.mjs pins
// it. The React hook lives in salesforce-hooks.ts.

export type SalesforceDataKind = "people" | "connection" | "relation";

const versions: Record<SalesforceDataKind, number> = {
  people: 0, connection: 0, relation: 0,
};
const listeners = new Set<(kinds: SalesforceDataKind[]) => void>();

/** Mark data as changed; every subscriber of those kinds reloads. */
export function invalidateSalesforce(...kinds: SalesforceDataKind[]): void {
  if (!kinds.length) return;
  kinds.forEach((k) => { versions[k] += 1; });
  listeners.forEach((fn) => fn(kinds));
}

export function salesforceVersion(kind: SalesforceDataKind): number {
  return versions[kind];
}

/** Call `fn` whenever any of `kinds` is invalidated. Returns the unsubscribe. */
export function subscribeSalesforce(
  kinds: SalesforceDataKind[], fn: () => void,
): () => void {
  const wanted = new Set(kinds);
  const listener = (changed: SalesforceDataKind[]) => {
    if (changed.some((k) => wanted.has(k))) fn();
  };
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}
