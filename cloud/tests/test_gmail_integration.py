#!/usr/bin/env python3
"""test_gmail_integration.py — the Integrations layer and the Gmail provider.

This feature holds a credential that can SEND MAIL AS THE USER, so the tests
that matter most are the ones asserting what it refuses to do. The file is
organised around that: the happy paths are a handful of cases, and the bulk
below pins the ways a send must be refused.

WHY EACH GROUP EXISTS — every one is a real failure mode, not a box-tick:

  STATUS        The requirement is that the BACKEND is the source of truth and
                that a token's mere presence is not "connected". So a revoked
                connection must report REAUTH_REQUIRED (not CONNECTED, not
                absent), and discovering a dead token during a send must
                PERSIST that, or the next status read lies until another send
                fails.

  ENFORCEMENT   "Hide the button" is not a security control. Every Gmail route
                must refuse on the server when Gmail is not usable — with 409
                and a stable `code`, never 401, because 401 on this API makes
                the app clear the JWT and sign the user out of MinuteX over a
                dead Google token. That bug already happened once with
                Salesforce (see SF_RECONNECT_STATUS); these tests exist so it
                cannot happen again through a new door.

  ISOLATION     One user's connection must be invisible and unusable to
                another. Since every route keys the connection off the JWT's
                user_id and never off a client-supplied id, the test that
                proves it is the one where a stranger's send finds no
                connection at all rather than someone else's.

  RECIPIENTS    A participant with no email address must BLOCK the send, not
                be dropped from it — "sent to 2 of 3" and "sent to everyone"
                are indistinguishable to the sender otherwise, and the person
                left out never learns. Also: a contact_id belonging to another
                user must not resolve, or the picker becomes a way to mail
                arbitrary addresses under a legitimate-looking contact.

  MESSAGE       The MIME builder handles untrusted input: attachment bytes and
                filenames come from the client, and the subject can contain
                anything a meeting title can. Header injection through a
                newline in the subject is the specific attack; mislabelled
                attachment types and oversized payloads are the specific bugs.

  TOKENS        A refresh token must never appear in any response. The test
                that proves it stuffs a connection row and asserts the token
                is absent from the serialized body — an assembled payload
                (integrations.public_status) can only emit what it lists, and
                this pins that it stays that way.

OFFLINE by design, same as the rest of this directory: fake_dynamodb tables,
no AWS, no network, no Google. Run it before every deploy —
40_deploy_gmail_integration.sh does exactly that.

Run:  python -m pytest tests/test_gmail_integration.py
"""
import base64
import json
import sys
import unittest
from email import message_from_bytes
from email.header import decode_header, make_header
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "functions/userapi"))

# Importing test_ai_workspace installs the boto3/Groq stubs and gives us the
# same `api` module object every other test in this directory binds — see
# conftest.py on why one shared stub identity matters.
from test_ai_workspace import (  # noqa: E402
    RECORDING, api, parse,
)

import fake_dynamodb as fdb  # noqa: E402
import email_message  # noqa: E402
import integrations  # noqa: E402

KEY = "recordings/u-1/mobile/mobile-abc_1754300000.m4a"
OWNER = "u-1"
STRANGER = "u-2"
GMAIL = integrations.PROVIDER_GMAIL

CONTACT_RAHUL = "c-rahul"
CONTACT_NEHA = "c-neha"
CONTACT_NOEMAIL = "c-amit"
CONTACT_OTHER_USER = "c-stranger"


def call(handler, ev):
    """Invoke a route handler the way lambda_handler REALLY does.

    test_ai_workspace exports a `call` of its own, but it predates errors that
    carry a machine-readable `code` and rebuilds the body as {"error": ...}
    only. Every enforcement test in this file asserts on that code — it is how
    the app tells "connect Gmail" apart from "reconnect Gmail", and from an
    expired MinuteX session — so a helper that silently drops it would let a
    regression in the real router pass unnoticed here.

    This mirrors lambda_handler's except-block exactly instead. It is local
    rather than a fix to the shared helper because changing that would alter
    what every other suite in this directory asserts against.
    """
    try:
        return handler(ev)
    except api.ApiError as e:
        body = {"error": e.message}
        code = getattr(e, "code", "")
        if code:
            body["code"] = code
        return api._resp(e.status, body)


def event(method, route, body=None, params=None, auth=True, qs=None):
    """An API Gateway HTTP API v2.0 event."""
    ev = {
        "routeKey": f"{method} {route}",
        "requestContext": {"http": {"method": method, "path": "/"},
                           "stage": "$default"},
        "pathParameters": params or {},
        "headers": {"host": "api.example.com"},
    }
    if auth:
        ev["headers"]["authorization"] = "Bearer test-token"
    if qs is not None:
        ev["queryStringParameters"] = qs
    if body is not None:
        ev["body"] = json.dumps(body)
    return ev


def connection_row(user_id=OWNER, status=integrations.STATUS_CONNECTED,
                   account="user@gmail.com", token="enc-refresh-token",
                   message=""):
    return {
        "user_id": user_id,
        "provider": GMAIL,
        "status": status,
        "status_message": message,
        "refresh_token_enc": token,
        "account_identifier": account,
        "account_name": "Test User",
        "scopes": list(api.GMAIL_SCOPES),
        "connected_at": "2026-08-01T00:00:00Z",
        "updated_at": "2026-08-01T00:00:00Z",
    }


