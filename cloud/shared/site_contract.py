"""site_contract.py — the MinuteX -> Salesforce Site delivery contract, v1.

WHAT THIS IS. The request shapes MinuteX POSTs to a customer's Salesforce
Site receiver (salesforce/minutex-site: one Apex REST endpoint,
/services/apexrest/minutex/v1/), and the People shape that receiver sends
back. Pure: no AWS, no network, no clock except where passed in.

WHY IT IS SEPARATE FROM meeting_payload.py. meeting_payload is the contract of
the OAuth-based generic delivery, which is live and must not change under
it. This contract differs on purpose in three ways:

  * The meeting id is OPAQUE. meeting_payload uses the Recordings key, which
    contains the owner's user id and upload path; here it is a one-way hash of
    that key (opaque_meeting_id), stable forever so create/update keys on it.
  * The transcript is sent WHOLE OR NOT AT ALL. meeting_payload truncates at
    30,000 characters and flags it; this contract carries up to 131,072 (the
    Salesforce Long Text Area maximum) and raises TranscriptTooLarge beyond
    that. Nothing is ever silently cut.
  * No destination vocabulary and no internal ids. meeting_payload's
    participants carry salesforce_* ids and MinuteX user/contact ids; here a
    participant carries only what a receiver can match on (name, email) plus
    the generic external identity the People Store recorded, if any.

The summary/overview/highlights/decisions/action-item shaping is REUSED from
meeting_payload (called, never modified), so both contracts read the same
fields the same way — including the legacy highlight shapes.

VERSIONING. CONTRACT_VERSION is sent on every request. The receiver accepts
any 1.x; bump the minor for additive keys, the major for anything else.
"""
import base64
import hashlib
import hmac
import json
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import meeting_payload

# 1.1 (additive): the authenticated-channel actions below. Receivers accept
# any 1.x, and a 1.0 receiver simply refuses the actions it does not know.
# 1.2 (additive): upsert_meeting carries `relation` (the Salesforce records
# the meeting is about), and create_task/update_task carry `related_records`
# (a Who AND a What). A 1.1 receiver ignores both keys: the meeting is stored
# unrelated, and a Task keeps the single related_object_type/related_record_id
# pair, which is still sent.
CONTRACT_VERSION = "1.2"

# The delivery type this contract belongs to — the value that separates this
# path from the existing OAuth delivery everywhere it is stored (job rows,
# endpoint rows, results).
DELIVERY_TYPE_SALESFORCE_SITE = "salesforce_site"

ACTION_HEALTH = "health"
ACTION_UPSERT_MEETING = "upsert_meeting"
ACTION_PEOPLE_SYNC = "people_sync"
# Org actions: sent to the same Site receiver, which works within the
# configured integration user's Salesforce permissions.
ACTION_ORG_INFO = "org_info"
ACTION_OBJECT_SCHEMA = "object_schema"
ACTION_RECORD_SEARCH = "record_search"
ACTION_CREATE_TASK = "create_task"
ACTION_UPDATE_TASK = "update_task"
ACTIONS = (ACTION_HEALTH, ACTION_UPSERT_MEETING, ACTION_PEOPLE_SYNC,
           ACTION_ORG_INFO, ACTION_OBJECT_SCHEMA, ACTION_RECORD_SEARCH,
           ACTION_CREATE_TASK, ACTION_UPDATE_TASK)

# The Salesforce Long Text Area maximum, and therefore the receiver's
# Transcript__c limit. Counted in UTF-16 code units — see text_length.
MAX_TRANSCRIPT_CHARS = 131_072

PEOPLE_TYPES = ("contact", "user")
PEOPLE_STATUSES = ("active", "inactive")

