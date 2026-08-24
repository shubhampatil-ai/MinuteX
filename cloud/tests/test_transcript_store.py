#!/usr/bin/env python3
# =============================================================
# test_transcript_store.py — transcript + timestamps stored in S3, not
# DynamoDB (the 400 KB item-size fix).
#
# THE BUG THIS LOCKS DOWN
#   A recording used to keep its transcript inline on the DynamoDB row in two
#   shapes at once: `transcript` (flat diarized text) and `timestamps` (the SAME
#   words again, as per-speaker-turn segments). On a real 45-minute meeting that
#   measured 151 KB + 173 KB = 96% of DynamoDB's hard 400 KB per-item limit, and
#   the next longer recording failed the final UpdateItem with
#   "Item size to update has exceeded the maximum allowed size". Because the
#   small status writes ("transcribing" -> "generating_ai") had already
#   succeeded, the row was stranded at generating_ai forever and the app showed
#   "writing the summary" that never finished — after ElevenLabs and Groq were
#   already paid. DynamoDB counts UTF-8 BYTES, so Devanagari (~3 bytes/char) hit
#   the ceiling at roughly a third the duration English would.
#
# UNIT (offline, always runs — fakes for S3/DynamoDB, no AWS calls):
#   1.  put() writes the object and returns pointer + counts.
#   2.  hydrate() restores an offloaded row's transcript/timestamps.
#   3.  LEGACY row (inline, no pointer) is served as-is with ZERO S3 GETs —
#       the fallback that keeps pre-existing recordings readable.
#   4.  hydrate() does not mutate the caller's item (a hydrated item must never
#       be written back with the transcript re-inlined).
#   5.  Missing/corrupt S3 object degrades to ("", []) instead of raising.
#   6.  Decimal start/end (what boto3 returns) survive as JSON NUMBERS —
#       json.dumps raises TypeError on Decimal, so without this the first
#       offloaded write would fail; and a stringified Decimal ("1E+1") would
#       reach the app's Number(seg.start) as NaN and break tap-to-seek.
#   7.  gzip round-trip is lossless, and an uncompressed object still reads.
#   8.  Devanagari survives (the language that triggered the bug).
#   9.  strip_for_write() drops the offloaded attributes.
#   10. A row with BOTH inline and pointer prefers inline (mid-migration).
#   11. Offloaded item is far under 400 KB where the inline one blew past it.
#   12. get_recording returns transcript+timestamps for an OFFLOADED row —
#       the app's JSON shape is unchanged, so no app rebuild is needed.
#   13. get_recording still works for a LEGACY inline row.
#   14. patch_recording (speaker rename) returns a HYDRATED row: the app adopts
#       this response and renders its Transcript tab from `timestamps`, so a
#       bare row would blank that tab the moment a speaker is renamed.
#   15. _row_fingerprint prefers the stamped fingerprint (no S3 read) and falls
#       back to hashing an inline transcript for legacy rows.
#   16. Ownership is checked BEFORE hydrating (no S3 spend for a stranger).
#
# LIVE (real AWS, opt-in with --live): none — the end-to-end path is covered by
# scripts/26_reprocess_stuck.py and test_ai_workspace.py.
# =============================================================
import importlib.util
import json
import os
import sys
import uuid
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_tests import check, _results  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

os.environ.setdefault("JWT_SECRET", "unit-test-secret")
os.environ.setdefault("BUCKET_NAME", "unit-test-bucket")
os.environ.setdefault("AWS_REGION", "ap-south-1")

# Same flat-import trick as test_recording_sources.py: the shared modules are
# vendored into each zip, so shared/ goes on sys.path.
sys.path.insert(0, str(ROOT / "shared"))

import transcript_store as tstore  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "userapi", ROOT / "functions/userapi" / "lambda_function.py")
userapi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(userapi)


