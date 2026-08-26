// lib/contacts.ts — assignee directory + notification dispatch.
//
// Three sources feed the "Assign To" picker:
//   Team Members    — no backend directory exists yet (confirmed: no /team,
//                      /org or /users-list route). Shown as clearly-labeled
//                      sample entries so the picker isn't empty on a fresh
//                      account, never presented as real teammates.
//   Recent Contacts  — same story: no backend history of who tasks were
//                      assigned to before. Sample entries, same labeling.
//   Phone Contacts   — REAL: read from the device via expo-contacts.
//   Manual Entry     — a typed email or phone number, always available.
//
// Dispatch (WhatsApp / Email / SMS) is a deep-link HANDOFF, not a silent
// send: there is no backend capable of dispatching a message on the user's
// behalf, so each channel opens the real native app pre-filled with the
// generated text and the user taps Send there themselves. "MinuteX App" has
// no transport at all (no push/notification backend) — it's recorded as a
// local, in-app-only acknowledgement, never claimed as delivered.
import { Linking, Platform } from "react-native";
import type { Assignee, NotifyChannel } from "./task-model";
import { avatarColorFor } from "./task-model";

export type ContactPickResult = {
  name: string;
  phone?: string;
  email?: string;
  /** Stable identity for list keys and dedup. For device contacts this is the
   * OS record id; for sample/manual entries it is derived from the fields. */
  id: string;
  /** A LOCAL file:// (or content://) URI for this person's address-book photo,
   * when the device has one. Only ever a device-local path — it is not a MinuteX
   * avatar and must be uploaded (lib/avatars.ts) before it can be stored on a
   * contact, since the URI means nothing to any other device or after the OS
   * revokes it. Absent for most contacts: an address book usually has photos
   * for a small minority of entries. */
  photoUri?: string;
};

// ---- Sample directories — DEPRECATED, no longer rendered anywhere ---------
//
// These existed because there was no contact backend: the Assign To screen had
// to show SOMETHING, so it showed clearly-labelled invented people. That is now
// actively harmful — a real Contacts service exists, and offering a user
// fictional teammates while hiding their actual contacts is worse than an empty
// list. The assign flow uses lib/contact-picker.tsx against the real /contacts
// API instead (folder-first, then global, plus create-new and phone import).
//
// Kept only so nothing importing them breaks in a partial build. Do NOT wire
// these into a screen; grep confirmed no screen references them any more.
/** @deprecated Use the real contacts API via lib/contact-picker.tsx. */
export const SAMPLE_TEAM: ContactPickResult[] = [
  { id: "sample-team-1", name: "Shubham Patil", email: "shubham.patil@company.com" },
  { id: "sample-team-2", name: "Ritesh More", email: "ritesh.more@company.com" },
];

/** @deprecated Use the real contacts API via lib/contact-picker.tsx. */
export const SAMPLE_RECENT: ContactPickResult[] = [
  { id: "sample-recent-1", name: "Rahul Patil", email: "rahul.patil@company.com", phone: "+91 98765 43210" },
  { id: "sample-recent-2", name: "Anita Sharma", email: "anita.sharma@company.com" },
];

// ---- Device contacts (expo-contacts is a native module — same defensive
// require() pattern as lib/audio-player.tsx's expo-audio load, so a build
// without it degrades to "no phone contacts" instead of crashing).
//
// Imported from "expo-contacts/legacy", NOT the "expo-contacts" top-level
// entry point: this SDK version made the classic getContactsAsync/Fields/
// requestPermissionsAsync API (still what this file is written against)
// deprecated on the default export, and it now actually THROWS on call
// rather than just warning — every phone-contacts search below would reject
// with "Uncaught (in promise)" otherwise. /legacy re-exports the identical
// function signatures without the deprecation throw.
type ContactsModule = typeof import("expo-contacts/legacy");

function loadContacts(): ContactsModule | null {
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    return require("expo-contacts/legacy") as ContactsModule;
  } catch {
    return null;
  }
}
const Contacts = loadContacts();

