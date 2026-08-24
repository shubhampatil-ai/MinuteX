#!/usr/bin/env python3
# =============================================================
# query_recording.py — print a recording row by audio_s3_key.
#
#   python tests/query_recording.py esp32-001/meeting-190680_190680.wav
#
# Shows transcript + summary + sync_status (+ error) from the
# 'recordings' DynamoDB table. Region + table from .env.
# =============================================================
import os
import sys
from pathlib import Path

import boto3


def load_dotenv(env_path: Path) -> None:
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")
    region = os.environ.get("AWS_REGION", "ap-south-1")
    table = os.environ.get("RECORDINGS_TABLE", "recordings")

    if len(sys.argv) < 2:
        print("usage: python tests/query_recording.py <audio_s3_key>")
        return 2
    key = sys.argv[1]

    ddb = boto3.resource("dynamodb", region_name=region)
    resp = ddb.Table(table).get_item(Key={"audio_s3_key": key})
    item = resp.get("Item")
    if not item:
        print(f"No row for audio_s3_key='{key}' in {table}")
        return 1

    def field(name):
        return item.get(name, "")

    print("=" * 70)
    print(f"audio_s3_key   : {field('audio_s3_key')}")
    print(f"device_id      : {field('device_id')}")
    print(f"meeting_id     : {field('meeting_id')}")
    print(f"recorded_at    : {field('recorded_at')}")
    print(f"detected_lang  : {field('detected_language')}")
    print(f"transcribed_at : {field('transcribed_at')}")
    print(f"sync_status    : {field('sync_status')}")
    if field("sync_error"):
        print(f"sync_error     : {field('sync_error')}")
    if field("salesforce_object"):
        print(f"salesforce     : {field('salesforce_object')}/{field('salesforce_record_id')}")
    print("-" * 70)
    print("SUMMARY:")
    print(f"  {field('summary')}")
    print("-" * 70)
    print("TRANSCRIPT:")
    print(field("transcript") or "  (empty)")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
