#!/usr/bin/env python3
# =============================================================
# 57_backfill_speaker_summaries.py — regenerate the per-speaker contribution
# blurbs for meetings that stored them blank.
#
# WHAT THIS IS FOR
#   `participants[].summary` is the one-line "what this person contributed"
#   shown on the Speakers tab and in the MoM Attendees table. It is written by
#   the single analysis call and then matched against the structural speaker
#   roster by ai_schema._roster_filtered.
#
#   That match used to be an EXACT string lookup. The roster handed to the
#   prompt is authoritative about who spoke but only guidance about how to
#   spell them back, so a model answering "0", "speaker_0" or the speaker's
#   human name for a roster of "Speaker 0" missed on every entry — and each
#   speaker silently took the "" default. The row then reads: every speaker
#   listed, every summary blank, while the title, overview and tasks are all
#   fine. Seen in production on code-switched (Hindi/Marathi) meetings, where
#   label drift is most common.
#
#   _roster_filtered now matches on the NORMALIZED label and resolves human
#   names through the row's `speaker_names`, so NEW meetings are unaffected.
#   This script is only for the back catalogue already written with blanks.
#
# WHY IT RE-RUNS THE MODEL
#   The blurbs were never stored — the mismatch discarded them before the
#   write. There is nothing on the row to repair from, so the only way to
#   recover them is to ask the model again over the same transcript. That is
#   also why this is opt-in and rate-limited rather than automatic.
#
#   The re-analysis is done with the SAME prompt and coercion the Lambda uses
#   (imported, not reimplemented), so a backfilled row is indistinguishable
#   from a freshly transcribed one.
#
# WHAT IT WRITES — AND WHAT IT DELIBERATELY DOES NOT
#   It writes ONE attribute: `participants`. Nothing else.
#
#   The re-analysis returns a whole analysis object (title, overview, tasks,
#   meeting_highlights), and every one of those is DISCARDED here. Overwriting
#   them would throw away work the user may have edited since — a renamed
#   meeting, a corrected task, a regenerated MoM — to fix a field that is
#   independent of all of them. The speaker labels themselves are also left
#   alone: they come from the roster, which was already correct.
#
#   `speaker_names` (the user's renames) is READ, never written. It is what
#   lets a model answering "Yuvraj Sir" land on Speaker 1.
#
# SAFETY PROPERTIES
#   * IDEMPOTENT. A row whose summaries are already populated is skipped, so
#     running it twice does the work once.
#   * RESUMABLE. State lives on the rows, not in this process. Kill it and
#     re-run; it picks up from what is still blank, with no checkpoint file.
#   * NON-DESTRUCTIVE. One UpdateItem setting `participants`. It never touches
#     the transcript, overview, tasks, documents, MoM or speaker_names, and it
#     never deletes anything.
#   * CONDITIONAL. The write asserts the roster has not changed underneath it
#     (a concurrent re-transcription), so a racing reprocess cannot be
#     clobbered — such a row is reported as `skipped (roster changed)`.
#   * A FAILURE CHANGES NOTHING. A meeting that errors is left exactly as it
#     was — blank summaries, everything else intact — and is retried by
#     running this again.
#
# COST
#   ONE Groq call per meeting, over the full transcript. That is real quota:
#   budget it like a re-transcription of the back catalogue, not like a
#   metadata migration. --limit and --sleep exist to pace it; START WITH
#   --dry-run, which makes NO model calls and only reports what would run.
#
# USAGE
#   python scripts/57_backfill_speaker_summaries.py --dry-run     # report only
#   python scripts/57_backfill_speaker_summaries.py --key <s3key> # one meeting
#   python scripts/57_backfill_speaker_summaries.py --user <uid>  # one account
#   python scripts/57_backfill_speaker_summaries.py --limit 20    # first N
#   python scripts/57_backfill_speaker_summaries.py --sleep 2     # pace it
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
from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
# The shared modules are imported directly rather than reimplemented: a
# backfilled row MUST be indistinguishable from a freshly transcribed one, and
# a second copy of the prompt or the coercion here would be free to drift.
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
# cloud/ — the same location aws.sh loads it from.
load_dotenv(ROOT.parent / ".env")
load_dotenv(ROOT / ".env")

