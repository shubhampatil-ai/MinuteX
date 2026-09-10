"""ai_schema — strict coercion of Groq JSON into fixed shapes.

Every field and nested key is validated and defaulted, so a malformed model
response can never write a half-broken DynamoDB item. This is the transcribe
Lambda's original coercion layer, moved here so the highlights stage and the
userApi read path validate through exactly the same primitives.

The contract that makes this worth having: a coercer NEVER raises. Whatever the
model returns — a list where an object belongs, a missing key, an integer for a
string, a bare non-JSON string — comes out as the correct shape with empty
defaults. The caller then only has to decide whether an EMPTY result is worth
storing, which is a much simpler question than "is this dict safe to index".
"""
import hashlib
import re

import ai_sanitize
import mom_schema
import speaker_identity

from decimal import Decimal

# Bumped when a prompt or schema change makes previously stored AI output
# stale. Written alongside every generation as `ai_version`, so a revision can
# invalidate old rows lazily (on next read) instead of needing a migration.
AI_VERSION = "1"


def fingerprint(transcript):
    """Stable content hash of a transcript — the document cache's identity.

    The caching rule is "transcript unchanged AND document exists -> serve the
    stored copy", so the cache has to key on the transcript's CONTENT, not on a
    timestamp: reprocessing a recording rewrites updated_at even when the
    transcript comes back byte-identical, and that would throw away a perfectly
    valid document. Conversely a re-transcription that genuinely changes the
    text MUST invalidate every document generated from the old one.

    Truncated to 16 hex chars (64 bits) — collision risk is negligible at any
    plausible scale, and it keeps the attribute small enough to store on every
    document without bloating the row.
    """
    return hashlib.sha256((transcript or "").encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


def s(v):
    """Any -> stripped string; None/dict/list -> ''."""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, bool):
        # Before the numeric branch: bool is an int subclass, and "True" is a
        # far less useful string than "".
        return ""
    if isinstance(v, (int, float, Decimal)):
        return str(v)
    return ""


def slist(v):
    """Any -> [str]; drops non-string / empty-after-strip elements."""
    if not isinstance(v, list):
        return []
    return [x for x in (s(e) for e in v) if x]


def clamp(v, allowed, default):
    """Enum coercer: v must be one of `allowed` (case-sensitive) else `default`."""
    out = s(v)
    return out if out in allowed else default


def obj_list(v, spec, required=None):
    """Any -> [dict]; each element built strictly from `spec`
    (out_key -> (source_key, coercer)). Non-dict elements are skipped.

    `required` names output keys that must be non-empty for the element to be
    kept — an action item with no task text is noise, not data, and dropping it
    here means no downstream consumer has to check.
    """
    out = []
    if not isinstance(v, list):
        return out
    for e in v:
        if not isinstance(e, dict):
            continue
        row = {k: fn(e.get(src)) for k, (src, fn) in spec.items()}
        if required and any(not row.get(k) for k in required):
            continue
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# STAGE 1 — the meeting analysis. FIVE fields, and that is the whole schema:
# title, summary, highlights, tasks, participants.
#
# The older agenda/key_points/decisions/pending_discussions/action_items fields
# were REMOVED. Their ground is covered by `summary` (which the single-pass
# prompt now writes at real length, organized by topic) and `highlights`, and
# by the separate Stage 2 `meeting_highlights` extraction for the structured
# decisions/deadlines/numbers view. Do not reintroduce them here: a coercer
# that emits a field again would silently start writing it back to DynamoDB.
#
# Rows written before the removal may still carry those attributes in DynamoDB.
# Nothing reads them — coerce_analysis drops unknown keys by construction, and
# prompts.analysis_context no longer looks for them.
# ---------------------------------------------------------------------------
PARTICIPANT_SPEC = {
    "speaker": ("speaker", s),
    "summary": ("summary", s),
}

# ---------------------------------------------------------------------------
# The speaker ROSTER — participants derived STRUCTURALLY, not by the model.
#
# The prompt tells the model that only actual speakers are participants, but a
# prompt rule is a prior, not a guarantee: in practice the model kept promoting
# MENTIONED names (a task owner, a manager, a customer) into `participants`,
# which is a data-correctness bug — the app renders that list as "who was in
# this meeting". Names invented that way are indistinguishable to a reader from
# real attendees.
#
# So participation is decided HERE, from the transcript's own diarization. The
# transcript is built by lambda-transcribe-live.build_diarized_text as strict
# "Speaker N: ..." lines, so the set of speakers is a fact we can extract, not
# something to infer. The model's only job for this field is the per-speaker
# contribution blurb; anyone outside the roster is dropped in coercion.
#
# Deliberately NOT applied when a transcript has no speaker labels at all (a
# non-diarized STT result, which build_diarized_text falls back to): with no
# structural evidence there is nothing to enforce, and dropping every
# participant would be worse than trusting the prompt. In that case the roster
# is empty and coercion leaves the model's list alone.
# ---------------------------------------------------------------------------
_SPEAKER_LINE = re.compile(r"^\s*(Speaker\s+[^:\n]{1,40}?)\s*:", re.MULTILINE)


def speaker_roster(transcript):
    """Ordered, de-duplicated speaker labels actually present in `transcript`.

    Matches the "Speaker N:" line format build_diarized_text emits. Returns []
    for a transcript with no labels — the caller then skips enforcement rather
    than filtering everything away.
    """
    seen, out = set(), []
    for label in _SPEAKER_LINE.findall(transcript or ""):
        # Collapse internal whitespace so "Speaker  0" and "Speaker 0" are one.
        norm_label = " ".join(label.split())
        k = norm_label.lower()
        if k not in seen:
            seen.add(k)
            out.append(norm_label)
    return out


