// lib/api.ts — MinuteX backend client (userApi on API Gateway).
//
// One place for: the base URL, the JWT (stored in expo-secure-store), and the
// three calls the app needs — login, list recordings, get one recording.
//
// Backend routes (deployed, ap-south-1):
//   POST /login            {email,password}         -> {token,user_id,email}
//   GET  /recordings       (Bearer JWT)             -> {recordings:[...],count}
//   GET  /recordings/{key} (Bearer JWT)             -> {recording:{...}}
// The recording shape is written by the transcribeRecording Lambda (ElevenLabs
// transcript + Groq analysis).
//
// Token storage — see lib/storage.ts for the graceful-degradation rationale
// (expo-secure-store when available, in-memory fallback otherwise). Shared
// with lib/theme.tsx (light/dark preference) so both go through one proven
// fallback instead of duplicating it.
import { store } from "./storage";

// The HTTP API base URL (same gateway as the upload endpoint).
// Migrated eu-north-1 -> ap-south-1 on 2026-08-01; the JWT secret was copied
// byte-for-byte, so tokens issued by the old stack remain valid here.
export const API_BASE =
  "https://q87zfn5vyj.execute-api.ap-south-1.amazonaws.com";

const TOKEN_KEY = "minutex.jwt";

// ---------------------------------------------------------------------------
// Types — mirror the DynamoDB item the backend returns.
// ---------------------------------------------------------------------------

// Where a recording came from. One backend pipeline serves all three — the
// source only changes the entry point (and the S3 prefix):
//   DEVICE  the MinuteX hardware recorder (device-authenticated presign)
//   MOBILE  in-app phone recording        (JWT presign, /recordings/upload-request)
//   UPLOAD  an imported audio file        (JWT presign, /recordings/upload-request)
export type RecordingSource = "DEVICE" | "MOBILE" | "UPLOAD";

// The processing lifecycle every recording moves through (any source):
//   uploading -> uploaded -> transcribing -> generating_ai -> complete
// "transcribed" is the legacy "transcript ok, AI analysis failed" state;
// "failed" is an unrecoverable transcription error.
export type RecordingStatus =
  | "uploading" | "uploaded" | "transcribing" | "generating_ai"
  | "complete" | "transcribed" | "failed";

export type Participant = {
  speaker: string;
  summary: string;
};

export type TimestampSeg = {
  speaker: string;
  start: number;
  end: number;
  text: string;
};

// The lightweight shape returned by the LIST endpoint.
export type RecordingSummary = {
  audio_s3_key: string;
  recording_id?: string;
  device_id: string | null; // null for MOBILE/UPLOAD recordings
  source?: RecordingSource; // backend derives it for legacy rows
  meeting_id: string;
  recorded_at: string;
  // Seconds. DynamoDB Decimals serialize as strings, so tolerate both.
  duration?: number | string | null;
  title: string;
  summary: string;
  language: string;
  status: RecordingStatus | string;
  created_at: string;
  // User-supplied names for the diarization labels ("0" -> "Ravi"). The audio
  // carries no names — ElevenLabs only separates voices — so this map is the
  // ONLY source of real identities. Stored beside the transcript rather than
  // baked into it, so the transcript stays the verbatim record and a name can
  // be corrected at any time, including on old recordings. Absent until the
  // user names someone.
  speaker_names?: Record<string, string> | null;
  // The folder this meeting is filed under, or "" for General (= no folder).
  // A meeting belongs to at most ONE folder and is never duplicated across
  // them — folder views filter this one master list.
  folder_id?: string;
};

// A recording in Trash: the same summary the Desk renders, plus when it was
// deleted. `status` still carries the PIPELINE status it had when it was
// trashed (complete, failed, …) — trashing never overwrites it, which is what
// lets Restore put the recording back exactly as it was.
export type TrashedRecording = RecordingSummary & {
  deleted_at: string;
};

// ---------------------------------------------------------------------------
// AI Meeting Workspace — the structured highlights the workspace renders.
//
// Produced by the transcribe Lambda right after the executive summary, and
// regenerable on demand (POST /recordings/ai/highlights/{key}) for recordings
// that predate the feature or whose second Groq call was rate-limited.
// ---------------------------------------------------------------------------
export type HighlightDecision = { decision: string; context: string };
export type HighlightAction = { task: string; owner: string; deadline: string };
export type HighlightDeadline = { what: string; when: string };
export type HighlightNumberKind =
  | "money" | "quantity" | "percentage" | "measurement" | "duration";
export type HighlightNumber = {
  label: string;
  value: string;
  kind: HighlightNumberKind | string;
};

export type MeetingHighlights = {
  decisions: HighlightDecision[];
  action_items: HighlightAction[];
  deadlines: HighlightDeadline[];
  important_numbers: HighlightNumber[];
  open_questions: string[];
  risks: string[];
};

// The eight AI document types. Mirrors prompts.DOCUMENT_KEYS on the backend —
// which also ADVERTISES the list via GET .../documents, so the workspace
// renders whatever the backend offers rather than trusting this union.
export type DocumentType =
  | "minutes_of_meeting"
  | "executive_summary"
  | "follow_up_email"
  | "whatsapp_summary"
  | "action_items"
  | "sales_meeting_report"
  | "site_visit_report"
  | "customer_requirement_report";

export type AiDocument = {
  type: string;
  label: string;
  content: string;      // Markdown
  format: string;
  generated_at: string;
  // True once the user has edited it. An edited document is never overwritten
  // by a regeneration unless the user explicitly asks.
  edited: boolean;
  ai_version: string;
  // True for a freeform (custom_<id>) document — no fixed prompt/type to
  // regenerate against, same as the old chat-drafted documents used to be,
  // except this one IS persisted server-side.
  is_custom?: boolean;
  // The recording's speaker_mapping_version at the time this document was
  // generated — compared against RecordingDetail.speaker_mapping_version to
  // compute `status`. Present once the backend adds it; absent (older
  // backend/cached shape) reads the same as 0.
  speaker_mapping_version?: number;
  // "needs_update" when a speaker was renamed since this document was
  // generated (and it isn't hand-edited — an edit is never flagged). Drives
  // the per-row indicator and the Documents list's "Update All" banner.
  status?: "current" | "needs_update";
};

// One entry per document type from GET .../documents, so the workspace can
// render all eight buttons and show which already have output in one call.
export type AiDocumentSlot = {
  type: string;
  label: string;
  generated: boolean;
  fresh: boolean;
};

export type ChatTurn = {
  role: "user" | "assistant";
  content: string;
  at?: string;
};

export type ChatSuggestionGroup = { group: string; prompts: string[] };

