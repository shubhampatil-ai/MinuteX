#!/usr/bin/env python3
"""26_reprocess_stuck.py — re-run transcription for recordings stranded
mid-pipeline by the DynamoDB 400 KB item-size failure.

WHY THESE ROWS EXIST
--------------------
transcribeRecording stamps small status rows as it goes:

    uploading -> uploaded -> transcribing -> generating_ai
              -> complete | transcribed | failed

Those status writes are tiny, so they succeeded. The FINAL write — the one
carrying transcript + timestamps + the whole analysis — exceeded DynamoDB's hard
400 KB per-item limit and threw ValidationException. Result: the row is frozen at
`transcribing` or `generating_ai`, the app polls a status that will never
advance and shows "writing the summary" forever, and the ElevenLabs + Groq spend
for that recording was thrown away.

The code fix (shared/transcript_store.py) moves transcript + timestamps
to S3 so the item stays ~10 KB. But it only applies to FUTURE runs — the already
stranded rows still need one more pass to actually get their summaries.

WHAT THIS DOES
--------------
Finds rows stuck in a non-terminal status and re-invokes transcribeRecording
with the SAME synthetic S3 event shape the real trigger sends
(Records[].s3.bucket.name / object.key, key URL-encoded exactly as S3 does), so
the replay goes down an identical code path — no special "reprocess" branch that
could drift from production behaviour.

COSTS REAL MONEY: each row re-runs ElevenLabs STT + 3 Groq calls. That is the
point (it is how the summary is recovered), but it is why --yes is required and
why the default is a dry run.

Safe to re-run: reprocessing is idempotent by design — the pipeline UPSERTs by
audio_s3_key and preserves user-owned fields (a user-typed title, created_at,
ownership). A row that has since completed on its own is skipped.

USAGE
    python scripts/26_reprocess_stuck.py                 # dry run (default)
    python scripts/26_reprocess_stuck.py --yes           # actually reprocess
    python scripts/26_reprocess_stuck.py --yes --async    # don't wait for each
    python scripts/26_reprocess_stuck.py --status failed --yes
    python scripts/26_reprocess_stuck.py --key <audio_s3_key> --yes
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
from pathlib import Path

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parents[1]

# The statuses that mean "the pipeline started and never finished". Deliberately
# NOT including "uploading"/"uploaded": those rows may still have an upload in
# flight or a trigger about to fire, and reprocessing them would race the real
# pipeline. "failed" is opt-in via --status because a genuine STT failure (bad
# audio) will just fail again and burn the spend a second time.
STUCK_STATUSES = ("transcribing", "generating_ai")

AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".mp4", ".aac", ".flac", ".ogg", ".opus",
              ".webm", ".mkv", ".amr", ".3gp", ".wma", ".aiff", ".alac",
              ".mpeg", ".mpga", ".oga")


def load_dotenv(path=ROOT / ".env"):
    if not Path(path).exists():
        return
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def s3_event(bucket, key):
    """The exact event shape S3 sends transcribeRecording.

    The key is quoted with quote_plus because that is what S3 does (spaces
    become "+"), and the handler calls unquote_plus on it. Getting this wrong
    would silently reprocess the WRONG key for any recording with a space in
    its name — of which there are several ("WhatsApp Audio 2026-07-27 at ...").
    """
    return {"Records": [{
        "eventSource": "aws:s3",
        "eventName": "ObjectCreated:Put",
        "s3": {"bucket": {"name": bucket},
               "object": {"key": urllib.parse.quote_plus(key)}},
    }]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true",
                    help="actually reprocess (default is a dry run)")
    ap.add_argument("--status", action="append", default=None,
                    help=f"status to target (repeatable; default {STUCK_STATUSES})")
    ap.add_argument("--key", action="append", default=None,
                    help="reprocess a specific audio_s3_key (repeatable)")
    ap.add_argument("--async", dest="async_", action="store_true",
                    help="fire-and-forget instead of waiting for each run")
    ap.add_argument("--table", default=os.environ.get("RECORDINGS_TABLE", "Recordings"))
    ap.add_argument("--lambda-name", default=os.environ.get(
        "TRANSCRIBE_LAMBDA_NAME", "transcribeRecording"))
    args = ap.parse_args()

    load_dotenv()
    region = os.environ.get("AWS_REGION", "ap-south-1")
    bucket = os.environ.get("BUCKET_NAME")
    if not bucket:
        sys.exit("BUCKET_NAME not set (put it in .env)")

    ddb = boto3.resource("dynamodb", region_name=region)
    table = ddb.Table(args.table)
    # A synchronous invoke runs the whole pipeline (ElevenLabs + 3 Groq calls),
    # which can take minutes — the default 60s socket read timeout would give up
    # long before the Lambda does and make a successful run look like a failure.
    lam = boto3.client("lambda", region_name=region,
                       config=Config(read_timeout=900, retries={"max_attempts": 0}))

    targets = []
    if args.key:
        for k in args.key:
            item = table.get_item(Key={"audio_s3_key": k}).get("Item")
            if not item:
                print(f"[skip] not found: {k}")
                continue
            targets.append(item)
    else:
        wanted = tuple(args.status) if args.status else STUCK_STATUSES
        kw = {}
        while True:
            page = table.scan(
                ProjectionExpression="audio_s3_key,#s,created_at,title,"
                                     "transcript_s3_key",
                ExpressionAttributeNames={"#s": "status"}, **kw)
            for it in page["Items"]:
                if (it.get("status") or "") in wanted:
                    targets.append(it)
            if "LastEvaluatedKey" not in page:
                break
            kw["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        targets.sort(key=lambda i: i.get("created_at") or "")

    if not targets:
        print("nothing stuck — no rows to reprocess")
        return 0

    print(f"{len(targets)} recording(s) to reprocess "
          f"(lambda={args.lambda_name}, bucket={bucket}):\n")
    for it in targets:
        key = it["audio_s3_key"]
        skip = "" if key.lower().endswith(AUDIO_EXTS) else "  [UNSUPPORTED EXT — will skip]"
        print(f"  {str(it.get('status')):<14} {it.get('created_at', '')[:19]}  "
              f"{key[-60:]}{skip}")

    if not args.yes:
        print("\nDRY RUN — nothing invoked. Re-run with --yes to reprocess.")
        print("Each row costs one ElevenLabs STT + up to 3 Groq calls.")
        return 0

    print(f"\nreprocessing ({'async' if args.async_ else 'sync'})...\n")
    ok = failed = 0
    for it in targets:
        key = it["audio_s3_key"]
        if not key.lower().endswith(AUDIO_EXTS):
            print(f"[skip] unsupported extension: {key}")
            continue
        started = time.time()
        try:
            resp = lam.invoke(
                FunctionName=args.lambda_name,
                InvocationType="Event" if args.async_ else "RequestResponse",
                Payload=json.dumps(s3_event(bucket, key)).encode(),
            )
            if args.async_:
                print(f"[queued] {key[-55:]}")
                ok += 1
                continue

            payload = resp["Payload"].read().decode("utf-8", "replace")
            if resp.get("FunctionError"):
                print(f"[FAIL] {key[-55:]}\n       {payload[:400]}")
                failed += 1
                continue

            # Report the row's REAL post-run state, not just the invoke result:
            # the pipeline degrades on purpose (a Groq failure still writes
            # status="transcribed"), so "invoke returned 200" alone would
            # overstate what the user actually got.
            row = table.get_item(Key={"audio_s3_key": key}).get("Item") or {}
            status = row.get("status")
            has_tx = bool(row.get("transcript_s3_key") or row.get("transcript"))
            has_sum = bool((row.get("summary") or "").strip())
            print(f"[ok] {key[-55:]}\n     status={status} transcript={has_tx} "
                  f"summary={has_sum} ({time.time() - started:.0f}s)")
            ok += 1
        except Exception as err:  # noqa: BLE001
            print(f"[FAIL] {key[-55:]}\n       {err}")
            failed += 1

    print(f"\n{ok} reprocessed, {failed} failed")
    if args.async_:
        print("async: check CloudWatch logs / re-run without --yes to see status")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