def _roster_filtered(participants, roster, speaker_names=None):
    """Keep only participants whose speaker is in `roster`, in ROSTER order.

    Roster order (not model order) so the list reads as the meeting's own
    speaker order, and every roster speaker appears even if the model omitted
    one — a speaker with turns in the transcript IS a participant whether or not
    the model bothered to describe them.

    Matching is on the NORMALIZED label, not the raw string. The roster handed
    to the prompt is authoritative about WHO spoke, but it is only guidance
    about how to spell them back, and an exact-string lookup made that spelling
    load-bearing: a model answering "0", "speaker_0" or "Speaker  0" for a
    roster of "Speaker 0" missed on every entry, and each speaker silently took
    the "" default. The visible symptom is a meeting where every speaker is
    listed and every contribution blurb is blank, which is indistinguishable
    from a model that wrote no blurbs at all. Observed in production on
    code-switched (Hindi/Marathi) meetings, where label drift is common.

    `speaker_names` (the row's rename map, {"0": "Yuvraj Sir"}) adds the second
    miss: a model that answers with the HUMAN name it read off a renamed
    transcript. Those names are resolved back to their label so the blurb lands
    on the right speaker instead of being dropped.

    Widening the MATCH does not widen the FILTER — the output is still exactly
    the roster, in roster order, so a merely-mentioned name still cannot become
    an attendee. That guarantee is the whole point of this function and is
    unchanged; only the lookup that finds an existing blurb got more forgiving.
    """
    if not roster:
        return participants

    def _key(label):
        return mom_schema.normalize_speaker_label(label).strip().casefold()

    # Human name -> roster label, so a model answering "Yuvraj Sir" for
    # "Speaker 1" still lands. Built only from the roster's own speakers: a
    # name outside the roster must not resolve to anyone.
    by_name = {}
    for label in roster:
        named = (speaker_names or {}).get(
            mom_schema.normalize_speaker_label(label))
        if named and str(named).strip():
            by_name.setdefault(str(named).strip().casefold(), label)

    by_label, unmatched = {}, []
    for p in participants:
        raw = p.get("speaker") or ""
        k = _key(raw)
        if not k:
            continue
        # A human name resolves to that speaker's label; anything else is
        # already a label (or a name nobody is mapped to, which stays unmatched
        # and is reported below).
        k = _key(by_name.get(k, k))
        if k not in by_label:
            by_label[k] = p
        else:
            unmatched.append(raw)

    out = []
    for label in roster:
        got = by_label.pop(_key(label), None)
        out.append({"speaker": label,
                    "summary": (got or {}).get("summary", "")})

    # Anything left in by_label named someone outside the roster. Dropping it
    # is correct (that is the anti-hallucination filter doing its job), but a
    # DROP CARRYING A SUMMARY is also the signature of the label-drift bug
    # above, so it is logged rather than silently discarded — the failure that
    # needed a DynamoDB read to diagnose leaves a trace now.
    dropped = [p.get("speaker") or "" for p in by_label.values()
               if str(p.get("summary") or "").strip()]
    dropped += [lbl for lbl in unmatched if lbl]
    if dropped:
        print(f"[ai_schema] roster: dropped {len(dropped)} participant "
              f"summary/summaries not matching the roster {roster}: {dropped}")
    blank = sum(1 for p in out if not str(p.get("summary") or "").strip())
    if blank and participants:
        print(f"[ai_schema] roster: {blank}/{len(out)} speaker(s) have no "
              "contribution summary after matching")
    return out

def _resolve_task_speakers(tasks, roster=None, speaker_names=None):
    """Force every `assignee_speaker_id` to a canonical id, or to "".

    THE BUG THIS CLOSES. transcript_store.as_labelled_lines renders the user's
    names INTO the lines the model reads ("Rahul: I'll send it"), while the
    prompt asks it to copy a "Speaker 0" label that is no longer on the page.
    So on a RENAMED meeting the model answers with the name, and `s` stored it
    verbatim — a name sitting in an id field. That value matches no
    participant row (normalize("Rahul") is "Rahul", not "0"), so the task
    never resolves to a contact, never fires TASK_ASSIGNED, and can never be
    corrected by a later rename, because nothing records that it ever meant
    speaker "0". Renaming a meeting therefore made task ownership WORSE.

    `participants` already survives this — _roster_filtered resolves a
    human name back to its label. This gives tasks the same treatment, at the
    one place a task's speaker enters the system.

    UNRESOLVABLE BECOMES "", WHICH IS A SUPPORTED VALUE. The prompt already
    documents "" as correct and expected whenever the owner is not a specific
    speaker, and _seed_ai_tasks treats a task with no speaker as unresolved —
    a visible, fixable gap. A wrong id is invisible and assigns real work to
    the wrong person, so an ambiguous name resolves to nothing (see
    speaker_identity.resolve_legacy_speaker_reference's ambiguity rule).

    `assignee` (the spoken NAME) is untouched: it is legitimately a name, it
    is what the confidence gate and the fingerprint read, and rewriting it
    would change task identity for every already-seeded row.
    """
    roster_ids = [speaker_identity.normalize_speaker_id(r)
                  for r in (roster or [])]
    roster_ids = [r for r in roster_ids if r] or None
    for t in tasks:
        raw = t.get("assignee_speaker_id")
        if not str(raw or "").strip():
            t["assignee_speaker_id"] = ""
            continue
        resolved = speaker_identity.resolve_legacy_speaker_reference(
            raw, roster_ids, speaker_names)
        if not resolved and str(raw or "").strip():
            print("[ai_schema] task: dropped unresolvable "
                  f"assignee_speaker_id {raw!r} (roster {roster_ids})")
        t["assignee_speaker_id"] = resolved
    return tasks


CONFIDENCE_HIGH = "high"
CONFIDENCE_MEDIUM = "medium"
CONFIDENCE_LOW = "low"
CONFIDENCE_LEVELS = (CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW)


def coerce_confidence(value):
    """An AI confidence label reduced to the fixed enum, or "" for no claim.

    Case- and whitespace-insensitive, unlike the generic `clamp`: the model is
    asked for lowercase and usually complies, but "High" and " high " are the
    SAME claim and dropping them threw away a real signal — a task whose
    confidence silently became "" is indistinguishable from one the model never
    scored, and the gating below would then treat a high-confidence extraction
    as unscored.

    A NUMBER is deliberately refused. Some models answer 0.87 despite the
    prompt; mapping that onto a band would be inventing a threshold nobody
    chose, and the whole point of the 3-value enum is that it carries only what
    the model actually meant. "" (no claim) is the honest reading.
    """
    if isinstance(value, bool) or isinstance(value, (int, float)):
        return ""
    text = s(value).strip().lower()
    return text if text in CONFIDENCE_LEVELS else ""


# `assignee_speaker_id`, `confidence` and `evidence` are NOT decoration: the
# Tasks layer reads all three off each raw row (_seed_ai_tasks). Because
# obj_list builds every element STRICTLY from this spec, a field missing here
# is dropped before the seeder ever sees it — which is exactly how the
# speaker-resolution chain (assignee_speaker_id -> Contact) came to be dead
# code despite both ends being implemented. Add a field to the prompt and to
# this spec together, or the prompt's output is silently discarded.
#
# assignee_speaker_id holds a transcript LABEL ("Speaker 0"), never a name —
# that is the join key _seed_ai_tasks looks up in the meeting's speaker map and
# _resolve_tasks_for_speaker matches on later.
TASK_SPEC = {
    "task": ("task", s),
    "assignee": ("assignee", s),
    # Shape-coerced only here; the ROSTER-aware pass is _resolve_task_speakers
    # below, which is the one that can tell a real speaker from a name. Same
    # split as evidence_segment_ids: obj_list cannot see the meeting.
    "assignee_speaker_id": ("assignee_speaker_id", s),
    "due_date": ("due_date", s),
    "priority": ("priority", lambda x: clamp(
        x, {"Low", "Medium", "High"}, "") or None),
    # A fixed enum, not a free number: a model asked for a 0-1 score returns
    # noise dressed as precision. Unrecognised -> "" (no claim made).
    "confidence": ("confidence", lambda x: coerce_confidence(x)),
    "evidence": ("evidence", s),
    # The verbatim quote above says WHAT created the task; this says WHERE it
    # is, so the app can jump to that moment of the audio. Coerced permissively
    # here (shape only) and re-validated against the real segment list by
    # validate_task_evidence once the transcript is known — obj_list has no
    # access to it, and a task must never be dropped over a bad reference.
    "evidence_segment_ids": ("evidence_segment_ids",
                             lambda v: _evidence_ids(v, None)),
}

