// lib/site-salesforce-flow.ts — the decisions behind the Salesforce Site
// screens (src/app/site-salesforce/*, lib/site-salesforce-meeting.tsx), with
// no React Native (delivery_type = salesforce_site).
//
// PURE so every rule is pinned by lib/__tests__/site-salesforce-flow.test.mjs.
// Nothing here classifies Salesforce data: object names, fields and picklist
// values always come from the org's discovered metadata.

/** The MinuteX Apex REST path on the customer's Site (site_delivery_client.RECEIVER_PATH). */
export const RECEIVER_PATH = "/services/apexrest/minutex/v1/";

/** The Site URL a tester types -> the receiver URL the backend stores.
 *
 *  Accepts the Site's own URL (`https://acme.my.salesforce-sites.com/minutex`)
 *  or the full receiver URL, and appends the receiver path when it is
 *  missing. The backend validates the result again (host suffix, https,
 *  path); this only saves the tester from typing the Apex REST path. */
export function normalizeSiteUrl(input: string): { url: string; error: string } {
  let raw = String(input || "").trim();
  if (!raw) return { url: "", error: "Enter your Salesforce Site URL." };
  if (/^http:\/\//i.test(raw)) return { url: "", error: "The Site URL must start with https://." };
  if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(raw)) raw = `https://${raw}`;
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    return { url: "", error: "That is not a valid URL." };
  }
  if (parsed.protocol !== "https:") return { url: "", error: "The Site URL must start with https://." };
  if (parsed.search || parsed.hash) {
    return { url: "", error: "Enter the Site URL without a query string." };
  }
  let path = parsed.pathname.replace(/\/+$/, "");
  const at = path.toLowerCase().indexOf("/services/apexrest");
  if (at >= 0) path = path.slice(0, at);
  return { url: `https://${parsed.host.toLowerCase()}${path}${RECEIVER_PATH}`, error: "" };
}

/** The Salesforce org's name as a person reads it, or "" when Salesforce
 *  has none worth showing. Orgs created without a company name report it as
 *  the literal text "None" (or "null"), which must never read as
 *  "Connected to: None". */
export function orgDisplayName(name?: string | null): string {
  const n = (name || "").trim();
  return /^(none|null|undefined)$/i.test(n) ? "" : n;
}

/** The receiver URL shown back as the Site URL the tester entered. */
export function displaySiteUrl(receiverUrl: string | undefined): string {
  const u = String(receiverUrl || "");
  return u.endsWith(RECEIVER_PATH) ? u.slice(0, -RECEIVER_PATH.length) || u : u;
}

// ---------------------------------------------------------------------------
// Errors: backend `code` -> what a tester can act on. The server's message is
// kept for any code not listed (it is already user-facing; it never carries
// a stack trace or a secret).
// ---------------------------------------------------------------------------
const SITE_UNAVAILABLE = "Salesforce isn't reachable right now. Try again in a moment.";
const OBJECT_UNAVAILABLE = "This Salesforce object is no longer available. Refresh Salesforce metadata.";
const RECORD_UNAVAILABLE = "The selected Salesforce record could not be found.";
const PERMISSION_DENIED = "You do not have permission to create a Task for this Salesforce record.";