# ---------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------
class FakeS3:
    """Records puts, counts gets, and can be made to fail."""

    def __init__(self, fail=False):
        self.objects = {}
        self.fail = fail
        self.gets = 0

    def put_object(self, Bucket, Key, Body, **kw):
        self.objects[Key] = Body
        self.meta = kw

    def get_object(self, Bucket, Key):
        self.gets += 1
        if self.fail or Key not in self.objects:
            raise RuntimeError("NoSuchKey")

        class _Body:
            def __init__(self, data):
                self._data = data

            def read(self):
                return self._data

        return {"Body": _Body(self.objects[Key])}

    def generate_presigned_url(self, op, Params=None, ExpiresIn=None):
        return f"https://unit-test.s3/{Params['Key']}?sig=test"


class FakeRecordings:
    def __init__(self, items=None):
        self.items = dict(items or {})
        self.updates = []

    def get_item(self, Key):
        item = self.items.get(Key["audio_s3_key"])
        return {"Item": item} if item else {}

    def update_item(self, **kwargs):
        self.updates.append(kwargs)
        return {}


SEGS = [
    {"speaker": "0", "start": Decimal("0.12"), "end": Decimal("25.66"),
     "text": "Let us begin the review."},
    {"speaker": "1", "start": Decimal("25.66"), "end": Decimal("31"),
     "text": "Numbers are up eleven percent."},
]
TEXT = "Speaker 1: Let us begin the review.\nSpeaker 2: Numbers are up eleven percent."


def _event(token, key):
    return {"headers": {"authorization": f"Bearer {token}"},
            "pathParameters": {"key": key}}


def resp_body(resp):
    return json.loads(resp["body"])


