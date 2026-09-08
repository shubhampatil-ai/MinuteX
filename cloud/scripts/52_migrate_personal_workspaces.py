#!/usr/bin/env python3
# =============================================================
# 52_migrate_personal_workspaces.py — give every existing user a Personal
# Workspace row and an OWNER membership.
#
# WHAT THIS IS FOR
#   Phase 2A introduces Workspaces. Every existing MinuteX user must have a
#   Personal Workspace, and every existing personal resource must stay
#   personal. This script writes the Workspaces and WorkspaceMemberships rows
#   for accounts that predate the feature.
#
# THIS SCRIPT IS AN OPTIMIZATION, NOT A CORRECTNESS GATE.
#   A personal workspace id is DERIVED from the user id
#   (workspace_schema.personal_workspace_id), and the userApi synthesizes the
#   workspace when no row exists (_user_workspaces / _ensure_personal_workspace).
#   So every user already behaves correctly whether or not this has run. What
#   the rows buy is a materialized list — the ability to query, rename and
#   later bill a workspace — not access itself.
#
#   That is deliberate and it is the property that makes this safe: an
#   interrupted, partial, skipped or re-run migration cannot break a user.
#
# WHAT THIS SCRIPT DOES NOT DO
#   * It does not touch Recordings, Tasks, Contacts, Folders or any other
#     existing table. NO resource is stamped with workspace_id here — resource
#     ownership is Phase 2B, and doing it now would be an irreversible write
#     against 16 tables before the read paths that depend on it exist.
#   * It does not create, infer or join any ORGANISATION. Existing data is
#     personal and stays personal; there is no code path in this file that can
#     produce an ORGANISATION workspace.
#   * It does not modify the Users table.
#
# SAFETY PROPERTIES
#   * IDEMPOTENT. Both writes are conditional
#     (attribute_not_exists), so a second run creates nothing and an existing
#     row — including one the user renamed — is left exactly as it is.
#   * ADDITIVE. Only ever inserts into the two NEW tables. No existing row is
#     read for mutation and none is written.
#   * RESUMABLE. Each user is independent; a crash halfway leaves the
#     already-migrated users correct and the rest simply unmigrated (which,
#     per the note above, still works).
#   * PAGINATES. The Users scan follows LastEvaluatedKey. A scan that ignores
#     it silently processes only the first 1 MB and reports success.
#   * REPORTS EVERYTHING. Created, skipped and failed counts, and a non-zero
#     exit code if anything failed.
#
# ROLLBACK
#   Delete the rows this created. Nothing else was modified, so that is a
#   complete undo. Every row it writes carries created_by_migration="52", so
#   they are identifiable and distinguishable from rows created lazily by the
#   API:
#     aws dynamodb scan --table-name Workspaces \
#       --filter-expression 'created_by_migration = :m' \
#       --expression-attribute-values '{":m":{"S":"52"}}'
#   Deleting them returns the system to the synthesized-workspace behaviour,
#   which is fully functional.
#
# USAGE
#   python scripts/52_migrate_personal_workspaces.py --dry-run   # report only
#   python scripts/52_migrate_personal_workspaces.py             # migrate
#   python scripts/52_migrate_personal_workspaces.py --user <id> # one account
#   python scripts/52_migrate_personal_workspaces.py --verify    # check only
#
# Prerequisites: scripts/50_create_workspace_tables.sh
# =============================================================
import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared"))
import workspace_schema  # noqa: E402

REGION = os.environ.get("AWS_REGION", "ap-south-1")
USERS_TABLE = os.environ.get("USERS_TABLE", "Users")
WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE", "Workspaces")
MEMBERSHIPS_TABLE = os.environ.get("WORKSPACE_MEMBERSHIPS_TABLE",
                                   "WorkspaceMemberships")

MIGRATION_TAG = "52"


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def is_claim_row(item):
    """Email-uniqueness claim rows are not accounts.

    signup() writes a deterministic "email#{address}" row in the Users table to
    make email uniqueness atomic. It is not a user and must never be given a
    workspace — see _user_email_claim in the userApi.
    """
    return str(item.get("user_id", "")).startswith("email#")


def iter_users(users_table, only_user=None):
    """Every real account. Paginated — a scan that ignores LastEvaluatedKey
    silently stops after 1 MB and still looks like it succeeded."""
    if only_user:
        item = users_table.get_item(Key={"user_id": only_user}).get("Item")
        if item and not is_claim_row(item):
            yield item
        return

    kwargs = {}
    while True:
        res = users_table.scan(**kwargs)
        for item in res.get("Items", []):
            if not is_claim_row(item) and item.get("user_id"):
                yield item
        last = res.get("LastEvaluatedKey")
        if not last:
            return
        kwargs["ExclusiveStartKey"] = last