# ---------------------------------------------------------------------------
# THE DYNAMIC OVERVIEW — the primary meeting understanding.
#
# Replaces the fixed `summary` prose + `highlights` list with sections the
# MODEL chooses per meeting. A technical review yields "Architecture Concerns"
# and "Panel Feedback"; a sales call yields "Pricing" and "Objections". There
# is deliberately NO section taxonomy anywhere in this file or in the prompt —
# a fixed list is exactly what this replaces, and adding one "just as a
# fallback" would reintroduce the template the feature exists to remove.
#
# What IS enforced here is everything that is NOT a content decision:
#
#   * BOUNDS. The overview rides on the DynamoDB row (see the 400 KB limit
#     transcript_store.py exists because of), so section count, text length,
#     item count and total size are capped. The caps below are set well above
#     what a real meeting produces — they protect storage, they do not shape
#     the answer. A model that wants 4 sections and one that wants 9 are both
#     under the cap; only a runaway response is trimmed.
#   * EMPTINESS. A section with no content is dropped rather than stored. An
#     empty "Risks" heading reads to a user as "risks were considered and
#     there were none", which is a claim the transcript may not support — the
#     same reason coerce_highlights drops a row missing its load-bearing field.
#   * IDENTITY. Every section gets a stable id, so the app can key a list, and
#     a user edit or a comment can point at one section across regenerations.
#   * PROVENANCE. `source` records who wrote it, mirroring mom_schema's SOURCE_*
#     values — the field that makes a future "user edited this section" merge
#     decidable without a migration.
# ---------------------------------------------------------------------------
OVERVIEW_KIND_TEXT = "text"
OVERVIEW_KIND_LIST = "list"
OVERVIEW_KINDS = (OVERVIEW_KIND_TEXT, OVERVIEW_KIND_LIST)

# Provenance. Only "ai" is ever written by a generation; the value exists so a
# later user-edit path has somewhere to record itself.
OVERVIEW_SOURCE_AI = "ai"

MAX_OVERVIEW_SECTIONS = 12
MAX_OVERVIEW_TITLE_CHARS = 80
MAX_OVERVIEW_TEXT_CHARS = 4_000
MAX_OVERVIEW_ITEMS = 20
MAX_OVERVIEW_ITEM_CHARS = 600
MAX_OVERVIEW_EVIDENCE = 8
# Total budget across every section. Sized against the row's other occupants
# (~10 KB of analysis on a slim row) with the 400 KB item ceiling far above —
# a normal overview lands around 2-4 KB, so this only ever catches a runaway.
MAX_OVERVIEW_CHARS = 24_000

_SEGMENT_ID_RE = re.compile(r"^seg_\d+$")


def _evidence_ids(v, valid_ids=None):
    """Model-supplied segment references -> the subset that actually exists.

    NEVER trusted as given. A model asked for evidence will happily return
    "seg_999999" for a 40-segment meeting, and an ID that resolves to nothing
    is worse than no ID: the app would offer a "jump to this moment" action
    that silently goes nowhere.

    `valid_ids` is the set derived from the real transcript. When it is None
    (no timestamps available — a non-diarized STT result, or an old row whose
    segments could not be read) the shape is still enforced but membership
    cannot be, so well-formed IDs pass through; there is nothing to check them
    against, and dropping them all would strip grounding from every legacy
    recording. An INVALID id is always dropped, never repaired and never
    allowed to fail the surrounding section — the section's text is useful
    with no evidence, and useless if a bad reference discards it.
    """
    out, seen = [], set()
    for raw in (v if isinstance(v, list) else []):
        ident = s(raw)
        if not ident or not _SEGMENT_ID_RE.match(ident):
            continue
        if valid_ids is not None and ident not in valid_ids:
            continue
        if ident in seen:
            continue
        seen.add(ident)
        out.append(ident)
        if len(out) >= MAX_OVERVIEW_EVIDENCE:
            break
    return out


def _overview_section(raw, index, valid_ids=None):
    """One coerced section, or None when it carries no content.

    `kind` follows the CONTENT, not the model's label for it: a section that
    declared itself "text" but returned only items is stored as a list, and
    vice versa. The model gets the harder half (what to say) and this decides
    the half that has an objectively right answer, so the app never has to
    render a "text" section whose `content` is empty.
    """
    if not isinstance(raw, dict):
        return None

    # SANITIZED AT COERCION, not at render. These three fields are the only
    # ones a user READS, and the model composes them while looking at a
    # transcript labelled `[seg_N] Speaker: ...` — so an id occasionally lands
    # in the prose ("as noted in seg_12"). Cleaning here means it never enters
    # DynamoDB, so an existing row is fixed on its next regeneration rather
    # than needing every reader to remember to strip it.
    #
    # `evidence_segment_ids` below is deliberately NOT sanitized: that is the
    # field the ids belong in, and the app turns it into a deep-link.
    title = ai_sanitize.sanitize_ai_user_output(
        s(raw.get("title")), "overview:title")[:MAX_OVERVIEW_TITLE_CHARS]
    content = ai_sanitize.sanitize_ai_user_output(
        s(raw.get("content")), "overview:content")[:MAX_OVERVIEW_TEXT_CHARS]
    items = [ai_sanitize.sanitize_ai_user_output(i, "overview:item")
             [:MAX_OVERVIEW_ITEM_CHARS]
             for i in slist(raw.get("items"))][:MAX_OVERVIEW_ITEMS]
    # An item that was nothing BUT an id is empty now; a blank bullet is worse
    # than a missing one.
    items = [i for i in items if i.strip()]

    # No title, or nothing to say under it -> not a section. This is the rule
    # that keeps filler out: a model padding to look thorough emits exactly
    # this shape (a heading with nothing under it), and it is dropped here.
    if not title or (not content and not items):
        return None

    kind = clamp(raw.get("kind"), set(OVERVIEW_KINDS), "")
    if items and not content:
        kind = OVERVIEW_KIND_LIST
    elif content and not items:
        kind = OVERVIEW_KIND_TEXT
    elif not kind:
        # Both present and no usable declaration: prefer the list, which is
        # the denser rendering, and keep the prose as the section's lead-in.
        kind = OVERVIEW_KIND_LIST

    # Positional id. Same reasoning as transcript_store.segment_id: derived
    # from order, so it is stable for a given generation without the model
    # being trusted to invent unique keys (it reliably repeats "section_1").
    return {
        "id": f"section_{index}",
        "title": title,
        "kind": kind,
        "content": content,
        "items": items,
        "source": OVERVIEW_SOURCE_AI,
        "evidence_segment_ids": _evidence_ids(
            raw.get("evidence_segment_ids"), valid_ids),
    }


def empty_overview():
    """Fresh overview; never shares mutable lists."""
    return {"sections": []}


def coerce_overview(obj, valid_ids=None):
    """Strict coerce the model's overview into the bounded section list.

    Accepts either {"sections": [...]} or a bare list, because those are the
    two shapes a model actually returns for this field and rejecting the second
    would throw away a perfectly good answer over an envelope.

    De-duplicates on TITLE: the failure mode worth guarding is a model emitting
    "Next Steps" twice with the content split across both, which renders as two
    half-empty cards. The first occurrence keeps its position and absorbs the
    second's items — the same "fuller copy wins" rule merge_highlights uses.
    """
    if isinstance(obj, dict):
        raw_sections = obj.get("sections")
    elif isinstance(obj, list):
        raw_sections = obj
    else:
        return empty_overview()

    if not isinstance(raw_sections, list):
        return empty_overview()

    sections, by_title, total = [], {}, 0
    for raw in raw_sections:
        section = _overview_section(raw, len(sections), valid_ids)
        if section is None:
            continue

        key = section["title"].strip().lower()
        prior = by_title.get(key)
        if prior is not None:
            # Fold into the copy already holding this title.
            if not prior["content"]:
                prior["content"] = section["content"]
            for item in section["items"]:
                if len(prior["items"]) >= MAX_OVERVIEW_ITEMS:
                    break
                if item not in prior["items"]:
                    prior["items"].append(item)
            if prior["items"] and prior["kind"] == OVERVIEW_KIND_TEXT \
                    and not prior["content"]:
                prior["kind"] = OVERVIEW_KIND_LIST
            continue

        size = len(section["title"]) + len(section["content"]) + \
            sum(len(i) for i in section["items"])
        # Budget check AFTER the de-dupe fold, so a duplicate can't consume it.
        if total + size > MAX_OVERVIEW_CHARS:
            break
        total += size

        by_title[key] = section
        sections.append(section)
        if len(sections) >= MAX_OVERVIEW_SECTIONS:
            break

    return {"sections": sections}


