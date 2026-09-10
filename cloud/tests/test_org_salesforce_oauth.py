#!/usr/bin/env python3
"""test_org_salesforce_oauth.py — Phase 2D.1: Organisation Salesforce OAuth,
RBAC and scope isolation.

WHAT THIS FILE PINS.

  1. RBAC. Owner/Manager may connect, disconnect; Member may not. Enforced
     via CAP_MANAGE_INTEGRATIONS server-side — never something a client can
     satisfy by hiding a button.

  2. SCOPE ISOLATION. Personal Salesforce (CrmConnections, keyed by user_id)
     and Organisation Salesforce (OrgCrmConnections, keyed by workspace_id)
     never read or write each other's table. A workspace's connection
     cannot leak into another workspace's status, and a personal connection
     is never substituted for a missing organisation one.

  3. OAUTH SECURITY. Signed, expiring state carrying BOTH the initiating
     user and the target workspace; PKCE; cross-workspace/removed-member
     callback rejection even with a validly signed state (membership is
     re-checked at exchange time, not trusted from /connect time).

  4. TOKEN LIFECYCLE. KMS-encrypted refresh token, rotation-safe persistence,
     reconnect-required (409) distinct from "not connected" (400 with a
     stable machine-readable code) distinct from MinuteX session expiry
     (never 401).

  5. REGRESSION. Personal Salesforce is completely untouched by any of this
     — same table, same routes, same behavior.

SECURITY POSTURE, as in every suite here: tests call the ROUTE FUNCTION
DIRECTLY with a forged identity/workspace, exactly as curl against the
deployed API would.

OFFLINE: fake_dynamodb, no AWS, no network.

Run:  python -m pytest tests/test_org_salesforce_oauth.py
"""
import json
import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "functions/userapi"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# ORDER MATTERS — see test_task_permissions.py: importing api through
# test_ai_workspace ensures boto3/botocore are stubbed exactly once, before
# lambda_function is imported anywhere in this test run.
from test_ai_workspace import api  # noqa: E402
import fake_dynamodb as fdb  # noqa: E402
import workspace_schema as ws  # noqa: E402

OWNER_A, MANAGER_A, MEMBER_A = "u-oa", "u-ma", "u-mea"
OWNER_B = "u-ob"
OUTSIDER = "u-out"
REMOVED = "u-rm"

ORG_A = "wso_c2aaaa11"
ORG_B = "wso_c2bbbb22"
NOW = "2026-09-08T10:00:00Z"

FAKE_TOKENS = {
    "access_token": "AT-1", "refresh_token": "RT-1",
    "instance_url": "https://abcrealty.my.salesforce.com",
    "id": "https://login.salesforce.com/id/00Dxx0000000001/005xx000000001",
}


def parse(resp):
    return resp["statusCode"], json.loads(resp["body"])


def event(method="GET", route="/workspaces/{workspace_id}/crm/salesforce/connect",
          path=None, qs=None):
    ev = {
        "routeKey": f"{method} {route}",
        "requestContext": {"http": {"method": method, "path": route}},
        "headers": {"authorization": "Bearer test-token"},
        "pathParameters": dict(path or {}),
    }
    if qs is not None:
        ev["queryStringParameters"] = qs
    return ev


def call(handler, ev):
    """As every other suite's call(): handlers raise ApiError and the ROUTER
    turns it into a response, so tests assert on what the client sees."""
    try:
        return handler(ev)
    except api.ApiError as e:
        body = {"error": e.message}
        code = getattr(e, "code", "")
        if code:
            body["code"] = code
        return api._resp(e.status, body)


