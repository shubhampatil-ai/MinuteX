#!/usr/bin/env python3
# =============================================================
# 45_backfill_pageindex.py — build the retrieval index for meetings recorded
# before PageIndex existed.
#
# WHAT THIS IS FOR
#   Meeting AI retrieves against a per-meeting PageIndex tree (see
#   shared/pageindex.py). The transcribe Lambda now builds that tree as each
#   meeting finishes, and userApi builds one lazily the first time an
#   un-indexed meeting is asked a question — so the back catalogue already
#   WORKS without this script.
#
#   What it changes is WHEN the cost is paid. Lazy generation makes the first
#   person to ask an old long meeting a question wait for the build; running
#   this beforehand means they don't. That is the whole benefit, which is why
#   this is optional and safe to defer.
#
#   NOTHING HERE IS REQUIRED FOR CORRECTNESS. If it is never run, every
#   meeting still answers.
#
# SAFETY PROPERTIES
#   * IDEMPOTENT. A meeting whose stored index already matches its current
#     transcript fingerprint is skipped. Running this twice does the work
#     once; running it after the lazy path has already indexed a meeting does
#     nothing for that meeting.
#   * RESUMABLE. State lives entirely on the rows (the `pageindex` attribute),
#     not in this process. Kill it at any point and re-run: it picks up from
#     what is already indexed, with no checkpoint file to keep or corrupt.
#   * NON-DESTRUCTIVE. It writes exactly two things: a new S3 object under
#     pageindex/, and the `pageindex` attribute on the row. It never touches
#     the transcript, the analysis, the documents, the tasks, the MoM or any
#     other attribute, and it never deletes anything.
#   * SAFE TO RETRY. A failure is recorded on the row as status="failed" and
#     leaves the meeting exactly as it was — answerable through the fallback
#     path, and re-attemptable by running this again.
#   * SINGLE-FLIGHT AWARE. It claims each meeting through the same conditional
#     write userApi uses, so running this WHILE the app is serving traffic
#     cannot produce two concurrent builds of the same meeting.
#
# COST
#   The build is deterministic and does no LLM calls unless
#   PAGEINDEX_LLM_SUMMARY=1 is set — see shared/pageindex.py for why that is
#   off by default. With it off this script costs one S3 GET (the transcript),
#   one S3 PUT (the tree) and one UpdateItem per meeting, and no Groq quota.
#   With it ON it makes roughly one Groq call PER NODE per meeting, which on a
#   free-tier 12k TPM quota is slow and rate-limited — do not enable it for a
#   bulk run without checking the plan first.
#
# USAGE
#   python scripts/45_backfill_pageindex.py --dry-run       # report only
#   python scripts/45_backfill_pageindex.py --user <uid>    # one account
#   python scripts/45_backfill_pageindex.py --key <s3key>   # one meeting
#   python scripts/45_backfill_pageindex.py --limit 50      # first N needing it
#   python scripts/45_backfill_pageindex.py                 # everything
#
#   START WITH --dry-run. It reads and reports without writing anything, so it
#   is the safe way to see how many meetings a full run would touch before
#   committing to one.
#
# Credentials come from the environment / ~/.aws like every other script here.
# =============================================================
import argparse
import os
import sys
import time
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Key

ROOT = Path(__file__).resolve().parents[1]
# The shared modules are imported directly rather than reimplemented: the tree
# this writes MUST be byte-compatible with what the Lambdas build and read, and
# a second implementation here would be free to drift.
sys.path.insert(0, str(ROOT / "shared"))


