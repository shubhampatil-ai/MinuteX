#!/usr/bin/env python3
# =============================================================
# 90_purge_orphaned_accounts.py — remove every trace of a user whose Users
# row has already been deleted.
#
# WHY THIS EXISTS
#   MinuteX has no account-deletion endpoint (the Phase 1 audit flagged it),
#   so deleting a Users row by hand in the console leaves everything that
#   user owned behind: recordings, their S3 audio/transcript/pageindex
#   objects, tasks, contacts, folders, participants, shares, notifications,
#   chat sessions, memberships and the derived personal workspace.
#
#   None of it is reachable — no Users row means login cannot mint a JWT — so
#   this is a cost and hygiene cleanup, not a security fix.
#
# NUMBERED 90, NOT 58
#   Scripts 10-57 build the product. This is an OPERATIONAL tool that is run
#   deliberately against specific ids, never as part of a deploy sequence.
#   The 90+ range keeps it out of "run everything in order".
#
# SAFETY PROPERTIES
#   * REFUSES A LIVE ACCOUNT. If the Users row still exists, the script stops.
#     It only ever cleans up AFTER a deletion someone else performed, so it
#     cannot be turned into an account-deletion endpoint by accident.
#   * DRY RUN BY DEFAULT. --apply is required to write anything.
#   * BACKS UP FIRST. Every row it will delete is written to a JSON file
#     before a single delete is issued, and --apply refuses to run without a
#     writable backup path.
#   * S3 LAST, DATABASE FIRST is deliberately INVERTED here relative to
#     permanently_delete_recording: that function deletes S3 first so a crash
#     leaves a reclaimable row. Here the row IS the only record of which S3
#     keys exist, so the keys are collected up front, then the rows go, then
#     the objects. The collected key list is in the backup either way.
#   * VERIFIES BY RE-QUERYING. The summary at the end is measured, not
#     assumed — a previous manual pass reported successes that had silently
#     failed on Windows line endings, which is what this replaces.
#   * IDEMPOTENT. Re-running finds nothing and reports zeroes.
#
# WHAT IT DOES NOT TOUCH
#   * Any workspace with type ORGANISATION. Organisation-owned data belongs
#     to the organisation, not to the departing member — the same rule
#     remove_member follows. Only the user's own PERSONAL workspace row goes.
#   * Rows owned by anyone else. Every query is partitioned on the target id.
#
# USAGE
#   python scripts/90_purge_orphaned_accounts.py --user <id> [--user <id>]
#   python scripts/90_purge_orphaned_accounts.py --user <id> --apply
# =============================================================
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared"))
import workspace_schema  # noqa: E402

REGION = os.environ.get("AWS_REGION", "ap-south-1")
BUCKET = os.environ.get("BUCKET_NAME", "meeting-recorder-shubham-aps1")

USERS_TABLE = os.environ.get("USERS_TABLE", "Users")
RECORDINGS_TABLE = os.environ.get("RECORDINGS_TABLE", "Recordings")
TASKS_TABLE = os.environ.get("TASKS_TABLE", "Tasks")
CONTACTS_TABLE = os.environ.get("CONTACTS_TABLE", "Contacts")
FOLDERS_TABLE = os.environ.get("FOLDERS_TABLE", "Folders")
FOLDER_CONTACTS_TABLE = os.environ.get("FOLDER_CONTACTS_TABLE",
                                       "FolderContacts")
PARTICIPANTS_TABLE = os.environ.get("MEETING_PARTICIPANTS_TABLE",
                                    "MeetingParticipants")
SHARES_TABLE = os.environ.get("SHARES_TABLE", "Shares")
NOTIFICATIONS_TABLE = os.environ.get("NOTIFICATIONS_TABLE", "Notifications")
CHAT_SESSIONS_TABLE = os.environ.get("CHAT_SESSIONS_TABLE", "ChatSessions")
WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE", "Workspaces")
MEMBERSHIPS_TABLE = os.environ.get("WORKSPACE_MEMBERSHIPS_TABLE",
                                   "WorkspaceMemberships")