import ai_schema        # noqa: E402
import groq_client      # noqa: E402
import prompts          # noqa: E402
import transcript_store  # noqa: E402

REGION = os.environ.get("AWS_REGION", "ap-south-1")
RECORDINGS_TABLE = os.environ.get("RECORDINGS_TABLE", "Recordings")
BUCKET_NAME = os.environ.get("BUCKET_NAME", "")
USER_INDEX = os.environ.get("RECORDINGS_USER_INDEX", "user-index")

# Matches the Lambda's own analysis deadline. A backfill is not more patient
# than production: a transcript that cannot be analyzed in that budget is a
# failure to retry, not something to block the run on.
DEADLINE_SECONDS = int(os.environ.get("GROQ_DEADLINE_SECONDS", "540"))

_ddb = boto3.resource("dynamodb", region_name=REGION)
_table = _ddb.Table(RECORDINGS_TABLE)
_s3 = boto3.client("s3", region_name=REGION)


def iter_rows(user_id=None, key=None):
    """The recording rows to consider, as a stream.

    Paginated rather than collected: an account with thousands of meetings
    should not need all of them resident before the first one is fixed, and
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


def needs_summaries(item):
    """(bool, reason) — whether this meeting should be re-analyzed now.

    The bar is deliberately narrow: at least one speaker, and EVERY speaker
    blank. A partially-filled list is what a model legitimately returns when it
    had nothing to say about a quiet speaker, and spending a Groq call to
    second-guess that would be inventing contributions rather than recovering
    lost ones.
    """
    status = (item.get("status") or "").strip()
    if status in ("uploading", "uploaded", "transcribing", "generating_ai"):
        return False, "still processing"
    if not (item.get("transcript_s3_key") or item.get("transcript")):
        return False, "no transcript"

    participants = item.get("participants")
    if not isinstance(participants, list) or not participants:
        return False, "no speakers"
    if any(str((p or {}).get("summary") or "").strip()
           for p in participants if isinstance(p, dict)):
        return False, "already summarized"
    return True, f"{len(participants)} speaker(s), all blank"


def _roster_of(item):
    """The stored speakers, in stored order — the roster the row was built
    with. Read from `participants` rather than re-derived from the transcript
    so the conditional write below compares like with like."""
    return [str((p or {}).get("speaker") or "").strip()
            for p in (item.get("participants") or [])
            if isinstance(p, dict) and str((p or {}).get("speaker") or "").strip()]


def fix_one(item, dry_run=False):
    """Re-analyze one meeting and store only its participant summaries.
    Returns a short outcome string."""
    key = item["audio_s3_key"]
    roster = _roster_of(item)
    if not roster:
        return "skipped (no roster)"

    if dry_run:
        return f"would re-analyze {len(roster)} speaker(s)"

    # hydrate() is the ONE transcript read path — used here rather than a hand
    # rolled S3 GET so legacy inline rows and offloaded rows are handled by the
    # same code the Lambdas use.
    hydrated = transcript_store.hydrate(_s3, BUCKET_NAME, item)
    transcript = hydrated.get("transcript") or ""
    timestamps = hydrated.get("timestamps") or []
    if not transcript.strip():
        return "skipped (empty transcript)"

    # The model sees exactly what the Lambda shows it: segment-id-prefixed
    # lines, with the user's renames applied. Those renames are the reason the
    # model may answer with a human NAME, which is precisely what the fixed
    # matcher now resolves.
    names = hydrated.get("speaker_names") or {}
    valid_ids = transcript_store.valid_segment_ids(timestamps)
    labelled = transcript_store.as_labelled_lines(transcript, timestamps, names)

    try:
        analysis, _covered, _total = groq_client.analyze(
            labelled,
            map_prompt=prompts.unified_analysis_system(roster),
            reduce_prompt=prompts.unified_reduce_system(),
            merge=lambda partials: ai_schema.merge_unified(
                partials, roster, names),
            coerce=lambda obj: ai_schema.coerce_unified(
                obj, roster, valid_ids, names),
            deadline_seconds=DEADLINE_SECONDS,
            label="backfill-speaker-summaries",
        )
    except Exception as err:  # noqa: BLE001 — reported per row, run continues
        return f"FAILED ({type(err).__name__}: {str(err)[:160]})"

    participants = analysis.get("participants") or []
    filled = sum(1 for p in participants
                 if str((p or {}).get("summary") or "").strip())
    if not filled:
        # The model genuinely had nothing to say, or drifted in a way the
        # matcher still cannot resolve. Writing an all-blank list back would
        # burn the quota for no change — leave the row exactly as it was.
        return "skipped (model returned no summaries)"

    # ONLY `participants`. The re-analysis also produced a title, overview,
    # tasks and highlights; all are discarded rather than overwrite work the
    # user may have edited since.
    #
    # CONDITIONAL on the roster being unchanged: if a re-transcription landed
    # while this call was in flight, the row now describes different speakers
    # and this result is stale. Comparing the whole list also catches a
    # concurrent run of this same script.
    try:
        _table.update_item(
            Key={"audio_s3_key": key},
            UpdateExpression="SET participants = :p, updated_at = :u",
            ConditionExpression="participants = :old",
            ExpressionAttributeValues={
                ":p": participants,
                ":old": item.get("participants"),
                ":u": time.strftime("%Y-%m-%dT%H:%M:%S.000000Z",
                                    time.gmtime()),
            },
        )
    except ClientError as err:
        if err.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return "skipped (roster changed underneath)"
        raise
    return f"{filled}/{len(participants)} speaker(s) summarized"


def main():
    ap = argparse.ArgumentParser(
        description="Regenerate blank per-speaker contribution summaries.")
    ap.add_argument("--dry-run", action="store_true",
                    help="report only; makes no model calls and writes nothing")
    ap.add_argument("--user", help="restrict to one user_id")
    ap.add_argument("--key", help="restrict to one audio_s3_key")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after N meetings fixed")
    ap.add_argument("--sleep", type=float, default=0.0,
                    help="seconds to wait between model calls (rate limiting)")
    args = ap.parse_args()

    if not BUCKET_NAME:
        print("!! BUCKET_NAME is not set — cannot read offloaded transcripts.")
        return 2
    if not args.dry_run and not os.environ.get("GROQ_API_KEY"):
        print("!! GROQ_API_KEY is not set — this script re-runs the model. "
              "Use --dry-run to report without calling it.")
        return 2

    print(f">> table={RECORDINGS_TABLE} region={REGION} "
          f"{'DRY RUN' if args.dry_run else 'LIVE'}")

    counts = {"considered": 0, "fixed": 0, "skipped": 0, "failed": 0}
    started = time.monotonic()

    for item in iter_rows(user_id=args.user, key=args.key):
        counts["considered"] += 1
        key = item.get("audio_s3_key", "?")
        ok, reason = needs_summaries(item)
        if not ok:
            counts["skipped"] += 1
            continue

        print(f"   . {key}: {reason}")
        outcome = fix_one(item, dry_run=args.dry_run)
        if outcome.startswith("FAILED"):
            counts["failed"] += 1
            print(f"   !! {key}: {outcome}")
        elif outcome.startswith("skipped"):
            counts["skipped"] += 1
            print(f"   - {key}: {outcome}")
        else:
            counts["fixed"] += 1
            print(f"   + {key}: {outcome}")

        if args.limit and counts["fixed"] >= args.limit:
            print(f">> stopping at --limit {args.limit}")
            break
        if args.sleep and not args.dry_run:
            time.sleep(args.sleep)

    elapsed = time.monotonic() - started
    print(f">> considered={counts['considered']} fixed={counts['fixed']} "
          f"skipped={counts['skipped']} failed={counts['failed']} "
          f"in {elapsed:.1f}s")
    if counts["failed"]:
        print(">> re-run to retry the failures; each left its row unchanged.")
    # Non-zero on failure so a CI/cron caller notices, matching 45_backfill.
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