def load_dotenv(path: Path) -> None:
    """Same minimal loader the other scripts use — no python-dotenv dep."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# .env lives at the REPO root (shared with the app/ and device/ trees), not in
# cloud/ — the same location aws.sh loads it from. ROOT here is cloud/, so the
# parent is what holds it; loading only cloud/.env would silently find nothing
# and fail on BUCKET_NAME with no hint as to why.
load_dotenv(ROOT.parent / ".env")
load_dotenv(ROOT / ".env")

import ai_schema        # noqa: E402
import pageindex        # noqa: E402
import pageindex_store  # noqa: E402
import transcript_store  # noqa: E402

REGION = os.environ.get("AWS_REGION", "ap-south-1")
RECORDINGS_TABLE = os.environ.get("RECORDINGS_TABLE", "Recordings")
BUCKET_NAME = os.environ.get("BUCKET_NAME", "")
USER_INDEX = os.environ.get("RECORDINGS_USER_INDEX", "user-index")

_ddb = boto3.resource("dynamodb", region_name=REGION)
_table = _ddb.Table(RECORDINGS_TABLE)
_s3 = boto3.client("s3", region_name=REGION)


def iter_rows(user_id=None, key=None):
    """The recording rows to consider, as a stream.

    Paginated rather than collected: an account with thousands of meetings
    should not need all of them resident before the first one is indexed, and
    streaming is what makes the run interruptible at any point.
    """
    if key:
        item = _table.get_item(Key={"audio_s3_key": key}).get("Item")
        if item:
            yield item
        return

    kwargs = {}
    if user_id:
        kwargs = {"IndexName": USER_INDEX,
                  "KeyConditionExpression": Key("user_id").eq(user_id)}
        paginate = _table.query
    else:
        paginate = _table.scan

    while True:
        page = paginate(**kwargs)
        for item in page.get("Items", []):
            yield item
        token = page.get("LastEvaluatedKey")
        if not token:
            return
        kwargs["ExclusiveStartKey"] = token


def needs_index(item):
    """(bool, reason) — whether this meeting should be indexed now.

    The fingerprint check is the SAME one userApi uses (pageindex_store.
    is_fresh), so this script and the live path can never disagree about
    whether an index is current.
    """
    status = (item.get("status") or "").strip()
    if status in ("uploading", "uploaded", "transcribing", "generating_ai"):
        return False, "still processing"
    if not (item.get("transcript_s3_key") or item.get("transcript")):
        return False, "no transcript"

    fingerprint = (item.get("transcript_fingerprint")
                   or ai_schema.fingerprint(item.get("transcript") or ""))
    if not fingerprint:
        return False, "no fingerprint"
    if pageindex_store.is_fresh(item, fingerprint):
        return False, "already indexed"
    return True, "needs index"


def index_one(item, dry_run=False):
    """Build and store one meeting's index. Returns a short outcome string."""
    key = item["audio_s3_key"]
    fingerprint = (item.get("transcript_fingerprint")
                   or ai_schema.fingerprint(item.get("transcript") or ""))

    # hydrate() is the ONE transcript read path — used here rather than a hand
    # rolled S3 GET so legacy inline rows and offloaded rows are handled by the
    # same code the Lambdas use, and every segment arrives already carrying its
    # derived seg_N.
    hydrated = transcript_store.hydrate(_s3, BUCKET_NAME, item)
    timestamps = hydrated.get("timestamps") or []
    transcript = hydrated.get("transcript") or ""
    if not timestamps:
        return "skipped (no segments)"

    if dry_run:
        tree = pageindex.build_tree(timestamps, transcript)
        return (f"would index: {len(tree['nodes'])} nodes from "
                f"{len(timestamps)} segments")

    owner = item.get("user_id") or ""
    if not pageindex_store.claim(_table, key, fingerprint, owner):
        # Either the app is building it right now, or another copy of this
        # script is. Both are fine outcomes — skip rather than race.
        return "skipped (claimed elsewhere)"

    try:
        tree = pageindex_store.build_for(hydrated, timestamps, transcript)
    except Exception as err:  # noqa: BLE001
        pageindex_store.mark_failed(_table, key, fingerprint, str(err))
        return f"FAILED: {err}"

    if not tree.get("nodes"):
        pageindex_store.save(_s3, BUCKET_NAME, _table, key, tree,
                             fingerprint, owner)
        return "indexed (empty — transcript too short to split)"

    if pageindex_store.save(_s3, BUCKET_NAME, _table, key, tree,
                            fingerprint, owner) is None:
        return "FAILED: could not store index"
    return f"indexed: {len(tree['nodes'])} nodes from {len(timestamps)} segments"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would happen; write nothing")
    ap.add_argument("--user", help="only this owner_user_id")
    ap.add_argument("--key", help="only this audio_s3_key")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after indexing this many meetings")
    args = ap.parse_args()

    if not BUCKET_NAME:
        sys.exit("BUCKET_NAME is not set — the index has nowhere to go.")

    if pageindex.llm_summaries_enabled() and not args.dry_run:
        # Loud, because on a free-tier quota this turns a seconds-long run into
        # an hours-long one and spends Groq budget per node per meeting.
        print("!! PAGEINDEX_LLM_SUMMARY is ON — this run will make Groq calls "
              "per node, per meeting. Ctrl-C now if that was not intended.")
        time.sleep(5)

    print(f">> table={RECORDINGS_TABLE} bucket={BUCKET_NAME} region={REGION}"
          f"{' (DRY RUN)' if args.dry_run else ''}")

    counts = {"indexed": 0, "skipped": 0, "failed": 0, "considered": 0}
    started = time.monotonic()

    for item in iter_rows(user_id=args.user, key=args.key):
        counts["considered"] += 1
        key = item.get("audio_s3_key", "?")
        wanted, reason = needs_index(item)
        if not wanted:
            counts["skipped"] += 1
            continue

        outcome = index_one(item, dry_run=args.dry_run)
        if outcome.startswith("FAILED"):
            counts["failed"] += 1
            print(f"   !! {key}: {outcome}")
        elif outcome.startswith("skipped"):
            counts["skipped"] += 1
        else:
            counts["indexed"] += 1
            print(f"   + {key}: {outcome}")

        if args.limit and counts["indexed"] >= args.limit:
            print(f">> stopping at --limit {args.limit}")
            break

    elapsed = time.monotonic() - started
    print(f">> considered={counts['considered']} "
          f"indexed={counts['indexed']} skipped={counts['skipped']} "
          f"failed={counts['failed']} in {elapsed:.1f}s")
    if counts["failed"]:
        print(">> re-run to retry the failures; each is recorded on its row "
              "with status=failed and blocks nothing.")
    # Non-zero on failure so a CI/cron caller notices, matching 32_backfill.
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
