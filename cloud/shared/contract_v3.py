"""contract_v3.py — MinuteX Salesforce Contract v3.0 (the sender side).

WHAT THIS IS. The request shapes MinuteX sends to the MinuteX managed
package: one meeting LINKED to up to five Salesforce records, with only the
outputs the user and the customer's admin allowed. Pure: no AWS, no
network, no clock except where passed in. Nothing calls it yet; it is built
beside Contract v2 (webhook_contract) so v2 stays unchanged.

WHAT CHANGES FROM v2.
  * `links` (1-5 records, exactly one primary) replace v2's single
    `meeting_type` + `record_context`. A link is an object API name and a
    record id: the sender never needs to know a customer's objects.
  * `included_outputs` names what is sent. An output that is not included is
    ABSENT from `data`, never sent empty, so the receiver can clear what a
    user unticked and nothing excluded ever leaves MinuteX.
  * Every action item carries `related`: null follows the primary link, or
    one record the user chose for that task.
  * `meeting.recorded_by` {name, email} names who recorded the meeting.
  * The transcript travels as TEXT in `data.transcript.parts`: up to
    TRANSCRIPT_FIELDS pieces of at most 131,072 characters, split at line
    ends, for the MinuteX Meeting's Transcript fields (part 1 to 4). Past
    them it is cut with a note — never refused, never a file.
    `data.transcript.text` is part 1, for receivers older than the parts. The MoM and the
    generated documents travel as PDF FILES (POST /files, before the event).

REUSED, NOT RE-IMPLEMENTED. The snapshot starts from v2's build_snapshot (the
same participant/summary/action-item shaping and opaque meeting id), and the
event id is v2's derivation: a sequence is bound to one content state per
meeting whichever contract carries it.
"""
import base64
import hashlib
import re

import site_contract
import webhook_contract as wc

CONTRACT_VERSION = "3.0"

EVENT_CREATED = wc.EVENT_CREATED
EVENT_UPDATED = wc.EVENT_UPDATED
EVENT_DELETED = wc.EVENT_DELETED
EVENTS = wc.EVENTS

TYPE_EVENT = wc.TYPE_EVENT
TYPE_FILE = "file.upload"
PATH_EVENTS = wc.PATH_EVENTS
PATH_FILES = "/files"

LIFECYCLE_ACTIVE = wc.LIFECYCLE_ACTIVE
LIFECYCLE_TRASHED = wc.LIFECYCLE_TRASHED

# THE limit on records per meeting. Change this one number (for example
# to 1 for "one record per meeting") and everything follows: the backend
# refuses more, and the app reads it from the link configuration
# (link_config_block) instead of keeping its own copy.
MAX_LINKS = 5

# The outputs a meeting can carry, in their canonical order. The meeting's
# identity (title, date, duration, language, recorded by) and its links are
# not outputs: they always travel.
OUTPUT_OVERVIEW = "overview"                      # overview, decisions, deadlines, open questions
OUTPUT_MOM = "mom"                                # the MoM PDF File only, never text
OUTPUT_TASKS = "tasks"
OUTPUT_PARTICIPANTS = "participants"
OUTPUT_CONTRIBUTIONS = "participant_contributions"  # what each person said
OUTPUT_TRANSCRIPT = "transcript"                  # text, in the meeting's Transcript field
OUTPUT_RECORDING = "recording"                    # the audio link
OUTPUT_DOCUMENTS = "documents"                    # each generated document, a PDF File only
OUTPUTS = (OUTPUT_OVERVIEW, OUTPUT_MOM, OUTPUT_TASKS, OUTPUT_PARTICIPANTS,
           OUTPUT_CONTRIBUTIONS, OUTPUT_TRANSCRIPT, OUTPUT_RECORDING, OUTPUT_DOCUMENTS)

