// lib/salesforce-people.ts — how Salesforce people (Contacts, Leads, Users)
// read in the app: badges, tags, tab labels, and how live search results
// merge with people already stored. Used by every choose-a-person sheet
// (lib/contact-picker.tsx) and the Contacts screen.
//
// PURE (no React Native), so lib/__tests__/salesforce-people.test.mjs pins it.

/** "Contacts (23)" — a people tab label. The count is left off until known. */
export function peopleTabLabel(type: "contact" | "user", counts?: { contacts: number; users: number } | null): string {
  const label = type === "user" ? "Users" : "Contacts";
  const n = counts ? (type === "user" ? counts.users : counts.contacts) : null;
  return typeof n === "number" ? `${label} (${n})` : label;
}

/** The badge for a contact linked to a Salesforce person, or "". */
export function salesforceBadge(c: { people_provider?: string; people_type?: string } | null | undefined): string {
  if (!c || c.people_provider !== "salesforce") return "";
  return c.people_type === "user" ? "Salesforce User"
    : c.people_type === "contact" ? "Salesforce Contact"
      : c.people_type === "lead" ? "Salesforce Lead" : "Salesforce";
}

/** The short tag on a Salesforce person row: SF USER / SF LEAD / SF CONTACT. */
export function salesforceTypeTag(type: string | undefined): string {
  return type === "user" ? "SF USER" : type === "lead" ? "SF LEAD" : "SF CONTACT";
}

/** Live search runs only once the term is long enough for the org's search. */
export const LIVE_PEOPLE_MIN_CHARS = 2;
export function shouldSearchLive(term: string): boolean {
  return term.trim().length >= LIVE_PEOPLE_MIN_CHARS;
}

/** Live results first, then any stored match the live search didn't return,
 *  one row per person. */
export function mergePeople<T extends { person_key: string }>(live: T[], stored: T[]): T[] {
  const seen = new Set<string>();
  return [...live, ...stored].filter((p) => {
    if (seen.has(p.person_key)) return false;
    seen.add(p.person_key);
    return true;
  });
}