// The full shape returned by the DETAIL endpoint.
export type RecordingDetail = RecordingSummary & {
  transcript: string;
  timestamps: TimestampSeg[];
  participants: Participant[];
  // Short-lived presigned S3 GET url for playback. Null if the backend
  // couldn't generate one (e.g. bucket misconfigured) — playback UI should
  // hide itself rather than error in that case.
  audio_url: string | null;
  // The PRIMARY user-facing highlights: 3-7 plain strings, produced directly
  // by the single-pass analysis (title/summary/highlights/tasks/participants —
  // the whole schema). Replaces building highlight rows from
  // meeting_highlights' nested decisions/action_items/deadlines/numbers — that
  // structured extraction still exists (below) for the Assistant's chat
  // context, but the app renders this flat list instead. Absent on a recording
  // processed before this shipped; Highlights (lib/meeting-summary.tsx) just
  // renders nothing.
  //
  // The analysis's agenda/key_points/decisions/pending_discussions/
  // action_items fields were REMOVED from the schema; `summary` (now written
  // at real length, organized by topic) and this list cover their ground. A
  // row processed before the removal may still carry them over the wire —
  // nothing reads them, and reprocessing clears them.
  highlights?: string[];
  // "placeholder" | "ai" | "user" — which kind of title is currently stored.
  // Absent on an older row (equivalent to "placeholder" if title is blank,
  // "user" otherwise — see lambda-transcribe-live's title_source handling).
  // The app doesn't need to branch on this today; it exists so a future
  // "this title was auto-generated" affordance has something to read.
  title_source?: "placeholder" | "ai" | "user";
  // ---- AI Meeting Workspace ----
  // All optional: a recording processed before the workspace shipped has none
  // of them, and the workspace generates what's missing on first open.
  meeting_highlights?: MeetingHighlights | null;
  documents?: Record<string, AiDocument> | null;
  chat_history?: ChatTurn[] | null;
  // Bumped server-side every time speaker_names actually changes (see
  // patch_recording). Documents stamp the version they were generated
  // under; comparing the two is how the app knows a document may still say
  // an old name. Absent on an older backend response — treat as 0.
  speaker_mapping_version?: number;
  // Record identifiers for CRM linking, keyed by Salesforce OBJECT name —
  // whatever objects the user configured. Absent or empty on most meetings, and
  // always empty for a user with no Salesforce mappings; that is the NORMAL
  // case (the extractor only returns a value when it was actually spoken, and
  // an ungrounded guess is discarded server-side), so the UI treats "nothing
  // here" as ordinary and simply renders no inputs.
  crm_records?: Record<string, CrmRecordValue> | null;
};

// Where one mapping stands for one meeting. Object-neutral: the same states
// describe a site visit, a lead or a custom object, and the UI branches ONLY on
// these — so a new Salesforce object needs no new UI condition.
//
//   not_linked      no identifier yet (the common resting state)
//   lookup_pending  an identifier exists but hasn't been resolved
//   record_found    resolved to one record, awaiting the user's confirmation
//   ambiguous       matched several records; the user must pick one
//   confirmed       approved for syncing
//   syncing         a push is in flight
//   synced          the configured fields were written to Salesforce
//   failed          the last lookup or push failed; retryable
export type CrmSyncStatus =
  | "not_linked" | "lookup_pending" | "record_found" | "ambiguous"
  | "confirmed" | "syncing" | "synced" | "failed";

// One ambiguous-lookup candidate the user chooses between.
export type CrmCandidate = { record_id: string; display_name: string };

// One extracted (or hand-entered) CRM record association — a site visit number,
// a lead email, an opportunity number, whatever the configured object uses.
//
// `lookup_value` is the user's visible business reference; `record_id` is the
// internal Salesforce association key, and once resolved it is what every
// subsequent sync writes to (no repeated SOQL).
//
// `confidence` is a coarse label, not a float the model emitted:
//   explicit — stated outright in the meeting
//   probable — clearly this meeting's record, said less directly
//   manual   — typed by the user, which outranks any extraction
// `evidence` is the verbatim transcript sentence the value came from ("" for
// manual entry). Show it before confirming: it is what makes a wrong extraction
// auditable before anything is pushed to Salesforce.
export type CrmRecordValue = {
  object: string;
  label: string;
  lookup_field: string;
  lookup_value: string;
  record_id: string;
  record_label?: string;
  status: CrmSyncStatus;
  source?: "manual" | "ai" | "";
  confidence?: string;
  evidence?: string;
  candidates?: CrmCandidate[];
  error?: string;
  synced_at?: string;
  updated_at?: string;
};

// POST /crm/salesforce/lookup — three ordinary outcomes, not two plus an error.
export type CrmLookupResult = {
  status: "found" | "not_found" | "ambiguous";
  object: string;
  label: string;
  lookup_field: string;
  lookup_value: string;
  record_id?: string;
  record_label?: string;
  records?: CrmCandidate[];
  truncated?: boolean;
};

export type LoginResult = { token: string; user_id: string; email: string; name?: string };

export type UserProfile = {
  user_id: string;
  email: string;
  name: string;
  avatar_url: string;
  created_at: string;
};

export type DeviceStatus = "UNPAIRED" | "PAIRING" | "PAIRED";

// A device row as the backend reports it. `name` always has a value (the
// backend falls back to device_id). battery/storage are null until the
// firmware reports them — the dashboard substitutes mocked values.
export type DeviceInfo = {
  device_id: string;
  name: string;
  status: DeviceStatus;
  paired_at: string | null;
  last_seen: string | number | null;
  firmware_version: string | null;
  serial_number: string | null;
  battery: number | null;
  storage: number | null;
};

// Salesforce connection state. Only ever describes the connection — tokens
// live server-side (KMS-encrypted) and are never sent to the app.
export type SalesforceStatus = {
  connected: boolean;
  instance_url?: string;
  sf_username?: string;
  connected_at?: string;
};

// ---- Salesforce schema mapping ----
// These describe the USER'S org, read live from Salesforce's Describe API.
// Nothing here is a constant in our code: "SiteVisit__c" is one org's name for
// it, not the product's.
export type SalesforceObject = {
  name: string;    // API name, e.g. "SiteVisit__c"
  label: string;   // what the org's users call it, e.g. "Site Visit"
  custom: boolean;
};

export type SalesforceField = {
  name: string;
  label: string;
  type: string;
  length: number;
  custom: boolean;
};

// The four data targets are keyed exactly as the backend expects them.
export type SalesforceFieldKey =
  | "lookup_field"
  | "transcript_field"
  | "summary_field"
  | "highlights_field"
  | "action_items_field";

export type SalesforceFieldsResponse = {
  object: string;
  label: string;
  // Filterable fields — a lookup key must be searchable to find a record.
  number_fields: SalesforceField[];
  // Writable long-text fields — anything shorter would truncate a transcript.
  long_text_fields: SalesforceField[];
  suggested: Partial<Record<SalesforceFieldKey, string | null>>;
  transcript_min_length: number;
};

// One configured object: "this Salesforce object, identified by this field".
// The UI renders one input per mapping and knows nothing about which objects
// exist — adding Lead or Opportunity support is a configuration change, not a
// code change.
export type CrmMapping = {
  object: string;             // API name, e.g. "SiteVisit__c" | "Lead"
  object_label: string;       // the org's words, e.g. "Site Visit" | "Lead"
  lookup_field: string;       // e.g. "Site_Visit_Number__c" | "Email"
  lookup_field_label: string; // the org's label for that field
  lookup_field_type?: string; // Salesforce type — drives the keyboard
  label: string;              // what to call the input, e.g. "Lead Email"
  transcript_field: string | null;
  summary_field: string | null;
  highlights_field: string | null;
  action_items_field: string | null;
  // The same four targets, nested and containing ONLY the configured ones.
  // The push sends exactly these fields; an unmapped target is absent, never
  // blanked. Use this to tell the user what a sync will actually write.
  content_targets: Partial<Record<
    "transcript" | "summary" | "highlights" | "action_items", string>>;
};

