"""notification_schema.py — the generic in-app notification model.

WHAT THIS IS. One place that answers "what notifications can MinuteX raise,
what does each one say, and which entity does it point at?" — for the in-app
centre today, and for Gmail / WhatsApp / push as DELIVERY CHANNELS later
without a second type vocabulary, a second content style or a second table.

WHY IT IS A SHARED MODULE. The two Lambdas that raise notifications are
different processes: userApi (JWT routes — task assignment, AI review,
documents, sharing) and transcribeRecording (the S3-triggered pipeline —
processing completed/failed). Both are packaged with shared/*.py flat at the
archive root (scripts/_package_lambda.py), so putting the model here is what
makes ONE notification vocabulary serve both. A copy in each Lambda would
drift the moment a type was added on one side.

THE ENGINE IS NOT HERE. This module is pure data and pure functions: no
boto3, no table handle, no clock beyond what is passed in. The WRITE side
lives in userApi (`notifications` section) because that is where the table
handle and the audit helper already live, and transcribeRecording reaches it
through the same shared contract. That split matches ai_schema (shape here,
persistence there) rather than inventing a new arrangement.

DELIVERY CHANNELS. Every notification records the channels it was delivered
on. In Phase 1 that is always exactly [IN_APP] — the constant exists so a
later Gmail/WhatsApp channel is an added value in this list plus a delivery
worker, NOT a schema migration and NOT a change to any event site. Nothing in
this phase reads it except the API response; it is the seam, not a feature.

WHAT A NOTIFICATION IS NOT. It is not a copy of the entity. It stores a
REFERENCE (entity_type + entity_id) plus the minimum display text, because a
notification that embedded a task would show stale text after a rename, and
one that embedded a document would blow past DynamoDB's item limit. The app
re-reads the entity when the user opens it — which is also what re-checks
ownership at open time (see the engine's authorization note).

CONTENT IS ASSEMBLED HERE, NOT AT THE CALL SITE. `build()` is the only way to
make a notification body. Event sites pass facts (a task title, a meeting
title); the wording lives in one table below. That keeps ten call sites from
inventing ten phrasings of "task assigned", and makes the copy reviewable in
one screen. No content is ever invented: a builder that has no title to show
says nothing rather than guessing one (project rule — never fabricate).
"""

from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Delivery channels. Phase 1 activates exactly one.
#
# The list on a stored notification is what WAS delivered, not what was
# requested — so a later email channel that fails does not get to claim it
# reached the user. In this phase there is nothing to fail: an in-app
# notification is delivered by existing.
# ---------------------------------------------------------------------------
CHANNEL_IN_APP = "IN_APP"
CHANNEL_EMAIL = "EMAIL"        # not implemented in Phase 1
CHANNEL_WHATSAPP = "WHATSAPP"  # not implemented in Phase 1
CHANNEL_PUSH = "PUSH"          # not implemented in Phase 1

# The only channel this phase delivers on. Every notification is written with
# exactly this, so the day a second channel ships, the rows already say which
# ones actually reached the user rather than being retro-interpreted.
DEFAULT_CHANNELS = (CHANNEL_IN_APP,)

# ---------------------------------------------------------------------------
# Priority. Three levels, deliberately not five: the only decision the app
# makes with this is how loud the row looks, and a scale finer than
# "informational / normal / act on this" cannot be applied consistently by the
# people writing event sites.
# ---------------------------------------------------------------------------
PRIORITY_LOW = "LOW"
PRIORITY_NORMAL = "NORMAL"
PRIORITY_HIGH = "HIGH"
PRIORITIES = (PRIORITY_LOW, PRIORITY_NORMAL, PRIORITY_HIGH)

# ---------------------------------------------------------------------------
# Entity types — what a notification can point AT.
#
# These are the nouns the app can already open. Adding one means adding a
# route to open it, which is why the set is closed: a notification pointing at
# an entity with no destination is a dead tap, and the app cannot tell that
# apart from a bug.
# ---------------------------------------------------------------------------
ENTITY_TASK = "task"
ENTITY_MEETING = "meeting"
ENTITY_DOCUMENT = "document"
ENTITY_TYPES = (ENTITY_TASK, ENTITY_MEETING, ENTITY_DOCUMENT)

