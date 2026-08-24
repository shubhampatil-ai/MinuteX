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


def _roster_filtered(participants, roster):
    """Keep only participants whose speaker is in `roster`, in ROSTER order.

    Roster order (not model order) so the list reads as the meeting's own
    speaker order, and every roster speaker appears even if the model omitted
    one — a speaker with turns in the transcript IS a participant whether or not
    the model bothered to describe them.
    """
    if not roster:
        return participants
    by_label = {}
    for p in participants:
        k = " ".join((p.get("speaker") or "").split()).lower()
        if k and k not in by_label:
            by_label[k] = p
    out = []
    for label in roster:
        got = by_label.get(label.lower())
        out.append({"speaker": label,
                    "summary": (got or {}).get("summary", "")})
    return out

TASK_SPEC = {
    "task": ("task", s),
    "assignee": ("assignee", s),
    "due_date": ("due_date", s),
    "priority": ("priority", lambda x: clamp(
        x, {"Low", "Medium", "High"}, "") or None),
}

# The prompt asks for 3-7 highlights; this is the hard cap that makes a model
# ignoring the range harmless. Kept slightly generous rather than exact — the
# cap exists to bound the UI list, not to enforce the prompt.
MAX_HIGHLIGHTS = 7


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
            for field in ("assignee", "due_date", "priority"):
                if t.get(field) and not prior.get(field):
                    prior[field] = t[field]
    return out


def empty_analysis():
    """Fresh dict with every field; never shares mutable lists."""
    return {
        "title": "",
        "summary": "",
        "highlights": [],
        "tasks": [],
        "participants": [],
    }


