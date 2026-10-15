"""test_push_notifications.py — OS push through Expo, end to end offline.

Organised around the ways push goes wrong in production:

  REGISTRATION  one user, many devices; a rotated token must not leave its
                predecessor behind; a token that moves to another account
                stops notifying the old one; nobody can remove or read
                someone else's installation; garbage is rejected.
  PREFERENCE    the Settings switch is the row's `enabled`, and the sender
                honours it — a decorative toggle is the failure this pins.
  CONTENT       a push carries fixed copy and identifiers only — never the
                meeting title, which is on every lock screen.
  DELIVERY      a dead token (DeviceNotRegistered) is deactivated and the
                user's OTHER devices still get the push; a transport failure
                raises nothing.
  RECEIPTS      a late DeviceNotRegistered receipt deactivates the token.
  TRIGGER       only `complete` (the brief exists) sends; `transcribed` and
                `failed` never do; a reprocess or retry never sends twice.

OFFLINE: fake tables, a fake Expo transport. No AWS, no network.

Run:  python -m pytest tests/test_push_notifications.py -q
"""
import importlib.util as _ilu
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "shared", ROOT / "functions/userapi", Path(__file__).resolve().parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from test_ai_workspace import api, parse  # noqa: E402  (installs AWS stubs)

import fake_dynamodb as fdb  # noqa: E402
import notification_schema as ns  # noqa: E402
import push_delivery as pd  # noqa: E402

_spec = _ilu.spec_from_file_location(
    "transcribe_lambda_function_push", str(ROOT / "functions/transcribe/lambda_function.py"))
tr = _ilu.module_from_spec(_spec)
sys.modules["transcribe_lambda_function_push"] = tr
_spec.loader.exec_module(tr)

OWNER, OTHER = "u-1", "u-2"
KEY = "recordings/u-1/mobile/mobile-abc_1754300000.m4a"
TOK_A = "ExponentPushToken[aaaaaaaaaaaaaaaaaaaaaa]"
TOK_B = "ExponentPushToken[bbbbbbbbbbbbbbbbbbbbbb]"
TOK_C = "ExponentPushToken[cccccccccccccccccccccc]"
INST_1 = "install-0001-phone"
INST_2 = "install-0002-tablet"


def token_table():
    return fdb.FakeTable("PushTokens", "push_token",
                         indexes={"user-index": ("user_id", None)})


def call(handler, ev):
    try:
        return handler(ev)
    except api.ApiError as e:
        return api._resp(e.status, {"error": e.message})


def event(method, route, user=OWNER, body=None, params=None):
    ev = {
        "routeKey": f"{method} {route}",
        "requestContext": {"http": {"method": method, "path": "/"}},
        "pathParameters": params or {},
        "headers": {"authorization": f"Bearer {user}"},
    }
    if body is not None:
        ev["body"] = json.dumps(body)
    return ev


class FakeExpo:
    """Records every request; answers with scripted tickets per token."""

    def __init__(self, ticket_for=None, receipts=None, raise_on_send=False):
        self.sent = []          # list of message lists
        self.receipt_calls = []
        self.ticket_for = ticket_for or {}
        self.receipts = receipts or {}
        self.raise_on_send = raise_on_send

    def __call__(self, url, payload):
        if url == pd.EXPO_RECEIPTS_URL:
            self.receipt_calls.append(payload["ids"])
            return {"data": {i: self.receipts[i] for i in payload["ids"] if i in self.receipts}}
        if self.raise_on_send:
            raise OSError("network down")
        self.sent.append(payload)
        data = []
        for m in payload:
            data.append(self.ticket_for.get(m["to"], {"status": "ok", "id": f"rcpt-{m['to'][-4:-1]}"}))
        return {"data": data}