def overview_empty(o):
    """True when the overview has no sections worth storing.

    Same role as highlights_empty: an all-empty result is indistinguishable
    from "never generated", and storing it would make a cache serve emptiness
    forever instead of regenerating.
    """
    if not isinstance(o, dict):
        return True
    return not o.get("sections")


def overview_text(o, limit=None):
    """The overview flattened to plain text — for prompts and legacy readers.

    ONE definition, because three different callers need this and three
    slightly different flattenings would be three slightly different meetings.
    Used by prompts.analysis_context (the Chat/document digest) and by the
    compatibility `summary` derivation.
    """
    if not isinstance(o, dict):
        return ""
    parts = []
    for section in o.get("sections") or []:
        if not isinstance(section, dict):
            continue
        title = s(section.get("title"))
        if not title:
            continue
        block = [f"{title}:"]
        content = s(section.get("content"))
        if content:
            block.append(content)
        for item in slist(section.get("items")):
            block.append(f"- {item}")
        parts.append("\n".join(block))
    text = "\n\n".join(parts)
    if limit and len(text) > limit:
        text = text[:limit].rstrip()
    return text


def _dedupe_tasks(tasks):
    """Same task text (case/space-insensitive) kept once — the first,
    fullest-looking occurrence wins, same principle as merge_highlights'
    "fuller copy wins" rule below."""
    seen = {}
    out = []
    for t in tasks:
        norm = t.get("task", "").strip().lower()
        if not norm:
            continue
        if norm not in seen:
            seen[norm] = t
            out.append(t)
        else:
            prior = seen[norm]
            for field in ("assignee", "assignee_speaker_id", "due_date",
                          "priority", "confidence", "evidence"):
                if t.get(field) and not prior.get(field):
                    prior[field] = t[field]
    return out


def empty_analysis():
    """Fresh dict with every field; never shares mutable lists."""
    return {
        "title": "",
        "overview": empty_overview(),
        "tasks": [],
        "participants": [],
    }


def coerce_analysis(obj, roster=None, valid_ids=None, speaker_names=None):
    """Strict coerce a parsed Groq object into the fixed analysis schema.

    Builds the result key-by-key from the four known fields, so a model that
    still emits a removed field (summary, highlights, agenda, …) has it DROPPED
    here rather than passed through to the caller's DynamoDB write.

    `valid_ids` is the set of transcript segment IDs that actually exist (see
    transcript_store.with_segment_ids). Evidence references — on overview
    sections and on tasks alike — are checked against it and the invalid ones
    removed. Pass None when the segment list is unavailable; the shape is still
    enforced, only membership is not.

    `roster` — the speaker labels actually present in the transcript (see
    speaker_roster). When given and non-empty, `participants` is REPLACED by the
    roster: a name the model volunteered that never had a speaker turn is
    dropped, and a speaker the model skipped is added back with an empty blurb.
    Omit it (or pass []) to keep the model's own list, which is the right
    behaviour for a transcript with no speaker labels to check against.

    Note `tasks` is deliberately NOT roster-filtered — an assignee may be a
    non-attendee, a team or an external party. Participation and responsibility
    are independent (see prompts.SUMMARY_SYSTEM's TASK ASSIGNMENT RULE).
    """
    if not isinstance(obj, dict):
        return empty_analysis() if not roster else {
            **empty_analysis(),
            "participants": _roster_filtered([], roster, speaker_names),
        }
    tasks = obj_list(obj.get("tasks"), TASK_SPEC, required=("task",))
    if valid_ids is not None:
        # Re-check what TASK_SPEC could only shape-check: obj_list has no
        # access to the transcript. A task NEVER fails over a bad reference —
        # the reference is dropped and the commitment is kept.
        for t in tasks:
            t["evidence_segment_ids"] = _evidence_ids(
                t.get("evidence_segment_ids"), valid_ids)
    _resolve_task_speakers(tasks, roster, speaker_names)
    participants = obj_list(obj.get("participants"), PARTICIPANT_SPEC,
                            required=("speaker",))
    return {
        "title": s(obj.get("title")),
        "overview": coerce_overview(obj.get("overview"), valid_ids),
        "tasks": _dedupe_tasks(tasks),
        "participants": _roster_filtered(participants, roster or [],
                                         speaker_names),
    }


def merge_analyses(partials, roster=None, speaker_names=None):
    """Fold per-chunk analyses into one, de-duplicated, order-preserving.

    Used as the reduce step's input, and as the final result if the reduce call
    itself fails — a concatenated brief beats no brief at all. Only reached via
    the map_reduce OVERFLOW path, for a transcript that genuinely exceeds the
    model's context window; a meeting that fits is analyzed in one pass and
    never comes through here.

    The OVERVIEW merges by section TITLE, which is the only join key available:
    each chunk chose its own sections, and two chunks that both wrote "Pricing"
    are describing one topic split across the meeting, not two topics. Sections
    only one chunk produced are kept as-is — a subject discussed once is still a
    subject. Content is concatenated rather than re-summarized, because merging
    prose without the transcript in hand can only lose information; the reduce
    CALL rewrites it properly when it succeeds, and this is the fallback for
    when it does not.
    """
    merged = empty_analysis()
    speakers = set()
    sections_by_title = {}
    merged_sections = []

    for p in partials:
        if not isinstance(p, dict):
            continue
        for section in (p.get("overview") or {}).get("sections") or []:
            if not isinstance(section, dict):
                continue
            title = s(section.get("title"))
            if not title:
                continue
            key = title.strip().lower()
            prior = sections_by_title.get(key)
            if prior is None:
                prior = {**section, "title": title}
                prior["items"] = list(section.get("items") or [])
                prior["evidence_segment_ids"] = list(
                    section.get("evidence_segment_ids") or [])
                sections_by_title[key] = prior
                merged_sections.append(prior)
                continue
            content = s(section.get("content"))
            if content and content not in prior.get("content", ""):
                prior["content"] = (prior.get("content", "") + " " + content).strip()
            for item in section.get("items") or []:
                if item not in prior["items"]:
                    prior["items"].append(item)
            for ident in section.get("evidence_segment_ids") or []:
                if ident not in prior["evidence_segment_ids"]:
                    prior["evidence_segment_ids"].append(ident)
        # tasks[] is collected RAW here (every occurrence across every
        # chunk, no pre-filtering) and de-duplicated once at the end via
        # _dedupe_tasks — which, unlike a plain "keep first occurrence"
        # rule, also fills in a later chunk's assignee/due_date/priority
        # onto an earlier chunk's bare mention of the same task (see
        # prompts.SUMMARY_REDUCE_SYSTEM's "tasks merges duplicate
        # commitments... keeping whichever segment named an assignee").
        # Pre-filtering here would silently discard exactly that fuller
        # later copy before _dedupe_tasks ever saw it.
        merged["tasks"].extend(p.get("tasks", []))
        # One entry per speaker; keep the first non-empty description.
        for pt in p.get("participants", []):
            spk = (pt.get("speaker") or "").strip()
            if spk and spk.lower() not in speakers:
                speakers.add(spk.lower())
                merged["participants"].append(pt)

    # Re-coerce so the merged sections get fresh positional ids and are
    # re-bounded: concatenating chunks can push a section past the per-section
    # and total budgets that coerce_overview enforces.
    merged["overview"] = coerce_overview({"sections": merged_sections})
    merged["tasks"] = _dedupe_tasks(merged["tasks"])
    merged["title"] = next((p.get("title") for p in partials
                            if isinstance(p, dict) and p.get("title")), "")
    # Same structural guarantee as the single-pass path: a name that never had a
    # speaker turn is not a participant, however many segments volunteered it.
    merged["participants"] = _roster_filtered(merged["participants"],
                                              roster or [], speaker_names)
    return merged


