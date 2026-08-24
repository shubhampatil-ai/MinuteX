"""stt_result — parse an ElevenLabs Scribe result into MinuteX's transcript shape.

WHY THIS IS SHARED
------------------
Two Lambdas now turn an ElevenLabs `words[]` payload into the pair MinuteX
stores everywhere:

  * transcribeRecording — for a transcript it fetched itself (reconciliation).
  * userApi             — for a transcript delivered to the STT webhook.

Both must produce the SAME text and the SAME segments, because the app's
tap-to-seek maps a timestamp segment onto the diarized line at the same index.
A second copy of this grouping logic would eventually disagree about where one
speaker's turn ends — and the symptom would be playback jumping to the wrong
sentence, which is very hard to trace back to a duplicated helper. So there is
one implementation, vendored into both zips like the rest of lambda-shared.

Extracted VERBATIM from transcribeRecording, which has run this code in
production since the ElevenLabs rollout. Not a rewrite: the speaker-label
normalization, the word filtering, the turn grouping and the Decimal rounding
are all the originals.

THE SHAPE
---------
    transcript  "Speaker 0: ...

Speaker 1: ..."   (diarized, one line/turn)
    timestamps  [{speaker, start, end, text}, ...]     (one per turn, REAL
                                                        ElevenLabs word timing)
    language    the detected language_code, or "unknown"

Decimal (not float) for start/end: DynamoDB cannot serialize a Python float,
and these segments used to be written straight onto the item.
"""
import json  # noqa: F401  (kept for parity with the callers' expectations)
from decimal import Decimal


def _speaker_label(speaker_id):
    """Normalize an ElevenLabs speaker_id into a compact label.
    "speaker_0" -> "0"; other ids (e.g. "agent"/"customer") pass through."""
    if isinstance(speaker_id, str) and speaker_id.startswith("speaker_"):
        return speaker_id[len("speaker_"):]
    return str(speaker_id) if speaker_id is not None else "?"


def _real_words(result):
    """The transcribable tokens from an ElevenLabs response: words[] entries
    whose type == "word" (drop "spacing" and "audio_event")."""
    words = result.get("words") if isinstance(result, dict) else None
    if not isinstance(words, list):
        return []
    return [w for w in words if isinstance(w, dict) and w.get("type") == "word"]


# Build a diarized "Speaker N: ..." transcript from ElevenLabs words. Group
# consecutive real words by speaker_id into speaker-turn lines. Falls back to
# the flat `text` if there are no speaker_ids at all.
def build_diarized_text(result):
    words = _real_words(result)
    if not (words and any(w.get("speaker_id") for w in words)):
        return (result.get("text") or "").strip() if isinstance(result, dict) else ""
    lines, cur, buf = [], None, []
    for w in words:
        spk = w.get("speaker_id")
        tok = (w.get("text") or "").strip()
        if not tok:
            continue
        if spk != cur:
            if buf:
                lines.append(f"Speaker {_speaker_label(cur)}: {' '.join(buf)}")
            cur, buf = spk, [tok]
        else:
            buf.append(tok)
    if buf:
        lines.append(f"Speaker {_speaker_label(cur)}: {' '.join(buf)}")
    return "\n\n".join(lines)


def _round(x):
    """Round seconds to 2dp and store as a Decimal — DynamoDB cannot serialize
    Python floats, but boto3 accepts Decimal for numeric attrs."""
    try:
        return Decimal(str(round(float(x), 2)))
    except (TypeError, ValueError):
        return Decimal("0")


# Build the timestamps array: one {speaker, start, end, text} segment per
# speaker turn, with REAL ElevenLabs word timing (not LLM-guessed). Merges
# consecutive same-speaker words so the timeline matches the diarized lines.
def build_timestamps(result):
    words = _real_words(result)
    if not words:
        return []
    segs, cur, buf, start, end = [], None, [], None, None
    for w in words:
        spk = w.get("speaker_id")
        tok = (w.get("text") or "").strip()
        if not tok:
            continue
        if spk != cur:
            if buf:
                segs.append({"speaker": _speaker_label(cur), "start": _round(start),
                             "end": _round(end), "text": " ".join(buf)})
            cur, buf, start = spk, [tok], w.get("start")
        else:
            buf.append(tok)
        end = w.get("end")
    if buf:
        segs.append({"speaker": _speaker_label(cur), "start": _round(start),
                     "end": _round(end), "text": " ".join(buf)})
    return segs


def parse(result):
    """(transcript, timestamps, language) from one ElevenLabs result object.

    Accepts either the webhook's `data.transcription` object or the synchronous
    /v1/speech-to-text response body — they carry the same fields, which is why
    a reconciled job and a webhook-delivered one produce identical rows.
    """
    transcript = build_diarized_text(result)
    timestamps = build_timestamps(result)
    language = (result.get("language_code")
                if isinstance(result, dict) else None) or "unknown"
    return transcript, timestamps, language
