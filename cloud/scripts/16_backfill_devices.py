#!/usr/bin/env python3
# =============================================================
# 16_backfill_devices.py — migrate existing devices into the
# Devices lifecycle table (user-owned architecture).
#
# For every row in DeviceKeys (apiKey -> deviceId) this creates a
# Devices row, defaulting to the spec's safe migration state:
#
#     status         = UNPAIRED
#     paired_user_id = (absent)
#
# NOTE: an UNPAIRED device CANNOT upload (getUploadUrl returns
# 403 "Device not paired"). For a live fleet that must keep
# uploading through the migration, run with --adopt-claims: any
# device with EXACTLY ONE legacy UserDevices claim is promoted to
# PAIRED for that user (paired_at = the original claimed_at).
# Devices claimed by MULTIPLE users (pre-enforcement data) are
# reported and left UNPAIRED — resolve those by hand, one owner
# per device is now the rule.
#
# Idempotent and non-destructive: existing Devices rows are never
# overwritten, and promotion only touches rows still UNPAIRED.
#
# Usage:
#   python scripts/16_backfill_devices.py
#   python scripts/16_backfill_devices.py --adopt-claims
# =============================================================
import argparse
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


def load_dotenv(env_path: Path) -> None:
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def scan_all(table) -> list:
    items, kwargs = [], {}
    while True:
        res = table.scan(**kwargs)
        items.extend(res.get("Items", []))
        lek = res.get("LastEvaluatedKey")
        if not lek:
            return items
        kwargs["ExclusiveStartKey"] = lek


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    region = os.environ.get("AWS_REGION", "ap-south-1")

    p = argparse.ArgumentParser(description="Backfill the Devices table.")
    p.add_argument("--adopt-claims", action="store_true",
                   help="Promote single-user legacy UserDevices claims to PAIRED")
    p.add_argument("--device-keys-table",
                   default=os.environ.get("TABLE_NAME", "DeviceKeys"))
    p.add_argument("--devices-table",
                   default=os.environ.get("DEVICES_TABLE", "Devices"))
    p.add_argument("--user-devices-table",
                   default=os.environ.get("USER_DEVICES_TABLE", "UserDevices"))
    p.add_argument("--region", default=region)
    args = p.parse_args()

    ddb = boto3.resource("dynamodb", region_name=args.region)
    device_keys = ddb.Table(args.device_keys_table)
    devices = ddb.Table(args.devices_table)

    # ---- 1. DeviceKeys -> Devices rows (UNPAIRED) -------------------
    created, skipped = 0, 0
    device_ids = sorted({it["deviceId"] for it in scan_all(device_keys)
                         if it.get("deviceId")})
    for device_id in device_ids:
        try:
            devices.put_item(
                Item={"device_id": device_id, "status": "UNPAIRED",
                      "serial_number": "", "firmware_version": "",
                      "created_at": now_iso()},
                ConditionExpression="attribute_not_exists(device_id)",
            )
            created += 1
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            skipped += 1
    print(f">> Devices rows: {created} created, {skipped} already existed "
          f"({len(device_ids)} devices in {args.device_keys_table}).")

    # ---- 2. Optional: adopt unambiguous legacy claims ---------------
    if not args.adopt_claims:
        print(">> Skipping claim adoption (all new rows stay UNPAIRED).")
        print(">> Re-run with --adopt-claims to keep a live fleet uploading.")
        return 0

    user_devices = ddb.Table(args.user_devices_table)
    claims = defaultdict(list)  # device_id -> [(user_id, claimed_at), ...]
    for it in scan_all(user_devices):
        claims[it["device_id"]].append((it["user_id"], it.get("claimed_at", "")))

    promoted, conflicts, already = 0, 0, 0
    for device_id, users in sorted(claims.items()):
        if len(users) > 1:
            conflicts += 1
            owners = ", ".join(u for u, _ in users)
            print(f"   CONFLICT {device_id}: claimed by {owners} — left UNPAIRED")
            continue
        user_id, claimed_at = users[0]
        try:
            devices.update_item(
                Key={"device_id": device_id},
                UpdateExpression=("SET #st = :paired, paired_user_id = :u, "
                                  "paired_at = :at"),
                ConditionExpression=("attribute_exists(device_id) AND "
                                     "#st = :unpaired"),
                ExpressionAttributeNames={"#st": "status"},
                ExpressionAttributeValues={":paired": "PAIRED", ":u": user_id,
                                           ":at": claimed_at or now_iso(),
                                           ":unpaired": "UNPAIRED"},
            )
            promoted += 1
        except ClientError as e:
            if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
            already += 1  # already PAIRING/PAIRED — leave it alone
    print(f">> Claims adopted: {promoted} promoted to PAIRED, "
          f"{conflicts} multi-user conflicts left UNPAIRED, "
          f"{already} already had a state.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