FILE_MOM_PDF = "mom_pdf"
# One per generated document; `name` is the document's type
# (executive_summary, follow_up_email, custom_<id>…). The MoM's own mirror
# (minutes_of_meeting) is never one: the MoM goes as FILE_MOM_PDF.
FILE_DOCUMENT = "document"
MOM_DOCUMENT_TYPE = "minutes_of_meeting"
DOCUMENT_NAME_RE = re.compile(r"^[a-z0-9_]{1,64}$")
# Which output a file belongs to: a file is sent only with its output.
FILE_OUTPUT = {FILE_MOM_PDF: OUTPUT_MOM, FILE_DOCUMENT: OUTPUT_DOCUMENTS}
FILE_CONTENT_TYPES = {FILE_MOM_PDF: "application/pdf", FILE_DOCUMENT: "application/pdf"}

# The MinuteX Meeting's Transcript fields (Transcript__c, Transcript_2__c …
# Transcript_4__c): Salesforce Long Text Areas, whose hard maximum is
# 131,072 characters each. Four hold about 8–10 hours of meeting.
MAX_TRANSCRIPT_CHARS = 131072
TRANSCRIPT_FIELDS = 4
TRANSCRIPT_CUT_NOTE = ("\n\n[The transcript continues in MinuteX: Salesforce holds "
                       "the first 524,288 characters.]")


def _cut_at_line(text, room):
    """The longest start of `text` that fits `room` (as Salesforce counts
    characters), ending at a line end when one is near."""
    cut = text[:room]
    while site_contract.text_length(cut) > room:   # characters Salesforce counts twice
        cut = cut[:-64]
    if len(cut) < len(text) and "\n" in cut[-2000:]:
        cut = cut[:cut.rindex("\n")]
    return cut


def split_transcript(text):
    """(parts, truncated): the transcript in at most TRANSCRIPT_FIELDS
    pieces that each fit one Transcript field, split at line ends. Past the
    last field it is cut, and the last piece ends with TRANSCRIPT_CUT_NOTE."""
    rest = _s(text)
    parts = []
    while rest:
        last = len(parts) == TRANSCRIPT_FIELDS - 1
        if site_contract.text_length(rest) <= MAX_TRANSCRIPT_CHARS:
            parts.append(rest)
            return parts, False
        if last:
            room = MAX_TRANSCRIPT_CHARS - site_contract.text_length(TRANSCRIPT_CUT_NOTE)
            parts.append(_cut_at_line(rest, room).rstrip() + TRANSCRIPT_CUT_NOTE)
            return parts, True
        piece = _cut_at_line(rest, MAX_TRANSCRIPT_CHARS)
        parts.append(piece.rstrip())
        rest = rest[len(piece):].lstrip("\n")
    return parts, False
# Decoded size cap for one file. The receiver holds the request body, its
# base64 text and the decoded file at once inside Apex's 6 MB synchronous
# heap, so 2 MB is the safe ceiling. A MoM PDF or a transcript is normally
# well under 1 MB.
MAX_FILE_BYTES = 2 * 1024 * 1024

# Snapshot sections, hashed separately so `changed_fields` can name them.
# `audio` stays out for v2's reason: its signed URL changes on every build.
SECTIONS = ("meeting", "lifecycle", "participants", "summary", "minutes",
            "action_items", "transcript", "links", "included_outputs", "files")

# 15- or 18-character Salesforce record id.
SF_ID_RE = re.compile(r"^[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?$")
# An object API name: Lead, Property__c, acme__Unit__c.
OBJECT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,79}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ContractError(wc.ContractError):
    """Something MinuteX refuses to send. `code` is stable; the message is
    safe to show a user."""


def _s(value, limit=None):
    out = "" if value is None else str(value).strip()
    return out[:limit] if limit else out


def _bad(code, message, field=""):
    raise ContractError(code, message, field=field)


# ---------------------------------------------------------------------------
# What the user chose: links, outputs, task records, recorder
# ---------------------------------------------------------------------------

def records(n):
    """'1 record' / '5 records'."""
    return f"{n} record" if n == 1 else f"{n} records"


def link_config_block(objects):
    """What the app needs to build its link sheet: the linkable objects and
    the record limit. The app takes `max_links` from here, so MAX_LINKS is
    the only place the limit is set. `objects` is None when the customer's
    receiver doesn't support linking yet."""
    return {"supported": objects is not None, "max_links": MAX_LINKS,
            "objects": list(objects or [])}


MAX_LINKABLE_OBJECTS = 50
TYPE_LINK_SEARCH = "records.search"


