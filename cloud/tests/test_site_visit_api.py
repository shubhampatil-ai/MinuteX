#!/usr/bin/env python3
# =============================================================
# test_site_visit_api.py — verify the Site Visit Number API against
# REAL AWS (same style as test_pairing_api.py).
#
# Covers the manual-entry/correction path (plan Phase 4) and the read path
# the meeting UI depends on. The AI EXTRACTION itself is not exercised here —
# that needs real audio saying a real number through the S3 pipeline; the
# extraction's own logic (grounding, confidence, malformed replies) is
# covered by pure-function tests against ai_schema.coerce_site_visit, which
# need no AWS at all. See the manual checklist printed on success.
#
#   1.  A fresh recording has no site_visit.
#   2.  PATCH {site_visit_number} -> stored, source=manual, confidence=manual.
#   3.  GET returns it (this is what the meeting screen reads).
#   4.  PATCH a different number -> overwrites (correcting a wrong value).
#   5.  PATCH over an AI-extracted value -> manual wins.
#   6.  PATCH {site_visit_number: null} -> cleared (undo a bad extraction).
#   7.  PATCH {site_visit_number: ""} -> also cleared.
#   8.  An over-long number is truncated, not rejected outright.
#   9.  PATCH {title} alone leaves site_visit untouched (no accidental wipe).
#   10. PATCH {} -> 400 naming the accepted fields.
#   11. Another user PATCHing -> 404, and the value is unchanged.
#   12. No JWT -> 401.
#
# Creates an ephemeral user + an ephemeral Recordings row; both are deleted
# afterwards. Nothing is uploaded to S3 and no AI runs, so this is fast and
# costs nothing.
#
# Requires (from .env / environment):
#   API_URL, AWS_REGION  (+ table names if not the defaults)
# =============================================================
import os
import sys
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
                            json=body, timeout=20)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")

    base = os.environ.get("API_URL", "").rstrip("/")
    region = os.environ.get("AWS_REGION", "ap-south-1")
    users_table = os.environ.get("USERS_TABLE", "Users")
    recordings_table = os.environ.get("RECORDINGS_TABLE", "Recordings")
    if not base:
        print("ERROR: API_URL not set")
        return 2

    ddb = boto3.client("dynamodb", region_name=region)
    run_tag = uuid.uuid4().hex[:8]
    cleanup = []

    # ---- setup: one ephemeral user + two ephemeral recordings -------------
    tokens, user_ids = {}, {}
    for who in ("alice", "bob"):
        r = api("POST", base, "/signup",
                body={"email": f"{who}-{run_tag}@svtest.local",
                      "password": "svtest-password", "name": who})
        if r.status_code != 201:
            print(f"ERROR: signup {who} failed: {r.status_code} {r.text}")
            return 2
        data = r.json()
        tokens[who], user_ids[who] = data["token"], data["user_id"]
        cleanup.append((users_table, {"user_id": {"S": data["user_id"]}}))

    key = f"recordings/{user_ids['alice']}/uploads/svtest-{run_tag}.m4a"
    ddb.put_item(TableName=recordings_table, Item={
        "audio_s3_key": {"S": key},
        "user_id": {"S": user_ids["alice"]},
        "source": {"S": "UPLOAD"},
        "status": {"S": "complete"},
        "title": {"S": f"Site visit test {run_tag}"},
        "transcript": {"S": "Speaker 1: hello."},
        "summary": {"S": "A test recording."},
    })
    cleanup.append((recordings_table, {"audio_s3_key": {"S": key}}))
    enc_key = requests.utils.quote(key, safe="")
    print(f"Recording: {key}   API: {base}")
    print("-" * 60)

    def get_rec(token):
        return api("GET", base, f"/recordings/{enc_key}", token)

    def patch_rec(body, token):
        return api("PATCH", base, f"/recordings/{enc_key}", token, body)

    # ---- 1. no site_visit initially --------------------------------------
    r = get_rec(tokens["alice"])
    rec = (r.json() or {}).get("recording", {}) if r.status_code == 200 else {}
    check("1. fresh recording has no site_visit",
          r.status_code == 200 and not rec.get("site_visit"),
          f"status={r.status_code} site_visit={rec.get('site_visit')}")

    # ---- 2. manual entry --------------------------------------------------
    r = patch_rec({"site_visit_number": "SV-10245"}, tokens["alice"])
    sv = ((r.json() or {}).get("recording", {}) or {}).get("site_visit") or {}
    check("2. PATCH site_visit_number -> stored as manual",
          r.status_code == 200 and sv.get("site_visit_number") == "SV-10245"
          and sv.get("source") == "manual" and sv.get("confidence") == "manual",
          f"status={r.status_code} site_visit={sv}")

    # ---- 3. GET returns it -------------------------------------------------
    r = get_rec(tokens["alice"])
    sv = ((r.json() or {}).get("recording", {}) or {}).get("site_visit") or {}
    check("3. GET returns site_visit for the meeting screen",
          sv.get("site_visit_number") == "SV-10245", str(sv))

    # ---- 4. correcting it -------------------------------------------------
    r = patch_rec({"site_visit_number": "SV-77777"}, tokens["alice"])
    sv = ((r.json() or {}).get("recording", {}) or {}).get("site_visit") or {}
    check("4. PATCH again -> number corrected",
          sv.get("site_visit_number") == "SV-77777", str(sv))

    # ---- 5. manual beats an AI extraction ---------------------------------
    ddb.update_item(
        TableName=recordings_table, Key={"audio_s3_key": {"S": key}},
        UpdateExpression="SET site_visit = :sv",
        ExpressionAttributeValues={":sv": {"M": {
            "site_visit_number": {"S": "SV-00000"},
            "confidence": {"S": "probable"},
            "confidence_score": {"N": "0.6"},
            "evidence": {"S": "visit SV-00000 maybe"},
        }}})
    r = patch_rec({"site_visit_number": "SV-12345"}, tokens["alice"])
    sv = ((r.json() or {}).get("recording", {}) or {}).get("site_visit") or {}
    check("5. manual entry overrides an AI extraction",
          sv.get("site_visit_number") == "SV-12345"
          and sv.get("source") == "manual", str(sv))

    # ---- 6/7. clearing ----------------------------------------------------
    for label, value in (("null", None), ('""', "")):
        patch_rec({"site_visit_number": "SV-1"}, tokens["alice"])  # ensure set
        r = patch_rec({"site_visit_number": value}, tokens["alice"])
        rec = (r.json() or {}).get("recording", {}) if r.status_code == 200 else {}
        check(f"{'6' if value is None else '7'}. PATCH {label} clears site_visit",
              r.status_code == 200 and not rec.get("site_visit"),
              f"status={r.status_code} site_visit={rec.get('site_visit')}")

    # ---- 8. over-long value is truncated ----------------------------------
    r = patch_rec({"site_visit_number": "S" * 500}, tokens["alice"])
    sv = ((r.json() or {}).get("recording", {}) or {}).get("site_visit") or {}
    check("8. over-long number truncated (not rejected)",
          r.status_code == 200 and 0 < len(sv.get("site_visit_number", "")) <= 100,
          f"len={len(sv.get('site_visit_number', ''))}")

    # ---- 9. title-only PATCH must not touch site_visit --------------------
    patch_rec({"site_visit_number": "SV-KEEP"}, tokens["alice"])
    r = patch_rec({"title": f"Renamed {run_tag}"}, tokens["alice"])
    rec = (r.json() or {}).get("recording", {}) if r.status_code == 200 else {}
    sv = rec.get("site_visit") or {}
    check("9. title-only PATCH leaves site_visit untouched",
          r.status_code == 200 and sv.get("site_visit_number") == "SV-KEEP"
          and rec.get("title") == f"Renamed {run_tag}", str(sv))

    # ---- 10. empty PATCH --------------------------------------------------
    r = patch_rec({}, tokens["alice"])
    err = (r.json() or {}).get("error", "")
    check("10. empty PATCH -> 400 naming site_visit_number",
          r.status_code == 400 and "site_visit_number" in err,
          f"status={r.status_code} error={err!r}")

    # ---- 11. another user cannot change it --------------------------------
    r = patch_rec({"site_visit_number": "SV-EVIL"}, tokens["bob"])
    after = get_rec(tokens["alice"])
    sv = ((after.json() or {}).get("recording", {}) or {}).get("site_visit") or {}
    check("11. non-owner PATCH -> 404 and value unchanged",
          r.status_code == 404 and sv.get("site_visit_number") == "SV-KEEP",
          f"status={r.status_code} site_visit={sv}")

    # ---- 12. unauthenticated ---------------------------------------------
    r = api("PATCH", base, f"/recordings/{enc_key}", None,
            {"site_visit_number": "SV-NOPE"})
    check("12. PATCH without JWT -> 401", r.status_code == 401,
          f"got {r.status_code}")

    # ---- cleanup ----------------------------------------------------------
    for tbl, k in cleanup:
        try:
            ddb.delete_item(TableName=tbl, Key=k)
        except Exception:
            pass

    print("-" * 60)
    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print(f"RESULT: {passed}/{total} passed")
    if passed == total:
        print()
        print("Remaining MANUAL check (needs real audio through the pipeline):")
        print("  1. Record/upload a meeting whose audio SAYS a site visit number")
        print('     (e.g. "today\'s site visit number is SV-10245").')
        print("  2. After processing, inspect the row:")
        print("       python tests/query_recording.py <key>")
        print("     Expect site_visit.site_visit_number == the spoken number,")
        print('     confidence "explicit", and evidence quoting the sentence.')
        print("  3. Upload a meeting that mentions NO site visit number and")
        print("     confirm site_visit is ABSENT (the common, correct case) —")
        print("     not a guessed number.")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