// The whole client-facing Salesforce configuration.
//
// `mappings` is the contract the meeting UI renders from: one input per entry,
// and NOTHING when the list is empty. An empty list is a legitimate state
// meaning "connected, but no record fields" — not an error.
export type SalesforceConfig = {
  enabled: boolean;
  mappings: CrmMapping[];
  // True when at least one mapping exists — i.e. record linking is usable.
  configured: boolean;
  updated_at: string | null;
};

export type SalesforceMappingInput = {
  object: string;
  lookup_field: string;
  label?: string;
  transcript_field?: string | null;
  summary_field?: string | null;
  highlights_field?: string | null;
  action_items_field?: string | null;
};

export type SalesforceConfigInput = {
  // Possibly empty: saving [] is how a user turns record linking off without
  // disconnecting Salesforce.
  mappings: SalesforceMappingInput[];
};

// ---------------------------------------------------------------------------
// Token storage
// ---------------------------------------------------------------------------
export async function saveToken(token: string): Promise<void> {
  await store.setItemAsync(TOKEN_KEY, token);
}

export async function getToken(): Promise<string | null> {
  return store.getItemAsync(TOKEN_KEY);
}

export async function clearToken(): Promise<void> {
  await store.deleteItemAsync(TOKEN_KEY);
}

// ---------------------------------------------------------------------------
// HTTP helper. Adds the Bearer token when we have one; throws ApiError with a
// useful message on non-2xx so screens can show it.
// ---------------------------------------------------------------------------
export class ApiError extends Error {
  status: number;
  // A stable machine-readable identifier the backend attaches to errors that
  // need branching on identity rather than on wording. Empty for the ordinary
  // errors, which only carry prose.
  code: string;
  constructor(status: number, message: string, code = "") {
    super(message);
    this.status = status;
    this.code = code;
  }
}

// The backend's "your MinuteX session is fine, but the SALESFORCE credential
// behind this route is dead" answer (HTTP 409 + this code).
//
// It is deliberately NOT a 401. Salesforce-backed routes used to report a dead
// refresh token as 401, which is indistinguishable on the wire from an expired
// MinuteX JWT — so opening CRM Mapping after the Salesforce connection went
// stale ran the session-expiry path below, cleared a perfectly valid token and
// redirected the user to the login screen. Reconnecting Salesforce is the fix
// for this error; signing out of MinuteX never was.
export const SALESFORCE_RECONNECT_CODE = "salesforce_reconnect_required";

export function isSalesforceReconnect(e: unknown): boolean {
  return e instanceof ApiError && e.code === SALESFORCE_RECONNECT_CODE;
}

async function request<T>(
  path: string,
  opts: {
    method?: string;
    body?: unknown;
    auth?: boolean;
    // A 401 on an authenticated call normally means the stored JWT is dead,
    // so we clear it centrally (see below). Set false for endpoints that
    // ALSO validate a credential taken from the request body: /me/password
    // answers 401 for "current password is incorrect", and signing the user
    // out because they fat-fingered it would be wrong.
    sessionExpiryOn401?: boolean;
  } = {}
): Promise<T> {
  const { method = "GET", body, auth = true, sessionExpiryOn401 = true } = opts;
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (auth) {
    const token = await getToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
  }

  let resp: Response;
  try {
    resp = await fetch(`${API_BASE}${path}`, {
      method,
      headers,
      body: body != null ? JSON.stringify(body) : undefined,
    });
  } catch (e: any) {
    // Network failure (offline, DNS, etc.) — surface a clean message.
    throw new ApiError(0, "Network error — check your connection.");
  }

  const text = await resp.text();
  let data: any = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }

  if (!resp.ok) {
    // Drop the dead token at the ONE place every authenticated call passes
    // through. Screens used to each remember to clearToken() before
    // redirecting to /login, and the four that forgot caused an infinite
    // redirect loop: the root layout's auth gate re-read the still-present
    // (but expired) token, concluded the user was authed, and bounced them
    // straight back to the tab that had just 401'd — which 401'd again, at
    // network speed, forever. Clearing here means no screen can reintroduce
    // that by forgetting.
    if (resp.status === 401 && auth && sessionExpiryOn401) await clearToken();
    const msg = (data && (data.error || data.message)) || `Request failed (${resp.status})`;
    const code = (data && typeof data.code === "string" && data.code) || "";
    throw new ApiError(resp.status, msg, code);
  }
  return data as T;
}

// ---------------------------------------------------------------------------
// API calls
// ---------------------------------------------------------------------------
export async function login(email: string, password: string): Promise<LoginResult> {
  const res = await request<LoginResult>("/login", {
    method: "POST",
    body: { email, password },
    auth: false,
  });
  await saveToken(res.token);
  return res;
}

export async function signup(
  email: string,
  password: string,
  name?: string
): Promise<LoginResult> {
  const res = await request<LoginResult>("/signup", {
    method: "POST",
    body: { email, password, name },
    auth: false,
  });
  await saveToken(res.token);
  return res;
}

// ---- Profile ----
export async function getMe(): Promise<UserProfile> {
  const res = await request<{ user: UserProfile }>("/me");
  return res.user;
}

export async function updateMe(
  patch: { name?: string; avatar_url?: string }
): Promise<UserProfile> {
  const res = await request<{ user: UserProfile }>("/me", {
    method: "PATCH",
    body: patch,
  });
  return res.user;
}

export async function changePassword(
  current_password: string,
  new_password: string
): Promise<void> {
  await request("/me/password", {
    method: "POST",
    body: { current_password, new_password },
    // The backend returns 401 for BOTH "expired session" and "current
    // password is incorrect", so a 401 here can't be read as session death.
    // Opt out of the auto-clear rather than sign the user out mid-typo; a
    // genuinely expired session gets cleared by the next /me or /recordings
    // call the app makes.
    sessionExpiryOn401: false,
  });
}

// Link a device to the logged-in account using its device API key.
export async function claimDevice(apiKey: string): Promise<{ device_id: string }> {
  return request("/devices/claim", { method: "POST", body: { apiKey } });
}

export async function getDevicesList(): Promise<string[]> {
  const res = await request<{ devices: string[] }>("/devices");
  return res.devices ?? [];
}

// Full device rows. `devices` (ids only) is kept above for older callers;
// this returns the detail the dashboard renders.
export async function getDevices(): Promise<DeviceInfo[]> {
  const res = await request<{ devices: string[]; details?: DeviceInfo[] }>("/devices");
  return res.details ?? [];
}

export async function getDevice(deviceId: string): Promise<DeviceInfo> {
  const res = await request<{ device: DeviceInfo }>(
    `/devices/${encodeURIComponent(deviceId)}`
  );
  return res.device;
}

// Step 1 of pairing: ask the backend for the 6-digit code. The device shows
// the same code, and the user confirms it in pairDevice().
export async function requestPairing(
  deviceId: string
): Promise<{ pairing_code: string; expires_in: number }> {
  return request("/devices/pair-request", {
    method: "POST",
    body: { device_id: deviceId },
  });
}

// Step 2: confirm. 409 => the device already belongs to another account.
export async function pairDevice(
  deviceId: string,
  pairingCode: string
): Promise<DeviceInfo> {
  const res = await request<{ device: DeviceInfo }>("/devices/pair", {
    method: "POST",
    body: { device_id: deviceId, pairing_code: pairingCode },
  });
  return res.device;
}