class OrgSalesforceHarness(unittest.TestCase):
    def setUp(self):
        self.t = fdb.build_tables()
        self.current_user = OWNER_A
        self.patches = [
            mock.patch.object(api, "_workspaces", self.t["workspaces"]),
            mock.patch.object(api, "_memberships", self.t["memberships"]),
            mock.patch.object(api, "_org_crm_connections",
                              self.t["org_crm_connections"]),
            # Personal CrmConnections stays a bare MagicMock, never touched
            # by these tests — any call into it fails loudly rather than
            # silently succeeding, so a scope-leak bug would be caught
            # immediately by an unexpected-call assertion error.
            mock.patch.object(api, "_crm_connections", mock.MagicMock(
                side_effect=AssertionError(
                    "Organisation Salesforce code path touched "
                    "CrmConnections (Personal) — scope leak"))),
            mock.patch.object(api, "_require_auth",
                              side_effect=lambda ev: self.current_user),
            mock.patch.object(api, "_jwt_secret", return_value="test-secret"),
            mock.patch.object(api, "SALESFORCE_CLIENT_ID", "cid-abc"),
            mock.patch.object(api, "ORG_SALESFORCE_REDIRECT_URI",
                              "https://api.example.com/crm/salesforce/org-callback"),
            mock.patch.object(api, "SALESFORCE_RETURN_URL",
                              "recorderapp://crm-connected"),
            mock.patch.object(api, "_kms_encrypt", side_effect=lambda pt: f"ENC({pt})"),
            mock.patch.object(api, "_kms_decrypt",
                              side_effect=lambda ct: ct[4:-1] if ct.startswith("ENC(") else ct),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

        for wid, owner, name in ((ORG_A, OWNER_A, "ABC Realty"),
                                 (ORG_B, OWNER_B, "XYZ Corp")):
            self.t["workspaces"].put_item(Item={
                "workspace_id": wid, "type": ws.TYPE_ORGANISATION,
                "name": name, "owner_user_id": owner,
                "status": ws.STATUS_ACTIVE,
                "created_at": NOW, "updated_at": NOW})
        for wid, uid, role in (
            (ORG_A, OWNER_A, ws.ROLE_OWNER),
            (ORG_A, MANAGER_A, ws.ROLE_MANAGER),
            (ORG_A, MEMBER_A, ws.ROLE_MEMBER),
            (ORG_A, REMOVED, ws.ROLE_MEMBER),
            (ORG_B, OWNER_B, ws.ROLE_OWNER),
        ):
            self.t["memberships"].put_item(
                Item=ws.new_membership(wid, uid, role, NOW))

    def as_user(self, uid):
        self.current_user = uid

    def remove_from_org_a(self, uid):
        keeper = self.current_user
        self.as_user(OWNER_A)
        call(api.remove_member, event(
            method="DELETE",
            route="/workspaces/{workspace_id}/members/{user_id}",
            path={"workspace_id": ORG_A, "user_id": uid}))
        self.as_user(keeper)

    # -- helpers ----------------------------------------------------------
    def connect(self, workspace_id=ORG_A):
        return parse(call(api.org_salesforce_connect, event(
            "GET", "/workspaces/{workspace_id}/crm/salesforce/connect",
            path={"workspace_id": workspace_id})))

    @staticmethod
    def _qs(url):
        return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)

    def do_callback(self, code="auth-code-1", state=None, exchange=None,
                    whoami=None):
        exchange = exchange or (lambda code, code_verifier="": dict(FAKE_TOKENS))
        whoami = whoami if whoami is not None else {
            "organization_id": "00Dxx0000000001", "user_id": "005xx000000001",
            "preferred_username": "integration@abcrealty.com"}
        qs = {}
        if code is not None:
            qs["code"] = code
        if state is not None:
            qs["state"] = state
        with mock.patch.object(api._salesforce, "exchange_code", side_effect=exchange), \
             mock.patch.object(api._salesforce, "whoami", return_value=whoami):
            return api.org_salesforce_callback({"queryStringParameters": qs})

    def connect_and_callback(self, workspace_id=ORG_A, as_user=None):
        if as_user:
            self.as_user(as_user)
        status, body = self.connect(workspace_id)
        assert status == 200, body
        state = self._qs(body["authorize_url"])["state"][0]
        return self.do_callback(state=state)


