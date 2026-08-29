"""transcribeRecording — Stage 3 of the AI_recorder pipeline (Python).

TWO ENTRY POINTS, one analysis path:

  A. S3 ObjectCreated on the recordings bucket (suffix-filtered to audio).
     STARTS an ASYNCHRONOUS ElevenLabs transcription and returns immediately.
     It does NOT wait for, or ever see, the transcript.

  B. A "stt.completed" event, invoked asynchronously by userApi's ElevenLabs
     webhook handler once ElevenLabs delivers the transcript. This carries the
     transcript + timestamps and runs the whole Groq analysis + persistence
     path, which is the SAME code the synchronous flow always used.

WHY ASYNC (the failure this replaces)
-------------------------------------
The old flow held one Lambda invocation open across the entire transcription:
a single POST /v1/speech-to-text with a 290s socket timeout, inside a 300s
Lambda. Any recording that took longer than that died mid-request — and it
died in a way no `except` block could see, because a Lambda TIMEOUT is not a
Python exception: the process is killed, the handler's error path never runs,
so `status` was left at "transcribing" forever. The app polled a status that
would never advance, and S3's retry re-ran (and re-paid for) the same doomed
request. Observed in production as `error = "The read operation timed out"`.

So the transcription duration is no longer allowed to live inside a Lambda
invocation at all. ElevenLabs accepts the job, hands back a `request_id`, and
delivers the result to a webhook whenever it is done — 10 minutes or 3 hours
later makes no difference to us, because nothing of ours is waiting.

Flow A — start (the Lambda never downloads the audio, and never waits):
  1. Read bucket + key from the S3 event.
  2. Presign a short-lived GET URL for the audio.
  3. POST it to ElevenLabs Scribe with `webhook=true` (remote-URL mode via
     `source_url`, so ElevenLabs fetches the bytes itself): model_id=scribe_v2,
     diarize=true, automatic language detection. The call returns in ~a second
     with a `request_id` instead of a transcript.
  4. Store `stt_request_id` on the row and leave status="transcribing".
     Exit. Total invocation: a couple of seconds.

Flow B — completion (webhook -> userApi -> here):
  5. Build the diarized "Speaker N:" transcript from the webhook's `words[]`
     (grouping consecutive `type=="word"` tokens by `speaker_id`) and real
     per-speaker-turn `timestamps` from the same words' start/end.
  6. ONE Groq call produces the whole analysis — title, summary, highlights,
     tasks, participants, the structured `meeting_highlights` AND any
     configured CRM identifiers (see analyze_meeting()).
  7. UPSERT one item into DynamoDB keyed by audio_s3_key, with the transcript
     itself offloaded to S3 (see lambda-shared/transcript_store.py).

Resilience / failure ordering (each stage degrades independently):
  * ElevenLabs REJECTS the job -> raise. Nothing is written; S3/Lambda retries.
    (Better to retry than to store an empty-transcript record.)
  * ElevenLabs ACCEPTS then never calls back -> the row keeps its
    stt_request_id and status="transcribing". It is reconcilable at any time
    from ElevenLabs' own transcript endpoint (see userApi's stt_reconcile),
    because we persisted the id rather than relying on a live socket.
  * Transcript ok, Groq fails -> STILL write the item with the transcript,
    summary="", status="transcribed". The transcript is never lost.
  * All ok                    -> status="complete".

Groq itself lives in lambda-shared/groq_client.py and every prompt in
lambda-shared/prompts.py — the SAME modules userApi uses for on-demand
documents and chat, so there is one client, one retry policy and one set of
prompts across the backend. Both files are vendored into this function's zip
(see scripts/21_deploy_ai_workspace.sh).

No trigger loop: this Lambda writes only to DynamoDB, never back to S3. If a
future change writes to S3, it MUST use a non-.wav key so it can't re-trigger.

boto3 is bundled in the Lambda Python runtime; HTTP calls use the stdlib
(urllib) so there are no dependencies to bundle. Secrets (ElevenLabs/Groq keys)
come from env only, never hardcoded.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from botocore.config import Config as _BotoConfig

# The shared AI core — one Groq client, one set of prompts, one coercion layer
# for the whole backend. Vendored into this zip alongside lambda_function.py.
import ai_schema
import groq_client
import prompts
import stt_result
import transcript_store

REGION = os.environ.get("AWS_REGION")
TABLE = os.environ.get("RECORDINGS_TABLE", "Recordings")
URL_EXPIRY = int(os.environ.get("URL_EXPIRY", "900"))

# Wall-clock ceiling for the analysis phase. TPM pacing means a very long
# meeting could otherwise out-run the Lambda's timeout — and a timeout is
# strictly worse than a partial brief, because the invocation dies before the
# transcript is persisted at all. When this budget runs out we stop mapping and
# return what the completed chunks produced.
#
# NO LONGER SPLIT. The analysis was three Groq calls (summary, then highlights,
# then one per CRM mapping), so the budget had to be carved up between them —
# 60/40 plus a fixed CRM slice — precisely so a slow first stage could not
# starve the others. With ONE call there is nothing to divide: the single
# analysis gets the entire budget, which is also strictly more time than the
# summary stage used to get.
GROQ_DEADLINE_SECONDS = int(os.environ.get("GROQ_DEADLINE_SECONDS", "180"))

# The user's CRM config lives on the CrmConnections row userApi owns. This
# Lambda reads it (never writes it) to learn WHAT to extract — without it there
# is no way to know that this customer identifies records by "Lead Email".
CRM_CONNECTIONS_TABLE = os.environ.get("CRM_CONNECTIONS_TABLE", "CrmConnections")
CRM_PROVIDER_SALESFORCE = "salesforce"

# --- ElevenLabs speech-to-text (Scribe) ---
ELEVENLABS_URL = "https://api.elevenlabs.io/v1/speech-to-text"
ELEVENLABS_MODEL = os.environ.get("ELEVENLABS_MODEL", "scribe_v2")

# Optional: target ONE configured STT webhook by id. Left empty, ElevenLabs
# delivers to every speech-to-text webhook configured on the workspace, which
# is the right default for a single-environment workspace. Set it when a second
# environment shares the workspace, so staging's transcripts aren't also POSTed
# at production (the bucket check in the webhook rejects them either way, but
# not delivering them at all is cleaner).
ELEVENLABS_WEBHOOK_ID = os.environ.get("ELEVENLABS_WEBHOOK_ID", "").strip()

# How long the presigned source_url stays valid. This must cover ElevenLabs'
# QUEUE WAIT plus the download, not just our own request: the job may be
# accepted now and fetched minutes later, and an expired URL then fails the
# DOWNLOAD ("Failed to download the file from the provided URL") — a failure
# that looks like bad audio but is really an expiry bug. 6h by default, well
# beyond any realistic queue delay and still bounded.
STT_URL_EXPIRY = int(os.environ.get("STT_URL_EXPIRY", str(6 * 3600)))

# Socket timeout for the QUEUE call only. This no longer bounds the
# transcription (that is the whole point of the webhook), so it can be short:
# a hung control-plane call should fail fast into the row's error field rather
# than consume the invocation.
STT_START_TIMEOUT = int(os.environ.get("STT_START_TIMEOUT", "30"))

# Version stamped into webhook_metadata and required back on delivery. Bump
# only for an INCOMPATIBLE envelope change; a webhook whose version we don't
# recognise is rejected rather than guessed at.
WEBHOOK_METADATA_VERSION = 1

# Prompts live in lambda-shared/prompts.py — ONE module for every prompt in the
# backend, so the analysis stages here and the on-demand documents/chat in
# userApi cannot drift apart on the rules that matter (never hallucinate, state
# what's missing, keep names verbatim, answer in the transcript's language).
# The transcript itself and its timestamps come from ElevenLabs, NOT Groq —
# Groq only produces the analysis fields.
SUMMARY_SYSTEM_PROMPT = prompts.SUMMARY_SYSTEM

# boto3 clients are created once per container (reused across invocations).
# Regional endpoint, NOT the legacy global one: a new bucket's global
# hostname resolves to us-east-1 until DNS propagates, and S3 then answers
# with a 307 that the ESP32 firmware treats as a fatal error.
_s3 = boto3.client(
    "s3",
    region_name=REGION,
    endpoint_url=f"https://s3.{REGION}.amazonaws.com",
    config=_BotoConfig(s3={"addressing_style": "virtual"}, signature_version="s3v4"),
)
_ddb = boto3.resource("dynamodb", region_name=REGION)
_table = _ddb.Table(TABLE)


# ---------------------------------------------------------------------------
# Audio formats the pipeline accepts (mirror of userApi's UPLOAD_FORMATS).
# Device recordings are .wav; MOBILE records m4a; UPLOAD imports any common
# audio container. ElevenLabs' remote-URL mode is format-agnostic, so
# accepting a key is purely a matter of its extension.
# ---------------------------------------------------------------------------
AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".flac",
              ".webm", ".mp4", ".mp2", ".mpga", ".amr", ".3gp", ".aiff",
              ".aif", ".wma", ".caf", ".mka")

# The reserved {device_id}-slot literals for non-device recording sources.
SOURCE_SEGMENTS = {"mobile": "MOBILE", "uploads": "UPLOAD"}


# ---------------------------------------------------------------------------
# Key parsing. Three layouts exist:
#
#   USER-OWNED:  recordings/{userId}/{segment}/{recordingId}.{ext}
#     where recordingId = "{meetingId}_{timestamp}" and {segment} is a
#     deviceId (source DEVICE) or one of the reserved literals
#     "mobile" (MOBILE) / "uploads" (UPLOAD).
#   LEGACY:      {deviceId}/{meetingId}_{timestamp}.wav
#
# user_id is "" for legacy keys — ownership of those rows is resolved by the
# userApi through the UserDevices join instead, so the upsert must NOT write
# an empty user_id over a value someone else stamped. device_id is "" for
# MOBILE/UPLOAD — those recordings have no device. meetingId itself contains
# no underscore, so we split the basename on the LAST "_".
# ---------------------------------------------------------------------------
def parse_key(key):
    parts = key.split("/")
    user_id = ""
    device_id = ""
    source = "DEVICE"
    rest = key
    if len(parts) == 4 and parts[0] == "recordings":
        _, user_id, device_id, rest = parts
        if device_id in SOURCE_SEGMENTS:
            source = SOURCE_SEGMENTS[device_id]
            device_id = ""
    elif len(parts) == 2:
        device_id, rest = parts
    base = rest
    low = rest.lower()
    for ext in AUDIO_EXTS:
        if low.endswith(ext):
            base = rest[:-len(ext)]
            break
    us = base.rfind("_")
    meeting_id = base[:us] if us >= 0 else base
    recorded_at = base[us + 1:] if us >= 0 else ""
    return user_id, device_id, source, meeting_id, recorded_at, base


# ---------------------------------------------------------------------------
# The ElevenLabs result -> (transcript, timestamps, language) parsing now lives
# in lambda-shared/stt_result.py, because userApi's STT webhook has to produce
# the IDENTICAL shape from the identical payload. Two copies of the speaker-turn
# grouping would eventually disagree about where a turn ends, and the visible
# symptom (tap-to-seek jumping to the wrong sentence) is very hard to trace back
# to a duplicated helper. Re-exported under their original names so the rest of
# this file, and the tests, read unchanged.
# ---------------------------------------------------------------------------
build_diarized_text = stt_result.build_diarized_text
build_timestamps = stt_result.build_timestamps
_speaker_label = stt_result._speaker_label
_real_words = stt_result._real_words


def _post_json(url, headers, payload, timeout):
    """POST a JSON body and return (status, parsed_json_or_text)."""
    data = json.dumps(payload).encode("utf-8")
    # Groq sits behind Cloudflare, which blocks the default "Python-urllib/x.y"
    # User-Agent with a 403 (error 1010). Send a normal UA so the request is
    # accepted.
    headers = {"User-Agent": "AI-recorder-transcribe/1.0", **headers}
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as e:
        # Non-2xx: surface the status + body so the caller can decide.
        body = e.read().decode("utf-8", "replace")
        return e.code, body


def _post_multipart(url, headers, fields, timeout):
    """POST a multipart/form-data body (text fields only) and return
    (status, parsed_json_or_text). Used for ElevenLabs, which requires
    multipart/form-data even in remote-URL (source_url) mode."""
    # Build the multipart body by hand (stdlib only — no `requests`).
    boundary = "----airecorder" + uuid.uuid4().hex
    crlf = "\r\n"
    parts = []
    for name, value in fields.items():
        parts.append(f"--{boundary}{crlf}")
        parts.append(f'Content-Disposition: form-data; name="{name}"{crlf}{crlf}')
        parts.append(f"{value}{crlf}")
    parts.append(f"--{boundary}--{crlf}")
    body = "".join(parts).encode("utf-8")

    headers = {
        "User-Agent": "AI-recorder-transcribe/1.0",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        **headers,
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8")
            return resp.status, json.loads(data)
    except urllib.error.HTTPError as e:
        data = e.read().decode("utf-8", "replace")
        return e.code, data


# ---------------------------------------------------------------------------
# ElevenLabs: START an asynchronous transcription and return its request_id.
#
# Two independent properties make this cheap and bounded:
#   * REMOTE-URL mode (`source_url` = a presigned S3 GET). ElevenLabs fetches
#     the bytes itself, so a 3 GB recording never passes through this Lambda's
#     memory or its /tmp — the request body stays a few hundred bytes whatever
#     the audio weighs.
#   * WEBHOOK mode (`webhook=true`). The response comes back as soon as the job
#     is QUEUED, carrying a request_id instead of a transcript, so the duration
#     of the transcription is decoupled from this invocation entirely. Verified
#     against the live OpenAPI spec: `webhook`, `webhook_id` and
#     `webhook_metadata` are accepted on the same multipart request as
#     `source_url`, and the async response model is {message, request_id,
#     transcription_id?} with request_id REQUIRED.
#
# The presign must outlive the QUEUE WAIT, not just the request: ElevenLabs may
# start fetching minutes after accepting the job, and a URL that has expired by
# then fails the download instead of the transcription (observed in production
# as "Failed to download the file from the provided URL"). Hence
# STT_URL_EXPIRY defaults far above the 900s used for the old inline flow.
#
# Raises on any non-2xx so the caller can mark the row failed — a REJECTED job
# is a real, immediate failure, unlike a slow one.
# ---------------------------------------------------------------------------
def start_transcription(bucket, key):
    """Queue an async ElevenLabs transcription; return its request_id.

    Returns (request_id, transcription_id) — transcription_id is optional in
    the API's own response model, so it may be "".
    """
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY not set")

    presigned = _s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=STT_URL_EXPIRY,
    )
    print(f"[elevenlabs] presigned GET valid {STT_URL_EXPIRY}s; queueing async job")

    fields = {
        "model_id": ELEVENLABS_MODEL,
        "source_url": presigned,
        "diarize": "true",
        # Deliver the result to our webhook instead of this response.
        "webhook": "true",
        # webhook_metadata is what lets the webhook find the row. It carries the
        # DynamoDB PARTITION KEY (audio_s3_key) — the authoritative identifier —
        # plus the bucket it belongs to, so a webhook meant for another
        # environment sharing the same ElevenLabs workspace is rejected rather
        # than applied to a same-named key here. `v` versions the envelope so a
        # future shape change is detectable instead of silently misparsed.
        # A JSON STRING, per the API ("a JSON string representing an object with
        # a maximum depth of 2 levels").
        "webhook_metadata": json.dumps({
            "audio_s3_key": key,
            "bucket": bucket,
            "v": WEBHOOK_METADATA_VERSION,
        }),
        # language_code omitted -> ElevenLabs auto-detects the language.
    }
    # Only when the workspace has several webhooks configured and we must target
    # one; omitted, ElevenLabs delivers to every configured STT webhook.
    if ELEVENLABS_WEBHOOK_ID:
        fields["webhook_id"] = ELEVENLABS_WEBHOOK_ID

    status, result = _post_multipart(
        ELEVENLABS_URL,
        headers={"xi-api-key": api_key},
        fields=fields,
        # Queueing is a fast control-plane call. A tight timeout here is now
        # SAFE (it no longer bounds the transcription) and desirable: it fails
        # fast into the row's error field instead of burning the invocation.
        timeout=STT_START_TIMEOUT,
    )
    # The workspace has no STT webhook yet — a deployment-ordering condition,
    # not a bad recording. Signalled distinctly so the caller can fall back to
    # the synchronous path instead of failing the upload.
    if _is_no_webhook_configured(status, result):
        raise NoWebhookConfigured(str(result)[:300])

    # 202 is the documented async status; 200 is accepted too because the spec
    # lists the async response model under 200 as well, and treating a
    # successful queue as a failure would double-charge on the retry.
    if status not in (200, 202):
        raise RuntimeError(f"ElevenLabs {status}: {str(result)[:500]}")
    if not isinstance(result, dict):
        raise RuntimeError(f"ElevenLabs {status}: non-JSON response "
                           f"{str(result)[:300]}")

    request_id = str(result.get("request_id") or "").strip()
    if not request_id:
        # Without an id there is nothing to match a webhook against, so every
        # later delivery would be indistinguishable from a stale one. Fail now.
        raise RuntimeError(f"ElevenLabs accepted the job but returned no "
                           f"request_id: {str(result)[:300]}")
    transcription_id = str(result.get("transcription_id") or "").strip()

    print(f"[elevenlabs] queued: request_id={request_id} "
          f"transcription_id={transcription_id or '-'}")
    return request_id, transcription_id


# ---------------------------------------------------------------------------
# SYNCHRONOUS FALLBACK — only when the workspace has no STT webhook configured.
#
# ElevenLabs REFUSES `webhook=true` outright when no speech-to-text webhook
# exists on the workspace:
#     {"code":"invalid_parameters","status":"no_webhooks_configured"}
# That is a deployment-ordering hazard, not a recording problem: the async code
# can be live before the webhook registration has been completed (it needs a key
# with the `webhooks_write` permission, which is granted out-of-band). Without a
# fallback, every upload in that window would fail — a working pipeline replaced
# by a broken one for a reason the user has nothing to do with.
#
# So on exactly that error we transcribe the OLD way: one synchronous request,
# in this invocation. It is subject to the original 290s ceiling and therefore
# to the original stuck-at-"transcribing" risk for a long recording — which is
# precisely why it is a fallback and not the design. It keeps normal-length
# meetings working until the webhook is registered, at which point the async
# path takes over automatically with no code change.
#
# Deliberately NOT triggered by any other failure. A 401, a quota error or bad
# audio must still fail loudly; silently retrying those synchronously would burn
# the invocation and hide a real problem.
# ---------------------------------------------------------------------------
SYNC_FALLBACK_TIMEOUT = int(os.environ.get("SYNC_FALLBACK_TIMEOUT", "290"))


class NoWebhookConfigured(RuntimeError):
    """ElevenLabs rejected webhook=true because the workspace has none.

    A distinct type so the handler can fall back synchronously for THIS reason
    only — every other ElevenLabs failure must still fail the recording loudly.
    """


def _is_no_webhook_configured(status, result):
    """True for ElevenLabs' "no speech-to-text webhooks are configured" error."""
    if status != 422 and status != 400:
        return False
    text = str(result)
    return ("no_webhooks_configured" in text
            or "webhooks are configured" in text)


