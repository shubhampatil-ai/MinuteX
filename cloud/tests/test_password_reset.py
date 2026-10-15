#!/usr/bin/env python3
# =============================================================
# test_password_reset.py — POST /password/forgot and /password/reset.
#
# OFFLINE: Users is tests/fake_dynamodb.py (real conditional writes and the
# email-index GSI), SES is a MagicMock whose send_email call is where the test
# reads the emailed code — the same place a user would.
#
# WHAT IS AT RISK, and therefore what these tests pin:
#   1. ENUMERATION — forgot answers identically for unknown addresses.
#   2. GUESSING — attempts are capped per code and counted before comparing.
#   3. REPLAY — a code works once, expires, and is replaced by a reissue.
#   4. HONESTY — unconfigured or a failed send says so instead of "sent".
#   5. NO SECRETS AT REST — the raw code is never stored.
#
# Run:  python -m pytest tests/test_password_reset.py -q
# =============================================================
import re
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_dynamodb as fdb  # noqa: E402
from test_workspace_org import api, call, parse  # noqa: E402

EMAIL = "asha@example.com"
USER_ID = "11111111-2222-3333-4444-555555555555"
OLD_PASSWORD = "old-password-1"
NEW_PASSWORD = "new-password-2"


def ev(route, body):
    import json
    return {
        "routeKey": f"POST {route}",
        "requestContext": {"http": {"method": "POST", "path": route}},
        "headers": {},
        "body": json.dumps(body),
    }


