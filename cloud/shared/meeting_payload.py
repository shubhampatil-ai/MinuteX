"""meeting_payload.py — THE generic MinuteX meeting data contract.

WHAT THIS IS. One pure function, build_meeting_payload(), that turns a
MinuteX meeting into a complete, standardized, versioned JSON document
describing everything MinuteX knows about that meeting — and nothing about
where any of it is going to end up.

WHY IT EXISTS. The original Salesforce integration made MinuteX responsible
for the CUSTOMER'S schema: an org configured which SObject and which four
long-text fields received the transcript/summary/highlights/action items,
and MinuteX stored that configuration (`config.mappings`), validated it
against a live Describe, and built its push payload out of it. That put a
customer's CRM design decisions inside MinuteX's database and made every new
customer a configuration exercise.

This module is the inversion. MinuteX emits ONE shape, the same for every
customer, and the customer decides in Salesforce (Flow, Apex, reports, their
own objects) what any of it means to their business. MinuteX owns the data
contract; the customer owns the business logic.

THEREFORE: NO SALESFORCE VOCABULARY BELOW THIS LINE. There are deliberately
no SObject names, no field API names, no `__c` suffixes and no CRM concepts
anywhere in this file. It does not import a Salesforce client and does not
know one exists. That is not incidental tidiness — it is the property that
stops customer-specific mapping creeping back in, and it is why this module
can be unit-tested with a plain dict and no AWS, no network and no mocking.
The Salesforce-shaped translation happens exactly once, at the delivery
boundary in the Lambda, against the stable shape defined here.

ASSEMBLED, NEVER FILTERED — the share_schema.public_payload rule.
This function builds its output key by key from named inputs. It never
copies the recording row and deletes the sensitive parts. That distinction
is load-bearing: the Recordings row carries `user_id`, `device_id`,
`audio_s3_key`, `transcript_s3_key`, `chat_history`, `crm_records` and
whatever attribute someone adds next quarter, and a deny-list would leak the
first attribute a future author forgets to add to it. An allow-list leaks
nothing by construction. share_schema.public_payload and
integrations.public_status are built the same way for the same reason.

WHAT IS DELIBERATELY NOT IN THE PAYLOAD:
  * S3 keys and presigned URLs of any kind. A presign is a bearer credential
    with an expiry; writing one into a customer's CRM produces either a dead
    link or a leaked one. Audio is delivered as a file or not at all.
  * MinuteX internal user ids as the PRIMARY identity. Ids are included as
    correlation hints (a customer may have stored them), but email/name are
    what a customer's automation can actually match on.
  * Anything derived from a customer's Salesforce schema.

VERSIONING. `payload_version` is the contract's version and the customer's
compatibility signal: their Flow/Apex reads it and can refuse or adapt.
Bump the MINOR for a purely additive change (a new key, which existing
consumers ignore); bump the MAJOR only for a removal or a shape change, and
expect that to be a coordinated customer-facing migration. Everything this
module emits is either always present or always absent-as-empty — a consumer
never has to distinguish "missing key" from "no data", which is what makes
additive changes safe.
"""

PAYLOAD_VERSION = "1.0"

# Bounds. These exist because a payload is delivered into a system with its
# own field limits, and because an unbounded transcript in a JSON body is a
# denial-of-service against our own worker as much as against the customer's
# org. The transcript is the only field big enough to matter in practice;
# when it exceeds this, the delivery layer sends it as a file instead and
# `transcript.truncated` says so. Everything else is already bounded upstream
# by ai_schema/mom_schema's own caps.
MAX_INLINE_TRANSCRIPT_CHARS = 30_000
MAX_SUMMARY_CHARS = 32_000
MAX_TITLE_CHARS = 255


def _s(value, limit=None):
    """A clean string, never None. `limit` truncates rather than raising:
    a slightly short title is always better than a failed delivery."""
    out = "" if value is None else str(value).strip()
    if limit and len(out) > limit:
        out = out[:limit].rstrip()
    return out