export const SITE_ERROR_MESSAGES: Record<string, string> = {
  timeout: SITE_UNAVAILABLE,
  network_error: SITE_UNAVAILABLE,
  receiver_unavailable: SITE_UNAVAILABLE,
  endpoint_not_found: "MinuteX isn't installed at that connection link. Check the link with your Salesforce admin.",
  site_requires_login: "That link needs a Salesforce login. Use the MinuteX connection link from your Salesforce admin.",
  site_redirected: "Salesforce isn't set up for MinuteX yet. Ask your Salesforce admin to finish the MinuteX setup.",
  invalid_response: "That link isn't a MinuteX connection. Check it with your Salesforce admin.",
  invalid_endpoint: "That isn't a Salesforce connection link.",
  auth_failed: "The Salesforce Site rejected MinuteX's request signature.",
  receiver_disabled: "The MinuteX receiver is turned off in Salesforce (MinuteX Site Settings > Enabled).",
  receiver_misconfigured: "The MinuteX receiver's settings in Salesforce are incomplete.",
  site_delivery_not_configured: "Connect Salesforce first.",
  destination_unavailable: "That Salesforce destination isn't available anymore. Choose another one.",
  install_unavailable: "Installing MinuteX in Salesforce isn't available right now. Try again later.",
  install_not_finished: "Finish installing MinuteX in Salesforce first, then enter the setup code in MinuteX Setup.",
  install_expired: "This setup code expired. Start again.",
  invalid_setup_code: "This setup code isn't valid or has expired. Start again.",
  destination_not_allowed: "You don't have permission to push meetings there. Ask your Salesforce admin.",
  record_not_editable: "You don't have access to update that Salesforce record.",
  acting_user_invalid: "Your Salesforce user is inactive. Ask your Salesforce admin.",
  meeting_not_pushed: "Push the meeting to Salesforce first.",
  salesforce_rejected: "Salesforce didn't accept the update. Check the record in Salesforce and try again.",
  salesforce_not_connected: "Connect Salesforce first.",
  integration_user_not_configured:
    "Set the Integration Username in Salesforce (Custom Settings > MinuteX Site Settings).",
  integration_user_invalid:
    "The Integration Username in MinuteX Site Settings is not an active Salesforce user.",
  org_mismatch: "The Salesforce Site answered for a different org than the one connected.",
  discovery_required: "Discover Salesforce objects first.",
  mapping_invalid: OBJECT_UNAVAILABLE,
  unknown_object: OBJECT_UNAVAILABLE,
  invalid_object: OBJECT_UNAVAILABLE,
  invalid_record: RECORD_UNAVAILABLE,
  record_not_found: RECORD_UNAVAILABLE,
  salesforce_permission_denied: PERMISSION_DENIED,
  permission_denied: PERMISSION_DENIED,
  forbidden: PERMISSION_DENIED,
  task_in_flight: "This Task is still being sent to Salesforce. Try again in a moment.",
  record_required: "Tag a Salesforce Contact in this meeting, or choose the Salesforce record these tasks belong to.",
  invalid_person: "Choose a Salesforce Contact from the synced people.",
  invalid_relationship: "A Salesforce Task can be related to one person and one record, and a Lead cannot also have a record.",
  transcript_too_large: "This meeting's transcript is too long for Salesforce (131,072 characters).",
};

export function siteErrorMessage(code: string | undefined, serverMessage?: string): string {
  const mapped = code ? SITE_ERROR_MESSAGES[code] : "";
  return mapped || serverMessage || "Something went wrong. Try again.";
}

// ---------------------------------------------------------------------------
// Connection state (the screen shows exactly one)
// ---------------------------------------------------------------------------
export type SiteStage =
  | "not_connected" | "connecting" | "connected" | "discovering" | "syncing_people" | "ready";

export const IN_FLIGHT_STATUSES = ["PENDING", "SYNCING", "RETRYING"];
export const isInFlight = (status?: string) => IN_FLIGHT_STATUSES.includes(status || "");

export function siteStage(o: {
  connected: boolean; connecting?: boolean;
  discoveryStatus?: string; peopleStatus?: string;
}): SiteStage {
  if (o.connecting) return "connecting";
  if (!o.connected) return "not_connected";
  if (isInFlight(o.discoveryStatus)) return "discovering";
  if (isInFlight(o.peopleStatus)) return "syncing_people";
  return o.discoveryStatus === "SYNCED" && o.peopleStatus === "SYNCED" ? "ready" : "connected";
}

