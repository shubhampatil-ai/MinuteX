#!/usr/bin/env python3
"""59_remove_folders.py — delete the folder feature's live data.

DESTRUCTIVE AND IRREVERSIBLE. The user explicitly confirmed removing folders
everywhere, personal workspaces included, "including the live data".

WHAT IT TOUCHES

  1. Folders            every row, INCLUDING the `name#<user>#<name>` uniqueness
                        claim rows. Those are not folders — they are the
                        conditional-write reservations create_folder used to
                        enforce per-owner name uniqueness — but nothing reads
                        them any more either, so they go too. Counting them AS
                        folders is the mistake to avoid: it roughly doubles the
                        apparent scale (19 rows vs 7 real folders).
  2. FolderContacts     every row. These are ASSOCIATIONS, not people; deleting
                        one never deletes a contact.
  3. Recordings         removes the `folder_id` ATTRIBUTE from any row carrying
                        one. The meeting itself is untouched — same one-attribute
                        edit the old "move to General" did.
  4. Tasks              same, for `folder_id` inherited from a meeting.

WHAT IT DOES NOT TOUCH: no recording, task, contact or participant row is ever
deleted. Only the two folder tables, and one attribute elsewhere.

SAFETY

  * --dry-run (DEFAULT) reports exactly what would change and writes nothing.
    Pass --apply to actually delete.
  * Every page is enumerated with LastEvaluatedKey. A Scan caps at 1 MB, so a
    single-page scan would silently miss rows and report success.
  * Re-runnable: deleting an absent row and REMOVEing an absent attribute are
    both no-ops, so an interrupted run can simply be repeated.

BACK UP FIRST. This does not snapshot anything:

    aws dynamodb scan --table-name Folders        --output json > Folders.json
    aws dynamodb scan --table-name FolderContacts --output json > FolderContacts.json
    aws dynamodb scan --table-name Recordings --filter-expression \\
        "attribute_exists(folder_id)" --output json > Recordings_folder_id.json
    aws dynamodb scan --table-name Tasks --filter-expression \\
        "attribute_exists(folder_id)" --output json > Tasks_folder_id.json

Run from cloud/:
    python scripts/59_remove_folders.py              # dry run
    python scripts/59_remove_folders.py --apply
"""
import argparse
import os
import sys

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("AWS_REGION", "ap-south-1")
FOLDERS_TABLE = os.environ.get("FOLDERS_TABLE", "Folders")
FOLDER_CONTACTS_TABLE = os.environ.get("FOLDER_CONTACTS_TABLE",
                                       "FolderContacts")
RECORDINGS_TABLE = os.environ.get("RECORDINGS_TABLE", "Recordings")
TASKS_TABLE = os.environ.get("TASKS_TABLE", "Tasks")


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
                    help="actually delete (default is a dry run)")
    args = ap.parse_args()
    apply = args.apply

    ddb = boto3.resource("dynamodb", region_name=REGION)
    folders = ddb.Table(FOLDERS_TABLE)
    folder_contacts = ddb.Table(FOLDER_CONTACTS_TABLE)
    recordings = ddb.Table(RECORDINGS_TABLE)
    tasks = ddb.Table(TASKS_TABLE)

    mode = "APPLY (destructive)" if apply else "DRY RUN (no writes)"
    print(f"=== 59_remove_folders — {mode} ===")
    print(f"region={REGION}\n")

    # ---- 1. Folders -------------------------------------------------------
    rows = scan_all(folders)
    claims = [r for r in rows
              if str(r.get("folder_id", "")).startswith("name#")]
    real = [r for r in rows if r not in claims]
    print(f"{FOLDERS_TABLE}: {len(rows)} rows "
          f"= {len(real)} folders + {len(claims)} name-claim rows")
    for r in real:
        print(f"    folder {r.get('folder_id')} "
              f"owner={r.get('owner_user_id')} name={r.get('name')!r}")
    if apply:
        with folders.batch_writer() as bw:
            for r in rows:
                bw.delete_item(Key={"folder_id": r["folder_id"]})
        print(f"    -> deleted {len(rows)} rows")

    # ---- 2. FolderContacts ------------------------------------------------
    links = scan_all(folder_contacts)
    print(f"\n{FOLDER_CONTACTS_TABLE}: {len(links)} association rows "
          f"(contacts themselves are NOT deleted)")
    if apply:
        with folder_contacts.batch_writer() as bw:
            for r in links:
                bw.delete_item(Key={"folder_id": r["folder_id"],
                                    "contact_id": r["contact_id"]})
        print(f"    -> deleted {len(links)} rows")

    # ---- 3/4. folder_id attribute elsewhere -------------------------------
    for table, key_name, label in ((recordings, "audio_s3_key",
                                    RECORDINGS_TABLE),
                                   (tasks, "task_id", TASKS_TABLE)):
        filed = scan_all(table,
                         FilterExpression="attribute_exists(folder_id)",
                         ProjectionExpression=f"{key_name}, folder_id")
        print(f"\n{label}: {len(filed)} rows carry folder_id "
              f"(attribute removed; rows kept)")
        for r in filed[:10]:
            print(f"    {r.get(key_name)} folder_id={r.get('folder_id')}")
        if len(filed) > 10:
            print(f"    ... and {len(filed) - 10} more")
        if apply:
            done = 0
            for r in filed:
                try:
                    table.update_item(
                        Key={key_name: r[key_name]},
                        UpdateExpression="REMOVE folder_id",
                        # Belt and braces: never CREATE a row here. Without
                        # this an update_item on a key that vanished
                        # mid-migration would resurrect it as a stub.
                        ConditionExpression=f"attribute_exists({key_name})")
                    done += 1
                except ClientError as err:
                    code = err.response.get("Error", {}).get("Code")
                    if code == "ConditionalCheckFailedException":
                        continue          # row went away; nothing to clear
                    raise
            print(f"    -> cleared folder_id on {done} rows")

    print("\n" + ("Done." if apply
                  else "Dry run only — nothing was written. "
                       "Re-run with --apply to delete."))
    return 0


if __name__ == "__main__":
    sys.exit(main())
