#!/usr/bin/env python3
# =============================================================
# test_pairing_api.py — verify the pairing/ownership API against
# REAL AWS (same style as run_tests.py).
#
# Exercises the user-owned architecture end to end:
#   1.  pair-request -> 200, 6-digit code, expires_in.
#   2.  pair with a WRONG code -> 403.
#   3.  pair with the right code but the WRONG account -> 403.
#   4.  pair (right code, right account) -> 200, status PAIRED.
#   5.  pair-request by a second user on a paired device -> 409.
#   6.  GET /devices lists it for the owner (details incl. status).
#   7.  GET /devices/{id}: owner -> 200 with detail fields,
#       non-owner -> 404.
#   8.  Device presign while PAIRED -> 200, key under
#       recordings/{owner}/{device}/...
#   9.  DELETE /devices/{id} by a non-owner -> 404.
#   10. DELETE /devices/{id} by the owner -> 200 (unpair).
#   11. Device presign after unpair -> 403 "Device not paired".
#   12. Re-pairing by the second user succeeds (device is free again).
#   13. Expired pairing code -> 410 (expiry is forced via DynamoDB).
#
# Creates ephemeral users (random emails) and an ephemeral device
# (DeviceKeys + Devices rows); everything is deleted afterwards.
#
# Requires (from .env / environment):
#   API_URL, AWS_REGION  (+ table names if not the defaults)
# =============================================================
import os
import sys
import time
import uuid
from pathlib import Path

import boto3
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_tests import check, load_dotenv, _results  # noqa: E402