def transcribe_sync(bucket, key):
    """The pre-async path: one blocking request, transcript in the response.

    Returns (transcript, timestamps, language). Raises on any non-200.
    """
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY not set")

    presigned = _s3.generate_presigned_url(
        "get_object", Params={"Bucket": bucket, "Key": key},
        ExpiresIn=URL_EXPIRY)
    print(f"[elevenlabs] SYNC fallback: presigned GET valid {URL_EXPIRY}s")

    status, result = _post_multipart(
        ELEVENLABS_URL,
        headers={"xi-api-key": api_key},
        fields={
            "model_id": ELEVENLABS_MODEL,
            "source_url": presigned,
            "diarize": "true",
        },
        timeout=SYNC_FALLBACK_TIMEOUT,
    )
    if status != 200:
        raise RuntimeError(f"ElevenLabs {status}: {str(result)[:500]}")
    return transcript_from_result(result)


# ---------------------------------------------------------------------------
# The transcript, timestamps and language of a completed ElevenLabs result.
#
# Shared by BOTH the webhook payload (`data.transcription`) and the polled
# transcript endpoint, because they carry the same shape — so a reconciled job
# and a webhook-delivered one produce byte-identical rows.
# ---------------------------------------------------------------------------
def transcript_from_result(result):
    transcript = build_diarized_text(result)
    # timestamps: real ElevenLabs speaker-turn timing (not LLM-guessed).
    timestamps = build_timestamps(result)
    language = (result.get("language_code")
                if isinstance(result, dict) else None) or "unknown"
    print(f"[elevenlabs] transcript: {len(transcript)} chars, "
          f"{len(timestamps)} segments, language={language}")
    return transcript, timestamps, language


