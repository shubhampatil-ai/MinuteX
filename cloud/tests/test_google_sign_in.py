#!/usr/bin/env python3
# =============================================================
# test_google_sign_in.py — POST /auth/google ("Continue with Google").
#
# OFFLINE: Users is tests/fake_dynamodb.py; Google's tokeninfo call is
# replaced at _fetch_google_tokeninfo, so every test controls exactly what
# "Google said" and the verification logic around it is real.
#
# WHAT IS AT RISK, and therefore what these tests pin:
#   1. TOKEN TRUST — a token for another app, expired, or with an unverified
#      email never signs anyone in.
#   2. ONE ACCOUNT PER PERSON — first sign-in creates; later ones (by `sub`)
#      return the same account; an existing email account is LINKED, not
#      duplicated.
#   3. PRE-ACCOUNT TAKEOVER — linking disables an unproven password, keeps a
#      proven one.
#   4. PASSWORDLESS ACCOUNTS — login, change-password and reset behave.
#
# Run:  python -m pytest tests/test_google_sign_in.py -q
# =============================================================
import json
import re
import sys
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_dynamodb as fdb  # noqa: E402
from test_workspace_org import api, call, parse  # noqa: E402

CLIENT = "web-client.apps.googleusercontent.com"
SUB = "109876543210"
EMAIL = "asha@example.com"


def ev(route, body, auth=False):
    e = {
        "routeKey": f"POST {route}",
        "requestContext": {"http": {"method": "POST", "path": route}},
        "headers": {"authorization": "Bearer t"} if auth else {},
        "body": json.dumps(body),
    }
    return e


def tokeninfo(**over):
    info = {"aud": CLIENT, "iss": "https://accounts.google.com", "sub": SUB,
            "email": EMAIL, "email_verified": "true", "name": "Asha Rao",
            "exp": str(int(time.time()) + 3600)}
    info.update(over)
    return info