// Cosmetic label. Pass "" to clear it back to the device id.
export async function renameDevice(
  deviceId: string,
  name: string
): Promise<DeviceInfo> {
  const res = await request<{ device: DeviceInfo }>(
    `/devices/${encodeURIComponent(deviceId)}`,
    { method: "PATCH", body: { name } }
  );
  return res.device;
}

// Releases ownership only — recordings stay in the account.
export async function unpairDevice(deviceId: string): Promise<void> {
  await request(`/devices/${encodeURIComponent(deviceId)}`, { method: "DELETE" });
}

// Wipes the hardware AND unpairs it. Recordings are kept, same as unpair.
export async function factoryResetDevice(
  deviceId: string
): Promise<{ device_id: string; reset: boolean }> {
  return request(`/devices/${encodeURIComponent(deviceId)}/factory-reset`, {
    method: "POST",
  });
}

// ---- Salesforce (CRM) ----
// Three-legged OAuth: this returns the Salesforce authorize URL to open in a
// browser. The backend handles the redirect + code exchange, so the app never
// sees a Salesforce token — see lib/salesforce.ts for the browser half.
export async function getSalesforceAuthorizeUrl(): Promise<string> {
  const res = await request<{ authorize_url: string }>("/crm/salesforce/connect");
  return res.authorize_url;
}

export async function getSalesforceStatus(): Promise<SalesforceStatus> {
  return request<SalesforceStatus>("/crm/salesforce/status");
}

export async function disconnectSalesforce(): Promise<void> {
  await request("/crm/salesforce", { method: "DELETE" });
}

// ---- Salesforce configuration (the user maps their OWN org's schema) ----
// No object or field API name is hardcoded anywhere in the app: every org
// names things differently, so the picker is populated from the org's live
// Describe metadata and `suggested` is only a pre-selection the user confirms.
export async function getSalesforceObjects(): Promise<{
  objects: SalesforceObject[];
  suggested: string | null;
}> {
  return request("/crm/salesforce/objects");
}

export async function getSalesforceFields(
  objectName: string
): Promise<SalesforceFieldsResponse> {
  return request(`/crm/salesforce/fields/${encodeURIComponent(objectName)}`);
}

export async function getSalesforceConfig(): Promise<SalesforceConfig> {
  const res = await request<{ config: SalesforceConfig }>("/crm/salesforce/config");
  return res.config;
}

export async function saveSalesforceConfig(
  config: SalesforceConfigInput
): Promise<SalesforceConfig> {
  const res = await request<{ config: SalesforceConfig }>("/crm/salesforce/config", {
    method: "PUT",
    body: config,
  });
  return res.config;
}

// Resolve an identifier to a Salesforce record Id using the configured object +
// lookup field. Stateless — it reports what Salesforce says and persists
// nothing, so a lookup can never silently relink a meeting.
export async function lookupCrmRecord(
  object: string,
  lookupValue: string
): Promise<CrmLookupResult> {
  return request<CrmLookupResult>("/crm/salesforce/lookup", {
    method: "POST",
    body: { object, lookup_value: lookupValue },
  });
}

// Push the meeting's configured content onto the confirmed record. Requires a
// confirmed association — the backend refuses an unconfirmed one.
export async function syncCrmRecord(
  key: string,
  object: string
): Promise<{ crm_record: CrmRecordValue; synced_fields: string[] }> {
  return request(`/crm/salesforce/sync/${encodeURIComponent(key)}`, {
    method: "POST",
    body: { object },
  });
}

export async function getRecordings(): Promise<RecordingSummary[]> {
  const res = await request<{ recordings: RecordingSummary[] }>("/recordings");
  return res.recordings ?? [];
}

export async function getRecording(key: string): Promise<RecordingDetail> {
  // Encode the key (it contains "/", e.g. "esp32-001/Meeting.wav"). The backend
  // route is greedy ({key+}) and decodes it back.
  const res = await request<{ recording: RecordingDetail }>(
    `/recordings/${encodeURIComponent(key)}`
  );
  return res.recording;
}

// ---------------------------------------------------------------------------
// User uploads (MOBILE / UPLOAD sources) — the JWT twin of the device presign.
// The flow (request -> S3 PUT -> complete) is orchestrated by lib/uploads.tsx;
// these are just the two backend calls.
// ---------------------------------------------------------------------------
export type UploadTicket = {
  upload_url: string;   // presigned S3 PUT — send the audio bytes here
  key: string;          // the recording's audio_s3_key (timeline identity)
  recording_id: string;
  expires_in: number;
  // Advisory ONLY — deliberately NOT part of the signature. The backend omits
  // ContentType when presigning (see request_upload in the userApi Lambda) so
  // clients don't have to reproduce a byte-identical header; the file
  // extension carries the format. Sending a Content-Type that differs from a
  // signed one would be a 403, so lib/uploads.tsx sends none at all. Do not
  // "fix" this by signing it.
  content_type: string;
  /** The folder the row was filed into, or "" for General. */
  folder_id?: string;
};

export async function requestUpload(params: {
  source: Exclude<RecordingSource, "DEVICE">;
  format?: string;   // wav | mp3 | m4a | aac | ogg (default wav)
  title?: string;
  duration?: number; // seconds, if known
  size?: number;     // bytes, if known — backend rejects oversize before the PUT
  // File the recording into a folder at PRESIGN time, for a recording started
  // from inside one. The row is created already carrying its folder, so the
  // meeting is never briefly visible in General and killing the app mid-upload
  // cannot leave it unfiled. A folder the caller does not own fails the whole
  // request (404) rather than yielding an unfiled recording.
  folder_id?: string;
}): Promise<UploadTicket> {
  return request<UploadTicket>("/recordings/upload-request", {
    method: "POST",
    body: params,
  });
}

// PATCH /recordings/{key} — the only user-editable fields on a recording.
// Pass a speaker_names map to rename speakers; a blank/absent name clears
// that mapping. Returns the updated row so callers can adopt it directly.
//
// crm_records is the CRM association path, keyed by Salesforce object name.
// Per object, send either:
//   * a string — the identifier alone (clears any record resolved from a
//     different identifier, since that association no longer applies);
//   * null/"" — remove the association entirely (undo a wrong extraction);
//   * {lookup_value, record_id, status} — the identifier plus a record already
//     resolved by /crm/salesforce/lookup, so confirming needs no extra round
//     trip. `syncing`/`synced` are refused here; only the sync route sets those.
// Omit the key entirely to leave associations untouched.
export async function updateRecording(
  key: string,
  patch: {
    speaker_names?: Record<string, string>;
    title?: string;
    crm_records?: Record<string, string | null | {
      lookup_value: string;
      record_id?: string;
      record_label?: string;
      status?: CrmSyncStatus;
    }>;
  }
): Promise<RecordingDetail> {
  const res = await request<{ recording: RecordingDetail }>(
    `/recordings/${encodeURIComponent(key)}`,
    { method: "PATCH", body: patch }
  );
  return res.recording;
}

// ---------------------------------------------------------------------------
// Trash — soft delete, restore, permanent delete
//
// Delete is TWO STEPS. trashRecording moves a meeting to Trash and destroys
// NOTHING: the audio, the transcript and every AI artifact stay exactly where
// they are, on the same row. Only permanentlyDeleteRecording removes data, and
// only the Trash screen calls it.
//
// PATH SHAPE — restore/permanent put the ACTION BEFORE the key, matching the
// AI routes above and for the same reason: API Gateway only permits a greedy
// path variable in the final position, and a recording key contains slashes.
// "/recordings/{key}/restore" cannot exist as a route at all.
// ---------------------------------------------------------------------------

