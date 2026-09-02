"""pageindex.py — a reasoning-based retrieval index over ONE meeting transcript.

WHY THIS MODULE EXISTS
----------------------
Meeting AI (userApi's `chat`) answered from a context built by
prompts.build_context(), which does exactly this when the transcript is longer
than one TPM window:

    transcript = transcript[:transcript_budget_chars] + "[TRANSCRIPT TRUNCATED
                 — this is the beginning of a longer meeting ...]"

That is a HEAD truncation. On a 15-minute meeting it never fires and nothing is
wrong. On a two-hour meeting the model is handed the first ~35 minutes and the
analysis digest, and is then asked "what did we decide about pricing near the
end?" — a question whose answer is in the 90 minutes that were cut. The model
cannot answer it, and (because the truncation is honestly labelled) says so.
The information was in S3 the whole time; nothing ever went and got it.

So the missing piece is RETRIEVAL: given a question, find the parts of *this*
meeting that bear on it, wherever in the meeting they are, and send only those.

WHY A TREE AND NOT EMBEDDINGS
-----------------------------
The obvious reflex is a vector database — chunk, embed, cosine-search. That was
rejected, and not only because Phase 1 forbids it:

  * It is a second datastore to provision, pay for, secure and keep consistent
    with DynamoDB, for a corpus that is ONE meeting per query. The whole index
    being searched is a few hundred KB that we already have in S3.
  * Embedding search answers "which chunk is lexically nearest this question",
    which is a poor proxy for "where in this conversation was pricing settled".
    A decision is usually phrased in words the question does not contain.
  * It needs an embedding model. We have Groq, which is a chat model. Adding an
    embeddings provider is a new vendor, a new key, a new failure mode.

PageIndex's insight (VectifyAI/PageIndex) is that a document already has a
STRUCTURE, and an LLM navigating a table of contents is a better retriever than
a nearest-neighbour lookup — it can reason about what a section is FOR. So the
index is a tree of nodes, each with a short description, and retrieval hands
the model that tree and asks it to pick the relevant nodes. Same idea here, with
the meeting as the document.

WHAT A TRANSCRIPT'S "STRUCTURE" IS
----------------------------------
PageIndex's reference implementation targets PDFs, which have chapters,
headings and a real table of contents to parse. A diarized transcript has none
of that — it is one flat list of speaker turns. So the tree here is built from
the only structure a conversation actually has: TIME and SPEAKER TURNS. Segments
are grouped into consecutive windows, and windows into a shallow parent level,
producing the two-level table of contents a navigator needs.

That build is DELIBERATELY DETERMINISTIC — no LLM call. The reference
implementation summarises every node with the model, which on our Groq quota
(GROQ_TPM_LIMIT, 12k tokens/minute on the free tier) would mean ~40 paced calls
to index one 3-hour meeting: minutes of Lambda wall-clock, real 429 exposure,
and a bill for every meeting whether or not anybody ever asks it a question.
For a transcript the LLM summary also buys much less than it does for a PDF,
because the node's own opening lines already say what the passage is about.

`PAGEINDEX_LLM_SUMMARY=1` upgrades node descriptions to Groq-generated ones for
environments where that trade is worth making (a paid Groq plan, say). The tree
SHAPE is identical either way, so turning the flag on and off does not
invalidate stored indexes — only the `summary` text differs.

WHAT IS AND IS NOT IN HERE
--------------------------
This module is pure: it builds a tree, serialises it, and picks nodes given a
list of node ids. It does NOT do authorization, S3, DynamoDB or Groq calls —
those live in `pageindex_store.py` (persistence) and userApi's retrieval helper
(auth + orchestration), so this file stays unit-testable with no AWS at all.

SEGMENT IDS ARE NOT REDEFINED HERE. `seg_N` is transcript_store's positional
identity and it stays the only one — a node stores a segment RANGE, and every
retrieval result carries the same `seg_N` the transcript UI, the task evidence
and the deep-link already use. That is what makes a retrieved passage clickable
without a second navigation mechanism existing anywhere.
"""

import json
import os
import re

import transcript_store

# The index format version. Stamped into every stored tree and checked on load
# (see pageindex_store.is_fresh) so that a change to how nodes are cut can
# invalidate old trees without touching the transcript fingerprint, which
# describes the CONTENT and must keep meaning only "the words changed".
PAGEINDEX_VERSION = 2

