"""webhook_contract.py — MinuteX Webhook Contract v2.0 (the sender side).

WHAT THIS IS. The request shapes MinuteX POSTs to a customer-controlled
webhook endpoint, and the validation of what that endpoint sends back
(configuration, record search, people). Pure: no AWS, no network, no clock
except where passed in. The normative spec is docs/webhook-contract-v2.md;
this module and that document must never disagree.

HOW IT DIFFERS FROM site_contract (v1, the MinuteX Salesforce package). v1
delivers into a receiver MinuteX ships (upsert_meeting, Destinations, Field
Mappings). v2 delivers EVENTS; the customer's endpoint owns meeting types,
record search and every business rule. So:

  * The unit is an event (meeting.created / .updated / .deleted) carrying a
    FULL snapshot plus a `changed_fields` hint — never a diff.
  * `sequence` is monotonic per meeting and `event_id` is derived from it, so
    one (meeting_id, sequence) is bound to exactly one content state for its
    whole life (docs §3.4). The binding itself is enforced where sequences
    are allocated (userapi, one conditional write); this module only derives.
  * The person being met and the business record are separate:
    participants[].external_identity vs record_context (docs §3.1).

REUSED, NOT RE-IMPLEMENTED. The snapshot is v1's build_meeting_payload (the
same participant/summary/action-item/transcript shaping, including the
131,072-char whole-or-nothing transcript rule) minus `relation`, plus the
meeting's lifecycle and minutes. The opaque meeting id is v1's, so a meeting
keeps one identity across both contracts.
"""
import hashlib
import json
import re
from decimal import Decimal

import site_contract

CONTRACT_VERSION = "2.0"

# How this path is told apart wherever it is stored (endpoint rows, jobs).
# The transport is generic; the provider says whose endpoint it is. Provider
# is MinuteX-internal routing data and is NOT sent in the event envelope.
DELIVERY_TYPE_WEBHOOK = "webhook"
PROVIDER_SALESFORCE = "salesforce"
PROVIDERS = (PROVIDER_SALESFORCE,)

EVENT_CREATED = "meeting.created"
EVENT_UPDATED = "meeting.updated"
EVENT_DELETED = "meeting.deleted"
EVENTS = (EVENT_CREATED, EVENT_UPDATED, EVENT_DELETED)

# Body `type` per endpoint. A receiver rejects a body whose type does not
# match the endpoint it arrived on, so a signed body cannot be replayed
# against a different one.
TYPE_EVENT = "event"
TYPE_CONFIGURATION = "configuration.request"
TYPE_SEARCH = "records.search"
TYPE_PEOPLE = "people.sync"
TYPE_PEOPLE_SEARCH = "people.search"
TYPE_PEOPLE_RELATED = "people.related"
PATH_EVENTS = "/events"
PATH_CONFIGURATION = "/configuration"
PATH_SEARCH = "/records/search"
PATH_PEOPLE = "/people"
PATH_PEOPLE_SEARCH = "/people/search"
PATH_PEOPLE_RELATED = "/people/related"

LIFECYCLE_ACTIVE = "active"
LIFECYCLE_TRASHED = "trashed"
LIFECYCLE_DELETED = "deleted"

# Snapshot sections, hashed separately so `changed_fields` can name them.
# `audio` is deliberately absent: its signed URL and expiry change on every
# build, so hashing it would make every snapshot look changed.
SECTIONS = ("meeting", "lifecycle", "participants", "summary", "minutes",
            "action_items", "transcript", "meeting_type", "record_context")

MAX_MINUTES_CHARS = 131_072

# Response limits (docs §5-§7).
MAX_CONFIGURATION_BYTES = 64 * 1024
MAX_SEARCH_BYTES = 256 * 1024
MAX_PEOPLE_BYTES = 2 * 1024 * 1024
MAX_MEETING_TYPES = 50
MAX_SEARCH_RECORDS = 50
MAX_SEARCH_QUERY_CHARS = 100
MAX_SEARCH_LIMIT = 25
MAX_CURSOR_CHARS = 256
MAX_PEOPLE_PAGE_SIZE = 500
MAX_PEOPLE_SEARCH_BYTES = 256 * 1024
MAX_PEOPLE_SEARCH_LIMIT = 20
MIN_PEOPLE_SEARCH_CHARS = 2
MAX_RELATED_RECORDS = 5
MAX_PEOPLE_RELATED_LIMIT = 50

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