// DELETE /recordings/{key} — move to Trash. Reversible via restoreRecording;
// the confirm dialogs say so rather than warning about a permanent loss.
export async function trashRecording(key: string): Promise<void> {
  await request(`/recordings/${encodeURIComponent(key)}`, { method: "DELETE" });
}

// GET /trash — the trashed twin of getRecordings. Same summary shape plus
// deleted_at, newest deletion first.
export async function getTrash(): Promise<TrashedRecording[]> {
  const res = await request<{ recordings: TrashedRecording[] }>("/trash");
  return res.recordings ?? [];
}

// POST /recordings/restore/{key} — back to the Desk with its transcript and
// AI artifacts intact. Nothing is re-transcribed; nothing was ever removed.
export async function restoreRecording(key: string): Promise<void> {
  await request(`/recordings/restore/${encodeURIComponent(key)}`, {
    method: "POST",
    body: {},
  });
}

// DELETE /recordings/permanent/{key} — the ONLY call that destroys data:
// the S3 audio, the S3 transcript and the DynamoDB row with every AI artifact
// on it. Irreversible. A recording whose bytes are still landing answers 409
// (see isStillUploading) because the transcription pipeline would otherwise
// resurrect the row it just deleted.
export async function permanentlyDeleteRecording(key: string): Promise<void> {
  await request(`/recordings/permanent/${encodeURIComponent(key)}`, {
    method: "DELETE",
  });
}

// A 409 from the permanent-delete route means the upload is still in flight —
// the only case the backend refuses. Distinct from isNotReady (same status,
// different route) so the delete call sites read as what they are.
export function isStillUploading(e: unknown): boolean {
  return e instanceof ApiError && e.status === 409;
}

export async function completeUpload(
  key: string,
  duration?: number
): Promise<{ key: string; status: string }> {
  return request("/recordings/upload-complete", {
    method: "POST",
    body: duration != null ? { key, duration } : { key },
  });
}

// ---------------------------------------------------------------------------
// AI Meeting Workspace
//
// PATH SHAPE — note the action comes BEFORE the recording key:
//   /recordings/ai/{action}/{key}     not   /recordings/{key}/{action}
// API Gateway only permits a greedy path variable in the FINAL position
// ("Greedy variables may only be in last position of route key"), and a
// recording key contains slashes, so it has to be greedy and has to be last.
// aiPath() builds these so no call site has to remember the ordering.
//
// Every one of these is a Groq call behind the scenes and can take a few
// seconds; they all cache server-side on a fingerprint of the transcript, so
// repeating a request that hasn't changed is free (`cached: true`).
// ---------------------------------------------------------------------------
function aiPath(action: string, key: string): string {
  return `/recordings/ai/${action}/${encodeURIComponent(key)}`;
}

// A 502 means Groq itself failed and retrying may well work — that's the
// "Unable to generate AI output. Retry" case. Anything else is a real error.
export function isRetryable(e: unknown): boolean {
  return e instanceof ApiError && (e.status === 502 || e.status === 0);
}

// A 409 means the recording isn't transcribed yet — the workspace keeps
// showing its progress UI rather than an error.
export function isNotReady(e: unknown): boolean {
  return e instanceof ApiError && e.status === 409;
}

export async function getMeetingHighlights(
  key: string,
  regenerate = false
): Promise<{ meeting_highlights: MeetingHighlights; cached: boolean }> {
  return request(aiPath("highlights", key), {
    method: "POST",
    body: { regenerate },
  });
}

// What has been generated, plus every type the backend offers. One call, so
// the workspace can render all eight document buttons with their state.
export async function listAiDocuments(key: string): Promise<{
  documents: Record<string, AiDocument>;
  available: AiDocumentSlot[];
  speakerMappingVersion: number;
  documentsNeedingUpdate: string[];
}> {
  const res = await request<{
    documents?: Record<string, AiDocument>;
    available?: AiDocumentSlot[];
    speaker_mapping_version?: number;
    documents_needing_update?: string[];
  }>(aiPath("documents", key));
  return {
    documents: res.documents ?? {},
    available: res.available ?? [],
    speakerMappingVersion: res.speaker_mapping_version ?? 0,
    documentsNeedingUpdate: res.documents_needing_update ?? [],
  };
}

// "Update All" — regenerates every document currently flagged needs_update
// against the CURRENT speaker names. Runs against a shared deadline on the
// backend, so a large meeting with many stale documents may come back with
// some left in `remaining` — call again to finish those.
export async function updateStaleDocuments(key: string): Promise<{
  updated: { type: string; document: AiDocument }[];
  remaining: string[];
}> {
  const res = await request<{
    updated?: { type: string; document: AiDocument }[];
    remaining?: string[];
  }>(aiPath("update-documents", key), { method: "POST", body: {} });
  return { updated: res.updated ?? [], remaining: res.remaining ?? [] };
}

// A 429 from the reprocess route: a replay is already running. Not a failure
// the user should see as an error — it means "wait, it's working".
export function isAlreadyRunning(e: unknown): boolean {
  return e instanceof ApiError && e.status === 429;
}

// Re-run the WHOLE pipeline (transcription + AI analysis) for a recording that
// failed or stalled. Returns as soon as the work is ACCEPTED (202), not when
// it finishes — the pipeline takes minutes, so the caller polls `status` the
// same way it does after a first-time upload.
//
// Costs real money per call (ElevenLabs + Groq), which is why the backend
// enforces a cooldown and the UI confirms before calling this.
export async function reprocessRecording(key: string): Promise<{
  status: string;
  startedAt: string;
  previousStatus: string;
}> {
  const res = await request<{
    status?: string;
    started_at?: string;
    previous_status?: string;
  }>(aiPath("reprocess", key), { method: "POST", body: {} });
  return {
    status: res.status ?? "transcribing",
    startedAt: res.started_at ?? "",
    previousStatus: res.previous_status ?? "",
  };
}

export async function generateAiDocument(
  key: string,
  type: string,
  regenerate = false
): Promise<{ document: AiDocument; cached: boolean }> {
  return request(aiPath("documents", key), {
    method: "POST",
    body: { type, regenerate },
  });
}

// Documents are editable. Saving marks the document as edited, which protects
// it from being overwritten by a later regeneration. Pass `label` (alone or
// alongside content) to rename — the backend persists it, unlike the old
// client-only rename.
export async function saveAiDocument(
  key: string,
  type: string,
  patch: { content?: string; label?: string }
): Promise<AiDocument> {
  const res = await request<{ document: AiDocument }>(aiPath("documents", key), {
    method: "PATCH",
    body: { type, ...patch },
  });
  return res.document;
}

export async function deleteAiDocument(key: string, type: string): Promise<void> {
  await request(aiPath("documents", key), { method: "DELETE", body: { type } });
}

// Freeform "generate any document" — the real backend counterpart to what
// used to silently reroute through chat. Always persisted immediately
// server-side with an AI-generated title, so it shows up in Documents(N)
// exactly like a template document (survives reload, is renameable/deletable
// through the same routes as any other document).
export async function generateCustomDocument(
  key: string,
  prompt: string
): Promise<{ document: AiDocument }> {
  return request(aiPath("custom-document", key), {
    method: "POST",
    body: { prompt },
  });
}