class IntegrationTestCase(unittest.TestCase):
    """Real fake tables for Integrations, Contacts, Recordings, Participants.

    Genuine FakeTables rather than MagicMocks because most tests here are a
    ROUND TRIP — connect then read status, disconnect then confirm the row is
    gone, mark reauth then confirm the next status read reports it. A stub
    that only records the last call cannot tell a working state transition
    from a broken one.
    """

    def setUp(self):
        self.item = json.loads(json.dumps(RECORDING))
        self.item["user_id"] = OWNER

        self.integrations = fdb.FakeTable("Integrations", "user_id", "provider")
        self.contacts = fdb.FakeTable("Contacts", "contact_id")
        self.recordings = fdb.FakeTable("Recordings", "audio_s3_key")
        self.participants = fdb.FakeTable(
            "MeetingParticipants", "audio_s3_key", "speaker_id")
        self.tasks = fdb.FakeTable("Tasks", "task_id")
        # Salesforce lives in its OWN table and is merged into the catalog by
        # _integration_rows. A real fake table (rather than leaving the
        # Lambda's module-level handle in place) is what lets these tests
        # assert the merge — and stops an unstubbed handle answering with
        # whatever another suite left behind.
        self.crm = fdb.FakeTable("CrmConnections", "user_id", "provider")

        self.recordings.items[(KEY,)] = self.item
        self.contacts.items[(CONTACT_RAHUL,)] = {
            "contact_id": CONTACT_RAHUL, "owner_user_id": OWNER,
            "name": "Rahul Sharma", "email": "rahul@example.com"}
        self.contacts.items[(CONTACT_NEHA,)] = {
            "contact_id": CONTACT_NEHA, "owner_user_id": OWNER,
            "name": "Neha Shah", "email": "neha@example.com"}
        self.contacts.items[(CONTACT_NOEMAIL,)] = {
            "contact_id": CONTACT_NOEMAIL, "owner_user_id": OWNER,
            "name": "Amit Patel", "email": ""}
        # Belongs to somebody else — must never resolve for OWNER.
        self.contacts.items[(CONTACT_OTHER_USER,)] = {
            "contact_id": CONTACT_OTHER_USER, "owner_user_id": STRANGER,
            "name": "Someone Else", "email": "someone@elsewhere.com"}

        self.sent = []          # every raw message handed to Gmail
        self.refreshed = []     # every refresh_credentials call

        patches = [
            mock.patch.object(api, "Key", fdb.Key),
            mock.patch.object(api, "_integrations", self.integrations),
            mock.patch.object(api, "_crm_connections", self.crm),
            mock.patch.object(api, "_contacts", self.contacts),
            mock.patch.object(api, "_recordings", self.recordings),
            mock.patch.object(api, "_meeting_participants", self.participants),
            mock.patch.object(api, "_tasks", self.tasks),
            mock.patch.object(api, "_require_auth", return_value=OWNER),
            mock.patch.object(api, "_owned_devices", return_value=[]),
            # KMS is a pass-through in tests: these tests are about the
            # LIFECYCLE and the refusals, not about whether boto3 encrypts.
            # The "never returned to the client" property is asserted directly
            # against the response body instead, which is the real guarantee.
            mock.patch.object(api, "_integration_kms_encrypt",
                              side_effect=lambda p: f"enc({p})"),
            mock.patch.object(api, "_integration_kms_decrypt",
                              side_effect=lambda c: c),
            mock.patch.object(api, "GOOGLE_CLIENT_ID", "test-client-id"),
            mock.patch.object(api, "GOOGLE_REDIRECT_URI",
                              "https://api.example.com/integrations/gmail/callback"),
            mock.patch.object(api, "INTEGRATION_RETURN_URL",
                              "recorderapp://integrations-connected"),
            mock.patch.object(api, "_google_client_secret",
                              return_value="test-secret"),
            mock.patch.object(api, "_jwt_secret", return_value="test-jwt-secret"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

        # The provider's two network calls, stubbed at the seam.
        self.refresh_result = {"access_token": "ya29.access"}
        self.send_result = {"id": "msg-1", "threadId": "thr-1"}

        def fake_refresh(refresh_token):
            self.refreshed.append(refresh_token)
            if isinstance(self.refresh_result, Exception):
                raise self.refresh_result
            return self.refresh_result

        def fake_send(access_token, raw):
            self.sent.append(raw)
            if isinstance(self.send_result, Exception):
                raise self.send_result
            return self.send_result

        for name, fn in (("refresh_credentials", fake_refresh),
                         ("send_message", fake_send)):
            p = mock.patch.object(api._gmail_provider, name, side_effect=fn)
            p.start()
            self.addCleanup(p.stop)

    # -- helpers ---------------------------------------------------------
    def connect_gmail(self, **kw):
        row = connection_row(**kw)
        self.integrations.items[(OWNER, GMAIL)] = row
        return row

    def connect_salesforce(self, user_id=OWNER):
        """A CrmConnections row, in that table's OWN shape — deliberately not
        the Integrations shape, because translating between them is what
        integrations.salesforce_row() is for and what these tests check."""
        row = {"user_id": user_id, "provider": "salesforce",
               "instance_url": "https://acme.my.salesforce.com",
               "refresh_token_enc": "enc-sf-token",
               "sf_username": "admin@acme.com",
               "connected_at": "2026-07-01T00:00:00Z",
               "updated_at": "2026-07-01T00:00:00Z"}
        self.crm.items[(user_id, "salesforce")] = row
        return row

    def add_participant(self, speaker_id, contact_id):
        self.participants.items[(KEY, speaker_id)] = {
            "audio_s3_key": KEY, "speaker_id": speaker_id,
            "contact_id": contact_id, "owner_user_id": OWNER}

    def last_message(self):
        """The last message handed to Gmail, parsed back into a MIME object."""
        return message_from_bytes(base64.urlsafe_b64decode(self.sent[-1]))

    def last_subject(self):
        """The decoded Subject.

        Read through decode_header because a subject containing an em dash —
        which every default subject here does, and which real meeting titles
        routinely do — is correctly RFC 2047 encoded on the wire. Asserting
        against the raw header would be asserting that we DON'T encode it.
        """
        return str(make_header(decode_header(self.last_message()["Subject"])))

    def send_meeting(self, body, expect=200):
        resp = call(api.gmail_send_meeting,
                    event("POST", "/integrations/gmail/send/meeting/{key+}",
                          body=body, params={"key": KEY}))
        status, data = parse(resp)
        self.assertEqual(status, expect, data)
        return data


# ---------------------------------------------------------------------------
# CONNECTION STATUS — the backend is the source of truth.
# ---------------------------------------------------------------------------
class StatusTests(IntegrationTestCase):

    def test_catalog_lists_every_provider_when_nothing_is_connected(self):
        """The Coming Soon cards come from the server, not from the app.

        Covers BOTH sources: no Integrations row and no CrmConnections row
        must read as NOT_CONNECTED for every card, including Salesforce."""
        status, data = parse(call(api.list_integrations, event("GET", "/integrations")))
        self.assertEqual(status, 200)
        providers = [i["provider"] for i in data["integrations"]]
        self.assertIn(GMAIL, providers)
        for expected in ("whatsapp", "salesforce", "google_calendar",
                         "google_tasks"):
            self.assertIn(expected, providers)
        for i in data["integrations"]:
            self.assertEqual(i["status"], integrations.STATUS_NOT_CONNECTED)
            self.assertFalse(i["connected"])

    def test_working_integrations_are_available_and_the_rest_are_not(self):
        """The card list must separate "this works" from "this is coming".

        Gmail and Salesforce both WORK — they are just connected by different
        flows — so both are available. Everything else is a Coming Soon card
        and must never advertise itself as connectable, or an old build could
        offer a connect button the backend cannot honour.
        """
        status, data = parse(call(api.list_integrations, event("GET", "/integrations")))
        by_id = {i["provider"]: i for i in data["integrations"]}
        self.assertTrue(by_id[GMAIL]["available"])
        self.assertTrue(by_id["salesforce"]["available"])
        for other in ("whatsapp", "google_calendar", "google_tasks"):
            self.assertFalse(by_id[other]["available"])

    def test_salesforce_is_available_but_not_on_the_generic_flow(self):
        """`available` and `managed_elsewhere` are two separate facts: the card
        works, and it is connected somewhere else. Collapsing them is how
        Salesforce ends up either wrongly greyed out or wrongly routed into an
        OAuth flow that does not exist for it."""
        _, data = parse(call(api.list_integrations, event("GET", "/integrations")))
        sf = {i["provider"]: i for i in data["integrations"]}["salesforce"]
        self.assertTrue(sf["available"])
        self.assertTrue(sf["managed_elsewhere"])
        self.assertNotIn("salesforce", integrations.CONNECTABLE)
        self.assertIn(GMAIL, integrations.CONNECTABLE)

    def test_the_generic_connect_flow_refuses_salesforce(self):
        """It has no /integrations/salesforce/connect route — claiming
        otherwise would send the user into a dead flow."""
        status, _ = parse(call(
            api.integration_connect,
            event("POST", "/integrations/{provider}/connect",
                  body={}, params={"provider": "salesforce"})))
        self.assertEqual(status, 400)

    def test_the_generic_disconnect_refuses_salesforce(self):
        """The dangerous one. Without the managed_elsewhere guard this would
        delete from the Integrations table, report success, and leave the real
        CrmConnections credential in place — a disconnect that did not."""
        self.connect_salesforce()
        status, _ = parse(call(
            api.integration_disconnect,
            event("DELETE", "/integrations/{provider}",
                  params={"provider": "salesforce"})))
        self.assertEqual(status, 400)
        self.assertIn((OWNER, "salesforce"), self.crm.items)

    def test_a_linked_salesforce_org_shows_as_connected(self):
        """Read from CrmConnections and merged into the one catalog — the whole
        point of moving Salesforce inside Connected apps."""
        self.connect_salesforce()
        _, data = parse(call(api.list_integrations, event("GET", "/integrations")))
        sf = {i["provider"]: i for i in data["integrations"]}["salesforce"]
        self.assertEqual(sf["status"], integrations.STATUS_CONNECTED)
        self.assertTrue(sf["connected"])
        self.assertEqual(sf["account_identifier"], "admin@acme.com")
        self.assertEqual(sf["connected_at"], "2026-07-01T00:00:00Z")

    def test_a_strangers_salesforce_org_is_not_shown(self):
        self.connect_salesforce(user_id=STRANGER)
        _, data = parse(call(api.list_integrations, event("GET", "/integrations")))
        sf = {i["provider"]: i for i in data["integrations"]}["salesforce"]
        self.assertFalse(sf["connected"])
        self.assertNotIn("admin@acme.com", json.dumps(data))

    def test_salesforce_status_never_returns_its_refresh_token(self):
        self.connect_salesforce()
        resp = call(api.list_integrations, event("GET", "/integrations"))
        self.assertNotIn("enc-sf-token", resp["body"])

    def test_a_failed_salesforce_read_degrades_only_that_card(self):
        """An unconfigured CrmConnections table must not take down the whole
        Integrations screen — which is also how the Coming Soon cards and the
        Gmail card reach the user."""
        self.connect_gmail()
        with mock.patch.object(api, "_get_salesforce_connection",
                               side_effect=RuntimeError("no table")):
            status, data = parse(call(api.list_integrations,
                                      event("GET", "/integrations")))
        self.assertEqual(status, 200)
        by_id = {i["provider"]: i for i in data["integrations"]}
        self.assertTrue(by_id[GMAIL]["connected"])
        self.assertFalse(by_id["salesforce"]["connected"])
        self.assertEqual(len(data["integrations"]), len(integrations.PROVIDERS))

    def test_connected_status_reports_the_account(self):
        self.connect_gmail()
        status, data = parse(call(
            api.get_integration,
            event("GET", "/integrations/{provider}", params={"provider": GMAIL})))
        self.assertEqual(status, 200)
        self.assertEqual(data["integration"]["status"],
                         integrations.STATUS_CONNECTED)
        self.assertTrue(data["integration"]["connected"])
        self.assertEqual(data["integration"]["account_identifier"],
                         "user@gmail.com")

    def test_reauth_required_is_not_connected(self):
        """The whole point of a status vocabulary: a row exists and holds a
        token, and the honest answer is still 'not usable'."""
        self.connect_gmail(status=integrations.STATUS_REAUTH_REQUIRED,
                           message="The connection was revoked or expired.")
        status, data = parse(call(
            api.get_integration,
            event("GET", "/integrations/{provider}", params={"provider": GMAIL})))
        self.assertEqual(data["integration"]["status"],
                         integrations.STATUS_REAUTH_REQUIRED)
        self.assertFalse(data["integration"]["connected"])
        self.assertTrue(data["integration"]["message"])

    def test_presence_of_a_token_is_not_connected(self):
        """is_usable() must read STATUS, never 'is there a token'."""
        row = connection_row(status=integrations.STATUS_ERROR)
        self.assertFalse(integrations.is_usable(row))
        self.assertFalse(integrations.is_usable(
            connection_row(status=integrations.STATUS_REAUTH_REQUIRED)))
        self.assertTrue(integrations.is_usable(connection_row()))
        # A CONNECTED row with no credential is also unusable.
        self.assertFalse(integrations.is_usable(connection_row(token="")))

    def test_unknown_provider_is_404(self):
        status, _ = parse(call(
            api.get_integration,
            event("GET", "/integrations/{provider}",
                  params={"provider": "myspace"})))
        self.assertEqual(status, 404)

    def test_status_never_returns_the_refresh_token(self):
        """Assembled, not filtered — the guarantee that a new attribute cannot
        leak by being forgotten in a deny-list."""
        self.integrations.items[(OWNER, GMAIL)] = {
            **connection_row(token="SUPER-SECRET-REFRESH-TOKEN"),
            "some_future_attribute": "ANOTHER-SECRET",
        }
        resp = call(api.list_integrations, event("GET", "/integrations"))
        body = resp["body"]
        self.assertNotIn("SUPER-SECRET-REFRESH-TOKEN", body)
        self.assertNotIn("ANOTHER-SECRET", body)
        self.assertNotIn("refresh_token_enc", body)


# ---------------------------------------------------------------------------
# OAUTH — connect, callback, cancel, disconnect.
# ---------------------------------------------------------------------------
class OAuthTests(IntegrationTestCase):

    def connect(self, provider=GMAIL, expect=200):
        resp = call(api.integration_connect,
                    event("POST", "/integrations/{provider}/connect",
                          body={}, params={"provider": provider}))
        status, data = parse(resp)
        self.assertEqual(status, expect, data)
        return data

    def callback(self, qs, provider=GMAIL):
        return api.integration_callback(
            event("GET", "/integrations/{provider}/callback",
                  params={"provider": provider}, qs=qs, auth=False))

    def test_connect_returns_a_pkce_authorize_url(self):
        url = self.connect()["authorize_url"]
        self.assertTrue(url.startswith(api.GOOGLE_AUTH_URL))
        self.assertIn("code_challenge=", url)
        self.assertIn("code_challenge_method=S256", url)
        self.assertIn("client_id=test-client-id", url)
        # The refresh token depends on both of these; without them the
        # connection works for an hour and then dies.
        self.assertIn("access_type=offline", url)
        self.assertIn("prompt=consent", url)
        # The VERIFIER must never be in the URL — that is the point of PKCE.
        self.assertNotIn("code_verifier", url)

    def test_connect_requests_only_send_and_identity_scopes(self):
        """The security boundary. A read scope appearing here would turn a
        meeting-notes product into a mailbox-scraping one."""
        url = self.connect()["authorize_url"]
        self.assertIn("gmail.send", url)
        self.assertIn("userinfo.email", url)
        for forbidden in ("gmail.readonly", "gmail.modify", "gmail.compose",
                          "gmail.metadata", "auth/calendar", "auth/tasks"):
            self.assertNotIn(forbidden, url)

    def test_connect_refuses_a_provider_that_is_not_available(self):
        self.connect(provider="whatsapp", expect=400)

    def test_state_round_trips_and_is_tamper_evident(self):
        state = api._sign_integration_state(OWNER, GMAIL, "verifier-1")
        user_id, verifier = api._verify_integration_state(state, GMAIL)
        self.assertEqual(user_id, OWNER)
        self.assertEqual(verifier, "verifier-1")
        # One flipped character invalidates it.
        tampered = state[:-2] + ("aa" if not state.endswith("aa") else "bb")
        with self.assertRaises(api.ApiError):
            api._verify_integration_state(tampered, GMAIL)

    def test_state_is_bound_to_its_provider(self):
        """A validly-signed state for one integration must not be replayable
        against another's callback — same secret, same user, still wrong."""
        state = api._sign_integration_state(OWNER, GMAIL, "verifier-1")
        with self.assertRaises(api.ApiError):
            api._verify_integration_state(state, "google_calendar")

    def test_expired_state_is_rejected(self):
        with mock.patch.object(api, "INTEGRATION_STATE_TTL", -1):
            state = api._sign_integration_state(OWNER, GMAIL, "v")
        with self.assertRaises(api.ApiError):
            api._verify_integration_state(state, GMAIL)

    def test_callback_success_stores_a_connected_row(self):
        state = api._sign_integration_state(OWNER, GMAIL, "verifier-1")
        with mock.patch.object(
                api._gmail_provider, "exchange_code",
                return_value={"access_token": "ya29.a", "refresh_token": "r-1",
                              "scope": " ".join(api.GMAIL_SCOPES)}), \
             mock.patch.object(
                api._gmail_provider, "account_info",
                return_value={"account_identifier": "user@gmail.com",
                              "account_name": "Test User"}):
            resp = self.callback({"code": "auth-code", "state": state})

        self.assertEqual(resp["statusCode"], 302)
        self.assertIn("connected=1", resp["headers"]["Location"])
        self.assertIn("provider=gmail", resp["headers"]["Location"])

        row = self.integrations.items[(OWNER, GMAIL)]
        self.assertEqual(row["status"], integrations.STATUS_CONNECTED)
        self.assertEqual(row["account_identifier"], "user@gmail.com")
        # Stored as ciphertext, never as the raw token.
        self.assertEqual(row["refresh_token_enc"], "enc(r-1)")
        self.assertNotIn("r-1", json.dumps(
            {k: v for k, v in row.items() if k != "refresh_token_enc"}))

    def test_callback_user_denied_redirects_without_connecting(self):
        resp = self.callback({"error": "access_denied"})
        self.assertEqual(resp["statusCode"], 302)
        self.assertIn("connected=0", resp["headers"]["Location"])
        self.assertIn("reason=denied", resp["headers"]["Location"])
        self.assertNotIn((OWNER, GMAIL), self.integrations.items)

    def test_callback_without_params_redirects_with_an_error(self):
        resp = self.callback({})
        self.assertIn("connected=0", resp["headers"]["Location"])
        self.assertIn("reason=missing_params", resp["headers"]["Location"])
        self.assertNotIn((OWNER, GMAIL), self.integrations.items)

    def test_callback_with_a_bad_state_does_not_connect(self):
        resp = self.callback({"code": "c", "state": "not-a-real-state"})
        self.assertIn("connected=0", resp["headers"]["Location"])
        self.assertNotIn((OWNER, GMAIL), self.integrations.items)

    def test_callback_refuses_a_grant_with_no_refresh_token(self):
        """Without one the connection works for an hour and then dies. Storing
        it would leave the user with a card that says Connected and a Send
        button that fails an hour later."""
        state = api._sign_integration_state(OWNER, GMAIL, "verifier-1")
        with mock.patch.object(api._gmail_provider, "exchange_code",
                               return_value={"access_token": "ya29.a"}):
            resp = self.callback({"code": "c", "state": state})
        self.assertIn("reason=no_refresh_token", resp["headers"]["Location"])
        self.assertNotIn((OWNER, GMAIL), self.integrations.items)

    def test_callback_survives_a_failed_identity_lookup(self):
        """The address is cosmetic; losing it must not fail the connection."""
        state = api._sign_integration_state(OWNER, GMAIL, "v")
        with mock.patch.object(
                api._gmail_provider, "exchange_code",
                return_value={"access_token": "a", "refresh_token": "r"}), \
             mock.patch.object(api._gmail_provider, "account_info",
                               side_effect=RuntimeError("boom")):
            resp = self.callback({"code": "c", "state": state})
        self.assertIn("connected=1", resp["headers"]["Location"])
        self.assertEqual(self.integrations.items[(OWNER, GMAIL)]["status"],
                         integrations.STATUS_CONNECTED)

    def test_reconnect_preserves_the_original_connected_at(self):
        self.connect_gmail()
        state = api._sign_integration_state(OWNER, GMAIL, "v")
        with mock.patch.object(
                api._gmail_provider, "exchange_code",
                return_value={"access_token": "a", "refresh_token": "r2"}), \
             mock.patch.object(api._gmail_provider, "account_info",
                               return_value={}):
            self.callback({"code": "c", "state": state})
        self.assertEqual(
            self.integrations.items[(OWNER, GMAIL)]["connected_at"],
            "2026-08-01T00:00:00Z")

    def test_disconnect_removes_the_row_and_revokes(self):
        self.connect_gmail()
        with mock.patch.object(api._gmail_provider, "revoke") as revoke:
            status, data = parse(call(
                api.integration_disconnect,
                event("DELETE", "/integrations/{provider}",
                      params={"provider": GMAIL})))
        self.assertEqual(status, 200)
        self.assertTrue(data["disconnected"])
        revoke.assert_called_once()
        # DELETED, not flagged: a row that still holds ciphertext is a
        # credential we said we removed and did not.
        self.assertNotIn((OWNER, GMAIL), self.integrations.items)
        self.assertEqual(data["integration"]["status"],
                         integrations.STATUS_NOT_CONNECTED)

    def test_disconnect_succeeds_even_if_revoke_fails(self):
        """The user asked MinuteX to stop. That must happen whether or not
        Google is reachable."""
        self.connect_gmail()
        with mock.patch.object(api._gmail_provider, "revoke",
                               side_effect=RuntimeError("network down")):
            status, _ = parse(call(
                api.integration_disconnect,
                event("DELETE", "/integrations/{provider}",
                      params={"provider": GMAIL})))
        self.assertEqual(status, 200)
        self.assertNotIn((OWNER, GMAIL), self.integrations.items)

    def test_disconnect_when_never_connected_is_not_an_error(self):
        status, data = parse(call(
            api.integration_disconnect,
            event("DELETE", "/integrations/{provider}",
                  params={"provider": GMAIL})))
        self.assertEqual(status, 200)
        self.assertTrue(data["disconnected"])

    def test_disconnect_does_not_touch_minutex_data(self):
        """Explicitly required: disconnecting removes the ability to send, not
        anything the user created."""
        self.connect_gmail()
        self.add_participant("0", CONTACT_RAHUL)
        self.tasks.items[("t-1",)] = {"task_id": "t-1", "owner_user_id": OWNER,
                                      "title": "Keep me"}
        with mock.patch.object(api._gmail_provider, "revoke"):
            call(api.integration_disconnect,
                 event("DELETE", "/integrations/{provider}",
                       params={"provider": GMAIL}))
        self.assertIn((KEY,), self.recordings.items)
        self.assertIn((CONTACT_RAHUL,), self.contacts.items)
        self.assertIn(("t-1",), self.tasks.items)
        self.assertIn((KEY, "0"), self.participants.items)


# ---------------------------------------------------------------------------
# ENFORCEMENT — the server refuses when Gmail is not usable.
# ---------------------------------------------------------------------------
class EnforcementTests(IntegrationTestCase):
    """Hiding the button is presentation. THESE are the control."""

    def gmail_routes(self):
        """Every route that must refuse. Kept as a list so a new Gmail route
        added without enforcement fails here rather than shipping open."""
        return [
            (api.gmail_send,
             event("POST", "/integrations/gmail/send",
                   body={"recipients": [{"email": "a@b.com"}],
                         "subject": "s", "body": "b"})),
            (api.gmail_meeting_recipients,
             event("GET", "/integrations/gmail/recipients/{key+}",
                   params={"key": KEY})),
            (api.gmail_send_meeting,
             event("POST", "/integrations/gmail/send/meeting/{key+}",
                   body={"recipients": [{"contact_id": CONTACT_RAHUL}]},
                   params={"key": KEY})),
            (api.gmail_send_task,
             event("POST", "/integrations/gmail/send/task/{task_id}",
                   body={}, params={"task_id": "t-1"})),
        ]

    def test_every_gmail_route_refuses_when_not_connected(self):
        for handler, ev in self.gmail_routes():
            with self.subTest(handler=handler.__name__):
                status, data = parse(call(handler, ev))
                self.assertEqual(status, api.INTEGRATION_STATUS_CODE, data)
                self.assertEqual(data["code"],
                                 api.INTEGRATION_NOT_CONNECTED_CODE)
                self.assertIn("Connect Gmail", data["error"])
        self.assertEqual(self.sent, [])

    def test_every_gmail_route_refuses_when_reauth_is_required(self):
        self.connect_gmail(status=integrations.STATUS_REAUTH_REQUIRED)
        for handler, ev in self.gmail_routes():
            with self.subTest(handler=handler.__name__):
                status, data = parse(call(handler, ev))
                self.assertEqual(status, api.INTEGRATION_STATUS_CODE, data)
                self.assertEqual(data["code"], api.INTEGRATION_REAUTH_CODE)
        self.assertEqual(self.sent, [])

    def test_refusal_is_never_401(self):
        """401 on this API means the MinuteX JWT is dead, and the app reacts by
        clearing the session and bouncing to /login. A dead GOOGLE token must
        never do that — that exact bug already shipped once with Salesforce."""
        for handler, ev in self.gmail_routes():
            with self.subTest(handler=handler.__name__):
                status, _ = parse(call(handler, ev))
                self.assertNotEqual(status, 401)

    def test_disconnected_gmail_cannot_send(self):
        """The end of the flow the requirement describes: connect, send,
        disconnect, and sending must stop working."""
        self.connect_gmail()
        self.add_participant("0", CONTACT_RAHUL)
        self.send_meeting({"recipients": [{"contact_id": CONTACT_RAHUL}]})
        self.assertEqual(len(self.sent), 1)

        with mock.patch.object(api._gmail_provider, "revoke"):
            call(api.integration_disconnect,
                 event("DELETE", "/integrations/{provider}",
                       params={"provider": GMAIL}))

        self.send_meeting({"recipients": [{"contact_id": CONTACT_RAHUL}]},
                          expect=api.INTEGRATION_STATUS_CODE)
        self.assertEqual(len(self.sent), 1, "no second message may be sent")


# ---------------------------------------------------------------------------
# ISOLATION — one user's connection is invisible and unusable to another.
# ---------------------------------------------------------------------------
class IsolationTests(IntegrationTestCase):

    def test_a_strangers_connection_is_invisible(self):
        self.integrations.items[(STRANGER, GMAIL)] = connection_row(
            user_id=STRANGER, account="victim@gmail.com")
        status, data = parse(call(api.list_integrations,
                                  event("GET", "/integrations")))
        self.assertEqual(status, 200)
        by_id = {i["provider"]: i for i in data["integrations"]}
        self.assertFalse(by_id[GMAIL]["connected"])
        self.assertNotIn("victim@gmail.com", resp_body(data))

    def test_a_user_cannot_send_through_someone_elses_gmail(self):
        """The routes never take a user_id from the client — identity comes
        only from the JWT — so a stranger's connection is simply not found."""
        self.integrations.items[(STRANGER, GMAIL)] = connection_row(
            user_id=STRANGER, account="victim@gmail.com")
        status, data = parse(call(
            api.gmail_send,
            event("POST", "/integrations/gmail/send",
                  body={"recipients": [{"email": "a@b.com"}],
                        "subject": "s", "body": "b"})))
        self.assertEqual(status, api.INTEGRATION_STATUS_CODE, data)
        self.assertEqual(self.sent, [])

    def test_a_user_cannot_email_someone_elses_meeting(self):
        self.connect_gmail()
        other_key = "recordings/u-2/mobile/other.m4a"
        self.recordings.items[(other_key,)] = {
            "audio_s3_key": other_key, "user_id": STRANGER,
            "title": "Private meeting"}
        status, _ = parse(call(
            api.gmail_send_meeting,
            event("POST", "/integrations/gmail/send/meeting/{key+}",
                  body={"recipients": [{"contact_id": CONTACT_RAHUL}]},
                  params={"key": other_key})))
        # 404, not 403 — the recording routes report a miss and a forbidden
        # identically so the API cannot be used to probe for keys.
        self.assertEqual(status, 404)
        self.assertEqual(self.sent, [])

    def test_a_user_cannot_email_someone_elses_task(self):
        self.connect_gmail()
        self.tasks.items[("t-other",)] = {
            "task_id": "t-other", "owner_user_id": STRANGER,
            "title": "Their task", "assignee_contact_id": CONTACT_OTHER_USER}
        status, _ = parse(call(
            api.gmail_send_task,
            event("POST", "/integrations/gmail/send/task/{task_id}",
                  body={}, params={"task_id": "t-other"})))
        self.assertEqual(status, 404)
        self.assertEqual(self.sent, [])

    def test_another_users_contact_id_does_not_resolve(self):
        """Otherwise the picker is a way to mail an arbitrary address while
        looking like a legitimate contact send."""
        self.connect_gmail()
        status, data = parse(call(
            api.gmail_send,
            event("POST", "/integrations/gmail/send",
                  body={"recipients": [{"contact_id": CONTACT_OTHER_USER}],
                        "subject": "s", "body": "b"})))
        self.assertEqual(status, 422, data)
        self.assertEqual(self.sent, [])

    def test_a_client_supplied_email_cannot_override_a_contact(self):
        """The stored contact is authoritative for the address."""
        self.connect_gmail()
        self.send_meeting({"recipients": [
            {"contact_id": CONTACT_RAHUL, "email": "attacker@evil.com"}]})
        to = self.last_message()["To"]
        self.assertIn("rahul@example.com", to)
        self.assertNotIn("attacker@evil.com", to)


def resp_body(data):
    return json.dumps(data)


# ---------------------------------------------------------------------------
# RECIPIENTS — resolved, never guessed.
# ---------------------------------------------------------------------------
class RecipientTests(IntegrationTestCase):

    def test_meeting_recipients_resolve_from_participants(self):
        self.connect_gmail()
        self.add_participant("0", CONTACT_RAHUL)
        self.add_participant("1", CONTACT_NEHA)
        status, data = parse(call(
            api.gmail_meeting_recipients,
            event("GET", "/integrations/gmail/recipients/{key+}",
                  params={"key": KEY})))
        self.assertEqual(status, 200)
        emails = sorted(r["email"] for r in data["recipients"])
        self.assertEqual(emails, ["neha@example.com", "rahul@example.com"])
        self.assertEqual(data["unresolved"], [])

    def test_a_participant_without_an_email_is_reported_not_hidden(self):
        self.connect_gmail()
        self.add_participant("0", CONTACT_RAHUL)
        self.add_participant("1", CONTACT_NOEMAIL)
        _, data = parse(call(
            api.gmail_meeting_recipients,
            event("GET", "/integrations/gmail/recipients/{key+}",
                  params={"key": KEY})))
        self.assertEqual([r["email"] for r in data["recipients"]],
                         ["rahul@example.com"])
        self.assertEqual([r["name"] for r in data["unresolved"]],
                         ["Amit Patel"])

    def test_sending_to_someone_without_an_email_is_refused(self):
        """Do NOT silently attempt to send. A partial send is indistinguishable
        from a complete one, and the person left out never learns."""
        self.connect_gmail()
        data = self.send_meeting(
            {"recipients": [{"contact_id": CONTACT_RAHUL},
                            {"contact_id": CONTACT_NOEMAIL}]},
            expect=422)
        self.assertIn("Amit Patel", data["error"])
        self.assertIn("Email address unavailable", data["error"])
        self.assertEqual(self.sent, [], "nothing may be sent")

    def test_the_unresolved_message_names_everyone_affected(self):
        _, unresolved = email_message.resolve_recipients([
            {"name": "Rahul Sharma"}, {"name": "Neha Shah"},
            {"name": "Amit Patel"}])
        msg = email_message.describe_unresolved(unresolved)
        self.assertIn("Rahul Sharma", msg)
        self.assertIn("Neha Shah", msg)
        self.assertIn("Amit Patel", msg)

    def test_duplicate_recipients_receive_one_email(self):
        self.connect_gmail()
        self.send_meeting({"recipients": [
            {"contact_id": CONTACT_RAHUL},
            {"email": "rahul@example.com"},
            {"email": "RAHUL@EXAMPLE.COM"}]})
        self.assertEqual(self.last_message()["To"].count("rahul@example.com"), 1)

    def test_an_empty_recipient_list_is_refused(self):
        self.connect_gmail()
        data = self.send_meeting({"recipients": []}, expect=400)
        self.assertIn("recipient", data["error"].lower())
        self.assertEqual(self.sent, [])

    def test_multiple_recipients_all_appear(self):
        self.connect_gmail()
        data = self.send_meeting({"recipients": [
            {"contact_id": CONTACT_RAHUL}, {"contact_id": CONTACT_NEHA}]})
        self.assertEqual(data["recipient_count"], 2)
        to = self.last_message()["To"]
        self.assertIn("rahul@example.com", to)
        self.assertIn("neha@example.com", to)
        # The display name rides along, so the recipient sees a person.
        self.assertIn("Rahul Sharma", to)


# ---------------------------------------------------------------------------
# SENDING — the message, its attachments and its failures.
# ---------------------------------------------------------------------------
class SendTests(IntegrationTestCase):

    def test_send_a_simple_meeting_email(self):
        self.connect_gmail()
        data = self.send_meeting({
            "recipients": [{"contact_id": CONTACT_RAHUL}],
            "subject": "Minutes of Meeting — Client Discussion",
            "body": "Hi Rahul,\n\nPlease find the MoM.\n\nRegards,\nMinuteX"})
        self.assertTrue(data["sent"])
        self.assertEqual(data["message_id"], "msg-1")
        msg = self.last_message()
        self.assertEqual(self.last_subject(),
                         "Minutes of Meeting — Client Discussion")
        self.assertIn("rahul@example.com", msg["To"])
        self.assertIn("user@gmail.com", msg["From"])

    def test_the_body_is_sent_as_both_text_and_html(self):
        self.connect_gmail()
        self.send_meeting({"recipients": [{"contact_id": CONTACT_RAHUL}],
                           "body": "Line one\n\nLine two"})
        types = {p.get_content_type() for p in self.last_message().walk()}
        self.assertIn("text/plain", types)
        self.assertIn("text/html", types)

    def test_the_default_subject_and_body_use_the_meeting_title(self):
        self.connect_gmail()
        self.item["title"] = "Client Discussion"
        self.send_meeting({"recipients": [{"contact_id": CONTACT_RAHUL}]})
        self.assertIn("Client Discussion", self.last_subject())

    def test_send_a_pdf_attachment(self):
        self.connect_gmail()
        self.send_meeting({
            "recipients": [{"contact_id": CONTACT_RAHUL}],
            "attachments": [{"type": "pdf", "filename": "MoM.pdf",
                             "content_base64": base64.b64encode(
                                 b"%PDF-1.4 fake").decode()}]})
        parts = {p.get_filename(): p for p in self.last_message().walk()
                 if p.get_filename()}
        self.assertIn("MoM.pdf", parts)
        self.assertEqual(parts["MoM.pdf"].get_content_type(), "application/pdf")
        self.assertEqual(parts["MoM.pdf"].get_payload(decode=True),
                         b"%PDF-1.4 fake")

    def test_send_a_docx_attachment(self):
        self.connect_gmail()
        self.send_meeting({
            "recipients": [{"contact_id": CONTACT_RAHUL}],
            "attachments": [{"type": "docx", "filename": "MoM.docx",
                             "content_base64": base64.b64encode(
                                 b"PK\x03\x04fake").decode()}]})
        parts = {p.get_filename(): p for p in self.last_message().walk()
                 if p.get_filename()}
        self.assertEqual(
            parts["MoM.docx"].get_content_type(),
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document")

    def test_send_both_pdf_and_docx(self):
        self.connect_gmail()
        b64 = base64.b64encode(b"data").decode()
        self.send_meeting({
            "recipients": [{"contact_id": CONTACT_RAHUL}],
            "attachments": [
                {"type": "pdf", "filename": "Client_Discussion_MoM.pdf",
                 "content_base64": b64},
                {"type": "docx", "filename": "Client_Discussion_MoM.docx",
                 "content_base64": b64}]})
        names = {p.get_filename() for p in self.last_message().walk()
                 if p.get_filename()}
        self.assertEqual(names, {"Client_Discussion_MoM.pdf",
                                 "Client_Discussion_MoM.docx"})

    # ---- Generic meeting-output sharing -------------------------------
    #
    # The share sheet is no longer MoM-shaped: a meeting can send its Minutes,
    # Executive Summary, Action Item Report, a freeform AI document, or any
    # combination, each in PDF/DOCX/Markdown. These pin the BACKEND half of
    # that — the routes must carry several unrelated documents in one message
    # without knowing what any of them are.

    def test_send_several_different_outputs_in_one_email(self):
        """The headline case. Three unrelated documents, three formats, one
        message — and the backend needs no knowledge of what they are."""
        self.connect_gmail()
        b64 = base64.b64encode(b"data").decode()
        self.send_meeting({
            "recipients": [{"contact_id": CONTACT_RAHUL}],
            "subject": "Meeting notes — Client Discussion",
            "attachments": [
                {"type": "pdf", "filename": "Client_Discussion_Minutes.pdf",
                 "content_base64": b64},
                {"type": "docx", "filename": "Client_Discussion_Summary.docx",
                 "content_base64": b64},
                {"type": "md", "filename": "Client_Discussion_Action_Items.md",
                 "content_base64": b64}]})
        parts = {p.get_filename(): p for p in self.last_message().walk()
                 if p.get_filename()}
        self.assertEqual(set(parts), {
            "Client_Discussion_Minutes.pdf",
            "Client_Discussion_Summary.docx",
            "Client_Discussion_Action_Items.md"})
        # Each keeps its OWN content type — a Markdown file mislabelled as a
        # PDF opens as garbage in the recipient's mail client.
        self.assertEqual(parts["Client_Discussion_Minutes.pdf"].get_content_type(),
                         "application/pdf")
        self.assertEqual(parts["Client_Discussion_Action_Items.md"].get_content_type(),
                         "text/markdown")

    def test_the_same_output_in_two_formats_is_two_attachments(self):
        """Minutes as PDF *and* DOCX — one document, two files."""
        self.connect_gmail()
        b64 = base64.b64encode(b"data").decode()
        self.send_meeting({
            "recipients": [{"contact_id": CONTACT_RAHUL}],
            "attachments": [
                {"type": "pdf", "filename": "Minutes.pdf", "content_base64": b64},
                {"type": "docx", "filename": "Minutes.docx", "content_base64": b64}]})
        names = {p.get_filename() for p in self.last_message().walk()
                 if p.get_filename()}
        self.assertEqual(names, {"Minutes.pdf", "Minutes.docx"})

    def test_a_markdown_output_is_accepted(self):
        """Some outputs are genuinely text — the Follow-up Email draft is
        meant to be pasted, not opened in Word."""
        self.connect_gmail()
        self.send_meeting({
            "recipients": [{"contact_id": CONTACT_RAHUL}],
            "attachments": [{
                "type": "md", "filename": "Follow_up_Email.md",
                "content_base64": base64.b64encode(
                    b"# Follow-up\n\nThanks all.").decode()}]})
        part = [p for p in self.last_message().walk()
                if p.get_filename() == "Follow_up_Email.md"][0]
        self.assertEqual(part.get_content_type(), "text/markdown")
        self.assertIn(b"Thanks all", part.get_payload(decode=True))

    def test_outputs_go_to_several_recipients_at_once(self):
        self.connect_gmail()
        b64 = base64.b64encode(b"data").decode()
        data = self.send_meeting({
            "recipients": [{"contact_id": CONTACT_RAHUL},
                           {"contact_id": CONTACT_NEHA}],
            "attachments": [
                {"type": "pdf", "filename": "Minutes.pdf", "content_base64": b64},
                {"type": "docx", "filename": "Summary.docx", "content_base64": b64}]})
        self.assertEqual(data["recipient_count"], 2)
        msg = self.last_message()
        self.assertIn("rahul@example.com", msg["To"])
        self.assertIn("neha@example.com", msg["To"])
        # ONE message carrying both files, not one message per attachment.
        self.assertEqual(len(self.sent), 1)

    def test_more_outputs_than_the_cap_are_refused(self):
        """The ceiling the share sheet mirrors client-side. Refused as a whole
        — a partial send would drop documents the user explicitly ticked."""
        self.connect_gmail()
        b64 = base64.b64encode(b"data").decode()
        data = self.send_meeting({
            "recipients": [{"contact_id": CONTACT_RAHUL}],
            "attachments": [
                {"type": "pdf", "filename": f"Doc{i}.pdf", "content_base64": b64}
                for i in range(email_message.MAX_ATTACHMENTS + 1)]},
            expect=400)
        self.assertIn("at most", data["error"])
        self.assertEqual(self.sent, [])

    def test_an_output_with_a_disguised_type_is_refused(self):
        """Attachment bytes come from the client and are untrusted. Only the
        four MinuteX output formats may be sent."""
        self.connect_gmail()
        data = self.send_meeting({
            "recipients": [{"contact_id": CONTACT_RAHUL}],
            "attachments": [{"type": "exe", "filename": "payload.exe",
                             "content_base64": base64.b64encode(b"MZ").decode()}]},
            expect=400)
        self.assertIn("PDF, DOCX", data["error"])
        self.assertEqual(self.sent, [])

    def test_sharing_outputs_still_requires_gmail(self):
        """The generic path must be gated exactly like the old MoM one."""
        b64 = base64.b64encode(b"data").decode()
        data = self.send_meeting({
            "recipients": [{"contact_id": CONTACT_RAHUL}],
            "attachments": [
                {"type": "pdf", "filename": "Minutes.pdf", "content_base64": b64}]},
            expect=api.INTEGRATION_STATUS_CODE)
        self.assertEqual(data["code"], api.INTEGRATION_NOT_CONNECTED_CODE)
        self.assertEqual(self.sent, [])

    def test_a_message_with_no_outputs_still_sends(self):
        """Sharing nothing but a note is legitimate follow-up communication."""
        self.connect_gmail()
        data = self.send_meeting({
            "recipients": [{"contact_id": CONTACT_RAHUL}],
            "subject": "Following up", "body": "Quick note, no attachments."})
        self.assertTrue(data["sent"])
        self.assertEqual(self.last_message().get_content_type(),
                         "multipart/alternative")

    def test_task_email_defaults_to_the_assignee(self):
        self.connect_gmail()
        self.tasks.items[("t-1",)] = {
            "task_id": "t-1", "owner_user_id": OWNER,
            "title": "Complete OAuth integration",
            "assignee_contact_id": CONTACT_RAHUL,
            "due_date": "2026-09-04", "priority": "High"}
        data = parse(call(
            api.gmail_send_task,
            event("POST", "/integrations/gmail/send/task/{task_id}",
                  body={}, params={"task_id": "t-1"})))[1]
        self.assertTrue(data["sent"])
        msg = self.last_message()
        self.assertIn("rahul@example.com", msg["To"])
        self.assertIn("Complete OAuth integration", self.last_subject())
        body = msg.get_payload(0).get_payload(decode=True).decode()
        self.assertIn("Complete OAuth integration", body)
        self.assertIn("2026-09-04", body)

    def test_task_email_omits_fields_the_task_does_not_have(self):
        """Never invent a deadline. An empty 'Due:' line invites the reader to
        infer one that was never set."""
        self.connect_gmail()
        self.tasks.items[("t-2",)] = {
            "task_id": "t-2", "owner_user_id": OWNER, "title": "Bare task",
            "assignee_contact_id": CONTACT_RAHUL}
        call(api.gmail_send_task,
             event("POST", "/integrations/gmail/send/task/{task_id}",
                   body={}, params={"task_id": "t-2"}))
        body = self.last_message().get_payload(0).get_payload(decode=True).decode()
        self.assertNotIn("Due:", body)
        self.assertNotIn("Details:", body)
        self.assertIn("Bare task", body)

    def test_task_email_without_an_assignee_is_refused(self):
        self.connect_gmail()
        self.tasks.items[("t-3",)] = {
            "task_id": "t-3", "owner_user_id": OWNER, "title": "Unassigned"}
        status, data = parse(call(
            api.gmail_send_task,
            event("POST", "/integrations/gmail/send/task/{task_id}",
                  body={}, params={"task_id": "t-3"})))
        self.assertEqual(status, 422, data)
        self.assertEqual(self.sent, [])

    def test_the_access_token_is_refreshed_for_every_send(self):
        """Access tokens are never stored — one cheap refresh per request keeps
        exactly one durable secret per user instead of two."""
        self.connect_gmail()
        self.send_meeting({"recipients": [{"contact_id": CONTACT_RAHUL}]})
        self.assertEqual(self.refreshed, ["enc-refresh-token"])

    def test_a_rejected_access_token_is_refreshed_and_retried_once(self):
        self.connect_gmail()
        calls = {"n": 0}

        def flaky(access_token, raw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise api.GmailAuthExpired("401")
            self.sent.append(raw)
            return {"id": "msg-2", "threadId": "t"}

        with mock.patch.object(api._gmail_provider, "send_message",
                               side_effect=flaky):
            data = self.send_meeting({"recipients": [{"contact_id": CONTACT_RAHUL}]})
        self.assertTrue(data["sent"])
        self.assertEqual(calls["n"], 2)
        self.assertEqual(len(self.refreshed), 2)

    def test_a_dead_refresh_token_marks_the_connection_and_refuses(self):
        """The transition that makes the backend the source of truth: the next
        status read must be honest WITHOUT another failed send."""
        self.connect_gmail()
        self.refresh_result = api.IntegrationReauthRequired(GMAIL)
        self.send_meeting({"recipients": [{"contact_id": CONTACT_RAHUL}]},
                          expect=api.INTEGRATION_STATUS_CODE)
        self.assertEqual(self.integrations.items[(OWNER, GMAIL)]["status"],
                         integrations.STATUS_REAUTH_REQUIRED)

        _, data = parse(call(
            api.get_integration,
            event("GET", "/integrations/{provider}", params={"provider": GMAIL})))
        self.assertEqual(data["integration"]["status"],
                         integrations.STATUS_REAUTH_REQUIRED)
        self.assertFalse(data["integration"]["connected"])

    def test_a_gmail_api_failure_surfaces_readable_wording(self):
        """'invalid_grant: 400' is not an error message a person can act on."""
        self.connect_gmail()
        self.send_result = api.ApiError(
            429, "Gmail is rate-limiting this account. Wait a moment and try again.")
        data = self.send_meeting({"recipients": [{"contact_id": CONTACT_RAHUL}]},
                                 expect=429)
        self.assertIn("rate-limiting", data["error"])
        self.assertNotIn("429", data["error"])

    def test_a_network_failure_does_not_mark_the_connection_dead(self):
        """A transient outage must not push a healthy connection into
        REAUTH_REQUIRED and make the user reconnect for nothing."""
        self.connect_gmail()
        self.send_result = api.ApiError(502, "Could not reach Gmail.")
        self.send_meeting({"recipients": [{"contact_id": CONTACT_RAHUL}]},
                          expect=502)
        self.assertEqual(self.integrations.items[(OWNER, GMAIL)]["status"],
                         integrations.STATUS_CONNECTED)


# ---------------------------------------------------------------------------
# THE MESSAGE BUILDER — untrusted input.
# ---------------------------------------------------------------------------
class MessageBuilderTests(unittest.TestCase):

    def build(self, **kw):
        kw.setdefault("sender", "me@gmail.com")
        kw.setdefault("sender_name", "Me")
        kw.setdefault("to", ["a@b.com"])
        kw.setdefault("subject", "Subject")
        kw.setdefault("body", "Body")
        return message_from_bytes(
            base64.urlsafe_b64decode(email_message.build_message(**kw)))

    def test_a_newline_in_the_subject_cannot_inject_a_header(self):
        """Meeting titles come from real speech and end up in the subject. A
        crafted one must not be able to add its own Bcc."""
        msg = self.build(subject="Hello\r\nBcc: victim@evil.com")
        self.assertIsNone(msg["Bcc"])
        self.assertNotIn("\n", msg["Subject"])

    def test_a_non_ascii_subject_is_encoded_not_mangled(self):
        msg = self.build(subject="Réunion — 会議")
        self.assertEqual(str(make_header(decode_header(msg["Subject"]))),
                         "Réunion — 会議")

    def test_html_in_the_body_is_escaped(self):
        html = email_message.text_to_html("<script>alert(1)</script>")
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_an_unknown_attachment_type_is_refused(self):
        with self.assertRaises(email_message.EmailError):
            email_message.decode_attachment(
                {"type": "exe", "filename": "x.exe",
                 "content_base64": base64.b64encode(b"MZ").decode()})

    def test_an_attachment_filename_cannot_traverse(self):
        att = email_message.decode_attachment(
            {"type": "pdf", "filename": "../../etc/passwd",
             "content_base64": base64.b64encode(b"x").decode()})
        self.assertNotIn("/", att["filename"])
        self.assertNotIn("..", att["filename"])
        self.assertTrue(att["filename"].endswith(".pdf"))

    def test_an_attachment_extension_is_enforced_not_trusted(self):
        """A '.pdf' label on DOCX bytes is a mislabelled file in an inbox."""
        att = email_message.decode_attachment(
            {"type": "docx", "filename": "report.pdf",
             "content_base64": base64.b64encode(b"PK").decode()})
        self.assertTrue(att["filename"].endswith(".docx"))

    def test_an_oversized_attachment_is_refused(self):
        big = base64.b64encode(
            b"x" * (email_message.MAX_ATTACHMENT_BYTES + 1)).decode()
        with self.assertRaises(email_message.EmailError):
            email_message.decode_attachment(
                {"type": "pdf", "filename": "big.pdf", "content_base64": big})

    def test_too_many_attachments_are_refused(self):
        one = {"type": "pdf", "filename": "a.pdf",
               "content_base64": base64.b64encode(b"x").decode()}
        with self.assertRaises(email_message.EmailError):
            email_message.decode_attachments(
                [one] * (email_message.MAX_ATTACHMENTS + 1))

    def test_a_message_without_attachments_is_not_multipart_mixed(self):
        self.assertEqual(self.build().get_content_type(),
                         "multipart/alternative")

    def test_an_empty_recipient_list_is_refused(self):
        with self.assertRaises(email_message.EmailError):
            email_message.build_message(
                sender="a@b.com", sender_name="", to=[], subject="s", body="b")

    def test_an_empty_subject_is_refused(self):
        with self.assertRaises(email_message.EmailError):
            email_message.build_message(
                sender="a@b.com", sender_name="", to=["c@d.com"],
                subject="   ", body="b")

    def test_too_many_recipients_are_refused(self):
        many = [f"u{i}@x.com" for i in range(email_message.MAX_RECIPIENTS + 1)]
        with self.assertRaises(email_message.EmailError):
            email_message.build_message(
                sender="a@b.com", sender_name="", to=many, subject="s", body="b")

    def test_cc_appears_on_the_message(self):
        msg = self.build(cc=["cc@x.com"])
        self.assertIn("cc@x.com", msg["Cc"])

    def test_an_address_with_a_display_name_is_accepted(self):
        """Contacts imported from a phone address book sometimes carry it."""
        self.assertEqual(
            email_message.normalize_email("Rahul Sharma <R@Example.com>"),
            "r@example.com")

    def test_an_invalid_address_normalizes_to_empty(self):
        for bad in ("", "not-an-email", "a@b", "@b.com", None):
            self.assertEqual(email_message.normalize_email(bad), "")

    def test_a_name_with_a_comma_is_quoted(self):
        """Otherwise the comma splits one recipient into two."""
        formatted = email_message.format_recipient("Sharma, Rahul",
                                                   "r@example.com")
        msg = self.build(to=[formatted])
        self.assertEqual(len(msg["To"].split("<")) - 1, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