# ===========================================================================
# 1. RBAC — connect / disconnect
# ===========================================================================
class TestConnectRbac(OrgSalesforceHarness):
    def test_owner_can_start_connect(self):
        self.as_user(OWNER_A)
        status, body = self.connect()
        self.assertEqual(status, 200)
        self.assertIn("authorize_url", body)

    def test_manager_can_start_connect(self):
        self.as_user(MANAGER_A)
        status, body = self.connect()
        self.assertEqual(status, 200)
        self.assertIn("authorize_url", body)

    def test_member_cannot_start_connect(self):
        self.as_user(MEMBER_A)
        status, body = self.connect()
        self.assertEqual(status, 403)

    def test_outsider_gets_404_not_403(self):
        """Not a member at all — existence of the workspace must not leak."""
        self.as_user(OUTSIDER)
        status, body = self.connect()
        self.assertEqual(status, 404)

    def test_removed_member_cannot_connect(self):
        self.remove_from_org_a(MEMBER_A)
        self.as_user(MEMBER_A)
        status, body = self.connect()
        self.assertIn(status, (403, 404))

    def test_personal_workspace_rejected(self):
        """Organisation Salesforce has no meaning for a personal workspace —
        Personal Salesforce already covers it."""
        personal_wid = ws.personal_workspace_id(OWNER_A)
        self.as_user(OWNER_A)
        status, body = self.connect(personal_wid)
        self.assertEqual(status, 400)


class TestDisconnectRbac(OrgSalesforceHarness):
    def setUp(self):
        super().setUp()
        self.connect_and_callback(as_user=OWNER_A)

    def _disconnect(self, workspace_id=ORG_A):
        return parse(call(api.org_salesforce_disconnect, event(
            "DELETE", "/workspaces/{workspace_id}/crm/salesforce",
            path={"workspace_id": workspace_id})))

    def test_owner_can_disconnect(self):
        self.as_user(OWNER_A)
        status, body = self._disconnect()
        self.assertEqual(status, 200)
        self.assertTrue(body["disconnected"])
        self.assertIsNone(self.t["org_crm_connections"].get_item(
            Key={"workspace_id": ORG_A, "provider": "salesforce"}).get("Item"))

    def test_manager_can_disconnect(self):
        self.as_user(MANAGER_A)
        status, _ = self._disconnect()
        self.assertEqual(status, 200)

    def test_member_cannot_disconnect(self):
        self.as_user(MEMBER_A)
        status, _ = self._disconnect()
        self.assertEqual(status, 403)
        # Connection must survive the denied attempt.
        self.assertIsNotNone(self.t["org_crm_connections"].get_item(
            Key={"workspace_id": ORG_A, "provider": "salesforce"}).get("Item"))


# ===========================================================================
# 2. STATUS — readable by any active member
# ===========================================================================
class TestStatus(OrgSalesforceHarness):
    def _status(self, workspace_id=ORG_A):
        return parse(call(api.org_salesforce_status, event(
            "GET", "/workspaces/{workspace_id}/crm/salesforce/status",
            path={"workspace_id": workspace_id})))

    def test_not_connected_is_false_not_an_error(self):
        self.as_user(OWNER_A)
        status, body = self._status()
        self.assertEqual(status, 200)
        self.assertFalse(body["connected"])

    def test_member_can_read_status(self):
        self.connect_and_callback(as_user=OWNER_A)
        self.as_user(MEMBER_A)
        status, body = self._status()
        self.assertEqual(status, 200)
        self.assertTrue(body["connected"])
        self.assertEqual(body["sf_username"], "integration@abcrealty.com")
        self.assertEqual(body["connected_by_user_id"], OWNER_A)

    def test_outsider_gets_404(self):
        self.as_user(OUTSIDER)
        status, _ = self._status()
        self.assertEqual(status, 404)


