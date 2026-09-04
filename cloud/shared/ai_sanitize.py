"""ai_sanitize — the last hop before AI prose reaches a user.

WHY THIS MODULE EXISTS. Two defects were being fixed in three places at once
and would have become three divergent string patches:

  1. ESCAPED MARKDOWN. Models trained to emit Markdown into environments that
     require escaping sometimes write `\\*Important\\*` instead of
     `**Important**`. The app's renderer (app/lib/document-renderer.tsx) is a
     deliberately small Markdown subset that had no notion of backslash
     escapes, so the backslashes reached the screen literally.
  2. INTERNAL SEGMENT IDS. The transcript is handed to the model as
     `[seg_42] Speaker 0: ...` lines, because `seg_N` is the identity the app
     deep-links on (transcript_store.segment_id). Those ids are RETRIEVAL
     METADATA: they belong in the structured `sources` / `evidence_segment_ids`
     arrays, never in prose. A model reading them on every line will
     occasionally cite them inline — "According to seg_24, the team decided…" —
     which reads to a user like a debug dump.
  3. MODEL-INVENTED CITATION MARKERS. A SEPARATE format from (2), found in
     AI Chat after (2) was fixed: 【-6】, 【-32】, 【-94】 and the bare 【】.
     These contain no segment id, so the `seg_`-keyed rules above never saw
     them. They are the CJK corner-bracket citation syntax the gpt-oss family
     reaches for over retrieved context — the model attributing its answer to
     "sources" using a reference number it makes up, which is why several are
     negative and several are empty. See _CJK_CITATION below.

WHAT THIS IS NOT. It is not a content filter and it must never become one. The
prompts are the primary fix for (2); this is the backstop that guarantees the
defect cannot reach a user when a model ignores an instruction, which models
do. So every rule here is narrow enough to be provably safe on ordinary prose:

  * `\\*` -> `*` only for characters that ARE Markdown punctuation. A backslash
    before an ordinary letter is left alone, so a Windows path or a regex in a
    code answer survives.
  * `seg_N` is matched only in its exact form — the literal prefix followed by
    digits, on a word boundary. The words "segment", "segments", "seg" and
    "segment 4" are untouched, and so is `seg_id` or `seg_alpha`.
  * The citation markers are keyed on the CJK corner-bracket CHARACTERS
    (【 】), which no answer this app generates legitimately contains — not on
    the ASCII bracket SHAPE, which answers contain constantly. The only ASCII
    bracket forms removed are the two that cannot be content: an empty `[]`
    and a negative-only `[-32]`. `[Google, Facebook]`, `[2026]` and
    `[Launch Configuration]` come through untouched.
  * Nothing else is removed. There is no profanity list, no length cap, no
    reformatting.

WHERE IT IS APPLIED. Only at user-facing boundaries: the two chat replies and
the dynamic overview's prose fields. It is NOT applied to the structured
evidence arrays, which are exactly where the ids are supposed to live, and not
to the transcript or to any prompt input.
"""
import re

# The exact internal identifier: the literal prefix from
# transcript_store.SEGMENT_ID_PREFIX followed by one or more digits, bounded so
# it cannot match inside a longer token. `(?!\w)` rather than `\b` on the tail
# so `seg_12abc` (not an id) is left alone.
_SEG_ID = r"seg_\d+(?!\w)"

# One id, with the bracket form the labelled transcript uses. `[seg_12]` is how
# the model SEES the line, so it is the form most often copied out verbatim.
_SEG_TOKEN = re.compile(r"\[?\b" + _SEG_ID + r"\]?", re.IGNORECASE)

