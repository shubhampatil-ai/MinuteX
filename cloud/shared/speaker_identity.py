"""speaker_identity — THE definition of who a speaker is.

WHY THIS MODULE EXISTS
----------------------
A speaker has two separate things: an IDENTITY and a DISPLAY NAME. The
identity is the compact diarization label ElevenLabs gives us ("0", "1",
"agent"); the display name is whatever the user has since called that person.
The identity never changes. The name changes whenever the user renames, and a
rename must not require regenerating anything that stored the identity.

    speaker_id            "0"                  <- stable, never rewritten
    speaker_names["0"]    "Rahul" -> "Amit"    <- display metadata, mutable

That model was already the intent, but the rules for it lived in FOUR places
that had drifted apart:

    stt_result.normalize_speaker_id     strict: only a bare number survives
    mom_schema.normalize_speaker_label  loose: returns whatever follows
    pageindex                           none: a bare dict lookup on "0"
    app/lib/sources.ts                  a fifth copy, on the client

Four implementations of one rule is how "Speaker 0" ended up unable to find
the name stored under "0" — the exact class of bug stt_result's own header
warns about ("a second copy of this grouping logic would eventually
disagree"). This module is that one implementation for the backend.

WHAT IT DELIBERATELY DOES NOT DO
--------------------------------
It does not INVENT identity. Every function here either finds a speaker that
demonstrably exists or reports that it could not. A wrong match attributes one
person's words — or one person's work — to another, which is worse than a
visible "Speaker 0". "No match" is always the safer failure, so ambiguity
always resolves to nothing rather than to a guess.

It also does not change what anything is CALLED on screen. An unnamed speaker
still renders "Speaker 0", exactly as before, because that is what the
transcript beside it says.

IMPORT SAFETY
-------------
Standard library only, and it imports nothing from this package. Both Lambdas
vendor cloud/shared as a flat directory, and mom_schema notes that importing
back into a caller would be a cycle — a leaf module cannot create one.
"""

import re

# A speaker label written the way a human (or a model) writes it, reduced to
# the bare label: "Speaker 0" / "speaker_0" / "Speaker-0" / " speaker 0 ".
# Anchored at the start only; the remainder is inspected by the caller, which
# is what lets "Speaker Two" keep its own identity instead of becoming "Two".
_PREFIX_RE = re.compile(r"^speaker[\s_-]*", re.IGNORECASE)