class ResetTestCase(unittest.TestCase):
    def setUp(self):
        self.users = fdb.build_tables()["users"]
        salt, h = api._hash_password(OLD_PASSWORD)
        self.users.put_item(Item={"user_id": USER_ID, "email": EMAIL,
                                  "salt": salt, "password_hash": h})
        self.ses = mock.MagicMock()
        patches = [
            mock.patch.object(api, "_users", self.users),
            mock.patch.object(api, "_ses", self.ses),
            mock.patch.object(api, "_jwt_secret", return_value="test-secret"),
            mock.patch.object(api, "PASSWORD_RESET_FROM_EMAIL", "no-reply@minutex.test"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    # -- helpers ---------------------------------------------------------
    def forgot(self, email=EMAIL):
        return parse(call(api.forgot_password, ev("/password/forgot", {"email": email})))

    def reset(self, code, email=EMAIL, new=NEW_PASSWORD):
        return parse(call(api.reset_password, ev("/password/reset", {
            "email": email, "code": code, "new_password": new})))

    def emailed_code(self):
        content = self.ses.send_email.call_args.kwargs["Content"]
        text = content["Simple"]["Body"]["Text"]["Data"]
        return re.search(r"\b(\d{6})\b", text).group(1)

    def row(self):
        return self.users.get_item(Key={"user_id": USER_ID})["Item"]

    def wrong(self, code):
        return f"{(int(code) + 1) % 1_000_000:06d}"

    def allow_resend(self):
        item = self.row()
        item["reset_sent_at"] = 0
        self.users.put_item(Item=item)


class TestForgot(ResetTestCase):
    def test_sends_code_to_account_email(self):
        code, body = self.forgot()
        self.assertEqual(code, 200)
        self.assertTrue(body["sent"])
        kw = self.ses.send_email.call_args.kwargs
        self.assertEqual(kw["Destination"], {"ToAddresses": [EMAIL]})
        self.assertIn("no-reply@minutex.test", kw["FromEmailAddress"])
        self.assertRegex(self.emailed_code(), r"^\d{6}$")

    def test_email_is_case_and_space_insensitive(self):
        self.assertEqual(self.forgot("  Asha@Example.COM ")[0], 200)
        self.ses.send_email.assert_called_once()

    def test_unknown_email_is_indistinguishable(self):
        known = self.forgot()
        unknown = self.forgot("nobody@example.com")
        self.assertEqual(known, unknown)
        self.ses.send_email.assert_called_once()  # only for the real account

    def test_raw_code_is_never_stored(self):
        self.forgot()
        code = self.emailed_code()
        self.assertNotIn(code, repr(self.row()))

    def test_resend_cooldown_does_not_send_or_reveal(self):
        self.forgot()
        again = self.forgot()
        self.assertEqual(again[0], 200)
        self.assertEqual(self.ses.send_email.call_count, 1)

    def test_invalid_email_400(self):
        self.assertEqual(self.forgot("not-an-email")[0], 400)

    def test_unconfigured_is_503_not_fake_success(self):
        with mock.patch.object(api, "PASSWORD_RESET_FROM_EMAIL", ""):
            code, body = self.forgot()
        self.assertEqual(code, 503)
        self.assertEqual(body["code"], api.RESET_ERR_UNAVAILABLE)
        self.ses.send_email.assert_not_called()

    def test_send_failure_is_reported_and_retry_allowed(self):
        self.ses.send_email.side_effect = RuntimeError("ses down")
        code, body = self.forgot()
        self.assertEqual(code, 502)
        self.assertEqual(body["code"], api.RESET_ERR_SEND_FAILED)
        # The cooldown was cleared, so an immediate retry really sends.
        self.ses.send_email.side_effect = None
        self.assertEqual(self.forgot()[0], 200)
        self.assertEqual(self.ses.send_email.call_count, 2)


class TestReset(ResetTestCase):
    def test_happy_path_changes_password_and_signs_in(self):
        self.forgot()
        code, body = self.reset(self.emailed_code())
        self.assertEqual(code, 200)
        self.assertEqual(body["user_id"], USER_ID)
        self.assertEqual(body["email"], EMAIL)
        self.assertEqual(api._jwt_verify(body["token"])["sub"], USER_ID)
        row = self.row()
        self.assertTrue(api._verify_password(NEW_PASSWORD, row["salt"], row["password_hash"]))
        self.assertFalse(api._verify_password(OLD_PASSWORD, row["salt"], row["password_hash"]))
        for attr in ("reset_code_hash", "reset_expires_at", "reset_attempts", "reset_sent_at"):
            self.assertNotIn(attr, row)

    def test_code_is_single_use(self):
        self.forgot()
        c = self.emailed_code()
        self.assertEqual(self.reset(c)[0], 200)
        code, body = self.reset(c, new="another-password-3")
        self.assertEqual(code, 400)
        self.assertEqual(body["code"], api.RESET_ERR_INVALID)

    def test_wrong_code_rejected_and_password_unchanged(self):
        self.forgot()
        code, body = self.reset(self.wrong(self.emailed_code()))
        self.assertEqual(code, 400)
        self.assertEqual(body["code"], api.RESET_ERR_INVALID)
        row = self.row()
        self.assertTrue(api._verify_password(OLD_PASSWORD, row["salt"], row["password_hash"]))

    def test_unknown_account_looks_like_wrong_code(self):
        code, body = self.reset("123456", email="nobody@example.com")
        self.assertEqual((code, body["code"]), (400, api.RESET_ERR_INVALID))

    def test_no_pending_reset_rejected(self):
        code, body = self.reset("123456")
        self.assertEqual((code, body["code"]), (400, api.RESET_ERR_INVALID))

    def test_expired_code_rejected_and_cleared(self):
        self.forgot()
        c = self.emailed_code()
        item = self.row()
        item["reset_expires_at"] = int(time.time()) - 1
        self.users.put_item(Item=item)
        self.assertEqual(self.reset(c)[0], 400)
        self.assertNotIn("reset_code_hash", self.row())

    def test_attempts_capped_then_even_right_code_fails(self):
        self.forgot()
        c = self.emailed_code()
        for _ in range(api.PASSWORD_RESET_MAX_ATTEMPTS):
            self.assertEqual(self.reset(self.wrong(c))[0], 400)
        code, body = self.reset(c)
        self.assertEqual((code, body["code"]), (429, api.RESET_ERR_LOCKED))
        self.assertNotIn("reset_code_hash", self.row())

    def test_last_allowed_attempt_still_works(self):
        self.forgot()
        c = self.emailed_code()
        for _ in range(api.PASSWORD_RESET_MAX_ATTEMPTS - 1):
            self.reset(self.wrong(c))
        self.assertEqual(self.reset(c)[0], 200)

    def test_reissue_replaces_old_code(self):
        self.forgot()
        first = self.emailed_code()
        self.allow_resend()
        self.forgot()
        second = self.emailed_code()
        if first == second:  # 1-in-a-million; nothing to distinguish
            self.skipTest("identical codes issued")
        self.assertEqual(self.reset(first)[0], 400)
        self.assertEqual(self.reset(second)[0], 200)

    def test_short_password_rejected_before_code_is_spent(self):
        self.forgot()
        code, _ = self.reset(self.emailed_code(), new="short")
        self.assertEqual(code, 400)
        self.assertEqual(int(self.row()["reset_attempts"]), 0)

    def test_malformed_code_400(self):
        self.forgot()
        self.assertEqual(self.reset("12ab56")[0], 400)

    def test_old_password_login_fails_new_succeeds(self):
        self.forgot()
        self.reset(self.emailed_code())
        login = lambda pw: parse(call(api.login, ev("/login", {"email": EMAIL, "password": pw})))[0]
        self.assertEqual(login(OLD_PASSWORD), 401)
        self.assertEqual(login(NEW_PASSWORD), 200)


class TestTestMode(ResetTestCase):
    """PASSWORD_RESET_DEV_EMAILS — the code comes back in the response."""

    def dev(self, *entries, sender=""):
        stack = [mock.patch.object(api, "PASSWORD_RESET_DEV_EMAILS", tuple(entries)),
                 mock.patch.object(api, "PASSWORD_RESET_FROM_EMAIL", sender)]
        for p in stack:
            p.start()
            self.addCleanup(p.stop)

    def test_allowlisted_address_gets_code_in_response_and_no_email(self):
        self.dev(EMAIL)
        code, body = self.forgot()
        self.assertEqual(code, 200)
        self.assertRegex(body["dev_code"], r"^\d{6}$")
        self.ses.send_email.assert_not_called()
        self.assertEqual(self.reset(body["dev_code"])[0], 200)

    def test_domain_wildcard(self):
        self.dev("*@example.com")
        self.assertIn("dev_code", self.forgot()[1])

    def test_unlisted_address_never_gets_a_code(self):
        self.dev("someone-else@example.com", sender="no-reply@minutex.test")
        code, body = self.forgot()
        self.assertEqual(code, 200)
        self.assertNotIn("dev_code", body)
        self.ses.send_email.assert_called_once()  # normal email path

    def test_unlisted_address_without_sender_is_503(self):
        self.dev("someone-else@example.com")
        code, body = self.forgot()
        self.assertEqual((code, body["code"]), (503, api.RESET_ERR_UNAVAILABLE))

    def test_unknown_allowlisted_address_reveals_nothing(self):
        self.dev("*@example.com")
        code, body = self.forgot("nobody@example.com")
        self.assertEqual(code, 200)
        self.assertNotIn("dev_code", body)

    def test_cooldown_still_applies(self):
        self.dev(EMAIL)
        self.forgot()
        self.assertNotIn("dev_code", self.forgot()[1])

    def test_code_is_not_logged(self):
        self.dev(EMAIL)
        with mock.patch("builtins.print") as p:
            body = self.forgot()[1]
        logged = " ".join(str(a) for c in p.call_args_list for a in c.args)
        self.assertNotIn(body["dev_code"], logged)

    def test_overbroad_entries_are_ignored(self):
        """A typo must never turn test mode on for every account."""
        for raw, expected in [("", ()), ("*", ()), ("*@*", ()), ("*@", ()),
                              ("*@*.com", ()), ("nope", ()),
                              ("A@B.com, *@test.io", ("a@b.com", "*@test.io"))]:
            self.assertEqual(api._parse_reset_dev_emails(raw), expected, raw)


class TestRoutes(unittest.TestCase):
    def test_routes_registered(self):
        self.assertIs(api._ROUTES[("POST", "/password/forgot")], api.forgot_password)
        self.assertIs(api._ROUTES[("POST", "/password/reset")], api.reset_password)

    def test_email_body_has_code_and_expiry(self):
        subject, text, html = api._password_reset_email("042917", 15)
        for part in (subject, text, html):
            self.assertIn("042917", part)
        self.assertIn("15 minutes", text)


if __name__ == "__main__":
    unittest.main()
