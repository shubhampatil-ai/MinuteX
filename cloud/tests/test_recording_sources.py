#!/usr/bin/env python3
# =============================================================
# test_recording_sources.py — MOBILE / UPLOAD recording sources.
#
# Two layers:
#
# UNIT (offline, always runs — no AWS calls, fakes for DynamoDB/S3):
#   1.  upload-request MOBILE -> key recordings/{uid}/mobile/{id}.m4a,
#       stub row: source=MOBILE, device_id null, status "uploading".
#   2.  upload-request UPLOAD -> key under uploads/, wav default format.
#   3.  upload-request source=DEVICE -> 400 (device flow is getUploadUrl).
#   4.  unsupported format -> 400.
#   5.  oversize file -> 413.   6. over-long duration -> 400.
#   7.  upload-complete flips uploading -> uploaded (+duration).
#   8.  upload-complete never moves a status backwards (pipeline won).
#   9.  upload-complete on someone else's / unknown key -> 404.
#   10. _source_from_key: device, mobile, uploads, legacy layouts.
#   11. _with_source: derives source for legacy rows, nulls device_id
#       for non-device sources.
#   12. reserved device ids ("mobile"/"uploads") are rejected.
#   13. transcribe Lambda parseKey (runs only if `node` is on PATH):
#       source/deviceId/recordingId for all three layouts + formats.
#
# LIVE (real AWS, same style as run_tests.py — runs only when API_URL
# is configured and --live is passed):
#   14. signup ephemeral user -> upload-request MOBILE -> PUT sample.wav
#       -> upload-complete -> row appears in GET /recordings with
#       source MOBILE and no device_id. Ephemeral data cleaned up.
#
# Requires for LIVE: API_URL, BUCKET_NAME, AWS_REGION (creds from ~/.aws).
# =============================================================
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import uuid
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_tests import check, load_dotenv, _results  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------
# Import the userApi module (hyphenated dir -> spec import), with a
# deterministic JWT secret so tokens can be minted locally.
# ---------------------------------------------------------------
os.environ.setdefault("JWT_SECRET", "unit-test-secret")
os.environ.setdefault("BUCKET_NAME", "unit-test-bucket")
os.environ.setdefault("AWS_REGION", "ap-south-1")

# userApi imports the shared AI core FLAT (`import groq_client`), because that
# is how those modules are vendored into its zip — see
# scripts/21_deploy_ai_workspace.sh. Put shared/ on sys.path so the
# spec-import below resolves them the same way the Lambda runtime does.
sys.path.insert(0, str(ROOT / "shared"))

_spec = importlib.util.spec_from_file_location(
    "userapi", ROOT / "functions/userapi" / "lambda_function.py")
userapi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(userapi)

from botocore.exceptions import ClientError  # noqa: E402  (after module load)


# ---------------------------------------------------------------
# Fakes — capture DynamoDB/S3 calls instead of performing them.
# ---------------------------------------------------------------
class FakeRecordings:
    """Just enough of boto3's Table for request_upload/complete_upload."""

    def __init__(self, items=None):
        self.items = dict(items or {})   # audio_s3_key -> item
        self.updates = []                # kwargs of every update_item

    def get_item(self, Key):
        item = self.items.get(Key["audio_s3_key"])
        return {"Item": item} if item else {}

    def update_item(self, **kwargs):
        self.updates.append(kwargs)
        cond = kwargs.get("ConditionExpression", "")
        key = kwargs["Key"]["audio_s3_key"]
        item = self.items.get(key)
        if cond == "#st = :uploading":
            # complete_upload's guard: only flip from "uploading".
            if not item or item.get("status") != userapi.STATUS_UPLOADING:
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException"}},
                    "UpdateItem")
            item["status"] = userapi.STATUS_UPLOADED
        return {}


class FakeS3:
    def generate_presigned_url(self, op, Params=None, ExpiresIn=None):
        return (f"https://unit-test.s3/{Params['Key']}"
                f"?sig=test&ct={Params.get('ContentType', '')}")