def _num(value, default=0):
    """A number from DynamoDB's Decimal, a string, or None — without
    propagating Decimal into the JSON body (json.dumps cannot serialize it,
    and a delivery failing at serialize time is a failure this cheap
    conversion removes entirely)."""
    if value is None or value == "":
        return default
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return int(f) if f.is_integer() else f


def _iso_from_recorded_at(raw):
    """recorded_at -> an ISO 8601 UTC string, or "".

    The upload path writes recorded_at as a Unix epoch STRING; older rows
    carry an ISO timestamp. Both shapes are live in production (the same dual
    format spoken_dates and the Event push already handle), so both are
    accepted here rather than assuming one and emitting a wrong timestamp for
    every row in the other shape.
    """
    from datetime import datetime, timezone

    raw = _s(raw)
    if not raw:
        return ""
    if raw.isdigit():
        try:
            return datetime.fromtimestamp(
                int(raw), tz=timezone.utc).isoformat().replace("+00:00", "Z")
        except (ValueError, OSError, OverflowError):
            return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat().replace("+00:00", "Z")


def _end_iso(start_iso, duration_seconds):
    """The meeting's end, derived from start + duration. Emitted as its own
    field because a consumer building a calendar entry needs both ends of the
    interval, and making every customer redo this arithmetic in Flow — where
    date handling is genuinely awkward — is exactly the kind of work this
    contract should absorb once."""
    from datetime import datetime, timedelta

    if not start_iso or not duration_seconds:
        return ""
    try:
        start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return (start + timedelta(seconds=float(duration_seconds))) \
        .isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Section builders. One per top-level payload key, each taking exactly what it
# needs. Split out so each is independently testable and so the top-level
# function reads as the contract itself.
# ---------------------------------------------------------------------------

def _overview(item):
    """The Dynamic Overview: AI-chosen sections, as structure AND as text.

    BOTH representations are emitted on purpose. The structured `sections`
    are what a customer should build on (each section titled and typed, so a
    Flow can pick out "Risks" without string-matching prose), but a great
    many real consumers just want something to drop in a rich-text field, and
    making every one of them write a JSON-flattening loop in Apex to get it
    would be a poor contract. The text form is derived from the same
    structure, so the two can never disagree.
    """
    raw = item.get("overview")
    if not isinstance(raw, dict):
        return {"sections": [], "text": ""}

    sections = []
    for section in raw.get("sections") or []:
        if not isinstance(section, dict):
            continue
        title = _s(section.get("title"))
        if not title:
            continue
        sections.append({
            "id": _s(section.get("id")),
            "title": title,
            "kind": _s(section.get("kind")) or "text",
            "content": _s(section.get("content")),
            "items": [_s(i) for i in (section.get("items") or []) if _s(i)],
        })

    # Flatten here rather than importing ai_schema.overview_text: this module
    # is deliberately dependency-free so it can be tested and reasoned about
    # in isolation, and the flattening is six lines. The FORMAT matches
    # ai_schema.overview_text's so a consumer sees the same text MinuteX's own
    # prompts and `summary` derivation use.
    parts = []
    for section in sections:
        block = [f"{section['title']}:"]
        if section["content"]:
            block.append(section["content"])
        block.extend(f"- {i}" for i in section["items"])
        parts.append("\n".join(block))
    return {"sections": sections, "text": "\n\n".join(parts)}