export async function requestContactsPermission(): Promise<boolean> {
  if (!Contacts) return false;
  const { status } = await Contacts.requestPermissionsAsync();
  return status === "granted";
}

/** Same three outcomes the device-permission helpers use (see
 * PermissionOutcome), so UIs can branch the same way: "denied" still has a
 * live OS dialog to show, "blocked" does not and needs the Settings route.
 * expo-contacts reports that difference via canAskAgain. */
export type ContactsOutcome = "granted" | "denied" | "blocked";

export async function requestContactsPermissionDetailed(): Promise<ContactsOutcome> {
  if (!Contacts) return "blocked";
  try {
    const { status, canAskAgain } = await Contacts.requestPermissionsAsync();
    if (status === "granted") return "granted";
    return canAskAgain ? "denied" : "blocked";
  } catch {
    return "denied";
  }
}

/** Read-only variant of the above — never prompts. */
export async function getContactsOutcome(): Promise<ContactsOutcome> {
  if (!Contacts) return "blocked";
  try {
    const { status, canAskAgain } = await Contacts.getPermissionsAsync();
    if (status === "granted") return "granted";
    return canAskAgain ? "denied" : "blocked";
  } catch {
    return "denied";
  }
}

/** Read-only check — never shows the OS prompt. Use to re-check after the
 * user comes back from Settings (see lib/permissions.ts's useOnForeground). */
export async function hasContactsPermission(): Promise<boolean> {
  if (!Contacts) return false;
  const { status } = await Contacts.getPermissionsAsync();
  return status === "granted";
}

/** Comparable form of a phone number: digits only, keeping any leading "+".
 * Mirrors the backend's _norm_phone so the app and the API agree on when two
 * numbers are "the same" — "+91 98765 43210" and "+919876543210" collapse,
 * while a bare local number stays distinct from its international spelling
 * (the safe direction to fail: two rows the user can merge, not one row
 * wrongly fused from two people). */
function normPhone(raw?: string): string {
  const t = (raw ?? "").trim();
  if (!t) return "";
  const digits = t.replace(/\D/g, "");
  if (digits.length < 7) return "";
  return (t.startsWith("+") ? "+" : "") + digits;
}

function normEmail(raw?: string): string {
  return (raw ?? "").trim().toLowerCase();
}

function normName(raw?: string): string {
  return (raw ?? "").replace(/\s+/g, " ").trim().toLowerCase();
}

/** Collapse the SAME PERSON appearing several times in one address book.
 *
 * This is the normal state of a real device, not an edge case: a phone linked
 * to both a Google account and a SIM (or to two Google accounts, or to
 * WhatsApp) holds one row per source for the same person. expo-contacts
 * returns every row, so without this the picker shows "Rahul Patil" two or
 * three times, and — because those rows carry identical name/email/phone —
 * React sees duplicate keys and warns.
 *
 * Two rows are the same person when they share a strong identifier (email or
 * phone) or when the name matches and neither side contributes a CONFLICTING
 * identifier. A shared name with two different emails is left as two rows:
 * two people called "Rahul Sharma" is ordinary, and merging them here would
 * be the silent identity inference the backend deliberately refuses to make.
 *
 * Merging keeps the first row's name and fills in any field the winner is
 * missing, so a SIM row with only a number and a Google row with only an
 * email become one complete person rather than two half ones.
 */