# ---------------------------------------------------------------------------
# Strict coercion + the empty-analysis fallback now live in
# lambda-shared/ai_schema.py, so the highlights stage below and userApi's read
# path validate through exactly the same primitives. Aliased rather than
# re-exported so the rest of this file reads unchanged.
# ---------------------------------------------------------------------------
_empty_analysis = ai_schema.empty_analysis
_coerce_analysis = ai_schema.coerce_analysis
_merge_partials = ai_schema.merge_analyses

# Analysis attributes that LEFT the schema. The prompt no longer asks for them,
# ai_schema.coerce_analysis no longer emits them, and nothing reads them — but a
# row processed under the old shape still carries them, and a SET-only update
# would leave that stale copy in place forever. Reprocessing REMOVEs them, so a
# reprocessed row ends up with exactly the current shape.
#
# `action_items` here is the ANALYSIS field only. Do not confuse it with
# meeting_highlights["action_items"] (the Stage 2 structured extraction, still
# live) or userApi's action_items document/CRM target, which keep their names.
RETIRED_ANALYSIS_ATTRS = (
    "agenda",
    "key_points",
    "decisions",
    "pending_discussions",
    "action_items",
    # The flat 3-7 string list the old fixed schema produced. Superseded by
    # `overview`, whose sections carry the same ground in the shape this
    # meeting actually warranted. NOT accompanied by `summary`, which is
    # deliberately still written (derived, see analyze_and_persist) because
    # the meetings list and the Salesforce push both need one short string.
    "highlights",
    # Never written by this pipeline again — CRM extraction left the analysis
    # path. Deliberately ABSENT from this tuple even so: a stored crm_records
    # value may be a user's MANUAL, confirmed link to a real Salesforce
    # record, and reprocessing a recording must not silently unlink it.
)

