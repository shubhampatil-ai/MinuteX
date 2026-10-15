// lib/webhook-flow.ts — Salesforce webhook (Webhook Contract v2) UI logic.
//
// PURE: no React Native, no network — imported directly by
// lib/__tests__/webhook-flow.test.mjs. The screens (lib/webhook-meeting.tsx,
// src/app/salesforce/index.tsx) only render what these return.
//
// A SEPARATE PATH from the MinuteX Salesforce package (site-salesforce-*):
// the customer's endpoint owns meeting types, record search and every
// business rule; MinuteX sends signed events (docs/webhook-contract-v2.md).
//
// Three things on a meeting stay separate, here as in the contract:
//   the people in it   -> participant tagging (unchanged, elsewhere)
//   the meeting type   -> chosen from the customer's configuration
//   the record         -> optional, only when the type enables it
// A record's Salesforce id is stored but NEVER shown: people see its
// display_name / secondary_text only.

export type WebhookRecordSelection = {
  enabled: boolean; required: boolean; label?: string; placeholder?: string;
};
export type WebhookMeetingType = {
  id: string; name: string; description?: string; record_selection: WebhookRecordSelection;
};
export type WebhookConfiguration = {
  status: "ok" | "stale" | "error" | "never";
  configuration_version: string;
  fetched_at: string;
  last_attempt_at?: string;
  last_error_code?: string;
  meeting_types: WebhookMeetingType[];
  /** The records users can link a meeting to (the linking approach); null
   *  when the customer's receiver doesn't support linking. */
  linkable_objects?: {
    object: string; label: string; can_be_primary: boolean;
    outputs_allowed: string[]; outputs_default_on: string[]; outputs_user_can_change: string[];
  }[] | null;
};
export type WebhookRecord = {
  id: string; display_name: string; secondary_text?: string; object_type?: string;
};
export type WebhookContext = {
  meeting_type_id: string | null;
  meeting_type_name: string | null;
  record: { id: string; display_name: string; secondary_text?: string } | null;
  selected_at?: string;
};
/** Where the retired package put this meeting (read-only history). */
export type WebhookEarlierRecord = { name: string; url: string; delivered_at: string };

/** A meeting screen's Salesforce section, one of three:
 *  - "live":    connected now — choose a type/record, push, retry;
 *  - "history": not connected (or pushing is off in this build) but the
 *               meeting WAS sent — show where, press nothing;
 *  - "none":    not connected and never sent — no section at all. */
export function meetingBlockMode(s: {
  mode: WebhookMode; endpoint: { enabled: boolean };
  job: WebhookJob | null; earlier_record?: WebhookEarlierRecord | null;
}, readOnly: boolean): "live" | "history" | "none" {
  if (!readOnly && s.mode === "webhook" && s.endpoint.enabled) return "live";
  if (s.job?.delivered || s.earlier_record) return "history";
  return "none";
}

/** The history card's headline: when the meeting was last sent. */
export function historyLine(job: WebhookJob | null | undefined,
                            earlier?: WebhookEarlierRecord | null): string {
  const at = (job?.delivered && job.last_delivered_at) || earlier?.delivered_at || "";
  const d = at ? new Date(at) : null;
  if (!d || isNaN(d.getTime())) return "Sent to Salesforce";
  return `Sent to Salesforce on ${d.toLocaleDateString(undefined,
    { day: "numeric", month: "short", year: "numeric" })}`;
}

export type WebhookDeliveryStatus =
  "queued" | "update_pending" | "delivering" | "delivered" | "retrying" | "failed" | "unknown";
export type WebhookJob = {
  job_id: string;
  status: string;
  delivery_status: WebhookDeliveryStatus;
  attempt: number;
  max_attempts: number;
  has_pending_changes: boolean;
  delivered: boolean;
  delivered_sequence: number;
  last_delivered_at: string;
  lifecycle: string;
  next_attempt_at: string;
  last_error_code: string;
  last_error_message: string;
  external: { record_id: string; url: string } | null;
};
export type WebhookTest = {
  ok: boolean; at: string; code?: string; message?: string; http_status?: number;
  configuration_version: string; meeting_type_count?: number;
};
export type WebhookEndpoint = {
  configured: boolean;
  enabled: boolean;
  delivery_type: "webhook";
  url?: string;
  has_secret?: boolean;
  secret_last4?: string;
  secret_created_at?: string;
  previous_secret_expires_at?: string;
  last_test?: WebhookTest | Record<string, never>;
  configuration?: WebhookConfiguration;
  people_sync?: { status?: string; counts?: Record<string, number> };
};
export type WebhookMode = "webhook" | "site" | "none";

export const IN_FLIGHT_DELIVERY: WebhookDeliveryStatus[] =
  ["queued", "update_pending", "delivering", "retrying"];