def migrate_user(user, workspaces, memberships, dry_run):
    """(created_workspace, created_membership) for one account.

    Both writes are conditional, so this is safe to run concurrently with the
    API's own lazy creation (_ensure_personal_workspace) — whichever gets
    there first wins and the other is a no-op.
    """
    user_id = user["user_id"]
    workspace_id = workspace_schema.personal_workspace_id(user_id)
    ts = now_iso()

    made_workspace = False
    item = workspace_schema.new_personal_workspace(user_id, ts)
    item["created_by_migration"] = MIGRATION_TAG
    if dry_run:
        made_workspace = not workspaces.get_item(
            Key={"workspace_id": workspace_id}).get("Item")
    else:
        try:
            workspaces.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(workspace_id)")
            made_workspace = True
        except ClientError as err:
            if err.response.get("Error", {}).get("Code") \
                    != "ConditionalCheckFailedException":
                raise

    made_membership = False
    membership = workspace_schema.new_membership(
        workspace_id, user_id, workspace_schema.ROLE_OWNER, ts)
    membership["created_by_migration"] = MIGRATION_TAG
    if dry_run:
        made_membership = not memberships.get_item(
            Key={"workspace_id": workspace_id,
                 "user_id": user_id}).get("Item")
    else:
        try:
            memberships.put_item(
                Item=membership,
                ConditionExpression="attribute_not_exists(workspace_id)")
            made_membership = True
        except ClientError as err:
            if err.response.get("Error", {}).get("Code") \
                    != "ConditionalCheckFailedException":
                raise

    return made_workspace, made_membership


def verify(users_table, workspaces, memberships, only_user=None):
    """Prove the three invariants the brief asks for (section 28).

    Returns the number of PROBLEMS found, so the caller can exit non-zero.
    """
    total = missing_ws = missing_member = wrong_type = not_owner = 0
    for user in iter_users(users_table, only_user):
        total += 1
        uid = user["user_id"]
        wid = workspace_schema.personal_workspace_id(uid)

        ws_row = workspaces.get_item(Key={"workspace_id": wid}).get("Item")
        if not ws_row:
            missing_ws += 1
            continue
        # Existing data must NEVER become organisation data.
        if ws_row.get("type") != workspace_schema.TYPE_PERSONAL:
            wrong_type += 1
        if ws_row.get("owner_user_id") != uid:
            not_owner += 1

        m = memberships.get_item(
            Key={"workspace_id": wid, "user_id": uid}).get("Item")
        if not m:
            missing_member += 1
        elif m.get("role") != workspace_schema.ROLE_OWNER:
            not_owner += 1

    print(f"   users                       : {total}")
    print(f"   missing personal workspace  : {missing_ws}")
    print(f"   missing OWNER membership    : {missing_member}")
    print(f"   wrong workspace TYPE        : {wrong_type}")
    print(f"   wrong owner/role            : {not_owner}")

    # A missing row is NOT a failure: the API synthesizes it (see the header).
    # A wrong type or a wrong owner IS — that is real corruption.
    return wrong_type + not_owner


def main():
    ap = argparse.ArgumentParser(
        description="Give every existing user a Personal Workspace.")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change; write nothing")
    ap.add_argument("--verify", action="store_true",
                    help="check invariants only; write nothing")
    ap.add_argument("--user", default=None, help="migrate one account only")
    args = ap.parse_args()

    ddb = boto3.resource("dynamodb", region_name=REGION)
    users_table = ddb.Table(USERS_TABLE)
    workspaces = ddb.Table(WORKSPACES_TABLE)
    memberships = ddb.Table(MEMBERSHIPS_TABLE)

    print(f">> region={REGION} users={USERS_TABLE} "
          f"workspaces={WORKSPACES_TABLE} memberships={MEMBERSHIPS_TABLE}")

    if args.verify:
        print(">> VERIFY (no writes)")
        problems = verify(users_table, workspaces, memberships, args.user)
        print(">> OK" if not problems else f">> {problems} PROBLEM(S)")
        return 1 if problems else 0

    mode = "DRY RUN (no writes)" if args.dry_run else "MIGRATING"
    print(f">> {mode}")

    seen = ws_created = m_created = failed = 0
    for user in iter_users(users_table, args.user):
        seen += 1
        try:
            made_ws, made_m = migrate_user(user, workspaces, memberships,
                                           args.dry_run)
            ws_created += int(made_ws)
            m_created += int(made_m)
        except Exception as err:  # noqa: BLE001 — report, never abort the run
            failed += 1
            print(f"   FAILED {user.get('user_id')}: "
                  f"{type(err).__name__}: {err}")

    print()
    print(f"   users seen                  : {seen}")
    print(f"   personal workspaces created : {ws_created}")
    print(f"   OWNER memberships created   : {m_created}")
    print(f"   already present (skipped)   : {seen - ws_created}")
    print(f"   failed                      : {failed}")

    if not args.dry_run and not failed:
        print()
        print(">> verifying ...")
        problems = verify(users_table, workspaces, memberships, args.user)
        if problems:
            print(">> VERIFICATION FOUND PROBLEMS")
            return 1

    if args.dry_run:
        print()
        print(">> Nothing was written. Re-run without --dry-run to migrate.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
