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

export type ContactPickResult = { name: string; phone?: string; email?: string };

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
  { name: "Shubham Patil", email: "shubham.patil@company.com" },
  { name: "Ritesh More", email: "ritesh.more@company.com" },
];

/** @deprecated Use the real contacts API via lib/contact-picker.tsx. */
export const SAMPLE_RECENT: ContactPickResult[] = [
  { name: "Rahul Patil", email: "rahul.patil@company.com", phone: "+91 98765 43210" },
  { name: "Anita Sharma", email: "anita.sharma@company.com" },
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

export async function searchPhoneContacts(query: string): Promise<ContactPickResult[]> {
  if (!Contacts) return [];
  try {
    const { status } = await Contacts.getPermissionsAsync();
    if (status !== "granted") return [];

    const { data } = await Contacts.getContactsAsync({
      fields: [Contacts.Fields.Emails, Contacts.Fields.PhoneNumbers],
      name: query || undefined,
    });

    const q = query.trim().toLowerCase();
    return data
      // Real device address books are messy: some rows have a name field
      // that's present but blank/whitespace-only, or a name made only of
      // characters that don't survive round-tripping — normalize and drop
      // anything that isn't a usable non-empty string before it ever
      // reaches ContactRow/initialsOf/avatarColorFor.
      .map((c) => ({ ...c, name: typeof c.name === "string" ? c.name.trim() : "" }))
      .filter((c) => c.name.length > 0)
      .filter((c) => !q || c.name.toLowerCase().includes(q))
      .slice(0, 30)
      .map((c) => ({
        name: c.name,
        phone: c.phoneNumbers?.[0]?.number ?? undefined,
        email: c.emails?.[0]?.email ?? undefined,
      }));
  } catch (e) {
    if (__DEV__) console.warn("[contacts] searchPhoneContacts failed:", e);
    return [];
  }
}

export function toAssignee(pick: ContactPickResult, source: Assignee["source"]): Assignee {
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