def normalize_speaker_id(value):
    """A speaker reference reduced to its canonical id.

        "Speaker 0" / "speaker_0" / "Speaker-0" / " 0 " / "0"  -> "0"
        "agent" / "customer"                                   -> unchanged
        "Speaker Two" / "Rahul"                                -> unchanged
        "" / None                                              -> ""

    The prefix is only stripped when what remains is a plain NUMBER. A name
    that merely begins with those letters keeps its own identity rather than
    being truncated into something that could collide with a real speaker.

    Idempotent, so it can be applied at every boundary without tracking
    whether a value has already been through it.

    Byte-for-byte the same rule as stt_result.normalize_speaker_id, which is
    the behaviour the production join already depends on.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    stripped = _PREFIX_RE.sub("", text).strip()
    return stripped if stripped.isdigit() else text


def is_canonical_speaker_id(value):
    """True when `value` is already a canonical id.

    A canonical id is a bare number ("0", "12") or a non-numeric diarization
    label ElevenLabs emitted ("agent"). What it is NOT is a display form
    ("Speaker 0") or a person's name — both of which normalize to something
    other than themselves, or to a value the roster does not contain.
    """
    text = str(value or "").strip()
    return bool(text) and normalize_speaker_id(text) == text


def normalize_speaker_names(mapping):
    """A `{label: name}` map re-keyed on canonical ids.

    Accepts the historical spellings a client or an older write may have
    stored — {"Speaker 0": "Rahul"} — and returns {"0": "Rahul"}, so a lookup
    on the canonical id finds a name that was filed under a display form.

    A CANONICAL KEY ALWAYS WINS over a non-canonical one that folds onto it.
    Given {"0": "Rahul", "Speaker 0": "Stale"} the answer is "Rahul": the
    canonical spelling is the one every current writer produces, so it is the
    more recent and more trustworthy of the two. Between two keys of equal
    standing the first wins, making the result deterministic rather than
    dependent on dict ordering.

    Blank names are dropped, matching _clean_speaker_names' rule that an empty
    name REMOVES a mapping rather than storing an empty one.
    """
    if not isinstance(mapping, dict):
        return {}
    out, canonical_keys = {}, set()
    for raw_key, raw_name in mapping.items():
        key = normalize_speaker_id(raw_key)
        if not key:
            continue
        name = str(raw_name or "").strip()
        if not name:
            continue
        was_canonical = is_canonical_speaker_id(raw_key)
        if key in out:
            # Only a canonical key may displace an already-claimed slot, and
            # only when the sitting value came from a non-canonical key.
            if not was_canonical or key in canonical_keys:
                continue
        out[key] = name
        if was_canonical:
            canonical_keys.add(key)
    return out


def resolve_speaker_name(speaker_id, speaker_names):
    """The user-supplied name for a speaker, or "" if there isn't one.

    Tolerates a `speaker_names` map that was stored under display-form keys,
    so a legacy row resolves exactly as a current one does. Returns "" rather
    than a fallback label — callers that want "Speaker 0" ask for the DISPLAY
    (below); callers that need to know whether a real name exists check this.
    """
    key = normalize_speaker_id(speaker_id)
    if not key:
        return ""
    names = speaker_names if isinstance(speaker_names, dict) else {}
    named = names.get(key)
    if named and str(named).strip():
        return str(named).strip()
    # The map may predate key normalization. Re-key and try once more rather
    # than making every caller remember to do it.
    normalized = normalize_speaker_names(names)
    named = normalized.get(key)
    return str(named).strip() if named and str(named).strip() else ""


def fallback_display_name(speaker_id):
    """What a speaker with no name is called: "Speaker 0".

    Numeric labels get the prefix back; a word label ("agent") stands alone,
    because "Speaker agent" reads as a mistake. Empty in, empty out.

    This is the string the transcript itself shows, which is why it is the
    right fallback: the chip and the transcript line agree. Deliberately NOT
    "Participant 1" or "Unknown" — renumbering would break that correspondence
    and neither string exists anywhere in the product today.
    """
    key = normalize_speaker_id(speaker_id)
    if not key:
        return ""
    return f"Speaker {key}" if key.isdigit() else key


def resolve_speaker_display(speaker_id, speaker_names):
    """The name to SHOW for a speaker: their name, else "Speaker N"."""
    return (resolve_speaker_name(speaker_id, speaker_names)
            or fallback_display_name(speaker_id))


def resolve_speaker(speaker_id, speaker_names):
    """The full identity picture for one speaker.

        {"speaker_id": "0", "display_name": "Rahul", "resolved": True}
        {"speaker_id": "0", "display_name": "Speaker 0", "resolved": False}

    `resolved` says whether a HUMAN name exists — it is not about whether the
    speaker is real. It is what a caller uses to decide whether to offer a
    rename affordance, or whether attribution is safe to print.
    """
    key = normalize_speaker_id(speaker_id)
    name = resolve_speaker_name(key, speaker_names)
    return {
        "speaker_id": key,
        "display_name": name or fallback_display_name(key),
        "resolved": bool(name),
    }


def resolve_legacy_speaker_reference(value, roster=None, speaker_names=None):
    """A speaker reference of ANY vintage reduced to a canonical id, or "".

    Three shapes reach this function, and only the third is interesting:

      1. A canonical id ("0")           -> returned as-is.
      2. A display form ("Speaker 0")   -> normalized to "0".
      3. A NAME ("Rahul")               -> resolved through `speaker_names`,
                                           but only when the answer is certain.

    Case 3 exists because of a real production defect. The model is handed a
    transcript whose lines have already had the user's names substituted in
    ("Rahul: I'll send it"), while the prompt asks it to copy a label it can
    no longer see — so it answers with the NAME. Stored unchanged in an id
    field, that name is frozen: it matches no participant row, and a later
    rename can never move it, because nothing knows it was ever speaker "0".

    THE AMBIGUITY RULE. A name resolves only when exactly ONE speaker carries
    it. If two speakers are both called "Rahul", this returns "" — an
    unassigned task is a visible, fixable gap, while a task assigned to the
    wrong Rahul is invisible and wrong. Matching is case- and
    whitespace-insensitive because the model re-types the name it read.

    `roster` (canonical ids known to be real for this meeting) narrows the
    answer when given. A reference that survives normalization but names no
    known speaker is returned as "" rather than as a plausible-looking id,
    so a caller can never store a speaker this meeting does not have.
    """
    text = str(value or "").strip()
    if not text:
        return ""

    known = None
    if roster is not None:
        known = {normalize_speaker_id(r) for r in roster}
        known.discard("")

    # (1) and (2): already an id, or a display form of one.
    key = normalize_speaker_id(text)
    if key and (known is None or key in known):
        # Only trust a bare-label reading. "Rahul" normalizes to itself, and
        # accepting that here would store a name in an id field — the very
        # defect this function exists to close.
        if is_canonical_speaker_id(key) and (known is not None
                                             or key.isdigit()):
            return key

    # (3) a NAME. Resolve it, but only if exactly one speaker answers to it.
    names = normalize_speaker_names(speaker_names)
    if not names:
        return ""
    wanted = text.casefold()
    hits = [sid for sid, name in names.items()
            if str(name).strip().casefold() == wanted
            and (known is None or sid in known)]
    return hits[0] if len(hits) == 1 else ""


def speaker_roster_ids(timestamps=None, speaker_names=None, participants=None):
    """The canonical ids this meeting actually has, in first-seen order.

    Assembled from the structural evidence first — the diarized segments,
    which are a FACT about the audio — then widened by any speaker the user
    has named or mapped. Order is stable so a roster rendered into a prompt
    reads in the meeting's own speaking order.

    Used to bound legacy resolution: a reference that names no speaker on this
    list is not resolvable, however plausible it looks.
    """
    out, seen = [], set()

    def _add(value):
        key = normalize_speaker_id(value)
        if key and key not in seen:
            seen.add(key)
            out.append(key)

    for seg in timestamps or []:
        if isinstance(seg, dict):
            _add(seg.get("speaker"))
    for key in normalize_speaker_names(speaker_names):
        _add(key)
    for row in participants or []:
        if isinstance(row, dict):
            _add(row.get("speaker_id") or row.get("speaker"))
    return out