# ---------------------------------------------------------------------------
# Notification types.
#
# The registry is DATA, not code: each entry fixes the type's priority, the
# entity it points at, and how its title/message read. A new type is one entry
# here — the API, the store, the app's rendering and the deep-link resolver
# all read this rather than switching on the type name.
#
# `message` is a format string over the facts the event site supplies. The
# facts are always ENTITY TITLES the user already wrote or the AI already
# generated and stored — never anything derived or invented here.
# ---------------------------------------------------------------------------
TYPE_MEETING_PROCESSING_COMPLETED = "MEETING_PROCESSING_COMPLETED"
TYPE_MEETING_PROCESSING_FAILED = "MEETING_PROCESSING_FAILED"
TYPE_AI_OUTPUT_READY = "AI_OUTPUT_READY"
TYPE_AI_ACTION_REQUIRED = "AI_ACTION_REQUIRED"
TYPE_TASK_ASSIGNED = "TASK_ASSIGNED"
TYPE_TASK_REASSIGNED = "TASK_REASSIGNED"
TYPE_TASK_DUE_TODAY = "TASK_DUE_TODAY"
TYPE_TASK_OVERDUE = "TASK_OVERDUE"
TYPE_MEETING_DOCUMENT_READY = "MEETING_DOCUMENT_READY"
TYPE_MEETING_OUTPUT_SHARED = "MEETING_OUTPUT_SHARED"

# The fallback shown when the fact a message needs is genuinely absent.
#
# It is a LABEL, not a guess: an untitled meeting is a real state (a recording
# whose AI title never generated), and writing "Untitled meeting" is honest
# where inventing a plausible title would not be. Kept in one place so no
# builder invents its own.
UNTITLED_MEETING = "Untitled meeting"
UNTITLED_TASK = "Untitled task"

TYPES = {
    TYPE_MEETING_PROCESSING_COMPLETED: {
        "priority": PRIORITY_NORMAL,
        "entity_type": ENTITY_MEETING,
        "title": "Meeting processing completed",
        "message": "{subject} is ready",
    },
    TYPE_MEETING_PROCESSING_FAILED: {
        # HIGH because it is the only notification that asks the user to do
        # something about a thing that already went wrong — a completed
        # meeting can wait, a lost one cannot.
        "priority": PRIORITY_HIGH,
        "entity_type": ENTITY_MEETING,
        "title": "Meeting processing failed",
        "message": "{subject} could not be processed",
    },
    TYPE_AI_OUTPUT_READY: {
        "priority": PRIORITY_NORMAL,
        "entity_type": ENTITY_MEETING,
        "title": "AI output ready",
        "message": "{subject} — summary and highlights are ready",
    },
    TYPE_AI_ACTION_REQUIRED: {
        "priority": PRIORITY_HIGH,
        # Points at the TASK, not the meeting: the review the user has to do
        # is on one task, and landing them on the meeting would make them find
        # it themselves.
        "entity_type": ENTITY_TASK,
        "title": "AI needs your confirmation",
        "message": "{subject} requires your review",
    },
    TYPE_TASK_ASSIGNED: {
        "priority": PRIORITY_HIGH,
        "entity_type": ENTITY_TASK,
        "title": "New task assigned to you",
        "message": "{subject}",
    },
    TYPE_TASK_REASSIGNED: {
        "priority": PRIORITY_HIGH,
        "entity_type": ENTITY_TASK,
        # Reads from the DEPARTING assignee's point of view, because that is
        # the only person who gets this type — the new assignee gets
        # TASK_ASSIGNED. One type, one audience, one wording.
        "title": "Task reassigned",
        "message": "{subject} is no longer assigned to you",
    },
    TYPE_TASK_DUE_TODAY: {
        "priority": PRIORITY_HIGH,
        "entity_type": ENTITY_TASK,
        "title": "Task due today",
        "message": "{subject}",
    },
    TYPE_TASK_OVERDUE: {
        "priority": PRIORITY_HIGH,
        "entity_type": ENTITY_TASK,
        "title": "Task overdue",
        "message": "{subject}",
    },
    TYPE_MEETING_DOCUMENT_READY: {
        "priority": PRIORITY_NORMAL,
        # Points at the MEETING even though it is about a document: documents
        # are not independently addressable in the app — they are read inside
        # the meeting workspace. entity_id carries the meeting key and the
        # document type travels in metadata, so the app opens the right
        # meeting and can scroll to the right document.
        "entity_type": ENTITY_MEETING,
        "title": "Meeting document ready",
        "message": "{subject}",
    },
    TYPE_MEETING_OUTPUT_SHARED: {
        "priority": PRIORITY_NORMAL,
        "entity_type": ENTITY_MEETING,
        "title": "Meeting output shared",
        "message": "{subject} was shared",
    },
}