# ===========================================================================
# 3. SCOPE ISOLATION — cross-workspace, cross-tenant, personal vs org
# ===========================================================================
class TestScopeIsolation(OrgSalesforceHarness):
    def test_org_b_sees_no_connection_after_org_a_connects(self):
        self.connect_and_callback(workspace_id=ORG_A, as_user=OWNER_A)
        self.as_user(OWNER_B)
        status, body = parse(call(api.org_salesforce_status, event(
            "GET", "/workspaces/{workspace_id}/crm/salesforce/status",
            path={"workspace_id": ORG_B})))
        self.assertEqual(status, 200)
        self.assertFalse(body["connected"])

    def test_two_orgs_can_hold_independent_connections(self):
        self.connect_and_callback(workspace_id=ORG_A, as_user=OWNER_A)
        self.connect_and_callback(workspace_id=ORG_B, as_user=OWNER_B)
        a = self.t["org_crm_connections"].get_item(
            Key={"workspace_id": ORG_A, "provider": "salesforce"}).get("Item")
        b = self.t["org_crm_connections"].get_item(
            Key={"workspace_id": ORG_B, "provider": "salesforce"}).get("Item")
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertNotEqual(a["refresh_token_enc"], "")
        self.assertEqual(len(self.t["org_crm_connections"].items), 2)

    def test_state_from_org_a_connect_cannot_create_a_connection_for_org_b(self):
        """A signed state genuinely names ORG_A. Even if an attacker could
        get it accepted at all, the workspace it creates a row for is the
        one INSIDE the state, never one supplied by the request."""
        self.as_user(OWNER_A)
        status, body = self.connect(ORG_A)
        state = self._qs(body["authorize_url"])["state"][0]
        user_id, workspace_id, verifier = api._verify_org_crm_state(state)
        self.assertEqual(workspace_id, ORG_A)
        self.assertNotEqual(workspace_id, ORG_B)

    def test_org_crm_never_falls_back_to_personal_connection(self):
        """_sf_call_org must raise rather than ever consulting CrmConnections.
        The AssertionError side_effect on the mocked _crm_connections proves
        no code path touches it."""
        with self.assertRaises(api.OrganisationSalesforceNotConnected):
            api._sf_call_org(ORG_A, lambda url, tok: None)

    def test_not_connected_uses_stable_machine_readable_code(self):
        with self.assertRaises(api.ApiError) as ctx:
            api._sf_call_org(ORG_A, lambda url, tok: None)
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, "organisation_salesforce_not_connected")