def normalize_linkable_objects(raw):
    """The receiver's `linkable_objects`, normalized; None when the receiver
    didn't send the key (it doesn't support linking). Unusable rows are
    skipped rather than failing the whole configuration."""
    if raw is None:
        return None
    if not isinstance(raw, list):
        _bad("invalid_configuration", "linkable_objects must be a list")
    out, seen = [], set()
    for item in raw[:MAX_LINKABLE_OBJECTS]:
        if not isinstance(item, dict):
            continue
        obj = _s(item.get("object"))
        label = _s(item.get("label"), 80)
        if not OBJECT_RE.match(obj) or not label or obj.lower() in seen:
            continue
        seen.add(obj.lower())

        def outs(key, within=OUTPUTS):
            vals = item.get(key) if isinstance(item.get(key), list) else []
            return [o for o in OUTPUTS if o in {_s(v) for v in vals} and o in within]

        allowed = outs("outputs_allowed")
        out.append({
            "object": obj, "label": label,
            "can_be_primary": item.get("can_be_primary") is not False,
            "outputs_allowed": allowed,
            "outputs_default_on": outs("outputs_default_on", allowed),
            "outputs_user_can_change": outs("outputs_user_can_change", allowed),
        })
    return out


def apply_link_policy(links, included_outputs, task_relations, objects):
    """The user's choice, checked against the admin's per-object settings
    on the server: the app's sheet is a convenience, this is the rule.

    Returns (links, outputs, task_relations). Raises ContractError for a
    record whose object isn't linkable, a main record whose object can't be
    one, or an output no linked object allows. An output the admin locked
    (no linked object lets users change it) is set to its default whatever
    was sent, mirroring the app's outputOptions().
    """
    by_object = {o["object"].lower(): o for o in (objects or [])}
    links = normalize_links(links)
    for i, link in enumerate(links):
        if link["object"].lower() not in by_object:
            _bad("object_not_linkable", f"records of {link['object']} can't be linked",
                 field=f"links[{i}]")
    primary = next(l for l in links if l["primary"])
    primary_obj = by_object[primary["object"].lower()]
    if not primary_obj["can_be_primary"]:
        _bad("invalid_primary", f"a {primary_obj['label']} can't be the main record",
             field="links")
    linked = [by_object[l["object"].lower()] for l in links]

    chosen = set(normalize_outputs(included_outputs))
    final = set()
    for output in OUTPUTS:
        allowing = [o for o in linked if output in o["outputs_allowed"]]
        if not allowing:
            if output in chosen:
                _bad("output_not_allowed",
                     f"{output} isn't allowed for these records", field="included_outputs")
            continue
        locked = not any(output in o["outputs_user_can_change"] for o in allowing)
        if locked:
            source = primary_obj if output in primary_obj["outputs_allowed"] else allowing[0]
            if output in source["outputs_default_on"]:
                final.add(output)
        elif output in chosen:
            final.add(output)
    outputs = normalize_outputs(final)

    relations = {}
    for task_id, target in (task_relations or {}).items():
        tid = _s(task_id, 64)
        ref = normalize_task_related(target, field=f"task {tid}.related")
        if tid and ref is not None:
            relations[tid] = ref
    return links, outputs, relations


def link_search_request(*, request_id, query, objects, limit=10, user_email="",
                        workspace_id=""):
    """Search across the linkable objects (no meeting type): the receiver
    returns records of any of `objects`, each with its object_type."""
    query = _s(query)
    if len(query) < 2:
        _bad("query_too_short", "type at least 2 characters")
    if len(query) > wc.MAX_SEARCH_QUERY_CHARS:
        _bad("query_too_long", f"search at most {wc.MAX_SEARCH_QUERY_CHARS} characters")
    names = [o for o in (objects or []) if OBJECT_RE.match(_s(o))]
    if not names:
        _bad("no_linkable_objects", "no records can be linked yet")
    return {"type": TYPE_LINK_SEARCH, "contract_version": CONTRACT_VERSION,
            "request_id": _s(request_id), "workspace_id": _s(workspace_id),
            "query": query, "objects": names[:MAX_LINKABLE_OBJECTS],
            "limit": max(1, min(wc.MAX_SEARCH_LIMIT, int(limit or 10))),
            "user_email": _s(user_email, 254).lower()}


