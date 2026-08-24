#!/usr/bin/env python3
# =============================================================
# run_tests.py — verify the upload backend against REAL AWS.
#
# Tests (Stage 1 gate + user-owned architecture):
#   1. Valid upload -> 200, and object exists at
#        recordings/{userId}/{deviceId}/{meetingId}_{timestamp}.wav
#      (requires the test device to be PAIRED — pair it first via the
#       userApi, or run scripts/16_backfill_devices.py --adopt-claims).
#   2. Missing API key -> 401.
#   3. Unknown API key -> 403.
#   4. Expired presigned URL -> 403.
#   5. Path-injection in meetingId (e.g. "../../evil") is sanitized
#      and cannot escape the key prefix.
#   6. UNPAIRED device -> 403 "Device not paired" (ephemeral device
#      rows are created in DynamoDB and cleaned up afterwards).
#   7. x-device-id header that mismatches the api key -> 403.
#   8. Heartbeat -> 200 and Devices.last_seen advances.
#
# Requires (from .env / environment):
#   API_URL, DEVICE_API_KEY, BUCKET_NAME, AWS_REGION
# The device's deviceId is resolved from DynamoDB (TABLE_NAME) using
# DEVICE_API_KEY, and its owner from DEVICES_TABLE, so the test knows
# where the object should land.
#
# AWS creds come from ~/.aws. Deepgram is NOT involved here.
# =============================================================
import os
import sys
import time
import uuid
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from botocore.config import Config

import requests

# Import the simulator (the wire-format reference).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import device_simulator as dev  # noqa: E402


# ---------- tiny .env loader + colored pass/fail ----------
def load_dotenv(env_path: Path) -> None:
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip())


PASS, FAIL = "PASS", "FAIL"
_results = []


def check(name: str, ok: bool, detail: str = ""):
    _results.append((name, ok, detail))
    tag = PASS if ok else FAIL
    line = f"[{tag}] {name}"
    if detail:
        line += f"  — {detail}"
    print(line)


# ---------------------------------------------------------------
def resolve_device_id(table_name: str, api_key: str, region: str) -> str:
    ddb = boto3.client("dynamodb", region_name=region)
    resp = ddb.get_item(TableName=table_name, Key={"apiKey": {"S": api_key}})
    item = resp.get("Item")
    if not item:
        raise RuntimeError(f"DEVICE_API_KEY not found in table {table_name}")
    return item["deviceId"]["S"]


def get_device_row(devices_table: str, device_id: str, region: str) -> dict:
    """The Devices lifecycle row ({} if the table/row doesn't exist yet)."""
    ddb = boto3.client("dynamodb", region_name=region)
    try:
        resp = ddb.get_item(TableName=devices_table,
                            Key={"device_id": {"S": device_id}})
    except ClientError:
        return {}
    item = resp.get("Item") or {}
    return {k: list(v.values())[0] for k, v in item.items()}