# How much of the overview the derived `summary` preview carries. Sized for
# the meetings list's 3-line snippet with room for search to still be useful,
# not to reproduce the overview — the app reads the overview itself for that.
SUMMARY_PREVIEW_CHARS = 600


# ---------------------------------------------------------------------------
# Groq analysis. The client (request shape, 429 policy, TPM chunk budgeting,
# map-reduce pacing) is lambda-shared/groq_client.py — the same module userApi
# uses for on-demand documents and chat. The TPM budget is a per-ACCOUNT quota,
# so both callers spend from one bucket and MUST pace identically to be correct.
#
# What stays here is only what is specific to this pipeline: which prompts to
# use, how a partial result is disclosed, and what gets persisted.
# ---------------------------------------------------------------------------


# The reduce prompt is now built per-call by prompts.unified_reduce_system(),
# which extends SUMMARY_REDUCE_SYSTEM with the unified shape's extra sections.
# The old module-level REDUCE_SYSTEM_PROMPT alias is gone with the three-stage
# analysis that used it; SUMMARY_REDUCE_SYSTEM itself is very much still there.


def _note_partial_overview(overview, covered, total):
    """Disclose a truncated analysis in the text the user actually reads.

    An overview that silently covers 60% of a meeting is worse than one that
    admits it — the reader would otherwise trust it as complete. Only reached
    when the deadline cut the tail off.

    The note goes on the FIRST section rather than into a section of its own:
    a "Coverage" section would be a fixed section appearing in every truncated
    meeting, which is precisely the template behaviour the dynamic overview
    exists to remove, and coerce_overview would be entitled to drop it.
    """
    if covered >= total or not isinstance(overview, dict):
        return overview
    sections = overview.get("sections") or []
    if not sections:
        return overview
    note = (f"[Note: this overview covers the first {covered} of {total} "
            f"segments of the recording. The full transcript is available "
            f"below.]")
    first = dict(sections[0])
    first["content"] = f"{first.get('content', '')} {note}".strip()
    return {**overview, "sections": [first] + list(sections[1:])}