def _record_ref(raw, field):
    """{object, id} from a dict, or ContractError."""
    if not isinstance(raw, dict):
        _bad("invalid_record", "a record must be an object with object and id",
             field=field)
    obj = _s(raw.get("object"))
    record_id = _s(raw.get("id"))
    if not OBJECT_RE.match(obj):
        _bad("invalid_record", f"{obj!r} is not a Salesforce object name",
             field=f"{field}.object")
    if not SF_ID_RE.match(record_id):
        _bad("invalid_record", f"{record_id!r} is not a Salesforce record id",
             field=f"{field}.id")
    return {"object": obj, "id": record_id}


def _same_record(a, b):
    # A 15-character id and its 18-character form are the same record; the
    # first 15 characters are case-sensitive and identify it.
    return a[:15] == b[:15]


def normalize_links(raw):
    """The meeting's links: 1-5 records, exactly one primary.

    With one link and no primary flag, that link is the primary. With
    several, the caller must say which: guessing would relate every task to
    a record the user didn't pick as the main one.
    """
    if not isinstance(raw, list) or not raw:
        _bad("invalid_links", "link the meeting to at least one record",
             field="links")
    if len(raw) > MAX_LINKS:
        _bad("too_many_links", f"a meeting can be linked to at most {records(MAX_LINKS)}",
             field="links")
    out = []
    for i, item in enumerate(raw):
        ref = _record_ref(item, f"links[{i}]")
        if any(_same_record(ref["id"], o["id"]) for o in out):
            _bad("duplicate_link", "the same record is linked twice",
                 field=f"links[{i}]")
        ref["name"] = _s(item.get("name"), 255)
        ref["primary"] = item.get("primary") is True
        out.append(ref)
    primaries = sum(1 for o in out if o["primary"])
    if primaries == 0 and len(out) == 1:
        out[0]["primary"] = True
    elif primaries != 1:
        _bad("invalid_primary", "mark exactly one linked record as primary",
             field="links")
    return out


def normalize_outputs(raw):
    """The included outputs, de-duplicated, in canonical order."""
    if not isinstance(raw, (list, tuple, set)):
        _bad("invalid_outputs", "included_outputs must be a list",
             field="included_outputs")
    chosen = set()
    for value in raw:
        name = _s(value)
        if name not in OUTPUTS:
            _bad("invalid_outputs", f"{name!r} is not a MinuteX output",
                 field="included_outputs")
        chosen.add(name)
    if OUTPUT_CONTRIBUTIONS in chosen and OUTPUT_PARTICIPANTS not in chosen:
        _bad("invalid_outputs",
             "what each person said can only be sent with the participants",
             field="included_outputs")
    return [o for o in OUTPUTS if o in chosen]


def normalize_task_related(raw, field="related"):
    """None (follow the primary link) or one {object, id}."""
    if raw is None:
        return None
    return _record_ref(raw, field)


def normalize_recorded_by(raw):
    """{name, email} for whoever recorded the meeting, or None."""
    if not isinstance(raw, dict):
        return None
    name = _s(raw.get("name"), 255)
    email = _s(raw.get("email"), 254).lower()
    if email and not EMAIL_RE.match(email):
        email = ""
    if not name and not email:
        return None
    return {"name": name, "email": email}


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def file_id(kind, name=""):
    """One file's id within a meeting: the kind, or "document:<type>"."""
    return f"{kind}:{name}" if kind == FILE_DOCUMENT else kind


def split_file_id(fid):
    """file_id() back to (kind, name)."""
    kind, _, name = str(fid).partition(":")
    return kind, name


def file_key(meeting_id, kind, name=""):
    """The receiver's upsert key for one meeting file. Stable, so a changed
    file becomes a new version of the same Salesforce File."""
    if kind not in FILE_OUTPUT:
        _bad("invalid_file", f"{kind!r} is not a MinuteX file kind", field="kind")
    mid = _s(meeting_id)
    if not mid:
        _bad("invalid_file", "a file needs a meeting", field="meeting_id")
    if kind == FILE_DOCUMENT:
        if not DOCUMENT_NAME_RE.match(_s(name)) or name == MOM_DOCUMENT_TYPE:
            _bad("invalid_file", "a document file needs its document type", field="name")
        return f"{mid}:{kind}:{name}"
    return f"{mid}:{kind}"