export function isDeliveryInFlight(job: WebhookJob | null | undefined): boolean {
  return !!job && IN_FLIGHT_DELIVERY.includes(job.delivery_status);
}

/** Can "Send now" be pressed? Yes while a delivery WAITS (queued, the
 *  minute that batches quick edits, or before a retry): pressing it sends
 *  at once. No only while one is actually being sent; a failed delivery has
 *  its own Retry. */
export function canSendNow(job: WebhookJob | null | undefined): boolean {
  return !job || (job.delivery_status !== "delivering" && job.delivery_status !== "failed");
}

/** The meeting type the context names, from the CURRENT configuration —
 *  null when none is chosen or the customer has since removed it. */
export function selectedType(
  cfg: WebhookConfiguration | null | undefined, ctx: WebhookContext | null | undefined,
): WebhookMeetingType | null {
  const id = ctx?.meeting_type_id;
  if (!id || !cfg) return null;
  return cfg.meeting_types.find((t) => t.id === id) ?? null;
}

/** Whether the record field shows, and whether it is required:
 *  Site Visit -> "required", Internal Meeting -> "hidden",
 *  Customer Call -> "optional". */
export function recordFieldState(type: WebhookMeetingType | null): "hidden" | "optional" | "required" {
  if (!type || !type.record_selection?.enabled) return "hidden";
  return type.record_selection.required ? "required" : "optional";
}

/** The record field's label: the customer's own ("Property"), else "Record". */
export function recordLabel(type: WebhookMeetingType | null): string {
  return (type?.record_selection?.label || "").trim() || "Salesforce record";
}

/** What a picked record reads as. NEVER the id — a Salesforce id is not
 *  something a person recognises, and showing it would leak the CRM's
 *  internals into the meeting screen. */
export function recordDisplay(record: { display_name: string; secondary_text?: string } | null): {
  title: string; sub: string;
} | null {
  if (!record) return null;
  return { title: record.display_name, sub: (record.secondary_text || "").trim() };
}

/** Why Push is disabled, or null when it can go. Mirrors the server's own
 *  checks (_webhook_validate_push) so the button never accepts a tap the
 *  server will refuse. */
export function canPush(
  cfg: WebhookConfiguration | null | undefined,
  ctx: WebhookContext | null | undefined,
  job: WebhookJob | null | undefined,
): string | null {
  if (!cfg || !cfg.fetched_at) return "Salesforce hasn't sent its meeting types yet.";
  if (isDeliveryInFlight(job)) return "Sending to Salesforce…";
  if (!ctx?.meeting_type_id) return "Choose a meeting type first.";
  const type = selectedType(cfg, ctx);
  if (!type) return "This meeting type is no longer available. Choose another.";
  if (recordFieldState(type) === "required" && !ctx.record) {
    return `Choose a ${recordLabel(type)} first.`;
  }
  if (job?.delivery_status === "failed") return "The last delivery failed. Retry it.";
  return null;
}

export function pushButtonLabel(job: WebhookJob | null | undefined): string {
  if (!job) return "Push to Salesforce";
  if (job.delivery_status === "retrying") return "Try again now";
  if (job.delivered) return "Send update now";
  return "Push to Salesforce";
}

/** The delivery status as a person reads it: a headline and one line of
 *  detail. The two FAILED cases are deliberately different: a failure with
 *  no newer changes, and one the meeting has moved on from since. */
export function deliveryLine(job: WebhookJob | null | undefined): {
  title: string; detail: string; tone: "idle" | "busy" | "ok" | "warn";
} {
  if (!job) return { title: "Not sent to Salesforce", detail: "", tone: "idle" };
  switch (job.delivery_status) {
    case "queued":
      return { title: "Queued", detail: "Sending to Salesforce shortly.", tone: "busy" };
    case "update_pending":
      return { title: "Update pending",
               detail: "Recent changes go to Salesforce in about a minute.", tone: "busy" };
    case "delivering":
      return { title: "Sending…", detail: "", tone: "busy" };
    case "retrying":
      return { title: `Retrying (${job.attempt}/${job.max_attempts})`,
               detail: job.last_error_message || "Salesforce didn't accept it yet; MinuteX will try again.",
               tone: "warn" };
    case "delivered":
      return { title: "Delivered", detail: "Updates are sent automatically.", tone: "ok" };
    case "failed":
      return job.has_pending_changes
        ? { title: "Failed — changes pending",
            detail: "The meeting changed after the failed delivery. Retry to send the latest version.",
            tone: "warn" }
        : { title: "Failed",
            detail: job.last_error_message || "No changes since the failed delivery.",
            tone: "warn" };
    default:
      return { title: "Unknown status", detail: "", tone: "idle" };
  }
}

