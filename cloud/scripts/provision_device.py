#!/usr/bin/env python3
# =============================================================
# provision_device.py — register a device in DynamoDB.
#
# Generates a random API key, stores {apiKey, deviceId, location}
# in the DeviceKeys table (device AUTHENTICATION), and creates the
# device's lifecycle row in the Devices table (device OWNERSHIP:
# status=UNPAIRED, serial_number, firmware_version). Prints the key
# ONCE. This is how you create a device such as esp32-001.
#
# The API key is the ONLY secret the device holds. The backend maps
# apiKey -> deviceId at request time; the device never sends its own
# deviceId (see the security model in the README). Ownership is a
# separate concern: a freshly provisioned device is UNPAIRED and
# cannot upload until a user pairs it (POST /devices/pair-request
# + /devices/pair on the userApi).
#
# Credentials come from your ~/.aws profile. Region + table name come
# from .env (loaded below). Nothing secret is written to disk here —
# the printed key is yours to copy into the device / .env.
#
# Usage:
#   python scripts/provision_device.py --device-id esp32-001 --location "Office"
#   python scripts/provision_device.py --device-id esp32-002   # location optional
# =============================================================
import argparse
import os
import secrets
import sys
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


def load_dotenv(env_path: Path) -> None:
    """Minimal .env loader — only sets vars that aren't already set."""
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        # Do not clobber an already-exported value.
        os.environ.setdefault(key, val)


def make_api_key() -> str:
    """dk_live_<43 url-safe base64 chars> — ~256 bits of entropy."""
    return "dk_live_" + secrets.token_urlsafe(32)


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    load_dotenv(project_root / ".env")

    region = os.environ.get("AWS_REGION", "ap-south-1")
    table_name = os.environ.get("TABLE_NAME", "DeviceKeys")

    parser = argparse.ArgumentParser(description="Provision a device API key.")
    parser.add_argument("--device-id", required=True,
                        help="Logical device id, e.g. esp32-001")
    parser.add_argument("--location", default="",
                        help="Optional human-readable location label")
    parser.add_argument("--serial-number", default="",
                        help="Optional hardware serial number (printed next "
                             "to the QR code as the scan fallback)")
    parser.add_argument("--firmware-version", default="",
                        help="Optional firmware version flashed at the factory")
    parser.add_argument("--table", default=table_name,
                        help=f"DynamoDB table (default: {table_name})")
    parser.add_argument("--devices-table",
                        default=os.environ.get("DEVICES_TABLE", "Devices"),
                        help="Devices lifecycle table (default: Devices)")
    parser.add_argument("--region", default=region,
                        help=f"AWS region (default: {region})")
    args = parser.parse_args()

    api_key = make_api_key()

    ddb = boto3.client("dynamodb", region_name=args.region)
    item = {
        "apiKey":   {"S": api_key},
        "deviceId": {"S": args.device_id},
        "location": {"S": args.location},
    }

    try:
        # attribute_not_exists(apiKey) guards against the (astronomically
        # unlikely) collision of overwriting an existing key.
        ddb.put_item(
            TableName=args.table,
            Item=item,
            ConditionExpression="attribute_not_exists(apiKey)",
        )
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code == "ConditionalCheckFailedException":
            print("ERROR: generated key collided with an existing one; "
                  "re-run to try again.", file=sys.stderr)
        else:
            print(f"ERROR: DynamoDB put_item failed: {code}: {e}",
                  file=sys.stderr)
        return 1

    # Ownership row (Devices): factory-fresh devices start UNPAIRED with no
    # owner. attribute_not_exists keeps a re-run (new key for an existing
    # device) from resetting a live pairing.
    from datetime import datetime, timezone
    try:
        ddb.put_item(
            TableName=args.devices_table,
            Item={
                "device_id":        {"S": args.device_id},
                "status":           {"S": "UNPAIRED"},
                "serial_number":    {"S": args.serial_number},
                "firmware_version": {"S": args.firmware_version},
                "created_at":       {"S": datetime.now(timezone.utc)
                                          .isoformat().replace("+00:00", "Z")},
            },
            ConditionExpression="attribute_not_exists(device_id)",
        )
        devices_row = "created (UNPAIRED)"
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            devices_row = "already exists — left untouched"
        else:
            print(f"ERROR: Devices put_item failed: {e}", file=sys.stderr)
            return 1

    print("=" * 60)
    print(f"  Device provisioned:  {args.device_id}")
    print(f"  Devices row:         {devices_row}")
    if args.serial_number:
        print(f"  Serial number:       {args.serial_number}")
    if args.location:
        print(f"  Location:            {args.location}")
    print(f"  Table:               {args.table}  (region {args.region})")
    print("-" * 60)
    print("  API KEY (shown once — copy it now):")
    print(f"    {api_key}")
    print("-" * 60)
    print("  Put this in .env as DEVICE_API_KEY for the simulator,")
    print("  and flash it into the device's x-api-key header.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