function dedupePhoneContacts(rows: ContactPickResult[]): ContactPickResult[] {
  const out: ContactPickResult[] = [];
  // Strong identifiers -> index into `out`, so a later row can find its match
  // in O(1) instead of rescanning.
  const byEmail = new Map<string, number>();
  const byPhone = new Map<string, number>();
  const byName = new Map<string, number[]>();

  for (const row of rows) {
    const email = normEmail(row.email);
    const phone = normPhone(row.phone);
    const name = normName(row.name);

    let at = -1;
    if (email && byEmail.has(email)) at = byEmail.get(email)!;
    else if (phone && byPhone.has(phone)) at = byPhone.get(phone)!;
    else {
      // Name-only match: allowed ONLY when nothing contradicts it.
      for (const i of byName.get(name) ?? []) {
        const kept = out[i];
        const keptEmail = normEmail(kept.email);
        const keptPhone = normPhone(kept.phone);
        const emailConflict = !!email && !!keptEmail && email !== keptEmail;
        const phoneConflict = !!phone && !!keptPhone && phone !== keptPhone;
        if (!emailConflict && !phoneConflict) { at = i; break; }
      }
    }

    if (at >= 0) {
      // Fill the gaps in the row we already kept; never overwrite a value it
      // already has, so the first (usually richest) source stays authoritative.
      const kept = out[at];
      if (!kept.email && row.email) {
        kept.email = row.email;
        byEmail.set(normEmail(row.email), at);
      }
      if (!kept.phone && row.phone) {
        kept.phone = row.phone;
        const np = normPhone(row.phone);
        if (np) byPhone.set(np, at);
      }
      // Same gap-filling rule for the photo: only ONE of a person's several
      // address-book rows usually carries it (typically the Google/WhatsApp
      // one, not the SIM), so without this the merged person loses the picture
      // whenever the photo-less row happens to come first.
      if (!kept.photoUri && row.photoUri) kept.photoUri = row.photoUri;
      continue;
    }

    const i = out.length;
    out.push({ ...row });
    if (email) byEmail.set(email, i);
    if (phone) byPhone.set(phone, i);
    byName.set(name, [...(byName.get(name) ?? []), i]);
  }
  return out;
}

export async function searchPhoneContacts(query: string): Promise<ContactPickResult[]> {
  if (!Contacts) return [];
  try {
    const { status } = await Contacts.getPermissionsAsync();
    if (status !== "granted") return [];

    const { data } = await Contacts.getContactsAsync({
      // Image is the THUMBNAIL (320x320 on iOS, device-dependent on Android),
      // deliberately not RawImage: an avatar renders at ~72pt, and rawImage is
      // the full uncropped original — megabytes per contact, fetched for every
      // row in a search result, to be downscaled anyway.
      fields: [
        Contacts.Fields.Emails, Contacts.Fields.PhoneNumbers,
        Contacts.Fields.Image,
      ],
      name: query || undefined,
    });

    const q = query.trim().toLowerCase();
    const mapped = data
      // Real device address books are messy: some rows have a name field
      // that's present but blank/whitespace-only, or a name made only of
      // characters that don't survive round-tripping — normalize and drop
      // anything that isn't a usable non-empty string before it ever
      // reaches ContactRow/initialsOf/avatarColorFor.
      .map((c) => ({ ...c, name: typeof c.name === "string" ? c.name.trim() : "" }))
      .filter((c) => c.name.length > 0)
      .filter((c) => !q || c.name.toLowerCase().includes(q))
      .map((c, i) => ({
        // The OS record id is the only genuinely stable key. Some Android
        // providers omit it, so fall back to a positional id — unique within
        // this result set, which is all a list key needs.
        id: String(c.id ?? `idx-${i}`),
        name: c.name,
        phone: c.phoneNumbers?.[0]?.number ?? undefined,
        email: c.emails?.[0]?.email ?? undefined,
        // imageAvailable is the cheap check the OS provides; the uri can still
        // be missing when it is true (a provider that reports a photo it will
        // not hand over), so both are required before we claim a photo.
        photoUri: c.imageAvailable && c.image?.uri ? c.image.uri : undefined,
      }));

    // Dedupe BEFORE capping. Capping first would spend the 30-row budget on
    // repeats of the same few people and push real contacts off the list.
    return dedupePhoneContacts(mapped).slice(0, 30);
  } catch (e) {
    if (__DEV__) console.warn("[contacts] searchPhoneContacts failed:", e);
    return [];
  }
}