# Target seconds of conversation per leaf node.
#
# A leaf is the unit of retrieval: pick a node, get its segments. Too small and
# the tree grows a table of contents too long to hand the model cheaply; too
# large and every hit drags in minutes of irrelevant talk and burns the context
# budget. Four minutes is roughly one topic's worth of a meeting and gives a
# 2-hour meeting ~30 leaves — a table of contents of ~30 short lines, which is
# a few hundred tokens: affordable on every question.
LEAF_TARGET_SECONDS = float(os.environ.get("PAGEINDEX_LEAF_SECONDS", "240"))

# Hard ceiling on segments per leaf, for transcripts where `start`/`end` are
# missing or degenerate (all zeros). Without it such a transcript would collapse
# into ONE leaf holding everything, which is precisely the head-truncation
# failure this module exists to remove — just one level up.
LEAF_MAX_SEGMENTS = int(os.environ.get("PAGEINDEX_LEAF_MAX_SEGMENTS", "60"))

# Leaves per parent node. The tree is intentionally SHALLOW (two levels): the
# navigator reads the whole table of contents in one call, so depth buys
# nothing and only makes the structure harder for the model to hold. Parents
# exist to give a long meeting a coarse "which third of the meeting" layer.
LEAVES_PER_PARENT = int(os.environ.get("PAGEINDEX_LEAVES_PER_PARENT", "8"))

# Characters of a node's own text used as its extractive description. Long
# enough to convey the topic, short enough that a 40-node table of contents
# stays a few hundred tokens.
NODE_SUMMARY_CHARS = int(os.environ.get("PAGEINDEX_SUMMARY_CHARS", "180"))


def _seg_time(seg, field, default=0.0):
    """A segment's start/end as a float, tolerating Decimal, str and absent.

    Transcripts reach here from two places with different numeric types (S3
    JSON gives float, a legacy inline DynamoDB row gives Decimal), and a
    transcript with no timing at all is a real shape we must not crash on —
    it simply falls back to segment-count windowing.
    """
    try:
        return float(seg.get(field, default))
    except (TypeError, ValueError):
        return float(default)


def _clean_text(raw):
    """Collapse whitespace so a node description is one readable line."""
    return re.sub(r"\s+", " ", str(raw or "")).strip()


def _summarise(segments):
    """The extractive description of a node: its opening words.

    NOT the whole node — the description's job is to let the navigator decide
    whether to OPEN this node, and the opening turn of a passage is what most
    reliably signals its topic. Truncation is marked so the model does not read
    a cut-off sentence as a complete thought.
    """
    text = _clean_text(" ".join(_clean_text(s.get("text")) for s in segments
                                if isinstance(s, dict)))
    if len(text) <= NODE_SUMMARY_CHARS:
        return text
    return text[:NODE_SUMMARY_CHARS].rstrip() + "…"


def _speakers(segments):
    """The distinct speaker labels in a node, in first-appearance order.

    Speaker IDS, never display names. A rename ("Speaker 1" -> "Priya") must not
    invalidate the index (Part 18), so the stable id is what gets stored; the
    display name is resolved at READ time from the row's speaker_names, which is
    where it is authoritative anyway.
    """
    out = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        sp = str(seg.get("speaker", "")).strip()
        if sp and sp not in out:
            out.append(sp)
    return out


def _cut_leaves(segments):
    """Group consecutive segments into leaf-sized windows.

    Time-driven, with a segment-count backstop: a window closes when it has
    covered LEAF_TARGET_SECONDS of conversation OR accumulated
    LEAF_MAX_SEGMENTS turns, whichever comes first. The backstop is what keeps
    an untimed transcript (every start/end 0) from degenerating into a single
    node.

    Boundaries fall BETWEEN speaker turns, never inside one, because a segment
    is the atom the evidence layer points at — splitting one would create a
    passage no `seg_N` can address.
    """
    if not segments:
        return []

    leaves = []
    current = []
    window_start = None

    for seg in segments:
        if not isinstance(seg, dict):
            continue
        start = _seg_time(seg, "start")
        if not current:
            window_start = start
        current.append(seg)

        elapsed = _seg_time(seg, "end", start) - (window_start or 0.0)
        if elapsed >= LEAF_TARGET_SECONDS or len(current) >= LEAF_MAX_SEGMENTS:
            leaves.append(current)
            current = []
            window_start = None

    if current:
        # A short trailing window is merged into the previous leaf rather than
        # left as a one-turn node: a stub node costs a table-of-contents line
        # and answers nothing on its own. Only when it is the ONLY leaf does it
        # stand alone.
        if leaves and len(current) <= 2:
            leaves[-1].extend(current)
        else:
            leaves.append(current)
    return leaves


