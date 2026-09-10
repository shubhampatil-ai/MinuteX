"""mom_schema — the structured Minutes-of-Meeting document.

WHY THIS EXISTS. `documents.minutes_of_meeting` already holds a MoM, but as
one opaque Markdown blob. That is fine to read and hopeless to EDIT: "delete
the Highlights section", "move Action Items above Decisions", "add a Deadline
column", and above all "keep MY wording for this one line when the AI
regenerates the rest" are all questions a blob cannot answer. So the MoM gets
a structured representation, stored beside the blob rather than instead of it.

THE TWO-REPRESENTATION CONTRACT. The structured `mom` attribute is the SOURCE
OF TRUTH. `documents.minutes_of_meeting` is a derived MIRROR, re-rendered by
render_markdown() on every structured write. Nothing that already reads the
document — the Documents list, DOCX/PDF export, share, the Assistant's
context — has to learn about `mom`; they keep reading Markdown and keep
working. The mirror is one-way: editing the Markdown blob directly does NOT
feed back into the structured model, and doing so is what marks the document
`edited` (see update_document), which the existing freshness rules already
honour.

SOURCE TRACKING is the load-bearing idea, not decoration. Every section,
field, row and cell carries a `source`:

    ai          written by a generation, never touched by the user
    user_edited started as `ai`, the user changed the text
    user_added  the user created it; no generation produced it

Regeneration (build_from_recording with an existing mom) MERGES rather than
replaces: `ai` items take the new AI text, `user_edited` and `user_added`
items keep the user's text, and anything the user DELETED stays deleted —
which is why deletions are remembered in `deleted_ids` instead of just
dropping the item. Without that tombstone set, every regeneration would
resurrect the sections a user deliberately removed.

NO NEW AI CALL. build_from_recording reads what the pipeline already produced
— the dynamic overview, participants, meeting_highlights and tasks (plus the
legacy summary/highlights attributes on older rows) — and arranges it. Generating a MoM therefore costs zero Groq tokens and works offline of the
model entirely. That is deliberate: the transcript has already been analysed,
and asking a model to re-derive decisions it already extracted would be both
slower and less consistent than reading the extraction.

Same contract as ai_schema: a coercer NEVER raises. Whatever is in DynamoDB —
written by an older version, half-migrated, or hand-edited — comes out as a
valid structure.
"""
import hashlib

import speaker_identity

from datetime import datetime, timedelta
from decimal import Decimal

# Bumped when a change to the section catalogue or rendering makes a stored
# structure stale. Mirrors ai_schema.AI_VERSION's role and is stamped on the
# document the same way, so a revision invalidates lazily on next read rather
# than needing a migration.
#
# "2" introduced TASK-BACKED ACTION ROW IDENTITY. Under "1" an Action Items row
# id was _ident("r", role, position) — pure position, carrying nothing about
# which Task it projected. Two consequences, both reproduced against the real
# merge: deleting a Task shifted every later Task up one slot so a user's edit
# stayed on the slot and attached to the WRONG Task, and a vanished Task left
# its stale row behind forever because _merge_rows keeps a stored row whose id
# the generation no longer produces.
#
# The version is load-bearing, not decorative: a stored "1" row id and a "2"
# row id are both opaque strings, so without the stamp the merge cannot tell a
# positional id from a task-derived one and would treat every migrated row as
# brand new. See migrate_action_identity() for how a "1" MoM is carried over.
MOM_VERSION = "2"
MOM_VERSION_POSITIONAL = "1"

# Where an action row records the Task it projects. Kept OUT of `cells` (which
# is the rendered grid and would show it to the user) and off the column list
# entirely — it is identity, not content. render_markdown never reads it.
TASK_REF = "task_ref"

# Provenance values. `ai` is the only one a generation may write; the other two
# are only ever set by a user edit, which is what makes the merge rule in
# merge_generated() decidable.
SOURCE_AI = "ai"
SOURCE_USER_EDITED = "user_edited"
SOURCE_USER_ADDED = "user_added"
SOURCES = (SOURCE_AI, SOURCE_USER_EDITED, SOURCE_USER_ADDED)

# Section kinds. `fields` is a label/value list (Meeting Details); `table` is a
# grid (Meeting Points, Attendees); `text` is prose (Summary); `list` is
# bullets (Highlights, Decisions). Four kinds cover every section in the
# reference design, and the editor renders one UI per kind — adding a fifth
# would mean a fifth editor, so the catalogue below deliberately expresses new
# sections as one of these rather than growing the enum.
KIND_FIELDS = "fields"
KIND_TABLE = "table"
KIND_TEXT = "text"
KIND_LIST = "list"
KINDS = (KIND_FIELDS, KIND_TABLE, KIND_TEXT, KIND_LIST)

# Bounds. Every one of these is a "a real MoM never reaches this" ceiling whose
# job is to stop a malformed payload growing the DynamoDB row, not to constrain
# genuine use. The recording row also carries the transcript-derived documents
# and tasks maps, so the 400KB item limit is a shared budget.
MAX_SECTIONS = 40
MAX_FIELDS_PER_SECTION = 60
MAX_COLUMNS = 12
MAX_ROWS = 200
MAX_TITLE_CHARS = 160
MAX_LABEL_CHARS = 120
MAX_VALUE_CHARS = 4_000
MAX_TEXT_CHARS = 12_000
MAX_LIST_ITEMS = 100
MAX_DELETED_IDS = 200


# ---------------------------------------------------------------------------
# Primitives. Deliberately local rather than imported from ai_schema: that
# module coerces MODEL output, this one coerces USER output, and the two have
# different truncation rules (a user's 4000-char field is kept and clipped; a
# model's is a runaway). Sharing `s` alone would not be worth the coupling.
# ---------------------------------------------------------------------------
def _s(v, limit=MAX_VALUE_CHARS):
    """Any -> string, clipped to `limit`. None/dict/list -> ''."""
    if isinstance(v, str):
        out = v.strip()
    elif isinstance(v, bool):
        # Before the numeric branch: bool is an int subclass and "True" is a
        # far less useful string than "".
        return ""
    elif isinstance(v, (int, float, Decimal)):
        out = str(v)
    else:
        return ""
    return out[:limit]


def _source(v):
    """Any -> a valid source, defaulting to `ai`.

    Defaults to `ai` rather than `user_added` on purpose: an item with no
    recorded provenance came from a generation that predates source tracking,
    and treating it as AI-authored means a regeneration may refresh it. The
    opposite default would freeze old content permanently.
    """
    out = _s(v, 20)
    return out if out in SOURCES else SOURCE_AI


def _bool(v, default=True):
    return bool(v) if isinstance(v, bool) else default


