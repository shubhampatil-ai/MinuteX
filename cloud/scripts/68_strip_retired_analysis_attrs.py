#!/usr/bin/env python3
"""68_strip_retired_analysis_attrs.py — drop the retired analysis attributes.

DESTRUCTIVE AND IRREVERSIBLE (short of a PITR restore). Removes the six
TOP-LEVEL analysis attributes that left the schema from every Recordings row
still carrying them.

WHY THIS EXISTS. The transcribe Lambda already REMOVEs these on every write
(see RETIRED_ANALYSIS_ATTRS in functions/transcribe/lambda_function.py) — but
only for a row it actually reprocesses. A recording analysed under the old
shape and never touched again keeps its stale copy forever, because a SET-only
update has no way to say "the item is exactly this shape now". This script is
that cleanup, applied once to the back catalogue.

WHAT IT REMOVES — top-level attributes only:

    agenda, key_points, decisions, pending_discussions, action_items, highlights

Nothing reads any of them: prompts.py no longer asks for them and
ai_schema.coerce_analysis builds its result key-by-key from title/overview/
tasks/participants, so a model that still volunteers one has it dropped before
the write.

WHAT IT MUST NOT TOUCH — and the whole reason this script is narrow:

  * meeting_highlights.action_items  — a NESTED attribute that merely SHARES A
    NAME with the retired top-level one. It is still READ: mom_schema's
    _build_actions falls back to it whenever `tasks` is empty, which is every
    recording older than task seeding. Stripping it would silently blank the
    Action Items section of the MoM across that back catalogue, with no error
    and no way to regenerate it (the extraction prompt no longer produces it).
    The REMOVE clause below names bare top-level attributes, so the nested map
    is never in scope — but the distinction is the one to keep in mind if
    anyone extends this list.

  * meeting_highlights.decisions — same story: live, and only coincidentally
    named like the retired top-level `decisions`.

  * crm_records — deliberately absent from RETIRED_ANALYSIS_ATTRS upstream and
    absent here, because a stored value may be a user's MANUAL, confirmed link
    to a real Salesforce record.

SAFETY

  * --dry-run (DEFAULT) reports exactly what would change and writes nothing.
    Pass --apply to actually strip.
  * Every page is enumerated with LastEvaluatedKey. A Scan caps at 1 MB, so a
    single-page scan would silently miss rows and report success.
  * Re-runnable: REMOVEing an absent attribute is a no-op, so an interrupted
    run can simply be repeated.
  * ConditionExpression="attribute_exists(audio_s3_key)" — the row must already
    be there. An UpdateItem on a vanished key would otherwise CREATE a stub
    recording out of a stale scan result.
  * Projects only the key + the six names, so the scan does not drag every
    transcript pointer and overview map across the wire to count them.

PITR is ENABLED on Recordings (scripts/51_enable_pitr_protection.sh), so a bad
run is recoverable by point-in-time restore to just before it started.

BACK UP FIRST if you want a local copy — this does not snapshot anything:

    aws dynamodb scan --table-name Recordings --filter-expression \\
        "attribute_exists(agenda) OR attribute_exists(key_points) OR \\
         attribute_exists(decisions) OR attribute_exists(pending_discussions) \\
         OR attribute_exists(action_items) OR attribute_exists(highlights)" \\
        --output json > Recordings_retired_attrs.json

Run from cloud/:
    python scripts/68_strip_retired_analysis_attrs.py            # dry run
    python scripts/68_strip_retired_analysis_attrs.py --apply
"""
import argparse
import os
import sys

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION", "ap-south-1")
RECORDINGS_TABLE = os.environ.get("RECORDINGS_TABLE", "Recordings")

# The six TOP-LEVEL attributes that left the analysis schema. Kept in sync with
# RETIRED_ANALYSIS_ATTRS in functions/transcribe/lambda_function.py — that
# tuple is the source of truth for what the pipeline clears; this is the same
# list applied to rows the pipeline will never revisit.
RETIRED_ATTRS = (
    "agenda",
    "key_points",
    "decisions",
    "pending_discussions",
    "action_items",
    "highlights",
)

# Placeholders throughout: every one of these is a reserved word or risks
# becoming one, and `decisions`/`action_items` in particular read like schema
# terms. ExpressionAttributeNames sidesteps the question entirely.
_NAMES = {f"#a{i}": a for i, a in enumerate(RETIRED_ATTRS)}
_FILTER = " OR ".join(f"attribute_exists({p})" for p in _NAMES)
_REMOVE = "REMOVE " + ", ".join(_NAMES)


def scan_all(table, **kwargs):
    """Every matching row, following LastEvaluatedKey.

    A Scan response caps at 1 MB. Without this loop a large table silently
    yields a partial answer and the script reports a clean finish having
    missed rows.
    """
    out, start = [], None
    while True:
        if start:
            kwargs["ExclusiveStartKey"] = start
        resp = table.scan(**kwargs)
        out.extend(resp.get("Items", []))
        start = resp.get("LastEvaluatedKey")
        if not start:
            return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually strip (default is a dry run)")
    args = ap.parse_args()
    apply = args.apply

    ddb = boto3.resource("dynamodb", region_name=REGION)
    recordings = ddb.Table(RECORDINGS_TABLE)

    mode = "APPLY (destructive)" if apply else "DRY RUN (no writes)"
    print(f">> Region: {REGION}")
    print(f">> Table:  {RECORDINGS_TABLE}")
    print(f">> Mode:   {mode}")
    print(f">> Attrs:  {', '.join(RETIRED_ATTRS)}")
    print()

    rows = scan_all(
        recordings,
        FilterExpression=_FILTER,
        # Key + the six, nothing else: counting them must not pull every
        # overview map and transcript pointer across the wire.
        ProjectionExpression=", ".join(["#k"] + list(_NAMES)),
        ExpressionAttributeNames={**_NAMES, "#k": "audio_s3_key"},
    )

    if not rows:
        print(">> No row carries any retired attribute. Nothing to do.")
        return 0

    # Per-attribute counts, so the report says WHICH of the six are actually
    # out there rather than just how many rows are dirty.
    per_attr = {a: 0 for a in RETIRED_ATTRS}
    for row in rows:
        for a in RETIRED_ATTRS:
            if a in row:
                per_attr[a] += 1

    print(f">> {len(rows)} row(s) carry at least one retired attribute:")
    for a in RETIRED_ATTRS:
        if per_attr[a]:
            print(f"     {a:<22} {per_attr[a]}")
    print()

    if not apply:
        print(">> DRY RUN — nothing was written.")
        print(">> Re-run with --apply to strip these attributes.")
        return 0

    stripped, failed = 0, 0
    for row in rows:
        key = row.get("audio_s3_key")
        if not key:
            continue
        try:
            recordings.update_item(
                Key={"audio_s3_key": key},
                UpdateExpression=_REMOVE,
                ExpressionAttributeNames=_NAMES,
                # The row must still exist. Without this an UpdateItem on a key
                # deleted since the scan would CREATE a stub recording.
                ConditionExpression="attribute_exists(audio_s3_key)",
            )
            stripped += 1
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                # Deleted between the scan and now — the attributes went with
                # it, so the goal is met either way.
                continue
            print(f"   !! {key}: {code or err}", file=sys.stderr)
            failed += 1

    print(f">> Stripped {stripped} row(s).")
    if failed:
        print(f">> {failed} row(s) FAILED — re-run to retry (it is a no-op on "
              f"rows already done).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