# Every type the system will accept on a write. A type outside this set is a
# programming error, not user input, so the store raises rather than coercing.
KNOWN_TYPES = frozenset(TYPES)

# ---------------------------------------------------------------------------
# Field ceilings. A notification is a POINTER plus a label — these bounds are
# what keep it that way, so no event site can turn one into a document by
# passing a transcript as the message.
# ---------------------------------------------------------------------------
MAX_TITLE = 120
MAX_MESSAGE = 240
MAX_ENTITY_ID = 1024   # a recording key is a full S3 key, so this is generous
MAX_METADATA_VALUE = 200
MAX_METADATA_KEYS = 10


def priority_of(notification_type):
    """The declared priority for a type, or NORMAL for an unknown one.

    Unknown types cannot be WRITTEN (the store rejects them), but they can be
    READ: a row written by a newer deploy must still render on an older one
    rather than crashing the list. Defaulting to NORMAL is the honest reading
    of "we do not know how loud this is".
    """
    return (TYPES.get(notification_type) or {}).get("priority", PRIORITY_NORMAL)


def entity_type_of(notification_type):
    """The entity kind a type points at, or "" when the type is unknown."""
    return (TYPES.get(notification_type) or {}).get("entity_type", "")


def _clip(value, limit):
    return str(value or "").strip()[:limit]


def _clean_metadata(raw):
    """Small, flat, string-valued display extras — nothing more.

    Metadata exists so the app can render a row and open the right place
    (which document type, which meeting a task came from) WITHOUT a second
    fetch. It is explicitly not a payload: values are stringified and clipped,
    the key count is bounded, and nested structures are dropped rather than
    stored. That bound is what stops "just put the object in metadata"
    becoming the way business data gets duplicated into notifications.
    """
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key in sorted(raw):
        if len(out) >= MAX_METADATA_KEYS:
            break
        value = raw[key]
        if value is None or isinstance(value, (dict, list, tuple, set)):
            continue
        name = _clip(key, 40)
        if not name:
            continue
        out[name] = _clip(value, MAX_METADATA_VALUE)
    return out


def build(notification_type, *, subject="", metadata=None, title="",
          message=""):
    """The rendered (title, message, priority, entity_type) for one event.

    `subject` is the entity's own label — a task title, a meeting title. It is
    the ONLY variable the copy table interpolates, which is what keeps every
    notification's wording reviewable in one place.

    `title`/`message` overrides exist for the one case the table cannot cover:
    a type whose message depends on something other than a single subject (the
    document notification names both the meeting and the document). They are
    clipped like everything else, and a caller that passes neither still gets
    the registered copy rather than an empty row.

    Raises ValueError for an unregistered type — an event site naming a type
    that does not exist is a bug to surface at the write, not a row that
    renders blank on a handset.
    """
    spec = TYPES.get(notification_type)
    if spec is None:
        raise ValueError(f"unknown notification type: {notification_type}")

    rendered_title = _clip(title, MAX_TITLE) or spec["title"]
    if message:
        rendered_message = _clip(message, MAX_MESSAGE)
    else:
        # The subject is already clipped before interpolation so the ceiling
        # applies to the SUBJECT rather than to the sentence around it —
        # otherwise a long title would eat the words that give it meaning.
        rendered_message = _clip(
            spec["message"].format(subject=_clip(subject, MAX_MESSAGE)),
            MAX_MESSAGE)

    return {
        "type": notification_type,
        "title": rendered_title,
        "message": rendered_message,
        "priority": spec["priority"],
        "entity_type": spec["entity_type"],
        "metadata": _clean_metadata(metadata),
    }