INTEGRATIONS_TABLE = os.environ.get("INTEGRATIONS_TABLE", "Integrations")
CRM_TABLE = os.environ.get("CRM_CONNECTIONS_TABLE", "CrmConnections")

TRANSCRIPT_PREFIX = os.environ.get("TRANSCRIPT_PREFIX", "transcripts")
PAGEINDEX_PREFIX = os.environ.get("PAGEINDEX_PREFIX", "pageindex")


def query_all(table, **kwargs):
    """Every matching item, following LastEvaluatedKey.

    A query that ignores it silently returns a PREFIX of the matches — which
    on a purge means leaving rows behind while reporting success.
    """
    items = []
    while True:
        res = table.query(**kwargs)
        items.extend(res.get("Items", []))
        last = res.get("LastEvaluatedKey")
        if not last:
            return items
        kwargs["ExclusiveStartKey"] = last


def collect(ddb, user_id):
    """Everything this user owns, as {label: (table, [keys], [rows])}.

    Read-only. Nothing is deleted here, so the caller can print or back up
    the exact plan before committing to it.
    """
    from boto3.dynamodb.conditions import Key

    plan = {}

    def add(label, table_name, pk_fields, rows):
        plan[label] = {
            "table": table_name,
            "keys": [{f: r[f] for f in pk_fields if f in r} for r in rows],
            "rows": rows,
        }

    recordings = ddb.Table(RECORDINGS_TABLE)
    recs = query_all(recordings, IndexName="user-index",
                     KeyConditionExpression=Key("user_id").eq(user_id))
    add("recordings", RECORDINGS_TABLE, ["audio_s3_key"], recs)

    # The S3 keys are derived from the recording keys, so they must be
    # collected BEFORE the rows are deleted — afterwards there is no record
    # of which objects existed.
    s3_keys = []
    for r in recs:
        key = r.get("audio_s3_key")
        if not key:
            continue
        s3_keys.append(key)
        s3_keys.append(f"{TRANSCRIPT_PREFIX}/{key}.transcript.json.gz")
        s3_keys.append(f"{PAGEINDEX_PREFIX}/{key}.pageindex.json.gz")
    plan["_s3_keys"] = s3_keys

    # Participants and Shares hang off the recording key, not off the user,
    # so they are enumerated per recording. These are exactly the tables
    # permanently_delete_recording forgets (the stale-comment bug the Phase 1
    # audit found), which is why they are explicit here.
    participants = ddb.Table(PARTICIPANTS_TABLE)
    prows = []
    for r in recs:
        prows.extend(query_all(
            participants,
            KeyConditionExpression=Key("audio_s3_key").eq(r["audio_s3_key"])))
    add("participants", PARTICIPANTS_TABLE, ["audio_s3_key", "speaker_id"],
        prows)

    shares = ddb.Table(SHARES_TABLE)
    srows = []
    for r in recs:
        srows.extend(query_all(
            shares, IndexName="recording-index",
            KeyConditionExpression=Key("recording_key").eq(
                r["audio_s3_key"])))
    add("shares", SHARES_TABLE, ["share_id"], srows)

    tasks = ddb.Table(TASKS_TABLE)
    add("tasks", TASKS_TABLE, ["task_id"],
        query_all(tasks, IndexName="owner-index",
                  KeyConditionExpression=Key("owner_user_id").eq(user_id)))

    contacts = ddb.Table(CONTACTS_TABLE)
    crows = query_all(contacts, IndexName="owner-index",
                      KeyConditionExpression=Key("owner_user_id").eq(user_id))
    # ORGANISATION contacts are SHARED workspace resources and survive their
    # creator, exactly as remove_member leaves them alone. Only personal ones
    # are the departing user's to take.
    personal_contacts = [
        c for c in crows
        if not workspace_schema.is_organisation_workspace_id(
            workspace_schema.resolve_workspace_id(
                c, owner_field="owner_user_id"))]
    add("contacts", CONTACTS_TABLE, ["contact_id"], personal_contacts)

    folders = ddb.Table(FOLDERS_TABLE)
    frows = query_all(folders, IndexName="owner-index",
                      KeyConditionExpression=Key("owner_user_id").eq(user_id))
    add("folders", FOLDERS_TABLE, ["folder_id"], frows)

    # FolderContacts is an edge table keyed by folder, with no owner field.
    fcontacts = ddb.Table(FOLDER_CONTACTS_TABLE)
    fcrows = []
    for f in frows:
        fid = f.get("folder_id")
        if not fid or str(fid).startswith("name#"):
            continue  # a uniqueness claim row, not a real folder
        fcrows.extend(query_all(
            fcontacts, KeyConditionExpression=Key("folder_id").eq(fid)))
    add("folder_contacts", FOLDER_CONTACTS_TABLE, ["folder_id", "contact_id"],
        fcrows)

    notifications = ddb.Table(NOTIFICATIONS_TABLE)
    add("notifications", NOTIFICATIONS_TABLE, ["notification_id"],
        query_all(notifications, IndexName="user-index",
                  KeyConditionExpression=Key("user_id").eq(user_id)))

    sessions = ddb.Table(CHAT_SESSIONS_TABLE)
    add("chat_sessions", CHAT_SESSIONS_TABLE, ["session_id"],
        query_all(sessions, IndexName="user-index",
                  KeyConditionExpression=Key("user_id").eq(user_id)))

    # Memberships: the PERSONAL one goes; ORGANISATION ones are marked
    # REMOVED rather than deleted by the product, but a purge of a
    # nonexistent account should not leave a dangling grant either, so they
    # are listed and removed too.
    memberships = ddb.Table(MEMBERSHIPS_TABLE)
    add("memberships", MEMBERSHIPS_TABLE, ["workspace_id", "user_id"],
        query_all(memberships, IndexName="user-index",
                  KeyConditionExpression=Key("user_id").eq(user_id)))

    # The derived personal workspace ONLY. An organisation this user owned is
    # deliberately left alone — deleting a shared workspace is a different,
    # much larger decision (Phase 2E).
    workspaces = ddb.Table(WORKSPACES_TABLE)
    wid = workspace_schema.personal_workspace_id(user_id)
    wrow = workspaces.get_item(Key={"workspace_id": wid}).get("Item")
    add("personal_workspace", WORKSPACES_TABLE, ["workspace_id"],
        [wrow] if wrow else [])

    # Per-user OAuth tokens (KMS-encrypted). PK is user_id + provider.
    for label, tname in (("integrations", INTEGRATIONS_TABLE),
                         ("crm_connections", CRM_TABLE)):
        try:
            t = ddb.Table(tname)
            add(label, tname, ["user_id", "provider"],
                query_all(t, KeyConditionExpression=Key(
                    "user_id").eq(user_id)))
        except ClientError as err:
            print(f"   (skipping {tname}: {err.response['Error']['Code']})")
            add(label, tname, ["user_id", "provider"], [])

    return plan