def object_exists(s3, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        code = e.response["Error"]["Code"]
        # 404/NoSuchKey/NotFound = plainly absent.
        # 400 Bad Request = the key itself is malformed (e.g. contains
        # "../../"), so S3 won't even look it up — for our purposes that
        # means "no such object could exist at this path". Both are
        # treated as 'does not exist' rather than propagated.
        if code in ("404", "NoSuchKey", "NotFound", "400", "BadRequest"):
            return False
        raise


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")

    api_url    = os.environ.get("API_URL", "").rstrip("/")
    api_key    = os.environ.get("DEVICE_API_KEY", "")
    bucket     = os.environ.get("BUCKET_NAME", "")
    region     = os.environ.get("AWS_REGION", "ap-south-1")
    table_name = os.environ.get("TABLE_NAME", "DeviceKeys")
    wav_path   = Path(__file__).parent / "sample.wav"

    missing = [n for n, v in
               [("API_URL", api_url), ("DEVICE_API_KEY", api_key),
                ("BUCKET_NAME", bucket)] if not v]
    if missing:
        print(f"ERROR: missing required config: {', '.join(missing)}")
        return 2
    if not wav_path.is_file():
        print(f"ERROR: sample wav not found at {wav_path} "
              f"(run: python tests/make_sample_wav.py)")
        return 2

    s3 = boto3.client("s3", region_name=region,
                      config=Config(signature_version="s3v4"))
    device_id = resolve_device_id(table_name, api_key, region)
    devices_table = os.environ.get("DEVICES_TABLE", "Devices")
    device_row = get_device_row(devices_table, device_id, region)
    paired_user = device_row.get("paired_user_id", "")
    print(f"Resolved deviceId from DB: {device_id}")
    print(f"Device status: {device_row.get('status', '(no Devices row)')} "
          f"paired_user={paired_user or '(none)'}")
    if not paired_user or device_row.get("status") != "PAIRED":
        print("WARNING: test device is not PAIRED — test 1 will get "
              "403 'Device not paired'. Pair it via the userApi "
              "(POST /devices/pair-request + /devices/pair) or run "
              "scripts/16_backfill_devices.py --adopt-claims first.")
    print(f"API: {api_url}")
    print("-" * 60)

    # Unique ids so re-runs don't collide.
    run_tag   = uuid.uuid4().hex[:8]
    meeting_1 = f"meeting-{run_tag}"
    ts_1      = str(int(time.time()))
    created_keys = []

    # ----- Test 1: valid upload -> 200, object exists -------------
    # User-owned key layout: recordings/{userId}/{deviceId}/{recordingId}.wav
    try:
        res = dev.record_and_upload(api_url, api_key, meeting_1, ts_1, wav_path)
        hs_ok = res.get("handshake_status") == 200
        up_ok = res.get("upload_status") in (200, 204)
        expected_key = f"recordings/{paired_user}/{device_id}/{meeting_1}_{ts_1}.wav"
        key_ok = res.get("key") == expected_key
        exists = hs_ok and up_ok and object_exists(s3, bucket, res.get("key", ""))
        if res.get("key"):
            created_keys.append(res["key"])
        check("1. valid upload -> 200 + object exists",
              hs_ok and up_ok and key_ok and exists,
              f"handshake={res.get('handshake_status')} "
              f"upload={res.get('upload_status')} "
              f"key={res.get('key')} exists={exists}")
    except Exception as e:
        check("1. valid upload -> 200 + object exists", False, f"exception: {e}")

    # ----- Test 2: missing API key -> 401 -------------------------
    try:
        r = dev.get_upload_url(api_url, None, meeting_1, ts_1)
        check("2. missing api key -> 401", r.status_code == 401,
              f"got {r.status_code}")
    except Exception as e:
        check("2. missing api key -> 401", False, f"exception: {e}")

    # ----- Test 3: unknown API key -> 403 -------------------------
    try:
        r = dev.get_upload_url(api_url, "dk_live_totally_unknown_key_xyz",
                               meeting_1, ts_1)
        check("3. unknown api key -> 403", r.status_code == 403,
              f"got {r.status_code}")
    except Exception as e:
        check("3. unknown api key -> 403", False, f"exception: {e}")

    # ----- Test 4: expired presigned URL -> 403 -------------------
    # We presign a PUT the SAME way the Lambda does but with ExpiresIn=1,
    # then wait it out. S3 rejects an expired v4 signature with 403.
    try:
        exp_key = f"{device_id}/expired-{run_tag}_{ts_1}.wav"
        short_url = s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": bucket, "Key": exp_key,
                    "ContentType": "audio/wav"},
            ExpiresIn=1,
        )
        time.sleep(3)  # let it expire
        r = dev.upload_to_presigned(short_url, wav_path.read_bytes())
        expired_ok = r.status_code == 403
        # Extra safety: object must NOT exist.
        landed = object_exists(s3, bucket, exp_key)
        if landed:
            created_keys.append(exp_key)
        check("4. expired presigned url -> 403",
              expired_ok and not landed,
              f"got {r.status_code}, landed={landed}")
    except Exception as e:
        check("4. expired presigned url -> 403", False, f"exception: {e}")

    # ----- Test 5: path-injection sanitized -----------------------
    # meetingId="../../evil" must be REJECTED (400) by the backend, so
    # no object can be created outside the deviceId prefix.
    try:
        evil = "../../evil"
        r = dev.get_upload_url(api_url, api_key, evil, ts_1)
        rejected = r.status_code == 400
        # Prove nothing escaped: the naive "escaped" key must not exist.
        escaped_key = f"evil_{ts_1}.wav"      # what a traversal might produce
        escaped_key2 = f"../../evil_{ts_1}.wav"
        escaped = (object_exists(s3, bucket, escaped_key)
                   or object_exists(s3, bucket, escaped_key2))
        check("5. path-injection in meetingId sanitized (400, no escape)",
              rejected and not escaped,
              f"status={r.status_code} escaped_object={escaped}")
    except Exception as e:
        check("5. path-injection in meetingId sanitized", False,
              f"exception: {e}")

    # ----- Test 6: UNPAIRED device -> 403 "Device not paired" -----
    # Provision an ephemeral device (auth row + UNPAIRED lifecycle row),
    # prove the presign is refused, then remove both rows.
    tmp_key = f"dk_test_{uuid.uuid4().hex}"
    tmp_dev = f"testdev-{run_tag}"
    ddb = boto3.client("dynamodb", region_name=region)
    try:
        ddb.put_item(TableName=table_name,
                     Item={"apiKey": {"S": tmp_key},
                           "deviceId": {"S": tmp_dev},
                           "location": {"S": "run_tests ephemeral"}})
        ddb.put_item(TableName=devices_table,
                     Item={"device_id": {"S": tmp_dev},
                           "status": {"S": "UNPAIRED"}})
        r = dev.get_upload_url(api_url, tmp_key, meeting_1, ts_1)
        body_ok = "not paired" in (r.text or "").lower()
        check("6. unpaired device -> 403 Device not paired",
              r.status_code == 403 and body_ok,
              f"got {r.status_code}: {r.text[:120]}")
    except Exception as e:
        check("6. unpaired device -> 403 Device not paired", False,
              f"exception: {e}")
    finally:
        for tbl, key in ((table_name, {"apiKey": {"S": tmp_key}}),
                         (devices_table, {"device_id": {"S": tmp_dev}})):
            try:
                ddb.delete_item(TableName=tbl, Key=key)
            except Exception:
                pass

    # ----- Test 7: x-device-id mismatching the key -> 403 ----------
    try:
        r = dev.get_upload_url(api_url, api_key, meeting_1, ts_1,
                               device_id="some-other-device")
        check("7. x-device-id mismatch -> 403", r.status_code == 403,
              f"got {r.status_code}")
    except Exception as e:
        check("7. x-device-id mismatch -> 403", False, f"exception: {e}")

    # ----- Test 8: heartbeat -> 200 + last_seen advances ------------
    try:
        before = get_device_row(devices_table, device_id, region).get("last_seen", "")
        time.sleep(1.1)  # ISO timestamps are second-resolution safe
        r = dev.heartbeat(api_url, api_key, firmware_version="test-1.0.0")
        after = get_device_row(devices_table, device_id, region).get("last_seen", "")
        check("8. heartbeat -> 200 + last_seen advances",
              r.status_code == 200 and after > before,
              f"status={r.status_code} before={before or '(unset)'} after={after}")
    except Exception as e:
        check("8. heartbeat -> 200 + last_seen advances", False,
              f"exception: {e}")

    # ----- cleanup: remove objects this run created ---------------
    for k in created_keys:
        try:
            s3.delete_object(Bucket=bucket, Key=k)
        except Exception:
            pass

    # ----- summary -----------------------------------------------
    print("-" * 60)
    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print(f"RESULT: {passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