def api(method: str, base: str, path: str, token: str = None, body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.request(method, base + path, headers=headers,
                            json=body, timeout=15)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")

    base = os.environ.get("API_URL", "").rstrip("/")
    region = os.environ.get("AWS_REGION", "ap-south-1")
    keys_table = os.environ.get("TABLE_NAME", "DeviceKeys")
    devices_table = os.environ.get("DEVICES_TABLE", "Devices")
    users_table = os.environ.get("USERS_TABLE", "Users")
    user_devices_table = os.environ.get("USER_DEVICES_TABLE", "UserDevices")
    recordings_table = os.environ.get("RECORDINGS_TABLE", "Recordings")
    if not base:
        print("ERROR: API_URL not set")
        return 2

    ddb = boto3.client("dynamodb", region_name=region)
    run_tag = uuid.uuid4().hex[:8]
    device_id = f"pairtest-{run_tag}"
    api_key = f"dk_test_{uuid.uuid4().hex}"
    cleanup = []  # (table, key) pairs deleted at the end

    # ---- setup: ephemeral device + two ephemeral users ----------------
    ddb.put_item(TableName=keys_table,
                 Item={"apiKey": {"S": api_key}, "deviceId": {"S": device_id},
                       "location": {"S": "test_pairing_api ephemeral"}})
    cleanup.append((keys_table, {"apiKey": {"S": api_key}}))
    ddb.put_item(TableName=devices_table,
                 Item={"device_id": {"S": device_id},
                       "status": {"S": "UNPAIRED"},
                       "serial_number": {"S": f"SN-{run_tag}"},
                       "firmware_version": {"S": "1.7.4"}})
    cleanup.append((devices_table, {"device_id": {"S": device_id}}))

    tokens, user_ids = {}, {}
    for who in ("alice", "bob"):
        r = api("POST", base, "/signup",
                body={"email": f"{who}-{run_tag}@pairtest.local",
                      "password": "pairtest-password", "name": who})
        if r.status_code != 201:
            print(f"ERROR: signup {who} failed: {r.status_code} {r.text}")
            return 2
        data = r.json()
        tokens[who], user_ids[who] = data["token"], data["user_id"]
        cleanup.append((users_table, {"user_id": {"S": data["user_id"]}}))
    print(f"Device: {device_id}   users: alice, bob   API: {base}")
    print("-" * 60)

    # ---- 1. pair-request -> code -------------------------------------
    r = api("POST", base, "/devices/pair-request", tokens["alice"],
            {"device_id": device_id})
    code = (r.json() or {}).get("pairing_code", "") if r.status_code == 200 else ""
    check("1. pair-request -> 200 + 6-digit code",
          r.status_code == 200 and len(code) == 6 and code.isdigit()
          and (r.json() or {}).get("expires_in") == 300,
          f"status={r.status_code} body={r.text[:120]}")

    # ---- 2. wrong code -> 403 -----------------------------------------
    wrong = "000000" if code != "000000" else "999999"
    r = api("POST", base, "/devices/pair", tokens["alice"],
            {"device_id": device_id, "pairing_code": wrong})
    check("2. pair with wrong code -> 403", r.status_code == 403,
          f"got {r.status_code}")

    # ---- 3. right code, wrong account -> 403 --------------------------
    r = api("POST", base, "/devices/pair", tokens["bob"],
            {"device_id": device_id, "pairing_code": code})
    check("3. pair with another account's code -> 403", r.status_code == 403,
          f"got {r.status_code}")

    # ---- 4. pair -> PAIRED --------------------------------------------
    r = api("POST", base, "/devices/pair", tokens["alice"],
            {"device_id": device_id, "pairing_code": code})
    dev = (r.json() or {}).get("device", {}) if r.status_code == 200 else {}
    check("4. pair -> 200 + status PAIRED",
          r.status_code == 200 and dev.get("status") == "PAIRED",
          f"status={r.status_code} device={dev}")
    cleanup.append((user_devices_table,
                    {"user_id": {"S": user_ids["alice"]},
                     "device_id": {"S": device_id}}))

    # ---- 5. second user pair-request -> 409 ---------------------------
    r = api("POST", base, "/devices/pair-request", tokens["bob"],
            {"device_id": device_id})
    check("5. pair-request on paired device -> 409", r.status_code == 409,
          f"got {r.status_code}")

    # ---- 6. GET /devices lists it for the owner -----------------------
    r = api("GET", base, "/devices", tokens["alice"])
    body = r.json() if r.status_code == 200 else {}
    details = {d.get("device_id"): d for d in body.get("details", [])}
    check("6. GET /devices shows device + details",
          device_id in body.get("devices", [])
          and details.get(device_id, {}).get("status") == "PAIRED",
          f"status={r.status_code} devices={body.get('devices')}")

    # ---- 7. device detail: owner 200, non-owner 404 --------------------
    r = api("GET", base, f"/devices/{device_id}", tokens["alice"])
    dev = (r.json() or {}).get("device", {}) if r.status_code == 200 else {}
    fields_ok = all(k in dev for k in
                    ("firmware_version", "last_seen", "paired_at",
                     "serial_number", "status", "battery", "storage"))
    r2 = api("GET", base, f"/devices/{device_id}", tokens["bob"])
    check("7. detail: owner -> 200 all fields, non-owner -> 404",
          r.status_code == 200 and fields_ok and r2.status_code == 404,
          f"owner={r.status_code} fields_ok={fields_ok} other={r2.status_code}")

    # ---- 8. presign while PAIRED -> key under the owner ----------------
    ts = str(int(time.time()))
    r = requests.get(f"{base}/get-upload-url",
                     headers={"x-api-key": api_key},
                     params={"meetingId": f"pt-{run_tag}", "timestamp": ts},
                     timeout=15)
    key = (r.json() or {}).get("key", "") if r.status_code == 200 else ""
    expected = f"recordings/{user_ids['alice']}/{device_id}/pt-{run_tag}_{ts}.wav"
    check("8. presign while PAIRED -> user-owned key",
          r.status_code == 200 and key == expected,
          f"status={r.status_code} key={key}")
    if key:
        cleanup.append((recordings_table, {"audio_s3_key": {"S": key}}))

    # ---- 9/10. unpair: non-owner 404, owner 200 -------------------------
    r = api("DELETE", base, f"/devices/{device_id}", tokens["bob"])
    check("9. unpair by non-owner -> 404", r.status_code == 404,
          f"got {r.status_code}")
    r = api("DELETE", base, f"/devices/{device_id}", tokens["alice"])
    check("10. unpair by owner -> 200", r.status_code == 200
          and (r.json() or {}).get("unpaired") is True,
          f"status={r.status_code} body={r.text[:120]}")

    # ---- 11. presign after unpair -> 403 Device not paired --------------
    r = requests.get(f"{base}/get-upload-url",
                     headers={"x-api-key": api_key},
                     params={"meetingId": f"pt2-{run_tag}", "timestamp": ts},
                     timeout=15)
    check("11. presign after unpair -> 403 Device not paired",
          r.status_code == 403 and "not paired" in r.text.lower(),
          f"status={r.status_code} body={r.text[:120]}")

    # ---- 12. re-pair by the second user ---------------------------------
    r = api("POST", base, "/devices/pair-request", tokens["bob"],
            {"device_id": device_id})
    code2 = (r.json() or {}).get("pairing_code", "") if r.status_code == 200 else ""
    r2 = api("POST", base, "/devices/pair", tokens["bob"],
             {"device_id": device_id, "pairing_code": code2})
    check("12. re-pair by second user -> 200 PAIRED",
          r.status_code == 200 and r2.status_code == 200
          and (r2.json() or {}).get("device", {}).get("status") == "PAIRED",
          f"request={r.status_code} pair={r2.status_code}")
    cleanup.append((user_devices_table,
                    {"user_id": {"S": user_ids["bob"]},
                     "device_id": {"S": device_id}}))

    # ---- 13. expired code -> 410 ----------------------------------------
    api("DELETE", base, f"/devices/{device_id}", tokens["bob"])  # free it
    r = api("POST", base, "/devices/pair-request", tokens["bob"],
            {"device_id": device_id})
    code3 = (r.json() or {}).get("pairing_code", "") if r.status_code == 200 else ""
    # Force the expiry into the past instead of sleeping 5 minutes.
    ddb.update_item(TableName=devices_table,
                    Key={"device_id": {"S": device_id}},
                    UpdateExpression="SET pairing_expires_at = :past",
                    ExpressionAttributeValues={":past": {"N": "1"}})
    r2 = api("POST", base, "/devices/pair", tokens["bob"],
             {"device_id": device_id, "pairing_code": code3})
    check("13. expired pairing code -> 410", r2.status_code == 410,
          f"got {r2.status_code}")

    # ---- cleanup ---------------------------------------------------------
    for tbl, key in cleanup:
        try:
            ddb.delete_item(TableName=tbl, Key=key)
        except Exception:
            pass

    print("-" * 60)
    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print(f"RESULT: {passed}/{total} passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