def _highlights_and_decisions(item):
    """(highlights, decisions) from whichever shape this row carries.

    DECISIONS ARE PROMOTED TO A TOP-LEVEL PAYLOAD KEY even though MinuteX
    stores them inside meeting_highlights. They are the single most
    business-relevant output of a meeting and the thing a customer is most
    likely to route somewhere specific, and burying them one level down
    inside a "highlights" grab-bag would make the contract worse for no
    storage reason. Same data, better contract.

    Three source shapes are read, newest first, because this is a DELIVERY
    path and the alternative to reading a legacy shape is silently sending an
    empty highlights block for every meeting recorded before the current
    schema — exactly the class of silent-empty bug the transcript hydration
    fix exists to remove.
    """
    structured = item.get("meeting_highlights")
    if isinstance(structured, dict) and structured:
        decisions = []
        for row in structured.get("decisions") or []:
            if isinstance(row, dict):
                text = _s(row.get("decision"))
                if not text:
                    continue
                decisions.append({
                    "decision": text,
                    "context": _s(row.get("context")),
                    "speaker_id": _s(row.get("speaker_id")),
                })
            elif _s(row):
                decisions.append({"decision": _s(row), "context": "",
                                  "speaker_id": ""})

        deadlines = []
        for row in structured.get("deadlines") or []:
            if isinstance(row, dict):
                what = _s(row.get("what"))
                if what:
                    deadlines.append({"what": what, "when": _s(row.get("when"))})
            elif _s(row):
                deadlines.append({"what": _s(row), "when": ""})

        questions = [_s(q) for q in (structured.get("open_questions") or [])
                     if _s(q)]
        return ({"deadlines": deadlines, "open_questions": questions,
                 "items": []}, decisions)

    # Legacy flat `highlights` list — no structure to recover, so it is
    # emitted as plain items and decisions stay empty rather than being
    # guessed at from free text.
    flat = [_s(h) for h in (item.get("highlights") or []) if _s(h)]
    return ({"deadlines": [], "open_questions": [], "items": flat}, [])


def _participants(item, identity=None):
    """Everyone MinuteX believes was in this meeting, with identity HINTS.

    THE HINT PRINCIPLE (and the behaviour change it encodes). The previous
    architecture treated identity as a GATE: an organisation meeting with any
    tagged-but-unresolved speaker was refused with a 409 and nothing was
    delivered at all. That made MinuteX's incomplete knowledge of the
    customer's Salesforce user list a reason to withhold the meeting itself.

    Here identity is DATA. Every known correlation handle is emitted —
    MinuteX ids, name, email, role, and any Salesforce id MinuteX already
    happens to have confirmed — each independently optional, and the
    customer's automation resolves whatever it wants however it wants. An
    unresolvable participant costs that participant's hints, never the
    meeting.

    `salesforce_*` keys are the one place this module names the destination,
    and they are deliberately NOT a mapping: they are ids MinuteX already
    stored from the customer's own explicit search-and-confirm actions. They
    are passed through as correlation hints, never used to decide a schema.
    """
    by_speaker = {}
    for row in (identity or {}).get("speakers") or []:
        if isinstance(row, dict) and _s(row.get("speaker_id")):
            by_speaker[_s(row["speaker_id"])] = row

    speaker_names = item.get("speaker_names")
    speaker_names = speaker_names if isinstance(speaker_names, dict) else {}

    # Union of every speaker MinuteX knows about, from either source, so a
    # participant tagged in the identity system but absent from the AI's
    # `participants` list (or vice versa) is still delivered.
    ordered_ids = []
    for entry in item.get("participants") or []:
        if isinstance(entry, dict) and _s(entry.get("speaker")):
            ordered_ids.append(_s(entry["speaker"]))
    for sid in list(speaker_names) + list(by_speaker):
        if sid not in ordered_ids:
            ordered_ids.append(sid)

    blurbs = {}
    for entry in item.get("participants") or []:
        if isinstance(entry, dict) and _s(entry.get("speaker")):
            blurbs[_s(entry["speaker"])] = _s(entry.get("summary"))

    out = []
    for speaker_id in ordered_ids:
        ident = by_speaker.get(speaker_id) or {}
        out.append({
            "speaker_id": speaker_id,
            "display_name": _s(speaker_names.get(speaker_id))
            or _s(ident.get("contact_name")),
            "contribution_summary": blurbs.get(speaker_id, ""),
            # Internal / External as the CUSTOMER'S OWN organisation tagged
            # them — MinuteX's word for it, not a Salesforce concept.
            "identity_role": _s(ident.get("identity_role")),
            "resolution_status": _s(ident.get("status")),
            "minutex_contact_id": _s(ident.get("contact_id")),
            "minutex_user_id": _s(ident.get("minutex_user_id")),
            "email": _s(ident.get("email")),
            # Already-confirmed Salesforce ids, when MinuteX has them. Empty
            # is normal and never blocks delivery.
            "salesforce_user_id": _s(ident.get("sf_user_id")),
            "salesforce_contact_id": _s(ident.get("sf_contact_id")),
            "salesforce_account_id": _s(ident.get("sf_account_id")),
        })
    return out