// Quick AI — one tap, no typing. Several actions alias a document type and so
// share its cache entry; the response says which type it stored under.
export async function runQuickAction(
  key: string,
  action: string,
  regenerate = false
): Promise<{ document: AiDocument; action: string; cached: boolean }> {
  return request(aiPath("quick", key), {
    method: "POST",
    body: { action, regenerate },
  });
}

export async function getAiChat(
  key: string
): Promise<{ chat_history: ChatTurn[]; suggestions: ChatSuggestionGroup[] }> {
  const res = await request<{
    chat_history?: ChatTurn[];
    suggestions?: ChatSuggestionGroup[];
  }>(aiPath("chat", key));
  return { chat_history: res.chat_history ?? [], suggestions: res.suggestions ?? [] };
}

// `history` lets the conversation stay coherent before anything is persisted.
// The backend trims and sanitizes it, and bounds how much is sent to Groq —
// so passing the whole on-screen thread is safe.
export async function sendAiChat(
  key: string,
  message: string,
  history?: ChatTurn[]
): Promise<{ reply: string; chat_history: ChatTurn[] }> {
  return request(aiPath("chat", key), {
    method: "POST",
    body: history?.length ? { message, history } : { message },
  });
}

export async function clearAiChat(key: string): Promise<void> {
  await request(aiPath("chat", key), { method: "DELETE" });
}

// ---------------------------------------------------------------------------
// Tasks — persisted assign/notify (Task Detail -> Assign To -> Notify).
//
// Core fields only (task/due/priority/status/assignee/notified_via) — see
// lambda-userapi's Tasks section for why subtasks/attachments/notes/activity
// deliberately stay client-side rather than moving here too.
// ---------------------------------------------------------------------------
export type ApiTaskAssignee = {
  name: string;
  phone?: string;
  email?: string;
  source: "team" | "recent" | "phone" | "manual";
};

// Tasks are now FIRST-CLASS backend entities (their own table), not entries in
// the recording row. Every field below that existed before still means exactly
// what it did — the shape was kept backward compatible on purpose so this
// change needed no coordinated release — and the new fields carry the identity
// and organization context the Task Tracker is built on.
//
// The assignee story is the important one. `assignee` (a NAME) and
// `assignee_contact_id` (an IDENTITY) are different things, and
// `resolution_status` says which one you actually have:
//   RESOLVED    a real Contact is attached; assignee_contact_id is set
//   UNRESOLVED  only a name is known (the AI heard "Rahul", or a legacy task
//               carried a typed name) — the app must offer to resolve it and
//               must NOT present it as a confirmed person
//   AMBIGUOUS   several contacts could match; the user has to choose
//   NONE        nobody is assigned, which is a normal state, not an error
export type TaskStatusV2 = "Open" | "In Progress" | "Completed" | "Cancelled";
export type TaskResolutionStatus =
  | "RESOLVED"
  | "UNRESOLVED"
  | "AMBIGUOUS"
  | "NONE";
export type TaskSourceType = "AI" | "MANUAL" | "LEGACY";

export type ApiTask = {
  id: string;
  task: string;
  due: string;
  priority: "Low" | "Medium" | "High";
  status: TaskStatusV2;
  assignee: ApiTaskAssignee | null;
  notified_via: string[];
  created_at: string;
  updated_at: string;
  from_action_item: boolean;
  // --- first-class fields ---
  title?: string;
  description?: string;
  due_date?: string;
  // Computed server-side from due_date + status, never stored — so it is
  // always correct rather than correct-as-of-the-last-write.
  is_overdue?: boolean;
  assignee_contact_id?: string;
  // The MinuteX account behind the assignee, when they have one. This is what
  // makes a task notification-ready; empty means "no account", not "unknown".
  assignee_user_id?: string;
  assignee_name?: string;
  assignee_name_legacy?: string;
  assignee_speaker_id?: string;
  // The speaker this task came from, rendered through the meeting's CURRENT
  // speaker_names — "Speaker 2" while unnamed, "Siddhesh Gawade" after a
  // rename. Resolved SERVER-side (lambda-userapi's _public_task_v2) because
  // the cross-meeting Task Tracker has no recording loaded and so cannot map
  // the label itself. Empty when the task never came from a speaker.
  speaker_name?: string;
  resolution_status?: TaskResolutionStatus;
  folder_id?: string;
  source_recording_id?: string;
  source_type?: TaskSourceType;
  ai_confidence?: string;
  ai_evidence?: string;
  completed_at?: string;
};

/** True when this task names a person we have NOT identified — the app should
 * offer to resolve it rather than showing the name as a confirmed assignee. */
export function needsAssigneeResolution(t: ApiTask): boolean {
  return (
    t.resolution_status === "UNRESOLVED" || t.resolution_status === "AMBIGUOUS"
  );
}

/** The best label for a task's assignee, and whether it is a real identity.
 *
 * `speaker_name` outranks `assignee_name_legacy` because the legacy field is
 * the VERBATIM AI extraction ("Speaker 2") — kept for provenance, not for
 * display — while speaker_name is that same speaker read through the
 * meeting's current names. This is what makes a rename reach every task with
 * no writes: the backend recomputes it per request (see ApiTask.speaker_name).
 *
 * `assignee_name` still wins over both: it is only set once a real Contact is
 * attached, and renaming the speaker who happened to say the sentence must
 * never re-point a task someone assigned by hand. */
export function assigneeLabel(t: ApiTask): { name: string; confirmed: boolean } {
  const confirmed = t.resolution_status === "RESOLVED";
  const name =
    t.assignee_name ||
    (t.assignee_contact_id ? "" : t.speaker_name) ||
    t.assignee_name_legacy ||
    t.assignee?.name ||
    "";
  return { name, confirmed: confirmed && !!name };
}

export async function getTasks(key: string): Promise<ApiTask[]> {
  const res = await request<{ tasks: ApiTask[] }>(aiPath("tasks", key));
  return res.tasks ?? [];
}

export async function createTask(
  key: string,
  input: {
    task: string;
    due?: string;
    priority?: ApiTask["priority"];
    status?: ApiTask["status"];
    /** Legacy name-only assignee. Produces an UNRESOLVED task — prefer
     * assignee_contact_id, which the backend can actually notify. */
    assignee?: ApiTaskAssignee | null;
    /** A real Contact. This is what makes the task RESOLVED and links it to a
     * person (and their MinuteX account, if they have one). */
    assignee_contact_id?: string;
  }
): Promise<ApiTask> {
  const res = await request<{ task: ApiTask }>(aiPath("tasks", key), {
    method: "POST",
    body: input,
  });
  return res.task;
}

export type TaskPatch = {
  task?: string;
  due?: string;
  priority?: ApiTask["priority"];
  status?: ApiTask["status"];
  assignee?: ApiTaskAssignee | null;
  // Added to (not replaced in) the stored set server-side — see update_task.
  notify_channels?: string[];
};

export async function updateTask(key: string, id: string, patch: TaskPatch): Promise<ApiTask> {
  const res = await request<{ task: ApiTask }>(aiPath("tasks", key), {
    method: "PATCH",
    body: { id, ...patch },
  });
  return res.task;
}