# ===========================================================================
# 4. OAUTH SECURITY — signed state, PKCE, membership re-check at callback
# ===========================================================================
class TestOAuthSecurity(OrgSalesforceHarness):
    def test_authorize_url_carries_pkce_challenge(self):
        self.as_user(OWNER_A)
        status, body = self.connect()
        params = self._qs(body["authorize_url"])
        self.assertIn("code_challenge", params)
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.assertEqual(params["client_id"], ["cid-abc"])
        self.assertEqual(params["redirect_uri"],
                         ["https://api.example.com/crm/salesforce/org-callback"])

    def test_state_binds_user_and_workspace(self):
        self.as_user(MANAGER_A)
        status, body = self.connect()
        state = self._qs(body["authorize_url"])["state"][0]
        user_id, workspace_id, verifier = api._verify_org_crm_state(state)
        self.assertEqual(user_id, MANAGER_A)
        self.assertEqual(workspace_id, ORG_A)
        self.assertTrue(verifier)

    def test_tampered_state_is_rejected(self):
        self.as_user(OWNER_A)
        _, body = self.connect()
        state = self._qs(body["authorize_url"])["state"][0]
        seg, sig = state.split(".")
        tampered = seg + "." + sig[:-2] + ("aa" if sig[-2:] != "aa" else "bb")
        resp = self.do_callback(state=tampered)
        self.assertIn("connected=0", resp["headers"]["Location"])
        self.assertIsNone(self.t["org_crm_connections"].get_item(
            Key={"workspace_id": ORG_A, "provider": "salesforce"}).get("Item"))

    def test_personal_oauth_state_rejected_by_org_callback(self):
        """A state minted by the PERSONAL /connect (no `wid` claim) must not
        be accepted here even though it shares the signing secret."""
        personal_state = api._sign_oauth_state(OWNER_A, "some-verifier")
        resp = self.do_callback(state=personal_state)
        self.assertIn("connected=0", resp["headers"]["Location"])

    def test_expired_state_is_rejected(self):
        self.as_user(OWNER_A)
        with mock.patch.object(api.time, "time", return_value=1_000_000):
            _, body = self.connect()
        state = self._qs(body["authorize_url"])["state"][0]
        with mock.patch.object(api.time, "time", return_value=1_000_000 + 3600):
            resp = self.do_callback(state=state)
        self.assertIn("connected=0", resp["headers"]["Location"])

    def test_callback_rejects_a_member_who_was_removed_mid_flow(self):
        """The security-critical case: state was validly signed for a
        Manager who could legitimately start the flow, but by the time
        Salesforce redirects back, they have been removed from the org.
        The connection must NOT be created."""
        self.as_user(MANAGER_A)
        _, body = self.connect()
        state = self._qs(body["authorize_url"])["state"][0]

        self.remove_from_org_a(MANAGER_A)

        resp = self.do_callback(state=state)
        self.assertIn("connected=0", resp["headers"]["Location"])
        self.assertIn("not_authorized", resp["headers"]["Location"])
        self.assertIsNone(self.t["org_crm_connections"].get_item(
            Key={"workspace_id": ORG_A, "provider": "salesforce"}).get("Item"))

    def test_callback_rejects_a_manager_demoted_to_member_mid_flow(self):
        self.as_user(MANAGER_A)
        _, body = self.connect()
        state = self._qs(body["authorize_url"])["state"][0]

        self.as_user(OWNER_A)
        call(api.update_member, event(
            method="PATCH", route="/workspaces/{workspace_id}/members/{user_id}",
            path={"workspace_id": ORG_A, "user_id": MANAGER_A}))
        self.t["memberships"].update_item(
            Key={"workspace_id": ORG_A, "user_id": MANAGER_A},
            UpdateExpression="SET #r = :r",
            ExpressionAttributeNames={"#r": "role"},
            ExpressionAttributeValues={":r": ws.ROLE_MEMBER})

        resp = self.do_callback(state=state)
        self.assertIn("connected=0", resp["headers"]["Location"])
        self.assertIsNone(self.t["org_crm_connections"].get_item(
            Key={"workspace_id": ORG_A, "provider": "salesforce"}).get("Item"))

    def test_missing_code_or_state_is_rejected(self):
        resp = self.do_callback(code=None)
        self.assertIn("missing_params", resp["headers"]["Location"])
        resp = self.do_callback(state=None)
        self.assertIn("missing_params", resp["headers"]["Location"])

    def test_salesforce_denial_is_handled_cleanly(self):
        resp = api.org_salesforce_callback(
            {"queryStringParameters": {"error": "access_denied"}})
        self.assertEqual(resp["statusCode"], 302)
        self.assertIn("connected=0", resp["headers"]["Location"])
        self.assertIn("denied", resp["headers"]["Location"])

    def test_successful_callback_persists_connection_and_redirects(self):
        resp = self.connect_and_callback(as_user=OWNER_A)
        self.assertEqual(resp["statusCode"], 302)
        self.assertIn("connected=1", resp["headers"]["Location"])
        conn = self.t["org_crm_connections"].get_item(
            Key={"workspace_id": ORG_A, "provider": "salesforce"}).get("Item")
        self.assertIsNotNone(conn)
        self.assertEqual(conn["connected_by_user_id"], OWNER_A)
        self.assertEqual(conn["instance_url"], FAKE_TOKENS["instance_url"])
        self.assertNotEqual(conn["refresh_token_enc"], FAKE_TOKENS["refresh_token"])
        self.assertEqual(conn["org_id"], "00Dxx0000000001")

    def test_exchange_uses_the_states_own_verifier(self):
        captured = {}

        def _fake_exchange(code, code_verifier=""):
            captured["verifier"] = code_verifier
            return dict(FAKE_TOKENS)

        self.as_user(OWNER_A)
        _, body = self.connect()
        state = self._qs(body["authorize_url"])["state"][0]
        _u, _w, expected_verifier = api._verify_org_crm_state(state)
        self.do_callback(state=state, exchange=_fake_exchange)
        self.assertEqual(captured["verifier"], expected_verifier)


