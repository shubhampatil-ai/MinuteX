#!/usr/bin/env python3
"""Render a REAL recording's transcript exactly as AI Chat will see it.

Read-only: one DynamoDB GetItem plus (if the transcript is offloaded) one S3
GetObject. Writes nothing, calls no LLM, costs nothing but a read.

Run from cloud/:
    python <this> recordings/<user>/mobile/<file>.m4a
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for p in (ROOT / "shared", ROOT / "functions/userapi"):
    sys.path.insert(0, str(p))

import boto3  # noqa: E402
import prompts  # noqa: E402
import transcript_store  # noqa: E402

KEY = sys.argv[1] if len(sys.argv) > 1 else ""
if not KEY:
    sys.exit("usage: check_live_row.py <audio_s3_key>")

BUCKET = os.environ.get("BUCKET_NAME") or sys.exit("set BUCKET_NAME")
TABLE = os.environ.get("RECORDINGS_TABLE", "Recordings")

ddb = boto3.resource("dynamodb")
s3 = boto3.client("s3")

item = ddb.Table(TABLE).get_item(Key={"audio_s3_key": KEY}).get("Item")
if not item:
    sys.exit(f"no such recording: {KEY}")

# The one read path — populates transcript/timestamps whether inline or in S3.
item = transcript_store.hydrate(s3, BUCKET, item)

names = item.get("speaker_names") or {}
transcript = item.get("transcript") or ""
stamps = item.get("timestamps") or []

print(f"key            : {KEY}")
print(f"language       : {item.get('language')}")
print(f"speaker_names  : {names}")
print(f"transcript     : {len(transcript)} chars")
print(f"timestamps     : {len(stamps)} segments")
print()

if not names:
    print("!! No speaker_names on this row — rename a speaker in the app "
          "first, or the fix has nothing to apply.")

# The exact call the chat path makes.
labelled = transcript_store.as_labelled_lines(transcript, stamps, names)

if labelled == transcript:
    print("!! FELL BACK to the plain transcript (no timestamps, or a "
          "line/segment count mismatch). Names were NOT applied — this row "
          "cannot carry evidence ids either, which is pre-existing.")

lines = [ln for ln in labelled.split("\n") if ln.strip()]
print("--- first 12 rendered lines (what the model reads) ---")
for ln in lines[:12]:
    print(ln[:160])
print()

resolved = sum(1 for ln in lines
               if any(f"] {n}:" in ln for n in names.values()))
stale = sum(1 for ln in lines if "] Speaker " in ln)
print(f"lines showing a USER NAME : {resolved}")
print(f"lines still 'Speaker N'   : {stale}  "
      "(expected only for speakers nobody renamed)")