export async function deleteTask(key: string, id: string): Promise<void> {
  await request(aiPath("tasks", key), { method: "DELETE", body: { id } });
}
// ---------------------------------------------------------------------------
// FOLDERS, CONTACTS, PARTICIPANTS and cross-meeting TASKS.
//
// The organization layer. Three things worth knowing before using these:
//
//  1. A meeting belongs to ZERO OR ONE folder, and moving it rewrites one
//     attribute — there is no copy, and `folder_id: null` means General. So
//     "which meetings are in this folder" is a filter over the one master
//     list, never a separate collection.
//
//  2. A contact is GLOBAL per account. Adding one to a folder creates an
//     association, not a second person, so the same contact_id appears in as
//     many folders as you like and editing them once edits them everywhere.
//
//  3. Speaker labels in the transcript ("0", "1", …) are NEVER rewritten.
//     setParticipant maps a label to a contact alongside the transcript; the
//     backend also syncs the recording speaker_names map, which is what the
//     already-shipped surfaces (documents, highlights, transcript) render.
// ---------------------------------------------------------------------------
export type ApiContact = {
  id: string;
  name: string;
  email: string;
  phone: string;
  company: string;
  role: string;
  notes: string;
  // Set when this person also has a MinuteX account — the prerequisite for
  // in-app notification. Empty means no account exists for their email.
  minutex_user_id: string;
  created_at: string;
  updated_at: string;
};

// Folder appearance is a TOKEN, never a hex colour or a raw icon name: the
// backend stores the token and the app owns what it looks like, so the palette
// stays coherent in light and dark and can be re-themed without rewriting
// stored data. See FOLDER_COLORS / FOLDER_ICONS in lambda-userapi.
export type FolderColor =
  | "slate" | "blue" | "green" | "amber" | "teal" | "red" | "purple";
export type FolderIconToken =
  | "folder" | "briefcase" | "person.2" | "building" | "chart"
  | "lightbulb" | "flag" | "heart" | "star" | "phone" | "cart" | "gear";

export type ApiFolder = {
  id: string;
  name: string;
  description: string;
  // Always present on read — the backend defaults them, so folders created
  // before appearance existed render normally rather than needing a fallback
  // at every call site.
  color: FolderColor;
  icon: FolderIconToken;
  created_at: string;
  updated_at: string;
  meeting_count?: number;
};

export type ApiParticipant = {
  speaker_id: string;
  contact_id: string;
  participant_role: string;
  created_at: string;
  updated_at: string;
  contact?: ApiContact;
};

/** Returned as a 409 when a contact name matches people we already know
 * about. Carries the candidates so the UI can ask "which Rahul?" instead of
 * failing outright. */
export const CONTACT_AMBIGUOUS_CODE = "contact_ambiguous";

export function isAmbiguousContact(e: unknown): boolean {
  return e instanceof ApiError && e.code === CONTACT_AMBIGUOUS_CODE;
}

/** The candidate list carried on an ambiguous-contact 409. Read off the error
 * so the caller can render a chooser without a second round trip. */
export function ambiguousCandidates(e: unknown): ApiContact[] {
  if (!(e instanceof ApiError)) return [];
  const list = (e as any).candidates;
  return Array.isArray(list) ? (list as ApiContact[]) : [];
}

// -- Folders ----------------------------------------------------------------
export async function getFolders(): Promise<{
  folders: ApiFolder[];
  general_count: number;
}> {
  const res = await request<{ folders: ApiFolder[]; general_count: number }>(
    "/folders"
  );
  return { folders: res.folders ?? [], general_count: res.general_count ?? 0 };
}

export async function getFolder(
  folderId: string
): Promise<{ folder: ApiFolder; contacts: ApiContact[] }> {
  const res = await request<{ folder: ApiFolder; contacts: ApiContact[] }>(
    `/folders/${encodeURIComponent(folderId)}`
  );
  return { folder: res.folder, contacts: res.contacts ?? [] };
}

export async function createFolder(input: {
  name: string;
  description?: string;
  color?: FolderColor;
  icon?: FolderIconToken;
}): Promise<ApiFolder> {
  const res = await request<{ folder: ApiFolder }>("/folders", {
    method: "POST",
    body: input,
  });
  return res.folder;
}

export async function updateFolder(
  folderId: string,
  patch: {
    name?: string;
    description?: string;
    color?: FolderColor;
    icon?: FolderIconToken;
  }
): Promise<ApiFolder> {
  const res = await request<{ folder: ApiFolder }>(
    `/folders/${encodeURIComponent(folderId)}`,
    { method: "PATCH", body: patch }
  );
  return res.folder;
}

/** Deletes the FOLDER only. Its meetings move to General and its contacts and
 * tasks survive — the counts in the result say how many of each were touched,
 * which is what the confirmation screen should show. */
export async function deleteFolder(folderId: string): Promise<{
  meetings_moved: number;
  contacts_unlinked: number;
  tasks_unfiled: number;
}> {
  return request(`/folders/${encodeURIComponent(folderId)}`, {
    method: "DELETE",
  });
}

export async function getFolderContacts(
  folderId: string
): Promise<ApiContact[]> {
  const res = await request<{ contacts: ApiContact[] }>(
    `/folders/${encodeURIComponent(folderId)}/contacts`
  );
  return res.contacts ?? [];
}

export async function addContactToFolder(
  folderId: string,
  contactId: string
): Promise<ApiContact> {
  const res = await request<{ contact: ApiContact }>(
    `/folders/${encodeURIComponent(folderId)}/contacts/${encodeURIComponent(
      contactId
    )}`,
    { method: "POST" }
  );
  return res.contact;
}

/** Removes the ASSOCIATION. The contact itself is untouched. */
export async function removeContactFromFolder(
  folderId: string,
  contactId: string
): Promise<void> {
  await request(
    `/folders/${encodeURIComponent(folderId)}/contacts/${encodeURIComponent(
      contactId
    )}`,
    { method: "DELETE" }
  );
}

/** Move a meeting into a folder, or to General with folderId = null. The
 * meeting is never duplicated — this rewrites one attribute. */
export async function moveRecordingToFolder(
  key: string,
  folderId: string | null
): Promise<{ folder_id: string; tasks_moved: number }> {
  return request(`/recordings/folder/${encodeURIComponent(key)}`, {
    method: "PATCH",
    body: { folder_id: folderId },
  });
}

// -- Contacts ---------------------------------------------------------------
export async function getContacts(params?: {
  search?: string;
  limit?: number;
  cursor?: string;
}): Promise<{ contacts: ApiContact[]; next_cursor: string }> {
  const qs = new URLSearchParams();
  if (params?.search) qs.set("search", params.search);
  if (params?.limit) qs.set("limit", String(params.limit));
  if (params?.cursor) qs.set("cursor", params.cursor);
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  const res = await request<{ contacts: ApiContact[]; next_cursor: string }>(
    `/contacts${suffix}`
  );
  return { contacts: res.contacts ?? [], next_cursor: res.next_cursor ?? "" };
}

export async function getContact(
  contactId: string
): Promise<{ contact: ApiContact; folders: ApiFolder[] }> {
  const res = await request<{ contact: ApiContact; folders: ApiFolder[] }>(
    `/contacts/${encodeURIComponent(contactId)}`
  );
  return { contact: res.contact, folders: res.folders ?? [] };
}