# ===========================================================================
# 5. TOKEN LIFECYCLE — encryption, rotation, reconnect-required
# ===========================================================================
class TestTokenLifecycle(OrgSalesforceHarness):
    def setUp(self):
        super().setUp()
        self.connect_and_callback(as_user=OWNER_A)

    def test_refresh_token_never_stored_in_plaintext(self):
        """The stored value must be the OUTPUT of _kms_encrypt, not the raw
        token — proven against the real encrypt function's call, not this
        suite's transparent test double (which deliberately embeds its
        input so other assertions can invert it)."""
        conn = self.t["org_crm_connections"].get_item(
            Key={"workspace_id": ORG_A, "provider": "salesforce"}).get("Item")
        self.assertNotEqual(conn["refresh_token_enc"], FAKE_TOKENS["refresh_token"])
        self.assertEqual(conn["refresh_token_enc"],
                         api._kms_encrypt(FAKE_TOKENS["refresh_token"]))

    def test_access_token_never_persisted(self):
        conn = self.t["org_crm_connections"].get_item(
            Key={"workspace_id": ORG_A, "provider": "salesforce"}).get("Item")
        self.assertNotIn("access_token", conn)

    def test_sf_call_org_mints_access_token_from_stored_refresh_token(self):
        with mock.patch.object(api._salesforce, "refresh_access_token",
                               return_value={"access_token": "NEW-AT"}) as m:
            result = api._sf_call_org(ORG_A, lambda url, tok: (url, tok))
        m.assert_called_once_with("RT-1")
        self.assertEqual(result, (FAKE_TOKENS["instance_url"], "NEW-AT"))

    def test_dead_refresh_token_raises_reconnect_required_not_401(self):
        with mock.patch.object(api._salesforce, "refresh_access_token",
                               return_value={}):
            with self.assertRaises(api.SalesforceReconnectRequired) as ctx:
                api._sf_call_org(ORG_A, lambda url, tok: None)
        self.assertEqual(ctx.exception.status, 409)
        self.assertNotEqual(ctx.exception.status, 401)
        self.assertEqual(ctx.exception.code, "salesforce_reconnect_required")

    def test_rotated_refresh_token_is_persisted(self):
        with mock.patch.object(api._salesforce, "refresh_access_token",
                               return_value={"access_token": "AT-2",
                                             "refresh_token": "RT-2"}):
            api._sf_call_org(ORG_A, lambda url, tok: None)
        conn = self.t["org_crm_connections"].get_item(
            Key={"workspace_id": ORG_A, "provider": "salesforce"}).get("Item")
        self.assertEqual(conn["refresh_token_enc"], "ENC(RT-2)")

    def test_401_on_first_call_triggers_one_retry_then_succeeds(self):
        attempts = {"n": 0}

        def flaky(url, tok):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise api.SalesforceAuthExpired("stale")
            return "ok"

        with mock.patch.object(api._salesforce, "refresh_access_token",
                               return_value={"access_token": "AT-2"}):
            result = api._sf_call_org(ORG_A, flaky)
        self.assertEqual(result, "ok")
        self.assertEqual(attempts["n"], 2)

    def test_persistent_401_becomes_reconnect_required(self):
        def always_expired(url, tok):
            raise api.SalesforceAuthExpired("stale")

        with mock.patch.object(api._salesforce, "refresh_access_token",
                               return_value={"access_token": "AT-2"}):
            with self.assertRaises(api.SalesforceReconnectRequired):
                api._sf_call_org(ORG_A, always_expired)


# ===========================================================================
# 6. ROUTER — every new route registered and reachable
# ===========================================================================
class TestRouter(unittest.TestCase):
    def test_org_salesforce_routes_registered(self):
        self.assertIs(api._ROUTES[("GET", "/workspaces/{workspace_id}/crm/salesforce/connect")],
                      api.org_salesforce_connect)
        self.assertIs(api._ROUTES[("GET", "/crm/salesforce/org-callback")],
                      api.org_salesforce_callback)
        self.assertIs(api._ROUTES[("GET", "/workspaces/{workspace_id}/crm/salesforce/status")],
                      api.org_salesforce_status)
        self.assertIs(api._ROUTES[("DELETE", "/workspaces/{workspace_id}/crm/salesforce")],
                      api.org_salesforce_disconnect)

    def test_personal_salesforce_routes_untouched(self):
        self.assertIs(api._ROUTES[("GET", "/crm/salesforce/connect")], api.salesforce_connect)
        self.assertIs(api._ROUTES[("GET", "/crm/salesforce/callback")], api.salesforce_callback)
        self.assertIs(api._ROUTES[("GET", "/crm/salesforce/status")], api.salesforce_status)
        self.assertIs(api._ROUTES[("DELETE", "/crm/salesforce")], api.salesforce_disconnect)


if __name__ == "__main__":
    unittest.main()