# ---------------------------------------------------------------------------
# STAGE 2 — meeting highlights.
# ---------------------------------------------------------------------------
HL_DECISION_SPEC = {
    "decision": ("decision", s),
    "context": ("context", s),
    # WHO decided, as a canonical speaker id — the structured half of
    # attribution, so a rename moves the displayed name without the decision
    # TEXT having to be rewritten. Shape-coerced here; resolved against the
    # roster by coerce_highlights, which is where the meeting is known.
    #
    # OPTIONAL BY CONSTRUCTION. Not in `required`, so a decision without one
    # is kept exactly as before, and every stored decision written before
    # this field existed still coerces and still renders. "" is the ordinary
    # value for a collective decision.
    "speaker_id": ("speaker_id", s),
}
HL_DEADLINE_SPEC = {
    "what": ("what", s),
    "when": ("when", s),
}
# ---------------------------------------------------------------------------
# The structured extraction. FOUR sections, down from six.
#
# This is no longer a user-facing "meeting highlights" view — the Dynamic
# Overview above is what a reader now sees. What survives here is only the
# TYPED data that machine consumers need in rows rather than prose, and each
# remaining section is kept because something reads it:
#
#   decisions      -> mom_schema._build_decisions      (MoM "Decisions")
#   deadlines      -> mom_schema._build_followups      (MoM "Follow-ups") AND
#                     app calendar.tsx (deadline markers on the month grid)
#   open_questions -> mom_schema._build_followups      (MoM "Follow-ups")
#
# `action_items` was REMOVED because it DUPLICATED `tasks`. Measured on a real
# meeting, the two came back with identical task text, owners and deadlines,
# three for three — the model was extracting the same commitments twice, in one
# already-truncating reply, and the copies were free to disagree. `tasks` is
# the survivor: it alone carries evidence, confidence and assignee_speaker_id,
# and it is what the Tasks screen, notifications and the Salesforce push read.
# Its MoM consumer (_build_points, the "Meeting Points" table) went with it —
# that table printed the same string in both its "Discussion Point" and
# "Action Item" columns, so it was a duplicate of the Action Items table.
# mom_schema._build_actions still FALLS BACK to a stored action_items list for
# a row older than task seeding, which is why old rows keep rendering.
#
# `important_numbers` and `risks` were REMOVED: nothing consumed them in rows.
# Their content is not lost — a meeting's figures and risks now land in the
# Overview, where they read as part of the meeting's own story instead of as
# two more template sections. Do not reintroduce them here without a consumer;
# a coercer that emits a field starts writing it back to DynamoDB.
#
# The tuple ORDER is still load-bearing: highlights_empty and merge_highlights
# both iterate it, and old rows carrying the two removed keys stay readable
# because coercion builds the result key-by-key and simply never looks at them.
# ---------------------------------------------------------------------------
HIGHLIGHT_SECTIONS = ("decisions", "deadlines", "open_questions")


def empty_highlights():
    return {k: [] for k in HIGHLIGHT_SECTIONS}


def coerce_highlights(obj, roster=None, speaker_names=None):
    """Strict coerce a parsed Groq object into the meeting_highlights schema.

    Elements missing their load-bearing field are dropped: a deadline with no
    "what" or a number with no "value" carries no information, and keeping it
    would render as an empty row in the workspace.

    `roster`/`speaker_names` resolve each decision's `speaker_id` the same way
    tasks are resolved — a name or a display label becomes a canonical id, and
    anything ambiguous becomes "". Both default to None so every existing
    caller keeps working; without them the field is shape-checked only.
    """
    if not isinstance(obj, dict):
        return empty_highlights()
    decisions = obj_list(obj.get("decisions"), HL_DECISION_SPEC,
                         required=("decision",))
    roster_ids = [speaker_identity.normalize_speaker_id(r)
                  for r in (roster or [])]
    roster_ids = [r for r in roster_ids if r] or None
    for d in decisions:
        d["speaker_id"] = speaker_identity.resolve_legacy_speaker_reference(
            d.get("speaker_id"), roster_ids, speaker_names)
    return {
        "decisions": decisions,
        "deadlines": obj_list(obj.get("deadlines"), HL_DEADLINE_SPEC,
                              required=("what",)),
        "open_questions": slist(obj.get("open_questions")),
    }


def merge_highlights(partials):
    """Fold per-chunk highlights into one, de-duplicated, order-preserving.

    De-dupes on the load-bearing field of each section (the decision text, the
    task text, the number's value) rather than the whole dict, because a later
    segment usually restates the same item with MORE detail — so the first
    mention wins position and we keep whichever copy has an owner/deadline.
    """
    merged = empty_highlights()
    seen = {k: {} for k in HIGHLIGHT_SECTIONS}

    def _key(section, item):
        if section == "decisions":
            return (item.get("decision") or "").strip().lower()
        if section == "deadlines":
            return (item.get("what") or "").strip().lower()
        return str(item).strip().lower()

    for p in partials:
        if not isinstance(p, dict):
            continue
        for section in ("open_questions",):
            for item in p.get(section, []):
                norm = item.strip().lower()
                if norm and norm not in seen[section]:
                    seen[section][norm] = True
                    merged[section].append(item)
        for section in ("decisions", "deadlines"):
            for item in p.get(section, []):
                if not isinstance(item, dict):
                    continue
                k = _key(section, item)
                if not k or (isinstance(k, str) and not k.strip()):
                    continue
                prior = seen[section].get(k)
                if prior is None:
                    seen[section][k] = item
                    merged[section].append(item)
                    continue
                # Already have it — fill in blanks from this fuller copy.
                for field, value in item.items():
                    if value and not prior.get(field):
                        prior[field] = value
    return merged


def highlights_empty(h):
    """True when every section is empty — i.e. there is nothing to show.

    Used to decide whether a generation is worth storing: an all-empty result
    is indistinguishable from "never generated", and storing it would make the
    cache serve emptiness forever.
    """
    if not isinstance(h, dict):
        return True
    return not any(h.get(k) for k in HIGHLIGHT_SECTIONS)