export type SalesforceCardStatus =
  "connected" | "paused" | "setup_needed" | "reconnect" | "not_connected";

/** The Integrations card's status. The receiver row (address + secret) alone
 *  is NOT "connected" where the build uses the Salesforce login: disconnecting
 *  the login deletes only the login, and the receiver row stays behind — it
 *  used to keep the card on CONNECTED after a disconnect.
 *  `login` is the workspace's login connection, or "not_used" in a build
 *  without the login (EXPO_PUBLIC_SALESFORCE_OAUTH off). */
export function salesforceCardStatus(
  s: { mode: WebhookMode; endpoint: { configured: boolean; enabled: boolean } },
  login: { connected: boolean; status: string } | "not_used",
): SalesforceCardStatus {
  const receiver = s.mode === "webhook" && s.endpoint.configured;
  if (login !== "not_used") {
    if (login.status === "reconnect_needed") return "reconnect";
    if (!login.connected) return "not_connected";
    if (!receiver) return "setup_needed";
  } else if (!receiver) {
    return "not_connected";
  }
  return s.endpoint.enabled ? "connected" : "paused";
}

/** One line about the customer's configuration, with its version — what an
 *  admin reads to tell one Salesforce setup from the next. */
export function configStatusLine(cfg: WebhookConfiguration | null | undefined): string {
  if (!cfg || cfg.status === "never") return "Meeting types not loaded yet.";
  const v = cfg.configuration_version ? ` (version ${cfg.configuration_version})` : "";
  const n = cfg.meeting_types.length;
  const types = `${n} meeting type${n === 1 ? "" : "s"}`;
  if (cfg.status === "ok") return `${types}${v}`;
  if (cfg.status === "stale") return `${types}${v} — couldn't refresh from Salesforce; showing the last good list.`;
  return "Couldn't load meeting types from Salesforce.";
}

export function testLine(test: WebhookTest | Record<string, never> | null | undefined): string {
  if (!test || !("at" in test)) return "Not tested yet.";
  if (test.ok) {
    return `Connected — configuration version ${test.configuration_version}` +
      (typeof test.meeting_type_count === "number" ? `, ${test.meeting_type_count} meeting types` : "");
  }
  return `Test failed: ${webhookErrorMessage(test.code || "", test.message || "")}`;
}

/** Wording for the codes a person can act on; otherwise the server's own
 *  message, which is already user-facing. */
export const WEBHOOK_ERROR_MESSAGES: Record<string, string> = {
  webhook_not_configured: "Salesforce isn't connected for this workspace.",
  webhook_disabled: "Salesforce isn't available yet.",
  site_mode_active: "This workspace has an earlier Salesforce connection. Reconnect to replace it.",
  webhook_mode_active: "Salesforce is already connected for this workspace.",
  configuration_unavailable: "MinuteX couldn't load the meeting types from Salesforce yet.",
  meeting_type_required: "Choose a meeting type first.",
  meeting_type_unavailable: "This meeting type is no longer available. Choose another.",
  unknown_meeting_type: "That meeting type isn't available any more.",
  record_required: "Choose a record first.",
  record_selection_disabled: "This meeting type doesn't use a record.",
  transcript_too_large: "This transcript is too long to send to Salesforce.",
  delivery_failed: "The last delivery failed. Retry it.",
  auth_rejected: "Salesforce rejected MinuteX's signature. Check the signing secret in Salesforce.",
  bad_signature: "Salesforce rejected MinuteX's signature. Check the signing secret in Salesforce.",
  endpoint_blocked: "That address isn't a public web address MinuteX can reach.",
  endpoint_not_found: "No MinuteX receiver was found at that address.",
  endpoint_unavailable: "Salesforce isn't answering right now.",
  timeout: "Salesforce didn't answer in time.",
  network_error: "MinuteX couldn't reach Salesforce.",
  redirect_not_followed: "That address redirects. Use the final address.",
  invalid_configuration: "Salesforce sent a meeting type list MinuteX can't use.",
  invalid_search_response: "Salesforce sent search results MinuteX can't use.",
};

export function webhookErrorMessage(code: string, fallback = ""): string {
  return WEBHOOK_ERROR_MESSAGES[code] || fallback || "Something went wrong.";
}

/** Light client-side check before the server's full SSRF validation. */
export function normalizeWebhookUrl(raw: string): { url: string } | { error: string } {
  const url = (raw || "").trim().replace(/\/+$/, "");
  if (!url) return { error: "Enter the Salesforce address." };
  if (!/^https:\/\//i.test(url)) return { error: "The address must start with https://." };
  if (/[?#]/.test(url)) return { error: "The address must not contain ? or #." };
  return { url };
}