def analyze_meeting(transcript, valid_ids=None):
    """THE analysis: title, the dynamic overview, tasks, participants and the
    structured meeting_highlights — in ONE Groq call.

    Replaces stages that each re-sent the same transcript:
        summarize()               title/summary/highlights/tasks/participants
        extract_highlights()      the structured sections
    A normal meeting therefore went out as 2+ requests over identical source
    text, each paying its own TPM window and its own latency. It is now one
    request, and the whole analysis either lands together or degrades together.

    `valid_ids` is the set of transcript segment ids that actually exist, used
    to validate the model's evidence references. A reference the transcript
    cannot support is dropped — see ai_schema._evidence_ids.

    The reason those calls were originally SEPARATE was real — asking one call
    for prose and for exact extractions made the extractions weaker — so the
    merge does not simply concatenate the field lists. It keeps each prompt's
    proven wording (see prompts.unified_analysis_system) and adds a final
    extraction-fidelity instruction in the recency position, which is where
    instructions actually hold. The unified reply is then split back apart and
    validated by the SAME coercers as before (ai_schema.coerce_unified), so
    every downstream shape stays what its consumers already parse.

    groq_client.analyze() sends the COMPLETE transcript in a single call
    whenever it fits the model's context window (true for the large majority of
    real meetings on the current plan — see analyze()'s docstring), and the
    model writes the final analysis directly from it. There is no
    segment-summary-then-reduce stage in that path. map_reduce() remains only
    as the overflow fallback for a transcript that genuinely doesn't fit, where
    the alternative is dropping the meeting's tail entirely.

    What remains here is this pipeline's own policy: which prompts, and how a
    partial result (only possible via that overflow fallback) is disclosed.
    """
    key = groq_client.api_key()
    if not transcript or not transcript.strip():
        # Nothing to analyze — treat as an empty (but successful) result.
        return ai_schema.empty_unified()

    # WHO WAS IN THE MEETING is decided from the transcript's own diarization,
    # not by the model. The prompt states the rule, but the model kept promoting
    # merely-MENTIONED names (a task owner, a manager, a customer) into
    # `participants`, and the app renders that list as the attendee list — an
    # invented attendee is indistinguishable from a real one to the reader. The
    # roster is bound into both the coerce and merge callbacks, so it is enforced
    # on the single-pass result AND on the overflow path's merge.
    #
    # Empty for a transcript with no "Speaker N:" labels (a non-diarized STT
    # result); coercion then leaves the model's list alone rather than dropping
    # every participant. Tasks are never roster-filtered — an assignee may be a
    # non-attendee, a team or an external party.
    roster = ai_schema.speaker_roster(transcript)
    print(f"[groq] analyze: {len(roster)} speaker(s) in the transcript: "
          f"{roster}")

    analysis, covered, total = groq_client.analyze(
        transcript,
        # The roster goes INTO the prompt too, not just the filter: telling the
        # model who the speakers are beats asking it to work that out from prose,
        # and it means the contribution blurbs land on the right speakers rather
        # than being dropped by the filter for naming someone else.
        map_prompt=prompts.unified_analysis_system(roster),
        # OVERFLOW ONLY — a transcript past the single-pass budget. Still built
        # on SUMMARY_REDUCE_SYSTEM, which is NOT retired.
        reduce_prompt=prompts.unified_reduce_system(),
        merge=lambda partials: ai_schema.merge_unified(partials, roster),
        coerce=lambda obj: ai_schema.coerce_unified(obj, roster, valid_ids),
        # The whole analysis is ONE call, so it gets the whole analysis budget
        # instead of the old summary-vs-highlights split.
        deadline_seconds=GROQ_DEADLINE_SECONDS,
        label="analyze",
        key=key,
        # A 200 whose overview came back empty is exactly the production
        # failure this pipeline exists to avoid — retry once (still cheaper
        # and safer than a reduce over partial JSONs) before falling back to
        # map_reduce.
        is_usable=lambda a: not ai_schema.overview_empty(a.get("overview")),
    )
    # A reduce that came back empty is worse than nothing to show for it.
    if ai_schema.overview_empty(analysis.get("overview")) and total > 1:
        print("[groq] analyze: empty overview after reduce")
    analysis["overview"] = _note_partial_overview(
        analysis.get("overview") or ai_schema.empty_overview(), covered, total)
    hl = analysis.get("meeting_highlights") or {}
    sections = (analysis.get("overview") or {}).get("sections") or []
    print(f"[groq] analyze ok ({covered}/{total} segments): "
          f"title={analysis['title']!r}, "
          f"{len(sections)} overview section(s) "
          f"{[s.get('title') for s in sections]}, "
          f"{len(analysis['tasks'])} tasks, "
          f"{len(analysis['participants'])} participants, "
          + ", ".join(f"{len(hl.get(k, []))} {k}"
                      for k in ai_schema.HIGHLIGHT_SECTIONS)
          + f" (model={groq_client.GROQ_MODEL})")
    return analysis


def _crm_mappings_for_user(user_id):
    """The user's configured CRM mappings, or [] if none/unreachable.

    Read-only view of the config userApi owns (see its CRM configuration
    section for the shape and the legacy-config compatibility rule, which is
    reimplemented here because the two Lambdas share no code for it).

    Every failure path returns [] rather than raising: a CRM config problem must
    never cost a user their transcript and summary. Not configured is also by
    far the common case.
    """
    if not user_id:
        return []
    try:
        item = _ddb.Table(CRM_CONNECTIONS_TABLE).get_item(
            Key={"user_id": user_id, "provider": CRM_PROVIDER_SALESFORCE}
        ).get("Item")
    except Exception as err:  # noqa: BLE001
        print(f"[crm] could not read config for {user_id}: {err}")
        return []
    cfg = (item or {}).get("config") or {}
    stored = cfg.get("mappings")
    if isinstance(stored, list):
        return [m for m in stored if isinstance(m, dict)
                and m.get("object") and m.get("lookup_field")]
    # Legacy single-object config (pre-mappings). Mirrors userApi's
    # _mappings_from_config so an upgraded user's extraction keeps working.
    obj, lookup = cfg.get("object"), cfg.get("site_visit_number_field")
    if obj and lookup:
        label = cfg.get("object_label") or obj
        return [{"object": obj, "object_label": label,
                 "lookup_field": lookup, "label": f"{label} Number"}]
    return []


# ---------------------------------------------------------------------------
# Title source — placeholder -> AI title always allowed; ai -> AI title may
# update (e.g. reprocessing with a corrected transcript); user -> NEVER
# overwritten again. A pure function (no DynamoDB access) so it is directly
# unit-testable without touching the rest of the handler.
#
# The problem this replaces: checking only "is the existing title non-empty"
# could never distinguish a genuine user-typed title from a client-side
# PLACEHOLDER (e.g. an imported file's own OS-generated name, frequently a
# raw date/time string like "04-08-2026 11.35") — a placeholder would
# permanently block the real AI title from ever being saved. title_source
# persists which kind the CURRENT title is, so that distinction survives
# across invocations instead of being re-guessed from the string itself.
# ---------------------------------------------------------------------------
def _resolve_title_fields(existing, ai_title):
    """(existing DynamoDB item, the AI-generated title) -> fields to merge
    into the write, or {} if the existing title must not be touched.

    A row with no title_source attribute predates this fix: an EMPTY title on
    such a row is treated as "placeholder" (nothing to protect, matching the
    original behavior before title_source existed), while a NON-empty title
    with no title_source is ambiguous history (could be a genuine old
    user-typed title OR an old unlabelled placeholder) and is conservatively
    treated as "user" — this fix must never retroactively overwrite
    something a real person may have actually typed.
    """
    existing_title_source = existing.get("title_source") or (
        "placeholder" if not (existing.get("title") or "").strip() else "user"
    )
    if existing_title_source in ("placeholder", "ai") and ai_title:
        return {"title": ai_title, "title_source": "ai"}
    return {}