# ---------------------------------------------------------------------------
# STAGE 2b — CRM record identifier.
#
# Coerced HARDER than the other stages, because this value selects which
# Salesforce record a meeting is pushed onto: a hallucinated identifier doesn't
# render as a bad UI row, it writes real notes onto the wrong customer's record.
# So the prompt's "never invent" rule is ALSO enforced here in code — see
# _ident_grounded below. Prompt rules are a strong prior, not a guarantee; this
# is the guarantee.
#
# Object-agnostic: the same coercion serves a site visit number, a lead email or
# an opportunity number. Nothing here knows which object it is validating for.
#
# Two steps, in this order and not the other: GROUND the model's answer against
# its own evidence quote (is it real?), then NORMALIZE spoken digit words into
# digits (is it in the form Salesforce stores?). See normalize_crm_identifier.
# ---------------------------------------------------------------------------
CRM_IDENT_CONFIDENCE = ("explicit", "probable", "none")

# Confidence label -> the numeric score stored/shown. The model gives a coarse
# label (it is poorly calibrated at emitting floats); the mapping to a number
# lives here so the UI and the CRM-link record agree on one scale.
CRM_IDENT_CONFIDENCE_SCORE = {"explicit": 0.95, "probable": 0.6, "none": 0.0}

# Comparing an extracted number against its evidence quote: strip everything
# that is formatting rather than identity, so "SV-10245" still matches an
# evidence quote that says "SV 10245" or "sv10245".
_IDENT_NOISE = str.maketrans("", "", " -_/.,#:")


def _ident_norm(v):
    return (v or "").translate(_IDENT_NOISE).lower()


def _ident_grounded(value, evidence):
    """True when `value` appears in `evidence` as a COMPLETE reference.

    A plain substring test is not enough. "SV-102" is a substring of the
    evidence "SV-10245", so a model that truncated the number would pass a
    naive check — and a truncated number is not a harmless typo here, it
    resolves to a DIFFERENT Salesforce record. Same for a value that is
    really a slice of a phone number.

    The boundary test looks only for DIGITS either side of the match, not for
    non-alphanumerics: both strings have already had their separators stripped
    by _ident_norm (so "is SV-10245." normalizes to "issv10245" and there is no
    surviving space to anchor against), and it is a longer RUN OF DIGITS that
    signals a fragment. Adjacent letters are just the sentence's words.
    """
    n, ev = _ident_norm(value), _ident_norm(evidence)
    if not n or not ev:
        return False
    start = 0
    while True:
        i = ev.find(n, start)
        if i < 0:
            return False
        before = ev[i - 1] if i > 0 else ""
        after = ev[i + len(n)] if i + len(n) < len(ev) else ""
        if not before.isdigit() and not after.isdigit():
            return True
        start = i + 1


# --- Spoken-digit normalization -------------------------------------------
#
# Speech-to-text writes dictated identifiers as WORDS: a site visit read aloud
# as "S V one zero zero four" transcribes to "SV one zero zero four", and the
# model faithfully extracts that (it is what the transcript says, so it passes
# the grounding check above — correctly). But Salesforce stores "SV1004", so the
# SOQL lookup finds nothing and the meeting silently fails to link.
#
# So the fix belongs HERE, between extraction and lookup, not in the prompt: the
# model's job is to quote the transcript accurately, and it did.
#
# DELIBERATELY LIMITED to the ten single-digit words, and only where they form
# a RUN. Identifiers get dictated digit-by-digit ("one zero zero four"), and it
# is that run of consecutive digit words — not any one word alone — that marks a
# spelled-out identifier. One "five" in isolation is ordinary speech ("phase
# five", "five units"), so converting it would be a guess.
#
# Compound number words are never reinterpreted, and they BLOCK a run rather
# than joining it: "twenty four" could mean the digits 2-4, the number 24, or a
# year, and the identifier alone cannot say which. Guessing would turn a semantic
# number into a wrong record reference — the exact failure this module exists to
# prevent. An unconvertible value is left as-is; the lookup then reports
# not_found, which the user can see and correct.
_SPOKEN_DIGITS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9",
}

# "oh"/"o" is how a zero gets voiced mid-sequence ("SV one oh oh four"). Only
# ever read as a zero INSIDE a run of real digit words, never on its own.
_SPOKEN_ZERO_ALIASES = {"oh", "o"}

# Number words that are part of a compound/semantic number. Their presence next
# to a digit word means "this is a quantity being spoken", not a dictated id, so
# they terminate a run instead of extending it.
_COMPOUND_NUMBER_WORDS = {
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen", "twenty", "thirty", "forty", "fifty",
    "sixty", "seventy", "eighty", "ninety", "hundred", "thousand", "million",
    "billion", "dozen",
}

# A run of letters, a run of digits, a run of whitespace, or any single other
# character. Splitting this way keeps every separator (hyphen, slash, "#")
# exactly where it was — only whole words are ever candidates for conversion.
_IDENT_TOKENS = re.compile(r"[A-Za-z]+|[0-9]+|\s+|.", re.DOTALL)

# How many consecutive spoken digits it takes to call something dictated. Two:
# "SV one two" is a spelled-out id, while a lone "five" is just a word.
_SPOKEN_RUN_MIN = 2


