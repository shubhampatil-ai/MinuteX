#!/usr/bin/env python3
# =============================================================
# 32_backfill_tasks.py — migrate the recording rows' embedded `tasks` maps
# into the first-class Tasks table.
#
# WHAT THIS IS FOR
#   Tasks used to live inside each recording row as a `tasks` map. They now
#   live in their own table so they can be queried across meetings (by
#   assignee, folder, status, due date). The userApi migrates a meeting's
#   tasks lazily, the first time that meeting's task list is read; this
#   script does the same work EAGERLY for the whole account, so the Task
#   Tracker is complete on day one instead of filling in as meetings are
#   opened.
#
# SAFETY PROPERTIES (spec section 15)
#   * IDEMPOTENT. An embedded entry is skipped when a Tasks row already
#     carries its `legacy_task_id`. Running this twice creates nothing the
#     second time; running it after the lazy path has already migrated a
#     meeting also creates nothing.
#   * NON-DESTRUCTIVE. The embedded `tasks` maps are never written to and
#     never cleared. This script only ever ADDS rows to the Tasks table, so
#     the old data stays exactly where it was and remains the fallback until
#     the counts below have been verified in production.
#   * NEVER GUESSES IDENTITY. A legacy assignee is a NAME string. It is
#     preserved verbatim in `assignee_name_legacy` with
#     resolution_status = UNRESOLVED. No name is matched to a Contact here,
#     even when exactly one contact has that name — that is the user's call
#     (spec section 16), made through
#     POST /tasks/{id}/resolve.
#   * REPORTS EVERYTHING. Old count, migrated count, skipped count,
#     unresolved count and failures are all printed, and the exit code is
#     non-zero if any row failed. There is no silent loss.
#
# ROLLBACK
#   Deleting the rows this created is enough to undo it — the source data was
#   never modified. Every row it writes carries migrated_by="32_backfill" and
#   a legacy_task_id, so they are identifiable:
#     aws dynamodb scan --table-name Tasks \
#       --filter-expression 'migrated_by = :m' \
#       --expression-attribute-values '{":m":{"S":"32_backfill"}}'
#
# USAGE
#   python scripts/32_backfill_tasks.py --dry-run      # report, write nothing
#   python scripts/32_backfill_tasks.py               # migrate
#   python scripts/32_backfill_tasks.py --user <uid>  # one account only
#   python scripts/32_backfill_tasks.py --verify      # compare counts only
#
# Credentials come from the environment / ~/.aws like every other script here.
# Table names come from .env (RECORDINGS_TABLE, TASKS_TABLE), defaulting to
# Recordings / Tasks.
# =============================================================
import argparse
import hashlib
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3
from boto3.dynamodb.conditions import Key

ROOT = Path(__file__).resolve().parents[1]


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


load_dotenv(ROOT / ".env")

REGION = os.environ.get("AWS_REGION", "ap-south-1")
RECORDINGS_TABLE = os.environ.get("RECORDINGS_TABLE", "Recordings")
TASKS_TABLE = os.environ.get("TASKS_TABLE", "Tasks")
TASKS_MEETING_INDEX = os.environ.get("TASKS_MEETING_INDEX", "meeting-index")

# Mirrors of the userApi's vocabulary. Duplicated deliberately: this script
# must be runnable without importing the Lambda (which builds boto3 resources
# and needs its whole env at import time), and these four strings are a stable
# contract, not logic.
TASK_STATUSES = ("Open", "In Progress", "Completed", "Cancelled")
TASK_PRIORITIES = ("Low", "Medium", "High")
STATUS_ALIASES = {
    "open": "Open", "in_progress": "In Progress", "in progress": "In Progress",
    "completed": "Completed", "complete": "Completed", "done": "Completed",
    "cancelled": "Cancelled", "canceled": "Cancelled",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip()).casefold()


def fingerprint(recording_key: str, text: str, hint: str = "") -> str:
    """Byte-for-byte the same fingerprint the Lambda computes, so a task
    migrated here is recognized as already-existing by the live seeding path
    (and vice versa). If this drifts from _task_fingerprint in
    functions/userapi/lambda_function.py, AI seeding will duplicate migrated
    tasks — they are a matched pair.

    MEETING + task text only. `hint` is accepted and ignored, mirroring the
    Lambda: the assignee is the most-edited field on a task, so hashing it made
    a reassigned task stop matching its own extraction and get duplicated.
    """
    basis = "\x00".join([recording_key, norm(text)])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