def _action_items(tasks):
    """MinuteX action items, as data — NOT as Salesforce Tasks.

    The previous integration created a Salesforce Task per action item with a
    hardcoded field map (Subject/Description/ActivityDate/OwnerId|WhoId).
    That is precisely a customer-specific decision: one customer wants
    Tasks, another wants a Custom_Action_Item__c, a third wants them rolled
    into an Opportunity. So this emits the action item and its assignee
    hints, and the customer's Flow decides what object it becomes.

    `tasks` is the authoritative Tasks-table rows for this meeting, passed
    in by the caller — this module performs no I/O.
    """
    out = []
    for row in tasks or []:
        if not isinstance(row, dict):
            continue
        title = _s(row.get("title"))
        if not title:
            continue
        out.append({
            "minutex_task_id": _s(row.get("task_id")),
            "title": title,
            "description": _s(row.get("description")),
            "status": _s(row.get("status")),
            "priority": _s(row.get("priority")),
            # Both forms: `due_date` as the user/AI expressed it, and the
            # normalized calendar date a consumer can actually put in a date
            # field without parsing natural language.
            "due_date": _s(row.get("due_date")),
            "due_date_normalized": _s(row.get("due_date_normalized")),
            "assignee": {
                "name": _s(row.get("assignee_name")),
                "email": _s(row.get("assignee_email")),
                "speaker_id": _s(row.get("assignee_speaker_id")),
                "minutex_contact_id": _s(row.get("assignee_contact_id")),
                "minutex_user_id": _s(row.get("assignee_user_id")),
                "resolution_status": _s(row.get("resolution_status")),
            },
            "source": _s(row.get("source_type")),
            "created_at": _s(row.get("created_at")),
        })
    return out


def _transcript(item):
    """The transcript block, with an explicit truncation signal.

    `available` is the honest answer to "does this meeting have a transcript
    at all", which is NOT the same question as "is there text in this
    payload" once truncation exists. A consumer that sees available=True and
    truncated=True knows to look for the attached file rather than concluding
    the meeting had no transcript — the exact ambiguity that made the
    hydration bug invisible for so long.

    This function does NOT fetch from S3. The caller is responsible for
    handing in an already-hydrated item (transcript_store.hydrate); keeping
    the I/O out here is what makes this module pure and testable.
    """
    text = _s(item.get("transcript"))
    segments = []
    for seg in item.get("timestamps") or []:
        if not isinstance(seg, dict):
            continue
        seg_text = _s(seg.get("text"))
        if not seg_text:
            continue
        segments.append({
            "id": _s(seg.get("id")),
            "speaker_id": _s(seg.get("speaker")),
            "start": _num(seg.get("start")),
            "end": _num(seg.get("end")),
            "text": seg_text,
        })

    full_chars = len(text)
    truncated = full_chars > MAX_INLINE_TRANSCRIPT_CHARS
    return {
        "available": bool(text) or bool(item.get("transcript_s3_key")),
        "text": text[:MAX_INLINE_TRANSCRIPT_CHARS] if truncated else text,
        "truncated": truncated,
        "total_chars": full_chars,
        "language": _s(item.get("language")),
        "segments": segments,
        "segment_count": len(segments),
    }


