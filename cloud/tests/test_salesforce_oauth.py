#!/usr/bin/env python3
# =============================================================
# test_salesforce_oauth.py — verify the Salesforce OAuth connect API
# against REAL AWS (same style as test_pairing_api.py).
#
# SCOPE NOTE: the one thing this cannot test unattended is the actual
# authorization-code exchange — that needs a human to log into a real
# Salesforce org in a browser. Everything AROUND the exchange is covered
# here, and the exchange itself is verified manually (see the checklist
# printed at the end of a successful run).
#
#   1.  GET /crm/salesforce/status on a fresh user -> {connected: false}.
#   2.  GET /crm/salesforce/status without a JWT -> 401.
#   3.  GET /crm/salesforce/connect -> 200 + an authorize_url pointing at
#       the configured login host, carrying client_id/redirect_uri/state.
#   4.  The `state` param is per-user and tamper-evident: a state minted
#       for alice, mutated by one character, is rejected by /callback.
#   5.  GET /crm/salesforce/callback with no params -> 302 connected=0.
#   6.  GET /crm/salesforce/callback?error=access_denied -> 302 connected=0
#       reason=denied (the user-pressed-Deny path).
#   7.  GET /crm/salesforce/callback with a VALID state but a bogus code
#       -> 302 connected=0 reason=exchange_failed (Salesforce rejects the
#       code; proves the state check passed and the exchange was attempted).
#   8.  Callback with an EXPIRED state -> 302 connected=0 (state TTL works;
#       expiry is forced by minting a state with a past exp using the same
#       JWT secret the Lambda signs with).
#   9.  status/disconnect against a hand-seeded CrmConnections row:
#       status -> connected:true with instance_url + sf_username,
#       DELETE /crm/salesforce -> {disconnected: true}, and the row is
#       actually gone from DynamoDB afterwards.
#   10. One user's connection is invisible to another user (per-user
#       isolation — bob's status stays false while alice is connected).
#   11. The configuration routes all require a connection first (400) and a
#       JWT (401) — they cannot be probed by an unconnected caller. The
#       schema-discovery paths themselves need a REAL org (they call
#       Salesforce's Describe API), so their happy path is in the manual
#       checklist; the scoring/filtering logic they depend on is covered
#       offline by tests/test_ai_workspace.py TestCrmFieldSuggestions.
#
# Creates ephemeral users (random emails); all rows are deleted afterwards.
#
# Requires (from .env / environment):
#   API_URL, AWS_REGION  (+ table names if not the defaults)
#   JWT_SECRET or JWT_SECRET_ARN — only for test 8 (forging an expired
#   state); that single check is skipped if neither is reachable.
# =============================================================
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import uuid
from pathlib import Path

import boto3
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_tests import check, load_dotenv, _results  # noqa: E402


def api(method: str, base: str, path: str, token: str = None, body=None,
        allow_redirects: bool = True):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.request(method, base + path, headers=headers, json=body,
                            timeout=20, allow_redirects=allow_redirects)


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _resolve_jwt_secret(region: str, lambda_name: str):
    """Same resolution order the Lambda uses, so the test can forge a state
    with a past expiry instead of waiting out SALESFORCE_STATE_TTL."""
    env_secret = os.environ.get("JWT_SECRET")
    if env_secret:
        return env_secret
    arn = os.environ.get("JWT_SECRET_ARN")
    if not arn:
        # Fall back to reading it off the deployed function's config.
        try:
            cfg = boto3.client("lambda", region_name=region).get_function_configuration(
                FunctionName=lambda_name)
            arn = (cfg.get("Environment", {}).get("Variables", {}) or {}).get("JWT_SECRET_ARN")
        except Exception:
            return None
    if not arn:
        return None
    try:
        sm = boto3.client("secretsmanager", region_name=region)
        return sm.get_secret_value(SecretId=arn)["SecretString"]
    except Exception:
        return None