class GoogleTestCase(unittest.TestCase):
    def setUp(self):
        self.users = fdb.build_tables()["users"]
        self.info = tokeninfo()
        self.ses = mock.MagicMock()
        patches = [
            mock.patch.object(api, "_users", self.users),
            mock.patch.object(api, "_ses", self.ses),
            mock.patch.object(api, "_jwt_secret", return_value="test-secret"),
            mock.patch.object(api, "GOOGLE_SIGNIN_CLIENT_IDS", frozenset({CLIENT})),
            mock.patch.object(api, "PASSWORD_RESET_FROM_EMAIL", "no-reply@minutex.test"),
            mock.patch.object(api, "_fetch_google_tokeninfo",
                              side_effect=lambda token: dict(self.info)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    # -- helpers ---------------------------------------------------------
    def google(self, token="google-id-token"):
        return parse(call(api.google_sign_in, ev("/auth/google", {"id_token": token})))

    def signup(self, email=EMAIL, password="password-123"):
        return parse(call(api.signup, ev("/signup", {"email": email, "password": password})))

    def login(self, password, email=EMAIL):
        return parse(call(api.login, ev("/login", {"email": email, "password": password})))

    def user(self, user_id):
        return self.users.get_item(Key={"user_id": user_id}).get("Item")

    def accounts(self):
        return [i for i in self.users.items.values() if not api._is_user_claim(i)]


class TestNewAccount(GoogleTestCase):
    def test_first_sign_in_creates_account(self):
        code, body = self.google()
        self.assertEqual(code, 201)
        self.assertTrue(body["created"])
        self.assertEqual(body["email"], EMAIL)
        self.assertEqual(body["name"], "Asha Rao")
        self.assertEqual(api._jwt_verify(body["token"])["sub"], body["user_id"])
        row = self.user(body["user_id"])
        self.assertEqual(row["google_sub"], SUB)
        self.assertNotIn("password_hash", row)
        self.assertIn("email_verified_at", row)
        self.assertEqual(len(self.accounts()), 1)

    def test_second_sign_in_returns_same_account(self):
        first = self.google()[1]
        code, again = self.google()
        self.assertEqual(code, 200)
        self.assertFalse(again["created"])
        self.assertEqual(again["user_id"], first["user_id"])
        self.assertEqual(len(self.accounts()), 1)

    def test_identity_is_sub_not_email(self):
        first = self.google()[1]
        self.info = tokeninfo(email="asha.new@example.com")
        self.assertEqual(self.google()[1]["user_id"], first["user_id"])

    def test_email_is_claimed_so_password_signup_is_refused(self):
        self.google()
        self.assertEqual(self.signup()[0], 409)

    def test_claim_rows_are_not_accounts(self):
        body = self.google()[1]
        self.assertTrue(api._is_user_claim({"user_id": api._google_claim_id(SUB)}))
        self.assertFalse(api._is_user_claim({"user_id": body["user_id"]}))

    def test_stale_claim_for_deleted_account_is_replaced(self):
        old = self.google()[1]["user_id"]
        self.users.delete_item(Key={"user_id": old})
        self.users.delete_item(Key={"user_id": api._user_email_claim(EMAIL)})
        code, body = self.google()
        self.assertEqual(code, 201)
        self.assertNotEqual(body["user_id"], old)


class TestTokenTrust(GoogleTestCase):
    def assert_rejected(self, status, code_str):
        code, body = self.google()
        self.assertEqual((code, body.get("code")), (status, code_str))
        self.assertEqual(self.accounts(), [])

    def test_token_for_another_app(self):
        self.info = tokeninfo(aud="someone-else.apps.googleusercontent.com")
        self.assert_rejected(401, api.GOOGLE_ERR_INVALID)

    def test_wrong_issuer(self):
        self.info = tokeninfo(iss="https://evil.example")
        self.assert_rejected(401, api.GOOGLE_ERR_INVALID)

    def test_expired(self):
        self.info = tokeninfo(exp=str(int(time.time()) - 5))
        self.assert_rejected(401, api.GOOGLE_ERR_INVALID)

    def test_unverified_google_email(self):
        self.info = tokeninfo(email_verified="false")
        self.assert_rejected(403, api.GOOGLE_ERR_EMAIL_UNVERIFIED)

    def test_missing_token_400(self):
        code, _ = parse(call(api.google_sign_in, ev("/auth/google", {})))
        self.assertEqual(code, 400)

    def test_unconfigured_503(self):
        with mock.patch.object(api, "GOOGLE_SIGNIN_CLIENT_IDS", frozenset()):
            self.assert_rejected(503, api.GOOGLE_ERR_UNAVAILABLE)

    def test_google_rejecting_token_is_401(self):
        err = urllib.error.HTTPError("u", 400, "bad", {}, None)
        with mock.patch.object(api.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(api.ApiError) as ctx:
                _REAL_FETCH("x")
        self.assertEqual((ctx.exception.status, ctx.exception.code),
                         (401, api.GOOGLE_ERR_INVALID))

    def test_google_outage_is_502_not_401(self):
        """A Google outage must not read as 'your sign-in was bad'."""
        err = urllib.error.HTTPError("u", 503, "down", {}, None)
        with mock.patch.object(api.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(api.ApiError) as ctx:
                _REAL_FETCH("x")
        self.assertEqual(ctx.exception.status, 502)


# The real fetcher, captured before any test patches the module attribute.
_REAL_FETCH = api._fetch_google_tokeninfo


class TestLinking(GoogleTestCase):
    def test_unverified_password_account_is_linked_and_password_disabled(self):
        uid = self.signup()[1]["user_id"]
        code, body = self.google()
        self.assertEqual(code, 200)
        self.assertEqual(body["user_id"], uid)
        self.assertTrue(body["password_disabled"])
        row = self.user(uid)
        self.assertEqual(row["google_sub"], SUB)
        self.assertNotIn("password_hash", row)
        self.assertEqual(len(self.accounts()), 1)
        code, body = self.login("password-123")
        self.assertEqual((code, body["code"]), (401, api.AUTH_ERR_USE_GOOGLE))

    def test_verified_password_account_keeps_password(self):
        uid = self.signup()[1]["user_id"]
        item = self.user(uid)
        item["email_verified_at"] = "2026-09-01T00:00:00Z"
        self.users.put_item(Item=item)
        body = self.google()[1]
        self.assertFalse(body["password_disabled"])
        self.assertEqual(self.login("password-123")[0], 200)

    def test_link_fills_missing_name_but_never_overwrites(self):
        uid = self.signup()[1]["user_id"]
        self.google()
        self.assertEqual(self.user(uid)["name"], "Asha Rao")

        other = self.signup("bo@example.com")[1]["user_id"]
        item = self.user(other)
        item["name"] = "Bo"
        self.users.put_item(Item=item)
        self.info = tokeninfo(sub="2", email="bo@example.com", name="Robert")
        self.google()
        self.assertEqual(self.user(other)["name"], "Bo")

    def test_email_linked_to_a_different_google_account_is_409(self):
        self.google()
        self.info = tokeninfo(sub="different-sub")
        code, body = self.google()
        self.assertEqual((code, body["code"]), (409, api.GOOGLE_ERR_CONFLICT))

    def test_linked_account_reports_sign_in_methods(self):
        uid = self.google()[1]["user_id"]
        pub = api._public_user(self.user(uid))
        self.assertEqual((pub["has_password"], pub["google_linked"]), (False, True))
        self.assertNotIn("google_sub", pub)


class TestPasswordlessAccount(GoogleTestCase):
    def test_login_with_password_points_to_google(self):
        self.google()
        code, body = self.login("anything-at-all")
        self.assertEqual((code, body["code"]), (401, api.AUTH_ERR_USE_GOOGLE))

    def test_change_password_explains_instead_of_500(self):
        uid = self.google()[1]["user_id"]
        with mock.patch.object(api, "_require_auth", return_value=uid):
            code, body = parse(call(api.change_password, ev("/me/password", {
                "current_password": "", "new_password": "brand-new-pass"}, auth=True)))
        self.assertEqual((code, body["code"]), (400, api.AUTH_ERR_NO_PASSWORD))

    def test_forgot_password_sets_a_first_password(self):
        uid = self.google()[1]["user_id"]
        parse(call(api.forgot_password, ev("/password/forgot", {"email": EMAIL})))
        text = self.ses.send_email.call_args.kwargs["Content"]["Simple"]["Body"]["Text"]["Data"]
        reset_code = re.search(r"\b(\d{6})\b", text).group(1)
        code, _ = parse(call(api.reset_password, ev("/password/reset", {
            "email": EMAIL, "code": reset_code, "new_password": "first-pass-1"})))
        self.assertEqual(code, 200)
        self.assertEqual(self.login("first-pass-1")[0], 200)
        self.assertTrue(api._public_user(self.user(uid))["has_password"])


class TestRoute(unittest.TestCase):
    def test_route_registered(self):
        self.assertIs(api._ROUTES[("POST", "/auth/google")], api.google_sign_in)


if __name__ == "__main__":
    unittest.main()
