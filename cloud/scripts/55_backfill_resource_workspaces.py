#!/usr/bin/env python3
# =============================================================
# 55_backfill_resource_workspaces.py — stamp workspace_id / created_by onto
# existing Recordings, Tasks and Contacts.
#
# WHAT THIS IS FOR
#   Phase 2B writes workspace_id on every NEW resource. This stamps the rows
#   that already existed. Every one of them becomes PERSONAL to its current
#   owner — which is what it has always been.
#
# THIS SCRIPT CANNOT CREATE ORGANISATION DATA.
#   The workspace it writes is always workspace_schema.personal_workspace_id
#   (owner), a pure function of the owner's user id. There is no input, flag
#   or row shape that makes it emit an organisation id. That is the migration
#   guarantee the product requires: existing personal data can never silently
#   become organisation data.
#
# IT IS AN OPTIMIZATION, NOT A CORRECTNESS GATE.
#   Reads already resolve an unstamped row to its owner's personal workspace
#   (workspace_schema.resolve_workspace_id), and the new workspace-index is
#   only ever queried for an ORGANISATION — which no pre-existing row can
#   belong to. So an interrupted, partial or skipped run cannot break a user
#   or hide their data. What the stamp buys is a materialized index entry and
#   a smaller tail for any later shared-contacts migration.
#
# SAFETY PROPERTIES
#   * IDEMPOTENT. attribute_not_exists(workspace_id) on every write, so a row
#     that already has one — including one written by the live API — is never
#     touched, and a second run changes nothing.
#   * ADDITIVE. Only ever ADDS two attributes. No key, no index key, no
#     existing attribute and no S3 object is modified. owner_user_id and
#     user_id are left exactly as they are.
#   * RESUMABLE. Rows are independent; a crash leaves the done ones done.
#   * PAGINATED. Every scan follows LastEvaluatedKey — a scan that ignores it
#     silently processes the first 1 MB and reports success.
#   * REPORTS EVERYTHING, and exits non-zero if anything failed.
#
# ROLLBACK
#   Remove the two attributes from the rows this stamped. They are
#   identifiable by `workspace_backfill = "55"`:
#     aws dynamodb scan --table-name Recordings \
#       --filter-expression 'workspace_backfill = :m' \
#       --expression-attribute-values '{":m":{"S":"55"}}'
#   Removing them returns the system to resolve-on-read, which is fully
#   functional. Nothing else needs undoing.
#
# USAGE
#   python scripts/55_backfill_resource_workspaces.py --dry-run
#   python scripts/55_backfill_resource_workspaces.py
#   python scripts/55_backfill_resource_workspaces.py --table Recordings
#   python scripts/55_backfill_resource_workspaces.py --verify
#
# Prerequisites: 54 (the workspace-index GSIs must exist and be ACTIVE).
# =============================================================
import argparse
import os
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared"))
import workspace_schema  # noqa: E402

REGION = os.environ.get("AWS_REGION", "ap-south-1")
RECORDINGS_TABLE = os.environ.get("RECORDINGS_TABLE", "Recordings")
TASKS_TABLE = os.environ.get("TASKS_TABLE", "Tasks")
CONTACTS_TABLE = os.environ.get("CONTACTS_TABLE", "Contacts")

BACKFILL_TAG = "55"

# (table, primary key attr, the attribute naming the owner). The owner field
# differs per table — Recordings uses user_id, the other two owner_user_id —
# which is exactly why this is a table and not a hardcoded assumption.
SPECS = {
    RECORDINGS_TABLE: ("audio_s3_key", "user_id"),
    TASKS_TABLE: ("task_id", "owner_user_id"),
    CONTACTS_TABLE: ("contact_id", "owner_user_id"),
}


def is_claim_row(item, pk_attr):
    """Uniqueness-claim rows are not resources.

    The Folders table carries "name#..." claims and the Users table carries
    "email#..." ones. Neither is a real row and neither may be stamped. Tasks,
    Recordings and Contacts have none today, but the guard is cheap and this
    script would otherwise be one schema change away from corrupting one.
    """
    value = str(item.get(pk_attr, ""))
    return value.startswith("name#") or value.startswith("email#") \
        or value.startswith("idem#")


def iter_rows(table, pk_attr):
    """Every row, paginated."""
    kwargs = {}
    while True:
        res = table.scan(**kwargs)
        for item in res.get("Items", []):
            if item.get(pk_attr) and not is_claim_row(item, pk_attr):
                yield item
        last = res.get("LastEvaluatedKey")
        if not last:
            return
        kwargs["ExclusiveStartKey"] = last