class RegistrationBase(unittest.TestCase):
    def setUp(self):
        self.tokens = token_table()
        self.expo = FakeExpo()
        self.patches = [
            mock.patch.object(api, "_push_tokens", self.tokens),
            mock.patch.object(api, "Key", fdb.Key),
            # The JWT is not the subject here: the bearer value IS the user id.
            mock.patch.object(api, "_require_auth",
                              side_effect=lambda ev: ev["headers"]["authorization"][7:]),
            mock.patch.object(pd, "urllib_transport", return_value=self.expo),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def register(self, token, inst, user=OWNER, platform="android", **extra):
        body = {"token": token, "installation_id": inst, "platform": platform, **extra}
        return parse(call(api.register_push_token,
                          event("POST", "/me/push-tokens", user=user, body=body)))

    def rows(self, user=None):
        return [r for r in self.tokens.items.values() if user is None or r["user_id"] == user]


class RegistrationTests(RegistrationBase):
    def test_register_creates_one_row_per_installation(self):
        status, body = self.register(TOK_A, INST_1, app_version="1.0.0")
        self.assertEqual(status, 200)
        self.assertEqual(body["push_token"]["installation_id"], INST_1)
        self.assertTrue(body["push_token"]["enabled"])
        self.assertNotIn(TOK_A, json.dumps(body), "the token is never echoed back")
        row = self.rows(OWNER)[0]
        for field in ("user_id", "installation_id", "platform", "app_version",
                      "enabled", "created_at", "updated_at", "last_seen_at"):
            self.assertIn(field, row)

    def test_re_registration_is_an_upsert_not_a_duplicate(self):
        self.register(TOK_A, INST_1)
        created = self.rows()[0]["created_at"]
        self.register(TOK_A, INST_1)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["created_at"], created)

    def test_one_user_many_devices(self):
        self.register(TOK_A, INST_1, platform="android")
        self.register(TOK_B, INST_2, platform="ios")
        self.assertEqual(sorted(r["installation_id"] for r in self.rows(OWNER)), [INST_1, INST_2])

    def test_rotated_token_replaces_its_predecessor_on_the_same_installation(self):
        self.register(TOK_A, INST_1)
        self.register(TOK_B, INST_1)
        self.assertEqual([r["push_token"] for r in self.rows(OWNER)], [TOK_B])

    def test_token_moving_to_another_account_leaves_the_old_one(self):
        self.register(TOK_A, INST_1, user=OWNER)
        self.register(TOK_A, INST_1, user=OTHER)
        self.assertEqual(self.rows(OWNER), [])
        self.assertEqual(len(self.rows(OTHER)), 1)

    def test_enabled_is_preserved_when_omitted(self):
        self.register(TOK_A, INST_1, enabled=False)
        _, body = self.register(TOK_A, INST_1)       # an app start: no `enabled`
        self.assertFalse(body["push_token"]["enabled"])
        self.assertFalse(self.rows()[0]["enabled"])

    def test_registration_revives_a_token_expo_had_declared_dead(self):
        self.register(TOK_A, INST_1)
        pd.deactivate(self.tokens, TOK_A, "DeviceNotRegistered", "2026-09-01T00:00:00Z")
        self.assertFalse(pd.is_deliverable(self.rows()[0]))
        self.register(TOK_A, INST_1)
        self.assertTrue(pd.is_deliverable(self.rows()[0]))

    def test_validation(self):
        cases = [
            {"token": "not-a-token", "installation_id": INST_1, "platform": "ios"},
            {"token": TOK_A, "installation_id": "x", "platform": "ios"},
            {"token": TOK_A, "installation_id": INST_1, "platform": "windows"},
            {"token": TOK_A, "installation_id": INST_1, "platform": "ios", "enabled": "yes"},
        ]
        for body in cases:
            status, _ = parse(call(api.register_push_token,
                                   event("POST", "/me/push-tokens", body=body)))
            self.assertEqual(status, 400, body)
        self.assertEqual(self.rows(), [])

    def test_delete_is_scoped_to_the_caller(self):
        self.register(TOK_A, INST_1, user=OWNER)
        status, body = parse(call(api.delete_push_token, event(
            "DELETE", "/me/push-tokens/{installation_id}", user=OTHER,
            params={"installation_id": INST_1})))
        self.assertEqual((status, body["removed"]), (200, 0))
        self.assertEqual(len(self.rows(OWNER)), 1, "a stranger removed nothing")
        status, body = parse(call(api.delete_push_token, event(
            "DELETE", "/me/push-tokens/{installation_id}", user=OWNER,
            params={"installation_id": INST_1})))
        self.assertEqual((status, body["removed"]), (200, 1))
        self.assertEqual(self.rows(), [])

    def test_routes_are_registered(self):
        self.assertIs(api._ROUTES[("POST", "/me/push-tokens")], api.register_push_token)
        self.assertIs(api._ROUTES[("DELETE", "/me/push-tokens/{installation_id}")],
                      api.delete_push_token)


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tokens = token_table()

    def seed(self, token, user=OWNER, inst=INST_1, enabled=True, **extra):
        self.tokens.items[(token,)] = {"push_token": token, "user_id": user,
                                       "installation_id": inst, "platform": "android",
                                       "enabled": enabled, **extra}

    def send(self, expo, user=OWNER):
        return pd.send_to_user(
            self.tokens, user, lambda t: pd.brief_ready_message(t, KEY, "n-1"),
            post=expo, key_cls=fdb.Key, now_iso="2026-09-23T10:00:00Z", now_epoch=1_000_000)

    def test_every_enabled_device_of_the_user_and_nobody_else(self):
        self.seed(TOK_A, inst=INST_1)
        self.seed(TOK_B, inst=INST_2)
        self.seed(TOK_C, user=OTHER, inst="install-other-01")
        expo = FakeExpo()
        out = self.send(expo)
        self.assertEqual(out["accepted"], 2)
        sent_to = sorted(m["to"] for batch in expo.sent for m in batch)
        self.assertEqual(sent_to, sorted([TOK_A, TOK_B]))

    def test_the_preference_controls_delivery(self):
        self.seed(TOK_A, enabled=False)
        expo = FakeExpo()
        out = self.send(expo)
        self.assertEqual(out["devices"], 0)
        self.assertEqual(expo.sent, [])

    def test_payload_is_fixed_copy_and_identifiers_only(self):
        msg = pd.brief_ready_message(TOK_A, KEY, "n-1")
        self.assertEqual(msg["title"], "Brief ready")
        self.assertEqual(msg["body"], "Your meeting brief is ready to review.")
        self.assertEqual(msg["data"], {"type": "brief_ready", "audio_s3_key": KEY,
                                       "notification_id": "n-1"})
        self.assertEqual(msg["channelId"], "briefs")
        self.assertNotIn("title", msg["data"])

    def test_dead_token_is_deactivated_and_other_devices_still_receive(self):
        self.seed(TOK_A, inst=INST_1)
        self.seed(TOK_B, inst=INST_2)
        expo = FakeExpo(ticket_for={TOK_A: {"status": "error", "message": "gone",
                                            "details": {"error": "DeviceNotRegistered"}}})
        out = self.send(expo)
        self.assertEqual((out["accepted"], out["failed"], out["deactivated"]), (1, 1, 1))
        self.assertFalse(pd.is_deliverable(self.tokens.items[(TOK_A,)]))
        self.assertTrue(pd.is_deliverable(self.tokens.items[(TOK_B,)]))
        # And it is not retried: the next send skips it.
        expo2 = FakeExpo()
        self.send(expo2)
        self.assertEqual([m["to"] for b in expo2.sent for m in b], [TOK_B])

    def test_a_transient_error_does_not_deactivate(self):
        self.seed(TOK_A)
        expo = FakeExpo(ticket_for={TOK_A: {"status": "error",
                                            "details": {"error": "MessageRateExceeded"}}})
        self.send(expo)
        self.assertTrue(pd.is_deliverable(self.tokens.items[(TOK_A,)]))

    def test_transport_failure_never_raises(self):
        self.seed(TOK_A)
        out = self.send(FakeExpo(raise_on_send=True))
        self.assertEqual(out["failed"], 1)
        self.assertTrue(pd.is_deliverable(self.tokens.items[(TOK_A,)]))

    def test_accepted_ticket_parks_its_receipt(self):
        self.seed(TOK_A)
        self.send(FakeExpo())
        row = self.tokens.items[(TOK_A,)]
        self.assertTrue(row["pending_receipts"])
        self.assertEqual(row["pending_since"], 1_000_000)


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tokens = token_table()
        self.tokens.items[(TOK_A,)] = {"push_token": TOK_A, "user_id": OWNER, "enabled": True,
                                       "installation_id": INST_1, "pending_receipts": ["r-1"],
                                       "pending_since": 1_000}

    def check(self, expo, now):
        return pd.check_receipts(self.tokens, OWNER, post=expo, key_cls=fdb.Key,
                                 now_iso="t", now_epoch=now)

    def test_not_checked_before_expo_has_them(self):
        expo = FakeExpo()
        self.check(expo, 1_000 + 60)
        self.assertEqual(expo.receipt_calls, [])

    def test_dead_receipt_deactivates(self):
        expo = FakeExpo(receipts={"r-1": {"status": "error",
                                          "details": {"error": "DeviceNotRegistered"}}})
        out = self.check(expo, 1_000 + pd.RECEIPT_MIN_AGE_S + 1)
        self.assertEqual(out["deactivated"], 1)
        row = self.tokens.items[(TOK_A,)]
        self.assertFalse(pd.is_deliverable(row))
        self.assertNotIn("pending_receipts", row)

    def test_ok_receipt_just_clears(self):
        expo = FakeExpo(receipts={"r-1": {"status": "ok"}})
        self.check(expo, 1_000 + pd.RECEIPT_MIN_AGE_S + 1)
        row = self.tokens.items[(TOK_A,)]
        self.assertTrue(pd.is_deliverable(row))
        self.assertNotIn("pending_receipts", row)