# ---------------------------------------------------------------
def unit_tests():
    # --- 1. put() ----------------------------------------------------------
    s3 = FakeS3()
    fields = tstore.put(s3, "buck", "esp32-001/a.wav", TEXT, SEGS)
    ok = (fields["transcript_s3_key"] == tstore.s3_key_for("esp32-001/a.wav")
          and fields["transcript_chars"] == len(TEXT)
          and fields["timestamps_count"] == 2
          and fields["transcript_s3_key"] in s3.objects
          # never the content itself — that is the whole point
          and "transcript" not in fields and "timestamps" not in fields)
    check("put() stores object, returns pointer + counts only", ok, str(fields)[:90])

    # --- 2. hydrate() an offloaded row -------------------------------------
    row = {"audio_s3_key": "esp32-001/a.wav", "status": "complete", **fields}
    h = tstore.hydrate(s3, "buck", row)
    ok = h["transcript"] == TEXT and len(h["timestamps"]) == 2
    check("hydrate() restores transcript + timestamps from S3", ok,
          f"{len(h['transcript'])} chars, {len(h['timestamps'])} segs")

    # --- 3. LEGACY row: inline, no pointer, no S3 --------------------------
    s3.gets = 0
    legacy = {"audio_s3_key": "old.wav", "transcript": "Speaker 1: legacy",
              "timestamps": SEGS}
    h = tstore.hydrate(s3, "buck", legacy)
    ok = h["transcript"] == "Speaker 1: legacy" and s3.gets == 0
    check("LEGACY inline row served as-is with 0 S3 GETs", ok,
          f"s3.gets={s3.gets}")

    # --- 4. no mutation ----------------------------------------------------
    row2 = {"audio_s3_key": "esp32-001/a.wav", **fields}
    tstore.hydrate(s3, "buck", row2)
    ok = "transcript" not in row2 and "timestamps" not in row2
    check("hydrate() leaves the caller's item slim (no re-inline on write-back)",
          ok, str(sorted(row2))[:80])

    # --- 5. missing object degrades ---------------------------------------
    broken = {"audio_s3_key": "x", "status": "complete",
              "transcript_s3_key": "transcripts/gone.json.gz"}
    h = tstore.hydrate(s3, "buck", broken)
    ok = h["transcript"] == "" and h["timestamps"] == []
    check("missing/corrupt S3 object -> ('', []) not an exception", ok)

    # --- 6. Decimal -> JSON number ----------------------------------------
    back = tstore._loads(tstore._dumps({"transcript": TEXT, "timestamps": SEGS}))
    got = back["timestamps"]
    ok = (isinstance(got[0]["start"], (int, float))
          and not isinstance(got[0]["start"], str)
          and abs(got[0]["start"] - 0.12) < 1e-9
          # Decimal("31") is integral -> int 31, never "3E+1"
          and got[1]["end"] == 31
          and float(got[1]["end"]) == float(got[1]["end"]))  # not NaN
    check("Decimal start/end serialize as JSON numbers (Number() safe)", ok,
          f"start={got[0]['start']!r} end={got[1]['end']!r}")

    # --- 7. gzip round-trip + plain-JSON tolerance ------------------------
    blob = tstore._dumps({"transcript": TEXT, "timestamps": SEGS})
    plain = json.dumps({"transcript": TEXT, "timestamps": []}).encode()
    ok = (blob[:2] == b"\x1f\x8b"
          and tstore._loads(blob)["transcript"] == TEXT
          and tstore._loads(plain)["transcript"] == TEXT)
    check("gzip round-trip lossless; uncompressed object still readable", ok,
          f"{len(blob)}B gzipped")

    # --- 8. Devanagari ----------------------------------------------------
    hi = "वक्ता १: बैठक शुरू करते हैं। राजस्व ग्यारह प्रतिशत बढ़ा।"
    segs_hi = [{"speaker": "0", "start": Decimal("0"), "end": Decimal("4.5"),
                "text": hi}]
    back = tstore._loads(tstore._dumps({"transcript": hi, "timestamps": segs_hi}))
    ok = back["transcript"] == hi and back["timestamps"][0]["text"] == hi
    check("Devanagari survives round-trip (the language that broke first)", ok)

    # --- 9. strip_for_write ------------------------------------------------
    ok = tstore.strip_for_write(
        {"transcript": "x", "timestamps": [], "summary": "s"}) == {"summary": "s"}
    check("strip_for_write() drops transcript/timestamps", ok)

    # --- 10. both present -> inline wins ----------------------------------
    s3.gets = 0
    both = {"transcript": "inline wins", "timestamps": SEGS, **fields}
    h = tstore.hydrate(s3, "buck", both)
    ok = h["transcript"] == "inline wins" and s3.gets == 0
    check("row with BOTH inline and pointer prefers inline", ok)

    # --- 11. the size fix itself ------------------------------------------
    big_text = "Speaker 1: " + ("मीटिंग की बात " * 4000)
    big_segs = [{"speaker": str(i % 2), "start": Decimal(i),
                 "end": Decimal(i + 1), "text": "मीटिंग की बात " * 20}
                for i in range(400)]
    inline_row = {"audio_s3_key": "k", "transcript": big_text,
                  "timestamps": big_segs, "summary": "s" * 500}
    inline_bytes = len(json.dumps(inline_row, default=str).encode())
    s3b = FakeS3()
    slim = {"audio_s3_key": "k", "summary": "s" * 500}
    slim.update(tstore.put(s3b, "buck", "k", big_text, big_segs))
    slim_bytes = len(json.dumps(slim, default=str).encode())
    ok = inline_bytes > 409_600 and slim_bytes < 20_000
    check("oversized inline row (>400KB) becomes a small offloaded row", ok,
          f"{inline_bytes // 1024}KB inline -> {slim_bytes // 1024}KB offloaded")

    # --- userApi route integration ----------------------------------------
    user_id = "user-" + uuid.uuid4().hex[:8]
    token = userapi._mint_for(user_id, "unit@example.com")
    key = "esp32-001/a.wav"

    # --- 12. get_recording on an OFFLOADED row ----------------------------
    s3r = FakeS3()
    f2 = tstore.put(s3r, userapi.BUCKET_NAME, key, TEXT, SEGS)
    off_row = {"audio_s3_key": key, "user_id": user_id, "status": "complete",
               "title": "Review", "summary": "went well", **f2}
    userapi._recordings = FakeRecordings({key: off_row})
    userapi._s3 = s3r
    body = resp_body(userapi.get_recording(_event(token, key)))
    rec = body["recording"]
    ok = (rec["transcript"] == TEXT
          and len(rec["timestamps"]) == 2
          # the app reads these exact field names / shapes
          and rec["timestamps"][0]["speaker"] == "0"
          and isinstance(rec["timestamps"][0]["start"], (int, float))
          and rec.get("audio_url", "").startswith("https://"))
    check("get_recording hydrates an OFFLOADED row (app JSON unchanged)", ok,
          f"{len(rec['transcript'])} chars, {len(rec['timestamps'])} segs")

    # --- 13. get_recording on a LEGACY row --------------------------------
    lkey = "esp32-001/legacy.wav"
    userapi._recordings = FakeRecordings({lkey: {
        "audio_s3_key": lkey, "user_id": user_id, "status": "complete",
        "transcript": "Speaker 1: legacy inline", "timestamps": SEGS}})
    s3r.gets = 0
    rec = resp_body(userapi.get_recording(_event(token, lkey)))["recording"]
    ok = rec["transcript"] == "Speaker 1: legacy inline" and s3r.gets == 0
    check("get_recording still serves a LEGACY inline row (no S3 GET)", ok,
          f"s3.gets={s3r.gets}")

    # --- 14. patch_recording returns a hydrated row -----------------------
    # The app ADOPTS this response (updateRecording in lib/api.ts) and its
    # Transcript tab renders from `timestamps`.
    patched = dict(off_row)
    patched["speaker_names"] = {"0": "Asha"}
    fake = FakeRecordings({key: patched})
    userapi._recordings = fake
    userapi._s3 = s3r
    ev = _event(token, key)
    ev["body"] = json.dumps({"speaker_names": {"0": "Asha"}})
    rec = resp_body(userapi.patch_recording(ev))["recording"]
    ok = rec.get("transcript") == TEXT and len(rec.get("timestamps") or []) == 2
    check("patch_recording (speaker rename) returns HYDRATED row "
          "-> Transcript tab survives", ok,
          f"transcript={len(rec.get('transcript') or '')} chars")

    # --- 15. _row_fingerprint --------------------------------------------
    s3r.gets = 0
    stamped = userapi._row_fingerprint({"transcript_fingerprint": "abc123"})
    inline_fp = userapi._row_fingerprint({"transcript": TEXT})
    ok = (stamped == "abc123"
          and inline_fp == userapi.ai_schema.fingerprint(TEXT)
          and s3r.gets == 0)
    check("_row_fingerprint prefers the stamped value, falls back inline "
          "(never reads S3)", ok, f"stamped={stamped}")

    # --- 16. ownership checked before hydrating ---------------------------
    other = userapi._mint_for("user-someone-else", "other@example.com")
    userapi._recordings = FakeRecordings({key: off_row})
    s3r.gets = 0
    try:
        userapi._owned_recording(_event(other, key))
        check("ownership checked BEFORE hydrate (no S3 spend for a stranger)",
              False, "expected 404")
    except userapi.ApiError as e:
        check("ownership checked BEFORE hydrate (no S3 spend for a stranger)",
              e.status == 404 and s3r.gets == 0,
              f"status={e.status} s3.gets={s3r.gets}")


def main():
    print("=== transcript_store (S3 offload) — unit tests ===\n")
    unit_tests()
    failed = [r for r in _results if not r[1]]
    print(f"\n{len(_results) - len(failed)}/{len(_results)} passed")
    if failed:
        for name, _ok, detail in failed:
            print(f"  FAILED: {name}  {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