# ---------------------------------------------------------------------------
# Handler — dispatches between the two entry points (see the module docstring).
#
# The discriminator is the event's own shape, so the S3 trigger keeps sending
# exactly what it always sent and needs no change: an S3 event has Records[],
# a completion event is {"type": "stt.completed", ...}. Nothing else can reach
# this function — it has no function URL and no other event source.
# ---------------------------------------------------------------------------
STT_COMPLETED_EVENT = "stt.completed"


def lambda_handler(event, context):
    if (event or {}).get("type") == STT_COMPLETED_EVENT:
        return handle_stt_completed(event)
    return handle_s3_event(event)


# ---------------------------------------------------------------------------
# Entry point A — an upload landed. QUEUE the transcription and exit.
#
# This invocation is now a couple of seconds long regardless of how long the
# recording is, which is the entire point: nothing here waits for ElevenLabs,
# so nothing here can be killed by a Lambda timeout mid-transcription and
# strand the row at "transcribing" (see the module docstring).
# ---------------------------------------------------------------------------
def handle_s3_event(event):
    # S3 can batch multiple records into one event; process each independently
    # so one bad object doesn't sink the others.
    records = (event or {}).get("Records", [])
    outcomes = []

    for record in records:
        bucket = (((record or {}).get("s3") or {}).get("bucket") or {}).get("name")
        # Object keys arrive URL-encoded in S3 events (spaces -> "+", etc.).
        raw_key = (((record or {}).get("s3") or {}).get("object") or {}).get("key", "")
        key = urllib.parse.unquote_plus(raw_key)

        if not bucket or not key:
            print("skipping record with no bucket/key:", json.dumps(record))
            continue
        if not key.lower().endswith(AUDIO_EXTS):
            # Defensive: the S3 notification is unfiltered (script 18), so
            # never process anything that isn't a supported audio container.
            print(f"[skip] not a supported audio format: {key}")
            continue

        print(f"[start] s3://{bucket}/{key}")

        # Processing-status lifecycle (the app polls this):
        #   uploading -> uploaded -> transcribing -> generating_ai
        #   -> complete | transcribed (Groq failed) | failed (STT failed).
        # One lifecycle for every source — DEVICE, MOBILE, UPLOAD.
        #
        # The row stays "transcribing" from here until the webhook completes it.
        # That is now an HONEST intermediate state rather than a dead end:
        # stt_request_id below makes the job independently trackable, so a
        # webhook that never arrives is RECOVERABLE (see userApi's
        # /stt/reconcile) instead of permanent.
        _upsert(key, {"status": "transcribing"})

        # Queue the job. A REJECTION is still a real, immediate failure (bad
        # audio, exhausted quota, bad credentials) and marks the row failed —
        # only the job's DURATION has stopped being our problem, not its
        # validity.
        try:
            request_id, transcription_id = start_transcription(bucket, key)
        except NoWebhookConfigured as err:
            # The workspace has no STT webhook registered yet (see
            # transcribe_sync). Do the whole thing synchronously in this
            # invocation instead of failing an upload over a configuration step
            # the user has no part in. Reverts to the async path automatically
            # once the webhook exists — no redeploy needed.
            print(f"[elevenlabs] no webhook configured ({err}) — falling back "
                  f"to a SYNCHRONOUS transcription for {key}")
            try:
                transcript, timestamps, language = transcribe_sync(bucket, key)
            except Exception as sync_err:  # noqa: BLE001
                print(f"[elevenlabs] SYNC fallback FAILED for {key}: {sync_err}")
                _upsert(key, {"status": "failed",
                              "error": str(sync_err)[:1000]})
                raise
            outcome = analyze_and_persist(bucket, key, transcript, timestamps,
                                          language)
            outcomes.append(outcome)
            continue
        except Exception as err:  # noqa: BLE001
            print(f"[elevenlabs] START FAILED for {key}: {err}")
            _upsert(key, {"status": "failed", "error": str(err)[:1000]})
            raise  # abort -> S3/Lambda retry semantics apply

        # Persist the id BEFORE returning. This write is what makes the async
        # job trackable at all, and it is also the STALENESS FENCE: a webhook
        # only applies if its request_id still equals this value, so a reprocess
        # that queues a second job immediately invalidates the first one's
        # delivery (see userApi's stt_webhook).
        #
        # stt_started_at supports reconciliation ("has this been pending
        # unreasonably long?"), and `error` is cleared so a successful re-queue
        # doesn't leave a previous attempt's message on the row.
        fields = {
            "stt_request_id": request_id,
            "stt_started_at": datetime.now(timezone.utc)
                              .isoformat().replace("+00:00", "Z"),
            "status": "transcribing",
            "error": "",
        }
        if transcription_id:
            # Non-authoritative, but it is the handle ElevenLabs' own transcript
            # endpoint takes, so reconciliation can use it directly.
            fields["stt_transcription_id"] = transcription_id
        _upsert(key, fields)

        print(f"[queued] {key} stt_request_id={request_id} — exiting without "
              f"waiting for the transcript")
        outcomes.append({"key": key, "status": "transcribing",
                         "stt_request_id": request_id})

    # "queued" counts records HANDLED, which on the synchronous-fallback path
    # means fully processed rather than merely queued. The distinction is in
    # each outcome's own status, so a caller reading the log can tell them apart.
    return {"queued": len(outcomes), "outcomes": outcomes}