def build_meeting_payload(item, tasks=None, identity=None, *,
                          meeting_url="", workspace_id="",
                          include_transcript=True):
    """THE generic meeting payload. Pure: no AWS, no network, no Salesforce.

    Args:
      item: the Recordings row, ALREADY HYDRATED by transcript_store.hydrate
            when a transcript is wanted. This function cannot fetch it.
      tasks: the Tasks-table rows for this meeting (authoritative action
            items). Falls back to the row's embedded `tasks` map when not
            supplied, so a caller without table access still gets action
            items rather than silently none.
      identity: _meeting_crm_identity_state()'s output, used purely as a
            source of per-speaker HINTS. Optional — its absence costs
            identity hints, never the meeting.
      meeting_url: a STABLE MinuteX deep link. Never a presigned URL; see the
            module docstring.
      include_transcript: False delivers everything except transcript text
            (the per-organisation privacy control — a customer may want
            meeting outcomes in their CRM without the verbatim record).

    Returns a plain JSON-serializable dict. Every key is always present.
    """
    item = item if isinstance(item, dict) else {}

    start_iso = _iso_from_recorded_at(item.get("recorded_at"))
    duration = _num(item.get("duration_seconds") or item.get("duration"))
    highlights, decisions = _highlights_and_decisions(item)

    # Prefer the authoritative Tasks rows; fall back to the row's mirrored
    # map so a legacy/embedded-only meeting still delivers its action items.
    if tasks is None:
        embedded = item.get("tasks")
        tasks = list(embedded.values()) if isinstance(embedded, dict) else []

    payload = {
        "payload_version": PAYLOAD_VERSION,
        "source": "minutex",

        "meeting": {
            # THE stable identity of this meeting across every system, and
            # the upsert key at the delivery boundary. This is the Recordings
            # table's own primary key, so it is unique, immutable and already
            # the id every other MinuteX subsystem uses.
            "minutex_meeting_id": _s(item.get("audio_s3_key")),
            "title": _s(item.get("title"), MAX_TITLE_CHARS),
            "status": _s(item.get("status")),
            "source": _s(item.get("source")),
            "language": _s(item.get("language")),
            "started_at": start_iso,
            "ended_at": _end_iso(start_iso, duration),
            "duration_seconds": duration,
            "created_at": _s(item.get("created_at")),
            "minutex_workspace_id": _s(workspace_id or item.get("workspace_id")),
            # A stable, permission-checked MinuteX link. Empty when the
            # deployment has no public base URL configured — never a
            # presigned S3 URL as a substitute.
            "minutex_url": _s(meeting_url),
        },

        "summary": _s(item.get("summary"), MAX_SUMMARY_CHARS),
        "overview": _overview(item),
        "highlights": highlights,
        "decisions": decisions,
        "participants": _participants(item, identity),
        "action_items": _action_items(tasks),
        "transcript": _transcript(item) if include_transcript else {
            "available": bool(item.get("transcript")
                              or item.get("transcript_s3_key")),
            "text": "", "truncated": False, "total_chars": 0,
            "language": _s(item.get("language")),
            "segments": [], "segment_count": 0,
            # The one case where empty text is a POLICY decision rather than
            # an absence of data, said out loud so a customer investigating
            # "where is my transcript" gets an answer from the payload itself.
            "excluded_by_policy": True,
        },
    }
    if include_transcript:
        payload["transcript"]["excluded_by_policy"] = False
    return payload


def payload_counts(payload):
    """Small structural summary of a built payload — for logging and for the
    delivery layer's own bookkeeping, so neither has to re-walk the document
    or (worse) log the meeting's contents to count them."""
    payload = payload if isinstance(payload, dict) else {}
    transcript = payload.get("transcript") or {}
    return {
        "participants": len(payload.get("participants") or []),
        "action_items": len(payload.get("action_items") or []),
        "decisions": len(payload.get("decisions") or []),
        "overview_sections": len((payload.get("overview") or {})
                                 .get("sections") or []),
        "transcript_chars": int(transcript.get("total_chars") or 0),
        "transcript_truncated": bool(transcript.get("truncated")),
    }
