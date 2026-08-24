#!/usr/bin/env python3
# =============================================================
# test_device_mgmt.py — rename / factory-reset / pairing-gated presign,
# against REAL AWS (same style as test_pairing_api.py).
#
# Covers what scripts/20_deploy_device_mgmt.sh ships:
#   1.  Presign while UNPAIRED -> 403 "Device not paired".
#   2.  Pair the device (pair-request + pair).
#   3.  Presign while PAIRED -> 200, key under
#       recordings/{owner}/{device}/{meeting}_{ts}.wav.
#   4.  Upload really succeeds against that presigned URL.
#   5.  last_seen was touched by the presign.
#   6.  PATCH /devices/{id} renames; GET reflects it.
#   7.  PATCH by a non-owner -> 404 (no existence leak).
#   8.  PATCH with an over-long name -> 400.
#   9.  PATCH "" clears the name back to the device_id default.
#   10. factory-reset by a non-owner -> 404.
#   11. factory-reset by the owner -> 200, device UNPAIRED, name cleared,
#       factory_reset_at stamped.
#   12. Recordings SURVIVE the reset (only ownership is released).
#   13. Presign after reset -> 403 again (hardware cut off).
#   14. The device can be re-paired afterwards.
#
# Creates ephemeral users/devices and deletes them at the end. The S3
# object uploaded in step 4 is deleted too.
#
# Requires (from .env / environment):
#   API_URL, AWS_REGION, BUCKET_NAME  (+ table names if not the defaults)
# =============================================================
import os
import sys
import uuid
from pathlib import Path

import boto3
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_tests import check, load_dotenv, _results  # noqa: E402
import device_simulator as wire  # noqa: E402


