"""Transcript storage: the transcript + timestamps live in S3, not DynamoDB.

WHY THIS MODULE EXISTS
----------------------
A recording used to store its whole transcript inline on the DynamoDB row, in
TWO shapes at once:

  * `transcript`  — the flat diarized text ("Speaker 1: ...\n")
  * `timestamps`  — the SAME words again, split into per-speaker-turn segments
                    with start/end times

On a real 45-minute meeting that measured 151 KB + 173 KB = 96% of DynamoDB's
HARD 400 KB per-item limit, with the ~10 KB of actual analysis (summary,
highlights, tasks, agenda) squeezed into what was left. The next slightly
longer recording crossed 400 KB and UpdateItem failed with:

    ValidationException: Item size to update has exceeded the maximum allowed size

That failure was not merely a lost write. The pipeline stamps small status
rows as it goes ("transcribing" -> "generating_ai"), and those fit; only the
final big write failed. So the row was stranded at `generating_ai` forever and
the app showed "writing the summary" that never finished — after ElevenLabs and
Groq had already been paid for work that was then thrown away. Worse, the
exception propagated, S3/Lambda retried the whole invocation, and it re-ran the
paid stages only to fail at the identical write.

Two properties of the old layout made this inevitable rather than unlucky:

  1. The text was stored TWICE. The segments' text and the flat transcript are
     the same words; the only difference is the "Speaker N: " prefixes and the
     newlines between turns.
  2. DynamoDB counts UTF-8 BYTES, not characters. That 151 KB was only ~46k
     chars — Devanagari at ~3 bytes/char. A Hindi/Marathi meeting therefore hit
     the ceiling at roughly a third the duration an English one would, which is
     precisely the language the live users speak.

So the fix is not a smaller field or a tighter budget: it is to stop keeping
the transcript in the item at all. It now lives in one S3 object next to the
audio, and the row keeps a POINTER plus small counts. The item drops to ~10 KB
and recording length stops being a correctness concern.

THE CONTRACT
------------
S3 object (JSON, gzipped — see `_dumps`):

    {"transcript": "...", "timestamps": [{speaker,start,end,text}, ...]}

DynamoDB row keeps only:

    transcript_s3_key   pointer to that object   (absent on legacy rows)
    transcript_chars    len(transcript)          — so list views can say
    timestamps_count    len(timestamps)            "has a transcript" cheaply

`hydrate()` is the ONE read path. It returns an item whose `transcript` and
`timestamps` keys are populated exactly as they were when they lived inline, so
every existing reader — get_recording's response, prompts.build_context, the
`_require_transcript` callers, the fingerprint calls — keeps working unchanged,
and the mobile app's JSON shape is byte-identical (no new app build needed).

LEGACY ROWS
-----------
Rows written before this change still hold `transcript`/`timestamps` inline and
have no `transcript_s3_key`. hydrate() serves those from the item as-is. That
fallback is load-bearing: without it every pre-existing recording would show an
empty transcript in the app. It is keyed on "is the inline value present", not
on a migration flag, so inline and offloaded rows can coexist indefinitely and
no backfill is required.

WRITE ORDER
-----------
Callers MUST write S3 first, then DynamoDB (see `put`). Two stores are not
atomic: an orphaned S3 object is harmless garbage, but a row pointing at an
object that was never written is a recording whose transcript is gone. Ordering
the writes so the pointer is only ever stamped after its target exists makes
the failure mode the harmless one.
"""

import gzip
import json
import os
from decimal import Decimal

# Where the transcript object sits, relative to the audio key. Deriving it from
# the audio key (rather than storing a uuid) means the object is findable from
# the key alone during debugging, and a re-transcribe overwrites in place
# instead of leaking a new object per run.
TRANSCRIPT_PREFIX = os.environ.get("TRANSCRIPT_PREFIX", "transcripts")

# Gzip level 6: the default speed/ratio knee. Diarized transcripts are highly
# repetitive ("Speaker 1:" every few lines, and the segment text repeats the
# flat text verbatim) so they compress ~5-8x, which keeps the S3 GET on the
# recording-detail path small enough not to be felt.
_GZIP_LEVEL = 6


def s3_key_for(audio_key):
    """The transcript object key for a given audio key.

    Suffixed rather than extension-swapped so two uploads that differ only by
    extension ("meeting.m4a" / "meeting.wav") cannot collide onto one
    transcript object.
    """
    return f"{TRANSCRIPT_PREFIX}/{audio_key}.transcript.json.gz"


def _json_default(o):
    """Make DynamoDB's Decimal serializable.

    A timestamp segment read back from DynamoDB carries `start`/`end` as
    Decimal (boto3's resource layer converts every DynamoDB N to Decimal), and
    json.dumps raises TypeError on it. Without this the FIRST offloaded write
    would fail outright.

    Emitted as a JSON number, not a string: the app does Number(seg.start) for
    tap-to-seek, and "12.34" would coerce fine while a stringified Decimal like
    "1E+1" would not. int when integral so a whole second is `12`, not `12.0`.
    """
    if isinstance(o, Decimal):
        f = float(o)
        return int(f) if f.is_integer() else f
    raise TypeError(f"Object of type {o.__class__.__name__} is not JSON serializable")