def _forge_state(secret: str, user_id: str, exp: int) -> str:
    payload = {"sub": user_id, "exp": exp}
    seg = _b64u_encode(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(secret.encode(), seg.encode(), hashlib.sha256).digest()
    return seg + "." + _b64u_encode(sig)


def _query_of(location: str) -> dict:
    from urllib.parse import urlparse, parse_qs
    return {k: v[0] for k, v in parse_qs(urlparse(location).query).items()}


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env")

    base = os.environ.get("API_URL", "").rstrip("/")
    region = os.environ.get("AWS_REGION", "ap-south-1")
    users_table = os.environ.get("USERS_TABLE", "Users")
    crm_table = os.environ.get("CRM_CONNECTIONS_TABLE", "CrmConnections")
    userapi_name = os.environ.get("USERAPI_LAMBDA_NAME", "userApi")
    login_url = os.environ.get("SALESFORCE_LOGIN_URL", "https://login.salesforce.com")
    if not base:
        print("ERROR: API_URL not set")
        return 2

    ddb = boto3.client("dynamodb", region_name=region)
    run_tag = uuid.uuid4().hex[:8]
    cleanup = []  # (table, key) pairs deleted at the end

    # ---- setup: two ephemeral users ------------------------------------
    tokens, user_ids = {}, {}
    for who in ("alice", "bob"):
        r = api("POST", base, "/signup",
                body={"email": f"{who}-{run_tag}@sftest.local",
                      "password": "sftest-password", "name": who})
        if r.status_code != 201:
            print(f"ERROR: signup {who} failed: {r.status_code} {r.text}")
            return 2
        data = r.json()
        tokens[who], user_ids[who] = data["token"], data["user_id"]
        cleanup.append((users_table, {"user_id": {"S": data["user_id"]}}))
    print(f"Users: alice, bob   API: {base}   login host: {login_url}")
    print("-" * 60)

    # ---- 1. fresh user -> not connected --------------------------------
    r = api("GET", base, "/crm/salesforce/status", tokens["alice"])
    body = r.json() if r.status_code == 200 else {}
    check("1. status on fresh user -> connected:false",
          r.status_code == 200 and body.get("connected") is False,
          f"status={r.status_code} body={r.text[:120]}")

    # ---- 2. status without a JWT -> 401 --------------------------------
    r = api("GET", base, "/crm/salesforce/status")
    check("2. status without JWT -> 401", r.status_code == 401,
          f"got {r.status_code}")

    # ---- 3. connect -> authorize_url ------------------------------------
    r = api("GET", base, "/crm/salesforce/connect", tokens["alice"])
    body = r.json() if r.status_code == 200 else {}
    url = body.get("authorize_url", "")
    q = _query_of(url)
    alice_state = q.get("state", "")
    url_ok = (r.status_code == 200
              and url.startswith(f"{login_url}/services/oauth2/authorize")
              and q.get("response_type") == "code"
              and bool(q.get("client_id"))
              and bool(q.get("redirect_uri"))
              and bool(alice_state))
    check("3. connect -> 200 + well-formed authorize_url", url_ok,
          f"status={r.status_code} url={url[:160]}")

    # ---- 3b. PKCE: Salesforce REQUIRES a code challenge ------------------
    # Without these two params authorize fails with
    #   error=invalid_request&error_description=missing required code challenge
    challenge = q.get("code_challenge", "")
    check("3b. authorize_url carries code_challenge + method S256",
          bool(challenge) and len(challenge) == 43
          and q.get("code_challenge_method") == "S256"
          and not set("+/=") & set(challenge),
          f"challenge={challenge[:20]}... method={q.get('code_challenge_method')}")

    # The verifier must never travel as its own parameter — only the challenge.
    check("3c. code_verifier is NOT in the authorize URL",
          "code_verifier" not in q, str(list(q))[:120])

    # ---- 3d. every attempt is a fresh PKCE transaction -------------------
    r2 = api("GET", base, "/crm/salesforce/connect", tokens["alice"])
    q2 = _query_of(r2.json().get("authorize_url", "") if r2.status_code == 200 else "")
    check("3d. a second connect mints a different challenge + state",
          bool(q2.get("code_challenge"))
          and q2.get("code_challenge") != challenge
          and q2.get("state") != alice_state,
          "challenge/state reused between attempts!")

    # ---- 4. tampered state -> rejected ----------------------------------
    if alice_state:
        # Flip one character of the signature segment.
        seg, _, sig = alice_state.rpartition(".")
        flipped = "A" if sig[:1] != "A" else "B"
        tampered = f"{seg}.{flipped}{sig[1:]}"
        r = api("GET", base,
                f"/crm/salesforce/callback?code=bogus&state={tampered}",
                allow_redirects=False)
        loc = r.headers.get("Location", "")
        check("4. tampered state -> 302 connected=0",
              r.status_code == 302 and _query_of(loc).get("connected") == "0",
              f"status={r.status_code} location={loc[:120]}")
    else:
        check("4. tampered state -> 302 connected=0", False,
              "skipped: no state minted in test 3")

    # ---- 5. callback with no params -------------------------------------
    r = api("GET", base, "/crm/salesforce/callback", allow_redirects=False)
    loc = r.headers.get("Location", "")
    qq = _query_of(loc)
    check("5. callback with no params -> 302 connected=0 missing_params",
          r.status_code == 302 and qq.get("connected") == "0"
          and qq.get("reason") == "missing_params",
          f"status={r.status_code} location={loc[:120]}")

    # ---- 6. user pressed Deny on Salesforce ------------------------------
    r = api("GET", base, "/crm/salesforce/callback?error=access_denied",
            allow_redirects=False)
    loc = r.headers.get("Location", "")
    qq = _query_of(loc)
    check("6. callback?error=access_denied -> 302 connected=0 denied",
          r.status_code == 302 and qq.get("connected") == "0"
          and qq.get("reason") == "denied",
          f"status={r.status_code} location={loc[:120]}")

    # ---- 7. valid state, bogus code -> exchange_failed -------------------
    # Proves the state check passed and a real exchange was attempted
    # (Salesforce rejects the fake code with invalid_grant).
    if alice_state:
        r = api("GET", base,
                f"/crm/salesforce/callback?code=bogus-{run_tag}&state={alice_state}",
                allow_redirects=False)
        loc = r.headers.get("Location", "")
        qq = _query_of(loc)
        check("7. valid state + bogus code -> 302 exchange_failed",
              r.status_code == 302 and qq.get("connected") == "0"
              and qq.get("reason") == "exchange_failed",
              f"status={r.status_code} location={loc[:120]}")
    else:
        check("7. valid state + bogus code -> 302 exchange_failed", False,
              "skipped: no state minted in test 3")

    # ---- 8. expired state -> rejected ------------------------------------
    secret = _resolve_jwt_secret(region, userapi_name)
    if secret:
        expired = _forge_state(secret, user_ids["alice"], int(time.time()) - 10)
        r = api("GET", base, f"/crm/salesforce/callback?code=bogus&state={expired}",
                allow_redirects=False)
        loc = r.headers.get("Location", "")
        check("8. expired state -> 302 connected=0",
              r.status_code == 302 and _query_of(loc).get("connected") == "0",
              f"status={r.status_code} location={loc[:120]}")
    else:
        print("[SKIP] 8. expired state — JWT secret not reachable locally")

    # ---- 9. seeded connection: status + disconnect ------------------------
    # Stands in for a completed OAuth exchange. refresh_token_enc is a
    # deliberately INVALID ciphertext: disconnect must still succeed (the
    # Salesforce revoke call is best-effort and must never block).
    crm_key = {"user_id": {"S": user_ids["alice"]}, "provider": {"S": "salesforce"}}
    ddb.put_item(TableName=crm_table, Item={
        **crm_key,
        "instance_url": {"S": "https://sftest.my.salesforce.com"},
        "refresh_token_enc": {"S": "not-a-real-kms-ciphertext"},
        "org_id": {"S": "00Dtest000000000"},
        "sf_user_id": {"S": "005test000000000"},
        "sf_username": {"S": f"alice-{run_tag}@sftest.local"},
        "connected_at": {"S": "2026-08-11T00:00:00Z"},
        "updated_at": {"S": "2026-08-11T00:00:00Z"},
    })
    cleanup.append((crm_table, crm_key))

    r = api("GET", base, "/crm/salesforce/status", tokens["alice"])
    body = r.json() if r.status_code == 200 else {}
    check("9a. status with a connection -> connected:true + org info",
          r.status_code == 200 and body.get("connected") is True
          and body.get("instance_url") == "https://sftest.my.salesforce.com"
          and body.get("sf_username") == f"alice-{run_tag}@sftest.local",
          f"status={r.status_code} body={r.text[:160]}")

    # ---- 10. per-user isolation (before disconnecting alice) --------------
    r = api("GET", base, "/crm/salesforce/status", tokens["bob"])
    body = r.json() if r.status_code == 200 else {}
    check("10. another user's status is unaffected -> connected:false",
          r.status_code == 200 and body.get("connected") is False,
          f"status={r.status_code} body={r.text[:120]}")

    r = api("DELETE", base, "/crm/salesforce", tokens["alice"])
    disconnect_ok = r.status_code == 200 and (r.json() or {}).get("disconnected") is True
    row = ddb.get_item(TableName=crm_table, Key=crm_key).get("Item")
    check("9b. disconnect -> 200 and the row is deleted",
          disconnect_ok and row is None,
          f"status={r.status_code} row_present={row is not None}")

    r = api("GET", base, "/crm/salesforce/status", tokens["alice"])
    check("9c. status after disconnect -> connected:false",
          r.status_code == 200 and (r.json() or {}).get("connected") is False,
          f"status={r.status_code} body={r.text[:120]}")

    # ---- 11. config routes are gated on connection + auth -----------------
    # Alice is disconnected again at this point, so every config route must
    # report "not connected" rather than leaking an empty/default mapping.
    config_routes = [
        ("GET", "/crm/salesforce/objects"),
        ("GET", "/crm/salesforce/fields/Account"),
        ("GET", "/crm/salesforce/config"),
    ]
    gated_ok = True
    for method, path in config_routes:
        r = api(method, base, path, tokens["alice"])
        if r.status_code != 400:
            gated_ok = False
            print(f"      {method} {path} -> {r.status_code} {r.text[:80]}")
    r = api("PUT", base, "/crm/salesforce/config", tokens["alice"],
            {"object": "Account", "site_visit_number_field": "Name"})
    if r.status_code != 400:
        gated_ok = False
        print(f"      PUT /crm/salesforce/config -> {r.status_code} {r.text[:80]}")
    check("11a. config routes without a connection -> 400", gated_ok)

    unauth_ok = True
    for method, path in config_routes + [("PUT", "/crm/salesforce/config")]:
        r = api(method, base, path, None,
                {"object": "Account", "site_visit_number_field": "Name"}
                if method == "PUT" else None)
        if r.status_code != 401:
            unauth_ok = False
            print(f"      {method} {path} -> {r.status_code} {r.text[:80]}")
    check("11b. config routes without a JWT -> 401", unauth_ok)

    # ---- cleanup -----------------------------------------------------------
    for tbl, key in cleanup:
        try:
            ddb.delete_item(TableName=tbl, Key=key)
        except Exception:
            pass

    print("-" * 60)
    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print(f"RESULT: {passed}/{total} passed")
    if passed == total:
        print()
        print("Remaining MANUAL checks (need a real Salesforce login):")
        print("  1. GET /crm/salesforce/connect with a real JWT, open authorize_url")
        print("     in a browser, log into the org and approve.")
        print("  2. Confirm the browser lands on SALESFORCE_RETURN_URL?connected=1")
        print("  3. GET /crm/salesforce/status -> connected:true with YOUR org's")
        print("     instance_url + username.")
        print("  4. Confirm CrmConnections.refresh_token_enc is ciphertext, not a")
        print("     readable token.")
        print("  5. GET /crm/salesforce/objects -> your org's real objects, and")
        print("     `suggested` naming your site-visit object if you have one.")
        print("  6. GET /crm/salesforce/fields/<that object> -> number_fields and")
        print("     long_text_fields, with `suggested` pre-filled.")
        print("  7. PUT /crm/salesforce/config with those names -> configured:true;")
        print("     then PUT a bogus field name and confirm a 400 that NAMES the")
        print("     field (validation is against live Describe, not a guess).")
        print("  8. In the app: Settings -> Salesforce -> Salesforce mapping.")
        print("     Confirm the pickers list YOUR org's objects/fields — no")
        print("     object or field name is hardcoded anywhere in the product.")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
