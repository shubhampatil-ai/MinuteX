"""userApi — read-side API for the AI_recorder app.

A logged-in user fetches the recordings belonging to the device(s) they own.
There was no user/account concept before this; this Lambda introduces one.

Routes (HTTP API, payload format v2.0):
  POST   /signup               {email, password, name?}   -> {token, user_id, email, name}
  POST   /login                {email, password}          -> {token, user_id, email}
  GET    /me                   (JWT)                       -> {user:{...}}
  PATCH  /me                   {name?, avatar_url?} (JWT)  -> {user:{...}}
  POST   /avatars/upload-request {format, scope?, contact_id?, size?} (JWT)
                                                          -> {upload_url, key}
  POST   /me/password          {current_password, new_password} (JWT) -> {changed}
  POST   /devices/pair-request {device_id}       (JWT)    -> {pairing_code, expires_in}
  POST   /devices/pair         {device_id, pairing_code} (JWT) -> {device:{...}}
  POST   /devices/claim        {apiKey}          (JWT)    -> {device_id, claimed}   (LEGACY)
  GET    /devices              (JWT)                       -> {devices:[device_id,...], details:[{...}]}
  GET    /devices/{device_id}  (JWT)                       -> {device:{...}}
  PATCH  /devices/{device_id}  {name}              (JWT)    -> {device:{...}}
  DELETE /devices/{device_id}  (JWT)                       -> {device_id, unpaired}
  POST   /devices/{device_id}/factory-reset (JWT)          -> {device_id, reset}
  GET    /recordings           (JWT)                       -> {recordings:[summary,...]}
  GET    /recordings/{key}     (JWT)                       -> {recording:{...full..., audio_url}}
  DELETE /recordings/{key}     (JWT)  SOFT -> {trashed, key, deleted_at}
  POST   /recordings/restore/{key}   (JWT)  -> {restored, key, status}
  DELETE /recordings/permanent/{key} (JWT)  -> {deleted, key}
  GET    /trash                (JWT)        -> {recordings:[...], count}
  POST   /recordings/upload-request  {source, format?, title?, duration?,
                                      size?, folder_id?} (JWT)
                                  -> {upload_url, key, recording_id, expires_in,
                                      content_type, folder_id}
         folder_id files the recording at PRESIGN time, so a recording started
         from inside a folder is never briefly unfiled.
  POST   /recordings/upload-complete {key, duration?} (JWT) -> {key, status}

  AI Meeting Workspace (JWT) — on-demand generation, see the section at the
  bottom of this file. NOTE the path shape: the action is a literal prefix and
  the recording key comes LAST, because API Gateway only allows a greedy {key+}
  in the final position (a "/recordings/{key+}/chat" route is rejected outright
  — see the _ROUTES comment).
  GET    /recordings/ai/documents/{key+}                     -> {documents, available}
  POST   /recordings/ai/documents/{key+}  {type, regenerate?} -> {document, cached}
  PATCH  /recordings/ai/documents/{key+}  {type, content?, label?} -> {document}
  DELETE /recordings/ai/documents/{key+}  {type}              -> {deleted, type}
  POST   /recordings/ai/custom-document/{key+} {prompt}       -> {document}
  POST   /recordings/ai/update-documents/{key+} {}            -> {updated, remaining}
  POST   /recordings/ai/reprocess/{key+}  {}                  -> 202 {status, started_at}
  POST   /recordings/ai/quick/{key+}      {action, regenerate?} -> {document, cached}
  POST   /recordings/ai/highlights/{key+} {regenerate?}      -> {meeting_highlights}
  GET    /recordings/ai/chat/{key+}                          -> {chat_history, suggestions}
  POST   /recordings/ai/chat/{key+}       {message, history?} -> {reply, chat_history}
  DELETE /recordings/ai/chat/{key+}                          -> {cleared}
  GET    /recordings/ai/tasks/{key+}                         -> {tasks}
  POST   /recordings/ai/tasks/{key+}      {task, ...}         -> {task}
  PATCH  /recordings/ai/tasks/{key+}      {id, ...}           -> {task}
  DELETE /recordings/ai/tasks/{key+}      {id}                -> {deleted, id}

  Meeting Share — a read-only public link (see the Meeting Share section).
  POST   /recordings/share/{key+}   {summary?,highlights?,decisions?,tasks?,
                                     participants?,transcript?,audio?,
                                     expires_at?}          (JWT)
                                  -> 201 {share_id, url, expires_at, share}
         The `url` is the ONLY time the raw token is ever returned — only its
         sha256 is stored, so it cannot be re-derived later.
  GET    /recordings/shares/{key+}                  (JWT)  -> {shares:[...]}
  PATCH  /shares/{share_id}   {toggles?, expires_at?} (JWT) -> {share}
  DELETE /shares/{share_id}                          (JWT)  -> {share_id, revoked}
  GET    /share/{token}     (NO JWT — the token IS the credential) -> text/html
  GET    /share/{token}/audio  (NO JWT) -> 302 to a fresh short-lived presign
         The <audio> element points HERE, never at S3, so every seek
         re-validates the share and re-signs. That is what makes revocation
         reach playback already under way, and what makes a two-hour meeting
         seekable — S3 checks a presign at REQUEST time, so one flat window
         would break the first seek past it.
         The greedy {key+} sits LAST for the same API Gateway reason the AI
         routes give above, which is why it is /recordings/share/{key+} and
         not /recordings/{key}/share.

  CRM — Salesforce connect + configuration (see the CRM sections below).
  GET    /crm/salesforce/connect                    (JWT)    -> {authorize_url}
  GET    /crm/salesforce/callback  ?code&state    (NO JWT — Salesforce redirect) -> 302
  GET    /crm/salesforce/status                     (JWT)    -> {connected, ...}
  DELETE /crm/salesforce                             (JWT)    -> {disconnected}
  GET    /crm/salesforce/objects                    (JWT)    -> {objects, suggested}
  GET    /crm/salesforce/fields/{object_name}       (JWT)    -> {number_fields, ...}
  GET    /crm/salesforce/config                     (JWT)    -> {config}
  PUT    /crm/salesforce/config     {mappings:[...]}  (JWT)   -> {config}
  POST   /crm/salesforce/lookup     {object, lookup_value} (JWT)
                              -> {status: found|not_found|ambiguous, ...}
  POST   /crm/salesforce/sync/{key+} {object}         (JWT)   -> {crm_record}

  Integrations — connect MinuteX to external applications (see the
  INTEGRATIONS section). Generic over providers; Gmail is the only one that
  can currently be connected.
  GET    /integrations                          (JWT) -> {integrations:[...]}
  GET    /integrations/{provider}               (JWT) -> {integration:{...}}
  POST   /integrations/{provider}/connect       (JWT) -> {authorize_url}
  GET    /integrations/{provider}/callback ?code&state  (NO JWT — the
                                    provider's browser redirect) -> 302
  DELETE /integrations/{provider}               (JWT) -> {disconnected}

  Gmail communication (JWT, and every one of them ALSO requires a usable
  Gmail connection — they answer 409 with a `code` otherwise):
  POST   /integrations/gmail/send   {recipients, subject, body, cc?,
                                     attachments?}         -> {sent, message_id}
  GET    /integrations/gmail/recipients/{key+}   -> {recipients, unresolved}
  POST   /integrations/gmail/send/meeting/{key+} {recipients, ...}
  POST   /integrations/gmail/send/task/{task_id} {recipients?, ...}

  Every /crm route that TALKS to Salesforce (objects, fields, config PUT,
  lookup, sync) answers 409 {"code": "salesforce_reconnect_required"} when the
  stored Salesforce refresh token is dead. That is NOT 401 on purpose: 401 from
  this API means the MinuteX JWT itself is bad, and the app reacts by clearing
  the session and sending the user to /login. See SF_RECONNECT_STATUS.

Recording sources (one AI pipeline, three ways in — see README):
  - Every recording carries a `source` attribute: DEVICE | MOBILE | UPLOAD.
    DEVICE rows keep their device_id; MOBILE/UPLOAD rows have no device.
  - MOBILE (in-app phone recording) and UPLOAD (imported audio file) get
    their presigned PUT from /recordings/upload-request here — the caller is
    a logged-in user, so user_id comes from the JWT, never the request. The
    DEVICE presign stays on the device-facing getUploadUrl Lambda (x-api-key
    auth) — different authentication, same S3 bucket, same pipeline after.
  - S3 layout (the {device_id} slot is a reserved literal for non-device
    sources, so one parser serves all three):
        DEVICE  recordings/{user_id}/{device_id}/{recording_id}.wav
        MOBILE  recordings/{user_id}/mobile/{recording_id}.{ext}
        UPLOAD  recordings/{user_id}/uploads/{recording_id}.{ext}
  - After the PUT, the S3 ObjectCreated trigger runs the ONE transcription /
    AI pipeline regardless of source. No per-source processing exists.
  - Status lifecycle (the Recordings 'status' attribute):
        uploading -> uploaded -> transcribing -> generating_ai
        -> complete | transcribed (legacy: AI step failed) | failed

Identity model (user-owned architecture):
  - Users table:        PK user_id (uuid). GSI email-index on email (login).
                        Attrs: user_id, email, password_hash, salt, created_at.
  - Devices table:      PK device_id. THE source of truth for device ownership.
                        Attrs: device_id, status (UNPAIRED|PAIRING|PAIRED),
                        paired_user_id (absent unless PAIRED), paired_at,
                        last_seen, firmware_version, serial_number, plus the
                        transient pairing_code_hash / pairing_expires_at /
                        pairing_user_id while status == PAIRING.
                        GSI paired-user-index (paired_user_id HASH) lists a
                        user's devices without a scan.
  - DeviceKeys table:   PK apiKey -> deviceId. Device AUTHENTICATION only
                        (the fixed firmware contract) — never ownership.
  - UserDevices table:  PK user_id, SK device_id. LEGACY claim link, kept so
                        recordings uploaded before user-ownership (rows with
                        no user_id) stay visible. New code writes it only as
                        a compatibility mirror of a successful pair/claim.
  - Pairing: a user requests a 6-digit code for a device_id
    (/devices/pair-request, code stored hashed with a 5-minute expiry,
    status -> PAIRING), then confirms it (/devices/pair). Firmware
    confirmation is MOCKED behind FirmwareVerifier — replace that class when
    the real device round-trip exists. One device belongs to at most ONE
    user: enforced with DynamoDB conditional writes (409 on conflict).
  - Recordings: new uploads carry user_id (stamped by getUploadUrl at
    presign time) and are queried via the Recordings 'user-index' GSI
    (user_id HASH, created_at RANGE). Legacy rows without user_id are still
    reached through the 'device-index' GSI (device_id HASH, created_at
    RANGE) for devices the caller owns. Unpairing stamps the departing
    owner's user_id onto the device's legacy rows so their history survives
    (and the next owner never sees it), then removes only the ownership
    link — recordings, AI data and the device row are never deleted.

AI Meeting Workspace:
  - The transcript stays the source of truth, but it is no longer the primary
    view: after processing, the app lands on a workspace of AI output
    (executive summary -> highlights -> documents -> Quick AI -> chat), with
    the transcript beneath it as reference.
  - Staged output (summary, meeting_highlights) is produced by the
    S3-triggered transcribeRecording Lambda. On-demand output (documents,
    Quick AI, chat) is produced HERE, because it needs the caller's JWT.
  - Both halves import the SAME lambda-shared modules — groq_client (one
    client, one retry policy, one TPM budget), prompts (every template) and
    ai_schema (strict coercion). There is no second Groq integration, and no
    prompt text lives in this file. Adding a document type is one entry in
    prompts.DOCUMENTS.
  - Generated documents are cached on the recording row under a fingerprint of
    the transcript they came from, so an unchanged transcript never pays for
    the same document twice. See the AI section at the bottom.

Zero external dependencies (matches the other Lambdas):
  - Groq is called over stdlib urllib (see lambda-shared/groq_client.py).
  - Passwords hashed with hashlib.scrypt (stdlib).
  - JWT is HS256 signed with hmac + hashlib (stdlib). Signing secret from
    Secrets Manager (JWT_SECRET_ARN) with an env fallback (JWT_SECRET) for MVP.
  - boto3 is bundled in the Lambda Python runtime.

Nothing here moves audio bytes — S3 object traffic always goes browser/app
<-> S3 via presigned URLs (a presigned url is a local signing operation, not
a request to AWS; this Lambda never issues a GetObject/PutObject call
itself). The only Recordings writes are the upload-request ownership stub
and the upload-complete status flip — mirroring exactly what getUploadUrl
does for DEVICE recordings, so the pipeline downstream of S3 cannot tell
the sources apart. (The Lambda role therefore needs s3:PutObject on
recordings/* for the presigned PUT to be honored — see
scripts/18_wire_upload_routes.sh.)
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

# The shared AI core — the SAME modules transcribeRecording uses. Vendored flat
# into this function's zip (see scripts/21_deploy_ai_workspace.sh).
import ai_sanitize
import ai_schema
import email_message
import groq_client
import integrations
import mom_schema
import notification_schema
import pageindex
import pageindex_store
import spoken_dates
import prompts
import share_schema
import speaker_identity
import stt_result
import transcript_store
import workspace_schema

REGION = os.environ.get("AWS_REGION")
USERS_TABLE = os.environ.get("USERS_TABLE", "Users")
USER_DEVICES_TABLE = os.environ.get("USER_DEVICES_TABLE", "UserDevices")
DEVICE_KEYS_TABLE = os.environ.get("DEVICE_KEYS_TABLE", "DeviceKeys")
DEVICES_TABLE = os.environ.get("DEVICES_TABLE", "Devices")
RECORDINGS_TABLE = os.environ.get("RECORDINGS_TABLE", "Recordings")
CRM_CONNECTIONS_TABLE = os.environ.get("CRM_CONNECTIONS_TABLE", "CrmConnections")
# Organisation-scoped Salesforce connections (Phase 2D.1) — a SEPARATE table
# from CrmConnections, never a repurposed row in it. See the "Organisation
# CRM" section below and scripts/60_create_org_crm_connections_table.sh.
ORG_CRM_CONNECTIONS_TABLE = os.environ.get("ORG_CRM_CONNECTIONS_TABLE", "OrgCrmConnections")
# Internal-speaker -> Salesforce User links (Phase 2D.3). One row per
# (workspace_id, minutex_user_id) — a MinuteX org member's confirmed
# Salesforce User identity, resolved once and reused by every later meeting
# rather than re-resolved per meeting. Deliberately its OWN table rather than
# fields on WorkspaceMemberships (a security/RBAC-critical structure — role,
# status — that a CRM-vendor-specific identity has no business living in) or
# on Contacts (which model people OUTSIDE the organisation; a member is
# already fully modeled by their user_id and has no Contacts row of their
# own). See scripts/62_create_org_salesforce_user_links_table.sh.
ORG_SALESFORCE_USER_LINKS_TABLE = os.environ.get(
    "ORG_SALESFORCE_USER_LINKS_TABLE", "OrgSalesforceUserLinks")
# Durable CRM sync job state (Phase 2D.4) — one row per asynchronous push
# ATTEMPT. Distinct from crm_event_synced/crm_tasks_synced/crm_records
# (Phase 2D.3), which remain the per-OPERATION idempotency record on the
# Recordings row itself; this table is the OUTER envelope the SQS-driven
# worker and the app's status polling both read. See
# scripts/63_create_crm_sync_jobs_table.sh and the "Organisation CRM push"
# section below for the full state machine.
CRM_SYNC_JOBS_TABLE = os.environ.get("CRM_SYNC_JOBS_TABLE", "CrmSyncJobs")
# The SQS queue a CRM sync job is enqueued onto. Empty by default so a
# deployment that has not yet run scripts/64_create_crm_sync_queue.sh keeps
# working exactly as before — see create_crm_sync_job's fallback comment.
CRM_SYNC_QUEUE_URL = os.environ.get("CRM_SYNC_QUEUE_URL", "")
INTEGRATIONS_TABLE = os.environ.get("INTEGRATIONS_TABLE", "Integrations")
CONTACTS_TABLE = os.environ.get("CONTACTS_TABLE", "Contacts")
MEETING_PARTICIPANTS_TABLE = os.environ.get("MEETING_PARTICIPANTS_TABLE",
                                            "MeetingParticipants")
TASKS_TABLE = os.environ.get("TASKS_TABLE", "Tasks")
NOTIFICATIONS_TABLE = os.environ.get("NOTIFICATIONS_TABLE", "Notifications")
NOTIFICATION_DEDUPE_TABLE = os.environ.get("NOTIFICATION_DEDUPE_TABLE",
                                           "NotificationDedupe")
DEVICE_INDEX = os.environ.get("DEVICE_INDEX", "device-index")
USER_INDEX = os.environ.get("USER_INDEX", "user-index")
PAIRED_USER_INDEX = os.environ.get("PAIRED_USER_INDEX", "paired-user-index")
EMAIL_INDEX = os.environ.get("EMAIL_INDEX", "email-index")
# Indexes on the organization-layer tables (see
# scripts/31_create_workspace_tables.sh for the full key schema of each).
CONTACTS_OWNER_INDEX = os.environ.get("CONTACTS_OWNER_INDEX", "owner-index")
# SHARED organisation contacts (Phase 2C). Sparse: personal rows carry no
# workspace_id and stay absent, which is correct — they are served by
# owner-index, the path they have always used.
CONTACTS_WORKSPACE_INDEX = os.environ.get("CONTACTS_WORKSPACE_INDEX",
                                          "workspace-index")
# Dedupe within a shared workspace. owner-email-index cannot answer "does
# this organisation already have this person", because its HASH key is the
# OWNER — two members adding the same client would each get their own row and
# create_contact would report 201 "created" instead of 200 "existing". This
# index is what makes shared dedupe correct rather than owner-local.
CONTACTS_WORKSPACE_EMAIL_INDEX = os.environ.get(
    "CONTACTS_WORKSPACE_EMAIL_INDEX", "workspace-email-index")
CONTACTS_EMAIL_INDEX = os.environ.get("CONTACTS_EMAIL_INDEX", "owner-email-index")
CONTACTS_PHONE_INDEX = os.environ.get("CONTACTS_PHONE_INDEX", "owner-phone-index")
PARTICIPANTS_CONTACT_INDEX = os.environ.get("PARTICIPANTS_CONTACT_INDEX",
                                            "contact-index")
TASKS_OWNER_INDEX = os.environ.get("TASKS_OWNER_INDEX", "owner-index")
TASKS_MEETING_INDEX = os.environ.get("TASKS_MEETING_INDEX", "meeting-index")
TASKS_ASSIGNEE_INDEX = os.environ.get("TASKS_ASSIGNEE_INDEX", "assignee-index")
# Tasks assigned to a MinuteX ACCOUNT (not a contact). The assignee-index
# above is keyed on assignee_contact_id — an address-book record, which is
# not an identity and cannot authenticate — so it cannot answer "tasks
# assigned to me". assignee_user_id is a SPARSE key (see _TASK_SPARSE_KEYS),
# so unassigned tasks never enter this index.
TASKS_ASSIGNEE_USER_INDEX = os.environ.get(
    "TASKS_ASSIGNEE_USER_INDEX", "assignee-user-index")
TASKS_DEDUPE_INDEX = os.environ.get("TASKS_DEDUPE_INDEX", "dedupe-index")
NOTIFICATIONS_USER_INDEX = os.environ.get("NOTIFICATIONS_USER_INDEX",
                                          "user-index")
NOTIFICATIONS_UNREAD_INDEX = os.environ.get("NOTIFICATIONS_UNREAD_INDEX",
                                            "user-unread-index")
# --- Workspace layer (Phase 2A). Tables created by
# scripts/50_create_workspace_tables.sh. Nothing below reads these unless a
# workspace row actually exists, so an environment where the script has not
# run behaves exactly as it did before — see _workspace_membership.
WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE", "Workspaces")
WORKSPACE_MEMBERSHIPS_TABLE = os.environ.get("WORKSPACE_MEMBERSHIPS_TABLE",
                                             "WorkspaceMemberships")
WORKSPACE_INVITATIONS_TABLE = os.environ.get("WORKSPACE_INVITATIONS_TABLE",
                                             "WorkspaceInvitations")
WORKSPACES_OWNER_INDEX = os.environ.get("WORKSPACES_OWNER_INDEX", "owner-index")
MEMBERSHIPS_USER_INDEX = os.environ.get("MEMBERSHIPS_USER_INDEX", "user-index")
INVITATIONS_TOKEN_INDEX = os.environ.get("INVITATIONS_TOKEN_INDEX",
                                         "token-index")
INVITATIONS_WORKSPACE_INDEX = os.environ.get("INVITATIONS_WORKSPACE_INDEX",
                                             "workspace-index")
INVITATIONS_EMAIL_INDEX = os.environ.get("INVITATIONS_EMAIL_INDEX",
                                         "email-index")
# Per-meeting access grants (Phase 2B). Only ever written for ORGANISATION
# meetings — in a personal workspace the owner is the only member, so a grant
# row could never change an answer.
MEETING_ACCESS_TABLE = os.environ.get("MEETING_ACCESS_TABLE", "MeetingAccess")
MEETING_ACCESS_USER_INDEX = os.environ.get("MEETING_ACCESS_USER_INDEX",
                                           "user-index")
# Workspace listing indexes (Phase 2B). SPARSE by construction: only rows
# stamped with a workspace_id enter them, so every pre-Phase-2B row is simply
# absent — which is correct, because those rows are personal and are served by
# the existing owner indexes. Nothing needs backfilling for reads to be right.
RECORDINGS_WORKSPACE_INDEX = os.environ.get("RECORDINGS_WORKSPACE_INDEX",
                                            "workspace-index")
TASKS_WORKSPACE_INDEX = os.environ.get("TASKS_WORKSPACE_INDEX",
                                       "workspace-index")
JWT_TTL = int(os.environ.get("JWT_TTL", "86400"))  # seconds (default 24h)
PAIRING_CODE_TTL = int(os.environ.get("PAIRING_CODE_TTL", "300"))  # seconds
BUCKET_NAME = os.environ.get("BUCKET_NAME")
AUDIO_URL_EXPIRY = int(os.environ.get("AUDIO_URL_EXPIRY", "3600"))  # seconds

# --- Salesforce Connected App (web-server OAuth flow) ---
SALESFORCE_LOGIN_URL = os.environ.get("SALESFORCE_LOGIN_URL", "https://login.salesforce.com")
SALESFORCE_CLIENT_ID = os.environ.get("SALESFORCE_CLIENT_ID", "")
SALESFORCE_REDIRECT_URI = os.environ.get("SALESFORCE_REDIRECT_URI", "")
SALESFORCE_CLIENT_SECRET_ARN = os.environ.get("SALESFORCE_CLIENT_SECRET_ARN", "")
SALESFORCE_KMS_KEY_ID = os.environ.get("SALESFORCE_KMS_KEY_ID", "")
SALESFORCE_STATE_TTL = int(os.environ.get("SALESFORCE_STATE_TTL", "600"))  # seconds
# REST API version used for every data/metadata call. Pinned, not "latest":
# Salesforce keeps old versions working for years, and a floating version
# would let an org's upgrade silently change describe output under us.
SALESFORCE_API_VERSION = os.environ.get("SALESFORCE_API_VERSION", "v62.0")
# Where the callback redirects the browser once the exchange is done — a deep
# link back into the app, e.g. "minutex://crm/salesforce/connected".
SALESFORCE_RETURN_URL = os.environ.get("SALESFORCE_RETURN_URL", "")

# --- Organisation Salesforce (Phase 2D.1) ---
# Reuses the SAME Connected App (SALESFORCE_CLIENT_ID/_CLIENT_SECRET_ARN),
# login URL, API version and KMS key as Personal — only the callback path
# and the storage table differ. See the "Organisation CRM" section below.
ORG_SALESFORCE_REDIRECT_URI = os.environ.get("ORG_SALESFORCE_REDIRECT_URI", "")

# --- Integrations (generic) + the Google OAuth client behind Gmail ---
# ONE Google OAuth client serves every Google-family integration: Gmail today,
# Calendar and Tasks later. They differ only by the scopes requested at
# authorize time, so a second client id would be three sets of credentials to
# rotate for no isolation gain — Google scopes the grant per user per scope,
# not per client.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET_ARN = os.environ.get("GOOGLE_CLIENT_SECRET_ARN", "")
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "")
# Separate from SALESFORCE_KMS_KEY_ID so an integration credential and a CRM
# credential are not protected by the same key — revoking or rotating one must
# not reach the other. Falls back to the Salesforce key when unset (see
# _integration_kms_encrypt) so an un-provisioned stack degrades to "still
# encrypted", never to plaintext.
INTEGRATIONS_KMS_KEY_ID = os.environ.get("INTEGRATIONS_KMS_KEY_ID", "")
# Deep link the OAuth callback redirects back to, e.g.
# "minutex://integrations/connected". One URL for every provider — the
# callback appends ?provider= so the app knows which card to refresh.
INTEGRATION_RETURN_URL = os.environ.get("INTEGRATION_RETURN_URL", "")
INTEGRATION_STATE_TTL = int(os.environ.get("INTEGRATION_STATE_TTL", "600"))

# Wall-clock ceiling for one on-demand AI generation. Unlike the S3-triggered
# pipeline (which can spend 300s because nobody is waiting), these routes answer
# a user staring at a spinner behind API Gateway's 30s integration timeout, so
# the deadline exists to bound the map-reduce path — a document/highlights call
# on a very long transcript stops mapping and returns what it has rather than
# being killed mid-flight. Raising the Lambda timeout beyond 29s does not help;
# the gateway cuts first.
ONDEMAND_DEADLINE_SECONDS = int(os.environ.get("ONDEMAND_DEADLINE_SECONDS", "22"))

# Device lifecycle states (the Devices table 'status' attribute).
STATUS_UNPAIRED = "UNPAIRED"
STATUS_PAIRING = "PAIRING"
STATUS_PAIRED = "PAIRED"

# Cosmetic device label (PATCH /devices/{id}); empty clears it.
DEVICE_NAME_MAX = 64

# A CRM record identifier supplied by hand (PATCH /recordings/{key+}) — a site
# visit number, a lead email, an opportunity number, whatever the user mapped.
# Generous ceiling: Salesforce identifiers vary by org and field type, so this
# is only a sanity bound, not a format rule. Validating the shape is
# Salesforce's job — the lookup either finds the record or it doesn't.
CRM_IDENTIFIER_MAX = 255

# ---------------------------------------------------------------------------
# Recording sources — the enum every recording carries. DEVICE recordings are
# presigned by the device-facing getUploadUrl Lambda; MOBILE and UPLOAD are
# presigned here (JWT auth). One S3 bucket, one pipeline after upload.
# ---------------------------------------------------------------------------
SOURCE_DEVICE = "DEVICE"
SOURCE_MOBILE = "MOBILE"
SOURCE_UPLOAD = "UPLOAD"
USER_UPLOAD_SOURCES = (SOURCE_MOBILE, SOURCE_UPLOAD)

# The reserved {device_id}-slot literal per non-device source. Real device
# ids can never collide with these (enforced below at pairing time).
SOURCE_SEGMENTS = {SOURCE_MOBILE: "mobile", SOURCE_UPLOAD: "uploads"}
RESERVED_DEVICE_IDS = frozenset(SOURCE_SEGMENTS.values())

# Import formats accepted by /recordings/upload-request — every common audio
# container. The transcription service (Deepgram/ElevenLabs remote-URL mode)
# accepts all of these, so no server-side conversion is needed. Phone
# recordings arrive as m4a (the platform encoder), device recordings as wav.
# The value is the Content-Type the presigned PUT is signed with.
UPLOAD_FORMATS = {
    "wav": "audio/wav",
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "ogg": "audio/ogg",
    "opus": "audio/opus",
    "flac": "audio/flac",
    "webm": "audio/webm",
    "mp4": "audio/mp4",
    "mp2": "audio/mpeg",
    "mpga": "audio/mpeg",
    "amr": "audio/amr",
    "3gp": "audio/3gpp",
    "aiff": "audio/aiff",
    "aif": "audio/aiff",
    "wma": "audio/x-ms-wma",
    "caf": "audio/x-caf",
    "mka": "audio/x-matroska",
}
# Upload ceilings — set to the TRANSCRIPTION SERVICE's own documented limits,
# not to an arbitrary product cap. ElevenLabs Scribe accepts up to 3 GB and 10
# hours in the remote-URL (source_url) mode this pipeline uses; verified against
# their published speech-to-text limits on 2026-08-17.
#
# These were 2 GB / 4 hours, which predate asynchronous STT and were really a
# proxy for "what can finish inside one Lambda invocation". That constraint is
# gone: the transcription no longer happens inside a Lambda at all (see
# transcribeRecording's module docstring), so the only real limits left are the
# provider's. A 6-hour recording now uploads, transcribes and completes while
# the user is elsewhere.
#
# NOTE the units differ deliberately: 3 GB here is the DECIMAL gigabyte
# ElevenLabs documents (3,000,000,000 bytes), not 3 GiB. Using the binary value
# would put our ceiling ~7% ABOVE the provider's and turn a rejected upload into
# a paid-for transcription that fails after the bytes are already in S3.
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(3 * 1000 * 1000 * 1000)))
MAX_DURATION_SECONDS = int(os.environ.get("MAX_DURATION_SECONDS", str(10 * 3600)))
UPLOAD_URL_EXPIRY = int(os.environ.get("UPLOAD_URL_EXPIRY", "900"))  # seconds

# ---------------------------------------------------------------------------
# Avatars (profile + contact photos).
#
# Stored in the SAME bucket as recordings, under a separate `avatars/` prefix.
# Two properties of that prefix are load-bearing:
#
#   * The S3 ObjectCreated trigger that starts transcription is filtered to the
#     ".wav" SUFFIX (see scripts/13_wire_s3_trigger.sh), so an image dropped in
#     this bucket never enters the audio pipeline. The allowed extensions below
#     deliberately exclude .wav for the same reason, from the other direction.
#   * The key always begins "avatars/{owner_user_id}/", which is what makes
#     ownership checkable from the key alone — the same property the recordings
#     layout relies on.
#
# Images are never served as a public object URL. S3 objects here stay private
# and the API hands out a short-lived presigned GET (_avatar_view_url) on every
# read, exactly like recording playback. That is why `avatar_url` is stored as
# an S3 KEY, not a URL: a stored URL would expire, and re-signing needs the key
# anyway. `avatar_url` keeps its historical attribute name (it is already on
# every Users row and in the PATCH /me contract); what changed is that the app
# now reads the presigned `avatar_view_url` the API derives from it.
AVATAR_FORMATS = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "heic": "image/heic",
}
# A profile photo is displayed at most ~72pt. 8 MB is far above any sensibly
# compressed image at that size and still small enough that a mistaken pick
# (a full-resolution burst photo) fails fast instead of costing an upload.
MAX_AVATAR_BYTES = int(os.environ.get("MAX_AVATAR_BYTES", str(8 * 1024 * 1024)))
# Longer than a recording's playback URL: avatars are rendered in lists that a
# user can sit on for a while, and a re-signed URL means a re-fetch of an image
# that has not changed.
AVATAR_URL_EXPIRY = int(os.environ.get("AVATAR_URL_EXPIRY", "21600"))  # 6h

# Recording processing statuses (the Recordings 'status' attribute). The
# userApi only ever writes the first two; the transcription pipeline owns the
# rest. "transcribed" is the legacy value for "transcript ok, AI step failed".
STATUS_UPLOADING = "uploading"
STATUS_UPLOADED = "uploaded"

# Fields returned in the LIST view (lightweight — no transcript/timestamps).
LIST_FIELDS = ("audio_s3_key", "recording_id", "user_id", "device_id",
               "source", "meeting_id", "recorded_at", "duration",
               "title", "summary", "language", "status", "created_at")

_ddb = boto3.resource("dynamodb", region_name=REGION)
_users = _ddb.Table(USERS_TABLE)
_user_devices = _ddb.Table(USER_DEVICES_TABLE)
# Mostly presigning (a local signing operation — no network call), which is all
# this client did originally. It now also performs two REAL object operations:
#   * GET  — transcript_store.hydrate, reading an offloaded transcript for the
#            AI routes (documents, chat, highlights).
#   * PUT  — the ElevenLabs STT webhook, storing a delivered transcript.
# Both are on the transcripts/ prefix, never on the audio itself.
_s3 = boto3.client("s3", region_name=REGION)
_device_keys = _ddb.Table(DEVICE_KEYS_TABLE)
_devices = _ddb.Table(DEVICES_TABLE)
_recordings = _ddb.Table(RECORDINGS_TABLE)
_crm_connections = _ddb.Table(CRM_CONNECTIONS_TABLE)
_org_crm_connections = _ddb.Table(ORG_CRM_CONNECTIONS_TABLE)
_org_salesforce_user_links = _ddb.Table(ORG_SALESFORCE_USER_LINKS_TABLE)
_crm_sync_jobs = _ddb.Table(CRM_SYNC_JOBS_TABLE)
_integrations = _ddb.Table(INTEGRATIONS_TABLE)
_notifications = _ddb.Table(NOTIFICATIONS_TABLE)
_notification_dedupe = _ddb.Table(NOTIFICATION_DEDUPE_TABLE)
# Workspace layer (Phase 2A). Constructing a Table handle is a local operation
# — no network call, no existence check — so these are safe to build even in an
# environment where scripts/50 has not run yet.
_workspaces = _ddb.Table(WORKSPACES_TABLE)
_memberships = _ddb.Table(WORKSPACE_MEMBERSHIPS_TABLE)
_invitations = _ddb.Table(WORKSPACE_INVITATIONS_TABLE)
_meeting_access = _ddb.Table(MEETING_ACCESS_TABLE)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# ---------------------------------------------------------------------------
# JWT secret — fetched once per container from Secrets Manager, env fallback.
# ---------------------------------------------------------------------------
_jwt_secret_cache = None


def _jwt_secret():
    global _jwt_secret_cache
    if _jwt_secret_cache is not None:
        return _jwt_secret_cache
    arn = os.environ.get("JWT_SECRET_ARN")
    if arn:
        sm = boto3.client("secretsmanager", region_name=REGION)
        _jwt_secret_cache = sm.get_secret_value(SecretId=arn)["SecretString"]
    else:
        env = os.environ.get("JWT_SECRET")
        if not env:
            raise RuntimeError("neither JWT_SECRET_ARN nor JWT_SECRET is set")
        _jwt_secret_cache = env
    return _jwt_secret_cache


# ---------------------------------------------------------------------------
# base64url helpers (no padding) used by both JWT and scrypt storage.
# ---------------------------------------------------------------------------
def _b64u_encode(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(s):
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


# ---------------------------------------------------------------------------
# Password hashing (scrypt, stdlib). Stored as base64url(salt) + base64url(hash).
# ---------------------------------------------------------------------------
_SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32)


def _hash_password(password, salt=None):
    if salt is None:
        salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt, **_SCRYPT)
    return _b64u_encode(salt), _b64u_encode(dk)


def _verify_password(password, salt_b64, hash_b64):
    salt = _b64u_decode(salt_b64)
    _, computed = _hash_password(password, salt=salt)
    # constant-time compare
    return hmac.compare_digest(computed, hash_b64)


# ---------------------------------------------------------------------------
# JWT (HS256) — mint + verify with hmac/hashlib. Payload carries sub + exp.
# ---------------------------------------------------------------------------
def _jwt_sign(claims):
    header = {"alg": "HS256", "typ": "JWT"}
    seg = (_b64u_encode(json.dumps(header, separators=(",", ":")).encode()) + "." +
           _b64u_encode(json.dumps(claims, separators=(",", ":")).encode()))
    sig = hmac.new(_jwt_secret().encode(), seg.encode(), hashlib.sha256).digest()
    return seg + "." + _b64u_encode(sig)


def _jwt_verify(token):
    """Return the claims dict if valid + unexpired, else None."""
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
    except (ValueError, AttributeError):
        return None
    seg = header_b64 + "." + payload_b64
    expected = hmac.new(_jwt_secret().encode(), seg.encode(), hashlib.sha256).digest()
    try:
        if not hmac.compare_digest(expected, _b64u_decode(sig_b64)):
            return None
        claims = json.loads(_b64u_decode(payload_b64))
    except (ValueError, TypeError):
        return None
    if not isinstance(claims, dict) or claims.get("exp", 0) < int(time.time()):
        return None
    return claims


# ---------------------------------------------------------------------------
# HTTP helpers (API Gateway HTTP API, payload v2.0).
# ---------------------------------------------------------------------------
def _resp(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _body(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        raise ApiError(400, "invalid JSON body")
    if not isinstance(data, dict):
        raise ApiError(400, "body must be a JSON object")
    return data


def _require_auth(event):
    """Extract + verify the Bearer token, return the user_id (sub)."""
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    auth = headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise ApiError(401, "missing bearer token")
    claims = _jwt_verify(auth[7:].strip())
    if not claims or not claims.get("sub"):
        raise ApiError(401, "invalid or expired token")
    return claims["sub"]


def _now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------
def _mint_for(user_id, email):
    claims = {"sub": user_id, "email": email,
              "iat": int(time.time()), "exp": int(time.time()) + JWT_TTL}
    return _jwt_sign(claims)


def signup(event):
    data = _body(event)
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    if not _EMAIL_RE.match(email):
        raise ApiError(400, "valid email required")
    if len(password) < 8:
        raise ApiError(400, "password must be at least 8 characters")

    # Fast rejection of an email that is ALREADY taken (GSI lookup). This is
    # the friendly path, not the guarantee: the index is eventually consistent,
    # so it can miss a signup that landed moments ago. The claim row below is
    # what actually enforces uniqueness — this read only turns the common case
    # into a clean 409 without a write.
    existing = _users.query(IndexName=EMAIL_INDEX,
                            KeyConditionExpression=Key("email").eq(email))
    if existing.get("Items"):
        raise ApiError(409, "email already registered")

    name = (data.get("name") or "").strip()[:100]
    user_id = str(uuid.uuid4())
    salt_b64, hash_b64 = _hash_password(password)

    # ATOMIC EMAIL UNIQUENESS.
    #
    # The read above cannot enforce this on its own, and the put's
    # ConditionExpression cannot either: it guards `user_id`, a fresh uuid that
    # never collides, so it says nothing about the email. Two concurrent
    # signups for the same address could both read "not found" from the
    # eventually-consistent GSI and both succeed — leaving two accounts on one
    # email, which login (`items[0]`) then resolves arbitrarily.
    #
    # The fix is the pattern this codebase already uses for folder names
    # (_folder_name_claim): claim a DETERMINISTIC row whose PRIMARY KEY encodes
    # the constraint, conditionally. DynamoDB evaluates a condition on the item
    # being written with full consistency, so exactly one of two racing
    # requests can create "email#{address}" and the other gets 409.
    #
    # The claim lives in the Users table under a key shape no real user_id can
    # take (a real one is a uuid4 string, which always contains hyphens and
    # never a "#"), so claims and accounts cannot collide. _is_user_claim
    # filters them out of the one read path that scans this table.
    claim_id = _user_email_claim(email)
    try:
        _users.put_item(
            Item={"user_id": claim_id, "claims_email": email,
                  "claims_user_id": user_id, "created_at": _now_iso()},
            ConditionExpression="attribute_not_exists(user_id)")
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") \
                == "ConditionalCheckFailedException":
            raise ApiError(409, "email already registered")
        raise

    try:
        _users.put_item(
            Item={"user_id": user_id, "email": email,
                  "password_hash": hash_b64, "salt": salt_b64,
                  "name": name, "avatar_url": "",
                  "created_at": _now_iso()},
            # Guard against a race creating the same user_id (uuid collision ~never).
            ConditionExpression="attribute_not_exists(user_id)",
        )
    except Exception:
        # Never leave a claim behind that no account owns — it would make the
        # address permanently unregisterable. Same rollback create_folder does.
        _users.delete_item(Key={"user_id": claim_id})
        raise
    return _resp(201, {"token": _mint_for(user_id, email),
                       "user_id": user_id, "email": email, "name": name})


def _user_email_claim(email):
    """The deterministic uniqueness-row id for an email address.

    Lives in the Users table under a key no uuid4 can produce, so claim rows
    and account rows never collide. See the reasoning in signup().
    """
    return f"email#{str(email or '').strip().lower()}"


def _is_user_claim(item):
    """True for an email-uniqueness claim row rather than a real account."""
    return str((item or {}).get("user_id", "")).startswith("email#")


def login(event):
    data = _body(event)
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    if not email or not password:
        raise ApiError(400, "email and password required")

    res = _users.query(IndexName=EMAIL_INDEX,
                       KeyConditionExpression=Key("email").eq(email))
    items = res.get("Items") or []
    # Always run a hash to keep timing similar whether or not the user exists.
    if not items:
        _hash_password(password)  # burn time, then fail
        raise ApiError(401, "invalid credentials")
    user = items[0]
    if not _verify_password(password, user["salt"], user["password_hash"]):
        raise ApiError(401, "invalid credentials")
    return _resp(200, {"token": _mint_for(user["user_id"], user["email"]),
                       "user_id": user["user_id"], "email": user["email"]})


# ---------------------------------------------------------------------------
# Avatar storage — shared by the profile photo and contact photos.
# ---------------------------------------------------------------------------
def _avatar_key(owner_user_id, scope, subject_id, ext):
    """The S3 key for one avatar image.

    "avatars/{owner}/{scope}/{subject}-{nonce}.{ext}". The owner segment comes
    FIRST so ownership is decidable from the key alone (see _owns_avatar_key),
    and the nonce makes every upload a new object rather than an overwrite:
    replacing a photo in place would be served stale from any presigned URL
    still in flight, and would leave no way to tell a failed upload from a
    successful one.
    """
    nonce = uuid.uuid4().hex[:12]
    return f"avatars/{owner_user_id}/{scope}/{subject_id}-{nonce}.{ext}"


def _owns_avatar_key(user_id, key):
    """True when `key` is an avatar the caller uploaded.

    Checked before an avatar_url written by the CLIENT is stored, because
    PATCH /me and PATCH /contacts take that key from the request body. Without
    this a caller could point their own profile at another account's image key
    and have the API presign it for them on every read — a cross-tenant read
    granted by the server, which is exactly what section-level ownership
    checks exist to prevent.
    """
    return bool(key) and str(key).startswith(f"avatars/{user_id}/")


def _avatar_view_url(key):
    """A short-lived presigned GET for an avatar key, or "".

    Best-effort by design: a bucket or credentials problem must degrade to
    "renders initials" and never fail the profile/contact/list route that
    embeds it. Every caller treats "" as "no photo".
    """
    key = str(key or "").strip()
    if not key or not BUCKET_NAME:
        return ""
    try:
        return _s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKET_NAME, "Key": key},
            ExpiresIn=AVATAR_URL_EXPIRY,
        )
    except Exception:
        return ""


def _delete_avatar_object(key):
    """Best-effort removal of a replaced/cleared avatar object.

    Never raises: the row has already stopped pointing at this key by the time
    we get here, so a failed delete leaves an orphaned object, not a broken
    profile. Guarded on the avatars/ prefix so a malformed stored value can
    never be turned into a delete of a recording.
    """
    key = str(key or "").strip()
    if not key or not BUCKET_NAME or not key.startswith("avatars/"):
        return
    try:
        _s3.delete_object(Bucket=BUCKET_NAME, Key=key)
    except Exception:
        pass


def request_avatar_upload(event):
    """POST /avatars/upload-request {format, scope?, contact_id?, size?}
       -> {upload_url, key, expires_in, content_type}

    Presigns the PUT only. The key is NOT written to any row here — the client
    stores it by calling PATCH /me or PATCH /contacts/{id} after the upload
    succeeds. Splitting it that way means an abandoned upload leaves an
    unreferenced S3 object rather than a profile pointing at bytes that never
    arrived.
    """
    user_id = _require_auth(event)
    if not BUCKET_NAME:
        raise ApiError(500, "server misconfigured (no bucket)")
    data = _body(event)

    ext = str(data.get("format") or "jpg").strip().lower().lstrip(".")
    if ext not in AVATAR_FORMATS:
        raise ApiError(400, "unsupported image format — use one of: "
                            + ", ".join(sorted(AVATAR_FORMATS)))

    size = data.get("size")
    if size is not None:
        try:
            size = int(size)
        except (TypeError, ValueError):
            raise ApiError(400, "size must be a number of bytes")
        if size > MAX_AVATAR_BYTES:
            raise ApiError(413, f"image too large — max "
                                f"{MAX_AVATAR_BYTES // (1024 * 1024)} MB")

    scope = str(data.get("scope") or "user").strip().lower()
    if scope not in ("user", "contact"):
        raise ApiError(400, "scope must be 'user' or 'contact'")

    if scope == "contact":
        # contact_id is OPTIONAL here, and its absence is a real case rather
        # than a sloppy client: the phone-import flow uploads the address-book
        # photo BEFORE the contact exists, then passes the key to POST
        # /contacts. There is nothing to own-check yet, and nothing is weakened
        # by that — the key is still written under avatars/{caller}/, which is
        # the only thing _owns_avatar_key consults when the key later comes
        # back on a create or patch.
        #
        # When an id IS given (replacing an existing contact's photo) it is
        # own-checked up front, because presigning a write for someone else's
        # contact would hand out a usable PUT even though the follow-up PATCH
        # would be rejected.
        subject = str(data.get("contact_id") or "").strip()
        if subject:
            # write=True for the reason the comment above states: the photo
            # is stored by a follow-up PATCH /contacts, so presigning here
            # for someone who cannot make that PATCH would hand out a usable
            # upload URL for an edit that will be refused.
            _owned_contact(user_id, subject, write=True)
        else:
            subject = "new"
    else:
        subject = user_id

    key = _avatar_key(user_id, scope, subject, ext)
    # ContentType deliberately unsigned — same React Native fetch() Blob
    # behaviour documented on request_upload: signing it produces
    # SignatureDoesNotMatch on every phone upload.
    upload_url = _s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": BUCKET_NAME, "Key": key},
        ExpiresIn=UPLOAD_URL_EXPIRY,
    )
    return _resp(200, {"upload_url": upload_url, "key": key,
                       "expires_in": UPLOAD_URL_EXPIRY,
                       "content_type": AVATAR_FORMATS[ext]})


# ---------------------------------------------------------------------------
# Profile (the logged-in user's own account).
# ---------------------------------------------------------------------------
def _public_user(item):
    """The safe, client-facing view of a Users row (never salt/hash).

    `avatar_url` is the stored S3 KEY and `avatar_view_url` is a presigned GET
    derived from it at read time — see the AVATAR_FORMATS block for why the
    stored value is a key rather than a URL. Clients render
    `avatar_view_url` and treat "" as "no photo, fall back to initials".
    """
    # `name` stays the RAW stored value here — "" when unset. This is the
    # caller's own profile, the one place that distinction matters: the
    # onboarding gate keys off it, and substituting a derived name would make
    # every account look like it had already chosen one.
    #
    # `suggested_name` is that derived guess, offered separately so the gate
    # can PRE-FILL the field instead of presenting an empty box. Advisory: it
    # is never stored unless the user actually saves it.
    stored_name = item.get("name", "")
    suggested = workspace_schema.display_name(item, fallback_user_id="")
    return {
        "user_id": item.get("user_id", ""),
        "email": item.get("email", ""),
        "name": stored_name,
        "name_set": bool(str(stored_name or "").strip()),
        "suggested_name": "" if str(stored_name or "").strip() else suggested,
        "avatar_url": item.get("avatar_url", ""),
        "avatar_view_url": _avatar_view_url(item.get("avatar_url", "")),
        "created_at": item.get("created_at", ""),
    }


def get_me(event):
    user_id = _require_auth(event)
    item = _users.get_item(Key={"user_id": user_id}).get("Item")
    if not item:
        raise ApiError(404, "user not found")
    return _resp(200, {"user": _public_user(item)})


def patch_me(event):
    user_id = _require_auth(event)
    data = _body(event)
    # Only these fields are updatable via the profile screen.
    sets, names, values = [], {}, {}
    if "name" in data:
        names["#name"] = "name"
        values[":name"] = (data.get("name") or "").strip()[:100]
        sets.append("#name = :name")
    # avatar_url is the S3 key returned by POST /avatars/upload-request. It is
    # validated against the caller's own prefix rather than trusted: the value
    # arrives from the client and is later presigned by the server on every
    # read, so an unchecked key would let one account have the API sign reads
    # of another account's image. "" is the legitimate "remove my photo".
    new_avatar = None
    if "avatar_url" in data:
        new_avatar = (data.get("avatar_url") or "").strip()[:1000]
        if new_avatar and not _owns_avatar_key(user_id, new_avatar):
            raise ApiError(400, "avatar_url must be a key from "
                                "POST /avatars/upload-request")
        names["#a"] = "avatar_url"
        values[":a"] = new_avatar
        sets.append("#a = :a")
    if not sets:
        raise ApiError(400, "nothing to update (name or avatar_url)")

    # Read the outgoing key BEFORE the write, so the object it points at can be
    # cleaned up once nothing references it any more.
    prev_avatar = ""
    if new_avatar is not None:
        prev = _users.get_item(Key={"user_id": user_id}).get("Item") or {}
        prev_avatar = str(prev.get("avatar_url") or "")

    _users.update_item(
        Key={"user_id": user_id},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
        ConditionExpression="attribute_exists(user_id)",
    )
    if prev_avatar and prev_avatar != new_avatar:
        _delete_avatar_object(prev_avatar)
    item = _users.get_item(Key={"user_id": user_id}).get("Item")
    return _resp(200, {"user": _public_user(item)})


def change_password(event):
    user_id = _require_auth(event)
    data = _body(event)
    current = data.get("current_password") or ""
    new = data.get("new_password") or ""
    if len(new) < 8:
        raise ApiError(400, "new password must be at least 8 characters")
    item = _users.get_item(Key={"user_id": user_id}).get("Item")
    if not item:
        raise ApiError(404, "user not found")
    if not _verify_password(current, item["salt"], item["password_hash"]):
        raise ApiError(401, "current password is incorrect")
    salt_b64, hash_b64 = _hash_password(new)
    _users.update_item(
        Key={"user_id": user_id},
        UpdateExpression="SET password_hash = :h, salt = :s",
        ExpressionAttributeValues={":h": hash_b64, ":s": salt_b64},
    )
    return _resp(200, {"changed": True})


# ---------------------------------------------------------------------------
# Devices — pairing service (user-owned architecture).
#
# The Devices table (PK device_id) is the source of truth for ownership.
# The app only ever handles device_id + pairing_code; the device API key
# stays between the firmware and DeviceKeys (see the module docstring).
# ---------------------------------------------------------------------------
_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PAIRING_CODE_RE = re.compile(r"^\d{6}$")


class FirmwareVerifier:
    """Seam for the real device-side pairing confirmation.

    In the target flow the device proves it heard the pairing code (it sends
    device_id + its API key + the code to the backend). That firmware path is
    NOT built yet, so this MVP mock auto-approves. Replace confirm_pairing()
    with the real round-trip (e.g. a device-acknowledged flag written by the
    device-facing endpoint) without touching the route handlers.
    """

    def confirm_pairing(self, device_id: str, pairing_code: str) -> bool:
        print(f"[mock-firmware] confirm_pairing device={device_id} auto-approved")
        return True

    def factory_reset(self, device_id: str) -> bool:
        """Ask the device to wipe its local state (creds, queued recordings).

        Mocked like confirm_pairing: the real implementation queues the
        command and waits for the device to acknowledge it. Returning False
        must leave server state untouched, so the caller can retry.
        """
        print(f"[mock-firmware] factory_reset device={device_id} auto-acknowledged")
        return True


_firmware = FirmwareVerifier()


def _hash_pairing_code(device_id: str, code: str) -> str:
    # Stored instead of the raw code so a DB read can't harvest live codes.
    return hashlib.sha256(f"{device_id}:{code}".encode("utf-8")).hexdigest()


def _get_device(device_id: str) -> dict:
    if not _DEVICE_ID_RE.match(device_id or ""):
        raise ApiError(400, "valid device_id required")
    # "mobile"/"uploads" are reserved S3 path segments for non-device
    # recording sources — a device must never be provisioned with them.
    if device_id in RESERVED_DEVICE_IDS:
        raise ApiError(400, "reserved device_id")
    item = _devices.get_item(Key={"device_id": device_id}).get("Item")
    if not item:
        raise ApiError(404, "device not found")
    return item


def _public_device(item: dict) -> dict:
    """Client-facing view of a Devices row (never pairing_code_hash)."""
    return {
        "device_id": item.get("device_id", ""),
        # Falls back to device_id so clients always have something to render.
        "name": item.get("name") or item.get("device_id", ""),
        "status": item.get("status", STATUS_UNPAIRED),
        "paired_at": item.get("paired_at") or None,
        "last_seen": item.get("last_seen") or None,
        "firmware_version": item.get("firmware_version") or None,
        "serial_number": item.get("serial_number") or None,
        # Not reported by the hardware yet (MVP) — explicit nulls, see spec.
        "battery": None,
        "storage": None,
    }


def _user_owns_device(user_id: str, device: dict) -> bool:
    """Paired owner, or a legacy UserDevices claim (pre-pairing data)."""
    if device.get("paired_user_id") == user_id:
        return True
    row = _user_devices.get_item(
        Key={"user_id": user_id, "device_id": device.get("device_id", "")}
    ).get("Item")
    return row is not None


def pair_request(event):
    """POST /devices/pair-request {device_id} -> {pairing_code, expires_in}."""
    user_id = _require_auth(event)
    data = _body(event)
    device_id = (data.get("device_id") or "").strip()
    device = _get_device(device_id)

    if device.get("paired_user_id"):
        raise ApiError(409, "device already paired")

    code = f"{secrets.randbelow(1_000_000):06d}"
    try:
        _devices.update_item(
            Key={"device_id": device_id},
            UpdateExpression=("SET #st = :pairing, pairing_code_hash = :h, "
                              "pairing_expires_at = :exp, pairing_user_id = :u"),
            # Single-user enforcement: never overwrite an existing owner,
            # even if this handler raced a concurrent /devices/pair.
            ConditionExpression=("attribute_exists(device_id) AND "
                                 "attribute_not_exists(paired_user_id)"),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":pairing": STATUS_PAIRING,
                ":h": _hash_pairing_code(device_id, code),
                ":exp": int(time.time()) + PAIRING_CODE_TTL,
                ":u": user_id,
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ApiError(409, "device already paired")
        raise
    return _resp(200, {"pairing_code": code, "expires_in": PAIRING_CODE_TTL})


def pair_device(event):
    """POST /devices/pair {device_id, pairing_code} -> {device:{...}}.

    Firmware confirmation is mocked (FirmwareVerifier). The conditional
    update makes exactly one confirm win, so two users can never both end
    up owning the device.
    """
    user_id = _require_auth(event)
    data = _body(event)
    device_id = (data.get("device_id") or "").strip()
    code = str(data.get("pairing_code") or "").strip()
    if not _PAIRING_CODE_RE.match(code):
        raise ApiError(400, "pairing_code must be 6 digits")
    device = _get_device(device_id)

    if device.get("paired_user_id"):
        raise ApiError(409, "device already paired")
    stored_hash = device.get("pairing_code_hash")
    if device.get("status") != STATUS_PAIRING or not stored_hash:
        raise ApiError(400, "no pairing in progress — call /devices/pair-request first")
    if int(device.get("pairing_expires_at") or 0) < int(time.time()):
        _lapse_pairing(device_id)
        raise ApiError(410, "pairing code expired — request a new one")
    if not hmac.compare_digest(_hash_pairing_code(device_id, code), stored_hash):
        raise ApiError(403, "invalid pairing code")
    if device.get("pairing_user_id") != user_id:
        raise ApiError(403, "pairing code was issued to a different account")
    if not _firmware.confirm_pairing(device_id, code):
        raise ApiError(502, "device did not confirm pairing")

    now = _now_iso()
    try:
        _devices.update_item(
            Key={"device_id": device_id},
            UpdateExpression=("SET #st = :paired, paired_user_id = :u, paired_at = :now "
                              "REMOVE pairing_code_hash, pairing_expires_at, pairing_user_id"),
            ConditionExpression=("#st = :pairing AND pairing_code_hash = :h AND "
                                 "attribute_not_exists(paired_user_id)"),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":paired": STATUS_PAIRED, ":pairing": STATUS_PAIRING,
                ":u": user_id, ":now": now, ":h": stored_hash,
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ApiError(409, "device already paired")
        raise
    # Legacy compatibility mirror — keeps pre-user_id recordings reachable
    # through the UserDevices join (see the module docstring).
    _user_devices.put_item(Item={"user_id": user_id, "device_id": device_id,
                                 "claimed_at": now})
    return _resp(200, {"device": _public_device(_get_device(device_id))})


def _lapse_pairing(device_id: str) -> None:
    """Best-effort reset of an expired PAIRING attempt back to UNPAIRED."""
    try:
        _devices.update_item(
            Key={"device_id": device_id},
            UpdateExpression=("SET #st = :unpaired "
                              "REMOVE pairing_code_hash, pairing_expires_at, pairing_user_id"),
            ConditionExpression="#st = :pairing AND attribute_not_exists(paired_user_id)",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":unpaired": STATUS_UNPAIRED,
                                       ":pairing": STATUS_PAIRING},
        )
    except ClientError:
        pass  # someone else already moved the state on — fine


def get_device_detail(event):
    """GET /devices/{device_id} -> {device:{...}} (owner only)."""
    user_id = _require_auth(event)
    device_id = (event.get("pathParameters") or {}).get("device_id", "")
    device = _get_device(device_id)
    if not _user_owns_device(user_id, device):
        # 404, not 403 — don't leak which device_ids exist to non-owners.
        raise ApiError(404, "device not found")
    return _resp(200, {"device": _public_device(device)})


def unpair_device(event):
    """DELETE /devices/{device_id} — remove ownership, keep everything else.

    Deletes NOTHING except the ownership link: the device row, its
    recordings and AI outputs all survive. Before releasing the device,
    legacy recordings (rows with no user_id) are stamped with the departing
    owner's user_id so their history stays in their account and the next
    owner can never see it.
    """
    user_id = _require_auth(event)
    device_id = (event.get("pathParameters") or {}).get("device_id", "")
    device = _get_device(device_id)
    if device.get("paired_user_id") != user_id:
        raise ApiError(404, "device not found")

    _stamp_user_on_legacy_recordings(device_id, user_id)
    try:
        _devices.update_item(
            Key={"device_id": device_id},
            UpdateExpression="SET #st = :unpaired REMOVE paired_user_id, paired_at",
            ConditionExpression="paired_user_id = :u",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":unpaired": STATUS_UNPAIRED, ":u": user_id},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ApiError(409, "device ownership changed — refresh and retry")
        raise
    # Drop the legacy claim link too, or the device-index union would keep
    # showing this user the NEXT owner's recordings.
    _user_devices.delete_item(Key={"user_id": user_id, "device_id": device_id})
    return _resp(200, {"device_id": device_id, "unpaired": True})


def rename_device(event):
    """PATCH /devices/{device_id} {name} -> {device:{...}} (owner only).

    Cosmetic, user-scoped label. Stored on the Devices row rather than the
    ownership link because only one user can own a device at a time; the
    unpair path clears it so the next owner doesn't inherit the old name.
    """
    user_id = _require_auth(event)
    device_id = (event.get("pathParameters") or {}).get("device_id", "")
    device = _get_device(device_id)
    if not _user_owns_device(user_id, device):
        raise ApiError(404, "device not found")

    data = _body(event)
    if "name" not in data:
        raise ApiError(400, "name required")
    name = (data.get("name") or "").strip()
    if len(name) > DEVICE_NAME_MAX:
        raise ApiError(400, f"name must be at most {DEVICE_NAME_MAX} characters")

    if name:
        _devices.update_item(
            Key={"device_id": device_id},
            UpdateExpression="SET #nm = :n",
            ExpressionAttributeNames={"#nm": "name"},
            ExpressionAttributeValues={":n": name},
        )
    else:
        # Empty string clears the label back to the default (device_id).
        _devices.update_item(
            Key={"device_id": device_id},
            UpdateExpression="REMOVE #nm",
            ExpressionAttributeNames={"#nm": "name"},
        )
    return _resp(200, {"device": _public_device(_get_device(device_id))})


def factory_reset_device(event):
    """POST /devices/{device_id}/factory-reset -> {device_id, reset, unpaired}.

    Owner-only. Asks the firmware to wipe itself FIRST (mocked) and only then
    releases ownership, so a device that never acknowledged stays paired and
    the call can be retried. Recordings are always preserved — same contract
    as unpair; a reset wipes the hardware, not the user's history.
    """
    user_id = _require_auth(event)
    device_id = (event.get("pathParameters") or {}).get("device_id", "")
    device = _get_device(device_id)
    if device.get("paired_user_id") != user_id:
        raise ApiError(404, "device not found")

    if not _firmware.factory_reset(device_id):
        raise ApiError(502, "device did not acknowledge factory reset")

    _stamp_user_on_legacy_recordings(device_id, user_id)
    now = _now_iso()
    try:
        _devices.update_item(
            Key={"device_id": device_id},
            UpdateExpression=("SET #st = :unpaired, factory_reset_at = :now "
                              "REMOVE paired_user_id, paired_at, #nm, "
                              "pairing_code_hash, pairing_expires_at, pairing_user_id"),
            ConditionExpression="paired_user_id = :u",
            ExpressionAttributeNames={"#st": "status", "#nm": "name"},
            ExpressionAttributeValues={":unpaired": STATUS_UNPAIRED, ":u": user_id,
                                       ":now": now},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ApiError(409, "device ownership changed — refresh and retry")
        raise
    _user_devices.delete_item(Key={"user_id": user_id, "device_id": device_id})
    return _resp(200, {"device_id": device_id, "reset": True, "unpaired": True,
                       "factory_reset_at": now})


def _stamp_user_on_legacy_recordings(device_id: str, user_id: str) -> None:
    """Give un-owned rows of this device to user_id (idempotent, paginated)."""
    kwargs = dict(IndexName=DEVICE_INDEX,
                  KeyConditionExpression=Key("device_id").eq(device_id))
    while True:
        res = _recordings.query(**kwargs)
        for item in res.get("Items", []):
            if item.get("user_id"):
                continue
            try:
                _recordings.update_item(
                    Key={"audio_s3_key": item["audio_s3_key"]},
                    UpdateExpression="SET user_id = :u",
                    ConditionExpression="attribute_not_exists(user_id)",
                    ExpressionAttributeValues={":u": user_id},
                )
            except ClientError as e:
                if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
        lek = res.get("LastEvaluatedKey")
        if not lek:
            return
        kwargs["ExclusiveStartKey"] = lek


def claim_device(event):
    """POST /devices/claim {apiKey} — LEGACY pairing path (typed device key).

    Kept so already-shipped app builds keep working, but bridged into the
    new ownership model: the claim now performs a real pairing (Devices row
    -> PAIRED) with the same single-user guarantee, so a claimed device can
    upload under the user-owned rules. New app builds should use
    /devices/pair-request + /devices/pair instead — the app should never
    handle the device API key (see module docstring, Security).
    """
    user_id = _require_auth(event)
    data = _body(event)
    api_key = (data.get("apiKey") or "").strip()
    if not api_key:
        raise ApiError(400, "apiKey required")

    # Verify the device key exists (DeviceKeys: PK apiKey -> deviceId).
    dk = _device_keys.get_item(Key={"apiKey": api_key}).get("Item")
    if not dk or not dk.get("deviceId"):
        raise ApiError(403, "invalid device key")
    device_id = dk["deviceId"]

    # Single-user enforcement, same rule as pairing: one owner, ever.
    now = _now_iso()
    try:
        _devices.update_item(
            Key={"device_id": device_id},
            UpdateExpression=("SET #st = :paired, paired_user_id = :u, paired_at = :now "
                              "REMOVE pairing_code_hash, pairing_expires_at, pairing_user_id"),
            ConditionExpression=("attribute_not_exists(paired_user_id) OR "
                                 "paired_user_id = :u"),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":paired": STATUS_PAIRED,
                                       ":u": user_id, ":now": now},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ApiError(409, "device already paired to another account")
        raise

    _user_devices.put_item(Item={"user_id": user_id, "device_id": device_id,
                                 "claimed_at": now})
    return _resp(200, {"device_id": device_id, "claimed": True})


def _owned_devices(user_id: str) -> list:
    """device_ids the user owns: paired (Devices GSI) + legacy claims."""
    ids = set()
    res = _user_devices.query(
        KeyConditionExpression=Key("user_id").eq(user_id))
    ids.update(it["device_id"] for it in res.get("Items", []))
    res = _devices.query(
        IndexName=PAIRED_USER_INDEX,
        KeyConditionExpression=Key("paired_user_id").eq(user_id))
    ids.update(it["device_id"] for it in res.get("Items", []))
    return sorted(ids)


# ===========================================================================
# WORKSPACE AUTHORIZATION (Phase 2A)
#
# The layer the AI TENANCY block predicted: "MinuteX has no organizations
# table: a user's workspace IS the tenant... If an org layer is added later,
# AIContext is the one place that has to learn about it."
#
# THE ONE RULE
#
#   The workspace a request acts in is RESOLVED FROM THE DATABASE, never
#   accepted from the caller.
#
# A client says which workspace it WANTS (a header — see _workspace_context).
# That is a request, not a claim. Membership and role are then read from
# WorkspaceMemberships and the caller gets whatever that row says, or 404.
#
# WHY MEMBERSHIP IS READ ON EVERY REQUEST AND NOT PUT IN THE JWT.
# The JWT here lasts 24h (JWT_TTL) and there is no refresh token, no denylist
# and no server-side logout — clearToken() on the client is all "sign out"
# does. So a role or membership copied into a token would stay valid for up to
# a day after it was revoked. Removing a member has to STOP ACCESS NOW, which
# makes this GetItem the revocation mechanism. It is one strongly-consistent
# point read on a PK+SK table, which is why the table is shaped that way.
#
# WHY 404 AND NOT 403, everywhere in this section: the same reason the rest of
# this file does it (see the _owned_* helpers). Telling an outsider that a
# workspace EXISTS but is closed to them is itself a disclosure.
#
# WHAT THIS SECTION DOES NOT DO, in Phase 2A:
#   - It does not decide which MEETINGS a member may read. Actions and
#     visibility are separate concerns; resource-level access is Phase 2B.
#   - It changes no existing route. Every helper here is currently called only
#     by the workspace routes and by tests.
#   - It does not touch authentication. _require_auth is unchanged.
# ===========================================================================
def _personal_workspace_id(user_id):
    """This user's personal workspace id. Derived, so it needs no lookup."""
    return workspace_schema.personal_workspace_id(user_id)


def _workspace_row(workspace_id):
    """The Workspaces row, or None. Never raises for a missing table.

    Tolerating a missing table matters during rollout: scripts/50 creates the
    tables and scripts/52 backfills them, and between those two the code is
    already deployed. A ResourceNotFound here must read as "no workspace yet",
    which is exactly how the personal fallback below treats it — not as a 500
    on a route that has nothing to do with workspaces.
    """
    wid = str(workspace_id or "").strip()
    if not wid:
        return None
    try:
        return _workspaces.get_item(Key={"workspace_id": wid}).get("Item")
    except ClientError as err:
        print(f"[workspace] read failed for {wid}: {err}")
        return None


def _workspace_membership(workspace_id, user_id, *, consistent=True):
    """The caller's membership row in this workspace, or None.

    CONSISTENT READ BY DEFAULT, and that is the whole point of this function.
    An eventually-consistent read here would let a just-removed member keep
    working for the length of the replication lag, which is precisely the
    window this design exists to close.

    THE PERSONAL SHORT-CIRCUIT. A personal workspace id encodes its owner
    (workspace_schema.personal_workspace_id), so membership in it is decidable
    without any read at all: you are the sole OWNER of your own personal
    workspace and nobody else is a member of it, ever. This is not an
    optimization — it is what lets every existing user behave normally before
    the backfill has written a single row.
    """
    wid = str(workspace_id or "").strip()
    uid = str(user_id or "").strip()
    if not wid or not uid:
        return None

    if workspace_schema.is_personal_workspace_id(wid):
        owner = workspace_schema.user_id_from_personal_workspace(wid)
        if owner != uid:
            return None
        return workspace_schema.new_membership(
            wid, uid, workspace_schema.ROLE_OWNER, _now_iso())

    try:
        return _memberships.get_item(
            Key={"workspace_id": wid, "user_id": uid},
            ConsistentRead=consistent,
        ).get("Item")
    except ClientError as err:
        # Fail CLOSED. A membership read that errors must deny, never allow —
        # the opposite of the best-effort reads elsewhere in this file, which
        # degrade a display. This one is an authorization decision.
        print(f"[workspace] membership read failed {wid}/{uid}: {err}")
        return None


def _active_membership(workspace_id, user_id):
    """Membership only when it currently grants access.

    Both halves are required and neither implies the other: a REMOVED member
    still has a row (kept for audit), and an ACTIVE member of a SUSPENDED or
    DELETED workspace must not get in either.
    """
    membership = _workspace_membership(workspace_id, user_id)
    if not workspace_schema.membership_is_active(membership):
        return None
    if workspace_schema.is_personal_workspace_id(workspace_id):
        # A personal workspace need not have a row yet (see the short-circuit
        # above); when it does exist it must still be active.
        row = _workspace_row(workspace_id)
        if row and not workspace_schema.workspace_is_active(row):
            return None
        return membership
    row = _workspace_row(workspace_id)
    if not workspace_schema.workspace_is_active(row):
        return None
    return membership


def _require_workspace_member(event, workspace_id=None):
    """(user_id, workspace_id, membership) or 404.

    THE entry point for every workspace-scoped route. Identity comes from the
    JWT via _require_auth; the workspace comes from the path or the header and
    is then VERIFIED, never trusted.
    """
    user_id = _require_auth(event)
    wid = str(workspace_id or "").strip() or _requested_workspace_id(event)
    if not wid:
        wid = _personal_workspace_id(user_id)
    membership = _active_membership(wid, user_id)
    if not membership:
        raise ApiError(404, "workspace not found")
    return user_id, wid, membership


def _require_workspace_role(event, minimum, workspace_id=None):
    """As _require_workspace_member, but the role must reach `minimum`.

    403 here, NOT 404 — and the difference from the helpers above is
    deliberate. A member who lacks a capability already knows the workspace
    exists (they are in it), so 404 would be a lie that makes the UI
    unexplainable. 404 hides existence from OUTSIDERS; 403 tells an insider
    they lack a right.
    """
    user_id, wid, membership = _require_workspace_member(
        event, workspace_id=workspace_id)
    if not workspace_schema.role_at_least(membership.get("role"), minimum):
        raise ApiError(403, f"this action requires the {minimum} role")
    return user_id, wid, membership


def _require_workspace_capability(event, capability, workspace_id=None):
    """Role check expressed as a CAPABILITY rather than a rank.

    Preferred at call sites: `_require_workspace_capability(event,
    CAP_MANAGE_MEMBERS)` states the intent, so changing which role may manage
    members is a one-line edit in workspace_schema rather than a hunt through
    routes for the right comparison.
    """
    user_id, wid, membership = _require_workspace_member(
        event, workspace_id=workspace_id)
    if not workspace_schema.role_can(membership.get("role"), capability):
        raise ApiError(403, f"this action requires the {capability} permission")
    return user_id, wid, membership


# The header a client uses to say which workspace it is acting in. A HINT: it
# names a workspace, it never proves membership of one. Every read of it goes
# straight into _active_membership before anything is returned.
WORKSPACE_HEADER = "x-minutex-workspace"

# 409 + a stable machine-readable code, following the same pattern
# SalesforceReconnectRequired uses: the request is authenticated and
# well-formed but conflicts with the current state of the identity, and the
# app must branch on `code` rather than on wording.
#
# Lowercase snake_case to match the existing codes
# (salesforce_reconnect_required, integration_not_connected).
ORG_EMAIL_CONFLICT_CODE = "organisation_email_conflict"


class OrganisationEmailConflict(ApiError):
    """The invited address belongs to a PERSONAL MinuteX account.

    The agreed identity model forbids both merging and converting, so the
    invitation cannot be accepted by this identity — the person needs a work
    email. Never 401: that would clear the JWT and sign the user out of the
    personal account the message tells them to keep.
    """

    def __init__(self, message):
        super().__init__(409, message)
        self.code = ORG_EMAIL_CONFLICT_CODE


# The creation-side twin of the above. A DISTINCT code, not a reuse: the two
# cases need different screens. organisation_email_conflict says "you were
# invited on the wrong address"; this one says "this account already has
# personal data, start the organisation from a work account". Collapsing them
# would leave the app unable to tell the user what to actually do.
PERSONAL_WORKSPACE_IN_USE_CODE = "personal_workspace_in_use"


class PersonalWorkspaceInUse(ApiError):
    """This identity has personal data, so it cannot become an organisation
    owner. Never converts and never merges — the personal account and its
    data are left exactly as they are."""

    def __init__(self, message):
        super().__init__(409, message)
        self.code = PERSONAL_WORKSPACE_IN_USE_CODE


def _requested_workspace_id(event):
    """The workspace the client ASKED for, or "" — unverified by construction.

    Deliberately not called anywhere except _require_workspace_member, so
    there is no path on which this value reaches a query without having been
    checked first. The body is never consulted: a workspace id arriving in a
    JSON payload would be one more place to forget to verify, and the header
    is the one the app already has a natural place to set.
    """
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    return str(headers.get(WORKSPACE_HEADER) or "").strip()


def _user_workspaces(user_id):
    """Every workspace this user can act in, personal first.

    The personal workspace is SYNTHESIZED when no row exists yet, so this
    returns a correct answer before the backfill runs and an identical one
    after it. That property is what makes scripts/52 an optimization rather
    than a correctness gate — and it is why the migration can be re-run,
    interrupted, or skipped entirely without breaking a user.

    The user-index is used only to ENUMERATE. Each row is still resolved
    through _active_membership, so a stale index entry cannot grant access:
    an index is a lookup path, never an authorization decision.
    """
    uid = str(user_id or "").strip()
    if not uid:
        return []

    out = []
    personal_id = _personal_workspace_id(uid)
    personal = _workspace_row(personal_id)
    if personal is None:
        personal = workspace_schema.new_personal_workspace(uid, _now_iso())
    if workspace_schema.workspace_is_active(personal):
        out.append((personal, workspace_schema.ROLE_OWNER, None))

    try:
        rows = _query_all(_memberships, IndexName=MEMBERSHIPS_USER_INDEX,
                          KeyConditionExpression=Key("user_id").eq(uid))
    except ClientError as err:
        # The personal workspace is still returned: a failure to list
        # ORGANISATIONS must not cost the user their own workspace.
        print(f"[workspace] membership list failed for {uid}: {err}")
        return out

    for row in rows:
        wid = str(row.get("workspace_id") or "")
        if not wid or wid == personal_id:
            continue
        membership = _active_membership(wid, uid)
        if not membership:
            continue
        workspace = _workspace_row(wid)
        if not workspace:
            continue
        out.append((workspace, membership.get("role", ""), membership))
    return out


def _ensure_personal_workspace(user_id):
    """Create this user's personal workspace row if it is absent. Idempotent.

    LAZY MIGRATION. Called from the workspace routes rather than from login,
    so it costs nothing on the hot auth path and needs no backfill to have
    run. The conditional write makes a concurrent double-call safe, and an
    already-present row is left exactly as it is — including a renamed one,
    which a blind put would have silently reset.
    """
    uid = str(user_id or "").strip()
    if not uid:
        return None
    wid = _personal_workspace_id(uid)
    existing = _workspace_row(wid)
    if existing:
        return existing

    item = workspace_schema.new_personal_workspace(uid, _now_iso())
    try:
        _workspaces.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(workspace_id)")
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") \
                == "ConditionalCheckFailedException":
            # Another request created it between our read and our write.
            return _workspace_row(wid) or item
        raise
    _audit("workspace.personal_created", uid, wid)
    return item


def _has_personal_resources(user_id):
    """Has this identity actually USED its personal workspace?

    The enforceable form of "already has a Personal Workspace" — see the
    ORGANISATION CREATION block in workspace_schema for why the literal
    reading is impossible (personal workspace ids are derived, so every
    identity has one from signup).

    A personal RESOURCE is the honest signal: a recording, a task, a folder
    or a contact the identity owns. Any one of them means this is a personal
    account with data to lose, and making it an organisation owner would be
    the silent conversion the identity model forbids.

    THREE Limit=1 POINT QUERIES, short-circuiting on the first hit — each one
    is an existing owner-partitioned GSI, so this is O(1) and runs only on
    organisation CREATION, never on a hot path.

    FAILS CLOSED. A read error returns True (treat as "has data"), which
    refuses the creation rather than risking the conversion. The user gets a
    retryable 409 instead of an irreversible account change.
    """
    uid = str(user_id or "").strip()
    if not uid:
        return False

    probes = (
        (_recordings, USER_INDEX, "user_id"),
        (_tasks, TASKS_OWNER_INDEX, "owner_user_id"),
        (_contacts, CONTACTS_OWNER_INDEX, "owner_user_id"),
    )
    for table, index, attr in probes:
        try:
            res = table.query(IndexName=index,
                              KeyConditionExpression=Key(attr).eq(uid),
                              Limit=1)
        except ClientError as err:
            print(f"[workspace] personal-resource probe failed on "
                  f"{index}/{attr} for {uid}: {err}")
            return True
        if res.get("Items"):
            return True
    return False


def _has_organisation_membership(user_id):
    """Does this identity belong to ANY organisation?

    The derived signal that separates an ORGANISATION identity from a
    PERSONAL-ONLY one (see the IDENTITY block in workspace_schema). Derived
    rather than stored, so there is no account-kind column to keep in step and
    no way for it to disagree with the memberships that actually exist.

    Fails CLOSED: a read error returns True, which makes acceptance fall
    through to the ordinary membership checks instead of wrongly telling a
    real organisation user that their address is personal-only. The wrong
    direction here would be an insulting and unfixable error message.
    """
    uid = str(user_id or "").strip()
    if not uid:
        return False
    try:
        rows = _query_all(_memberships, IndexName=MEMBERSHIPS_USER_INDEX,
                          KeyConditionExpression=Key("user_id").eq(uid))
    except ClientError as err:
        print(f"[workspace] org-membership probe failed for {uid}: {err}")
        return True
    for row in rows:
        if not workspace_schema.is_organisation_workspace_id(
                row.get("workspace_id")):
            continue
        if row.get("status") in (workspace_schema.MEMBERSHIP_ACTIVE,
                                 workspace_schema.MEMBERSHIP_INVITED):
            return True
    return False


# ---------------------------------------------------------------------------
# RESOURCE OWNERSHIP (Phase 2B)
#
# THE ONE RULE, and it is the reason this reads the row rather than the
# request:
#
#     A resource's workspace is whatever its STORED ROW says. The client is
#     asked which workspace it wants only at CREATION, and even then the
#     answer is verified against membership before it is written.
#
# After creation the row is authoritative forever. That is what makes the
# asynchronous pipeline safe: transcribeRecording, the STT webhook and the
# tasks.seed invoke all resolve ownership by loading the recording, so a
# retry, a replay, or a forged event payload cannot move a meeting between
# workspaces.
# ---------------------------------------------------------------------------
def _row_workspace_id(row, owner_field="user_id"):
    """The workspace a stored row belongs to.

    An absent workspace_id means the row predates Phase 2B, which means it is
    PERSONAL to its owner — the only thing it could have been. Resolved, not
    guessed, and it can never resolve to an organisation.
    """
    return workspace_schema.resolve_workspace_id(row, owner_field=owner_field)


def _creation_workspace(event, user_id):
    """The workspace a NEW resource should be created in.

    The client's header is a REQUEST. It is verified against membership here,
    once, and the verified id is what gets written to the row — after which
    nothing reads the header again for that resource.

    Falls back to the caller's personal workspace when no header is sent, so
    every existing client keeps creating personal resources exactly as before
    without being changed at all.
    """
    requested = _requested_workspace_id(event)
    if not requested:
        return _personal_workspace_id(user_id)
    membership = _active_membership(requested, user_id)
    if not membership:
        # Same 404 an unknown workspace gets — an outsider must not learn
        # that this one exists.
        raise ApiError(404, "workspace not found")
    return requested


def _workspace_role(workspace_id, user_id):
    """The caller's role in a workspace, or "" when they are not a member."""
    membership = _active_membership(workspace_id, user_id)
    return str((membership or {}).get("role") or "")


def _can_read_meeting(user_id, item, *, role=None, devices=None):
    """May this caller READ this meeting? The whole visibility rule, once.

    ORDER MATTERS, AND THIS IS THE ORDER (remediation of the P0 finding):

        1. what workspace owns the row?
        2. is it an ORGANISATION?
        3. if so: does the caller hold an ACTIVE membership? no -> DENY
        4. only then: creator / role / explicit grant

    THE BUG THIS FIXES. The creator branch used to run FIRST, so
    `item["user_id"] == user_id` returned "OWNER" before anything looked at
    membership. A member who was removed from an organisation therefore kept
    read AND write access to every meeting they had created there — for the
    ~30 mutating routes behind _owned_recording as well as create_share and
    crm_sync_record. Removal has to stop access to ORGANISATION-OWNED data
    even when the caller made it: the meeting belongs to the organisation,
    which is the whole point of workspace_id being the ownership boundary.

    PERSONAL IS UNCHANGED. A row with no workspace_id, or one whose workspace
    is personal, never reaches the membership gate — it takes the original
    creator/device test exactly as before. Personal resources must not start
    requiring organisation membership.

    `role` and `devices` let a LIST resolve both once for the whole page
    instead of per row (see _list_workspace_recordings). Passing them is an
    optimization only: omitting them changes no decision, it just costs
    reads.

    Returns the access reason ("OWNER", "ROLE", or a workspace_schema.ACCESS_*
    value), or "" for denied, so callers can report WHY without re-deriving.
    """
    if not item:
        return ""

    workspace_id = _row_workspace_id(item)
    is_org = workspace_schema.is_organisation_workspace_id(workspace_id)

    if is_org:
        # THE GATE. Before creator, before role, before any grant.
        if role is None:
            role = _workspace_role(workspace_id, user_id)
        if not role:
            # No active membership in the owning organisation. Nothing below
            # may rescue that — not being the creator, not owning the device,
            # and not holding a MeetingAccess row.
            return ""

    if item.get("user_id") == user_id:
        return "OWNER"
    if devices is None:
        devices = _owned_devices(user_id)
    if item.get("device_id") in devices:
        return "OWNER"

    if is_org:
        if workspace_schema.role_sees_all_meetings(role):
            return "ROLE"
        granted = _meeting_access_row(item.get("audio_s3_key", ""), user_id)
        if granted:
            return str(granted.get("access_type")
                       or workspace_schema.ACCESS_EXPLICIT)
    return ""


def _can_write_meeting(user_id, item, *, role=None, devices=None):
    """May this caller MUTATE this meeting?

    Narrower than _can_read_meeting on purpose, and the two must stay
    different: a MANAGER may READ a colleague's meeting but must not be able
    to edit its MoM, regenerate its documents or delete it. Only the
    CREATOR (or the owner of the device that recorded it) may write.

    The organisation membership gate applies here too, and for the same
    reason — a removed member must not keep editing the organisation's data
    just because they created it.
    """
    if not item:
        return False

    workspace_id = _row_workspace_id(item)
    if workspace_schema.is_organisation_workspace_id(workspace_id):
        if role is None:
            role = _workspace_role(workspace_id, user_id)
        if not role:
            return False

    if item.get("user_id") == user_id:
        return True
    if devices is None:
        devices = _owned_devices(user_id)
    return item.get("device_id") in devices


def _meeting_access_row(meeting_id, user_id):
    """The MeetingAccess row granting this user access, or None.

    Best-effort: a missing table (the feature not yet deployed) reads as "no
    explicit grant" rather than failing the request, because every other
    branch of _can_read_meeting still works without it.
    """
    mid = str(meeting_id or "").strip()
    uid = str(user_id or "").strip()
    if not mid or not uid:
        return None
    try:
        return _meeting_access.get_item(
            Key={"meeting_id": mid, "user_id": uid}).get("Item")
    except ClientError as err:
        print(f"[workspace] meeting access read failed {mid}/{uid}: {err}")
        return None


def _grant_meeting_access(meeting_id, user_id, access_type, *,
                          workspace_id="", granted_by=""):
    """Record that a user may reach a meeting. Idempotent.

    Only ever called for ORGANISATION meetings: in a personal workspace the
    owner is the only member, so a grant row would be noise that can never
    change an answer.
    """
    mid = str(meeting_id or "").strip()
    uid = str(user_id or "").strip()
    if not mid or not uid:
        return None
    if not workspace_schema.is_organisation_workspace_id(workspace_id):
        return None
    item = workspace_schema.new_meeting_access(
        mid, uid, access_type, _now_iso(),
        workspace_id=workspace_id, granted_by=granted_by)
    try:
        _meeting_access.put_item(Item=item)
    except ClientError as err:
        print(f"[workspace] meeting access write failed {mid}/{uid}: {err}")
        return None
    return item


def list_devices(event):
    user_id = _require_auth(event)
    ids = _owned_devices(user_id)
    # `devices` keeps the original list-of-ids shape (existing app builds
    # render it directly); `details` adds the full rows where they exist.
    details = []
    for device_id in ids:
        item = _devices.get_item(Key={"device_id": device_id}).get("Item")
        if item:
            details.append(_public_device(item))
    return _resp(200, {"devices": ids, "details": details})


# ---------------------------------------------------------------------------
# Recording sources & user uploads (MOBILE / UPLOAD presign service).
#
# The mirror image of getUploadUrl's DEVICE flow: authenticate (JWT instead
# of x-api-key), build the user-owned S3 key, presign the PUT, upsert the
# ownership stub. Downstream of S3 everything is source-agnostic — the same
# ObjectCreated trigger runs the same transcription + AI pipeline.
# ---------------------------------------------------------------------------
_FORMAT_RE = re.compile(r"^[a-z0-9]{1,8}$")


def _source_from_key(key: str) -> str:
    """Derive the source from an S3 key (for rows written before the source
    attribute existed). recordings/{uid}/mobile/... -> MOBILE,
    recordings/{uid}/uploads/... -> UPLOAD, anything else -> DEVICE (both the
    user-owned device layout and the legacy {device_id}/{file} layout)."""
    parts = (key or "").split("/")
    if len(parts) == 4 and parts[0] == "recordings":
        if parts[2] == SOURCE_SEGMENTS[SOURCE_MOBILE]:
            return SOURCE_MOBILE
        if parts[2] == SOURCE_SEGMENTS[SOURCE_UPLOAD]:
            return SOURCE_UPLOAD
    return SOURCE_DEVICE


def _with_source(row: dict) -> dict:
    """Ensure the client-facing row always carries `source` (derived for
    legacy rows) and that non-device sources never leak a device_id."""
    source = row.get("source") or _source_from_key(row.get("audio_s3_key", ""))
    row["source"] = source
    if source != SOURCE_DEVICE:
        row["device_id"] = None
    return row


def _with_crm_records(row: dict, user_id: str) -> dict:
    """Normalize `crm_records` for the detail view.

    Returns one entry per CONFIGURED mapping, in configuration order, so the UI
    renders straight from this list without cross-referencing the config: a
    mapping with no identifier yet comes back as not_linked rather than absent.
    Stored entries for objects the user has since UNMAPPED are omitted from the
    list (nothing to render them against) but left untouched in DynamoDB, so
    re-adding the mapping restores the association.

    Only called on the single-recording routes: the list view would pay a
    config read per request for data it doesn't render.
    """
    mappings = _mappings_from_config(_user_crm_config(user_id))
    stored = row.get("crm_records") or {}
    out = {}
    for mapping in mappings:
        entry = stored.get(mapping["object"])
        out[mapping["object"]] = (_public_crm_record(entry, mapping)
                                 if isinstance(entry, dict) and entry
                                 else _empty_crm_record(mapping))
    row["crm_records"] = out
    return row


def _as_duration(value):
    """Client-supplied duration -> Decimal seconds (DynamoDB rejects floats).
    Returns None when absent/invalid rather than failing the upload."""
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(round(float(value), 2)))
    except (TypeError, ValueError, InvalidOperation):
        return None
    return d if d >= 0 else None


def request_upload(event):
    """POST /recordings/upload-request {source, format?, title?, duration?,
    size?} -> {upload_url, key, recording_id, expires_in, content_type}.

    Validates, mints the recording identity, presigns the S3 PUT and writes
    the ownership stub (status="uploading") — so the recording exists in the
    timeline before a single byte is uploaded, exactly like DEVICE presigns.
    """
    user_id = _require_auth(event)
    if not BUCKET_NAME:
        raise ApiError(500, "server misconfigured (no bucket)")
    # The workspace this meeting will belong to, VERIFIED against membership
    # before anything is presigned or written. Resolved here — before the S3
    # key is built — so a caller with no right to the requested workspace is
    # refused without a presigned URL ever being minted.
    workspace_id = _creation_workspace(event, user_id)
    data = _body(event)

    source = str(data.get("source") or "").strip().upper()
    if source not in USER_UPLOAD_SOURCES:
        raise ApiError(400, "source must be MOBILE or UPLOAD "
                            "(DEVICE recordings are presigned by the device API)")

    fmt = str(data.get("format") or "wav").strip().lower().lstrip(".")
    if not _FORMAT_RE.match(fmt) or fmt not in UPLOAD_FORMATS:
        raise ApiError(400, "unsupported format — use one of: "
                            + ", ".join(sorted(UPLOAD_FORMATS)))

    # Advisory pre-flight limits. The client validates before uploading; we
    # reject here too so a misbehaving client fails fast, before the PUT.
    size = data.get("size")
    if size is not None:
        try:
            size = int(size)
        except (TypeError, ValueError):
            raise ApiError(400, "size must be a number of bytes")
        if size > MAX_UPLOAD_BYTES:
            # Reported in GB to match the constant's own unit. Dividing by
            # 1024*1024 used to render the 2 GiB ceiling as "max 2048 MB",
            # which no user could reconcile with the "up to 2 GB" the upload
            # screen advertised.
            raise ApiError(413, f"file too large — max "
                                f"{MAX_UPLOAD_BYTES / 1_000_000_000:g} GB")
    duration = _as_duration(data.get("duration"))
    if duration is not None and duration > MAX_DURATION_SECONDS:
        raise ApiError(400, f"recording too long — max "
                            f"{MAX_DURATION_SECONDS // 3600} hours")

    title = (str(data.get("title") or "")).strip()[:200]

    # Optional folder, for a recording started from inside one. Validated
    # BEFORE anything is written or presigned: a bad folder id must fail the
    # request outright rather than produce an unfiled recording the user then
    # RE-PRESIGN of an upload already under way, when the client sends back the
    # key it was given. A presigned PUT is only valid for UPLOAD_URL_EXPIRY, so
    # a big file on a slow connection (or one resumed after the app was killed)
    # outlives its URL and must ask for another. Minting a fresh identity there
    # is what the client's "one session = one key" durability design is built
    # to avoid: the second key becomes a SECOND timeline row, and the first is
    # left at "uploading" forever — a phantom duplicate the user cannot clear
    # except by trashing it, because reprocess refuses that status.
    #
    # Only the caller's OWN still-uploading row can be re-presigned. An
    # unknown key, someone else's, or one that already reached "uploaded" or
    # beyond falls through to a new identity, so this can never overwrite a
    # finished recording or let a key be guessed onto another user's row.
    prior_key = str(data.get("key") or "").strip()
    if prior_key:
        prior = _recordings.get_item(Key={"audio_s3_key": prior_key}).get("Item")
        # _can_write_meeting rather than a bare creator test: a member removed
        # from the owning organisation must not be handed a fresh presigned
        # PUT for an upload they started there. Personal rows are unaffected.
        if (prior
                and _can_write_meeting(user_id, prior)
                and prior.get("status") == STATUS_UPLOADING):
            return _resp(200, {
                "upload_url": _s3.generate_presigned_url(
                    "put_object",
                    Params={"Bucket": BUCKET_NAME, "Key": prior_key},
                    ExpiresIn=UPLOAD_URL_EXPIRY,
                ),
                "key": prior_key,
                "recording_id": prior.get("recording_id", ""),
                "expires_in": UPLOAD_URL_EXPIRY,
                "content_type": UPLOAD_FORMATS[fmt],
            })

    # recording_id keeps the device convention "{meeting_id}_{timestamp}" so
    # the pipeline's one key parser works unchanged. e.g. mobile-3fa8c2_1754.
    prefix = "mobile" if source == SOURCE_MOBILE else "upload"
    meeting_id = f"{prefix}-{uuid.uuid4().hex[:10]}"
    recorded_at = str(int(time.time()))
    recording_id = f"{meeting_id}_{recorded_at}"
    segment = SOURCE_SEGMENTS[source]
    key = f"recordings/{user_id}/{segment}/{recording_id}.{fmt}"
    content_type = UPLOAD_FORMATS[fmt]

    # ContentType is deliberately NOT signed. Signing it forces the client to
    # send back a byte-identical Content-Type, and React Native's fetch()
    # OVERRIDES that header with the Blob's own type when the body is a Blob —
    # producing SignatureDoesNotMatch (403) on every phone upload. The
    # extension already encodes the format, and content_type is still returned
    # so clients that CAN control the header send a useful one.
    upload_url = _s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": BUCKET_NAME, "Key": key},
        ExpiresIn=UPLOAD_URL_EXPIRY,
    )

    # Ownership stub BEFORE the upload exists (mirrors getUploadUrl). The
    # transcribe Lambda upserts onto the same audio_s3_key row and never
    # removes these fields. if_not_exists keeps a re-request of the same id
    # (can't happen — uuid — but cheap) from resetting created_at/status.
    # device_id is deliberately ABSENT (not NULL): it is the device-index
    # GSI's hash key, and DynamoDB rejects NULL for an index key — a
    # non-device recording must simply not appear in that index. The API
    # still returns device_id: null (see _with_source).
    # `documents`/`tasks` are seeded as empty maps here so the AI workspace's
    # per-document writes AND task CRUD are single atomic nested SETs
    # (`SET documents.<type> = :doc`, `SET tasks.<id> = :task`). DynamoDB will
    # not auto-create a parent map, and there is no legal single expression
    # that creates it and sets a key at once, so seeding both at insert time
    # keeps the common path to one round trip. (_save_document/_save_task
    # still handle an absent map, for rows created before this existed.)
    sets = ("SET recording_id = :rid, user_id = :uid, "
            "#src = :src, meeting_id = :mid, recorded_at = :ts, s3_key = :key, "
            "#dur = if_not_exists(#dur, :dur), "
            "title = if_not_exists(title, :title), "
            "#st = if_not_exists(#st, :uploading), "
            "#docs = if_not_exists(#docs, :emptymap), "
            "#tasks = if_not_exists(#tasks, :emptymap), "
            "created_at = if_not_exists(created_at, :now)")
    names = {"#st": "status", "#dur": "duration",
             "#src": "source", "#docs": DOCUMENTS_ATTR,
             "#tasks": TASKS_ATTR}
    values = {
        ":rid": recording_id, ":uid": user_id,
        ":src": source, ":mid": meeting_id, ":ts": recorded_at,
        ":key": key, ":dur": duration, ":title": title,
        ":uploading": STATUS_UPLOADING, ":emptymap": {},
        ":now": _now_iso(),
    }
    # WORKSPACE OWNERSHIP (Phase 2B). Stamped ONCE, at creation, from a
    # membership that was verified by _creation_workspace — after this the row
    # is authoritative and no later request (or async event) re-reads the
    # client's header for this meeting.
    #
    # if_not_exists on BOTH fields: request_upload is re-entered when a client
    # re-presigns a still-uploading recording, and a second call must not be
    # able to move an existing meeting into a different workspace.
    sets += (", workspace_id = if_not_exists(workspace_id, :wsid)"
             ", created_by = if_not_exists(created_by, :cby)")
    values[":wsid"] = workspace_id
    values[":cby"] = user_id
    _recordings.update_item(
        Key={"audio_s3_key": key},
        UpdateExpression=sets,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )
    return _resp(200, {"upload_url": upload_url, "key": key,
                       "recording_id": recording_id,
                       "expires_in": UPLOAD_URL_EXPIRY,
                       "content_type": content_type})


def complete_upload(event):
    """POST /recordings/upload-complete {key, duration?} -> {key, status}.

    Marks the row "uploaded" once the client's PUT succeeded (mirrors the
    device's POST /device/upload-complete). Never moves the status backwards:
    if the S3 trigger already advanced it (transcribing/…/complete), only the
    duration is filled in.
    """
    user_id = _require_auth(event)
    data = _body(event)
    key = str(data.get("key") or "").strip()
    if not key:
        raise ApiError(400, "key required")

    item = _recordings.get_item(Key={"audio_s3_key": key}).get("Item")
    # 404 (not 403) for both missing and someone else's — don't leak keys.
    # _can_write_meeting rather than a bare creator test: a member removed
    # from the owning organisation must not be able to finalize an upload
    # into it, even one they started.
    if not item or not _can_write_meeting(user_id, item):
        raise ApiError(404, "recording not found")

    duration = _as_duration(data.get("duration"))
    names = {"#st": "status"}
    values = {":uploaded": STATUS_UPLOADED, ":uploading": STATUS_UPLOADING,
              ":now": _now_iso()}
    sets = ["#st = :uploaded", "upload_completed_at = :now"]
    if duration is not None:
        names["#dur"] = "duration"
        values[":dur"] = duration
        sets.append("#dur = :dur")
    try:
        _recordings.update_item(
            Key={"audio_s3_key": key},
            UpdateExpression="SET " + ", ".join(sets),
            ConditionExpression="#st = :uploading",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
        return _resp(200, {"key": key, "status": STATUS_UPLOADED})
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    # Status already advanced past "uploading" — keep it, backfill duration.
    if duration is not None:
        _recordings.update_item(
            Key={"audio_s3_key": key},
            UpdateExpression="SET #dur = :dur, upload_completed_at = :now",
            ExpressionAttributeNames={"#dur": "duration"},
            ExpressionAttributeValues={":dur": duration, ":now": _now_iso()},
        )
    current = _recordings.get_item(Key={"audio_s3_key": key}).get("Item") or {}
    return _resp(200, {"key": key, "status": current.get("status", "")})


def _query_all(table, **kwargs):
    """Every item matching a Query, following LastEvaluatedKey to the end.

    DynamoDB caps a Query response at 1 MB and then stops, handing back a
    LastEvaluatedKey instead of an error. A caller that ignores it silently
    sees a PREFIX of the matches and cannot tell — which for a list endpoint
    means a user's oldest rows quietly disappear from their own timeline. That
    ceiling is reached far sooner than the row count suggests here, because
    both Recordings GSIs project ALL: every item carries its full AI payload
    (documents, mom, chat_history) even when the caller only wants a handful
    of summary fields.

    Use for the whole-collection reads (a user's recordings, their trash).
    Cursor-paginated endpoints like /tasks page deliberately instead — this
    walks the entire partition, and that is only correct when the caller
    genuinely needs all of it.
    """
    items = []
    while True:
        res = table.query(**kwargs)
        items.extend(res.get("Items", []))
        last = res.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last


def _list_workspace_recordings(event, user_id, workspace_id):
    """The ORGANISATION half of list_recordings.

    Membership is resolved ONCE for the whole page rather than per row — a
    per-row membership read would turn one list into N authorization reads,
    which is the N+1 the brief's performance section rules out. The role it
    yields is then passed into every _can_read_meeting call.
    """
    membership = _active_membership(workspace_id, user_id)
    if not membership:
        # An outsider must not learn the workspace exists.
        raise ApiError(404, "workspace not found")
    role = str(membership.get("role") or "")
    # Resolved ONCE for the page, like the role. _owned_devices costs two
    # DynamoDB queries, so leaving _can_read_meeting to fetch it per row made
    # this 2N reads — the N+1 the performance requirement rules out (P2
    # remediation). Device ownership is a property of the CALLER, not of the
    # row, so hoisting it changes no decision.
    devices = _owned_devices(user_id)

    # Optional ?user_id= — "show me only Priya's meetings".
    #
    # Applied AFTER _can_read_meeting, never instead of it. That ordering is
    # the whole security property: a MEMBER passing someone else's id gets the
    # intersection of "their meetings" and "what I may read", which is their
    # own shared-with-me set — not a way to enumerate a colleague's meeting
    # count. Only the display shrinks; authorization is untouched.
    qs = event.get("queryStringParameters") or {}
    only_user = (qs.get("user_id") or "").strip()

    out = []
    for row in _query_all(_recordings, IndexName=RECORDINGS_WORKSPACE_INDEX,
                          KeyConditionExpression=Key(
                              "workspace_id").eq(workspace_id),
                          ScanIndexForward=False):
        if _is_trashed(row):
            continue
        # The index found the row; this decides whether the caller may see it.
        if not _can_read_meeting(user_id, row, role=role, devices=devices):
            continue
        if only_user and _recorded_by(row) != only_user:
            continue
        out.append(_with_source({k: row.get(k, "") for k in LIST_FIELDS}))

    out.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    _attach_recorder(out)
    return _resp(200, {"recordings": out, "count": len(out),
                       "workspace_id": workspace_id})


def _recorded_by(row):
    """WHO recorded a meeting.

    `created_by` is the workspace-era stamp (workspace_schema.stamp); `user_id`
    is what every row predating it carries. Preferring created_by and falling
    back keeps attribution correct across both without a backfill.
    """
    return (str((row or {}).get("created_by") or "").strip()
            or str((row or {}).get("user_id") or "").strip())


def _attach_recorder(rows):
    """Add recorded_by_name / recorded_by_avatar to a page of recordings.

    ONE BatchGetItem for the whole page, not a read per row — a 200-meeting
    org list would otherwise be 200 profile reads. Mutates in place.

    Only ever called on the ORGANISATION path: in a personal workspace every
    row is the caller's own, so an attribution column there would be the same
    name repeated down the page.
    """
    if not rows:
        return
    users = _users_by_ids([_recorded_by(r) for r in rows])
    for row in rows:
        who = _recorded_by(row)
        profile = users.get(who)
        row["recorded_by"] = who
        row["recorded_by_name"] = workspace_schema.display_name(
            profile, fallback_user_id="")
        # Presigned, not the raw key: the stored value is an S3 key and the
        # app renders a signed GET (see _public_user). Handing over the key
        # would render nothing.
        row["recorded_by_avatar"] = _avatar_view_url(
            str((profile or {}).get("avatar_url") or ""))


def _list_workspace_trash(event, user_id, workspace_id):
    """The ORGANISATION half of list_trash. Mirrors _list_workspace_recordings.

    Membership and device ownership are both resolved ONCE for the whole page
    and passed into every _can_read_meeting call, so this is 2 authorization
    reads per request rather than 2 per row.
    """
    membership = _active_membership(workspace_id, user_id)
    if not membership:
        raise ApiError(404, "workspace not found")
    role = str(membership.get("role") or "")
    devices = _owned_devices(user_id)

    out = []
    for row in _query_all(_recordings, IndexName=RECORDINGS_WORKSPACE_INDEX,
                          KeyConditionExpression=Key(
                              "workspace_id").eq(workspace_id),
                          ScanIndexForward=False):
        if not _is_trashed(row):
            continue
        if not _can_read_meeting(user_id, row, role=role, devices=devices):
            continue
        summary = _with_source({k: row.get(k, "") for k in LIST_FIELDS})
        summary["deleted_at"] = row.get("deleted_at", "")
        out.append(summary)

    out.sort(key=lambda r: (r.get("deleted_at", ""), r.get("created_at", "")),
             reverse=True)
    return _resp(200, {"recordings": out, "count": len(out),
                       "workspace_id": workspace_id})


def list_recordings(event):
    """Recordings visible in the ACTIVE workspace.

    PERSONAL (the default, and what every existing client gets): unchanged —
    user-index rows unioned with legacy device-index rows for owned devices,
    deduped. A row with no workspace_id is personal to its owner
    (_row_workspace_id), so nothing needs backfilling for this to be right.

    ORGANISATION: the workspace-index is queried instead, and each row is then
    put through _can_read_meeting — so an OWNER/MANAGER sees every meeting,
    while a MEMBER sees only their own plus those explicitly granted. The index
    is a lookup path; the predicate is the authorization.
    """
    user_id = _require_auth(event)
    requested = _requested_workspace_id(event)
    if requested and not workspace_schema.is_personal_workspace_id(requested):
        return _list_workspace_recordings(event, user_id, requested)
    devices = _owned_devices(user_id)

    # Optional ?device_id= filter (must be one the user owns).
    qs = event.get("queryStringParameters") or {}
    want = (qs.get("device_id") or "").strip()
    if want:
        if want not in devices:
            raise ApiError(403, "device not owned by user")
        devices = [want]

    seen = set()
    out = []

    def _collect(items):
        for item in items:
            key = item.get("audio_s3_key", "")
            if key in seen:
                continue
            # Trashed rows belong to GET /trash, not MinuteX. This is the ONLY
            # thing filtered here: _is_trashed tests one exact string, so every
            # other lifecycle — complete, failed, uploading, transcribing,
            # generating_ai, and legacy rows carrying no recording_status at
            # all — still lists exactly as it did before Trash existed.
            if _is_trashed(item):
                continue
            seen.add(key)
            # Every LIST_FIELD defaults to "" so the shape is uniform for the
            # app. For folder_id that means General reads as "" rather than a
            # missing key — deliberately: the client tests it for truthiness,
            # and one representation on the wire beats two. The STORED
            # attribute is still absent (see move_recording_to_folder), which
            # is what keeps the sparse folder-index correct.
            out.append(_with_source({k: item.get(k, "") for k in LIST_FIELDS}))

    # user-index: user_id HASH, created_at RANGE. Newest first. This is the
    # primary (user-owned) path; it also covers recordings stamped to the
    # user by a past unpair, whose device is no longer in `devices`.
    #
    # PAGINATED, not a single query: DynamoDB caps a Query response at 1 MB,
    # and BOTH indexes project ALL — so each item carries its full AI payload
    # (documents, mom, chat_history, crm_records), not just the LIST_FIELDS
    # this route returns. Without the LastEvaluatedKey loop a heavy account
    # silently loses its OLDEST recordings off the end of the first page, with
    # no error anywhere: the response still looks like a complete list. Matches
    # the loop _recordings_in_folder already runs over this same index.
    items = []
    for row in _query_all(_recordings, IndexName=USER_INDEX,
                          KeyConditionExpression=Key("user_id").eq(user_id),
                          ScanIndexForward=False):
        if want and row.get("device_id") != want:
            continue
        items.append(row)
    _collect(items)

    for dev in devices:
        # device-index: device_id HASH, created_at RANGE. Newest first.
        # Legacy rows (uploaded before user ownership) have no user_id and
        # are only reachable this way.
        _collect(_query_all(_recordings, IndexName=DEVICE_INDEX,
                            KeyConditionExpression=Key("device_id").eq(dev),
                            ScanIndexForward=False))
    # Merge across sources, newest first by created_at.
    out.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return _resp(200, {"recordings": out, "count": len(out)})


# Speaker naming. Diarization only separates VOICES ("0", "1", …) — the audio
# carries no names — so the mapping from label to human name is supplied by the
# user and stored alongside the recording, NOT baked into the transcript. That
# keeps the transcript the verbatim record, lets a name be corrected at any
# time, and works on recordings that were transcribed long before this existed.
MAX_SPEAKER_NAME = 60
MAX_SPEAKERS = 32


def _clean_speaker_names(raw):
    """Validate a {label: name} map from the client.

    Labels are the diarization labels already in the transcript ("0", "1",
    "agent"). An empty/blank name REMOVES the mapping, so the UI can clear a
    name by sending "" rather than needing a separate delete route.
    """
    if not isinstance(raw, dict):
        raise ApiError(400, "speaker_names must be an object of {label: name}")
    if len(raw) > MAX_SPEAKERS:
        raise ApiError(400, f"too many speakers (max {MAX_SPEAKERS})")
    out = {}
    for label, name in raw.items():
        label = str(label).strip()
        if not label or len(label) > MAX_SPEAKER_NAME:
            raise ApiError(400, "invalid speaker label")
        if name is None:
            continue
        name = str(name).strip()[:MAX_SPEAKER_NAME]
        if name:                      # blank clears the mapping
            # CANONICAL KEYS ONLY. Every reader normalizes its lookup to the
            # compact id ("0"), so a client sending the display form
            # {"Speaker 0": "Rahul"} used to write a key nothing could ever
            # find — the name was stored and invisible. Normalizing on the
            # WRITE side keeps one spelling in the table without changing
            # what any existing reader does. A non-numeric diarization label
            # ("agent") normalizes to itself and is unaffected.
            out[speaker_identity.normalize_speaker_id(label) or label] = name
    return out


def _user_crm_config(user_id: str) -> dict:
    """The user's stored CRM config, or {} when Salesforce isn't connected."""
    conn = _get_salesforce_connection(user_id)
    return (conn or {}).get("config") or {}


def _legacy_crm_object_for(user_id: str) -> str:
    """Which object an old `site_visit_number` PATCH refers to.

    The pre-mappings API had no object in the request because there was only
    ever one. To keep those callers working, resolve it to the user's FIRST
    configured mapping — which for an upgraded user is exactly the object their
    old single-object config became. Returns "" when nothing is configured, and
    the caller reports that rather than inventing an object name.
    """
    mappings = _mappings_from_config(_user_crm_config(user_id))
    return mappings[0]["object"] if mappings else ""


def patch_recording(event):
    """PATCH /recordings/{key+} {speaker_names?, title?, site_visit_number?}.

    The only user-editable fields on a recording. Ownership is enforced the
    same way as get_recording — a miss is reported as 404, never 403, so the
    endpoint can't be used to probe for other users' recordings.

    site_visit_number is the CRM manual-entry path: null or "" clears it.
    """
    user_id = _require_auth(event)
    key = _url_unquote((event.get("pathParameters") or {}).get("key", ""))
    if not key:
        raise ApiError(400, "recording key required")

    item = _recordings.get_item(Key={"audio_s3_key": key}).get("Item")
    if not item:
        raise ApiError(404, "recording not found")
    # A metadata WRITE (title, speaker names, site-visit number), so the
    # write predicate — which also denies a member removed from the owning
    # organisation.
    if not _can_write_meeting(user_id, item):
        raise ApiError(404, "recording not found")

    data = _body(event)
    sets, names, values = [], {}, {}

    if "speaker_names" in data:
        cleaned = _clean_speaker_names(data.get("speaker_names") or {})
        sets.append("speaker_names = :sn")
        values[":sn"] = cleaned
        # Bump the version only when the map actually changes — a no-op rename
        # (same name resent, or clearing an already-absent label) must not
        # invalidate every generated document over nothing. Documents compare
        # their own stamped version against this counter (see
        # _needs_speaker_update) to know whether a rename happened since they
        # were generated, without storing a redundant status on each of them.
        if cleaned != (item.get("speaker_names") or {}):
            sets.append("speaker_mapping_version = if_not_exists(speaker_mapping_version, :zero) + :one")
            values[":zero"] = 0
            values[":one"] = 1

    if "title" in data:
        title = str(data.get("title") or "").strip()[:200]
        sets.append("title = :t")
        values[":t"] = title

    # CRM record identifiers — the manual-entry / correction path. The AI
    # extraction in transcribeRecording finds an identifier when it was spoken;
    # this is how the user supplies one that wasn't, or fixes a wrong one.
    #
    # Written into the generic `crm_records` map keyed by Salesforce object, so
    # one code path serves every configured object. A human typing the value IS
    # the authority (confidence "manual", no evidence to ground), which is why
    # it always outranks an extraction.
    #
    #   {"crm_records": {"Lead": "a@b.com", "SiteVisit__c": null}}
    #
    # `site_visit_number` is still accepted so an app build from before this
    # change keeps working; it is normalized into the same map. Reading it
    # requires knowing which object the user mapped it to, which only the
    # config knows — hence the lookup below.
    if "crm_records" in data or "site_visit_number" in data:
        requested = data.get("crm_records")
        if requested is None:
            requested = {}
        if not isinstance(requested, dict):
            raise ApiError(400, "crm_records must be an object keyed by "
                                "Salesforce object name")
        if "site_visit_number" in data:
            legacy_object = _legacy_crm_object_for(user_id)
            if not legacy_object:
                raise ApiError(400, "no Salesforce object is configured for "
                                    "this — set up Salesforce mapping first")
            requested = {**requested, legacy_object: data.get("site_visit_number")}

        mappings_by_object = {m["object"]: m for m
                              in _mappings_from_config(_user_crm_config(user_id))}
        stored = dict((item or {}).get("crm_records") or {})
        for object_name, raw in requested.items():
            key_name = str(object_name).strip()
            if not key_name:
                raise ApiError(400, "crm_records keys must be object names")
            mapping = mappings_by_object.get(key_name) or {"object": key_name}

            # A bare string is the identifier; an object may additionally carry
            # a resolved record_id/status (the app sends that after a lookup, so
            # confirming does not need a second round trip).
            if isinstance(raw, dict):
                value = str(raw.get("lookup_value") or "").strip()[:CRM_IDENTIFIER_MAX]
                record_id = str(raw.get("record_id") or "").strip()[:64]
                requested_status = str(raw.get("status") or "").strip()
            else:
                value = ("" if raw is None
                         else str(raw).strip()[:CRM_IDENTIFIER_MAX])
                record_id, requested_status = "", ""

            if not value:
                # Explicit null/"" removes it — the user saying "this meeting
                # isn't about one of those", which must be able to undo a bad
                # extraction. Dropped from the map rather than stored as null so
                # "absent" has exactly one representation.
                stored.pop(key_name, None)
                continue

            prior = stored.get(key_name) if isinstance(stored.get(key_name), dict) else {}
            prior_value = str(prior.get("lookup_value") or prior.get("value") or "")
            # Changing the identifier invalidates any record resolved from the
            # OLD one — keeping the stale record_id would push this meeting's
            # notes onto the previous record.
            if value != prior_value:
                record_id = record_id or ""
                prior = {}

            status = requested_status if requested_status in CRM_STATUSES else ""
            if not status:
                status = (CRM_STATUS_RECORD_FOUND if record_id
                          else CRM_STATUS_LOOKUP_PENDING)
            # Confirmation is the user's act, and it is what gates the push.
            # A client asking to jump straight to synced/syncing is refused:
            # only the push route may write those.
            if status in (CRM_STATUS_SYNCING, CRM_STATUS_SYNCED):
                raise ApiError(400, "sync status is set by the sync itself")
            if status in (CRM_STATUS_CONFIRMED, CRM_STATUS_RECORD_FOUND) and not record_id:
                raise ApiError(400, f"{key_name}: a record_id is required to "
                                    f"mark it {status}")

            entry = {
                "object": key_name,
                "label": mapping.get("object_label") or key_name,
                "lookup_field": mapping.get("lookup_field") or
                                prior.get("lookup_field") or "",
                "lookup_value": value,
                "record_id": record_id,
                "record_label": str(raw.get("record_label") or "")[:255]
                                if isinstance(raw, dict) else "",
                "status": status,
                # Manual entry always outranks an extraction — a human typing
                # the identifier IS the authority.
                "source": "manual",
                "updated_at": _now_iso(),
            }
            stored[key_name] = entry
        sets.append("crm_records = :cr")
        values[":cr"] = stored

    if not sets:
        raise ApiError(400, "nothing to update — send speaker_names, title "
                            "and/or crm_records")

    _recordings.update_item(
        Key={"audio_s3_key": key},
        UpdateExpression="SET " + ", ".join(sets),
        **({"ExpressionAttributeNames": names} if names else {}),
        ExpressionAttributeValues=values,
    )
    updated = _recordings.get_item(Key={"audio_s3_key": key}).get("Item") or {}
    # Hydrated like get_recording: the app ADOPTS this response as its current
    # recording (see updateRecording in lib/api.ts), and its Transcript tab
    # renders from `timestamps`. Returning the bare row would blank that tab the
    # moment a user renamed a speaker — which is precisely the screen they are
    # looking at when they do it.
    updated = transcript_store.hydrate(_s3, BUCKET_NAME, updated)
    return _resp(200, {"recording": _with_crm_records(_with_source(updated), user_id)})


# ---------------------------------------------------------------------------
# Trash — soft delete, restore, permanent delete
#
# Delete is TWO STEPS, not one. DELETE /recordings/{key+} moves a recording to
# Trash; only DELETE /recordings/permanent/{key+} actually destroys anything.
# The user gets an undo for the common case (a mis-tap, a brief deleted in a
# tidying spree) and still has a way to really be rid of something.
#
# SOFT DELETE IS A FLAG ON THE EXISTING ROW — never a copy, never a second
# table. `recording_status = "trashed"` plus a `deleted_at` stamp, written with
# update_item onto the row that is already there. Everything the recording owns
# (transcript, documents, tasks, chat, highlights, speaker names, crm_records)
# is an attribute ON that row, so trashing touches none of it and restoring
# brings all of it back intact. Copying the row into a "trash table" instead
# would duplicate every AI artifact and give us two rows that could drift.
#
# WHY A NEW ATTRIBUTE RATHER THAN status = "trashed": `status` is the PIPELINE's
# field (uploading -> transcribing -> generating_ai -> complete/failed) and the
# transcription Lambda writes it without asking anyone. Overloading it would
# mean a webhook landing after a trash could quietly un-trash the recording, and
# would also destroy the information needed to restore the row to what it was.
# `recording_status` is a separate axis — lifecycle, not progress — owned
# exclusively by this file. The two never race.
#
# BACKWARD COMPATIBILITY: every row written before this existed has no
# `recording_status` at all. MISSING MEANS ACTIVE — see _is_trashed. No
# backfill is needed and no existing recording changes behaviour.
#
# RETENTION: `deleted_at` is stamped so a "Trash for 30 days" sweep can be added
# later without another migration. Nothing expires automatically today; a
# recording stays in Trash until the user acts on it. Deliberate — a retention
# job that deletes user data is not something to ship as a side effect of
# adding a Trash screen.
# ---------------------------------------------------------------------------

# The lifecycle axis, kept separate from the pipeline's `status` (see above).
RECORDING_STATUS_ATTR = "recording_status"
RECORDING_TRASHED = "trashed"
RECORDING_ACTIVE = "active"


def _is_trashed(item):
    """True if this row is in Trash.

    Missing/blank `recording_status` reads as ACTIVE, which is what makes
    every pre-Trash recording keep working with no backfill. Only the exact
    string "trashed" hides a recording.
    """
    return (item.get(RECORDING_STATUS_ATTR) or "").strip() == RECORDING_TRASHED


# Statuses during which the PIPELINE may still write to this row — the S3
# trigger's bytes are landing and transcribeRecording is about to fire.
#
# Permanent delete is refused here: transcribeRecording writes with
# update_item, which would RESURRECT a row deleted under it as a fragment with
# no user_id and no audio — a ghost that lists but never opens.
#
# Trashing during these states is FINE, and deliberately allowed: setting a
# flag on a live row races nothing, the pipeline keeps writing `status` on its
# own axis, and the user who just uploaded a wrong file shouldn't have to wait
# out a transcription before removing it. The mid-pipeline states
# ("transcribing"/"generating_ai") are absent from this set on purpose — a
# recording stuck there for an hour is the one a user most wants gone, and the
# reprocess route already treats those as recoverable-or-dead.
DELETE_BLOCKING_STATUSES = frozenset({"uploading", "uploaded"})


def delete_recording(event):
    """DELETE /recordings/{key+} -> {trashed, key, deleted_at}.

    Moves the recording to Trash. NOTHING is destroyed: no S3 object is
    touched and the DynamoDB row stays exactly where it is, with every AI
    artifact still attached. POST .../restore puts it back.

    Ownership is enforced as get_recording does — someone else's recording
    answers 404, never 403, so this can't be used to probe for other users'
    keys.
    """
    _, key, item = _owned_recording(event, hydrate=False)

    # Already in Trash: report success rather than 409. The client may be
    # retrying a request whose response was lost, and "it is in Trash" is
    # exactly the state the caller asked for either way.
    if _is_trashed(item):
        return _resp(200, {"trashed": True, "key": key,
                           "deleted_at": item.get("deleted_at", "")})

    deleted_at = _now_iso()
    # The PIPELINE's `status` is deliberately left alone. It records what the
    # transcription actually achieved, and restore needs it intact to put the
    # recording back the way the user had it.
    _recordings.update_item(
        Key={"audio_s3_key": key},
        UpdateExpression="SET #rs = :trashed, deleted_at = :t",
        ExpressionAttributeNames={"#rs": RECORDING_STATUS_ATTR},
        ExpressionAttributeValues={":trashed": RECORDING_TRASHED,
                                   ":t": deleted_at},
    )
    print(f"[trash] {key} moved to trash (status '{item.get('status')}')")
    return _resp(200, {"trashed": True, "key": key, "deleted_at": deleted_at})


def restore_recording(event):
    """POST /recordings/restore/{key+} -> {restored, key, status}.

    Takes the recording out of Trash and returns it to MinuteX with its
    transcript and every AI artifact exactly as they were — nothing is
    re-transcribed and no AI call is made, because nothing was ever removed.

    The recording keeps the pipeline `status` it had when it was trashed, so a
    completed meeting comes back complete and a failed one comes back failed
    (still offering Try again). That is why delete_recording leaves `status`
    untouched: there is no "previous status" to guess at, because the real one
    was never overwritten.
    """
    _, key, item = _owned_recording(event, hydrate=False)

    if not _is_trashed(item):
        raise ApiError(409, "this recording isn't in Trash")

    # A row trashed mid-upload could come back still claiming "uploading"
    # forever if its trigger never fired while it sat in Trash. Restoring it
    # as "failed" puts it in a state the app can actually act on — the detail
    # screen offers Try again — instead of a spinner that never resolves.
    # Every other status is restored verbatim.
    status = (item.get("status") or "").strip()
    restored_status = "failed" if status in DELETE_BLOCKING_STATUSES else status

    names = {"#rs": RECORDING_STATUS_ATTR}
    values = {":active": RECORDING_ACTIVE}
    expr = "SET #rs = :active"
    if restored_status != status:
        names["#s"] = "status"
        values[":s"] = restored_status
        expr += ", #s = :s"
    # REMOVE, not SET-to-empty: an absent deleted_at is how a row that was
    # never trashed looks, and restore should leave no trace of the trip.
    expr += " REMOVE deleted_at"

    _recordings.update_item(
        Key={"audio_s3_key": key},
        UpdateExpression=expr,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )
    print(f"[trash] {key} restored (status '{restored_status}')")
    return _resp(200, {"restored": True, "key": key, "status": restored_status})


def list_trash(event):
    """GET /trash -> {recordings:[summary,...], count}.

    The Trash twin of list_recordings: the same two-index union (user-index
    plus legacy device-index), the same dedupe, the same lightweight LIST_FIELDS
    projection — inverted to keep ONLY trashed rows, and carrying deleted_at so
    the screen can show when each one was removed.

    Sorted by deleted_at descending: in Trash the question is "what did I just
    delete", not "when was this recorded", so the most recently binned row
    leads. created_at breaks ties for rows trashed in the same instant (a bulk
    delete) and covers legacy rows with no stamp.
    """
    user_id = _require_auth(event)
    # ORGANISATION Trash (P1 remediation). Without this an organisation
    # OWNER/MANAGER could read a workspace meeting through GET /recordings but
    # got an empty Trash for the same workspace — and a removed member's own
    # trashed rows stayed visible to them. Both are now decided by the ONE
    # predicate, with membership resolved once for the whole page.
    requested = _requested_workspace_id(event)
    if requested and not workspace_schema.is_personal_workspace_id(requested):
        return _list_workspace_trash(event, user_id, requested)

    devices = _owned_devices(user_id)

    seen = set()
    out = []

    def _collect(items):
        for item in items:
            key = item.get("audio_s3_key", "")
            if key in seen or not _is_trashed(item):
                continue
            # PERSONAL rows only reach here (the organisation branch returned
            # above), but a row may have been MOVED into an organisation since
            # it was trashed, so it is still checked rather than assumed.
            if not _can_read_meeting(user_id, item, devices=devices):
                continue
            seen.add(key)
            row = _with_source({k: item.get(k, "") for k in LIST_FIELDS})
            row["deleted_at"] = item.get("deleted_at", "")
            out.append(row)

    # Paginated for the same reason list_recordings is: a 1 MB Query page of
    # ALL-projected rows would silently drop the oldest trashed recordings,
    # and a user who cannot see a row in Trash cannot restore it.
    _collect(_query_all(_recordings, IndexName=USER_INDEX,
                        KeyConditionExpression=Key("user_id").eq(user_id),
                        ScanIndexForward=False))

    for dev in devices:
        _collect(_query_all(_recordings, IndexName=DEVICE_INDEX,
                            KeyConditionExpression=Key("device_id").eq(dev),
                            ScanIndexForward=False))

    out.sort(key=lambda r: (r.get("deleted_at", ""), r.get("created_at", "")),
             reverse=True)
    return _resp(200, {"recordings": out, "count": len(out)})


def permanently_delete_recording(event):
    """DELETE /recordings/permanent/{key+} -> {deleted, key}.

    The ONLY route in this file that destroys data. Removes:

      * the audio object in S3, at the key that IS the primary key;
      * the transcript object, at transcript_store.s3_key_for(key);
      * the PageIndex tree, at pageindex_store.s3_key_for(key) — its pointer
        lives on the row and dies with it, so leaving the object would orphan
        it exactly as an un-deleted transcript would;
      * the DynamoDB row — and with it EVERY AI artifact, because documents,
        tasks, chat history, highlights, speaker names and crm_records are all
        attributes ON that row rather than separate tables. One delete_item
        takes them all, so nothing can be orphaned.

    ORDER: S3 first, DynamoDB last. If this dies halfway the row survives, so
    the user can simply tap Delete permanently again and the operation
    converges. The reverse order would strand the S3 objects with nothing
    pointing at them: invisible in the app, still billed, findable only by
    diffing the bucket against the table.

    The S3 deletes are BEST-EFFORT for the same reason. S3 DELETE is idempotent
    (removing an absent key is a 204, not an error), so the only way they fail
    is a genuine S3/permission problem — and letting that block the row delete
    would leave the user staring at a recording they explicitly asked to be rid
    of. A leaked object is a billing footnote; a delete that doesn't delete is
    a broken product.

    NOT gated on the recording being in Trash. The app always routes through
    Trash, but a caller that knows what it wants shouldn't be forced through a
    two-step dance — and gating would make recovery from a half-finished
    restore harder, not safer.
    """
    _, key, item = _owned_recording(event, hydrate=False)

    # See DELETE_BLOCKING_STATUSES: transcribeRecording's update_item would
    # resurrect the row as a ghost fragment if it landed after the delete.
    status = (item.get("status") or "").strip()
    if status in DELETE_BLOCKING_STATUSES:
        raise ApiError(409, "this recording is still being processed — "
                            "try again in a minute")

    if BUCKET_NAME:
        # Both objects, both best-effort, each isolated: a missing transcript
        # (a recording that never got past upload has none) must not stop the
        # audio delete.
        for what, s3_key in (("audio", key),
                             ("transcript", transcript_store.s3_key_for(key)),
                             ("pageindex", pageindex_store.s3_key_for(key))):
            try:
                _s3.delete_object(Bucket=BUCKET_NAME, Key=s3_key)
            except Exception as err:  # noqa: BLE001 — see the docstring
                print(f"[delete] {what} object {s3_key} not removed: {err}")

    _recordings.delete_item(Key={"audio_s3_key": key})
    print(f"[delete] {key} permanently removed (was '{status}')")
    return _resp(200, {"deleted": True, "key": key})


def get_recording(event):
    user_id = _require_auth(event)
    # Greedy {key+} path var: value is the full remaining path with real
    # slashes (e.g. "esp32-001/Meeting.wav"), already URL-decoded by API GW.
    # A client that double-encodes (%252F) still decodes cleanly here.
    key = (event.get("pathParameters") or {}).get("key", "")
    key = _url_unquote(key)
    if not key:
        raise ApiError(400, "recording key required")

    item = _recordings.get_item(Key={"audio_s3_key": key}).get("Item")
    if not item:
        raise ApiError(404, "recording not found")
    # Ownership check: the recording is stamped with the user's user_id
    # (user-owned uploads / unpair backfill), or — legacy rows only — its
    # device is one the user currently owns. Since Phase 2B this also admits
    # an ORGANISATION reader (OWNER/MANAGER, or an explicit grant) via
    # _can_read_meeting, which returns "" for everyone else.
    if not _can_read_meeting(user_id, item):
        # Do not leak existence of recordings owned by others.
        raise ApiError(404, "recording not found")

    # Playback: a short-lived presigned GET so the app can stream the .wav
    # directly from S3 without ever holding real AWS credentials. Best-effort
    # — a bucket/config problem here shouldn't break the rest of the detail
    # view (transcript/summary still load fine without playback).
    audio_url = None
    if BUCKET_NAME:
        try:
            audio_url = _s3.generate_presigned_url(
                "get_object",
                Params={"Bucket": BUCKET_NAME, "Key": key},
                ExpiresIn=AUDIO_URL_EXPIRY,
            )
        except Exception:
            audio_url = None

    # The transcript + timestamps now live in S3 (see transcript_store), so they
    # are merged back in here under their ORIGINAL field names. The response
    # stays byte-identical to what the app already parses — its Transcript tab
    # renders from `timestamps`, so this is what keeps that tab working without
    # any app-side change or new build. Legacy rows still holding the values
    # inline pass through untouched.
    item = transcript_store.hydrate(_s3, BUCKET_NAME, item)
    # The dynamic overview is sanitized ON READ as well as on write.
    # ai_schema._overview_section cleans every overview generated from now
    # on, but rows analysed BEFORE that fix already carry a leaked seg_N in
    # their prose, and this is the screen that renders them. Doing it here
    # fixes the back catalogue without a migration or a re-analysis.
    if isinstance(item.get("overview"), dict):
        item = {**item, "overview": ai_sanitize.sanitize_overview(
            item["overview"], label=f"overview:{key}")}
    return _resp(200, {"recording": _with_crm_records(
        _with_source({**item, "audio_url": audio_url}), user_id)})


def _url_unquote(s):
    # Path params arrive URL-encoded; keys contain '/' and may be double-encoded.
    from urllib.parse import unquote
    prev = None
    cur = s
    # Decode until stable (handles %252F -> %2F -> /).
    while cur != prev:
        prev = cur
        cur = unquote(cur)
    return cur


# ===========================================================================
# AI MEETING WORKSPACE — on-demand generation (documents, Quick AI, chat).
#
# Everything below is the JWT-authenticated half of the AI pipeline. The staged
# half (transcript -> executive summary -> meeting highlights) runs in the
# S3-triggered transcribeRecording Lambda; these routes cover what the user
# asks for interactively, from the workspace screen.
#
# The Groq client, the prompt templates and the coercion layer are IMPORTED
# from lambda-shared/ — the exact modules transcribeRecording uses. There is no
# second Groq integration and no duplicated prompt anywhere in this file: a
# document type is one entry in prompts.DOCUMENTS, and adding one needs no code
# here at all.
#
# Routes (action first, recording key LAST — API Gateway rejects a greedy
# {key+} in any but the final position, so /recordings/{key+}/chat cannot exist):
#   GET    /recordings/ai/documents/{key+}                     -> {documents, available}
#   POST   /recordings/ai/documents/{key+}  {type, regenerate?} -> {document, cached}
#   PATCH  /recordings/ai/documents/{key+}  {type, content}     -> {document}
#   POST   /recordings/ai/update-documents/{key+} {}            -> {updated, remaining}
#   POST   /recordings/ai/quick/{key+}      {action, regenerate?} -> {document, cached}
#   POST   /recordings/ai/highlights/{key+} {regenerate?}      -> {meeting_highlights}
#   GET    /recordings/ai/chat/{key+}                          -> {chat_history, suggestions}
#   POST   /recordings/ai/chat/{key+}       {message, history?} -> {reply, chat_history}
#   DELETE /recordings/ai/chat/{key+}                          -> {cleared}
#
# CACHING (spec section 10) — the rule is "transcript unchanged AND document
# exists -> return the cached version; only regenerate when the user asks".
# Implemented by storing each document under a fingerprint of the transcript it
# was generated FROM (ai_schema.fingerprint). A stored document whose
# fingerprint no longer matches the recording's current transcript is stale and
# is regenerated transparently. That's why the cache can't key on a timestamp:
# reprocessing rewrites updated_at even when the transcript is byte-identical,
# which would throw away perfectly good documents.
# ===========================================================================

# Documents live in ONE map attribute on the recording row rather than a
# separate table: they are always read with the recording, never queried
# independently, and a map keeps generation atomic (one UpdateItem) without a
# transaction. DynamoDB's 400KB item ceiling is the constraint to respect —
# hence MAX_DOCUMENT_CHARS below and the transcript budget that bounds inputs.
DOCUMENTS_ATTR = "documents"
CHAT_ATTR = "chat_history"

# Tasks: same reasoning as documents — a map keyed by task id, nested SET/
# REMOVE for atomic writes, always read with the recording. See the "Tasks"
# section further down for the full CRUD surface (create/update/delete,
# assign, record-notification).
TASKS_ATTR = "tasks"

# A generated document is prose for a human to read; anything longer than this
# is a runaway model, not a document. Also keeps the row well inside the 400KB
# item limit once eight document types and a chat history coexist on it.
MAX_DOCUMENT_CHARS = 24_000

# Chat history retention. Kept ON the recording row (same reasoning as
# documents) and capped so a long-running conversation can't grow the item
# without bound. The cap is on stored TURNS; the per-request context window is
# bounded separately by CHAT_HISTORY_TURNS below.
MAX_CHAT_TURNS = 40

# How many prior turns are sent back to Groq as conversation context. Small on
# purpose: the meeting content is the expensive part of the prompt and the TPM
# quota is shared with every other caller, so history gets the smaller share.
# "Do not resend unnecessary data" (spec section 6) is a rate-limit
# requirement here, not a nicety.
CHAT_HISTORY_TURNS = 6

MAX_CHAT_MESSAGE_CHARS = 2_000

# Transcript budget for on-demand calls, in CHARACTERS.
#
# Unlike the staged pipeline, these routes answer a user who is waiting, so
# map-reducing a 90-minute transcript across many paced Groq calls is the wrong
# trade — it would blow the API Gateway timeout. Instead ONE call gets the
# analysis (always) plus as much transcript as the TPM window allows, and
# prompts.build_context() labels the truncation so the model knows its record
# is partial and says so rather than inventing the rest.
#
# Derived from the shared TPM budget so raising GROQ_TPM_LIMIT after a plan
# upgrade widens this automatically, with no second constant to remember.
def _transcript_budget_chars(system_prompt, reserve_tokens=1200):
    """Characters of transcript that fit one TPM window alongside `system_prompt`.

    reserve_tokens leaves room for the analysis digest and the model's own
    reply, both of which ride in the same window as the transcript.
    """
    budget_tokens = groq_client.chunk_budget(system_prompt) - reserve_tokens
    return max(2000, int(budget_tokens * groq_client.CHARS_PER_TOKEN))


# ---------------------------------------------------------------------------
# Ownership. Every AI route resolves the recording through this ONE function,
# so none of them can accidentally skip the check.
#
# A miss is reported as 404, never 403 — identical to get_recording/
# patch_recording — so these endpoints cannot be used to probe for the
# existence of other users' recordings.
# ---------------------------------------------------------------------------
def _owned_recording(event, hydrate=True):
    """(user_id, key, item) for the {key+} in the path, or ApiError.

    `hydrate=False` skips the S3 fetch for the transcript — pass it on routes
    that never read the transcript text (they only need the fingerprint, via
    _row_fingerprint) so they stay a single DynamoDB read.

    The key is read through _url_unquote for the same reason get_recording does:
    the app sends encodeURIComponent(key), so the slashes arrive as %2F, and API
    Gateway decodes path parameters once before the Lambda sees them. Decoding
    until stable also absorbs a client that double-encodes (%252F -> %2F -> /).
    """
    user_id = _require_auth(event)
    key = _url_unquote((event.get("pathParameters") or {}).get("key", ""))
    if not key:
        raise ApiError(400, "recording key required")

    item = _recordings.get_item(Key={"audio_s3_key": key}).get("Item")
    if not item:
        raise ApiError(404, "recording not found")
    # WRITE-CAPABLE PREDICATE — deliberately NARROWER than _can_read_meeting.
    #
    # This helper gates roughly thirty routes, most of which MUTATE the
    # meeting (generate a document, edit the MoM, create a task, remap a
    # speaker, reprocess, delete). Read visibility and write authority are
    # different questions, and widening this to everyone who may READ an
    # organisation meeting would silently hand a MANAGER edit rights over a
    # colleague's recording — a permission nobody asked for.
    #
    # _can_write_meeting keeps that narrowness (creator/device only) AND adds
    # the organisation membership gate, so a REMOVED member can no longer
    # mutate organisation meetings they created. Personal rows are unaffected:
    # they never reach the gate.
    if not _can_write_meeting(user_id, item):
        raise ApiError(404, "recording not found")
    # Hydrated AFTER the ownership check, never before: an unauthorized caller
    # must not be able to make us spend an S3 GET on someone else's transcript.
    # Every AI route below reaches the transcript through this one call, so the
    # S3-vs-inline split is invisible to all of them.
    if hydrate:
        item = transcript_store.hydrate(_s3, BUCKET_NAME, item)
    return user_id, key, item


def _require_transcript(item):
    """The transcript, or a 409 explaining that AI needs one.

    409 rather than 400: the request is well-formed and will succeed later —
    the recording just hasn't finished transcribing. The app uses this to keep
    showing its progress UI instead of surfacing an error.
    """
    transcript = (item.get("transcript") or "").strip()
    if not transcript:
        status = item.get("status") or "unknown"
        if status == "failed":
            raise ApiError(409, "this recording has no usable transcript, so "
                                "AI output can't be generated")
        raise ApiError(409, f"transcript not ready yet (status: {status})")
    return transcript


def _groq_error(err, what):
    """Map a GroqError onto the HTTP status the app should see.

    502 for a retryable upstream failure (the app shows "Unable to generate AI
    output. Retry"), 500 for a configuration error that retrying won't fix. The
    transcript is never touched either way — spec section 13.
    """
    print(f"[ai] {what} failed: {err}")
    if isinstance(err, groq_client.GroqError) and err.retryable:
        raise ApiError(502, f"Unable to generate {what}. Please retry.")
    raise ApiError(500, f"Unable to generate {what}.")


# ---------------------------------------------------------------------------
# Document storage + cache
# ---------------------------------------------------------------------------
def _stored_documents(item):
    docs = item.get(DOCUMENTS_ATTR)
    return docs if isinstance(docs, dict) else {}


def _public_document(doc_type, doc, speaker_mapping_version=0):
    """One stored document in API shape.

    label prefers a user-set/custom-generated label stored ON the document
    over the static per-type template label, so Rename (which writes
    doc["label"]) and freeform generation (which has no template label at
    all) both work the same way as the 8 fixed types.

    `speaker_mapping_version` is the RECORDING's current counter (see
    patch_recording), passed in by every caller so `status` can be computed
    here rather than stored redundantly on the document itself — storing it
    would mean rewriting every document on every rename, one nested SET each,
    which defeats the whole point of _save_document's nested-path design.
    """
    label = doc.get("label") or \
        (prompts.DOCUMENTS.get(doc_type) or {}).get("label", doc_type)
    return {
        "type": doc_type,
        "label": label,
        "content": doc.get("content", ""),
        "format": doc.get("format", "markdown"),
        "generated_at": doc.get("generated_at", ""),
        "edited": bool(doc.get("edited")),
        "ai_version": doc.get("ai_version", ""),
        # True for a freeform request — the app uses this to hide
        # "Regenerate" (there is no fixed prompt/type to regenerate against,
        # same reasoning it already applies to chat-drafted documents).
        "is_custom": doc.get("type_kind") == "custom",
        "speaker_mapping_version": doc.get("speaker_mapping_version", 0),
        "status": "needs_update" if _needs_speaker_update(doc, speaker_mapping_version) else "current",
    }


def _row_fingerprint(item):
    """The recording's current transcript fingerprint, WITHOUT reading S3.

    Routes that only compare cache identity (document listing, an edit's
    provenance stamp) need the fingerprint, never the transcript text. The
    transcribe Lambda already stamps `transcript_fingerprint` on the row, so
    preferring it keeps those routes on a single DynamoDB read now that the
    transcript itself lives in S3.

    Falls back to hashing an inline transcript for legacy rows written before
    that stamp existed — computing it from whatever is actually present is what
    keeps a pre-existing document from flipping to "stale" and silently
    inviting a regeneration the user didn't ask for.
    """
    stamped = item.get("transcript_fingerprint")
    if stamped:
        return stamped
    return ai_schema.fingerprint(item.get("transcript") or "")


def _is_fresh(doc, fingerprint):
    """True when a stored document was generated from the CURRENT transcript
    by the CURRENT prompt version — i.e. the cache may serve it.

    A user-EDITED document is always fresh: the user's own text must never be
    silently replaced by a regeneration. Only an explicit regenerate=true
    overwrites it, and the app warns before sending that.
    """
    if not isinstance(doc, dict) or not doc.get("content"):
        return False
    if doc.get("edited"):
        return True
    return (doc.get("transcript_fingerprint") == fingerprint
            and doc.get("ai_version") == ai_schema.AI_VERSION)


def _needs_speaker_update(doc, speaker_mapping_version):
    """True when `doc` was generated under an OLDER speaker_names mapping than
    the recording's current one — i.e. a rename happened since this document
    was written and its content may still say the old name.

    Deliberately a version-counter comparison, not a text search for "Speaker
    N" or the old name in `content`: a text search can both miss (a since-
    superseded name that's still a valid substring of the new one) and
    false-positive no better than the version check, while costing a scan over
    up to MAX_DOCUMENT_CHARS on every list call. The counter is exact and
    free to compare.

    A user-EDITED document is never flagged — the user's own text must never
    be silently marked wrong just because a rename happened after they wrote
    it, mirroring _is_fresh's treatment of `edited`.
    """
    if not isinstance(doc, dict) or not doc.get("content") or doc.get("edited"):
        return False
    return (doc.get("speaker_mapping_version") or 0) < speaker_mapping_version


def _save_document(key, doc_type, doc):
    """Write one entry into the documents map, creating the map if absent.

    A NESTED-PATH SET is the only safe form here. The workspace can fire two
    generations at once (two Quick AI taps, or a document while a quick action
    is still running), and a whole-map `SET #docs = :map` read-modify-write
    loses every concurrent write but the last — verified against real DynamoDB:
    8 concurrent whole-map writes left 1 document, 8 nested-path writes left
    all 8.

    Two DynamoDB constraints shape the rest, both verified empirically:

      * `SET #docs = if_not_exists(#docs, :empty), #docs.#t = :doc` is REJECTED
        at parse time — "Two document paths overlap with each other". It fails
        regardless of the item's contents, so there is no single-expression way
        to create-the-map-and-set-a-key. TransactWriteItems can't help either
        ("cannot include multiple operations on one item"), and seeding the doc
        into the if_not_exists value SILENTLY DISCARDS it when the map already
        exists.
      * A nested SET whose parent map is absent throws ValidationException
        ("The document path provided in the update expression is invalid for
        update") — it does not auto-create the parent.

    So: attempt the nested SET (the steady-state path, one round trip), and only
    if the parent map is missing create it — guarded by attribute_not_exists so
    a racing writer's map is never blanked — then retry.
    """
    names = {"#docs": DOCUMENTS_ATTR, "#t": doc_type}
    values = {":doc": doc, ":now": _now_iso()}

    def _set_nested():
        _recordings.update_item(
            Key={"audio_s3_key": key},
            UpdateExpression="SET #docs.#t = :doc, updated_at = :now",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    try:
        _set_nested()
        return
    except ClientError as err:
        # Match the message, not just the code: ValidationException is generic,
        # and a real expression bug should surface rather than be retried.
        if err.response.get("Error", {}).get("Code") != "ValidationException" \
                or "invalid for update" not in str(err):
            raise

    # The row predates the documents map. Create it, tolerating the race where
    # a concurrent generation created it first.
    try:
        _recordings.update_item(
            Key={"audio_s3_key": key},
            UpdateExpression="SET #docs = :empty",
            ConditionExpression="attribute_not_exists(#docs)",
            ExpressionAttributeNames={"#docs": DOCUMENTS_ATTR},
            ExpressionAttributeValues={":empty": {}},
        )
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") \
                != "ConditionalCheckFailedException":
            raise
        # Someone else created it between our two calls — exactly what we want.
    _set_nested()


def _generate_document(item, doc_type, system_prompt, label):
    """Run ONE Groq call for a document and return the stored shape.

    The only place a document is produced. Both /documents and /quick funnel
    through it, which is why a Quick AI action and its document twin can share
    a cache entry — they are byte-identical generations.
    """
    transcript = _require_transcript(item)
    highlights = item.get("meeting_highlights")
    context = prompts.build_context(
        item,
        highlights=highlights if isinstance(highlights, dict) else None,
        transcript_budget_chars=_transcript_budget_chars(system_prompt),
    )
    try:
        content = groq_client.complete(
            system_prompt, context, label=f"document:{doc_type}",
            json_mode=False, temperature=0.3,
            # Bail out rather than sleep through a 429 into API Gateway's 29s
            # ceiling — being killed mid-retry returns an opaque gateway 500
            # instead of a clean "retry" the app can act on.
            deadline=time.monotonic() + ONDEMAND_DEADLINE_SECONDS,
        )
    except groq_client.GroqError as err:
        _groq_error(err, label)

    # Same user-output boundary chat and the overview already pass through.
    # Documents are the most Markdown-heavy surface in the app (headings,
    # bullets, bold — see _MARKDOWN_RULES), and they were the one prose
    # surface skipping this: a model that escapes its own syntax sent
    # "\*Important\*" straight to the renderer, which shows the backslashes.
    # Applied BEFORE the length cap so the cap measures what is actually
    # stored, not text that is about to get shorter.
    content = ai_sanitize.sanitize_ai_user_output(
        content or "", label=f"document:{doc_type}")

    content = content.strip()
    if not content:
        raise ApiError(502, f"Unable to generate {label}. Please retry.")
    if len(content) > MAX_DOCUMENT_CHARS:
        content = content[:MAX_DOCUMENT_CHARS].rstrip() + "\n\n[Output truncated.]"

    return {
        "content": content,
        "format": "markdown",
        "generated_at": _now_iso(),
        # The cache identity: what transcript and prompt version produced this.
        "transcript_fingerprint": ai_schema.fingerprint(transcript),
        "ai_version": ai_schema.AI_VERSION,
        "edited": False,
        # The speaker_names version this generation saw — lets a later rename
        # be detected as "this document may now be stale" (see
        # _needs_speaker_update) without storing a computed status here.
        "speaker_mapping_version": item.get("speaker_mapping_version") or 0,
    }


def generate_document(event):
    """POST /recordings/{key+}/documents {type, regenerate?} -> {document}.

    Serves the cached document when the transcript hasn't changed (spec
    section 10); regenerate=true forces a fresh Groq call.
    """
    _, key, item = _owned_recording(event)
    data = _body(event)

    doc_type = str(data.get("type") or "").strip().lower()
    if doc_type not in prompts.DOCUMENTS:
        raise ApiError(400, "unknown document type — use one of: "
                            + ", ".join(sorted(prompts.DOCUMENT_KEYS)))

    regenerate = bool(data.get("regenerate"))
    transcript = _require_transcript(item)
    fingerprint = ai_schema.fingerprint(transcript)

    speaker_mapping_version = item.get("speaker_mapping_version") or 0
    stored = _stored_documents(item).get(doc_type)
    if not regenerate and _is_fresh(stored, fingerprint):
        return _resp(200, {"document": _public_document(doc_type, stored, speaker_mapping_version),
                           "cached": True})

    # THE MoM IS STRUCTURED. Its canonical form is the `mom` attribute, and
    # documents.minutes_of_meeting is a rendered mirror of it (see _mirror_
    # document). Generating one here would write a SECOND, prompt-authored MoM
    # into that same slot with a different section shape and no structure
    # behind it — which is how the 13 legacy Path-A documents came to exist
    # alongside 4 structured ones.
    #
    # Placed AFTER the cache check on purpose: a stored MoM (legacy Markdown
    # or the structured mirror) must still be READABLE through this route,
    # because the export path fetches the latest document this way. What is
    # refused is only creating or regenerating one, and the app already routes
    # Minutes of Meeting to the editor (meeting-documents.tsx) rather than
    # here, so no current client hits this.
    if doc_type == MOM_DOC_TYPE:
        raise ApiError(409, "minutes of meeting are generated as a structured "
                            "document — use the MoM routes")

    spec = prompts.DOCUMENTS[doc_type]
    doc = _generate_document(item, doc_type, spec["system"], spec["label"])
    _save_document(key, doc_type, doc)
    return _resp(200, {"document": _public_document(doc_type, doc, speaker_mapping_version),
                       "cached": False})


# Custom document keys are namespaced so they can never collide with one of
# the 8 fixed prompts.DOCUMENT_KEYS (which are all plain snake_case words) —
# letting update_document/delete_document's "must be a known type OR a
# custom_ key" check stay a simple prefix test rather than a stored set.
CUSTOM_DOC_PREFIX = "custom_"
MAX_CUSTOM_PROMPT_CHARS = 500


def _is_known_doc_type(doc_type, item):
    return doc_type in prompts.DOCUMENTS or (
        doc_type.startswith(CUSTOM_DOC_PREFIX)
        and doc_type in _stored_documents(item)
    )


def generate_custom_document(event):
    """POST /recordings/ai/custom-document/{key+} {prompt} -> {document}.

    The freeform twin of generate_document: no fixed type, no cache lookup
    (every request is a fresh ask, since two different freeform prompts on
    the same meeting are two different documents, not a cache hit/miss on
    one) — just a title call + the generation itself, both against
    prompts.CUSTOM_DOCUMENT_SYSTEM / CUSTOM_TITLE_SYSTEM. Always persisted
    immediately (never a preview-then-save step) so it shows up in
    Documents(N) exactly like a template document does.
    """
    _, key, item = _owned_recording(event)
    data = _body(event)

    prompt = str(data.get("prompt") or "").strip()
    if not prompt:
        raise ApiError(400, "prompt required")
    if len(prompt) > MAX_CUSTOM_PROMPT_CHARS:
        raise ApiError(400, f"prompt too long (max {MAX_CUSTOM_PROMPT_CHARS} chars)")

    _require_transcript(item)

    # A short label call first. Best-effort: if it fails or comes back empty,
    # fall back to a trimmed slice of the prompt itself rather than failing
    # the whole request over a cosmetic title.
    label = ""
    try:
        label = groq_client.complete(
            prompts.CUSTOM_TITLE_SYSTEM, prompt, label="custom-doc-title",
            json_mode=False, temperature=0.2,
            deadline=time.monotonic() + min(8, ONDEMAND_DEADLINE_SECONDS // 2),
        ).strip().strip('"')
    except groq_client.GroqError as err:
        print(f"[ai] custom document title failed, using fallback: {err}")
    # The title is model-written and shown as the document's name in the list,
    # so it crosses the same user-output boundary the body does.
    label = ai_sanitize.sanitize_ai_user_output(
        label, label="custom-doc-title").strip()
    if not label:
        label = prompt[:60] + ("…" if len(prompt) > 60 else "")

    doc = _generate_document(
        item, "custom", prompts.CUSTOM_DOCUMENT_SYSTEM + "\n\nTHE USER'S REQUEST:\n" + prompt,
        label,
    )
    doc["label"] = label
    doc["type_kind"] = "custom"
    doc["source_prompt"] = prompt

    doc_type = CUSTOM_DOC_PREFIX + uuid.uuid4().hex[:12]
    _save_document(key, doc_type, doc)
    return _resp(200, {"document": _public_document(
        doc_type, doc, item.get("speaker_mapping_version") or 0)})


def list_documents(event):
    """GET /recordings/{key+}/documents -> {documents, available,
    speaker_mapping_version, documents_needing_update}.

    `documents` holds what has been generated — the 8 fixed types AND any
    custom_* documents, so Documents(N) renders every real document with one
    call. `available` advertises the 8 fixed types with a `fresh` flag (custom
    documents have no "available slot" to advertise — each freeform request
    makes a new one, there's nothing to offer before it's asked for).

    `documents_needing_update` is the same information already carried by each
    document's own `status`, flattened into one list so the app can render a
    "N documents need updating" banner without re-deriving it from 8+
    individual fields.
    """
    # No hydration: this route lists documents and compares cache identity —
    # it never reads the transcript text, so it must not pay for the S3 GET.
    _, _key, item = _owned_recording(event, hydrate=False)
    stored = _stored_documents(item)
    fingerprint = _row_fingerprint(item)
    speaker_mapping_version = item.get("speaker_mapping_version") or 0

    out = {}
    for doc_type, doc in stored.items():
        if isinstance(doc, dict) and doc.get("content"):
            out[doc_type] = _public_document(doc_type, doc, speaker_mapping_version)

    available = [
        {"type": t, "label": prompts.DOCUMENTS[t]["label"],
         "generated": t in out,
         "fresh": _is_fresh(stored.get(t), fingerprint)}
        for t in prompts.DOCUMENT_KEYS
    ]
    needing_update = [t for t, d in out.items() if d["status"] == "needs_update"]
    return _resp(200, {"documents": out, "available": available,
                       "speaker_mapping_version": speaker_mapping_version,
                       "documents_needing_update": needing_update})


def update_document(event):
    """PATCH /recordings/{key+}/documents {type, content?, label?} -> {document}.

    Documents are editable (spec section 4), and — for both fixed-type and
    custom documents — RENAMABLE: `label` alone (no content) just relabels the
    stored document; either field, or both, may be sent in one call. Editing
    content marks the document so regeneration never silently overwrites the
    user's own text — see _is_fresh. A custom document accepts its own
    synthetic `custom_<id>` type here exactly like a fixed type would.
    """
    # No hydration: an edit stores the user's own text and only needs the
    # fingerprint for provenance (via _row_fingerprint) — not the transcript.
    _, key, item = _owned_recording(event, hydrate=False)
    data = _body(event)

    doc_type = str(data.get("type") or "").strip().lower()
    if not _is_known_doc_type(doc_type, item):
        raise ApiError(400, "unknown document type")
    if "content" not in data and "label" not in data:
        raise ApiError(400, "content and/or label required")

    existing = _stored_documents(item).get(doc_type) or {}
    doc = dict(existing)

    if "content" in data:
        content = str(data.get("content") or "")
        if len(content) > MAX_DOCUMENT_CHARS:
            raise ApiError(400, f"document too long (max {MAX_DOCUMENT_CHARS} chars)")
        doc.update({
            "content": content,
            "format": "markdown",
            "edited": True,
            "generated_at": existing.get("generated_at") or _now_iso(),
            "edited_at": _now_iso(),
            # Keep the provenance of the generation this edit started from; a
            # blank fingerprint would make the edit look stale on next read.
            "transcript_fingerprint": existing.get("transcript_fingerprint")
                or _row_fingerprint(item),
            "ai_version": existing.get("ai_version") or ai_schema.AI_VERSION,
            # The user just hand-verified this content against whatever names
            # are current right now, so it can't be "behind" any rename that
            # already happened — re-stamp to the current version. _is_fresh's
            # `edited` short-circuit means _needs_speaker_update would already
            # return False here regardless, but stamping keeps the field
            # meaningful if the document is ever un-edited or inspected raw.
            "speaker_mapping_version": item.get("speaker_mapping_version") or 0,
        })

    if "label" in data:
        label = str(data.get("label") or "").strip()[:120]
        if not label:
            raise ApiError(400, "label cannot be empty")
        doc["label"] = label

    _save_document(key, doc_type, doc)
    return _resp(200, {"document": _public_document(
        doc_type, doc, item.get("speaker_mapping_version") or 0)})


def delete_document(event):
    """DELETE /recordings/ai/documents/{key+} {type} -> {deleted}.

    Removes one entry from the documents map via a nested REMOVE, mirroring
    _save_document's nested-SET pattern for the same "never lose a concurrent
    write" reason — a whole-map read-modify-write here could just as easily
    resurrect a document someone else deleted a moment ago.
    """
    _, key, item = _owned_recording(event)
    data = _body(event)
    doc_type = str(data.get("type") or "").strip().lower()
    if not _is_known_doc_type(doc_type, item):
        raise ApiError(400, "unknown document type")
    if doc_type not in _stored_documents(item):
        raise ApiError(404, "document not found")

    _recordings.update_item(
        Key={"audio_s3_key": key},
        UpdateExpression="SET updated_at = :now REMOVE #docs.#t",
        ExpressionAttributeNames={"#docs": DOCUMENTS_ATTR, "#t": doc_type},
        ExpressionAttributeValues={":now": _now_iso()},
    )
    return _resp(200, {"deleted": True, "type": doc_type})


def _regenerate_custom_document(item, doc_type, doc):
    """Re-run a custom document against its ORIGINAL prompt.

    Same system prompt generate_custom_document used to create it, but
    skipping that route's title call — the document may since have been
    renamed by the user (update_document's `label`), and a regeneration must
    not silently discard that rename by re-rolling a fresh title. `label`
    falls back to the doc_type only in the pathological case of a custom
    document that somehow has neither a stored label nor content, which
    shouldn't happen in practice since generate_custom_document always sets one.
    """
    label = doc.get("label") or doc_type
    prompt = doc.get("source_prompt") or ""
    fresh = _generate_document(
        item, doc_type,
        prompts.CUSTOM_DOCUMENT_SYSTEM + "\n\nTHE USER'S REQUEST:\n" + prompt,
        label,
    )
    fresh["label"] = label
    fresh["type_kind"] = "custom"
    fresh["source_prompt"] = prompt
    return fresh


def update_stale_documents(event):
    """POST /recordings/ai/update-documents/{key+} {} -> {updated, remaining}.

    "Update All": regenerates every document currently flagged needs_update
    (see _needs_speaker_update) against the recording's CURRENT speaker_names,
    reusing the same generation primitives generate_document/
    generate_custom_document already use — no separate Groq integration.

    Runs against a DEADLINE SHARED across the whole loop (not reset per
    document, unlike a single generate_document call) because this can touch
    every document type in one request and API Gateway's ceiling is fixed
    regardless of how many documents there are to redo. Documents attempted
    before the deadline trips come back in `updated`; anything not yet
    attempted comes back in `remaining` so the app can show "N of M updated —
    tap again" and re-call this route to finish the rest, rather than risking
    an opaque gateway timeout by trying to force everything into one request.
    """
    _, key, item = _owned_recording(event)
    speaker_mapping_version = item.get("speaker_mapping_version") or 0

    stale = [
        (doc_type, doc) for doc_type, doc in _stored_documents(item).items()
        if _needs_speaker_update(doc, speaker_mapping_version)
    ]

    deadline = time.monotonic() + ONDEMAND_DEADLINE_SECONDS
    updated = []
    remaining = []
    for doc_type, doc in stale:
        if time.monotonic() >= deadline:
            remaining.append(doc_type)
            continue
        if doc_type.startswith(CUSTOM_DOC_PREFIX):
            fresh = _regenerate_custom_document(item, doc_type, doc)
        else:
            spec = prompts.DOCUMENTS.get(doc_type)
            if not spec:
                # An unrecognized non-custom key predates today's DOCUMENT_KEYS
                # or belongs to a retired type — nothing to regenerate it
                # against, so leave it exactly as update_document/Regenerate
                # already would (it has no fixed prompt either).
                continue
            fresh = _generate_document(item, doc_type, spec["system"], spec["label"])
        _save_document(key, doc_type, fresh)
        updated.append({"type": doc_type,
                        "document": _public_document(doc_type, fresh, speaker_mapping_version)})

    return _resp(200, {"updated": updated, "remaining": remaining})


# ---------------------------------------------------------------------------
# Reprocess — re-run the WHOLE pipeline for one recording.
#
# This is the app-facing equivalent of scripts/26_reprocess_stuck.py, and it
# deliberately reuses that script's approach: re-invoke transcribeRecording with
# the SAME synthetic S3 event the real trigger sends, rather than adding a
# "reprocess" branch inside the pipeline that could drift from production
# behaviour. The audio is still in S3 (nothing is deleted on failure), so a
# replay is always possible.
#
# Until now the ONLY way to recover a failed/stuck recording was an operator
# running that script from a laptop; the user's own screen was a dead end.
#
# Two things make this safe to expose to end users:
#   * ASYNC invoke ("Event"). The pipeline takes minutes — far past API
#     Gateway's ~30s ceiling — so waiting would guarantee a gateway timeout on
#     a run that is actually succeeding. The app polls `status` as it already
#     does for a first-time upload, so no new client machinery is needed.
#   * A COOLDOWN. Each replay costs one ElevenLabs STT + up to 3 Groq calls, so
#     a user tapping Retry repeatedly (or a client retry loop) would burn real
#     money. `reprocess_started_at` on the row is the guard; a repeat inside the
#     window is refused with 429 rather than silently double-charging.
# ---------------------------------------------------------------------------
REPROCESS_COOLDOWN_SECONDS = int(
    os.environ.get("REPROCESS_COOLDOWN_SECONDS", "300"))

# Only these statuses may be replayed.
#   failed / transcribed  terminal, and the user can see something went wrong.
#   transcribing / generating_ai  stranded mid-pipeline (the 400 KB
#       DynamoDB failure that motivated script 26); the app would otherwise
#       poll them forever.
# "uploading"/"uploaded" are deliberately EXCLUDED: the real S3 trigger may
# still be about to fire for those, and replaying would race the live pipeline.
# "complete" is excluded because Regenerate already covers redoing AI output
# without paying for transcription again.
REPROCESSABLE_STATUSES = frozenset(
    {"failed", "transcribed", "transcribing", "generating_ai"})

# Statuses during which a DELETE is refused with 409 — see delete_recording.
# Only the two windows where the S3 trigger genuinely may be about to write:
# the bytes are landing ("uploading"/"uploaded"). The mid-pipeline states are
# deliberately NOT here — a recording stuck at "transcribing" for an hour is
# the single most likely thing a user wants to delete, and REPROCESSABLE_
# STATUSES already treats those as recoverable-or-dead rather than live.
DELETE_BLOCKING_STATUSES = frozenset({"uploading", "uploaded"})

_lambda_client = boto3.client("lambda", region_name=REGION)

TRANSCRIBE_LAMBDA_NAME = os.environ.get("TRANSCRIBE_LAMBDA_NAME",
                                        "transcribeRecording")

# Phase 2D.4 — the CRM sync queue. A module-level client object, exactly like
# _lambda_client above, so tests patch it the same established way
# (mock.patch.object(api, "_sqs_client", ...)) rather than mocking boto3.client
# globally. THE FIRST SQS USAGE IN THIS CODEBASE — verified absent from the
# live account before this phase (see docs/WORKSPACE_PHASE2D.md).
_sqs_client = boto3.client("sqs", region_name=REGION)


def _s3_trigger_event(bucket, key):
    """The exact event shape S3 sends transcribeRecording.

    quote_plus, not quote: S3 encodes spaces as "+" and the handler calls
    unquote_plus. Getting this wrong would reprocess the WRONG key for any
    recording whose name contains a space — of which there are plenty
    ("WhatsApp Audio 2026-07-27 at ..."). Same reasoning as script 26.
    """
    return {"Records": [{
        "eventSource": "aws:s3",
        "eventName": "ObjectCreated:Put",
        "s3": {"bucket": {"name": bucket},
               "object": {"key": urllib.parse.quote_plus(key)}},
    }]}


def reprocess_recording(event):
    """POST /recordings/ai/reprocess/{key+} {} -> {status, started_at}.

    Re-runs transcription + AI analysis for a recording that failed or stalled.
    Returns 202: the work is accepted and running, not finished.
    """
    _, key, item = _owned_recording(event, hydrate=False)

    status = (item.get("status") or "").strip()
    if status not in REPROCESSABLE_STATUSES:
        if status == "complete":
            # Not an error the user should see as a failure — it just means
            # there is nothing to recover. Regenerate is the right tool.
            raise ApiError(409, "this recording already processed successfully "
                                "— use Regenerate to redo the AI output")
        raise ApiError(409, f"a recording with status '{status}' can't be "
                            "reprocessed yet")

    # Cooldown. Compared against the row's own last attempt so it survives a
    # cold start and holds across every container.
    last = item.get("reprocess_started_at")
    if last:
        # _now_iso() writes "...Z"; fromisoformat wants an offset it recognises.
        # A timestamp we can't parse must never permanently block recovery, so
        # an unparseable value falls through and the reprocess is allowed.
        try:
            started = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            started = None
        if started is not None:
            # NO lower bound on `elapsed`. It is derived from two INDEPENDENT
            # clock reads — datetime.now() when the stamp was written, and
            # time.time() here — so it comes out very slightly NEGATIVE for a
            # repeat that lands in the same instant (measured at ~0.8% of
            # same-moment calls, on the order of -2e-07s). An earlier
            # `0 <= elapsed` guard treated exactly that case as "outside the
            # window" and let the second call through with a 202, which is the
            # precise double-charge this cooldown exists to prevent — the two
            # taps closest together were the ones it failed to catch.
            #
            # A negative elapsed means the stamp is at-or-after now, i.e. the
            # attempt is as fresh as it can possibly be. That is the deepest
            # part of the window, not outside it.
            elapsed = time.time() - started.timestamp()
            if elapsed < REPROCESS_COOLDOWN_SECONDS:
                wait = int(REPROCESS_COOLDOWN_SECONDS - elapsed)
                raise ApiError(429, "already reprocessing — try again in "
                                    f"{wait}s if it still looks stuck")

    if not BUCKET_NAME:
        raise ApiError(500, "server is missing BUCKET_NAME")

    started_at = _now_iso()
    # Stamp BEFORE invoking: if the invoke succeeds but the response is lost in
    # flight, the cooldown has still been recorded and a client retry cannot
    # double-charge. The reverse order could spend twice for one user tap.
    #
    # The STT markers are CLEARED in the same write, and that is load-bearing
    # for correctness, not tidiness. A reprocess of a row whose first
    # transcription job is still in flight creates two live jobs for one
    # recording; the webhook decides which delivery counts by comparing
    # request_id against stt_request_id. Removing the OLD id here means the
    # stale job's late delivery matches nothing and is dropped, during the
    # window before the new job has registered its own id. Leaving it would let
    # the old transcript land on top of the new one.
    _recordings.update_item(
        Key={"audio_s3_key": key},
        UpdateExpression="SET reprocess_started_at = :t, #s = :s "
                         "REMOVE stt_request_id, stt_transcription_id, "
                         "stt_completed_request_id, stt_completed_at",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":t": started_at, ":s": "transcribing"},
    )

    try:
        _lambda_client.invoke(
            FunctionName=TRANSCRIBE_LAMBDA_NAME,
            InvocationType="Event",  # async — see the section comment
            Payload=json.dumps(_s3_trigger_event(BUCKET_NAME, key)).encode("utf-8"),
        )
    except Exception as err:  # noqa: BLE001 — surfaced as a clean 502 below
        print(f"[reprocess] invoke failed for {key}: {err}")
        # Put the status back so the app doesn't show a spinner for a run that
        # never started, and clear the cooldown so the user can retry at once.
        _recordings.update_item(
            Key={"audio_s3_key": key},
            UpdateExpression="SET #s = :s REMOVE reprocess_started_at",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": status},
        )
        raise ApiError(502, "couldn't start reprocessing. Please retry.")

    print(f"[reprocess] {key} re-invoked (was '{status}')")
    return _resp(202, {"status": "transcribing", "started_at": started_at,
                       "previous_status": status})


def quick_action(event):
    """POST /recordings/{key+}/quick {action, regenerate?} -> {document}.

    Quick AI (spec section 5): one tap, no typing. Several actions ALIAS a
    document type — "Generate Minutes of Meeting" is the same deliverable as
    the Minutes document — and aliasing means they share the prompt AND the
    cache entry, so tapping the Quick button after generating the document is
    free rather than a second identical Groq call.
    """
    _, key, item = _owned_recording(event)
    data = _body(event)

    action = str(data.get("action") or "").strip().lower()
    spec = prompts.QUICK_ACTIONS.get(action)
    if not spec:
        raise ApiError(400, "unknown quick action — use one of: "
                            + ", ".join(sorted(prompts.QUICK_ACTION_KEYS)))

    regenerate = bool(data.get("regenerate"))
    transcript = _require_transcript(item)
    fingerprint = ai_schema.fingerprint(transcript)

    # Aliased actions store under the DOCUMENT key (shared cache); standalone
    # extractions store under their own action key. Either way the stored shape
    # is identical, so export/copy/share treat them the same.
    store_key = spec["alias"] or action

    speaker_mapping_version = item.get("speaker_mapping_version") or 0
    stored = _stored_documents(item).get(store_key)
    if not regenerate and _is_fresh(stored, fingerprint):
        return _resp(200, {"document": _public_document(store_key, stored, speaker_mapping_version),
                           "action": action, "cached": True})

    # Same reasoning as generate_document's guard: Quick AI's "Generate
    # Minutes of Meeting" ALIASES the minutes_of_meeting document key, so
    # without this it is a second door onto the same slot and would write a
    # prompt-authored MoM over the structured mirror.
    if store_key == MOM_DOC_TYPE:
        raise ApiError(409, "minutes of meeting are generated as a structured "
                            "document — use the MoM routes")

    doc = _generate_document(item, store_key, spec["system"], spec["label"])
    _save_document(key, store_key, doc)
    return _resp(200, {"document": _public_document(store_key, doc, speaker_mapping_version),
                       "action": action, "cached": False})


def regenerate_highlights(event):
    """POST /recordings/{key+}/highlights {regenerate?} -> {meeting_highlights}.

    The staged pipeline normally writes highlights right after the summary. This
    route exists for the two cases where it didn't: the second Groq call was
    rate-limited during processing, or the recording predates this feature. The
    workspace calls it on first open when the field is missing, which is what
    makes the whole thing work on the existing back catalogue.

    Single Groq call, truncating a long transcript rather than map-reducing it —
    the response has to land inside API Gateway's 29s window. `segments_covered`
    / `segments_total` in the response say whether the whole meeting was seen.
    """
    _, key, item = _owned_recording(event)
    data = _body(event) if event.get("body") else {}
    regenerate = bool(data.get("regenerate"))

    stored = item.get("meeting_highlights")
    if not regenerate and isinstance(stored, dict) and \
            not ai_schema.highlights_empty(stored):
        return _resp(200, {"meeting_highlights": stored, "cached": True})

    transcript = _require_transcript(item)

    # ONE Groq call, never a map-reduce — see _transcript_budget_chars for the
    # full reasoning. A long transcript is TRUNCATED to what fits a single TPM
    # window rather than split across paced calls: map-reducing a 45k-char
    # transcript here took 22s on the first chunk and then died on a 429 retry
    # at API Gateway's 29s ceiling, returning an opaque gateway 500. The staged
    # pipeline (which has 300s and nobody waiting) is where full-fidelity
    # map-reduce belongs; this route exists to backfill rows it missed.
    budget = _transcript_budget_chars(prompts.HIGHLIGHTS_SYSTEM, reserve_tokens=900)
    truncated = len(transcript) > budget
    deadline = time.monotonic() + ONDEMAND_DEADLINE_SECONDS
    try:
        raw = groq_client.complete_json(
            prompts.HIGHLIGHTS_SYSTEM, transcript[:budget],
            label="highlights", deadline=deadline)
    except groq_client.GroqError as err:
        _groq_error(err, "meeting highlights")
    highlights = ai_schema.coerce_highlights(raw)
    # Report coverage honestly: 1-of-2 tells the app (and anyone reading the
    # response) that this covers the start of a longer meeting.
    covered, total = (1, 2) if truncated else (1, 1)

    # Storing an all-empty result would make the cache serve emptiness forever
    # (it's indistinguishable from "never generated"). A meeting really can
    # have no decisions or numbers, so this is returned but not persisted.
    if not ai_schema.highlights_empty(highlights):
        _recordings.update_item(
            Key={"audio_s3_key": key},
            UpdateExpression=("SET meeting_highlights = :h, ai_version = :v, "
                              "transcript_fingerprint = :fp, updated_at = :now"),
            ExpressionAttributeValues={
                ":h": highlights, ":v": ai_schema.AI_VERSION,
                ":fp": ai_schema.fingerprint(transcript), ":now": _now_iso(),
            },
        )
    return _resp(200, {"meeting_highlights": highlights, "cached": False,
                       "segments_covered": covered, "segments_total": total})


# ===========================================================================
# MEETING RETRIEVAL — grounded context for the AI, from anywhere in a meeting.
#
# THE PROBLEM THIS SOLVES. `chat` below used to build its context with
# prompts.build_context(..., transcript_budget_chars=...), which head-truncates:
# a transcript longer than one TPM window is cut to its FIRST N characters and
# the model is told the record is partial. On a 15-minute meeting that never
# fires. On a two-hour one it means "what did we decide about pricing at the
# end?" is unanswerable — the model is holding the first thirty-five minutes,
# and the rest is in S3, unread.
#
# So the transcript is indexed into a per-meeting PageIndex tree (see
# shared/pageindex.py) and the question is used to RETRIEVE the sections that
# bear on it, wherever in the meeting they fall.
#
# WHEN RETRIEVAL RUNS. Only when it has to. If the whole labelled transcript
# fits the budget it is all sent, exactly as before — retrieval can only lose
# information in that case, and it would cost an extra Groq round-trip to do
# it. Retrieval engages precisely in the case the old code got wrong.
#
# THE ONE ENTRY POINT. `retrieve_meeting_context` is the only way into this,
# and it takes the ALREADY-AUTHORIZED item from _owned_recording — it is never
# handed a bare meeting_id. That ordering (authorize, then retrieve) is what
# makes it impossible to reach another user's index through this path: there is
# no code path that resolves an index from a key alone.
# ===========================================================================

# How many retrieved segments are turned into `sources` on the response. Four
# is what a chat bubble can show without becoming a citation list; the answer
# itself is grounded in everything retrieved, not just these.
MAX_CHAT_SOURCES = 4

# Deadline split for the two-call retrieval path. The navigation call is small
# (a table of contents and a question) and must not eat the budget the ANSWER
# needs — a fast retrieval followed by a killed answer is strictly worse than
# no retrieval at all, because the user waited and got nothing.
RETRIEVAL_DEADLINE_SECONDS = int(os.environ.get("PAGEINDEX_RETRIEVAL_DEADLINE", "7"))


def _speaker_names(item):
    names = item.get("speaker_names")
    return names if isinstance(names, dict) else {}


def _authoritative_mom(item):
    """The user-reviewed MoM as Markdown, or "".

    Sent to the model ABOVE the transcript and marked authoritative (see
    prompts.MOM_CONTEXT_RULES) so a correction the user made in the minutes
    wins over what the audio literally says.

    Read from the stored STRUCTURE and re-rendered rather than from the
    mirrored document, because the structure is the source of truth
    (mom_schema) and the mirror is refreshed from it — going to the structure
    means the model cannot see a mirror that a failed write left stale.
    """
    mom = _stored_mom(item)
    if not mom:
        return ""
    try:
        return mom_schema.render_markdown(mom) or ""
    except Exception as err:  # noqa: BLE001
        print(f"[pageindex] MoM render failed: {err}")
        return ""


def _ensure_index(key, item, fingerprint, deadline=None):
    """The meeting's PageIndex tree, building it if needed. None if unavailable.

    LAZY GENERATION. An index that is missing (an old meeting, a build that
    failed, a pipeline that skipped it) is built here, on the first question
    that needs it. That is what keeps "no index" from being a user-visible
    error: the feature repairs itself on demand and the user just gets an
    answer (Part 23).

    SINGLE-FLIGHT. The build is behind pageindex_store.claim(), a conditional
    write only one caller can win. A caller that loses returns None and falls
    back for that one request rather than duplicating the work.

    NEVER RAISES. Every failure returns None, and None means "answer the old
    way" — which still works. An index is an optimisation over a fallback that
    is already correct, so no failure in here is worth a 500.
    """
    stale_pointer = False
    if pageindex_store.is_fresh(item, fingerprint):
        tree = pageindex_store.load(_s3, BUCKET_NAME, item)
        if tree:
            return tree
        # Pointer says ready, object is gone or corrupt. Rebuild rather than
        # trust a record we just failed to honour — and claim with force, or the
        # claim would be refused for the very freshness we have just disproved
        # and this meeting could never recover.
        stale_pointer = True

    owner = item.get("user_id") or ""
    if not pageindex_store.claim(_recordings, key, fingerprint, owner,
                                 force=stale_pointer):
        return None

    try:
        tree = pageindex_store.build_for(
            item, item.get("timestamps") or [], item.get("transcript") or "",
            groq=groq_client, deadline=deadline)
    except Exception as err:  # noqa: BLE001
        print(f"[pageindex] build FAILED for {key}: {err}")
        pageindex_store.mark_failed(_recordings, key, fingerprint, str(err))
        return None

    if not tree.get("nodes"):
        # A transcript too short to cut into nodes is not a failure — it is a
        # meeting that never needed retrieval. Recorded as ready so we do not
        # re-attempt on every question.
        pageindex_store.save(_s3, BUCKET_NAME, _recordings, key, tree,
                             fingerprint, owner)
        return None

    pageindex_store.save(_s3, BUCKET_NAME, _recordings, key, tree,
                         fingerprint, owner)
    return tree


def _navigate(tree, question, item, deadline):
    """Ask the model which sections to read. Returns (node ids, mode).

    On ANY failure — Groq down, a deadline hit, an unparseable reply — this
    falls back to a SPREAD of nodes across the meeting rather than to nothing.
    Evenly spaced beats the first N: the first N is the head-truncation
    behaviour this whole module exists to replace, so degrading into it would
    be the one fallback that reproduces the bug.
    """
    toc = pageindex.table_of_contents(tree, _speaker_names(item))
    try:
        reply = groq_client.complete(
            prompts.RETRIEVAL_SYSTEM,
            prompts.retrieval_request(question, toc),
            label="pageindex-retrieval", json_mode=True, temperature=0.0,
            deadline=deadline,
        )
        node_ids = pageindex.parse_node_ids(reply, tree)
        if node_ids:
            return node_ids, "llm"
    except groq_client.GroqError as err:
        print(f"[pageindex] navigation failed, spreading instead: {err}")
    except Exception as err:  # noqa: BLE001
        print(f"[pageindex] navigation error, spreading instead: {err}")

    nodes = tree.get("nodes") or []
    if not nodes:
        return [], "none"
    step = max(1, len(nodes) // 6)
    return [n["node_id"] for n in nodes[::step]][:6], "spread"


def _fit_segments(segments, budget_chars, names):
    """Retrieved segments trimmed to the context budget. (lines, kept, cut).

    Trimmed from the END so the best-first ordering of the navigator's picks is
    honoured: the sections it ranked highest survive. Segments are dropped
    WHOLE — a half segment would be text no `seg_N` can address, which would
    break the evidence link for the one line most likely to be cited.
    """
    kept, used = [], 0
    for seg in segments:
        line = pageindex.render_segments([seg], names)
        if used + len(line) > budget_chars and kept:
            return pageindex.render_segments(kept, names), kept, True
        kept.append(seg)
        used += len(line) + 1
    return pageindex.render_segments(kept, names), kept, False


def retrieve_meeting_context(item, key, question, system_prompt):
    """Grounded context for ONE meeting question. THE retrieval entry point.

    `item` must be an ALREADY-AUTHORIZED, hydrated recording (what
    _owned_recording returns). This function does not take a meeting id and
    cannot resolve one — authorization happens before retrieval, structurally,
    not by remembering to check.

    Returns (context, sources, metrics). `sources` is the public evidence
    array — segment ids the app already knows how to deep-link. `metrics` is
    for the log line, never for the response body.

    THE TWO PATHS, and why the choice is made on size alone:
      * transcript FITS the budget -> send all of it. Retrieval could only
        subtract, and the model answering from the complete record is strictly
        better than answering from a chosen part of it.
      * transcript does NOT fit -> retrieve. This is the case the old code
        answered with the first N characters.
    """
    started = time.monotonic()
    names = _speaker_names(item)
    transcript = item.get("transcript") or ""
    timestamps = item.get("timestamps") or []
    highlights = item.get("meeting_highlights")
    highlights = highlights if isinstance(highlights, dict) else None
    mom = _authoritative_mom(item)

    # The budget must account for the MoM: it rides in the same window as the
    # transcript, and on a heavily-edited meeting it is not small.
    budget = _transcript_budget_chars(system_prompt + mom)
    # `names` is passed so a renamed speaker reads as "Ravi:" in the line
    # itself, exactly as pageindex.render_segments already does on the
    # retrieval path below. Without it this path handed the model
    # "Speaker 0:" plus a distant roster line and made every name question a
    # two-hop lookup — see transcript_store.as_labelled_lines.
    labelled = transcript_store.as_labelled_lines(transcript, timestamps,
                                                  names)

    metrics = {"retrieval_mode": "full", "retrieved_segments": 0,
               "nodes_selected": 0, "retrieval_ms": 0}

    if len(labelled) <= budget:
        # Whole meeting fits. Note this is now the LABELLED transcript, so even
        # this path can cite segment ids — evidence is not a retrieval-only
        # feature.
        context = prompts.build_grounded_context(
            item, labelled, highlights=highlights, mom_markdown=mom)
        metrics["retrieved_segments"] = len(timestamps)
        metrics["retrieval_ms"] = int((time.monotonic() - started) * 1000)
        return context, [], metrics

    fingerprint = _row_fingerprint(item)
    tree = _ensure_index(key, item, fingerprint,
                         deadline=time.monotonic() + RETRIEVAL_DEADLINE_SECONDS)

    if not tree:
        # NO INDEX AND WE COULD NOT MAKE ONE. Fall back to the old behaviour
        # rather than failing the question: a head-truncated answer is what the
        # user got yesterday, and it is not nothing. The MoM still rides along,
        # so the authoritative-minutes fix holds even on this path.
        metrics["retrieval_mode"] = "fallback"
        metrics["retrieval_ms"] = int((time.monotonic() - started) * 1000)
        context = prompts.build_context(
            item, highlights=highlights, transcript_budget_chars=budget)
        if mom:
            context = ("=== MINUTES OF MEETING (user-reviewed, authoritative) "
                       "===\n" + mom.strip() + "\n\n" + context)
        return context, [], metrics

    node_ids, mode = _navigate(tree, question, item,
                               time.monotonic() + RETRIEVAL_DEADLINE_SECONDS)
    segments = pageindex.segments_for_nodes(tree, timestamps, node_ids)
    lines, kept, truncated = _fit_segments(segments, budget - len(mom), names)

    context = prompts.build_grounded_context(
        item, lines, highlights=highlights, mom_markdown=mom,
        truncated=truncated)

    metrics.update({
        "retrieval_mode": f"pageindex:{mode}",
        "retrieved_segments": len(kept),
        "nodes_selected": len(node_ids),
        "retrieval_ms": int((time.monotonic() - started) * 1000),
        "extract_truncated": truncated,
    })
    return context, pageindex.evidence_from_segments(kept, MAX_CHAT_SOURCES), metrics


# ---------------------------------------------------------------------------
# The model cites its evidence on a trailing SOURCES line (see
# prompts.GROUNDED_CHAT_RULES). It is stripped from the prose before the user
# sees it and turned into the structured `sources` array instead — the app
# renders a tappable source row, not a line of raw ids.
# ---------------------------------------------------------------------------
_SOURCES_LINE = re.compile(r"\n*^\s*SOURCES:\s*(.*?)\s*$",
                           re.IGNORECASE | re.MULTILINE)


def _log_ai_metrics(route, key, item, metrics, system, message, reply, llm_ms):
    """One structured line per AI request, for CloudWatch Insights.

    WHY A SINGLE LINE OF JSON. These fields only answer questions when they can
    be correlated — "what did retrieval cost on the meetings where the answer
    was wrong?" needs mode, token count and latency from the SAME request. Split
    across several prints they cannot be joined; as one JSON object an Insights
    query filters and aggregates them directly.

    TOKEN COUNTS ARE ESTIMATES, and labelled so in the field names via the
    `est_` prefix. Groq returns real usage on the response, but groq_client.
    complete() hands back only the message text, and widening that return type
    would touch every caller in both Lambdas — too much blast radius for
    telemetry. est_tokens() is the same estimator the budgeting already trusts
    to decide what fits, so the numbers are consistent with the decisions made
    from them, which matters more here than absolute accuracy.

    NO TRANSCRIPT CONTENT. Sizes and counts only — never the question, never the
    answer, never a retrieved line. These logs are retained and broadly
    readable, and a meeting's contents must not leak into them.
    """
    try:
        payload = {
            "evt": "ai_request",
            "route": route,
            "meeting_id": key,
            "index_status": pageindex_store.meta(item).get("status") or "none",
            "transcript_fingerprint": _row_fingerprint(item),
            "est_context_tokens": groq_client.est_tokens(system),
            "est_input_tokens": groq_client.est_tokens(system + message),
            "est_output_tokens": groq_client.est_tokens(reply),
            "llm_ms": llm_ms,
            "total_ms": metrics.get("retrieval_ms", 0) + llm_ms,
        }
        payload.update(metrics)
        payload["est_total_tokens"] = (payload["est_input_tokens"]
                                       + payload["est_output_tokens"])
        print("[ai_metrics] " + json.dumps(payload, default=str))
    except Exception as err:  # noqa: BLE001
        # Telemetry must never be able to fail a request that already succeeded.
        print(f"[ai_metrics] logging failed: {err}")


def _ddb_safe(sources):
    """Sources with their float timestamps as Decimal, for storage.

    boto3's resource layer raises "Float types are not supported" outright, so
    writing a source array straight from pageindex.evidence_from_segments (which
    produces floats, correctly — it also feeds the JSON response, where Decimal
    is what would break) would 500 the whole chat request at the very last
    write, AFTER Groq had already been paid for the answer. Converted here at
    the boundary, the same way _as_duration does for an upload.

    Rounded to 2dp: these are seek positions in seconds, and the app does
    Number(...) on them for playback — centisecond precision is already more
    than the player can act on.
    """
    out = []
    for src in sources or []:
        row = dict(src)
        for field in ("start_time", "end_time"):
            try:
                row[field] = Decimal(str(round(float(src.get(field) or 0), 2)))
            except (TypeError, ValueError, InvalidOperation):
                row[field] = Decimal("0")
        out.append(row)
    return out


def _split_sources(reply, retrieved_sources):
    """(prose, sources) — the SOURCES line removed and resolved.

    The model's cited ids are matched against what was actually RETRIEVED, so a
    hallucinated or out-of-context id cannot become a source row that scrolls
    nowhere. When it cites nothing usable, the retrieved segments stand as the
    sources anyway: the answer did come from them, and a working "view in
    transcript" is more useful than a missing one.
    """
    match = _SOURCES_LINE.search(reply or "")
    if not match:
        return (reply or "").strip(), retrieved_sources

    prose = _SOURCES_LINE.sub("", reply, count=1).strip()
    raw = match.group(1) or ""
    if raw.strip().lower() in ("none", "-", ""):
        return prose, []

    cited = [t.strip() for t in re.split(r"[,\s]+", raw) if t.strip()]
    by_id = {s["segment_id"]: s for s in retrieved_sources}
    picked = [by_id[c] for c in cited if c in by_id]
    return prose, (picked or retrieved_sources)


# ---------------------------------------------------------------------------
# AI Chat — "Ask MinuteX"
# ---------------------------------------------------------------------------
def _stored_chat(item):
    hist = item.get(CHAT_ATTR)
    return hist if isinstance(hist, list) else []


def _clean_history(raw):
    """Validate a client-supplied history array.

    The client may send its own history (so a conversation works before
    anything is persisted), but it is never trusted as-is: only user/assistant
    roles, only strings, and only the last CHAT_HISTORY_TURNS entries — a
    client that sent 500 turns would otherwise blow the TPM window on history
    and leave nothing for the transcript.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ApiError(400, "history must be an array of {role, content}")
    out = []
    for m in raw[-(CHAT_HISTORY_TURNS * 2):]:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "").strip().lower()
        content = str(m.get("content") or "").strip()[:MAX_CHAT_MESSAGE_CHARS]
        if role in ("user", "assistant") and content:
            out.append({"role": role, "content": content})
    return out


def chat(event):
    """POST /recordings/{key+}/chat {message, history?} -> {reply, chat_history, sources}.

    Reuses the shared Groq client with the chat prompt from prompts.py. Context
    now comes from retrieve_meeting_context (see the Meeting Retrieval section
    above) rather than from prompts.build_context directly: a transcript that
    fits the budget is still sent whole, and one that does not is RETRIEVED
    against instead of head-truncated, so a question about the end of a long
    meeting is answerable.

    `sources` is new and ADDITIVE — the segment ids the answer rests on, in the
    same shape the transcript deep-link already consumes. An older app build
    ignores the extra key; a newer one renders tappable sources.

    Generated documents are deliberately NOT included in the context: they are
    derived from the same transcript and analysis already present, so sending
    them would spend the TPM budget restating what the model can already see —
    exactly the "do not resend unnecessary data" constraint in spec section 6.
    The MoM is the ONE exception, because the user can edit it and their
    correction must outrank the transcript (see _authoritative_mom).
    """
    _, key, item = _owned_recording(event)
    data = _body(event)

    message = str(data.get("message") or "").strip()
    if not message:
        raise ApiError(400, "message required")
    if len(message) > MAX_CHAT_MESSAGE_CHARS:
        raise ApiError(400, f"message too long (max {MAX_CHAT_MESSAGE_CHARS} chars)")

    _require_transcript(item)

    # Client history wins when supplied (it reflects what the user actually has
    # on screen); otherwise continue from what's stored.
    history = _clean_history(data.get("history"))
    if not history:
        history = _clean_history(_stored_chat(item))

    # The system prompt is assembled BEFORE retrieval because the retrieval
    # budget is computed against it — the meeting content and the instructions
    # share one context window, so the rules' own size has to be subtracted
    # before deciding how much transcript fits.
    system_rules = prompts.CHAT_SYSTEM + prompts.GROUNDED_CHAT_RULES
    if _stored_mom(item):
        system_rules += prompts.MOM_CONTEXT_RULES

    context, retrieved, metrics = retrieve_meeting_context(
        item, key, message, system_rules)

    # The meeting content goes in the SYSTEM turn, not the user turn: it is
    # standing context for the whole conversation, and keeping the user turn to
    # just the question is what lets history stay meaningful across turns.
    system = system_rules + "\n\n" + context

    llm_started = time.monotonic()
    try:
        reply = groq_client.complete(
            system, message, label="chat", json_mode=False, temperature=0.3,
            history=history,
            deadline=time.monotonic() + ONDEMAND_DEADLINE_SECONDS,
        )
    except groq_client.GroqError as err:
        _groq_error(err, "a reply")
    llm_ms = int((time.monotonic() - llm_started) * 1000)

    reply = (reply or "").strip()
    if not reply:
        raise ApiError(502, "Unable to generate a reply. Please retry.")

    reply, sources = _split_sources(reply, retrieved)
    # LAST HOP BEFORE THE USER. The transcript reaches the model as
    # `[seg_N] Speaker: ...` lines because seg_N is what the app deep-links
    # on, and a model reading an id on every line will occasionally cite one
    # in its prose. The prompt forbids it (prompts.GROUNDED_CHAT_RULES); this
    # guarantees it, and unescapes the Markdown the renderer cannot.
    reply = ai_sanitize.sanitize_ai_user_output(reply, label=f"chat:{key}")
    if not reply:
        # The model answered with nothing but a SOURCES line. Rare, but a blank
        # bubble is worse than an honest one.
        raise ApiError(502, "Unable to generate a reply. Please retry.")

    _log_ai_metrics("chat", key, item, metrics, system, message, reply, llm_ms)

    now = _now_iso()
    assistant_turn = {"role": "assistant",
                      "content": reply[:MAX_DOCUMENT_CHARS], "at": now}
    # Sources ride ON the stored turn, so reopening the screen restores the
    # tappable source rows instead of showing an answer whose evidence
    # evaporated. Written only when there are any, keeping the stored shape
    # identical to before for the full-transcript path — an older app parsing
    # this history sees exactly the turns it always did.
    if sources:
        assistant_turn["sources"] = _ddb_safe(sources)

    turns = _stored_chat(item) + [
        {"role": "user", "content": message, "at": now},
        assistant_turn,
    ]
    # Trim oldest-first so the item can't grow without bound.
    turns = turns[-(MAX_CHAT_TURNS * 2):]
    _recordings.update_item(
        Key={"audio_s3_key": key},
        UpdateExpression="SET chat_history = :h, updated_at = :now",
        ExpressionAttributeValues={":h": turns, ":now": now},
    )
    return _resp(200, {"reply": reply, "chat_history": turns,
                       "sources": sources})


def get_chat(event):
    """GET /recordings/{key+}/chat -> {chat_history, suggestions}.

    Suggestions are served from the backend so the prompt catalogue lives in
    one place (prompts.py) rather than being hardcoded in the app — the same
    reason the document list is advertised by the API.
    """
    _, _key, item = _owned_recording(event)
    return _resp(200, {"chat_history": _stored_chat(item),
                       "suggestions": CHAT_SUGGESTIONS})


def clear_chat(event):
    """DELETE /recordings/{key+}/chat -> {cleared}. Starts a fresh conversation."""
    _, key, _item = _owned_recording(event)
    _recordings.update_item(
        Key={"audio_s3_key": key},
        UpdateExpression="SET chat_history = :empty, updated_at = :now",
        ExpressionAttributeValues={":empty": [], ":now": _now_iso()},
    )
    return _resp(200, {"cleared": True})


# Suggested prompts, grouped exactly as specified (spec section 6). Data, so
# the app renders whatever the backend advertises and a new suggestion ships
# without an app release.
CHAT_SUGGESTIONS = [
    {"group": "Meeting", "prompts": [
        "Summarize this meeting",
        "Explain what this meeting was about",
    ]},
    {"group": "Business", "prompts": [
        "What was decided?",
        "What are the risks?",
        "What are the deadlines?",
        "What are the action items?",
    ]},
    {"group": "Sales", "prompts": [
        "What buying signals were there?",
        "What objections were raised?",
        "What was said about budget?",
        "What pricing was discussed?",
        "Were any competitors mentioned?",
    ]},
    {"group": "Project", "prompts": [
        "What tasks came out of this?",
        "What are the deliverables?",
        "What materials are needed?",
        "What is the timeline?",
    ]},
    {"group": "Reports", "prompts": [
        "Write the minutes of meeting",
        "Write an executive summary",
        "Write a site visit report",
    ]},
    {"group": "Follow-up", "prompts": [
        "Draft a follow-up email",
        "Draft a WhatsApp update",
        "Draft a reminder message",
    ]},
]


# ===========================================================================
# MINUTES OF MEETING — the structured, editable MoM.
#
# WHAT THIS ADDS, AND WHAT IT DELIBERATELY DOES NOT REPLACE.
#
# `documents.minutes_of_meeting` already existed as a Markdown blob generated
# by prompts.DOCUMENTS. That is fine to read and impossible to EDIT
# structurally — "delete the Highlights section", "move Action Items above
# Decisions", "add a Deadline column" and "keep MY wording for this line when
# the AI regenerates the rest" are all unanswerable against a blob.
#
# So the MoM gains a structured representation in the `mom` attribute, and the
# Markdown document becomes a derived MIRROR of it, rewritten by
# _persist_mom on every structured write. Every existing consumer — the
# Documents list, DOCX/PDF export, Share, the Assistant's context — keeps
# reading `documents.minutes_of_meeting` and keeps working, unchanged. There
# is exactly ONE MoM in the product, not two.
#
# NO GROQ CALL. mom_schema.build_sections arranges data the pipeline already
# produced (participants, meeting_highlights, tasks, summary, highlights), so
# generating a MoM costs no tokens and cannot fail on a rate limit. That is
# also why there is no `regenerate` cache check here: rebuilding is cheap, and
# the merge in mom_schema keeps user edits regardless.
#
# Routes (action first, key LAST — the same API Gateway constraint that shapes
# every other AI route in this file):
#   GET    /recordings/ai/mom/{key+}                  -> {mom, document}
#   POST   /recordings/ai/mom/{key+}   {}             -> {mom, document}
#   PUT    /recordings/ai/mom/{key+}   {mom}          -> {mom, document}
#   DELETE /recordings/ai/mom/{key+}                  -> {deleted}
# ===========================================================================

# Same storage reasoning as DOCUMENTS_ATTR/TASKS_ATTR: one attribute on the
# recording row, always read with the recording, never queried independently.
MOM_ATTR = "mom"

# The document slot the structured MoM mirrors into. Deliberately the EXISTING
# minutes_of_meeting type rather than a new one, so the app's Documents list
# shows one MoM, not a structured one beside a legacy one.
MOM_DOC_TYPE = "minutes_of_meeting"

# A structured MoM whose rendered Markdown exceeds MAX_DOCUMENT_CHARS cannot
# be mirrored into the document slot. The structure is still stored (it is the
# source of truth); the mirror is truncated exactly as _generate_document
# truncates a runaway model, so the two paths behave identically.


def _stored_mom(item):
    got = item.get(MOM_ATTR)
    return got if isinstance(got, dict) else None


def _mom_tasks(user_id, key):
    """The meeting's real tasks, in API shape, for the Action Items section.

    Reuses the Tasks table read that list_meeting_tasks uses, so the MoM
    states the same owners and due dates the Tasks screen does. Never seeds or
    migrates — that is list_meeting_tasks' job and doing it here would make a
    read route write.
    """
    try:
        rows = [r for r in _tasks_for_recording(key)
                if r.get("owner_user_id") == user_id]
    except ClientError as err:
        # A MoM without its Action Items table beats no MoM at all; the
        # builder falls back to the highlights extraction.
        print(f"[mom] task read failed for {key}: {type(err).__name__}: {err}")
        return []
    rows.sort(key=lambda r: r.get("created_at", ""))
    return rows


def _self_tagged_attendee_names(user_id, key):
    """Names of people recorded as PRESENT but not speaking, for the MoM.

    Read from the participant rows rather than the transcript, because that is
    the only place this fact exists — a non-speaking attendee leaves no trace
    in the audio by definition. Best-effort: a Minutes document missing one
    attendee line beats failing to generate.
    """
    try:
        rows = _participant_rows(key)
    except ClientError as err:
        print(f"[mom] participant read failed for {key}: "
              f"{type(err).__name__}: {err}")
        return []
    names = []
    for row in rows:
        if not _is_self_speaker(row.get("speaker_id")):
            continue
        if row.get("owner_user_id") != user_id:
            continue
        c = _contacts.get_item(
            Key={"contact_id": row.get("contact_id", "")}).get("Item")
        if c and c.get("owner_user_id") == user_id and c.get("name"):
            names.append(c["name"])
    return names


def _build_fresh_sections(user_id, key, item):
    names = item.get("speaker_names") or {}
    tasks = [_public_task_v2(r, names) for r in _mom_tasks(user_id, key)]
    return mom_schema.build_sections(
        item, tasks=tasks, speaker_names=names,
        attendees=_self_tagged_attendee_names(user_id, key))


def _persist_mom(key, item, mom, regenerated=False):
    """Write the structure AND refresh its Markdown mirror. One place.

    Both writes always happen together — a structure whose mirror is stale
    would show one MoM in the editor and a different one in the exported DOCX,
    which is the single worst failure this feature could have. They are two
    UpdateItems rather than one because _save_document's nested-path write is
    what keeps concurrent document writes from clobbering each other, and that
    reasoning applies here too.
    """
    mom = mom_schema.coerce_mom(mom)
    now = _now_iso()
    mom["updated_at"] = now
    if regenerated or not mom.get("generated_at"):
        mom["generated_at"] = now
    mom["transcript_fingerprint"] = _row_fingerprint(item)
    mom["speaker_mapping_version"] = item.get("speaker_mapping_version") or 0
    mom["mom_version"] = mom_schema.MOM_VERSION

    _recordings.update_item(
        Key={"audio_s3_key": key},
        UpdateExpression="SET #mom = :mom, updated_at = :now",
        ExpressionAttributeNames={"#mom": MOM_ATTR},
        ExpressionAttributeValues={":mom": mom, ":now": now},
    )

    document = _mirror_document(key, item, mom)
    return mom, document


def _mirror_document(key, item, mom):
    """Render the structure into documents.minutes_of_meeting.

    The mirror is marked `edited: True` on purpose. It is not AI output any
    more — it is whatever the user's structured MoM currently says — and
    `edited` is exactly the flag the existing _is_fresh/_needs_speaker_update
    rules read to mean "never silently overwrite this". Without it, a
    Regenerate on the Documents screen would replace a hand-built MoM with a
    fresh prompt-generated blob and the structure would silently diverge from
    the document.
    """
    content = mom_schema.render_markdown(mom)
    if len(content) > MAX_DOCUMENT_CHARS:
        content = content[:MAX_DOCUMENT_CHARS].rstrip() + "\n\n[Output truncated.]"

    existing = _stored_documents(item).get(MOM_DOC_TYPE) or {}
    doc = dict(existing)
    doc.update({
        "content": content,
        "format": "markdown",
        "edited": True,
        "structured": True,
        "generated_at": existing.get("generated_at") or mom.get("generated_at") or _now_iso(),
        "edited_at": _now_iso(),
        "transcript_fingerprint": mom.get("transcript_fingerprint") or _row_fingerprint(item),
        "ai_version": existing.get("ai_version") or ai_schema.AI_VERSION,
        "speaker_mapping_version": item.get("speaker_mapping_version") or 0,
    })
    _save_document(key, MOM_DOC_TYPE, doc)
    return _public_document(MOM_DOC_TYPE, doc,
                            item.get("speaker_mapping_version") or 0)


def _mom_response(mom, document, **extra):
    payload = {"mom": mom, "document": document}
    payload.update(extra)
    return _resp(200, payload)


def get_mom(event):
    """GET /recordings/ai/mom/{key+} -> {mom, document, exists}.

    A pure read: a recording with no MoM yet returns exists=false and an empty
    structure rather than generating one. Generation is POST, so opening the
    editor can never cost a write on a recording the user was only browsing.
    """
    _, key, item = _owned_recording(event, hydrate=False)
    stored = _stored_mom(item)
    if stored is None:
        return _resp(200, {"mom": mom_schema.empty_mom(), "document": None,
                           "exists": False})
    mom = mom_schema.coerce_mom(stored)
    documents = _stored_documents(item)
    document = None
    if MOM_DOC_TYPE in documents:
        document = _public_document(MOM_DOC_TYPE, documents[MOM_DOC_TYPE],
                                    item.get("speaker_mapping_version") or 0)
    return _mom_response(mom, document, exists=True)


def generate_mom(event):
    """POST /recordings/ai/mom/{key+} {} -> {mom, document}.

    First call builds the MoM from the existing analysis. A later call
    REGENERATES: fresh AI content is merged into the stored structure, so
    `ai` items refresh while `user_edited`/`user_added` items and the user's
    ordering, deletions and hidden flags all survive (see
    mom_schema.merge_generated).

    Requires a transcript for the same reason every other AI route does — a
    MoM built from an empty analysis would be an empty document.
    """
    user_id, key, item = _owned_recording(event)
    _require_transcript(item)

    fresh = _build_fresh_sections(user_id, key, item)
    stored = _stored_mom(item)

    if stored is None:
        mom = mom_schema.coerce_mom({
            "title": "Minutes of Meeting",
            "subtitle": item.get("title") or "",
            "sections": fresh,
        })
    else:
        mom = mom_schema.merge_generated(stored, fresh)
        # A subtitle follows the meeting title unless the user retitled it.
        if not mom.get("subtitle"):
            mom["subtitle"] = item.get("title") or ""

    if mom_schema.is_empty(mom):
        raise ApiError(409, "there isn't enough analysed content in this "
                            "meeting to build minutes yet")

    mom, document = _persist_mom(key, item, mom, regenerated=True)
    return _mom_response(mom, document, regenerated=stored is not None)


def save_mom(event):
    """PUT /recordings/ai/mom/{key+} {mom} -> {mom, document}.

    The ONE write path for every edit the editor makes — add/edit/delete a
    section, field, row, column or list item, and reordering. A whole-document
    PUT rather than a route per operation because the editor holds the entire
    structure in memory anyway (it has to, to render it), the document is
    small, and fifteen granular routes would each need their own ownership
    check, coercion and mirror refresh — fifteen chances to forget one.

    Concurrency is last-write-wins, which is correct for a single-user editor
    and is what the rest of this file already does for a document edit.

    Deletions arrive as `deleted_ids`, not as absences: a section simply
    missing from the payload could equally mean "an older client didn't send
    it", and treating that as a delete would lose content. The client sends
    the tombstone explicitly, and mom_schema.merge_generated honours it on
    every later regeneration.
    """
    _, key, item = _owned_recording(event, hydrate=False)
    data = _body(event)

    raw = data.get("mom")
    if not isinstance(raw, dict):
        raise ApiError(400, "mom object required")

    mom = mom_schema.coerce_mom(raw)
    if not mom["sections"]:
        raise ApiError(400, "a MoM needs at least one section")

    # Preserve provenance the client has no business rewriting.
    stored = _stored_mom(item) or {}
    mom["generated_at"] = mom_schema.coerce_mom(stored).get("generated_at") or ""

    mom, document = _persist_mom(key, item, mom)
    return _mom_response(mom, document)


def delete_mom(event):
    """DELETE /recordings/ai/mom/{key+} -> {deleted}.

    Removes the STRUCTURE only. The mirrored Markdown document is left alone
    and stays deletable through the existing DELETE .../ai/documents route —
    deleting both here would make "reset the editor" also destroy a document
    the user may have exported and still wants listed.
    """
    _, key, item = _owned_recording(event, hydrate=False)
    if _stored_mom(item) is None:
        raise ApiError(404, "no minutes for this recording")
    _recordings.update_item(
        Key={"audio_s3_key": key},
        UpdateExpression="SET updated_at = :now REMOVE #mom",
        ExpressionAttributeNames={"#mom": MOM_ATTR},
        ExpressionAttributeValues={":now": _now_iso()},
    )
    return _resp(200, {"deleted": True})


# ===========================================================================
# TASKS — persisted assign/notify (Task Detail -> Assign To -> Notify).
#
# Previously entirely client-side (lib/task-model.ts): a task, its assignee
# and its notification log all lived only in React state and were lost on
# reload. This persists the CORE fields on the recording row — task text,
# assignee, due date, priority, status, and which channels it's been notified
# through — the same way `documents` does: one map attribute, keyed by task
# id, nested SET/REMOVE for atomic writes.
#
# Deliberately NOT persisted here (stay client-side, per the same tradeoff
# already accepted for generated-document rename before this change, and
# documented in task-model.ts): subtasks, attachments, freeform notes, and the
# per-task activity log. None of those need a backend concept to be useful —
# attachments in particular have nowhere to actually store a file (only
# recordings have an S3 upload pipeline) — and adding them now would be
# persisting client bookkeeping the spec didn't ask for. A task's identity
# (id, task, assignee, due, priority, status, notified_via) is what actually
# needs to survive a reload/second-device; the rest is per-session UI state
# built ON TOP of a task the app already knows how to look up by id.
#
# Routes (action first, key LAST — same API Gateway constraint as documents):
#   GET    /recordings/ai/tasks/{key+}                         -> {tasks}
#   POST   /recordings/ai/tasks/{key+}      {task, ...}         -> {task}
#   PATCH  /recordings/ai/tasks/{key+}      {id, ...}           -> {task}
#   DELETE /recordings/ai/tasks/{key+}      {id}                -> {deleted, id}
#
# Tasks are seeded from the AI's extracted tasks (ai_tasks) on first read
# (get_recording), same as the app
# used to do client-side in meeting-context.tsx's load() — but ONCE,
# server-side, so every device/session sees the same seeded set instead of
# each client re-deriving its own copy with its own local ids.
# ===========================================================================
MAX_TASK_TEXT = 300
MAX_TASK_NOTE_TEXT = 500
MAX_TASKS = 200
# Evidence references per task. One or two segments carry a spoken sentence;
# the cap only stops a runaway model answer from bloating the row.
MAX_EVIDENCE_SEGMENTS = 8
TASK_STATUSES = ("Open", "In Progress", "Completed")
TASK_PRIORITIES = ("Low", "Medium", "High")
NOTIFY_CHANNELS = ("whatsapp", "email", "sms", "app")


def _stored_tasks(item):
    tasks = item.get(TASKS_ATTR)
    return tasks if isinstance(tasks, dict) else {}


def _public_task(task_id, t):
    """One stored task in API shape."""
    assignee = t.get("assignee")
    return {
        "id": task_id,
        "task": t.get("task", ""),
        "due": t.get("due", ""),
        "priority": t.get("priority", "Medium"),
        "status": t.get("status", "Open"),
        "assignee": assignee if isinstance(assignee, dict) else None,
        "notified_via": t.get("notified_via", []),
        "created_at": t.get("created_at", ""),
        "updated_at": t.get("updated_at", ""),
        # Present only when this task was seeded from an AI action_item,
        # so the app can show "detected from meeting transcript" once
        # rather than trying to infer it from the id.
        "from_action_item": bool(t.get("from_action_item")),
    }


def _save_task(key, task_id, t):
    """Write one entry into the tasks map — identical nested-SET-then-
    create-map-if-absent pattern as _save_document, and for the same reason:
    two concurrent task writes (e.g. assign + status change) must not lose
    one to a whole-map read-modify-write. See _save_document's docstring for
    the full DynamoDB-constraint reasoning; it applies verbatim here with
    `tasks` in place of `documents`.
    """
    names = {"#tasks": TASKS_ATTR, "#t": task_id}
    values = {":task": t, ":now": _now_iso()}

    def _set_nested():
        _recordings.update_item(
            Key={"audio_s3_key": key},
            UpdateExpression="SET #tasks.#t = :task, updated_at = :now",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )

    try:
        _set_nested()
        return
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") != "ValidationException" \
                or "invalid for update" not in str(err):
            raise

    try:
        _recordings.update_item(
            Key={"audio_s3_key": key},
            UpdateExpression="SET #tasks = :empty",
            ConditionExpression="attribute_not_exists(#tasks)",
            ExpressionAttributeNames={"#tasks": TASKS_ATTR},
            ExpressionAttributeValues={":empty": {}},
        )
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") \
                != "ConditionalCheckFailedException":
            raise
    _set_nested()


def _seeded_task_from_ai_task(t):
    """One entry from the analysis's ai_tasks field
    ({task, assignee, due_date, priority}, already never-fabricated per
    prompts.SUMMARY_SYSTEM) -> the persisted task shape.

    The only seeding source. The analysis's older `action_items` field was
    removed from the schema, along with the fallback that read it."""
    if not isinstance(t, dict) or not (t.get("task") or "").strip():
        return None
    assignee = (t.get("assignee") or "").strip()
    priority = t.get("priority") or "Medium"
    if priority not in TASK_PRIORITIES:
        priority = "Medium"
    return {
        "task": t["task"].strip()[:MAX_TASK_TEXT],
        "due": (t.get("due_date") or "").strip()[:100],
        "priority": priority,
        "status": "Open",
        "assignee": {"name": assignee, "source": "manual"} if assignee else None,
        "notified_via": [],
        "from_action_item": True,
    }


def _seed_tasks_from_action_items(key, item):
    """First-read seed: turn the AI's extracted tasks into real, id-bearing
    entries in the persisted `tasks` map — once, server-side. Idempotent
    (checks the map is genuinely empty first) so this never re-seeds over
    tasks a user has since edited, reordered, or deleted individually.

    Seeds from `ai_tasks` (the analysis schema: task/assignee/due_date/
    priority) — the single source. The analysis's older `action_items` field
    was removed from the schema, so the fallback that read it is gone too; a
    row old enough to have only `action_items` seeds nothing until it is
    reprocessed, which writes `ai_tasks`.

    Mirrors what meeting-context.tsx used to do client-side on first load
    (taskFromActionItem), except now every session sees the SAME seeded
    tasks with the SAME ids, because it happens once here rather than once
    per client.
    """
    if _stored_tasks(item):
        return item  # already seeded (or user has since created/edited tasks)

    source = item.get("ai_tasks") or []
    if not isinstance(source, list) or not source:
        return item
    builder = _seeded_task_from_ai_task

    now = _now_iso()
    seeded = {}
    for raw in source[:MAX_TASKS]:
        built = builder(raw)
        if built is None:
            continue
        built["created_at"] = now
        built["updated_at"] = now
        seeded[uuid.uuid4().hex[:12]] = built
    if not seeded:
        return item

    try:
        _recordings.update_item(
            Key={"audio_s3_key": key},
            UpdateExpression="SET #tasks = :seeded",
            # Only seed into a map that is STILL empty at write time — a
            # concurrent request (two devices opening the same meeting at
            # once) must not both seed and double the task list.
            ConditionExpression="#tasks = :empty OR attribute_not_exists(#tasks)",
            ExpressionAttributeNames={"#tasks": TASKS_ATTR},
            ExpressionAttributeValues={":seeded": seeded, ":empty": {}},
        )
        item = dict(item)
        item[TASKS_ATTR] = seeded
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") \
                != "ConditionalCheckFailedException":
            raise
        # Someone else seeded (or created tasks) between our read and this
        # write — re-read so the caller sees the real current state.
        fresh = _recordings.get_item(Key={"audio_s3_key": key}).get("Item")
        if fresh:
            item = fresh
    return item


def list_tasks(event):
    """GET /recordings/ai/tasks/{key+} -> {tasks}. Seeds from the AI's
    extracted tasks on first call so the list is never empty for a meeting
    the AI found work in, without every client re-deriving its own copy."""
    _, key, item = _owned_recording(event)
    item = _seed_tasks_from_action_items(key, item)
    stored = _stored_tasks(item)
    tasks = [_public_task(tid, t) for tid, t in stored.items()
              if isinstance(t, dict)]
    tasks.sort(key=lambda t: t.get("created_at", ""))
    return _resp(200, {"tasks": tasks})


def _clean_assignee(raw):
    """Validate a client-supplied assignee — permissive on shape (the app's
    four sources — team/recent/phone/manual — all normalize to the same
    {name, phone?, email?} before sending), strict on types and length so a
    malformed value can't corrupt the stored task."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ApiError(400, "assignee must be an object or null")
    name = str(raw.get("name") or "").strip()[:100]
    if not name:
        raise ApiError(400, "assignee.name required")
    out = {"name": name}
    phone = str(raw.get("phone") or "").strip()[:32]
    email = str(raw.get("email") or "").strip()[:200]
    if phone:
        out["phone"] = phone
    if email:
        out["email"] = email
    source = str(raw.get("source") or "manual").strip().lower()
    out["source"] = source if source in ("team", "recent", "phone", "manual") else "manual"
    return out


def create_task(event):
    """POST /recordings/ai/tasks/{key+} {task, due?, priority?, status?,
    assignee?} -> {task}. Manual task creation — the app also gets tasks for
    free via list_tasks's action_item seeding; this is for a task the user
    adds themselves that the AI never extracted."""
    _, key, item = _owned_recording(event)
    data = _body(event)

    text = str(data.get("task") or "").strip()[:MAX_TASK_TEXT]
    if not text:
        raise ApiError(400, "task required")
    if len(_stored_tasks(_seed_tasks_from_action_items(key, item))) >= MAX_TASKS:
        raise ApiError(400, f"too many tasks (max {MAX_TASKS})")

    priority = str(data.get("priority") or "Medium").strip()
    if priority not in TASK_PRIORITIES:
        priority = "Medium"
    status = str(data.get("status") or "Open").strip()
    if status not in TASK_STATUSES:
        status = "Open"

    now = _now_iso()
    task_id = uuid.uuid4().hex[:12]
    t = {
        "task": text,
        "due": str(data.get("due") or "").strip()[:100],
        "priority": priority,
        "status": status,
        "assignee": _clean_assignee(data.get("assignee")),
        "notified_via": [],
        "created_at": now,
        "updated_at": now,
        "from_action_item": False,
    }
    _save_task(key, task_id, t)
    return _resp(201, {"task": _public_task(task_id, t)})


def update_task(event):
    """PATCH /recordings/ai/tasks/{key+} {id, task?, due?, priority?,
    status?, assignee?, notify_channels?} -> {task}.

    One route for every task mutation (edit/reassign/status-change/record-a-
    notification) rather than one per field — matching the app's own
    updateTask/assignTask/setTaskStatus/recordNotification, which are all
    "patch this task with these fields" at the HTTP boundary regardless of
    how many separate UI actions call them.

    `notify_channels`, when present, is ADDED to the stored set (not
    replaced) — Notify Assignee can be run more than once, and a channel
    already notified should stay marked even if a later call notifies only
    the others.
    """
    _, key, item = _owned_recording(event)
    data = _body(event)

    task_id = str(data.get("id") or "").strip()
    if not task_id:
        raise ApiError(400, "id required")
    item = _seed_tasks_from_action_items(key, item)
    existing = _stored_tasks(item).get(task_id)
    if not existing:
        raise ApiError(404, "task not found")

    t = dict(existing)
    if "task" in data:
        text = str(data.get("task") or "").strip()[:MAX_TASK_TEXT]
        if not text:
            raise ApiError(400, "task cannot be empty")
        t["task"] = text
    if "due" in data:
        t["due"] = str(data.get("due") or "").strip()[:100]
    if "priority" in data:
        priority = str(data.get("priority") or "").strip()
        if priority not in TASK_PRIORITIES:
            raise ApiError(400, "priority must be one of: " + ", ".join(TASK_PRIORITIES))
        t["priority"] = priority
    if "status" in data:
        status = str(data.get("status") or "").strip()
        if status not in TASK_STATUSES:
            raise ApiError(400, "status must be one of: " + ", ".join(TASK_STATUSES))
        t["status"] = status
    if "assignee" in data:
        t["assignee"] = _clean_assignee(data.get("assignee"))
    if "notify_channels" in data:
        raw = data.get("notify_channels")
        if not isinstance(raw, list):
            raise ApiError(400, "notify_channels must be an array")
        channels = [c for c in (str(c).strip().lower() for c in raw)
                   if c in NOTIFY_CHANNELS]
        existing_channels = set(t.get("notified_via") or [])
        t["notified_via"] = sorted(existing_channels | set(channels))

    t["updated_at"] = _now_iso()
    _save_task(key, task_id, t)
    return _resp(200, {"task": _public_task(task_id, t)})


def delete_task(event):
    """DELETE /recordings/ai/tasks/{key+} {id} -> {deleted, id}."""
    _, key, item = _owned_recording(event)
    data = _body(event)
    task_id = str(data.get("id") or "").strip()
    if not task_id:
        raise ApiError(400, "id required")
    item = _seed_tasks_from_action_items(key, item)
    if task_id not in _stored_tasks(item):
        raise ApiError(404, "task not found")

    _recordings.update_item(
        Key={"audio_s3_key": key},
        UpdateExpression="SET updated_at = :now REMOVE #tasks.#t",
        ExpressionAttributeNames={"#tasks": TASKS_ATTR, "#t": task_id},
        ExpressionAttributeValues={":now": _now_iso()},
    )
    return _resp(200, {"deleted": True, "id": task_id})


# ---------------------------------------------------------------------------
# CONTACTS, FOLDERS, PARTICIPANTS, TASKS — the organization layer.
#
# Four ideas, deliberately kept orthogonal:
#
#   Folder   organizes MEETINGS. A recording belongs to zero or one folder
#            (`folder_id` on the recording row; absent == General). Folders are
#            a VIEW over the one master collection — moving a meeting rewrites
#            a single attribute and never copies the row. Deleting a folder
#            never deletes a meeting.
#
#   Contact  is a PERSON, globally unique per owner. Not owned by a folder:
#            "Rahul in Client Alpha" and "Rahul in Product" are the same
#            Contact row reached from two folders.
#
#   FolderContact  many-to-many between the two, uniqueness enforced by the
#            composite primary key rather than by a check.
#
#   MeetingParticipant  maps a diarization speaker label to a Contact FOR ONE
#            MEETING. The transcript keeps its "0"/"1" labels forever — this
#            layer sits beside it, exactly like the existing `speaker_names`
#            map (which stays, and stays authoritative for display names; see
#            _sync_speaker_name_from_contact).
#
#   Task     is first-class and lives in its own table, so it can be queried
#            by assignee / folder / status / due date across every meeting.
#            The recording row's embedded `tasks` map is still written (see
#            _mirror_task_to_recording) and is NOT the read path any more.
#
# WHY the mirror exists: the embedded map is the store this app shipped with.
# Until a backfill has been run and verified against production data, deleting
# it would be an unrecoverable one-way step, and any client build still reading
# it would silently show an empty task list. So every write goes to both, reads
# come from Tasks, and the map is a warm standby that can be dropped in a later
# change once the counts have been confirmed. Section 15 of the spec asks for
# exactly this ordering.
#
# OWNERSHIP: every entity here carries owner_user_id and every route resolves
# it through _owned_folder / _owned_contact / _owned_task, which raise 404
# (never 403) on a miss — identical to _owned_recording, and for the same
# reason: a 403 would confirm that someone else's folder id exists.
# ---------------------------------------------------------------------------

# Field ceilings. Generous but bounded — these are display strings, and an
# unbounded write is how a single row grows past DynamoDB's 400KB item limit.
CONTACT_NAME_MAX = 120
CONTACT_EMAIL_MAX = 254        # RFC 5321 maximum path length
CONTACT_PHONE_MAX = 32
CONTACT_COMPANY_MAX = 120
CONTACT_ROLE_MAX = 80
CONTACT_NOTES_MAX = 500
PARTICIPANT_ROLE_MAX = 80

# ---------------------------------------------------------------------------
# Speaker IDENTITY classification for CRM (Phase 2D.3) — deliberately a
# SEPARATE field from participant_role above, not a repurposing of it.
# participant_role is 80-char free text a user types ("Site Manager",
# "Buyer's Agent") with no enum semantics MinuteX code branches on; this is
# the CRM-facing question "does this speaker's identity resolve through the
# organisation (a MinuteX member) or outside it (an organisation Contact)?"
# — the fork every downstream Salesforce identity-resolution function needs
# an authoritative answer to. Absent/"" means "not yet classified", which is
# an ORDINARY resting state (most speakers on most meetings are never pushed
# to Salesforce at all), never coerced to a guess.
# ---------------------------------------------------------------------------
SPEAKER_IDENTITY_INTERNAL = "internal"
SPEAKER_IDENTITY_EXTERNAL = "external"
SPEAKER_IDENTITY_ROLES = (SPEAKER_IDENTITY_INTERNAL, SPEAKER_IDENTITY_EXTERNAL)

# Page sizes for the list routes. A mobile client never needs more in one
# screen, and an unbounded response is how a large account times out.
CONTACTS_PAGE_DEFAULT = 50
CONTACTS_PAGE_MAX = 200
TASKS_PAGE_DEFAULT = 50
TASKS_PAGE_MAX = 200

# Task vocabulary. The three legacy statuses the embedded map has always used
# stay EXACTLY as they are ("Open"/"In Progress"/"Completed") because existing
# rows and the shipped app both speak them; CANCELLED is added because the spec
# requires a terminal non-completed state. Sending the spec's uppercase form is
# also accepted (see _clean_task_status) so a client written against either
# vocabulary works.
TASK_STATUS_OPEN = "Open"
TASK_STATUS_IN_PROGRESS = "In Progress"
TASK_STATUS_COMPLETED = "Completed"
TASK_STATUS_CANCELLED = "Cancelled"
TASK_STATUSES_V2 = (TASK_STATUS_OPEN, TASK_STATUS_IN_PROGRESS,
                    TASK_STATUS_COMPLETED, TASK_STATUS_CANCELLED)
# The states that stop a task being "overdue" no matter what its due date says.
TASK_TERMINAL_STATUSES = (TASK_STATUS_COMPLETED, TASK_STATUS_CANCELLED)

# Alias table: accept the spec's SCREAMING_SNAKE vocabulary and the app's
# Title Case, store the Title Case form. Kept as one map so there is exactly
# one place where the two vocabularies meet.
_TASK_STATUS_ALIASES = {
    "open": TASK_STATUS_OPEN,
    "in_progress": TASK_STATUS_IN_PROGRESS,
    "in progress": TASK_STATUS_IN_PROGRESS,
    "inprogress": TASK_STATUS_IN_PROGRESS,
    "completed": TASK_STATUS_COMPLETED,
    "complete": TASK_STATUS_COMPLETED,
    "done": TASK_STATUS_COMPLETED,
    "cancelled": TASK_STATUS_CANCELLED,
    "canceled": TASK_STATUS_CANCELLED,
}

# How a task's assignee came to be what it is. This is the field that keeps the
# system honest about identity: UNRESOLVED means "the AI (or a legacy row) gave
# us a NAME and we refused to guess which Contact it meant". Nothing downstream
# treats an unresolved assignee as a person.
RESOLUTION_RESOLVED = "RESOLVED"
RESOLUTION_UNRESOLVED = "UNRESOLVED"
RESOLUTION_AMBIGUOUS = "AMBIGUOUS"
RESOLUTION_NONE = "NONE"          # no assignee at all — not a failure state
RESOLUTION_STATUSES = (RESOLUTION_RESOLVED, RESOLUTION_UNRESOLVED,
                       RESOLUTION_AMBIGUOUS, RESOLUTION_NONE)

# Where a task came from. AI tasks are fingerprinted for idempotency; MANUAL
# ones never are (a user is allowed to create two identical tasks on purpose).
TASK_SOURCE_AI = "AI"
TASK_SOURCE_MANUAL = "MANUAL"
TASK_SOURCE_LEGACY = "LEGACY"     # migrated out of the embedded map
TASK_SOURCES = (TASK_SOURCE_AI, TASK_SOURCE_MANUAL, TASK_SOURCE_LEGACY)

_contacts = _ddb.Table(CONTACTS_TABLE)
_meeting_participants = _ddb.Table(MEETING_PARTICIPANTS_TABLE)
_tasks = _ddb.Table(TASKS_TABLE)


# ---------------------------------------------------------------------------
# Normalization. Every dedupe decision in this file rests on these three
# functions, so they are written to be boring and total: same input always
# gives the same output, and an unusable input gives "" rather than raising.
# ---------------------------------------------------------------------------
def _norm_email(raw):
    """Lowercased, trimmed email — or "" if it isn't one.

    Case-insensitive because that is how mail actually works (the domain is
    definitionally case-insensitive and no real provider distinguishes local
    parts). This is the value stored in `email_lc` and used as the GSI key, so
    two spellings of one address can never become two contacts.

    Deliberately NOT doing provider-specific canonicalization (stripping dots
    or +tags the way Gmail does): that is a Gmail rule, not an email rule, and
    applying it globally would merge two genuinely different addresses on
    providers where the local part is significant.
    """
    email = str(raw or "").strip().lower()[:CONTACT_EMAIL_MAX]
    return email if email and _EMAIL_RE.match(email) else ""


def _norm_phone(raw):
    """Phone reduced to comparable digits, or "".

    Keeps a leading + and strips every separator, so "+91 98765 43210",
    "+919876543210" and "+91-98765-43210" all collapse to one value. This is
    NOT full E.164 validation — we have no country context to expand a local
    number with, and guessing one would be exactly the kind of silent identity
    inference section 5 forbids. A bare 10-digit local number therefore stays
    distinct from the same number written internationally, which is the safe
    direction to fail: two contacts the user can merge by hand, rather than one
    contact wrongly fused from two people.
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if len(digits) < 7:      # too short to identify anyone
        return ""
    return ("+" if plus else "") + digits[:CONTACT_PHONE_MAX]


def _norm_name(raw):
    """Collapsed-whitespace, casefolded name for COMPARISON only.

    Used to detect that two contacts might be the same person — never to
    decide that they are. See _match_contacts: a name match alone is always
    reported as ambiguous, never auto-merged.
    """
    return re.sub(r"\s+", " ", str(raw or "").strip()).casefold()


# ---------------------------------------------------------------------------
# Contacts — public shape + ownership
# ---------------------------------------------------------------------------
def _public_contact(item, linked_avatars=None, member_roles=None):
    """One Contact row in API shape.

    `member_roles` is {user_id: ROLE} for the ACTIVE members of the workspace
    being listed (see _member_role_map). When this contact is one of them,
    the response carries their live `workspace_role` — the tag the picker
    renders next to the name. Omitted or unmatched leaves the field "", which
    means "not a colleague", never "role unknown".

    `minutex_user_id` is present only when this contact has been matched to a
    real MinuteX account (see _resolve_minutex_user). It is what makes a task
    notification-ready, so it is never fabricated: absent means "we have no
    account for this person", which the app must treat as "cannot notify
    in-app", not as "not yet looked up".

    For a LIST of contacts call _public_contacts() rather than mapping this
    over them — it batches the linked-avatar lookup into one read.
    """
    out = {
        "id": item.get("contact_id", ""),
        "name": item.get("name", ""),
        "email": item.get("email", ""),
        "phone": item.get("phone", ""),
        "company": item.get("company", ""),
        "role": item.get("role", ""),
        "notes": item.get("notes", ""),
        "minutex_user_id": item.get("minutex_user_id", ""),
        # WHERE this contact lives (Phase 2C). Resolved rather than copied
        # raw, so a pre-Phase-2C row reads as its owner's personal workspace
        # instead of "". The app uses `shared` to label a shared entry and to
        # decide whether to offer Edit/Delete — presentation only; the backend
        # enforces the rule in _owned_contact.
        "workspace_id": _row_workspace_id(item,
                                          owner_field="owner_user_id"),
        "shared": workspace_schema.is_organisation_workspace_id(
            _row_workspace_id(item, owner_field="owner_user_id")),
        # Provenance. "manual" today; CRM-imported contacts will carry their
        # own source once that integration lands, and the field exists now so
        # the UI does not have to change shape then.
        "source": item.get("source", "manual"),
        "created_by": item.get("created_by", ""),
        "created_at": item.get("created_at", ""),
        "updated_at": item.get("updated_at", ""),
        # CRM identity (Phase 2D.3). Present only once EXPLICITLY confirmed
        # via resolve_speaker_crm_identity — never inferred from name/email
        # here, so "crm_external_id" being set is always the record of a
        # deliberate human choice (see that route's docstring for the
        # matching-priority rule this guarantees).
        "crm_provider": item.get("crm_provider", ""),
        "crm_object_type": item.get("crm_object_type", ""),
        "crm_external_id": item.get("crm_external_id", ""),
        "crm_account_id": item.get("crm_account_id", ""),
        "crm_resolved_at": item.get("crm_resolved_at", ""),
    }
    # THE ROLE TAG. Keyed on the LINKED MinuteX account, not on the contact's
    # owner: the point is "this person is a colleague, and this is their role
    # in this organisation", which is a fact about the person, not about who
    # typed them in. So a hand-added contact who turns out to be a member
    # gets the tag too — which is exactly the case a colleague added by hand
    # before this shipped.
    linked_user = str(item.get("minutex_user_id") or "")
    out["workspace_role"] = str((member_roles or {}).get(linked_user) or "")
    # A member's directory entry is maintained by that member (their profile
    # name and photo), so nobody else may edit or delete it. Surfaced so the
    # app can hide the affordances rather than offer a button that 403s.
    out["is_member"] = bool(out["workspace_role"]) and (
        item.get("source") == CONTACT_SOURCE_MEMBER
        or _is_member_contact(out["id"]))
    out.update(_contact_avatar_fields(item, linked_avatars))
    return out


# Which photo a contact shows, and where it came from. Precedence is
# deliberate and is the answer to "whose picture is this really":
#
#   1. The OWN photo on the contact row — set by the owner, either imported
#      from their phone's address book or picked by hand. It is the one the
#      owner chose, so it wins.
#   2. The linked MinuteX user's own profile photo, when this contact has a
#      minutex_user_id and no photo of its own. That person maintains it, so
#      it is fresher than anything cached here and updates itself.
#   3. Nothing — the app falls back to coloured initials, as it always has.
#
# `avatar_source` is returned alongside the URL rather than left implicit
# because the two cases are not interchangeable to a user: "the photo you
# saved" and "their MinuteX profile photo" differ in who can change it, and
# the contact screen says so.
AVATAR_SOURCE_NONE = ""
AVATAR_SOURCE_OWN = "own"
AVATAR_SOURCE_MINUTEX = "minutex"


def _contact_avatar_fields(item, linked_avatars=None):
    """The avatar_* fields for one contact, applying the precedence above.

    `linked_avatars` maps minutex_user_id -> that user's stored avatar key, and
    is what keeps a 50-contact page from doing 50 Users reads. Callers that
    render a list build it once with _linked_avatar_map; single-contact callers
    pass nothing and take the one lookup.
    """
    own = str(item.get("avatar_url") or "").strip()
    if own:
        return {"avatar_url": own,
                "avatar_view_url": _avatar_view_url(own),
                "avatar_source": AVATAR_SOURCE_OWN}

    linked_id = str(item.get("minutex_user_id") or "").strip()
    if linked_id:
        if linked_avatars is None:
            linked_avatars = _linked_avatar_map([linked_id])
        linked_key = str(linked_avatars.get(linked_id) or "").strip()
        if linked_key:
            return {"avatar_url": "",
                    "avatar_view_url": _avatar_view_url(linked_key),
                    "avatar_source": AVATAR_SOURCE_MINUTEX}

    return {"avatar_url": "", "avatar_view_url": "",
            "avatar_source": AVATAR_SOURCE_NONE}


def _linked_avatar_map(user_ids):
    """{user_id: avatar_url key} for MinuteX users linked to these contacts.

    This is a deliberate CROSS-ACCOUNT read: the rows belong to other users,
    not to the caller. It is narrow on purpose — it projects `avatar_url` and
    `user_id` and nothing else, so no name, email or account state of another
    user can leak through this path, and it is only ever reached for a user id
    that the owner's own contact row already carries (i.e. someone they have
    already matched by email). Showing a MinuteX user's profile photo to
    someone who has them as a contact is the point of the feature; anything
    beyond the photo is not.

    Best-effort: any failure yields {} and every contact falls back to
    initials rather than failing the list route.
    """
    ids = {str(u).strip() for u in (user_ids or []) if str(u or "").strip()}
    if not ids or not USERS_TABLE:
        return {}
    out = {}
    try:
        ids = list(ids)
        # BatchGetItem caps at 100 keys per call.
        for start in range(0, len(ids), 100):
            chunk = ids[start:start + 100]
            resp = _ddb.batch_get_item(RequestItems={USERS_TABLE: {
                "Keys": [{"user_id": u} for u in chunk],
                "ProjectionExpression": "user_id, avatar_url",
            }})
            for row in resp.get("Responses", {}).get(USERS_TABLE, []):
                key = str(row.get("avatar_url") or "").strip()
                if key:
                    out[row.get("user_id")] = key
    except Exception:
        return out
    return out


def _public_contacts(items, member_roles=None):
    """Many contacts in API shape, with ONE batched lookup for linked photos.

    Use this instead of a [_public_contact(c) for c in ...] comprehension on
    any route that returns a list — the comprehension would do a Users read per
    contact that has a linked account.

    `member_roles` is passed straight through to _public_contact, so the role
    tag costs the caller ONE membership query for the whole page rather than
    one per contact. Callers serving a personal workspace pass nothing.
    """
    items = list(items or [])
    linked = _linked_avatar_map([
        c.get("minutex_user_id") for c in items
        if not str(c.get("avatar_url") or "").strip()
    ])
    return [_public_contact(c, linked, member_roles) for c in items]


def _roles_for_contact(item):
    """The member_roles map needed to tag ONE contact, or None.

    A single-contact route has no page to amortize a membership query over,
    so this keeps the cost to the case that can actually carry a tag: a row
    in an ORGANISATION workspace that is linked to a MinuteX account. A
    personal contact, or one with no linked account, cannot be a colleague
    and skips the read entirely.
    """
    if not str(item.get("minutex_user_id") or ""):
        return None
    wid = _row_workspace_id(item, owner_field="owner_user_id")
    if not _contact_scope_is_org(wid):
        return None
    return _member_role_map(wid)


def _owned_contact(user_id, contact_id, *, write=False):
    """The Contact row, or 404/403. THE single contact authorization gate.

    PERSONAL contacts (the default, and every row written before Phase 2C):
    unchanged — the owner and nobody else, 404 for everyone.

    ORGANISATION contacts (Phase 2C): SHARED workspace resources.

        READ / CREATE   every ACTIVE member
        UPDATE / DELETE OWNER and MANAGER only

    `write=True` asks for the mutating right; the default asks only to read.
    Callers that mutate must pass it — the deny-by-default direction, so a
    new mutating route that forgets is refused rather than silently allowed.

    WHY 403 AND NOT 404 FOR A MEMBER WHO MAY ONLY READ: they can already see
    the contact, so 404 would be a lie that makes the UI unexplainable. 404
    still hides existence from non-members, exactly as before.

    THE MEMBERSHIP GATE COMES FIRST for organisation rows, for the same
    reason it does on meetings (the Phase 2B P0): a member removed from the
    organisation must lose access even to contacts they created themselves.
    """
    cid = str(contact_id or "").strip()
    if not cid:
        raise ApiError(400, "contact id required")
    item = _contacts.get_item(Key={"contact_id": cid}).get("Item")
    if not item:
        # A PROJECTED colleague (Phase 2D) has no stored row until somebody
        # tags them, so a plain get_item is not the whole answer for a
        # member- id. Resolve it from live membership instead of answering
        # 404 for a person the picker just listed.
        #
        # READ-ONLY, and it deliberately does NOT write: materializing is
        # the tagging paths' job (_ensure_member_contact), so a GET can
        # never populate the address book as a side effect.
        projected = _projected_member_contact(user_id, cid)
        if projected is None:
            raise ApiError(404, "contact not found")
        if write:
            # A member's own profile is the source of their directory entry;
            # nobody else may rewrite it here. Same 403 a member-only role
            # gets on a shared contact — they can see it, so 404 would be a
            # lie.
            raise ApiError(403, "this contact is an organisation member; "
                                "their name and photo come from their own "
                                "profile")
        return projected

    workspace_id = _row_workspace_id(item, owner_field="owner_user_id")
    if workspace_schema.is_organisation_workspace_id(workspace_id):
        role = _workspace_role(workspace_id, user_id)
        if not role:
            # Not (or no longer) a member of the owning organisation. Being
            # the creator does not rescue this.
            raise ApiError(404, "contact not found")
        if write and not workspace_schema.role_can(
                role, workspace_schema.CAP_MANAGE_CONTACTS):
            raise ApiError(403, "only an owner or manager can change "
                                "organisation contacts")
        return item

    # PERSONAL — the original rule, byte for byte.
    if item.get("owner_user_id") != user_id:
        # 404 not 403 — see this section's header.
        raise ApiError(404, "contact not found")
    return item


def _taggable_contact(user_id, contact_id, workspace_id=""):
    """The contact to STORE a reference to, materializing a member first.

    THE ONE ENTRY POINT for every path that persists a contact_id as a
    foreign key — a participant mapping, a task assignee. Those rows outlive
    the request, so a projected colleague (Phase 2D) has to become a real row
    before its id is written anywhere; otherwise the reference dangles and
    every later hydration answers 404.

    Materializing is idempotent, so calling this on a colleague who has
    already been tagged is a read. Ordinary contacts fall straight through to
    the unchanged _owned_contact gate.
    """
    cid = str(contact_id or "").strip()
    if _is_member_contact(cid):
        wid = str(workspace_id or "").strip()
        if not _contact_scope_is_org(wid):
            # No workspace in hand (an older client, or a personal-workspace
            # request): find the organisation the two share, exactly as
            # _owned_contact does for a read.
            projected = _projected_member_contact(user_id, cid)
            wid = str((projected or {}).get("workspace_id") or "")
        row = _ensure_member_contact(user_id, cid, workspace_id=wid)
        if row:
            return row
        # Fall through: not a resolvable member (removed from the
        # organisation between the picker listing them and this tap), so the
        # standard gate answers 404 rather than this inventing a row.
    return _owned_contact(user_id, cid)


def _readable_contact(user_id, contact_id):
    """The contact if this caller may READ it, else None. Never raises.

    The soft twin of _owned_contact, for the HYDRATION paths — the places
    that decorate something else (a participant row, an email recipient list,
    a task card) with contact details and must skip a contact they cannot
    read rather than fail the whole request.

    Those call sites used to compare `owner_user_id == user_id` inline. That
    is still correct for a personal contact and became INCOMPLETE for a
    shared organisation one: a manager hydrating a participant mapped to a
    colleague's shared contact would silently get no name. Fail-closed, so
    never a leak — but the shared address book is meant to be shared.

    Delegates to _owned_contact so there is ONE contact authorization rule,
    and swallows its ApiError because "not readable" is an ordinary outcome
    here rather than a request-ending one.
    """
    if not contact_id:
        return None
    try:
        return _owned_contact(user_id, contact_id)
    except ApiError:
        return None


def _resolve_minutex_user(email_lc):
    """The MinuteX user_id for an email, or "".

    This is the ONLY honest source of `assignee_user_id` in the system: there
    is no team/org directory, so "is this contact also a MinuteX user" can only
    be answered by looking their email up in the Users table via its
    email-index GSI (the same index login uses). No email -> no answer, which
    is why a contact with only a phone number is never notification-ready.
    """
    if not email_lc:
        return ""
    try:
        res = _users.query(
            IndexName=EMAIL_INDEX,
            KeyConditionExpression=Key("email").eq(email_lc),
            Limit=1,
        )
    except ClientError as err:
        # A missing index must not take down contact creation — the contact is
        # still perfectly valid without a linked account.
        print(f"[warn] minutex user lookup failed for {email_lc}: {err}")
        return ""
    items = res.get("Items", [])
    return items[0].get("user_id", "") if items else ""


# ---------------------------------------------------------------------------
# CONTACT LOOKUP SCOPE (Phase 2C).
#
# Every helper below takes an OPTIONAL workspace_id. When it names an
# ORGANISATION the lookup is partitioned by the WORKSPACE — because an
# organisation address book is shared, and "does this organisation already
# have this client" cannot be answered from one member's namespace. Two
# colleagues adding the same person would otherwise each get a row and
# create_contact would report 201 "created" rather than 200 "existing".
#
# Omitted or personal -> the original owner-partitioned path, unchanged. That
# is also what serves every pre-Phase-2C row, since those carry no
# workspace_id and are absent from the sparse workspace indexes.
# ---------------------------------------------------------------------------
def _contact_scope_is_org(workspace_id):
    return workspace_schema.is_organisation_workspace_id(workspace_id)


def _find_contact_by_email(user_id, email_lc, workspace_id=""):
    """Exact-email match within this owner's (or workspace's) namespace."""
    if not email_lc:
        return None
    if _contact_scope_is_org(workspace_id):
        res = _contacts.query(
            IndexName=CONTACTS_WORKSPACE_EMAIL_INDEX,
            KeyConditionExpression=Key("workspace_id").eq(workspace_id)
                                   & Key("email_lc").eq(email_lc),
            Limit=1,
        )
    else:
        res = _contacts.query(
            IndexName=CONTACTS_EMAIL_INDEX,
            KeyConditionExpression=Key("owner_user_id").eq(user_id)
                                   & Key("email_lc").eq(email_lc),
            Limit=1,
        )
    items = res.get("Items", [])
    return items[0] if items else None


def _find_contact_by_phone(user_id, phone_e164, workspace_id=""):
    """Exact-phone match within this owner's (or workspace's) namespace.

    NOTE the asymmetry with email: there is no workspace-phone index, so an
    organisation phone lookup filters the workspace page in memory instead.
    Deliberate — phone is the WEAKER of the two strong identifiers and the
    less common input, so it does not justify a fourth Contacts GSI on the
    hot create path. Bounded by CONTACTS_PAGE_MAX exactly as the name path is.
    """
    if not phone_e164:
        return None
    if _contact_scope_is_org(workspace_id):
        for row in _all_contacts(user_id, workspace_id=workspace_id):
            if row.get("phone_e164") == phone_e164:
                return row
        return None
    res = _contacts.query(
        IndexName=CONTACTS_PHONE_INDEX,
        KeyConditionExpression=Key("owner_user_id").eq(user_id)
                               & Key("phone_e164").eq(phone_e164),
        Limit=1,
    )
    items = res.get("Items", [])
    return items[0] if items else None


def _all_contacts(user_id, limit=CONTACTS_PAGE_MAX, workspace_id=""):
    """This owner's — or this organisation's — contacts. Bounded: the
    name-matching paths below need to compare against the set, and an
    unbounded read of a large account inside a 29s request is not something to
    leave lying around."""
    if _contact_scope_is_org(workspace_id):
        res = _contacts.query(
            IndexName=CONTACTS_WORKSPACE_INDEX,
            KeyConditionExpression=Key("workspace_id").eq(workspace_id),
            ScanIndexForward=False,
            Limit=limit,
        )
    else:
        res = _contacts.query(
            IndexName=CONTACTS_OWNER_INDEX,
            KeyConditionExpression=Key("owner_user_id").eq(user_id),
            ScanIndexForward=False,
            Limit=limit,
        )
    return res.get("Items", [])


# ---------------------------------------------------------------------------
# ORGANISATION MEMBERS IN THE ADDRESS BOOK (Phase 2D).
#
# THE REQUIREMENT: a colleague must be taggable in ANY meeting run by ANY
# member of the organisation, without anybody hand-typing them, and must be
# visibly labelled with their role.
#
# WHY THIS IS A PROJECTION AND NOT A COPY. The obvious implementation writes
# a Contact row when an invitation is accepted. It is the wrong one here:
#
#   1. It cannot serve the members who joined BEFORE this shipped, so it
#      needs a backfill that then has to be kept in step forever.
#   2. It goes STALE. A member who renames themselves, sets a profile photo,
#      or is promoted to MANAGER would keep the name/role frozen at the
#      moment they joined — and a role tag that lies is worse than no tag.
#   3. accept_invitation is a three-step conditional claim with a
#      compensating rollback (see its ATOMICITY block). A fourth write in
#      there either has to join that compensation or is allowed to fail
#      silently, and neither is acceptable.
#
# So membership stays THE single source of truth and the address book reads
# from it. _member_contacts projects every ACTIVE member of the active
# organisation into contact shape on the READ path, and a row is only
# MATERIALIZED (written) when someone is actually tagged — see
# _ensure_member_contact, called from the paths that need a real contact_id
# foreign key (participant mapping, task assignment).
#
# IDENTITY IS THE EMAIL, the same identity create_contact already dedupes
# on. That is what makes a projected member and a hand-typed contact for the
# same person converge on ONE row instead of two: whoever gets there first
# owns the row, and the projection recognises it by email.
#
# THE ROLE TAG IS NEVER STORED ON THE CONTACT. It is resolved from the live
# membership every time a contact is rendered (see _member_role_map), so a
# promotion shows up immediately and a REMOVED member's tag disappears
# instead of lingering in the address book as a former colleague.
# ---------------------------------------------------------------------------

# Marks a contact row that exists BECAUSE the person is an organisation
# member, rather than because someone added them. Kept distinct from
# "manual" so the app can explain why the row is not editable, and so a
# future directory sweep can find exactly these rows.
CONTACT_SOURCE_MEMBER = "member"

# Prefix of the derived contact id for a member. Reserved: create_contact
# never mints one, so a stored row carrying it can only have come from
# _ensure_member_contact.
MEMBER_CONTACT_PREFIX = "member-"


def _member_contact_id(user_id):
    """The reserved, derived contact id for an organisation member.

    Derived rather than random for the same reason _self_speaker_id is:
    materializing the row twice (two colleagues tagging the same person at
    the same moment) lands on ONE primary key and overwrites, instead of
    leaving two rows for one person.
    """
    return f"{MEMBER_CONTACT_PREFIX}{str(user_id or '').strip()}"


def _is_member_contact(contact_id):
    return str(contact_id or "").startswith(MEMBER_CONTACT_PREFIX)


def _active_members(workspace_id):
    """Every ACTIVE membership row in an organisation. [] for personal.

    Personal short-circuits without a read: a personal workspace has exactly
    one member (its owner), and projecting yourself into your own address
    book as a contact is not something anybody asked for.
    """
    wid = str(workspace_id or "").strip()
    if not _contact_scope_is_org(wid):
        return []
    try:
        rows = _query_all(_memberships,
                          KeyConditionExpression=Key("workspace_id").eq(wid))
    except ClientError as err:
        # A membership read failure must not empty the address book — the
        # stored contacts are still perfectly serveable. The colleagues
        # simply do not appear on this response.
        print(f"[workspace] member projection failed for {wid}: {err}")
        return []
    return [r for r in rows
            if r.get("status") == workspace_schema.MEMBERSHIP_ACTIVE]


def _member_role_map(workspace_id):
    """{user_id: ROLE} for the ACTIVE members of an organisation.

    The input to the role TAG on every contact. Resolved live rather than
    read off the contact row, so it cannot go stale (see the header above).
    """
    return {str(r.get("user_id") or ""): str(r.get("role") or "")
            for r in _active_members(workspace_id)
            if str(r.get("user_id") or "")}


def _member_contact_projection(membership, user, workspace_id):
    """One ACTIVE member in stored-Contact shape. NOT persisted.

    Every field comes from the member's OWN profile: their name and photo
    are theirs to maintain, and a colleague must not be able to rename them
    in the shared book.
    """
    uid = str(membership.get("user_id") or "")
    profile = user or {}
    email_lc = _norm_email(profile.get("email"))
    name = workspace_schema.display_name(profile, fallback_user_id=uid)
    return {
        "contact_id": _member_contact_id(uid),
        # The MEMBER owns their own directory entry. Nobody else's user_id
        # may appear here: _owned_contact would then let that person edit a
        # colleague's entry as if it were their own personal contact.
        "owner_user_id": uid,
        "workspace_id": workspace_id,
        "created_by": uid,
        "name": name,
        "name_lc": _norm_name(name),
        "email": email_lc,
        "phone": "",
        "company": "",
        "role": "",
        "notes": "",
        "avatar_url": str(profile.get("avatar_url") or ""),
        "source": CONTACT_SOURCE_MEMBER,
        # The whole point of the projection: this person IS a MinuteX
        # account, so they are notification-ready with no email lookup.
        "minutex_user_id": uid,
        "created_at": str(membership.get("joined_at")
                          or membership.get("created_at") or ""),
        "updated_at": str(membership.get("updated_at")
                          or membership.get("created_at") or ""),
    }


def _member_contacts(user_id, workspace_id):
    """Every colleague in this organisation, in stored-row shape.

    Excludes the CALLER: "contacts" means other people, and a user tagging
    themselves already has a dedicated path (allowSelf on the picker,
    _self_contact_id server-side), which is what keeps self-tagging to one
    code path.

    One BatchGetItem for the profiles, never a read per member — the same
    N+1 rule list_members follows.
    """
    members = _active_members(workspace_id)
    if not members:
        return []
    others = [m for m in members if str(m.get("user_id") or "") != user_id]
    if not others:
        return []
    users = _users_by_ids([m.get("user_id", "") for m in others])
    out = []
    for m in others:
        profile = users.get(str(m.get("user_id") or ""))
        # No profile row means the account is gone (or the batched read
        # dropped it). Skip rather than project a person with no name and no
        # email: an unnameable, un-notifiable picker entry is worse than one
        # absent colleague.
        if not profile or not _norm_email(profile.get("email")):
            continue
        out.append(_member_contact_projection(m, profile, workspace_id))
    return out


def _merge_member_contacts(stored, user_id, workspace_id):
    """Stored contacts + projected colleagues, ONE row per person.

    The STORED row wins whenever both exist for the same email: it carries
    the phone number, company and notes somebody took the trouble to enter,
    and its contact_id is the one already referenced by participant and task
    rows. The projection only supplies the colleagues who have no row yet.

    Colleagues come FIRST. They are the people a member assigns work to most
    often — the same reasoning that already orders the picker's meeting tier
    ahead of everyone else.
    """
    projected = _member_contacts(user_id, workspace_id)
    if not projected:
        return list(stored)
    have = {_norm_email(row.get("email")) for row in stored
            if _norm_email(row.get("email"))}
    # A MATERIALIZED member row is in `stored` and matches by email, so this
    # can never double-count one person.
    fresh = [p for p in projected if p["email"] not in have]
    return fresh + list(stored)


def _projected_member_contact(user_id, contact_id):
    """A member- contact id resolved from live membership, or None.

    Serves the window between "the picker listed this colleague" and "somebody
    tagged them", during which the row genuinely does not exist yet. Read-only.

    THE SHARED ORGANISATION IS FOUND, NOT SUPPLIED. _owned_contact is reached
    from ~24 call sites, most of which have no workspace header to consult
    (a hydration path decorating a task card, for instance), so requiring one
    would make a colleague's name resolve on some screens and not others.
    Instead: both parties must be ACTIVE members of the SAME organisation,
    which is the same predicate the header would have been checked against —
    so the header's absence costs nothing but a membership listing.

    Returns None (never raises) when the id is not a member projection, the
    target is not a real account, or the two share no organisation.
    """
    cid = str(contact_id or "").strip()
    if not _is_member_contact(cid):
        return None
    target_id = cid[len(MEMBER_CONTACT_PREFIX):]
    if not target_id or target_id == user_id:
        # Self is deliberately not projected — see _member_contacts.
        return None

    # Every organisation the CALLER is in, established first: nothing about
    # the target is read until the caller's own standing is known.
    try:
        rows = _query_all(_memberships, IndexName=MEMBERSHIPS_USER_INDEX,
                          KeyConditionExpression=Key("user_id").eq(user_id))
    except ClientError as err:
        print(f"[workspace] member contact resolve failed for {user_id}: {err}")
        return None
    mine = [str(r.get("workspace_id") or "") for r in rows
            if r.get("status") == workspace_schema.MEMBERSHIP_ACTIVE]

    for wid in mine:
        if not _contact_scope_is_org(wid):
            continue
        # Re-checked through _active_membership rather than trusted from the
        # index, so a stale index entry cannot grant a read (the same rule
        # _user_workspaces follows).
        if not _active_membership(wid, user_id):
            continue
        membership = _active_membership(wid, target_id)
        if not membership:
            continue
        profile = _users_by_ids([target_id]).get(target_id)
        if not profile or not _norm_email(profile.get("email")):
            return None
        return _member_contact_projection(membership, profile, wid)
    return None


def _ensure_member_contact(user_id, contact_id, workspace_id=""):
    """Materialize a projected member into a REAL Contacts row, or None.

    Called from the paths that need a genuine contact_id foreign key — a
    participant mapping, a task assignee — because those STORE the id and
    must still resolve it long after the request ends. A projection has no
    row behind it, so _owned_contact would answer 404 for one.

    IDEMPOTENT AND RACE-SAFE: the id is derived and the write is conditional
    on the row's absence, so two members tagging the same colleague at once
    produce one row and the loser simply reads it.

    Returns None when the id is not a member projection, when the CALLER is
    not a member of the workspace, or when the TARGET is not an ACTIVE
    member of it — so this can never conjure a contact row for somebody
    outside the organisation.
    """
    cid = str(contact_id or "").strip()
    if not _is_member_contact(cid):
        return None
    wid = str(workspace_id or "").strip()
    if not _contact_scope_is_org(wid):
        return None
    # The CALLER must be in the organisation, checked before anything about
    # the target is read — an outsider must not learn who is in it.
    if not _active_membership(wid, user_id):
        return None

    target_id = cid[len(MEMBER_CONTACT_PREFIX):]
    membership = _active_membership(wid, target_id)
    if not membership:
        return None
    profile = _users_by_ids([target_id]).get(target_id)
    if not profile or not _norm_email(profile.get("email")):
        return None

    # Someone may have hand-added this colleague already. That row is the
    # canonical one (see _merge_member_contacts), so use it rather than
    # creating a second row for one person.
    email_lc = _norm_email(profile.get("email"))
    existing = _find_contact_by_email(user_id, email_lc, workspace_id=wid)
    if existing:
        return existing

    item = _member_contact_projection(membership, profile, wid)
    now = _now_iso()
    item["created_at"] = item["created_at"] or now
    item["updated_at"] = now
    # email_lc is the GSI key attribute; the projection carries the display
    # `email` only. Set here rather than in the projection so an
    # UNPERSISTED projection never looks like an indexed row.
    item["email_lc"] = email_lc
    try:
        _contacts.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(contact_id)")
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") \
                != "ConditionalCheckFailedException":
            raise
        # Lost the race — the other writer's row is the real one.
        return _contacts.get_item(Key={"contact_id": cid}).get("Item")
    _audit("contact.member_synced", user_id, cid, workspace=wid)
    return item


def _match_contacts(user_id, name="", email="", phone="", workspace_id=""):
    """Find the contact(s) a (name, email, phone) triple could refer to.

    Returns (status, matches):
      ("exact",     [one])   a STRONG identifier matched — email or phone.
      ("ambiguous", [n>=1])  only the NAME matched. Never treated as identity.
      ("none",      [])      nothing matched.

    This is the function that implements section 5's core rule, so the ordering
    matters: strong identifiers first, and a name match NEVER upgrades to
    "exact" no matter how confident it looks. Two people genuinely called
    "Rahul Sharma" is an ordinary situation, and silently assigning one
    person's task to the other is the failure this exists to prevent — hence a
    single name match is still reported as ambiguous, not as a hit.
    """
    email_lc = _norm_email(email)
    phone_e164 = _norm_phone(phone)

    if email_lc:
        hit = _find_contact_by_email(user_id, email_lc,
                                     workspace_id=workspace_id)
        if hit:
            return "exact", [hit]
    if phone_e164:
        hit = _find_contact_by_phone(user_id, phone_e164,
                                     workspace_id=workspace_id)
        if hit:
            return "exact", [hit]

    wanted = _norm_name(name)
    if not wanted:
        return "none", []
    matches = [c for c in _all_contacts(user_id, workspace_id=workspace_id)
               if _norm_name(c.get("name")) == wanted]
    if not matches:
        return "none", []
    return "ambiguous", matches


def _contact_item(user_id, name, email="", phone="", company="", role="",
                  notes="", avatar_url="", workspace_id=""):
    """Build a Contact row. Optional GSI key attributes (email_lc, phone_e164)
    are OMITTED when empty rather than written as "" — DynamoDB rejects an
    empty-string index key outright, so writing one would fail the whole put.
    Same rule the Recordings/Devices tables already follow.

    `avatar_url` is an S3 key the CALLER must already have validated as their
    own (create_contact does). It exists here mainly for the phone-import path,
    which uploads the device address-book photo and creates the contact
    carrying it in one flow."""
    now = _now_iso()
    email_lc = _norm_email(email)
    phone_e164 = _norm_phone(phone)
    item = {
        "contact_id": uuid.uuid4().hex[:16],
        # owner_user_id stays THE authorization key for contacts in Phase 2B,
        # and every existing ownership check is untouched. workspace_id is
        # recorded ALONGSIDE it as provenance, so the eventual shared-contact
        # migration has the data it needs and no backfill of historical rows
        # is required later. It does NOT yet widen visibility — see the
        # CONTACTS STOP note above create_contact for why sharing needs a new
        # GSI and a decision that has not been made.
        "owner_user_id": user_id,
        "workspace_id": workspace_id or _personal_workspace_id(user_id),
        "created_by": user_id,
        "name": str(name or "").strip()[:CONTACT_NAME_MAX],
        "email": email_lc,
        "phone": str(phone or "").strip()[:CONTACT_PHONE_MAX],
        "company": str(company or "").strip()[:CONTACT_COMPANY_MAX],
        "role": str(role or "").strip()[:CONTACT_ROLE_MAX],
        "notes": str(notes or "").strip()[:CONTACT_NOTES_MAX],
        "avatar_url": str(avatar_url or "").strip()[:1000],
        "name_lc": _norm_name(name),
        "created_at": now,
        "updated_at": now,
    }
    if email_lc:
        item["email_lc"] = email_lc
        linked = _resolve_minutex_user(email_lc)
        if linked:
            item["minutex_user_id"] = linked
    if phone_e164:
        item["phone_e164"] = phone_e164
    return item


# ===========================================================================
# CONTACTS AND WORKSPACES — HISTORY, AND WHAT IS STILL OPEN
#
# THE STOP BELOW HAS BEEN LIFTED. It described Phase 2B, when contacts were
# stamped with workspace_id but not yet shared. Both blockers it names are
# now resolved and the text is kept only because it records WHY the design
# looks the way it does:
#
#   * (1) dedupe partitioned on the owner -> workspace-email-index exists
#     (CONTACTS_WORKSPACE_EMAIL_INDEX), and _find_contact_by_email switches
#     on the scope, so two members adding one client converge on one row.
#   * (2) a shared list needing a new GSI + backfill -> CONTACTS_WORKSPACE_
#     INDEX exists and is SPARSE, which is exactly why pre-Phase-2C rows
#     (no workspace_id) still serve correctly as personal.
#   * (3) the owner_user_id re-checks -> centralized in _owned_contact.
#
# The decision it defers ("visible to every member, or OWNER/MANAGER only")
# was made: READ and CREATE for every active member, EDIT and DELETE for
# OWNER/MANAGER (CAP_MANAGE_CONTACTS).
#
# STILL OPEN: a member's directory entry (see the Phase 2D block above
# _member_contact_id) is keyed "member-<user_id>" WITHOUT the workspace in
# the key. One person in two organisations therefore has one such row, whose
# workspace_id is whichever organisation tagged them first — so the second
# organisation falls back to the projection and materializes nothing. That is
# correct-but-degraded, not wrong: the tag and the picker entry still come
# from live membership. Making the key "member-<workspace>-<user>" is the fix
# and needs a migration of any rows already written, so it is deliberately
# not done here.
#
# ---- the original Phase 2B note, retained for its reasoning ---------------
#
# Contacts are STAMPED with workspace_id from this phase on, but organisation
# contacts are NOT yet SHARED between members. That is a stop, not an
# oversight, and the brief asks for exactly this when a change would need a
# key redesign.
#
# WHY SHARING CANNOT BE SWITCHED ON HERE.
#
#  1. DEDUPE IS HARD-PARTITIONED ON THE OWNER. _find_contact_by_email and
#     _find_contact_by_phone query owner-email-index / owner-phone-index with
#     `owner_user_id` as the HASH key. In a shared workspace two members
#     adding the same person would each create a row, and create_contact would
#     answer 201 "created" instead of 200 "existing" — a CORRECTNESS
#     regression in the exact feature those indexes exist to provide. No
#     amount of filtering fixes it: the partition key forces you to already
#     know whose namespace to look in.
#
#  2. A SHARED LIST NEEDS A NEW GSI *AND* A BACKFILL. A workspace-index would
#     be sparse, so every pre-Phase-2B contact (which has no workspace_id)
#     would be invisible in it — a silently empty contact list for existing
#     users until a backfill completes. Unlike Recordings and Tasks, where the
#     personal path still runs off the untouched owner indexes, contacts have
#     nowhere else to fall back to for a SHARED read.
#
#  3. TWENTY-FOUR EXPLICIT `owner_user_id == user_id` RE-CHECKS would each
#     have to become a membership check. Every one of them is currently the
#     only thing standing between a PK `get_item` and a cross-tenant read, so
#     loosening them piecemeal is precisely how a leak gets introduced.
#
# WHAT PHASE 2B DOES INSTEAD: records workspace_id on every new contact, so
# the data needed for that migration accumulates from now on and the eventual
# backfill covers a shrinking tail. Existing behaviour is bit-for-bit
# unchanged — no read path consults the new attribute.
#
# THE DECISION NEEDED BEFORE SHARING SHIPS is recorded in the Phase 2B report:
# whether org contacts are visible to every member or only to OWNER/MANAGER,
# and what happens to the personal contacts a member already had.
# ===========================================================================
def create_contact(event):
    """POST /contacts {name, email?, phone?, company?, role?, notes?}
       -> 201 {contact} | 200 {contact, existing:true} | 409 {ambiguous}

    Deduplication is by STRONG identifier only:
      * an email or phone that already exists returns the EXISTING contact
        (200, existing:true) instead of making a second row for one person;
      * a name that already exists answers 409 with the candidates, because
        two people can share a name and picking one for the user is exactly
        the silent-merge section 5 forbids. Send `force:true` to say "yes, this
        really is a different person" and create it anyway.
    """
    user_id = _require_auth(event)
    data = _body(event)

    name = str(data.get("name") or "").strip()[:CONTACT_NAME_MAX]
    if not name:
        raise ApiError(400, "name required")

    raw_email = str(data.get("email") or "").strip()
    if raw_email and not _norm_email(raw_email):
        raise ApiError(400, "email is not a valid address")
    raw_phone = str(data.get("phone") or "").strip()
    if raw_phone and not _norm_phone(raw_phone):
        raise ApiError(400, "phone is not a usable number")

    # Optional photo, uploaded before this call (phone import carries the
    # device address-book picture here). Ownership-checked like every other
    # client-supplied avatar key — see _owns_avatar_key.
    avatar_url = str(data.get("avatar_url") or "").strip()[:1000]
    if avatar_url and not _owns_avatar_key(user_id, avatar_url):
        raise ApiError(400, "avatar_url must be a key from "
                            "POST /avatars/upload-request")

    # The workspace this contact will belong to, VERIFIED against membership
    # before anything is read or written. Every ACTIVE member may create an
    # organisation contact (a shared address book nobody can add to is
    # useless), so no capability is required here — only membership.
    contact_workspace = _creation_workspace(event, user_id)

    # Deduplicated within the WORKSPACE for an organisation, so two members
    # adding the same client converge on one row instead of two.
    status, matches = _match_contacts(user_id, name, raw_email, raw_phone,
                                      workspace_id=contact_workspace)
    if status == "exact":
        existing = matches[0]
        # A photo offered for someone we already know FILLS A GAP, it never
        # overwrites. The phone-import path reaches here whenever the person is
        # already a contact, and dropping their address-book picture would mean
        # importing the same person twice gives a photo the first time and not
        # the second. Replacing an existing one is the owner's explicit call,
        # made on the contact screen — not a side effect of an import.
        if avatar_url and not str(existing.get("avatar_url") or "").strip():
            _apply_update(_contacts, {"contact_id": existing["contact_id"]},
                          {"avatar_url": avatar_url,
                           "updated_at": _now_iso()}, [])
            existing = _contacts.get_item(
                Key={"contact_id": existing["contact_id"]}).get("Item") or existing
        elif avatar_url:
            # Not stored — do not leave the uploaded object orphaned in S3.
            _delete_avatar_object(avatar_url)
        return _resp(200, {"contact": _public_contact(
                               existing,
                               member_roles=_roles_for_contact(existing)),
                           "existing": True,
                           "reason": "a contact with this email or phone "
                                     "already exists"})
    if status == "ambiguous" and not data.get("force"):
        raise AmbiguousContact(matches)

    item = _contact_item(user_id, name, raw_email, raw_phone,
                         data.get("company"), data.get("role"),
                         data.get("notes"), avatar_url,
                         workspace_id=contact_workspace)
    # attribute_not_exists on the PK: a uuid collision is astronomically
    # unlikely, but "astronomically unlikely" is not "cannot silently
    # overwrite a real person's record".
    _contacts.put_item(Item=item,
                       ConditionExpression="attribute_not_exists(contact_id)")
    _audit("contact.created", user_id, item["contact_id"])
    return _resp(201, {"contact": _public_contact(
        item, member_roles=_roles_for_contact(item))})


class AmbiguousContact(ApiError):
    """409 + the candidate list, so the app can ask "which Rahul?".

    A distinct exception rather than a plain ApiError because the body needs
    the candidates in it — the whole point is that the client can render a
    choice instead of a dead end. Carries a stable `code` for the same reason
    SalesforceReconnectRequired does: clients branch on the code, not on
    English text.
    """

    def __init__(self, matches):
        super().__init__(409, "more than one contact could match — choose one "
                              "or send force:true to create a new person")
        self.code = "contact_ambiguous"
        self.candidates = _public_contacts(matches)


class MemberContactImmutable(ApiError):
    """409 — the contact IS an organisation member (Phase 2D).

    Carries a stable `code` for the same reason AmbiguousContact does:
    clients branch on the code, never on English text. The app uses it to
    hide Edit/Delete rather than to explain a failure after the fact.
    """

    def __init__(self, message):
        super().__init__(409, message)
        self.code = "contact_is_member"


def list_contacts(event):
    """GET /contacts?search=&limit=&cursor= -> {contacts, count, next_cursor}

    Search is server-side (section 30) over name / email / company. It is a
    substring filter applied to the owner's page, not a full-text index —
    DynamoDB has no such index, and the alternative (pulling every contact to
    the phone and filtering there) is what section 31 rules out. `limit` bounds
    every response, and `next_cursor` pages.
    """
    user_id = _require_auth(event)
    qs = event.get("queryStringParameters") or {}
    limit = _clean_limit(qs.get("limit"), CONTACTS_PAGE_DEFAULT,
                         CONTACTS_PAGE_MAX)
    search = str(qs.get("search") or "").strip().casefold()

    # ORGANISATION contacts are SHARED, so the list is partitioned by the
    # WORKSPACE rather than by the owner (Phase 2C). Membership is verified
    # before the query — the index is a lookup path, never the decision.
    #
    # Personal keeps the original owner-index path byte for byte, which is
    # also what every pre-Phase-2C row is served by: those rows carry no
    # workspace_id and are therefore absent from the sparse workspace index,
    # which is correct because they are personal.
    requested = _requested_workspace_id(event)
    if requested and not workspace_schema.is_personal_workspace_id(requested):
        if not _active_membership(requested, user_id):
            raise ApiError(404, "workspace not found")
        scope_workspace_id = requested
        query = {
            "IndexName": CONTACTS_WORKSPACE_INDEX,
            "KeyConditionExpression": Key("workspace_id").eq(requested),
            "ScanIndexForward": False,
        }
    else:
        scope_workspace_id = _personal_workspace_id(user_id)
        query = {
            "IndexName": CONTACTS_OWNER_INDEX,
            "KeyConditionExpression": Key("owner_user_id").eq(user_id),
            "ScanIndexForward": False,
        }
    cursor = _decode_cursor(qs.get("cursor"))
    if cursor:
        query["ExclusiveStartKey"] = cursor

    out, last_key = [], None
    # A search filters AFTER the page is read, so a page of 50 rows can yield
    # fewer than 50 matches. Keep reading until the page is full or the index
    # is exhausted, bounded so one request can't walk a whole large table.
    for _ in range(_SEARCH_MAX_PAGES):
        query["Limit"] = limit + 1 if not search else max(limit * 4, 100)
        res = _contacts.query(**query)
        for item in res.get("Items", []):
            if search and not _contact_matches_search(item, search):
                continue
            # The PERSONAL page reads owner-index, which returns every row
            # this user owns — INCLUDING organisation contacts they created,
            # because they are still the owner of those. Those belong to the
            # organisation's shared book, not to their private one, so they
            # are filtered out here. Without this, switching to Personal
            # would leak the organisation's address book into it.
            # Resolved, not compared raw: a pre-Phase-2C row carries no
            # workspace_id and is PERSONAL to its owner, so it must still
            # appear in their personal list.
            if _row_workspace_id(item, owner_field="owner_user_id") \
                    != scope_workspace_id:
                continue
            out.append(item)
        last_key = res.get("LastEvaluatedKey")
        if not last_key or len(out) >= limit:
            break
        query["ExclusiveStartKey"] = last_key

    more = out[limit:]
    out = out[:limit]
    # When the trim dropped rows, the cursor must resume from the last row we
    # actually RETURNED, not from where the scan stopped — otherwise the next
    # page would skip everything in between.
    next_cursor = ""
    if more:
        next_cursor = _encode_cursor({"owner_user_id": user_id,
                                      "created_at": out[-1]["created_at"],
                                      "contact_id": out[-1]["contact_id"]})
    elif last_key:
        next_cursor = _encode_cursor(last_key)

    # ORGANISATION COLLEAGUES (Phase 2D). Projected from live membership and
    # prepended, so every member of the organisation is taggable in any
    # meeting without anybody adding them by hand.
    #
    # ON THE FIRST PAGE ONLY. The projection does not live in the index the
    # cursor walks, so re-adding it to page 2 would repeat the same
    # colleagues on every page. A cursor means "continue the stored rows",
    # and the colleagues were already delivered with page 1.
    #
    # The role tag needs the membership map regardless of paging — page 2 of
    # a shared book still has to label a hand-added colleague.
    member_roles = None
    if _contact_scope_is_org(scope_workspace_id):
        member_roles = _member_role_map(scope_workspace_id)
        if not cursor:
            out = _merge_member_contacts(out, user_id, scope_workspace_id)
            if search:
                # The stored rows were filtered server-side against the same
                # needle; the projected ones have not been, and returning an
                # unmatched colleague on a search would look like a bug.
                out = [c for c in out
                       if not _is_member_contact(c.get("contact_id"))
                       or _contact_matches_search(c, search)]
    return _resp(200, {"contacts": _public_contacts(out, member_roles),
                       "count": len(out),
                       "next_cursor": next_cursor})


def _contact_matches_search(item, needle):
    """Substring match over the fields a human would search by."""
    for field in ("name", "email", "company", "role", "phone"):
        if needle in str(item.get(field) or "").casefold():
            return True
    return False


def get_contact(event):
    """GET /contacts/{contact_id} -> {contact, folders}

    """
    user_id = _require_auth(event)
    contact_id = (event.get("pathParameters") or {}).get("contact_id", "")
    item = _owned_contact(user_id, contact_id)
    return _resp(200, {"contact": _public_contact(
        item, member_roles=_roles_for_contact(item))})


def update_contact(event):
    """PATCH /contacts/{contact_id} {name?, email?, phone?, company?, role?,
    notes?} -> {contact}

    Changing an email/phone re-checks for a collision with ANOTHER contact and
    refuses (409) rather than creating two rows that dedupe to one identity.
    The linked MinuteX account is re-resolved whenever the email changes, so a
    contact who signs up later becomes notification-ready on their next edit.
    """
    user_id = _require_auth(event)
    contact_id = (event.get("pathParameters") or {}).get("contact_id", "")
    # write=True: editing a shared organisation contact is OWNER/MANAGER only.
    item = _owned_contact(user_id, contact_id, write=True)

    # A MEMBER's directory entry mirrors their own profile (Phase 2D), so it
    # is not editable here — a manager renaming a colleague in the shared
    # book would be overwritten by the projection on the next read, and the
    # name shown would depend on which row happened to win. The person edits
    # their own name and photo in their profile.
    if _is_member_contact(item.get("contact_id"))             or item.get("source") == CONTACT_SOURCE_MEMBER:
        raise MemberContactImmutable(
            "this contact is an organisation member. Their name and photo "
            "come from their own MinuteX profile.")

    data = _body(event)

    updates, removes = {}, []

    if "name" in data:
        name = str(data.get("name") or "").strip()[:CONTACT_NAME_MAX]
        if not name:
            raise ApiError(400, "name cannot be empty")
        updates["name"] = name
        updates["name_lc"] = _norm_name(name)

    if "email" in data:
        raw = str(data.get("email") or "").strip()
        if raw:
            email_lc = _norm_email(raw)
            if not email_lc:
                raise ApiError(400, "email is not a valid address")
            # Scoped to the row's OWN workspace, taken from the stored item —
            # not from the request. For an organisation contact the clash
            # question is "does this organisation already use this email",
            # which owner-index cannot answer.
            clash = _find_contact_by_email(
                user_id, email_lc,
                workspace_id=_row_workspace_id(
                    item, owner_field="owner_user_id"))
            if clash and clash.get("contact_id") != item["contact_id"]:
                raise ApiError(409, "another contact already uses this email")
            updates["email"] = email_lc
            updates["email_lc"] = email_lc
            linked = _resolve_minutex_user(email_lc)
            if linked:
                updates["minutex_user_id"] = linked
            else:
                # The old link belonged to the OLD address; keeping it would
                # attribute this person's tasks to an unrelated account.
                removes.append("minutex_user_id")
        else:
            updates["email"] = ""
            removes.extend(["email_lc", "minutex_user_id"])

    if "phone" in data:
        raw = str(data.get("phone") or "").strip()
        if raw:
            phone_e164 = _norm_phone(raw)
            if not phone_e164:
                raise ApiError(400, "phone is not a usable number")
            clash = _find_contact_by_phone(
                user_id, phone_e164,
                workspace_id=_row_workspace_id(
                    item, owner_field="owner_user_id"))
            if clash and clash.get("contact_id") != item["contact_id"]:
                raise ApiError(409, "another contact already uses this phone")
            updates["phone"] = raw[:CONTACT_PHONE_MAX]
            updates["phone_e164"] = phone_e164
        else:
            updates["phone"] = ""
            removes.append("phone_e164")

    for field, cap in (("company", CONTACT_COMPANY_MAX),
                       ("role", CONTACT_ROLE_MAX),
                       ("notes", CONTACT_NOTES_MAX)):
        if field in data:
            updates[field] = str(data.get(field) or "").strip()[:cap]

    # The contact's OWN photo — an S3 key from POST /avatars/upload-request,
    # validated against the caller's prefix for the same reason patch_me does
    # (the value is client-supplied and the server presigns reads of it). ""
    # clears it, after which the contact falls back to the linked MinuteX
    # user's photo if there is one, then to initials.
    prev_avatar = ""
    if "avatar_url" in data:
        raw = str(data.get("avatar_url") or "").strip()[:1000]
        if raw and not _owns_avatar_key(user_id, raw):
            raise ApiError(400, "avatar_url must be a key from "
                                "POST /avatars/upload-request")
        prev_avatar = str(item.get("avatar_url") or "")
        if raw:
            updates["avatar_url"] = raw
        else:
            updates["avatar_url"] = ""

    if not updates and not removes:
        raise ApiError(400, "nothing to update")

    updates["updated_at"] = _now_iso()
    _apply_update(_contacts, {"contact_id": item["contact_id"]},
                  updates, removes)
    if prev_avatar and prev_avatar != updates.get("avatar_url"):
        _delete_avatar_object(prev_avatar)
    fresh = _contacts.get_item(
        Key={"contact_id": item["contact_id"]}).get("Item") or {}
    _audit("contact.updated", user_id, item["contact_id"],
           fields=sorted(set(updates) | set(removes)))
    return _resp(200, {"contact": _public_contact(
        fresh, member_roles=_roles_for_contact(fresh))})


def delete_contact(event):
    """DELETE /contacts/{contact_id} -> {deleted, id, unlinked_folders,
    unassigned_tasks}

    Deleting a PERSON does not delete their history. Folder associations and
    participant rows that point at them are removed (they would otherwise be
    orphans referencing a row that no longer exists), and tasks assigned to
    them are set back to UNRESOLVED with the name preserved in
    assignee_name_legacy — the work still exists, it just no longer claims to
    belong to a contact record that is gone. Meetings and tasks themselves are
    never deleted (section 36).
    """
    user_id = _require_auth(event)
    contact_id = (event.get("pathParameters") or {}).get("contact_id", "")
    # write=True: deleting a shared organisation contact is OWNER/MANAGER only.
    item = _owned_contact(user_id, contact_id, write=True)
    cid = item["contact_id"]

    # A MEMBER's directory entry is not deletable (Phase 2D). Deleting it
    # would unassign their tasks and unlink them from every meeting they were
    # tagged in, and then the next contacts read would simply project them
    # back — destructive AND futile. Removing the person from the
    # organisation is the operation that actually means this, and it lives on
    # the members route where it belongs.
    if _is_member_contact(cid) or item.get("source") == CONTACT_SOURCE_MEMBER:
        raise MemberContactImmutable(
            "this contact is an organisation member. Remove them from the "
            "organisation instead.")

    participants = 0
    res = _meeting_participants.query(
        IndexName=PARTICIPANTS_CONTACT_INDEX,
        KeyConditionExpression=Key("contact_id").eq(cid),
    )
    for row in res.get("Items", []):
        _meeting_participants.delete_item(
            Key={"audio_s3_key": row["audio_s3_key"],
                 "speaker_id": row["speaker_id"]})
        participants += 1

    unassigned = 0
    res = _tasks.query(
        IndexName=TASKS_ASSIGNEE_INDEX,
        KeyConditionExpression=Key("assignee_contact_id").eq(cid),
    )
    for row in res.get("Items", []):
        if row.get("owner_user_id") != user_id:
            continue
        _apply_update(
            _tasks, {"task_id": row["task_id"]},
            {"resolution_status": RESOLUTION_UNRESOLVED,
             "assignee_name_legacy": row.get("assignee_name")
                                     or item.get("name", ""),
             "updated_at": _now_iso()},
            ["assignee_contact_id", "assignee_user_id"])
        unassigned += 1

    _contacts.delete_item(Key={"contact_id": cid})
    # The row is gone, so nothing can reference its photo any more. Deleting a
    # linked MinuteX user's avatar is impossible here by construction: only the
    # contact's OWN key is ever stored on the row (see _contact_avatar_fields),
    # and _delete_avatar_object refuses anything outside avatars/.
    _delete_avatar_object(item.get("avatar_url"))
    _audit("contact.deleted", user_id, cid, unassigned_tasks=unassigned)
    return _resp(200, {"deleted": True, "id": cid,
                       "unlinked_participants": participants,
                       "unassigned_tasks": unassigned})


# ---------------------------------------------------------------------------
# Meeting participants — speaker label -> Contact
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# ATTENDANCE WITHOUT SPEECH.
#
# MeetingParticipants is keyed (audio_s3_key, speaker_id), which modelled
# participation as speaker->contact and ONLY that. Someone who attended without
# speaking — or anyone in a meeting diarization produced no labels for — had no
# way to record that they were there.
#
# A non-speaking attendee is stored in the SAME table under a reserved
# speaker_id: "self:<contact_id>". No new table, no schema change, no
# migration.
#
# WHY THIS CANNOT COLLIDE WITH A REAL SPEAKER. Diarization labels come from
# stt_result._speaker_label, which strips a "speaker_" prefix and otherwise
# passes the provider's id through — in production these are compact ("0",
# "1", "2", "7"). A colon never appears in one, and "self:" is not a prefix any
# provider label can produce. Verified against the live table before choosing
# this representation.
#
# TWO RULES MAKE IT ATTENDANCE RATHER THAN SPEECH, and both are enforced by
# NOT calling something rather than by a flag:
#
#   * it is never written into the recording's `speaker_names` map, so
#     _speaker_labels (which falls back to that map) can never surface it as a
#     speaker, and no generated document can name it as one;
#   * it never reaches _resolve_tasks_for_speaker, so tagging yourself as
#     present cannot make the AI assign you work you never agreed to.
# ---------------------------------------------------------------------------
SELF_SPEAKER_PREFIX = "self:"


def _self_speaker_id(contact_id):
    """The reserved key for "this contact attended, but did not speak".

    Derived from the contact id rather than random, which is what makes the
    write idempotent for free: the same person tagging themselves twice —
    a double tap, a retry, a second visit — lands on the same primary key and
    overwrites one row instead of accumulating duplicates.
    """
    return f"{SELF_SPEAKER_PREFIX}{str(contact_id or '').strip()}"


def _is_self_speaker(speaker_id):
    return str(speaker_id or "").startswith(SELF_SPEAKER_PREFIX)


def _public_participant(row, contact=None):
    speaker_id = row.get("speaker_id", "")
    attendance_only = _is_self_speaker(speaker_id)
    out = {
        "speaker_id": speaker_id,
        "contact_id": row.get("contact_id", ""),
        "participant_role": row.get("participant_role", ""),
        # CRM identity classification (Phase 2D.3) — "" when never set, which
        # the client renders as "not classified" rather than a default guess.
        "identity_role": row.get("identity_role", ""),
        "created_at": row.get("created_at", ""),
        "updated_at": row.get("updated_at", ""),
        # The ONE thing the app needs to know about the distinction: this
        # person was present but is not a voice in the transcript. Exposed as a
        # boolean rather than making every client parse the sentinel — the
        # storage shape stays an implementation detail.
        "attendance_only": attendance_only,
    }
    if contact is not None:
        out["contact"] = _public_contact(
            contact, member_roles=_roles_for_contact(contact))
    return out


def _participant_rows(key):
    res = _meeting_participants.query(
        KeyConditionExpression=Key("audio_s3_key").eq(key))
    return res.get("Items", [])


def list_participants(event):
    """GET /recordings/participants/{key+} -> {participants, speakers,
    folder_contacts, folder_id}

    Everything the speaker-mapping screen needs in ONE call: the labels the
    transcript actually contains and whatever each is already mapped to. The
    contact list stays a separate paged call.
    """
    user_id, key, item = _owned_recording(event)
    rows = _participant_rows(key)

    participants = []
    for row in rows:
        contact = None
        cid = row.get("contact_id")
        if cid:
            # Central gate, so a SHARED organisation contact hydrates for
            # every member rather than only for whoever created it.
            contact = _readable_contact(user_id, cid)
        participants.append(_public_participant(row, contact))
    # Speakers first in speaker order, then attendance-only rows by name.
    # _speaker_sort_key already puts non-numeric labels after numeric ones, so
    # the sentinel sorts last without a special case — but the explicit key
    # keeps that a decision rather than an accident of string ordering.
    participants.sort(key=lambda p: (
        1 if p.get("attendance_only") else 0,
        _speaker_sort_key(p["speaker_id"]),
    ))

    return _resp(200, {
        "participants": participants,
        "speakers": _speaker_labels(item),
        "speaker_names": item.get("speaker_names") or {},
        # PROCESSING STATE, so the screen can tell "no speakers YET" from "no
        # speakers, ever". Without it the empty state promised that voices
        # would appear once transcribing finished — on recordings that had
        # already finished, which is a promise it could not keep. The row is
        # already read for the ownership check, so this costs nothing.
        "recording_status": str(item.get("status") or ""),
    })


def _speaker_sort_key(label):
    """Numeric labels sort numerically ("2" before "10"), others alphabetically
    after them — so the participant list reads in speaker order."""
    s = str(label or "")
    return (0, int(s), "") if s.isdigit() else (1, 0, s)


def _speaker_labels(item):
    """The diarization labels this recording's transcript actually contains.

    Read from `timestamps` (the per-segment diarization output) with
    `speaker_names` as a fallback for rows whose transcript has been offloaded
    or predates diarization. Derived, never stored: the transcript is the
    source of truth for who spoke.
    """
    labels = []
    seen = set()
    for seg in (item.get("timestamps") or []):
        if not isinstance(seg, dict):
            continue
        label = str(seg.get("speaker", "")).strip()
        if label and label not in seen:
            seen.add(label)
            labels.append(label)
    for label in (item.get("speaker_names") or {}):
        if label not in seen:
            seen.add(label)
            labels.append(label)
    labels.sort(key=_speaker_sort_key)
    return labels


def set_participant(event):
    """PUT /recordings/participants/{key+} {speaker_id, contact_id,
    participant_role?, identity_role?} -> {participant}

    Maps one speaker label to one Contact. The TRANSCRIPT IS NEVER TOUCHED —
    labels stay "0"/"1" forever (section 9); this row is what lets the UI show
    a name over them.

    `identity_role` (Phase 2D.3) is "internal" or "external" — the CRM-facing
    classification of whether this speaker resolves through the organisation
    (a MinuteX member -> a Salesforce User) or outside it (an organisation
    Contact -> a Salesforce Contact/Account). Optional and independent of
    `participant_role`'s free text; omitting it (or sending "") leaves the
    speaker unclassified, which is the ordinary state for a meeting nobody
    intends to push to Salesforce.

    Also mirrors the contact's name into the recording's existing
    `speaker_names` map, because that map is what every already-shipped
    surface renders from (documents, highlights, the transcript view) and what
    `speaker_mapping_version` invalidates generated documents against. Writing
    only the new row would map the speaker for this screen while every AI
    document kept saying "Speaker 0".

    `contact_id: null` clears the mapping.

    ATTENDANCE WITHOUT A SPEAKER. Send `attendance_only: true` (with a
    contact_id and NO speaker_id) to record that someone was present without
    claiming they spoke. The row is stored under the reserved
    "self:<contact_id>" key, and the two speaker side effects below — the
    speaker_names sync and the AI task resolution — are deliberately SKIPPED
    for it. See the SELF_SPEAKER_PREFIX block for why that is what makes this
    attendance rather than invented speech.

    Ownership is unchanged and applies to both shapes: _owned_recording means
    only the meeting's owner can add or remove anyone, on their own meeting.
    """
    user_id, key, item = _owned_recording(event, hydrate=False)
    data = _body(event)

    attendance_only = bool(data.get("attendance_only"))
    speaker_id = str(data.get("speaker_id") or "").strip()[:MAX_SPEAKER_NAME]
    if not speaker_id and not attendance_only:
        raise ApiError(400, "speaker_id required")
    identity_role = str(data.get("identity_role") or "").strip().lower()
    if identity_role and identity_role not in SPEAKER_IDENTITY_ROLES:
        raise ApiError(400, "identity_role must be "
                            f"{' or '.join(SPEAKER_IDENTITY_ROLES)!s}")
    raw_contact = data.get("contact_id")
    clearing = raw_contact is None or str(raw_contact).strip() == ""
    # A caller must not be able to hand-craft the reserved key and have it
    # treated as a speaker MAPPING — that is the one way the sentinel could be
    # used to fake a speaker. The prefix is ours to write, never theirs.
    #
    # Scoped to writes: CLEARING by the reserved key is how an attendance row
    # is removed, and the app is handed that key by the API rather than
    # building one. Rejecting it here would make attendance addable but never
    # removable.
    if (speaker_id and _is_self_speaker(speaker_id)
            and not attendance_only and not clearing):
        raise ApiError(400, "speaker_id must not start with "
                            f"{SELF_SPEAKER_PREFIX!r}")

    if clearing:
        if not speaker_id:
            raise ApiError(400, "speaker_id required to clear a mapping")
        _meeting_participants.delete_item(
            Key={"audio_s3_key": key, "speaker_id": speaker_id})
        # An attendance row was never in speaker_names, so there is nothing
        # there to unwind — and calling this for one would bump
        # speaker_mapping_version, marking every generated document stale over
        # a change that touched no name in them.
        if not _is_self_speaker(speaker_id):
            _sync_speaker_name_from_contact(user_id, key, item, speaker_id,
                                            None)
        _audit("meeting.participant_cleared", user_id, key,
               speaker_id=speaker_id)
        return _resp(200, {"cleared": True, "speaker_id": speaker_id})

    # Materializes a projected organisation member (Phase 2D) — this row
    # stores contact_id, so the colleague must have a real row behind it.
    contact = _taggable_contact(user_id, str(raw_contact).strip(),
                                workspace_id=_row_workspace_id(item))
    # Attendance is keyed by the CONTACT, not by a speaker slot: that is what
    # makes a double tap idempotent rather than duplicating a person.
    if attendance_only:
        speaker_id = _self_speaker_id(contact["contact_id"])
    # Read before write, so a re-tag that only touches contact_id/
    # participant_role (the app's existing "rename/re-map" action) does not
    # silently un-classify a speaker's CRM identity_role — that classification
    # is a separate, deliberate act (the tagging screen's Internal/External
    # picker) and must survive an unrelated edit exactly like created_at does
    # a few lines below.
    prior = _meeting_participants.get_item(
        Key={"audio_s3_key": key, "speaker_id": speaker_id}).get("Item")
    now = _now_iso()
    row = {
        "audio_s3_key": key,
        "speaker_id": speaker_id,
        "contact_id": contact["contact_id"],
        "owner_user_id": user_id,
        "participant_role": str(data.get("participant_role")
                                or "").strip()[:PARTICIPANT_ROLE_MAX],
        "identity_role": identity_role or str((prior or {}).get("identity_role") or ""),
        "created_at": now,
        "updated_at": now,
    }
    # Preserve the original created_at on a re-tag, so "when were they added"
    # stays true across repeat taps rather than resetting on each one.
    if prior and prior.get("created_at"):
        row["created_at"] = prior["created_at"]
    _meeting_participants.put_item(Item=row)

    # THE TWO SPEAKER SIDE EFFECTS, SKIPPED FOR ATTENDANCE.
    #
    # Writing speaker_names would make the sentinel a label _speaker_labels
    # surfaces and every generated document renders — inventing a speaker.
    # Resolving tasks would assign the AI's work to someone whose only claim is
    # "I was in the room". Both are the difference between attendance and
    # speech, and both are enforced by not running rather than by a flag a
    # later change could forget to check.
    if attendance_only:
        _audit("meeting.attendee_self_tagged", user_id, key,
               contact_id=contact["contact_id"])
        return _resp(200, {"participant": _public_participant(row, contact),
                           "tasks_resolved": 0})

    _sync_speaker_name_from_contact(user_id, key, item, speaker_id, contact)

    # Mapping a speaker is the event that can resolve AI tasks assigned to that
    # speaker — this is the join in section 18's chain.
    resolved = _resolve_tasks_for_speaker(user_id, key, speaker_id, contact)

    _audit("meeting.participant_set", user_id, key, speaker_id=speaker_id,
           contact_id=contact["contact_id"], tasks_resolved=resolved)
    return _resp(200, {"participant": _public_participant(row, contact),
                       "tasks_resolved": resolved})


def _sync_speaker_name_from_contact(user_id, key, item, speaker_id, contact):
    """Keep the recording's `speaker_names` map in step with the mapping.

    Reuses the EXISTING mechanism rather than adding a second one: the map is
    already what every rendered surface reads and what
    `speaker_mapping_version` guards generated documents with, so a mapping
    change must bump that counter exactly as a manual rename does — otherwise
    documents generated under the old names would never be marked stale.
    """
    names = dict(item.get("speaker_names") or {})
    if contact is None:
        names.pop(speaker_id, None)
    else:
        names[speaker_id] = contact.get("name", "")[:MAX_SPEAKER_NAME]
    if names == (item.get("speaker_names") or {}):
        return
    _recordings.update_item(
        Key={"audio_s3_key": key},
        UpdateExpression=("SET speaker_names = :sn, updated_at = :now, "
                          "speaker_mapping_version = "
                          "if_not_exists(speaker_mapping_version, :zero) + :one"),
        ExpressionAttributeValues={":sn": names, ":now": _now_iso(),
                                   ":zero": 0, ":one": 1},
    )


# ---------------------------------------------------------------------------
# First-class Tasks
# ---------------------------------------------------------------------------
def _task_fingerprint(recording_key, text, assignee_hint=""):
    """Stable identity for an AI-extracted task: MEETING + task text.

    So the same extraction re-run (a reprocess, a duplicated Groq call, a
    retried invocation) recognizes what it already created instead of adding a
    second copy. Including the recording key means identical text in two
    different meetings correctly stays two tasks.

    THE ASSIGNEE IS DELIBERATELY NOT PART OF THIS, and `assignee_hint` is
    accepted-but-ignored so existing call sites keep reading naturally.
    A fingerprint must be stable over a task's whole life, and the assignee is
    the single most-edited field on a task — the AI says "Speaker 2", the user
    corrects it to "Siddhesh Gawade". Hashing it meant the corrected task no
    longer matched its own extraction, so the seeder saw a gap and created a
    duplicate. Observed on production data: three meetings where a reassigned
    task came back twice.

    The cost of dropping it is that two genuinely different tasks with
    IDENTICAL text in ONE meeting collapse to one fingerprint. That is the
    right trade: the AI does not emit the same sentence twice for one meeting,
    and if it did, one task is a better outcome than a duplicate that
    reappears every time someone fixes an assignee.

    Not applied to manual tasks — a user typing the same task twice on purpose
    is not a duplicate to be suppressed.
    """
    basis = "\x00".join([recording_key, _norm_name(text)])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------
# Live speaker-name resolution for tasks.
#
# WHY THIS EXISTS. An AI-extracted task records WHICH SPEAKER owed the work
# (`assignee_speaker_id`) and, until someone says who that speaker is, the
# only name it can show is the label the transcript carried: "Speaker 2".
# `_resolve_tasks_for_speaker` writes a real identity onto the row, but it
# only ever fires from set_participant — mapping a speaker to a CONTACT. A
# plain rename (patch_recording's speaker_names) went nowhere near tasks, so
# a renamed speaker kept saying "Speaker 2" on every task forever while the
# transcript, participants list and documents all updated.
#
# RESOLVED AT READ TIME, NOT WRITTEN. The rename already lands in exactly one
# authoritative place — `speaker_names` on the recording — and the task
# already stores the join key. So the display name is DERIVED here, the same
# way the transcript view derives it (lib/sources.ts' speakerName) and the
# same way _is_overdue derives overdue-ness rather than storing it. A rename
# therefore needs no writes to any task: every task the speaker owns reads
# correctly on the very next request, including ones in other folders and the
# cross-meeting Task Tracker.
#
# Writing the name onto each task instead would mean N extra DynamoDB writes
# per rename (unbounded — a speaker can own tasks across a whole meeting),
# would clobber the verbatim AI-extracted string in `assignee_name_legacy`,
# and would leave any task written between the rename and the sweep stale.
#
# THIS IS DISPLAY ONLY, AND ONLY FOR UNRESOLVED TASKS. A task with a real
# Contact behind it (RESOLUTION_RESOLVED / `assignee_contact_id`) is left
# exactly as it is — the same guard `_resolve_tasks_for_speaker` applies:
# a user who hand-assigned a task keeps their choice, and a rename of the
# speaker who happened to say the sentence must never re-point it at someone
# else. `assignee_name_legacy` also keeps its stored value in the response,
# so the AI's original extraction stays inspectable.
# ---------------------------------------------------------------------------


def _speaker_display_name(label, speaker_names):
    """A speaker label rendered for a human, or "" when there is nothing to
    render. Mirrors lib/sources.ts' speakerName so the app and the API never
    disagree about what one label is called.

    Numeric labels read as "Speaker 2"; named ones ("agent") stand alone.
    """
    # Normalized before the lookup because `speaker_names` is keyed on the
    # COMPACT id ("0"), while a task row written before speaker normalization
    # existed can still carry the transcript's "Speaker 0". Without this such a
    # row renders the literal label forever instead of the person's real name,
    # even after the user maps that speaker.
    #
    # Delegates to speaker_identity so this, mom_schema, pageindex and the app
    # cannot drift apart about what one label is called. Behaviour is
    # unchanged, with one repair: a map stored under a legacy display-form key
    # ({"Speaker 0": "Rahul"}) now resolves too, instead of rendering the
    # literal label forever.
    return speaker_identity.resolve_speaker_display(label, speaker_names)


def _speaker_names_for_recording(key, cache=None):
    """The `speaker_names` map for one recording, read straight from the row.

    `cache` is a per-REQUEST dict, not module state: list_all_tasks can return
    a page of tasks drawn from many meetings, and without it that page would
    re-read the same recording once per task. Deliberately not cached across
    invocations — a warm Lambda container would then serve the name from
    before the rename, which is the exact bug this whole section fixes.

    Ownership is NOT checked here. Every caller has already established that
    the task is the caller's, and a task's `source_recording_id` is written by
    the pipeline, never by a client — so this can only ever read the map of a
    recording the caller's own task came from.
    """
    key = str(key or "")
    if not key:
        return {}
    if cache is not None and key in cache:
        return cache[key]
    names = {}
    try:
        row = _recordings.get_item(
            Key={"audio_s3_key": key},
            ProjectionExpression="speaker_names",
        ).get("Item") or {}
        got = row.get("speaker_names")
        if isinstance(got, dict):
            names = got
    except ClientError as e:
        # A task must still render if the recording read fails — the assignee
        # falls back to whatever is stored on the row, which is what every
        # build before this change showed anyway.
        print(f"[warn] speaker_names read failed for {key}: "
              f"{type(e).__name__}: {e}")
    if cache is not None:
        cache[key] = names
    return names


def _task_needs_review(row):
    """True when a human has to confirm this task's assignee.

    ONE definition, computed from state that already exists rather than stored
    as a fourth flag that could drift. It is exactly the UNRESOLVED case: an
    assignee was CLAIMED (a spoken name, or a low-confidence match the gate
    withheld) but not confirmed. Both AI and legacy-migrated tasks reach it,
    which is correct — a legacy task carrying a bare name needs the same
    confirmation as a freshly-gated one.

    NONE is deliberately NOT review-worthy: an unassigned task is a normal
    outcome, not an open question, and treating it as one would fill the queue
    with work nobody ever claimed.
    """
    return row.get("resolution_status") == RESOLUTION_UNRESOLVED


def _public_task_v2(row, speaker_names=None):
    """A first-class Task in API shape.

    `is_overdue` is COMPUTED, never stored (section 14): a stored flag would be
    wrong the moment the clock passed midnight with nothing writing to the row.

    `speaker_name` and the display half of `assignee` are computed the same
    way and for the same reason — see the live-resolution section above.
    Callers pass the source recording's `speaker_names` map; omitting it keeps
    the pre-rename behaviour (the stored string), so a call site that has no
    recording in hand still returns a valid task.
    """
    due = row.get("due_date") or ""
    status = row.get("status") or TASK_STATUS_OPEN

    # The name a rename should move. Only meaningful while the task is still
    # pinned to a speaker rather than a person: once a Contact is attached,
    # `assignee_name` is that person's name and is not the speaker's to change.
    speaker_id = str(row.get("assignee_speaker_id") or "")
    resolved_by_contact = bool(row.get("assignee_contact_id"))
    speaker_name = ("" if not speaker_id
                    else _speaker_display_name(speaker_id, speaker_names))
    # What the assignee should READ as. The renamed speaker wins over the
    # AI's stored string, but only for a task no human has assigned.
    #
    # ORDER MATTERS, AND `assignee_name` NO LONGER COMES FIRST. Current
    # writers only set `assignee_name` alongside a real Contact (see
    # _new_task_row), where `resolved_by_contact` already protects it. But a
    # legacy row can carry `assignee_name` with NO contact — and there the
    # stored string used to shadow the live speaker map, so the one case this
    # whole section exists to fix (rename the speaker, see the task update)
    # silently did nothing. Checking the speaker first restores that for those
    # rows while leaving every contact-assigned task byte-identical.
    display_name = ((speaker_name if not resolved_by_contact else "")
                    or row.get("assignee_name")
                    or row.get("assignee_name_legacy") or "")

    return {
        "id": row.get("task_id", ""),
        "task": row.get("title", ""),
        "title": row.get("title", ""),
        "description": row.get("description", ""),
        "status": status,
        "priority": row.get("priority", "Medium"),
        "due": due,
        "due_date": due,
        # The resolved calendar date beside the spoken phrase. It was already
        # stored and already used for `is_overdue`, but was never exposed — so
        # every consumer could only show what was SAID. A MoM built from that
        # still reads "tomorrow" six months later, which is what production
        # rows show. "" whenever normalization did not resolve the phrase
        # ("Next Meeting", "2 to 3 days", every non-English form), so a
        # consumer must fall back to `due` rather than render a blank.
        "due_date_normalized": row.get("due_date_normalized", ""),
        "is_overdue": _is_overdue(due, status,
                                  row.get("due_date_normalized", "")),
        "assignee_contact_id": row.get("assignee_contact_id", ""),
        "assignee_user_id": row.get("assignee_user_id", ""),
        "assignee_name": row.get("assignee_name", ""),
        "assignee_name_legacy": row.get("assignee_name_legacy", ""),
        "assignee_speaker_id": speaker_id,
        # The speaker label rendered through the meeting's CURRENT
        # speaker_names — "Speaker 2" before anyone names them, "Ravi" after.
        # Present whenever the task came from a speaker, even once a Contact
        # has been attached, so the app can still say where it came from.
        "speaker_name": speaker_name,
        "resolution_status": row.get("resolution_status", RESOLUTION_NONE),
        "source_recording_id": row.get("source_recording_id", ""),
        "source_type": row.get("source_type", TASK_SOURCE_MANUAL),
        "ai_confidence": row.get("ai_confidence", ""),
        "ai_evidence": row.get("ai_evidence", ""),
        # WHERE the evidence came from, so the app can offer to jump to that
        # moment of the meeting. Absent on rows with no reference, and on
        # manual tasks, which is what lets the UI show AI provenance only
        # where it genuinely exists.
        "ai_evidence_segment_ids": list(
            row.get("ai_evidence_segment_ids") or []),
        # The ONE flag the UI branches on for "this needs a human". Computed
        # here rather than stored so it can never disagree with the
        # resolution_status it is derived from — a task resolved by the user
        # stops needing review the instant they resolve it, with no second
        # write to keep in step.
        "needs_review": _task_needs_review(row),
        "notified_via": row.get("notified_via", []),
        "created_at": row.get("created_at", ""),
        "updated_at": row.get("updated_at", ""),
        "completed_at": row.get("completed_at", ""),
        "from_action_item": row.get("source_type") == TASK_SOURCE_AI,
        # The legacy API's assignee shape, so a client build written against
        # the embedded-map API keeps rendering an assignee without changes.
        "assignee": ({"name": display_name,
                      "email": row.get("assignee_email", ""),
                      "phone": row.get("assignee_phone", ""),
                      "source": "manual"}
                     if display_name else None),
    }


def _is_overdue(due, status, due_normalized=""):
    """True when a non-terminal task's due date is in the past.

    Timezone-safe: the stored value may be a full ISO timestamp or a bare
    date. A bare date is treated as END of that day in UTC, so a task due
    "2026-08-20" is not overdue at 00:01 on the 20th. Anything unparseable
    (the AI can emit "next Friday", which is real data we must not crash on)
    is simply not overdue — it cannot be compared, so it is not claimed.

    `due_normalized` is the resolved calendar day for a SPOKEN due date, and
    is preferred when present: without it a task due "Friday" could never be
    overdue at all, because the raw phrase does not parse. Rows written before
    normalization existed simply do not have it and fall back to `due` — the
    same not-claimed behaviour as before, never a wrong claim.
    """
    if status in TASK_TERMINAL_STATUSES:
        return False
    text = str(due_normalized or "").strip() or str(due or "").strip()
    if not text:
        return False
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            when = datetime.fromisoformat(text).replace(
                hour=23, minute=59, second=59, tzinfo=timezone.utc)
        else:
            when = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False
    return when < datetime.now(timezone.utc)


def _clean_task_status(raw, field="status"):
    """Accept either vocabulary, store the Title Case form."""
    text = str(raw or "").strip()
    if not text:
        raise ApiError(400, f"{field} cannot be empty")
    mapped = _TASK_STATUS_ALIASES.get(text.casefold())
    if not mapped:
        raise ApiError(400, f"{field} must be one of: "
                            + ", ".join(TASK_STATUSES_V2))
    return mapped


def _clean_task_priority(raw):
    text = str(raw or "").strip()
    for p in TASK_PRIORITIES:
        if text.casefold() == p.casefold():
            return p
    raise ApiError(400, "priority must be one of: " + ", ".join(TASK_PRIORITIES))


def _tasks_for_recording(key):
    """Every task sourced from one meeting (meeting-index)."""
    res = _tasks.query(
        IndexName=TASKS_MEETING_INDEX,
        KeyConditionExpression=Key("source_recording_id").eq(key),
    )
    return res.get("Items", [])


# Fingerprints of AI-seeded tasks the user has DELETED. Kept on the recording
# row because that is what the seeder already reads, so honoring a tombstone
# costs no extra request.
#
# WHY this is needed: seeding is idempotent through the fingerprint index,
# which asks "does a task with this fingerprint exist?". Deleting the task
# removes that row — and therefore the answer — so the very next read would
# helpfully re-create the task the user just threw away. A deletion has to
# leave a mark behind, or "delete" means "hide until you look again".
DELETED_TASK_FINGERPRINTS_ATTR = "deleted_task_fingerprints"
# Bounded so a user repeatedly seeding-and-deleting cannot grow the recording
# row without limit (DynamoDB items cap at 400KB). Oldest entries fall off;
# the worst case if one is evicted is that a very old deleted AI task can
# reappear once, which is far better than an unwritable recording row.
MAX_DELETED_FINGERPRINTS = 200


def _deleted_fingerprints(item):
    raw = item.get(DELETED_TASK_FINGERPRINTS_ATTR)
    return [f for f in raw if isinstance(f, str)] if isinstance(raw, list) else []


def _tombstone_fingerprint(key, item, fingerprint):
    """Record that an AI-seeded task was deliberately deleted."""
    if not fingerprint:
        return
    existing = _deleted_fingerprints(item)
    if fingerprint in existing:
        return
    updated = (existing + [fingerprint])[-MAX_DELETED_FINGERPRINTS:]
    _recordings.update_item(
        Key={"audio_s3_key": key},
        UpdateExpression="SET #d = :d, updated_at = :now",
        ExpressionAttributeNames={"#d": DELETED_TASK_FINGERPRINTS_ATTR},
        ExpressionAttributeValues={":d": updated, ":now": _now_iso()},
    )


def _find_task_by_fingerprint(user_id, fingerprint):
    """The existing task with this fingerprint, or None (dedupe-index)."""
    if not fingerprint:
        return None
    res = _tasks.query(
        IndexName=TASKS_DEDUPE_INDEX,
        KeyConditionExpression=Key("owner_user_id").eq(user_id)
                               & Key("fingerprint").eq(fingerprint),
        Limit=1,
    )
    items = res.get("Items", [])
    return items[0] if items else None


def _owned_task(user_id, task_id):
    """The task, if this caller CREATED it. 404 otherwise.

    Deliberately still creator-only: this is the predicate for the actions
    only a creator may perform (deadline, details, assignee, AI resolution).
    Routes an assignee may also reach use _visible_task / _task_for_status
    instead — see the permission model below.
    """
    tid = str(task_id or "").strip()
    if not tid:
        raise ApiError(400, "task id required")
    row = _tasks.get_item(Key={"task_id": tid}).get("Item")
    if not row or row.get("owner_user_id") != user_id:
        raise ApiError(404, "task not found")
    return row


# ---------------------------------------------------------------------------
# TASK LIFECYCLE PERMISSIONS
#
# Two DIFFERENT people have rights over a task, and conflating them is the bug
# this section exists to prevent:
#
#   CREATOR   `owner_user_id`. The authenticated user for a manual task; the
#             MEETING OWNER for an AI-seeded one (_seed_ai_tasks is handed the
#             recording row's user_id, never anything off the event). They
#             control the task's CONFIGURATION — deadline, details, assignee,
#             AI assignment resolution.
#
#   ASSIGNEE  `assignee_user_id`. Only ever written from a Contact linked to a
#             real MinuteX account (see _new_task_row), so its presence means
#             "there is an authenticated person here". They EXECUTE the task
#             and may change exactly one field: status.
#
# `assignee_contact_id` is NOT an authorization input. A contact is a record in
# someone's address book, not an identity that can authenticate — a task
# assigned to a contact with no MinuteX account simply has no assignee who can
# act on it, which is correct rather than a gap to paper over.
#
# ERROR CODES. An unrelated caller gets 404 from every route, matching
# _owned_task / _owned_contact / _owned_recording: a 403 would confirm the id
# exists. An ASSIGNEE attempting a creator-only action gets 403, because they
# can already see the task — telling a permitted viewer their own task does
# not exist would be a lie that is very hard to debug.
# ---------------------------------------------------------------------------

def _task_creator(row):
    """The user who controls this task's configuration."""
    return str((row or {}).get("owner_user_id") or "").strip()


def _is_task_creator(user_id, row):
    return bool(user_id) and _task_creator(row) == user_id


def _is_task_assignee(user_id, row):
    """Whether this caller is the task's assignee AS AN ACCOUNT.

    Reuses _task_assignee_user — the same predicate the notification engine
    uses to decide there is someone to notify — so "can act on this task" and
    "can receive mail about this task" can never drift apart.
    """
    return bool(user_id) and _task_assignee_user(row) == user_id


def _visible_task(user_id, task_id):
    """The task, if this caller may VIEW it: creator or assignee.

    404 for everyone else, so an unrelated caller cannot use this route to
    discover that a task id exists.
    """
    tid = str(task_id or "").strip()
    if not tid:
        raise ApiError(400, "task id required")
    row = _tasks.get_item(Key={"task_id": tid}).get("Item")
    if not row or not (_is_task_creator(user_id, row)
                       or _is_task_assignee(user_id, row)):
        raise ApiError(404, "task not found")
    return row


def _authorize_task_patch(user_id, row, data):
    """Reject a patch that touches fields this caller may not change.

    Status is the ONLY field an assignee may modify. Everything else —
    title, description, due date, priority, assignee, notification record —
    is the creator's. Checked against the REQUEST BODY rather than against
    what changed, so a no-op write of a forbidden field is still refused: a
    client must not be able to probe which fields it can reach.

    Called after the row is loaded and before any update is built, so a denied
    request writes nothing at all.
    """
    if _is_task_creator(user_id, row):
        return
    if not _is_task_assignee(user_id, row):
        # Not creator, not assignee: this row is not theirs to see, let alone
        # patch. 404 keeps it indistinguishable from a task that never was.
        raise ApiError(404, "task not found")
    touched = [f for f in data if f in _TASK_CREATOR_ONLY_FIELDS]
    if touched:
        raise ApiError(
            403,
            "only the task creator can change "
            + ", ".join(sorted(touched))
            + " — the assignee can change status only")


def _task_permissions(user_id, row):
    """What this caller may do with this task, as plain booleans.

    Sent on the task detail response so the app can render the right controls
    without re-implementing the rule. This is a CONVENIENCE for the UI, never
    the enforcement point — every mutation route checks for itself, because a
    client is free to ignore what it is told.
    """
    creator = _is_task_creator(user_id, row)
    assignee = _is_task_assignee(user_id, row)
    return {
        "is_creator": creator,
        "is_assignee": assignee,
        "can_view": creator or assignee,
        # The one field an assignee owns.
        "can_change_status": creator or assignee,
        # Everything that configures the task belongs to its creator.
        "can_edit_details": creator,
        "can_change_deadline": creator,
        "can_change_assignee": creator,
        "can_resolve_assignment": creator,
        "can_delete": creator,
    }


# Every writable field of the patch body EXCEPT status (and `id`, which selects
# the row rather than changing it). Named explicitly rather than derived as
# "not status" so a NEW writable field is denied to the assignee by default: a
# field added here is a decision, a field forgotten is a vulnerability.
_TASK_CREATOR_ONLY_FIELDS = (
    "task", "title", "description", "due", "due_date", "priority",
    "assignee", "assignee_contact_id", "notify_channels",
)


def _write_task(row):
    """Put a Task row, omitting every empty GSI key attribute.

    assignee_contact_id / fingerprint are all index keys, and DynamoDB rejects
    an empty string as one — so "unset" must be an ABSENT attribute, not "".
    Centralized here so no caller has to remember.
    """
    clean = {k: v for k, v in row.items()
             if not (k in _TASK_SPARSE_KEYS and not v)}
    _tasks.put_item(Item=clean)
    return clean


_TASK_SPARSE_KEYS = ("assignee_contact_id", "fingerprint",
                     "assignee_user_id", "workspace_id")

# The key attributes of each Tasks index, used to build a resume cursor that is
# valid for the index the query actually ran against (see list_all_tasks). A
# cursor carrying only the table key is rejected on a GSI query.
_TASK_INDEX_KEYS = {
    TASKS_OWNER_INDEX: ("owner_user_id", "created_at"),
    TASKS_ASSIGNEE_USER_INDEX: ("assignee_user_id", "created_at"),
    TASKS_MEETING_INDEX: ("source_recording_id", "created_at"),
    TASKS_ASSIGNEE_INDEX: ("assignee_contact_id", "created_at"),
    TASKS_DEDUPE_INDEX: ("owner_user_id", "fingerprint"),
}


def _new_task_row(user_id, title, *, recording_key="",
                  description="", due="", priority="Medium",
                  status=TASK_STATUS_OPEN, source_type=TASK_SOURCE_MANUAL,
                  assignee_contact=None, assignee_name="",
                  assignee_speaker_id="", ai_confidence="", ai_evidence="",
                  fingerprint="", notified_via=None, due_normalized="",
                  workspace_id=""):
    """Assemble a Task row, deriving the identity fields consistently.

    The resolution_status logic is the important part and lives ONLY here:
      * a Contact  -> RESOLVED, and assignee_user_id is filled from the
                      contact's linked MinuteX account when it has one;
      * a bare NAME -> UNRESOLVED, with the name kept in assignee_name_legacy
                      so nothing is lost and the user can resolve it later;
      * neither     -> NONE, which is not a failure, just an unassigned task.
    A name is never silently turned into a contact here (section 16).
    """
    now = _now_iso()
    row = {
        "task_id": uuid.uuid4().hex[:16],
        "owner_user_id": user_id,
        # WORKSPACE (Phase 2B). INHERITED from the parent meeting where there
        # is one — never derived from whoever happens to be calling. That is
        # what stops an AI-extracted task from a workspace-A meeting landing in
        # workspace B because a differently-scoped request triggered the
        # seeding. Empty for a personal task, and _write_task strips it (it is
        # a sparse index key), so a personal task is simply absent from the
        # workspace index and is resolved by _row_workspace_id instead.
        "workspace_id": workspace_id or "",
        # WHO made it. owner_user_id remains the CREATOR and keeps every
        # permission it already had — this is provenance that survives the
        # creator leaving an organisation, not a second authorization input.
        "created_by": user_id,
        "title": str(title or "").strip()[:MAX_TASK_TEXT],
        "description": str(description or "").strip()[:MAX_TASK_NOTE_TEXT],
        "status": status,
        "priority": priority,
        "due_date": str(due or "").strip()[:100],
        # The spoken date resolved to a calendar day, or "" when the
        # phrase is not placeable ("end of Q3"). `due_date` keeps what was
        # actually said; every comparison uses this. Storing both is the
        # point: the record stays faithful AND the machinery can compare.
        "due_date_normalized": str(due_normalized or "").strip()[:10],
        "source_recording_id": recording_key or "",
        "source_type": source_type,
        "ai_confidence": str(ai_confidence or "")[:32],
        "ai_evidence": str(ai_evidence or "")[:MAX_TASK_NOTE_TEXT],
        "fingerprint": fingerprint or "",
        "notified_via": list(notified_via or []),
        "assignee_speaker_id": str(assignee_speaker_id or "")[:MAX_SPEAKER_NAME],
        "created_at": now,
        "updated_at": now,
        "completed_at": "",
    }
    if assignee_contact:
        row["assignee_contact_id"] = assignee_contact["contact_id"]
        row["assignee_name"] = assignee_contact.get("name", "")
        row["assignee_email"] = assignee_contact.get("email", "")
        row["assignee_phone"] = assignee_contact.get("phone", "")
        row["resolution_status"] = RESOLUTION_RESOLVED
        linked = assignee_contact.get("minutex_user_id") or ""
        if linked:
            row["assignee_user_id"] = linked
    elif str(assignee_name or "").strip():
        # A NAME, not an identity. Preserved verbatim, explicitly unresolved.
        row["assignee_name_legacy"] = str(assignee_name).strip()[:CONTACT_NAME_MAX]
        row["resolution_status"] = RESOLUTION_UNRESOLVED
    else:
        row["resolution_status"] = RESOLUTION_NONE
    if status == TASK_STATUS_COMPLETED:
        row["completed_at"] = now
    return row


def _mirror_task_to_recording(key, task_row):
    """Write the task into the recording row's legacy embedded map too.

    The dual-write from the migration plan: the Tasks table is authoritative
    and is what every read path uses, but the embedded map this app shipped
    with is kept in step so (a) nothing is lost if the new table has to be
    rebuilt and (b) an older client build still reading the map keeps working.
    Reuses _save_task, so the concurrency-safe nested-SET behavior is shared
    rather than reimplemented.

    Best-effort ON PURPOSE: the authoritative write has already succeeded by
    the time this runs, so a mirror failure must not fail the request. It is
    logged instead — a divergence is a thing to notice, not a thing to 500 on.
    """
    if not key:
        return
    try:
        _save_task(key, task_row["task_id"], {
            "task": task_row.get("title", ""),
            "due": task_row.get("due_date", ""),
            "priority": task_row.get("priority", "Medium"),
            "status": task_row.get("status", TASK_STATUS_OPEN),
            "assignee": ({"name": task_row.get("assignee_name")
                                  or task_row.get("assignee_name_legacy") or "",
                          "source": "manual"}
                         if (task_row.get("assignee_name")
                             or task_row.get("assignee_name_legacy")) else None),
            "notified_via": task_row.get("notified_via", []),
            "created_at": task_row.get("created_at", ""),
            "updated_at": task_row.get("updated_at", ""),
            "from_action_item": task_row.get("source_type") == TASK_SOURCE_AI,
            # Marks the map entry as a mirror, so the backfill can tell an
            # already-migrated entry from a genuine legacy one.
            "task_id": task_row["task_id"],
        })
    except Exception as e:  # noqa: BLE001
        print(f"[warn] task mirror failed for {key}/{task_row['task_id']}: "
              f"{type(e).__name__}: {e}")


def _migrate_embedded_tasks(user_id, key, item):
    """Bring one recording's embedded tasks into the Tasks table, once.

    The read-path half of the migration (section 15): the first time a
    meeting's tasks are read through the new API, any entry in the legacy map
    that has no counterpart in the Tasks table is copied across. Idempotent by
    construction — an entry is skipped when a task with its `legacy_task_id`
    already exists, so this can run on every read forever without duplicating.

    Legacy assignees become UNRESOLVED with their name preserved, never a
    guessed Contact (section 16).

    Returns the number of rows created.
    """
    stored = _stored_tasks(item)
    if not stored:
        return 0
    existing = {r.get("legacy_task_id") for r in _tasks_for_recording(key)}
    created = 0
    for legacy_id, t in stored.items():
        if not isinstance(t, dict) or legacy_id in existing:
            continue
        # An entry with no task text is not a task. Migrating it would create a
        # blank row the user can neither read nor act on, so it is skipped —
        # the same rule scripts/32_backfill_tasks.py applies, and the two must
        # agree or the eager and lazy paths would produce different results.
        if not str(t.get("task") or "").strip():
            continue
        # A mirrored entry carries the new task_id it came from — that is our
        # own write coming back, not a legacy task to migrate.
        if t.get("task_id"):
            continue
        assignee = t.get("assignee") if isinstance(t.get("assignee"), dict) else {}
        status = t.get("status") or TASK_STATUS_OPEN
        if status not in TASK_STATUSES_V2:
            status = _TASK_STATUS_ALIASES.get(str(status).casefold(),
                                              TASK_STATUS_OPEN)
        row = _new_task_row(
            user_id, t.get("task", ""),
            recording_key=key,
            workspace_id=_row_workspace_id(item),
            due=t.get("due", ""),
            priority=t.get("priority") or "Medium",
            status=status,
            source_type=(TASK_SOURCE_AI if t.get("from_action_item")
                         else TASK_SOURCE_LEGACY),
            assignee_name=(assignee or {}).get("name", ""),
            notified_via=t.get("notified_via") or [],
        )
        # Preserve the original identity and timestamps: the migrated task IS
        # the old task, not a new one that happens to look like it.
        row["legacy_task_id"] = legacy_id
        row["created_at"] = t.get("created_at") or row["created_at"]
        row["updated_at"] = t.get("updated_at") or row["updated_at"]
        if (assignee or {}).get("email"):
            row["assignee_email"] = assignee["email"]
        if (assignee or {}).get("phone"):
            row["assignee_phone"] = assignee["phone"]
        # A migrated task that ORIGINALLY came from the AI must carry the same
        # fingerprint the seeder would compute for it, or the two idempotency
        # schemes cannot see each other and both create the same task.
        #
        # This is not hypothetical — it is what happened on real data: the
        # embedded map was itself seeded from `ai_tasks` long ago, so migration
        # (keyed on legacy_task_id) and seeding (keyed on fingerprint) each
        # produced their own copy, and a meeting with 5 tasks read back 10.
        #
        # Only AI-sourced entries get one. A task the user typed by hand has no
        # counterpart in `ai_tasks` and must not be suppressed by a collision
        # with one.
        if t.get("from_action_item"):
            row["fingerprint"] = _task_fingerprint(
                key, row["title"], row.get("assignee_name_legacy") or "")
        _write_task(row)
        created += 1
    if created:
        print(f"[migrate] {key}: {created} embedded task(s) -> Tasks table")
    return created


# ---------------------------------------------------------------------------
# AI TASK VALIDATION AND CONFIDENCE GATING.
#
# THE RULE THIS ENFORCES: MinuteX must never silently assign work to a person
# it is not sure about. A wrong assignee is worse than no assignee — the real
# owner never learns the task exists, and the person it landed on has no way to
# know it was a guess.
#
# WHAT IS AND IS NOT A NEW STATE. No new review state was introduced: the Task
# model already distinguishes RESOLVED / UNRESOLVED / NONE, and UNRESOLVED
# already means exactly "there is an assignee claim here that a human must
# confirm". Low-confidence gating therefore DEMOTES a task into the existing
# UNRESOLVED state rather than inventing a parallel one — which is also why the
# existing AI_ACTION_REQUIRED notification and the existing
# resolve-assignee flow light up for it with no extra wiring.
#
# CONFIDENCE IS THE MODEL'S CLAIM, GATING IS OURS. `ai_confidence` is stored
# verbatim as what the model said; the gate below is a separate, deterministic
# decision about what to DO with that claim. Keeping them apart is deliberate —
# a stored field that mixed "the model was unsure" with "we downgraded it"
# could not answer either question later.
#
# THE GATE, and why each band behaves as it does:
#
#   high    -> trust the resolution as-is. The model says the work and its
#              owner are both explicit; the speaker chain either found an
#              account or it did not, and that outcome stands.
#
#   medium  -> trust it too. "The work is clear, the owner needs context"
#              describes ordinary meeting speech, and the resolution chain is
#              itself evidence-based (a mapped speaker is a fact, not a guess).
#              Demoting these would bury real assignments under review noise,
#              which trains people to ignore the review queue.
#
#   low     -> NEVER auto-assign. The model is telling us the work or its owner
#              is genuinely ambiguous. A speaker-derived contact match on an
#              ambiguous utterance is precisely the case that produces a
#              confidently-wrong assignee, so the assignment is withheld and
#              the owner is asked. The task is still CREATED — the work was
#              discussed and deleting it would lose real information.
#
#   ""      -> no claim was made (an older row, or a model answer the enum
#              refused). Treated like medium: absence of a score is not
#              evidence of doubt, and demoting every unscored task would put
#              the entire back catalogue into review.
#
# EVIDENCE. A task the model could not quote is not blocked — the work may
# still be real and the transcript is on the row either way — but it cannot be
# auto-assigned on a LOW-confidence reading either, which the gate already
# covers. Evidence is recorded so the user can check the claim; it is not used
# as a second gate, because a missing quote is a formatting failure, not a
# statement about who owes the work.
AI_CONFIDENCE_GATED = (ai_schema.CONFIDENCE_LOW,)


def _gate_ai_assignment(raw_confidence, contact, assignee_name):
    """Decide whether an AI task may keep its resolved assignee.

    Returns (contact, assignee_name, gated) — the values to build the row
    with, and whether the gate fired. A gated task keeps everything it knows
    (the speaker id, the spoken name, the evidence) but is NOT handed a
    Contact, so `_new_task_row` files it as UNRESOLVED and the owner is asked
    to confirm through the flow that already exists for that state.

    The spoken NAME is preserved rather than dropped: "Rahul" is what the
    reviewer needs in order to answer the question, and throwing it away would
    make the review harder than the extraction.
    """
    confidence = ai_schema.coerce_confidence(raw_confidence)
    if confidence not in AI_CONFIDENCE_GATED:
        return contact, assignee_name, False
    if contact is None and not assignee_name:
        # Nothing was claimed, so there is nothing to withhold. This is an
        # unassigned task, which is a normal outcome and not a review item.
        return None, "", False

    # Demote. A contact that WAS matched becomes the contact's name, so the
    # reviewer sees who the system nearly picked and can confirm in one tap.
    kept_name = assignee_name or (contact or {}).get("name", "")
    return None, kept_name, True


def _seed_ai_tasks(user_id, key, item):
    """Create first-class Tasks from the AI's extracted `ai_tasks`, once.

    Replaces the embedded map's seeding path. Idempotent through the
    fingerprint index, which is stronger than the old "is the map empty?"
    check: it survives the user deleting one seeded task (that task stays
    deleted instead of reappearing) and it makes a second AI run — a
    reprocess, a retried invocation, two devices opening the meeting at once —
    a no-op rather than a duplicate (section 20).

    Assignees arrive as NAMES from the transcript. They become UNRESOLVED
    tasks, except where the meeting's speaker mapping already identifies the
    speaker, in which case the chain of section 18 resolves them properly.
    """
    source = item.get("ai_tasks") or []
    if not isinstance(source, list) or not source:
        return 0

    # Speaker -> Contact for this meeting, so an AI task naming a mapped
    # speaker resolves immediately instead of waiting to be resolved twice.
    by_speaker = {}
    for row in _participant_rows(key):
        cid = row.get("contact_id")
        if not cid:
            continue
        c = _contacts.get_item(Key={"contact_id": cid}).get("Item")
        if c and c.get("owner_user_id") == user_id:
            # Keyed on the NORMALIZED id. The AI copies the transcript's own
            # "Speaker 0" label while participant rows hold the compact "0",
            # and comparing those two directly is what made self-assignment
            # silently fail. Normalizing BOTH sides is the fix.
            by_speaker[stt_result.normalize_speaker_id(row.get("speaker_id"))] = c

    # The meeting's own day is the only correct anchor for "Friday" or
    # "tomorrow". Resolving against NOW would silently re-point an old
    # meeting's deadlines every time this seeder ran. None (no usable
    # timestamp) leaves every date unresolved rather than guessing.
    anchor = spoken_dates.anchor_date(item.get("recorded_at"),
                                      item.get("created_at"))

    tombstoned = set(_deleted_fingerprints(item))
    created = 0
    for raw in source[:MAX_TASKS]:
        if not isinstance(raw, dict):
            continue
        title = str(raw.get("task") or "").strip()
        if not title:
            continue
        assignee_name = str(raw.get("assignee") or "").strip()
        # Normalized at the boundary, so everything downstream — the lookup
        # below, the stored value, the display map, and the later
        # _resolve_tasks_for_speaker join — all speak the same dialect.
        speaker_id = stt_result.normalize_speaker_id(
            raw.get("assignee_speaker_id"))
        fingerprint = _task_fingerprint(key, title, assignee_name or speaker_id)
        if fingerprint in tombstoned:
            continue  # the user deleted this one; do not resurrect it
        if _find_task_by_fingerprint(user_id, fingerprint):
            continue

        contact = by_speaker.get(speaker_id) if speaker_id else None
        priority = raw.get("priority") or "Medium"
        if priority not in TASK_PRIORITIES:
            priority = "Medium"
        spoken_due = raw.get("due_date") or ""
        # CONFIDENCE GATE, applied after resolution and before the row is
        # built: a LOW-confidence reading never gets to keep an auto-derived
        # assignee, however well the speaker chain matched. See
        # _gate_ai_assignment for why medium and unscored are trusted.
        gated_contact, gated_name, was_gated = _gate_ai_assignment(
            raw.get("confidence"), contact, assignee_name)
        row = _new_task_row(
            user_id, title,
            recording_key=key,
            # Inherited from the MEETING, never from the caller: AI seeding
            # runs from an async invoke where "the caller" is a Lambda, not a
            # person. Section 20 of the brief.
            workspace_id=_row_workspace_id(item),
            due=spoken_due,
            due_normalized=spoken_dates.normalize_spoken_date(
                spoken_due, anchor),
            priority=priority,
            source_type=TASK_SOURCE_AI,
            assignee_contact=gated_contact,
            assignee_name="" if gated_contact else gated_name,
            assignee_speaker_id=speaker_id,
            # Stored VERBATIM as the model's own claim — the gate above is a
            # separate decision and does not rewrite what the model said.
            ai_confidence=ai_schema.coerce_confidence(raw.get("confidence")),
            ai_evidence=str(raw.get("evidence") or ""),
            fingerprint=fingerprint,
        )
        # WHERE the evidence sits in the transcript, so the app can offer
        # "view in transcript" rather than only quoting the line. Already
        # validated against the real segment list by coerce_analysis, and
        # written only when present so the attribute stays absent (rather
        # than an empty list) on rows that have none.
        segment_ids = [str(i) for i in (raw.get("evidence_segment_ids") or [])
                       if str(i).strip()][:MAX_EVIDENCE_SEGMENTS]
        if segment_ids:
            row["ai_evidence_segment_ids"] = segment_ids
        if was_gated:
            print(f"[seed] {key}: low-confidence assignment withheld for "
                  f"review: {title[:60]!r}")
        try:
            _write_task(row)
        except ClientError as err:
            # Lost a race with a concurrent seeder — its row is as good as ours.
            if err.response.get("Error", {}).get("Code") \
                    != "ConditionalCheckFailedException":
                raise
            continue
        _mirror_task_to_recording(key, row)
        # Two DIFFERENT facts, and a task is only ever one of them:
        #
        #   * the AI named a speaker we could map to a linked account -> the
        #     assignee is real, so they are told (TASK_ASSIGNED);
        #   * the AI named someone we could NOT resolve -> nobody was
        #     assigned, and the OWNER is asked to confirm rather than the
        #     system guessing a person (AI_ACTION_REQUIRED, section 13).
        #
        # A task the AI left unassigned entirely (RESOLUTION_NONE) raises
        # neither: there is no ambiguity to review and nobody to notify.
        if _task_assignee_user(row):
            _notify_task_assigned(row, actor_user_id=user_id)
        elif row.get("resolution_status") == RESOLUTION_UNRESOLVED:
            _notify_ai_action_required(row)
        created += 1
    if created:
        print(f"[seed] {key}: {created} AI task(s) created")
    return created


def _resolve_tasks_for_speaker(user_id, key, speaker_id, contact):
    """Attach a newly-mapped speaker's Contact to that speaker's open tasks.

    This is the join that completes section 18: the AI recorded WHICH SPEAKER
    owed the task, the user has now said who that speaker is, so the task can
    finally name a person — including their MinuteX user id when they have an
    account, which is what makes it notification-ready.

    Only tasks whose assignee_speaker_id matches are touched, and only ones
    not already resolved to a contact: a user who hand-assigned a task keeps
    their choice.

    ATTENDANCE ROWS NEVER GET HERE. set_participant already returns before
    calling this for a self-tag; the guard below makes the rule true of the
    FUNCTION rather than of one call site, so a future caller cannot let "I
    was in the room" assign somebody work the AI attributed to a voice.
    """
    if _is_self_speaker(speaker_id):
        return 0
    resolved = 0
    for row in _tasks_for_recording(key):
        if row.get("owner_user_id") != user_id:
            continue
        # Both sides normalized: the row may carry a pre-fix "Speaker 0"
        # written before this normalization existed, and the caller passes the
        # participant's compact id. Neither is trusted to already match.
        if stt_result.normalize_speaker_id(row.get("assignee_speaker_id"))                 != stt_result.normalize_speaker_id(speaker_id):
            continue
        if row.get("assignee_contact_id"):
            continue
        updates = {
            "assignee_contact_id": contact["contact_id"],
            "assignee_name": contact.get("name", ""),
            "assignee_email": contact.get("email", ""),
            "assignee_phone": contact.get("phone", ""),
            "resolution_status": RESOLUTION_RESOLVED,
            "updated_at": _now_iso(),
        }
        removes = ["assignee_name_legacy"]
        linked = contact.get("minutex_user_id") or ""
        if linked:
            updates["assignee_user_id"] = linked
        else:
            removes.append("assignee_user_id")
        _apply_update(_tasks, {"task_id": row["task_id"]}, updates, removes)
        # Naming a speaker is what finally gives these tasks a real recipient
        # (section 18's chain). Read back rather than assuming: only a contact
        # LINKED to a MinuteX account produces an assignee_user_id, and
        # _notify_task_assigned is what decides whether there is anyone to tell.
        fresh = _tasks.get_item(Key={"task_id": row["task_id"]}).get("Item")
        if fresh:
            _notify_task_assigned(fresh, actor_user_id=user_id)
        resolved += 1
    return resolved


def list_meeting_tasks(event):
    """GET /recordings/ai/tasks/{key+} -> {tasks}

    Same route the app already calls, now served from the Tasks table. On the
    way it (1) migrates any legacy embedded tasks and (2) seeds the AI's
    extracted tasks — both idempotent, so this is safe on every call and a
    meeting the AI found work in is never empty.
    """
    user_id, key, item = _owned_recording(event, hydrate=False)
    _migrate_embedded_tasks(user_id, key, item)
    _seed_ai_tasks(user_id, key, item)
    rows = [r for r in _tasks_for_recording(key)
            if r.get("owner_user_id") == user_id]
    rows.sort(key=lambda r: r.get("created_at", ""))
    # Same sweep as the Task Tracker, over the rows this route already read.
    _sweep_task_deadlines(user_id, rows)
    # The recording row is already loaded, so speaker names cost no extra read.
    names = item.get("speaker_names") or {}
    return _resp(200, {"tasks": [_public_task_v2(r, names) for r in rows],
                       "count": len(rows)})


def _manual_due(data):
    """The due value a client sent, under either key it may use."""
    return str(data.get("due") if "due" in data
               else data.get("due_date") or "").strip()


def create_meeting_task(event):
    """POST /recordings/ai/tasks/{key+} {task, due?, priority?, status?,
    assignee_contact_id?, assignee?} -> 201 {task}

    A task the user adds themselves. `assignee_contact_id` is the first-class
    path (a real Contact); the legacy `assignee: {name}` object is still
    accepted from older clients and stored as an UNRESOLVED name rather than
    being guessed into a contact.
    """
    user_id, key, item = _owned_recording(event, hydrate=False)
    data = _body(event)

    title = str(data.get("task") or data.get("title") or "").strip()
    if not title:
        raise ApiError(400, "task required")

    existing = [r for r in _tasks_for_recording(key)
                if r.get("owner_user_id") == user_id]
    if len(existing) >= MAX_TASKS:
        raise ApiError(400, f"too many tasks (max {MAX_TASKS})")

    contact = None
    if data.get("assignee_contact_id"):
        # Materializes a projected organisation member, since the task row
        # stores assignee_contact_id as a foreign key (Phase 2D).
        contact = _taggable_contact(user_id, data["assignee_contact_id"],
                                    workspace_id=_row_workspace_id(item))
    legacy_name = ""
    if contact is None and isinstance(data.get("assignee"), dict):
        legacy_name = str(data["assignee"].get("name") or "").strip()

    row = _new_task_row(
        user_id, title,
        recording_key=key,
        workspace_id=_row_workspace_id(item),
        description=data.get("description") or "",
        due=_manual_due(data),
        due_normalized=spoken_dates.normalize_spoken_date(
            _manual_due(data),
            spoken_dates.anchor_date(item.get("recorded_at"),
                                     item.get("created_at"))),
        priority=(_clean_task_priority(data["priority"])
                  if data.get("priority") else "Medium"),
        status=(_clean_task_status(data["status"])
                if data.get("status") else TASK_STATUS_OPEN),
        source_type=TASK_SOURCE_MANUAL,
        assignee_contact=contact,
        assignee_name=legacy_name,
    )
    _write_task(row)
    _mirror_task_to_recording(key, row)
    _audit("task.created", user_id, row["task_id"], recording_key=key)
    # AFTER the task exists, never before: a notification pointing at a task
    # that failed to write is a dead tap. `actor_user_id` stops a user who
    # assigned work to themselves being told about it.
    _notify_task_assigned(row, actor_user_id=user_id,
                          meeting_title=_recording_title(item))
    return _resp(201, {"task": _public_task_v2(row,
                                               item.get("speaker_names") or {})})


def update_meeting_task(event):
    """PATCH /recordings/ai/tasks/{key+} {id, ...} -> {task}

    The one mutation route, exactly as before (edit / reassign / status /
    notification-record all patch the same row). Status changes stamp or clear
    `completed_at` so a completion time is real rather than inferred.
    """
    user_id, key, item = _owned_recording(event, hydrate=False)
    data = _body(event)
    task_id = str(data.get("id") or "").strip()
    if not task_id:
        raise ApiError(400, "id required")

    # Migrate first: the id the client holds may be a legacy map id it got
    # from a previous build, which only exists in the Tasks table afterwards.
    _migrate_embedded_tasks(user_id, key, item)
    row = _find_task_for_update(user_id, key, task_id)
    # Reaching this route already required owning the MEETING, and
    # _find_task_for_update already required owning the TASK — so the caller
    # is the creator. The check is kept anyway: it is the one place the field
    # rules live, and a future change that relaxes either predicate must not
    # silently open every field to an assignee.
    _authorize_task_patch(user_id, row, data)
    updates, removes = {}, []

    if "task" in data or "title" in data:
        title = str(data.get("task") or data.get("title") or "").strip()
        if not title:
            raise ApiError(400, "task cannot be empty")
        updates["title"] = title[:MAX_TASK_TEXT]
    if "description" in data:
        updates["description"] = str(
            data.get("description") or "").strip()[:MAX_TASK_NOTE_TEXT]
    if "due" in data or "due_date" in data:
        raw = data.get("due") if "due" in data else data.get("due_date")
        updates["due_date"] = str(raw or "").strip()[:100]
        # Re-resolve rather than leaving a stale day behind: an edited due
        # date whose normalized twin still pointed at the old one would make
        # overdue/due_before disagree with what the user just typed.
        updates["due_date_normalized"] = spoken_dates.normalize_spoken_date(
            updates["due_date"],
            spoken_dates.anchor_date(item.get("recorded_at"),
                                     item.get("created_at")))
    if "priority" in data:
        updates["priority"] = _clean_task_priority(data["priority"])
    if "status" in data:
        status = _clean_task_status(data["status"])
        updates["status"] = status
        if status == TASK_STATUS_COMPLETED:
            # Preserve an existing completion time — re-saving a completed task
            # must not move the moment it was finished.
            if not row.get("completed_at"):
                updates["completed_at"] = _now_iso()
        else:
            updates["completed_at"] = ""

    if "assignee_contact_id" in data:
        raw = data.get("assignee_contact_id")
        if raw is None or str(raw).strip() == "":
            removes.extend(["assignee_contact_id", "assignee_user_id"])
            updates["assignee_name"] = ""
            updates["assignee_email"] = ""
            updates["assignee_phone"] = ""
            updates["resolution_status"] = RESOLUTION_NONE
        else:
            contact = _taggable_contact(
                user_id, str(raw).strip(),
                workspace_id=_row_workspace_id(row, owner_field="owner_user_id"))
            updates["assignee_contact_id"] = contact["contact_id"]
            updates["assignee_name"] = contact.get("name", "")
            updates["assignee_email"] = contact.get("email", "")
            updates["assignee_phone"] = contact.get("phone", "")
            updates["resolution_status"] = RESOLUTION_RESOLVED
            removes.append("assignee_name_legacy")
            linked = contact.get("minutex_user_id") or ""
            if linked:
                updates["assignee_user_id"] = linked
            else:
                removes.append("assignee_user_id")
    elif "assignee" in data:
        # Legacy shape from an older client: a NAME. Kept unresolved.
        raw = data.get("assignee")
        if raw is None:
            removes.extend(["assignee_contact_id", "assignee_user_id",
                            "assignee_name_legacy"])
            updates["assignee_name"] = ""
            updates["resolution_status"] = RESOLUTION_NONE
        elif isinstance(raw, dict):
            name = str(raw.get("name") or "").strip()
            if not name:
                raise ApiError(400, "assignee.name required")
            updates["assignee_name_legacy"] = name[:CONTACT_NAME_MAX]
            updates["assignee_name"] = ""
            updates["resolution_status"] = RESOLUTION_UNRESOLVED
            removes.extend(["assignee_contact_id", "assignee_user_id"])
        else:
            raise ApiError(400, "assignee must be an object or null")

    if "notify_channels" in data:
        raw = data.get("notify_channels")
        if not isinstance(raw, list):
            raise ApiError(400, "notify_channels must be an array")
        channels = [c for c in (str(c).strip().lower() for c in raw)
                    if c in NOTIFY_CHANNELS]
        # ADDED, not replaced — same rule as before: a channel already
        # notified stays marked when a later call notifies only the others.
        updates["notified_via"] = sorted(
            set(row.get("notified_via") or []) | set(channels))

    if not updates and not removes:
        raise ApiError(400, "nothing to update")

    # Captured BEFORE the write: after it, the row no longer knows who used to
    # hold the task, and that is exactly who TASK_REASSIGNED has to reach.
    previous_assignee = _task_assignee_user(row)

    updates["updated_at"] = _now_iso()
    _apply_update(_tasks, {"task_id": row["task_id"]}, updates, removes)
    fresh = _tasks.get_item(Key={"task_id": row["task_id"]}).get("Item") or {}
    _mirror_task_to_recording(key, fresh)
    _audit("task.updated", user_id, row["task_id"],
           fields=sorted(set(updates) | set(removes)))
    # A reassignment is TWO facts for two different people, and only when the
    # assignee actually changed — an edit to the title or the due date of an
    # already-assigned task must not re-notify anyone.
    if _task_assignee_user(fresh) != previous_assignee:
        _notify_task_reassigned(previous_assignee, fresh, actor_user_id=user_id)
        _notify_task_assigned(fresh, actor_user_id=user_id,
                              meeting_title=_recording_title(item))
    return _resp(200, {"task": _public_task_v2(fresh,
                                               item.get("speaker_names") or {})})


def _find_task_for_update(user_id, key, task_id):
    """Resolve a task id that may be either a Tasks-table id or a legacy
    embedded-map id, so a client holding an old id still works after migration.
    """
    row = _tasks.get_item(Key={"task_id": task_id}).get("Item")
    if row and row.get("owner_user_id") == user_id:
        return row
    for candidate in _tasks_for_recording(key):
        if candidate.get("legacy_task_id") == task_id \
                and candidate.get("owner_user_id") == user_id:
            return candidate
    raise ApiError(404, "task not found")


def delete_meeting_task(event):
    """DELETE /recordings/ai/tasks/{key+} {id} -> {deleted, id}

    Removes the authoritative row AND its legacy mirror entry, so a deleted
    task cannot come back from the map on a later migration pass.
    """
    user_id, key, item = _owned_recording(event, hydrate=False)
    data = _body(event)
    task_id = str(data.get("id") or "").strip()
    if not task_id:
        raise ApiError(400, "id required")

    _migrate_embedded_tasks(user_id, key, item)
    row = _find_task_for_update(user_id, key, task_id)
    _tasks.delete_item(Key={"task_id": row["task_id"]})
    # An AI-seeded task carries a fingerprint, which is also what stops it
    # being seeded twice. Removing the row removes that guard, so the deletion
    # must be recorded explicitly or the next read re-creates it.
    _tombstone_fingerprint(key, item, row.get("fingerprint") or "")

    for mirror_id in filter(None, [row["task_id"], row.get("legacy_task_id")]):
        try:
            _recordings.update_item(
                Key={"audio_s3_key": key},
                UpdateExpression="SET updated_at = :now REMOVE #tasks.#t",
                ExpressionAttributeNames={"#tasks": TASKS_ATTR,
                                          "#t": mirror_id},
                ExpressionAttributeValues={":now": _now_iso()},
            )
        except ClientError as err:
            # The map (or the entry) may simply not exist — not an error.
            if err.response.get("Error", {}).get("Code") \
                    not in ("ValidationException",):
                raise
    _audit("task.deleted", user_id, row["task_id"], recording_key=key)
    return _resp(200, {"deleted": True, "id": task_id})


def list_all_tasks(event):
    """GET /tasks?status=&assignee_contact_id=&recording_key=
                  &overdue=&due_before=&assigned_to_me=&limit=&cursor=
       -> {tasks, count, next_cursor}

    The cross-meeting task query the Task Tracker is built on (section 21).
    Every filter is applied SERVER-side, and the query is driven off whichever
    GSI the filter set makes cheapest — assignee-index for an assignee,
    meeting-index for one meeting, otherwise owner-index. Filters that
    DynamoDB cannot express as a key condition (overdue, which depends on the
    current time) are applied after the read, which is why the page loop below
    keeps reading until the page fills.
    """
    user_id = _require_auth(event)
    qs = event.get("queryStringParameters") or {}
    limit = _clean_limit(qs.get("limit"), TASKS_PAGE_DEFAULT, TASKS_PAGE_MAX)

    status = str(qs.get("status") or "").strip()
    status = _clean_task_status(status) if status else ""
    assignee_contact_id = str(qs.get("assignee_contact_id") or "").strip()
    recording_key = _url_unquote(str(qs.get("recording_key") or "").strip())
    overdue_only = str(qs.get("overdue") or "").strip().lower() in ("1", "true")
    assigned_to_me = str(qs.get("assigned_to_me")
                         or "").strip().lower() in ("1", "true")
    due_before = str(qs.get("due_before") or "").strip()

    # Ownership of a filter target is checked BEFORE it is used as a key: a
    # contact id the caller doesn't own must 404, not silently return that
    # other tenant's tasks.
    #
    # Deliberately _owned_contact and NOT _taggable_contact: this is a GET,
    # and filtering by a colleague must never WRITE a contact row as a side
    # effect. _owned_contact resolves a projected member for reading anyway,
    # and a member with no stored row has no tasks keyed to it — so the
    # filter correctly returns nothing instead of materializing a row to
    # prove it.
    if assignee_contact_id:
        _owned_contact(user_id, assignee_contact_id)

    # Set only on the unfiltered path (see below): a second index read whose
    # rows are merged into the page. None for every explicit filter, each of
    # which already names the one partition it wants.
    extra_query = None

    if assignee_contact_id:
        base = {"IndexName": TASKS_ASSIGNEE_INDEX,
                "KeyConditionExpression":
                    Key("assignee_contact_id").eq(assignee_contact_id)}
    elif recording_key:
        base = {"IndexName": TASKS_MEETING_INDEX,
                "KeyConditionExpression":
                    Key("source_recording_id").eq(recording_key)}
    elif assigned_to_me:
        # "Tasks I must execute" — keyed on the ACCOUNT, so it returns work
        # assigned by OTHER people too. The owner-index cannot answer this:
        # its partition is the creator, so a task User A created for User B
        # simply is not in B's partition, and post-filtering would mean a
        # full table scan.
        base = {"IndexName": TASKS_ASSIGNEE_USER_INDEX,
                "KeyConditionExpression":
                    Key("assignee_user_id").eq(user_id)}
    else:
        base = {"IndexName": TASKS_OWNER_INDEX,
                "KeyConditionExpression": Key("owner_user_id").eq(user_id)}
        # The UNFILTERED list must show both directions of a user's work: what
        # they created AND what was assigned to them. Those live in two
        # different partitions (creator vs assignee), and a GSI query can only
        # read one — so the assignee side is read as a SECOND query and merged
        # below. Without it a task User A created for User B would be missing
        # from every one of B's views except "My tasks", which is exactly the
        # gap section 8 calls out.
        extra_query = {"IndexName": TASKS_ASSIGNEE_USER_INDEX,
                       "KeyConditionExpression":
                           Key("assignee_user_id").eq(user_id),
                       "ScanIndexForward": False}
    base["ScanIndexForward"] = False

    cursor = _decode_cursor(qs.get("cursor"))
    if cursor:
        base["ExclusiveStartKey"] = cursor

    def _keep(row):
        """Every filter that is not expressed as a key condition.

        Shared by both index reads below so the creator side and the assignee
        side can never apply different rules to the same task.
        """
        # EVERY row is re-checked against the caller, including on indexes
        # not keyed by owner (folder/assignee/meeting): the index is a
        # lookup path, never an authorization decision.
        #
        # The predicate is "may this caller SEE this task" — creator OR
        # assignee — not "did this caller create it". Checking ownership
        # alone is what used to hide a task from the very person meant to
        # do it: User A's task assigned to User B never reached B's list.
        if not (_is_task_creator(user_id, row)
                or _is_task_assignee(user_id, row)):
            return False
        if status and row.get("status") != status:
            return False
        if assigned_to_me and row.get("assignee_user_id") != user_id:
            return False
        if overdue_only and not _is_overdue(
                row.get("due_date"), row.get("status"),
                row.get("due_date_normalized", "")):
            return False
        if due_before:
            # Compare on the RESOLVED day; a spoken "Friday" is a real
            # deadline and belongs in a due_before window. Falling back to
            # the raw value keeps pre-normalization rows behaving as
            # before rather than dropping out of the filter entirely.
            due = (str(row.get("due_date_normalized") or "").strip()
                   or str(row.get("due_date") or ""))
            if not due or due > due_before:
                return False
        return True

    out, last_key = [], None
    for _ in range(_SEARCH_MAX_PAGES):
        base["Limit"] = max(limit * 2, 100)
        res = _tasks.query(**base)
        out.extend(r for r in res.get("Items", []) if _keep(r))
        last_key = res.get("LastEvaluatedKey")
        if not last_key or len(out) >= limit:
            break
        base["ExclusiveStartKey"] = last_key

    # The assignee half of the unfiltered list, merged in.
    #
    # FIRST PAGE ONLY, on purpose. `next_cursor` is an ExclusiveStartKey for
    # ONE index, so a paginated union of two indexes cannot be resumed
    # coherently — a cursor into owner-index means nothing to
    # assignee-user-index. Reading the assignee side only when there is no
    # incoming cursor keeps the contract honest: the first page is the union
    # (which is what the dashboard renders), and paging past it continues
    # through the creator's own tasks exactly as it always has.
    #
    # This is a real limit rather than a hidden one — see the comment on
    # next_cursor below — and it only bites a user with more than one page of
    # created tasks who ALSO has tasks assigned to them by others.
    #
    # Skipped once the creator's own tasks already fill the page: the merge
    # cannot emit a resumable cursor (see next_cursor below), so merging into
    # a page that still has more to give would strand the remainder. A user
    # with a full page of their own tasks reads them normally and finds
    # assigned work under "My tasks"; the merge exists for the ordinary case
    # where the first page has room.
    if extra_query is not None and not cursor and len(out) <= limit:
        seen = {r.get("task_id") for r in out}
        extra_query["Limit"] = max(limit * 2, 100)
        try:
            extra = _tasks.query(**extra_query)
        except ClientError as err:
            # The GSI may not exist yet on an environment that has not run
            # scripts/44_add_assignee_user_index.sh. Degrading to "creator's
            # tasks only" is strictly better than 500-ing the whole dashboard,
            # and it is loud in the logs rather than silent.
            code = err.response.get("Error", {}).get("Code")
            if code in ("ValidationException", "ResourceNotFoundException"):
                print(f"[warn] {TASKS_ASSIGNEE_USER_INDEX} unavailable: {err}")
                extra = {"Items": []}
            else:
                raise
        for row in extra.get("Items", []):
            # Deduped by task_id: a task a user created AND is assigned to
            # appears in both indexes and must be listed once.
            if row.get("task_id") in seen or not _keep(row):
                continue
            seen.add(row.get("task_id"))
            out.append(row)
        # Both indexes are sorted newest-first individually; the merged list
        # has to be re-sorted to keep that order across the two.
        out.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)

    # The cursor must resume from the last row we actually RETURNED, not from
    # wherever the index scan happened to stop.
    #
    # Those are different positions whenever the filters trimmed the page, and
    # getting it wrong is silent: an earlier version emitted "" when the results
    # overflowed the page, so a client with 7 matching tasks and limit=3 saw
    # three and stopped, with the other four unreachable. A task tracker that
    # quietly hides work is worse than one that errors.
    #
    # Both index key attributes AND the table key go into the cursor, because
    # ExclusiveStartKey on a GSI query needs enough to identify the row in both
    # the index and the base table.
    #
    # A MERGED page (the unfiltered union above) cannot emit a row-anchored
    # cursor at all: its last row may have come from assignee-user-index, and
    # an ExclusiveStartKey built from that row is meaningless to owner-index —
    # DynamoDB would reject it, or worse, resume from the wrong place. So the
    # merged page falls back to the creator index's own LastEvaluatedKey,
    # which resumes the creator's tasks correctly. Assigned-by-others tasks
    # are all on page one; "My tasks" (assigned_to_me) pages through them
    # properly on its own index.
    merged = (extra_query is not None and not cursor
              and any(not _is_task_creator(user_id, r) for r in out))
    overflowed = len(out) > limit
    out = out[:limit]
    next_cursor = ""
    if overflowed and out and not merged:
        anchor = out[-1]
        index_name = base.get("IndexName") or ""
        cursor_key = {"task_id": anchor["task_id"]}
        for attr in _TASK_INDEX_KEYS.get(index_name, ()):
            if anchor.get(attr) not in (None, ""):
                cursor_key[attr] = anchor[attr]
        next_cursor = _encode_cursor(cursor_key)
    elif last_key:
        next_cursor = _encode_cursor(last_key)
    # Deadlines are noticed HERE because nothing in MinuteX fires at the
    # moment a task falls due (see the sweep's own header). It costs no extra
    # reads — these rows are already loaded and already ownership-checked —
    # and the day-scoped dedupe key makes it exactly-once per day however
    # often the tracker is opened.
    _sweep_task_deadlines(user_id, out)

    # One recording read per DISTINCT meeting on this page, not per task —
    # a page of 50 tasks from 3 meetings costs 3 reads (see
    # _speaker_names_for_recording on why the cache is per-request).
    names_cache = {}
    return _resp(200, {
        "tasks": [_public_task_v2(
                      r, _speaker_names_for_recording(
                          r.get("source_recording_id"), names_cache))
                  for r in out],
        "count": len(out), "next_cursor": next_cursor})


def get_task(event):
    """GET /tasks/{task_id} -> {task, contact?, folder?, recording?}

    The task detail screen's one call: the task plus the entities it points at
    (section 13's who / where / why), each ownership-checked in its own right.

    VISIBLE to the creator AND the assignee — the assignee cannot act on work
    they cannot open. Everyone else gets 404. The related entities below are
    still resolved against the CALLER, so an assignee sees the task without
    inheriting any read access to the creator's contacts or folders.
    """
    user_id = _require_auth(event)
    task_id = (event.get("pathParameters") or {}).get("task_id", "")
    row = _visible_task(user_id, task_id)
    out = {}
    # What this caller may DO with the task, decided server-side and sent to
    # the client so the UI never has to re-derive the rule (and cannot get it
    # wrong). The backend stays the enforcement point regardless.
    out["permissions"] = _task_permissions(user_id, row)

    # THE RELATED ENTITIES, AND WHY THE ASSIGNEE SEES A NARROWER SET.
    #
    # These records belong to the CREATOR — the contact is a row in their
    # address book, the folder is their workspace, the meeting is theirs. An
    # assignee owns none of them, so an ownership-gated read returns nothing
    # and the task arrives context-free. That is what made the detail screen
    # tell an assignee "Nobody is assigned to this task": the assignee WAS
    # set on the row, but the `contact` the UI renders it from was withheld.
    #
    # The fix is not to hand the assignee the creator's records. It is to
    # answer the two questions they legitimately have — "who is this for?"
    # and "where did it come from?" — from the TASK ROW itself, which already
    # carries the assignee's display fields, plus a deliberately minimal
    # projection of the meeting. Nothing here exposes the creator's other
    # contacts, their folder contents, or the transcript.
    creator = _is_task_creator(user_id, row)

    cid = row.get("assignee_contact_id")
    if cid:
        c = _contacts.get_item(Key={"contact_id": cid}).get("Item")
        if c and c.get("owner_user_id") == user_id:
            out["contact"] = _public_contact(c)
        elif _is_task_assignee(user_id, row):
            # The assignee, seeing THEMSELVES. Built from the task row rather
            # than the creator's contact record: it is the same person, and
            # this way no address-book row crosses a tenant boundary. The id
            # is deliberately omitted — it addresses a contact they cannot
            # open, and offering it would only produce a 404 on tap.
            out["contact"] = {
                "id": "",
                "name": row.get("assignee_name", ""),
                "email": row.get("assignee_email", ""),
                "phone": row.get("assignee_phone", ""),
            }
    # The recording is read for the `recording` block anyway, so its
    # speaker_names come along free — no second read to resolve the assignee.
    # `speaker_names` is also returned so the detail screen can render the
    # "this came from <name>" line without a separate participants call.
    speaker_names = {}
    key = row.get("source_recording_id")
    if key:
        rec = _recordings.get_item(Key={"audio_s3_key": key}).get("Item")
        # Speaker names are meeting CONTENT, so they follow meeting read
        # authorization rather than a local creator test — which also means a
        # member removed from the owning organisation stops seeing them.
        owns_recording = bool(_can_read_meeting(user_id, rec))
        if owns_recording:
            got = rec.get("speaker_names")
            speaker_names = got if isinstance(got, dict) else {}
            out["recording"] = {"audio_s3_key": key,
                                "title": rec.get("title", ""),
                                "recorded_at": rec.get("recorded_at", ""),
                                "speaker_names": speaker_names,
                                # The full meeting screen. Stated rather than
                                # implied by the absence of the assignee
                                # marker, so the app switches on one field.
                                "access": "owner"}
        elif rec and not creator and not _is_trashed(rec):
            # WHERE THIS CAME FROM, for the assignee. A task that arrives with
            # no provenance reads as if it appeared from nowhere; the meeting
            # title and date are what make it accountable work rather than an
            # anonymous instruction.
            #
            # THE KEY IS SENT, and it addresses a real route for them: the
            # read-only notes view at /recordings/shared-with-me/{key+} (see
            # the Assignee Meeting Access section). It is NOT a route into the
            # owner's meeting screen — that one still gates on _owned_recording
            # and 404s for this caller, which is why `access` below tells the
            # app which of the two screens the key is good for.
            #
            # STILL NO FOLDER AND NO speaker_names. The folder is the owner's
            # workspace and addresses a screen the assignee cannot open, and
            # an empty speaker_names is what makes _public_task_v2 fall back to
            # the stored assignee string instead of resolving a speaker label
            # out of the creator's meeting.
            out["recording"] = {"audio_s3_key": key,
                                "title": rec.get("title", ""),
                                "recorded_at": rec.get("recorded_at", ""),
                                "speaker_names": {},
                                # Read-only notes, not the full meeting. The
                                # server enforces this either way; the field
                                # exists so the app routes to the right screen
                                # instead of guessing from who is calling.
                                "access": "assignee"}
    # WHO GAVE ME THIS WORK. Shown to the assignee, and to them only — the
    # creator is looking at a task they made and does not need to be told.
    #
    # The CREATOR is the assigner. MinuteX does not store an `assigned_by`
    # separate from `owner_user_id`, and for these tasks the two are the same
    # person by construction: only the creator can assign or reassign (that is
    # the permission model), and an AI-seeded task is assigned by the meeting
    # owner who recorded it. Inventing a second field would be a schema change
    # to record what owner_user_id already means.
    #
    # NAME AND PHOTO ONLY. Not the email — the assignee has no relationship
    # with the creator's account beyond this task, and an address is contact
    # detail, not provenance.
    if not creator:
        # Projected, so the promise above is enforced by the QUERY and not
        # merely by which keys the dict literal happens to copy.
        u = _users.get_item(
            Key={"user_id": _task_creator(row)},
            ProjectionExpression="#n, avatar_url",
            ExpressionAttributeNames={"#n": "name"}).get("Item") or {}
        if u:
            out["assigned_by"] = {
                "name": u.get("name", ""),
                "avatar_view_url": _avatar_view_url(u.get("avatar_url", "")),
            }
    out["task"] = _public_task_v2(row, speaker_names)
    return _resp(200, {**out})


def _update_task_status_as_assignee(user_id, row, data):
    """Apply a STATUS-ONLY patch on behalf of the task's assignee.

    Deliberately narrow: it writes `status`, the completion timestamp that
    belongs to it, and `updated_at`. Nothing else is touched, so every piece
    of AI provenance on the row — confidence, evidence, evidence segment ids,
    speaker id, resolution status, deadline provenance, folder — survives an
    assignee moving the task along, which section 16 requires.

    Status validation goes through _clean_task_status, the SAME helper the
    creator's path uses, so the two callers can never accept different values.
    """
    if "status" not in data:
        # Nothing this caller is allowed to change was actually sent.
        raise ApiError(400, "status required")
    status = _clean_task_status(data["status"])
    updates = {"status": status, "updated_at": _now_iso()}
    if status == TASK_STATUS_COMPLETED:
        # Preserve an existing completion time, exactly as the creator's path
        # does — re-completing must not move when the work was finished.
        if not row.get("completed_at"):
            updates["completed_at"] = _now_iso()
    else:
        updates["completed_at"] = ""

    _apply_update(_tasks, {"task_id": row["task_id"]}, updates, [])
    fresh = _tasks.get_item(Key={"task_id": row["task_id"]}).get("Item") or {}
    # Keep the legacy embedded map in step, the same dual-write every other
    # task mutation performs.
    if fresh.get("source_recording_id"):
        _mirror_task_to_recording(fresh["source_recording_id"], fresh)
    _audit("task.status_changed_by_assignee", user_id, row["task_id"],
           status=status)
    return _resp(200, {"task": _public_task_v2(
        fresh, _speaker_names_for_recording(fresh.get("source_recording_id")))})


def update_task_v2(event):
    """PATCH /tasks/{task_id} — the same mutation as the meeting-scoped route,
    reached by task id alone so the Task Tracker doesn't need to know which
    meeting a task came from.

    TWO CALLERS, TWO PATHS. The creator goes through the meeting-scoped
    implementation exactly as before, so edit/reassign/deadline logic stays in
    one place. The ASSIGNEE cannot: that route begins with _owned_recording,
    and an assignee does not own the creator's meeting — it would 404 before
    reaching any task check. So a status-only patch by the assignee is applied
    here directly, against the same validation helpers, and every other field
    is refused by _authorize_task_patch before a single write is built.
    """
    user_id = _require_auth(event)
    task_id = (event.get("pathParameters") or {}).get("task_id", "")
    row = _visible_task(user_id, task_id)
    data = _body(event)
    # Refuses a forbidden field for BOTH callers, and 404s anyone who is
    # neither — before any mutation is assembled.
    _authorize_task_patch(user_id, row, data)

    if not _is_task_creator(user_id, row):
        # Assignee: status and nothing else (already guaranteed above).
        return _update_task_status_as_assignee(user_id, row, data)

    # Delegate to the meeting-scoped implementation by handing it the shape it
    # expects — one code path for task mutation, not two that can drift.
    forged = dict(event)
    forged["pathParameters"] = {
        "key": urllib.parse.quote(row.get("source_recording_id") or "",
                                  safe="")}
    body = _body(event)
    body["id"] = row["task_id"]
    forged["body"] = json.dumps(body)
    forged["isBase64Encoded"] = False
    if not row.get("source_recording_id"):
        # A task with no source meeting (never happens today, but the field is
        # nullable by design) can't go through the meeting route — patch it
        # directly with the same validation by reusing the helpers.
        raise ApiError(409, "this task has no source meeting")
    return update_meeting_task(forged)


def resolve_task_assignee(event):
    """POST /tasks/{task_id}/resolve {contact_id} -> {task}

    The explicit "which Rahul?" answer (section 19). A user choosing a Contact
    for an unresolved task is the ONLY way an unresolved assignee becomes a
    resolved one by name — nothing in this system upgrades a name to an
    identity on its own.

    CREATOR ONLY. Resolving an assignment decides WHO the work belongs to,
    which is task configuration, not execution — and letting the current
    assignee reassign would let them hand their work to someone else. The
    _owned_task predicate below is exactly that check: an assignee gets 404
    here, since this route never tells a non-creator a task exists.
    """
    user_id = _require_auth(event)
    task_id = (event.get("pathParameters") or {}).get("task_id", "")
    row = _owned_task(user_id, task_id)
    data = _body(event)
    contact = _taggable_contact(
        user_id, data.get("contact_id"),
        workspace_id=_row_workspace_id(row, owner_field="owner_user_id"))

    updates = {
        "assignee_contact_id": contact["contact_id"],
        "assignee_name": contact.get("name", ""),
        "assignee_email": contact.get("email", ""),
        "assignee_phone": contact.get("phone", ""),
        "resolution_status": RESOLUTION_RESOLVED,
        "updated_at": _now_iso(),
    }
    removes = ["assignee_name_legacy"]
    linked = contact.get("minutex_user_id") or ""
    if linked:
        updates["assignee_user_id"] = linked
    else:
        removes.append("assignee_user_id")
    previous_assignee = _task_assignee_user(row)
    _apply_update(_tasks, {"task_id": row["task_id"]}, updates, removes)
    fresh = _tasks.get_item(Key={"task_id": row["task_id"]}).get("Item") or {}
    if fresh.get("source_recording_id"):
        _mirror_task_to_recording(fresh["source_recording_id"], fresh)
    _audit("task.assignee_resolved", user_id, row["task_id"],
           contact_id=contact["contact_id"])
    # Resolving "which Rahul?" to a contact with a MinuteX account is the
    # moment the task first has a real recipient — so it is a genuine
    # assignment, and the person it moved away from (if any) is told too.
    if _task_assignee_user(fresh) != previous_assignee:
        _notify_task_reassigned(previous_assignee, fresh, actor_user_id=user_id)
        _notify_task_assigned(fresh, actor_user_id=user_id)
    return _resp(200, {"task": _public_task_v2(
        fresh, _speaker_names_for_recording(fresh.get("source_recording_id")))})


def suggest_task_assignees(event):
    """GET /tasks/{task_id}/assignee-candidates -> {status, candidates}

    CREATOR ONLY (via _owned_task): these are candidate people from the
    creator's own address book, and only the creator can act on the answer.

    Who an unresolved task's NAME might refer to. Returns candidates for the
    user to choose from — it never picks. The
    `status` mirrors _match_contacts: "ambiguous" with one candidate still
    means "you decide", because a lone name match is not an identity.
    """
    user_id = _require_auth(event)
    task_id = (event.get("pathParameters") or {}).get("task_id", "")
    row = _owned_task(user_id, task_id)
    name = row.get("assignee_name_legacy") or row.get("assignee_name") or ""
    if not name:
        return _resp(200, {"status": "none", "candidates": [],
                           "searched_name": ""})

    status, matches = _match_contacts(user_id, name=name)
    candidates = sorted(matches, key=lambda c: _norm_name(c.get("name")))
    return _resp(200, {
        "status": status,
        "searched_name": name,
        "candidates": _public_contacts(candidates),
    })


# ---------------------------------------------------------------------------
# Shared plumbing for this section
# ---------------------------------------------------------------------------
# How many index pages one filtered list request may read before giving up and
# returning what it has with a cursor. Bounds worst-case latency when a filter
# matches very few rows in a large table.
_SEARCH_MAX_PAGES = 5


def _clean_limit(raw, default, maximum):
    try:
        value = int(str(raw or "").strip() or default)
    except (TypeError, ValueError):
        raise ApiError(400, "limit must be an integer")
    if value < 1:
        raise ApiError(400, "limit must be at least 1")
    return min(value, maximum)


def _encode_cursor(last_key):
    """DynamoDB LastEvaluatedKey -> opaque base64url pagination cursor.

    Opaque on purpose: it is a server-side implementation detail, and a client
    that took it apart would break the moment an index changed.
    """
    if not last_key:
        return ""
    return _b64u_encode(json.dumps(last_key, default=str).encode("utf-8"))


def _decode_cursor(raw):
    if not raw:
        return None
    try:
        data = json.loads(_b64u_decode(str(raw)))
    except (ValueError, TypeError):
        raise ApiError(400, "invalid cursor")
    if not isinstance(data, dict):
        raise ApiError(400, "invalid cursor")
    return data


def _apply_update(table, key, updates, removes):
    """One UpdateItem from a {field: value} dict plus a list of REMOVEs.

    Exists because this section does a lot of partial updates and hand-built
    UpdateExpressions are where reserved-word collisions hide. Every name is
    aliased through ExpressionAttributeNames, so a field called `status`,
    `role` or `name` — all DynamoDB reserved words — can never break a write.
    """
    if not updates and not removes:
        return
    names, values, sets = {}, {}, []
    for i, (field, value) in enumerate(updates.items()):
        names[f"#f{i}"] = field
        values[f":v{i}"] = value
        sets.append(f"#f{i} = :v{i}")
    drops = []
    for i, field in enumerate(removes or []):
        names[f"#r{i}"] = field
        drops.append(f"#r{i}")
    expr = ""
    if sets:
        expr = "SET " + ", ".join(sets)
    if drops:
        expr += (" " if expr else "") + "REMOVE " + ", ".join(drops)
    kwargs = {"Key": key, "UpdateExpression": expr,
              "ExpressionAttributeNames": names}
    if values:
        kwargs["ExpressionAttributeValues"] = values
    table.update_item(**kwargs)


def _audit(action, user_id, entity_id, **extra):
    """Structured audit line for an important mutation (section 33/37).

    Goes to CloudWatch via print, like every other log in this file — there is
    no separate audit store, and inventing one would be a bigger change than
    this asks for. Deliberately logs IDS AND COUNTS ONLY: never a contact's
    name, email or phone, never task text, never transcript content. Those are
    the personal data section 37 rules out, and an audit trail that leaks them
    is worse than none.
    """
    fields = " ".join(f"{k}={v}" for k, v in sorted(extra.items())
                      if v is not None)
    print(f"[audit] {action} user={user_id} entity={entity_id}"
          + (f" {fields}" if fields else ""))



# ---------------------------------------------------------------------------
# CRM — Salesforce connect (Phase 1: OAuth only; no record linking yet).
#
# Web-server (authorization code) flow:
#   1. GET  /crm/salesforce/connect  (JWT) mints a signed, expiring `state`
#      (HMAC over user_id + exp, same secret as the JWT — no extra table
#      needed for CSRF protection) and returns the Salesforce authorize URL.
#   2. The app opens that URL in a browser. The user logs in / approves on
#      Salesforce's own page — we never see their password (see the
#      Username-Password flow note below for why that path was rejected).
#   3. Salesforce redirects to GET /crm/salesforce/callback?code=...&state=...
#      — this route is UNAUTHENTICATED (no JWT: the browser, not the app,
#      calls it). `state` is verified instead, exactly like the pairing
#      code hash stands in for a session there.
#   4. The callback exchanges `code` for tokens over stdlib urllib (matches
#      the zero-dependency convention — see module docstring), envelope-
#      encrypts the refresh token with KMS, and writes one CrmConnections
#      row (PK user_id, SK provider) — a new single-purpose table, same
#      shape convention as Devices/DeviceKeys/UserDevices.
#   5. GET /crm/salesforce/status and DELETE /crm/salesforce let the app
#      show connection state and disconnect, without ever exposing tokens.
#
# SalesforceClient is a seam (mirrors FirmwareVerifier): every real network
# call to Salesforce goes through it, so Phase 5/7 (record lookup, push)
# plug in later without touching the route handlers, and it is the one
# place a test can monkeypatch.
# ---------------------------------------------------------------------------
CRM_PROVIDER_SALESFORCE = "salesforce"


def _salesforce_client_secret():
    """Connected App consumer secret — Secrets Manager, same lazy-cache
    pattern as _jwt_secret(). No env-var fallback: unlike the JWT secret
    there is no pre-existing plaintext deployment to stay compatible with,
    so this should never be allowed to land as a plaintext env var."""
    global _salesforce_secret_cache
    if _salesforce_secret_cache is not None:
        return _salesforce_secret_cache
    if not SALESFORCE_CLIENT_SECRET_ARN:
        raise RuntimeError("SALESFORCE_CLIENT_SECRET_ARN is not set")
    sm = boto3.client("secretsmanager", region_name=REGION)
    _salesforce_secret_cache = sm.get_secret_value(
        SecretId=SALESFORCE_CLIENT_SECRET_ARN)["SecretString"]
    return _salesforce_secret_cache


_salesforce_secret_cache = None
_kms = boto3.client("kms", region_name=REGION)


def _kms_encrypt(plaintext: str) -> str:
    resp = _kms.encrypt(KeyId=SALESFORCE_KMS_KEY_ID, Plaintext=plaintext.encode("utf-8"))
    return _b64u_encode(resp["CiphertextBlob"])


def _kms_decrypt(ciphertext_b64: str) -> str:
    resp = _kms.decrypt(CiphertextBlob=_b64u_decode(ciphertext_b64), KeyId=SALESFORCE_KMS_KEY_ID)
    return resp["Plaintext"].decode("utf-8")


# ---------------------------------------------------------------------------
# PKCE (RFC 7636). Salesforce Connected Apps now REQUIRE a code challenge:
# without one the authorize call fails with
#   error=invalid_request&error_description=missing required code challenge
#
# WHERE THE VERIFIER LIVES — the one design decision worth stating, because the
# obvious answer is wrong here. In a public-client flow the app keeps the
# verifier and performs the exchange itself. In THIS flow the app never sees an
# authorization code: Salesforce redirects to our Lambda, and the Lambda
# exchanges the code using the client secret from Secrets Manager (which must
# never ship in a mobile binary). So a verifier stored on the device could
# never reach the code that needs it.
#
# The verifier is therefore generated server-side in /connect and carried
# inside the SIGNED STATE — the only value that provably round-trips through
# Salesforce back to /callback. That gives PKCE exactly the properties it
# needs, for free, from machinery that already exists:
#   * per attempt      — a fresh verifier every /connect call
#   * tamper-proof     — the state is HMAC-signed; editing it invalidates it
#   * expiring         — SALESFORCE_STATE_TTL bounds the transaction
#   * correctly paired — verifier and state are the SAME string, so a verifier
#                        from another attempt is structurally impossible
#
# The state is opaque to the client and the verifier is never in a URL
# parameter of its own, never logged, and never stored at rest.
# ---------------------------------------------------------------------------
PKCE_VERIFIER_BYTES = 32          # -> 43 chars base64url, RFC 7636 minimum
PKCE_METHOD = "S256"


def _new_pkce_verifier() -> str:
    """A fresh, cryptographically random code_verifier (RFC 7636 §4.1)."""
    return _b64u_encode(secrets.token_bytes(PKCE_VERIFIER_BYTES))


def _pkce_challenge(verifier: str) -> str:
    """code_challenge = BASE64URL(SHA256(ASCII(verifier))), no padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return _b64u_encode(digest)


def _sign_oauth_state(user_id: str, verifier: str = "") -> str:
    """HMAC-signed, expiring state param — the CSRF guard for the redirect
    round-trip, and the carrier for the PKCE verifier.

    Reuses the JWT secret rather than a new one: this is not a session token,
    just a tamper-proof "this browser redirect really started from an
    authenticated /connect call for this user" claim. The verifier rides inside
    the signed payload (see the PKCE note above), so it cannot be swapped
    between attempts without breaking the signature.
    """
    payload = {"sub": user_id, "exp": int(time.time()) + SALESFORCE_STATE_TTL}
    if verifier:
        payload["cv"] = verifier
    seg = _b64u_encode(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(_jwt_secret().encode(), seg.encode(), hashlib.sha256).digest()
    return seg + "." + _b64u_encode(sig)


def _verify_oauth_state(state: str) -> tuple:
    """Return (user_id, code_verifier) from `state`, or raise ApiError.

    The verifier comes back as "" for a state minted before PKCE existed; the
    caller decides whether that is acceptable (it is not, for an exchange).
    """
    try:
        seg, sig_b64 = state.split(".")
        expected = hmac.new(_jwt_secret().encode(), seg.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64u_decode(sig_b64)):
            raise ValueError("bad signature")
        payload = json.loads(_b64u_decode(seg))
    except (ValueError, TypeError, KeyError):
        raise ApiError(400, "invalid or tampered state")
    if not isinstance(payload, dict) or payload.get("exp", 0) < int(time.time()):
        raise ApiError(410, "connect session expired — try again")
    user_id = payload.get("sub")
    if not user_id:
        raise ApiError(400, "invalid state")
    return user_id, str(payload.get("cv") or "")


def _salesforce_error_message(body: str) -> str:
    """Salesforce's own error text out of a REST error body.

    Errors come back as [{"message", "errorCode", "fields": [...]}]. Surfacing
    the org's wording (and the offending field) is the whole point: only
    Salesforce knows that a validation rule fired or which field is read-only,
    and a generic "update failed" would leave the user with nothing to act on.
    Returns "" when the body isn't parseable, so callers keep their default.
    """
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return ""
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return ""
    parts = []
    for err in parsed[:3]:
        if not isinstance(err, dict):
            continue
        message = str(err.get("message") or "").strip()
        if not message:
            continue
        fields = [str(f) for f in (err.get("fields") or []) if f]
        parts.append(f"{message} ({', '.join(fields)})" if fields else message)
    return " · ".join(parts)[:500]


class SalesforceAuthExpired(Exception):
    """The ACCESS token was rejected (401) — internal, never surfaced.

    Distinct from the user-visible failure on purpose: this one means "mint a
    new access token from the refresh token and retry", which _sf_call does
    transparently. A dead REFRESH token is the user-visible failure and is
    raised as SalesforceReconnectRequired (HTTP 409) — see below.
    """


# HTTP status for "your MinuteX session is fine, but the SALESFORCE credential
# behind this route is dead; reconnect Salesforce".
#
# It is deliberately NOT 401. 401 on this API means one specific thing — the
# MinuteX JWT is missing/expired — and the app acts on it globally: lib/api.ts
# clears the stored token at the single choke point every authenticated call
# passes through, and the screen then redirects to /login. Returning 401 for a
# dead Salesforce refresh token made those two unrelated failures
# indistinguishable on the wire, so opening CRM Mapping with a stale Salesforce
# connection destroyed a perfectly good MinuteX session and bounced the user to
# the login screen (the bug this constant exists to prevent).
#
# 409 Conflict is the honest code: the request is authenticated and well-formed,
# but conflicts with the current state of the resource — Salesforce is linked
# yet unusable. The app reads `code` (not the prose) to offer "Reconnect".
SF_RECONNECT_STATUS = 409
SF_RECONNECT_CODE = "salesforce_reconnect_required"


class SalesforceReconnectRequired(ApiError):
    """The user's stored Salesforce refresh token is dead — they must redo the
    OAuth connect. Carries a stable `code` so the app can branch on identity
    rather than on wording, and never on a status code that means something
    else."""

    def __init__(self, message: str):
        super().__init__(SF_RECONNECT_STATUS, message)
        self.code = SF_RECONNECT_CODE


# Distinct from SF_RECONNECT_CODE on purpose (spec section 16): "not
# connected" and "connected but the credential died" are different states
# that need different UI — one shows a Connect button, the other shows
# Reconnect. Collapsing them into one code would lose that distinction for
# the organisation flow, which — unlike Personal, where "not connected" is
# just a 400 nobody branches on — has a Member who needs to know WHICH
# empty state they are looking at.
ORG_SF_NOT_CONNECTED_CODE = "organisation_salesforce_not_connected"


class OrganisationSalesforceNotConnected(ApiError):
    """No workspace has connected Salesforce yet. Never falls back to the
    caller's Personal Salesforce connection — see the module note on why
    that would be a silent, surprising scope violation."""

    def __init__(self, message: str = "Organisation Salesforce is not connected"):
        super().__init__(400, message)
        self.code = ORG_SF_NOT_CONNECTED_CODE


class SalesforceClient:
    """Seam for every real Salesforce network call (mirrors FirmwareVerifier
    for devices). Zero external dependencies — stdlib urllib, matching the
    rest of the repo's Groq/ElevenLabs integrations."""

    def _post_form(self, url: str, form: dict, what: str) -> dict:
        """POST an x-www-form-urlencoded body, return the parsed JSON."""
        body = urllib.parse.urlencode(form).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            print(f"[salesforce] {what} failed: {e.code} {detail[:300]}")
            raise ApiError(502, f"Salesforce rejected the {what} — try again")
        except urllib.error.URLError as e:
            print(f"[salesforce] {what} network error: {e}")
            raise ApiError(502, "could not reach Salesforce")

    def _get_json(self, url: str, access_token: str, what: str):
        """Authenticated GET against the REST API.

        Raises SalesforceAuthExpired on 401 so the caller can refresh the
        access token and retry ONCE — distinguishing "this token is stale"
        (recoverable, and expected: access tokens are short-lived) from "this
        request is wrong" (not recoverable by retrying).
        """
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {access_token}"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            if e.code == 401:
                raise SalesforceAuthExpired(detail[:200])
            print(f"[salesforce] {what} failed: {e.code} {detail[:300]}")
            if e.code == 403:
                raise ApiError(403, "your Salesforce user lacks permission for "
                                    "this — ask your admin")
            if e.code == 404:
                raise ApiError(404, f"{what}: not found in your Salesforce org")
            raise ApiError(502, f"Salesforce {what} failed")
        except urllib.error.URLError as e:
            print(f"[salesforce] {what} network error: {e}")
            raise ApiError(502, "could not reach Salesforce")

    def exchange_code(self, code: str, code_verifier: str = "") -> dict:
        """Authorization code -> {access_token, refresh_token, instance_url,
        id, ...}. Raises ApiError(502) on any Salesforce-side failure.

        `code_verifier` completes the PKCE pair whose challenge was sent at
        authorize time; Salesforce rejects the exchange without it. The client
        secret still comes from Secrets Manager — PKCE is in ADDITION to it,
        not a replacement, because this is a confidential client.
        """
        form = {"grant_type": "authorization_code",
                "code": code,
                "client_id": SALESFORCE_CLIENT_ID,
                "client_secret": _salesforce_client_secret(),
                "redirect_uri": SALESFORCE_REDIRECT_URI}
        if code_verifier:
            form["code_verifier"] = code_verifier
        return self._post_form(
            f"{SALESFORCE_LOGIN_URL}/services/oauth2/token", form, "connection")

    def refresh_access_token(self, refresh_token: str) -> dict:
        """refresh_token -> a fresh short-lived {access_token, instance_url?}.

        Access tokens are deliberately NOT stored: they expire in hours, and
        keeping them would mean a second secret to encrypt, rotate and leak.
        The refresh token is the only durable credential, so every request
        path mints an access token on demand.

        The response MAY carry a new refresh_token: with Refresh Token Rotation
        enabled on the Connected App, Salesforce returns one on every refresh
        and invalidates the token just used. Callers must persist it — see
        _persist_rotated_refresh_token, which _sf_call invokes on every
        successful refresh. (This docstring previously claimed Salesforce never
        rotates here; that is true only with rotation off, and believing it
        caused every call after the first to fail with invalid_grant.)

        A 400 means the refresh token itself is dead (user revoked access in
        Salesforce, or an admin uninstalled the Connected App) — surfaced as
        SF_RECONNECT_STATUS so the app knows to prompt for reconnection rather
        than retrying, without it being mistaken for MinuteX session expiry.
        """
        try:
            return self._post_form(
                f"{SALESFORCE_LOGIN_URL}/services/oauth2/token",
                {"grant_type": "refresh_token",
                 "refresh_token": refresh_token,
                 "client_id": SALESFORCE_CLIENT_ID,
                 "client_secret": _salesforce_client_secret()},
                "token refresh")
        except ApiError as e:
            if e.status == 502 and "rejected" in e.message:
                raise SalesforceReconnectRequired(
                    "your Salesforce connection has expired — "
                    "reconnect Salesforce in Settings")
            raise

    def whoami(self, instance_url: str, access_token: str) -> dict:
        """GET the identity URL to confirm the token works and fetch the
        connected org/user display info shown on the status screen."""
        try:
            return self._get_json(f"{instance_url}/services/oauth2/userinfo",
                                  access_token, "org info")
        except SalesforceAuthExpired:
            # Only called immediately after a fresh exchange/refresh, so a 401
            # here is not the ordinary stale-token case worth retrying.
            raise ApiError(502, "connected, but could not read Salesforce org info")
        except ApiError:
            raise ApiError(502, "connected, but could not read Salesforce org info")

    def list_objects(self, instance_url: str, access_token: str) -> list:
        """Every object in the org, as [{name, label, custom, createable, ...}].

        The global describe is one call and returns the full object list with
        enough metadata to filter client-side — much cheaper than describing
        each object just to decide whether to offer it.
        """
        data = self._get_json(
            f"{instance_url}/services/data/{SALESFORCE_API_VERSION}/sobjects/",
            access_token, "object list")
        return data.get("sobjects") or []

    def describe_object(self, instance_url: str, access_token: str,
                        object_name: str) -> dict:
        """Full describe of ONE object, including every field's type/length."""
        return self._get_json(
            f"{instance_url}/services/data/{SALESFORCE_API_VERSION}"
            f"/sobjects/{urllib.parse.quote(object_name)}/describe/",
            access_token, f"describe {object_name}")

    def update_record(self, instance_url: str, access_token: str,
                      object_name: str, record_id: str, fields: dict) -> None:
        """PATCH one record's fields. Works for ANY object — the caller supplies
        the object name, the record Id and the field map, all of which come from
        the user's configuration, so there is no per-object push code.

        Returns None on success: Salesforce answers 204 with an empty body.
        Raises SalesforceAuthExpired on 401 (so _sf_call can refresh + retry),
        and ApiError with Salesforce's own message on a validation/permission
        failure, because only the org's message says WHICH field it rejected.
        """
        body = json.dumps(fields).encode("utf-8")
        req = urllib.request.Request(
            f"{instance_url}/services/data/{SALESFORCE_API_VERSION}"
            f"/sobjects/{urllib.parse.quote(object_name)}"
            f"/{urllib.parse.quote(record_id)}",
            data=body, method="PATCH",
            headers={"Authorization": f"Bearer {access_token}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                r.read()
                return None
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            if e.code == 401:
                raise SalesforceAuthExpired(detail[:200])
            message = _salesforce_error_message(detail)
            print(f"[salesforce] update {object_name}/{record_id} failed: "
                  f"{e.code} {detail[:300]}")
            if e.code == 404:
                # The record was deleted (or is no longer visible) since we
                # resolved it — the association is stale, not the request wrong.
                raise ApiError(404, message or "that Salesforce record no longer "
                                               "exists — look it up again")
            if e.code == 403:
                raise ApiError(403, message or "your Salesforce user cannot edit "
                                               "this record — ask your admin")
            if e.code in (400, 422):
                raise ApiError(422, message or "Salesforce rejected the update")
            raise ApiError(502, message or "Salesforce update failed")
        except urllib.error.URLError as e:
            print(f"[salesforce] update network error: {e}")
            raise ApiError(502, "could not reach Salesforce")

    def query(self, instance_url: str, access_token: str, soql: str) -> dict:
        """Run a SOQL query. Used to resolve a user-supplied identifier to a
        Salesforce record Id, against whichever object/field the user mapped."""
        return self._get_json(
            f"{instance_url}/services/data/{SALESFORCE_API_VERSION}/query/"
            f"?q={urllib.parse.quote(soql)}",
            access_token, "record lookup")

    def create_record(self, instance_url: str, access_token: str,
                      object_name: str, fields: dict) -> str:
        """POST a new record, return its Id. Salesforce answers 201 with
        {"id": ..., "success": true, ...} on success.

        Added in Phase 2D.3 for Salesforce TASK creation from a MinuteX
        action item (see push_org_meeting_crm) — the only object this
        codebase creates rather than looks up and updates. Deliberately NOT
        used for Contact/Account: spec section 5 requires those to be
        resolved by explicit human selection from a search
        (crm_search_salesforce_contacts), never auto-created from partial
        meeting data, which is what keeps this method's blast radius to
        Task creation only.

        Same error-mapping shape as update_record, for the same reasons:
        401 -> SalesforceAuthExpired (retry once), 403/400/422 -> a typed
        ApiError carrying Salesforce's own validation message.
        """
        body = json.dumps(fields).encode("utf-8")
        req = urllib.request.Request(
            f"{instance_url}/services/data/{SALESFORCE_API_VERSION}"
            f"/sobjects/{urllib.parse.quote(object_name)}",
            data=body, method="POST",
            headers={"Authorization": f"Bearer {access_token}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                parsed = json.loads(r.read().decode("utf-8"))
                return str(parsed.get("id") or "")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            if e.code == 401:
                raise SalesforceAuthExpired(detail[:200])
            message = _salesforce_error_message(detail)
            print(f"[salesforce] create {object_name} failed: "
                  f"{e.code} {detail[:300]}")
            if e.code == 403:
                raise ApiError(403, message or "your Salesforce user cannot create "
                                               f"a {object_name} — ask your admin")
            if e.code in (400, 422):
                raise ApiError(422, message or f"Salesforce rejected the new {object_name}")
            raise ApiError(502, message or f"Salesforce {object_name} creation failed")
        except urllib.error.URLError as e:
            print(f"[salesforce] create network error: {e}")
            raise ApiError(502, "could not reach Salesforce")

    def revoke(self, refresh_token: str) -> None:
        """Best-effort revoke on disconnect — Salesforce still lets the user
        revoke from their own org's Connected Apps page either way, so a
        failure here must never block the local disconnect."""
        form = urllib.parse.urlencode({"token": refresh_token}).encode("utf-8")
        req = urllib.request.Request(
            f"{SALESFORCE_LOGIN_URL}/services/oauth2/revoke", data=form, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            urllib.request.urlopen(req, timeout=10).read()
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            print(f"[salesforce] revoke failed (non-fatal): {e}")


_salesforce = SalesforceClient()


def _sf_call(user_id: str, fn):
    """Run `fn(instance_url, access_token)` against the user's Salesforce org.

    Owns the whole access-token lifecycle so no route handler repeats it:
    decrypt the refresh token, mint an access token, call, and on a 401 mint
    once more and retry. Access tokens are never persisted (see
    refresh_access_token) — each request pays one cheap refresh call, which
    keeps exactly one durable secret per user instead of two.

    Raises ApiError(400) when Salesforce isn't connected, and
    SalesforceReconnectRequired (409) when the refresh token itself is dead —
    never 401, which on this API means the MinuteX session died. Overloading
    401 here signed the user out of MinuteX entirely; see SF_RECONNECT_STATUS.
    """
    conn = _get_salesforce_connection(user_id)
    if not conn or not conn.get("refresh_token_enc"):
        raise ApiError(400, "Salesforce is not connected")
    instance_url = conn.get("instance_url") or ""
    stored_enc = conn["refresh_token_enc"]
    refresh_token = _kms_decrypt(stored_enc)

    tokens = _salesforce.refresh_access_token(refresh_token)
    access_token = tokens.get("access_token")
    if not access_token:
        raise SalesforceReconnectRequired(
            "your Salesforce connection has expired — "
            "reconnect Salesforce in Settings")
    # Persist BEFORE calling fn(): under rotation the token we just used is
    # already dead, so losing the replacement to a failure inside fn() would
    # brick the connection until the user reconnects.
    rotated = _persist_rotated_refresh_token(user_id, tokens, stored_enc)
    if rotated:
        refresh_token = rotated
    # Salesforce can hand back a different instance_url after an org move.
    instance_url = tokens.get("instance_url") or instance_url
    if not instance_url:
        raise ApiError(502, "Salesforce did not report an instance URL")

    try:
        return fn(instance_url, access_token)
    except SalesforceAuthExpired:
        # The token we just minted was rejected — rare, but it happens when a
        # session is invalidated mid-flight. One more attempt, then give up.
        # Uses `refresh_token`, which the block above updated if it rotated:
        # retrying with the consumed one would fail every time under rotation.
        print("[salesforce] access token rejected; refreshing once and retrying")
        stored_enc = _kms_encrypt(refresh_token) if rotated else stored_enc
        tokens = _salesforce.refresh_access_token(refresh_token)
        access_token = tokens.get("access_token")
        if not access_token:
            raise SalesforceReconnectRequired(
                "your Salesforce connection has expired — "
                "reconnect Salesforce in Settings")
        _persist_rotated_refresh_token(user_id, tokens, stored_enc)
        try:
            return fn(tokens.get("instance_url") or instance_url, access_token)
        except SalesforceAuthExpired:
            raise SalesforceReconnectRequired(
                "Salesforce kept rejecting the session — "
                "reconnect Salesforce in Settings")


def salesforce_connect(event):
    """GET /crm/salesforce/connect (JWT) -> {authorize_url}.

    Mints a fresh PKCE verifier per attempt, sends only its SHA-256 challenge
    to Salesforce, and carries the verifier itself inside the signed state so
    /callback can complete the exchange (see the PKCE note above for why the
    verifier is server-side rather than on the device).
    """
    user_id = _require_auth(event)
    if not SALESFORCE_CLIENT_ID or not SALESFORCE_REDIRECT_URI:
        raise ApiError(500, "Salesforce integration is not configured")
    verifier = _new_pkce_verifier()
    state = _sign_oauth_state(user_id, verifier)
    qs = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": SALESFORCE_CLIENT_ID,
        "redirect_uri": SALESFORCE_REDIRECT_URI,
        "state": state,
        # Only the CHALLENGE goes over the wire. The verifier is never a URL
        # parameter — that is the entire point of PKCE.
        "code_challenge": _pkce_challenge(verifier),
        "code_challenge_method": PKCE_METHOD,
    })
    return _resp(200, {"authorize_url": f"{SALESFORCE_LOGIN_URL}/services/oauth2/authorize?{qs}"})


def salesforce_callback(event):
    """GET /crm/salesforce/callback?code&state (Salesforce redirect, NO JWT).

    Verifies `state` instead of a bearer token, exchanges the code, and
    redirects the browser to SALESFORCE_RETURN_URL (a deep link back into
    the app) with a simple ok/error query param — the browser tab, not this
    Lambda, is the one thing that can hand control back to the app.
    """
    qs = event.get("queryStringParameters") or {}
    error = qs.get("error")

    def _redirect(ok: bool, reason: str = "") -> dict:
        params = {"connected": "1"} if ok else {"connected": "0", "reason": reason}
        target = SALESFORCE_RETURN_URL or "/"
        location = f"{target}?{urllib.parse.urlencode(params)}"
        return {"statusCode": 302, "headers": {"Location": location}, "body": ""}

    if error:
        print(f"[salesforce] callback error param: {error}")
        return _redirect(False, "denied")

    code = qs.get("code")
    state = qs.get("state")
    if not code or not state:
        return _redirect(False, "missing_params")

    try:
        # Verifies the signature and expiry (invalid/tampered/expired state all
        # raise), and yields the PKCE verifier bound to THIS attempt.
        user_id, code_verifier = _verify_oauth_state(state)
        if not code_verifier:
            # A signed state with no verifier means the /connect that produced
            # it predates PKCE. Salesforce would reject the exchange anyway;
            # failing here is clearer and never falls back to a non-PKCE
            # exchange, which would be a downgrade.
            print("[salesforce] callback: state carries no PKCE verifier")
            return _redirect(False, "pkce_missing")
        tokens = _salesforce.exchange_code(code, code_verifier)
    except ApiError as e:
        # e.message is Salesforce's or our own wording — never the code, the
        # verifier or any token.
        print(f"[salesforce] callback failed: {e.message}")
        return _redirect(False, "exchange_failed")

    refresh_token = tokens.get("refresh_token")
    access_token = tokens.get("access_token")
    instance_url = tokens.get("instance_url")
    if not refresh_token or not access_token or not instance_url:
        print("[salesforce] token response missing required fields")
        return _redirect(False, "exchange_failed")

    identity = {}
    try:
        identity = _salesforce.whoami(instance_url, access_token)
    except ApiError:
        pass  # non-fatal — connection still succeeded, org info is cosmetic

    # tokens["id"] is an identity URL of the form
    # "{instance}/id/{org_id}/{user_id}" — org_id/user_id are also in
    # `identity` when whoami() succeeded, but that call is best-effort, so
    # parse the always-present id URL as the reliable fallback.
    id_parts = (tokens.get("id") or "").rstrip("/").split("/")
    org_id_fallback = id_parts[-2] if len(id_parts) >= 2 else ""

    now = _now_iso()
    _crm_connections.put_item(Item={
        "user_id": user_id,
        "provider": CRM_PROVIDER_SALESFORCE,
        "instance_url": instance_url,
        "refresh_token_enc": _kms_encrypt(refresh_token),
        "org_id": identity.get("organization_id") or org_id_fallback,
        "sf_user_id": identity.get("user_id") or "",
        "sf_username": identity.get("preferred_username") or identity.get("email") or "",
        "connected_at": now,
        "updated_at": now,
    })
    return _redirect(True)


def _get_salesforce_connection(user_id: str) -> dict:
    return _crm_connections.get_item(
        Key={"user_id": user_id, "provider": CRM_PROVIDER_SALESFORCE}).get("Item")


def _persist_rotated_refresh_token(user_id: str, tokens: dict,
                                   old_enc: str) -> str:
    """Store the refresh token Salesforce hands back on a refresh, if it rotated.

    REFRESH TOKEN ROTATION. When the Connected App has rotation enabled,
    /services/oauth2/token returns a NEW refresh_token on every refresh and
    invalidates the one just used. Keeping the original then guarantees exactly
    one working call per connect, and `invalid_grant: expired access/refresh
    token` on every request after that — which is the bug this exists to fix.

    Rotation is OFF by default, and then no refresh_token comes back at all;
    that is why this is conditional rather than unconditional. Both modes are
    handled by the same path: only a token that is present AND different is
    written.

    Returns the refresh token to use from here on — the rotated one when it
    rotated, otherwise the caller's existing one.

    CONCURRENCY. Two in-flight requests for the same user can each refresh; the
    second rotation invalidates the first, and whichever write lands last wins.
    The conditional write makes the LOSER fail its own update instead of
    clobbering the newer token with an older one. A lost race still leaves that
    request's token dead, but the stored token stays the newest one written, so
    the account self-heals on the next call rather than needing a reconnect.
    """
    new_token = (tokens.get("refresh_token") or "").strip()
    if not new_token:
        return ""  # rotation off (or nothing returned) — nothing to persist

    new_enc = _kms_encrypt(new_token)
    try:
        _crm_connections.update_item(
            Key={"user_id": user_id, "provider": CRM_PROVIDER_SALESFORCE},
            UpdateExpression="SET refresh_token_enc = :new, updated_at = :now",
            # Only overwrite the exact ciphertext we read. A concurrent request
            # that already rotated past us fails here rather than reinstating a
            # token Salesforce has since invalidated.
            ConditionExpression="refresh_token_enc = :old",
            ExpressionAttributeValues={
                ":new": new_enc, ":old": old_enc, ":now": _now_iso()},
        )
        print("[salesforce] refresh token rotated; stored the new one")
    except Exception as e:  # noqa: BLE001
        # Includes ConditionalCheckFailedException (someone else rotated first).
        # Never fatal: we hold a VALID access token for this request, so failing
        # the user's call over a bookkeeping write would be strictly worse.
        print(f"[salesforce] could not persist rotated refresh token "
              f"(non-fatal): {type(e).__name__}: {e}")
    return new_token


def salesforce_status(event):
    """GET /crm/salesforce/status (JWT) -> {connected, instance_url?, sf_username?, connected_at?}."""
    user_id = _require_auth(event)
    conn = _get_salesforce_connection(user_id)
    if not conn:
        return _resp(200, {"connected": False})
    return _resp(200, {
        "connected": True,
        "instance_url": conn.get("instance_url", ""),
        "sf_username": conn.get("sf_username", ""),
        "connected_at": conn.get("connected_at", ""),
    })


def salesforce_disconnect(event):
    """DELETE /crm/salesforce (JWT) -> {disconnected}."""
    user_id = _require_auth(event)
    conn = _get_salesforce_connection(user_id)
    if conn and conn.get("refresh_token_enc"):
        try:
            refresh_token = _kms_decrypt(conn["refresh_token_enc"])
            _salesforce.revoke(refresh_token)
        except Exception as e:  # noqa: BLE001 - revoke is best-effort, never blocks disconnect
            print(f"[salesforce] revoke on disconnect failed (non-fatal): {e}")
    _crm_connections.delete_item(Key={"user_id": user_id, "provider": CRM_PROVIDER_SALESFORCE})
    return _resp(200, {"disconnected": True})


# ===========================================================================
# ORGANISATION CRM — Salesforce (Phase 2D.1)
#
# A WORKSPACE-scoped twin of the Personal flow above. Deliberately built as
# parallel functions rather than parameterizing the Personal ones: the two
# differ in identity (user_id vs workspace_id), in authorization (JWT owner
# vs CAP_MANAGE_INTEGRATIONS), and in storage (CrmConnections vs
# OrgCrmConnections), and folding both into one code path would mean every
# future change to either has to reason about the other. What genuinely IS
# shared is reused directly: SalesforceClient, PKCE helpers, KMS
# encrypt/decrypt, and the Connected App credentials — there is exactly one
# OAuth client and one token endpoint either flow talks to.
#
# THE SCOPE BOUNDARY (spec section 6), enforced structurally, not by
# convention:
#   Personal:     user_id      -> CrmConnections      (unchanged, above)
#   Organisation: workspace_id -> OrgCrmConnections    (this section)
# Nothing here ever reads or writes CrmConnections, and nothing above ever
# reads or writes OrgCrmConnections. A Personal connection can never be used
# for organisation data and vice versa, because there is no code path that
# looks in the other table.
# ---------------------------------------------------------------------------

def _sign_org_crm_state(user_id: str, workspace_id: str, verifier: str) -> str:
    """HMAC-signed, expiring state for the ORGANISATION connect round-trip.

    A separate signer from _sign_oauth_state (Personal) and
    _sign_integration_state (Gmail/etc), even though the construction is
    identical, because this one carries an extra claim that changes what a
    valid state MEANS: `wid`, the workspace the connection will be created
    for. That claim is what makes "prevent cross-workspace connection
    creation" a property of the token instead of something a route has to
    remember to check — the workspace comes from the SIGNED state at
    callback time, never from a query/body parameter Salesforce echoes back,
    because Salesforce does not echo application parameters through its own
    OAuth redirect.
    """
    payload = {"sub": user_id, "wid": workspace_id, "cv": verifier,
               "exp": int(time.time()) + SALESFORCE_STATE_TTL}
    seg = _b64u_encode(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(_jwt_secret().encode(), seg.encode(), hashlib.sha256).digest()
    return seg + "." + _b64u_encode(sig)


def _verify_org_crm_state(state: str) -> tuple:
    """(user_id, workspace_id, code_verifier) from `state`, or raise ApiError.

    Signature/expiry checks mirror _verify_oauth_state exactly. The
    additional requirement — both `sub` AND `wid` present — means a state
    minted by the PERSONAL connect flow (which carries no `wid`) is rejected
    here even though it shares the same signing secret: the two states are
    structurally distinct, not just conventionally so.
    """
    try:
        seg, sig_b64 = state.split(".")
        expected = hmac.new(_jwt_secret().encode(), seg.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64u_decode(sig_b64)):
            raise ValueError("bad signature")
        payload = json.loads(_b64u_decode(seg))
    except (ValueError, TypeError, KeyError):
        raise ApiError(400, "invalid or tampered state")
    if not isinstance(payload, dict) or payload.get("exp", 0) < int(time.time()):
        raise ApiError(410, "connect session expired — try again")
    user_id = str(payload.get("sub") or "")
    workspace_id = str(payload.get("wid") or "")
    verifier = str(payload.get("cv") or "")
    if not user_id or not workspace_id or not verifier:
        raise ApiError(400, "invalid state")
    return user_id, workspace_id, verifier


def _get_org_salesforce_connection(workspace_id: str) -> dict:
    return _org_crm_connections.get_item(
        Key={"workspace_id": workspace_id, "provider": CRM_PROVIDER_SALESFORCE}
    ).get("Item")


def _sf_call_org(workspace_id: str, fn):
    """As _sf_call, but against the WORKSPACE's Salesforce connection.

    Identical token lifecycle (decrypt -> refresh -> call -> retry-once on
    401), against OrgCrmConnections instead of CrmConnections. Kept as its
    own function rather than a shared helper parameterized by table+key: the
    two call sites already read differently (_get_salesforce_connection vs
    _get_org_salesforce_connection, user_id vs workspace_id in the Key), and
    forcing them through one indirection would obscure exactly the
    Personal/Organisation boundary this section exists to keep visible.
    """
    conn = _get_org_salesforce_connection(workspace_id)
    if not conn or not conn.get("refresh_token_enc"):
        raise OrganisationSalesforceNotConnected()
    instance_url = conn.get("instance_url") or ""
    stored_enc = conn["refresh_token_enc"]
    refresh_token = _kms_decrypt(stored_enc)

    tokens = _salesforce.refresh_access_token(refresh_token)
    access_token = tokens.get("access_token")
    if not access_token:
        raise SalesforceReconnectRequired(
            "the organisation's Salesforce connection has expired — "
            "an owner or manager must reconnect it")
    rotated = _persist_rotated_org_refresh_token(workspace_id, tokens, stored_enc)
    if rotated:
        refresh_token = rotated
    instance_url = tokens.get("instance_url") or instance_url
    if not instance_url:
        raise ApiError(502, "Salesforce did not report an instance URL")

    try:
        return fn(instance_url, access_token)
    except SalesforceAuthExpired:
        print("[org-salesforce] access token rejected; refreshing once and retrying")
        stored_enc = _kms_encrypt(refresh_token) if rotated else stored_enc
        tokens = _salesforce.refresh_access_token(refresh_token)
        access_token = tokens.get("access_token")
        if not access_token:
            raise SalesforceReconnectRequired(
                "the organisation's Salesforce connection has expired — "
                "an owner or manager must reconnect it")
        _persist_rotated_org_refresh_token(workspace_id, tokens, stored_enc)
        try:
            return fn(tokens.get("instance_url") or instance_url, access_token)
        except SalesforceAuthExpired:
            raise SalesforceReconnectRequired(
                "Salesforce kept rejecting the organisation session — "
                "an owner or manager must reconnect it")


def _persist_rotated_org_refresh_token(workspace_id: str, tokens: dict,
                                       old_enc: str) -> str:
    """As _persist_rotated_refresh_token, keyed by workspace_id/provider."""
    new_token = (tokens.get("refresh_token") or "").strip()
    if not new_token:
        return ""

    new_enc = _kms_encrypt(new_token)
    try:
        _org_crm_connections.update_item(
            Key={"workspace_id": workspace_id, "provider": CRM_PROVIDER_SALESFORCE},
            UpdateExpression="SET refresh_token_enc = :new, updated_at = :now",
            ConditionExpression="refresh_token_enc = :old",
            ExpressionAttributeValues={
                ":new": new_enc, ":old": old_enc, ":now": _now_iso()},
        )
        print(f"[org-salesforce] refresh token rotated for {workspace_id}")
    except Exception as e:  # noqa: BLE001
        print(f"[org-salesforce] could not persist rotated refresh token "
              f"(non-fatal): {type(e).__name__}: {e}")
    return new_token


def org_salesforce_connect(event):
    """GET /workspaces/{workspace_id}/crm/salesforce/connect (JWT)
    -> {authorize_url}.

    OWNER/MANAGER ONLY (spec section 5): a Member may use an already-
    configured organisation connection but must not be able to create or
    replace one. Enforced server-side via CAP_MANAGE_INTEGRATIONS — the
    frontend hiding the "Connect" button is presentation, this check is the
    actual authorization, exactly like every other capability gate in this
    file.

    Mints a fresh PKCE verifier per attempt and binds this attempt to BOTH
    the initiating user and the target workspace inside the signed state
    (see _sign_org_crm_state) — the callback re-validates both at exchange
    time, so a membership change mid-flow (the user leaves the workspace, or
    is demoted below Manager, between /connect and /callback) is caught
    rather than trusted from ten minutes ago.
    """
    workspace_id = _url_unquote(
        (event.get("pathParameters") or {}).get("workspace_id", ""))
    user_id, wid, membership = _require_workspace_capability(
        event, workspace_schema.CAP_MANAGE_INTEGRATIONS, workspace_id=workspace_id)
    if not workspace_schema.is_organisation_workspace_id(wid):
        # A personal workspace has no "organisation Salesforce" concept —
        # Personal Salesforce already covers it, and this route silently
        # creating an OrgCrmConnections row for a personal workspace would
        # be exactly the "silently promote Personal to Organisation"
        # behavior the spec forbids.
        raise ApiError(400, "organisation Salesforce is only available for "
                            "an organisation workspace")
    if not SALESFORCE_CLIENT_ID or not ORG_SALESFORCE_REDIRECT_URI:
        raise ApiError(500, "Organisation Salesforce integration is not configured")

    verifier = _new_pkce_verifier()
    state = _sign_org_crm_state(user_id, wid, verifier)
    qs = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": SALESFORCE_CLIENT_ID,
        "redirect_uri": ORG_SALESFORCE_REDIRECT_URI,
        "state": state,
        "code_challenge": _pkce_challenge(verifier),
        "code_challenge_method": PKCE_METHOD,
    })
    return _resp(200, {"authorize_url": f"{SALESFORCE_LOGIN_URL}/services/oauth2/authorize?{qs}"})


def org_salesforce_callback(event):
    """GET /crm/salesforce/org-callback?code&state (Salesforce redirect, NO JWT).

    Fixed path — see the module header on why this cannot be
    /workspaces/{workspace_id}/... — with the workspace and initiating user
    recovered from the signed `state` instead of the URL. Re-validates BOTH
    at exchange time (not just at /connect time): a Member who was Manager
    when they clicked Connect but was demoted or removed while the
    Salesforce consent screen was open must not have their consent produce a
    connection. This is the enforcement for "prevent connecting Salesforce
    to a workspace the user no longer belongs to" — the earlier check in
    org_salesforce_connect narrows who can START the flow, this one is what
    actually stops a stale grant from completing it.
    """
    qs = event.get("queryStringParameters") or {}
    error = qs.get("error")

    def _redirect(ok: bool, reason: str = "") -> dict:
        params = {"connected": "1", "scope": "organisation"} if ok else \
            {"connected": "0", "scope": "organisation", "reason": reason}
        target = SALESFORCE_RETURN_URL or "/"
        location = f"{target}?{urllib.parse.urlencode(params)}"
        return {"statusCode": 302, "headers": {"Location": location}, "body": ""}

    if error:
        print(f"[org-salesforce] callback error param: {error}")
        return _redirect(False, "denied")

    code = qs.get("code")
    state = qs.get("state")
    if not code or not state:
        return _redirect(False, "missing_params")

    try:
        user_id, workspace_id, code_verifier = _verify_org_crm_state(state)
    except ApiError as e:
        print(f"[org-salesforce] callback state rejected: {e.message}")
        return _redirect(False, "expired")

    # Re-check membership + role NOW, not just at /connect time — see the
    # docstring above. Fails closed: no membership row, wrong role, or a
    # read error all deny.
    membership = _active_membership(workspace_id, user_id)
    if not membership or not workspace_schema.role_can(
            membership.get("role"), workspace_schema.CAP_MANAGE_INTEGRATIONS):
        print(f"[org-salesforce] callback: {user_id} lost Manager+ access to "
              f"{workspace_id} during the OAuth round-trip")
        return _redirect(False, "not_authorized")

    try:
        tokens = _salesforce.exchange_code(code, code_verifier)
    except ApiError as e:
        print(f"[org-salesforce] callback failed: {e.message}")
        return _redirect(False, "exchange_failed")

    refresh_token = tokens.get("refresh_token")
    access_token = tokens.get("access_token")
    instance_url = tokens.get("instance_url")
    if not refresh_token or not access_token or not instance_url:
        print("[org-salesforce] token response missing required fields")
        return _redirect(False, "exchange_failed")

    identity = {}
    try:
        identity = _salesforce.whoami(instance_url, access_token)
    except ApiError:
        pass  # non-fatal — connection still succeeded, org info is cosmetic

    id_parts = (tokens.get("id") or "").rstrip("/").split("/")
    org_id_fallback = id_parts[-2] if len(id_parts) >= 2 else ""
    new_org_id = identity.get("organization_id") or org_id_fallback

    # CONFIG SAFETY (Phase 2D.2). A field mapping is only meaningful against
    # the SPECIFIC Salesforce org it was built from — "SiteVisit__c" on one
    # org can be a different object, or nothing at all, on another. So a
    # reconnect PRESERVES the existing mapping only when it is provably the
    # SAME org (org_id unchanged); any other outcome — a different org, or no
    # prior connection at all — starts with no config, and an Owner/Manager
    # must review and re-save it before it applies again. Never guessed,
    # never silently carried over.
    previous = _get_org_salesforce_connection(workspace_id)
    previous_org_id = str((previous or {}).get("org_id") or "")
    same_org = bool(previous_org_id) and previous_org_id == new_org_id
    preserved_config = (previous or {}).get("config") if same_org else None

    now = _now_iso()
    item = {
        "workspace_id": workspace_id,
        "provider": CRM_PROVIDER_SALESFORCE,
        "connected_by_user_id": user_id,
        "instance_url": instance_url,
        "refresh_token_enc": _kms_encrypt(refresh_token),
        "org_id": new_org_id,
        "sf_user_id": identity.get("user_id") or "",
        "sf_username": identity.get("preferred_username") or identity.get("email") or "",
        "connected_at": now,
        "updated_at": now,
    }
    if preserved_config is not None:
        item["config"] = preserved_config
    elif previous_org_id and not same_org:
        # Explicit audit trail for the case that matters most: an admin
        # reconnected to a DIFFERENT org and any prior mapping was dropped.
        # Read by org_salesforce_get_config to explain the empty state rather
        # than leaving an Owner guessing why their mapping "disappeared".
        item["config_cleared_reason"] = (
            f"Salesforce organisation changed ({previous_org_id} -> "
            f"{new_org_id}) on reconnect — mapping must be reconfigured")
        item["config_cleared_at"] = now
    # PutItem, not update — a re-connect fully replaces every OTHER field of
    # the prior connection (new identity, new token); only `config` is ever
    # deliberately carried across, and only under the same-org condition above.
    _org_crm_connections.put_item(Item=item)
    if previous_org_id and not same_org:
        print(f"[org-salesforce] workspace={workspace_id} reconnected to a "
              f"DIFFERENT org ({previous_org_id} -> {new_org_id}) — "
              f"config cleared")
    print(f"[org-salesforce] connected workspace={workspace_id} by user={user_id}")
    return _redirect(True)


def org_salesforce_status(event):
    """GET /workspaces/{workspace_id}/crm/salesforce/status (JWT)
    -> {connected, instance_url?, sf_username?, connected_by_user_id?, connected_at?}.

    Readable by ANY active member (not gated on CAP_MANAGE_INTEGRATIONS): a
    Member needs to see that Organisation Salesforce is connected — and by
    whom — to use it on a meeting, per spec section 5 ("Members may use an
    existing configured organisation CRM connection"). Only connect/
    disconnect/configure are privileged.
    """
    workspace_id = _url_unquote(
        (event.get("pathParameters") or {}).get("workspace_id", ""))
    _user_id, wid, _membership = _require_workspace_member(
        event, workspace_id=workspace_id)
    conn = _get_org_salesforce_connection(wid)
    if not conn:
        return _resp(200, {"connected": False})
    out = {
        "connected": True,
        "instance_url": conn.get("instance_url", ""),
        "sf_username": conn.get("sf_username", ""),
        "connected_by_user_id": conn.get("connected_by_user_id", ""),
        "connected_at": conn.get("connected_at", ""),
        "configured": bool(_mappings_from_config(conn.get("config") or {})),
    }
    # Present only right after a reconnect changed the underlying Salesforce
    # org — see org_salesforce_callback. Read once and never repeated: the
    # next config save (or the next reconnect) is what clears it, since by
    # then either an Owner/Manager has acted on it or a fresher reason exists.
    if conn.get("config_cleared_reason"):
        out["config_cleared_reason"] = conn["config_cleared_reason"]
        out["config_cleared_at"] = conn.get("config_cleared_at", "")
    return _resp(200, out)


def org_salesforce_disconnect(event):
    """DELETE /workspaces/{workspace_id}/crm/salesforce (JWT) -> {disconnected}.

    OWNER/MANAGER ONLY, same gate as connect.
    """
    workspace_id = _url_unquote(
        (event.get("pathParameters") or {}).get("workspace_id", ""))
    _user_id, wid, _membership = _require_workspace_capability(
        event, workspace_schema.CAP_MANAGE_INTEGRATIONS, workspace_id=workspace_id)
    conn = _get_org_salesforce_connection(wid)
    if conn and conn.get("refresh_token_enc"):
        try:
            refresh_token = _kms_decrypt(conn["refresh_token_enc"])
            _salesforce.revoke(refresh_token)
        except Exception as e:  # noqa: BLE001 - revoke is best-effort, never blocks disconnect
            print(f"[org-salesforce] revoke on disconnect failed (non-fatal): {e}")
    _org_crm_connections.delete_item(
        Key={"workspace_id": wid, "provider": CRM_PROVIDER_SALESFORCE})
    print(f"[org-salesforce] disconnected workspace={wid}")
    return _resp(200, {"disconnected": True})


# ---------------------------------------------------------------------------
# ORGANISATION CRM configuration (Phase 2D.2).
#
# The workspace-scoped twin of the Personal config routes below. Every rule
# — generic object/field discovery, the four optional content targets, one
# mapping per object, CRM_MAX_MAPPINGS — is IDENTICAL, because a Salesforce
# schema does not care who owns the connection. So these three handlers are
# thin: resolve+authorize the workspace, then call straight into the SAME
# _sf_object_is_selectable/_score_*/_validate_mapping/_mappings_from_config/
# _public_mapping/_public_crm_config functions Personal uses, parameterized
# by _sf_call_org instead of _sf_call. No rule is duplicated; only the
# identity/authorization/storage differs.
#
# READ (objects, fields, config): any active member — a Member must be able
# to SEE the configured mapping to understand what a push will do, even
# though only Owner/Manager may change it.
# WRITE (config): CAP_MANAGE_INTEGRATIONS (Owner/Manager), same gate as
# connect/disconnect.
# ---------------------------------------------------------------------------

def org_salesforce_list_objects(event):
    """GET /workspaces/{workspace_id}/crm/salesforce/objects (JWT)
    -> {objects:[...], suggested}."""
    workspace_id = _url_unquote(
        (event.get("pathParameters") or {}).get("workspace_id", ""))
    _user_id, wid, _membership = _require_workspace_member(
        event, workspace_id=workspace_id)
    raw = _sf_call_org(wid, lambda url, tok: _salesforce.list_objects(url, tok))

    objects, best, best_score = [], None, 0
    for obj in raw:
        if not _sf_object_is_selectable(obj):
            continue
        objects.append({
            "name": obj.get("name", ""),
            "label": obj.get("label", "") or obj.get("name", ""),
            "custom": bool(obj.get("custom")),
        })
        score = _score_site_visit_object(obj)
        if score > best_score:
            best, best_score = obj.get("name", ""), score

    objects.sort(key=lambda o: (not o["custom"], o["label"].lower()))
    return _resp(200, {"objects": objects, "suggested": best})


def org_salesforce_list_fields(event):
    """GET /workspaces/{workspace_id}/crm/salesforce/fields/{object_name} (JWT)
    -> {object, number_fields, long_text_fields, suggested:{...}}."""
    workspace_id = _url_unquote(
        (event.get("pathParameters") or {}).get("workspace_id", ""))
    _user_id, wid, _membership = _require_workspace_member(
        event, workspace_id=workspace_id)
    object_name = (event.get("pathParameters") or {}).get("object_name", "")
    object_name = _url_unquote(object_name).strip()
    if not object_name or not re.match(r"^[A-Za-z0-9_]{1,80}$", object_name):
        raise ApiError(400, "valid object_name required")

    described = _sf_call_org(wid, lambda url, tok:
                             _salesforce.describe_object(url, tok, object_name))
    fields = described.get("fields") or []

    number_fields, long_text_fields = [], []
    best_number, best_number_score = None, 0
    for f in fields:
        ftype = f.get("type") or ""
        public = _public_sf_field(f)
        if f.get("filterable") and ftype in SF_IDENTIFIER_TYPES:
            number_fields.append(public)
            score = _score_number_field(f)
            if score > best_number_score:
                best_number, best_number_score = public["name"], score
        if (f.get("updateable") and ftype in SF_LONG_TEXT_TYPES
                and not f.get("calculated")):
            long_text_fields.append(public)

    number_fields.sort(key=lambda f: (not f["custom"], f["label"].lower()))
    long_text_fields.sort(key=lambda f: (-f["length"], f["label"].lower()))

    suggested = {"lookup_field": best_number}
    taken = set()
    for key, label, needs_long in CRM_DATA_TARGETS:
        pool = [f for f in long_text_fields if f["name"] not in taken
                and (not needs_long or f["length"] >= 255)]
        pick, pick_score = None, 0
        for f in pool:
            score = _score_data_field(
                next((x for x in fields if x.get("name") == f["name"]), {}), label)
            if score > pick_score:
                pick, pick_score = f["name"], score
        if pick and pick_score >= 50:
            suggested[key] = pick
            taken.add(pick)
        else:
            suggested[key] = None

    return _resp(200, {
        "object": object_name,
        "label": described.get("label") or object_name,
        "number_fields": number_fields,
        "long_text_fields": long_text_fields,
        "suggested": suggested,
        "transcript_min_length": SF_TRANSCRIPT_MIN_LENGTH,
    })


def org_salesforce_get_config(event):
    """GET /workspaces/{workspace_id}/crm/salesforce/config (JWT)
    -> {config:{enabled, mappings:[...]}}."""
    workspace_id = _url_unquote(
        (event.get("pathParameters") or {}).get("workspace_id", ""))
    _user_id, wid, _membership = _require_workspace_member(
        event, workspace_id=workspace_id)
    conn = _get_org_salesforce_connection(wid)
    if not conn:
        raise OrganisationSalesforceNotConnected()
    return _resp(200, {"config": _public_crm_config(conn)})


def org_salesforce_put_config(event):
    """PUT /workspaces/{workspace_id}/crm/salesforce/config (JWT) -> {config}.

    OWNER/MANAGER ONLY. Body shape and every validation rule are identical to
    Personal's salesforce_put_config — see _validate_mapping. Saving ALSO
    clears any pending config_cleared_reason from a prior org-change
    reconnect (see org_salesforce_callback): once Owner/Manager has reviewed
    and saved a mapping, the "your config was cleared" notice has served its
    purpose and must not keep reappearing on every status check.
    """
    workspace_id = _url_unquote(
        (event.get("pathParameters") or {}).get("workspace_id", ""))
    _user_id, wid, _membership = _require_workspace_capability(
        event, workspace_schema.CAP_MANAGE_INTEGRATIONS, workspace_id=workspace_id)
    conn = _get_org_salesforce_connection(wid)
    if not conn:
        raise OrganisationSalesforceNotConnected()

    data = _body(event)
    requested = data.get("mappings")
    if requested is None:
        raise ApiError(400, "mappings required (a list, possibly empty)")
    if not isinstance(requested, list):
        raise ApiError(400, "mappings must be a list")
    if len(requested) > CRM_MAX_MAPPINGS:
        raise ApiError(400, f"at most {CRM_MAX_MAPPINGS} mappings")
    if any(not isinstance(m, dict) for m in requested):
        raise ApiError(400, "each mapping must be an object")

    mappings, objects_seen = [], set()
    sf_call = lambda fn: _sf_call_org(wid, fn)  # noqa: E731
    for raw in requested:
        mapping = _validate_mapping(sf_call, raw)
        if mapping["object"] in objects_seen:
            raise ApiError(400, f"{mapping['object']} is mapped twice — one "
                                f"lookup field per object")
        objects_seen.add(mapping["object"])
        mappings.append(mapping)

    cfg = {"mappings": mappings, "updated_at": _now_iso()}
    _org_crm_connections.update_item(
        Key={"workspace_id": wid, "provider": CRM_PROVIDER_SALESFORCE},
        UpdateExpression=("SET config = :c, updated_at = :now "
                          "REMOVE config_cleared_reason, config_cleared_at"),
        ConditionExpression="attribute_exists(workspace_id)",
        ExpressionAttributeValues={":c": cfg, ":now": _now_iso()},
    )
    print(f"[org-salesforce] config saved for workspace={wid}: "
          + (", ".join(f"{m['object']}.{m['lookup_field']}" for m in mappings)
             or "no mappings"))
    updated = _get_org_salesforce_connection(wid)
    return _resp(200, {"config": _public_crm_config(updated)})


# ===========================================================================
# MEETING IDENTITY -> SALESFORCE (Phase 2D.3)
#
# THE CORE RULE (spec section 1): MinuteX must know WHO participated before
# it pushes anything meaningful to Salesforce. The Organisation Salesforce
# CONNECTION (2D.1) is only the technical credential that performs the API
# call — it is never the business owner of a meeting, and it is never
# substituted for actually resolving who spoke. This section is the identity
# layer that sits between "a meeting has a transcript" and "Salesforce has a
# Contact/User/Account to attach data to":
#
#   speaker (MeetingParticipants.identity_role, added above)
#     INTERNAL -> MinuteX org member (user_id) -> Salesforce USER
#                 (OrgSalesforceUserLinks, this section)
#     EXTERNAL -> MinuteX organisation Contact (contact_id) -> Salesforce
#                 CONTACT (+ ACCOUNT), stored on the Contacts row itself
#                 (crm_provider/crm_external_id/crm_account_id, added above)
#
# ONE SOURCE OF TRUTH (spec section 21): every route below that needs "what
# is this meeting's resolved CRM picture" calls _meeting_crm_identity_state,
# and nothing computes that picture a second way. Nothing here ever
# name-matches a speaker to a Salesforce record automatically — every
# stored crm_external_id/sf_user_id is the record of an EXPLICIT confirmation
# (a human picked one candidate from a search), never an inference.
# ---------------------------------------------------------------------------

def _require_org_meeting(event, *, hydrate=False):
    """(user_id, key, item, workspace_id) for a meeting that belongs to an
    ORGANISATION, or a clean error for every other case.

    THE GATE every Phase 2D.3 route in this section opens with. Reuses
    _owned_recording (the existing, exhaustively-tested write-capable
    meeting gate — creator/device-owner, org-membership-checked-first) for
    ownership/authorization, then adds exactly one more check on top: the
    meeting must actually belong to an organisation. A Personal meeting
    reaching one of these routes is a client bug, not a permission question,
    so it is refused with 400 rather than silently resolved against nothing.

    `hydrate` is forwarded to _owned_recording — pass True only for routes
    that actually read transcript/summary text (the CRM review/push routes),
    so every identity-only route stays a single DynamoDB read.
    """
    user_id, key, item = _owned_recording(event, hydrate=hydrate)
    workspace_id = _row_workspace_id(item)
    if not workspace_schema.is_organisation_workspace_id(workspace_id):
        raise ApiError(400, "this meeting is not part of an organisation")
    return user_id, key, item, workspace_id


def _sf_search(sf_call, object_name: str, select: list, where: str,
              limit=None) -> list:
    """Generic SOQL search — reuses _soql_quote's escaping and the same
    `sf_call` seam every other CRM function in this file takes.

    `where` is a caller-built clause with values ALREADY passed through
    _soql_quote — kept as one string rather than a structured filter because
    every call site here has a genuinely different shape (an OR across
    Email/Phone for a Contact search, a single LIKE for a User search), and
    forcing them through one filter-builder would buy no real reuse.

    `limit` defaults to CRM_AMBIGUOUS_LIMIT, resolved INSIDE the call rather
    than as the parameter default: that constant is defined later in this
    file (in the Personal CRM section), and a default evaluated at function-
    definition time would raise NameError at import — the same reason
    several other module-level defaults in this file are resolved lazily.
    """
    if limit is None:
        limit = CRM_AMBIGUOUS_LIMIT
    soql = f"SELECT {', '.join(select)} FROM {object_name} WHERE {where} LIMIT {limit}"
    result = sf_call(lambda url, tok: _salesforce.query(url, tok, soql))
    return [r for r in (result.get("records") or []) if isinstance(r, dict)]


def _org_sf_call(workspace_id: str):
    """The org's sf_call closure — the one place every route in this section
    gets it, so a future change to how that closure is built (e.g. adding a
    cache) needs no per-route change."""
    return lambda fn: _sf_call_org(workspace_id, fn)


def crm_search_salesforce_contacts(event):
    """GET /workspaces/{workspace_id}/crm/salesforce/search/contacts?q=...
    (JWT, any active member) -> {records:[{record_id, name, email, phone,
    account_id, account_name}], truncated}

    Used by the speaker-tagging screen to let a human pick the Salesforce
    Contact an EXTERNAL speaker corresponds to. Matches on email OR phone OR
    name — a broad, human-reviewed candidate list, never an auto-pick: the
    caller always confirms via resolve_contact_crm_identity, which is the
    only route that PERSISTS a match.
    """
    workspace_id = _url_unquote(
        (event.get("pathParameters") or {}).get("workspace_id", ""))
    _user_id, wid, _membership = _require_workspace_member(
        event, workspace_id=workspace_id)
    q = str((event.get("queryStringParameters") or {}).get("q") or "").strip()
    if not q:
        raise ApiError(400, "q required")
    if len(q) > CRM_IDENTIFIER_MAX:
        raise ApiError(400, f"q must be at most {CRM_IDENTIFIER_MAX} characters")

    sf_call = _org_sf_call(wid)
    quoted = _soql_quote(q)
    where = (f"Email = '{quoted}' OR Phone = '{quoted}' OR MobilePhone = '{quoted}' "
            f"OR Name LIKE '%{quoted}%'")
    records = _sf_search(
        sf_call, "Contact",
        ["Id", "Name", "Email", "Phone", "AccountId", "Account.Name"],
        where, limit=CRM_AMBIGUOUS_LIMIT + 1)
    truncated = len(records) > CRM_AMBIGUOUS_LIMIT
    records = records[:CRM_AMBIGUOUS_LIMIT]
    return _resp(200, {
        "records": [{
            "record_id": str(r.get("Id") or ""),
            "name": str(r.get("Name") or ""),
            "email": str(r.get("Email") or ""),
            "phone": str(r.get("Phone") or ""),
            "account_id": str(r.get("AccountId") or ""),
            "account_name": str((r.get("Account") or {}).get("Name") or "")
                            if isinstance(r.get("Account"), dict) else "",
        } for r in records],
        "truncated": truncated,
    })


def crm_search_salesforce_users(event):
    """GET /workspaces/{workspace_id}/crm/salesforce/search/users?q=...
    (JWT, any active member) -> {records:[{record_id, name, username, email}],
    truncated}

    For an INTERNAL speaker: the candidate list of Salesforce Users an org
    member might correspond to. Deliberately does NOT assume
    "MinuteX email == Salesforce username" (spec section 7) — it searches
    Name/Username/Email independently and hands back candidates for a human
    to confirm, exactly like the Contact search above.
    """
    workspace_id = _url_unquote(
        (event.get("pathParameters") or {}).get("workspace_id", ""))
    _user_id, wid, _membership = _require_workspace_member(
        event, workspace_id=workspace_id)
    q = str((event.get("queryStringParameters") or {}).get("q") or "").strip()
    if not q:
        raise ApiError(400, "q required")
    if len(q) > CRM_IDENTIFIER_MAX:
        raise ApiError(400, f"q must be at most {CRM_IDENTIFIER_MAX} characters")

    sf_call = _org_sf_call(wid)
    quoted = _soql_quote(q)
    where = (f"IsActive = true AND (Username = '{quoted}' OR Email = '{quoted}' "
            f"OR Name LIKE '%{quoted}%')")
    records = _sf_search(sf_call, "User", ["Id", "Name", "Username", "Email"],
                        where, limit=CRM_AMBIGUOUS_LIMIT + 1)
    truncated = len(records) > CRM_AMBIGUOUS_LIMIT
    records = records[:CRM_AMBIGUOUS_LIMIT]
    return _resp(200, {
        "records": [{
            "record_id": str(r.get("Id") or ""),
            "name": str(r.get("Name") or ""),
            "username": str(r.get("Username") or ""),
            "email": str(r.get("Email") or ""),
        } for r in records],
        "truncated": truncated,
    })


def resolve_contact_crm_identity(event):
    """PUT /contacts/{contact_id}/crm {record_id, account_id?} (JWT)
    -> {contact}

    Persists an EXPLICIT match: this organisation Contact IS this Salesforce
    Contact (optionally with its Account). `record_id` must be one the
    caller actually saw from crm_search_salesforce_contacts (or already knew)
    — this route does not search or guess, it only stores what it is told,
    which is what keeps "who confirmed this match" a real, auditable fact
    (spec section 21: one source of truth, never re-derived differently
    elsewhere).

    AUTHORIZATION: the same _taggable_contact/_owned_contact rule every other
    contact mutation uses — for a SHARED organisation contact that means
    OWNER/MANAGER only (CAP_MANAGE_CONTACTS), consistent with "only
    Owner/Manager can modify Organisation CRM configuration"-adjacent data;
    an ordinary member tagging a speaker as external still triggers a
    SEARCH+resolve only when the contact isn't already linked, and an Owner/
    Manager is expected to have done that linking as part of Salesforce
    setup — mirrors how only Owner/Manager may edit a shared contact at all.
    """
    contact_id = str((event.get("pathParameters") or {}).get("contact_id") or "").strip()
    if not contact_id:
        raise ApiError(400, "contact id required")
    user_id = _require_auth(event)
    contact = _owned_contact(user_id, contact_id, write=True)

    data = _body(event)
    record_id = str(data.get("record_id") or "").strip()
    account_id = str(data.get("account_id") or "").strip()
    if not record_id:
        raise ApiError(400, "record_id required")
    if len(record_id) > 40 or len(account_id) > 40:
        # Salesforce Ids are 15 or 18 chars; 40 is a generous ceiling that
        # rejects garbage without hardcoding Salesforce's own format rules.
        raise ApiError(400, "invalid Salesforce record id")

    now = _now_iso()
    update = {
        "crm_provider": CRM_PROVIDER_SALESFORCE,
        "crm_object_type": "Contact",
        "crm_external_id": record_id,
        "crm_account_id": account_id,
        "crm_resolved_at": now,
        "crm_resolved_by": user_id,
        "updated_at": now,
    }
    names = {f"#{k}": k for k in update}
    _contacts.update_item(
        Key={"contact_id": contact_id},
        UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in update),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues={f":{k}": v for k, v in update.items()},
    )
    print(f"[crm-identity] contact {contact_id} resolved to Salesforce "
          f"Contact {record_id} by {user_id}")
    updated = _contacts.get_item(Key={"contact_id": contact_id}).get("Item") or contact
    return _resp(200, {"contact": _public_contact(
        updated, member_roles=_roles_for_contact(updated))})


def resolve_member_crm_identity(event):
    """PUT /workspaces/{workspace_id}/crm/salesforce/members/{user_id}
    {record_id} (JWT) -> {link}

    Persists an EXPLICIT match: this organisation MEMBER IS this Salesforce
    User. `record_id` comes from crm_search_salesforce_users — never
    inferred from "MinuteX email == Salesforce username" (spec section 7).

    RBAC: CAP_MANAGE_INTEGRATIONS (Owner/Manager) — this is organisation-wide
    identity configuration (one member's Salesforce identity applies to
    every meeting they are tagged Internal/SM on), not a per-meeting
    action, so it sits at the same privilege level as connecting Salesforce
    itself rather than at ordinary meeting-CRM-use level.
    """
    workspace_id = _url_unquote(
        (event.get("pathParameters") or {}).get("workspace_id", ""))
    target_user_id = str(
        (event.get("pathParameters") or {}).get("user_id") or "").strip()
    if not target_user_id:
        raise ApiError(400, "user id required")
    actor_id, wid, _membership = _require_workspace_capability(
        event, workspace_schema.CAP_MANAGE_INTEGRATIONS, workspace_id=workspace_id)
    # The target must themselves be an active member of THIS workspace — an
    # Owner cannot pre-link a Salesforce User for someone outside the org.
    if not _active_membership(wid, target_user_id):
        raise ApiError(404, "member not found")

    data = _body(event)
    record_id = str(data.get("record_id") or "").strip()
    if not record_id:
        raise ApiError(400, "record_id required")
    if len(record_id) > 40:
        raise ApiError(400, "invalid Salesforce record id")

    username = str(data.get("username") or "").strip()[:CONTACT_EMAIL_MAX]
    now = _now_iso()
    item = {
        "workspace_id": wid,
        "user_id": target_user_id,
        "sf_user_id": record_id,
        "sf_username": username,
        "resolved_by": actor_id,
        "resolved_at": now,
        "updated_at": now,
    }
    _org_salesforce_user_links.put_item(Item=item)
    print(f"[crm-identity] workspace={wid} member={target_user_id} resolved "
          f"to Salesforce User {record_id} by {actor_id}")
    return _resp(200, {"link": {
        "user_id": target_user_id, "sf_user_id": record_id,
        "sf_username": username, "resolved_at": now,
    }})


def _get_member_sf_user_link(workspace_id: str, user_id: str) -> dict:
    """The stored Salesforce User link for one member, or {} if unresolved."""
    return _org_salesforce_user_links.get_item(
        Key={"workspace_id": workspace_id, "user_id": user_id}).get("Item") or {}


# ---------------------------------------------------------------------------
# CRM REVIEW — the ONE function that computes "is this meeting's identity
# picture ready to push" (spec sections 9, 21). Every route that needs this
# answer — the review screen AND the push gate — calls THIS, so the rule for
# "what counts as resolved" is never re-derived two different ways.
# ---------------------------------------------------------------------------
SPEAKER_RESOLUTION_RESOLVED = "resolved"
SPEAKER_RESOLUTION_UNRESOLVED = "unresolved"
# A speaker tagged Internal/External but whose linked contact/member has no
# CONFIRMED Salesforce identity yet — distinct from UNRESOLVED (no identity_
# role/contact at all): this one has a next step ("resolve their Salesforce
# match"), that one has an earlier one ("tag this speaker first").
SPEAKER_RESOLUTION_NOT_LINKED = "not_linked"


def _speaker_crm_state(row: dict, workspace_id: str) -> dict:
    """One MeetingParticipants row -> its CRM identity picture.

    Deliberately reads ONLY what set_participant/resolve_contact_crm_identity/
    resolve_member_crm_identity already persisted — never a live Salesforce
    call, so opening the review screen is cheap and never itself fails on a
    dead Salesforce connection (the push step is where a live call happens
    and where that failure belongs).
    """
    speaker_id = row.get("speaker_id", "")
    identity_role = str(row.get("identity_role") or "")
    contact_id = str(row.get("contact_id") or "")
    base = {"speaker_id": speaker_id, "identity_role": identity_role,
           "contact_id": contact_id}

    if not identity_role:
        return {**base, "status": SPEAKER_RESOLUTION_UNRESOLVED,
               "reason": "not tagged Internal or External yet"}

    if identity_role == SPEAKER_IDENTITY_INTERNAL:
        contact = _contacts.get_item(Key={"contact_id": contact_id}).get("Item") \
            if contact_id else None
        member_user_id = str((contact or {}).get("minutex_user_id") or "")
        if not member_user_id:
            return {**base, "status": SPEAKER_RESOLUTION_UNRESOLVED,
                   "reason": "tagged Internal but not linked to an organisation member"}
        link = _get_member_sf_user_link(workspace_id, member_user_id)
        if not link.get("sf_user_id"):
            return {**base, "status": SPEAKER_RESOLUTION_NOT_LINKED,
                   "minutex_user_id": member_user_id,
                   "reason": "no Salesforce User confirmed for this member yet"}
        return {**base, "status": SPEAKER_RESOLUTION_RESOLVED,
               "minutex_user_id": member_user_id,
               "sf_user_id": link["sf_user_id"],
               "sf_username": link.get("sf_username", "")}

    # EXTERNAL.
    if not contact_id:
        return {**base, "status": SPEAKER_RESOLUTION_UNRESOLVED,
               "reason": "tagged External but not linked to a contact yet"}
    contact = _contacts.get_item(Key={"contact_id": contact_id}).get("Item")
    if not contact:
        return {**base, "status": SPEAKER_RESOLUTION_UNRESOLVED,
               "reason": "linked contact no longer exists"}
    sf_contact_id = str(contact.get("crm_external_id") or "")
    if not sf_contact_id:
        return {**base, "status": SPEAKER_RESOLUTION_NOT_LINKED,
               "reason": "no Salesforce Contact confirmed for this person yet"}
    return {**base, "status": SPEAKER_RESOLUTION_RESOLVED,
           "sf_contact_id": sf_contact_id,
           "sf_account_id": str(contact.get("crm_account_id") or ""),
           "contact_name": str(contact.get("name") or "")}


def _meeting_crm_identity_state(key: str, item: dict, workspace_id: str) -> dict:
    """The full CRM identity picture for one meeting — the single function
    behind BOTH get_meeting_crm_review and the push gate in
    push_org_meeting_crm, so those two can never disagree about what
    "ready to push" means.

    Speakers with NO identity_role at all (the ordinary case for a meeting
    nobody intends to push — most attendees of most meetings) are reported
    but never block anything by themselves; only a mapping's ACTUAL required
    fields (see the caller) decide what must be resolved before a push.
    """
    rows = _participant_rows(key)
    speakers = [_speaker_crm_state(r, workspace_id) for r in rows
               if not _is_self_speaker(r.get("speaker_id", ""))]
    internal = [s for s in speakers if s["identity_role"] == SPEAKER_IDENTITY_INTERNAL]
    external = [s for s in speakers if s["identity_role"] == SPEAKER_IDENTITY_EXTERNAL]
    unresolved = [s for s in speakers
                 if s["status"] != SPEAKER_RESOLUTION_RESOLVED and s["identity_role"]]
    return {
        "speakers": speakers,
        "internal": internal,
        "external": external,
        # Only CLASSIFIED-but-unresolved speakers are surfaced as blocking —
        # an untagged speaker (identity_role == "") is not "unresolved", it is
        # simply not part of this meeting's CRM picture at all, per spec
        # section 4's ordinary resting state.
        "unresolved": unresolved,
        "owner": next((s for s in internal
                      if s["status"] == SPEAKER_RESOLUTION_RESOLVED), None),
        "primary_client": next((s for s in external
                                if s["status"] == SPEAKER_RESOLUTION_RESOLVED), None),
    }


def get_meeting_crm_review(event):
    """GET /recordings/ai/crm-review/{key+} (JWT, organisation meetings only)
    -> {meeting, identity, content, ready, blocking_reasons}

    Assembles the CRM Review screen's whole payload in one call: the
    resolved SM/Internal owner, the resolved external Contact(s)/Account,
    every unresolved speaker, and whether the meeting's CONTENT (summary,
    action items) is present. Never itself pushes anything — see
    push_org_meeting_crm for the mutating half.

    `ready` is computed against the mapping actually configured for this
    workspace (spec section 9: "based on which data is required by the
    configured Salesforce mapping") — a workspace with no content-field
    mapping at all is never blocked on identity resolution it does not need
    to push anything meaningful.
    """
    user_id, key, item, workspace_id = _require_org_meeting(event, hydrate=True)
    identity = _meeting_crm_identity_state(key, item, workspace_id)

    conn = _get_org_salesforce_connection(workspace_id)
    mappings = _mappings_from_config((conn or {}).get("config") or {}) if conn else []

    summary_text = str(item.get("summary") or "")
    highlights_text = _render_highlights(item)
    action_items_text = _render_action_items(item)
    action_item_count = len(list((item.get("tasks") or {}).values())) \
        if isinstance(item.get("tasks"), dict) else len(item.get("ai_tasks") or [])

    blocking_reasons = []
    if not conn:
        blocking_reasons.append("organisation_salesforce_not_connected")
    elif not mappings:
        blocking_reasons.append("no_salesforce_mapping_configured")
    if identity["unresolved"]:
        blocking_reasons.append("unresolved_speaker_identity")

    return _resp(200, {
        "meeting": {"title": item.get("title", ""), "audio_s3_key": key},
        "identity": identity,
        "content": {
            "summary_ready": bool(summary_text.strip()),
            "highlights_ready": bool(highlights_text.strip()),
            "action_items_ready": action_item_count > 0,
            "action_item_count": action_item_count,
        },
        "mappings": [_public_mapping(m) for m in mappings],
        "ready": not blocking_reasons,
        "blocking_reasons": blocking_reasons,
    })


# ---------------------------------------------------------------------------
# CRM configuration — the user maps THEIR org's schema, for ANY object.
#
# No object or field name is hardcoded anywhere in this codebase, and none
# should be: every Salesforce org names things differently, and a customer maps
# whichever objects they actually use — Site Visit, Lead, Contact, Opportunity,
# a custom object, several at once. The app reads the org's REAL schema through
# the Describe API and the user picks from it, which also makes an invalid
# configuration unrepresentable: you cannot select a field that isn't there.
#
# The config is a LIST of mappings, each one "this object, identified by this
# field":
#
#   {"enabled": true,
#    "mappings": [{"object": "Lead", "lookup_field": "Email",
#                  "label": "Lead Email", ...}, ...]}
#
# Every consumer — the API response, the app's inputs, extraction, lookup,
# push — iterates that list. Adding support for a new object is therefore a
# configuration change by the customer, never a code change here: there is no
# `if object == ...` anywhere, and an empty list legitimately means "connected,
# but show no record fields".
#
#   GET  /crm/salesforce/objects        -> the org's objects + a suggestion
#   GET  /crm/salesforce/fields/{obj}   -> that object's fields, bucketed by
#                                          what each one is usable FOR
#   GET  /crm/salesforce/config         -> {enabled, mappings:[...]}
#   PUT  /crm/salesforce/config         -> validate against Describe, save
#
# The suggestion heuristics only PRE-SELECT; the user always confirms.
# ---------------------------------------------------------------------------

# Field types that can hold a long block of prose (transcript, summary...).
# A 40-char Text field would silently truncate a 50KB transcript, so those
# are not offered as targets for the big fields at all.
SF_LONG_TEXT_TYPES = frozenset({"textarea", "richtextarea"})
# Minimum length before a textarea is worth offering for a transcript.
SF_TRANSCRIPT_MIN_LENGTH = 32_768
# Types that can identify a record by number/name.
SF_IDENTIFIER_TYPES = frozenset({"string", "double", "int", "currency",
                                 "percent", "reference", "id", "textarea",
                                 "phone", "url", "email"})

# The four optional data targets, in the order the UI shows them. `long`
# marks the ones that need a genuinely large field.
CRM_DATA_TARGETS = (
    ("transcript_field", "Transcript", True),
    ("summary_field", "Summary", True),
    ("highlights_field", "Highlights", True),
    ("action_items_field", "Action Items", True),
)
CRM_DATA_TARGET_KEYS = tuple(k for k, _, _ in CRM_DATA_TARGETS)

# How many objects one user may map. A ceiling, not a design limit: each
# mapping costs a describe call to validate and an extraction call per
# meeting, so an unbounded list would quietly become expensive.
CRM_MAX_MAPPINGS = int(os.environ.get("CRM_MAX_MAPPINGS", "10"))

# LEGACY key. The first cut of this feature stored ONE object plus a
# "site_visit_number_field", because Site Visit was the only object the MVP
# planned for. That was wrong: a customer maps whatever object they use (Lead,
# Opportunity, a custom object), so the config is now a LIST of mappings and
# nothing in this file special-cases site visits. Old rows are read through
# _mappings_from_config below and rewritten to the new shape on the next save,
# so existing configurations keep working without a migration.
LEGACY_SITE_VISIT_KEY = "site_visit_number_field"

# ---------------------------------------------------------------------------
# Sync status — one vocabulary for every mapping, per meeting.
#
# Deliberately object-neutral: the same states describe a site visit, a lead or
# a custom object. They are also the ONLY thing the UI branches on, so a new
# Salesforce object needs no new status and no new UI condition.
#
#   not_linked      no identifier yet (the common resting state)
#   lookup_pending  an identifier exists but hasn't been resolved
#   record_found    resolved to exactly one record, awaiting confirmation
#   ambiguous       the identifier matched several records; the user must pick
#   confirmed       the user approved this record for syncing
#   syncing         a push is in flight
#   synced          the configured fields were written to Salesforce
#   failed          the last lookup or push failed; retryable
#
# CONFIRMATION IS LOAD-BEARING: nothing transitions record_found -> syncing
# without an explicit user action, because pushing notes onto the wrong record
# is worse than not pushing at all.
# ---------------------------------------------------------------------------
CRM_STATUS_NOT_LINKED = "not_linked"
CRM_STATUS_LOOKUP_PENDING = "lookup_pending"
CRM_STATUS_RECORD_FOUND = "record_found"
CRM_STATUS_AMBIGUOUS = "ambiguous"
CRM_STATUS_CONFIRMED = "confirmed"
CRM_STATUS_SYNCING = "syncing"
CRM_STATUS_SYNCED = "synced"
CRM_STATUS_FAILED = "failed"
CRM_STATUSES = (CRM_STATUS_NOT_LINKED, CRM_STATUS_LOOKUP_PENDING,
                CRM_STATUS_RECORD_FOUND, CRM_STATUS_AMBIGUOUS,
                CRM_STATUS_CONFIRMED, CRM_STATUS_SYNCING,
                CRM_STATUS_SYNCED, CRM_STATUS_FAILED)

# Statuses from which a push may start. Anything else means either "nothing to
# push" or "the user hasn't approved this record yet".
CRM_PUSHABLE_STATUSES = frozenset({CRM_STATUS_CONFIRMED, CRM_STATUS_SYNCED,
                                   CRM_STATUS_FAILED})

# The four optional content targets, as the API exposes them (nested) vs. how
# they are stored on a mapping (flat `<name>_field`). Nested is what the client
# reads; flat is what the existing config validation already writes, so both
# shapes stay in sync without a migration.
CRM_CONTENT_TARGET_NAMES = ("transcript", "summary", "highlights", "action_items")

# "Nothing matched" is a LOOKUP OUTCOME, not a stored status: a meeting whose
# identifier resolves to nothing stays lookup_pending (the user fixes the
# identifier), so this sentinel never reaches DynamoDB or the status enum.
CRM_LOOKUP_NOT_FOUND = "not_found"

# How many ambiguous candidates to offer. A non-unique identifier is a data
# problem in the org; showing a handful is enough for the user to recognize the
# right record, and an unbounded list would be unusable anyway.
CRM_AMBIGUOUS_LIMIT = int(os.environ.get("CRM_AMBIGUOUS_LIMIT", "10"))


def _sf_object_is_selectable(obj: dict) -> bool:
    """Objects worth showing in the picker.

    A global describe returns ~1000 entries in a real org, most of which are
    Salesforce internals (share rows, history tables, feed items) that nobody
    would ever push meeting notes to. Requiring updateable+queryable and
    dropping the machine-generated suffixes keeps the list navigable.
    """
    name = obj.get("name") or ""
    if not obj.get("updateable") or not obj.get("queryable"):
        return False
    if obj.get("deprecatedAndHidden") or obj.get("customSetting"):
        return False
    return not name.endswith(("Share", "History", "Feed", "ChangeEvent", "Tag"))


def _score_site_visit_object(obj: dict) -> int:
    """How likely this object is the org's "site visit" record. 0 = not."""
    haystack = f"{obj.get('label', '')} {obj.get('name', '')}".lower()
    compact = re.sub(r"[^a-z]", "", haystack)
    if "sitevisit" in compact:
        return 100
    if "site" in haystack and "visit" in haystack:
        return 90
    if "visit" in haystack:
        return 50
    if "inspection" in haystack or "sitesurvey" in compact:
        return 30
    return 0


def _score_number_field(field: dict) -> int:
    """How likely this field holds the human-facing site visit number.

    Scored off the API NAME first, then the label. Orgs routinely leave the
    standard `Name` field labelled "Site Visit Number" while ALSO having a
    real `Site_Visit_Number__c` — scoring the label alone makes those tie, and
    the purpose-built custom field is the better guess: someone created it
    deliberately for exactly this.
    """
    name = (field.get("name") or "")
    label = (field.get("label") or "")
    name_compact = re.sub(r"[^a-z]", "", name.lower())
    label_compact = re.sub(r"[^a-z]", "", label.lower())
    score = 0
    if "sitevisitnumber" in name_compact:
        score = 100
    elif "visitnumber" in name_compact:
        score = 92
    elif "sitevisitnumber" in label_compact:
        score = 85
    elif "visitnumber" in label_compact:
        score = 80
    elif field.get("nameField"):
        # The org's own Name field is the usual human identifier, and on an
        # auto-number object it IS the visit number.
        score = 70
    elif "number" in name_compact or "reference" in name_compact:
        score = 60
    elif "number" in label_compact or "reference" in label_compact:
        score = 55
    if field.get("autoNumber"):
        score += 6
    if field.get("unique"):
        score += 6
    return score


def _score_data_field(field: dict, label: str) -> int:
    """How likely this long-text field is meant for `label`'s content."""
    haystack = f"{field.get('label', '')} {field.get('name', '')}".lower()
    want = label.lower().split()
    score = 0
    if all(w in haystack for w in want):
        score = 80
    elif want[0] in haystack:
        score = 50
    # Prefer a roomier field when names tie — a transcript needs the space.
    return score + min(int(field.get("length") or 0) // 32_768, 5)


def _public_sf_field(field: dict) -> dict:
    return {
        "name": field.get("name", ""),
        "label": field.get("label", "") or field.get("name", ""),
        "type": field.get("type", ""),
        "length": int(field.get("length") or 0),
        "custom": bool(field.get("custom")),
    }


def salesforce_list_objects(event):
    """GET /crm/salesforce/objects (JWT) -> {objects:[...], suggested}.

    `suggested` is the best guess at the org's site-visit object so the app can
    pre-select it. null when nothing scored — the user then picks manually.
    """
    user_id = _require_auth(event)
    raw = _sf_call(user_id, lambda url, tok: _salesforce.list_objects(url, tok))

    objects, best, best_score = [], None, 0
    for obj in raw:
        if not _sf_object_is_selectable(obj):
            continue
        objects.append({
            "name": obj.get("name", ""),
            "label": obj.get("label", "") or obj.get("name", ""),
            "custom": bool(obj.get("custom")),
        })
        score = _score_site_visit_object(obj)
        if score > best_score:
            best, best_score = obj.get("name", ""), score

    objects.sort(key=lambda o: (not o["custom"], o["label"].lower()))
    print(f"[salesforce] objects: {len(objects)} selectable, suggested={best!r}")
    return _resp(200, {"objects": objects, "suggested": best})


def salesforce_list_fields(event):
    """GET /crm/salesforce/fields/{object_name} (JWT).

    -> {object, number_fields, long_text_fields, suggested:{...}}

    Two buckets because the two kinds of target have genuinely different
    requirements: the number field must be readable/filterable to look a
    record UP, while the content fields must be big enough to hold a
    transcript without truncating it.
    """
    user_id = _require_auth(event)
    object_name = (event.get("pathParameters") or {}).get("object_name", "")
    object_name = _url_unquote(object_name).strip()
    if not object_name or not re.match(r"^[A-Za-z0-9_]{1,80}$", object_name):
        raise ApiError(400, "valid object_name required")

    described = _sf_call(user_id, lambda url, tok:
                         _salesforce.describe_object(url, tok, object_name))
    fields = described.get("fields") or []

    number_fields, long_text_fields = [], []
    best_number, best_number_score = None, 0
    for f in fields:
        ftype = f.get("type") or ""
        public = _public_sf_field(f)

        # Lookup key: must be filterable (we SOQL on it) — a field we cannot
        # filter cannot find a record, no matter how well-named it is.
        if f.get("filterable") and ftype in SF_IDENTIFIER_TYPES:
            number_fields.append(public)
            score = _score_number_field(f)
            if score > best_number_score:
                best_number, best_number_score = public["name"], score

        # Content target: must be writable AND long. Formula/auto-number
        # fields are read-only, so a push to them always fails.
        if (f.get("updateable") and ftype in SF_LONG_TEXT_TYPES
                and not f.get("calculated")):
            long_text_fields.append(public)

    number_fields.sort(key=lambda f: (not f["custom"], f["label"].lower()))
    long_text_fields.sort(key=lambda f: (-f["length"], f["label"].lower()))

    # Suggest a content target per data field, never reusing one field twice:
    # writing the transcript and the summary to the same field would mean one
    # silently overwrites the other.
    # `lookup_field` is the generic key. The old `site_visit_number_field` is
    # echoed alongside it so an app build from before this change keeps
    # pre-selecting correctly (see the compatibility note on the config).
    suggested = {"lookup_field": best_number,
                 LEGACY_SITE_VISIT_KEY: best_number}
    taken = set()
    for key, label, needs_long in CRM_DATA_TARGETS:
        pool = [f for f in long_text_fields if f["name"] not in taken
                and (not needs_long or f["length"] >= 255)]
        pick, pick_score = None, 0
        for f in pool:
            score = _score_data_field(
                next((x for x in fields if x.get("name") == f["name"]), {}), label)
            if score > pick_score:
                pick, pick_score = f["name"], score
        # Only suggest on a real name match. Guessing "the biggest empty text
        # field" would put a transcript somewhere arbitrary in the user's CRM.
        if pick and pick_score >= 50:
            suggested[key] = pick
            taken.add(pick)
        else:
            suggested[key] = None

    return _resp(200, {
        "object": object_name,
        "label": described.get("label") or object_name,
        "number_fields": number_fields,
        "long_text_fields": long_text_fields,
        "suggested": suggested,
        "transcript_min_length": SF_TRANSCRIPT_MIN_LENGTH,
    })


def _mappings_from_config(cfg: dict) -> list:
    """The stored config -> a list of mappings, in the generic shape.

    THE compatibility seam. A config written by the first version of this
    feature is a single object with a `site_visit_number_field`; a config
    written now is `{"mappings": [...]}`. Everything downstream — the API
    response, the UI, extraction, lookup, push — reads mappings through here
    and therefore never needs to know which shape it came from, nor that
    "site visit" was ever special.
    """
    if not isinstance(cfg, dict):
        return []
    stored = cfg.get("mappings")
    if isinstance(stored, list):
        return [m for m in stored if isinstance(m, dict) and m.get("object")
                and m.get("lookup_field")]
    # Legacy single-object shape.
    obj = cfg.get("object")
    lookup = cfg.get(LEGACY_SITE_VISIT_KEY)
    if not obj or not lookup:
        return []
    legacy = {
        "object": obj,
        "object_label": cfg.get("object_label") or obj,
        "lookup_field": lookup,
        # The old shape carried no label for the lookup field. "<Object> Number"
        # is what the old UI hardcoded, so an upgraded user sees the same words
        # they saw before.
        "label": f"{cfg.get('object_label') or obj} Number",
    }
    for key in CRM_DATA_TARGET_KEYS:
        legacy[key] = cfg.get(key) or ""
    return [legacy]


def _public_mapping(m: dict) -> dict:
    out = {
        "object": m.get("object") or "",
        "object_label": m.get("object_label") or m.get("object") or "",
        "lookup_field": m.get("lookup_field") or "",
        "lookup_field_label": m.get("lookup_field_label") or "",
        "lookup_field_type": m.get("lookup_field_type") or "",
        "label": m.get("label") or "",
    }
    for key in CRM_DATA_TARGET_KEYS:
        out[key] = m.get(key) or None
    # Also nested, which is the shape the push builds from and the clearer one
    # to read. Same values as the flat keys above — one source, two views.
    out["content_targets"] = _content_targets(m)
    return out


def _content_targets(mapping: dict) -> dict:
    """{"transcript": "Field__c", ...} for the targets this mapping configured.

    ONLY configured targets appear: an unmapped target is absent rather than
    null, so the push can build its payload by iterating this dict without
    having to filter empties (and can never send an unconfigured field).
    """
    out = {}
    for name in CRM_CONTENT_TARGET_NAMES:
        field = (mapping or {}).get(f"{name}_field")
        if field:
            out[name] = field
    return out


def _empty_crm_record(mapping: dict) -> dict:
    """The resting state for a mapping with no identifier yet."""
    return {
        "object": mapping.get("object") or "",
        "label": mapping.get("object_label") or mapping.get("object") or "",
        "lookup_field": mapping.get("lookup_field") or "",
        "lookup_value": "",
        "record_id": "",
        "status": CRM_STATUS_NOT_LINKED,
        "source": "",
    }


def _public_crm_record(stored: dict, mapping: dict) -> dict:
    """One stored crm_records entry -> the client-facing view.

    Normalizes shape drift so the UI only ever sees the current contract:
      * `lookup_value` is the identifier; the extractor's older `value` key is
        still read, so a meeting written before this change still displays.
      * `status` is derived when absent — an entry with a record_id but no
        status predates status tracking and is at least record_found.
      * mapping-derived labels are refreshed on read, so renaming an object in
        Salesforce updates every meeting without a data migration.
    """
    stored = stored if isinstance(stored, dict) else {}
    value = str(stored.get("lookup_value") or stored.get("value") or "")
    record_id = str(stored.get("record_id") or "")
    status = str(stored.get("status") or "")
    if status not in CRM_STATUSES:
        # Derive from what IS known rather than guessing a terminal state.
        status = (CRM_STATUS_RECORD_FOUND if record_id
                  else CRM_STATUS_LOOKUP_PENDING if value
                  else CRM_STATUS_NOT_LINKED)
    return {
        "object": stored.get("object") or mapping.get("object") or "",
        "label": mapping.get("object_label") or stored.get("label") or "",
        "lookup_field": (stored.get("lookup_field")
                         or mapping.get("lookup_field") or ""),
        "lookup_value": value,
        "record_id": record_id,
        "record_label": str(stored.get("record_label") or ""),
        "status": status,
        "source": str(stored.get("source") or ""),
        # Extraction provenance — absent for manual entry. The UI shows the
        # evidence quote so a wrong AI extraction is catchable before a push.
        "confidence": str(stored.get("confidence") or ""),
        "evidence": str(stored.get("evidence") or ""),
        # What the model extracted before spoken digits were normalized ("SV one
        # zero zero four" behind the "SV1004" we looked up). Surfaced so the
        # evidence quote and the identifier shown can be reconciled by eye when
        # a lookup misses; empty when normalization changed nothing.
        "lookup_value_raw": (raw_value if (raw_value := str(
            stored.get("value_raw") or "")) != value else ""),
        "candidates": [c for c in (stored.get("candidates") or [])
                       if isinstance(c, dict)],
        "error": str(stored.get("error") or ""),
        "synced_at": str(stored.get("synced_at") or ""),
        "updated_at": str(stored.get("updated_at") or ""),
    }


def _public_crm_config(conn: dict) -> dict:
    """The client-facing configuration.

    `enabled` + `mappings` is the whole contract the UI renders from: it shows
    one input per mapping and nothing at all when the list is empty. No object
    name appears in the app's code as a result.
    """
    cfg = (conn or {}).get("config") or {}
    mappings = _mappings_from_config(cfg)
    return {
        "enabled": bool(conn),
        "mappings": [_public_mapping(m) for m in mappings],
        "configured": bool(mappings),
        "updated_at": cfg.get("updated_at") or None,
    }


def salesforce_get_config(event):
    """GET /crm/salesforce/config (JWT) -> {config:{enabled, mappings:[...]}}."""
    user_id = _require_auth(event)
    conn = _get_salesforce_connection(user_id)
    if not conn:
        raise ApiError(400, "Salesforce is not connected")
    return _resp(200, {"config": _public_crm_config(conn)})


def _validate_mapping(sf_call, raw: dict) -> dict:
    """One requested mapping -> the validated, storable mapping.

    Every name is re-checked against a live Describe. The app only ever sends
    names it got FROM Describe, so this is not about distrusting the client —
    it is about the org changing underneath a config that was valid when it
    was saved (a field deleted, a permission revoked). Failing here with a
    clear message beats failing later mid-push.

    `sf_call` is a _sf_call/_sf_call_org-SHAPED function — `fn -> fn(url, tok)`
    against whichever Salesforce connection (Personal or one workspace's) owns
    this configuration. Taking the caller rather than a user_id is what lets
    Organisation config (Phase 2D.2) reuse this without a second copy of every
    validation rule below; only the ONE line that resolves credentials differs
    between scopes, and it lives entirely in the caller-supplied function.
    """
    object_name = str(raw.get("object") or "").strip()
    lookup_field = str(raw.get("lookup_field") or "").strip()
    if not object_name:
        raise ApiError(400, "each mapping needs an object")
    if not lookup_field:
        raise ApiError(400, f"{object_name}: lookup_field required")

    described = sf_call(lambda url, tok:
                        _salesforce.describe_object(url, tok, object_name))
    by_name = {f.get("name"): f for f in (described.get("fields") or [])}
    object_label = described.get("label") or object_name

    lookup = by_name.get(lookup_field)
    if not lookup:
        raise ApiError(400, f"{lookup_field} does not exist on {object_name}")
    if not lookup.get("filterable"):
        raise ApiError(400, f"{lookup_field} cannot be searched on — pick a "
                            f"filterable field")

    # The label the UI puts on the input. Defaults to the org's own words for
    # the field, so "Email" on Lead reads "Lead Email" without anything in our
    # code knowing what a Lead is.
    lookup_label = lookup.get("label") or lookup_field
    label = str(raw.get("label") or "").strip() or f"{object_label} {lookup_label}"

    mapping = {
        "object": object_name,
        "object_label": object_label,
        "lookup_field": lookup_field,
        "lookup_field_label": lookup_label,
        "label": label[:100],
    }

    # The four content targets are OPTIONAL: whatever is left unmapped simply
    # isn't pushed. A user whose org has nowhere to put Highlights should
    # still be able to sync a transcript.
    seen = {}
    for key, target_label, _needs_long in CRM_DATA_TARGETS:
        name = str(raw.get(key) or "").strip()
        if not name:
            mapping[key] = ""
            continue
        f = by_name.get(name)
        if not f:
            raise ApiError(400, f"{name} does not exist on {object_name}")
        if not f.get("updateable") or f.get("calculated"):
            raise ApiError(400, f"{object_label} {target_label}: {name} is "
                                f"read-only in Salesforce")
        if (f.get("type") or "") not in SF_LONG_TEXT_TYPES:
            raise ApiError(400, f"{object_label} {target_label}: {name} is not "
                                f"a long text field — it would truncate the content")
        if name in seen:
            raise ApiError(400, f"{name} is already used for {seen[name]} — pick "
                                f"a different field for {target_label}")
        seen[name] = target_label
        mapping[key] = name
    return mapping


def salesforce_put_config(event):
    """PUT /crm/salesforce/config (JWT) -> {config}.

    Body is the generic shape:
        {"mappings": [{"object", "lookup_field", "label"?, <data targets>?}, ...]}

    An empty list is a legitimate configuration meaning "connected, but don't
    show me any record fields" — that is exactly what the UI renders nothing
    for, so it must be storable rather than rejected.

    The legacy single-object body ({"object", "site_visit_number_field"}) is
    still accepted and normalized into one mapping, so an older app build
    keeps working against this endpoint without a rebuild.
    """
    user_id = _require_auth(event)
    conn = _get_salesforce_connection(user_id)
    if not conn:
        raise ApiError(400, "Salesforce is not connected")

    data = _body(event)
    requested = data.get("mappings")
    if requested is None:
        # Legacy body shape — one object at the top level.
        if data.get("object"):
            legacy = dict(data)
            legacy["lookup_field"] = (data.get("lookup_field")
                                      or data.get(LEGACY_SITE_VISIT_KEY) or "")
            requested = [legacy]
        else:
            raise ApiError(400, "mappings required (a list, possibly empty)")
    if not isinstance(requested, list):
        raise ApiError(400, "mappings must be a list")
    if len(requested) > CRM_MAX_MAPPINGS:
        raise ApiError(400, f"at most {CRM_MAX_MAPPINGS} mappings")
    if any(not isinstance(m, dict) for m in requested):
        raise ApiError(400, "each mapping must be an object")

    mappings, objects_seen = [], set()
    sf_call = lambda fn: _sf_call(user_id, fn)  # noqa: E731
    for raw in requested:
        mapping = _validate_mapping(sf_call, raw)
        # One mapping per object: two lookup fields for the same object would
        # make "which record is this meeting about" ambiguous.
        if mapping["object"] in objects_seen:
            raise ApiError(400, f"{mapping['object']} is mapped twice — one "
                                f"lookup field per object")
        objects_seen.add(mapping["object"])
        mappings.append(mapping)

    cfg = {"mappings": mappings, "updated_at": _now_iso()}
    _crm_connections.update_item(
        Key={"user_id": user_id, "provider": CRM_PROVIDER_SALESFORCE},
        UpdateExpression="SET config = :c, updated_at = :now",
        ConditionExpression="attribute_exists(user_id)",
        ExpressionAttributeValues={":c": cfg, ":now": _now_iso()},
    )
    print(f"[salesforce] config saved for {user_id}: "
          + (", ".join(f"{m['object']}.{m['lookup_field']}" for m in mappings)
             or "no mappings"))
    updated = _get_salesforce_connection(user_id)
    return _resp(200, {"config": _public_crm_config(updated)})


# ---------------------------------------------------------------------------
# CRM record lookup — identifier -> Salesforce record Id.
#
# Generic by construction: the object and the field to match on both come from
# the user's saved mapping, so this one route resolves a Site Visit number, a
# Lead email or an Opportunity number without knowing which is which.
# ---------------------------------------------------------------------------
def _soql_quote(value: str) -> str:
    """Escape a string for a SOQL string literal.

    The identifier is user-supplied, so it cannot be interpolated raw: a value
    containing a quote would otherwise change the query's meaning. Salesforce
    uses backslash escaping inside single-quoted literals.
    """
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _find_mapping(cfg: dict, object_name: str) -> dict:
    for m in _mappings_from_config(cfg):
        if m.get("object") == object_name:
            return m
    raise ApiError(400, f"{object_name} is not configured — set it up in "
                        f"Salesforce mapping first")


def _crm_scope_for_meeting(item: dict):
    """The (sf_call, crm_config) pair a meeting's OWN workspace dictates.

    THE ONE PLACE that decides "does this meeting's CRM activity run against
    Personal Salesforce or an Organisation's?" — every function that talks to
    Salesforce on behalf of a MEETING (lookup, resolve, sync, and Phase 2D.3's
    identity resolution) must go through this rather than deciding for
    itself, which is what let crm_sync_record/salesforce_lookup_record use
    _sf_call(user_id, ...) UNCONDITIONALLY for months — silently correct for
    every personal meeting and silently WRONG for every organisation one
    (an org meeting's CRM push would use the CALLER's personal Salesforce
    connection, never the organisation's).

    Resolved from the MEETING's stored workspace_id, never from the caller's
    JWT or a client-supplied header — a member's personal Salesforce
    connection must never be reachable through a route that operates on an
    organisation's meeting, and vice versa. `_row_workspace_id` already
    encodes "no workspace_id -> personal", so this function needs no
    parallel fallback logic of its own.

    Returns (sf_call, config) where sf_call is `fn -> fn(url, tok)`, exactly
    the shape _validate_mapping already takes — Phase 2D.1/2D.2's seam,
    reused rather than re-invented.
    """
    workspace_id = _row_workspace_id(item)
    if workspace_schema.is_organisation_workspace_id(workspace_id):
        sf_call = lambda fn: _sf_call_org(workspace_id, fn)  # noqa: E731
        conn = _get_org_salesforce_connection(workspace_id)
        return sf_call, (conn or {}).get("config") or {}
    user_id = item.get("user_id") or ""
    sf_call = lambda fn: _sf_call(user_id, fn)  # noqa: E731
    return sf_call, _user_crm_config(user_id)


def _sf_name_field(sf_call, object_name: str) -> str:
    """The object's own "name" field, or "" when it has none.

    Gives the UI something human to confirm against ("Rahul Sharma") beside the
    raw identifier. Not every object has one, so callers must tolerate "".

    `sf_call` is a _sf_call/_sf_call_org-SHAPED function (see _validate_mapping
    for the same seam) — this function itself never decides Personal vs
    Organisation scope.
    """
    described = sf_call(lambda url, tok:
                        _salesforce.describe_object(url, tok, object_name))
    return next((f.get("name") for f in (described.get("fields") or [])
                 if f.get("nameField")), "") or ""


def _resolve_crm_record(sf_call, mapping: dict, value: str) -> dict:
    """Identifier -> {status, record_id?, record_label?, candidates?}.

    The generic resolution step: SOQL against the mapping's own object and
    lookup field, so the same code resolves a site visit number, a lead email or
    a custom object's reference. Three outcomes, all of them ORDINARY:

      not_found     nothing matched — the user fixes the identifier
      record_found  exactly one match — awaits confirmation before any push
      ambiguous     several matched — the user picks; we never choose for them

    Ambiguity is returned as candidates rather than an error precisely because
    the caller CAN resolve it (by asking); silently taking the first match is
    the failure this whole flow exists to prevent.

    `sf_call` — see _sf_name_field above; this function is likewise
    scope-agnostic by construction.
    """
    object_name = mapping["object"]
    lookup_field = mapping["lookup_field"]
    name_field = _sf_name_field(sf_call, object_name)

    select = ["Id", lookup_field]
    if name_field and name_field != lookup_field:
        select.append(name_field)
    # LIMIT is CRM_AMBIGUOUS_LIMIT + 1 so "more than we can show" is
    # distinguishable from "exactly this many".
    soql = (f"SELECT {', '.join(select)} FROM {object_name} "
            f"WHERE {lookup_field} = '{_soql_quote(value)}' "
            f"LIMIT {CRM_AMBIGUOUS_LIMIT + 1}")

    result = sf_call(lambda url, tok: _salesforce.query(url, tok, soql))
    records = [r for r in (result.get("records") or []) if isinstance(r, dict)]

    if not records:
        print(f"[salesforce] lookup {object_name}.{lookup_field}={value!r}: no match")
        return {"status": CRM_LOOKUP_NOT_FOUND}

    def _display(rec: dict) -> str:
        return str((rec.get(name_field) if name_field else "")
                   or rec.get(lookup_field) or value)

    if len(records) == 1:
        record = records[0]
        return {
            "status": CRM_STATUS_RECORD_FOUND,
            "record_id": str(record.get("Id") or ""),
            "record_label": _display(record),
        }

    print(f"[salesforce] lookup {object_name}.{lookup_field}={value!r}: "
          f"{len(records)} matches — ambiguous")
    return {
        "status": CRM_STATUS_AMBIGUOUS,
        "candidates": [{"record_id": str(r.get("Id") or ""),
                        "display_name": _display(r)}
                       for r in records[:CRM_AMBIGUOUS_LIMIT]],
        "truncated": len(records) > CRM_AMBIGUOUS_LIMIT,
    }


def salesforce_lookup_record(event):
    """POST /crm/salesforce/lookup {object, value|lookup_value} (JWT)

    -> {status: "found"|"not_found"|"ambiguous", ...}

    Resolves an identifier to a Salesforce record Id using the configured
    object + lookup field. Stateless: it only reports what Salesforce says.
    Persisting the association is a separate, explicit step (see the meeting
    PATCH), so a lookup can never silently relink a meeting.
    """
    user_id = _require_auth(event)
    conn = _get_salesforce_connection(user_id)
    if not conn:
        raise ApiError(400, "Salesforce is not connected")

    data = _body(event)
    object_name = str(data.get("object") or "").strip()
    # `lookup_value` is the current name; `value` is accepted for the earlier
    # shape so a deployed client keeps working.
    value = str(data.get("lookup_value") or data.get("value") or "").strip()
    if not object_name:
        raise ApiError(400, "object required")
    if not value:
        raise ApiError(400, "lookup_value required")
    if len(value) > CRM_IDENTIFIER_MAX:
        raise ApiError(400, f"lookup_value must be at most "
                            f"{CRM_IDENTIFIER_MAX} characters")

    mapping = _find_mapping((conn or {}).get("config") or {}, object_name)
    outcome = _resolve_crm_record(lambda fn: _sf_call(user_id, fn), mapping, value)

    base = {
        "object": object_name,
        "label": mapping["object_label"],
        "lookup_field": mapping["lookup_field"],
        "lookup_value": value,
    }
    if outcome["status"] == CRM_LOOKUP_NOT_FOUND:
        return _resp(200, {**base, "status": "not_found"})
    if outcome["status"] == CRM_STATUS_AMBIGUOUS:
        return _resp(200, {**base, "status": "ambiguous",
                           "records": outcome["candidates"],
                           "truncated": outcome.get("truncated", False)})
    return _resp(200, {**base, "status": "found",
                       "record_id": outcome["record_id"],
                       "record_label": outcome["record_label"]})


# ---------------------------------------------------------------------------
# CRM push — write the meeting's content onto the linked Salesforce record.
#
# ONE generic update for every object. What gets written is entirely the
# mapping's configured content targets: only configured targets are sent, and an
# unconfigured one is omitted rather than blanked (writing "" would erase
# whatever the customer's own process put there).
#
# The push NEVER runs off the back of a lookup. It requires a record the user
# has confirmed, because notes on the wrong record are worse than no notes.
# ---------------------------------------------------------------------------
def _render_highlights(item: dict) -> str:
    """The meeting's highlights as plain text for a Salesforce long-text field.

    Reads, in order of preference:
      1. the DYNAMIC OVERVIEW — the current primary output, flattened to text;
      2. the flat `highlights` list, for a row analysed before the overview;
      3. the structured `meeting_highlights`, for a row older still.

    Three sources rather than one because this is a PUSH: the alternative to
    reading a legacy shape is pushing "" into a customer's Salesforce field,
    and _crm_push_payload skips empty content precisely so that nothing
    overwrites what is already there. Falling back costs nothing and keeps the
    back catalogue pushable.
    """
    overview = item.get("overview")
    if isinstance(overview, dict):
        text = ai_schema.overview_text(overview)
        if text:
            return text

    flat = [str(h).strip() for h in (item.get("highlights") or []) if str(h).strip()]
    if flat:
        return "\n".join(f"- {h}" for h in flat)

    structured = item.get("meeting_highlights") or {}
    if not isinstance(structured, dict):
        return ""
    lines = []
    # Iterates a LITERAL section list, not ai_schema.HIGHLIGHT_SECTIONS: this
    # is a legacy reader, and it must keep rendering the two sections
    # (important_numbers, risks) that left the live schema but still sit on
    # rows written before they did.
    for section in ("decisions", "action_items", "deadlines",
                    "important_numbers", "open_questions", "risks"):
        rows = structured.get(section) or []
        if not rows:
            continue
        lines.append(section.replace("_", " ").title())
        for row in rows:
            if isinstance(row, dict):
                text = " — ".join(str(v).strip() for v in row.values() if str(v).strip())
            else:
                text = str(row).strip()
            if text:
                lines.append(f"- {text}")
        lines.append("")
    return "\n".join(lines).strip()


def _render_action_items(item: dict) -> str:
    """Tasks/action items as plain text, from whichever shape exists.

    `tasks` is the CRUD-managed map the app owns; `ai_tasks` is the pipeline's
    raw extraction. Preferring the managed map means a user's edits (owner, due
    date, status) are what reaches Salesforce.

    The analysis's older `action_items` field was removed from the schema, so it
    is no longer read here — an unreprocessed legacy row pushes no action items
    rather than pushing a stale extraction, and _crm_push_payload already skips
    empty content so nothing overwrites a value in Salesforce.
    """
    rows = []
    tasks = item.get("tasks")
    if isinstance(tasks, dict) and tasks:
        rows = list(tasks.values())
    elif isinstance(item.get("ai_tasks"), list):
        rows = item["ai_tasks"]

    lines = []
    for row in rows:
        if not isinstance(row, dict):
            text = str(row).strip()
            if text:
                lines.append(f"- {text}")
            continue
        text = str(row.get("task") or row.get("title") or "").strip()
        if not text:
            continue
        owner = str(row.get("assignee") or row.get("owner") or "").strip()
        if isinstance(row.get("assignee"), dict):
            owner = str(row["assignee"].get("name") or "").strip()
        due = str(row.get("due_date") or row.get("due") or "").strip()
        status = str(row.get("status") or "").strip()
        extra = " · ".join(p for p in (owner, due, status) if p)
        lines.append(f"- {text}" + (f" ({extra})" if extra else ""))
    return "\n".join(lines)


def _crm_push_payload(mapping: dict, item: dict) -> dict:
    """{Salesforce field: content} for exactly the configured content targets.

    Iterates the mapping's targets, so an object with only Summary configured
    gets a one-field payload and nothing else is touched. Empty content is
    skipped too: pushing "" over an existing value destroys data the customer
    may have written themselves.
    """
    content = {
        "transcript": str(item.get("transcript") or ""),
        "summary": str(item.get("summary") or ""),
        "highlights": _render_highlights(item),
        "action_items": _render_action_items(item),
    }
    payload = {}
    for name, field in _content_targets(mapping).items():
        value = content.get(name) or ""
        if value.strip():
            payload[field] = value
    return payload


def _write_crm_record(key: str, object_name: str, entry: dict) -> None:
    """Persist one crm_records entry. Nested-map update so a concurrent write to
    a DIFFERENT object's entry cannot be clobbered.

    Same two-step shape as _store_document, for the same two DynamoDB reasons
    documented there:

      * `SET #cr = if_not_exists(#cr, :empty), #cr.#obj = :entry` is REJECTED at
        parse time — "Two document paths overlap with each other" — because it
        writes both a map and a key inside that map in one expression. It fails
        on EVERY call regardless of the item's contents, which is what made
        "Sync to Salesforce" return a blanket `internal error`.
      * A nested SET whose parent map is absent raises ValidationException
        ("...invalid for update"); DynamoDB does not auto-create the parent.

    So: attempt the nested SET (the steady-state path, one round trip), and only
    if the map is missing create it — guarded by attribute_not_exists so a
    racing writer's map is never blanked — then retry.
    """
    names = {"#cr": "crm_records", "#obj": object_name}

    def _set_nested():
        _recordings.update_item(
            Key={"audio_s3_key": key},
            UpdateExpression="SET #cr.#obj = :entry, updated_at = :now",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues={":entry": entry, ":now": _now_iso()},
        )

    try:
        _set_nested()
        return
    except ClientError as err:
        # Match the message, not just the code: ValidationException is generic,
        # and a real expression bug should surface rather than be retried.
        if err.response.get("Error", {}).get("Code") != "ValidationException" \
                or "invalid for update" not in str(err):
            raise

    # The row has no crm_records map yet (the common case: nothing has ever been
    # linked). Create it, tolerating the race where a concurrent write won.
    try:
        _recordings.update_item(
            Key={"audio_s3_key": key},
            UpdateExpression="SET #cr = :empty",
            ConditionExpression="attribute_not_exists(#cr)",
            ExpressionAttributeNames={"#cr": "crm_records"},
            ExpressionAttributeValues={":empty": {}},
        )
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") \
                != "ConditionalCheckFailedException":
            raise
        # Someone else created it between our two calls — exactly what we want.
    _set_nested()


def crm_sync_record(event):
    """POST /crm/salesforce/sync/{key+} {object} (JWT) -> {crm_record}

    Thin HTTP wrapper — see _sync_one_crm_record for the actual logic,
    extracted in Phase 2D.4 so the CRM worker (an internal caller with no
    JWT/event to authenticate) can invoke the exact same code as this route
    without a forged HTTP event or a second, weaker auth path.
    """
    user_id, key, item = _owned_recording(event)
    data = _body(event)
    object_name = str(data.get("object") or "").strip()
    if not object_name:
        raise ApiError(400, "object required")
    result = _sync_one_crm_record(key, item, object_name)
    status_code = result.pop("status_code")
    return _resp(status_code, result)


def _sync_one_crm_record(key: str, item: dict, object_name: str) -> dict:
    """Pushes the meeting's configured content onto the confirmed Salesforce
    record. Requires a stored record_id AND a confirmed status: this is the
    only path that writes syncing/synced/failed.

    Uses the STORED record_id — no second SOQL lookup. The identifier is the
    user's visible reference; the record Id is the association, and re-resolving
    it on every sync would risk drifting onto a different record if the org's
    data changed.

    SCOPE (Phase 2D.3 fix): which Salesforce connection this pushes to is
    decided by _crm_scope_for_meeting from the MEETING's own workspace_id —
    an organisation-owned meeting pushes through that organisation's
    OrgCrmConnections, never through the caller's Personal CrmConnections.
    Before that fix, every push here used _sf_call(user_id, ...)
    unconditionally, which was silently correct only for personal meetings.

    Takes `item` directly rather than resolving it from an event/JWT — the
    caller (the HTTP route above, or Phase 2D.4's worker) is responsible for
    its own authorization; this function itself performs no auth check,
    exactly like every other _push_*/_execute_* helper in this section.

    A Salesforce failure marks the entry failed and returns the org's message.
    The meeting's own transcript/summary are never touched by any of this.
    """
    sf_call, crm_config = _crm_scope_for_meeting(item)
    mapping = _find_mapping(crm_config, object_name)
    stored = (item.get("crm_records") or {}).get(object_name)
    if not isinstance(stored, dict) or not stored:
        raise ApiError(400, f"no {mapping['object_label']} is linked to this "
                            f"meeting yet")

    record = _public_crm_record(stored, mapping)
    record_id = record["record_id"]
    if not record_id:
        raise ApiError(400, f"{mapping['object_label']}: look the record up "
                            f"before syncing")
    if record["status"] not in CRM_PUSHABLE_STATUSES:
        # record_found (not yet confirmed) lands here, which is the point: the
        # user must approve the record before anything is written to it.
        raise ApiError(409, f"confirm the {mapping['object_label']} record "
                            f"before syncing")

    payload = _crm_push_payload(mapping, item)
    if not payload:
        raise ApiError(400, f"{mapping['object_label']} has no content fields "
                            f"configured — set them in Salesforce mapping")

    base = {**stored, "status": CRM_STATUS_SYNCING, "error": "",
            "updated_at": _now_iso()}
    _write_crm_record(key, object_name, base)

    try:
        sf_call(lambda url, tok: _salesforce.update_record(
            url, tok, object_name, record_id, payload))
    except ApiError as e:
        failed = {**base, "status": CRM_STATUS_FAILED,
                  "error": e.message[:500], "updated_at": _now_iso()}
        # A 404 means the record is gone: drop the stale record_id so the next
        # attempt re-resolves instead of retrying a write that cannot succeed.
        if e.status == 404:
            failed["record_id"] = ""
            failed["status"] = CRM_STATUS_LOOKUP_PENDING
        _write_crm_record(key, object_name, failed)
        print(f"[salesforce] sync {object_name}/{record_id} failed: {e.message}")
        # Answers directly rather than re-raising (the caller needs the
        # persisted crm_record alongside the error), so `code` travels in the
        # returned dict itself — otherwise a dead Salesforce credential would
        # reach the app as a bare 409 and lose its "reconnect Salesforce"
        # identity. `status_code` here is an HTTP-status-SHAPED field on a
        # plain dict, not an actual response — crm_sync_record (the HTTP
        # route) is what turns it into one; the worker reads it directly.
        result = {"error": e.message,
                 "crm_record": _public_crm_record(failed, mapping),
                 "status_code": e.status}
        code = getattr(e, "code", "")
        if code:
            result["code"] = code
        return result

    synced = {**base, "status": CRM_STATUS_SYNCED, "error": "",
              "synced_at": _now_iso(), "updated_at": _now_iso(),
              "synced_fields": sorted(payload.keys())}
    _write_crm_record(key, object_name, synced)
    print(f"[salesforce] synced {object_name}/{record_id}: "
          f"{', '.join(sorted(payload))}")
    return {"crm_record": _public_crm_record(synced, mapping),
           "synced_fields": sorted(payload.keys()), "status_code": 200}


# ---------------------------------------------------------------------------
# ORGANISATION MEETING PUSH (Phase 2D.3) — extends the push architecture
# above rather than replacing it. Two distinct pieces of Salesforce state:
#
#   1. The mapped OBJECT's content fields — reuses crm_sync_record UNCHANGED
#      (called as a function here, exactly as the route already does; no
#      second copy of that state machine).
#   2. Salesforce TASKS from MinuteX action items — genuinely new (spec
#      section 13), because a Task is a CREATE, not a field update onto an
#      already-confirmed record, and needs its own idempotency story.
#
# Nothing here runs for a Personal meeting: _require_org_meeting refuses one
# with 400 before any Salesforce call is made.
# ---------------------------------------------------------------------------

def _crm_tasks_synced_map(item: dict) -> dict:
    """{minutex_task_id: {sf_task_id, synced_at}} already pushed for this
    meeting — the idempotency record for push_org_meeting_crm's Task half."""
    m = item.get("crm_tasks_synced")
    return m if isinstance(m, dict) else {}


def _write_crm_task_synced(key: str, task_id: str, entry: dict) -> None:
    """As _write_crm_record, but for the crm_tasks_synced nested map — same
    two-step SET (create-the-map-then-retry) for the same DynamoDB
    "overlapping document paths" reason documented on _write_crm_record."""
    names = {"#ts": "crm_tasks_synced", "#tid": task_id}

    def _set_nested():
        _recordings.update_item(
            Key={"audio_s3_key": key},
            UpdateExpression="SET #ts.#tid = :entry, updated_at = :now",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues={":entry": entry, ":now": _now_iso()},
        )

    try:
        _set_nested()
        return
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") != "ValidationException" \
                or "invalid for update" not in str(err):
            raise
    try:
        _recordings.update_item(
            Key={"audio_s3_key": key},
            UpdateExpression="SET #ts = :empty",
            ConditionExpression="attribute_not_exists(#ts)",
            ExpressionAttributeNames={"#ts": "crm_tasks_synced"},
            ExpressionAttributeValues={":empty": {}},
        )
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") \
                != "ConditionalCheckFailedException":
            raise
    _set_nested()


def _task_assignee_sf_identity(task_row: dict, workspace_id: str) -> dict:
    """The Salesforce identity (User or Contact) a MinuteX task's assignee
    resolves to, or {} if it cannot be resolved yet.

    Task assignment and meeting PARTICIPATION are different concepts (spec
    section 13) — this deliberately does NOT look at identity_role/
    MeetingParticipants at all. It reads the task's OWN assignee_contact_id
    and asks the same two questions _speaker_crm_state asks for a speaker:
    is this an org member (-> Salesforce User) or an external contact
    (-> Salesforce Contact)? Never falls back to "assign to whoever is
    convenient" — an unresolvable assignee means the Task is created with
    no OwnerId/WhoId rather than guessed.
    """
    contact_id = str(task_row.get("assignee_contact_id") or "")
    if not contact_id:
        return {}
    contact = _contacts.get_item(Key={"contact_id": contact_id}).get("Item")
    if not contact:
        return {}
    member_user_id = str(contact.get("minutex_user_id") or "")
    if member_user_id:
        link = _get_member_sf_user_link(workspace_id, member_user_id)
        if link.get("sf_user_id"):
            return {"kind": "user", "sf_id": link["sf_user_id"]}
        return {}
    sf_contact_id = str(contact.get("crm_external_id") or "")
    if sf_contact_id:
        return {"kind": "contact", "sf_id": sf_contact_id}
    return {}


def _push_action_items_as_tasks(sf_call, key: str, item: dict,
                                workspace_id: str) -> dict:
    """Create a Salesforce Task for each eligible, not-yet-synced MinuteX
    task sourced from this meeting. Returns {created, skipped, failed}
    counts plus per-task detail for the caller to report.

    IDEMPOTENCY (spec section 15): a MinuteX task_id already present in
    crm_tasks_synced is skipped outright — no second Salesforce Task is
    created on a retried push, which is what makes this function safe to
    call again after a partial failure.
    """
    already = _crm_tasks_synced_map(item)
    rows = [r for r in _tasks_for_recording(key)
           if str(r.get("status") or "") != TASK_STATUS_CANCELLED]

    created, skipped, failed = [], [], []
    for row in rows:
        task_id = str(row.get("task_id") or "")
        if not task_id:
            continue
        if task_id in already:
            skipped.append({"task_id": task_id, "reason": "already_synced"})
            continue
        title = str(row.get("title") or "").strip()
        if not title:
            skipped.append({"task_id": task_id, "reason": "no_title"})
            continue

        identity = _task_assignee_sf_identity(row, workspace_id)
        fields = {"Subject": title[:255]}
        description = str(row.get("description") or "").strip()
        if description:
            fields["Description"] = description[:32000]
        due = str(row.get("due_date_normalized") or row.get("due_date") or "")
        if due:
            fields["ActivityDate"] = due
        if identity.get("kind") == "user":
            fields["OwnerId"] = identity["sf_id"]
        elif identity.get("kind") == "contact":
            fields["WhoId"] = identity["sf_id"]

        try:
            sf_task_id = sf_call(lambda url, tok: _salesforce.create_record(
                url, tok, "Task", fields))
        except ApiError as e:
            # status/code travel WITH the message (Phase 2D.4) so a job-level
            # rollup can classify this failure correctly rather than
            # guessing — see execute_crm_sync_job's partial-failure rollup.
            failed.append({"task_id": task_id, "error": e.message[:500],
                          "status": e.status, "code": getattr(e, "code", "")})
            continue

        entry = {"sf_task_id": sf_task_id, "synced_at": _now_iso(),
                 "assignee_resolved": bool(identity)}
        _write_crm_task_synced(key, task_id, entry)
        created.append({"task_id": task_id, "sf_task_id": sf_task_id,
                        "assignee_resolved": bool(identity)})

    return {"created": created, "skipped": skipped, "failed": failed}


# ---------------------------------------------------------------------------
# SALESFORCE EVENT (Phase 2D.3 completion). The meeting itself, pushed as one
# Salesforce Activity Event — the third and final Salesforce object this
# phase creates (Task above; the mapped record's fields via crm_sync_record
# use update_record, never create_record).
#
# FIELDS ACTUALLY SUPPORTED BY THE STANDARD EVENT OBJECT (verified against
# Salesforce's own object reference, not assumed):
#   OwnerId   — a Salesforce USER. Valid, and exactly what "meeting owner"
#               means here — the resolved SM, never the org's connection
#               identity (spec section 8's whole point).
#   WhoId     — polymorphic, accepts Contact OR Lead. One value: Event does
#               not carry a multi-Contact attendee list on the core object
#               without EventRelation records (a separate related-object
#               write this codebase does not make — see the note below).
#   WhatId    — polymorphic, accepts Account (among other objects). Valid
#               ALONGSIDE WhoId on Event — unlike some Activity
#               configurations, Event supports "Name" (Who) and "Related To"
#               (What) at the same time.
#   Subject, Description — ordinary text fields.
#   StartDateTime, EndDateTime — the two fields that actually schedule the
#               Event; Salesforce also accepts DurationInMinutes instead of
#               EndDateTime, but this codebase always has both ends of the
#               interval (recorded_at + duration), so EndDateTime is sent
#               explicitly rather than asking Salesforce to derive it.
#
# NOT IMPLEMENTED, DELIBERATELY: EventRelation invitee records for multiple
# participants. The org's Event page layout and sharing model determine
# whether/how those are even usable, and inventing that write here would be
# exactly the "unsupported relationship" this phase was told not to invent.
# The single resolved SM (Owner) and single resolved primary client (Who) are
# the relationships _meeting_crm_identity_state already resolves as
# singular, and are the ones this function uses.
# ---------------------------------------------------------------------------

def _meeting_start_end_iso(item: dict) -> tuple:
    """(start_iso, end_iso) for this meeting, or ("", "") when recorded_at is
    unusable. Salesforce DateTime fields require full ISO 8601 with an
    offset/Z — a bare date is not accepted, unlike anchor_date's use in
    spoken_dates (which only ever needs a calendar day).

    recorded_at is written as a Unix epoch STRING by the upload path, but
    older rows carry an ISO timestamp — the same dual format
    spoken_dates.anchor_date already has to handle, so both are accepted
    here rather than assuming one and silently mis-scheduling the Event for
    every row in the other shape.
    """
    raw = str(item.get("recorded_at") or "").strip()
    start = None
    if raw:
        if raw.isdigit():
            try:
                start = datetime.fromtimestamp(int(raw), tz=timezone.utc)
            except (ValueError, OSError, OverflowError):
                start = None
        else:
            try:
                start = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                start = None
    if start is None:
        return "", ""
    try:
        duration_seconds = float(item.get("duration") or 0)
    except (TypeError, ValueError):
        duration_seconds = 0
    # A meeting with no known duration still gets a schedulable Event — one
    # hour is a reasonable default block, and it is far better than refusing
    # to create the Event at all over a field the recording pipeline simply
    # never captured for this row.
    if duration_seconds <= 0:
        duration_seconds = 3600
    end = start + timedelta(seconds=duration_seconds)
    return (start.isoformat().replace("+00:00", "Z"),
            end.isoformat().replace("+00:00", "Z"))


def _crm_event_synced(item: dict) -> dict:
    """The Event already pushed for this meeting, or {} if none yet —
    idempotency record for _push_meeting_as_event, same design as
    crm_tasks_synced (singular here: one meeting produces at most one
    Event, never a map keyed by anything)."""
    m = item.get("crm_event_synced")
    return m if isinstance(m, dict) else {}


def _write_crm_event_synced(key: str, entry: dict) -> None:
    """Persist the Event idempotency record. A flat top-level attribute, not
    a nested map like crm_tasks_synced/crm_records — there is only ever ONE
    Event per meeting, so there is no sibling-key collision to guard against
    and no "create the parent map first" step is needed."""
    _recordings.update_item(
        Key={"audio_s3_key": key},
        UpdateExpression="SET crm_event_synced = :entry, updated_at = :now",
        ExpressionAttributeValues={":entry": entry, ":now": _now_iso()},
    )


def _push_meeting_as_event(sf_call, key: str, item: dict,
                           identity: dict) -> dict:
    """Create ONE Salesforce Event for this meeting, or report why not.

    Returns {"created": bool, "sf_event_id": str, "skipped_reason": str,
    "error": str, "code": str} — never raises, so a failure here cannot take
    down the object-mapping push or Task creation that already ran in the
    same request (push_org_meeting_crm reports this alongside them
    instead). `code` mirrors crm_sync_record's own contract (a stable
    machine-readable identity, e.g. "salesforce_reconnect_required", never
    just prose) so a dead Salesforce credential is distinguishable from an
    ordinary validation failure here exactly as it already is for the
    object-mapping push.

    IDEMPOTENT: an existing crm_event_synced entry short-circuits before any
    Salesforce call is made — a retried push (client retry, Lambda double
    invoke, a timeout whose write actually landed) can never create a
    second Event for the same meeting.
    """
    already = _crm_event_synced(item)
    if already.get("sf_event_id"):
        return {"created": False, "sf_event_id": already["sf_event_id"],
               "skipped_reason": "already_synced", "error": "", "code": ""}

    owner = identity.get("owner")
    if not owner or not owner.get("sf_user_id"):
        # No resolved SM — an Event with no Owner is not a meaningful
        # record of "who ran this meeting", so this is a skip, not a
        # best-effort create with OwnerId absent.
        return {"created": False, "sf_event_id": "",
               "skipped_reason": "no_resolved_owner", "error": "", "code": ""}

    start_iso, end_iso = _meeting_start_end_iso(item)
    if not start_iso or not end_iso:
        return {"created": False, "sf_event_id": "",
               "skipped_reason": "no_meeting_time", "error": "", "code": ""}

    fields = {
        "OwnerId": owner["sf_user_id"],
        "Subject": str(item.get("title") or "Meeting")[:255],
        "StartDateTime": start_iso,
        "EndDateTime": end_iso,
    }
    description = str(item.get("summary") or "").strip()
    if description:
        fields["Description"] = description[:32000]

    client = identity.get("primary_client")
    if client and client.get("sf_contact_id"):
        fields["WhoId"] = client["sf_contact_id"]
        # WhatId (Account) is populated ONLY when it is the ACCOUNT OF THIS
        # SAME CONTACT — never a MinuteX-side guess and never a different
        # client's Account left over from stale state. crm_account_id is
        # written exclusively by resolve_contact_crm_identity, at the same
        # moment as crm_external_id, so the two are always a matched pair.
        if client.get("sf_account_id"):
            fields["WhatId"] = client["sf_account_id"]

    try:
        sf_event_id = sf_call(lambda url, tok: _salesforce.create_record(
            url, tok, "Event", fields))
    except ApiError as e:
        # status travels alongside code/error (Phase 2D.4) — see the same
        # note on the Task failure path above.
        return {"created": False, "sf_event_id": "", "skipped_reason": "",
               "error": e.message[:500], "code": getattr(e, "code", ""),
               "status": e.status}

    entry = {"sf_event_id": sf_event_id, "synced_at": _now_iso(),
             "owner_sf_user_id": owner["sf_user_id"],
             "contact_sf_id": (client or {}).get("sf_contact_id", ""),
             "account_sf_id": fields.get("WhatId", "")}
    _write_crm_event_synced(key, entry)
    return {"created": True, "sf_event_id": sf_event_id,
           "skipped_reason": "", "error": "", "code": ""}


def _execute_org_meeting_crm_push(user_id: str, key: str, item: dict,
                                  workspace_id: str,
                                  requested_objects=None) -> dict:
    """Perform the actual Salesforce work for one organisation meeting's CRM
    push — the exact logic push_org_meeting_crm ran INLINE in Phase 2D.3,
    extracted unchanged so Phase 2D.4's worker (and the still-synchronous
    identity/readiness checks) share ONE implementation. Returns a plain
    dict, never an HTTP response — the caller (the job worker, or a test)
    decides how to report it.

    Raises ApiError for a condition that blocks the ENTIRE push (not
    configured, unresolved required identity) — the caller is expected to
    turn that into the job's terminal state via _classify_crm_error. Any
    Salesforce-level failure on an INDIVIDUAL operation (one object's sync,
    one Task, the Event) is instead captured in that operation's own
    push_errors/tasks/event result, exactly as in 2D.3 — only a whole-push
    precondition failure raises here.
    """
    identity = _meeting_crm_identity_state(key, item, workspace_id)

    conn = _get_org_salesforce_connection(workspace_id)
    if not conn:
        raise OrganisationSalesforceNotConnected()
    mappings = _mappings_from_config(conn.get("config") or {})
    if not mappings:
        raise ApiError(400, "no Salesforce mapping is configured for this "
                            "organisation — set it up in Salesforce mapping first")
    if identity["unresolved"]:
        names = ", ".join(s["speaker_id"] for s in identity["unresolved"])
        raise ApiError(409, "resolve every tagged speaker's Salesforce "
                            f"identity before pushing (unresolved: {names})")

    if requested_objects is not None and not isinstance(requested_objects, list):
        raise ApiError(400, "objects must be a list")

    sf_call, _cfg = _crm_scope_for_meeting(item)
    pushed, push_errors = [], []
    for mapping in mappings:
        object_name = mapping["object"]
        if requested_objects is not None and object_name not in requested_objects:
            continue
        stored = (item.get("crm_records") or {}).get(object_name)
        if not isinstance(stored, dict) or not stored:
            continue  # not linked to a record at all — nothing to push here
        record = _public_crm_record(stored, mapping)
        if record["status"] not in CRM_PUSHABLE_STATUSES or not record["record_id"]:
            continue  # not yet confirmed — crm_sync_record's own gate applies

        # Delegates to the EXACT SAME function the standalone route calls —
        # _sync_one_crm_record, given the item this function already has in
        # hand — so its entire state machine (syncing -> synced/failed,
        # stale-record recovery) runs unchanged rather than being
        # re-implemented here. No forged HTTP event/JWT needed (Phase 2D.4):
        # this function itself takes no event, so neither does its callee.
        result = _sync_one_crm_record(key, item, object_name)
        status_code = result.pop("status_code")
        if status_code == 200:
            pushed.append({"object": object_name, "crm_record": result["crm_record"]})
        else:
            push_errors.append({"object": object_name, "error": result.get("error", ""),
                                "code": result.get("code", ""), "status": status_code})

    task_result = _push_action_items_as_tasks(sf_call, key, item, workspace_id)
    event_result = _push_meeting_as_event(sf_call, key, item, identity)

    print(f"[org-crm-push] meeting={key} workspace={workspace_id} "
          f"requested_by={user_id} "
          f"objects_pushed={len(pushed)} object_errors={len(push_errors)} "
          f"tasks_created={len(task_result['created'])} "
          f"tasks_failed={len(task_result['failed'])} "
          f"event_created={event_result['created']}")

    return {
        "pushed": pushed,
        "push_errors": push_errors,
        "tasks": task_result,
        "event": event_result,
        "identity": identity,
    }


def push_org_meeting_crm(event):
    """POST /recordings/ai/crm-push/{key+} {objects?: [str]} (JWT,
    organisation meetings only) -> {job_id, status} — 202 Accepted.

    PHASE 2D.4: this route no longer talks to Salesforce itself. It
    validates ownership, creates a durable CrmSyncJobs row, enqueues one SQS
    message naming it, and returns immediately — CRM synchronization must
    never block recording completion, transcription, AI processing or this
    very request (spec section 2). The actual Salesforce work
    (_execute_org_meeting_crm_push, unchanged from Phase 2D.3) now runs in
    crmSyncWorker, reading the SAME durable rows this route would have read
    synchronously — nothing about identity resolution moved.

    Readiness is NOT re-validated here beyond ownership/workspace — that
    would require the same Salesforce-adjacent reads (connection, config)
    this route is trying to avoid doing synchronously. The worker validates
    readiness itself, exactly as _execute_org_meeting_crm_push already does,
    and reports a blocked precondition as the job's own FAILED/RECONNECT_
    REQUIRED terminal state rather than as an HTTP error the caller has to
    poll to discover happened at all.
    """
    user_id, key, item, workspace_id = _require_org_meeting(event, hydrate=False)
    data = _body(event)
    requested_objects = data.get("objects")
    if requested_objects is not None and not isinstance(requested_objects, list):
        raise ApiError(400, "objects must be a list")

    job = create_crm_sync_job(
        workspace_id=workspace_id, recording_key=key, user_id=user_id,
        requested_objects=requested_objects)
    return _resp(202, {"job_id": job["job_id"], "status": job["status"]})


# ===========================================================================
# CRM SYNC JOBS (Phase 2D.4) — durable state for an ASYNCHRONOUS push, and
# the queue/worker contract built on top of the (unchanged) Phase 2D.3
# identity/push logic above.
#
# WHY A SEPARATE STATE MACHINE FROM CRM_STATUS_* (defined earlier in this
# file). CRM_STATUS_* describes ONE field-mapping record's state
# (not_linked -> ... -> confirmed -> synced/failed) — a fact about a single
# Salesforce object association. CRM_JOB_STATUS_* describes the OUTER push
# ATTEMPT as a whole (this meeting's whole CRM sync, covering the object
# sync(es) AND Task creation AND Event creation together). They are
# deliberately not unified: a job can be SYNCED overall while one Task
# inside it FAILED (see execute_crm_sync_job's rollup rule below), which
# CRM_STATUS_* has no vocabulary for and should not need one.
# ---------------------------------------------------------------------------
CRM_JOB_STATUS_PENDING = "PENDING"
CRM_JOB_STATUS_SYNCING = "SYNCING"
CRM_JOB_STATUS_SYNCED = "SYNCED"
CRM_JOB_STATUS_RETRYING = "RETRYING"
CRM_JOB_STATUS_FAILED = "FAILED"
CRM_JOB_STATUS_RECONNECT_REQUIRED = "RECONNECT_REQUIRED"
CRM_JOB_STATUSES = (CRM_JOB_STATUS_PENDING, CRM_JOB_STATUS_SYNCING,
                   CRM_JOB_STATUS_SYNCED, CRM_JOB_STATUS_RETRYING,
                   CRM_JOB_STATUS_FAILED, CRM_JOB_STATUS_RECONNECT_REQUIRED)
# Terminal — a job in one of these will never be picked up by the worker
# again on its own; only an explicit user retry re-queues it (see
# retry_crm_sync_job).
CRM_JOB_TERMINAL_STATUSES = frozenset({CRM_JOB_STATUS_SYNCED, CRM_JOB_STATUS_FAILED,
                                       CRM_JOB_STATUS_RECONNECT_REQUIRED})

CRM_SYNC_OPERATION = "organisation_meeting_crm"

# ---------------------------------------------------------------------------
# Error classification (spec section 9). ONE function every retry/DLQ
# decision reads — never re-derived per call site, so "is this worth
# retrying" has exactly one answer.
# ---------------------------------------------------------------------------
CRM_ERROR_TRANSIENT = "transient"    # Salesforce 5xx/timeout/network, AWS hiccup
CRM_ERROR_REAUTH = "reauth"          # dead/revoked Salesforce refresh token
CRM_ERROR_PERMISSION = "permission"  # Salesforce/MinuteX authorization denial
CRM_ERROR_VALIDATION = "validation"  # Salesforce rejected a field/value
CRM_ERROR_PERMANENT = "permanent"    # bad config, missing identity, gone data

# Only these categories are worth a retry. permission/validation/permanent
# describe a condition that a retry, by itself, cannot fix — the org's
# Salesforce admin, or an Owner/Manager reconfiguring MinuteX, has to act
# first. Retrying those anyway would just repeat the same rejection until
# the DLQ, wasting attempts that could have gone to a genuinely transient
# job. reauth is also excluded: retrying with the SAME dead refresh token
# can never succeed until the user reconnects — see RECONNECT_REQUIRED.
CRM_RETRYABLE_ERROR_CATEGORIES = frozenset({CRM_ERROR_TRANSIENT})


def _classify_crm_error_fields(status: int, code: str) -> str:
    """The actual classification rule, over raw (status, code) rather than
    an exception object — so it can classify BOTH a freshly-raised ApiError
    (via _classify_crm_error below) AND an already-captured per-operation
    failure dict (push_errors[i], tasks.failed[i], event) that only ever
    stored status/code/error text, never the original exception instance.
    Never guesses from prose — only status codes and the stable `code`
    attribute this codebase already uses everywhere else
    (SalesforceReconnectRequired, OrganisationSalesforceNotConnected, ...).
    """
    code = code or ""
    status = status or 0

    if code == SF_RECONNECT_CODE:
        return CRM_ERROR_REAUTH
    if code in (ORG_SF_NOT_CONNECTED_CODE,):
        # Not connected/configured at all — nothing to retry until an
        # Owner/Manager fixes the organisation's Salesforce setup.
        return CRM_ERROR_PERMANENT
    if status == 403:
        return CRM_ERROR_PERMISSION
    if status in (400, 422):
        # Salesforce validation rejections use 422 in this codebase's own
        # convention (see update_record/create_record); a plain 400 here is
        # MinuteX's own precondition failure (no mapping configured, no
        # record linked) — both mean "won't succeed without a config/data
        # change", i.e. permanent, not validation-retry-worthy in the
        # Salesforce sense. Kept as one bucket: neither should be retried.
        return CRM_ERROR_VALIDATION if status == 422 else CRM_ERROR_PERMANENT
    if status == 404:
        # The linked Salesforce record is gone — crm_sync_record itself
        # already demotes this to lookup_pending; from the JOB's point of
        # view this meeting needs a human to re-link before it can proceed.
        return CRM_ERROR_PERMANENT
    if status == 429 or status >= 500 or status == 0:
        # 429 (throttling), 5xx, or 0 (network/timeout — this codebase's
        # SalesforceClient raises ApiError(502, ...) for both, so 0 covers a
        # non-ApiError exception the worker wraps defensively).
        return CRM_ERROR_TRANSIENT
    return CRM_ERROR_PERMANENT


def _classify_crm_error(e) -> str:
    """One ApiError (or ApiError-like: has .status and optionally .code) ->
    an error category. Thin wrapper over _classify_crm_error_fields — see
    that function for the actual rule."""
    return _classify_crm_error_fields(getattr(e, "status", 0) or 0,
                                      getattr(e, "code", "") or "")


def _crm_job_idempotency_key(workspace_id: str, recording_key: str,
                             op: str = CRM_SYNC_OPERATION) -> str:
    """Deterministic per (organisation, meeting, operation) — spec section 5.

    Used to find an EXISTING queued/in-flight job for this meeting before
    creating a new one (create_crm_sync_job), so a user double-tapping
    "Push to Salesforce" enqueues one job, not two. This is a MinuteX-level
    idempotency key, not a Salesforce one — Salesforce itself has no
    external-id mechanism this codebase can attach to Event/Task creation
    (evaluated, not invented: neither standard object exposes one in this
    org's schema without a custom field this codebase does not assume
    exists), which is exactly why crm_event_synced/crm_tasks_synced —
    durable state checked BEFORE any Salesforce call — carry the real
    duplicate-prevention weight; this key only prevents duplicate JOBS.
    """
    return f"{workspace_id}:{recording_key}:{op}"


def _active_crm_sync_job_for(recording_key: str, idempotency_key: str) -> dict:
    """An existing non-terminal job for this meeting, or None.

    Queried via the recording-index GSI rather than scanning CrmSyncJobs —
    see scripts/63_create_crm_sync_jobs_table.sh for why that index exists.
    """
    try:
        res = _crm_sync_jobs.query(
            IndexName="recording-index",
            KeyConditionExpression=Key("recording_key").eq(recording_key),
            ScanIndexForward=False,  # most recent first
        )
    except Exception as e:  # noqa: BLE001
        print(f"[crm-sync] job lookup failed for {recording_key}: "
              f"{type(e).__name__}: {e}")
        return None
    for row in res.get("Items", []):
        if row.get("idempotency_key") != idempotency_key:
            continue
        if row.get("status") not in CRM_JOB_TERMINAL_STATUSES:
            return row
    return None


def _enqueue_crm_sync_job(job: dict) -> None:
    """Send the SQS message for one job. MINIMAL PAYLOAD ONLY (spec section
    6) — job_id, workspace_id, recording_key, operation. No Salesforce
    token, no meeting content, no identity data: the worker reads all of
    that itself from DynamoDB via job_id, which is also what makes a
    duplicate delivery harmless (the worker re-reads current truth every
    time, never trusts what shipped in the message body).

    Best-effort in the sense that a failure here does not lose the job: the
    row already exists in CrmSyncJobs with status PENDING, so
    retry_crm_sync_job (or an operator) can re-enqueue it. It DOES raise,
    though — unlike the codebase's other best-effort async invokes (task
    seeding), silently swallowing a failed enqueue would leave a job stuck
    at PENDING forever with nothing telling the user it never actually got
    queued.
    """
    if not CRM_SYNC_QUEUE_URL:
        # No queue configured — this deployment has not run
        # scripts/64_create_crm_sync_queue.sh yet. Fail loudly rather than
        # silently leaving the job at PENDING with no way to progress.
        raise ApiError(503, "CRM sync queue is not configured")
    message = {
        "job_id": job["job_id"],
        "workspace_id": job["workspace_id"],
        "recording_key": job["recording_key"],
        "operation": job["operation"],
    }
    _sqs_client.send_message(
        QueueUrl=CRM_SYNC_QUEUE_URL,
        MessageBody=json.dumps(message),
    )


def create_crm_sync_job(*, workspace_id: str, recording_key: str, user_id: str,
                        requested_objects=None) -> dict:
    """Create (or reuse) the durable job row for one meeting's CRM push, and
    enqueue it. Returns the job row.

    IDEMPOTENT AT THE JOB LEVEL: an existing non-terminal job for the same
    (workspace, meeting, operation) is returned AS-IS rather than creating a
    second one — a double-tap of "Push to Salesforce" before the first
    attempt finishes must not queue two jobs that would both try to create
    the same Task/Event (crm_tasks_synced/crm_event_synced would make the
    second one a no-op Salesforce-side, but there is no reason to burn a
    second SQS message and worker invocation to learn that).
    """
    idempotency_key = _crm_job_idempotency_key(workspace_id, recording_key)
    existing = _active_crm_sync_job_for(recording_key, idempotency_key)
    if existing:
        print(f"[crm-sync] reusing existing job {existing['job_id']} for "
              f"{recording_key} (status={existing['status']})")
        return existing

    now = _now_iso()
    job = {
        "job_id": uuid.uuid4().hex,
        "workspace_id": workspace_id,
        "recording_key": recording_key,
        "requested_by_user_id": user_id,
        "operation": CRM_SYNC_OPERATION,
        "status": CRM_JOB_STATUS_PENDING,
        "attempt_count": 0,
        "idempotency_key": idempotency_key,
        "requested_objects": requested_objects if requested_objects is not None else None,
        "next_attempt_at": now,
        "last_error_category": "",
        "last_error_code": "",
        "last_error_message": "",
        "result": {},
        "created_at": now,
        "updated_at": now,
        "completed_at": "",
    }
    # requested_objects=None must not be written as DynamoDB NULL noise when
    # absent — omit rather than write None.
    if job["requested_objects"] is None:
        del job["requested_objects"]
    _crm_sync_jobs.put_item(Item=job)
    print(f"[crm-sync] job {job['job_id']} created for workspace={workspace_id} "
          f"meeting={recording_key} by={user_id}")

    try:
        _enqueue_crm_sync_job(job)
    except Exception as e:  # noqa: BLE001
        print(f"[crm-sync] job {job['job_id']} enqueue failed: "
              f"{type(e).__name__}: {e}")
        raise
    return job


def _get_crm_sync_job(job_id: str) -> dict:
    return _crm_sync_jobs.get_item(Key={"job_id": job_id}).get("Item")


def _update_crm_sync_job(job_id: str, **fields) -> None:
    fields["updated_at"] = _now_iso()
    names = {f"#{k}": k for k in fields}
    _crm_sync_jobs.update_item(
        Key={"job_id": job_id},
        UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in fields),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues={f":{k}": v for k, v in fields.items()},
    )


# Retry policy (spec section 10): 3 attempts, doubling backoff, then the SQS
# redrive policy (scripts/64_create_crm_sync_queue.sh,
# CRM_SYNC_MAX_RECEIVES=5) is what ultimately moves a message to the DLQ.
# The two numbers are deliberately different: this is the APPLICATION-level
# "is it worth trying again" decision (recorded durably in CrmSyncJobs, and
# what governs next_attempt_at); SQS's maxReceiveCount is the transport-
# level backstop for a worker that crashes/times out without updating the
# job row at all. A job this function marks FAILED never reaches the queue
# again on its own regardless of how many SQS receives remain — the two
# limits are independent safety nets, not one policy expressed twice.
CRM_JOB_MAX_ATTEMPTS = int(os.environ.get("CRM_JOB_MAX_ATTEMPTS", "3"))
CRM_JOB_BACKOFF_SECONDS = (60, 300, 900)  # 1 min, 5 min, 15 min


def _next_attempt_delay(attempt_count: int) -> int:
    idx = min(attempt_count, len(CRM_JOB_BACKOFF_SECONDS) - 1)
    return CRM_JOB_BACKOFF_SECONDS[idx]


def execute_crm_sync_job(job_id: str) -> dict:
    """THE worker entry point — called by crmSyncWorker's Lambda handler for
    every SQS message, and directly by tests (no SQS in the offline suite).

    SECURITY (spec section 16): re-verifies workspace/meeting state from
    DynamoDB using ONLY the job_id the message named — never trusts a
    workspace_id or recording_key carried in the SQS message body itself
    (the message is not an authorization boundary). The job row IS the
    authorization record: it was created by create_crm_sync_job under a
    verified _require_org_meeting call, so re-reading it (rather than the
    message) is what makes a forged/tampered SQS message unable to target
    someone else's workspace no matter what it claims.

    IDEMPOTENT AND CRASH-SAFE: status is written to SYNCING before any
    Salesforce call, and _execute_org_meeting_crm_push's own per-operation
    idempotency (crm_records status, crm_tasks_synced, crm_event_synced) is
    what makes a re-run after a crash — worker died after Salesforce
    succeeded but before this function persisted SYNCED, a duplicate SQS
    delivery, a visibility-timeout redelivery — skip everything already
    done and retry only what is not.
    """
    job = _get_crm_sync_job(job_id)
    if not job:
        # A job_id with no row is not this function's problem to retry —
        # either it was never created (a malformed/forged message) or it
        # was somehow deleted. Either way there is nothing to act on.
        print(f"[crm-sync] job {job_id} not found — dropping")
        return {"job_id": job_id, "dropped": True}

    workspace_id = job["workspace_id"]
    recording_key = job["recording_key"]
    attempt = int(job.get("attempt_count") or 0) + 1
    _update_crm_sync_job(job_id, status=CRM_JOB_STATUS_SYNCING, attempt_count=attempt)
    print(f"[crm-sync] job {job_id} attempt={attempt} workspace={workspace_id} "
          f"meeting={recording_key} operation={job.get('operation', '')}")

    # Re-validate the meeting/workspace still exist and are still linked the
    # way the job claims — an organisation could be deleted, or (far more
    # likely) the meeting could have been deleted, between enqueue and
    # worker pickup. Fails PERMANENT, not transient: a missing meeting will
    # never reappear.
    item = _recordings.get_item(Key={"audio_s3_key": recording_key}).get("Item")
    if not item:
        return _finish_crm_sync_job(job_id, attempt, CRM_JOB_STATUS_FAILED,
                                    CRM_ERROR_PERMANENT, "",
                                    "the meeting no longer exists")
    row_workspace_id = _row_workspace_id(item)
    if row_workspace_id != workspace_id:
        # The meeting's workspace no longer matches what the job was
        # created for (moved, or the job/message was tampered with). Never
        # push against the WRONG organisation's Salesforce connection.
        return _finish_crm_sync_job(job_id, attempt, CRM_JOB_STATUS_FAILED,
                                    CRM_ERROR_PERMANENT, "",
                                    "this meeting no longer belongs to the "
                                    "workspace this job was created for")
    if not workspace_schema.is_organisation_workspace_id(workspace_id):
        return _finish_crm_sync_job(job_id, attempt, CRM_JOB_STATUS_FAILED,
                                    CRM_ERROR_PERMANENT, "",
                                    "not an organisation meeting")
    membership = _workspace_row(workspace_id)
    if not membership or not workspace_schema.workspace_is_active(membership):
        return _finish_crm_sync_job(job_id, attempt, CRM_JOB_STATUS_FAILED,
                                    CRM_ERROR_PERMANENT, "",
                                    "this workspace is no longer active")

    requested_objects = job.get("requested_objects")
    try:
        result = _execute_org_meeting_crm_push(
            job.get("requested_by_user_id", ""), recording_key, item,
            workspace_id, requested_objects)
    except ApiError as e:
        category = _classify_crm_error(e)
        code = getattr(e, "code", "")
        return _finish_crm_sync_job(job_id, attempt,
                                    CRM_JOB_STATUS_RECONNECT_REQUIRED
                                    if category == CRM_ERROR_REAUTH
                                    else CRM_JOB_STATUS_FAILED,
                                    category, code, e.message, retryable=(
                                        category in CRM_RETRYABLE_ERROR_CATEGORIES))
    except Exception as e:  # noqa: BLE001
        # An unexpected failure (not an ApiError this codebase raised on
        # purpose) is treated as transient — worth one more try — rather
        # than permanent, since the alternative (never retrying an unknown
        # failure mode) risks silently dropping a recoverable job.
        print(f"[crm-sync] job {job_id} unexpected error: "
              f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
        return _finish_crm_sync_job(job_id, attempt, CRM_JOB_STATUS_FAILED,
                                    CRM_ERROR_TRANSIENT, "", str(e)[:500],
                                    retryable=True)

    # PARTIAL FAILURE ROLLUP (spec section 13). The job as a whole is SYNCED
    # only when every operation it attempted actually succeeded; a Task or
    # object-sync failure inside an otherwise-successful push must still be
    # visible as a job-level problem the user can retry, even though the
    # underlying idempotency state means a retry only repeats the failed
    # part (crm_event_synced/crm_tasks_synced skip what already succeeded).
    had_failure = bool(result.get("push_errors")) or bool(result["tasks"].get("failed")) \
        or bool(result["event"].get("error"))
    reconnect = any(pe.get("code") == SF_RECONNECT_CODE for pe in result.get("push_errors", [])) \
        or result["event"].get("code") == SF_RECONNECT_CODE
    if reconnect:
        return _finish_crm_sync_job(job_id, attempt, CRM_JOB_STATUS_RECONNECT_REQUIRED,
                                    CRM_ERROR_REAUTH, SF_RECONNECT_CODE,
                                    "the organisation's Salesforce connection has "
                                    "expired — reconnect Salesforce", result=result)
    if had_failure:
        # At least one operation failed with a non-reauth error. Classify
        # from the FIRST failure encountered — using its OWN status/code
        # (Phase 2D.4 addition to push_errors/tasks.failed/event; see those
        # capture points) rather than assuming transient, so a permission or
        # validation failure is never retried just because it happened
        # alongside an otherwise-successful push. The individual
        # per-operation errors remain fully available in `result` for the UI
        # to render per spec section 14's example.
        first_error = next(
            (pe for pe in result.get("push_errors", []) if pe.get("error")),
            None)
        if first_error is None:
            first_task_failure = next(iter(result["tasks"].get("failed", [])), None)
            if first_task_failure:
                message = first_task_failure.get("error") or "task creation failed"
                category = _classify_crm_error_fields(
                    first_task_failure.get("status", 0), first_task_failure.get("code", ""))
            elif result["event"].get("error"):
                message = result["event"]["error"]
                category = _classify_crm_error_fields(
                    result["event"].get("status", 0), result["event"].get("code", ""))
            else:
                message = "one or more CRM operations failed"
                category = CRM_ERROR_TRANSIENT
        else:
            message = first_error["error"]
            category = _classify_crm_error_fields(
                first_error.get("status", 0), first_error.get("code", ""))
        return _finish_crm_sync_job(job_id, attempt, CRM_JOB_STATUS_FAILED,
                                    category, "", message, result=result,
                                    retryable=category in CRM_RETRYABLE_ERROR_CATEGORIES)

    return _finish_crm_sync_job(job_id, attempt, CRM_JOB_STATUS_SYNCED,
                                "", "", "", result=result)


def _finish_crm_sync_job(job_id: str, attempt: int, terminal_or_retrying_status: str,
                         error_category: str, error_code: str, error_message: str,
                         *, result=None, retryable: bool = False) -> dict:
    """Persist the outcome of one attempt, deciding RETRYING vs the final
    terminal status. The ONE place attempt-count/backoff/DLQ-eligibility
    logic lives, so execute_crm_sync_job's callers never duplicate it.
    """
    now = _now_iso()
    fields = {
        "last_error_category": error_category, "last_error_code": error_code,
        "last_error_message": (error_message or "")[:500],
    }
    if result is not None:
        fields["result"] = result

    if terminal_or_retrying_status == CRM_JOB_STATUS_SYNCED:
        fields.update(status=CRM_JOB_STATUS_SYNCED, completed_at=now,
                      next_attempt_at="")
        _update_crm_sync_job(job_id, **fields)
        print(f"[crm-sync] job {job_id} SYNCED on attempt {attempt}")
        return _get_crm_sync_job(job_id)

    if terminal_or_retrying_status == CRM_JOB_STATUS_RECONNECT_REQUIRED:
        # Never retried automatically (spec section 11) — only a fresh user
        # action (reconnect, then explicit retry) re-queues this job.
        fields.update(status=CRM_JOB_STATUS_RECONNECT_REQUIRED,
                      completed_at=now, next_attempt_at="")
        _update_crm_sync_job(job_id, **fields)
        print(f"[crm-sync] job {job_id} RECONNECT_REQUIRED on attempt {attempt}")
        return _get_crm_sync_job(job_id)

    # FAILED, possibly retryable.
    if retryable and attempt < CRM_JOB_MAX_ATTEMPTS:
        delay = _next_attempt_delay(attempt - 1)
        next_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat().replace("+00:00", "Z")
        fields.update(status=CRM_JOB_STATUS_RETRYING, next_attempt_at=next_at)
        _update_crm_sync_job(job_id, **fields)
        print(f"[crm-sync] job {job_id} RETRYING (attempt {attempt}/{CRM_JOB_MAX_ATTEMPTS}, "
              f"category={error_category}, next_attempt_at={next_at}, "
              f"retry_after_seconds={delay})")
        # The job row records next_attempt_at for status reporting; the
        # ACTUAL delay before redelivery is enforced by the worker Lambda
        # handler calling SQS ChangeMessageVisibility with `delay` on the
        # message that triggered this attempt (see crm-sync-worker's
        # lambda_handler) — capped to the queue's own VisibilityTimeout
        # maximum there, never assumed here.
        raise _CrmSyncJobRetry(job_id, retry_after_seconds=delay)

    fields.update(status=CRM_JOB_STATUS_FAILED, completed_at=now, next_attempt_at="")
    _update_crm_sync_job(job_id, **fields)
    print(f"[crm-sync] job {job_id} FAILED permanently on attempt {attempt} "
          f"(category={error_category}, retryable={retryable})")
    return _get_crm_sync_job(job_id)


class _CrmSyncJobRetry(Exception):
    """Raised by _finish_crm_sync_job when a job should be retried — the
    worker Lambda handler catches this, reports the message as a batch item
    failure (so it is NOT deleted), and also applies `retry_after_seconds`
    to the message via SQS ChangeMessageVisibility so the actual redelivery
    delay matches the job's own computed backoff (_next_attempt_delay)
    instead of always falling back to the queue's fixed VisibilityTimeout.
    Never escapes to an HTTP caller: only the worker's lambda_handler and
    tests observe this."""

    def __init__(self, job_id: str, retry_after_seconds: int = 0):
        super().__init__(job_id)
        self.job_id = job_id
        self.retry_after_seconds = retry_after_seconds


def get_crm_sync_job_status(event):
    """GET /recordings/ai/crm-sync-jobs/{key+} (JWT, organisation meetings
    only) -> {job} | {job: null}

    The status the frontend polls after a Push — spec section 21's "CRM
    sync queued... syncing... synced/failed/reconnect required". Returns
    the MOST RECENT job for this meeting (by created_at), or null if none
    was ever created (the meeting has never been pushed).
    """
    _user_id, key, _item, _workspace_id = _require_org_meeting(event, hydrate=False)
    res = _crm_sync_jobs.query(
        IndexName="recording-index",
        KeyConditionExpression=Key("recording_key").eq(key),
        ScanIndexForward=False, Limit=1,
    )
    items = res.get("Items") or []
    return _resp(200, {"job": _public_crm_sync_job(items[0]) if items else None})


def _public_crm_sync_job(job: dict) -> dict:
    return {
        "job_id": job.get("job_id", ""),
        "status": job.get("status", ""),
        "attempt_count": int(job.get("attempt_count") or 0),
        "last_error_category": job.get("last_error_category", ""),
        "last_error_code": job.get("last_error_code", ""),
        "last_error_message": job.get("last_error_message", ""),
        "result": job.get("result") or {},
        "created_at": job.get("created_at", ""),
        "updated_at": job.get("updated_at", ""),
        "completed_at": job.get("completed_at", ""),
    }


def retry_crm_sync_job(event):
    """POST /recordings/ai/crm-sync-jobs/{key+}/retry (JWT, organisation
    meetings only) -> {job_id, status} — 202 Accepted.

    User-initiated retry for a job sitting in a TERMINAL state (spec
    section 14): FAILED (recoverable — Salesforce was down, a transient
    error), or RECONNECT_REQUIRED (after the user has reconnected
    Salesforce — see org_salesforce_connect). Re-enqueues the EXISTING job
    row rather than creating a new one, so attempt_count/history is
    continuous and idempotency state (crm_event_synced etc.) is read fresh
    at execution time — if identity was re-resolved or Salesforce
    reconnected since the failure, THIS retry uses the current truth, never
    a cached copy (spec section 25).
    """
    _user_id, key, _item, _workspace_id = _require_org_meeting(event, hydrate=False)
    res = _crm_sync_jobs.query(
        IndexName="recording-index",
        KeyConditionExpression=Key("recording_key").eq(key),
        ScanIndexForward=False, Limit=1,
    )
    items = res.get("Items") or []
    if not items:
        raise ApiError(404, "no CRM sync job exists for this meeting yet")
    job = items[0]
    if job["status"] not in CRM_JOB_TERMINAL_STATUSES:
        raise ApiError(409, "this job is already queued or in progress")

    now = _now_iso()
    _update_crm_sync_job(job["job_id"], status=CRM_JOB_STATUS_PENDING,
                         next_attempt_at=now, last_error_category="",
                         last_error_code="", last_error_message="")
    refreshed = _get_crm_sync_job(job["job_id"])
    _enqueue_crm_sync_job(refreshed)
    print(f"[crm-sync] job {job['job_id']} manually retried")
    return _resp(202, {"job_id": job["job_id"], "status": CRM_JOB_STATUS_PENDING})


# ===========================================================================
# ELEVENLABS SPEECH-TO-TEXT WEBHOOK
#
# The completion half of the asynchronous STT flow. transcribeRecording queues
# a job with `webhook=true` and exits; ElevenLabs POSTs the transcript here
# whenever it finishes, minutes or hours later. See that Lambda's module
# docstring for why the transcription is no longer allowed to happen inside a
# Lambda invocation at all.
#
# THIS IS THE SECOND UNAUTHENTICATED ROUTE IN THIS FILE (the first is
# /crm/salesforce/callback). It is called by ElevenLabs' servers, which have no
# MinuteX JWT, so a bearer token is impossible. Identity comes from an HMAC
# signature over the raw body instead — the same substitution the Salesforce
# callback makes with its signed `state`.
#
# Five independent checks, in this order, each rejecting before anything is
# written. Order matters: the cheap cryptographic checks run before any
# DynamoDB read, so an unsigned flood costs no database traffic.
#
#   1. SIGNATURE   HMAC-SHA256 over "{timestamp}.{raw_body}" must match the
#                  v0= element of the ElevenLabs-Signature header. Verified
#                  against the official SDK's own implementation, not docs
#                  prose. Without this the endpoint would accept a forged
#                  transcript for any recording whose key an attacker guessed.
#   2. TIMESTAMP   Within ELEVENLABS_WEBHOOK_TOLERANCE (30 min, the SDK's
#                  value). A valid signature replayed weeks later is refused,
#                  so a captured request can't be used indefinitely.
#   3. ENVELOPE    webhook_metadata must carry our version and OUR bucket. Two
#                  environments sharing one ElevenLabs workspace would
#                  otherwise deliver each other's transcripts onto same-named
#                  keys.
#   4. STALENESS   request_id must equal the row's CURRENT stt_request_id. A
#                  reprocess queues a new job and rewrites that field, so the
#                  first job's late delivery is recognisably obsolete and is
#                  dropped instead of overwriting a newer transcript.
#   5. IDEMPOTENCE A conditional write claims the delivery. ElevenLabs retries
#                  webhooks, and duplicate AI processing would double-charge
#                  Groq and could double-push to Salesforce.
#
# The handler answers in well under a second: it writes the transcript to S3 and
# hands the analysis to transcribeRecording as an ASYNC invoke. Running the Groq
# pipeline inline would exceed API Gateway's 29s ceiling on any real meeting,
# and a webhook that times out is a webhook ElevenLabs retries — turning one
# slow analysis into several concurrent ones.
# ===========================================================================
ELEVENLABS_WEBHOOK_SECRET_ARN = os.environ.get(
    "ELEVENLABS_WEBHOOK_SECRET_ARN", "")

# Replay window. 1800s is the tolerance the official ElevenLabs SDK enforces;
# matching it keeps us from rejecting deliveries the sender considers valid.
ELEVENLABS_WEBHOOK_TOLERANCE = int(
    os.environ.get("ELEVENLABS_WEBHOOK_TOLERANCE", "1800"))

# The event type ElevenLabs sends for a finished transcription, and the
# metadata envelope version transcribeRecording stamps.
STT_WEBHOOK_EVENT = "speech_to_text_transcription"
STT_METADATA_VERSION = 1

# The completion event transcribeRecording's second entry point expects.
STT_COMPLETED_EVENT = "stt.completed"


def _stt_completion_payload(key, bucket, request_id, transcript, timestamps,
                            language):
    """The stt.completed invoke payload, JSON-encoded.

    `default=str` is REQUIRED, not defensive: timestamp segments carry `start`
    and `end` as Decimal (stt_result rounds them that way because DynamoDB
    rejects a Python float), and json.dumps raises TypeError on Decimal. Without
    it EVERY webhook delivery failed to hand off its analysis — caught by
    tests/test_async_stt.py before it reached production.

    Decimal -> str is safe for this hop: the receiving Lambda re-serializes the
    segments through transcript_store, whose own encoder emits them as JSON
    numbers, and the app does Number(seg.start) for tap-to-seek.
    """
    return json.dumps({
        "type": STT_COMPLETED_EVENT,
        "audio_s3_key": key,
        "bucket": bucket,
        "request_id": request_id,
        "transcript": transcript,
        "timestamps": timestamps,
        "language": language,
    }, default=str).encode("utf-8")

_elevenlabs_webhook_secret_cache = None


def _elevenlabs_webhook_secret():
    """The webhook signing secret, from Secrets Manager (env fallback).

    Cached per container like the JWT secret. Kept OUT of the Lambda env by
    default for the same reason the Salesforce client secret is: an env var is
    visible to anyone who can read the function's configuration, and this
    secret is what makes a forged transcript impossible.
    """
    global _elevenlabs_webhook_secret_cache
    if _elevenlabs_webhook_secret_cache is not None:
        return _elevenlabs_webhook_secret_cache
    if ELEVENLABS_WEBHOOK_SECRET_ARN:
        sm = boto3.client("secretsmanager", region_name=REGION)
        _elevenlabs_webhook_secret_cache = sm.get_secret_value(
            SecretId=ELEVENLABS_WEBHOOK_SECRET_ARN)["SecretString"]
    else:
        _elevenlabs_webhook_secret_cache = os.environ.get(
            "ELEVENLABS_WEBHOOK_SECRET", "")
    return _elevenlabs_webhook_secret_cache


def _raw_body(event):
    """The body EXACTLY as sent — the bytes the signature covers.

    Must not go through _body(): a parse-then-re-serialize round trip changes
    key order and whitespace, and the HMAC is over the original text, so a
    re-serialized body fails verification for every legitimate request.
    """
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except (ValueError, TypeError):
            return ""
    return raw


def _verify_elevenlabs_signature(event, raw_body):
    """Verify the ElevenLabs-Signature header. Raises ApiError(401) on failure.

    Scheme (verified against elevenlabs-js src/wrapper/webhooks.ts):
        header: "t=<unix_seconds>,v0=<hex_hmac_sha256>"
        signed: "{t}.{raw_body}"
    Hex-encoded, compared in constant time.
    """
    secret = _elevenlabs_webhook_secret()
    if not secret:
        # Fail CLOSED. An unconfigured secret must never mean "accept
        # everything" — that would leave the endpoint permanently open if a
        # deploy forgot the secret, which is exactly the kind of silent
        # misconfiguration nobody notices until it is abused.
        print("[stt-webhook] rejected: no webhook secret configured")
        raise ApiError(401, "webhook not configured")

    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    sig_header = headers.get("elevenlabs-signature", "")
    if not sig_header:
        raise ApiError(401, "missing signature")

    timestamp, signature = "", ""
    for part in sig_header.split(","):
        part = part.strip()
        if part.startswith("t="):
            timestamp = part[2:]
        elif part.startswith("v0="):
            signature = part
    if not timestamp or not signature:
        raise ApiError(401, "malformed signature header")

    # Reject a replay before spending a HMAC on it.
    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError):
        raise ApiError(401, "malformed signature timestamp")
    if abs(int(time.time()) - sent_at) > ELEVENLABS_WEBHOOK_TOLERANCE:
        raise ApiError(401, "signature timestamp outside tolerance")

    expected = "v0=" + hmac.new(secret.encode("utf-8"),
                                f"{timestamp}.{raw_body}".encode("utf-8"),
                                hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ApiError(401, "signature mismatch")


def _stt_webhook_metadata(data):
    """The validated metadata envelope, or ApiError(400).

    transcribeRecording stamps {audio_s3_key, bucket, v}. audio_s3_key is the
    AUTHORITATIVE identifier — it is the Recordings partition key — so the
    lookup uses it and nothing else. recording_id is derivable from the key and
    is never used to find the row.
    """
    meta = data.get("webhook_metadata")
    # Tolerate a JSON-encoded string: it is sent as one, and a sender that
    # echoes it back verbatim rather than parsed is a plausible variation.
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except (ValueError, TypeError):
            raise ApiError(400, "unparseable webhook_metadata")
    if not isinstance(meta, dict):
        raise ApiError(400, "missing webhook_metadata")

    version = meta.get("v")
    if version != STT_METADATA_VERSION:
        # An unrecognised envelope is refused rather than guessed at — the
        # whole point of versioning it.
        raise ApiError(400, f"unsupported webhook_metadata version {version!r}")

    key = str(meta.get("audio_s3_key") or "").strip()
    if not key:
        raise ApiError(400, "webhook_metadata has no audio_s3_key")

    bucket = str(meta.get("bucket") or "").strip()
    if not bucket or (BUCKET_NAME and bucket != BUCKET_NAME):
        # Another environment's delivery. Refused, never applied to a
        # same-named key in this one.
        print(f"[stt-webhook] rejected: bucket {bucket!r} is not this "
              f"environment's ({BUCKET_NAME!r})")
        raise ApiError(400, "bucket mismatch")
    return key, bucket


def stt_webhook(event):
    """POST /webhooks/elevenlabs/stt — ElevenLabs delivers a finished transcript.

    Unauthenticated by necessity, signature-verified in fact. Returns quickly;
    the AI analysis runs as a separate async invocation.

    Always answers 200 once the delivery is genuine and understood — including
    for a duplicate or a stale job. A non-2xx tells ElevenLabs to RETRY, and
    retrying is pointless for a delivery we have deliberately decided to ignore.
    Only a real failure (bad signature, malformed payload, a transcript we
    failed to store) answers non-2xx.
    """
    raw_body = _raw_body(event)
    # 1. Signature, before anything else is trusted or read.
    _verify_elevenlabs_signature(event, raw_body)

    try:
        data = json.loads(raw_body) if raw_body else {}
    except (ValueError, TypeError):
        raise ApiError(400, "invalid JSON body")
    if not isinstance(data, dict):
        raise ApiError(400, "body must be a JSON object")

    # 2. Payload structure/version.
    event_type = str(data.get("type") or "")
    if event_type != STT_WEBHOOK_EVENT:
        # Another webhook type on the same URL is not an error worth retrying.
        print(f"[stt-webhook] ignoring event type {event_type!r}")
        return _resp(200, {"ignored": True, "reason": "unsupported event type"})

    payload = data.get("data")
    if not isinstance(payload, dict):
        raise ApiError(400, "missing data object")

    key, bucket = _stt_webhook_metadata(payload)
    request_id = str(payload.get("request_id") or "").strip()
    if not request_id:
        raise ApiError(400, "missing request_id")

    transcription = payload.get("transcription")
    if not isinstance(transcription, dict):
        # ElevenLabs also reports FAILED transcriptions here. Mark the row
        # failed so the app stops polling and offers Retry, rather than leaving
        # it "transcribing" — the exact dead end this phase removes.
        return _stt_webhook_failure(key, request_id, payload)

    # 3. Locate the row by its PARTITION KEY and check staleness.
    item = _recordings.get_item(Key={"audio_s3_key": key}).get("Item")
    if not item:
        print(f"[stt-webhook] no recording for {key}")
        return _resp(200, {"ignored": True, "reason": "unknown recording"})

    stored = str(item.get("stt_request_id") or "")
    if stored and stored != request_id:
        # A LATER job has already been queued for this recording (a reprocess),
        # so this delivery belongs to a superseded one. Dropping it is the
        # point: the newer transcript must never be overwritten by the older.
        print(f"[stt-webhook] STALE for {key}: got {request_id}, "
              f"row has {stored}")
        return _resp(200, {"ignored": True, "reason": "stale request_id"})
    if not stored:
        # No id on the row at all. Either it predates async STT or the start
        # write was lost; either way we cannot prove this delivery is current,
        # and accepting it could overwrite a transcript from a job we know
        # nothing about.
        print(f"[stt-webhook] no stt_request_id on {key} — refusing to apply "
              f"an unverifiable delivery")
        return _resp(200, {"ignored": True, "reason": "no pending job"})

    # 4. Claim the delivery. A conditional write is what makes a retry a no-op:
    # only the FIRST delivery of this request_id passes the condition, so the
    # analysis is invoked exactly once even if ElevenLabs sends it five times.
    # DynamoDB provides the atomicity — no lock table, no new infrastructure.
    try:
        _recordings.update_item(
            Key={"audio_s3_key": key},
            UpdateExpression="SET stt_completed_request_id = :rid, "
                             "stt_completed_at = :now",
            ConditionExpression="stt_request_id = :rid AND "
                                "(attribute_not_exists(stt_completed_request_id) "
                                "OR stt_completed_request_id <> :rid)",
            ExpressionAttributeValues={":rid": request_id, ":now": _now_iso()},
        )
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") == \
                "ConditionalCheckFailedException":
            # Either already claimed (a duplicate delivery) or the row moved on
            # under us (a reprocess between the read above and here). Both mean
            # "do nothing", and both are successes from ElevenLabs' side.
            print(f"[stt-webhook] duplicate/superseded delivery for {key} "
                  f"({request_id}) — already processed")
            return _resp(200, {"duplicate": True})
        raise

    # 5. Build the transcript from the words[] payload, using the SHARED parser
    # (lambda-shared/stt_result.py) — the same code transcribeRecording uses.
    # One implementation, deliberately: a second copy here would eventually
    # disagree about where a speaker's turn ends, and the app's tap-to-seek maps
    # each timestamp segment onto the diarized line at the same index, so a
    # divergence surfaces as playback jumping to the wrong sentence.
    transcript, timestamps, language = stt_result.parse(transcription)
    if not transcript.strip():
        print(f"[stt-webhook] empty transcript for {key}")

    # The transcript goes to S3, never into the DynamoDB item (a real 45-minute
    # meeting's transcript + timestamps was 96% of the 400 KB item limit — see
    # lambda-shared/transcript_store.py). Written HERE, before the analysis is
    # invoked, so the transcript is durable even if the analysis never runs:
    # that is the difference between "reprocess the AI" and "the transcript is
    # gone and STT must be paid for again".
    fields = {"language": language}
    try:
        fields.update(transcript_store.put(
            _s3, bucket, key, transcript, timestamps))
    except Exception as err:  # noqa: BLE001
        # A 5xx here is CORRECT: we could not persist what we were given, so a
        # retry from ElevenLabs is genuinely useful. The claim is released so
        # that retry isn't rejected as a duplicate.
        print(f"[stt-webhook] transcript_store put FAILED for {key}: {err}")
        _release_stt_claim(key)
        raise ApiError(502, "could not store transcript")

    _update_recording_fields(key, fields)

    # 6. Hand off the analysis. ASYNC ("Event"): the Groq pipeline takes far
    # longer than API Gateway will wait, and a timed-out webhook is one
    # ElevenLabs retries — which would start a second analysis of the same
    # meeting. The transcript travels in the payload so the analysis Lambda
    # doesn't re-read what we just wrote.
    try:
        _lambda_client.invoke(
            FunctionName=TRANSCRIBE_LAMBDA_NAME,
            InvocationType="Event",
            Payload=_stt_completion_payload(key, bucket, request_id,
                                            transcript, timestamps, language),
        )
    except Exception as err:  # noqa: BLE001
        # The transcript IS saved, so this is recoverable without ElevenLabs:
        # the row keeps status "transcribing" and the user's Retry (or the
        # reconciler) re-runs the analysis. Answer 502 so the failure is
        # visible in ElevenLabs' delivery log rather than silently swallowed.
        print(f"[stt-webhook] analysis invoke FAILED for {key}: {err}")
        _update_recording_fields(key, {
            "status": "transcribed",
            "error": "transcript saved; AI analysis could not be started"})
        raise ApiError(502, "could not start analysis")

    print(f"[stt-webhook] accepted {key} ({request_id}): {len(transcript)} "
          f"chars, {len(timestamps)} segments, language={language} — analysis "
          f"invoked")
    return _resp(200, {"accepted": True, "audio_s3_key": key,
                       "transcript_chars": len(transcript)})


def _stt_webhook_failure(key, request_id, payload):
    """ElevenLabs reported a transcription that did NOT produce a transcript.

    Marks the row failed so the app stops polling and offers Retry. Gated on
    the same staleness rule as a success — an old job's failure must not fail a
    recording that has since been re-queued and may be transcribing fine.
    """
    item = _recordings.get_item(Key={"audio_s3_key": key}).get("Item") or {}
    stored = str(item.get("stt_request_id") or "")
    if stored and stored != request_id:
        print(f"[stt-webhook] stale FAILURE for {key} ignored "
              f"({request_id} != {stored})")
        return _resp(200, {"ignored": True, "reason": "stale request_id"})

    detail = (str(payload.get("error") or payload.get("message")
                  or payload.get("status") or "transcription failed"))[:1000]
    _update_recording_fields(key, {"status": "failed", "error": detail})
    print(f"[stt-webhook] transcription FAILED for {key}: {detail}")
    return _resp(200, {"accepted": True, "failed": True})


def _release_stt_claim(key):
    """Undo the idempotency claim so a genuine retry can be processed.

    Only called when we accepted a delivery and then failed to persist it —
    without this, ElevenLabs' retry would be rejected as a duplicate and the
    transcript would be lost for good.
    """
    try:
        _recordings.update_item(
            Key={"audio_s3_key": key},
            UpdateExpression="REMOVE stt_completed_request_id, stt_completed_at",
        )
    except Exception as err:  # noqa: BLE001
        print(f"[stt-webhook] could not release claim for {key}: {err}")


# ---------------------------------------------------------------------------
# RECONCILIATION — the guarantee that "transcribing" is never permanent.
#
# A webhook is a delivery, and deliveries can be lost: a bad deploy while
# ElevenLabs was retrying, a webhook deleted from the workspace, a signature
# secret rotated mid-flight. Without a way to ask "what happened to job X?", a
# lost delivery would leave the row at "transcribing" forever — the very
# failure mode async STT was adopted to remove, reintroduced by a different
# route.
#
# The recovery only exists because the job id is PERSISTED. We can ask
# ElevenLabs about a specific transcription at any later time, from any
# container, with nothing held open in between — which is precisely what the
# old synchronous socket could not survive.
#
# Two ways it resolves, both terminal:
#   * ElevenLabs HAS the transcript -> store it and run the analysis, exactly
#     as the webhook would have. The user loses nothing but time.
#   * ElevenLabs has no such job (404) -> the job is genuinely gone, so the row
#     becomes "failed" and the app offers Retry instead of a dead spinner.
# ---------------------------------------------------------------------------
ELEVENLABS_TRANSCRIPT_URL = (
    "https://api.elevenlabs.io/v1/speech-to-text/transcripts")

# A job younger than this is probably still legitimately running — a 10-hour
# recording takes a long time — so reconciling it would just add load and could
# race the real webhook. The webhook's idempotency claim makes that race safe,
# but not free.
STT_RECONCILE_MIN_AGE = int(os.environ.get("STT_RECONCILE_MIN_AGE", "900"))


def stt_reconcile(event):
    """POST /recordings/ai/stt-reconcile/{key+} -> {status, ...}.

    Ask ElevenLabs directly what became of this recording's transcription job.
    JWT-authenticated and owner-scoped (unlike the webhook, this is a user
    action). 202 when the transcript was recovered and analysis started.
    """
    _, key, item = _owned_recording(event, hydrate=False)

    request_id = str(item.get("stt_request_id") or "")
    transcription_id = str(item.get("stt_transcription_id") or "")
    status = (item.get("status") or "").strip()

    if status not in ("transcribing", "generating_ai"):
        raise ApiError(409, f"a recording with status '{status}' has nothing "
                            "to reconcile")
    if not request_id:
        # Predates async STT, or the start write was lost. Reprocess is the
        # right tool — it queues a brand-new job rather than chasing a
        # phantom one.
        raise ApiError(409, "no transcription job recorded for this recording "
                            "— use Reprocess instead")
    if item.get("stt_completed_request_id") == request_id:
        # The webhook already landed; the analysis is running or done.
        return _resp(200, {"status": status, "reconciled": False,
                           "reason": "already delivered"})

    started = item.get("stt_started_at")
    if started:
        try:
            age = time.time() - datetime.fromisoformat(
                str(started).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            age = None
        if age is not None and 0 <= age < STT_RECONCILE_MIN_AGE:
            wait = int(STT_RECONCILE_MIN_AGE - age)
            raise ApiError(429, "transcription is still in progress — check "
                                f"again in {wait}s")

    if not transcription_id:
        # ElevenLabs' transcript endpoint is keyed by transcription_id, which is
        # optional in the queue response. Without it there is nothing to fetch,
        # so the honest move is to let the user re-queue rather than to guess.
        raise ApiError(409, "this job has no transcription id to look up — use "
                            "Reprocess to start a new transcription")

    api_key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not api_key:
        raise ApiError(500, "server is missing ELEVENLABS_API_KEY")

    url = f"{ELEVENLABS_TRANSCRIPT_URL}/{urllib.parse.quote(transcription_id)}"
    req = urllib.request.Request(
        url, headers={"xi-api-key": api_key, "User-Agent": "minutex-ai/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", "replace")[:300]
        if err.code == 404:
            # Genuinely gone. Make the row terminal so the app stops polling.
            _update_recording_fields(key, {
                "status": "failed",
                "error": "the transcription job no longer exists at the "
                         "provider; please retry"})
            print(f"[stt-reconcile] {key}: job {transcription_id} is gone (404)")
            return _resp(200, {"status": "failed", "reconciled": True,
                               "reason": "job not found at provider"})
        print(f"[stt-reconcile] {key}: provider {err.code}: {body}")
        raise ApiError(502, f"transcription provider returned {err.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as err:
        print(f"[stt-reconcile] {key}: transport error: {err}")
        raise ApiError(502, "could not reach the transcription provider")

    transcript, timestamps, language = stt_result.parse(result)
    if not transcript.strip():
        # Reachable but with nothing usable in it — still in progress, or a
        # result with no speech. Leave the row alone rather than writing an
        # empty transcript over a job that may yet complete.
        return _resp(200, {"status": status, "reconciled": False,
                           "reason": "no transcript available yet"})

    # From here it is the webhook's own path: claim, store, invoke. The claim
    # keeps a late webhook from re-running the analysis we are about to start.
    try:
        _recordings.update_item(
            Key={"audio_s3_key": key},
            UpdateExpression="SET stt_completed_request_id = :rid, "
                             "stt_completed_at = :now",
            ConditionExpression="stt_request_id = :rid AND "
                                "(attribute_not_exists(stt_completed_request_id) "
                                "OR stt_completed_request_id <> :rid)",
            ExpressionAttributeValues={":rid": request_id, ":now": _now_iso()},
        )
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") == \
                "ConditionalCheckFailedException":
            return _resp(200, {"status": status, "reconciled": False,
                               "reason": "already claimed"})
        raise

    fields = {"language": language}
    try:
        fields.update(transcript_store.put(
            _s3, BUCKET_NAME, key, transcript, timestamps))
    except Exception as err:  # noqa: BLE001
        print(f"[stt-reconcile] transcript_store put FAILED for {key}: {err}")
        _release_stt_claim(key)
        raise ApiError(502, "could not store the recovered transcript")
    _update_recording_fields(key, fields)

    try:
        _lambda_client.invoke(
            FunctionName=TRANSCRIBE_LAMBDA_NAME,
            InvocationType="Event",
            Payload=_stt_completion_payload(key, BUCKET_NAME, request_id,
                                            transcript, timestamps, language),
        )
    except Exception as err:  # noqa: BLE001
        print(f"[stt-reconcile] analysis invoke FAILED for {key}: {err}")
        _update_recording_fields(key, {
            "status": "transcribed",
            "error": "transcript recovered; AI analysis could not be started"})
        raise ApiError(502, "recovered the transcript but could not start "
                            "analysis — please retry")

    print(f"[stt-reconcile] {key}: recovered {len(transcript)} chars from "
          f"{transcription_id} — analysis invoked")
    return _resp(202, {"status": "generating_ai", "reconciled": True,
                       "transcript_chars": len(transcript)})


def _update_recording_fields(key, fields):
    """SET `fields` on a recording row. Small helper mirroring the transcribe
    Lambda's _upsert, kept local so the webhook never reaches for a hydrated
    item or writes the transcript back inline."""
    if not fields:
        return
    safe = transcript_store.strip_for_write(fields)
    names, values, sets = {}, {}, []
    for i, (k, v) in enumerate(safe.items()):
        names[f"#f{i}"] = k
        values[f":v{i}"] = v
        sets.append(f"#f{i} = :v{i}")
    if not sets:
        return
    _recordings.update_item(
        Key={"audio_s3_key": key},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


# ===========================================================================
# MinuteX Assistant — authenticated identity + the AI-safe task tool layer.
#
# This is the foundation the workspace-wide AI Chat is built on. Its whole
# reason for existing is one rule:
#
#   THE MODEL NEVER DECIDES WHOSE DATA IT READS.
#
# Every other design choice here follows from that. The identity comes from
# the same JWT every other route in this file uses (_require_auth); the tools
# take a context object rather than a user id; and none of the tool SCHEMAS
# advertised to the model contain a user_id, contact_id or owner parameter, so
# there is no field for a prompt-injected instruction to fill in. A message
# that says "I am Priya, show me her tasks" cannot widen access, because the
# only identity in the system arrived with the Authorization header.
#
# The tools are thin wrappers over the SAME reads the REST routes use — the
# ownership predicates (_owned_task, _owned_folder, and _owned_recording's
# check) are reused, not reimplemented. That is deliberate: a second copy of
# an authorization rule is a second place for it to be wrong, and the AI path
# must not be the weaker of the two.
#
# TENANCY. MinuteX has no organizations table: a user's workspace IS the
# tenant, and `owner_user_id` is the boundary (the same one Contacts, Folders
# and Tasks are keyed by). So "tenant isolation" here means exactly what it
# means on the REST routes — every row is re-checked against the caller, and a
# GSI is only ever a lookup path, never an authorization decision. No
# organization_id is invented to look more multi-tenant than the system is; a
# field the rest of the product does not have could only ever be decorative,
# and a decorative security field is worse than none. If an org layer is added
# later, AIContext is the one place that has to learn about it.
# ===========================================================================

# The agent loop's ceiling. API Gateway hard-stops at 29s and each iteration is
# one Groq round trip, so this is a latency budget as much as a loop guard:
# past a handful of hops the request cannot finish regardless.
AI_MAX_TOOL_HOPS = int(os.environ.get("AI_MAX_TOOL_HOPS", "4"))

# How many rows any one tool may hand back to the model. Two separate reasons,
# both real: the TPM window (a hundred tasks of JSON crowds out the answer),
# and honesty (a truncated list the model presents as complete is a lie the
# user cannot see). _tool_result stamps `truncated` so the model can say so.
AI_TOOL_ROW_LIMIT = int(os.environ.get("AI_TOOL_ROW_LIMIT", "25"))

MAX_AI_MESSAGE_CHARS = 2_000
AI_HISTORY_TURNS = 6

# ===========================================================================
# WORKSPACE CHAT SESSIONS — persistent, resumable Task AI conversations.
#
# WHY ONE CAPPED ITEM AND NOT TWO TABLES.
#
# The obvious shape for a chat log is ChatSessions + ChatMessages with a
# composite key. Two things ruled it out here, and both came from reading the
# codebase rather than from preference:
#
#   1. MinuteX HAS NO COMPOSITE PK+SK TABLE. All sixteen tables are a single
#      HASH key plus GSIs for listing (Notifications is the closest analogue:
#      PK notification_id, GSI user_id+created_at). Introducing the first
#      PK/SK table — and the first fan-out read — for a feature whose
#      per-session volume is a few dozen short turns would be a new pattern
#      to maintain for no measured benefit.
#   2. THE MEETING CHAT ALREADY SOLVES THIS, in production, by storing a
#      bounded `chat_history` list on the recording row and trimming it to
#      MAX_CHAT_TURNS. Same problem, same scale, already proven.
#
# So a session is ONE item holding a bounded turn list. The 400 KB item
# ceiling is respected by construction rather than by hoping:
#
#      MAX_SESSION_TURNS (40 turns = 20 exchanges)
#    x MAX_AI_MESSAGE_CHARS (2 000 per user turn)
#    + MAX_STORED_REPLY_CHARS (4 000 per assistant turn)
#    ------------------------------------------------
#      worst case ~120 KB, comfortably inside 400 KB
#
# There is no code path that appends without trimming: _append_turns is the
# only writer and it slices before every write.
#
# STORAGE IS NOT CONTEXT. What is PERSISTED (40 turns) and what is SENT TO
# GROQ (AI_HISTORY_TURNS = 6 exchanges) are deliberately different numbers,
# because they answer different questions — "what can the user scroll back
# to" versus "what fits the model's window without crowding out the answer".
# _session_history does that narrowing, once.
# ===========================================================================
CHAT_SESSIONS_TABLE = os.environ.get("CHAT_SESSIONS_TABLE", "ChatSessions")
# GSI: user_id (HASH) + updated_at (RANGE). The ONLY way a session list is
# read, and a lookup path only — every row is still re-checked against the
# caller, exactly as the Tasks indexes are.
CHAT_SESSIONS_USER_INDEX = os.environ.get("CHAT_SESSIONS_USER_INDEX",
                                          "user-index")

# The hard ceiling on stored conversation. 40 turns = 20 exchanges, matching
# the meeting chat's MAX_CHAT_TURNS so the two feel the same to a user.
MAX_SESSION_TURNS = int(os.environ.get("MAX_SESSION_TURNS", "40"))

# An assistant reply is stored truncated. Replies are normally a few hundred
# characters; this only catches a runaway generation, and it is the second
# half of the item-size arithmetic above.
MAX_STORED_REPLY_CHARS = 4_000

# Session list page size. A user with hundreds of conversations must not be
# able to make the list route read all of them (spec section 6).
SESSIONS_PAGE_DEFAULT = 20
SESSIONS_PAGE_MAX = 50

# Title length. Long enough to identify a conversation in a list, short
# enough that it can never carry meaningful transcript content.
MAX_SESSION_TITLE_CHARS = 60

_chat_sessions = _ddb.Table(CHAT_SESSIONS_TABLE)

# Starter prompts. Served from the backend rather than hardcoded in the app
# for the same reason CHAT_SUGGESTIONS is: the catalogue belongs in one place,
# and every one of these must be a question the tools can actually answer —
# a suggested prompt that produces "I can't do that" is worse than no
# suggestion, because the app itself proposed it.
AI_SUGGESTIONS = [
    "What needs my attention?",
    "What's overdue?",
    "What should I work on this week?",
    "What did I commit to in my last meeting?",
]

# The per-task catalogue (spec section 4). Every one of these is answerable
# from get_task plus get_meeting_context, and each is phrased so the answer
# separates the task's CURRENT state from the meeting history behind it.
AI_TASK_SUGGESTIONS = [
    "What is this task about?",
    "Why was this task created?",
    "What was discussed about it?",
    "What should happen next?",
]


class AIContext:
    """The authenticated caller, resolved ONCE per request from the JWT.

    Everything the tool layer is allowed to touch hangs off this object, and it
    is built only by _ai_context() below — i.e. only from a verified token.
    There is deliberately no constructor path that takes a user id out of a
    request body: if this could be built from client input, every guarantee in
    this section would be a comment rather than a control.

    `contact_id` is the caller's own Contact row when one exists. It is not
    identity (the user_id is) — it is how the caller appears in task
    assignments, resolved through the existing owner+email index rather than
    invented.

    `sources` accumulates the transcript evidence the tools actually retrieved
    this request. It is a RECORD OF WHAT WAS READ, written only by the
    retrieval tool and never by the model: an id the model invents cannot get
    in here, which is what lets ai_chat return sources the app can trust
    enough to deep-link. See _ai_note_sources.

    `proposals` accumulates the task changes the model asked to make this
    request. They are proposals ONLY — validated and authorized server-side,
    but never applied here (spec section 8); ai_chat returns them for the app
    to confirm with the user. Collected on the context rather than threaded
    through the agent loop's return value so a tool result and its proposal
    cannot drift apart.
    """

    __slots__ = ("user_id", "email", "display_name", "contact_id",
                 "request_id", "_devices", "sources", "proposals",
                 "workspace_id", "workspace_name", "role")

    def __init__(self, user_id, email="", display_name="", contact_id="",
                 request_id="", workspace_id="", workspace_name="", role=""):
        self.user_id = user_id
        self.email = email
        self.display_name = display_name
        self.contact_id = contact_id
        self.request_id = request_id
        # WORKSPACE CONTEXT (Phase 2C). Resolved by _ai_context from a
        # VERIFIED membership — never from the model, never from a tool
        # argument (see _AI_FORBIDDEN_ARGS), never from the request body.
        #
        # This is CONTEXT, not authorization. It tells the tools which
        # workspace the user is working in so they read the right address
        # book and the right meeting list; it does not decide what they may
        # see. Every row is still checked by the same application predicates
        # the REST routes use (_can_read_meeting, _owned_contact,
        # _ai_visible). An LLM is never the enforcement point.
        self.workspace_id = workspace_id
        self.workspace_name = workspace_name
        self.role = role
        self._devices = None
        self.sources = []
        self.proposals = []

    @property
    def is_organisation(self):
        return workspace_schema.is_organisation_workspace_id(
            self.workspace_id)

    @property
    def devices(self):
        """Owned device ids, for the legacy recording-ownership path. Read at
        most once per request — list_recordings pays this cost too."""
        if self._devices is None:
            self._devices = _owned_devices(self.user_id)
        return self._devices

    def owns_recording(self, item):
        """The SAME predicate _owned_recording enforces, applied to a row we
        already hold. Kept as one CALL — not a copy of the expression — so the
        AI path and the REST path cannot drift apart on what "my meeting"
        means.

        THE DRIFT THIS FIXES (P2/AI remediation). This used to inline
        `user_id == self.user_id or device_id in self.devices`, which is
        exactly what _owned_recording said before the organisation membership
        gate was added. When that gate landed on the REST side, this copy
        silently became the weaker door: a member removed from an organisation
        could still pull labelled transcript segments for a meeting they had
        created there, through get_meeting_context. Delegating removes the
        possibility of that happening again.

        `devices` is passed through so the AI path keeps its per-request
        device cache instead of re-reading it here.
        """
        if not item:
            return False
        return _can_write_meeting(self.user_id, item, devices=self.devices)


def _self_contact_id(user_id, email_lc, workspace_id=""):
    """The caller's OWN contact row in the ACTIVE workspace, or "".

    Task assignment in MinuteX is contact-based as well as user-based (a task
    can carry assignee_contact_id, assignee_user_id, or a bare speaker), so
    "assigned to me" has to consider both. This resolves the contact half
    through the existing email index — the same lookup create_contact uses —
    rather than adding a second notion of "who am I".

    SCOPED TO THE WORKSPACE (Phase 2C): inside an organisation, "me" is my
    entry in the SHARED address book. Resolving it against my personal
    contacts there would make "my tasks" miss work assigned to my
    organisation contact row. Omitted/personal keeps the original behaviour.
    """
    email_lc = (email_lc or "").strip().lower()
    if not email_lc:
        return ""
    try:
        row = _find_contact_by_email(user_id, email_lc,
                                     workspace_id=workspace_id)
    except ClientError as err:
        print(f"[warn] ai: self-contact lookup failed for {user_id}: {err}")
        return ""
    return str((row or {}).get("contact_id") or "")


def _ai_context(event):
    """Build the AIContext for this request from the Authorization header.

    The ONLY entry point into the tool layer's identity. Note what is NOT read
    here: the request body. A client may send user_id/contact_id/
    organization_id and it changes nothing — those keys are never looked at,
    which is the point of section 5.
    """
    user_id = _require_auth(event)

    email, display_name = "", ""
    try:
        row = _users.get_item(Key={"user_id": user_id}).get("Item") or {}
        email = str(row.get("email") or "")
        display_name = str(row.get("name") or "")
    except ClientError as err:
        # A profile read failure must not deny access to your own tasks: the
        # JWT already proved who you are, and name/email are only used to
        # address the user politely in the prompt.
        print(f"[warn] ai: profile read failed for {user_id}: {err}")

    # WORKSPACE CONTEXT (Phase 2C). The header is a REQUEST; membership is
    # resolved here and the VERIFIED result is what the tools see. A caller
    # naming a workspace they are not in falls back to their personal one
    # rather than erroring, exactly as list_workspaces does — an invalid hint
    # must not break the assistant.
    requested = _requested_workspace_id(event)
    workspace_id = _personal_workspace_id(user_id)
    workspace_name = "Personal"
    role = workspace_schema.ROLE_OWNER
    if requested and requested != workspace_id:
        membership = _active_membership(requested, user_id)
        if membership:
            workspace_id = requested
            role = str(membership.get("role") or "")
            workspace_name = str(
                (_workspace_row(requested) or {}).get("name") or "")

    rc = event.get("requestContext") or {}
    return AIContext(
        user_id=user_id,
        email=email,
        display_name=display_name,
        # The caller's own contact row, resolved IN the active workspace: in
        # an organisation "me" is my entry in the shared address book, not
        # the one in my personal one.
        contact_id=_self_contact_id(user_id, email, workspace_id=workspace_id),
        request_id=str(rc.get("requestId") or ""),
        workspace_id=workspace_id,
        workspace_name=workspace_name,
        role=role,
    )


def _assigned_to_me(row, ctx):
    """Is this task the CALLER's own work?

    Three signals, in descending strength, all of them already in the task
    model (section 9) — this adds no fourth concept of ownership:
      1. assignee_user_id == me — an explicit link to my account.
      2. assignee_contact_id == my own contact row.
      3. nobody is assigned at all — an unassigned task in MY workspace is
         mine by default; it is work I captured and nobody else can see it.
    A task assigned to SOMEONE ELSE is excluded even though I own the row:
    "my tasks" means my commitments, not everything I can read.
    """
    if row.get("assignee_user_id") == ctx.user_id:
        return True
    cid = str(row.get("assignee_contact_id") or "")
    if cid and cid == ctx.contact_id:
        return True
    if cid or row.get("assignee_user_id"):
        return False
    # No contact and no user link. A bare speaker/name string means the task
    # was attributed to someone in a meeting, so it is not unassigned.
    return not str(row.get("assignee_name")
                   or row.get("assignee_speaker_id") or "").strip()


def _ai_visible(row, ctx):
    """May the caller SEE this task? Creator OR assignee — nothing else.

    THE SAME predicate list_all_tasks._keep applies, and deliberately a
    DIFFERENT question from _assigned_to_me above:

      _ai_visible      "can I read this row" — the dashboard's universe.
      _assigned_to_me  "is this MY commitment" — a narrower slice of it.

    Conflating the two is what made the assistant disagree with the
    dashboard. A task Alice created and delegated to Bob is VISIBLE to Alice
    (her dashboard lists it) but is not HER commitment, so a tool that
    filtered on _assigned_to_me alone could never return it — not even when
    the user asked "what is due this week" while looking at it on screen.

    Every tool now filters on this for reachability and uses
    _assigned_to_me only to decide DEFAULT scope, so nothing the dashboard
    shows is unreachable to the assistant.
    """
    return bool(_is_task_creator(ctx.user_id, row)
                or _is_task_assignee(ctx.user_id, row))


def _ai_scope(rows, ctx, mine_only):
    """Apply the caller's requested scope to an already-visible row set.

    One place, so "mine" means the same thing in every tool. `mine_only`
    False keeps everything the caller can see — which is what a question
    about a task on their dashboard needs.
    """
    if not mine_only:
        return rows
    return [r for r in rows if _assigned_to_me(r, ctx)]


# ---------------------------------------------------------------------------
# Tool output shaping
# ---------------------------------------------------------------------------
def _ai_task_view(row, ctx, speaker_names=None):
    """What a task looks like TO THE MODEL.

    Deliberately narrower than _public_task_v2 (section 8: "do not expose
    unnecessary internal database fields"): no fingerprints, no notified_via,
    no legacy assignee shapes, no internal ids beyond the task id the user may
    legitimately be given. Fewer fields also means more tasks fit the TPM
    window.
    """
    pub = _public_task_v2(row, speaker_names)
    day = _ai_due_day(row)
    view = {
        "id": pub["id"],
        "title": pub["title"],
        "status": pub["status"],
        "priority": pub["priority"],
        # THE RESOLVED CALENDAR DAY, so the model can reason about it. It used
        # to be the raw stored value, which on an AI-extracted task is a
        # PHRASE ("Wednesday", "next week") — the model was then asked "what
        # is due this week" while holding a field it could not compare, and
        # would either ignore the task or invent a date for it.
        "due_date": day,
        "is_overdue": pub["is_overdue"],
        "assigned_to_me": _assigned_to_me(row, ctx),
        "source": "meeting" if row.get("source_recording_id") else "manual",
    }
    # What was SAID, kept alongside the resolved day and only when the two
    # differ. The user asked for "Wednesday" and may well say "Wednesday"
    # back; dropping the phrase would make the assistant's wording drift from
    # the deadline the user set. Never used for comparison — that is `day`.
    spoken = str(row.get("due_date") or "").strip()
    if spoken and spoken[:10] != day:
        view["due_date_spoken"] = spoken[:100]
    if not day and spoken:
        # A phrase nothing could place ("end of Q3"). Said plainly so the
        # model reports "no firm date" rather than treating it as undated.
        view["due_date_unplaceable"] = True
    if pub.get("description"):
        view["description"] = pub["description"][:500]
    # The display name only — never the assignee's contact id, which the model
    # has no use for and could only echo into an answer.
    name = (pub.get("assignee") or {}).get("name") or ""
    if name:
        view["assignee_name"] = name
    if pub.get("resolution_status") and \
            pub["resolution_status"] != RESOLUTION_NONE:
        view["assignee_resolution"] = pub["resolution_status"]
    # The evidence sentence from the transcript is the whole point of "what did
    # I commit to" (section 14) — it is what lets the assistant show WHY a task
    # exists instead of asserting it.
    if row.get("ai_evidence"):
        view["evidence"] = str(row["ai_evidence"])[:300]
    return view


def _ai_meeting_view(item):
    """A meeting as the model sees it: enough to name it and date it, never
    the transcript (which is what the per-meeting chat route is for)."""
    return {
        "recording_key": item.get("audio_s3_key", ""),
        "title": item.get("title", "") or "Untitled meeting",
        "date": item.get("recorded_at", "") or item.get("created_at", ""),
    }


def _tool_result(rows, key, **extra):
    """Uniform tool envelope: the rows, a count, and an explicit truncation
    flag. `count` always equals the number of rows actually returned — a count
    that disagreed with the list is exactly the kind of detail a model will
    confidently repeat."""
    shown = rows[:AI_TOOL_ROW_LIMIT]
    out = {key: shown, "count": len(shown)}
    if len(rows) > AI_TOOL_ROW_LIMIT:
        out["truncated"] = True
        out["note"] = (f"Showing the first {AI_TOOL_ROW_LIMIT} of "
                       f"{len(rows)}. Tell the user the list is partial.")
    out.update(extra)
    return out

# ---------------------------------------------------------------------------
# The tools themselves.
#
# Every one of them takes (ctx, **model_args). The context is NOT a model
# argument — it is closed over by the dispatcher from the verified session, so
# no amount of prompt injection can supply or override it. The **model_args a
# tool does accept are all NON-identity: dates, statuses, search text, a task
# id. Each is validated here before it reaches DynamoDB, and the ones that
# name an entity (task_id, recording_key) are ownership-checked against ctx
# rather than trusted (section 10).
# ---------------------------------------------------------------------------
class AIToolError(Exception):
    """A tool failure the MODEL is allowed to see and explain.

    Distinct from ApiError on purpose: an ApiError aborts the HTTP request,
    which is right for a REST route but wrong here — "that task does not
    exist" is information the assistant should relay, not a 404 for the whole
    conversation. The message is written to be safe to show a user, and says
    "not found" for both missing AND unauthorized, exactly as _owned_task does
    (never confirm another tenant's row exists by 403-ing it).
    """


def _ai_index_pages(index, field, value):
    """Every row on one GSI partition, page-bounded. Rows are NOT filtered
    here — the caller owns the authorization predicate, because an index is a
    lookup path and never an authorization decision."""
    rows, start = [], None
    for _ in range(_SEARCH_MAX_PAGES):
        kwargs = {
            "IndexName": index,
            "KeyConditionExpression": Key(field).eq(value),
            "ScanIndexForward": False,
        }
        if start:
            kwargs["ExclusiveStartKey"] = start
        res = _tasks.query(**kwargs)
        rows.extend(res.get("Items", []))
        start = res.get("LastEvaluatedKey")
        if not start:
            break
    return rows


def _ai_owner_tasks(ctx):
    """Every task this caller may SEE, newest first — created OR assigned.

    TWO PARTITIONS, deliberately. A task lives in the creator's partition
    (owner_user_id) but a task someone else created FOR this user lives in
    theirs, keyed by assignee_user_id. Reading only the owner-index is what
    used to make the assistant answer "you have no tasks" to a user whose
    entire workload had been delegated to them — while the Task Dashboard,
    which reads both (see list_all_tasks' extra_query), showed them all. The
    assistant and the dashboard must not disagree about what tasks exist.

    Every row is still re-checked with _is_task_creator / _is_task_assignee —
    the SAME visibility predicate list_all_tasks._keep applies, so the two
    read paths cannot drift on what "my task" means. Duplicates (a task the
    caller both created and owns) are collapsed on task_id.
    """
    rows = _ai_index_pages(TASKS_OWNER_INDEX, "owner_user_id", ctx.user_id)
    rows.extend(_ai_index_pages(TASKS_ASSIGNEE_USER_INDEX,
                                "assignee_user_id", ctx.user_id))

    out, seen = [], set()
    for row in rows:
        tid = str(row.get("task_id") or "")
        if not tid or tid in seen:
            continue
        if not (_is_task_creator(ctx.user_id, row)
                or _is_task_assignee(ctx.user_id, row)):
            continue
        # SCOPED TO THE ACTIVE WORKSPACE (Phase 2C). Authorization already
        # passed above — this is CONTEXT: asking the assistant about ABC
        # Realty must not answer with personal tasks, and vice versa.
        #
        # ONLY STAMPED ROWS ARE FILTERED. A DELEGATED task — created by
        # someone else and assigned to me — carries THEIR owner_user_id, so
        # resolving an unstamped row would place it in the CREATOR's personal
        # workspace and hide work that is legitimately mine. That was the
        # regression test_a_task_assigned_to_me_by_someone_else caught.
        #
        # So: a row that names its workspace must match the active one; a row
        # with no workspace_id predates Phase 2B and is treated as in-scope,
        # which is the same permissive reading every other unstamped-row path
        # takes. Authorization is unaffected either way — that was decided
        # above by creator/assignee.
        stamped = str(row.get("workspace_id") or "").strip()
        if stamped and stamped != ctx.workspace_id:
            continue
        # And in an ORGANISATION, an unstamped (personal-era) row is not part
        # of the organisation's work.
        if not stamped and ctx.is_organisation:
            continue
        seen.add(tid)
        out.append(row)
    return out


def _ai_render_tasks(rows, ctx):
    """Task rows -> model views, with ONE recording read per distinct meeting
    (the names cache), matching list_all_tasks' cost profile."""
    cache = {}
    return [_ai_task_view(
                r, ctx,
                _speaker_names_for_recording(r.get("source_recording_id"), cache))
            for r in rows]


def _ai_due_day(row):
    """The task's deadline as a comparable YYYY-MM-DD, or "".

    THE BUG THIS FIXES, and it is the whole reason this helper exists.

    A task carries TWO date fields, and they mean different things
    (see _new_task_row): `due_date` keeps WHAT WAS SAID — "Wednesday",
    "next week", "end of Q3" — and `due_date_normalized` is that phrase
    resolved to a calendar day. Every comparison is supposed to use the
    normalized one; the raw string exists so the record stays faithful.

    The AI tools were comparing the RAW value. So an AI-extracted task due
    "Wednesday" (normalized to 2026-09-03) passed the dashboard's due_before
    filter and failed the assistant's regex, and the user got a dashboard
    listing three upcoming tasks beside an assistant insisting there were
    none. That is the exact inconsistency this pass was opened for.

    The fallback order is IDENTICAL to list_all_tasks._keep — normalized
    first, raw second — so the two read paths cannot disagree again. The raw
    fallback matters for rows written before normalization existed and for
    manual tasks the picker already stores as a real day.
    """
    day = str((row or {}).get("due_date_normalized") or "").strip()[:10]
    if not day:
        day = str((row or {}).get("due_date") or "").strip()[:10]
    return day if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) else ""


def _ai_sort_by_due(rows):
    """Soonest due first, undated last. Undated tasks sort last rather than
    first because a task with no date is not urgent — putting it at the top of
    "what is due" would be actively misleading.

    Sorts on the RESOLVED day (_ai_due_day), not the raw string: sorting
    "Wednesday" against "2026-09-11" alphabetically put every spoken date in
    front of every real one, which is a silently wrong order rather than a
    visible error. A row with no placeable day sorts last, as before."""
    return sorted(rows, key=lambda r: (not _ai_due_day(r), _ai_due_day(r)))


def _ai_clean_status(raw):
    """A model-supplied status, or "" — never an ApiError.

    The REST layer 400s on a bad status because a client sent it and should be
    fixed. Here the "client" is a language model that may well say "done", so
    an unrecognized value is treated as no filter and the model sees the full
    list rather than an error it cannot act on.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    return _TASK_STATUS_ALIASES.get(text.casefold(), "")


def _ai_clean_date(raw, field):
    """A bare YYYY-MM-DD, validated. Anything else is rejected loudly.

    Dates are the one model-supplied argument where silently ignoring a bad
    value would be dangerous: "what is due before next Friday" answered
    without the filter returns EVERYTHING, and the model would present that as
    the answer to the narrower question.
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise AIToolError(f"{field} must be a date in YYYY-MM-DD form")
    try:
        datetime.fromisoformat(text)
    except ValueError:
        raise AIToolError(f"{field} is not a real date")
    return text


def _ai_today():
    return datetime.now(timezone.utc).date().isoformat()


# -- the six read tools -----------------------------------------------------
def tool_get_my_tasks(ctx, status="", include_assigned_to_others=False,
                      limit=None):
    """Open work that belongs to the signed-in user."""
    rows = [r for r in _ai_owner_tasks(ctx) if _ai_visible(r, ctx)]
    rows = _ai_scope(rows, ctx, mine_only=not include_assigned_to_others)
    want = _ai_clean_status(status)
    if want:
        rows = [r for r in rows if r.get("status") == want]
    else:
        # With no status asked for, "my tasks" means outstanding work. A
        # completed task is not something the user still has to do, and
        # including it would pad every answer with finished work.
        rows = [r for r in rows if r.get("status") not in TASK_TERMINAL_STATUSES]
    rows = _ai_sort_by_due(rows)
    if limit:
        rows = rows[:max(1, min(int(limit), AI_TOOL_ROW_LIMIT))]
    return _tool_result(_ai_render_tasks(rows, ctx), "tasks",
                        filter=("all_statuses" if want else "outstanding_only"))


def tool_get_overdue_tasks(ctx, include_assigned_to_others=True):
    """Past their due date and not finished. Overdue is COMPUTED from the
    clock by the same _is_overdue the REST layer uses, never read from a
    stored flag that nothing updates at midnight.

    Scope defaults to EVERYTHING THE CALLER CAN SEE, not just their own
    commitments. "What's overdue?" asked from a dashboard that is showing a
    late delegated task must include that task — the dashboard's own Overdue
    filter does (list_all_tasks applies no assignee narrowing unless
    assigned_to_me is passed), and an assistant that quietly answered a
    narrower question was the bug.
    """
    rows = [r for r in _ai_owner_tasks(ctx)
            if _ai_visible(r, ctx)
            and _is_overdue(r.get("due_date"), r.get("status"),
                            r.get("due_date_normalized", ""))]
    rows = _ai_scope(rows, ctx, mine_only=not include_assigned_to_others)
    rows = _ai_sort_by_due(rows)
    return _tool_result(_ai_render_tasks(rows, ctx), "tasks",
                        as_of=_ai_today())


def tool_get_upcoming_tasks(ctx, due_before="", days=7,
                            include_assigned_to_others=True):
    """Due between today and a horizon — "what do I need to finish this week".

    Already-overdue tasks are excluded: they are what get_overdue_tasks is
    for, and mixing them in makes "this week" quietly mean "this week plus
    everything I am already late on".

    THIS TOOL WAS THE REPORTED BUG, and it had two independent causes, both
    of which made it answer "no upcoming tasks" beside a dashboard listing
    several:

      1. IT COMPARED THE RAW `due_date`. An AI-extracted task due
         "Wednesday" stores the phrase in due_date and the resolved day in
         due_date_normalized. The raw phrase failed the YYYY-MM-DD check and
         the task was skipped — while the dashboard, which resolves through
         the normalized field, listed it. Now both go through _ai_due_day.
      2. IT NARROWED TO _assigned_to_me. A task the caller created and
         delegated is on their dashboard but was unreachable here. Scope now
         defaults to everything the caller can SEE.

    `days` counts from today INCLUSIVE, so days=7 answers "the next week"
    the way a person means it.
    """
    horizon = _ai_clean_date(due_before, "due_before")
    if not horizon:
        try:
            span = max(1, min(int(days), 365))
        except (TypeError, ValueError):
            span = 7
        horizon = (datetime.now(timezone.utc).date()
                   + timedelta(days=span)).isoformat()

    today = _ai_today()
    rows = []
    for r in _ai_owner_tasks(ctx):
        if not _ai_visible(r, ctx):
            continue
        if r.get("status") in TASK_TERMINAL_STATUSES:
            continue
        # The RESOLVED day, so a spoken deadline is a real deadline here
        # exactly as it is on the dashboard. A phrase that could not be
        # placed on a calendar ("end of Q3") yields "" and is left out
        # rather than guessed at — the one case where dropping is correct.
        day = _ai_due_day(r)
        if not day:
            continue
        if today <= day <= horizon:
            rows.append(r)
    rows = _ai_scope(rows, ctx, mine_only=not include_assigned_to_others)
    return _tool_result(_ai_render_tasks(_ai_sort_by_due(rows), ctx), "tasks",
                        window={"from": today, "to": horizon})


def _ai_visible_task(ctx, task_id, what="task_id"):
    """One task row this caller may SEE, or AIToolError. THE task gate.

    _visible_task is the SAME predicate GET /tasks/{id} enforces — creator OR
    assignee — and it answers 404 for everyone else, so a model that invents
    or is fed another tenant's task id learns nothing beyond "not found". Used
    by every tool that takes a task id, so there is exactly one place where a
    model-supplied id becomes a row.

    Why _visible_task rather than _owned_task (which this used to use): an
    assignee reading their OWN assigned task is the ordinary case now that
    _ai_owner_tasks returns both partitions. Listing a task and then 404-ing
    on its detail would be the assistant contradicting itself.
    """
    tid = str(task_id or "").strip()
    if not tid:
        raise AIToolError(f"{what} is required")
    try:
        return _visible_task(ctx.user_id, tid)
    except ApiError:
        raise AIToolError("No task with that id exists in your workspace.")


def tool_get_task(ctx, task_id=""):
    """One task by id — authorization-checked, never trusted from the model."""
    row = _ai_visible_task(ctx, task_id)

    view = _ai_task_view(
        row, ctx, _speaker_names_for_recording(row.get("source_recording_id")))
    out = {"task": view}

    # The meeting it came from, ownership-checked in its own right rather than
    # assumed from the task (defense in depth: the task pointing at it is not
    # by itself proof the caller may read that recording row).
    key = row.get("source_recording_id")
    if key:
        rec = _recordings.get_item(Key={"audio_s3_key": key}).get("Item")
        if ctx.owns_recording(rec):
            out["meeting"] = _ai_meeting_view(rec)
        elif _assignee_tasks_in_recording(ctx.user_id, key):
            # An ASSIGNEE may name the meeting their work came from — the same
            # provenance GET /tasks/{id} already returns to them. What they may
            # not do is read its transcript; get_meeting_context enforces that
            # separately, and this view carries no transcript content.
            out["meeting"] = _ai_meeting_view(rec)
            out["meeting_access"] = "assignee"
    return out


def tool_search_my_tasks(ctx, query="", status="", include_completed=False):
    """Substring search across the caller's own tasks.

    Applied AFTER the owner-scoped read, never as a query that could reach
    outside it — the same shape as the REST contact search.
    """
    needle = str(query or "").strip().casefold()
    if not needle:
        raise AIToolError("query is required")

    want = _ai_clean_status(status)
    rows = []
    for r in _ai_owner_tasks(ctx):
        # Search deliberately spans everything the caller can SEE rather than
        # only their own commitments: "find the task about the invoice" is a
        # lookup, and a delegated task is exactly the kind of thing someone
        # searches for. This is also why search already disagreed with
        # get_my_tasks before this pass — it never applied the narrowing the
        # other tools did. The re-check keeps the index honest either way.
        if not _ai_visible(r, ctx):
            continue
        if want and r.get("status") != want:
            continue
        if not include_completed and not want and \
                r.get("status") in TASK_TERMINAL_STATUSES:
            continue
        haystack = " ".join(str(r.get(f) or "") for f in
                            ("title", "description", "assignee_name",
                             "assignee_name_legacy", "ai_evidence")).casefold()
        if needle in haystack:
            rows.append(r)
    return _tool_result(_ai_render_tasks(_ai_sort_by_due(rows), ctx), "tasks",
                        query=str(query).strip()[:100])


def tool_get_tasks_from_meeting(ctx, recording_key="", meeting_query=""):
    """Tasks captured from ONE meeting — the "what did I commit to" tool.

    The meeting can be named either by its key or in words ("yesterday's
    standup"), and either way it is resolved against MEETINGS THE CALLER OWNS
    before any task is read. A key the caller does not own is "not found", the
    same answer _owned_recording gives.
    """
    key = str(recording_key or "").strip()
    if key:
        rec = _recordings.get_item(Key={"audio_s3_key": key}).get("Item")
        if not ctx.owns_recording(rec):
            raise AIToolError("No meeting with that id exists in your workspace.")
    else:
        rec = _ai_resolve_meeting(ctx, meeting_query)
        if not rec:
            raise AIToolError(
                "Could not find a meeting matching that. Ask the user which "
                "meeting they mean.")
        key = rec.get("audio_s3_key", "")

    # Meeting-index rows are re-checked against the owner before use, for the
    # same reason list_all_tasks does it: the index is a lookup path only.
    rows = [r for r in _tasks_for_recording(key)
            if r.get("owner_user_id") == ctx.user_id]
    names = _speaker_names_for_recording(key)
    views = [_ai_task_view(r, ctx, names) for r in _ai_sort_by_due(rows)]
    return _tool_result(views, "tasks", meeting=_ai_meeting_view(rec))


def _ai_recent_meetings(ctx, limit=40):
    """The recent meetings the caller may see IN THE ACTIVE WORKSPACE.

    Mirrors list_recordings exactly — including its workspace split — so the
    assistant sees precisely what MinuteX shows the same user on the same
    screen: no more, and no fewer.

    ORGANISATION: the workspace index, then _can_read_meeting per row with
    the role and devices resolved ONCE. An OWNER/MANAGER therefore sees the
    organisation's meetings and a plain MEMBER sees only their own — the same
    answer the REST list gives.

    PERSONAL: the original user-index + legacy device-index union, unchanged.
    A personal request can never surface an organisation row, because those
    carry an organisation workspace_id and _can_read_meeting rejects them for
    a caller acting personally.
    """
    seen, out = set(), []

    def _collect(items):
        for item in items:
            key = item.get("audio_s3_key", "")
            if not key or key in seen or _is_trashed(item):
                continue
            seen.add(key)
            out.append(item)

    if ctx.is_organisation:
        devices = ctx.devices
        res = _recordings.query(
            IndexName=RECORDINGS_WORKSPACE_INDEX,
            KeyConditionExpression=Key("workspace_id").eq(ctx.workspace_id),
            ScanIndexForward=False, Limit=limit)
        _collect(r for r in res.get("Items", [])
                 if _can_read_meeting(ctx.user_id, r, role=ctx.role,
                                      devices=devices))
    else:
        res = _recordings.query(
            IndexName=USER_INDEX,
            KeyConditionExpression=Key("user_id").eq(ctx.user_id),
            ScanIndexForward=False, Limit=limit)
        # Filtered so a PERSONAL request cannot surface a row that has since
        # been stamped into an organisation.
        _collect(r for r in res.get("Items", [])
                 if not workspace_schema.is_organisation_workspace_id(
                     _row_workspace_id(r)))

        for device_id in ctx.devices:
            res = _recordings.query(
                IndexName=DEVICE_INDEX,
                KeyConditionExpression=Key("device_id").eq(device_id),
                ScanIndexForward=False, Limit=limit)
            _collect(r for r in res.get("Items", [])
                     if ctx.owns_recording(r))

    out.sort(key=lambda r: str(r.get("recorded_at") or r.get("created_at") or ""),
             reverse=True)
    return out[:limit]


def _ai_resolve_meeting(ctx, query):
    """A meeting matching free text, or None. Owner-scoped by construction:
    it only ever looks at _ai_recent_meetings, which is already the caller's.

    Title match first, then relative-date words. "Latest"/"" falls back to the
    most recent meeting, which is what a bare "the meeting" almost always
    means.
    """
    meetings = _ai_recent_meetings(ctx)
    if not meetings:
        return None
    text = str(query or "").strip().casefold()
    if not text or text in ("latest", "last", "most recent", "the meeting"):
        return meetings[0]

    for item in meetings:
        if text in str(item.get("title") or "").casefold():
            return item

    today = datetime.now(timezone.utc).date()
    target = None
    if "yesterday" in text:
        target = (today - timedelta(days=1)).isoformat()
    elif "today" in text:
        target = today.isoformat()
    if target:
        for item in meetings:
            when = str(item.get("recorded_at") or item.get("created_at") or "")
            if when[:10] == target:
                return item
        return None

    # A bare date in the question ("the Aug 20 call" arrives as 2026-08-20
    # once the model normalizes it) is worth honoring.
    match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    if match:
        for item in meetings:
            when = str(item.get("recorded_at") or item.get("created_at") or "")
            if when[:10] == match.group(0):
                return item
    return None


def tool_list_my_meetings(ctx, limit=10):
    """Recent meetings — what the assistant needs to answer "which meeting?"
    and to turn a name into something get_tasks_from_meeting can use."""
    try:
        count = max(1, min(int(limit), AI_TOOL_ROW_LIMIT))
    except (TypeError, ValueError):
        count = 10
    rows = _ai_recent_meetings(ctx, limit=max(count, 20))[:count]
    return _tool_result([_ai_meeting_view(r) for r in rows], "meetings")


# ---------------------------------------------------------------------------
# HISTORICAL MEETING CONTEXT — the task's "why", from the transcript.
#
# This is the one tool that reads meeting CONTENT, and it is the only place
# the assistant can. Everything about its shape follows from two rules:
#
#   1. AUTHORIZE, THEN RETRIEVE — structurally, not by remembering to check.
#      A task id is resolved to a row the caller may see, that row names its
#      source_recording_id, and THAT recording is authorized on its own before
#      retrieve_meeting_context is handed the already-hydrated item. There is
#      deliberately no parameter that lets the model name a recording directly:
#      transcript access is reachable ONLY through a task the caller can see,
#      so an invented recording key has nowhere to go.
#
#   2. THE TRANSCRIPT IS HISTORY, NOT STATE. What the model gets back is
#      labelled as the discussion that PRODUCED the task. Current status,
#      assignee and deadline come from the Tasks table via the other tools and
#      always win — see the prompt rules in ASSISTANT_TASK_RULES.
#
# It reuses retrieve_meeting_context() outright — the same _ensure_index (lazy
# build, single-flight), _navigate (LLM pick, spread fallback) and
# evidence_from_segments the per-meeting chat uses. A second retrieval system
# would be a second thing to keep correct, and this one already handles the
# long-meeting case that motivated PageIndex in the first place.
# ---------------------------------------------------------------------------

# The retrieval tool's own share of the request budget. Deliberately smaller
# than ONDEMAND_DEADLINE_SECONDS: this runs INSIDE an agent hop that still has
# to spend a Groq call turning the result into an answer, so a retrieval that
# used the whole budget would strand the user with no reply at all.
AI_CONTEXT_DEADLINE_SECONDS = int(
    os.environ.get("AI_CONTEXT_DEADLINE_SECONDS", "9"))

# How much retrieved transcript one tool result may carry back to the model.
# The agent loop's context already holds the system prompt, the history and
# every earlier tool result, so this cannot be the whole transcript budget.
AI_CONTEXT_MAX_CHARS = int(os.environ.get("AI_CONTEXT_MAX_CHARS", "6000"))


def _ai_note_sources(ctx, meeting_id, sources):
    """Record retrieved evidence on the context, de-duplicated.

    The ONLY writer of ctx.sources, and it writes what RETRIEVAL returned —
    never anything the model said. That is what makes the `sources` array on
    the /ai/chat response safe to deep-link from: every id in it was produced
    by pageindex.evidence_from_segments over segments that actually exist in a
    meeting this caller is authorized to read.

    `meeting_id` is added here because the workspace assistant can answer
    across several meetings in one turn, so a bare segment id is ambiguous in
    a way the per-meeting chat's sources never are — the app needs to know
    WHICH transcript to open (spec section 6).
    """
    seen = {(s.get("meeting_id"), s.get("segment_id")) for s in ctx.sources}
    for src in sources or []:
        ident = (meeting_id, src.get("segment_id"))
        if not src.get("segment_id") or ident in seen:
            continue
        seen.add(ident)
        row = dict(src)
        row["meeting_id"] = meeting_id
        ctx.sources.append(row)


def _ai_readable_meeting_for_task(ctx, row):
    """(key, hydrated item) for the meeting behind a task, or AIToolError.

    The recording is authorized ON ITS OWN — `ctx.owns_recording` — rather
    than inferred from the task pointing at it. A task the caller can see is
    not by itself proof they may read that meeting's transcript, and this is
    exactly where those two rights diverge:

    AN ASSIGNEE IS REFUSED. A task assignee may read the meeting's NOTES
    (GET /recordings/shared-with-me pins transcript_enabled and audio_enabled
    OFF precisely so they cannot reach the raw record), so letting the AI hand
    them labelled transcript segments would route around that decision — the
    assistant must not be the weaker door. They get a clear "not available"
    instead, and every task tool still works for them.
    """
    key = str(row.get("source_recording_id") or "")
    if not key:
        raise AIToolError(
            "That task was created manually, so there is no meeting "
            "discussion behind it.")

    rec = _recordings.get_item(Key={"audio_s3_key": key}).get("Item")
    if not rec or _is_trashed(rec):
        raise AIToolError(
            "The meeting this task came from is no longer available.")
    if not ctx.owns_recording(rec):
        raise AIToolError(
            "You can see this task, but its meeting transcript belongs to "
            "the person who shared it with you and is not available here.")

    _require_transcript(rec)
    # Hydrated because retrieval needs the transcript text and the timestamps,
    # and a long transcript lives in S3 rather than on the row.
    return key, transcript_store.hydrate(_s3, BUCKET_NAME, rec)


def tool_get_meeting_context(ctx, task_id="", question=""):
    """The meeting discussion behind ONE task, retrieved through PageIndex.

    Answers "why was this task created", "what was discussed about it", "what
    decision led to this". Returns transcript extracts with their seg_N ids
    intact plus the structured evidence the app deep-links from.
    """
    row = _ai_visible_task(ctx, task_id)
    key, item = _ai_readable_meeting_for_task(ctx, row)

    # The question steers the PageIndex navigation. Falling back to the task's
    # own title (rather than to a generic "what was discussed") keeps the
    # retrieval pointed at this task even when the model forgets to pass one.
    ask = str(question or "").strip()[:MAX_AI_MESSAGE_CHARS]
    if not ask:
        ask = str(row.get("title") or "").strip() or "what was discussed"

    started = time.monotonic()
    try:
        # `system_prompt` sizes the retrieval budget. Passed as a string of the
        # right ORDER OF MAGNITUDE rather than the real agent prompt: the tool
        # result rides inside an agent conversation whose true overhead this
        # function cannot see, and AI_CONTEXT_MAX_CHARS below is the real cap.
        context, sources, metrics = retrieve_meeting_context(
            item, key, ask, prompts.ASSISTANT_SYSTEM)
    except groq_client.GroqError as err:
        # Retrieval is an OPTIMISATION over an answer that can still be given
        # from the task row. A Groq failure inside the tool must not fail the
        # whole conversation, so the model is told what happened and answers
        # from what it has.
        print(f"[warn] ai.tool get_meeting_context retrieval failed for "
              f"{ctx.user_id}: {err}")
        raise AIToolError(
            "The meeting discussion could not be retrieved just now. Answer "
            "from the task's own details and say the meeting history was "
            "unavailable.")

    # Trimmed from the END: retrieve_meeting_context orders its extract
    # best-first, so the tail is what matters least.
    excerpt = (context or "")[:AI_CONTEXT_MAX_CHARS]
    truncated = len(context or "") > AI_CONTEXT_MAX_CHARS

    _ai_note_sources(ctx, key, sources)

    print(f"[audit] ai.tool.meeting_context user={ctx.user_id} "
          f"task={row.get('task_id')} meeting_mode="
          f"{metrics.get('retrieval_mode')} segs="
          f"{metrics.get('retrieved_segments')} "
          f"ms={int((time.monotonic() - started) * 1000)} req={ctx.request_id}")

    return {
        # Named to say what it IS, so the model cannot mistake it for state.
        "historical_discussion": excerpt,
        "excerpt_truncated": truncated,
        "meeting": _ai_meeting_view(item),
        # The same shape the per-meeting chat's `sources` uses, plus the
        # meeting id — reusing ai_evidence_segment_ids' seg_N identity rather
        # than inventing a citation format (spec section 6).
        "evidence": [dict(s, meeting_id=key) for s in sources],
        "current_task_state": {
            # Repeated INSIDE the retrieval result on purpose: this is the
            # tool whose output most invites a model to describe stale
            # transcript talk as the task's present state, and the correction
            # is most effective next to the temptation.
            "status": row.get("status") or TASK_STATUS_OPEN,
            "due_date": row.get("due_date") or "",
            "priority": row.get("priority") or "Medium",
            "note": ("These fields are the task's CURRENT state from the "
                     "Tasks database. The discussion above is history and "
                     "must not be used to contradict them."),
        },
    }


# ---------------------------------------------------------------------------
# AI-ASSISTED TASK ACTIONS — proposed here, applied only by the user.
#
# THE RULE (spec section 8): the AI never silently mutates task state. It can
# only PROPOSE, and the mutation happens through the existing authorized
# PATCH /tasks/{task_id} after the user taps confirm.
#
# So this tool writes NOTHING. What it does instead is worth stating plainly,
# because a proposal that cannot be applied is a worse outcome than a refusal:
#
#   * it validates the change the way the real route would, using the SAME
#     _authorize_task_patch — so a proposal the user could not perform is
#     refused HERE, before they are offered a button that would 403;
#   * it returns the CURRENT value beside the proposed one, so the app can
#     render "Open -> Completed" rather than an unanchored assertion;
#   * it names the exact patch body the app should send, so the confirmation
#     step is a straight pass-through to the existing route rather than the
#     client re-deriving what the AI meant.
#
# There is deliberately no `assignee` field. Reassignment needs a resolved
# Contact (see _clean_assignee and resolve_task_assignee), and a model naming
# "Priya" is precisely the ambiguity the human resolution flow exists to
# settle — a proposal carrying an unresolved name would either be unapplyable
# or would invite the client to guess. Reassignment stays a UI flow.
# ---------------------------------------------------------------------------

# Fields the AI may propose, mapped to the PATCH body key the existing route
# expects. Named explicitly (not derived) for the same reason
# _TASK_CREATOR_ONLY_FIELDS is: a new writable field must be added on purpose.
_AI_PROPOSABLE_FIELDS = ("status", "due_date", "priority")


def tool_propose_task_change(ctx, task_id="", status="", due_date="",
                             priority="", reason=""):
    """Validate a proposed task change and hand it back for confirmation.

    NEVER writes. Returns the proposal, the current values it would replace,
    and the patch body the app sends to PATCH /tasks/{task_id} if the user
    confirms.
    """
    row = _ai_visible_task(ctx, task_id)

    proposed, patch = {}, {}
    want_status = _ai_clean_status(status)
    if str(status or "").strip() and not want_status:
        raise AIToolError(
            "That is not a task status. Use Open, In Progress, Completed or "
            "Cancelled.")
    if want_status:
        proposed["status"] = want_status
        patch["status"] = want_status

    raw_due = str(due_date or "").strip()
    if raw_due:
        # Validated with the same _ai_clean_date every date-taking tool uses,
        # so "next Friday" is refused here rather than becoming a deadline
        # nobody asked for.
        #
        # A DEADLINE IS NEVER CLEARED THROUGH A PROPOSAL. An omitted argument
        # and a deliberate "" are indistinguishable once they reach here, so
        # honouring "" as "remove the deadline" would let a model that simply
        # left the field out silently drop a date the user still needs. The
        # user can clear a deadline in the date picker, where the intent is
        # unambiguous.
        proposed["due_date"] = _ai_clean_date(raw_due, "due_date")
        patch["due"] = proposed["due_date"]

    want_priority = str(priority or "").strip()
    if want_priority:
        if want_priority not in TASK_PRIORITIES:
            raise AIToolError(
                "That is not a task priority. Use Low, Medium or High.")
        proposed["priority"] = want_priority
        patch["priority"] = want_priority

    if not proposed:
        raise AIToolError(
            "Nothing was proposed. Pass at least one of status, due_date or "
            "priority.")

    # THE AUTHORIZATION CHECK, against the same predicate the real PATCH
    # enforces and against the same body shape. An assignee proposing a
    # deadline change is refused now, so the app never renders a confirm
    # button whose only possible outcome is a 403.
    try:
        _authorize_task_patch(ctx.user_id, row, patch)
    except ApiError as err:
        raise AIToolError(
            f"You cannot make that change to this task: {err.message}")

    current = {"status": row.get("status") or TASK_STATUS_OPEN,
               "due_date": row.get("due_date") or "",
               "priority": row.get("priority") or "Medium"}

    print(f"[audit] ai.tool.proposal user={ctx.user_id} "
          f"task={row.get('task_id')} fields={sorted(proposed)} "
          f"req={ctx.request_id}")

    out = {
        # `applied: False` is stated rather than implied. A model reading this
        # result should have no way to conclude the change went through, and
        # the flag is what the app keys its confirmation UI off.
        "applied": False,
        "requires_user_confirmation": True,
        "task": {"id": row.get("task_id", ""),
                 "title": row.get("title", "")},
        "current": {k: current[k] for k in proposed},
        "proposed": proposed,
        "reason": str(reason or "").strip()[:300],
        # Exactly what the app must PATCH. Returned so the confirmation is a
        # pass-through to the existing authorized route, not a re-derivation.
        "confirm": {"method": "PATCH",
                    "path": f"/tasks/{row.get('task_id', '')}",
                    "body": patch},
        "note": ("Nothing has changed yet. Tell the user what you propose and "
                 "that they must confirm it."),
    }
    # Recorded for the RESPONSE, so the app renders a confirmation card from
    # the server-validated proposal rather than from the model's prose (which
    # may describe it loosely, or claim more than was proposed).
    if not any(p["task"]["id"] == out["task"]["id"]
               and p["proposed"] == out["proposed"] for p in ctx.proposals):
        ctx.proposals.append(out)
    return out


# ---------------------------------------------------------------------------
# Tool schemas — what the MODEL is told exists.
#
# Read the parameter lists closely: there is no user_id, contact_id,
# organization_id, owner or account field anywhere in them, and that is the
# single most important property of this section. The model cannot ask for
# another user's data because the function signature it can see has nowhere to
# put one. Identity is supplied by the dispatcher from the session; these
# schemas describe only WHAT to fetch, never WHOSE.
# ---------------------------------------------------------------------------
AI_TOOL_SCHEMAS = [
    {"type": "function", "function": {
        "name": "get_my_tasks",
        "description": (
            "The signed-in user's own outstanding tasks, soonest due first. "
            "Use for 'what are my tasks', 'what am I working on', 'what do I "
            "owe'. Returns only tasks assigned to the signed-in user unless "
            "include_assigned_to_others is true."),
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string",
                       "enum": ["Open", "In Progress", "Completed", "Cancelled"],
                       "description": "Only this status. Omit for all "
                                      "outstanding (non-completed) work."},
            "include_assigned_to_others": {
                "type": "boolean",
                "description": "Also include tasks in the user's workspace "
                               "that are assigned to other people. Use for "
                               "'all tasks', not for 'my tasks'."},
        }, "required": []}}},
    {"type": "function", "function": {
        "name": "get_overdue_tasks",
        "description": (
            "Tasks whose due date has passed and which are not finished. Use "
            "for 'what is overdue', 'what am I late on', 'what did I miss'. "
            "Includes tasks the user delegated to other people, matching what "
            "their Task dashboard shows; pass "
            "include_assigned_to_others=false to narrow it to only the work "
            "they owe themselves."),
        "parameters": {"type": "object", "properties": {
            "include_assigned_to_others": {
                "type": "boolean",
                "description": "Default true. Set false for only the tasks "
                               "assigned to the signed-in user."},
        }, "required": []}}},
    {"type": "function", "function": {
        "name": "get_upcoming_tasks",
        "description": (
            "Tasks due between today and a horizon. Use for 'what is due this "
            "week', 'what is coming up', 'what do I need to finish by "
            "Friday', 'show me my upcoming tasks'. Excludes already-overdue "
            "tasks (get_overdue_tasks covers those). Includes tasks the user "
            "delegated to other people, matching what their Task dashboard "
            "shows."),
        "parameters": {"type": "object", "properties": {
            "days": {"type": "integer",
                     "description": "Days ahead to look, counting today. "
                                    "Default 7."},
            "due_before": {"type": "string",
                           "description": "Explicit horizon as YYYY-MM-DD. "
                                          "Overrides days."},
            "include_assigned_to_others": {
                "type": "boolean",
                "description": "Default true. Set false for only the tasks "
                               "assigned to the signed-in user."},
        }, "required": []}}},
    {"type": "function", "function": {
        "name": "search_my_tasks",
        "description": (
            "Search the signed-in user's tasks by keyword — title, "
            "description, assignee name or the sentence the task came from. "
            "Use when the user names a topic, project or person."),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Words to search for."},
            "status": {"type": "string",
                       "enum": ["Open", "In Progress", "Completed", "Cancelled"],
                       "description": "Restrict to one status."},
            "include_completed": {"type": "boolean",
                                  "description": "Include finished tasks."},
        }, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "get_task",
        "description": (
            "Full detail for ONE task by its id, including the meeting it "
            "came from. Only call with an id returned by another tool."),
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "string",
                        "description": "Task id from an earlier tool result."},
        }, "required": ["task_id"]}}},
    {"type": "function", "function": {
        "name": "get_tasks_from_meeting",
        "description": (
            "Tasks captured from one meeting, with the evidence sentence "
            "behind each. Use for 'what did I commit to in yesterday's "
            "meeting' or 'what came out of the Acme call'. Name the meeting "
            "in meeting_query, or pass a recording_key from list_my_meetings."),
        "parameters": {"type": "object", "properties": {
            "meeting_query": {"type": "string",
                              "description": "How the user referred to the "
                                             "meeting: a title, 'yesterday', "
                                             "'latest', or a YYYY-MM-DD date."},
            "recording_key": {"type": "string",
                              "description": "Exact key from list_my_meetings."},
        }, "required": []}}},
    {"type": "function", "function": {
        "name": "list_my_meetings",
        "description": (
            "The signed-in user's recent meetings, newest first. Use to find "
            "which meeting the user means before asking for its tasks."),
        "parameters": {"type": "object", "properties": {
            "limit": {"type": "integer",
                      "description": "How many to return. Default 10."},
        }, "required": []}}},
    {"type": "function", "function": {
        "name": "get_meeting_context",
        "description": (
            "The MEETING DISCUSSION behind one task — why it was created, what "
            "was said about it, what decision produced it. Use for 'why does "
            "this task exist', 'what was discussed about this', 'what decision "
            "led to this', 'what happened with this task'. Searches the "
            "transcript of the meeting the task came from and returns the "
            "relevant sections with their [seg_N] ids. "
            "The discussion returned is HISTORY: it explains the task's "
            "origin and must never be used to state the task's current "
            "status, assignee or deadline — those come from get_task."),
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "string",
                        "description": "Task id from an earlier tool result."},
            "question": {"type": "string",
                         "description": "What to look for in the meeting, in "
                                        "the user's own words. Steers which "
                                        "sections are retrieved."},
        }, "required": ["task_id"]}}},
    {"type": "function", "function": {
        "name": "propose_task_change",
        "description": (
            "PROPOSE a change to one task for the user to confirm. This does "
            "NOT change anything — it returns a proposal the app shows the "
            "user, who then approves or rejects it. Use when the user asks you "
            "to complete, reschedule, reprioritize or reopen a task. Say in "
            "your reply what you have proposed and that it needs their "
            "confirmation. Never claim the change has been made."),
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "string",
                        "description": "Task id from an earlier tool result."},
            "status": {"type": "string",
                       "enum": ["Open", "In Progress", "Completed",
                                "Cancelled"],
                       "description": "Proposed new status."},
            "due_date": {"type": "string",
                         "description": "Proposed new deadline as YYYY-MM-DD. "
                                        "Omit unless the user asked to "
                                        "reschedule."},
            "priority": {"type": "string", "enum": ["Low", "Medium", "High"],
                         "description": "Proposed new priority."},
            "reason": {"type": "string",
                       "description": "One short sentence on why, shown to "
                                      "the user beside the proposal."},
        }, "required": ["task_id"]}}},
]

# name -> (implementation, accepted argument names). The dispatcher below is
# the ONLY caller, and it passes ctx positionally, so a tool can never be
# reached without one.
#
# The argument names are listed EXPLICITLY rather than read off the function
# signature. That keeps the set of things a model may influence visible in one
# place next to the schemas, and means adding a parameter to a tool never
# silently widens what the model can pass — the allowlist has to be edited on
# purpose.
AI_TOOLS = {
    "get_my_tasks": (tool_get_my_tasks,
                     ("status", "include_assigned_to_others", "limit")),
    "get_overdue_tasks": (tool_get_overdue_tasks,
                          ("include_assigned_to_others",)),
    "get_upcoming_tasks": (tool_get_upcoming_tasks,
                           ("due_before", "days",
                            "include_assigned_to_others")),
    "search_my_tasks": (tool_search_my_tasks,
                        ("query", "status", "include_completed")),
    "get_task": (tool_get_task, ("task_id",)),
    "get_tasks_from_meeting": (tool_get_tasks_from_meeting,
                               ("recording_key", "meeting_query")),
    "list_my_meetings": (tool_list_my_meetings, ("limit",)),
    # Reads meeting CONTENT, and the only tool that does. Note it takes a
    # task_id and NOT a recording key: transcript access is reachable only
    # through a task the caller can see, so there is no argument a model could
    # fill in to reach an arbitrary meeting's index.
    "get_meeting_context": (tool_get_meeting_context, ("task_id", "question")),
    # WRITES NOTHING. Named "propose_" rather than "update_"/"complete_" so
    # the read-only-tools assertion in the test suite keeps holding and so the
    # name itself cannot mislead a model into reporting a change as done.
    "propose_task_change": (tool_propose_task_change,
                            ("task_id", "status", "due_date", "priority",
                             "reason")),
}

# Arguments the model is NEVER allowed to set, whatever the schemas say. The
# schemas already omit them, so reaching this list means either a model
# hallucinated an identity parameter or something tried to inject one — both
# worth a log line, and neither worth honoring. Belt and braces on the single
# rule this whole section exists to enforce.
_AI_FORBIDDEN_ARGS = ("user_id", "owner_user_id", "contact_id", "assignee_user_id",
                      "organization_id", "org_id", "tenant_id", "account_id",
                      "email", "ctx", "context",
                      # Phase 2C. The active workspace now genuinely exists on
                      # AIContext, which makes it a real escalation target
                      # rather than a hypothetical one: a model that could
                      # name a workspace could ask for a colleague's. It
                      # arrives from the verified header and nowhere else.
                      "workspace_id", "workspace", "created_by", "role")


def _ai_dispatch(ctx, name, raw_args):
    """Run one tool call and return its JSON-serializable result.

    Never raises: a tool failure comes back as {"error": ...} so the model can
    tell the user it could not retrieve something, which is a far better
    outcome than a 500 that loses the whole conversation. The one thing that
    IS refused outright is an attempt to pass identity.
    """
    entry = AI_TOOLS.get(name)
    if entry is None:
        return {"error": f"unknown tool: {name}"}
    fn, allowed = entry

    args = raw_args if isinstance(raw_args, dict) else {}
    stripped = [k for k in args if k.lower() in _AI_FORBIDDEN_ARGS]
    if stripped:
        # Log the ATTEMPT (ids only, per the audit rules) and drop the keys.
        # Execution continues with the caller's real identity, so the model
        # gets its own data rather than an error it might narrate as someone
        # else's absence of data.
        print(f"[audit] ai.tool.identity_arg_rejected user={ctx.user_id} "
              f"tool={name} args={sorted(stripped)} req={ctx.request_id}")
        args = {k: v for k, v in args.items() if k.lower() not in _AI_FORBIDDEN_ARGS}

    # Unknown extras are dropped rather than passed through as **kwargs, which
    # would TypeError on a model that invents a parameter — a routine event.
    args = {k: v for k, v in args.items() if k in allowed}

    try:
        result = fn(ctx, **args)
        ok, count = True, result.get("count")
    except AIToolError as err:
        result, ok, count = {"error": str(err)}, False, None
    except ApiError as err:
        result, ok, count = {"error": err.message}, False, None
    except ClientError as err:
        print(f"[error] ai.tool {name} failed for {ctx.user_id}: {err}")
        result, ok, count = {"error": "That could not be retrieved."}, False, None

    # Section 18: ids, tool name, outcome and correlation id — never the
    # message text, never task titles, never anything from a transcript.
    print(f"[audit] ai.tool user={ctx.user_id} tool={name} "
          f"ok={ok} rows={count if count is not None else '-'} "
          f"req={ctx.request_id}")
    return result


# ---------------------------------------------------------------------------
# Session storage — the persistence layer for workspace chat.
#
# THE ONE RULE, and every function below follows from it: A STORED
# CONVERSATION IS NOT AN AUTHORIZATION GRANT.
#
# A session's turns are text the assistant produced from data the caller
# could read AT THE TIME. Access can be revoked afterwards — a task
# reassigned, a meeting trashed, a contact unlinked. So replaying a session
# must never re-expose that data as though the check still passed:
#
#   * a session is authorized on OWNERSHIP of the session, which only
#     controls who may read their own past messages;
#   * every NEW answer re-runs the tools, which re-authorize from scratch
#     against the live tables. History is never a data source for the model's
#     factual claims, only conversational context (spec section 11).
#
# That is why history rides in the message list as plain prose and no tool
# result is ever persisted or replayed: there is nothing in a stored session
# that a tool would trust.
# ---------------------------------------------------------------------------
def _session_title(message):
    """A session's title, derived from the FIRST user message.

    Deterministic and free (spec section 7): no extra Groq call for a label,
    which would double the cost of starting a conversation and could fail or
    return something odd. Cut on a word boundary where one is close enough,
    so the title reads as a phrase rather than a severed word.

    Bounded hard at MAX_SESSION_TITLE_CHARS, which is also what stops a
    title carrying meaningful content: 60 characters cannot hold a
    transcript excerpt.
    """
    text = " ".join(str(message or "").split())
    if not text:
        return "New conversation"
    if len(text) <= MAX_SESSION_TITLE_CHARS:
        return text
    cut = text[:MAX_SESSION_TITLE_CHARS]
    space = cut.rfind(" ")
    if space > MAX_SESSION_TITLE_CHARS // 2:
        cut = cut[:space]
    return cut.rstrip(" ,.;:!?-") + "…"


def _stored_turns(row):
    """The session's persisted turns, defensively.

    A row whose `turns` is missing or the wrong type reads as an empty
    conversation rather than raising: a corrupt session must degrade to "no
    history" and keep answering, never 500 the chat route.
    """
    turns = (row or {}).get("turns")
    return turns if isinstance(turns, list) else []


def _public_session(row, *, with_turns=False):
    """A session as the API returns it.

    The LIST shape is deliberately lightweight (spec section 6) — no turns at
    all, because a session list that carried every conversation's full text
    would be the most expensive read in the app and is never what a list
    needs. `with_turns` is the detail route's opt-in.
    """
    out = {
        "session_id": str((row or {}).get("session_id") or ""),
        "title": str((row or {}).get("title") or ""),
        "created_at": str((row or {}).get("created_at") or ""),
        "updated_at": str((row or {}).get("updated_at") or ""),
        # Stored rather than derived so the list route does not have to read
        # the turn array to count it. Kept in step by _append_turns, which is
        # the only writer.
        "message_count": int((row or {}).get("message_count") or 0),
        "last_message_preview": str(
            (row or {}).get("last_message_preview") or ""),
    }
    if with_turns:
        # CHRONOLOGICAL, oldest first — the order the screen renders and the
        # order the turns are stored in. Stated explicitly because a chat that
        # renders backwards is a subtle, confusing bug.
        out["turns"] = _stored_turns(row)
    return out


def _owned_session(user_id, session_id):
    """The session, if this caller owns it. 404 otherwise.

    THE AUTHORIZATION GATE for every session route, and shaped exactly like
    _owned_task / _owned_folder / _owned_contact so it reads the same at every
    call site.

    404 AND NEVER 403, matching the rest of this file: a 403 would confirm
    that a session id exists, which is precisely what an attacker enumerating
    ids wants to learn. A session belonging to someone else is
    indistinguishable from one that never existed.

    The user_id comes from the caller's JWT via _require_auth. There is no
    parameter here a client or a model could fill in to change whose session
    is loaded (spec section 12).
    """
    sid = str(session_id or "").strip()
    if not sid:
        raise ApiError(400, "session id required")
    # A malformed id is not found rather than a 400: the client did not
    # necessarily send it (the model may have), and "no such session" is the
    # honest answer either way.
    if len(sid) > 64:
        raise ApiError(404, "conversation not found")
    try:
        row = _chat_sessions.get_item(Key={"session_id": sid}).get("Item")
    except ClientError as err:
        # A read failure is NOT "not found" — treating it as a new session
        # would silently start a fresh conversation while the user's own one
        # still exists, and (worse) is the shape spec section 14 forbids:
        # never let a load failure look like an absent session.
        print(f"[error] ai.session read failed for {user_id}: {err}")
        raise ApiError(503, "Could not load that conversation. Please retry.")
    if not row or str(row.get("user_id") or "") != user_id:
        raise ApiError(404, "conversation not found")
    return row


def _new_session(user_id, first_message):
    """Create a session row for this caller. Returns the row.

    `user_id` is the AUTHENTICATED caller, passed in by the route from
    _require_auth — never read from a body field.
    """
    now = _now_iso()
    row = {
        "session_id": uuid.uuid4().hex[:16],
        "user_id": user_id,
        "title": _session_title(first_message),
        "created_at": now,
        "updated_at": now,
        "message_count": 0,
        "last_message_preview": "",
        "turns": [],
    }
    _chat_sessions.put_item(Item=row)
    return row


def _append_turns(row, user_message, reply):
    """Persist one exchange, trimmed. Returns the updated row.

    THE ONLY WRITER of `turns`, which is what makes the bound real rather
    than aspirational: it slices to MAX_SESSION_TURNS before every write, so
    there is no path that appends without trimming.

    Trimmed OLDEST-FIRST, matching the meeting chat. `message_count` keeps
    counting the whole conversation rather than the stored window — the user
    did send those messages, and a count that reset would be a lie; it is
    metadata, not an index into the array.

    WHAT IS NOT STORED, and each for a reason from spec section 4:
      * tool ARGUMENTS — they can name task and recording ids, and a stored
        id is a durable reference to data whose access may be revoked;
      * tool RESULTS — the same, in bulk, plus they are the model's inputs
        rather than the conversation;
      * sources / proposals — a proposal is a live offer validated against
        current permissions, so replaying a stale one would be offering a
        change the user may no longer be allowed to make;
      * anything about identity beyond the owning user_id.
    Only `role`, `content` and `at` are persisted.
    """
    now = _now_iso()
    turns = _stored_turns(row) + [
        {"role": "user", "content": str(user_message)[:MAX_AI_MESSAGE_CHARS],
         "at": now},
        {"role": "assistant", "content": str(reply)[:MAX_STORED_REPLY_CHARS],
         "at": now},
    ]
    turns = turns[-MAX_SESSION_TURNS:]

    count = int(row.get("message_count") or 0) + 2
    # A short, safe label for the session list. Bounded to a title's length
    # for the same reason: it must not become a place conversation content
    # accumulates.
    preview = " ".join(str(reply or "").split())[:MAX_SESSION_TITLE_CHARS]

    _chat_sessions.update_item(
        Key={"session_id": row["session_id"]},
        UpdateExpression=("SET turns = :t, updated_at = :now, "
                          "message_count = :c, last_message_preview = :p"),
        ExpressionAttributeValues={":t": turns, ":now": now, ":c": count,
                                   ":p": preview},
    )
    return {**row, "turns": turns, "updated_at": now,
            "message_count": count, "last_message_preview": preview}


def _session_history(row):
    """The stored conversation, narrowed to what GROQ should see.

    STORAGE AND CONTEXT ARE SEPARATE CONCERNS (spec section 8). A session
    holds up to MAX_SESSION_TURNS so the user can scroll back; the model gets
    AI_HISTORY_TURNS exchanges, because the rest would crowd out the answer
    and cost tokens for context the question does not need.

    Reuses _ai_clean_ai_history — the SAME validator applied to client-sent
    history — so a stored turn and a client turn cannot reach the model
    through different rules. That matters because these rows were written by
    this service but read back much later, possibly after a schema change.
    """
    return _ai_clean_ai_history(_stored_turns(row))


# ---------------------------------------------------------------------------
# The agent loop
# ---------------------------------------------------------------------------
def _ai_clean_ai_history(raw):
    """Validate client-supplied history — same rules as the meeting chat's
    _clean_history: only user/assistant, only strings, only the recent tail."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ApiError(400, "history must be an array of {role, content}")
    out = []
    for m in raw[-(AI_HISTORY_TURNS * 2):]:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "").strip().lower()
        content = str(m.get("content") or "").strip()[:MAX_AI_MESSAGE_CHARS]
        if role in ("user", "assistant") and content:
            out.append({"role": role, "content": content})
    return out


def _ai_run_agent(ctx, message, history):
    """Drive the model until it answers, executing tool calls in between.

    Returns (reply_text, tools_used). The loop is bounded by AI_MAX_TOOL_HOPS
    and by the same wall-clock deadline the rest of the on-demand AI uses, so
    it cannot outlive API Gateway's 29s ceiling; a model that keeps asking for
    tools past the budget is answered from what it has rather than left to be
    killed mid-call.
    """
    system = (prompts.ASSISTANT_SYSTEM
              + prompts.ASSISTANT_TASK_RULES + "\n"
              + prompts.assistant_identity(
                  display_name=ctx.display_name, email=ctx.email,
                  today=_ai_today(),
                  # Context only — the tools are already scoped and every row
                  # is re-checked. See the note in assistant_identity.
                  workspace_name=ctx.workspace_name,
                  workspace_type=(workspace_schema.TYPE_ORGANISATION
                                  if ctx.is_organisation
                                  else workspace_schema.TYPE_PERSONAL),
                  role=ctx.role))

    messages = [{"role": "system", "content": system}]
    messages.extend(history)
    messages.append({"role": "user", "content": message})

    deadline = time.monotonic() + ONDEMAND_DEADLINE_SECONDS
    used = []

    for hop in range(AI_MAX_TOOL_HOPS):
        # On the last hop the tools are withdrawn, which forces prose: left
        # available, a model can spend the final turn asking for another call
        # whose result nobody will ever read, and the user gets no answer.
        last = hop == AI_MAX_TOOL_HOPS - 1
        reply = groq_client.complete_with_tools(
            messages, [] if last else AI_TOOL_SCHEMAS,
            label="assistant", temperature=0.2, deadline=deadline)

        calls = reply.get("tool_calls") or []
        if not calls or last:
            return (reply.get("content") or "").strip(), used

        # The assistant turn must go back verbatim, tool_calls included:
        # a tool result with no matching call is a protocol error.
        messages.append({"role": "assistant",
                         "content": reply.get("content") or "",
                         "tool_calls": calls})

        for call in calls[:len(AI_TOOLS)]:
            fn = call.get("function") or {}
            name = str(fn.get("name") or "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except (ValueError, TypeError):
                args = {}
            result = _ai_dispatch(ctx, name, args)
            used.append(name)
            messages.append({
                "role": "tool",
                "tool_call_id": call.get("id", ""),
                "name": name,
                "content": json.dumps(result, default=str),
            })

        if time.monotonic() >= deadline:
            break

    # Out of hops or out of time with tool results in hand. Ask for the answer
    # with no tools attached rather than returning nothing.
    try:
        reply = groq_client.complete_with_tools(
            messages, [], label="assistant-final", temperature=0.2,
            deadline=time.monotonic() + 8)
        return (reply.get("content") or "").strip(), used
    except groq_client.GroqError:
        return "", used


def ai_chat(event):
    """POST /ai/chat {message, session_id?, history?}
       -> {session_id, reply, tools_used, sources, proposals}

    The workspace-wide assistant. Identity comes from the Authorization header
    via _ai_context, and any user_id or contact_id in the body is ignored
    outright (section 5/19).

    SESSIONS. The conversation is now PERSISTED, so leaving the screen no
    longer loses it:

      * no `session_id`      -> a new session is created and its id returned;
      * a `session_id`       -> authorized against the caller via
                                _owned_session, then continued;
      * someone else's id    -> 404, indistinguishable from one that never
                                existed.

    `history` is still ACCEPTED but is now a FALLBACK, not the source of
    truth. That ordering is what keeps the deployed app working: a client that
    sends only history (no session_id) gets exactly the behaviour it always
    had, plus a session id it is free to ignore. When a session IS named, the
    stored conversation wins — the server's copy is authoritative, and a
    client's replay of it could be stale or trimmed differently.

    `sources` and `proposals` are ADDITIVE keys. Both come from the CONTEXT,
    not from the model's prose:

      * `sources` is the transcript evidence retrieval actually read, each row
        carrying the meeting id the segment belongs to so the app can
        deep-link with the mechanism it already has
        (/recording/{key}/transcript?evidence=seg_N). A model cannot put an
        invented id here — only _ai_note_sources writes it.
      * `proposals` is the set of task changes the model drafted, each already
        validated and authorized server-side and each carrying the exact patch
        to send if the user confirms. NOTHING has been written; the app must
        ask before applying (section 8).

    An older app build ignores every added key and behaves exactly as before.
    """
    ctx = _ai_context(event)
    data = _body(event)

    message = str(data.get("message") or "").strip()
    if not message:
        raise ApiError(400, "message required")
    if len(message) > MAX_AI_MESSAGE_CHARS:
        raise ApiError(400,
                       f"message too long (max {MAX_AI_MESSAGE_CHARS} chars)")

    # AUTHORIZE THE SESSION BEFORE ANYTHING ELSE. Done here, before the Groq
    # call, so an unauthorized id costs one GetItem rather than a generation —
    # and so a 404 can never be confused with an answer.
    requested = str(data.get("session_id") or "").strip()
    session = _owned_session(ctx.user_id, requested) if requested else None

    if session is not None:
        # The STORED conversation wins over anything the client replayed.
        history = _session_history(session)
    else:
        history = _ai_clean_ai_history(data.get("history"))

    print(f"[audit] ai.chat user={ctx.user_id} "
          f"session={session['session_id'] if session else 'new'} "
          f"chars={len(message)} turns={len(history)} req={ctx.request_id}")

    try:
        reply, used = _ai_run_agent(ctx, message, history)
    except groq_client.GroqError as err:
        _groq_error(err, "a reply")

    if not reply:
        raise ApiError(502, "Unable to generate a reply. Please retry.")

    # Same boundary as the per-meeting chat: get_meeting_context hands the
    # model labelled transcript, so the ids are in front of it here too. The
    # structured `sources` below is where they legitimately live — it is
    # written by _ai_note_sources from retrieval, never from this prose.
    reply = ai_sanitize.sanitize_ai_user_output(
        reply, label=f"ai_chat:{ctx.user_id}")
    if not reply:
        raise ApiError(502, "Unable to generate a reply. Please retry.")

    # PERSISTENCE COMES AFTER A SUCCESSFUL ANSWER, and cannot fail it.
    #
    # Spec section 14, and the reason this block is a try/except rather than
    # part of the happy path: the model has already been paid for and the
    # answer is already correct. Turning that into a generic AI failure
    # because a DynamoDB write blipped would lose the user real work and
    # misattribute the fault. So a persistence failure is LOGGED, flagged on
    # the response, and the answer is returned anyway.
    #
    # `session_id` is empty in that case, which is deliberate and honest: the
    # client has nothing to resume, and telling it otherwise would have it
    # send an id that does not exist on the next turn.
    session_id, saved = "", True
    try:
        if session is None:
            session = _new_session(ctx.user_id, message)
        session = _append_turns(session, message, reply)
        session_id = session["session_id"]
    except ClientError as err:
        saved = False
        print(f"[error] ai.chat persist FAILED user={ctx.user_id} "
              f"session={requested or 'new'} req={ctx.request_id}: {err}")

    print(f"[ai_metrics] " + json.dumps({
        "evt": "ai_chat",
        "user_id": ctx.user_id,
        "session_id": session_id,
        # Counts and outcomes only — never the question, never the answer.
        # These logs are retained and broadly readable (section 17).
        "message_count": int((session or {}).get("message_count") or 0),
        "history_turns": len(history),
        "tools_used": used,
        "persistence_success": saved,
    }, default=str))

    body = {
        "session_id": session_id,
        "reply": reply,
        "tools_used": used,
        "sources": ctx.sources[:MAX_CHAT_SOURCES],
        "proposals": ctx.proposals,
    }
    if not saved:
        # Stated so the app can tell the user this turn was not kept, rather
        # than silently losing it from a conversation they will reopen later.
        body["persisted"] = False
    return _resp(200, body)


def ai_list_sessions(event):
    """GET /ai/chat/sessions?limit=&cursor= -> {sessions, count, next_cursor}

    The caller's conversations, most recently updated first.

    LIGHTWEIGHT BY DESIGN (spec section 6): no turns are returned. A list that
    carried every conversation's full text would be the most expensive read in
    the app, and a list never needs it — `title`, `message_count` and
    `last_message_preview` are what a row renders.

    Read off the user-index, whose hash key IS user_id, and then re-checked
    against the caller anyway. Not redundant paranoia — the same rule the
    Tasks routes document: an index is a lookup path, never an authorization
    decision.
    """
    user_id = _require_auth(event)
    qs = event.get("queryStringParameters") or {}
    limit = _clean_limit(qs.get("limit"), SESSIONS_PAGE_DEFAULT,
                         SESSIONS_PAGE_MAX)

    query = {
        "IndexName": CHAT_SESSIONS_USER_INDEX,
        "KeyConditionExpression": Key("user_id").eq(user_id),
        # updated_at is the index's range key, so "most recent first" is the
        # index's own order rather than a sort in memory.
        "ScanIndexForward": False,
        "Limit": limit,
    }
    cursor = _decode_cursor(qs.get("cursor"))
    if cursor:
        query["ExclusiveStartKey"] = cursor

    try:
        res = _chat_sessions.query(**query)
    except ClientError as err:
        # The GSI may not exist on an environment that has not run the
        # create-table script. Loud in the logs, and an empty list rather
        # than a 500 — the chat itself still works without a list.
        code = err.response.get("Error", {}).get("Code")
        if code in ("ValidationException", "ResourceNotFoundException"):
            print(f"[warn] {CHAT_SESSIONS_USER_INDEX} unavailable: {err}")
            return _resp(200, {"sessions": [], "count": 0, "next_cursor": ""})
        raise

    rows = [r for r in res.get("Items", [])
            if str(r.get("user_id") or "") == user_id]

    # DETERMINISTIC ORDER WITHIN A PAGE.
    #
    # updated_at is the index's range key, so DynamoDB already returns the
    # page newest-first — but two sessions CAN share a timestamp, and then
    # the order between them is whatever the index happens to give. That is
    # not theoretical: _now_iso() is evaluated per Lambda invocation and
    # returns the same microsecond for two writes in one request, and two
    # sessions touched in the same second are ordinary.
    #
    # Re-sorting the page (not the table — the query already bounded it)
    # makes the list stable, with session_id as the tie-break so the same
    # data always renders in the same order. A list that reshuffled on
    # refresh reads as a bug even when every row is correct.
    rows.sort(key=lambda r: (str(r.get("updated_at") or ""),
                             str(r.get("session_id") or "")),
              reverse=True)
    sessions = [_public_session(r) for r in rows]
    return _resp(200, {
        "sessions": sessions,
        "count": len(sessions),
        "next_cursor": _encode_cursor(res.get("LastEvaluatedKey")),
    })


def ai_get_session(event):
    """GET /ai/chat/sessions/{session_id} -> {session}

    One conversation and its persisted turns, oldest first.

    Authorized by _owned_session, which 404s for anyone else's id. Note what
    replaying this does NOT do: it re-runs no tools and re-reads no task, so
    a turn that once described Task A does not re-expose Task A. It is text
    the caller was already shown (spec section 11).
    """
    user_id = _require_auth(event)
    sid = (event.get("pathParameters") or {}).get("session_id", "")
    row = _owned_session(user_id, sid)
    return _resp(200, {"session": _public_session(row, with_turns=True)})


def ai_delete_session(event):
    """DELETE /ai/chat/sessions/{session_id} -> {deleted, session_id}

    HARD delete, matching every other delete route in this file (folders,
    contacts, tasks): the row goes, rather than gaining an `archived` flag
    the rest of the codebase has no convention for. One item holds the whole
    conversation, so there is no separate message store left orphaned — which
    is a direct benefit of the single-item design (spec section 6).

    Authorized first: a caller can only ever delete their own session, and
    someone else's id is a 404 that deletes nothing.
    """
    user_id = _require_auth(event)
    sid = (event.get("pathParameters") or {}).get("session_id", "")
    row = _owned_session(user_id, sid)

    _chat_sessions.delete_item(Key={"session_id": row["session_id"]})
    print(f"[audit] ai.session.deleted user={user_id} "
          f"session={row['session_id']}")
    return _resp(200, {"deleted": True, "session_id": row["session_id"]})


def ai_suggestions(event):
    """GET /ai/suggestions -> {suggestions}

    Starter prompts, served from the backend for the same reason the meeting
    chat serves its own: the catalogue belongs in one place, not hardcoded in
    the app. Authenticated so it cannot be used to probe the API anonymously.

    `task_id` narrows the catalogue to the per-task questions (spec section 4).
    The id is NOT authorized here and deliberately not read from the store —
    these are literal strings with nothing of the user's data in them, so
    there is nothing to leak; the tools authorize when the question is
    actually asked.
    """
    _require_auth(event)
    qs = event.get("queryStringParameters") or {}
    if str(qs.get("task_id") or "").strip():
        return _resp(200, {"suggestions": AI_TASK_SUGGESTIONS})
    return _resp(200, {"suggestions": AI_SUGGESTIONS})


# ---------------------------------------------------------------------------
# TASK INTELLIGENCE — the dashboard's structured AI, on its own route.
#
# WHY NOT /ai/chat. The dashboard does not want a paragraph; it wants rows it
# can key by task id, group under fixed headings and make tappable. Getting
# that out of the conversational agent would mean either parsing its prose
# (fragile) or teaching it to emit two output shapes (worse). Spec section 7
# allows a dedicated endpoint where it is cleaner, and this is that case: one
# Groq call, JSON mode, strict coercion, no tool loop.
#
# WHAT IT IS NOT. It is not a second source of truth for tasks. The response
# carries JUDGEMENTS keyed by task id and nothing else — no status, no
# assignee, no deadline (the schema has no field for them). The app merges
# these onto the rows it already loaded from GET /tasks, which stays
# authoritative. That is what keeps "AI recommendations must not overwrite
# task state" true by construction rather than by discipline.
#
# COST. One call over at most AI_INTEL_MAX_TASKS task views. The tasks are
# read through the SAME _ai_owner_tasks the tools use, so this route can see
# neither more nor less than the assistant can.
# ---------------------------------------------------------------------------

# How many tasks are sent for analysis. The binding constraint is the context
# window, not the row count: each view is a few hundred characters, so this is
# comfortably inside one call while still covering a real backlog. Tasks are
# sent MOST-URGENT-FIRST, so a user with more than this loses the least
# interesting tail rather than a random slice.
AI_INTEL_MAX_TASKS = int(os.environ.get("AI_INTEL_MAX_TASKS", "60"))


def _ai_intel_task_view(row, ctx, speaker_names=None):
    """A task as the INTELLIGENCE prompt sees it.

    Narrower than _ai_task_view and shaped by one question: what does a model
    need in order to judge urgency, staleness and duplication? Dates and text,
    essentially. `created_at` is included here (the chat view omits it)
    because "open since March" is the only evidence for staleness there is.
    """
    pub = _public_task_v2(row, speaker_names)
    view = {
        "id": pub["id"],
        "title": pub["title"],
        "status": pub["status"],
        "priority": pub["priority"],
        # The RESOLVED day, for the same reason _ai_task_view uses it: a model
        # asked to judge urgency cannot compare "Wednesday" to today.
        "due_date": _ai_due_day(row),
        "is_overdue": pub["is_overdue"],
        "created_at": str(pub.get("created_at") or "")[:10],
        "assigned_to_me": _assigned_to_me(row, ctx),
        "source": "meeting" if row.get("source_recording_id") else "manual",
        "needs_review": pub["needs_review"],
    }
    if pub.get("description"):
        view["description"] = pub["description"][:300]
    name = (pub.get("assignee") or {}).get("name") or ""
    if name:
        view["assignee_name"] = name
    if row.get("ai_evidence"):
        view["evidence"] = str(row["ai_evidence"])[:200]
    return view


def ai_task_intelligence(event):
    """POST /ai/task-intelligence -> {summary, rows, analyzed, partial}

    Structured judgements over the caller's own open tasks, for the dashboard.
    Recommendations only: every row is a (task_id, kind, reason,
    recommendation) and the app merges them onto task rows it already has.
    """
    ctx = _ai_context(event)

    # Everything the caller can SEE and has not finished — the same universe
    # the dashboard renders, so an analysis cannot omit a task the user is
    # looking at while it runs.
    rows = [r for r in _ai_owner_tasks(ctx)
            if _ai_visible(r, ctx)
            and r.get("status") not in TASK_TERMINAL_STATUSES]
    # Most-urgent-first, so a truncated list keeps the tasks worth judging.
    rows = _ai_sort_by_due(rows)
    total = len(rows)
    rows = rows[:AI_INTEL_MAX_TASKS]

    if not rows:
        # Nothing open. Answered WITHOUT a Groq call: there is nothing to
        # judge, and paying for a model to say so would be both slower and an
        # invitation to invent something.
        return _resp(200, {"summary": "", "rows": [], "analyzed": 0,
                           "partial": False})

    cache = {}
    views = [_ai_intel_task_view(
                r, ctx,
                _speaker_names_for_recording(r.get("source_recording_id"), cache))
             for r in rows]

    # TRIM TO WHAT ACTUALLY FITS ONE CALL.
    #
    # AI_INTEL_MAX_TASKS bounds the COUNT, but a task view's size varies a
    # lot — a long title, a 300-char description and an evidence sentence is
    # an order of magnitude bigger than a bare title. 60 fat rows measured
    # ~15.6k tokens, which is over the 12k TPM a default-configured account
    # has, and a 429 that cannot be retried inside the request deadline
    # surfaces to the user as "Could not analyze your tasks".
    #
    # So the batch is cut to the SAME budget the rest of the on-demand AI
    # uses (single_pass_budget_chars, which accounts for the system prompt
    # and the reply reserve). Dropped from the END, after the urgency sort,
    # so what survives is the work most worth judging.
    budget = groq_client.single_pass_budget_chars(
        prompts.TASK_INTELLIGENCE_SYSTEM)
    while len(views) > 1 and len(
            prompts.task_intelligence_request(views, today=_ai_today())) > budget:
        views.pop()

    valid_ids = {v["id"] for v in views}

    print(f"[audit] ai.task_intelligence user={ctx.user_id} "
          f"tasks={len(views)} of={total} req={ctx.request_id}")

    try:
        raw = groq_client.complete_json(
            prompts.TASK_INTELLIGENCE_SYSTEM,
            prompts.task_intelligence_request(views, today=_ai_today()),
            label="task-intelligence", temperature=0.2,
            deadline=time.monotonic() + ONDEMAND_DEADLINE_SECONDS)
    except groq_client.GroqError as err:
        _groq_error(err, "task intelligence")

    # Coerced against the ids ACTUALLY SENT, so a judgement about a task the
    # model invented is dropped rather than rendered as a card that goes
    # nowhere. `valid_ids=None` for segments: these rows may cite evidence
    # from any of several meetings, so membership cannot be checked against
    # one transcript here — the shape still is, and the app resolves a
    # segment id against the meeting it opens.
    result = ai_schema.coerce_task_intelligence(raw, valid_task_ids=valid_ids)

    return _resp(200, {
        "summary": result["summary"],
        "rows": result["rows"],
        "analyzed": len(views),
        # Says so when the user has more open tasks than were analysed —
        # whether they were cut by the COUNT cap or by the token budget above.
        # Same reason _tool_result stamps `truncated`: a partial analysis the
        # UI presents as complete is a claim the data does not support.
        "partial": total > len(views),
    })


# ===========================================================================
# MEETING SHARE — a read-only public link to one meeting.
#
# The product goal is the one every meeting-notes tool has: send someone a URL
# and they read the notes on their phone, with no account, no app and no
# login. That requirement is what makes this the THIRD unauthenticated route
# in this file (after /crm/salesforce/callback and the ElevenLabs STT
# webhook), and it is worth being explicit about what replaces the JWT.
#
# THE TOKEN IS THE CREDENTIAL. Possession of a 256-bit URL-safe token IS the
# authorization — there is no identity behind it to check. Everything else
# follows from that:
#
#   * Only sha256(token) is stored. A dump of the Shares table yields no
#     working links, and the raw token exists exactly once, in the create
#     response. This is why there is no "resend link" route: the server
#     genuinely cannot reconstruct one. Revoke and re-share instead.
#   * The token is NEVER logged. share_schema.redact_token() produces the
#     hash prefix for the one place a log line is useful.
#   * The public route reads the SHARES table first and the recording second,
#     so an unknown token costs one indexed lookup and never touches the
#     recording row.
#   * The public payload is ASSEMBLED (share_schema.public_payload), never a
#     stripped-down recording row — see that module's docstring for why the
#     direction matters.
#
# WHAT THIS DOES NOT CHANGE. Every authenticated route keeps the exact
# ownership rule it had: _owned_recording / get_recording are untouched, and a
# share confers no authenticated access to anything. Sharing is purely
# additive — a recording with no shares behaves precisely as before.
#
# STORAGE. One table, Shares, PK share_id, with a token-index GSI on
# token_hash because the public route arrives holding only the token, and a
# recording-index GSI so the owner's list is a query rather than a scan.
# ===========================================================================
SHARES_TABLE = os.environ.get("SHARES_TABLE", "Shares")
SHARES_TOKEN_INDEX = os.environ.get("SHARES_TOKEN_INDEX", "token-index")
SHARES_RECORDING_INDEX = os.environ.get("SHARES_RECORDING_INDEX",
                                        "recording-index")
# Where the public page lives. An env var rather than a constant so the link
# can move to a custom domain later without touching this code — the route
# path (/share/{token}) stays identical either way.
SHARE_BASE_URL = os.environ.get("SHARE_BASE_URL", "")
# A ceiling on live links per recording. Not a licensing limit — it stops a
# runaway client (or a user tapping Create repeatedly) from filling the table
# with links nobody can enumerate afterwards.
MAX_SHARES_PER_RECORDING = int(os.environ.get("MAX_SHARES_PER_RECORDING", "20"))
MAX_SHARE_TTL_DAYS = int(os.environ.get("MAX_SHARE_TTL_DAYS", "365"))

_shares = _ddb.Table(SHARES_TABLE)


def _share_base_url(event):
    """The origin the public link should use.

    Prefers SHARE_BASE_URL; falls back to the API Gateway host the request
    arrived on, so a fresh deploy produces working links before anyone sets
    the variable. The Host header is only ever used to BUILD a link shown to
    the authenticated owner — never to make an authorization decision — so a
    spoofed Host cannot grant access to anything.
    """
    if SHARE_BASE_URL:
        return SHARE_BASE_URL.rstrip("/")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    host = headers.get("host") or ""
    stage = ((event.get("requestContext") or {}).get("stage") or "")
    if not host:
        return ""
    base = f"https://{host}"
    # HTTP API's implicit "$default" stage is not part of the URL; a named
    # stage is.
    if stage and stage != "$default":
        base = f"{base}/{stage}"
    return base


def _html_resp(status, body):
    """An HTML response, with the cache posture a private page needs.

    _resp() answers JSON for every other route in this file; the share page is
    the one place that must return text/html, so it gets its own builder
    rather than a mode flag on _resp.

    Cache-Control is `no-store` on purpose. A shared meeting can be revoked,
    and a page a CDN or a phone browser kept would outlive the revocation — so
    the one thing this response must never be is durably cached. It also keeps
    meeting content out of shared/proxy caches on whatever network the
    recipient happens to be using.
    """
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "text/html; charset=utf-8",
            "Cache-Control": "no-store, no-cache, must-revalidate, private",
            "Pragma": "no-cache",
            "X-Robots-Tag": "noindex, nofollow, noarchive",
            # Deny-by-default, then the four things this page genuinely
            # needs. Each entry is here for a stated reason; anything not
            # listed is blocked, which is the point.
            #
            #   style-src   'unsafe-inline' for the inline <style> block, plus
            #               fonts.googleapis.com for the app's own two
            #               families (theme.tsx FONT) — the shared page is
            #               typographically the SAME product, not a lookalike.
            #   font-src    fonts.gstatic.com, where that stylesheet's @font-face
            #               rules actually fetch the files from.
            #   script-src  'unsafe-inline' for the ~14-line tab switcher. It
            #               is the only script on the page and it touches
            #               nothing but a class name and `hidden`. A nonce
            #               would be stricter, but with no other script source
            #               permitted there is nothing for an injected tag to
            #               do — and every interpolation is html-escaped.
            #   media-src   https:, so the <audio> element can follow the
            #               gateway's 302 to the presigned S3 URL.
            "Content-Security-Policy": (
                "default-src 'none'; "
                "style-src 'unsafe-inline' https://fonts.googleapis.com; "
                "font-src https://fonts.gstatic.com; "
                "script-src 'unsafe-inline'; "
                "img-src 'self' data:; media-src https:; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
        "body": body,
    }


def _share_by_id(share_id, user_id):
    """One share the caller owns, or 404.

    Same 404-not-403 rule the recording routes use: a share_id belonging to
    another user is reported as missing, so the endpoint cannot confirm that
    an id exists.
    """
    if not share_id:
        raise ApiError(400, "share id required")
    item = _shares.get_item(Key={"share_id": share_id}).get("Item")
    if not item or item.get("owner_id") != user_id:
        raise ApiError(404, "share not found")
    return item


def _shares_for_recording(key):
    res = _shares.query(
        IndexName=SHARES_RECORDING_INDEX,
        KeyConditionExpression=Key("recording_key").eq(key),
    )
    return res.get("Items", [])


def _parse_expires_at(raw):
    """The requested expiry -> a stored ISO string, or "" for never.

    Accepts an ISO timestamp or a number of days, because the app's picker
    offers durations while an API caller more naturally sends an instant.
    """
    if raw is None or raw == "":
        return ""
    if isinstance(raw, bool):
        raise ApiError(400, "expires_at must be a timestamp, a number of "
                            "days, or null")
    if isinstance(raw, (int, float, Decimal)):
        days = float(raw)
        if days <= 0:
            raise ApiError(400, "expiry must be greater than zero days")
        if days > MAX_SHARE_TTL_DAYS:
            raise ApiError(400, f"expiry cannot exceed {MAX_SHARE_TTL_DAYS} days")
        at = datetime.now(timezone.utc) + timedelta(days=days)
        return at.isoformat().replace("+00:00", "Z")
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return ""
        try:
            at = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            raise ApiError(400, "expires_at must be an ISO-8601 timestamp")
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        if at <= now:
            raise ApiError(400, "expires_at must be in the future")
        if at > now + timedelta(days=MAX_SHARE_TTL_DAYS):
            raise ApiError(400, f"expiry cannot exceed {MAX_SHARE_TTL_DAYS} days")
        return at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    raise ApiError(400, "expires_at must be a timestamp, a number of days, or null")


def _shared_mom(user_id, key, item):
    """The MoM structure the public page renders from.

    Prefers the STORED structure, so the shared page shows exactly what the
    owner edited in the MoM editor. A recording that has never had a MoM
    generated falls back to building the sections on the fly — a read-only
    build, never persisted, because a viewer opening a link must not cause a
    write to the owner's meeting.
    """
    stored = _stored_mom(item)
    if stored is not None:
        return mom_schema.coerce_mom(stored)
    mom = mom_schema.empty_mom()
    try:
        mom["sections"] = _build_fresh_sections(user_id, key, item)
    except Exception as e:  # noqa: BLE001
        # A share that renders the header and nothing else beats a 500. The
        # recording key is safe to log; the token never appears here.
        print(f"[share] section build failed for {key}: {type(e).__name__}: {e}")
        mom["sections"] = []
    return mom


# ===========================================================================
# ASSIGNEE MEETING ACCESS — read-only notes for the person doing the work.
#
# THE PROBLEM. A task extracted from a meeting arrives in the assignee's list
# with a title, a deadline and nothing else. get_task already sends the
# meeting's TITLE and DATE as provenance, but a title is not context:
# "Prepare the quotation by Friday" does not say which vendor, what was
# agreed, or what the number is supposed to cover. The assignee had to go and
# ask the person who recorded the meeting, which is exactly the coordination
# this product exists to remove.
#
# THE GRANT IS DERIVED, NOT STORED. There is no grants table and no share row
# behind this. Access is recomputed on every request from one question: does
# this caller currently assignee-own a task whose source_recording_id is this
# key? Three consequences, all of them the reason for the design:
#
#   * REASSIGNMENT REVOKES INSTANTLY. Moving the task to someone else rewrites
#     assignee_user_id, so the next request from the old assignee finds no
#     task and 404s. A stored grant would have to be swept, and the day the
#     sweep is forgotten is the day a former assignee still reads the meeting.
#   * DELETING THE TASK REVOKES TOO, for the same reason and for free.
#   * NOTHING TO REVOKE BY HAND, so no owner-facing revoke UI has to exist
#     for the feature to be safe.
#
# WHAT COMES OUT. share_schema.public_payload() — the SAME assembler the
# public share page uses, deliberately, rather than a second payload builder
# for this route. That module's contract is that it can only emit the keys it
# explicitly writes, so no future attribute on the recording row
# (owner_user_id, device_id, the raw S3 key, CRM records, chat history,
# folder membership) can reach an assignee by being forgotten here. A
# parallel builder would be a deny-list by another name and would drift from
# the share page within one release.
#
# NOTES ONLY. The synthetic config below pins transcript_enabled and
# audio_enabled to FALSE — they are not toggles, and no request field can
# turn them on. This route is therefore strictly narrower than a public share
# link the owner could create anyway, and it never mints a presigned URL:
# the audio gateway (/share/{token}/audio) has no counterpart here, because
# there is no token and no audio in the payload to point at.
#
# 404, NEVER 403, matching _owned_recording and _owned_task: a caller with no
# task in this meeting must not learn that the meeting exists.
# ===========================================================================

def _assignee_share_config():
    """The synthetic share config an assignee's meeting view renders under.

    Not stored and never user-supplied: the FIXED visibility of this route.
    Built through coerce_config so it is validated by the same code path as a
    real share and picks up any future toggle at its default, rather than
    being a hand-rolled dict that silently lacks the new key.

    transcript/audio are pinned OFF *after* coercion, so a change to the
    module defaults can widen a public share without widening this.
    """
    config = share_schema.coerce_config({})
    config["transcript_enabled"] = False
    config["audio_enabled"] = False
    return config


def _assignee_tasks_in_recording(user_id, key):
    """This caller's tasks sourced from this meeting. [] when there are none.

    Queried on assignee-user-index — the ACCOUNT, not assignee_contact_id: a
    contact is an address-book row, not an identity that can authenticate,
    the same distinction the whole task permission model rests on.

    The index is keyed by assignee alone, so every row is still re-checked
    against BOTH the caller and the recording key. An index is a lookup path,
    never an authorization decision.
    """
    if not user_id or not key:
        return []
    try:
        res = _tasks.query(
            IndexName=TASKS_ASSIGNEE_USER_INDEX,
            KeyConditionExpression=Key("assignee_user_id").eq(user_id),
        )
    except ClientError as err:
        print(f"[assignee-view] task lookup failed for {key}: "
              f"{type(err).__name__}: {err}")
        return []
    return [row for row in res.get("Items", [])
            if str(row.get("source_recording_id") or "") == key
            and _is_task_assignee(user_id, row)]


def _assignee_readable_recording(event):
    """(user_id, key, item) when the caller may READ this meeting's notes.

    The assignee counterpart to _owned_recording, deliberately shaped like it
    so the two read the same at every call site. 404 for a caller with no
    task here, for a meeting that does not exist, and for one in the Trash —
    a trashed meeting stops being readable through a task exactly as it stops
    being readable through a share link.

    The OWNER is allowed through too: they can already open the meeting
    properly, and rejecting them would make the route lie about a recording
    the caller demonstrably owns.
    """
    user_id = _require_auth(event)
    key = _url_unquote((event.get("pathParameters") or {}).get("key", ""))
    if not key:
        raise ApiError(400, "recording key required")

    item = _recordings.get_item(Key={"audio_s3_key": key}).get("Item")
    if not item or _is_trashed(item):
        raise ApiError(404, "recording not found")

    # Routed through the ONE authoritative predicate rather than repeating a
    # creator test (P1 remediation): this now admits an organisation
    # OWNER/MANAGER — who could already open the meeting through
    # get_recording, so 404-ing them here made the API inconsistent — and it
    # DENIES a member removed from the owning organisation.
    #
    # The assignee fallback is preserved exactly: a task assignee who is not
    # a workspace member still reads the meeting their work came from, which
    # is the Phase-1 behaviour this route exists for.
    if not _can_read_meeting(user_id, item) \
            and not _assignee_tasks_in_recording(user_id, key):
        raise ApiError(404, "recording not found")
    return user_id, key, item


def get_assignee_meeting(event):
    """GET /recordings/shared-with-me/{key+} -> {meeting, access}

    The read-only meeting an ASSIGNEE opens from their task. Notes only: no
    transcript, no audio, no presigned URL — and no route here can write.

    The MoM is resolved against the recording's OWNER, not the caller. The
    stored structure and its fresh-build fallback both belong to the owner's
    meeting, and building them as the assignee would produce an empty
    document. That is a read of the owner's content on the assignee's behalf,
    which is the point of the route; what it can EMIT is still bounded by
    public_payload and the pinned-off toggles above.
    """
    user_id, key, item = _assignee_readable_recording(event)

    # Hydrated because the MoM fallback builds its sections from the
    # transcript. The transcript still cannot LEAVE: transcript_enabled is
    # False, so public_payload never copies it into the response.
    item = transcript_store.hydrate(_s3, BUCKET_NAME, item)
    owner_id = str(item.get("user_id") or "")
    config = _assignee_share_config()
    mom = _shared_mom(owner_id, key, item)

    # audio_url is not merely omitted from the response — it is never
    # GENERATED. A presign is a bearer credential, and minting one the payload
    # then drops would still put it in this Lambda's memory and in any future
    # log line. Hence audio_url=None here, on top of audio_enabled being False.
    meeting = share_schema.public_payload(item, mom, config, audio_url=None)
    return _resp(200, {
        "meeting": meeting,
        # Lets the app pick the right screen without re-deriving the rule.
        # Enforcement stays here regardless of what any client does.
        "access": "owner" if owner_id == user_id else "assignee",
    })


def create_share(event):
    """POST /recordings/share/{key+} -> {share_id, url, expires_at, share}

    The ONLY place a raw token exists. It is returned once and then forgotten:
    only its hash is written, so this response is the user's single
    opportunity to capture the link.
    """
    user_id, key, item = _owned_recording(event, hydrate=False)
    body = _body(event)

    active = [s for s in _shares_for_recording(key) if share_schema.is_active(s)]
    if len(active) >= MAX_SHARES_PER_RECORDING:
        raise ApiError(409, "this meeting already has the maximum number of "
                            "active share links; revoke one first")

    config = share_schema.coerce_config(body)
    expires_at = _parse_expires_at(
        body.get("expires_at", body.get("expires_in_days")))

    token = share_schema.new_token()
    now = _now_iso()
    share = {
        "share_id": f"shr_{uuid.uuid4().hex}",
        "recording_key": key,
        "owner_id": user_id,
        "token_hash": share_schema.hash_token(token),
        "access_type": share_schema.ACCESS_PUBLIC,
        "expires_at": expires_at,
        "revoked_at": "",
        "created_at": now,
        "updated_at": now,
        "view_count": 0,
        "last_viewed_at": "",
        # A denormalised copy so the owner's list screen reads without a
        # second lookup. Nothing reads it for authorization.
        "recording_title": (item.get("title") or "").strip()[:200],
    }
    share.update(config)

    _shares.put_item(Item=share,
                     ConditionExpression="attribute_not_exists(share_id)")

    # AFTER the share exists. The owner is both actor and recipient here, and
    # that is deliberate rather than an oversight of the never-notify-the-actor
    # rule: a share link is a durable thing that stays live until revoked, and
    # the notification is the record of "this meeting is exposed by a link" —
    # which is worth being able to find later, unlike a transient action. It is
    # the one event site that passes no actor_user_id, for that reason.
    _notify_meeting_shared(user_id, key, item, share["share_id"])

    base = _share_base_url(event)
    return _resp(201, {
        "share_id": share["share_id"],
        "url": share_schema.public_share_url(base, token),
        "expires_at": expires_at or None,
        "share": share_schema.owner_view(share, base_url=base, token=token),
    })


def list_shares(event):
    """GET /recordings/shares/{key+} -> {shares:[...]}

    No `url` on any row: the raw tokens are unrecoverable by design (only
    their hashes were stored), so the app shows a revoke control for old links
    and a Create button for a new one.
    """
    _, key, _item = _owned_recording(event, hydrate=False)
    rows = _shares_for_recording(key)
    rows.sort(key=lambda s: s.get("created_at", ""), reverse=True)
    return _resp(200, {"shares": [share_schema.owner_view(s) for s in rows]})


def update_share(event):
    """PATCH /shares/{share_id} -> {share}

    Toggles and expiry only. The token never changes: rotating it would break
    a link the owner has already sent while leaving the old one revoked
    anyway, so "change who can see what" and "issue a new link" stay separate
    operations.
    """
    user_id = _require_auth(event)
    share_id = (event.get("pathParameters") or {}).get("share_id", "")
    share = _share_by_id(share_id, user_id)
    body = _body(event)

    updates = share_schema.coerce_config(body, base=share)
    now = _now_iso()
    values = {":now": now}
    names = {}
    sets = ["updated_at = :now"]

    for i, (name, _default, _roles) in enumerate(share_schema.TOGGLES):
        sets.append(f"#n{i} = :t{i}")
        names[f"#n{i}"] = name
        values[f":t{i}"] = updates[name]

    if "expires_at" in body or "expires_in_days" in body:
        sets.append("expires_at = :exp")
        values[":exp"] = _parse_expires_at(
            body.get("expires_at", body.get("expires_in_days")))

    # Un-revoking is deliberately not offered: a revoked link's token is gone
    # from the owner's reach anyway, so "restore" would resurrect a URL only a
    # recipient still holds. The condition re-checks ownership at write time.
    _shares.update_item(
        Key={"share_id": share_id},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues={**values, ":owner": user_id},
        ConditionExpression="owner_id = :owner",
    )

    fresh = _shares.get_item(Key={"share_id": share_id}).get("Item") or share
    return _resp(200, {"share": share_schema.owner_view(fresh)})


def revoke_share(event):
    """DELETE /shares/{share_id} -> {share_id, revoked}

    A soft revoke: the row stays, stamped with revoked_at. Keeping it is what
    makes the kill auditable — "this link existed and was killed at 14:02" is
    worth more than the reclaimed row, and the public route treats a revoked
    share exactly like a nonexistent one.
    """
    user_id = _require_auth(event)
    share_id = (event.get("pathParameters") or {}).get("share_id", "")
    share = _share_by_id(share_id, user_id)

    if share.get("revoked_at"):
        return _resp(200, {"share_id": share_id, "revoked": True,
                           "revoked_at": share["revoked_at"]})

    now = _now_iso()
    _shares.update_item(
        Key={"share_id": share_id},
        UpdateExpression="SET revoked_at = :now, updated_at = :now",
        ExpressionAttributeValues={":now": now, ":owner": user_id},
        ConditionExpression="owner_id = :owner",
    )
    return _resp(200, {"share_id": share_id, "revoked": True, "revoked_at": now})


def _share_by_token(token):
    """The share row for a raw token, or None. Reads the hash, never the token."""
    res = _shares.query(
        IndexName=SHARES_TOKEN_INDEX,
        KeyConditionExpression=Key("token_hash").eq(
            share_schema.hash_token(token)),
    )
    rows = res.get("Items", [])
    return rows[0] if rows else None


def _count_share_view(share):
    """Best-effort view counter. Never fails the page.

    A share link's whole value is that it opens; a throttled counter update
    must not be the reason a recipient sees an error.
    """
    try:
        _shares.update_item(
            Key={"share_id": share.get("share_id")},
            UpdateExpression="SET view_count = if_not_exists(view_count, :z) "
                             "+ :one, last_viewed_at = :now",
            ExpressionAttributeValues={":z": 0, ":one": 1, ":now": _now_iso()},
        )
    except Exception as e:  # noqa: BLE001
        print(f"[share] view count failed for "
              f"{share.get('share_id')}: {type(e).__name__}: {e}")


# The wording every dead link gets, whatever killed it. One tuple so the
# revoked and unknown paths cannot drift apart and start distinguishing
# themselves — see _resolve_public_share.
_SHARE_GONE = ("This link isn't available",
               "It may have been revoked by its owner, or the address may be "
               "incomplete. Ask the sender for a new link.")


class ShareGone(Exception):
    """A public share request that must not be served.

    Carries the HTTP status and the page to render. Raised rather than
    returned so the validation sequence reads top-to-bottom in one place and
    BOTH public routes (the page and the audio gateway) are forced through
    exactly the same checks — an audio gateway that silently skipped the
    revocation test is precisely the bug this shape prevents.
    """

    def __init__(self, status, headline, detail):
        super().__init__(headline)
        self.status = status
        self.headline = headline
        self.detail = detail

    def response(self):
        return _html_resp(self.status, share_schema.render_error_page(
            self.status, self.headline, self.detail))


def _resolve_public_share(event):
    """(token, share, item) for a public request, or raise ShareGone.

    THE one gate in front of every unauthenticated read. The checks run
    cheapest and most-likely-to-reject first, so a flood of bad URLs costs a
    regex and at most one indexed lookup:

      1. SHAPE      the token must look like a token at all (no DB read).
      2. LOOKUP     token-index on sha256(token). Unknown -> 404.
      3. REVOKED    revoked_at set -> 404, the SAME page as unknown.
      4. EXPIRED    expires_at passed -> 410, and says so: an expiry is a
                    fact the owner chose to communicate, unlike a revocation.
      5. RECORDING  resolved from the SHARE, never from the URL.
      6. TRASHED    a meeting in the Trash stops being shared with it.

    Failures 2 and 3 render the same page on purpose, so a dead link cannot be
    used to learn whether a share ever existed.
    """
    token = _url_unquote((event.get("pathParameters") or {}).get("token", ""))

    if not share_schema.token_looks_valid(token):
        raise ShareGone(404, *_SHARE_GONE)

    try:
        share = _share_by_token(token)
    except ClientError as e:
        print(f"[share] lookup failed for {share_schema.redact_token(token)}: "
              f"{type(e).__name__}: {e}")
        raise ShareGone(503, "Something went wrong",
                        "This page couldn't be loaded right now. "
                        "Please try again.")

    if not share or share_schema.is_revoked(share):
        raise ShareGone(404, *_SHARE_GONE)

    if share_schema.is_expired(share):
        raise ShareGone(410, "This link has expired",
                        "The owner set this share link to expire. "
                        "Ask them for a new one.")

    key = share.get("recording_key") or ""
    item = _recordings.get_item(Key={"audio_s3_key": key}).get("Item") \
        if key else None
    if not item or _is_trashed(item):
        raise ShareGone(404, *_SHARE_GONE)

    return token, share, item


def public_share_audio(event):
    """GET /share/{token}/audio -> 302 to a fresh presign. NO JWT.

    THE AUDIO GATEWAY. The page never points <audio> at S3 directly; it points
    here, and this re-validates the share and re-signs on EVERY request. Three
    things follow, and each is a reason this route exists:

      * REVOCATION REACHES PLAYBACK. An HTML5 player re-requests on every seek
        and on many pause/resumes. Because each of those passes through
        _resolve_public_share again, revoking a share (or turning its audio
        toggle off) stops audio that is already under way, instead of leaving
        a signed URL working in someone's tab until it lapses.

      * NO S3 URL IN THE PAGE SOURCE. What ships in the HTML is this route,
        which is useless without a live share behind it. The presign exists
        only inside a 302 the browser follows.

      * LONG RECORDINGS WORK. S3 checks a presign's expiry at REQUEST time, so
        a ranged read that starts inside the window completes, but the NEXT
        one — every seek — is checked afresh. Re-signing per request means the
        window only ever has to be one read wide, and a two-hour meeting still
        seeks correctly at minute 118.

    RANGE / 206 is preserved because the browser replays its original request,
    Range header and all, against the Location it is given. S3 answers the 206
    directly; this Lambda never proxies a byte of audio, which also keeps it
    clear of API Gateway's 29s timeout and 10MB response cap.
    """
    try:
        _token, share, item = _resolve_public_share(event)
    except ShareGone as gone:
        return gone.response()

    # Checked HERE, not only when rendering the page: a viewer holding an
    # already-loaded page must not keep streaming after the owner turns audio
    # off. This is the check that makes that true.
    if not share.get("audio_enabled"):
        return ShareGone(404, *_SHARE_GONE).response()

    key = share.get("recording_key") or ""
    if not (BUCKET_NAME and key):
        return ShareGone(404, *_SHARE_GONE).response()

    try:
        url = _s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKET_NAME, "Key": key},
            # Flat and short: this URL only has to outlive ONE ranged read,
            # because the next seek comes back through this route.
            ExpiresIn=share_schema.SHARE_AUDIO_URL_EXPIRY,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[share] presign failed for {key}: {type(e).__name__}: {e}")
        return ShareGone(503, "Something went wrong",
                         "The recording couldn't be loaded right now. "
                         "Please try again.").response()

    # 302, not 301: a permanent redirect is exactly the thing a browser is
    # entitled to cache, and caching it would pin one expiring presign in
    # front of every later seek.
    return {
        "statusCode": 302,
        "headers": {
            "Location": url,
            "Cache-Control": "no-store, no-cache, must-revalidate, private",
            "Referrer-Policy": "no-referrer",
            "X-Robots-Tag": "noindex, nofollow",
        },
        "body": "",
    }


def public_share(event):
    """GET /share/{token} -> a mobile-friendly HTML page. NO JWT.

    THE THIRD UNAUTHENTICATED ROUTE IN THIS FILE (public_share_audio above is
    the fourth). Validation lives in _resolve_public_share; what remains here
    is assembling the payload from the enabled toggles only.
    """
    try:
        token, share, item = _resolve_public_share(event)
    except ShareGone as gone:
        return gone.response()

    key = share.get("recording_key") or ""

    # The transcript lives in S3 for newer rows, so it is only fetched when the
    # share actually publishes it — a notes-only share costs no S3 GET.
    if share.get("transcript_enabled"):
        try:
            item = transcript_store.hydrate(_s3, BUCKET_NAME, item)
        except Exception as e:  # noqa: BLE001
            print(f"[share] transcript hydrate failed for {key}: "
                  f"{type(e).__name__}: {e}")

    # The page points at the GATEWAY, never at S3. The permanent object URL is
    # never exposed and no presign appears in the HTML at all — the gateway
    # mints one per request, behind a fresh validation. See public_share_audio.
    #
    # FALLBACK: if the gateway address cannot be resolved (no SHARE_BASE_URL
    # and no Host header), fall back to a direct presign sized to the
    # RECORDING rather than to a flat 15 minutes — a fixed window breaks
    # seeking on any meeting longer than it. Still only when audio is shared.
    audio_url = None
    if share.get("audio_enabled") and BUCKET_NAME and key:
        base = _share_base_url(event)
        if base:
            audio_url = f"{base}/share/{token}/audio"
        else:
            try:
                audio_url = _s3.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": BUCKET_NAME, "Key": key},
                    ExpiresIn=share_schema.audio_url_expiry(item.get("duration")),
                )
            except Exception as e:  # noqa: BLE001
                print(f"[share] presign failed for {key}: "
                      f"{type(e).__name__}: {e}")
                audio_url = None

    mom = _shared_mom(share.get("owner_id") or "", key, item)
    payload = share_schema.public_payload(item, mom, share, audio_url=audio_url)
    _count_share_view(share)
    return _html_resp(200, share_schema.render_page(payload))


# ---------------------------------------------------------------------------
# INTEGRATIONS — the generic "connect MinuteX to an external application"
# layer, with Gmail as the first (and currently only) working provider.
#
# WHY A SECOND OAUTH SECTION EXISTS ALONGSIDE THE SALESFORCE ONE ABOVE. The
# Salesforce code is the right pattern and this section reuses every part of
# it that is genuinely generic — the signed/expiring `state` (_sign_oauth_state
# / _verify_oauth_state), PKCE, KMS envelope-encryption of the refresh token,
# the "never 401 for a dead third-party credential" rule. What it does NOT do
# is widen the Salesforce handlers to take a provider argument, because those
# handlers are wound through Salesforce-specific concerns (instance_url, org
# describe, field mapping, SOQL) that no other provider has. Generalising them
# would mean a growing pile of `if provider == "salesforce"` inside code that
# currently reads straight through.
#
# So the split is by SHAPE, not by vendor:
#   * shared/integrations.py owns the connection model + status vocabulary
#   * IntegrationProvider below owns the OAuth mechanics every provider shares
#   * GmailProvider owns only what is Gmail-specific
# Adding WhatsApp later is a subclass plus a PROVIDERS entry. Salesforce can be
# migrated onto this table when someone wants it to be, and until then it is
# listed in the catalog as managed elsewhere so the Integrations screen is
# still a complete picture.
#
# THE FLOW (identical in shape to the Salesforce one, three parties):
#   1. POST /integrations/{provider}/connect  (JWT) -> {authorize_url}
#   2. the app opens it; the user approves on Google's own page
#   3. Google redirects to GET /integrations/{provider}/callback?code&state
#      — UNAUTHENTICATED, because a browser redirect carries no JWT; `state`
#      is the credential there, exactly as in the Salesforce callback
#   4. the callback exchanges the code, encrypts the refresh token, writes
#      one Integrations row with status CONNECTED
#   5. GET /integrations and DELETE /integrations/{provider} let the app read
#      status and disconnect — tokens never appear in any response
# ---------------------------------------------------------------------------

# Google's OAuth endpoints. Constants rather than env vars: unlike the
# Salesforce login host (which legitimately differs for a sandbox org), these
# are the same for every Google account in the world.
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

# SCOPES — the minimum for Phase 1, and this list is the security boundary.
#
# gmail.send is a SEND-ONLY scope: it grants permission to send mail as the
# user and NOTHING else. It cannot read the inbox, cannot search, cannot list
# threads, cannot even read the message it just sent. That is exactly the
# capability MinuteX needs and exactly the one it should hold — the moment a
# read scope is added, every meeting-notes product becomes a mailbox-scraping
# product in the user's eyes and in Google's verification review.
#
# userinfo.email exists only so the Manage screen can show WHICH account is
# connected. Without it the app could say "Gmail: Connected" but not whose
# Gmail, which is the difference between a trustworthy integration and a
# spooky one.
#
# Explicitly NOT requested: gmail.readonly, gmail.modify, gmail.compose,
# gmail.metadata, or any Calendar/Tasks/Contacts scope. Phase 1 does not need
# them, and Gmail inbox reading is named in the scope boundary as out of scope.
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/userinfo.email",
]

# The provider whose connection is REQUIRED for the mail routes below.
INTEGRATION_GMAIL = integrations.PROVIDER_GMAIL

# HTTP status + code for "MinuteX is fine, but the INTEGRATION behind this
# route is not usable". Same reasoning as SF_RECONNECT_STATUS above, and the
# reasoning is worth restating because it is the single most load-bearing
# decision in this file's error handling: 401 on this API means the MinuteX
# JWT is dead, and lib/api.ts reacts by clearing the session and bouncing the
# user to /login. A dead Gmail refresh token must never do that — the user's
# MinuteX session is perfectly valid and the fix is "reconnect Gmail", not
# "sign in again".
#
# 409 Conflict is the honest code: authenticated, well-formed, but in conflict
# with the current state of the resource. The app branches on `code`.
INTEGRATION_STATUS_CODE = 409
INTEGRATION_REAUTH_CODE = "integration_reauth_required"
INTEGRATION_NOT_CONNECTED_CODE = "integration_not_connected"


class IntegrationNotConnected(ApiError):
    """The user has not connected this provider (or has disconnected it).

    409 rather than 400 for the same reason as below: the app needs ONE branch
    for "this integration cannot serve the request", distinguished by `code`.
    """

    def __init__(self, provider: str, message: str = ""):
        meta = integrations.PROVIDERS_BY_ID.get(provider, {})
        name = meta.get("name", provider)
        super().__init__(INTEGRATION_STATUS_CODE,
                         message or f"Connect {name} to use this feature.")
        self.code = INTEGRATION_NOT_CONNECTED_CODE
        self.provider = provider


class IntegrationReauthRequired(ApiError):
    """The stored credential is dead — the user must reconnect.

    Raising this also FLIPS THE STORED STATUS to REAUTH_REQUIRED (see
    _mark_integration_status), which is what makes the backend the source of
    truth the requirement asks for: the next GET /integrations reports the
    honest state without needing another failed send to discover it.
    """

    def __init__(self, provider: str, message: str = ""):
        meta = integrations.PROVIDERS_BY_ID.get(provider, {})
        name = meta.get("name", provider)
        super().__init__(INTEGRATION_STATUS_CODE,
                         message or f"Your {name} connection expired. "
                                    f"Reconnect {name} to continue.")
        self.code = INTEGRATION_REAUTH_CODE
        self.provider = provider


def _google_client_secret():
    """The OAuth client secret from Secrets Manager — same lazy per-container
    cache as _jwt_secret()/_salesforce_client_secret(). No env-var fallback:
    a client secret must never land as a plaintext Lambda env var, and there
    is no legacy deployment here to stay compatible with."""
    global _google_secret_cache
    if _google_secret_cache is not None:
        return _google_secret_cache
    if not GOOGLE_CLIENT_SECRET_ARN:
        raise ApiError(500, "Gmail integration is not configured")
    sm = boto3.client("secretsmanager", region_name=REGION)
    _google_secret_cache = sm.get_secret_value(
        SecretId=GOOGLE_CLIENT_SECRET_ARN)["SecretString"]
    return _google_secret_cache


_google_secret_cache = None


def _integration_kms_encrypt(plaintext: str) -> str:
    """Envelope-encrypt a refresh token for storage.

    Uses INTEGRATIONS_KMS_KEY_ID, falling back to the Salesforce key when it
    is unset so a deployment that has not yet run the new provisioning script
    still works rather than storing plaintext. Storing plaintext is never an
    acceptable degradation, so if NEITHER key exists this raises.
    """
    key_id = INTEGRATIONS_KMS_KEY_ID or SALESFORCE_KMS_KEY_ID
    if not key_id:
        raise ApiError(500, "integration credential storage is not configured")
    resp = _kms.encrypt(KeyId=key_id, Plaintext=plaintext.encode("utf-8"))
    return _b64u_encode(resp["CiphertextBlob"])


def _integration_kms_decrypt(ciphertext_b64: str) -> str:
    # KeyId is omitted deliberately: a symmetric ciphertext blob carries the
    # key it was encrypted under, so decrypt works even if the configured key
    # changed after the row was written. Passing a mismatched KeyId would
    # fail an otherwise recoverable decrypt.
    resp = _kms.decrypt(CiphertextBlob=_b64u_decode(ciphertext_b64))
    return resp["Plaintext"].decode("utf-8")


# ---------------------------------------------------------------------------
# Storage — the Integrations table, keyed exactly like CrmConnections.
# ---------------------------------------------------------------------------
def _get_integration(user_id: str, provider: str):
    return _integrations.get_item(
        Key={"user_id": user_id, "provider": provider}).get("Item")


def _all_integrations(user_id: str) -> dict:
    """Every connection row this user has, keyed by provider.

    One Query on the partition key, not N GetItems: the catalog needs all of
    them and the row count per user is bounded by the provider list.
    """
    try:
        res = _integrations.query(
            KeyConditionExpression=Key("user_id").eq(user_id))
    except Exception as e:  # noqa: BLE001
        # A brand-new deployment may not have the table yet. The Integrations
        # screen showing everything as not-connected is a far better failure
        # than a 500 that hides the Coming Soon cards too.
        print(f"[integrations] list failed for {user_id}: {type(e).__name__}: {e}")
        return {}
    return {row.get("provider"): row for row in res.get("Items", [])
            if row.get("provider")}


def _mark_integration_status(user_id: str, provider: str, status: str,
                             message: str = "") -> None:
    """Persist a status transition (CONNECTED -> REAUTH_REQUIRED / ERROR).

    Best-effort by design: this is called from a failure path that is already
    reporting a useful error to the user, and failing THAT over a bookkeeping
    write would be strictly worse. The next call re-discovers the same state.
    """
    try:
        _integrations.update_item(
            Key={"user_id": user_id, "provider": provider},
            UpdateExpression=("SET #s = :s, status_message = :m, "
                              "updated_at = :now"),
            # Only touch a row that EXISTS. A disconnect that raced with this
            # must not be resurrected as a REAUTH_REQUIRED row — that would
            # show the user a "Reconnect" card for something they just removed.
            ConditionExpression="attribute_exists(user_id)",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": status, ":m": message[:300],
                                       ":now": _now_iso()},
        )
    except Exception as e:  # noqa: BLE001
        print(f"[integrations] status write failed ({provider}={status}): "
              f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# Provider abstraction.
#
# The base class owns the OAuth-2 authorization-code flow as Google, Slack,
# HubSpot, Microsoft and most others implement it — which is why a future
# provider overrides configuration (URLs, scopes) rather than logic. It
# deliberately does NOT declare send_message(): a calendar provider has no
# such operation, and an abstract method that half the subclasses raise
# NotImplementedError from is a worse contract than no method at all. Each
# provider declares the capabilities it actually has.
# ---------------------------------------------------------------------------
class IntegrationProvider:
    """Base: connect / disconnect / status / refresh_credentials."""

    provider = ""
    auth_url = ""
    token_url = ""
    revoke_url = ""
    scopes = ()

    # ---- configuration -------------------------------------------------
    def client_id(self) -> str:
        raise NotImplementedError

    def client_secret(self) -> str:
        raise NotImplementedError

    def redirect_uri(self) -> str:
        raise NotImplementedError

    def is_configured(self) -> bool:
        return bool(self.client_id() and self.redirect_uri())

    # ---- HTTP ----------------------------------------------------------
    def _post_form(self, url: str, form: dict, what: str) -> dict:
        """POST x-www-form-urlencoded, return parsed JSON.

        Mirrors SalesforceClient._post_form — stdlib urllib, no dependencies,
        matching this file's convention. The provider's own error body is
        logged and never returned: an OAuth error body can echo parameters.
        """
        body = urllib.parse.urlencode(form).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            print(f"[{self.provider}] {what} failed: {e.code} {detail[:300]}")
            # invalid_grant is THE signal that a refresh token is dead (user
            # revoked access in their Google account, or it went unused for
            # six months). It is the one OAuth error with a specific user
            # action attached, so it must not be flattened into "try again".
            if "invalid_grant" in detail:
                raise IntegrationReauthRequired(self.provider)
            raise ApiError(502, f"Could not complete the {what}. Try again.")
        except urllib.error.URLError as e:
            print(f"[{self.provider}] {what} network error: {e}")
            raise ApiError(502, "Could not reach the provider. "
                                "Check your connection and try again.")

    # ---- flow ----------------------------------------------------------
    def authorize_url(self, state: str, verifier: str) -> str:
        """The provider's consent URL for this attempt.

        access_type=offline + prompt=consent is what makes Google return a
        REFRESH token. Google issues one only on the first consent for a given
        client/user pair, and returns nothing on subsequent authorizations —
        so a user who disconnects and reconnects would come back with no
        refresh token at all, i.e. a connection that works for one hour and
        then dies. prompt=consent forces the consent screen every time and
        with it a fresh refresh token. The cost is one extra tap on reconnect;
        the alternative is a connection that silently rots.
        """
        return self.auth_url + "?" + urllib.parse.urlencode({
            "response_type": "code",
            "client_id": self.client_id(),
            "redirect_uri": self.redirect_uri(),
            "scope": " ".join(self.scopes),
            "state": state,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            # Only the CHALLENGE goes over the wire — the verifier rides
            # inside the signed state. See the PKCE note in the CRM section.
            "code_challenge": _pkce_challenge(verifier),
            "code_challenge_method": PKCE_METHOD,
        })

    def exchange_code(self, code: str, verifier: str) -> dict:
        return self._post_form(self.token_url, {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": self.client_id(),
            "client_secret": self.client_secret(),
            "redirect_uri": self.redirect_uri(),
            "code_verifier": verifier,
        }, "connection")

    def refresh_credentials(self, refresh_token: str) -> dict:
        """refresh_token -> a fresh short-lived access token.

        Access tokens are NOT stored, for the same reason the Salesforce path
        does not store them: they expire in an hour, and persisting them would
        mean a second secret to encrypt, rotate and leak for no gain. Each
        request pays one cheap refresh.
        """
        return self._post_form(self.token_url, {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self.client_id(),
            "client_secret": self.client_secret(),
        }, "token refresh")

    def revoke(self, refresh_token: str) -> None:
        """Best-effort revoke on disconnect.

        Never blocks the local disconnect: the user asked MinuteX to stop
        using their account, and that must happen whether or not Google is
        reachable. They can also revoke from their Google account page.
        """
        if not self.revoke_url:
            return
        try:
            self._post_form(self.revoke_url, {"token": refresh_token}, "revoke")
        except ApiError as e:
            print(f"[{self.provider}] revoke failed (non-fatal): {e.message}")

    def account_info(self, access_token: str) -> dict:
        """{account_identifier, account_name} for the Manage screen. Optional —
        a provider with no identity endpoint returns {}."""
        return {}


class GmailProvider(IntegrationProvider):
    """Gmail: OAuth + send. No read capability, by design (see GMAIL_SCOPES)."""

    provider = integrations.PROVIDER_GMAIL
    auth_url = GOOGLE_AUTH_URL
    token_url = GOOGLE_TOKEN_URL
    revoke_url = GOOGLE_REVOKE_URL
    scopes = tuple(GMAIL_SCOPES)

    def client_id(self) -> str:
        return GOOGLE_CLIENT_ID

    def client_secret(self) -> str:
        return _google_client_secret()

    def redirect_uri(self) -> str:
        return GOOGLE_REDIRECT_URI

    def account_info(self, access_token: str) -> dict:
        """Which Google account this is — the userinfo.email scope's purpose."""
        req = urllib.request.Request(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, ValueError) as e:
            # Cosmetic: the connection itself succeeded. Better to show
            # "Connected" with no address than to fail the whole connect.
            print(f"[gmail] userinfo failed (non-fatal): {e}")
            return {}
        return {"account_identifier": str(data.get("email") or ""),
                "account_name": str(data.get("name") or "")}

    def send_message(self, access_token: str, raw_message: str) -> dict:
        """POST one base64url-encoded RFC 2822 message to Gmail.

        Error mapping is where the user-facing quality of this feature lives.
        Gmail answers with codes ("invalid_grant", "rateLimitExceeded",
        "Invalid to header") that mean nothing to a user, so each is turned
        into a sentence describing what happened and what to do. The raw body
        goes to CloudWatch, never to the client.
        """
        body = json.dumps({"raw": raw_message}).encode("utf-8")
        req = urllib.request.Request(
            GMAIL_SEND_URL, data=body, method="POST",
            headers={"Authorization": f"Bearer {access_token}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                raw = r.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            print(f"[gmail] send failed: {e.code} {detail[:400]}")
            if e.code == 401:
                # The ACCESS token was rejected. _gmail_call refreshes and
                # retries once before this can reach the user.
                raise GmailAuthExpired(detail[:200])
            if e.code == 403:
                if "rateLimit" in detail or "userRateLimit" in detail:
                    raise ApiError(429, "Gmail is rate-limiting this account. "
                                        "Wait a moment and try again.")
                if "quotaExceeded" in detail or "Daily" in detail:
                    raise ApiError(429, "This Gmail account has reached its "
                                        "daily sending limit. Try again "
                                        "tomorrow.")
                # A 403 that is not a quota means the grant no longer carries
                # the send scope — reconnecting is genuinely the fix.
                raise IntegrationReauthRequired(
                    self.provider,
                    "MinuteX no longer has permission to send from this "
                    "Gmail account. Reconnect Gmail to continue.")
            if e.code == 429:
                raise ApiError(429, "Gmail is rate-limiting this account. "
                                    "Wait a moment and try again.")
            if e.code == 400:
                # Almost always a malformed recipient that survived validation
                # (an address Gmail rejects but our regex accepts).
                raise ApiError(400, "Gmail rejected the message — check the "
                                    "recipient addresses and try again.")
            raise ApiError(502, "Gmail could not send the message. Try again.")
        except urllib.error.URLError as e:
            print(f"[gmail] send network error: {e}")
            raise ApiError(502, "Could not reach Gmail. Check your connection "
                                "and try again.")


class GmailAuthExpired(Exception):
    """The ACCESS token was rejected (401) — internal, never surfaced.

    Same distinction SalesforceAuthExpired draws: this one means "mint a new
    access token and retry", which _gmail_call does transparently. A dead
    REFRESH token is IntegrationReauthRequired, which the user does see.
    """


_gmail_provider = GmailProvider()

PROVIDER_IMPLS = {
    integrations.PROVIDER_GMAIL: _gmail_provider,
}


def _provider_impl(provider: str):
    """The implementation for `provider`, or a 404.

    404 rather than 400 for an unknown provider: the path segment names a
    resource, and one MinuteX does not have simply does not exist.
    """
    impl = PROVIDER_IMPLS.get(provider)
    if impl is None:
        raise ApiError(404, "unknown integration")
    return impl


def _path_provider(event) -> str:
    """The {provider} path parameter, normalised and checked against the
    registry BEFORE it is used to key anything."""
    provider = str((event.get("pathParameters") or {}).get("provider") or
                   "").strip().lower()
    if provider not in integrations.PROVIDERS_BY_ID:
        raise ApiError(404, "unknown integration")
    return provider


# ---------------------------------------------------------------------------
# The credential lifecycle — one place, exactly like _sf_call.
# ---------------------------------------------------------------------------
def _integration_call(user_id: str, provider: str, fn):
    """Run `fn(access_token)` against the user's connected account.

    Owns the whole access-token lifecycle so no route handler repeats it:
    verify the connection is USABLE, decrypt the refresh token, mint an access
    token, call, and on a 401 mint once more and retry.

    THE OWNERSHIP CHECK IS HERE, not in the handlers. Every outbound operation
    passes through this function and it reads the connection row keyed by the
    user_id from the JWT — so there is no code path on which user A's request
    can reach user B's credential, because no handler ever names a user.
    """
    row = _get_integration(user_id, provider)
    if not row or not row.get("refresh_token_enc"):
        raise IntegrationNotConnected(provider)
    if row.get("status") == integrations.STATUS_REAUTH_REQUIRED:
        raise IntegrationReauthRequired(provider)
    if not integrations.is_usable(row):
        raise IntegrationNotConnected(
            provider, row.get("status_message") or "")

    impl = _provider_impl(provider)
    refresh_token = _integration_kms_decrypt(row["refresh_token_enc"])

    try:
        tokens = impl.refresh_credentials(refresh_token)
    except IntegrationReauthRequired:
        # The refresh token itself is dead. Record it so the NEXT status read
        # is honest without needing another failed send to discover it — this
        # is what makes the backend the source of truth.
        _mark_integration_status(
            user_id, provider, integrations.STATUS_REAUTH_REQUIRED,
            "The connection was revoked or expired.")
        raise

    access_token = tokens.get("access_token")
    if not access_token:
        _mark_integration_status(
            user_id, provider, integrations.STATUS_REAUTH_REQUIRED,
            "The connection could not be renewed.")
        raise IntegrationReauthRequired(provider)

    # Google does not rotate refresh tokens the way Salesforce can, so there
    # is no rotation write here. If a provider that DOES rotate is added, this
    # is the one place that needs the conditional-write dance
    # _persist_rotated_refresh_token performs.
    try:
        return fn(access_token)
    except GmailAuthExpired:
        print(f"[{provider}] access token rejected; refreshing once and retrying")
        tokens = impl.refresh_credentials(refresh_token)
        access_token = tokens.get("access_token")
        if not access_token:
            _mark_integration_status(
                user_id, provider, integrations.STATUS_REAUTH_REQUIRED,
                "The connection could not be renewed.")
            raise IntegrationReauthRequired(provider)
        try:
            return fn(access_token)
        except GmailAuthExpired:
            _mark_integration_status(
                user_id, provider, integrations.STATUS_REAUTH_REQUIRED,
                "The connection kept being rejected.")
            raise IntegrationReauthRequired(provider)


def _require_integration(user_id: str, provider: str) -> dict:
    """The connection row, or the right 409. The server-side half of the
    "Gmail-dependent features are unavailable without Gmail" rule — hiding the
    button in the app is presentation; THIS is enforcement."""
    row = _get_integration(user_id, provider)
    if not row or not row.get("refresh_token_enc"):
        raise IntegrationNotConnected(provider)
    if row.get("status") == integrations.STATUS_REAUTH_REQUIRED:
        raise IntegrationReauthRequired(provider)
    if not integrations.is_usable(row):
        raise IntegrationNotConnected(provider, row.get("status_message") or "")
    return row


# ---------------------------------------------------------------------------
# Routes — status
# ---------------------------------------------------------------------------
def _integration_rows(user_id: str) -> dict:
    """Every provider's connection row for this user, from EVERY source.

    Two tables feed one catalog. Integrations holds the providers this system
    manages; CrmConnections holds Salesforce, which predates it and is
    connected through /crm/salesforce/*. Merging here — rather than teaching
    the catalog about two tables, or reporting Salesforce as "not connected"
    because its row lives elsewhere — is what lets the Integrations screen be
    one honest list.

    The Salesforce read is best-effort: an unconfigured CrmConnections table
    must degrade that ONE card to "not connected", never take down the whole
    Integrations screen (which is also how Coming Soon cards reach the user).
    """
    rows = _all_integrations(user_id)
    try:
        crm = _get_salesforce_connection(user_id)
    except Exception as e:  # noqa: BLE001
        print(f"[integrations] salesforce read failed for {user_id}: "
              f"{type(e).__name__}: {e}")
        crm = None
    sf = integrations.salesforce_row(crm)
    if sf:
        rows[integrations.PROVIDER_SALESFORCE] = sf
    return rows


def list_integrations(event):
    """GET /integrations (JWT) -> {integrations:[...]}.

    The FULL catalog, not just what is connected: the Integrations screen
    renders Coming Soon cards from this too, so a new provider appears in
    every already-installed client the day it is added to PROVIDERS.
    """
    user_id = _require_auth(event)
    return _resp(200, {
        "integrations": integrations.catalog(_integration_rows(user_id))})


def get_integration(event):
    """GET /integrations/{provider} (JWT) -> {integration:{...}}."""
    user_id = _require_auth(event)
    provider = _path_provider(event)
    # Reads through the same merge as the catalog, so a single-provider fetch
    # can never disagree with the list it came from.
    return _resp(200, {"integration": integrations.public_status(
        provider, _integration_rows(user_id).get(provider))})


def integration_connect(event):
    """POST /integrations/{provider}/connect (JWT) -> {authorize_url}.

    Mints a fresh PKCE verifier per attempt and carries it inside the signed
    state, exactly as salesforce_connect does — see the PKCE note in the CRM
    section for why the verifier is server-side rather than on the device.

    The state additionally carries the PROVIDER, so a state minted for one
    integration cannot be replayed against another's callback. Without it, the
    signature would still verify (same secret, same user) and the code would
    be exchanged against the wrong provider's token endpoint.
    """
    user_id = _require_auth(event)
    provider = _path_provider(event)
    if provider not in integrations.CONNECTABLE:
        raise ApiError(400, "That integration isn't available yet.")
    impl = _provider_impl(provider)
    if not impl.is_configured():
        raise ApiError(500, "This integration is not configured on the server.")

    verifier = _new_pkce_verifier()
    state = _sign_integration_state(user_id, provider, verifier)
    _audit("integration.connect_started", user_id, provider)
    return _resp(200, {"authorize_url": impl.authorize_url(state, verifier)})


def _sign_integration_state(user_id: str, provider: str, verifier: str) -> str:
    """HMAC-signed, expiring state — CSRF guard, PKCE carrier AND provider
    binding. Same construction and same secret as _sign_oauth_state; the extra
    `pv` claim is what stops a cross-provider replay."""
    payload = {"sub": user_id, "pv": provider, "cv": verifier,
               "exp": int(time.time()) + INTEGRATION_STATE_TTL}
    seg = _b64u_encode(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(_jwt_secret().encode(), seg.encode(), hashlib.sha256).digest()
    return seg + "." + _b64u_encode(sig)


def _verify_integration_state(state: str, provider: str) -> tuple:
    """(user_id, verifier) from a state minted for THIS provider, or ApiError."""
    try:
        seg, sig_b64 = state.split(".")
        expected = hmac.new(_jwt_secret().encode(), seg.encode(),
                            hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64u_decode(sig_b64)):
            raise ValueError("bad signature")
        payload = json.loads(_b64u_decode(seg))
    except (ValueError, TypeError, KeyError):
        raise ApiError(400, "invalid or tampered state")
    if not isinstance(payload, dict) or payload.get("exp", 0) < int(time.time()):
        raise ApiError(410, "connect session expired — try again")
    user_id = payload.get("sub")
    verifier = str(payload.get("cv") or "")
    if not user_id or not verifier:
        raise ApiError(400, "invalid state")
    if payload.get("pv") != provider:
        # A state signed for a different integration. Same user, same secret,
        # valid signature — and still wrong.
        raise ApiError(400, "state does not match this integration")
    return user_id, verifier


def integration_callback(event):
    """GET /integrations/{provider}/callback?code&state (NO JWT — the
    provider's browser redirect calls this).

    `state` is the credential here, exactly as in salesforce_callback: a
    browser redirect cannot carry a bearer token. Ends in a 302 to the app's
    deep link with an ok/error param, because the browser tab — not this
    Lambda — is the only thing that can hand control back to the app.
    """
    provider = _path_provider(event)
    qs = event.get("queryStringParameters") or {}

    def _redirect(ok: bool, reason: str = "") -> dict:
        params = {"provider": provider}
        params.update({"connected": "1"} if ok
                      else {"connected": "0", "reason": reason})
        target = INTEGRATION_RETURN_URL or SALESFORCE_RETURN_URL or "/"
        return {"statusCode": 302,
                "headers": {"Location": f"{target}?{urllib.parse.urlencode(params)}"},
                "body": ""}

    error = qs.get("error")
    if error:
        # access_denied is the user pressing Cancel/Deny — a normal choice,
        # distinguished from a real failure so the app can stay quiet about it.
        print(f"[{provider}] callback error param: {error}")
        return _redirect(False, "denied" if error == "access_denied" else "failed")

    code = qs.get("code")
    state = qs.get("state")
    if not code or not state:
        return _redirect(False, "missing_params")

    try:
        user_id, verifier = _verify_integration_state(state, provider)
    except ApiError as e:
        print(f"[{provider}] callback state rejected: {e.message}")
        return _redirect(False, "expired")

    impl = _provider_impl(provider)
    try:
        tokens = impl.exchange_code(code, verifier)
    except ApiError as e:
        # e.message is our own wording — never the code, the verifier or a token.
        print(f"[{provider}] callback exchange failed: {e.message}")
        return _redirect(False, "exchange_failed")

    refresh_token = tokens.get("refresh_token")
    access_token = tokens.get("access_token")
    if not access_token:
        print(f"[{provider}] token response carried no access token")
        return _redirect(False, "exchange_failed")
    if not refresh_token:
        # Google omits the refresh token when the user has already granted
        # consent and prompt=consent was not honoured. Without one the
        # connection would work for an hour and then die, so refuse now and
        # tell the app to retry rather than storing a connection that rots.
        print(f"[{provider}] token response carried no refresh token")
        return _redirect(False, "no_refresh_token")

    info = {}
    try:
        info = impl.account_info(access_token)
    except Exception as e:  # noqa: BLE001 - identity is cosmetic, never fatal
        print(f"[{provider}] account info failed (non-fatal): {e}")

    granted = str(tokens.get("scope") or "").split()
    now = _now_iso()
    existing = _get_integration(user_id, provider)
    _integrations.put_item(Item={
        "user_id": user_id,
        "provider": provider,
        "status": integrations.STATUS_CONNECTED,
        "status_message": "",
        "refresh_token_enc": _integration_kms_encrypt(refresh_token),
        "account_identifier": info.get("account_identifier", ""),
        "account_name": info.get("account_name", ""),
        "scopes": granted or list(impl.scopes),
        # Preserved across a reconnect so the Manage screen can show when the
        # user FIRST connected, not when they last re-approved.
        "connected_at": (existing or {}).get("connected_at") or now,
        "updated_at": now,
    })
    _audit("integration.connected", user_id, provider,
           scopes=len(granted or impl.scopes))
    return _redirect(True)


def integration_disconnect(event):
    """DELETE /integrations/{provider} (JWT) -> {disconnected}.

    Revokes the grant with the provider (best-effort) and DELETES the row.

    Deleting rather than flagging is deliberate: the requirement is that the
    stored credential is removed, and a row flagged "disconnected" that still
    holds ciphertext is a credential we said we deleted and did not. Absence
    of a row IS integrations.STATUS_NOT_CONNECTED, so nothing is lost.

    Nothing else is touched. Meetings, contacts, tasks, MoMs and documents are
    MinuteX data and have no dependency on the connection — disconnecting Gmail
    removes the ability to send mail, not anything the user created.
    """
    user_id = _require_auth(event)
    provider = _path_provider(event)

    # A provider whose connection this system does not own must not be
    # disconnected through here. Without this guard the delete below would run
    # against the Integrations table for a Salesforce row that lives in
    # CrmConnections — reporting success while leaving the real credential in
    # place, which is the worst possible outcome for a "disconnect".
    meta = integrations.PROVIDERS_BY_ID.get(provider) or {}
    if meta.get("managed_elsewhere"):
        raise ApiError(400, f"Disconnect {meta.get('name', provider)} from its "
                            f"own settings screen.")

    row = _get_integration(user_id, provider)
    if row and row.get("refresh_token_enc"):
        try:
            impl = _provider_impl(provider)
            impl.revoke(_integration_kms_decrypt(row["refresh_token_enc"]))
        except Exception as e:  # noqa: BLE001 - never blocks the disconnect
            print(f"[{provider}] revoke on disconnect failed (non-fatal): "
                  f"{type(e).__name__}: {e}")

    _integrations.delete_item(Key={"user_id": user_id, "provider": provider})
    _audit("integration.disconnected", user_id, provider)
    return _resp(200, {"disconnected": True,
                       "integration": integrations.public_status(provider)})


# ---------------------------------------------------------------------------
# Gmail — communication.
#
# WHERE THE LAYERING SITS. Meeting/task/MoM code does not know Gmail exists;
# it hands a message to the communication layer, which asks the integration
# layer for a provider. Concretely:
#
#     POST /integrations/gmail/send        a message the caller composed
#     POST /integrations/gmail/send/meeting/{key+}   a MEETING message
#     POST /integrations/gmail/send/task/{task_id}   a TASK message
#
# The last two exist so recipient resolution and ownership live on the server.
# The alternative — the app posting a list of raw addresses to the generic
# route — would mean the backend could not verify that a recipient is really a
# participant of a meeting the caller owns, and "send to whoever the client
# says" is how a mail relay gets abused.
#
# ATTACHMENTS COME FROM THE CLIENT. The PDF and DOCX renderers live in the app
# (lib/mom-pdf.ts, lib/mom-docx.ts) and are what the user previews before
# sending. Re-implementing them server-side would mean two renderers that
# drift, and the recipient would receive a document that differs from the
# preview. So the app sends the bytes it rendered, base64, and the backend
# validates them as untrusted input (see shared/email_message.py).
# ---------------------------------------------------------------------------
def _gmail_call(user_id: str, fn):
    return _integration_call(user_id, INTEGRATION_GMAIL, fn)


def _send_via_gmail(user_id: str, row: dict, to, subject: str, body: str,
                    cc=None, attachments=None) -> dict:
    """Build the message and send it. One place, so every caller — generic,
    meeting, task — produces identically shaped mail and identical errors."""
    sender = row.get("account_identifier") or ""
    if not sender:
        # The address is only cosmetic on the status screen, but as a From
        # header it matters. Gmail rewrites From to the authenticated account
        # anyway, so an empty one is safe — it just loses the display name.
        print(f"[gmail] no stored account identifier for {user_id}")
    sender_name = row.get("account_name") or ""

    try:
        raw = email_message.build_message(
            sender=sender, sender_name=sender_name, to=to, subject=subject,
            body=body, cc=cc, attachments=attachments)
    except email_message.EmailError as e:
        # Always user-facing wording by construction — see EmailError.
        raise ApiError(400, str(e))

    result = _gmail_call(user_id, lambda tok:
                         _gmail_provider.send_message(tok, raw))
    return {"message_id": str(result.get("id") or ""),
            "thread_id": str(result.get("threadId") or "")}


def _recipient_payload(data) -> list:
    """The `recipients` field of a send request, sanity-bounded.

    Accepts [{contact_id?, email?, name?}]. Plain strings are accepted too,
    so a caller with only an address does not have to wrap it.
    """
    raw = data.get("recipients")
    if not isinstance(raw, list):
        raise ApiError(400, "recipients must be a list")
    if len(raw) > email_message.MAX_RECIPIENTS:
        raise ApiError(400, f"Send to at most {email_message.MAX_RECIPIENTS} "
                            f"people at a time.")
    out = []
    for entry in raw:
        if isinstance(entry, str):
            out.append({"email": entry})
        elif isinstance(entry, dict):
            out.append(entry)
    return out


def _resolve_contact_recipients(user_id: str, requested) -> tuple:
    """Turn requested recipients into (resolved, unresolved), resolving any
    contact_id through the contacts this caller may READ — their own personal
    ones, plus the shared address book of the organisation they are acting in.

    A contact_id the caller cannot read resolves to nothing and lands in
    `unresolved` rather than raising — the caller reports "no email address
    for this person", which is also the honest answer for a contact that does
    not exist as far as this user is concerned. Same reasoning as the 404-not-
    403 rule elsewhere: the API must not confirm that some other user's
    contact id is real.
    """
    hydrated = []
    for entry in requested:
        contact_id = str(entry.get("contact_id") or "").strip()
        name = str(entry.get("name") or "").strip()
        email = str(entry.get("email") or "").strip()
        if contact_id:
            # Central gate: a SHARED organisation contact resolves for every
            # member, so emailing a colleague's client works. A contact the
            # caller may not read still lands in `unresolved` rather than
            # raising, exactly as before.
            row = _readable_contact(user_id, contact_id)
            if row:
                # The stored contact is authoritative for the address — a
                # client-supplied email alongside a contact_id would otherwise
                # be a way to send to an arbitrary address while looking like
                # a legitimate contact send.
                email = str(row.get("email") or "")
                name = name or str(row.get("name") or "")
            else:
                email = ""
                name = name or "This contact"
        hydrated.append({"name": name, "email": email, "contact_id": contact_id})
    return email_message.resolve_recipients(hydrated)


def _require_resolved(resolved, unresolved):
    """Refuse the send when anyone could not be resolved.

    The product rule is explicit: do NOT silently attempt to send. A partial
    send looks identical to a complete one from the app's point of view, and
    the person left out never learns they were.
    """
    if unresolved:
        raise ApiError(422, email_message.describe_unresolved(unresolved))
    if not resolved:
        raise ApiError(400, "Choose at least one recipient.")


def gmail_send(event):
    """POST /integrations/gmail/send (JWT) -> {sent, message_id}.

    The generic path: the caller supplies recipients, subject, body and any
    attachments. Used for follow-up communication that is not tied to one
    meeting or task.
    """
    user_id = _require_auth(event)
    row = _require_integration(user_id, INTEGRATION_GMAIL)
    data = _body(event)

    resolved, unresolved = _resolve_contact_recipients(
        user_id, _recipient_payload(data))
    _require_resolved(resolved, unresolved)

    sent = _send_via_gmail(
        user_id, row,
        to=[email_message.format_recipient(r["name"], r["email"])
            for r in resolved],
        subject=data.get("subject"),
        body=data.get("body"),
        cc=[a for a in (email_message.normalize_email(c)
                        for c in (data.get("cc") or [])) if a],
        attachments=data.get("attachments"),
    )
    # Recipient COUNT, never addresses — see _audit's rule on personal data.
    _audit("gmail.sent", user_id, "generic", recipients=len(resolved),
           attachments=len(data.get("attachments") or []))
    return _resp(200, {"sent": True, **sent, "recipient_count": len(resolved)})


def gmail_meeting_recipients(event):
    """GET /integrations/gmail/recipients/{key+} (JWT)
    -> {recipients:[...], unresolved:[...]}

    The participant list for the Share sheet, already resolved to addresses.
    Returned BEFORE the user picks, so the sheet can show "Email address
    unavailable for Rahul" next to the person it applies to rather than
    failing at Send time.

    Requires Gmail: this is a Gmail-dependent surface, and the requirement is
    that the backend enforces that too, not only the UI.
    """
    user_id, key, item = _owned_recording(event, hydrate=False)
    _require_integration(user_id, INTEGRATION_GMAIL)

    rows = _participant_rows(key)
    recipients, unresolved = [], []
    seen = set()
    for row in rows:
        contact_id = str(row.get("contact_id") or "")
        if not contact_id:
            continue
        contact = _readable_contact(user_id, contact_id)
        if not contact:
            continue
        name = str(contact.get("name") or "")
        email = email_message.normalize_email(contact.get("email"))
        entry = {"contact_id": contact_id, "name": name, "email": email,
                 "speaker_id": str(row.get("speaker_id") or "")}
        if not email:
            unresolved.append(entry)
            continue
        if email in seen:
            continue
        seen.add(email)
        recipients.append(entry)

    return _resp(200, {"recipients": recipients, "unresolved": unresolved,
                       "meeting_title": str(item.get("title") or "")})


def gmail_send_meeting(event):
    """POST /integrations/gmail/send/meeting/{key+} (JWT) -> {sent, message_id}.

    MoM / summary / highlights / action-item sharing for ONE meeting the caller
    owns. The recording key comes LAST for the same API Gateway reason every
    other {key+} route gives: a greedy variable is only legal in final position.

    Ownership is checked by _owned_recording before anything else, so a caller
    cannot mail themselves someone else's meeting by guessing a key.
    """
    user_id, key, item = _owned_recording(event, hydrate=False)
    row = _require_integration(user_id, INTEGRATION_GMAIL)
    data = _body(event)

    resolved, unresolved = _resolve_contact_recipients(
        user_id, _recipient_payload(data))
    _require_resolved(resolved, unresolved)

    title = str(item.get("title") or "Meeting")
    subject = str(data.get("subject") or "").strip() or f"Minutes of Meeting — {title}"
    body = str(data.get("body") or "").strip() or _default_meeting_body(title)

    sent = _send_via_gmail(
        user_id, row,
        to=[email_message.format_recipient(r["name"], r["email"])
            for r in resolved],
        subject=subject, body=body,
        cc=[a for a in (email_message.normalize_email(c)
                        for c in (data.get("cc") or [])) if a],
        attachments=data.get("attachments"),
    )
    _audit("gmail.sent_meeting", user_id, key, recipients=len(resolved),
           attachments=len(data.get("attachments") or []))
    return _resp(200, {"sent": True, **sent, "recipient_count": len(resolved)})


def _default_meeting_body(title: str) -> str:
    """The fallback body when the user did not write one.

    Deliberately plain and deliberately CONTENT-FREE beyond the title: the
    user chooses what to share by choosing attachments and by editing this
    text. Auto-composing a summary into the body would send meeting content
    the user did not explicitly select, which the requirement rules out.
    """
    return (f"Hi,\n\nPlease find the Minutes of Meeting from {title}.\n\n"
            f"Regards,\nMinuteX")


def gmail_send_task(event):
    """POST /integrations/gmail/send/task/{task_id} (JWT) -> {sent, message_id}.

    Task communication — explicitly triggered from the task screen, never
    automatic. This is NOT a notification engine: nothing here schedules,
    batches or reacts to a task changing. One user action, one email.

    When no recipients are supplied, the task's ASSIGNEE is used — which is
    the whole point of sending a task by mail, and saves the app resolving it.
    """
    user_id = _require_auth(event)
    row = _require_integration(user_id, INTEGRATION_GMAIL)
    task_id = str((event.get("pathParameters") or {}).get("task_id") or "").strip()
    task = _owned_task(user_id, task_id)
    data = _body(event)

    requested = _recipient_payload(data) if isinstance(data.get("recipients"),
                                                       list) else []
    if not requested:
        assignee_contact = str(task.get("assignee_contact_id") or "")
        if not assignee_contact:
            raise ApiError(422, "This task has no assignee to email. "
                                "Choose a recipient.")
        requested = [{"contact_id": assignee_contact}]

    resolved, unresolved = _resolve_contact_recipients(user_id, requested)
    _require_resolved(resolved, unresolved)

    title = str(task.get("title") or "Task")
    subject = str(data.get("subject") or "").strip() or f"Action item — {title}"
    body = str(data.get("body") or "").strip() or _default_task_body(task, resolved)

    sent = _send_via_gmail(
        user_id, row,
        to=[email_message.format_recipient(r["name"], r["email"])
            for r in resolved],
        subject=subject, body=body,
        attachments=data.get("attachments"),
    )
    _audit("gmail.sent_task", user_id, task_id, recipients=len(resolved))
    return _resp(200, {"sent": True, **sent, "recipient_count": len(resolved)})


def _default_task_body(task: dict, resolved) -> str:
    """The fallback task email.

    Only fields the task ACTUALLY has are included — an empty "Due:" line
    invites the reader to infer a deadline that was never set, and inventing
    one is exactly the fabrication the project rules forbid.
    """
    greeting = ""
    if len(resolved) == 1 and resolved[0].get("name"):
        greeting = f"Hi {resolved[0]['name'].split()[0]},\n\n"

    lines = [f"Task:\n{task.get('title') or 'Untitled task'}"]
    notes = str(task.get("description") or "").strip()
    if notes:
        lines.append(f"Details:\n{notes}")
    due = str(task.get("due_date") or "").strip()
    if due:
        lines.append(f"Due:\n{due}")
    priority = str(task.get("priority") or "").strip()
    if priority:
        lines.append(f"Priority:\n{priority}")

    return greeting + "\n\n".join(lines) + "\n\nRegards,\nMinuteX"


# ---------------------------------------------------------------------------
# NOTIFICATIONS — the in-app notification engine (Phase 1).
#
# WHAT THIS IS. One place that turns a MinuteX BUSINESS EVENT into a
# notification for one user. Every event site in this file calls
# `_notify(...)` and nothing else: no route writes the Notifications table
# directly, and no UI component decides what a notification says.
#
#     business action succeeds
#            |
#            v
#     _notify(user_id, TYPE, entity_id, subject=...)
#            |
#            +-- notification_schema.build()   copy + priority + entity kind
#            +-- dedupe claim (conditional)    "this fact, once"
#            +-- Notifications row             the stored record
#            |
#            v
#     GET /notifications  ->  in-app notification centre
#
# THE ORDERING RULE, AND WHY IT IS ABSOLUTE. A notification is only ever
# raised AFTER the business write it describes has succeeded. "A task was
# assigned to you" that links to a task which was never created is worse than
# no notification: the user taps it, gets a 404, and learns not to trust the
# bell. So every call site here sits after its _write_task / _apply_update /
# _upsert, never before and never in the same try block.
#
# THE FAILURE RULE, AND WHY IT IS THE OPPOSITE. A notification failing must
# NEVER fail the business action. The task was genuinely created; answering
# 500 because the bell could not be updated would turn a cosmetic problem into
# a data-entry one, and the client's retry would then create a second task.
# So `_notify` swallows and logs. This is the same best-effort reasoning
# _mirror_task_to_recording already documents.
#
# NEVER NOTIFY THE ACTOR. Every event site passes the RECIPIENT, and _notify
# drops the write when the recipient is the person who caused the event. A
# user who assigns a task to themselves already knows; telling them is the
# noise the requirement rules out. This is enforced HERE rather than at each
# call site, so a new event site cannot forget it.
#
# GMAIL IS NOT INVOLVED. This engine has no email path, imports nothing from
# the Gmail section, and is not reachable from it. Gmail remains what it was:
# a communication integration the user triggers by hand. When EMAIL becomes a
# delivery channel it will be a consumer of the rows written here, reading the
# `channels` seam — not a second place notifications are decided.
# ---------------------------------------------------------------------------

# How long a dedupe claim is kept before DynamoDB's TTL reaps it.
#
# 90 days, which is comfortably longer than any fact this system dedupes stays
# interesting: a "due today" claim matters for one day, a "processing
# completed" claim for as long as someone might reprocess that meeting. The
# cost of it being too LONG is a few bytes; the cost of it being too SHORT is
# a duplicate notification, so it is deliberately generous.
NOTIFICATION_DEDUPE_TTL_DAYS = int(
    os.environ.get("NOTIFICATION_DEDUPE_TTL_DAYS", "90"))

# Page sizes for GET /notifications. Same convention as the tasks list.
NOTIFICATIONS_PAGE_DEFAULT = 20
NOTIFICATIONS_PAGE_MAX = 50

# Ceiling for one mark-all-read call. A user with a huge unread backlog is
# served across several calls rather than one request that risks the gateway's
# 30s timeout half-way through — the response says whether more remain, so the
# client can finish the job. Chosen well under what 29s allows.
NOTIFICATIONS_MARK_ALL_MAX = 500


def _notification_row(user_id, built, entity_id, dedupe):
    """Assemble one Notifications item from a built notification.

    The SHAPE lives in notification_schema.make_row so this Lambda and
    transcribeRecording write identical rows — only the id and the clock are
    supplied here.
    """
    return notification_schema.make_row(
        user_id, built, entity_id, dedupe,
        notification_id=uuid.uuid4().hex[:20], now=_now_iso())


def _claim_notification(dedupe):
    """Win the right to write this notification, or return False.

    The uniqueness mechanism. DynamoDB can only enforce a condition against an
    item's OWN key, so the claim is an item whose KEY IS the dedupe key —
    written with attribute_not_exists, which exactly one caller can win.
    Whoever wins writes the notification; everyone else drops the event.

    This is what makes the whole engine idempotent under retries, reprocessing,
    concurrent readers and the deadline sweep running on every list call. It is
    the same conditional-claim shape _folder_name_claim uses for folder names.

    Fails OPEN on an unexpected error: if the claim table is unreachable, a
    possible duplicate notification is a better outcome than silently losing a
    real one, and the caller's log records it.
    """
    if not dedupe:
        return True
    expires = int(time.time()) + NOTIFICATION_DEDUPE_TTL_DAYS * 86400
    try:
        _notification_dedupe.put_item(
            Item={"dedupe_key": dedupe, "created_at": _now_iso(),
                  "expires_at": expires},
            ConditionExpression="attribute_not_exists(dedupe_key)",
        )
        return True
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") \
                == "ConditionalCheckFailedException":
            return False
        print(f"[notify] dedupe claim errored for {dedupe}: {err}")
        return True
    except Exception as err:  # noqa: BLE001
        # Broad on purpose, and it is what makes "fails open" true rather than
        # aspirational: an unreachable claim table raises something other than
        # ClientError, and letting that escape would reach _notify's outer
        # handler and drop the notification — turning a duplicate-prevention
        # mechanism into a notification-LOSS mechanism. A possible duplicate is
        # the strictly better failure.
        print(f"[notify] dedupe claim failed for {dedupe}: "
              f"{type(err).__name__}: {err}")
        return True


def _notify(user_id, notification_type, entity_id, *, subject="",
            metadata=None, title="", message="", dedupe_day="",
            actor_user_id=""):
    """Raise ONE notification for ONE user. The only way notifications are made.

    Returns the created row, or None when nothing was written — which is a
    normal outcome, not a failure: no recipient, the recipient is the actor, or
    the fact was already notified.

    `dedupe_day` makes the identity per-day instead of once-ever (the deadline
    types). `actor_user_id` is who CAUSED the event, and is never notified.

    Never raises. See the failure rule in this section's header: the business
    action has already succeeded by the time this runs, and it must not be
    undone by a notification problem.
    """
    try:
        recipient = str(user_id or "").strip()
        if not recipient:
            # Not an error: an unassigned task, or one assigned to a contact
            # with no MinuteX account, has nobody to notify. Silence is the
            # correct behaviour — there is no user to tell.
            return None
        if actor_user_id and recipient == str(actor_user_id).strip():
            return None

        built = notification_schema.build(
            notification_type, subject=subject, metadata=metadata,
            title=title, message=message)
        dedupe = notification_schema.dedupe_key(
            recipient, notification_type, entity_id, dedupe_day)
        if not _claim_notification(dedupe):
            return None

        row = _notification_row(recipient, built, entity_id, dedupe)
        _notifications.put_item(Item=row)
        _audit("notification.created", recipient, row["notification_id"],
               type=notification_type, entity=built["entity_type"])
        return row
    except Exception as err:  # noqa: BLE001
        # Deliberately broad. Every caller is a business action that has
        # already committed, and there is no notification failure worth
        # failing it for.
        print(f"[notify] FAILED type={notification_type} "
              f"user={user_id}: {type(err).__name__}: {err}")
        return None


def _recording_title(item):
    """The meeting label a notification shows, or an honest placeholder.

    Never invents one: an untitled recording is a real state (AI titling can
    fail), and "Untitled meeting" says so rather than guessing from the
    transcript — which would be exactly the fabrication the project rules
    forbid.
    """
    return (str((item or {}).get("title") or "").strip()
            or notification_schema.UNTITLED_MEETING)


def _task_title(row):
    return (str((row or {}).get("title") or "").strip()
            or notification_schema.UNTITLED_TASK)


def _task_assignee_user(row):
    """The MinuteX USER a task is assigned to, or "".

    This is the whole basis of task notifications, and it is deliberately NOT
    the assignee's name or email. A task can name "Rahul Sharma" without Rahul
    having a MinuteX account — that is an UNRESOLVED assignee, and there is no
    inbox to notify. `assignee_user_id` is only ever written from a Contact
    that is LINKED to a real account (see _new_task_row), so its presence is
    exactly the condition "there is a person here who can receive this".
    """
    return str((row or {}).get("assignee_user_id") or "").strip()


# ---------------------------------------------------------------------------
# Event sites — the notifications each MinuteX event raises.
#
# Each helper is named for the BUSINESS event, not for the notification, so a
# call site reads as "this happened" rather than "send this". They are grouped
# here rather than inlined so the complete set of things MinuteX notifies
# about can be read in one place — which is what stops the noise the
# requirement warns against creeping in one route at a time.
# ---------------------------------------------------------------------------
def _notify_task_assigned(task_row, *, actor_user_id="", meeting_title=""):
    """A task now has an assignee who holds a MinuteX account."""
    recipient = _task_assignee_user(task_row)
    if not recipient:
        return None
    meta = {}
    if task_row.get("source_recording_id"):
        meta["recording_key"] = task_row["source_recording_id"]
    if meeting_title:
        meta["meeting_title"] = meeting_title
    return _notify(recipient, notification_schema.TYPE_TASK_ASSIGNED,
                   task_row.get("task_id"), subject=_task_title(task_row),
                   metadata=meta, actor_user_id=actor_user_id)


def _notify_task_reassigned(previous_user_id, task_row, *, actor_user_id=""):
    """A task moved AWAY from someone who held a MinuteX account.

    Only the departing assignee gets this; the arriving one gets
    TASK_ASSIGNED from _notify_task_assigned. Nobody else is told — the
    requirement is explicit that unrelated users are not notified, and the
    task's owner is almost always the actor anyway.
    """
    previous = str(previous_user_id or "").strip()
    if not previous:
        return None
    if previous == _task_assignee_user(task_row):
        return None  # not actually a reassignment
    return _notify(previous, notification_schema.TYPE_TASK_REASSIGNED,
                   task_row.get("task_id"), subject=_task_title(task_row),
                   actor_user_id=actor_user_id)


def _notify_ai_action_required(task_row):
    """An AI-extracted task names an assignee the system could not resolve.

    This is the "AI must not silently assign uncertain work" rule made
    visible. The AI heard a name; MinuteX could not match it to a contact, so
    rather than guessing a person (or dropping the assignment quietly) it asks
    the OWNER to confirm. The owner is the recipient because they are the only
    one who can resolve it — the intended assignee has no account to notify,
    which is precisely why it is unresolved.
    """
    owner = str(task_row.get("owner_user_id") or "").strip()
    if not owner:
        return None
    meta = {"resolution_status": str(task_row.get("resolution_status") or "")}
    if task_row.get("source_recording_id"):
        meta["recording_key"] = task_row["source_recording_id"]
    name = str(task_row.get("assignee_name_legacy") or "").strip()
    if name:
        meta["assignee_name"] = name
    return _notify(owner, notification_schema.TYPE_AI_ACTION_REQUIRED,
                   task_row.get("task_id"), subject=_task_title(task_row),
                   metadata=meta)


def _notify_document_ready(user_id, key, item, doc_type, label=""):
    """A meeting-generated document finished generating.

    ONE path for every document type (summary, MoM, custom, Quick AI) — the
    requirement rules out per-type notification logic, and the generic
    Documents model already makes that unnecessary. The document TYPE travels
    in metadata so the app can scroll to it; the notification itself points at
    the MEETING, because that is where documents are read.

    Deduped on the meeting AND the document type, so regenerating a document
    the user already has does not re-notify, while a DIFFERENT document from
    the same meeting still does.
    """
    title = _recording_title(item)
    shown = str(label or "").strip() or str(doc_type or "").strip()
    return _notify(
        user_id, notification_schema.TYPE_MEETING_DOCUMENT_READY, key,
        message=f"{title} — {shown}" if shown else title,
        metadata={"document_type": str(doc_type or ""), "meeting_title": title},
        dedupe_day=str(doc_type or ""),
    )


def _notify_meeting_shared(user_id, key, item, share_id=""):
    """A read-only public link was created for a meeting's outputs."""
    return _notify(user_id, notification_schema.TYPE_MEETING_OUTPUT_SHARED,
                   key, subject=_recording_title(item),
                   metadata={"share_id": str(share_id or "")},
                   # Per share, not per meeting: creating a second link is a
                   # second real event worth its own row.
                   dedupe_day=str(share_id or ""))


# ---------------------------------------------------------------------------
# Deadline notifications — TASK_DUE_TODAY / TASK_OVERDUE.
#
# WHY THESE ARE SWEPT RATHER THAN SCHEDULED. A deadline is not an event
# anything in MinuteX causes: no request happens at the moment a task becomes
# overdue. The two honest ways to notice are a scheduled job (EventBridge) or
# a sweep when the user's tasks are read. This implements the SWEEP, because:
#
#   * it needs no new infrastructure, which the project rules ask us to avoid
#     adding without justification;
#   * it is exactly-once per day REGARDLESS of how often it runs, because the
#     dedupe key carries the day — so "every app open" costs nothing;
#   * a user who never opens MinuteX gets no in-app notification, which is
#     correct: an in-app notification only exists to be seen in the app. When
#     EMAIL becomes a channel, THAT is when a scheduled job earns its place,
#     and it will call this same function.
#
# The sweep is bounded (it only ever looks at tasks already read for another
# purpose) and it never blocks the response — a failure inside _notify is
# swallowed, so the task list is served either way.
# ---------------------------------------------------------------------------
def _sweep_task_deadlines(user_id, rows, now=None):
    """Raise due-today / overdue notifications for the caller's OWN tasks.

    `rows` are tasks that have ALREADY been read and ownership-checked by the
    caller, so this adds no reads of its own.

    Only tasks assigned to THIS user are considered — `assignee_user_id`, the
    same condition every other task notification uses. A task the user owns
    but assigned to someone else is that person's deadline to be reminded of,
    not theirs.

    Returns the number of notifications raised (used by tests; callers ignore
    it — the sweep is a side effect of reading, never something a response
    reports).
    """
    moment = now or datetime.now(timezone.utc)
    today = notification_schema.today_iso(moment)
    raised = 0
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        if row.get("owner_user_id") != user_id:
            continue
        if _task_assignee_user(row) != user_id:
            continue
        if row.get("status") in TASK_TERMINAL_STATUSES:
            continue

        due = (str(row.get("due_date_normalized") or "").strip()
               or str(row.get("due_date") or "").strip())
        if not due:
            continue

        if _is_overdue(row.get("due_date"), row.get("status"),
                       row.get("due_date_normalized", "")):
            ntype = notification_schema.TYPE_TASK_OVERDUE
        elif due[:10] == today:
            # Due today and not yet past — a bare date is compared on the day
            # itself, which is why _is_overdue is asked first: a task due
            # today only becomes overdue at the end of the day, and until then
            # "due today" is the true statement.
            ntype = notification_schema.TYPE_TASK_DUE_TODAY
        else:
            continue

        # The day is part of the identity, so this is at most one per task per
        # day per type — on every app open, from every device, forever.
        if _notify(user_id, ntype, row.get("task_id"),
                   subject=_task_title(row), dedupe_day=today,
                   metadata={"due_date": str(row.get("due_date") or "")}):
            raised += 1
    return raised


# ---------------------------------------------------------------------------
# Notification API — the read side.
#
# SECURITY. Every route derives the user from the JWT (_require_auth) and
# never from the request. Reads are keyed by that user_id on the user-index;
# writes go through _owned_notification, which re-checks the row's user_id
# after the GetItem. A notification_id is therefore not a capability: holding
# someone else's id gets a 404, the same answer an id that does not exist
# gets, so the API does not confirm the row's existence either.
# ---------------------------------------------------------------------------
def _owned_notification(user_id, notification_id):
    """One notification belonging to this user, or 404.

    404 rather than 403 ON PURPOSE, matching _owned_task / _owned_contact /
    _owned_folder: telling a caller "this exists but is not yours" leaks that
    the id is real. Same answer for both cases, no oracle.
    """
    nid = str(notification_id or "").strip()
    if not nid:
        raise ApiError(400, "notification id required")
    row = _notifications.get_item(Key={"notification_id": nid}).get("Item")
    if not row or row.get("user_id") != user_id:
        raise ApiError(404, "notification not found")
    return row


def list_notifications(event):
    """GET /notifications?limit=&cursor=&unread=  -> {notifications, count,
                                                     next_cursor, unread_count}

    Newest first, paginated — the app never loads a user's whole history.
    `unread=1` narrows to unread rows, served from the sparse unread index so
    it stays cheap no matter how much read history has accumulated.

    The unread COUNT rides along on the first page (no cursor) so the centre
    can render its badge without a second round trip on open. Later pages omit
    it: it is a property of the inbox, not of the page, and recomputing it per
    page would be a read the client already has the answer to.
    """
    user_id = _require_auth(event)
    qs = event.get("queryStringParameters") or {}
    limit = _clean_limit(qs.get("limit"), NOTIFICATIONS_PAGE_DEFAULT,
                         NOTIFICATIONS_PAGE_MAX)
    unread_only = str(qs.get("unread") or "").strip().lower() in ("1", "true")

    if unread_only:
        query = {"IndexName": NOTIFICATIONS_UNREAD_INDEX,
                 "KeyConditionExpression": Key("user_id").eq(user_id)}
    else:
        query = {"IndexName": NOTIFICATIONS_USER_INDEX,
                 "KeyConditionExpression": Key("user_id").eq(user_id)}
    query["ScanIndexForward"] = False
    query["Limit"] = limit

    cursor = _decode_cursor(qs.get("cursor"))
    if cursor:
        query["ExclusiveStartKey"] = cursor

    res = _notifications.query(**query)
    # Re-checked against the caller even though the index is keyed by user_id:
    # an index is a lookup path, never an authorization decision. Same rule
    # list_all_tasks states.
    rows = [r for r in res.get("Items", []) if r.get("user_id") == user_id]

    body = {
        "notifications": [notification_schema.public_notification(r)
                          for r in rows],
        "count": len(rows),
        "next_cursor": _encode_cursor(res.get("LastEvaluatedKey")),
    }
    if not cursor:
        body["unread_count"] = _unread_count(user_id)
    return _resp(200, body)


def _unread_count(user_id):
    """How many unread notifications this user has.

    Counted over the SPARSE unread index, so the work is proportional to the
    unread rows — not to the user's whole notification history. A user with
    three unread and four years of read notifications pays for three.

    Uses Select=COUNT so DynamoDB never ships the items themselves.
    """
    total, start_key = 0, None
    # Paged because COUNT is still subject to the 1 MB scan limit per call.
    # Bounded by the same page ceiling the filtered task list uses, so a
    # pathological backlog cannot make the badge query unbounded.
    for _ in range(_SEARCH_MAX_PAGES):
        query = {"IndexName": NOTIFICATIONS_UNREAD_INDEX,
                 "KeyConditionExpression": Key("user_id").eq(user_id),
                 "Select": "COUNT"}
        if start_key:
            query["ExclusiveStartKey"] = start_key
        res = _notifications.query(**query)
        total += int(res.get("Count") or 0)
        start_key = res.get("LastEvaluatedKey")
        if not start_key:
            break
    return total


def get_unread_count(event):
    """GET /notifications/unread-count -> {unread_count}

    Separate from the list on purpose (section 24): the badge is polled far
    more often than the centre is opened, and it must not pay for a page of
    notification bodies to render a number.
    """
    user_id = _require_auth(event)
    return _resp(200, {"unread_count": _unread_count(user_id)})


def _mark_read(row):
    """Flip one row to read, and drop it out of the unread index.

    REMOVING `unread_marker` is what takes the row out of the sparse index —
    that is the mechanism, not a cleanup. Writing is_read=true alone would
    leave the badge counting it forever.

    Conditional on the row still being unread so a double-tap (or two devices)
    cannot overwrite the original read_at with a later one.
    """
    now = _now_iso()
    try:
        _notifications.update_item(
            Key={"notification_id": row["notification_id"]},
            UpdateExpression="SET is_read = :t, read_at = :now "
                             "REMOVE unread_marker",
            ConditionExpression="attribute_exists(unread_marker)",
            ExpressionAttributeValues={":t": True, ":now": now},
        )
        return True
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") \
                == "ConditionalCheckFailedException":
            return False   # already read — the desired end state either way
        raise


def mark_notification_read(event):
    """POST /notifications/{notification_id}/read -> {notification}

    Idempotent: marking an already-read notification succeeds and returns it
    unchanged, because the client's goal ("this is read") is already true. An
    error there would make a double-tap look like a failure.
    """
    user_id = _require_auth(event)
    nid = (event.get("pathParameters") or {}).get("notification_id")
    row = _owned_notification(user_id, nid)

    if _mark_read(row):
        row = dict(row)
        row["is_read"] = True
        row["read_at"] = _now_iso()
        row.pop("unread_marker", None)
        _audit("notification.read", user_id, row["notification_id"])

    return _resp(200, {
        "notification": notification_schema.public_notification(row),
        "unread_count": _unread_count(user_id),
    })


def mark_all_notifications_read(event):
    """POST /notifications/read-all -> {marked, unread_count, remaining}

    Sweeps the sparse unread index, which is exactly the set that needs
    changing — a scan over all notifications filtering on is_read would get
    slower every week the user keeps the app.

    Bounded per call (NOTIFICATIONS_MARK_ALL_MAX). `remaining` tells the
    client whether to call again, so a very large backlog is finished across
    calls instead of risking the gateway timeout mid-sweep. In practice one
    call clears any realistic inbox.
    """
    user_id = _require_auth(event)
    marked, start_key, hit_ceiling = 0, None, False

    while True:
        query = {"IndexName": NOTIFICATIONS_UNREAD_INDEX,
                 "KeyConditionExpression": Key("user_id").eq(user_id)}
        if start_key:
            query["ExclusiveStartKey"] = start_key
        res = _notifications.query(**query)

        for row in res.get("Items", []):
            # The index is keyed by user_id, but ownership is re-checked
            # before a WRITE for the same reason it is before a read.
            if row.get("user_id") != user_id:
                continue
            if marked >= NOTIFICATIONS_MARK_ALL_MAX:
                hit_ceiling = True
                break
            if _mark_read(row):
                marked += 1

        start_key = res.get("LastEvaluatedKey")
        if hit_ceiling or not start_key:
            break

    remaining = _unread_count(user_id)
    _audit("notification.read_all", user_id, "-", marked=marked)
    return _resp(200, {"marked": marked, "unread_count": remaining,
                       "remaining": remaining})


# ---------------------------------------------------------------------------
# EAGER TASK SEEDING — the pipeline's entry point into the task layer.
#
# WHY THIS EXISTS. Task seeding used to happen only LAZILY: `_seed_ai_tasks`
# ran when somebody opened a task list. That made the Tasks table a function of
# who had browsed where, which is wrong in a specific and damaging way —
# a meeting could finish processing, extract five real action items, and none
# of them existed anywhere the product could see:
#
#   * GET /tasks (the Task Tracker) showed nothing from that meeting;
#   * the assignee never got TASK_ASSIGNED, because nothing had been created
#     to notify them about;
#   * AI_ACTION_REQUIRED never reached the owner, so an ambiguous assignment
#     sat unreviewed indefinitely.
#
# All three resolved themselves the moment someone opened the meeting, which is
# exactly what made it easy to miss: the bug is invisible to anyone testing by
# opening the meeting they just recorded.
#
# WHY IT IS AN INVOKE RATHER THAN A COPY. The seeder is not a small function.
# It reaches Tasks, Contacts, MeetingParticipants, Recordings and the two
# notification tables, and it carries the fingerprint/tombstone rules, the
# assignee-resolution chain and the meeting-anchored date normalisation. That
# logic must exist exactly once. transcribeRecording therefore does NOT
# reimplement any of it — it asks THIS Lambda to run the seeder it already
# owns, over the row it has just finished writing.
#
# This is the same cross-Lambda shape the pipeline already uses in the other
# direction (userApi hands the STT analysis to transcribeRecording as a typed
# async invoke — see the stt.completed section), so it introduces no new
# infrastructure and no new failure mode, only a second traveller on a proven
# road.
#
# IDEMPOTENCY IS WHAT MAKES THIS SAFE. Nothing here is a new guarantee: the
# seeder was ALREADY idempotent, because the lazy path could run on every
# single read. Eager seeding just adds one more caller to a function built to
# be called repeatedly. The lazy call stays exactly where it was, as the safety
# net for anything this invoke misses (a legacy row, a failed invoke, a
# recording that predates this feature).
INTERNAL_SEED_TASKS_EVENT = "tasks.seed"


def handle_seed_tasks_event(event):
    """Seed one meeting's AI tasks, invoked by the pipeline. NOT an HTTP route.

    There is deliberately no JWT here and no _require_auth: the caller is our
    own transcribeRecording Lambda, authenticated by IAM at the invoke
    boundary, and it has no user session to present. The tenant is taken from
    the RECORDING ROW's stamped `user_id` — the same value _owned_recording
    checks a JWT against — so this path cannot seed tasks for anyone other than
    the recording's real owner, and it cannot be reached from the internet at
    all (API Gateway only ever sends events carrying a routeKey).

    Returns a small dict rather than an HTTP response: the invoke is async and
    nobody reads the body, but it is what the logs and the tests assert on.
    """
    key = str((event or {}).get("audio_s3_key") or "").strip()
    if not key:
        print("[seed] no audio_s3_key on the seed event")
        return {"seeded": 0, "error": "audio_s3_key required"}

    item = _recordings.get_item(Key={"audio_s3_key": key}).get("Item")
    if not item:
        # The row should exist — the pipeline has just written it — so this is
        # worth a log line rather than a silent return.
        print(f"[seed] recording not found: {key}")
        return {"seeded": 0, "error": "recording not found"}

    # THE TENANT, taken from the row and never from the event. An event that
    # named its own user_id would be a way to write tasks into someone else's
    # account, so the field is not read even if present.
    user_id = str(item.get("user_id") or "").strip()
    if not user_id:
        # A legacy recording whose ownership is only resolvable through the
        # UserDevices join. Seeding needs a definite owner to stamp on the
        # rows, so this one waits for the lazy path, where the JWT supplies it.
        print(f"[seed] {key} has no stamped owner — leaving it to the "
              f"lazy path")
        return {"seeded": 0, "skipped": "no owner"}

    # The SAME two calls list_meeting_tasks makes, in the same order, for the
    # same reasons — migration first so a legacy embedded task is not seeded a
    # second time under a new id. Both are idempotent; that is precisely why
    # this can also run lazily afterwards without creating anything twice.
    migrated = _migrate_embedded_tasks(user_id, key, item)
    seeded = _seed_ai_tasks(user_id, key, item)

    _audit("tasks.seeded", user_id, key, seeded=seeded, migrated=migrated)
    return {"seeded": seeded, "migrated": migrated, "key": key}


# ===========================================================================
# WORKSPACE ROUTES (Phase 2A — READ ONLY)
#
# Two routes, both reads. Phase 2A deliberately ships NO route that creates an
# organisation, invites anyone, or changes a role: those depend on the
# unresolved identity decision recorded at the bottom of workspace_schema.py,
# and shipping half of an invitation flow would bake in an answer to a question
# the product has not settled.
#
# What these two DO give is the thing every later phase needs: a client can
# discover which workspaces it may act in, and the server has one verified
# place that decides. Personal-only users see exactly what they see today.
# ===========================================================================
def list_workspaces(event):
    """GET /workspaces -> {workspaces, count, current_workspace_id}

    Every workspace the caller may act in, personal first. The personal one is
    SYNTHESIZED when its row does not exist yet, so this returns the right
    answer before the backfill has run — see _user_workspaces.

    Creating the row is a side effect of the first read rather than a
    migration step (_ensure_personal_workspace is idempotent and conditional),
    which is what makes scripts/52 optional rather than required.
    """
    user_id = _require_auth(event)
    _ensure_personal_workspace(user_id)

    out = []
    for workspace, role, membership in _user_workspaces(user_id):
        out.append(workspace_schema.public_workspace(
            workspace, role=role, membership=membership))

    # What the CLIENT asked for, echoed back only when it is genuinely usable.
    # A header naming a workspace the caller is not in resolves to their
    # personal workspace instead of erroring: the request is a hint, and an
    # invalid hint should not break a list route.
    requested = _requested_workspace_id(event)
    current = _personal_workspace_id(user_id)
    if requested and _active_membership(requested, user_id):
        current = requested

    return _resp(200, {"workspaces": out, "count": len(out),
                       "current_workspace_id": current})


def get_workspace(event):
    """GET /workspaces/{workspace_id} -> {workspace}

    404 for a workspace the caller is not an active member of — the same
    answer a nonexistent id gets, for the same reason every _owned_* helper in
    this file returns 404 rather than 403.
    """
    workspace_id = _url_unquote(
        (event.get("pathParameters") or {}).get("workspace_id", ""))
    user_id, wid, membership = _require_workspace_member(
        event, workspace_id=workspace_id)

    workspace = _workspace_row(wid)
    if workspace is None and workspace_schema.is_personal_workspace_id(wid):
        # Personal workspace that has never been written. Synthesize rather
        # than 404: it exists conceptually for every user from signup onward.
        workspace = _ensure_personal_workspace(user_id)
    if not workspace:
        raise ApiError(404, "workspace not found")

    return _resp(200, {"workspace": workspace_schema.public_workspace(
        workspace, role=membership.get("role", ""), membership=membership)})


def create_workspace(event):
    """POST /workspaces {name, company_name?, domain?, ...} -> 201 {workspace}

    Creates an ORGANISATION. Personal workspaces are never created here — they
    are derived from the user id and materialized by _ensure_personal_workspace,
    so there is nothing for a client to create and no way to ask for a second
    one.

    IDEMPOTENT AGAINST CLIENT RETRIES. A double-tap or a retried request would
    otherwise leave the user owning two identical organisations, which is not
    something they can easily undo (organisation deletion is a later phase).
    An `idempotency_key` from the client is claimed exactly once, using the
    same conditional-claim-row pattern folder names and signup emails use.
    Without a key the request is still safe — it just cannot be deduplicated,
    so the client is expected to send one.
    """
    user_id = _require_auth(event)
    data = _body(event)

    try:
        name = workspace_schema.clean_name(data.get("name"))
        profile = workspace_schema.clean_org_profile(data)
    except workspace_schema.WorkspaceValidationError as err:
        raise ApiError(400, str(err))

    # THE IDENTITY GATE (Phase 2C). An identity that has already USED its
    # personal workspace must not also become an organisation owner — that is
    # the silent conversion the identity model forbids. See the ORGANISATION
    # CREATION block in workspace_schema for why this tests for personal
    # RESOURCES rather than for the existence of the derived workspace row.
    #
    # Checked BEFORE the idempotency claim and before any write, so a refused
    # attempt leaves nothing behind and can be retried from a work identity.
    #
    # An identity that is ALREADY an organisation member is exempt: it has
    # demonstrably not been converted (it was an organisation identity when it
    # joined), and blocking it would stop a legitimate owner from starting a
    # second organisation.
    if not _has_organisation_membership(user_id) \
            and _has_personal_resources(user_id):
        raise PersonalWorkspaceInUse(
            workspace_schema.PERSONAL_WORKSPACE_IN_USE_MESSAGE)

    # The retry guard. Claimed BEFORE the workspace so a retry that arrives
    # while the first is still in flight loses the race rather than creating
    # a twin.
    claim_id = ""
    idem = str(data.get("idempotency_key") or "").strip()[:64]
    if idem:
        claim_id = f"idem#{user_id}#{idem}"
        try:
            _workspaces.put_item(
                Item={"workspace_id": claim_id, "owner_user_id": user_id,
                      "created_at": _now_iso()},
                ConditionExpression="attribute_not_exists(workspace_id)")
        except ClientError as err:
            if err.response.get("Error", {}).get("Code") \
                    == "ConditionalCheckFailedException":
                raise ApiError(409, "this organisation was already created")
            raise

    workspace = workspace_schema.new_organisation_workspace(
        user_id, name, _now_iso(), profile=profile)
    workspace_id = workspace["workspace_id"]

    try:
        _workspaces.put_item(
            Item=workspace,
            ConditionExpression="attribute_not_exists(workspace_id)")
        # The OWNER membership is what makes the workspace usable — without it
        # the creator could not read back what they just made. Written second
        # so a failure here leaves a workspace with no members rather than a
        # membership pointing at nothing; the former is recoverable by
        # re-running, the latter is a dangling grant.
        _memberships.put_item(
            Item=workspace_schema.new_membership(
                workspace_id, user_id, workspace_schema.ROLE_OWNER,
                _now_iso()))
    except Exception:
        if claim_id:
            # Never strand a claim: it would make that idempotency key
            # permanently unusable for this user.
            _workspaces.delete_item(Key={"workspace_id": claim_id})
        raise

    _audit("workspace.created", user_id, workspace_id)
    return _resp(201, {"workspace": workspace_schema.public_workspace(
        workspace, role=workspace_schema.ROLE_OWNER)})


def list_members(event):
    """GET /workspaces/{workspace_id}/members -> {members, count}

    Any ACTIVE member may see who else is in their organisation — knowing your
    colleagues is not a privileged action, and hiding it would make task
    assignment unusable. Managing them is separate and gated below.

    REMOVED rows are filtered out rather than returned with a status: a former
    member is not a member, and listing them invites a client to render them.
    """
    workspace_id = _url_unquote(
        (event.get("pathParameters") or {}).get("workspace_id", ""))
    user_id, wid, _ = _require_workspace_member(event,
                                                workspace_id=workspace_id)

    rows = _query_all(_memberships,
                      KeyConditionExpression=Key("workspace_id").eq(wid))
    active = [r for r in rows
              if r.get("status") == workspace_schema.MEMBERSHIP_ACTIVE]

    # One BatchGetItem for the profiles rather than a GetItem per member —
    # the N+1 the brief's performance section rules out.
    users = _users_by_ids([r.get("user_id", "") for r in active])
    members = [workspace_schema.public_member(r, users.get(r.get("user_id")))
               for r in active]
    # workspace_schema is pure (no S3 client), so the stored avatar KEY is
    # turned into a presigned GET here — the same contract _public_user has.
    # Without this the app gets a key it cannot render and every member falls
    # back to initials.
    for m in members:
        m["avatar_view_url"] = _avatar_view_url(m.get("avatar_url", ""))
    members.sort(key=lambda m: (workspace_schema.ROLES.index(m["role"])
                                if m["role"] in workspace_schema.ROLES else -1,
                                m.get("email", "")), reverse=True)
    return _resp(200, {"members": members, "count": len(members),
                       "workspace_id": wid})


def _users_by_ids(user_ids):
    """{user_id: row} for a list of ids, batched.

    Narrow by construction: only the display fields a members list needs are
    projected, so no password material or unrelated account state can reach a
    caller through this path. Same discipline as _linked_avatar_map.
    """
    ids = {str(u).strip() for u in (user_ids or []) if str(u or "").strip()}
    if not ids or not USERS_TABLE:
        return {}
    out = {}
    ids = list(ids)
    try:
        for start in range(0, len(ids), 100):  # BatchGetItem caps at 100
            chunk = ids[start:start + 100]
            resp = _ddb.batch_get_item(RequestItems={USERS_TABLE: {
                "Keys": [{"user_id": u} for u in chunk],
                "ProjectionExpression": "user_id, email, #n, avatar_url",
                "ExpressionAttributeNames": {"#n": "name"},
            }})
            for row in resp.get("Responses", {}).get(USERS_TABLE, []):
                out[row.get("user_id")] = row
    except ClientError as err:
        # A profile read failure must not empty the members list — the
        # memberships themselves are the authoritative part.
        print(f"[workspace] member profile read failed: {err}")
    return out


def invite_member(event):
    """POST /workspaces/{workspace_id}/members/invite {email, role}
       -> 201 {invitation, invite_token}

    OWNER/MANAGER only. The RAW token is returned exactly once, here, and is
    never stored or logged — only its sha256 goes to the database, the same
    one-shot contract share links use.
    """
    workspace_id = _url_unquote(
        (event.get("pathParameters") or {}).get("workspace_id", ""))
    user_id, wid, _ = _require_workspace_capability(
        event, workspace_schema.CAP_MANAGE_MEMBERS, workspace_id=workspace_id)

    workspace = _workspace_row(wid)
    if not workspace_schema.is_organisation_workspace_id(wid):
        raise ApiError(400, "only an organisation can have members")

    data = _body(event)
    try:
        email = workspace_schema.clean_email(data.get("email"))
        role = workspace_schema.clean_role(data.get("role"))
    except workspace_schema.WorkspaceValidationError as err:
        raise ApiError(400, str(err))

    # A MANAGER cannot mint an OWNER. Ownership is transferred deliberately,
    # by an owner, through a route that does not exist yet — not handed out
    # with an invitation.
    if role == workspace_schema.ROLE_OWNER:
        raise ApiError(403, "an organisation owner cannot be invited; "
                            "transfer ownership instead")

    # Already a member? Say so rather than sending a link that would no-op.
    existing_user = _user_by_email(email)
    if existing_user:
        current = _active_membership(wid, existing_user.get("user_id", ""))
        if current:
            raise ApiError(409, "this person is already a member")

    token = workspace_schema.new_invite_token()
    invitation = workspace_schema.new_invitation(
        wid, email, role, user_id, token, _now_iso())
    _invitations.put_item(Item=invitation)

    # entity is the invitation id, never the email or the token — the audit
    # trail carries ids and counts only (see _audit).
    _audit("workspace.invited", user_id, invitation["invitation_id"],
           workspace=wid, role=role)
    return _resp(201, {
        "invitation": workspace_schema.public_invitation(invitation),
        # The ONE time this exists outside the client's hands.
        "invite_token": token,
        "workspace_name": (workspace or {}).get("name", ""),
    })


def _user_by_email(email_lc):
    """The Users row for an email, or None. Claim rows can never match."""
    if not email_lc:
        return None
    try:
        res = _users.query(IndexName=EMAIL_INDEX,
                           KeyConditionExpression=Key("email").eq(email_lc),
                           Limit=1)
    except ClientError as err:
        print(f"[workspace] user lookup failed: {err}")
        return None
    items = res.get("Items") or []
    return items[0] if items else None


def _invitation_by_token(token):
    """The invitation a raw token addresses, or None.

    The token is hashed before it is used as a key, so the raw value never
    reaches a query, a log or an index.
    """
    if not workspace_schema.token_looks_valid(token):
        return None
    try:
        res = _invitations.query(
            IndexName=INVITATIONS_TOKEN_INDEX,
            KeyConditionExpression=Key("token_hash").eq(
                workspace_schema.hash_invite_token(token)),
            Limit=1)
    except ClientError as err:
        print(f"[workspace] invitation lookup failed: {err}")
        return None
    items = res.get("Items") or []
    return items[0] if items else None


def accept_invitation(event):
    """POST /workspace-invitations/{token}/accept -> {workspace, role}

    THE IDENTITY RULE LIVES HERE (workspace_schema, IDENTITY (DECIDED)).
    Four outcomes, and only the last creates anything:

      Case D  the authenticated identity is not the invited email     -> 403
      Case C  the invited email is a PERSONAL-only identity           -> 409
      idem.   already an active member                                -> 200
      Case B  an organisation identity accepting                      -> 201

    SINGLE-USE is enforced by a CONDITIONAL status transition, not by a read
    followed by a write: two requests racing on one token both see PENDING,
    and exactly one of them can move it to ACCEPTED.
    """
    token = _url_unquote((event.get("pathParameters") or {}).get("token", ""))
    user_id = _require_auth(event)

    invitation = _invitation_by_token(token)
    if not invitation:
        raise ApiError(404, "this invitation is no longer valid")

    user = _users.get_item(Key={"user_id": user_id}).get("Item") or {}
    email = str(user.get("email") or "")
    wid = str(invitation.get("workspace_id") or "")

    already = _active_membership(wid, user_id)

    # THE CLOSED-INVITATION GATE, and it deliberately runs AFTER the
    # membership read.
    #
    # The bug this ordering fixes: accepting sets the invitation to ACCEPTED,
    # which makes invite_is_open false — so gating on it first meant the
    # already-a-member branch below could never be reached, and the person who
    # had just joined got 404 for re-opening their own link. Tapping an
    # invitation twice is ordinary behaviour, not an error.
    #
    # An ALREADY-MEMBER caller is therefore let through to the idempotent
    # branch even on a closed invitation. Everyone else gets one answer for
    # "no such token", "cancelled", "expired" and "mid-flight", so a caller
    # holding a dead link learns nothing about which kind of dead it is — and
    # no membership can be created from one, because the conditional claim
    # further down still requires PENDING.
    if not workspace_schema.invite_is_open(invitation) and not already:
        raise ApiError(404, "this invitation is no longer valid")
    verdict = workspace_schema.acceptance_verdict(
        email, invitation,
        has_org_membership=_has_organisation_membership(user_id),
        already_member=bool(already),
        # The SAME probe create_workspace uses, so the two identity paths
        # cannot disagree about what "a personal account" means. Every signup
        # gets a derived Personal workspace, so its mere existence proves
        # nothing — actual personal DATA is the signal.
        has_personal_data=_has_personal_resources(user_id))

    if verdict == workspace_schema.ACCEPT_WRONG_IDENTITY:
        # Deliberately does NOT name the invited address: the token holder may
        # not be the intended recipient, and echoing the target would turn a
        # leaked link into an email-address disclosure.
        raise ApiError(403, "this invitation was sent to a different email "
                            "address. Sign in as the invited account to "
                            "accept it.")

    if verdict == workspace_schema.ACCEPT_PERSONAL_IDENTITY:
        # Case C. NOT a merge and NOT a conversion — the agreed model forbids
        # both. 409 rather than 403 because the request is well-formed and
        # will succeed once the right identity is used; and never 401, which
        # would clear the JWT and sign the user out of the very personal
        # account this message tells them to keep.
        #
        # Carries a stable `code` (P2 remediation) so the app branches on
        # identity rather than on wording — the same contract
        # SalesforceReconnectRequired established.
        raise OrganisationEmailConflict(
            workspace_schema.PERSONAL_IDENTITY_MESSAGE)

    workspace = _workspace_row(wid)
    if not workspace or not workspace_schema.workspace_is_active(workspace):
        raise ApiError(404, "this invitation is no longer valid")

    if verdict == workspace_schema.ACCEPT_ALREADY_MEMBER:
        # Idempotent: re-opening the link after accepting is a normal thing to
        # do and must not error. The invitation is closed off on the way out.
        _close_invitation(invitation, user_id)
        return _resp(200, {
            "workspace": workspace_schema.public_workspace(
                workspace, role=already.get("role", "")),
            "role": already.get("role", ""), "already_member": True})

    # ---------------------------------------------------------------
    # ATOMICITY (P0 remediation). The required invariant is:
    #
    #     membership exists AND invitation = ACCEPTED
    #   OR
    #     membership absent AND invitation still USABLE
    #
    #   never: invitation = ACCEPTED with no membership.
    #
    # The old order flipped the status FIRST and wrote the membership second,
    # so a failed membership write spent the token and left the invitee
    # permanently locked out with no way to retry.
    #
    # WHY NOT TransactWriteItems. It would be the textbook answer, but it
    # needs a dynamodb CLIENT (this module holds only resource-level Table
    # handles), a new IAM action, and it cannot be exercised by the offline
    # test harness — so the atomicity fix itself would ship untested. The
    # three-step claim below gives the same invariant using only the
    # conditional writes this codebase already relies on everywhere else
    # (_folder_name_claim, the signup email claim, create_workspace).
    #
    # THREE STEPS:
    #   1. CLAIM the invitation (PENDING -> ACCEPTING). Conditional, so
    #      exactly one of N racing requests proceeds — single-use is enforced
    #      here, before anything is created.
    #   2. Write the membership. Now the grant exists.
    #   3. FINALIZE (ACCEPTING -> ACCEPTED).
    #
    # If step 2 or 3 fails, step 1 is COMPENSATED back to PENDING, so the
    # invitation stays usable and the invitee can simply retry. ACCEPTING is
    # never treated as open by invite_is_open, so a token stranded mid-flight
    # (a Lambda timeout between 1 and 3) cannot be replayed — it fails closed
    # and an owner can re-invite.
    # ---------------------------------------------------------------
    invitation_id = invitation["invitation_id"]
    role = str(invitation.get("role") or workspace_schema.ROLE_MEMBER)
    now = _now_iso()

    try:
        _invitations.update_item(
            Key={"invitation_id": invitation_id},
            UpdateExpression=("SET #s = :accepting, accepted_by = :uid, "
                              "updated_at = :now"),
            ConditionExpression="#s = :pending",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":accepting": workspace_schema.INVITE_ACCEPTING,
                ":pending": workspace_schema.INVITE_PENDING,
                ":uid": user_id, ":now": now})
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") \
                == "ConditionalCheckFailedException":
            raise ApiError(409, "this invitation has already been used")
        raise

    try:
        _memberships.put_item(Item=workspace_schema.new_membership(
            wid, user_id, role, now,
            invited_by=str(invitation.get("invited_by") or "")))
        _invitations.update_item(
            Key={"invitation_id": invitation_id},
            UpdateExpression=("SET #s = :accepted, accepted_at = :now, "
                              "updated_at = :now"),
            ConditionExpression="#s = :accepting",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":accepted": workspace_schema.INVITE_ACCEPTED,
                ":accepting": workspace_schema.INVITE_ACCEPTING,
                ":now": now})
    except Exception:
        # COMPENSATE. Return the invitation to PENDING so the invitee can
        # retry — the alternative is a spent token and a person who cannot
        # join. Best-effort and conditional on OUR claim still standing, so
        # it can never reopen an invitation somebody else has since accepted.
        try:
            _invitations.update_item(
                Key={"invitation_id": invitation_id},
                UpdateExpression="SET #s = :pending, updated_at = :now",
                ConditionExpression="#s = :accepting",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":pending": workspace_schema.INVITE_PENDING,
                    ":accepting": workspace_schema.INVITE_ACCEPTING,
                    ":now": _now_iso()})
        except ClientError as rollback_err:
            # Loud: the invitation is now stranded in ACCEPTING and an owner
            # must re-invite. Fails CLOSED (the token is unusable), so this is
            # a support problem, never a security one.
            print(f"[workspace] invitation {invitation_id} left ACCEPTING; "
                  f"rollback failed: {rollback_err}")
        raise

    _audit("workspace.joined", user_id, wid, role=role)
    return _resp(201, {
        "workspace": workspace_schema.public_workspace(workspace, role=role),
        "role": role, "already_member": False})


def _close_invitation(invitation, user_id):
    """Mark an invitation ACCEPTED, best-effort and only if still PENDING."""
    try:
        _invitations.update_item(
            Key={"invitation_id": invitation["invitation_id"]},
            UpdateExpression=("SET #s = :accepted, accepted_by = :uid, "
                              "accepted_at = :now, updated_at = :now"),
            ConditionExpression="#s = :pending",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={
                ":accepted": workspace_schema.INVITE_ACCEPTED,
                ":pending": workspace_schema.INVITE_PENDING,
                ":uid": user_id, ":now": _now_iso()})
    except ClientError:
        pass  # already closed — nothing to do


def update_member(event):
    """PATCH /workspaces/{workspace_id}/members/{user_id} {role} -> {member}

    Role changes, with the four safety rules from section 11:

      * the target must be an ACTIVE member of THIS workspace;
      * OWNER cannot be granted here — ownership transfer is its own
        operation and does not exist yet, so this route must not become a
        back door to it;
      * the current OWNER's role cannot be changed, which is what keeps the
        organisation from becoming ownerless;
      * a MANAGER cannot promote themselves (they cannot reach OWNER at all,
        by the second rule).
    """
    params = event.get("pathParameters") or {}
    workspace_id = _url_unquote(params.get("workspace_id", ""))
    target_id = _url_unquote(params.get("user_id", ""))
    actor_id, wid, _ = _require_workspace_capability(
        event, workspace_schema.CAP_MANAGE_MEMBERS, workspace_id=workspace_id)

    data = _body(event)
    try:
        role = workspace_schema.clean_role(data.get("role"), default=None)
    except workspace_schema.WorkspaceValidationError as err:
        raise ApiError(400, str(err))

    if role == workspace_schema.ROLE_OWNER:
        raise ApiError(403, "ownership cannot be granted here; "
                            "use ownership transfer")

    target = _active_membership(wid, target_id)
    if not target:
        raise ApiError(404, "member not found")
    if target.get("role") == workspace_schema.ROLE_OWNER:
        raise ApiError(403, "the organisation owner's role cannot be changed")

    _memberships.update_item(
        Key={"workspace_id": wid, "user_id": target_id},
        UpdateExpression="SET #r = :role, updated_at = :now",
        ExpressionAttributeNames={"#r": "role"},
        ExpressionAttributeValues={":role": role, ":now": _now_iso()})

    _audit("workspace.role_changed", actor_id, wid,
           target=target_id, role=role)
    updated = _workspace_membership(wid, target_id)
    return _resp(200, {"member": workspace_schema.public_member(
        updated, _users_by_ids([target_id]).get(target_id))})


def remove_member(event):
    """DELETE /workspaces/{workspace_id}/members/{user_id} -> {removed}

    REMOVES ACCESS ONLY. Organisation-owned data is deliberately untouched:
    a meeting recorded for ABC Realty belongs to ABC Realty, not to the person
    who pressed record, so it must survive them leaving. "Remove + delete
    data" is a later phase and is not reachable from here.

    The row is marked REMOVED rather than deleted, so "was this person ever a
    member" stays answerable and a re-invitation is an update rather than a
    resurrection. _active_membership already treats REMOVED as no access, and
    because membership is re-read on every request the revocation takes effect
    on the target's very next call — which is the whole reason it is not in
    the JWT.
    """
    params = event.get("pathParameters") or {}
    workspace_id = _url_unquote(params.get("workspace_id", ""))
    target_id = _url_unquote(params.get("user_id", ""))
    actor_id, wid, _ = _require_workspace_capability(
        event, workspace_schema.CAP_MANAGE_MEMBERS, workspace_id=workspace_id)

    target = _active_membership(wid, target_id)
    if not target:
        raise ApiError(404, "member not found")
    if target.get("role") == workspace_schema.ROLE_OWNER:
        # An ownerless organisation must never exist.
        raise ApiError(403, "the organisation owner cannot be removed; "
                            "transfer ownership first")

    _memberships.update_item(
        Key={"workspace_id": wid, "user_id": target_id},
        UpdateExpression=("SET #s = :removed, removed_at = :now, "
                          "removed_by = :actor, updated_at = :now"),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":removed": workspace_schema.MEMBERSHIP_REMOVED,
            ":now": _now_iso(), ":actor": actor_id})

    _audit("workspace.member_removed", actor_id, wid, target=target_id)
    return _resp(200, {"removed": True, "user_id": target_id,
                       "workspace_id": wid})


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------
_ROUTES = {
    ("POST", "/signup"): signup,
    ("POST", "/login"): login,
    ("GET", "/me"): get_me,
    ("PATCH", "/me"): patch_me,
    ("POST", "/me/password"): change_password,
    ("POST", "/avatars/upload-request"): request_avatar_upload,
    ("POST", "/devices/pair-request"): pair_request,
    ("POST", "/devices/pair"): pair_device,
    ("POST", "/devices/claim"): claim_device,   # legacy — see claim_device()
    ("GET", "/devices"): list_devices,
    ("GET", "/devices/{device_id}"): get_device_detail,
    ("PATCH", "/devices/{device_id}"): rename_device,
    ("DELETE", "/devices/{device_id}"): unpair_device,
    ("POST", "/devices/{device_id}/factory-reset"): factory_reset_device,
    ("GET", "/recordings"): list_recordings,
    ("POST", "/recordings/upload-request"): request_upload,
    ("POST", "/recordings/upload-complete"): complete_upload,
    # AI Meeting Workspace.
    #
    # The action is a LITERAL PREFIX and the recording key stays LAST, i.e.
    # /recordings/ai/chat/{key+} rather than the more natural-looking
    # /recordings/{key+}/chat. That is not a style choice: API Gateway rejects
    # a greedy variable anywhere but the final position —
    #   BadRequestException: Greedy variables may only be in last position
    # — so a sub-resource route under {key+} cannot be created at all. Verified
    # against live AWS while designing these routes.
    #
    # The "ai/" segment keeps the action namespace from ever colliding with a
    # real recording key: keys always begin "recordings/{user_id}/..." (or a
    # legacy "{device_id}/..."), so no key can be mistaken for an action.
    ("GET", "/recordings/ai/documents/{key+}"): list_documents,
    ("POST", "/recordings/ai/documents/{key+}"): generate_document,
    ("PATCH", "/recordings/ai/documents/{key+}"): update_document,
    ("DELETE", "/recordings/ai/documents/{key+}"): delete_document,
    ("POST", "/recordings/ai/custom-document/{key+}"): generate_custom_document,
    # Minutes of Meeting — the STRUCTURED, editable MoM. GET reads, POST
    # generates/regenerates (merging over user edits), PUT saves the whole
    # edited structure, DELETE resets it. Every write also refreshes the
    # mirrored documents.minutes_of_meeting, so the Documents list, DOCX/PDF
    # export and Share keep working with no knowledge of the structure.
    ("GET", "/recordings/ai/mom/{key+}"): get_mom,
    ("POST", "/recordings/ai/mom/{key+}"): generate_mom,
    ("PUT", "/recordings/ai/mom/{key+}"): save_mom,
    ("DELETE", "/recordings/ai/mom/{key+}"): delete_mom,
    ("POST", "/recordings/ai/update-documents/{key+}"): update_stale_documents,
    ("POST", "/recordings/ai/reprocess/{key+}"): reprocess_recording,
    ("POST", "/recordings/ai/quick/{key+}"): quick_action,
    ("POST", "/recordings/ai/highlights/{key+}"): regenerate_highlights,
    ("GET", "/recordings/ai/chat/{key+}"): get_chat,
    ("POST", "/recordings/ai/chat/{key+}"): chat,
    ("DELETE", "/recordings/ai/chat/{key+}"): clear_chat,
    # Tasks. Same four route templates the app has always called, now served
    # from the first-class Tasks table instead of the recording row's embedded
    # map — see the Tasks section. The legacy handlers (list_tasks/create_task/
    # update_task/delete_task) are kept in this file as the mirror-writer and
    # the migration source; they are no longer reachable over HTTP.
    ("GET", "/recordings/ai/tasks/{key+}"): list_meeting_tasks,
    ("POST", "/recordings/ai/tasks/{key+}"): create_meeting_task,
    ("PATCH", "/recordings/ai/tasks/{key+}"): update_meeting_task,
    ("DELETE", "/recordings/ai/tasks/{key+}"): delete_meeting_task,
    # Speaker -> Contact mapping and the meeting's folder. Both put the action
    # first and {key+} last for the same API Gateway reason as the AI routes.
    ("GET", "/recordings/participants/{key+}"): list_participants,
    ("PUT", "/recordings/participants/{key+}"): set_participant,
    ("GET", "/recordings/{key+}"): get_recording,
    ("PATCH", "/recordings/{key+}"): patch_recording,
    # Trash. DELETE /recordings/{key+} is a SOFT delete (see the Trash
    # section); only /recordings/permanent/{key+} destroys anything.
    #
    # The two sub-actions put the action FIRST and the key LAST, exactly
    # like the AI routes and for the same hard reason: API Gateway rejects
    # a greedy variable in any but the final position, and a recording key
    # contains slashes so it MUST be greedy. "/recordings/{key+}/restore"
    # cannot be created at all. The literal prefix cannot collide with a
    # real key either — keys always begin "recordings/{user_id}/..." or a
    # legacy device id, never "restore/" or "permanent/".
    ("DELETE", "/recordings/{key+}"): delete_recording,
    ("POST", "/recordings/restore/{key+}"): restore_recording,
    ("DELETE", "/recordings/permanent/{key+}"): permanently_delete_recording,
    ("GET", "/trash"): list_trash,
    # Folders — organizational views over the ONE master meeting collection.
    # Contacts — global per owner, never owned by a folder.
    ("POST", "/contacts"): create_contact,
    ("GET", "/contacts"): list_contacts,
    ("GET", "/contacts/{contact_id}"): get_contact,
    ("PATCH", "/contacts/{contact_id}"): update_contact,
    ("DELETE", "/contacts/{contact_id}"): delete_contact,
    # Contact -> Salesforce Contact/Account identity (Phase 2D.3). Separate
    # from the general PATCH above: this persists an EXPLICIT CRM match, not
    # an edit of the contact's own fields.
    ("PUT", "/contacts/{contact_id}/crm"): resolve_contact_crm_identity,
    # Cross-meeting task queries — the Task Tracker's read side.
    ("GET", "/tasks"): list_all_tasks,
    ("GET", "/tasks/{task_id}"): get_task,
    ("PATCH", "/tasks/{task_id}"): update_task_v2,
    ("POST", "/tasks/{task_id}/resolve"): resolve_task_assignee,
    ("GET", "/tasks/{task_id}/assignee-candidates"): suggest_task_assignees,
    # MinuteX Assistant — the workspace-wide AI. Distinct from the per-meeting
    # /recordings/ai/chat/{key+} above: that one answers about ONE transcript,
    # this one answers about the user's whole workspace by calling the task
    # tools. The client sends only {message}; identity comes from the JWT.
    ("POST", "/ai/chat"): ai_chat,
    ("GET", "/ai/suggestions"): ai_suggestions,
    # Persistent workspace conversations. All three are STATIC paths with a
    # non-greedy {session_id}, so none of the greedy-{key+} ordering
    # constraints that shape the /recordings/ai/* routes apply — and
    # "/ai/chat/sessions" cannot collide with "/ai/chat" because API Gateway
    # matches the full path, not a prefix.
    #
    # SEPARATE FROM MEETING CHAT PERSISTENCE, deliberately (spec section 15):
    # /recordings/ai/chat/{key+} stores its history on the recording row and
    # is untouched by any of this.
    ("GET", "/ai/chat/sessions"): ai_list_sessions,
    ("GET", "/ai/chat/sessions/{session_id}"): ai_get_session,
    ("DELETE", "/ai/chat/sessions/{session_id}"): ai_delete_session,
    # The dashboard's STRUCTURED AI. Separate from /ai/chat because the answer
    # is rows keyed by task id rather than prose — see the section above for
    # why parsing the conversational agent's output was the wrong alternative.
    # POST despite being a read: it runs a generation, so it must not be
    # cached or retried by a proxy that assumes GET is free.
    ("POST", "/ai/task-intelligence"): ai_task_intelligence,
    # CRM — Salesforce connect (Phase 1). /callback is the one route in this
    # file Salesforce's browser redirect calls directly — no JWT, verified
    # via `state` instead. See the section above for the full flow.
    ("GET", "/crm/salesforce/connect"): salesforce_connect,
    ("GET", "/crm/salesforce/callback"): salesforce_callback,
    ("GET", "/crm/salesforce/status"): salesforce_status,
    ("DELETE", "/crm/salesforce"): salesforce_disconnect,
    # CRM configuration — the user maps their OWN org's object/fields; no
    # object or field API name is ever hardcoded (see the section above).
    ("GET", "/crm/salesforce/objects"): salesforce_list_objects,
    ("GET", "/crm/salesforce/fields/{object_name}"): salesforce_list_fields,
    ("GET", "/crm/salesforce/config"): salesforce_get_config,
    ("PUT", "/crm/salesforce/config"): salesforce_put_config,
    # Identifier -> Salesforce record Id, using the configured object + field.
    ("POST", "/crm/salesforce/lookup"): salesforce_lookup_record,
    # Push the meeting's configured content onto the confirmed record. The
    # recording key comes LAST for the same API Gateway reason as the AI routes
    # (a greedy {key+} is only legal in the final position).
    ("POST", "/crm/salesforce/sync/{key+}"): crm_sync_record,
    # CRM Review (Phase 2D.3) — organisation meetings only. Read-only
    # identity/content picture; see push_org_meeting_crm for the push itself.
    ("GET", "/recordings/ai/crm-review/{key+}"): get_meeting_crm_review,
    # The push itself — now ASYNC (Phase 2D.4): returns {job_id, status}
    # immediately rather than the push result, gated on the SAME readiness
    # rule crm-review reports (validated inside the worker).
    ("POST", "/recordings/ai/crm-push/{key+}"): push_org_meeting_crm,
    # Poll/retry for the job the push above created. The retry action word
    # comes BEFORE the greedy {key+} (same API Gateway constraint as the AI
    # routes above — a greedy variable is only legal in the final position,
    # so "/crm-sync-jobs/{key+}/retry" cannot exist as a route at all).
    ("GET", "/recordings/ai/crm-sync-jobs/{key+}"): get_crm_sync_job_status,
    ("POST", "/recordings/ai/crm-sync-jobs-retry/{key+}"): retry_crm_sync_job,
    # ElevenLabs' asynchronous STT completion callback. The SECOND
    # unauthenticated route in this file (after /crm/salesforce/callback):
    # ElevenLabs has no MinuteX JWT, so identity is proven by an HMAC signature
    # over the raw body instead. See the section above for all five checks.
    # --- Meeting Share. The first four are owner-only (JWT); /share/{token}
    # is the ONLY unauthenticated way to read a meeting, and it authorizes on
    # the token alone. See the Meeting Share section above.
    ("POST", "/recordings/share/{key+}"): create_share,
    ("GET", "/recordings/shares/{key+}"): list_shares,
    # A task ASSIGNEE reading the meeting their work came from. JWT-
    # authenticated like the owner routes, but authorized on the task
    # assignment rather than on ownership, and read-only by construction —
    # see the Assignee Meeting Access section above.
    ("GET", "/recordings/shared-with-me/{key+}"): get_assignee_meeting,
    ("PATCH", "/shares/{share_id}"): update_share,
    ("DELETE", "/shares/{share_id}"): revoke_share,
    ("GET", "/share/{token}"): public_share,
    ("GET", "/share/{token}/audio"): public_share_audio,
    # --- Integrations. Generic connect/status/disconnect for ANY provider;
    # only the ones flagged available in shared/integrations.py can actually
    # start a flow. /callback is the THIRD unauthenticated route in this file
    # (after the Salesforce callback and the ElevenLabs webhook), for the same
    # reason: a provider's browser redirect carries no JWT, so the signed
    # `state` is the credential there.
    ("GET", "/integrations"): list_integrations,
    ("GET", "/integrations/{provider}"): get_integration,
    ("POST", "/integrations/{provider}/connect"): integration_connect,
    ("GET", "/integrations/{provider}/callback"): integration_callback,
    ("DELETE", "/integrations/{provider}"): integration_disconnect,
    # Gmail communication. Every one of these 409s with
    # "integration_not_connected" / "integration_reauth_required" when Gmail
    # is not usable — hiding the button in the app is presentation, these are
    # the enforcement. The {key+} routes put the action first and the greedy
    # key last, for the same API Gateway reason as the AI routes.
    ("POST", "/integrations/gmail/send"): gmail_send,
    ("GET", "/integrations/gmail/recipients/{key+}"): gmail_meeting_recipients,
    ("POST", "/integrations/gmail/send/meeting/{key+}"): gmail_send_meeting,
    ("POST", "/integrations/gmail/send/task/{task_id}"): gmail_send_task,
    # --- Notifications. The in-app notification centre (Phase 1). Every one
    # of these is JWT-only and scoped to the caller: there is no route that
    # takes a user_id, and no route that reads another user's rows. See the
    # NOTIFICATIONS section for the engine that writes them.
    ("GET", "/notifications"): list_notifications,
    ("GET", "/notifications/unread-count"): get_unread_count,
    ("POST", "/notifications/{notification_id}/read"): mark_notification_read,
    ("POST", "/notifications/read-all"): mark_all_notifications_read,
    # --- Workspaces (Phase 2A). READ ONLY, deliberately: creation,
    # invitation and role changes wait on the identity decision documented at
    # the end of shared/workspace_schema.py. Both are JWT-authenticated and
    # resolve membership from the database — a workspace_id in a header or a
    # path is a request, never a claim.
    ("GET", "/workspaces"): list_workspaces,
    ("GET", "/workspaces/{workspace_id}"): get_workspace,
    # Organisation write surface (Phase 2B). Every one resolves membership
    # and role from the database; none reads a role from the client.
    ("POST", "/workspaces"): create_workspace,
    ("GET", "/workspaces/{workspace_id}/members"): list_members,
    ("POST", "/workspaces/{workspace_id}/members/invite"): invite_member,
    ("PATCH", "/workspaces/{workspace_id}/members/{user_id}"): update_member,
    ("DELETE", "/workspaces/{workspace_id}/members/{user_id}"): remove_member,
    # Organisation CRM — Salesforce (Phase 2D.1). Workspace-scoped twin of
    # the Personal /crm/salesforce/* routes above; see the "Organisation
    # CRM" section for why /org-callback is a fixed path while connect/
    # status/disconnect carry {workspace_id}. connect/disconnect require
    # CAP_MANAGE_INTEGRATIONS (Owner/Manager); status is any active member.
    ("GET", "/workspaces/{workspace_id}/crm/salesforce/connect"): org_salesforce_connect,
    ("GET", "/crm/salesforce/org-callback"): org_salesforce_callback,
    ("GET", "/workspaces/{workspace_id}/crm/salesforce/status"): org_salesforce_status,
    ("DELETE", "/workspaces/{workspace_id}/crm/salesforce"): org_salesforce_disconnect,
    # Organisation CRM configuration (Phase 2D.2) — the workspace-scoped twin
    # of /crm/salesforce/{objects,fields,config} below. Read is any active
    # member; PUT config requires CAP_MANAGE_INTEGRATIONS.
    ("GET", "/workspaces/{workspace_id}/crm/salesforce/objects"): org_salesforce_list_objects,
    ("GET", "/workspaces/{workspace_id}/crm/salesforce/fields/{object_name}"): org_salesforce_list_fields,
    ("GET", "/workspaces/{workspace_id}/crm/salesforce/config"): org_salesforce_get_config,
    ("PUT", "/workspaces/{workspace_id}/crm/salesforce/config"): org_salesforce_put_config,
    # Meeting identity -> Salesforce (Phase 2D.3). Search is any active
    # member; resolving a MEMBER's own Salesforce User identity requires
    # CAP_MANAGE_INTEGRATIONS (it applies org-wide, not just to one meeting).
    ("GET", "/workspaces/{workspace_id}/crm/salesforce/search/contacts"): crm_search_salesforce_contacts,
    ("GET", "/workspaces/{workspace_id}/crm/salesforce/search/users"): crm_search_salesforce_users,
    ("PUT", "/workspaces/{workspace_id}/crm/salesforce/members/{user_id}"): resolve_member_crm_identity,
    # Acceptance is JWT-authenticated: the token proves possession of the
    # link, the JWT proves WHO is spending it, and both must agree.
    ("POST", "/workspace-invitations/{token}/accept"): accept_invitation,
    ("POST", "/webhooks/elevenlabs/stt"): stt_webhook,
    # Recover a job whose webhook never arrived, by asking ElevenLabs directly.
    # JWT-authenticated and owner-scoped — this one is for the user/operator,
    # not for ElevenLabs.
    ("POST", "/recordings/ai/stt-reconcile/{key+}"): stt_reconcile,
}


def lambda_handler(event, context):
    # An INTERNAL invoke from the pipeline, not an HTTP request. Checked first
    # because it carries no routeKey and would otherwise fall straight through
    # to the 404 below. Only our own Lambdas can reach this — API Gateway
    # always sets a routeKey, so no request from the internet can take this
    # branch.
    if (event or {}).get("type") == INTERNAL_SEED_TASKS_EVENT:
        return handle_seed_tasks_event(event)

    # HTTP API v2.0: method + matched route template live under requestContext.
    rc = (event.get("requestContext") or {}).get("http") or {}
    method = rc.get("method", "")
    route = event.get("routeKey", "")  # e.g. "GET /recordings/{key}"
    # routeKey is "METHOD /path"; split once.
    path = route.split(" ", 1)[1] if " " in route else rc.get("path", "")

    handler = _ROUTES.get((method, path))
    if handler is None:
        return _resp(404, {"error": "not found", "method": method, "path": path})
    try:
        return handler(event)
    except ApiError as e:
        # `code` is only present on errors that carry a stable machine-readable
        # identity (SalesforceReconnectRequired). Clients branch on it; every
        # other error keeps the plain {"error": ...} shape it has always had.
        body = {"error": e.message}
        code = getattr(e, "code", "")
        if code:
            body["code"] = code
        # AmbiguousContact carries the candidate list with it: the whole point
        # of that 409 is that the client can render "which Rahul?" instead of a
        # dead end, which needs the candidates in the body.
        candidates = getattr(e, "candidates", None)
        if candidates is not None:
            body["candidates"] = candidates
        return _resp(e.status, body)
    except Exception as e:  # noqa: BLE001
        # The RESPONSE stays generic on purpose — an unexpected exception's text
        # is not vetted for secrets, so it must never reach the client. The LOG
        # gets the full traceback: this handler previously printed only
        # "TypeName: message" on one line, which is how a hard-failing
        # UpdateExpression in _write_crm_record surfaced to the user as a
        # blanket "internal error" with no file or line to chase.
        print(f"[error] {method} {path}: {type(e).__name__}: {e}\n"
              f"{traceback.format_exc()}")
        return _resp(500, {"error": "internal error"})