export function toAssignee(pick: ContactPickResult, source: Assignee["source"]): Assignee {
  // NOTE: pick.photoUri is deliberately NOT carried onto an Assignee. An
  // Assignee is persisted with its task, and a device-local photo URI would be
  // a dangling path on any other device (and after the OS revokes it). A photo
  // reaches a task only via the CONTACT it resolves to, whose avatar is stored
  // server-side.
  return {
    name: pick.name,
    phone: pick.phone,
    email: pick.email,
    avatarColor: avatarColorFor(pick.name),
    source,
  };
}

// ---- Notification templates ------------------------------------------------
export function buildNotificationText(params: {
  assigneeName: string;
  meetingTitle: string;
  taskTitle: string;
  due: string;
  priority: string;
  fromName: string;
  customMessage: string;
}, channel: NotifyChannel): string {
  const { assigneeName, meetingTitle, taskTitle, due, priority, fromName, customMessage } = params;
  const firstName = assigneeName.split(" ")[0];

  if (channel === "sms") {
    return `MinuteX: New task assigned — "${taskTitle}" (${meetingTitle}). Due ${due}.${customMessage ? ` ${customMessage}` : ""}`;
  }

  if (channel === "email") {
    return [
      `Hi ${firstName},`,
      ``,
      `A task has been assigned to you.`,
      ``,
      `Meeting: ${meetingTitle}`,
      `Task: ${taskTitle}`,
      `Due Date: ${due}`,
      `Priority: ${priority}`,
      customMessage ? `\n${customMessage}` : "",
      ``,
      `Regards,`,
      `${fromName}`,
    ].join("\n");
  }

  // whatsapp / app — the conversational short form
  return [
    `Hi ${firstName},`,
    ``,
    `You have been assigned a task from the meeting "${meetingTitle}".`,
    ``,
    `Task: ${taskTitle}`,
    `Due: ${due}`,
    `Priority: ${priority}`,
    customMessage ? `\n${customMessage}` : "",
  ].join("\n");
}

// ---- Deep-link dispatch -----------------------------------------------------
export type SendOutcome = "opened" | "unavailable" | "no-contact-method" | "error";

// Timezone -> country calling code + national trunk prefix, for numbers
// saved on the device with no "+countrycode" (extremely common — most people
// save local contacts exactly as they'd dial them locally, e.g. "098765
// 43210" in India or "0412 345 678" in Australia). Keyed by IANA timezone
// (via Intl, no native module / new dependency needed) as a proxy for the
// phone's region, since expo-localization isn't installed and adding it would
// need a fresh native build. Not exhaustive — covers the common single-
// timezone-per-country cases; falls back to "assume it's already usable" for
// anything unrecognized rather than guessing wrong.
const TZ_COUNTRY: Record<string, { code: string; trunkPrefix: string }> = {
  "Asia/Calcutta": { code: "91", trunkPrefix: "0" },
  "Asia/Kolkata": { code: "91", trunkPrefix: "0" },
  "Asia/Karachi": { code: "92", trunkPrefix: "0" },
  "Asia/Dhaka": { code: "880", trunkPrefix: "0" },
  "Asia/Colombo": { code: "94", trunkPrefix: "0" },
  "Asia/Kathmandu": { code: "977", trunkPrefix: "0" },
  "America/New_York": { code: "1", trunkPrefix: "" },
  "America/Chicago": { code: "1", trunkPrefix: "" },
  "America/Denver": { code: "1", trunkPrefix: "" },
  "America/Los_Angeles": { code: "1", trunkPrefix: "" },
  "America/Toronto": { code: "1", trunkPrefix: "" },
  "Europe/London": { code: "44", trunkPrefix: "0" },
  "Europe/Dublin": { code: "353", trunkPrefix: "0" },
  "Europe/Paris": { code: "33", trunkPrefix: "0" },
  "Europe/Berlin": { code: "49", trunkPrefix: "0" },
  "Europe/Madrid": { code: "34", trunkPrefix: "" },
  "Europe/Rome": { code: "39", trunkPrefix: "" },
  "Australia/Sydney": { code: "61", trunkPrefix: "0" },
  "Australia/Melbourne": { code: "61", trunkPrefix: "0" },
  "Asia/Singapore": { code: "65", trunkPrefix: "" },
  "Asia/Dubai": { code: "971", trunkPrefix: "0" },
  "Asia/Riyadh": { code: "966", trunkPrefix: "0" },
};