# A citation clause: an id, or a run of them, optionally inside brackets or
# parentheses, optionally introduced by the connective the model reaches for.
# Matched as a WHOLE so "According to seg_24, the team decided" collapses to
# "the team decided" rather than to "According to , the team decided".
#
# The leading connective list is deliberately short and literal. Anything not
# listed falls through to the single-token pass below, which removes the id and
# tidies the punctuation around it — a slightly clumsier sentence, but never a
# wrong one.
_SEG_CITATION = re.compile(
    r"""
    (?:
        # "(see seg_3, seg_4)" / "[seg_3]" / "(seg_3)"
        \s*[\(\[]\s*(?:see\s+|from\s+|ref\.?\s+|source:?\s+)?
            (?:""" + _SEG_ID + r"""(?:\s*[,;and]+\s*)?)+
        \s*[\)\]]
      |
        # "According to seg_24," / "as stated in seg_3 and seg_4:"
        \b(?:according\s+to|as\s+(?:stated|mentioned|discussed|said|noted|shown)\s+in
           |as\s+per|per|based\s+on|see|from|in|ref\.?|source:?)\s+
            (?:""" + _SEG_ID + r"""(?:\s*(?:,|and|&)\s*)?)+
        \s*[,:;-]?
      |
        # A bare run: "seg_3, seg_4 and seg_5"
        \b(?:""" + _SEG_ID + r"""(?:\s*(?:,|and|&)\s*)?)+
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Punctuation left stranded once a citation is cut out of the middle of a
# sentence: " , the team decided" -> ", the team decided", and a doubled comma
# from "the team (seg_3), decided" -> "the team, decided".
_STRANDED_PUNCT = re.compile(r"\s+([,.;:!?])")
_DOUBLED_PUNCT = re.compile(r"([,;:])\s*(?=[,;:])")
_RUNS_OF_SPACE = re.compile(r"[ \t]{2,}")
_EMPTY_BRACKETS = re.compile(r"[\(\[]\s*[\)\]]")

# ---------------------------------------------------------------------------
# CITATION MARKERS — the SECOND leak, and a different format from `seg_N`.
#
# WHAT WAS ACTUALLY OBSERVED. The AI Chat screen showed markers with no
# segment id in them at all:
#
#     "...100 flats allocated for a Ganpati festival launch 【-6】"
#     "...cost-per-lead calculations (15k, 20k, 30k examples) 【-32】"
#     "...using a manager-sub-account hierarchy 【-94】"
#     "...uses an external ID for each account 【】"
#
# These are NOT `seg_N`. They are the citation syntax the OpenAI-lineage
# models (gpt-oss-*, which is what GROQ_MODEL points at in production) emit
# when they decide their context was "retrieved" material worth attributing —
# CJK corner brackets 【 】 wrapping a reference the model composes itself.
# Because our retrieval hands it `[seg_42] Speaker: ...` lines, it invents a
# reference number from them, and the number is frequently garbage: negative,
# an offset, a `4:2`-style pair, a `turn0search1` token, or absent entirely,
# leaving the bare 【】 shell.
#
# WHY THE OLD SANITIZER MISSED ALL OF IT. strip_segment_ids() opened with
# `if "seg_" not in text: return text` — a fast path that is correct for the
# defect it was written for and blind to this one, since none of these markers
# contains the substring "seg_". So every example above passed through the
# boundary function untouched.
#
# WHY THIS IS SAFE TO MATCH BROADLY. 【 】 are CJK corner brackets. They are
# not ASCII `[`, they are not produced by our prompts, our renderer or any
# legitimate English/Marathi/Hindi prose the app generates, and a user asking
# a question in Devanagari still gets Devanagari inside ASCII punctuation.
# So the marker CHARACTER itself is the signal, and its content does not have
# to be parsed to know it is machinery. That is what makes this rule narrow
# despite matching any payload: it keys on a character class no real answer
# uses, not on a bracket shape that real answers use constantly.
_CITATION_BRACKETS = "【】⌈⌉⌊⌋"

# 【anything-but-a-closing-bracket】, including the empty 【】. Non-greedy and
# newline-free so an unclosed 【 cannot swallow the rest of the reply.
_CJK_CITATION = re.compile(r"[【⌈⌊][^】⌉⌋\n]{0,64}?"
                           r"[】⌉⌋]")

# A lone corner bracket left by a truncated reply. Removed only for these
# characters — never for ASCII brackets.
_STRAY_CITATION_BRACKET = re.compile(r"[" + _CITATION_BRACKETS + r"]")

# The ASCII residue form: `[-6]`, `[-32]`, `[ -94 ]`, `[]`.
#
# DELIBERATELY NOT `\[\d+\]`. A bare `[6]` is legitimate user-facing content —
# a footnote a user pasted, an index, a bracketed figure — so a POSITIVE
# number is left alone. What is matched is the shape that cannot be content:
#   * an EMPTY bracket pair, which carries no information by definition, and
#   * a NEGATIVE-only number, because a citation index is never negative and
#     prose that means minus six writes "-6" or "(6)", not "[-6]".
# This is the whole of requirement 6: `[Google, Facebook]`, `[2026]` and
# `[Launch Configuration]` do not match, and are pinned as tests.
_ASCII_CITATION_RESIDUE = re.compile(r"\[\s*(?:-\s*\d+\s*)?\]")

# The Markdown punctuation an escape is legitimate for. A backslash before
# anything NOT in this set is left exactly as written — that is what keeps
# `C:\Users`, `\n` inside a quoted string and `\d` in a regex intact.
_MD_PUNCT = r"\\`*_{}\[\]()#+\-.!|>~"
_ESCAPED_MD = re.compile(r"\\([" + _MD_PUNCT + r"])")

# The lines the ids are ALLOWED on: the structured citation footer the grounded
# chat prompt asks for (prompts.GROUNDED_CHAT_RULES). userApi strips it before
# this runs, but if the order ever changes, unescaping must not eat it.
_SOURCES_LINE = re.compile(r"^\s*SOURCES:", re.IGNORECASE)


def unescape_markdown(text):
    """`\\*bold\\*` -> `*bold*`, for Markdown punctuation only.

    The app renders a Markdown subset with no escape handling, so an escaped
    asterisk is never anything but an artifact there: either the model meant
    emphasis (and the backslash breaks it) or it meant a literal asterisk (and
    the backslash is still not what the user should see).

    Non-Markdown escapes are preserved so a reply containing a path, a regex or
    a code sample is not corrupted.
    """
    if not text:
        return text or ""
    return _ESCAPED_MD.sub(r"\1", str(text))


def has_segment_ids(text):
    """True when the prose carries an internal id. For logging, not control
    flow — callers sanitize unconditionally and log on the way past."""
    return bool(text) and bool(_SEG_TOKEN.search(str(text)))


def has_citation_markers(text):
    """True when the prose carries a non-`seg_N` internal citation marker.

    Same role as has_segment_ids: a leak means the prompt was ignored and that
    is worth a CloudWatch line. Not used for control flow — the boundary
    function sanitizes unconditionally.
    """
    if not text:
        return False
    s = str(text)
    return bool(_CJK_CITATION.search(s)
                or _STRAY_CITATION_BRACKET.search(s)
                or _ASCII_CITATION_RESIDUE.search(s))


def strip_citation_markers(text):
    """Remove model-invented citation markers from user-facing prose.

    Handles the format `strip_segment_ids` cannot see, because it contains no
    `seg_` substring at all: the CJK corner-bracket citation 【…】 that the
    gpt-oss family emits over retrieved context, and the ASCII residue forms
    `[]` and `[-6]`.

    Ordering matters. The CJK pass runs FIRST and takes the whole marker
    including its payload, so `【-6】` never has to be handled as ASCII. The
    ASCII pass then catches the two shapes that reach us independently — a
    genuinely empty `[]` and a negative-only `[-32]` — which is also what
    cleans up after strip_segment_ids removed an id from inside brackets.

    Legitimate bracketed prose is untouched: `[Google, Facebook]`, `[2026]`
    and `[Launch Configuration]` match none of these patterns.
    """
    if not text:
        return text or ""

    out = _CJK_CITATION.sub(" ", str(text))
    # An unclosed corner bracket from a truncated reply.
    out = _STRAY_CITATION_BRACKET.sub(" ", out)
    out = _ASCII_CITATION_RESIDUE.sub("", out)

    out = _STRANDED_PUNCT.sub(r"\1", out)
    out = _DOUBLED_PUNCT.sub("", out)
    out = _RUNS_OF_SPACE.sub(" ", out)
    # Trailing whitespace a removed end-of-line marker left behind, without
    # touching the line structure the renderer depends on.
    return "\n".join(line.rstrip() for line in out.split("\n")).strip()


def strip_segment_ids(text):
    """Remove internal `seg_N` references from user-facing prose.

    Only the exact identifier form is targeted, so "we discussed three
    segments" and "segment 4 of the rollout" are untouched. A citation clause
    that becomes empty takes its connective and stranded punctuation with it,
    which is what turns "According to seg_123, the client approved." into "The
    client approved." rather than into ", the client approved."
    """
    if not text or "seg_" not in str(text).lower():
        return text or ""

    out = _SEG_CITATION.sub(" ", str(text))
    # Anything the citation shapes did not cover (an id glued to a word, an
    # unusual connective) still has to go.
    out = _SEG_TOKEN.sub(" ", out)

    out = _EMPTY_BRACKETS.sub("", out)
    out = _STRANDED_PUNCT.sub(r"\1", out)
    out = _DOUBLED_PUNCT.sub("", out)
    out = _RUNS_OF_SPACE.sub(" ", out)

    # Re-capitalize a sentence that lost its opening clause: "the client
    # approved" reading mid-paragraph is fine, but as the first word of the
    # reply it looks broken.
    lines = []
    for line in out.split("\n"):
        stripped = line.strip()
        if stripped and stripped[0].islower():
            # Only when the id removal is what created the lowercase start —
            # i.e. the line no longer begins where it did.
            stripped = stripped[0].upper() + stripped[1:]
        lines.append(stripped)
    return "\n".join(lines).strip()


def sanitize_ai_user_output(text, label="", drop_segment_ids=True):
    """The one boundary function: AI prose -> what a user may see.

    `label` is for the internal warning line only — a leak means a prompt is
    being ignored and that is worth seeing in CloudWatch, but the id itself is
    never echoed into a user-visible string.

    `drop_segment_ids=False` is for a surface that legitimately shows ids (none
    today); the escaping fix still applies, because it always does.
    """
    if not text:
        return text or ""

    cleaned = unescape_markdown(text)

    if drop_segment_ids and has_segment_ids(cleaned):
        print(f"[warn] ai_sanitize: internal segment id in user-facing "
              f"output ({label or 'unlabelled'}); stripped before return")
        cleaned = strip_segment_ids(cleaned)

    # THE SECOND PASS, AND IT IS NOT GATED ON THE FIRST. strip_segment_ids
    # returns early unless the text contains "seg_", which is why the 【-6】 /
    # 【】 markers reached production untouched: they carry no segment id to
    # trip that check. This runs on its own terms, and it runs AFTER the id
    # pass so that an id removed from inside brackets leaves a `[]` this pass
    # then clears.
    if drop_segment_ids and has_citation_markers(cleaned):
        print(f"[warn] ai_sanitize: internal citation marker in user-facing "
              f"output ({label or 'unlabelled'}); stripped before return")
        cleaned = strip_citation_markers(cleaned)

    return cleaned


def sanitize_overview(overview, label=""):
    """A dynamic overview with its PROSE sanitized and its evidence intact.

    `evidence_segment_ids` is the whole point of the ids and is deliberately
    NOT touched: it is structured metadata the app turns into a tappable
    deep-link, not something a user reads. Only `title`, `content` and `items`
    — the fields rendered as text — pass through the sanitizer.

    Returns a NEW dict; the input is never mutated, so a caller holding the
    stored row is unaffected.
    """
    if not isinstance(overview, dict):
        return overview

    sections = overview.get("sections")
    if not isinstance(sections, list):
        return overview

    out = []
    for section in sections:
        if not isinstance(section, dict):
            out.append(section)
            continue
        row = dict(section)
        row["title"] = sanitize_ai_user_output(
            row.get("title") or "", f"{label}:title")
        row["content"] = sanitize_ai_user_output(
            row.get("content") or "", f"{label}:content")
        items = row.get("items")
        if isinstance(items, list):
            row["items"] = [
                sanitize_ai_user_output(i, f"{label}:item")
                if isinstance(i, str) else i
                for i in items
            ]
            # An item that was NOTHING BUT an id is now empty and would render
            # as a blank bullet.
            row["items"] = [i for i in row["items"] if not isinstance(i, str)
                            or i.strip()]
        out.append(row)

    result = dict(overview)
    result["sections"] = out
    return result