def coerce_analysis(obj, roster=None):
    """Strict coerce a parsed Groq object into the fixed analysis schema.

    Builds the result key-by-key from the five known fields, so a model that
    still emits a removed field (agenda, decisions, …) has it DROPPED here
    rather than passed through to the caller's DynamoDB write.

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
            "participants": _roster_filtered([], roster),
        }
    tasks = obj_list(obj.get("tasks"), TASK_SPEC, required=("task",))
    participants = obj_list(obj.get("participants"), PARTICIPANT_SPEC,
                            required=("speaker",))
    return {
        "title": s(obj.get("title")),
        "summary": s(obj.get("summary")),
        "highlights": slist(obj.get("highlights"))[:MAX_HIGHLIGHTS],
        "tasks": _dedupe_tasks(tasks),
        "participants": _roster_filtered(participants, roster or []),
    }


def merge_analyses(partials, roster=None):
    """Fold per-chunk analyses into one, de-duplicated, order-preserving.

    Used as the reduce step's input, and as the final result if the reduce call
    itself fails — a concatenated brief beats no brief at all. Only reached via
    the map_reduce OVERFLOW path, for a transcript that genuinely exceeds the
    model's context window; a meeting that fits is analyzed in one pass and
    never comes through here.
    """
    merged = empty_analysis()
    seen_highlights = set()
    speakers = set()

    for p in partials:
        if not isinstance(p, dict):
            continue
        for item in p.get("highlights", []):
            norm = item.strip().lower()
            if norm and norm not in seen_highlights:
                seen_highlights.add(norm)
                merged["highlights"].append(item)
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

    merged["highlights"] = merged["highlights"][:MAX_HIGHLIGHTS]
    merged["tasks"] = _dedupe_tasks(merged["tasks"])
    merged["title"] = next((p.get("title") for p in partials
                            if isinstance(p, dict) and p.get("title")), "")
    merged["summary"] = " ".join(
        p["summary"].strip() for p in partials
        if isinstance(p, dict) and p.get("summary")
    ).strip()
    # Same structural guarantee as the single-pass path: a name that never had a
    # speaker turn is not a participant, however many segments volunteered it.
    merged["participants"] = _roster_filtered(merged["participants"],
                                              roster or [])
    return merged


# ---------------------------------------------------------------------------
# STAGE 2 — meeting highlights.
# ---------------------------------------------------------------------------
HL_DECISION_SPEC = {
    "decision": ("decision", s),
    "context": ("context", s),
}
HL_ACTION_SPEC = {
    "task": ("task", s),
    "owner": ("owner", s),
    "deadline": ("deadline", s),
}
HL_DEADLINE_SPEC = {
    "what": ("what", s),
    "when": ("when", s),
}
HL_NUMBER_SPEC = {
    "label": ("label", s),
    "value": ("value", s),
    "kind": ("kind", lambda x: clamp(
        x, {"money", "quantity", "percentage", "measurement", "duration"},
        "quantity")),
}

# The six highlight sections, in the order the UI renders them.
HIGHLIGHT_SECTIONS = ("decisions", "action_items", "deadlines",
                      "important_numbers", "open_questions", "risks")


def empty_highlights():
    return {k: [] for k in HIGHLIGHT_SECTIONS}


def coerce_highlights(obj):
    """Strict coerce a parsed Groq object into the meeting_highlights schema.

    Elements missing their load-bearing field are dropped: a deadline with no
    "what" or a number with no "value" carries no information, and keeping it
    would render as an empty row in the workspace.
    """
    if not isinstance(obj, dict):
        return empty_highlights()
    return {
        "decisions": obj_list(obj.get("decisions"), HL_DECISION_SPEC,
                              required=("decision",)),
        "action_items": obj_list(obj.get("action_items"), HL_ACTION_SPEC,
                                 required=("task",)),
        "deadlines": obj_list(obj.get("deadlines"), HL_DEADLINE_SPEC,
                              required=("what",)),
        "important_numbers": obj_list(obj.get("important_numbers"),
                                      HL_NUMBER_SPEC, required=("value",)),
        "open_questions": slist(obj.get("open_questions")),
        "risks": slist(obj.get("risks")),
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
        if section == "action_items":
            return (item.get("task") or "").strip().lower()
        if section == "deadlines":
            return (item.get("what") or "").strip().lower()
        if section == "important_numbers":
            # Same value under two labels is two different facts ("₹5000
            # deposit" vs "₹5000 balance"), so the label is part of identity.
            return ((item.get("label") or "").strip().lower(),
                    (item.get("value") or "").strip().lower())
        return str(item).strip().lower()

    for p in partials:
        if not isinstance(p, dict):
            continue
        for section in ("open_questions", "risks"):
            for item in p.get(section, []):
                norm = item.strip().lower()
                if norm and norm not in seen[section]:
                    seen[section][norm] = True
                    merged[section].append(item)
        for section in ("decisions", "action_items", "deadlines",
                        "important_numbers"):
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
# THE UNIFIED ANALYSIS — one model reply carrying what three calls used to.
#
# Shape (prompts.unified_analysis_system):
#     {title, summary, highlights, tasks, participants,   <- coerce_analysis
#      meeting_highlights: {...6 sections...},             <- coerce_highlights
#      crm_identifiers: {object_name: {...} | null}}       <- coerce_crm_identifier
#
# Deliberately built by DELEGATING to the three existing coercers rather than
# reimplementing their rules. Each carries protections that took production
# failures to find — the roster filter that stops a mentioned name becoming an
# attendee, the load-bearing-field drop that keeps empty rows out of the
# workspace, the grounding check that stops a hallucinated record id reaching
# Salesforce. A fresh unified coercer would have had to re-earn all of that.
#
# So the ONLY new logic here is the split: pull the three sub-objects out of one
# reply and hand each to the coercer that already owns it. A model that omits a
# section gets that section's documented empty value, exactly as if its own call
# had failed — which is what keeps the unified call's failure modes a subset of
# the old ones rather than a new set.
# ---------------------------------------------------------------------------
def empty_unified():
    """Fresh dict with every unified field; never shares mutable values."""
    return {**empty_analysis(),
            "meeting_highlights": empty_highlights(),
            "crm_identifiers": {}}


def coerce_unified(obj, roster=None, mappings=None):
    """Strict coerce ONE unified Groq reply into the three sub-schemas.

    `mappings` bounds which crm_identifiers keys are accepted: only objects the
    user actually configured. A model that volunteers an extra key (or echoes
    the example) can therefore never introduce a CRM link for an object nobody
    mapped — the same "configuration decides, not the model" rule the separate
    per-mapping calls got for free by construction.
    """
    if not isinstance(obj, dict):
        return {**empty_unified(),
                "participants": _roster_filtered([], roster or [])}

    out = coerce_analysis(obj, roster)
    out["meeting_highlights"] = coerce_highlights(obj.get("meeting_highlights"))

    # Only configured objects, and only entries that survive the grounding
    # check. `source` marks provenance for the merge in the pipeline (a MANUAL
    # value is never overwritten by an extraction).
    found = {}
    raw = obj.get("crm_identifiers")
    if isinstance(raw, dict):
        allowed = {str((m or {}).get("object") or "").strip()
                   for m in (mappings or [])}
        allowed.discard("")
        for object_name, value in raw.items():
            name = str(object_name or "").strip()
            if not name or (allowed and name not in allowed):
                continue
            ident = coerce_crm_identifier(value)
            if crm_identifier_found(ident):
                ident["source"] = "ai"
                found[name] = ident
    out["crm_identifiers"] = found
    return out


def merge_unified(partials, roster=None, mappings=None):
    """Fold per-chunk unified analyses into one — the OVERFLOW path only.

    Reached only when a transcript exceeds the single-pass budget and
    map_reduce chunks it (see groq_client.analyze). Each sub-schema is merged by
    its OWN existing merge, for the same reason coerce_unified delegates: the
    de-dupe rules ("fuller copy wins", first-mention-keeps-position) are already
    correct and differ per section.

    crm_identifiers takes the FIRST grounded value per object across chunks and
    keeps the highest confidence — an identifier is stated once, usually in the
    opening minutes, so an early chunk's explicit statement outranks a later
    chunk's inference about the same record.
    """
    dicts = [p for p in partials if isinstance(p, dict)]
    merged = merge_analyses(dicts, roster)
    merged["meeting_highlights"] = merge_highlights(
        [p.get("meeting_highlights") or {} for p in dicts])

    identifiers = {}
    for p in dicts:
        for name, ident in (p.get("crm_identifiers") or {}).items():
            if not crm_identifier_found(ident):
                continue
            prior = identifiers.get(name)
            if prior is None:
                identifiers[name] = ident
                continue
            # Prefer an explicit statement over an inferred one; otherwise the
            # earlier chunk wins (it saw the identification, not a callback).
            if (CRM_IDENT_CONFIDENCE_SCORE.get(ident.get("confidence"), 0.0)
                    > CRM_IDENT_CONFIDENCE_SCORE.get(prior.get("confidence"), 0.0)):
                identifiers[name] = ident
    merged["crm_identifiers"] = identifiers
    return merged