_PROVIDER_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_EXTERNAL_ID_RE = re.compile(r"^[A-Za-z0-9_:.\-]{1,255}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ContractError(ValueError):
    """A payload MinuteX refuses to send. `code` is stable and machine-
    readable; the message is safe to show a user."""

    def __init__(self, code, message, field=""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.field = field


class TranscriptTooLarge(ContractError):
    def __init__(self, chars, limit=MAX_TRANSCRIPT_CHARS):
        super().__init__(
            "transcript_too_large",
            f"The transcript is {chars:,} characters; Salesforce Site delivery "
            f"supports at most {limit:,}. The meeting was not delivered.",
            field="transcript.text")
        self.chars = chars
        self.limit = limit


def text_length(text):
    """Length as Salesforce counts it: UTF-16 code units.

    Apex String.length() and the Long Text Area limit both count UTF-16 code
    units, so a character outside the Basic Multilingual Plane (most emoji)
    counts TWO. Python's len() counts code points and would call a
    131,000-character transcript with a few hundred emoji "within the limit"
    when Salesforce will refuse it. Devanagari, Latin and CJK are all BMP, so
    for ordinary transcripts the two counts agree.
    """
    if not text:
        return 0
    return len(str(text).encode("utf-16-le")) // 2


def opaque_meeting_id(recording_key):
    """A stable, opaque id for one meeting: "mxm_" + 128 bits of SHA-256.

    Stable (the same recording always maps to the same id, so the receiver's
    create-then-update keying works across every delivery forever) and opaque
    (the Recordings key embeds the owner's user id and upload path, which a
    customer's CRM has no business holding). Unsalted on purpose: a salt is a
    secret that could rotate, and rotating it would re-key every meeting and
    duplicate every record already delivered. The key itself contains a
    random component, so the hash cannot be enumerated.
    """
    key = str(recording_key or "").strip()
    if not key:
        return ""
    digest = hashlib.sha256(f"minutex-meeting:v1:{key}".encode("utf-8"))
    return "mxm_" + digest.hexdigest()[:32]


def new_request_id():
    """Unique per HTTP attempt — the tracing handle, never a dedupe key."""
    return uuid.uuid4().hex


def meeting_idempotency_key(job_id, generation=0):
    """The receiver's dedupe key for one delivery INTENT.

    Keyed on the job (one push) and its generation (bumped by an explicit
    user retry), NOT on the attempt: an automatic retry after a timeout MUST
    reuse the key, because the timed-out attempt may already have committed
    in Salesforce and the receiver has to recognise the replay. A user retry
    is a new intent — possibly after editing the meeting — so it gets a new
    key and is processed rather than answered from the old result.
    """
    return f"minutex:{job_id}:g{int(generation or 0)}:upsert_meeting"


def iso_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds") \
        .replace("+00:00", "Z")


def envelope(action, payload=None, *, request_id, idempotency_key="",
             sent_at=None):
    """The request body every action shares."""
    if action not in ACTIONS:
        raise ContractError("unsupported_action", f"unknown action {action!r}")
    out = {
        "contract_version": CONTRACT_VERSION,
        "action": action,
        "request_id": request_id,
        "sent_at": sent_at or iso_now(),
        "payload": payload if payload is not None else {},
    }
    if idempotency_key:
        out["idempotency_key"] = idempotency_key
    return out


def _json_default(value):
    # DynamoDB hands back Decimal. str() would turn a duration into "1800",
    # which the receiver rightly rejects as not-a-number.
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return str(value)


def encode_body(env):
    """The exact bytes sent AND signed. Compact separators and UTF-8 text:
    the HMAC is over these bytes, so they are produced in exactly one place."""
    return json.dumps(env, separators=(",", ":"), ensure_ascii=False,
                      default=_json_default).encode("utf-8")


# ---------------------------------------------------------------------------
# Audio link tokens
#
# The audio URL written into Salesforce is a MinuteX URL,
#   {base}/delivery/audio/{token}
# never a presigned S3 URL. A presign from a Lambda role dies with the role's
# session (hours), so a CRM record would hold a dead link by the next day;
# a long-lived one would be a bearer credential for the raw recording.
#
# The token names only the DELIVERY JOB (a random id) and an expiry, signed
# with the workspace endpoint's random audio key. It deliberately does NOT
# carry the recording key: the claims are encoded, not encrypted, and the
# recording key embeds the owner's user id and upload path. The route
# resolves job -> (workspace, recording) from the authoritative CrmSyncJobs
# row, then re-checks the signature, the expiry, that Site delivery is still
# enabled and that the meeting still belongs to that workspace, and 302s to
# a fresh 15-minute presign — the same re-sign-per-request pattern the public
# share audio route uses, which is what lets an 85 MB recording stream and
# seek.
# ---------------------------------------------------------------------------

AUDIO_TOKEN_VERSION = "v1"


def _b64u(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _audio_mac(audio_key, claims_b64):
    return hmac.new(str(audio_key).encode("utf-8"),
                    f"{AUDIO_TOKEN_VERSION}.{claims_b64}".encode("ascii"),
                    hashlib.sha256).digest()


def sign_audio_token(job_id, expires_at_epoch, audio_key):
    claims = _b64u(json.dumps(
        {"j": str(job_id), "e": int(expires_at_epoch)},
        separators=(",", ":")).encode("utf-8"))
    return f"{AUDIO_TOKEN_VERSION}.{claims}.{_b64u(_audio_mac(audio_key, claims))}"


def read_audio_token(token):
    """The token's UNVERIFIED claims {j, e}, or None if malformed.

    Unverified on purpose: the signing key belongs to the job's workspace, so
    the caller must read the claims to find the job and then the key, and
    call verify_audio_token before trusting anything.
    """
    parts = str(token or "").split(".")
    if len(parts) != 3 or parts[0] != AUDIO_TOKEN_VERSION or len(token) > 2000:
        return None
    try:
        claims = json.loads(_b64u_decode(parts[1]))
    except (ValueError, TypeError):
        return None
    if not isinstance(claims, dict) or not isinstance(claims.get("j"), str) \
            or not claims["j"] or not isinstance(claims.get("e"), int):
        return None
    return claims


def verify_audio_token(token, audio_key):
    parts = str(token or "").split(".")
    if len(parts) != 3 or not audio_key:
        return False
    try:
        given = _b64u_decode(parts[2])
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(given, _audio_mac(audio_key, parts[1]))


# ---------------------------------------------------------------------------
# Meeting payload
# ---------------------------------------------------------------------------

def _s(value, limit=None):
    out = "" if value is None else str(value).strip()
    return out[:limit] if limit else out


def _participant(raw):
    """One participant, allow-listed. `raw` is built by the caller from
    MeetingParticipants + Contacts; anything not named here never ships."""
    ext = raw.get("external_identity") if isinstance(raw, dict) else None
    identity = None
    if isinstance(ext, dict) and _s(ext.get("external_id")):
        identity = {
            "provider": _s(ext.get("provider")),
            "type": _s(ext.get("type")),
            "external_id": _s(ext.get("external_id")),
        }
    return {
        "speaker_id": _s(raw.get("speaker_id")),
        "name": _s(raw.get("name"), 255),
        "email": _s(raw.get("email"), 254).lower(),
        "phone": _s(raw.get("phone"), 40),
        "company": _s(raw.get("company"), 255),
        "identity_role": _s(raw.get("identity_role"), 20),
        "participant_role": _s(raw.get("participant_role"), 80),
        "contribution_summary": _s(raw.get("contribution_summary"), 4000),
        "attendance_only": bool(raw.get("attendance_only")),
        "tagged": bool(raw.get("tagged")),
        "external_identity": identity,
    }


def _action_item(row):
    """meeting_payload's action item, minus MinuteX-internal account ids."""
    assignee = row.get("assignee") or {}
    return {
        "id": _s(row.get("minutex_task_id")),
        "title": _s(row.get("title")),
        "description": _s(row.get("description")),
        "status": _s(row.get("status")),
        "priority": _s(row.get("priority")),
        "due_date": _s(row.get("due_date")),
        "due_date_normalized": _s(row.get("due_date_normalized")),
        "assignee": {
            "name": _s(assignee.get("name")),
            "email": _s(assignee.get("email")),
            "speaker_id": _s(assignee.get("speaker_id")),
        },
        "source": _s(row.get("source")),
        "created_at": _s(row.get("created_at")),
    }


def _decision(row):
    return {
        "decision": _s(row.get("decision")),
        "context": _s(row.get("context")),
        "speaker_id": _s(row.get("speaker_id")),
    }


def relation_block(relations):
    """The meeting's Salesforce relationship for upsert_meeting: {who, what},
    each {object_type, record_id, name} or None. Only what a person chose (or
    the synced Contact they tagged) — never inferred from meeting text."""
    relations = relations if isinstance(relations, dict) else {}
    out = {}
    for slot in ("who", "what"):
        ref = relations.get(slot)
        out[slot] = {
            "object_type": _s(ref.get("object_api_name"), 80),
            "record_id": _s(ref.get("record_id"), 18),
            "name": _s(ref.get("record_name"), 255),
        } if isinstance(ref, dict) and _s(ref.get("record_id")) else None
    return out


def task_related_records(relations):
    """create_task/update_task `related_records`: every side that is set."""
    block = relation_block(relations)
    return [{"object_type": block[slot]["object_type"],
             "record_id": block[slot]["record_id"]}
            for slot in ("who", "what") if block[slot]]


def build_meeting_payload(item, tasks=None, *, participants=None,
                          workspace=None, meeting_url="", audio=None,
                          relation=None):
    """THE upsert_meeting payload. Raises TranscriptTooLarge / ContractError.

    Args:
      item: the Recordings row, ALREADY HYDRATED (transcript_store.hydrate).
      tasks: the meeting's Tasks rows (cancelled ones already removed).
      participants: caller-built participant dicts (see _participant). When
            None, falls back to the AI's speaker list with no identity.
      workspace: {"id", "name"} — only for an organisation. A personal
            workspace id embeds the owner's user id, so callers pass None.
      meeting_url: a stable MinuteX deep link, or "".
      audio: the audio block (see audio_block), or None.
      relation: the meeting's {who, what} Salesforce relationship, or None.
    """
    item = item if isinstance(item, dict) else {}
    key = _s(item.get("audio_s3_key"))
    meeting_id = opaque_meeting_id(key)
    if not meeting_id:
        raise ContractError("invalid_meeting", "this meeting has no id",
                            field="meeting.id")

    # Reused, not re-implemented: meeting_payload already reads every legacy
    # summary/highlight/task shape. include_transcript=False keeps its 30k
    # truncation out of this contract entirely — the transcript is built
    # below under this contract's own rule.
    base = meeting_payload.build_meeting_payload(
        item, tasks, None, meeting_url=meeting_url,
        workspace_id="", include_transcript=False)
    base_meeting = base.get("meeting") or {}

    if participants is None:
        participants = [{
            "speaker_id": p.get("speaker_id"),
            "name": p.get("display_name"),
            "contribution_summary": p.get("contribution_summary"),
        } for p in base.get("participants") or []]

    text = _s(item.get("transcript"))
    chars = text_length(text)
    if chars > MAX_TRANSCRIPT_CHARS:
        raise TranscriptTooLarge(chars)

    ws = workspace if isinstance(workspace, dict) else {}
    return {
        "meeting": {
            "id": meeting_id,
            "title": _s(base_meeting.get("title")),
            "status": _s(base_meeting.get("status")),
            "source": _s(base_meeting.get("source")),
            "language": _s(base_meeting.get("language")),
            "started_at": _s(base_meeting.get("started_at")),
            "ended_at": _s(base_meeting.get("ended_at")),
            "duration_seconds": base_meeting.get("duration_seconds") or 0,
            "created_at": _s(base_meeting.get("created_at")),
            "url": _s(meeting_url),
            "workspace": {"id": _s(ws.get("id")), "name": _s(ws.get("name"))},
        },
        "participants": [_participant(p) for p in participants
                         if isinstance(p, dict)],
        "summary": {
            "text": _s(base.get("summary")),
            "overview": base.get("overview") or {"sections": [], "text": ""},
            "highlights": base.get("highlights") or {},
            "decisions": [_decision(d) for d in base.get("decisions") or []],
        },
        "action_items": [_action_item(a) for a in base.get("action_items") or []],
        "transcript": {
            "available": bool(text) or bool(item.get("transcript_s3_key")),
            "text": text,
            "chars": chars,
            "language": _s(item.get("language")),
            "format": "text",
        },
        "audio": audio if isinstance(audio, dict) else audio_block(None),
        "relation": relation_block(relation),
    }


def audio_block(url, *, size_bytes=0, content_type="audio/wav",
                duration_seconds=0, expires_at=""):
    """The audio section. A URL only — never bytes, never Base64."""
    if not url:
        return {"available": False, "url": "", "size_bytes": 0,
                "content_type": "", "duration_seconds": 0, "expires_at": ""}
    return {
        "available": True,
        "url": url,
        "size_bytes": int(size_bytes or 0),
        "content_type": content_type or "audio/wav",
        "duration_seconds": duration_seconds or 0,
        "expires_at": expires_at,
    }


def payload_counts(payload):
    """Structural counts for logs and job results — never content."""
    payload = payload if isinstance(payload, dict) else {}
    summary = payload.get("summary") or {}
    return {
        "participants": len(payload.get("participants") or []),
        "action_items": len(payload.get("action_items") or []),
        "decisions": len(summary.get("decisions") or []),
        "transcript_chars": int((payload.get("transcript") or {}).get("chars") or 0),
        "audio": bool((payload.get("audio") or {}).get("available")),
    }


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------

def person_key(provider, person_type, external_id):
    return f"{provider}#{person_type}#{external_id}"


def normalize_person(raw):
    """One person from a receiver, validated into the People Store shape.

    Returns the normalized dict, or None for a row that cannot be stored
    (no stable identity, unknown type). A receiver is a system outside
    MinuteX, so every field is bounded and nothing is trusted to be a string.
    """
    if not isinstance(raw, dict):
        return None
    provider = _s(raw.get("provider")).lower()
    person_type = _s(raw.get("type")).lower()
    external_id = _s(raw.get("external_id"))
    if not _PROVIDER_RE.match(provider) or person_type not in PEOPLE_TYPES \
            or not _EXTERNAL_ID_RE.match(external_id):
        return None

    name = _s(raw.get("name"), 200)
    if not name:
        name = " ".join(p for p in (_s(raw.get("first_name")),
                                    _s(raw.get("last_name"))) if p)[:200]
    email = _s(raw.get("email"), 254).lower()
    if email and not _EMAIL_RE.match(email):
        email = ""
    if not name and not email:
        return None
    status = _s(raw.get("status")).lower()
    if status not in PEOPLE_STATUSES:
        status = "active"
    return {
        "person_key": person_key(provider, person_type, external_id),
        "provider": provider,
        "type": person_type,
        "external_id": external_id,
        "name": name,
        "email": email,
        "phone": _s(raw.get("phone"), 40),
        "title": _s(raw.get("title"), 128),
        "company": _s(raw.get("company"), 200),
        "status": status,
        "source_updated_at": _s(raw.get("source_updated_at"), 40),
    }