def normalize_crm_identifier(value):
    """Spoken-digit words in an extracted identifier -> the digits they name.

        "SV one zero zero four" -> "SV1004"
        "ABC four five six"     -> "ABC456"
        "SV1004"                -> "SV1004"        (already numeric: unchanged)
        "SV twenty four"        -> "SV twenty four" (compound: never guessed)
        "Priya Sharma"          -> "Priya Sharma"   (no digit words: unchanged)

    Generic — no object, prefix or field name is known here. Alphabetic
    prefixes, separators and letter case are all preserved exactly; only a RUN
    of whole words naming single digits is replaced, and only the whitespace
    inside and immediately around that run is closed up.

    Returns the input unchanged (stripped) whenever there is nothing to convert,
    so a caller can always use the result, and a value that was never dictated
    digit-by-digit is never touched.
    """
    text = s(value)
    if not text:
        return text

    tokens = _IDENT_TOKENS.findall(text)

    # What each word token could be, ignoring position: a digit word, a zero
    # alias, a compound number word, or none of those.
    def _kind(tok):
        if not tok.isalpha():
            return None
        low = tok.lower()
        if low in _SPOKEN_DIGITS:
            return "digit"
        if low in _SPOKEN_ZERO_ALIASES:
            return "alias"
        if low in _COMPOUND_NUMBER_WORDS:
            return "compound"
        return None

    kinds = [_kind(t) for t in tokens]
    # Word tokens only, so "the next word" can be found past the whitespace and
    # separators sitting between them.
    words = [i for i, t in enumerate(tokens) if t.isalpha()]

    # Locate the dictated RUNS as token spans. A run qualifies when it holds at
    # least _SPOKEN_RUN_MIN real digit words and no compound number word touches
    # either end ("twenty four", "four hundred" -> a quantity, not an id).
    # Leading/trailing aliases are trimmed off: an "oh" is a zero only BETWEEN
    # spoken digits.
    spans = []
    w = 0
    while w < len(words):
        if kinds[words[w]] not in ("digit", "alias"):
            w += 1
            continue
        start = w
        while w < len(words) and kinds[words[w]] in ("digit", "alias"):
            w += 1
        real = [i for i in words[start:w] if kinds[i] == "digit"]
        before = kinds[words[start - 1]] if start > 0 else None
        after = kinds[words[w]] if w < len(words) else None
        if (len(real) >= _SPOKEN_RUN_MIN
                and before != "compound" and after != "compound"):
            spans.append((real[0], real[-1]))

    if not spans:
        return text  # nothing was dictated as digits — leave it entirely alone.

    # Rebuild by walking the token list and emitting each span as one solid
    # digit string. Everything outside a span is copied through byte-for-byte,
    # so ordinary prose and spacing survive untouched.
    #
    # A span additionally absorbs the gap to an ALPHANUMERIC chunk directly
    # beside it — the identifier's prefix ("SV one two" -> "SV12") or suffix
    # ("one two A" -> "12A") — but only across a gap of pure separators, and
    # only for the immediately neighbouring token. That is what keeps
    # "Order five six for Priya" from collapsing into "Order56forPriya": the
    # word "for" is the neighbour on the right, and a word is not a chunk to
    # glue digits onto.
    def _gap_is_separators(a, b):
        """True when tokens strictly between a and b are all separators."""
        return all(not tokens[j].isalnum() for j in range(a + 1, b))

    def _absorbs(span_edge, other, step):
        """Should the gap between the span edge and `other` be closed up?"""
        if other < 0 or other >= len(tokens):
            return False
        if not tokens[other].isalnum():
            return False
        # Only a chunk that is NOT itself a plain word: digits, or a word that
        # reads as an identifier prefix/suffix rather than prose. A single
        # alphabetic token adjacent to the dictated digits is the prefix case.
        if not _gap_is_separators(min(span_edge, other), max(span_edge, other)):
            return False
        if tokens[other].isdigit():
            return True
        # An alphabetic neighbour glues on only when it LOOKS like part of an
        # identifier rather than prose. The signal is case: dictated ids carry a
        # capitalised or all-caps code ("SV", "ABC", "OPP", a trailing "A"),
        # while the ordinary words that surround a value in a sentence ("for",
        # "visit", "is") are lower case. So "site visit SV one zero zero four"
        # yields "site visit SV1004" — the code is absorbed, the prose is not.
        #
        # A lower-case PREFIX is still absorbed when it is the only thing to its
        # left, which is the "sv one two three" case: a bare value the model
        # extracted on its own, where casing is unreliable. Applied to the left
        # only — a trailing lower-case word is prose ("five six today"), whereas
        # a real suffix code is capitalised and already handled above.
        word = tokens[other]
        if word[:1].isupper():
            return True
        if step > 0:
            return False
        j = other - 1
        while j >= 0 and not tokens[j].strip():
            j -= 1
        return j < 0

    out, i = [], 0
    while i < len(tokens):
        span = next((sp for sp in spans if sp[0] == i), None)
        if span is None:
            out.append(tokens[i])
            i += 1
            continue
        lo, hi = span
        digits = "".join(_SPOKEN_DIGITS.get(tokens[j].lower(), "0")
                         for j in range(lo, hi + 1) if tokens[j].isalpha())
        # Trim the separator gap already emitted on the left, if it is absorbed.
        prev = lo - 1
        while prev >= 0 and not tokens[prev].strip():
            prev -= 1
        if prev >= 0 and _absorbs(lo, prev, -1):
            while out and not out[-1].strip():
                out.pop()
        out.append(digits)
        i = hi + 1
        # And skip the gap on the right when a suffix chunk is absorbed.
        nxt = i
        while nxt < len(tokens) and not tokens[nxt].strip():
            nxt += 1
        if nxt < len(tokens) and _absorbs(hi, nxt, 1):
            i = nxt
    return "".join(out).strip()


def empty_crm_identifier():
    return {"value": None, "value_raw": None, "confidence": "none",
            "confidence_score": 0.0, "evidence": ""}


def coerce_crm_identifier(obj):
    """Strict coerce a parsed Groq object into the CRM identifier schema.

    Never raises (the module contract). Returns the empty shape for anything
    that fails the grounding check, so a caller can treat "nothing mentioned"
    and "an ungrounded value" identically — both mean "ask the user", which is
    the safe outcome either way.

    Accepts `value` (current) or `site_visit_number` (the key the first,
    site-visit-only version of this prompt emitted) so a stored extraction or an
    in-flight response from either shape still coerces.
    """
    if not isinstance(obj, dict):
        return empty_crm_identifier()

    value = s(obj.get("value")) or s(obj.get("site_visit_number"))
    evidence = s(obj.get("evidence"))
    confidence = clamp(obj.get("confidence"), set(CRM_IDENT_CONFIDENCE), "none")

    # Nothing mentioned is the expected answer for most meetings — normalize
    # every flavour of "nothing" (null, "", "null", "N/A") to the empty shape.
    if not value or value.lower() in ("null", "none", "n/a", "na", "unknown"):
        return empty_crm_identifier()

    # THE grounding check: the value must appear in the model's own evidence
    # quote, as a complete reference rather than a fragment. A model that
    # invents "SV-10245" cannot also produce a transcript sentence containing
    # it, so this catches the failure mode the prompt only discourages.
    # Dropped rather than kept-with-low-confidence: a value the UI shows at all
    # is a value a rushed user may confirm.
    if not _ident_grounded(value, evidence):
        return empty_crm_identifier()

    # A value WITH grounding but labelled "none" is self-contradictory; trust
    # the evidence over the label and call it probable rather than discarding.
    if confidence == "none":
        confidence = "probable"

    # Spoken digits -> digits, AFTER grounding and never before: grounding
    # compares what the model said against the transcript it quoted, and both
    # sides are still in spoken form there ("SV one zero zero four" appears in
    # the evidence; "SV1004" does not). Normalizing first would reject the
    # extraction as ungrounded — the opposite of the intent.
    #
    # `value` is the normalized form because it is what every consumer looks up
    # with; `value_raw` keeps exactly what the model extracted so the UI and any
    # debugging can show what was actually said next to the evidence quote.
    normalized = normalize_crm_identifier(value)
    return {
        "value": normalized or value,
        "value_raw": value,
        "confidence": confidence,
        "confidence_score": Decimal(str(CRM_IDENT_CONFIDENCE_SCORE[confidence])),
        "evidence": evidence[:500],
    }


def crm_identifier_found(ident):
    """True when a usable identifier was extracted."""
    return bool(isinstance(ident, dict) and ident.get("value"))


# ---------------------------------------------------------------------------
# THE UNIFIED ANALYSIS — one model reply, one transcript read.
#
# Shape (prompts.unified_analysis_system):
#     {title, overview: {sections: [...]},     <- coerce_analysis/coerce_overview
#      tasks, participants,                     <- coerce_analysis
#      meeting_highlights: {...4 sections...}}  <- coerce_highlights
#
# Deliberately built by DELEGATING to the existing coercers rather than
# reimplementing their rules. Each carries protections that took production
# failures to find — the roster filter that stops a mentioned name becoming an
# attendee, the load-bearing-field drop that keeps empty rows out of the
# workspace. A fresh unified coercer would have had to re-earn all of that.
#
# So the ONLY new logic here is the split: pull the sub-objects out of one reply
# and hand each to the coercer that already owns it. A model that omits a
# section gets that section's documented empty value, exactly as if its own call
# had failed — which is what keeps the unified call's failure modes a subset of
# the old ones rather than a new set.
#
# CRM IDENTIFIER EXTRACTION WAS REMOVED from this path. It made every analysis
# carry per-mapping extraction rules for a feature that is not yet built out,
# and it is the one output whose failure mode is writing a real customer's
# meeting onto a stranger's Salesforce record. The coercers below it
# (coerce_crm_identifier, normalize_crm_identifier and their grounding checks)
# are DELIBERATELY KEPT: the manual identifier path and the Salesforce push
# still use them, and they are where the anti-hallucination work lives when
# integration extraction is designed properly.
# ---------------------------------------------------------------------------
def empty_unified():
    """Fresh dict with every unified field; never shares mutable values."""
    return {**empty_analysis(),
            "meeting_highlights": empty_highlights()}