export const STAGE_LABELS: Record<SiteStage, string> = {
  not_connected: "Not connected",
  connecting: "Connecting…",
  connected: "Connected",
  discovering: "Discovering objects…",
  syncing_people: "Syncing people…",
  ready: "Ready",
};

/** health from POST .../site-delivery/health -> one line for the screen. */
export function healthLine(h: { ok?: boolean; code?: string; message?: string } | null | undefined):
  { ok: boolean; title: string; detail: string } | null {
  if (!h || h.ok === undefined) return null;
  if (h.ok) return { ok: true, title: "Connected", detail: "" };
  return { ok: false, title: "Connection failed", detail: siteErrorMessage(h.code, h.message) };
}

/** "Contacts (23)" — a people tab label. The count is left off until known. */
export function peopleTabLabel(type: "contact" | "user", counts?: { contacts: number; users: number } | null): string {
  const label = type === "user" ? "Users" : "Contacts";
  const n = counts ? (type === "user" ? counts.users : counts.contacts) : null;
  return typeof n === "number" ? `${label} (${n})` : label;
}

/** People-sync counts -> "125 people synced" + per-type lines. Counts that
 *  the backend did not report are left out rather than shown as 0. */
export function peopleSummary(counts: Record<string, number> | undefined): {
  total: number | null; users: number | null; contacts: number | null;
} {
  const c = counts || {};
  const users = typeof c.users === "number" ? c.users : null;
  const contacts = typeof c.contacts === "number" ? c.contacts : null;
  const total = users !== null && contacts !== null ? users + contacts
    : typeof c.received === "number" ? c.received - (c.skipped || 0) : null;
  return { total, users, contacts };
}

// ---------------------------------------------------------------------------
// Objects
//
// The backend offers only objects a meeting or Task can relate to (usable
// standard business objects + usable custom objects). The UI shows their
// LABELS; API names are keys the app passes back, never text a person reads.
// ---------------------------------------------------------------------------
type ObjectLike = {
  api_name: string; label: string; label_plural?: string; is_custom?: boolean;
  queryable?: boolean; task_relation?: string; category?: string;
};

/** Objects a Task can belong to: Tasks can relate AND records can be searched. */
export const taskRelatable = (o: ObjectLike) => !!o.task_relation && !!o.queryable;

export type ObjectFilter = "all" | "standard" | "custom" | "tasks";

/** Search by what a person reads (label, plural label); the API name still
 *  matches so a typed "Site_Visit" finds Site Visit, but it is never shown. */
export function filterObjects<T extends ObjectLike>(
  objects: T[], q: string, filter: ObjectFilter = "all",
): T[] {
  const needle = q.trim().toLowerCase();
  return objects.filter((o) => {
    if (filter === "tasks" && !taskRelatable(o)) return false;
    if (filter === "custom" && !o.is_custom) return false;
    if (filter === "standard" && o.is_custom) return false;
    return !needle || o.label.toLowerCase().includes(needle)
      || (o.label_plural || "").toLowerCase().includes(needle)
      || o.api_name.toLowerCase().includes(needle);
  });
}

/** The short list, as a person reads it: "Recommended Salesforce records"
 *  (the common defaults), then the objects this workspace added. */
export function visibleObjectSections<T extends ObjectLike & { recommended?: boolean }>(
  objects: T[],
): { title: string; data: T[] }[] {
  const rec = objects.filter((o) => o.recommended);
  const added = objects.filter((o) => !o.recommended);
  return [
    ...(rec.length ? [{ title: "Recommended Salesforce records", data: rec }] : []),
    ...(added.length ? [{ title: "Added for this workspace", data: added }] : []),
  ];
}

/** Standard objects first, then custom objects, each under its own heading.
 *  Empty groups are left out. Keeps the backend's order inside a group. */