def build_tree(timestamps, transcript=""):
    """The PageIndex tree for one meeting's transcript segments.

    `timestamps` is the segment list as `transcript_store.hydrate` returns it —
    every segment already carrying its derived `seg_N` id. The ids are READ
    here, never invented: a node records the range of segment ids it covers, so
    a retrieved node maps straight back onto the transcript the user can see.

    Returns the serialisable tree dict. Empty input yields an empty tree rather
    than an error — a recording whose transcript failed is a normal state that
    the callers already render as "not ready", and it must not become a crash.
    """
    segments = transcript_store.with_segment_ids(timestamps)
    segments = [s for s in segments if isinstance(s, dict)]

    leaves = _cut_leaves(segments)
    nodes = []
    for i, group in enumerate(leaves):
        first, last = group[0], group[-1]
        nodes.append({
            "node_id": f"node_{i}",
            # The segment RANGE, not copies of the text. The transcript stays
            # the single source of the words; duplicating them into the index
            # would mean two places to keep in sync and a much larger object.
            "first_segment": first.get("id"),
            "last_segment": last.get("id"),
            "segment_count": len(group),
            "start": _seg_time(first, "start"),
            "end": _seg_time(last, "end", _seg_time(last, "start")),
            "speakers": _speakers(group),
            "summary": _summarise(group),
        })

    parents = []
    for i in range(0, len(nodes), LEAVES_PER_PARENT):
        chunk = nodes[i:i + LEAVES_PER_PARENT]
        if not chunk:
            continue
        parents.append({
            "node_id": f"part_{i // LEAVES_PER_PARENT}",
            "children": [n["node_id"] for n in chunk],
            "start": chunk[0]["start"],
            "end": chunk[-1]["end"],
        })

    return {
        "version": PAGEINDEX_VERSION,
        "nodes": nodes,
        "parents": parents,
        "segment_count": len(segments),
        "transcript_chars": len(transcript or ""),
    }


# ---------------------------------------------------------------------------
# OPTIONAL LLM node summaries.
#
# Off by default — see the module docstring for why. When enabled, this
# REPLACES the extractive `summary` on each leaf and changes nothing else, so
# an index built with the flag on and one built with it off are interchangeable
# as far as every reader is concerned.
# ---------------------------------------------------------------------------
_LLM_SUMMARY_SYSTEM = (
    "You label sections of a meeting transcript. Given one passage, reply with "
    "a single line of at most 15 words naming what is DISCUSSED or DECIDED in "
    "it. No preamble, no quotes, no speaker names. If the passage is small "
    "talk or has no substance, reply exactly: (no substantive content)"
)


def llm_summaries_enabled():
    return os.environ.get("PAGEINDEX_LLM_SUMMARY", "").strip() in ("1", "true", "yes")


def add_llm_summaries(tree, segments_by_node, groq, deadline=None):
    """Replace each leaf's extractive summary with a model-written one.

    Best-effort PER NODE: a node whose call fails keeps the extractive summary
    it already has, because a tree with a few plainer descriptions still
    retrieves, whereas aborting the build would leave the meeting with no index
    at all. That asymmetry is the whole reason this is written as a loop of
    independent calls rather than one batched call.
    """
    for node in tree.get("nodes", []):
        segs = segments_by_node.get(node["node_id"]) or []
        passage = "\n".join(_clean_text(s.get("text")) for s in segs
                            if isinstance(s, dict))
        if not passage.strip():
            continue
        try:
            reply = groq.complete(
                _LLM_SUMMARY_SYSTEM, passage[:6000],
                label="pageindex-summary", json_mode=False, temperature=0.1,
                deadline=deadline,
            )
        except Exception as err:  # noqa: BLE001
            print(f"[pageindex] LLM summary failed for {node['node_id']}: {err}")
            continue
        reply = _clean_text(reply)
        if reply:
            node["summary"] = reply[:NODE_SUMMARY_CHARS]
    return tree


# ---------------------------------------------------------------------------
# RETRIEVAL — the table of contents the navigator reads, and the node picker.
# ---------------------------------------------------------------------------
def _mmss(seconds):
    """Seconds as M:SS / H:MM:SS — how the app already shows a timestamp."""
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return "0:00"
    total = max(0, total)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def table_of_contents(tree, speaker_names=None):
    """The tree rendered for the navigator model.

    One line per leaf: id, time span, who spoke, what it is about. This is the
    ONLY thing the navigation call pays for — a 2-hour meeting's whole index
    reads as ~30 lines here, versus the ~120k characters of transcript it
    stands in for. That ratio is the entire economic argument for the module.

    Speaker DISPLAY NAMES are resolved here, at read time, from the row's
    current mapping — never baked into the tree. So renaming a speaker changes
    what the model reads on the very next question with no rebuild (Part 18).
    """
    names = speaker_names if isinstance(speaker_names, dict) else {}
    lines = []
    for node in tree.get("nodes", []):
        who = ", ".join(names.get(str(sp), f"Speaker {sp}")
                        for sp in node.get("speakers", [])) or "—"
        span = f"{_mmss(node.get('start'))}–{_mmss(node.get('end'))}"
        summary = node.get("summary") or "(no description)"
        lines.append(f"{node['node_id']} [{span}] ({who}): {summary}")
    return "\n".join(lines)