function deviceRegionGuess(): { code: string; trunkPrefix: string } | null {
  try {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    return TZ_COUNTRY[tz] ?? null;
  } catch {
    return null;
  }
}

// WhatsApp's click-to-chat spec wants country code + number, digits only —
// no "+", no spaces/dashes. A phone straight from the device address book or
// manual entry can be in any shape, so normalize before building the deep
// link; the wrong format silently opens WhatsApp to no conversation (or the
// wrong one) rather than erroring visibly.
//
// The number is used AS SAVED whenever possible — country code required is
// the wrong default, since most numbers people actually have on their phone
// are saved in local format with no "+". When there's no "+"/"00" prefix,
// this guesses the country from the device's own timezone (see TZ_COUNTRY)
// and strips exactly that country's national trunk prefix ("0" in India,
// none in the US) before prepending the code — NOT a blind strip of every
// leading zero, which previously turned a local number into a wrong,
// truncated-looking international one (reported as WhatsApp opening to
// "+9....." instead of the real +91 number). If the region can't be guessed,
// the digits are sent as-is rather than blocked — WhatsApp will simply fail
// to resolve an under-specified number, same as dialing it wrong yourself.
function normalizeWhatsAppPhone(phone: string): string {
  const trimmed = phone.trim();
  if (trimmed.startsWith("+")) {
    return trimmed.slice(1).replace(/[^\d]/g, "");
  }
  if (trimmed.startsWith("00")) {
    return trimmed.slice(2).replace(/[^\d]/g, "");
  }
  const digits = trimmed.replace(/[^\d]/g, "");
  const region = deviceRegionGuess();
  if (region) {
    const withoutTrunk = region.trunkPrefix && digits.startsWith(region.trunkPrefix)
      ? digits.slice(region.trunkPrefix.length)
      : digits;
    return region.code + withoutTrunk;
  }
  // Unknown region — send the digits as typed/saved rather than block.
  return digits;
}

export async function sendWhatsApp(phone: string | undefined, text: string): Promise<SendOutcome> {
  if (!phone) return "no-contact-method";
  const digits = normalizeWhatsAppPhone(phone);
  if (!digits) return "no-contact-method";
  const url = `whatsapp://send?phone=${digits}&text=${encodeURIComponent(text)}`;
  try {
    const can = await Linking.canOpenURL(url);
    if (!can) return "unavailable";
    await Linking.openURL(url);
    return "opened";
  } catch {
    return "error";
  }
}

export async function sendEmail(email: string | undefined, subject: string, body: string): Promise<SendOutcome> {
  if (!email) return "no-contact-method";
  const url = `mailto:${encodeURIComponent(email)}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(body)}`;
  try {
    const can = await Linking.canOpenURL(url);
    if (!can) return "unavailable";
    await Linking.openURL(url);
    return "opened";
  } catch {
    return "error";
  }
}

export async function sendSms(phone: string | undefined, text: string): Promise<SendOutcome> {
  if (!phone) return "no-contact-method";
  try {
    // eslint-disable-next-line @typescript-eslint/no-require-imports
    const SMS = require("expo-sms") as typeof import("expo-sms");
    const available = await SMS.isAvailableAsync();
    if (!available) {
      // Fall back to the sms: URL scheme — works on devices where expo-sms's
      // composer isn't available (e.g. some Android builds without Telephony).
      const sep = Platform.OS === "ios" ? "&" : "?";
      const url = `sms:${encodeURIComponent(phone)}${sep}body=${encodeURIComponent(text)}`;
      const can = await Linking.canOpenURL(url);
      if (!can) return "unavailable";
      await Linking.openURL(url);
      return "opened";
    }
    await SMS.sendSMSAsync([phone], text);
    return "opened";
  } catch {
    return "error";
  }
}