export function objectGroups<T extends ObjectLike>(objects: T[]): { title: string; data: T[] }[] {
  const standard = objects.filter((o) => !o.is_custom);
  const custom = objects.filter((o) => o.is_custom);
  return [
    ...(standard.length ? [{ title: "Standard objects", data: standard }] : []),
    ...(custom.length ? [{ title: "Custom objects", data: custom }] : []),
  ];
}

/** How a Task relates to this object, in words: "person" (Who) or "record". */
export function relationKindLabel(o: { task_relation?: string }): string {
  return o.task_relation === "who" ? "Person" : o.task_relation === "what" ? "Record" : "";
}

/** One line under an object's name. An object Salesforce will not attach
 *  Tasks to (Allow Activities off) can still be a meeting's record. */
export function objectUseLabel(o: { task_relation?: string }): string {
  if (o.task_relation === "who") return "A person meetings and Tasks can be with";
  if (o.task_relation === "what") return "A record meetings and Tasks can be about";
  return "Meetings only — Salesforce Tasks need Allow Activities on this object";
}

// ---------------------------------------------------------------------------
// The meeting's Salesforce relationship, as a person reads it
// ---------------------------------------------------------------------------
type RelationSideLike = { object_label: string; name: string; source?: string } | null | undefined;
type RelationLike = { who?: RelationSideLike; what?: RelationSideLike; automatic?: boolean } | null | undefined;

/** "Asha Rao", "Asha Rao · Big Deal", "Big Deal", or "" — names only. */
export function relationSummary(rel: RelationLike): string {
  return [rel?.who?.name, rel?.what?.name].filter(Boolean).join(" · ");
}

/** One line per side, e.g. {label: "Contact", name: "Asha Rao"}. */
export function relationLines(rel: RelationLike): { label: string; name: string }[] {
  return [rel?.who, rel?.what]
    .filter((side): side is NonNullable<RelationSideLike> => !!side && !!side.name)
    .map((side) => ({ label: side.object_label, name: side.name }));
}

export function hasRelation(rel: RelationLike): boolean {
  return !!(rel?.who || rel?.what);
}

type TaskSideLike = { object_label: string; name: string; source?: string; tasks_supported?: boolean } | null | undefined;

/** The part of the meeting's relationship a Salesforce Task can carry, and
 *  the sides it cannot (their object does not allow activities). */
export function taskRelationOf<T extends { who?: TaskSideLike; what?: TaskSideLike; automatic?: boolean }>(
  rel: T | null | undefined,
): { relation: { who: TaskSideLike; what: TaskSideLike; automatic?: boolean } | null; skipped: string[] } {
  if (!rel) return { relation: null, skipped: [] };
  const ok = (s: TaskSideLike) => (s && s.tasks_supported !== false ? s : null);
  const skipped = [rel.who, rel.what]
    .filter((s) => s && s.tasks_supported === false)
    .map((s) => (s as NonNullable<TaskSideLike>).object_label);
  return { relation: { who: ok(rel.who), what: ok(rel.what), automatic: rel.automatic }, skipped };
}

/** The line under the relationship: where it came from. */
export function relationSourceNote(rel: RelationLike): string {
  if (!hasRelation(rel)) {
    return "Tag a synced Salesforce Contact in People, or link the meeting to an existing Salesforce record.";
  }
  if (rel?.automatic) return "From the Salesforce Contact tagged in this meeting.";
  return "Chosen for this meeting.";
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

export function isIsoDate(s: string): boolean {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) return false;
  const d = new Date(`${s}T00:00:00Z`);
  return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === s;
}

/** Meeting delivery job -> the Push Meeting status line. */
export function meetingPushLine(job: { status?: string; last_error_code?: string; last_error_message?: string } | null | undefined):
  { tone: "success" | "pending" | "error" | "none"; text: string } {
  if (!job || !job.status) return { tone: "none", text: "Not pushed yet" };
  if (job.status === "SYNCED") return { tone: "success", text: "Pushed to Salesforce" };
  if (job.status === "FAILED" || job.status === "RECONNECT_REQUIRED") {
    return { tone: "error", text: siteErrorMessage(job.last_error_code, job.last_error_message) };
  }
  return { tone: "pending", text: job.status === "RETRYING" ? "Retrying…" : "Pushing…" };
}