def file_ref(meeting_id, kind, content, name=""):
    """{kind, [name], key, sha256, size_bytes} for the event's `files` list."""
    data = bytes(content or b"")
    ref = {"kind": kind, "key": file_key(meeting_id, kind, name),
           "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
    if kind == FILE_DOCUMENT:
        ref["name"] = name
    return ref


def file_ref_for(meeting_id, fid, content):
    """file_ref() for a file_id()."""
    kind, name = split_file_id(fid)
    return file_ref(meeting_id, kind, content, name)


def file_request(*, meeting_id, kind, content, filename, request_id, name=""):
    """The POST /files body for one file, or ContractError.

    Empty and oversize files are refused here rather than by the receiver, so
    the user sees why before anything is sent.
    """
    data = bytes(content or b"")
    if not data:
        _bad("empty_file", "the file is empty", field="content")
    if len(data) > MAX_FILE_BYTES:
        _bad("file_too_large",
             f"the file is {len(data):,} bytes; at most {MAX_FILE_BYTES:,} can be "
             "sent to Salesforce", field="content")
    shown = _s(filename, 255)
    if not shown:
        _bad("invalid_file", "the file needs a name", field="filename")
    ref = file_ref(meeting_id, kind, data, name)
    return {"type": TYPE_FILE, "contract_version": CONTRACT_VERSION,
            "request_id": _s(request_id), "meeting_id": _s(meeting_id),
            "kind": kind, "key": ref["key"], "filename": shown,
            "content_type": FILE_CONTENT_TYPES[kind],
            "sha256": ref["sha256"], "size_bytes": ref["size_bytes"],
            "content_base64": base64.b64encode(data).decode("ascii")}


def _normalize_files(raw, outputs, meeting_id):
    out = []
    for i, item in enumerate(raw or []):
        if not isinstance(item, dict):
            _bad("invalid_file", "a file must be an object", field=f"files[{i}]")
        kind = _s(item.get("kind"))
        name = _s(item.get("name"))
        key = file_key(meeting_id, kind, name)
        if FILE_OUTPUT[kind] not in outputs:
            _bad("invalid_file", f"the {kind} file is sent only with its output",
                 field=f"files[{i}]")
        if _s(item.get("key")) != key:
            _bad("invalid_file", "the file key does not belong to this meeting",
                 field=f"files[{i}].key")
        digest = _s(item.get("sha256")).lower()
        if not SHA256_RE.match(digest):
            _bad("invalid_file", "the file needs its SHA-256", field=f"files[{i}].sha256")
        if any(f["key"] == key for f in out):
            _bad("invalid_file", f"the {kind} file is listed twice", field=f"files[{i}]")
        ref = {"kind": kind, "key": key, "sha256": digest,
               "size_bytes": int(item.get("size_bytes") or 0)}
        if kind == FILE_DOCUMENT:
            ref["name"] = name
        out.append(ref)
    return out


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def build_snapshot(item, tasks=None, *, included_outputs, participants=None,
                   workspace=None, audio=None, lifecycle=LIFECYCLE_ACTIVE,
                   minutes_markdown="", recorded_by=None, task_relations=None,
                   files=None, transcript_body=None):
    """The `data` of a created/updated v3 event.

    `task_relations` maps a MinuteX task id to the record the user chose for
    it; a task missing from the map follows the primary link. `files` is
    the list of file_ref() dicts already uploaded for this state.
    `transcript_body` is the readable transcript (names and times) to send
    when the transcript is included; without it, the stored text is used.
    """
    outputs = normalize_outputs(included_outputs)
    item = dict(item if isinstance(item, dict) else {})
    # Taken out before the v2 builder, which refuses a long transcript; v3
    # splits it over the Transcript fields instead (split_transcript).
    transcript_text = _s(item.pop("transcript", ""))
    item.pop("transcript_s3_key", None)

    base = wc.build_snapshot(item, tasks, participants=participants,
                             workspace=workspace, meeting_url="", audio=audio,
                             lifecycle=lifecycle, minutes_markdown=minutes_markdown)

    meeting = dict(base["meeting"])
    meeting["recorded_by"] = normalize_recorded_by(recorded_by)
    data = {"meeting": meeting}

    if OUTPUT_OVERVIEW in outputs:
        data["summary"] = base["summary"]
    # The MoM travels ONLY as its PDF (a File, see `files`), never as text:
    # the app renders it at Link and after every MoM change.
    if OUTPUT_TASKS in outputs:
        relations = task_relations if isinstance(task_relations, dict) else {}
        data["action_items"] = []
        for row in base["action_items"]:
            row = dict(row)
            row["related"] = normalize_task_related(
                relations.get(row["id"]), field=f"task {row['id']}.related")
            data["action_items"].append(row)
    if OUTPUT_PARTICIPANTS in outputs:
        keep_said = OUTPUT_CONTRIBUTIONS in outputs
        data["participants"] = [
            p if keep_said else {**p, "contribution_summary": ""}
            for p in base["participants"]]
    data["transcript"] = {
        "included": OUTPUT_TRANSCRIPT in outputs,
        "chars": site_contract.text_length(transcript_text),
        "language": base["transcript"].get("language", ""),
    }
    if OUTPUT_TRANSCRIPT in outputs:
        parts, cut = split_transcript(transcript_text if transcript_body is None else transcript_body)
        data["transcript"]["parts"] = parts
        data["transcript"]["text"] = parts[0] if parts else ""
        data["transcript"]["truncated"] = cut
    if OUTPUT_RECORDING in outputs:
        data["audio"] = base["audio"]
    data["files"] = _normalize_files(files, outputs, meeting.get("id"))
    return data


def deleted_snapshot(meeting_id, deleted_at):
    return wc.deleted_snapshot(meeting_id, deleted_at)


def section_hashes(data, links=None, included_outputs=None):
    """{section: sha256 hex} over the snapshot plus the user's choices."""
    data = data if isinstance(data, dict) else {}
    meeting = dict(data.get("meeting") or {})
    lifecycle = meeting.pop("lifecycle", LIFECYCLE_ACTIVE)
    parts = {
        "meeting": meeting,
        "lifecycle": lifecycle,
        "participants": data.get("participants"),
        "summary": data.get("summary"),
        "minutes": data.get("minutes"),
        "action_items": data.get("action_items"),
        "transcript": data.get("transcript") or {},
        "links": links or [],
        "included_outputs": list(included_outputs or []),
        "files": data.get("files") or [],
    }
    return {name: hashlib.sha256(wc.canonical_json(parts[name])).hexdigest()
            for name in SECTIONS}


def content_hash(hashes):
    ordered = [f"{name}={(hashes or {}).get(name, '')}" for name in SECTIONS]
    return hashlib.sha256("|".join(ordered).encode("utf-8")).hexdigest()


def changed_fields(old_hashes, new_hashes):
    if not old_hashes:
        return ["*"]
    return [name for name in SECTIONS
            if (old_hashes or {}).get(name) != (new_hashes or {}).get(name)]


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------

def event_envelope(event, *, meeting_id, sequence, occurred_at, workspace_id="",
                   links=None, included_outputs=None, changed=None, data=None):
    """One v3 event. A deleted meeting carries no links or outputs."""
    if event not in EVENTS:
        _bad("unsupported_event", f"unknown event {event!r}")
    deleted = event == EVENT_DELETED
    return {
        "type": TYPE_EVENT,
        "contract_version": CONTRACT_VERSION,
        "event": event,
        "event_id": wc.event_id(meeting_id, sequence),
        "occurred_at": _s(occurred_at),
        "workspace_id": _s(workspace_id),
        "meeting_id": _s(meeting_id),
        "sequence": int(sequence),
        "links": [] if deleted else normalize_links(links),
        "included_outputs": [] if deleted else normalize_outputs(included_outputs or []),
        "changed_fields": list(changed or []),
        "data": data if isinstance(data, dict) else {},
    }