def stamp_row(table, item, pk_attr, owner_attr, dry_run):
    """(stamped, skipped_reason). Never overwrites an existing workspace_id."""
    if item.get("workspace_id"):
        return False, "already stamped"

    owner = str(item.get(owner_attr) or "").strip()
    if not owner:
        # A legacy device recording whose device was never paired has no
        # owner at all. There is genuinely nobody to attribute it to, and
        # inventing one would be worse than leaving it alone.
        return False, "no owner"

    workspace_id = workspace_schema.personal_workspace_id(owner)
    if dry_run:
        return True, ""

    try:
        table.update_item(
            Key={pk_attr: item[pk_attr]},
            UpdateExpression=("SET workspace_id = :w, "
                              "created_by = if_not_exists(created_by, :c), "
                              "workspace_backfill = :tag"),
            # Only stamp a row that STILL has no workspace — the live API may
            # have written one between the scan and this update.
            ConditionExpression="attribute_not_exists(workspace_id)",
            ExpressionAttributeValues={":w": workspace_id, ":c": owner,
                                       ":tag": BACKFILL_TAG})
        return True, ""
    except ClientError as err:
        if err.response.get("Error", {}).get("Code") \
                == "ConditionalCheckFailedException":
            return False, "raced (already stamped)"
        raise


def verify(table, pk_attr, owner_attr):
    """Prove the invariants. Returns the number of PROBLEMS."""
    total = stamped = unstamped = no_owner = wrong = 0
    for item in iter_rows(table, pk_attr):
        total += 1
        wid = item.get("workspace_id")
        owner = str(item.get(owner_attr) or "").strip()
        if not wid:
            if owner:
                unstamped += 1
            else:
                no_owner += 1
            continue
        stamped += 1
        # THE guarantee: nothing this migration touched may be organisational,
        # and every stamp must name its own owner.
        if workspace_schema.is_organisation_workspace_id(wid):
            if item.get("workspace_backfill") == BACKFILL_TAG:
                wrong += 1
        elif owner and wid != workspace_schema.personal_workspace_id(owner):
            wrong += 1

    print(f"   rows                 : {total}")
    print(f"   stamped              : {stamped}")
    print(f"   unstamped (resolves) : {unstamped}")
    print(f"   ownerless (skipped)  : {no_owner}")
    print(f"   MISATTRIBUTED        : {wrong}")
    return wrong


def main():
    ap = argparse.ArgumentParser(
        description="Stamp workspace_id on existing resources.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--table", default=None,
                    help=f"one of: {', '.join(SPECS)}")
    args = ap.parse_args()

    tables = [args.table] if args.table else list(SPECS)
    for name in tables:
        if name not in SPECS:
            print(f"unknown table: {name}", file=sys.stderr)
            return 2

    ddb = boto3.resource("dynamodb", region_name=REGION)
    print(f">> region={REGION} tables={', '.join(tables)}")

    problems = failed = 0
    for name in tables:
        pk_attr, owner_attr = SPECS[name]
        table = ddb.Table(name)
        print()
        print(f"== {name} (pk={pk_attr}, owner={owner_attr})")

        if args.verify:
            problems += verify(table, pk_attr, owner_attr)
            continue

        seen = stamped = 0
        reasons = {}
        for item in iter_rows(table, pk_attr):
            seen += 1
            try:
                did, why = stamp_row(table, item, pk_attr, owner_attr,
                                     args.dry_run)
            except Exception as err:  # noqa: BLE001 — report, never abort
                failed += 1
                print(f"   FAILED {item.get(pk_attr)}: "
                      f"{type(err).__name__}: {err}")
                continue
            stamped += int(did)
            if why:
                reasons[why] = reasons.get(why, 0) + 1

        verb = "would stamp" if args.dry_run else "stamped"
        print(f"   rows seen : {seen}")
        print(f"   {verb:<9} : {stamped}")
        for why, n in sorted(reasons.items()):
            print(f"   skipped ({why}): {n}")

    if not args.verify and not args.dry_run and not failed:
        print()
        print(">> verifying ...")
        for name in tables:
            pk_attr, owner_attr = SPECS[name]
            print(f"== {name}")
            problems += verify(ddb.Table(name), pk_attr, owner_attr)

    if args.dry_run:
        print()
        print(">> Nothing was written. Re-run without --dry-run to migrate.")
    if problems:
        print(f"\n>> {problems} MISATTRIBUTED row(s) — investigate before "
              f"relying on the workspace index.")
    return 1 if (failed or problems) else 0


if __name__ == "__main__":
    sys.exit(main())