def api(method: str, base: str, path: str, token: str = None, body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.request(method, base + path, headers=headers,
                            json=body, timeout=20)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")

    base = os.environ.get("API_URL", "").rstrip("/")
    region = os.environ.get("AWS_REGION", "ap-south-1")
    bucket = os.environ.get("BUCKET_NAME", "")
    keys_table = os.environ.get("TABLE_NAME", "DeviceKeys")
    devices_table = os.environ.get("DEVICES_TABLE", "Devices")
    users_table = os.environ.get("USERS_TABLE", "Users")
    user_devices_table = os.environ.get("USER_DEVICES_TABLE", "UserDevices")
    recordings_table = os.environ.get("RECORDINGS_TABLE", "Recordings")
    if not base or not bucket:
        print("ERROR: API_URL and BUCKET_NAME must be set")
        return 2

    ddb = boto3.client("dynamodb", region_name=region)
    s3 = boto3.client("s3", region_name=region)
    run_tag = uuid.uuid4().hex[:8]
    device_id = f"mgmttest-{run_tag}"
    api_key = f"dk_test_{uuid.uuid4().hex}"
    cleanup, s3_cleanup = [], []

    ddb.put_item(TableName=keys_table,
                 Item={"apiKey": {"S": api_key}, "deviceId": {"S": device_id},
                       "location": {"S": "test_device_mgmt ephemeral"}})
    cleanup.append((keys_table, {"apiKey": {"S": api_key}}))
    ddb.put_item(TableName=devices_table,
                 Item={"device_id": {"S": device_id},
                       "status": {"S": "UNPAIRED"},
                       "serial_number": {"S": f"SN-{run_tag}"},
                       "firmware_version": {"S": "1.7.4"}})
    cleanup.append((devices_table, {"device_id": {"S": device_id}}))

    tokens, user_ids = {}, {}
    for who in ("owner", "other"):
        r = api("POST", base, "/signup",
                body={"email": f"{who}-{run_tag}@mgmttest.local",
                      "password": "mgmttest-password", "name": who})
        if r.status_code != 201:
            print(f"ERROR: signup {who} failed: {r.status_code} {r.text}")
            return 2
        data = r.json()
        tokens[who], user_ids[who] = data["token"], data["user_id"]
        cleanup.append((users_table, {"user_id": {"S": data["user_id"]}}))
    print(f"Device: {device_id}   users: owner, other   API: {base}")
    print("-" * 60)

    meeting_id = f"meeting-{run_tag}"
    timestamp = "1721460000"

    # ---- 1. presign while UNPAIRED -> 403 -------------------------------
    r = wire.get_upload_url(base, api_key, meeting_id, timestamp)
    check("1. presign while UNPAIRED -> 403 not paired",
          r.status_code == 403 and "not paired" in r.text.lower(),
          f"status={r.status_code} body={r.text[:120]}")

    # ---- 2. pair -------------------------------------------------------
    r = api("POST", base, "/devices/pair-request", tokens["owner"],
            {"device_id": device_id})
    code = (r.json() or {}).get("pairing_code", "") if r.status_code == 200 else ""
    r2 = api("POST", base, "/devices/pair", tokens["owner"],
             {"device_id": device_id, "pairing_code": code})
    check("2. pair -> 200 PAIRED",
          r2.status_code == 200
          and (r2.json() or {}).get("device", {}).get("status") == "PAIRED",
          f"request={r.status_code} pair={r2.status_code} {r2.text[:120]}")
    cleanup.append((user_devices_table,
                    {"user_id": {"S": user_ids["owner"]},
                     "device_id": {"S": device_id}}))

    # ---- 3. presign while PAIRED -> user-owned key ----------------------
    r = wire.get_upload_url(base, api_key, meeting_id, timestamp,
                            device_id=device_id)
    key = (r.json() or {}).get("key", "") if r.status_code == 200 else ""
    expected = f"recordings/{user_ids['owner']}/{device_id}/{meeting_id}_{timestamp}.wav"
    check("3. presign while PAIRED -> 200 + user-owned key",
          r.status_code == 200 and key == expected,
          f"status={r.status_code} key={key!r} expected={expected!r}")
    presigned_url = (r.json() or {}).get("url", "") if r.status_code == 200 else ""
    if key:
        s3_cleanup.append(key)

    # ---- 4. the presigned URL actually accepts the upload ---------------
    wav = Path(__file__).parent / "sample.wav"
    wav_bytes = wav.read_bytes() if wav.is_file() else b"RIFF\0\0\0\0WAVE"
    if presigned_url:
        up = wire.upload_to_presigned(presigned_url, wav_bytes)
        check("4. PUT to presigned URL -> 200/204",
              up.status_code in (200, 204),
              f"status={up.status_code} body={up.text[:120]}")
    else:
        check("4. PUT to presigned URL -> 200/204", False, "no url from step 3")

    # ---- 5. presign touched last_seen -----------------------------------
    row = ddb.get_item(TableName=devices_table,
                       Key={"device_id": {"S": device_id}}).get("Item") or {}
    check("5. presign touched last_seen", "last_seen" in row,
          f"attrs={sorted(row)}")

    # ---- 6. rename ------------------------------------------------------
    r = api("PATCH", base, f"/devices/{device_id}", tokens["owner"],
            {"name": "Boardroom Recorder"})
    g = api("GET", base, f"/devices/{device_id}", tokens["owner"])
    check("6. PATCH name -> 200 and GET reflects it",
          r.status_code == 200
          and (r.json() or {}).get("device", {}).get("name") == "Boardroom Recorder"
          and (g.json() or {}).get("device", {}).get("name") == "Boardroom Recorder",
          f"patch={r.status_code} get={g.status_code} {r.text[:120]}")

    # ---- 7. rename by a non-owner -> 404 --------------------------------
    r = api("PATCH", base, f"/devices/{device_id}", tokens["other"],
            {"name": "stolen"})
    check("7. PATCH by non-owner -> 404", r.status_code == 404,
          f"got {r.status_code}")

    # ---- 8. over-long name -> 400 ---------------------------------------
    r = api("PATCH", base, f"/devices/{device_id}", tokens["owner"],
            {"name": "x" * 65})
    check("8. PATCH over-long name -> 400", r.status_code == 400,
          f"got {r.status_code}")

    # ---- 9. empty name clears back to device_id -------------------------
    r = api("PATCH", base, f"/devices/{device_id}", tokens["owner"],
            {"name": ""})
    check("9. PATCH empty name -> defaults to device_id",
          r.status_code == 200
          and (r.json() or {}).get("device", {}).get("name") == device_id,
          f"status={r.status_code} {r.text[:120]}")
    # Re-apply a name so step 11 can prove the reset clears it.
    api("PATCH", base, f"/devices/{device_id}", tokens["owner"],
        {"name": "Pre-reset Name"})

    # ---- 10. factory-reset by a non-owner -> 404 ------------------------
    r = api("POST", base, f"/devices/{device_id}/factory-reset", tokens["other"])
    check("10. factory-reset by non-owner -> 404", r.status_code == 404,
          f"got {r.status_code}")

    # ---- 11. factory-reset by the owner --------------------------------
    r = api("POST", base, f"/devices/{device_id}/factory-reset", tokens["owner"])
    row = ddb.get_item(TableName=devices_table,
                       Key={"device_id": {"S": device_id}}).get("Item") or {}
    check("11. factory-reset -> 200, UNPAIRED, name+owner cleared",
          r.status_code == 200
          and (r.json() or {}).get("reset") is True
          and row.get("status", {}).get("S") == "UNPAIRED"
          and "paired_user_id" not in row
          and "name" not in row
          and "factory_reset_at" in row,
          f"status={r.status_code} attrs={sorted(row)} {r.text[:120]}")

    # ---- 12. recordings survive the reset ------------------------------
    # The presign-created row is written by transcribeRecording on upload,
    # so assert on the S3 object (authoritative) plus any Recordings row.
    survived = False
    if key:
        try:
            s3.head_object(Bucket=bucket, Key=key)
            survived = True
        except Exception as e:  # noqa: BLE001
            survived = False
            detail = repr(e)[:120]
    check("12. recording object survives factory reset", survived,
          "" if survived else f"head_object failed: {detail if key else 'no key'}")

    # ---- 13. presign after reset -> 403 --------------------------------
    r = wire.get_upload_url(base, api_key, f"{meeting_id}b", timestamp)
    check("13. presign after factory reset -> 403", r.status_code == 403,
          f"got {r.status_code} {r.text[:120]}")

    # ---- 14. re-pair after reset ---------------------------------------
    r = api("POST", base, "/devices/pair-request", tokens["other"],
            {"device_id": device_id})
    code2 = (r.json() or {}).get("pairing_code", "") if r.status_code == 200 else ""
    r2 = api("POST", base, "/devices/pair", tokens["other"],
             {"device_id": device_id, "pairing_code": code2})
    check("14. re-pair after factory reset -> 200 PAIRED",
          r2.status_code == 200
          and (r2.json() or {}).get("device", {}).get("status") == "PAIRED",
          f"request={r.status_code} pair={r2.status_code}")
    cleanup.append((user_devices_table,
                    {"user_id": {"S": user_ids["other"]},
                     "device_id": {"S": device_id}}))

    # ---- cleanup --------------------------------------------------------
    for k in s3_cleanup:
        try:
            s3.delete_object(Bucket=bucket, Key=k)
        except Exception:
            pass
    # transcribeRecording may have created a Recordings row for the upload.
    for k in s3_cleanup:
        try:
            ddb.delete_item(TableName=recordings_table,
                            Key={"audio_s3_key": {"S": k}})
        except Exception:
            pass
    for tbl, tkey in cleanup:
        try:
            ddb.delete_item(TableName=tbl, Key=tkey)
        except Exception:
            pass

    print("-" * 60)
    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print(f"RESULT: {passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