// ---------------------------------------------------------------------------
// Pushing a meeting's EXISTING MinuteX tasks (AI-extracted or user-created)
// ---------------------------------------------------------------------------
type MinutexTaskLike = {
  id: string; task: string; title?: string; status: string;
  assignee?: { email?: string; name?: string } | null;
  due?: string; due_date_normalized?: string;
};
type PushedLike = {
  minutex_task_id?: string; sync_status: string; assignee_person_key?: string;
  related_object_type?: string; related_record_id?: string; related_record_name?: string;
};
type PersonLike = { person_key: string; email: string; status?: string };

/** The Salesforce Task already pushed from this MinuteX task, if any. */
export function pushedRowFor<T extends PushedLike>(taskId: string, pushed: T[]): T | undefined {
  return pushed.find((p) => p.minutex_task_id === taskId);
}

/** What the task's row says about Salesforce. */
export function salesforceStateOf(taskId: string, pushed: PushedLike[]):
  { key: "none" | "sending" | "synced" | "failed"; label: string } {
  const row = pushedRowFor(taskId, pushed);
  if (!row) return { key: "none", label: "Not in Salesforce" };
  if (isInFlight(row.sync_status)) return { key: "sending", label: "Sending…" };
  if (row.sync_status === "SYNCED") return { key: "synced", label: "In Salesforce" };
  return { key: "failed", label: "Failed" };
}

/** Which tasks start ticked: the one the person opened, else every task that
 *  is neither cancelled nor already in (or on its way to) Salesforce. */
export function defaultPushSelection(tasks: MinutexTaskLike[], pushed: PushedLike[], only?: string): string[] {
  if (only) return tasks.some((t) => t.id === only) ? [only] : [];
  return tasks
    .filter((t) => t.status !== "Cancelled")
    .filter((t) => ["none", "failed"].includes(salesforceStateOf(t.id, pushed).key))
    .map((t) => t.id);
}

/** The Salesforce User to preselect for a task — only on a FACT, never a
 *  guess: the User it was pushed to before, or an active synced User whose
 *  email is exactly the MinuteX assignee's. "" means the person picks. */
export function suggestSalesforceUser(task: MinutexTaskLike, people: PersonLike[], pushed: PushedLike[]):
  { personKey: string; reason: "previous" | "email" | "" } {
  const previous = pushedRowFor(task.id, pushed)?.assignee_person_key;
  if (previous) return { personKey: previous, reason: "previous" };
  const email = (task.assignee?.email || "").trim().toLowerCase();
  if (email) {
    const match = people.find((p) => p.email.toLowerCase() === email && (p.status || "active") === "active");
    if (match) return { personKey: match.person_key, reason: "email" };
  }
  return { personKey: "", reason: "" };
}

/** The record this meeting's tasks were already pushed onto, when they all
 *  agree — so a second push lands on the same record by default. */
export function sharedRelationship(pushed: PushedLike[]):
  { objectApiName: string; recordId: string; recordName: string } | null {
  const rows = pushed.filter((p) => p.minutex_task_id && p.related_record_id);
  if (!rows.length) return null;
  const first = rows[0];
  const same = rows.every((r) => r.related_record_id === first.related_record_id);
  return same ? { objectApiName: first.related_object_type || "", recordId: first.related_record_id || "",
                  recordName: first.related_record_name || "" } : null;
}

/** Where the meeting is in Salesforce, for a person: "Site Visit · Mumbai
 *  Tower A" when it was pushed to a destination, else "This meeting". */