class ContractError(site_contract.ContractError):
    """Something MinuteX refuses to send, or a response it refuses to accept.
    `code` is stable and machine-readable; the message is safe to show."""


def _s(value, limit=None):
    out = "" if value is None else str(value).strip()
    return out[:limit] if limit else out


def _json_default(value):
    # DynamoDB hands back Decimal; keep numbers numbers.
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return str(value)


def canonical_json(value):
    """Deterministic bytes for hashing: sorted keys, compact, UTF-8."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=_json_default).encode("utf-8")


def encode_body(env):
    """The exact bytes sent AND signed — produced in exactly one place."""
    return json.dumps(env, separators=(",", ":"), ensure_ascii=False,
                      default=_json_default).encode("utf-8")


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def event_id(meeting_id, sequence):
    """THE event id for one (meeting, sequence): "evt_" + 128 bits of SHA-256.

    Derived, never random, so a retry of the same logical event carries the
    same id by construction. Because a sequence is never re-bound to other
    content (docs §3.4), the same event_id always means the same content.
    """
    mid = _s(meeting_id)
    seq = int(sequence)
    if not mid or seq < 1:
        raise ContractError("invalid_event", "an event needs a meeting and a "
                                             "sequence of at least 1")
    digest = hashlib.sha256(f"minutex-event:v2:{mid}:{seq}".encode("utf-8"))
    return "evt_" + digest.hexdigest()[:32]


# ---------------------------------------------------------------------------
# Meeting context (meeting type + record) — what the user chose
# ---------------------------------------------------------------------------

def meeting_type_block(context):
    """{id, name} from the stored integration context, or None."""
    ctx = context if isinstance(context, dict) else {}
    type_id = _s(ctx.get("meeting_type_id"), 64)
    if not type_id:
        return None
    return {"id": type_id, "name": _s(ctx.get("meeting_type_name"), 80)}


def record_context_block(context):
    """{record_id, display_name, secondary_text} or None. Independent of the
    participants: clearing the record never touches a tagged Contact."""
    ctx = context if isinstance(context, dict) else {}
    record_id = _s(ctx.get("record_id"), 64)
    if not record_id:
        return None
    return {"record_id": record_id,
            "display_name": _s(ctx.get("record_display_name"), 200),
            "secondary_text": _s(ctx.get("record_secondary_text"), 200)}


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def minutes_block(markdown):
    text = _s(markdown)
    truncated = site_contract.text_length(text) > MAX_MINUTES_CHARS
    if truncated:
        # Cut on a UTF-16 boundary the way Salesforce counts, and SAY so —
        # nothing is ever shortened silently.
        text = text.encode("utf-16-le")[:MAX_MINUTES_CHARS * 2] \
            .decode("utf-16-le", errors="ignore")
    return {"available": bool(text), "markdown": text, "truncated": truncated}


def build_snapshot(item, tasks=None, *, participants=None, workspace=None,
                   meeting_url="", audio=None, lifecycle=LIFECYCLE_ACTIVE,
                   minutes_markdown=""):
    """The `data` of a created/updated event. Raises TranscriptTooLarge /
    ContractError exactly as v1 does — the transcript is whole or not sent."""
    data = site_contract.build_meeting_payload(
        item, tasks, participants=participants, workspace=workspace,
        meeting_url=meeting_url, audio=audio, relation=None)
    # v1-only keys: the customer's endpoint owns relations and destinations.
    for v1_only in ("relation", "destination", "acting_user_id"):
        data.pop(v1_only, None)
    data["meeting"]["lifecycle"] = lifecycle if lifecycle in (
        LIFECYCLE_ACTIVE, LIFECYCLE_TRASHED) else LIFECYCLE_ACTIVE
    data["minutes"] = minutes_block(minutes_markdown)
    return data


def deleted_snapshot(meeting_id, deleted_at):
    return {"meeting": {"id": _s(meeting_id), "deleted_at": _s(deleted_at)}}


def section_hashes(data, meeting_type=None, record_context=None):
    """{section: sha256 hex} over the snapshot plus the chosen context."""
    data = data if isinstance(data, dict) else {}
    meeting = dict(data.get("meeting") or {})
    lifecycle = meeting.pop("lifecycle", LIFECYCLE_ACTIVE)
    parts = {
        "meeting": meeting,
        "lifecycle": lifecycle,
        "participants": data.get("participants") or [],
        "summary": data.get("summary") or {},
        "minutes": data.get("minutes") or {},
        "action_items": data.get("action_items") or [],
        "transcript": data.get("transcript") or {},
        "meeting_type": meeting_type,
        "record_context": record_context,
    }
    return {name: hashlib.sha256(canonical_json(parts[name])).hexdigest()
            for name in SECTIONS}


def content_hash(hashes):
    """One hash for the whole snapshot state: what binds a sequence."""
    ordered = [f"{name}={(hashes or {}).get(name, '')}" for name in SECTIONS]
    return hashlib.sha256("|".join(ordered).encode("utf-8")).hexdigest()


def changed_fields(old_hashes, new_hashes):
    """The sections whose hash differs — a HINT (docs §3.3). ["*"] when there
    is no previous state to compare with on an update."""
    if not old_hashes:
        return ["*"]
    return [name for name in SECTIONS
            if (old_hashes or {}).get(name) != (new_hashes or {}).get(name)]


# ---------------------------------------------------------------------------
# Envelopes
# ---------------------------------------------------------------------------

def event_envelope(event, *, meeting_id, sequence, occurred_at, workspace_id="",
                   meeting_type=None, record_context=None, changed=None,
                   data=None):
    if event not in EVENTS:
        raise ContractError("unsupported_event", f"unknown event {event!r}")
    return {
        "type": TYPE_EVENT,
        "contract_version": CONTRACT_VERSION,
        "event": event,
        "event_id": event_id(meeting_id, sequence),
        "occurred_at": _s(occurred_at),
        "workspace_id": _s(workspace_id),
        "meeting_id": _s(meeting_id),
        "sequence": int(sequence),
        "meeting_type": meeting_type,
        "record_context": record_context,
        "changed_fields": list(changed or []),
        "data": data if isinstance(data, dict) else {},
    }


def configuration_request(*, request_id, workspace_id="", requested_at=None):
    return {"type": TYPE_CONFIGURATION, "contract_version": CONTRACT_VERSION,
            "request_id": request_id, "workspace_id": _s(workspace_id),
            "requested_at": requested_at or site_contract.iso_now()}


def search_request(*, request_id, meeting_type_id, query, limit=10, cursor="",
                   workspace_id=""):
    query = _s(query)
    if len(query) > MAX_SEARCH_QUERY_CHARS:
        raise ContractError("query_too_long",
                            f"search at most {MAX_SEARCH_QUERY_CHARS} characters")
    if not ID_RE.match(_s(meeting_type_id)):
        raise ContractError("invalid_meeting_type", "unknown meeting type")
    cursor = _s(cursor)
    if len(cursor) > MAX_CURSOR_CHARS:
        raise ContractError("invalid_cursor", "invalid cursor")
    return {"type": TYPE_SEARCH, "contract_version": CONTRACT_VERSION,
            "request_id": request_id, "workspace_id": _s(workspace_id),
            "meeting_type_id": _s(meeting_type_id), "query": query,
            "limit": max(1, min(MAX_SEARCH_LIMIT, int(limit or 10))),
            "cursor": cursor}


def people_request(*, request_id, cursor="", page_size=200, workspace_id=""):
    return {"type": TYPE_PEOPLE, "contract_version": CONTRACT_VERSION,
            "request_id": request_id, "workspace_id": _s(workspace_id),
            "cursor": _s(cursor)[:MAX_CURSOR_CHARS],
            "page_size": max(1, min(MAX_PEOPLE_PAGE_SIZE, int(page_size or 200)))}


def people_search_request(*, request_id, query, limit=20, types=None,
                          user_email="", workspace_id=""):
    """Live search of the customer's people (Contacts, Leads, Users) for
    speaker tagging. Nothing is copied: the receiver searches its own org.

    `user_email` is the MinuteX user doing the search, so a receiver that can
    check record access returns only people that user may see in Salesforce.
    """
    query = _s(query)
    if len(query) < MIN_PEOPLE_SEARCH_CHARS:
        raise ContractError("query_too_short",
                            f"type at least {MIN_PEOPLE_SEARCH_CHARS} characters")
    if len(query) > MAX_SEARCH_QUERY_CHARS:
        raise ContractError("query_too_long",
                            f"search at most {MAX_SEARCH_QUERY_CHARS} characters")
    wanted = [t for t in (types or site_contract.PEOPLE_TYPES)
              if t in site_contract.PEOPLE_TYPES]
    if not wanted:
        raise ContractError("invalid_people_types", "no searchable people type")
    return {"type": TYPE_PEOPLE_SEARCH, "contract_version": CONTRACT_VERSION,
            "request_id": request_id, "workspace_id": _s(workspace_id),
            "query": query, "types": wanted,
            "limit": max(1, min(MAX_PEOPLE_SEARCH_LIMIT, int(limit or 20))),
            "user_email": _s(user_email, 254).lower()}


def people_related_request(*, request_id, record_ids, limit=50, user_email="",
                           workspace_id=""):
    """The people on the records a meeting is linked to (a Lead itself, an
    Account's Contacts, an Opportunity's contact roles...), suggested before
    anyone searches. The receiver decides what "on the record" means."""
    ids = []
    for raw in record_ids or []:
        rid = _s(raw)
        if ID_RE.match(rid) and rid not in ids:
            ids.append(rid)
    if not ids:
        raise ContractError("no_records", "the meeting isn't linked to a record")
    return {"type": TYPE_PEOPLE_RELATED, "contract_version": CONTRACT_VERSION,
            "request_id": request_id, "workspace_id": _s(workspace_id),
            "record_ids": ids[:MAX_RELATED_RECORDS],
            "limit": max(1, min(MAX_PEOPLE_RELATED_LIMIT, int(limit or 50))),
            "user_email": _s(user_email, 254).lower()}


# ---------------------------------------------------------------------------
# Response validation — the endpoint is outside MinuteX, trust nothing
# ---------------------------------------------------------------------------

def _bad(code, message):
    raise ContractError(code, message)


def validate_configuration(obj):
    """The endpoint's configuration, normalized, or ContractError.

    Unknown keys are dropped. `configuration_version` is REQUIRED: it is what
    an admin reads to tell one configuration from the next.
    """
    if not isinstance(obj, dict):
        _bad("invalid_configuration", "the configuration is not a JSON object")
    version = _s(obj.get("configuration_version"))
    if not version or len(version) > 64:
        _bad("invalid_configuration",
             "configuration_version is required (1-64 characters)")
    types = obj.get("meeting_types")
    if not isinstance(types, list):
        _bad("invalid_configuration", "meeting_types must be a list")
    if len(types) > MAX_MEETING_TYPES:
        _bad("invalid_configuration",
             f"at most {MAX_MEETING_TYPES} meeting types are supported")
    out, seen = [], set()
    for i, raw in enumerate(types):
        if not isinstance(raw, dict):
            _bad("invalid_configuration", f"meeting_types[{i}] is not an object")
        type_id = _s(raw.get("id"))
        if not ID_RE.match(type_id):
            _bad("invalid_configuration", f"meeting_types[{i}].id is not a valid id")
        if type_id in seen:
            _bad("invalid_configuration", f"meeting type id {type_id!r} is repeated")
        seen.add(type_id)
        name = _s(raw.get("name"))
        if not name or len(name) > 80:
            _bad("invalid_configuration",
                 f"meeting_types[{i}].name must be 1-80 characters")
        selection = raw.get("record_selection")
        selection = selection if isinstance(selection, dict) else {}
        enabled = selection.get("enabled") is True
        required = selection.get("required") is True
        if required and not enabled:
            _bad("invalid_configuration",
                 f"meeting type {type_id!r}: a required record must be enabled")
        out.append({
            "id": type_id,
            "name": name,
            "description": _s(raw.get("description"), 200),
            "record_selection": {
                "enabled": enabled,
                "required": required,
                "label": _s(selection.get("label"), 40),
                "placeholder": _s(selection.get("placeholder"), 80),
            },
        })
    # Passed through for contract_v3.normalize_linkable_objects: a receiver
    # that supports linking sends its linkable objects; None means it doesn't.
    linkable = obj.get("linkable_objects")
    return {"configuration_version": version, "meeting_types": out,
            "linkable_objects": linkable if isinstance(linkable, list) else None}


def validate_search_response(obj):
    """{records:[{id, display_name, secondary_text, object_type}], next_cursor}."""
    if not isinstance(obj, dict) or not isinstance(obj.get("records"), list):
        _bad("invalid_search_response", "the search response has no records list")
    records = obj["records"]
    if len(records) > MAX_SEARCH_RECORDS:
        _bad("invalid_search_response",
             f"at most {MAX_SEARCH_RECORDS} records per search")
    out = []
    for raw in records:
        if not isinstance(raw, dict):
            continue
        record_id = _s(raw.get("id"))
        display = _s(raw.get("display_name"))
        # A row without a usable id or label is skipped rather than failing
        # the whole search: the user can still pick from the rest.
        if not ID_RE.match(record_id) or not display or len(display) > 200:
            continue
        out.append({"id": record_id, "display_name": display,
                    "secondary_text": _s(raw.get("secondary_text"), 200),
                    "object_type": _s(raw.get("object_type"), 80)})
    cursor = _s(obj.get("next_cursor"))
    return {"records": out,
            "next_cursor": cursor if len(cursor) <= MAX_CURSOR_CHARS else ""}


def validate_people_page(obj):
    """(raw_people, next_cursor, done) — the page's SHAPE only.

    Rows are returned raw: people_store.upsert_people normalizes each one with
    site_contract.normalize_person (the docs §7 field rules) and counts the
    rows it skips, which is what the settings screen reports. Normalizing
    here as well would hide those skips.
    """
    if not isinstance(obj, dict) or not isinstance(obj.get("people"), list):
        _bad("invalid_people_response", "the people response has no people list")
    people = [raw for raw in obj["people"] if isinstance(raw, dict)]
    if len(people) > MAX_PEOPLE_PAGE_SIZE:
        _bad("invalid_people_response",
             f"at most {MAX_PEOPLE_PAGE_SIZE} people per page")
    cursor = _s(obj.get("next_cursor"))
    if len(cursor) > MAX_CURSOR_CHARS:
        _bad("invalid_people_response", "next_cursor is too long")
    done = obj.get("done") is True or not cursor
    return people, cursor, done


def validate_people_search_response(obj):
    """[person] in the People Store shape, best match first as the receiver
    ordered them. Unusable rows are skipped, not fatal: the user can still
    pick from the rest. Repeated people are dropped."""
    if not isinstance(obj, dict) or not isinstance(obj.get("people"), list):
        _bad("invalid_people_search_response", "the search response has no people list")
    if len(obj["people"]) > MAX_PEOPLE_SEARCH_LIMIT * 3:
        _bad("invalid_people_search_response",
             f"at most {MAX_PEOPLE_SEARCH_LIMIT * 3} people per search")
    out, seen = [], set()
    for raw in obj["people"]:
        person = site_contract.normalize_person(raw)
        if person is None or person["person_key"] in seen:
            continue
        seen.add(person["person_key"])
        out.append(person)
    return out


def validate_people_related_response(obj):
    """Like a search response, plus each person's `relation` (why they are
    suggested, e.g. "Contact at Sharma Family"), at most 80 characters."""
    if not isinstance(obj, dict) or not isinstance(obj.get("people"), list):
        _bad("invalid_people_related_response", "the response has no people list")
    if len(obj["people"]) > MAX_PEOPLE_RELATED_LIMIT:
        _bad("invalid_people_related_response",
             f"at most {MAX_PEOPLE_RELATED_LIMIT} related people")
    out, seen = [], set()
    for raw in obj["people"]:
        person = site_contract.normalize_person(raw)
        if person is None or person["person_key"] in seen:
            continue
        seen.add(person["person_key"])
        person["relation"] = _s(raw.get("relation"), 80)
        out.append(person)
    return out