def _dumps(payload):
    # ensure_ascii=False keeps Devanagari as real UTF-8 rather than \uXXXX
    # escapes, which would inflate the object ~2x before gzip even runs.
    raw = json.dumps(payload, ensure_ascii=False,
                     default=_json_default).encode("utf-8")
    return gzip.compress(raw, _GZIP_LEVEL)


def _loads(body):
    # Tolerate an uncompressed object: earlier hand-written objects and any
    # future plain-JSON writer stay readable, since a wrong guess here would
    # blank a transcript the app depends on.
    if body[:2] == b"\x1f\x8b":
        body = gzip.decompress(body)
    # Plain floats, NOT parse_float=Decimal: this data goes into an HTTP
    # response, not back into DynamoDB, and the API's JSON encoder would choke
    # on Decimal the same way _dumps did. Timestamps are display/seek values —
    # float precision is not a concern the way it would be for money.
    return json.loads(body.decode("utf-8"))


def put(s3, bucket, audio_key, transcript, timestamps):
    """Write the transcript object and return the fields to stamp on the row.

    Call this BEFORE the DynamoDB update — see WRITE ORDER above. The returned
    dict is meant to be merged straight into the update's field map.
    """
    key = s3_key_for(audio_key)
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=_dumps({"transcript": transcript, "timestamps": timestamps}),
        ContentType="application/json",
        ContentEncoding="gzip",
    )
    return {
        "transcript_s3_key": key,
        # Counts, not the content: they let the list view and the app decide
        # "does this recording have a transcript / how long is it" without
        # anyone paying for an S3 GET.
        "transcript_chars": len(transcript or ""),
        "timestamps_count": len(timestamps or []),
    }


def fetch(s3, bucket, s3_key):
    """(transcript, timestamps) from S3, or ("", []) if unreadable.

    Never raises. A missing or corrupt transcript object must degrade to "no
    transcript yet" — which the callers already handle with a 409 and the app
    already renders as a progress state — rather than 500 the whole detail
    view and take the summary, highlights and playback down with it.
    """
    try:
        body = s3.get_object(Bucket=bucket, Key=s3_key)["Body"].read()
        data = _loads(body)
    except Exception as err:  # noqa: BLE001
        print(f"[transcript_store] fetch FAILED for {s3_key}: {err}")
        return "", []

    transcript = data.get("transcript") or ""
    timestamps = data.get("timestamps")
    if not isinstance(timestamps, list):
        timestamps = []
    return transcript, timestamps


# ---------------------------------------------------------------------------
# SEGMENT IDs — derived at read time, never stored.
#
# The AI grounding layer needs to point at a specific moment of the meeting
# ("this claim comes from these turns"), and a stable handle per segment is
# what makes that checkable. The obvious implementation — write an `id` onto
# every segment — was rejected: it would mean an S3 migration over every
# existing transcript object to make old recordings groundable, and a write
# path that can now disagree with a read path about what a segment is called.
#
# The IDs are POSITIONAL instead ("seg_0", "seg_1", ...), so they are a pure
# function of the segment list and identical for a legacy object and a fresh
# one. Nothing has to be migrated, and an ID never has to be reconciled.
#
# Position is a safe identity here BECAUSE the segment list is immutable in
# practice: it is written once by the STT stage and only ever replaced
# wholesale by a re-transcription — which also rewrites the analysis (and its
# evidence references) in the same invocation, so the two can never drift
# apart. There is no code path that inserts, deletes or reorders one segment.
#
# STRICTLY ADDITIVE. Ordering, boundaries, `start`/`end` and `text` are
# untouched, because the app aligns transcript lines to timestamps by POSITION
# and seeks on seg.start — a change to any of those would break tap-to-seek.
# ---------------------------------------------------------------------------
SEGMENT_ID_PREFIX = "seg_"


def segment_id(index):
    """The stable ID for the segment at `index`. One definition, used by the
    writer of evidence references and their validator alike."""
    return f"{SEGMENT_ID_PREFIX}{index}"


def with_segment_ids(timestamps):
    """`timestamps` with a derived `id` on every segment.

    Returns a new list of shallow-copied segments — the input is never mutated,
    so a caller holding the raw S3 payload is unaffected. A non-dict element is
    passed through untouched rather than dropped: this function's job is to add
    a field, not to validate a shape someone else already tolerates.

    An `id` already present is LEFT ALONE. Nothing writes one today, but that
    makes the function idempotent, so hydrating twice (or hydrating an item that
    already went through here) can never renumber a segment out from under an
    evidence reference that points at it.
    """
    if not isinstance(timestamps, list):
        return []
    out = []
    for i, seg in enumerate(timestamps):
        if not isinstance(seg, dict):
            out.append(seg)
            continue
        if seg.get("id"):
            out.append(seg)
            continue
        out.append({**seg, "id": segment_id(i)})
    return out