# ---------------------------------------------------------------------------
# Idempotency.
#
# THE PROBLEM THIS SOLVES. Almost every event site here can fire more than
# once for the same real-world fact:
#
#   * the deadline sweep runs on every task list read, so "due today" would be
#     raised again on every app open;
#   * a reprocess re-runs the whole pipeline, so "processing completed" would
#     be raised again;
#   * ElevenLabs retries a webhook it thinks failed;
#   * two devices open the same meeting at once and both seed AI tasks.
#
# So the notification's identity must come from the FACT, not from the moment
# of writing. `dedupe_key` builds that identity, and the store writes with a
# condition on it (attribute_not_exists) — the second attempt loses the race
# and is dropped, rather than being deduplicated after the fact by a reader.
#
# WHY THE DAY IS PART OF THE DEADLINE KEYS. "Due today" is a fact about a
# task AND a date: the same task legitimately deserves a fresh notification on
# a later day if its deadline moves. Including the day makes one-per-day fall
# out of the same mechanism as one-ever, with no separate sweep state to keep.
# Overdue uses the day too, so a task left overdue for a week produces one row
# per day rather than one forever (which would scroll away and be missed) or
# one per app open (which is the noise this whole section exists to prevent).
# ---------------------------------------------------------------------------
def dedupe_key(user_id, notification_type, entity_id, day=""):
    """Deterministic identity for "this notification, for this user, once".

    Same inputs -> same key, on every Lambda, on every retry. `day` is the
    ISO date (YYYY-MM-DD) for types whose fact is per-day; omitted for facts
    that are true once ever, like a task being assigned.
    """
    parts = [str(user_id or ""), str(notification_type or ""),
             str(entity_id or "")]
    if day:
        parts.append(str(day))
    return "\x00".join(parts)


def today_iso(now=None):
    """The current UTC calendar day as YYYY-MM-DD.

    UTC rather than the user's local day, deliberately and with a known
    limitation: MinuteX stores no per-user timezone (there is no field for it
    on the Users table), so a local day is not derivable without inventing
    one. UTC is the same boundary `_is_overdue` already compares against, so
    the deadline notifications agree with the overdue badge the user sees on
    the task itself. A per-user timezone is the right fix and is a product
    decision, not something to guess here.
    """
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def public_notification(row):
    """The client-facing shape of one stored notification.

    An ASSEMBLED dict, like integrations.public_status() and
    share_schema.public_payload(): it can only emit what it explicitly lists,
    so a new internal attribute (a delivery cursor, an idempotency key) cannot
    leak to the client by being forgotten in a deny-list. `dedupe_key` in
    particular contains the owner's user_id and must never be returned.
    """
    ntype = str(row.get("type") or "")
    return {
        "notification_id": str(row.get("notification_id") or ""),
        "type": ntype,
        "title": str(row.get("title") or ""),
        "message": str(row.get("message") or ""),
        # Read from the ROW, not re-derived from the type: a row written by a
        # deploy whose copy table differed must render as it was written.
        "priority": str(row.get("priority") or priority_of(ntype)),
        "entity_type": str(row.get("entity_type") or entity_type_of(ntype)),
        "entity_id": str(row.get("entity_id") or ""),
        "is_read": bool(row.get("is_read")),
        "read_at": str(row.get("read_at") or ""),
        "created_at": str(row.get("created_at") or ""),
        "metadata": row.get("metadata") if isinstance(
            row.get("metadata"), dict) else {},
        "channels": list(row.get("channels") or [CHANNEL_IN_APP]),
    }


# ---------------------------------------------------------------------------
# The WRITE path.
#
# WHY IT IS HERE RATHER THAN IN ONE LAMBDA. Two different processes raise
# notifications — userApi (task assignment, AI review, sharing) and
# transcribeRecording (processing completed/failed). Both are packaged with
# this module flat at the archive root, so one implementation here is what
# makes "a notification" mean the same thing on both sides. A copy in each
# would drift on the first change to the dedupe rule, and the two halves
# would then disagree about what counts as a duplicate.
#
# The tables are passed IN rather than built here: each Lambda already owns
# its boto3 resource, its region and its table-name environment variables, and
# this module deliberately has no AWS import (which is also what lets the
# offline tests exercise it against fake tables).
# ---------------------------------------------------------------------------
def make_row(user_id, built, entity_id, dedupe, notification_id, now):
    """Assemble one Notifications item. Pure — no clock, no uuid, no table."""
    return {
        "notification_id": notification_id,
        "user_id": user_id,
        "type": built["type"],
        "title": built["title"],
        "message": built["message"],
        "priority": built["priority"],
        "entity_type": built["entity_type"],
        "entity_id": str(entity_id or "")[:MAX_ENTITY_ID],
        "is_read": False,
        # The sparse-index marker: present ONLY while unread, so the unread
        # index holds exactly the unread rows. Its value is created_at so the
        # index is ordered too. See scripts/41 for the full reasoning.
        "unread_marker": now,
        "read_at": "",
        "created_at": now,
        "metadata": built["metadata"],
        "channels": list(DEFAULT_CHANNELS),
        "dedupe_key": dedupe,
    }