class TriggerTests(unittest.TestCase):
    """Drives the REAL transcribe `_notify_processing_outcome`."""

    def setUp(self):
        self.tables = {
            "Notifications": fdb.FakeTable(
                "Notifications", "notification_id",
                indexes={"user-index": ("user_id", "created_at"),
                         "user-unread-index": ("user_id", "unread_marker")}),
            "NotificationDedupe": fdb.FakeTable("NotificationDedupe", "dedupe_key"),
            "PushTokens": token_table(),
        }
        self.tables["PushTokens"].items[(TOK_A,)] = {
            "push_token": TOK_A, "user_id": OWNER, "installation_id": INST_1,
            "platform": "android", "enabled": True}
        self.expo = FakeExpo()
        ddb = mock.Mock()
        ddb.Table.side_effect = lambda name: self.tables[name]
        self.patches = [
            mock.patch.object(tr, "_ddb", ddb),
            mock.patch.object(tr, "Key", fdb.Key),
            mock.patch.object(tr.push_delivery, "urllib_transport", return_value=self.expo),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def pushes(self):
        return [m for batch in self.expo.sent for m in batch]

    def test_a_complete_brief_sends_exactly_one_push(self):
        tr._notify_processing_outcome(KEY, OWNER, "complete", "Client call")
        pushes = self.pushes()
        self.assertEqual(len(pushes), 1)
        self.assertEqual(pushes[0]["data"]["audio_s3_key"], KEY)
        self.assertEqual(pushes[0]["data"]["type"], "brief_ready")
        # It points at the in-app row it delivers.
        ids = [r["notification_id"] for r in self.tables["Notifications"].items.values()
               if r["type"] == ns.TYPE_AI_OUTPUT_READY]
        self.assertEqual(pushes[0]["data"]["notification_id"], ids[0])
        self.assertNotIn("Client call", json.dumps(pushes[0]), "no meeting title on the lock screen")

    def test_degraded_and_failed_outcomes_never_push(self):
        tr._notify_processing_outcome(KEY, OWNER, "transcribed", "Client call")
        tr._notify_processing_outcome(KEY + "x", OWNER, "failed", "Client call")
        self.assertEqual(self.pushes(), [])

    def test_retries_and_reprocessing_do_not_push_twice(self):
        for _ in range(4):
            tr._notify_processing_outcome(KEY, OWNER, "complete", "Client call")
        self.assertEqual(len(self.pushes()), 1)

    def test_a_meeting_that_first_degraded_pushes_once_its_brief_exists(self):
        tr._notify_processing_outcome(KEY, OWNER, "transcribed", "Client call")
        tr._notify_processing_outcome(KEY, OWNER, "complete", "Client call")
        self.assertEqual(len(self.pushes()), 1)

    def test_push_claim_fails_closed(self):
        """If the push claim cannot be written, nothing is sent — a missed
        buzz beats a duplicate one; the in-app row still exists."""
        row = {"notification_id": "n-9", "dedupe_key": "k"}
        broken = mock.Mock()
        broken.put_item.side_effect = fdb.ClientError("ProvisionedThroughputExceededException")
        self.tables["NotificationDedupe"] = broken
        tr._push_brief_ready(OWNER, KEY, row)
        self.assertEqual(self.pushes(), [])

    def test_no_recipient_no_push(self):
        tr._notify_processing_outcome(KEY, "", "complete", "Client call")
        self.assertEqual(self.pushes(), [])

    def test_a_push_failure_never_raises_into_the_pipeline(self):
        self.expo.raise_on_send = True
        tr._notify_processing_outcome(KEY, OWNER, "complete", "Client call")  # no exception


if __name__ == "__main__":
    unittest.main()