# ---------------------------------------------------------------------------
# Entry point B — ElevenLabs delivered a transcript. Run the analysis.
#
# Invoked ASYNCHRONOUSLY by userApi's webhook handler, which has already:
#   * verified the ElevenLabs HMAC signature,
#   * validated the metadata envelope (version + bucket),
#   * matched request_id against the row's stt_request_id (staleness fence),
#   * claimed the delivery idempotently (so this runs at most once per job),
#   * and written the transcript to S3.
#
# This function deliberately does NOT re-do those checks: it is not reachable
# from the internet, and a security check implemented in two places is a
# security check that will eventually disagree with itself. What it does run is
# exactly what the old synchronous flow ran from step 2 onward — which is why
# the analysis code below is unchanged.
# ---------------------------------------------------------------------------
def _restore_segment_timing(timestamps):
    """Re-Decimal the start/end of each segment after the JSON invoke hop.

    The webhook serializes segments with `default=str`, because json.dumps
    cannot encode the Decimal that stt_result produces (and that DynamoDB
    requires instead of a float). This restores the type on arrival so the
    transcript object is written with NUMERIC timings — the app calls
    Number(seg.start) for tap-to-seek, and a JSON string there is a shape
    change in the stored contract, not just a cosmetic one.

    Anything unparseable becomes Decimal("0") rather than raising: a segment
    with a bad timestamp should still show its text.
    """
    restored = []
    for seg in timestamps:
        if not isinstance(seg, dict):
            continue
        out = dict(seg)
        for field in ("start", "end"):
            if field in out:
                try:
                    out[field] = Decimal(str(out[field]))
                except (TypeError, ValueError, ArithmeticError):
                    out[field] = Decimal("0")
        restored.append(out)
    return restored


def handle_stt_completed(event):
    bucket = (event or {}).get("bucket") or ""
    key = (event or {}).get("audio_s3_key") or ""
    if not bucket or not key:
        print(f"[stt.completed] missing bucket/key: {json.dumps(event)[:300]}")
        return {"processed": 0, "error": "missing bucket/key"}

    transcript = (event or {}).get("transcript") or ""
    # start/end crossed the invoke boundary as JSON. They left here as Decimal
    # (DynamoDB rejects floats), and json.dumps can only carry a Decimal by
    # stringifying it — so they arrive as strings like "1.8" and MUST be
    # restored. Left as strings they would be written into the transcript object
    # as JSON strings, and the app's tap-to-seek reads seg.start as a number.
    timestamps = _restore_segment_timing((event or {}).get("timestamps") or [])
    language = (event or {}).get("language") or "unknown"

    print(f"[stt.completed] s3://{bucket}/{key}: {len(transcript)} chars, "
          f"{len(timestamps)} segments, language={language}")
    outcome = analyze_and_persist(bucket, key, transcript, timestamps, language)
    return {"processed": 1, "outcomes": [outcome]}