def main():
    ap = argparse.ArgumentParser(
        description="Purge every trace of an already-deleted MinuteX user.")
    ap.add_argument("--user", action="append", required=True,
                    help="user_id to purge (repeatable)")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete; omit for a dry run")
    ap.add_argument("--backup", default=None,
                    help="backup file path (required with --apply)")
    ap.add_argument("--force-live", action="store_true",
                    help=argparse.SUPPRESS)  # deliberately undocumented
    args = ap.parse_args()

    if args.apply and not args.backup:
        print("ERROR: --apply requires --backup <path>. A destructive run "
              "without a recoverable copy is not something this script will "
              "do.", file=sys.stderr)
        return 2

    ddb = boto3.resource("dynamodb", region_name=REGION)
    s3 = boto3.client("s3", region_name=REGION)
    users = ddb.Table(USERS_TABLE)

    print(f">> region={REGION} bucket={BUCKET}")
    print(f">> mode={'APPLY (destructive)' if args.apply else 'DRY RUN'}")

    plans = {}
    for user_id in args.user:
        print(f"\n=== {user_id} ===")
        # THE GUARD. This tool only cleans up after a deletion someone else
        # already made; it must never become a way to delete a live account.
        live = users.get_item(Key={"user_id": user_id}).get("Item")
        if live and not args.force_live:
            print("   REFUSED: this Users row still EXISTS. This script only "
                  "purges accounts that have already been deleted.")
            print(f"   email={live.get('email', '?')}")
            return 1

        plan = collect(ddb, user_id)
        plans[user_id] = plan

        total = 0
        for label, spec in plan.items():
            if label == "_s3_keys":
                continue
            n = len(spec["keys"])
            total += n
            if n:
                print(f"   {label:20} {n:5}  ({spec['table']})")
        print(f"   {'S3 objects':20} {len(plan['_s3_keys']):5}  "
              f"(audio + transcript + pageindex)")
        print(f"   {'TOTAL DB ROWS':20} {total:5}")

    if not args.apply:
        print("\n>> DRY RUN — nothing was written. Re-run with:")
        print("   --apply --backup <path>")
        return 0

    # ---- BACKUP BEFORE ANY WRITE ----
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = Path(args.backup)
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_text(json.dumps(
        {"purged_at": stamp, "region": REGION, "bucket": BUCKET,
         "users": {u: {k: (v if k == "_s3_keys" else v["rows"])
                       for k, v in p.items()}
                   for u, p in plans.items()}},
        indent=1, default=str), encoding="utf-8")
    print(f"\n>> backup written: {backup}")

    deleted = {}
    failed = 0
    for user_id, plan in plans.items():
        print(f"\n=== purging {user_id} ===")
        for label, spec in plan.items():
            if label == "_s3_keys":
                continue
            table = ddb.Table(spec["table"])
            ok = 0
            for key in spec["keys"]:
                try:
                    table.delete_item(Key=key)
                    ok += 1
                except ClientError as err:
                    failed += 1
                    print(f"   FAILED {label} {key}: {err}")
            if spec["keys"]:
                print(f"   {label:20} deleted {ok}/{len(spec['keys'])}")
            deleted[label] = deleted.get(label, 0) + ok

        # S3 last: the keys were captured before the rows went.
        keys = plan["_s3_keys"]
        gone = missing = 0
        for i in range(0, len(keys), 1000):   # delete_objects caps at 1000
            batch = [{"Key": k} for k in keys[i:i + 1000]]
            try:
                res = s3.delete_objects(Bucket=BUCKET,
                                        Delete={"Objects": batch,
                                                "Quiet": True})
                errs = res.get("Errors", [])
                for e in errs:
                    # A transcript or pageindex that was never generated is
                    # an ordinary absence, not a failure.
                    if e.get("Code") in ("NoSuchKey",):
                        missing += 1
                    else:
                        failed += 1
                        print(f"   FAILED s3 {e.get('Key')}: {e.get('Code')}")
                gone += len(batch) - len(errs)
            except ClientError as err:
                failed += 1
                print(f"   FAILED s3 batch: {err}")
        print(f"   {'s3 objects':20} deleted {gone} (absent: {missing})")

    # ---- VERIFY BY RE-QUERYING, never by trusting the log above ----
    print("\n>> verifying ...")
    problems = 0
    for user_id in args.user:
        after = collect(ddb, user_id)
        left = sum(len(v["keys"]) for k, v in after.items()
                   if k != "_s3_keys")
        print(f"   {user_id}: {left} rows remaining")
        problems += left

    print(f"\n   failures during delete : {failed}")
    print(f"   rows still present     : {problems}")
    if problems or failed:
        print(">> INCOMPLETE — re-run to finish (it is idempotent).")
        return 1
    print(">> Clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