export type CreateContactInput = {
  name: string;
  email?: string;
  phone?: string;
  company?: string;
  role?: string;
  notes?: string;
  /** Create and associate with this folder in one call. */
  folder_id?: string;
  /** "Yes, this really is a different person" — bypasses the name-ambiguity
   * 409. Only send this after the user has seen the candidates and said so. */
  force?: boolean;
};

/** Creates a contact, or returns the EXISTING one when a strong identifier
 * (email/phone) already matches — `existing` tells you which happened, so the
 * UI can say "already in your contacts" rather than implying it made a new
 * one. Throws an ambiguous-contact ApiError (see isAmbiguousContact) when only
 * the NAME matches, because two people can share a name. */
export async function createContact(
  input: CreateContactInput
): Promise<{ contact: ApiContact; existing: boolean }> {
  const res = await request<{ contact: ApiContact; existing?: boolean }>(
    "/contacts",
    { method: "POST", body: input }
  );
  return { contact: res.contact, existing: !!res.existing };
}

export async function updateContact(
  contactId: string,
  patch: Partial<Omit<CreateContactInput, "folder_id" | "force">>
): Promise<ApiContact> {
  const res = await request<{ contact: ApiContact }>(
    `/contacts/${encodeURIComponent(contactId)}`,
    { method: "PATCH", body: patch }
  );
  return res.contact;
}

/** Deletes the PERSON. Their tasks survive, reverting to unresolved. */
export async function deleteContact(contactId: string): Promise<{
  unlinked_folders: number;
  unassigned_tasks: number;
}> {
  return request(`/contacts/${encodeURIComponent(contactId)}`, {
    method: "DELETE",
  });
}

// -- Participants (speaker -> contact) --------------------------------------
export type ParticipantsResponse = {
  participants: ApiParticipant[];
  /** The diarization labels this transcript actually contains. */
  speakers: string[];
  speaker_names: Record<string, string>;
  folder_id: string;
  /** Offered FIRST in the picker — a shortcut, never a restriction. Any global
   * contact can still be chosen. */
  folder_contacts: ApiContact[];
};

export async function getParticipants(
  key: string
): Promise<ParticipantsResponse> {
  const res = await request<ParticipantsResponse>(
    `/recordings/participants/${encodeURIComponent(key)}`
  );
  return {
    participants: res.participants ?? [],
    speakers: res.speakers ?? [],
    speaker_names: res.speaker_names ?? {},
    folder_id: res.folder_id ?? "",
    folder_contacts: res.folder_contacts ?? [],
  };
}

/** Map a speaker label to a contact, or clear it with contactId = null.
 * `tasks_resolved` reports how many AI tasks this mapping just gave an
 * identity to — worth surfacing, since one mapping can resolve several. */
export async function setParticipant(
  key: string,
  speakerId: string,
  contactId: string | null,
  participantRole?: string
): Promise<{
  participant?: ApiParticipant;
  cleared?: boolean;
  tasks_resolved?: number;
}> {
  return request(`/recordings/participants/${encodeURIComponent(key)}`, {
    method: "PUT",
    body: {
      speaker_id: speakerId,
      contact_id: contactId,
      participant_role: participantRole,
    },
  });
}

// -- Cross-meeting tasks ----------------------------------------------------
export type TaskFilters = {
  status?: TaskStatusV2;
  folder_id?: string;
  assignee_contact_id?: string;
  recording_key?: string;
  overdue?: boolean;
  assigned_to_me?: boolean;
  due_before?: string;
  limit?: number;
  cursor?: string;
};

/** Every filter is applied SERVER-side — never fetch all tasks and filter on
 * the phone (that is what the cursor and limit are for). */
export async function getAllTasks(
  filters?: TaskFilters
): Promise<{ tasks: ApiTask[]; next_cursor: string }> {
  const qs = new URLSearchParams();
  if (filters?.status) qs.set("status", filters.status);
  if (filters?.folder_id) qs.set("folder_id", filters.folder_id);
  if (filters?.assignee_contact_id)
    qs.set("assignee_contact_id", filters.assignee_contact_id);
  if (filters?.recording_key) qs.set("recording_key", filters.recording_key);
  if (filters?.overdue) qs.set("overdue", "true");
  if (filters?.assigned_to_me) qs.set("assigned_to_me", "true");
  if (filters?.due_before) qs.set("due_before", filters.due_before);
  if (filters?.limit) qs.set("limit", String(filters.limit));
  if (filters?.cursor) qs.set("cursor", filters.cursor);
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  const res = await request<{ tasks: ApiTask[]; next_cursor: string }>(
    `/tasks${suffix}`
  );
  return { tasks: res.tasks ?? [], next_cursor: res.next_cursor ?? "" };
}

export type TaskDetail = {
  task: ApiTask;
  contact?: ApiContact;
  folder?: ApiFolder;
  recording?: {
    audio_s3_key: string;
    title: string;
    recorded_at: string;
    folder_id: string;
    // The meeting's speaker_names, so the detail screen can name the speaker
    // a task came from without a second call to getParticipants.
    speaker_names?: Record<string, string>;
  };
};

export async function getTaskDetail(taskId: string): Promise<TaskDetail> {
  return request<TaskDetail>(`/tasks/${encodeURIComponent(taskId)}`);
}

export async function patchTaskById(
  taskId: string,
  patch: TaskPatch & { assignee_contact_id?: string | null }
): Promise<ApiTask> {
  const res = await request<{ task: ApiTask }>(
    `/tasks/${encodeURIComponent(taskId)}`,
    { method: "PATCH", body: patch }
  );
  return res.task;
}

/** Answer "which Rahul?" — the ONLY way an unresolved name becomes a resolved
 * identity. Nothing server-side upgrades a name on its own. */
export async function resolveTaskAssignee(
  taskId: string,
  contactId: string
): Promise<ApiTask> {
  const res = await request<{ task: ApiTask }>(
    `/tasks/${encodeURIComponent(taskId)}/resolve`,
    { method: "POST", body: { contact_id: contactId } }
  );
  return res.task;
}

export type AssigneeCandidate = ApiContact & { in_folder: boolean };

/** Who an unresolved task name MIGHT refer to. Folder members rank first as a
 * hint for the human; the status stays "ambiguous" either way, because a lone
 * name match is still not proof of identity. */
export async function getAssigneeCandidates(taskId: string): Promise<{
  status: "exact" | "ambiguous" | "none";
  searched_name: string;
  candidates: AssigneeCandidate[];
}> {
  const res = await request<{
    status: "exact" | "ambiguous" | "none";
    searched_name: string;
    candidates: AssigneeCandidate[];
  }>(`/tasks/${encodeURIComponent(taskId)}/assignee-candidates`);
  return {
    status: res.status ?? "none",
    searched_name: res.searched_name ?? "",
    candidates: res.candidates ?? [],
  };
}

/** Assign a meeting task to a real contact (first-class path), or clear it
 * with null. Distinct from the legacy assignee-by-name patch, which can only
 * ever produce an UNRESOLVED task. */
export async function assignTaskToContact(
  key: string,
  id: string,
  contactId: string | null
): Promise<ApiTask> {
  const res = await request<{ task: ApiTask }>(aiPath("tasks", key), {
    method: "PATCH",
    body: { id, assignee_contact_id: contactId },
  });
  return res.task;
}