def make_event(token, body):
    return {"headers": {"authorization": f"Bearer {token}"},
            "body": json.dumps(body)}


def resp_body(resp):
    return json.loads(resp["body"])


def expect_api_error(fn, event, status, name):
    try:
        fn(event)
        check(name, False, "expected ApiError, got success")
    except userapi.ApiError as e:
        check(name, e.status == status, f"status={e.status} msg={e.message}")


# ---------------------------------------------------------------
# UNIT tests
# ---------------------------------------------------------------
def unit_tests():
    user_id = "user-" + uuid.uuid4().hex[:8]
    token = userapi._mint_for(user_id, "unit@example.com")

    # --- 1. MOBILE request -------------------------------------------------
    fake = FakeRecordings()
    userapi._recordings = fake
    userapi._s3 = FakeS3()

    resp = userapi.request_upload(make_event(token, {
        "source": "MOBILE", "format": "m4a", "duration": 61.5, "title": "Standup"}))
    body = resp_body(resp)
    key = body.get("key", "")
    ok = (resp["statusCode"] == 200
          and key.startswith(f"recordings/{user_id}/mobile/mobile-")
          and key.endswith(".m4a")
          and body.get("upload_url", "").startswith("https://")
          and body.get("content_type") == "audio/mp4"
          and "_" in body.get("recording_id", ""))
    check("upload-request MOBILE -> mobile/ key + presigned URL", ok, key)

    upd = fake.updates[-1]
    vals = upd["ExpressionAttributeValues"]
    # device_id must be ABSENT (not NULL/"") — it's the device-index GSI
    # hash key and DynamoDB rejects NULL/"" for an index key.
    ok = (vals[":src"] == "MOBILE"
          and ":did" not in vals
          and "device_id" not in upd["UpdateExpression"]
          and vals[":uploading"] == "uploading"
          and vals[":uid"] == user_id
          and vals[":title"] == "Standup"
          and vals[":dur"] == Decimal("61.5"))
    check("MOBILE stub row: source/no-device_id/status/title/duration", ok,
          str({k: vals[k] for k in (":src", ":title")}))

    # --- 2. UPLOAD request (wav default) ------------------------------------
    resp = userapi.request_upload(make_event(token, {"source": "upload"}))
    body = resp_body(resp)
    key = body.get("key", "")
    ok = (key.startswith(f"recordings/{user_id}/uploads/upload-")
          and key.endswith(".wav") and body.get("content_type") == "audio/wav")
    check("upload-request UPLOAD -> uploads/ key, wav default", ok, key)

    # --- 2b. any common audio format is accepted -----------------------------
    resp = userapi.request_upload(make_event(token, {"source": "UPLOAD",
                                                     "format": "flac"}))
    body = resp_body(resp)
    ok = (body.get("key", "").endswith(".flac")
          and body.get("content_type") == "audio/flac")
    check("upload-request accepts flac (broad format set)", ok, body.get("key", ""))

    # --- 2c. re-presign reuses the key instead of making a second row --------
    #
    # A presigned PUT expires, so a large file on a slow link asks for another
    # URL mid-upload. Minting a fresh identity there produced a SECOND timeline
    # row and stranded the first at "uploading" forever (reprocess refuses that
    # status), so the user saw a phantom duplicate they could only trash.
    fake = FakeRecordings()
    userapi._recordings = fake
    first = resp_body(userapi.request_upload(make_event(token, {
        "source": "MOBILE", "format": "m4a", "folder_id": ""})))
    fake.items[first["key"]] = {"audio_s3_key": first["key"],
                                "user_id": user_id,
                                "recording_id": first["recording_id"],
                                "status": userapi.STATUS_UPLOADING}
    before = len(fake.updates)
    again = resp_body(userapi.request_upload(make_event(token, {
        "source": "MOBILE", "format": "m4a", "key": first["key"]})))
    ok = (again.get("key") == first["key"]
          and again.get("recording_id") == first["recording_id"]
          and again.get("upload_url", "").startswith("https://")
          and len(fake.updates) == before)   # no second stub row written
    check("re-presign returns the SAME key and writes no second row", ok,
          f"{first['key']} -> {again.get('key')}")

    # A key that already finished uploading is NOT re-signable: the pipeline
    # owns the row from there, and re-signing would let a late PUT overwrite
    # a transcribed recording's audio.
    done_key = first["key"] + ".done"
    fake.items[done_key] = {"audio_s3_key": done_key, "user_id": user_id,
                            "status": userapi.STATUS_UPLOADED}
    body = resp_body(userapi.request_upload(make_event(token, {
        "source": "MOBILE", "format": "m4a", "key": done_key})))
    check("re-presign of an already-uploaded key issues a NEW recording",
          body.get("key") != done_key, body.get("key", ""))

    # Someone else's still-uploading key must never be re-signed — that would
    # hand the caller a write URL onto another user's recording.
    other_key = f"recordings/other-user/mobile/mobile-abc_1.m4a"
    fake.items[other_key] = {"audio_s3_key": other_key,
                             "user_id": "other-user",
                             "status": userapi.STATUS_UPLOADING}
    body = resp_body(userapi.request_upload(make_event(token, {
        "source": "MOBILE", "format": "m4a", "key": other_key})))
    check("re-presign of another user's key issues a NEW recording",
          body.get("key") != other_key and user_id in body.get("key", ""),
          body.get("key", ""))

    # --- 3-6. validation -----------------------------------------------------
    expect_api_error(userapi.request_upload,
                     make_event(token, {"source": "DEVICE"}), 400,
                     "upload-request DEVICE -> 400")
    expect_api_error(userapi.request_upload,
                     make_event(token, {"source": "UPLOAD", "format": "exe"}), 400,
                     "unsupported format -> 400")
    expect_api_error(userapi.request_upload,
                     make_event(token, {"source": "UPLOAD",
                                        "size": userapi.MAX_UPLOAD_BYTES + 1}), 413,
                     "oversize file -> 413")
    expect_api_error(userapi.request_upload,
                     make_event(token, {"source": "MOBILE",
                                        "duration": userapi.MAX_DURATION_SECONDS + 1}),
                     400, "over-long duration -> 400")

    # --- 7. complete: uploading -> uploaded ----------------------------------
    key = f"recordings/{user_id}/mobile/mobile-abc_1700000000.m4a"
    fake = FakeRecordings({key: {"audio_s3_key": key, "user_id": user_id,
                                 "status": "uploading"}})
    userapi._recordings = fake
    resp = userapi.complete_upload(make_event(token, {"key": key, "duration": 62}))
    ok = (resp_body(resp).get("status") == "uploaded"
          and fake.items[key]["status"] == "uploaded")
    check("upload-complete: uploading -> uploaded", ok)

    # --- 8. complete never regresses the status ------------------------------
    key2 = f"recordings/{user_id}/uploads/upload-def_1700000001.wav"
    fake = FakeRecordings({key2: {"audio_s3_key": key2, "user_id": user_id,
                                  "status": "transcribing"}})
    userapi._recordings = fake
    resp = userapi.complete_upload(make_event(token, {"key": key2, "duration": 5}))
    ok = (resp_body(resp).get("status") == "transcribing"
          and fake.items[key2]["status"] == "transcribing")
    check("upload-complete keeps a further-along status", ok,
          f"status stayed {fake.items[key2]['status']}")

    # --- 9. ownership --------------------------------------------------------
    other_key = "recordings/someone-else/mobile/mobile-x_1.m4a"
    userapi._recordings = FakeRecordings({other_key: {
        "audio_s3_key": other_key, "user_id": "someone-else",
        "status": "uploading"}})
    expect_api_error(userapi.complete_upload,
                     make_event(token, {"key": other_key}), 404,
                     "upload-complete on another user's key -> 404")
    expect_api_error(userapi.complete_upload,
                     make_event(token, {"key": "recordings/none/mobile/x.m4a"}),
                     404, "upload-complete on unknown key -> 404")

    # --- 10. _source_from_key ------------------------------------------------
    cases = [
        ("recordings/u1/esp32-001/meeting-1_1700.wav", "DEVICE"),
        ("recordings/u1/mobile/mobile-a_1700.m4a", "MOBILE"),
        ("recordings/u1/uploads/upload-b_1700.mp3", "UPLOAD"),
        ("esp32-001/meeting-1_1700.wav", "DEVICE"),   # legacy layout
    ]
    ok = all(userapi._source_from_key(k) == want for k, want in cases)
    check("_source_from_key: device/mobile/uploads/legacy", ok,
          str([(k.split('/')[-2], userapi._source_from_key(k)) for k, _ in cases]))

    # --- 11. _with_source ----------------------------------------------------
    row = userapi._with_source({"audio_s3_key": cases[1][0],
                                "device_id": "mobile"})
    ok = row["source"] == "MOBILE" and row["device_id"] is None
    check("_with_source derives + nulls device_id for MOBILE", ok, str(row))
    row = userapi._with_source({"audio_s3_key": cases[0][0],
                                "device_id": "esp32-001"})
    ok = row["source"] == "DEVICE" and row["device_id"] == "esp32-001"
    check("_with_source keeps device_id for DEVICE", ok, str(row))

    # --- 12. reserved device ids ----------------------------------------------
    for rid in ("mobile", "uploads"):
        try:
            userapi._get_device(rid)
            check(f"reserved device_id '{rid}' rejected", False, "no error")
        except userapi.ApiError as e:
            check(f"reserved device_id '{rid}' rejected", e.status == 400)

    # --- 13. transcribe Lambda parseKey (needs node) ---------------------------
    # NOTE: this exercises the RETIRED Node transcribeAndSync Lambda, which is
    # no longer deployed (the live function is functions/transcribe, Python).
    # The source now lives in _archive/dead-code/; the check is kept for the
    # key-format contract but skips cleanly when the archive is absent.
    node = shutil.which("node")
    if not node:
        print("[skip] node not on PATH — transcribe parseKey test skipped")
        return
    mjs_path = (ROOT.parent / "_archive" / "dead-code" /
                "lambda-node-transcribeandsync" / "index.mjs")
    if not mjs_path.exists():
        print("[skip] archived Node transcribe source absent — parseKey test skipped")
        return
    mjs = mjs_path.resolve().as_posix()
    script = f"""
import {{ readFileSync }} from "fs";
const src = readFileSync("{mjs}", "utf8");
// Extract the pure helpers (the module's top-level AWS clients need creds,
// so don't import it whole — evaluate just the parser).
const start = src.indexOf("const AUDIO_EXT_RE");
const end = src.indexOf("// -", src.indexOf("function parseKey"));
// eval() in an ES module keeps declarations in its own strict scope —
// new Function() returns them instead.
const parseKey = new Function(src.slice(start, end) + "\\nreturn parseKey;")();
const out = [
  parseKey("recordings/u1/esp32-001/meeting-1_1700.wav"),
  parseKey("recordings/u1/mobile/mobile-a1_1700.m4a"),
  parseKey("recordings/u1/uploads/upload-b2_1700.mp3"),
  parseKey("esp32-001/meeting-1_1700.wav"),
];
console.log(JSON.stringify(out));
"""
    r = subprocess.run([node, "--input-type=module", "-e", script],
                       capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        check("transcribe parseKey (node)", False, r.stderr.strip()[:200])
        return
    parsed = json.loads(r.stdout.strip())
    ok = (parsed[0]["source"] == "DEVICE" and parsed[0]["deviceId"] == "esp32-001"
          and parsed[1]["source"] == "MOBILE" and parsed[1]["deviceId"] == ""
          and parsed[1]["recordingId"] == "mobile-a1_1700"
          and parsed[2]["source"] == "UPLOAD" and parsed[2]["deviceId"] == ""
          and parsed[3]["source"] == "DEVICE" and parsed[3]["deviceId"] == "esp32-001")
    check("transcribe parseKey: source/deviceId for all layouts", ok, r.stdout.strip())


# ---------------------------------------------------------------
# LIVE end-to-end (opt-in: --live, needs API_URL + AWS creds)
# ---------------------------------------------------------------
def live_tests():
    import boto3
    import requests

    base = os.environ.get("API_URL", "").rstrip("/")
    bucket = os.environ.get("BUCKET_NAME", "")
    region = os.environ.get("AWS_REGION", "ap-south-1")
    recordings_table = os.environ.get("RECORDINGS_TABLE", "Recordings")
    users_table = os.environ.get("USERS_TABLE", "Users")
    if not base or not bucket:
        print("[skip] API_URL / BUCKET_NAME not set — live tests skipped")
        return

    def api(method, path, token=None, body=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return requests.request(method, base + path, headers=headers,
                                json=body, timeout=20)

    email = f"src-test-{uuid.uuid4().hex[:10]}@example.com"
    r = api("POST", "/signup", body={"email": email, "password": "test-pass-123"})
    check("live: signup ephemeral user", r.status_code == 201, r.text[:120])
    token = r.json().get("token", "")
    user_id = r.json().get("user_id", "")

    key = ""
    try:
        # upload-request (MOBILE, wav so the sample file matches)
        r = api("POST", "/recordings/upload-request", token,
                {"source": "MOBILE", "format": "wav", "title": "Live source test",
                 "duration": 2})
        ok = r.status_code == 200 and r.json().get("key", "").startswith(
            f"recordings/{user_id}/mobile/")
        check("live: upload-request MOBILE -> presign + mobile/ key", ok, r.text[:200])
        if not ok:
            return
        ticket = r.json()
        key = ticket["key"]

        # negative checks
        r = api("POST", "/recordings/upload-request", token, {"source": "DEVICE"})
        check("live: source DEVICE -> 400", r.status_code == 400, r.text[:120])
        r = api("POST", "/recordings/upload-request", token,
                {"source": "UPLOAD", "format": "exe"})
        check("live: bad format -> 400", r.status_code == 400, r.text[:120])

        # PUT the sample wav to the presigned URL
        wav = (Path(__file__).parent / "sample.wav").read_bytes()
        r = requests.put(ticket["upload_url"], data=wav,
                         headers={"Content-Type": ticket["content_type"]},
                         timeout=60)
        check("live: presigned PUT accepted", r.status_code == 200,
              f"HTTP {r.status_code}")

        r = api("POST", "/recordings/upload-complete", token,
                {"key": key, "duration": 2})
        check("live: upload-complete", r.status_code == 200
              and r.json().get("status") in ("uploaded", "transcribing",
                                             "generating_ai", "complete"),
              r.text[:120])

        r = api("GET", "/recordings", token)
        rows = [x for x in r.json().get("recordings", [])
                if x.get("audio_s3_key") == key]
        ok = (len(rows) == 1 and rows[0].get("source") == "MOBILE"
              and not rows[0].get("device_id"))
        check("live: timeline row carries source=MOBILE, no device", ok,
              str(rows[:1])[:200])
    finally:
        # ---- cleanup: S3 object, Recordings row, ephemeral user ----
        s3 = boto3.client("s3", region_name=region)
        ddb = boto3.resource("dynamodb", region_name=region)
        if key:
            try:
                s3.delete_object(Bucket=bucket, Key=key)
                ddb.Table(recordings_table).delete_item(
                    Key={"audio_s3_key": key})
            except Exception as e:  # noqa: BLE001
                print(f"[cleanup] {e}")
        if user_id:
            try:
                ddb.Table(users_table).delete_item(Key={"user_id": user_id})
            except Exception as e:  # noqa: BLE001
                print(f"[cleanup] {e}")


def main() -> int:
    load_dotenv(ROOT / ".env")
    unit_tests()
    if "--live" in sys.argv:
        live_tests()
    else:
        print("[info] live end-to-end skipped (pass --live to run against AWS)")

    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
