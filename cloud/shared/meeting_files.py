"""meeting_files.py — the files a linked meeting sends to Salesforce.

Pure: no AWS. Two files, both optional and both sent only with their output
(contract_v3.FILE_OUTPUT):

  * the TRANSCRIPT as plain UTF-8 text, built here from the stored
    transcript, its timestamps and the speakers' tagged names;
  * the MoM as a PDF, rendered BY THE APP (the same PDF it shares and
    exports) and uploaded once per Link; validated here.

The transcript reads like this:

    Site visit · Mumbai Heights — 30 Sep 2026, 10:00 (42 min)

    [00:00:05] Amit Verma: Welcome to Mumbai Heights…
    [00:00:12] Speaker 3: What about parking?
"""
import re

import transcript_store

PDF_MAGIC = b"%PDF-"
_UNSAFE_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def safe_filename(name, ext, fallback="MinuteX meeting"):
    """A file name Salesforce and every OS accept: no path characters,
    at most 120 characters before the extension."""
    base = _UNSAFE_NAME.sub(" ", str(name or "")).strip() or fallback
    base = re.sub(r"\s+", " ", base)[:120].strip()
    return f"{base}.{ext}"


def clock(seconds):
    """[hh:mm:ss] for a start time in seconds; "" when unknown."""
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    if total < 0:
        return ""
    return f"[{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}]"


def transcript_text(*, transcript, timestamps, speaker_names):
    """The transcript as readable text ("" when there's none): one turn per
    line, with its start time and the speaker's tagged name.

    Times and names are added only when the transcript lines and timestamps
    line up; otherwise the stored text is used as it is (still readable,
    just without them). A name is applied only where the line's own
    "Speaker N:" prefix matches, so one person's words are never
    attributed to another."""
    text = str(transcript or "").strip()
    if not text:
        return ""
    lines = [ln for ln in text.split("\n") if ln.strip()]
    names = speaker_names if isinstance(speaker_names, dict) else {}
    out = []
    if isinstance(timestamps, list) and len(timestamps) == len(lines):
        for line, seg in zip(lines, timestamps):
            named = transcript_store._named_line(line, seg, names) if names else line
            stamp = clock((seg or {}).get("start")) if isinstance(seg, dict) else ""
            out.append(f"{stamp} {named}".strip())
    else:
        out = lines
    return "\n".join(out)


def transcript_txt(*, title, header_detail, transcript, timestamps, speaker_names):
    """The transcript as a .txt file's bytes (UTF-8) — a title line, then
    transcript_text() — or b"" when there's no transcript."""
    body = transcript_text(transcript=transcript, timestamps=timestamps,
                           speaker_names=speaker_names)
    if not body:
        return b""
    head = str(title or "MinuteX meeting").strip()
    if header_detail:
        head = f"{head} — {header_detail}"
    return (head + "\n\n" + body + "\n").encode("utf-8")


def valid_pdf(content):
    """True for bytes that start like a PDF."""
    return isinstance(content, (bytes, bytearray)) and bytes(content[:5]) == PDF_MAGIC