def _ident(prefix, *parts):
    """A stable, content-derived id.

    Content-derived rather than random so that regenerating a MoM produces the
    SAME ids for the same sections — which is what lets merge_generated match
    an incoming AI section against the stored one the user edited. A uuid here
    would make every regeneration look like a wholly new document and lose
    every user edit.
    """
    raw = "|".join(str(p) for p in parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    return f"{prefix}_{digest}"


def new_id(prefix, seed):
    """Public id minter for user-added items. `seed` must be unique per call
    (the route passes a timestamp + counter); ids are otherwise content-derived
    and two identically-named user sections would collide."""
    return _ident(prefix, seed)


# ---------------------------------------------------------------------------
# Coercion
# ---------------------------------------------------------------------------
def _coerce_field(raw, index):
    if not isinstance(raw, dict):
        return None
    label = _s(raw.get("label"), MAX_LABEL_CHARS)
    value = _s(raw.get("value"))
    # A field with neither a label nor a value carries no information. Kept
    # when EITHER is present: a labelled blank is a real thing in an editor
    # (the user made the row and hasn't filled it yet).
    if not label and not value:
        return None
    return {
        "id": _s(raw.get("id"), 64) or _ident("f", label, index),
        "label": label,
        "value": value,
        "visible": _bool(raw.get("visible")),
        "source": _source(raw.get("source")),
    }


def _coerce_column(raw, index):
    if isinstance(raw, str):
        label = _s(raw, MAX_LABEL_CHARS)
        return {"id": _ident("c", label, index), "label": label,
                "source": SOURCE_AI} if label else None
    if not isinstance(raw, dict):
        return None
    label = _s(raw.get("label"), MAX_LABEL_CHARS)
    if not label:
        return None
    return {
        "id": _s(raw.get("id"), 64) or _ident("c", label, index),
        "label": label,
        "source": _source(raw.get("source")),
    }


def _coerce_row(raw, columns, index):
    """One table row, normalised to EXACTLY the current column set.

    Cells are stored keyed by COLUMN ID, not by position. Position-keyed cells
    would silently shift every value one column left when a column is deleted
    from the middle — the single most likely way for a table edit to corrupt
    data. Keying by id makes add/delete/reorder of columns lossless, and makes
    a missing cell simply blank.
    """
    # A row in a table with no columns holds nothing and can never render.
    # Dropping it here keeps a malformed payload from consuming MAX_ROWS with
    # empty cell maps.
    if not columns:
        return None
    if isinstance(raw, list):
        # A bare list of strings — the shape the builders emit before ids
        # exist, and a plausible hand-written payload. Map positionally.
        cells = {c["id"]: _s(raw[i]) if i < len(raw) else ""
                 for i, c in enumerate(columns)}
        return {"id": _ident("r", index, *[cells[c["id"]] for c in columns]),
                "cells": cells, "visible": True, "source": SOURCE_AI}
    if not isinstance(raw, dict):
        return None
    raw_cells = raw.get("cells")
    raw_cells = raw_cells if isinstance(raw_cells, dict) else {}
    cells = {c["id"]: _s(raw_cells.get(c["id"])) for c in columns}
    out = {
        "id": _s(raw.get("id"), 64)
            or _ident("r", index, *[cells[c["id"]] for c in columns]),
        "cells": cells,
        "visible": _bool(raw.get("visible")),
        "source": _source(raw.get("source")),
    }
    # Carried through explicitly. This function returns a FIXED dict, so any
    # key not named here is destroyed on the next coerce_mom — which is what
    # keeps a client from smuggling extra state onto a row, and is also why
    # the task ref has to be listed. Without this line the ref would survive
    # exactly one request and every later regeneration would see a row with no
    # ref and fall back to positional matching.
    ref = _s(raw.get(TASK_REF), 200)
    if ref:
        out[TASK_REF] = ref
    return out


def _coerce_section(raw, index):
    if not isinstance(raw, dict):
        return None
    kind = _s(raw.get("kind"), 20)
    if kind not in KINDS:
        kind = KIND_TEXT
    title = _s(raw.get("title"), MAX_TITLE_CHARS)
    if not title:
        return None

    section = {
        "id": _s(raw.get("id"), 64) or _ident("s", title, index),
        "kind": kind,
        "title": title,
        "visible": _bool(raw.get("visible")),
        "source": _source(raw.get("source")),
        # Which catalogue entry produced this section, "" for a user-added
        # one. build_from_recording matches on this rather than on the title,
        # so renaming "Attendees" to "Participants" doesn't make the next
        # generation create a duplicate section.
        "role": _s(raw.get("role"), 40),
    }

    if kind == KIND_FIELDS:
        fields = []
        for i, f in enumerate(raw.get("fields") or []):
            got = _coerce_field(f, i)
            if got:
                fields.append(got)
            if len(fields) >= MAX_FIELDS_PER_SECTION:
                break
        section["fields"] = _dedupe_ids(fields)
    elif kind == KIND_TABLE:
        columns = []
        for i, c in enumerate(raw.get("columns") or []):
            got = _coerce_column(c, i)
            if got:
                columns.append(got)
            if len(columns) >= MAX_COLUMNS:
                break
        columns = _dedupe_ids(columns)
        rows = []
        for i, r in enumerate(raw.get("rows") or []):
            got = _coerce_row(r, columns, i)
            if got:
                rows.append(got)
            if len(rows) >= MAX_ROWS:
                break
        section["columns"] = columns
        section["rows"] = _dedupe_ids(rows)
    elif kind == KIND_TEXT:
        section["text"] = _s(raw.get("text"), MAX_TEXT_CHARS)
    else:  # KIND_LIST
        items = []
        for i, it in enumerate(raw.get("items") or []):
            if isinstance(it, str):
                text = _s(it)
                if text:
                    items.append({"id": _ident("i", text, i), "text": text,
                                  "visible": True, "source": SOURCE_AI})
            elif isinstance(it, dict):
                text = _s(it.get("text"))
                if text:
                    items.append({
                        "id": _s(it.get("id"), 64) or _ident("i", text, i),
                        "text": text,
                        "visible": _bool(it.get("visible")),
                        "source": _source(it.get("source")),
                    })
            if len(items) >= MAX_LIST_ITEMS:
                break
        section["items"] = _dedupe_ids(items)

    return section


def _dedupe_ids(items):
    """Guarantee unique ids within one collection.

    Ids are content-derived, so two identical rows (a real possibility in a
    table — the same owner twice) would otherwise share an id and the editor
    would edit both at once. Suffixing the duplicate is enough: the id only has
    to be unique within its own list.
    """
    seen = set()
    out = []
    for item in items:
        ident = item["id"]
        if ident in seen:
            n = 2
            while f"{ident}_{n}" in seen:
                n += 1
            ident = f"{ident}_{n}"
            item = dict(item, id=ident)
        seen.add(ident)
        out.append(item)
    return out


def empty_mom():
    """A valid, empty structure. Never shares mutable state between calls."""
    return {
        "title": "",
        "subtitle": "",
        "sections": [],
        "deleted_ids": [],
        "mom_version": MOM_VERSION,
        "generated_at": "",
        "updated_at": "",
        "transcript_fingerprint": "",
        "speaker_mapping_version": 0,
    }


def coerce_mom(raw):
    """Any -> a valid MoM structure. Never raises."""
    if not isinstance(raw, dict):
        return empty_mom()

    sections = []
    for i, sec in enumerate(raw.get("sections") or []):
        got = _coerce_section(sec, i)
        if got:
            sections.append(got)
        if len(sections) >= MAX_SECTIONS:
            break

    deleted = []
    for d in raw.get("deleted_ids") or []:
        ident = _s(d, 64)
        if ident and ident not in deleted:
            deleted.append(ident)
        if len(deleted) >= MAX_DELETED_IDS:
            break

    version = raw.get("speaker_mapping_version")
    return {
        "title": _s(raw.get("title"), MAX_TITLE_CHARS),
        "subtitle": _s(raw.get("subtitle"), MAX_TITLE_CHARS),
        "sections": _dedupe_ids(sections),
        "deleted_ids": deleted,
        "mom_version": _s(raw.get("mom_version"), 10) or MOM_VERSION,
        "generated_at": _s(raw.get("generated_at"), 40),
        "updated_at": _s(raw.get("updated_at"), 40),
        "transcript_fingerprint": _s(raw.get("transcript_fingerprint"), 40),
        "speaker_mapping_version": int(version)
            if isinstance(version, (int, float, Decimal)) else 0,
    }


# ---------------------------------------------------------------------------
# Speaker labels. Mirrors lib/sources.ts' normalizeSpeakerLabel /
# speakerName pair so the app and this module never disagree about what one
# speaker is called — the same reason userapi's _speaker_display_name exists.
#
# Duplicated here rather than imported from the Lambda: lambda_function.py
# imports THIS module, and importing back would be a cycle. Six lines is the
# cheaper price.
# ---------------------------------------------------------------------------
def normalize_speaker_label(label):
    """"Speaker 0" / "speaker_0" / "0" -> "0". The raw diarization label,
    which is what the speaker_names map is keyed by.

    Now delegates to speaker_identity, the single definition. One BEHAVIOUR
    CHANGE comes with that: a label whose prefix does not leave a plain number
    ("Speaker Two") keeps its whole identity instead of being truncated to
    "Two". Truncating it could collide with a different speaker, and the
    stricter reading is what every join in the system already used.
    """
    return speaker_identity.normalize_speaker_id(label)


def speaker_display_name(label, speaker_names=None):
    """The human-facing name for a speaker label.

    Resolves through the shared identity layer, so MoM, the API and the
    transcript can never disagree — and a map stored under a legacy
    display-form key ({"Speaker 0": ...}) resolves here too.
    """
    return speaker_identity.resolve_speaker_display(label, speaker_names)


# ---------------------------------------------------------------------------
# The section catalogue — the DEFAULT MoM, in the reference design's order.
#
# Each entry names a `role` (the stable identity a regeneration matches on),
# a kind, a title, and a builder that turns already-extracted AI data into
# content. A section whose builder returns nothing is OMITTED rather than
# emitted empty: a MoM padded with "None recorded" rows is noise, and the same
# judgement is already made by the minutes_of_meeting prompt itself.
#
# Adding a section to the default MoM is one entry here. The editor needs no
# change — it renders by `kind`, not by role.
# ---------------------------------------------------------------------------
ROLE_DETAILS = "meeting_details"
ROLE_ATTENDEES = "attendees"
# The dynamic overview's sections. Unlike every other role here this is a
# PREFIX, not a whole role: the AI decides how many sections exist, so each one
# derives its role as "overview:<the overview section's id>". That keeps ids
# stable across regenerations (which is what merge_generated matches on) while
# letting the count vary per meeting.
ROLE_OVERVIEW = "overview"

# Pre-overview roles. Still emitted for recordings analysed before the dynamic
# overview shipped, and still honoured by the merge for MoMs already stored
# under them — see _build_legacy_summary / _build_legacy_highlights.
ROLE_SUMMARY = "summary"
ROLE_HIGHLIGHTS = "highlights"
ROLE_POINTS = "meeting_points"
ROLE_DECISIONS = "decisions"
ROLE_ACTIONS = "action_items"
ROLE_FOLLOWUPS = "follow_ups"


def _section(role, kind, title, **payload):
    """Assemble one catalogue section with a stable, role-derived id.

    No `order` field: order IS the list position in `sections`. Storing both
    would let them disagree, and every reorder would have to rewrite every
    section rather than move one list element.
    """
    base = {
        "id": _ident("s", role),
        "kind": kind,
        "title": title,
        "visible": True,
        "source": SOURCE_AI,
        "role": role,
    }
    base.update(payload)
    return base


def _fields(role, pairs):
    """[(label, value)] -> field dicts, skipping pairs with no value.

    Skipping the empty ones is what keeps Meeting Details honest: a meeting
    with no recorded objective shows no Objective row, rather than one reading
    "Not recorded". The user can always add the field back by hand, and it is
    then `user_added` and never removed by a regeneration.
    """
    out = []
    for label, value in pairs:
        value = _s(value)
        if not value:
            continue
        out.append({
            # Keyed on the LABEL, which is the stable half of a field; the
            # value is what a regeneration changes. Same merge-identity
            # reasoning as _items/_rows, satisfied here by the label alone.
            "id": _ident("f", role, label),
            "label": label,
            "value": value,
            "visible": True,
            "source": SOURCE_AI,
        })
    return out


def _columns(role, labels):
    return [{"id": _ident("c", role, label), "label": label,
             "source": SOURCE_AI} for label in labels]


def _rows(role, columns, values_list, refs=None):
    """[[cell, ...]] -> row dicts keyed by column id.

    Ids are POSITIONAL — derived from (role, position), never from cell
    contents. A regeneration that rewords a row must still match it to the
    stored one so the merge can decide whether the user edited it; a
    content-derived id would make every reworded row look brand new and
    duplicate it against the stale copy.

    `refs` OPTS A TABLE OUT of positional identity. Pass one external key per
    row — a Task id, for Action Items — and the row id becomes a function of
    that key instead of the index, with the key recorded under TASK_REF so the
    merge can tell a task-backed row from a user-added one. Position is the
    right identity for a table whose rows have no independent existence
    (Attendees follows the speaker roster, and roster slot N *is* the
    identity); it is the wrong identity for a projection of a mutable external
    collection, where deleting the first Task shifts every later one into a
    slot that already carries someone else's edit.

    A ref that is empty falls back to the positional id for that row alone, so
    a task with no id degrades to the old behaviour rather than colliding with
    every other id-less row on a shared hash of "".
    """
    out = []
    for i, values in enumerate(values_list):
        cells = {c["id"]: _s(values[j]) if j < len(values) else ""
                 for j, c in enumerate(columns)}
        ref = _s(refs[i], 200) if refs and i < len(refs) else ""
        row = {
            # Namespaced on the ref, not the raw ref: an id must stay
            # recognisably a row id, and "r_" + hash keeps every id in the
            # document the same shape whatever minted it.
            "id": _ident("r", role, "ref", ref) if ref
                  else _ident("r", role, i),
            "cells": cells,
            "visible": True,
            "source": SOURCE_AI,
        }
        if ref:
            row[TASK_REF] = ref
        out.append(row)
    return out


def _items(role, texts):
    """[text] -> list-item dicts with POSITIONAL ids.

    The id is derived from (role, position) and NOT from the text. That looks
    wrong next to _ident's "content-derived so regeneration is stable"
    docstring, and it is the opposite choice on purpose: for an item whose
    TEXT is the thing a regeneration changes, a content-derived id changes
    with it, so the merge sees an unrelated insert plus a delete and ends up
    keeping BOTH the stale and the fresh copy. Position is the only identity
    that survives a rewording, which is exactly what the merge needs to match
    on. The same reasoning applies to _rows below.
    """
    out = []
    for i, text in enumerate(texts):
        text = _s(text)
        if not text:
            continue
        # Numbered by OUTPUT position, not input index, so a dropped empty
        # entry does not leave a hole that shifts every later id.
        out.append({"id": _ident("i", role, len(out)), "text": text,
                    "visible": True, "source": SOURCE_AI})
    return out


def _fmt_date(iso):
    """An ISO timestamp -> "19 August 2026". Falls back to the raw string.

    Deliberately not locale-aware: this renders into a stored document that may
    be opened by anyone, so it uses one unambiguous format rather than the
    Lambda's incidental locale.
    """
    raw = str(iso or "").strip()
    if not raw:
        return ""
    try:
        text = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        return f"{dt.day} {dt.strftime('%B %Y')}"
    except (ValueError, TypeError):
        return raw


def _fmt_time_range(started, duration_seconds):
    """"3:30 PM - 4:00 PM", or "" when there is nothing solid to state.

    Returns just the start time rather than guessing when the duration is
    missing: an end time invented from a start time is exactly the kind of
    plausible-looking wrong detail a MoM must never contain.
    """
    raw = str(started or "").strip()
    if not raw:
        return ""
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return ""

    def _clock(d):
        # %-I is not portable (it fails on Windows); strip the pad by hand.
        return d.strftime("%I:%M %p").lstrip("0")

    start = _clock(dt)
    try:
        secs = int(float(duration_seconds or 0))
    except (TypeError, ValueError):
        secs = 0
    if secs <= 0:
        return start
    return f"{start} - {_clock(dt + timedelta(seconds=secs))}"


def _build_details(item, meta):
    pairs = [
        ("Date", _fmt_date(item.get("started_at") or item.get("created_at"))),
        ("Meeting Name", _s(item.get("title"), MAX_TITLE_CHARS)),
        ("Time", _fmt_time_range(item.get("started_at"),
                                 item.get("duration_seconds"))),
        ("Meeting Objective", _s(meta.get("objective"))),
    ]
    fields = _fields(ROLE_DETAILS, pairs)
    if not fields:
        return None
    return _section(ROLE_DETAILS, KIND_FIELDS, "Meeting Details",
                    fields=fields)


# What the Role/Contribution column says for someone who was present but did
# not speak. A plain, checkable statement rather than an empty cell: a blank
# there is indistinguishable from a speaker whose summary the model left out,
# and a reader could not tell attendance from missing data.
ATTENDED_ONLY = "Attended"


def _build_attendees(item, speaker_names, attendees=None):
    """The Attendees table: the speaker roster, plus anyone who self-tagged.

    Reads `participants`, which ai_schema has already filtered to actual
    speakers in the transcript (see its _roster_filtered) — so a name merely
    MENTIONED in the meeting can never appear here as an attendee. That
    filtering is the whole reason this section can be trusted, and is why the
    builder does not fall back to scanning tasks for owner names.

    `attendees` carries people who recorded that they were PRESENT without
    speaking (the self-tag: see SELF_SPEAKER_PREFIX in the API). They are
    listed with a fixed "Attended" contribution and never a summary, because
    the meeting contains no speech of theirs to summarise — writing one would
    be exactly the invented attribution the roster filtering exists to
    prevent. They are appended AFTER the speakers, so the table still reads in
    speaker order.

    A self-tagged person who ALSO spoke is not duplicated: the speaker row is
    the truer record, and it wins.
    """
    participants = item.get("participants")
    have_speakers = isinstance(participants, list) and participants
    attendees = [a for a in (attendees or []) if str(a or "").strip()]
    if not have_speakers and not attendees:
        return None
    columns = _columns(ROLE_ATTENDEES, ["Sr. No", "Name", "Role / Contribution"])
    values = []
    seen = set()
    for p in (participants if have_speakers else []):
        if not isinstance(p, dict):
            continue
        name = speaker_display_name(p.get("speaker"), speaker_names)
        if not name:
            continue
        seen.add(name.strip().casefold())
        values.append([str(len(values) + 1), name, _s(p.get("summary"))])
    for name in attendees:
        name = str(name).strip()
        if name.casefold() in seen:
            continue
        seen.add(name.casefold())
        values.append([str(len(values) + 1), name, ATTENDED_ONLY])
    if not values:
        return None
    return _section(ROLE_ATTENDEES, KIND_TABLE, "Attendees",
                    columns=columns, rows=_rows(ROLE_ATTENDEES, columns, values))


def _overview_sections(item):
    """The stored dynamic overview's sections, or [] when there is none."""
    overview = item.get("overview")
    if not isinstance(overview, dict):
        return []
    sections = overview.get("sections")
    return sections if isinstance(sections, list) else []


def _build_overview(item):
    """The meeting's own AI sections, carried into the MoM as MoM sections.

    This replaced the fixed Summary + Highlights pair. Those two were the MoM's
    presentation of a fixed analysis schema; the analysis is now dynamic, so
    the MoM follows it rather than flattening it back into a template — a
    meeting whose overview is "Panel Feedback" and "Hardware Concerns" produces
    a MoM with those headings.

    Each overview section becomes ONE MoM section, keeping its own kind: prose
    stays prose, a list stays a list. The ids are namespaced by the overview
    section's id rather than by position, so a regeneration that reorders or
    drops a section still matches a user's edits to the right one — that id
    join is what mom_schema's merge rule depends on.

    Returns a LIST (unlike the other builders, which return one section or
    None), because how many sections a meeting yields is now the AI's call.
    """
    out = []
    for section in _overview_sections(item):
        if not isinstance(section, dict):
            continue
        title = _s(section.get("title"), MAX_TITLE_CHARS)
        if not title:
            continue
        # Namespaced on the overview's own stable id; falls back to position
        # for a section that somehow lacks one.
        role = f"{ROLE_OVERVIEW}:{section.get('id') or len(out)}"
        raw_items = section.get("items")
        items = _items(role, raw_items if isinstance(raw_items, list) else [])
        text = _s(section.get("content"), MAX_TEXT_CHARS)
        if items and not text:
            out.append(_section(role, KIND_LIST, title, items=items))
        elif text:
            # Both present: the prose leads and the points follow it, rather
            # than one being dropped. Rendered as text so nothing is lost.
            if items:
                text = text + "\n" + "\n".join(f"- {i['text']}" for i in items)
            out.append(_section(role, KIND_TEXT, title, text=text))
    return out


def _build_legacy_summary(item):
    """Summary — from the pre-overview `summary` attribute only.

    Kept for recordings analysed before the dynamic overview shipped. A row
    that HAS an overview never reaches here (see build_sections), so a current
    recording is never described twice.
    """
    text = _s(item.get("summary"), MAX_TEXT_CHARS)
    if not text:
        return None
    return _section(ROLE_SUMMARY, KIND_TEXT, "Summary", text=text)


def _build_legacy_highlights(item):
    """Highlights — from the pre-overview `highlights` attribute only."""
    highlights = item.get("highlights")
    if not isinstance(highlights, list):
        return None
    items = _items(ROLE_HIGHLIGHTS, highlights)
    if not items:
        return None
    return _section(ROLE_HIGHLIGHTS, KIND_LIST, "Highlights", items=items)


def _owner_display(owner, speaker_names):
    """An extracted owner string rendered as a current display name.

    The extraction may hold either a real name ("Rohan") or a diarization
    label ("Speaker 0" / "0"). Only the label form is resolved through
    speaker_names, so a renamed speaker shows the new name while a plain name
    the model extracted is passed through untouched.
    """
    text = _s(owner)
    if not text:
        return ""
    normalized = normalize_speaker_label(text)
    if normalized != text or text.isdigit():
        return speaker_display_name(text, speaker_names)
    return text


def _build_decisions(item, speaker_names=None):
    """The Decisions list.

    A decision may carry `speaker_id` — WHO decided, as a canonical id. It is
    resolved to the CURRENT display name here, at render time, so renaming
    that speaker changes what the MoM says on its next render without the
    decision text being rewritten. A decision with no `speaker_id` (every row
    written before the field existed, and every collective decision) renders
    exactly as it always did.
    """
    highlights = item.get("meeting_highlights")
    if not isinstance(highlights, dict):
        return None
    texts = []
    for d in highlights.get("decisions") or []:
        if not isinstance(d, dict):
            continue
        decision = _s(d.get("decision"))
        if not decision:
            continue
        context = _s(d.get("context"))
        # The speaker's CURRENT name, never a stored one. Only shown when the
        # id resolves to a real name — an unnamed speaker adds "Speaker 0",
        # which tells a reader nothing a decision needs.
        who = speaker_identity.resolve_speaker_name(
            d.get("speaker_id"), speaker_names)
        if who:
            context = f"{context} — decided by {who}" if context \
                else f"decided by {who}"
        texts.append(f"{decision} ({context})" if context else decision)
    items = _items(ROLE_DECISIONS, texts)
    if not items:
        return None
    return _section(ROLE_DECISIONS, KIND_LIST, "Decisions", items=items)


def _task_deadline(task):
    """The Deadline cell for one Task: resolved date if there is one, else the
    phrase as spoken.

    A Task carries both. `due_date` is what was SAID ("tomorrow", "Friday",
    "कल") and `due_date_normalized` is that resolved against the meeting's own
    day. The spoken phrase is the honest record of the transcript but it is not
    a date: a MoM read six months later still says "tomorrow", and production
    rows show exactly that. So the resolved value wins for display wherever it
    exists.

    Normalization legitimately fails — "Next Meeting", "2 to 3 days",
    "weekend", and every non-English phrase resolve to "" — and in that case
    the spoken phrase is strictly better than a blank cell and infinitely
    better than a guessed date. Nothing here invents a date.
    """
    if not isinstance(task, dict):
        return ""
    normalized = _s(task.get("due_date_normalized")
                    or task.get("due_normalized"))
    return normalized or _s(task.get("due") or task.get("due_date"))


def _build_actions(item, tasks, speaker_names):
    """Action Items — from the first-class Tasks the pipeline seeded.

    Source order, most authoritative first:
      1. `tasks`     — the real, assignable, status-tracked records, so the MoM
                       states the same owners and due dates the Tasks screen
                       shows (including any the user edited).
      2. `ai_tasks`  — the analysis's extraction, for a row analyzed but not
                       yet seeded (seeding runs on first read of Tasks).
      3. `meeting_highlights.action_items` — LEGACY. No longer generated; kept
                       so rows written before its removal still render.
    """
    columns = _columns(ROLE_ACTIONS,
                       ["Sr. No", "Action Item", "Owner", "Deadline"])
    values = []
    # One external identity per row, positionally aligned with `values`. Only
    # the `tasks` tier can fill these — the other two tiers project extractions
    # that have no independent record to point at — so a row built from
    # ai_tasks or the legacy highlights keeps the positional id it always had.
    refs = []

    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        text = _s(t.get("task") or t.get("title"))
        if not text:
            continue
        assignee = t.get("assignee")
        owner = ""
        if isinstance(assignee, dict):
            owner = _s(assignee.get("name") or assignee.get("display_name"))
        if not owner:
            owner = speaker_display_name(
                t.get("assignee_speaker_id"), speaker_names)
        values.append([str(len(values) + 1), text, owner, _task_deadline(t)])
        # The Task's own id, falling back to its content fingerprint. The
        # fingerprint is stable for the same (meeting, title, owner) and is
        # what the seeder already uses for idempotency, so it is the right
        # second choice; "" then degrades this row to positional identity.
        refs.append(_s(t.get("id") or t.get("task_id")
                       or t.get("fingerprint"), 200))

    # The analysis's own extraction, for a row analyzed but not yet seeded into
    # first-class Tasks (seeding happens on first read of the Tasks screen).
    if not values:
        for a in item.get("ai_tasks") or []:
            if not isinstance(a, dict):
                continue
            text = _s(a.get("task"))
            if not text:
                continue
            owner = _s(a.get("assignee")) or speaker_display_name(
                a.get("assignee_speaker_id"), speaker_names)
            values.append([str(len(values) + 1), text, owner,
                           _s(a.get("due_date"))])

    # LEGACY ONLY. meeting_highlights.action_items is no longer generated — it
    # duplicated `tasks` field-for-field — but rows written before its removal
    # still carry one, and this is what keeps their MoM rendering.
    if not values:
        highlights = item.get("meeting_highlights")
        actions = (highlights or {}).get("action_items") \
            if isinstance(highlights, dict) else None
        for a in actions or []:
            if not isinstance(a, dict):
                continue
            text = _s(a.get("task"))
            if not text:
                continue
            values.append([str(len(values) + 1), text,
                           _owner_display(a.get("owner"), speaker_names),
                           _s(a.get("deadline"))])

    if not values:
        return None
    # refs is shorter than values whenever a lower tier supplied the rows; the
    # missing entries simply read as "" and those rows stay positional.
    return _section(ROLE_ACTIONS, KIND_TABLE, "Action Items",
                    columns=columns,
                    rows=_rows(ROLE_ACTIONS, columns, values, refs=refs))


def _build_followups(item):
    """Follow-ups — open questions and deadlines that still need something.

    Deliberately merges two extractions: an open question and an upcoming
    deadline are both "something still outstanding" to a reader, and the
    reference design has one Follow-ups block rather than two.
    """
    highlights = item.get("meeting_highlights")
    if not isinstance(highlights, dict):
        return None
    texts = []
    for q in highlights.get("open_questions") or []:
        text = _s(q)
        if text:
            texts.append(text)
    for d in highlights.get("deadlines") or []:
        if not isinstance(d, dict):
            continue
        what = _s(d.get("what"))
        if not what:
            continue
        when = _s(d.get("when"))
        texts.append(f"{what} — {when}" if when else what)
    items = _items(ROLE_FOLLOWUPS, texts)
    if not items:
        return None
    return _section(ROLE_FOLLOWUPS, KIND_LIST, "Follow-ups", items=items)


def build_sections(item, tasks=None, speaker_names=None, meta=None,
                   attendees=None):
    """The default MoM sections for one recording, in catalogue order.

    Every builder returns None for "nothing to say", and those are dropped —
    so a sparse meeting yields a short MoM rather than a long one full of
    placeholders. Order here IS the document order; the user's own reordering
    is re-applied afterwards by merge_generated.
    """
    speaker_names = speaker_names or {}
    meta = meta or {}
    # The prose block is EITHER the dynamic overview's sections OR the legacy
    # summary/highlights pair — never both. A row analysed under the current
    # pipeline has an overview and gets it; a row processed before the overview
    # shipped still has the old attributes and renders exactly as it always
    # did. Preferring the overview when present is what stops a reprocessed
    # meeting showing its content twice under two sets of headings.
    prose = _build_overview(item)
    if not prose:
        prose = [s for s in (_build_legacy_summary(item),
                             _build_legacy_highlights(item)) if s]

    built = [
        _build_details(item, meta),
        _build_attendees(item, speaker_names, attendees),
        *prose,
        _build_decisions(item, speaker_names),
        _build_actions(item, tasks, speaker_names),
        _build_followups(item),
    ]
    return [s for s in built if s]


# ---------------------------------------------------------------------------
# MERGE — the rule that makes regeneration safe.
#
# This is the part of the feature most likely to lose someone's work if it is
# wrong, so the rule is stated once, here, and applied uniformly at every
# level (section, field, row, cell, list item):
#
#   the stored item wins UNLESS it is pure `ai`
#
# `ai`          -> take the freshly generated text (the user never touched it)
#   `user_edited` -> keep the stored text (the user rewrote AI content)
#   `user_added`  -> keep it, and keep it even if the generation has no
#                    counterpart (it never had one)
#   deleted       -> stays deleted, via `deleted_ids`
#
# Order is the STORED order: a user who moved Action Items above Highlights
# expects it to stay there across a regeneration. Newly generated sections the
# user has never seen are appended at the end rather than inserted into their
# catalogue position, because inserting would silently reshuffle a document
# the user has already arranged.
# ---------------------------------------------------------------------------
def _keep_user_text(stored):
    """True when this item's text must survive a regeneration."""
    return _source(stored.get("source")) in (SOURCE_USER_EDITED, SOURCE_USER_ADDED)


def _merge_fields(stored_fields, fresh_fields, deleted):
    fresh_by_id = {f["id"]: f for f in fresh_fields}
    out = []
    for field in stored_fields:
        if field["id"] in deleted:
            continue
        fresh = fresh_by_id.get(field["id"])
        if fresh and not _keep_user_text(field):
            # Pure-AI field: refresh the VALUE but keep the user's visibility
            # choice — hiding a field is an edit to the document even though
            # it is not an edit to the text.
            out.append(dict(fresh, visible=field.get("visible", True)))
        else:
            out.append(field)
    seen = {f["id"] for f in out}
    for field in fresh_fields:
        if field["id"] not in seen and field["id"] not in deleted:
            out.append(field)
    return out


def _merge_rows(stored_rows, fresh_rows, columns, deleted):
    """Rows merge per-CELL, not per-row.

    A row where the user corrected only the Owner should still pick up a
    regenerated Deadline. Cell-level provenance is not stored (that would be a
    source map per row); instead a row the user touched is marked
    `user_edited` as a whole and keeps all of its cells. That is the
    conservative direction: it can keep a stale AI cell, but it can never
    destroy a user-typed one.
    """
    fresh_by_id = {r["id"]: r for r in fresh_rows}
    # A table is a PROJECTION when the generation identifies its rows by an
    # external key. Then "the generation stopped producing this row" means the
    # underlying record is gone, and keeping the row would leave an orphan
    # claiming a Task that no longer exists. For every other table the stored
    # row is kept, because a missing row there only means the AI said less
    # this time — see the module note on never blanking a section.
    #
    # A ref on EITHER side marks the table. Reading only the fresh rows would
    # miss the case that matters most: when the last Task is deleted the
    # generation returns no rows at all, and a table judged "not a projection"
    # would then keep every stale row forever — the exact orphan this rule
    # exists to prevent. A stored "1" MoM has no refs on either side, so it
    # still can never be misread as a projection.
    projection = any(r.get(TASK_REF) for r in fresh_rows) or \
        any(r.get(TASK_REF) for r in stored_rows)
    out = []
    for row in stored_rows:
        if row["id"] in deleted:
            continue
        fresh = fresh_by_id.get(row["id"])
        if fresh and not _keep_user_text(row):
            cells = {c["id"]: fresh["cells"].get(c["id"], "") for c in columns}
            out.append(dict(row, cells=cells))
        elif fresh:
            # User-touched and still present: keep every stored cell.
            cells = {c["id"]: row["cells"].get(c["id"], "") for c in columns}
            out.append(dict(row, cells=cells))
        elif projection and row.get(TASK_REF):
            # DROPPED. The row names a Task the generation no longer returns,
            # so the Task is deleted. This is the one place a regeneration
            # removes content, and it is deliberately narrow: the row must
            # itself carry a ref (so a user_added row is never touched) AND
            # this generation must be a projection (so a legacy positional MoM
            # is never pruned). A user_edited row goes too — its Task is gone,
            # and a MoM asserting work nobody owns is worse than a MoM missing
            # a line the user can re-add.
            continue
        else:
            # No fresh counterpart and no ref: the pre-existing behaviour.
            # user_added rows land here and survive, as do every row of a
            # non-projection table.
            cells = {c["id"]: row["cells"].get(c["id"], "") for c in columns}
            out.append(dict(row, cells=cells))
    seen = {r["id"] for r in out}
    for row in fresh_rows:
        if row["id"] not in seen and row["id"] not in deleted:
            out.append(dict(row, cells={c["id"]: row["cells"].get(c["id"], "")
                                        for c in columns}))
    return out


def _merge_items(stored_items, fresh_items, deleted):
    fresh_by_id = {i["id"]: i for i in fresh_items}
    out = []
    for item in stored_items:
        if item["id"] in deleted:
            continue
        fresh = fresh_by_id.get(item["id"])
        if fresh and not _keep_user_text(item):
            out.append(dict(fresh, visible=item.get("visible", True)))
        else:
            out.append(item)
    seen = {i["id"] for i in out}
    for item in fresh_items:
        if item["id"] not in seen and item["id"] not in deleted:
            out.append(item)
    return out


def _merge_columns(stored_columns, fresh_columns, deleted):
    """Columns the user added are kept; AI columns they deleted stay gone.

    Column identity matters more than most: dropping a stored column silently
    orphans every cell keyed by its id, so a column is only ever removed when
    it is explicitly in `deleted`.
    """
    out = [c for c in stored_columns if c["id"] not in deleted]
    seen = {c["id"] for c in out}
    for col in fresh_columns:
        if col["id"] not in seen and col["id"] not in deleted:
            out.append(col)
    return out


def _merge_section(stored, fresh, deleted):
    """One section merged. `fresh` may be None (the generation produced
    nothing for this role this time — e.g. no decisions were made in the new
    transcript), in which case the stored section is kept as-is rather than
    emptied: a user looking at their MoM should not find a section silently
    blanked by a regeneration.

    ONE EXCEPTION: a PROJECTION table (its rows carry an external key — see
    _merge_rows) whose generation vanished entirely. That happens when the
    last Task is deleted, because _build_actions then returns None and the
    section never reaches the merge at all. Keeping it would leave the MoM
    asserting every deleted Task forever, so the projection's own rows are
    pruned instead. A user_added row has no key and survives, which is why
    the section can still come back non-empty."""
    if fresh is None:
        rows = stored.get("rows") or []
        if stored.get("kind") == KIND_TABLE and \
                any(r.get(TASK_REF) for r in rows):
            kept = [r for r in rows if not r.get(TASK_REF)]
            return dict(stored, rows=kept)
        return stored
    # A retitled section keeps the user's title. `title` is content too.
    title = stored["title"] if _keep_user_text(stored) or \
        stored.get("title_edited") else fresh["title"]
    merged = dict(stored, title=title, kind=fresh["kind"])

    if fresh["kind"] != stored["kind"]:
        # The catalogue changed this role's kind between versions. The stored
        # content cannot be reinterpreted as the new kind, so take the fresh
        # section wholesale — but only when the user had not edited it.
        return stored if _keep_user_text(stored) else dict(
            fresh, visible=stored.get("visible", True))

    kind = fresh["kind"]
    if kind == KIND_FIELDS:
        merged["fields"] = _merge_fields(
            stored.get("fields") or [], fresh.get("fields") or [], deleted)
    elif kind == KIND_TABLE:
        columns = _merge_columns(stored.get("columns") or [],
                                 fresh.get("columns") or [], deleted)
        merged["columns"] = columns
        merged["rows"] = _merge_rows(stored.get("rows") or [],
                                     fresh.get("rows") or [], columns, deleted)
    elif kind == KIND_TEXT:
        if not _keep_user_text(stored):
            merged["text"] = fresh.get("text", "")
    else:
        merged["items"] = _merge_items(
            stored.get("items") or [], fresh.get("items") or [], deleted)
    return merged


RETIRED_ROLES = ("meeting_points",)


def migrate_action_identity(stored_mom, fresh_sections):
    """Re-key a version-"1" MoM's Action Items onto task-backed identity.

    WHY THIS IS NEEDED. Under "1" an action row's id was a hash of (role,
    position). Under "2" it is a hash of (role, "ref", task id). Nothing
    connects the two, so a straight merge would match nothing: every stored
    row would look deleted-then-recreated, and any user edit on it would be
    silently replaced by fresh AI text. This runs first and hands the merge a
    stored MoM already speaking "2", so the ordinary merge rule then applies
    unchanged.

    MATCHING IS BY ACTION TEXT, and only when it is UNAMBIGUOUS. The Action
    Item cell is the one value a stored row and a fresh row share, since the
    fresh row's owner and deadline may legitimately have changed since. A
    stored row whose text matches exactly one fresh row takes that row's id
    and ref. Anything else — no match, or two fresh rows with the same text —
    is LEFT ALONE at its positional id: it then has no ref, so the projection
    rule below never prunes it, and the worst case is one stale row the user
    can delete rather than an edit attached to the wrong Task. Guessing here
    would mean asserting the wrong owner against the wrong commitment.

    Production makes this safe to ship: at the time of writing all 11 stored
    action rows across 4 MoMs are `source: "ai"`, with zero deleted_ids and
    zero user_edited rows, so a mismatch cannot currently lose user work.

    RETIRED_ROLES are dropped in the same pass. `meeting_points` was a
    generated-only table (its builder produced Discussion Point/Action Item
    from the same extraction) and it was removed from the catalogue; two live
    MoMs still carry one. Since no builder emits the role, the merge would
    keep it forever as a section the AI "stopped producing". It is only
    dropped when the whole section is untouched AI content — a user edit
    anywhere in it means the user has adopted that table and it stays.
    """
    stored = coerce_mom(stored_mom)
    if stored.get("mom_version") != MOM_VERSION_POSITIONAL:
        return stored

    fresh_actions = next((s for s in fresh_sections
                          if s.get("role") == ROLE_ACTIONS), None)
    sections = []
    for section in stored["sections"]:
        if section.get("role") in RETIRED_ROLES and _section_is_pure_ai(section):
            continue
        if section.get("role") == ROLE_ACTIONS and fresh_actions:
            section = _rekey_action_rows(section, fresh_actions)
        sections.append(section)

    stored["sections"] = sections
    stored["mom_version"] = MOM_VERSION
    return stored


def _section_is_pure_ai(section):
    """True when nothing in this section carries a user's mark."""
    if _source(section.get("source")) != SOURCE_AI:
        return False
    for key in ("fields", "rows", "items", "columns"):
        for child in section.get(key) or []:
            if isinstance(child, dict) and \
                    _source(child.get("source")) != SOURCE_AI:
                return False
    return True


def _action_text(row, columns):
    """The Action Item cell, by column LABEL rather than by index.

    The label is what survives a column reorder, and a stored "1" MoM's column
    ids were minted from the same (role, label) pair, so they match the fresh
    ones — but reading the label keeps this correct even if they did not.
    """
    for col in columns or []:
        if str(col.get("label") or "").strip().casefold() == "action item":
            return _s((row.get("cells") or {}).get(col.get("id"))).casefold()
    return ""


def _rekey_action_rows(stored_section, fresh_section):
    stored_cols = stored_section.get("columns") or []
    fresh_cols = fresh_section.get("columns") or []
    fresh_rows = [r for r in fresh_section.get("rows") or [] if r.get(TASK_REF)]

    by_text = {}
    for row in fresh_rows:
        text = _action_text(row, fresh_cols)
        if text:
            by_text.setdefault(text, []).append(row)

    rows = []
    for row in stored_section.get("rows") or []:
        matches = by_text.get(_action_text(row, stored_cols)) or []
        # Exactly one candidate, or leave it positional. Two fresh rows with
        # the same action text carry no information about which Task this
        # stored row meant.
        if len(matches) == 1:
            row = dict(row, id=matches[0]["id"],
                       **{TASK_REF: matches[0][TASK_REF]})
        rows.append(row)
    return dict(stored_section, rows=rows)


def merge_generated(stored_mom, fresh_sections):
    """Fold a fresh generation into the stored MoM, preserving user work.

    Returns a NEW mom dict; neither argument is mutated. The caller stamps
    generated_at/fingerprint afterwards.
    """
    # Re-key a legacy positional MoM BEFORE matching anything. A no-op for a
    # MoM already at the current version, so this costs one version compare on
    # the normal path.
    stored = migrate_action_identity(stored_mom, fresh_sections)
    deleted = set(stored.get("deleted_ids") or [])

    # Match on `role` first (stable across a title rename), falling back to id
    # for a section whose role is "" — i.e. one the user added.
    fresh_by_role = {}
    for section in fresh_sections:
        role = section.get("role") or ""
        if role:
            fresh_by_role[role] = section

    out = []
    used_roles = set()
    for section in stored["sections"]:
        if section["id"] in deleted:
            continue
        role = section.get("role") or ""
        fresh = fresh_by_role.get(role) if role else None
        if fresh is not None:
            used_roles.add(role)
        out.append(_merge_section(section, fresh, deleted))

    # Sections generated for the first time go to the END, so a document the
    # user has already ordered is never reshuffled underneath them.
    for section in fresh_sections:
        role = section.get("role") or ""
        if role and role in used_roles:
            continue
        if section["id"] in deleted:
            continue
        out.append(section)

    merged = dict(stored)
    merged["sections"] = _dedupe_ids(out[:MAX_SECTIONS])
    return merged


def mark_edited(item):
    """Flip an `ai` item to `user_edited`; leave `user_added` alone.

    Every mutating route funnels its text changes through this, so provenance
    can never be forgotten at one call site and remembered at another.
    """
    source = _source(item.get("source"))
    if source == SOURCE_AI:
        return dict(item, source=SOURCE_USER_EDITED)
    return item


# ---------------------------------------------------------------------------
# RENDER — the structured MoM as Markdown.
#
# The output is the derived MIRROR written into
# documents.minutes_of_meeting.content, so it must stay inside the SAME small
# Markdown subset every existing renderer already handles: headings, bold,
# bullets, pipe tables, paragraphs (see lib/document-renderer.tsx,
# lib/export-doc.ts's markdownToHtml, lib/docx-export.ts). Emitting anything
# outside that subset would render as literal punctuation in three places at
# once.
#
# Hidden sections/fields/rows are OMITTED here — "visible: false" means "not
# in the document", which is the whole point of the hide affordance.
# ---------------------------------------------------------------------------
def _escape_cell(text):
    """A pipe inside a cell would end the cell. Escaping is the only
    correct handling — dropping the character would change the content, and
    a table cell containing "A|B" is perfectly legitimate user text."""
    return str(text or "").replace("|", "\\|").replace("\n", " ").strip()


def _render_section(section):
    lines = [f"## {section['title']}", ""]
    kind = section["kind"]

    if kind == KIND_FIELDS:
        fields = [f for f in section.get("fields") or [] if f.get("visible", True)]
        if not fields:
            return []
        for field in fields:
            label = field.get("label") or ""
            value = field.get("value") or ""
            if label and value:
                lines.append(f"**{label}:** {value}")
            elif value:
                lines.append(value)
            elif label:
                lines.append(f"**{label}:**")
            lines.append("")
    elif kind == KIND_TABLE:
        columns = section.get("columns") or []
        rows = [r for r in section.get("rows") or [] if r.get("visible", True)]
        if not columns or not rows:
            return []
        lines.append("| " + " | ".join(_escape_cell(c["label"]) for c in columns) + " |")
        lines.append("|" + "|".join(["---"] * len(columns)) + "|")
        for row in rows:
            cells = row.get("cells") or {}
            lines.append("| " + " | ".join(
                _escape_cell(cells.get(c["id"], "")) for c in columns) + " |")
        lines.append("")
    elif kind == KIND_TEXT:
        text = (section.get("text") or "").strip()
        if not text:
            return []
        lines.append(text)
        lines.append("")
    else:
        items = [i for i in section.get("items") or [] if i.get("visible", True)]
        if not items:
            return []
        for item in items:
            lines.append(f"- {item.get('text', '')}")
        lines.append("")

    return lines


def render_markdown(mom):
    """The structured MoM as the Markdown subset every existing renderer
    understands. A section with no visible content is skipped entirely,
    heading included — an empty "## Decisions" reads as a mistake."""
    mom = coerce_mom(mom)
    out = []
    for section in mom["sections"]:
        if not section.get("visible", True):
            continue
        out.extend(_render_section(section))
    # Collapse the trailing blank so the stored document does not accumulate
    # whitespace every time it is re-rendered.
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out)


def is_empty(mom):
    """True when the MoM would render to nothing — used to decide whether a
    generation actually produced a document worth storing."""
    return not render_markdown(mom).strip()