def _segment_index(seg_id):
    """The integer position behind a `seg_N` id, or None.

    Parsed rather than stored because transcript_store DERIVES the id from
    position in the first place — recomputing it here keeps one definition of
    that relationship instead of two that could drift.
    """
    prefix = transcript_store.SEGMENT_ID_PREFIX
    text = str(seg_id or "")
    if not text.startswith(prefix):
        return None
    try:
        return int(text[len(prefix):])
    except ValueError:
        return None


def segments_for_nodes(tree, timestamps, node_ids):
    """The transcript segments covered by `node_ids`, in meeting order.

    Deduplicated across overlapping/repeated node ids, and returned as the
    hydrated segments themselves so every one still carries its `seg_N`, its
    speaker and its start/end — the exact fields the evidence contract and the
    transcript deep-link need. Unknown node ids are IGNORED rather than raising:
    they are model output, and a hallucinated id must degrade to "that node
    contributed nothing", never to a 500 on the user's question.
    """
    segments = transcript_store.with_segment_ids(timestamps)
    by_id = {n["node_id"]: n for n in tree.get("nodes", [])}

    wanted = set()
    for node_id in node_ids or []:
        node = by_id.get(str(node_id).strip())
        if not node:
            continue
        lo = _segment_index(node.get("first_segment"))
        hi = _segment_index(node.get("last_segment"))
        if lo is None or hi is None:
            continue
        wanted.update(range(lo, hi + 1))

    return [seg for i, seg in enumerate(segments)
            if i in wanted and isinstance(seg, dict)]


def render_segments(segments, speaker_names=None):
    """Retrieved segments as labelled transcript lines for the prompt.

    Same `[seg_N] Speaker: text` shape transcript_store.as_labelled_lines
    produces for the full-transcript path, so the model reads ONE format
    whichever way its context was built and the evidence instructions in the
    prompt hold in both cases.
    """
    names = speaker_names if isinstance(speaker_names, dict) else {}
    lines = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        sp = str(seg.get("speaker", ""))
        who = names.get(sp, f"Speaker {sp}")
        lines.append(f"[{seg.get('id')}] {who}: {_clean_text(seg.get('text'))}")
    return "\n".join(lines)


def evidence_from_segments(segments, limit=8):
    """The public `sources` array for an answer.

    Only the four fields the app needs to render a source and jump to it:
    segment id, speaker, start, end. No node ids, no summaries, no tree
    internals — those are retrieval mechanics and leaking them would make the
    index's shape part of the API contract (Part 11's rule), which would then
    be expensive to change.
    """
    out = []
    for seg in segments[:limit]:
        if not isinstance(seg, dict) or not seg.get("id"):
            continue
        out.append({
            "segment_id": seg["id"],
            "speaker_id": str(seg.get("speaker", "")),
            "start_time": _seg_time(seg, "start"),
            "end_time": _seg_time(seg, "end", _seg_time(seg, "start")),
        })
    return out


def parse_node_ids(reply, tree):
    """Node ids out of the navigator's reply, filtered to ones that exist.

    Accepts the JSON object we ask for, and falls back to scraping `node_N`
    tokens out of prose — models wrap JSON in explanations often enough that
    failing the whole retrieval over a stray ``` is not worth it. Filtering
    against the tree is what makes a hallucinated id harmless.
    """
    valid = {n["node_id"] for n in tree.get("nodes", [])}
    text = str(reply or "")

    ids = []
    try:
        data = json.loads(text)
        raw = data.get("nodes") if isinstance(data, dict) else data
        if isinstance(raw, list):
            ids = [str(x).strip() for x in raw]
    except (ValueError, AttributeError):
        pass

    if not ids:
        ids = re.findall(r"node_\d+", text)

    seen, out = set(), []
    for node_id in ids:
        if node_id in valid and node_id not in seen:
            seen.add(node_id)
            out.append(node_id)
    return out