# ---------------------------------------------------------------------------
# The analysis + persistence path.
#
# Same Groq stage, same degradation ordering and same DynamoDB write the
# synchronous flow always used — extracted into one function so a webhook
# completion and a reconciliation reach it identically, and so the S3 entry
# point above can stay purely about queueing.
# ---------------------------------------------------------------------------
def analyze_and_persist(bucket, key, transcript, timestamps, language):
    """Analyze `transcript` and write the finished row. Returns the outcome."""
    user_id, device_id, source, meeting_id, recorded_at, recording_id = parse_key(key)
    created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # Step 2: analyze with Groq — ONE call over the full transcript producing
    # EVERYTHING: title, the dynamic overview, tasks, participants and the
    # structured meeting_highlights.
    # Failure here is non-fatal — we still keep the transcript + timestamps.
    #
    # The segment ids are derived the SAME way the read path derives them
    # (transcript_store.with_segment_ids), so an evidence reference the model
    # returns is validated against exactly the ids the app will later resolve.
    # Deriving them separately here would let the two drift apart, and a
    # reference that validates at write time but resolves to nothing at read
    # time is the one failure the validation exists to prevent.
    _upsert(key, {"status": "generating_ai"})
    valid_ids = {seg["id"] for seg in transcript_store.with_segment_ids(timestamps)
                 if isinstance(seg, dict) and seg.get("id")}

    analysis = ai_schema.empty_unified()
    status = "complete"
    try:
        analysis = analyze_meeting(transcript, valid_ids)
        if ai_schema.overview_empty(analysis.get("overview")):
            # analyze_meeting() can return NORMALLY with an empty overview (e.g.
            # map_reduce's reduce step came back blank without Groq itself
            # raising — see the "empty overview after reduce" log inside it).
            # That is functionally the same failure as an exception: nothing to
            # show. Treating it as success wrote status="complete" with an empty
            # overview to DynamoDB, which the app can't tell apart from "still
            # generating" and shows an infinite progress state for. Degrade the
            # same way an exception would.
            print(f"[groq] analyze returned empty for {key} — "
                  f"treating as failed, persisting transcript only")
            status = "transcribed"
    except Exception as err:  # noqa: BLE001
        print(f"[groq] FAILED for {key} — persisting transcript only: {err}")
        analysis = ai_schema.empty_unified()
        status = "transcribed"

    # The outputs now arrive TOGETHER, so they no longer degrade independently —
    # there is one call to fail, not several. That trade is deliberate and it is
    # a net gain: the old independent degradation existed because extra calls
    # could each be rate-limited on their own, and removing the extra calls
    # removes those failure modes outright rather than handling them. What
    # remains is the same fallback that always mattered most: a failed analysis
    # still persists the transcript (status "transcribed"), and the workspace
    # can still regenerate highlights on demand (POST
    # /recordings/ai/highlights/{key+}) or the user can reprocess.
    highlights = analysis.get("meeting_highlights") or ai_schema.empty_highlights()
    overview = analysis.get("overview") or ai_schema.empty_overview()

    # Step 3: UPSERT one item keyed by audio_s3_key. An UpdateItem (not
    # put_item, which would REPLACE the row) so the ownership/metadata
    # stamped at presign time — user_id, source, duration, a user-typed
    # title, created_at — always survives reprocessing.
    #   - user_id only when the key carries it: legacy keys must never
    #     blank a user_id stamped by getUploadUrl or an unpair.
    #   - title_source decides whether the AI title may be written — see
    #     the block below. Simply checking "is the existing title
    #     non-empty" (the old rule) could never distinguish a real
    #     user-typed title from a client-side placeholder (e.g. an
    #     imported file's own OS-generated name, which is frequently a
    #     raw date/time string) — a placeholder would permanently block
    #     the real AI title from ever being saved. title_source persists
    #     which kind the CURRENT title is, so that distinction survives
    #     across invocations instead of being re-guessed from the string
    #     itself. Values: "placeholder" (AI may always overwrite),
    #     "ai" (AI may update — e.g. on reprocessing with a longer/
    #     corrected transcript), "user" (set only by the explicit rename
    #     API — the AI title must NEVER touch it again).
    # Transcript + timestamps are ElevenLabs'; the rest come from Groq.
    existing = _table.get_item(Key={"audio_s3_key": key}).get("Item") or {}
    fields = {
        "source": source,
        "meeting_id": meeting_id,
        "recorded_at": recorded_at,
        "recording_id": recording_id,
        "s3_key": key,
        # Stored as `ai_tasks`, NOT `tasks` — `tasks` is a DIFFERENT,
        # already-deployed DynamoDB attribute: the persisted map behind
        # Task Detail/Assign/Notify (see lambda-userapi's Tasks section,
        # TASKS_ATTR). ai_tasks is this analysis's raw extraction — read
        # by userApi's task-seeding (which turns it into real, id-bearing
        # entries in the `tasks` map on first read, same as it already
        # does for action_items) — never a map itself, never touched by
        # the task CRUD routes.
        "ai_tasks": analysis["tasks"],
        "participants": analysis["participants"],
        # The DYNAMIC OVERVIEW — the primary user-facing analysis, replacing
        # the fixed `summary` string and `highlights` list. Written
        # unconditionally (unlike meeting_highlights below) because an empty
        # overview already degraded this row to status="transcribed" above, so
        # reaching here means there is something to store; and because a
        # REPROCESS that legitimately produces fewer sections must overwrite
        # the old ones rather than leaving a stale mix of both generations.
        "overview": overview,
        # `summary` is now DERIVED from the overview, not generated. It costs
        # no extra tokens and makes no claim the overview doesn't already
        # make — it is a flattened PREVIEW, kept because two shipped surfaces
        # need one short string per meeting and neither can render sections:
        #   * the meetings list, which shows a 3-line snippet under each row
        #     and searches it (app/src/app/(tabs)/index.tsx);
        #   * the Salesforce push's Summary content target, which writes into
        #     a single long-text field.
        # Bounded to a preview length rather than the whole overview: the list
        # view reads this attribute for EVERY row it returns, so its size is
        # multiplied across the page.
        "summary": ai_schema.overview_text(overview, limit=SUMMARY_PREVIEW_CHARS),
        "ai_version": ai_schema.AI_VERSION,
        "language": language,
        "status": status,
        "created_at": existing.get("created_at") or created_at,
        "error": "",
    }
    # device_id / user_id only when the key carries them: device_id is
    # the device-index GSI hash key and DynamoDB rejects NULL/"" for an
    # index key (MOBILE/UPLOAD rows must simply not be in that index);
    # legacy keys must never blank a stamped user_id.
    if device_id:
        fields["device_id"] = device_id
    if user_id:
        fields["user_id"] = user_id
    fields.update(_resolve_title_fields(existing, analysis["title"]))

    # meeting_highlights is written ONLY when it actually contains
    # something. An all-empty result is indistinguishable from "never
    # generated", and storing it would make the workspace's cache serve
    # emptiness forever instead of regenerating on first open. The
    # ai_version stamp travels with it so a future prompt revision can
    # invalidate old rows without a migration.
    if not ai_schema.highlights_empty(highlights):
        fields["meeting_highlights"] = highlights

    # Reprocessing a recording invalidates any documents generated from the
    # OLD transcript — that is exactly the condition the document cache
    # keys on (see userApi's _transcript_fingerprint). Stamping the new
    # fingerprint here means the next document request regenerates instead
    # of serving a document written against text that no longer exists.
    fields["transcript_fingerprint"] = ai_schema.fingerprint(transcript)

    # Step 3b: the transcript + timestamps go to S3, NOT into the item.
    # Inline, those two attributes were 96% of the row (151 KB + 173 KB on a
    # real 45-minute meeting) and pushed it past DynamoDB's hard 400 KB
    # limit, so UpdateItem threw ValidationException and the recording was
    # stranded at status="generating_ai" forever — after ElevenLabs and Groq
    # had already been paid. See lambda-shared/transcript_store.py for the
    # full reasoning, including why Devanagari hits the ceiling first.
    #
    # S3 BEFORE DynamoDB, deliberately: the two writes are not atomic, and
    # an orphaned S3 object is harmless whereas a row pointing at an object
    # that was never written is a recording whose transcript is gone.
    #
    # A failure here must NOT cost the analysis we just paid for, so it
    # degrades instead of raising: the row is still written (status
    # "transcribed"), and the transcript is recoverable by reprocessing.
    try:
        fields.update(transcript_store.put(
            _s3, bucket, key, transcript, timestamps))
    except Exception as err:  # noqa: BLE001
        print(f"[transcript_store] put FAILED for {key} — persisting "
              f"analysis without the transcript: {err}")
        status = "transcribed"
        fields["status"] = status

    # stt_request_id is deliberately LEFT ON THE ROW. It is the staleness fence
    # (a late duplicate of THIS job must still be recognisable as this job, and a
    # delivery for an OLDER job must still be rejectable), so clearing it here
    # would turn a duplicate delivery into an unmatchable one. What marks the job
    # finished is stt_completed_request_id, written by the webhook's idempotency
    # claim — the reconciler compares the two rather than testing for absence.
    _upsert(key, fields, remove=RETIRED_ANALYSIS_ATTRS)
    print(f"[done] {key} status={status} source={source} (device={device_id})")
    return {"key": key, "status": status}


# ---------------------------------------------------------------------------
# UPSERT helper: SET the given fields on the row keyed by audio_s3_key,
# creating it if absent, never touching fields it wasn't given (unlike
# put_item, which replaces the whole item).
# ---------------------------------------------------------------------------
def _upsert(key, fields, remove=()):
    """SET `fields` on the row, and REMOVE the attributes named in `remove`.

    `remove` exists for attributes that have left the schema: reprocessing a
    row written under an older shape has to clear them, because a SET-only
    update leaves the stale value in place forever (DynamoDB has no notion of
    "the item is exactly this shape now"). Removing an attribute that isn't
    there is a no-op, so this is safe on a fresh row.

    A name may not appear in both — SET and REMOVE of the same attribute in one
    UpdateExpression is rejected by DynamoDB — so `fields` wins and the name is
    dropped from the REMOVE clause.
    """
    names, values, sets, removes = {}, {}, [], []
    for i, (k, v) in enumerate(fields.items()):
        names[f"#f{i}"] = k
        values[f":v{i}"] = v
        sets.append(f"#f{i} = :v{i}")
    for j, k in enumerate(dict.fromkeys(r for r in remove if r not in fields)):
        names[f"#r{j}"] = k
        removes.append(f"#r{j}")

    expression = "SET " + ", ".join(sets) if sets else ""
    if removes:
        expression = (expression + " REMOVE " + ", ".join(removes)).strip()
    if not expression:
        return
    kwargs = {
        "Key": {"audio_s3_key": key},
        "UpdateExpression": expression,
        "ExpressionAttributeNames": names,
    }
    if values:
        kwargs["ExpressionAttributeValues"] = values
    _table.update_item(**kwargs)