class Backfill:
    def __init__(self, dry_run: bool = False, only_user: str = ""):
        ddb = boto3.resource("dynamodb", region_name=REGION)
        self.recordings = ddb.Table(RECORDINGS_TABLE)
        self.tasks = ddb.Table(TASKS_TABLE)
        self.dry_run = dry_run
        self.only_user = only_user
        # Counters — every one of these is printed at the end.
        self.rows_scanned = 0
        self.rows_with_tasks = 0
        self.embedded_total = 0
        self.created = 0
        self.skipped_existing = 0
        self.skipped_mirror = 0
        self.unresolved = 0
        self.resolved = 0
        self.malformed = 0
        self.no_owner = 0
        self.failures: list = []

    # -- reading ---------------------------------------------------------
    def scan_recordings(self):
        """Every recording row, paged. A scan is correct here: this is a
        one-off whole-table migration, not a request path."""
        kwargs = {}
        while True:
            res = self.recordings.scan(**kwargs)
            for item in res.get("Items", []):
                yield item
            last = res.get("LastEvaluatedKey")
            if not last:
                return
            kwargs["ExclusiveStartKey"] = last

    def existing_legacy_ids(self, key: str) -> set:
        """legacy_task_id values already present for this meeting — the
        idempotency check."""
        out = set()
        kwargs = {
            "IndexName": TASKS_MEETING_INDEX,
            "KeyConditionExpression":
                Key("source_recording_id").eq(key),
        }
        while True:
            res = self.tasks.query(**kwargs)
            for row in res.get("Items", []):
                if row.get("legacy_task_id"):
                    out.add(row["legacy_task_id"])
            last = res.get("LastEvaluatedKey")
            if not last:
                return out
            kwargs["ExclusiveStartKey"] = last

    # -- writing ---------------------------------------------------------
    def build_row(self, user_id, key, folder_id, legacy_id, t) -> dict:
        """One embedded entry -> one Tasks row.

        The migrated task IS the old task: its original created_at/updated_at
        and its assignee NAME are carried over unchanged. Only the storage
        location changes.
        """
        status = t.get("status") or "Open"
        if status not in TASK_STATUSES:
            status = STATUS_ALIASES.get(str(status).casefold(), "Open")
        priority = t.get("priority") or "Medium"
        if priority not in TASK_PRIORITIES:
            priority = "Medium"

        assignee = t.get("assignee") if isinstance(t.get("assignee"), dict) else {}
        name = str((assignee or {}).get("name") or "").strip()

        title = str(t.get("task") or "").strip()[:300]
        row = {
            "task_id": uuid.uuid4().hex[:16],
            "owner_user_id": user_id,
            "title": title,
            "description": "",
            "status": status,
            "priority": priority,
            "due_date": str(t.get("due") or "").strip()[:100],
            "source_recording_id": key,
            # from_action_item told us the AI extracted it; anything else was
            # typed by a human, which is what LEGACY means here.
            "source_type": "AI" if t.get("from_action_item") else "LEGACY",
            "notified_via": list(t.get("notified_via") or []),
            "assignee_speaker_id": "",
            "created_at": t.get("created_at") or now_iso(),
            "updated_at": t.get("updated_at") or now_iso(),
            "completed_at": now_iso() if status == "Completed" else "",
            # Provenance: identifies every row this script created, both for
            # the idempotency check and for the rollback query in the header.
            "legacy_task_id": legacy_id,
            "migrated_by": "32_backfill",
            "migrated_at": now_iso(),
        }
        if folder_id:
            row["folder_id"] = folder_id
        if t.get("from_action_item"):
            # Match the live seeder's fingerprint so a later AI run recognizes
            # this task instead of creating a second copy of it.
            row["fingerprint"] = fingerprint(key, title, name)
        if name:
            # A NAME, explicitly not an identity. See the header.
            row["assignee_name_legacy"] = name[:120]
            row["resolution_status"] = "UNRESOLVED"
            self.unresolved += 1
            if (assignee or {}).get("email"):
                row["assignee_email"] = assignee["email"]
            if (assignee or {}).get("phone"):
                row["assignee_phone"] = assignee["phone"]
        else:
            row["resolution_status"] = "NONE"
        return row

    def run(self):
        for item in self.scan_recordings():
            self.rows_scanned += 1
            stored = item.get("tasks")
            if not isinstance(stored, dict) or not stored:
                continue

            key = item.get("audio_s3_key", "")
            user_id = item.get("user_id", "")
            if self.only_user and user_id != self.only_user:
                continue

            self.rows_with_tasks += 1
            self.embedded_total += len(stored)

            if not user_id:
                # A legacy row with no user_id has no owner to attribute the
                # task to, and owner_user_id is the Tasks table's whole
                # authorization basis — a row without it would be unreachable
                # AND unfiltered. Reported, never invented.
                self.no_owner += len(stored)
                self.failures.append(
                    (key, "no user_id on recording — cannot attribute owner"))
                continue

            existing = self.existing_legacy_ids(key)
            folder_id = str(item.get("folder_id") or "")

            for legacy_id, t in stored.items():
                if not isinstance(t, dict):
                    self.malformed += 1
                    continue
                if not str(t.get("task") or "").strip():
                    self.malformed += 1
                    continue
                if legacy_id in existing:
                    self.skipped_existing += 1
                    continue
                if t.get("task_id"):
                    # A mirror of a task the new API already wrote — its
                    # authoritative row exists; migrating it would duplicate.
                    self.skipped_mirror += 1
                    continue

                row = self.build_row(user_id, key, folder_id, legacy_id, t)
                if self.dry_run:
                    self.created += 1
                    continue
                try:
                    # Empty GSI key attributes must be ABSENT, not "" —
                    # DynamoDB rejects an empty-string index key outright.
                    # Non-indexed empty strings are fine and are kept so the
                    # row shape matches what the Lambda writes.
                    sparse = ("folder_id", "fingerprint",
                              "assignee_contact_id", "assignee_user_id")
                    self.tasks.put_item(
                        Item={k: v for k, v in row.items()
                              if not (k in sparse and not v)},
                        ConditionExpression="attribute_not_exists(task_id)")
                    self.created += 1
                except Exception as e:  # noqa: BLE001
                    self.failures.append(
                        (f"{key}/{legacy_id}", f"{type(e).__name__}: {e}"))

    def verify(self):
        """Compare embedded counts against migrated counts per meeting.

        This is the check that proves nothing was lost — spec section 15's
        "compare counts" step. A meeting is only OK when every embedded entry
        has a Tasks row (mirrors excluded, since those never needed one).
        """
        mismatches = []
        for item in self.scan_recordings():
            stored = item.get("tasks")
            if not isinstance(stored, dict) or not stored:
                continue
            key = item.get("audio_s3_key", "")
            user_id = item.get("user_id", "")
            if self.only_user and user_id != self.only_user:
                continue
            expected = {lid for lid, t in stored.items()
                        if isinstance(t, dict)
                        and str(t.get("task") or "").strip()
                        and not t.get("task_id")}
            if not expected:
                continue
            found = self.existing_legacy_ids(key)
            missing = expected - found
            if missing:
                mismatches.append((key, len(expected), len(expected - missing),
                                   sorted(missing)[:5]))
        return mismatches


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Migrate embedded recording tasks into the Tasks table.")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would happen; write nothing")
    ap.add_argument("--user", default="",
                    help="restrict to one owner_user_id")
    ap.add_argument("--verify", action="store_true",
                    help="only compare counts; migrate nothing")
    args = ap.parse_args()

    job = Backfill(dry_run=args.dry_run, only_user=args.user)

    print(f">> Region:     {REGION}")
    print(f">> Recordings: {RECORDINGS_TABLE}")
    print(f">> Tasks:      {TASKS_TABLE}")
    if args.user:
        print(f">> User:       {args.user}")
    print()

    if args.verify:
        print(">> Verifying migrated counts against embedded maps ...")
        mismatches = job.verify()
        if not mismatches:
            print("   OK — every embedded task has a Tasks row.")
            return 0
        print(f"   {len(mismatches)} meeting(s) INCOMPLETE:")
        for key, expected, found, sample in mismatches:
            print(f"     {key}: {found}/{expected} migrated, "
                  f"missing e.g. {sample}")
        print("\n   Re-run without --verify to migrate the remainder.")
        return 1

    if args.dry_run:
        print(">> DRY RUN — no writes will be made.")
    job.run()

    print()
    print("=" * 62)
    print("MIGRATION REPORT")
    print("=" * 62)
    print(f"  recording rows scanned      : {job.rows_scanned}")
    print(f"  rows carrying embedded tasks: {job.rows_with_tasks}")
    print(f"  embedded tasks found (OLD)  : {job.embedded_total}")
    print(f"  tasks created (MIGRATED)    : {job.created}")
    print(f"  skipped, already migrated   : {job.skipped_existing}")
    print(f"  skipped, mirror of new task : {job.skipped_mirror}")
    print(f"  unresolved assignees        : {job.unresolved}")
    print(f"  malformed entries skipped   : {job.malformed}")
    print(f"  unattributable (no user_id) : {job.no_owner}")
    print(f"  failures                    : {len(job.failures)}")

    accounted = (job.created + job.skipped_existing + job.skipped_mirror
                 + job.malformed + job.no_owner + len(job.failures))
    print(f"  accounted for               : {accounted} / {job.embedded_total}")
    if accounted != job.embedded_total:
        # Every embedded entry must land in exactly one bucket. If this line
        # ever prints, the buckets are wrong and the report cannot be trusted.
        print("  !! UNACCOUNTED ENTRIES — do not treat this run as complete")

    if job.failures:
        print("\n  FAILURES:")
        for where, why in job.failures[:20]:
            print(f"    {where}: {why}")
        if len(job.failures) > 20:
            print(f"    ... and {len(job.failures) - 20} more")

    print()
    if job.unresolved:
        print(f">> {job.unresolved} task(s) have a NAME but no Contact. That is")
        print("   intentional — nothing here guesses which person a name means.")
        print("   Resolve them in the app, or via POST /tasks/{id}/resolve.")
    if not args.dry_run and not job.failures and accounted == job.embedded_total:
        print(">> Migration complete. Verify with:")
        print("   python scripts/32_backfill_tasks.py --verify")
        print(">> The embedded `tasks` maps were NOT modified and remain the")
        print("   fallback. Do not remove them until --verify is clean and the")
        print("   app has been exercised against the new table.")

    return 1 if job.failures or accounted != job.embedded_total else 0


if __name__ == "__main__":
    sys.exit(main())