def coerce_unified(obj, roster=None, valid_ids=None, speaker_names=None):
    """Strict coerce ONE unified Groq reply into its sub-schemas."""
    if not isinstance(obj, dict):
        return {**empty_unified(),
                "participants": _roster_filtered([], roster or [],
                                                 speaker_names)}

    out = coerce_analysis(obj, roster, valid_ids, speaker_names)
    # Roster-aware, so a decision's speaker_id lands as a canonical id for the
    # same reason a task's does.
    out["meeting_highlights"] = coerce_highlights(
        obj.get("meeting_highlights"), roster, speaker_names)
    return out


def merge_unified(partials, roster=None, speaker_names=None):
    """Fold per-chunk unified analyses into one — the OVERFLOW path only.

    Reached only when a transcript exceeds the single-pass budget and
    map_reduce chunks it (see groq_client.analyze). Each sub-schema is merged by
    its OWN existing merge, for the same reason coerce_unified delegates: the
    de-dupe rules ("fuller copy wins", first-mention-keeps-position) are already
    correct and differ per section.
    """
    dicts = [p for p in partials if isinstance(p, dict)]
    merged = merge_analyses(dicts, roster, speaker_names)
    merged["meeting_highlights"] = merge_highlights(
        [p.get("meeting_highlights") or {} for p in dicts])
    return merged


# ---------------------------------------------------------------------------
# TASK INTELLIGENCE — the structured half of the AI Task Dashboard.
#
# WHY A SCHEMA AND NOT PROSE. The dashboard renders these as cards keyed by
# task id, with a tap target per row. Prose cannot be keyed, and a model asked
# for prose that the client then parses is a parser waiting to break. So the
# model is asked for JSON and it is coerced here, through the same primitives
# every other AI output in this file goes through.
#
# THE ONE RULE THAT MATTERS MOST: A REASON ABOUT AN UNKNOWN TASK IS NOISE.
# Every row names a task_id, and `coerce_task_intelligence` is given the set
# of ids that were actually sent to the model. A row whose id is not in that
# set is DROPPED, not repaired — a model that invents "task_99" would
# otherwise produce a card the user can tap and that goes nowhere, which is
# exactly the failure _evidence_ids exists to prevent for segment references.
# The same argument, one level up.
#
# WHAT IS DELIBERATELY *NOT* HERE: status, assignee, due_date, priority. The
# model is not asked for them and they are not coerced, so an AI recommendation
# structurally cannot carry a task's state — the app reads state from the Tasks
# API and merges by id. That is the schema enforcing spec section 3's "AI
# recommendations must NOT overwrite task state": there is no field to
# overwrite it with.
# ---------------------------------------------------------------------------

# The buckets the dashboard renders. A fixed enum, unlike the Dynamic
# Overview's free-form sections, because each one is a SECTION OF UI with its
# own heading and empty state — a model inventing "kind" values would render
# nowhere.
TASK_INTEL_KINDS = (
    "needs_attention",     # important or acting soon
    "priority",            # do this first
    "overdue_risk",        # late, or heading that way
    "stale",               # open, untouched, no progress
    "duplicate",           # this and another task look like the same work
)

MAX_INTEL_ROWS = 12
MAX_INTEL_REASON_CHARS = 300
MAX_INTEL_RECOMMENDATION_CHARS = 300
MAX_INTEL_SUMMARY_CHARS = 600


def _intel_row(raw, valid_task_ids, valid_ids=None):
    """One coerced intelligence row, or None when it is unusable.

    Unusable means exactly two things, and both would produce a broken card:
      * the row names no task, or names a task that was not in the input —
        see the module note above on why this is dropped rather than kept;
      * the row says nothing (no reason and no recommendation). A card with a
        heading and no content reads as an assertion the model never made.
    """
    if not isinstance(raw, dict):
        return None

    task_id = s(raw.get("task_id"))
    if not task_id or task_id not in valid_task_ids:
        return None

    reason = s(raw.get("reason"))[:MAX_INTEL_REASON_CHARS]
    recommendation = s(
        raw.get("recommendation"))[:MAX_INTEL_RECOMMENDATION_CHARS]
    if not reason and not recommendation:
        return None

    row = {
        "task_id": task_id,
        "kind": clamp(raw.get("kind"), set(TASK_INTEL_KINDS), "needs_attention"),
        "reason": reason,
        "recommendation": recommendation,
        # Evidence is OPTIONAL here in a way it is not for a meeting answer:
        # most of these judgements come from the task rows themselves (a date
        # in the past needs no transcript), so a row with no citation is the
        # normal case rather than a degraded one.
        "evidence_segment_ids": _evidence_ids(
            raw.get("evidence_segment_ids"), valid_ids),
    }
    # Only present when the model actually names a counterpart, and only when
    # that counterpart is a REAL task in the input — a duplicate claim
    # pointing at an invented id is the same broken card as above.
    related = s(raw.get("related_task_id"))
    if related and related != task_id and related in valid_task_ids:
        row["related_task_id"] = related
    return row


def empty_task_intelligence():
    """Fresh result; never shares mutable lists."""
    return {"summary": "", "rows": []}


def coerce_task_intelligence(obj, valid_task_ids=(), valid_ids=None):
    """Strict coerce the model's dashboard intelligence.

    `valid_task_ids` is the set of ids that were actually SENT to the model.
    Passing it is not optional in spirit: with an empty set every row is
    dropped, which is the safe direction — no cards rather than cards that go
    nowhere.

    NEVER RAISES, like every other coercer here. A model that answers with a
    bare list, a string, or nothing comes back as an empty result the caller
    can simply not render.
    """
    out = empty_task_intelligence()
    if isinstance(obj, list):
        # A model that skipped the envelope and answered with the rows alone.
        obj = {"rows": obj}
    if not isinstance(obj, dict):
        return out

    allowed = {s(t) for t in valid_task_ids if s(t)}
    rows, seen = [], set()
    raw_rows = obj.get("rows")
    if not isinstance(raw_rows, list):
        raw_rows = obj.get("tasks") if isinstance(obj.get("tasks"), list) else []

    for raw in raw_rows:
        row = _intel_row(raw, allowed, valid_ids)
        if row is None:
            continue
        # One row per (task, kind). A model listing the same task twice under
        # the same heading is padding, and the dashboard would render it twice.
        ident = (row["task_id"], row["kind"])
        if ident in seen:
            continue
        seen.add(ident)
        rows.append(row)
        if len(rows) >= MAX_INTEL_ROWS:
            break

    out["rows"] = rows
    out["summary"] = s(obj.get("summary"))[:MAX_INTEL_SUMMARY_CHARS]
    return out