export function meetingRecordLine(rec: {
  object_label: string; name: string;
  destination?: { name: string; record_name: string } | null;
} | null | undefined): string {
  if (!rec) return "";
  if (rec.destination) return [rec.destination.name, rec.destination.record_name].filter(Boolean).join(" · ");
  return "This meeting in Salesforce";
}

/** The main button on the meeting's Salesforce card. */
export function pushButtonLabel(o: { pushed: boolean; failed: boolean }): string {
  if (o.failed) return "Try again";
  return o.pushed ? "Update Salesforce" : "Push to Salesforce";
}

const SKIP_REASONS: Record<string, string> = {
  no_access: "no access", missing: "field missing", not_empty: "already filled", no_value: "no data",
};

/** "Not updated: Transcript (no access), Visit Date (no data)" — or "". */
export function skippedLine(skipped: { field: string; reason: string }[] | undefined): string {
  const worth = (skipped || []).filter((x) => x.reason !== "no_value" && x.reason !== "not_empty");
  if (!worth.length) return "";
  return `Not updated: ${worth.map((x) => `${x.field} (${SKIP_REASONS[x.reason] || x.reason})`).join(", ")}`;
}

/** What a meeting's Task is attached to, in words: Related To = the
 *  meeting's Salesforce record (or the record chosen for these Tasks), Name =
 *  the meeting's Contact (or the person chosen). */
export function taskTargetLines(o: {
  meetingRecord: { object_label: string; name: string } | null | undefined;
  who: { name: string } | null | undefined;
  override?: { relation: string; objectApiName: string; name: string } | null;
}): { label: string; name: string }[] {
  const ov = o.override;
  const what = ov && ov.relation !== "who" ? ov.name : meetingRecordLine(o.meetingRecord);
  const who = ov && ov.relation === "who" ? ov.name : o.who?.name || "";
  // Salesforce: a Task whose Name is a Lead cannot also have a Related To.
  const whatShown = ov && ov.relation === "who" && ov.objectApiName === "Lead" ? "" : what;
  return [
    ...(whatShown ? [{ label: "Related To", name: whatShown }] : []),
    ...(who ? [{ label: "Name", name: who }] : []),
  ];
}

/** The first thing stopping the push, or "" when it can go.
 *
 *  By default tasks INHERIT the meeting's relationship (`meetingRelated`), so
 *  no object or record is needed. An object chosen under "Change related
 *  Salesforce record" needs its record too. */
export function pushProblem(o: {
  selected: string[]; objectApiName: string; recordId: string; assigneeFor: (id: string) => string;
  meetingRelated?: boolean;
  /** false: the meeting is not in Salesforce yet — Tasks attach to its record. */
  meetingPushed?: boolean;
}): string {
  if (o.meetingPushed === false) {
    return "Push the meeting to Salesforce first — its Tasks are attached to the meeting's Salesforce record.";
  }
  if (!o.selected.length) return "Select at least one task.";
  if (o.objectApiName && !o.recordId) return "Select the Salesforce record these tasks belong to.";
  if (!o.objectApiName && !o.meetingRelated) {
    return "Tag a Salesforce Contact in this meeting, or change the related Salesforce record.";
  }
  const missing = o.selected.filter((id) => !o.assigneeFor(id)).length;
  if (missing) return `Choose a Salesforce User for ${missing} task${missing === 1 ? "" : "s"}.`;
  return "";
}

/** The due date Salesforce will get, as the backend decides it: the task's
 *  resolved date; a spoken phrase nobody resolved goes into the description. */
export function salesforceDue(task: MinutexTaskLike): { date: string; spoken: string } {
  const resolved = (task.due_date_normalized || "").slice(0, 10);
  if (isIsoDate(resolved)) return { date: resolved, spoken: "" };
  const spoken = (task.due || "").trim();
  if (isIsoDate(spoken.slice(0, 10))) return { date: spoken.slice(0, 10), spoken: "" };
  return { date: "", spoken };
}