def hydrate(s3, bucket, item):
    """Item with `transcript`/`timestamps` filled in, whatever the storage.

    The single read path for both layouts:
      * offloaded row (has transcript_s3_key) -> fetched from S3
      * legacy row (inline attributes)        -> returned as-is

    Every segment comes back carrying a derived `id` (see with_segment_ids), on
    both paths and for objects written long before IDs existed — which is what
    lets the AI's evidence references resolve against the back catalogue
    without a migration.

    Returns a SHALLOW COPY: the caller's item is left untouched, so a hydrated
    item is never accidentally written back to DynamoDB with the transcript
    re-inlined (which would reintroduce the 400 KB failure this module exists
    to remove).
    """
    if not isinstance(item, dict):
        return item

    # Inline content wins. Legacy rows are served without an S3 round-trip,
    # and a row mid-migration that still has both stays readable.
    if (item.get("transcript") or "").strip():
        # Still a COPY, not the caller's item: stamping IDs onto the original
        # would mutate a dict the caller may be about to write back.
        out = dict(item)
        out["timestamps"] = with_segment_ids(item.get("timestamps"))
        return out

    s3_key = item.get("transcript_s3_key")
    if not s3_key or not bucket:
        return item

    transcript, timestamps = fetch(s3, bucket, s3_key)
    out = dict(item)
    out["transcript"] = transcript
    out["timestamps"] = with_segment_ids(timestamps)
    return out


def strip_for_write(fields):
    """Drop the offloaded attributes from a DynamoDB field map.

    Belt-and-braces for write paths that build their update from a previously
    hydrated item: if `transcript`/`timestamps` slipped back in, writing them
    would restore the oversized item. Cheaper to strip here than to audit
    every future caller.
    """
    return {k: v for k, v in fields.items()
            if k not in ("transcript", "timestamps")}


# ---------------------------------------------------------------------------
# The transcript AS THE MODEL SEES IT.
#
# WHY THIS EXISTS. Segment ids have been derivable since with_segment_ids()
# was written, and the task schema has always had a place to put them — but the
# model was handed the PLAIN prose transcript ("Speaker 0: ...") built by
# stt_result.build_diarized_text, which contains no ids at all. So the prompt
# asked for evidence_segment_ids, the model had nothing to copy, and every real
# extraction came back with []. The plumbing on both sides was complete and the
# middle was missing.
#
# WHY THE IDS CAN BE DERIVED RATHER THAN STORED. stt_result builds the prose
# lines and the timestamps array from the SAME word stream with the SAME
# speaker-turn grouping (build_diarized_text and build_timestamps are the same
# loop), so line N is always segment N. Verified against 10 real production
# transcripts: line count == segment count and the texts match, on every one.
# That is what makes `seg_{index}` stable across rendering, extraction, storage
# and later viewing without persisting a second copy of anything.
#
# WHAT THE MODEL IS GIVEN, and nothing more:
#
#     [seg_0] Speaker 0: Right, where are we on the proposal?
#     [seg_1] Speaker 1: I'll send it tomorrow.
#
# No start/end times (the model has no use for them and they cost tokens on
# every line of a 1500-segment meeting), no database ids, no internal keys.
# The id and the speaker label are exactly what an evidence reference needs.
#
# FORMAT. The id is a PREFIX on the existing line rather than a separate line,
# because it keeps the transcript's shape — one turn per line — which is what
# the rest of the prompt's rules are written against. Reformatting it into
# blocks would have meant re-tuning wording that is already proven.
def as_labelled_lines(transcript, timestamps):
    """The transcript with `[seg_N]` on each line, for the model to quote.

    Falls back to the plain transcript whenever the two sides do not line up —
    a legacy row with no timestamps, an offloaded transcript that failed to
    hydrate, or a count mismatch from some future STT change. A mismatched
    label is far worse than no label: it would point evidence at the wrong
    moment of the meeting while looking perfectly valid, and the validator
    downstream cannot catch that because the id would genuinely exist.

    Degrading here costs only the evidence REFERENCES; the quote, the tasks and
    the whole analysis are unaffected.
    """
    text = str(transcript or "")
    if not isinstance(timestamps, list) or not timestamps:
        return text

    lines = text.split("\n")
    # The prose joins turns with a blank line ("\n\n"), so drop the blanks
    # before pairing. Positions are what carry the identity.
    content = [i for i, ln in enumerate(lines) if ln.strip()]
    if len(content) != len(timestamps):
        return text

    out = list(lines)
    for position, line_index in enumerate(content):
        out[line_index] = f"[{segment_id(position)}] {lines[line_index]}"
    return "\n".join(out)


def valid_segment_ids(timestamps):
    """The set of ids that actually exist for this meeting.

    The one place the "which ids are real?" question is answered, so the
    prompt's input and the validator's allow-list can never disagree about what
    a valid reference is.
    """
    if not isinstance(timestamps, list):
        return set()
    return {seg["id"] for seg in with_segment_ids(timestamps)
            if isinstance(seg, dict) and seg.get("id")}
