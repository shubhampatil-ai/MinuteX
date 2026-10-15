#!/usr/bin/env python3
"""test_salesforce_oauth_connection.py — connecting a workspace to its
Salesforce org through an integration user (OAuth web-server flow + PKCE).

Pins: the module's safety checks (Salesforce hosts only, org/user from the id
URL, signature when a secret is set, My Domain shapes); the connect URL
(PKCE S256, prompt, scopes) and its signed state; the callback storing a
KMS-encrypted refresh token and redirecting to the app; permissions checked
again at the callback; access tokens minted from the refresh token and
cached; a refused refresh marking the connection "reconnect needed" with a
409 (never 401); disconnect revoking and deleting; the flag.

Run:  python -m pytest tests/test_salesforce_oauth_connection.py
"""
import base64
import hashlib
import hmac
import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "functions/userapi"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from salesforce_harness import MEMBER, MANAGER, ORG, OWNER, SalesforceHarness, call, event, worker  # noqa: E402
import fake_dynamodb as fdb  # noqa: E402
import salesforce_oauth as so  # noqa: E402
import webhook_client as whc  # noqa: E402

api = worker.userapi
ORG_ID = "00D5g00000ABCDEEAY"
USER_ID = "0055g00000INTEGRAQ"
INSTANCE = "https://acme.my.salesforce.com"
RETURN = "recorderapp://integrations-connected"


def token_body(refresh="rt-1", org=ORG_ID, instance=INSTANCE, secret=""):
    body = {"access_token": "at-1", "refresh_token": refresh, "instance_url": instance,
            "id": f"https://login.salesforce.com/id/{org}/{USER_ID}",
            "issued_at": "1790000000000", "token_type": "Bearer"}
    if not refresh:
        body.pop("refresh_token")
    if secret:
        mac = hmac.new(secret.encode(), (body["id"] + body["issued_at"]).encode(),
                       hashlib.sha256).digest()
        body["signature"] = base64.b64encode(mac).decode()
    return body


class ModuleTests(unittest.TestCase):
    def test_only_salesforce_hosts(self):
        for good in ("https://login.salesforce.com", "https://acme.my.salesforce.com",
                     "https://acme--dev.sandbox.my.salesforce.com", "https://x.force.com"):
            self.assertTrue(so.is_salesforce_url(good), good)
        for bad in ("http://login.salesforce.com", "https://salesforce.com.evil.io",
                    "https://evil.io/x.salesforce.com", "https://u:p@acme.my.salesforce.com",
                    "https://acme.my.salesforce.com:8443", ""):
            self.assertFalse(so.is_salesforce_url(bad), bad)

    def test_login_hosts(self):
        self.assertEqual(so.login_host("production"), so.LOGIN_PRODUCTION)
        self.assertEqual(so.login_host("sandbox"), so.LOGIN_SANDBOX)
        self.assertEqual(so.login_host("my_domain", "https://Acme.my.salesforce.com/x"),
                         "https://acme.my.salesforce.com")
        with self.assertRaises(so.OAuthError):
            so.login_host("my_domain", "evil.io")

    def test_parse_token(self):
        t = so.parse_token(token_body(), require_refresh=True)
        self.assertEqual((t["org_id"], t["user_id"], t["instance_url"]), (ORG_ID, USER_ID, INSTANCE))
        with self.assertRaises(so.OAuthError) as cm:
            so.parse_token(token_body(refresh=""), require_refresh=True)
        self.assertEqual(cm.exception.code, "no_refresh_token")
        with self.assertRaises(so.OAuthError) as cm:
            so.parse_token(token_body(instance="https://evil.io"), require_refresh=True)
        self.assertEqual(cm.exception.code, "invalid_instance_url")
        with self.assertRaises(so.OAuthError) as cm:
            so.parse_token({"error": "invalid_grant"}, require_refresh=False)
        self.assertEqual(cm.exception.code, "invalid_grant")

    def test_signature_checked_when_a_secret_is_set(self):
        so.parse_token(token_body(secret="s3"), require_refresh=True, client_secret="s3")
        with self.assertRaises(so.OAuthError) as cm:
            so.parse_token(token_body(secret="other"), require_refresh=True, client_secret="s3")
        self.assertEqual(cm.exception.code, "bad_signature")


class ConnectionTests(SalesforceHarness):
    def setUp(self):
        super().setUp()
        self.t["sf_oauth"] = fdb.FakeTable("SalesforceOAuthConnections", "workspace_id")
        self.posts = []          # (url, form)
        self.reply = {"token": (200, token_body()), "revoke": (200, {})}
        patches = [
            mock.patch.object(api, "_sf_oauth_connections", self.t["sf_oauth"]),
            mock.patch.object(api, "SALESFORCE_OAUTH_ENABLED", True),
            mock.patch.object(api, "SF_OAUTH_CLIENT_ID", "client-1"),
            mock.patch.object(api, "INTEGRATIONS_KMS_KEY_ID", "test-key"),
            mock.patch.object(api, "_sf_client_secret", return_value=""),
            mock.patch.object(api, "SF_OAUTH_REDIRECT_URI", "https://api.example.com/salesforce/oauth/callback"),
            mock.patch.object(api, "INTEGRATION_RETURN_URL", RETURN),
            mock.patch.object(api, "_sf_oauth_post", side_effect=self._post),
            # One-click setup after every login: by default the org has no
            # receiver (tests that need one use setup_org).
            mock.patch.object(api, "_sf_api_post_json", return_value=(404, {})),
            mock.patch.object(api, "_webhook_refresh_configuration"),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        api._sf_access_cache.clear()

    def _post(self, url, form):
        self.posts.append((url, dict(form)))
        return self.reply["revoke" if url.endswith("/revoke") else "token"]

    def connect(self, body=None):
        return call(api.salesforce_oauth_connect, event(
            "POST", "/workspaces/{workspace_id}/salesforce/connect", {"workspace_id": ORG},
            body or {"environment": "production"}))

    def callback(self, **qs):
        return api.salesforce_oauth_callback({"queryStringParameters": qs})

    def connected(self):
        _, body = self.connect()
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(body["authorize_url"]).query))
        return self.callback(code="code-1", state=q["state"]), q

    def test_connect_builds_a_pkce_authorize_url(self):
        status, body = self.connect({"environment": "my_domain", "my_domain": "acme.my.salesforce.com"})
        self.assertEqual(status, 200)
        url = urllib.parse.urlsplit(body["authorize_url"])
        q = dict(urllib.parse.parse_qsl(url.query))
        self.assertEqual(url.netloc, "acme.my.salesforce.com")
        self.assertEqual((q["code_challenge_method"], q["scope"], q["prompt"]),
                         ("S256", "api refresh_token", "login consent"))
        self.assertEqual(q["client_id"], "client-1")
        self.assertNotIn("code_verifier", q)

    def test_only_owners_and_managers_connect(self):
        self.current_user = MEMBER
        self.assertEqual(self.connect()[0], 403)
        self.current_user = MANAGER
        self.assertEqual(self.connect()[0], 200)

    def test_callback_stores_an_encrypted_refresh_token_and_returns_to_the_app(self):
        resp, q = self.connected()
        self.assertEqual(resp["statusCode"], 302)
        self.assertEqual(resp["headers"]["Location"],
                         f"{RETURN}?provider=salesforce&connected=1&setup=no_receiver")
        url, form = self.posts[-1]
        self.assertEqual(url, "https://login.salesforce.com/services/oauth2/token")
        self.assertEqual(form["grant_type"], "authorization_code")
        # The verifier sent is the one whose hash was in the authorize URL.
        digest = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest())
        self.assertEqual(digest.decode().rstrip("="), q["code_challenge"])
        row = self.t["sf_oauth"].get_item(Key={"workspace_id": ORG})["Item"]
        self.assertEqual((row["status"], row["org_id"], row["sf_user_id"]), ("connected", ORG_ID, USER_ID))
        self.assertEqual(row["refresh_token_enc"], "ENC(rt-1)")
        status, body = call(api.salesforce_oauth_status, event(
            "GET", "/workspaces/{workspace_id}/salesforce/connection", {"workspace_id": ORG}))
        self.assertEqual(status, 200)
        self.assertTrue(body["connection"]["connected"])
        self.assertEqual(body["connection"]["instance_host"], "acme.my.salesforce.com")
        self.assertNotIn("rt-1", str(body))

    def test_callback_refusals_redirect_with_a_reason(self):
        self.assertIn("reason=denied", self.callback(error="access_denied")["headers"]["Location"])
        self.assertIn("reason=expired", self.callback(code="c", state="bad.state")["headers"]["Location"])
        self.reply["token"] = (200, token_body(refresh=""))
        resp, _ = self.connected()
        self.assertIn("reason=no_refresh_token", resp["headers"]["Location"])
        self.assertIsNone(self.t["sf_oauth"].get_item(Key={"workspace_id": ORG}).get("Item"))

    def test_permissions_are_checked_again_at_the_callback(self):
        self.current_user = MANAGER
        _, body = self.connect()
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(body["authorize_url"]).query))
        self.t["memberships"].delete_item(Key={"workspace_id": ORG, "user_id": MANAGER})
        resp = self.callback(code="c", state=q["state"])
        self.assertIn("reason=not_allowed", resp["headers"]["Location"])

    def test_access_tokens_are_minted_and_cached(self):
        self.connected()
        self.reply["token"] = (200, token_body(refresh=""))
        before = len(self.posts)
        self.assertEqual(api._sf_access_token(ORG), ("at-1", INSTANCE))
        self.assertEqual(api._sf_access_token(ORG), ("at-1", INSTANCE))
        self.assertEqual(len(self.posts), before + 1, "cached the second time")
        self.assertEqual(self.posts[-1][1]["grant_type"], "refresh_token")
        self.assertEqual(self.posts[-1][1]["refresh_token"], "rt-1")

    def test_no_refresh_without_the_key_to_store_the_rotated_token(self):
        # Refreshing would spend the stored token (rotation) and lose the new
        # one; refuse BEFORE asking Salesforce, and keep the connection.
        self.connected()
        before = len(self.posts)
        with mock.patch.object(api, "INTEGRATIONS_KMS_KEY_ID", ""):
            with self.assertRaises(api.SiteDeliveryApiError) as cm:
                api._sf_access_token(ORG)
        self.assertEqual(cm.exception.code, "credential_storage_unconfigured")
        self.assertEqual(len(self.posts), before, "Salesforce was not asked")
        row = self.t["sf_oauth"].get_item(Key={"workspace_id": ORG})["Item"]
        self.assertEqual(row["status"], "connected")

    def test_a_refused_refresh_needs_a_reconnect_and_is_409(self):
        self.connected()
        self.reply["token"] = (400, {"error": "invalid_grant"})
        with self.assertRaises(api.SiteDeliveryApiError) as cm:
            api._sf_access_token(ORG)
        self.assertEqual((cm.exception.status, cm.exception.code),
                         (409, "salesforce_reconnect_needed"))
        row = self.t["sf_oauth"].get_item(Key={"workspace_id": ORG})["Item"]
        self.assertEqual(row["status"], "reconnect_needed")

    def test_a_rotated_refresh_token_is_stored(self):
        # Salesforce's Refresh Token Rotation: each refresh spends the token
        # and returns its successor.
        self.connected()
        self.reply["token"] = (200, token_body(refresh="rt-2"))
        api._sf_access_token(ORG)
        row = self.t["sf_oauth"].get_item(Key={"workspace_id": ORG})["Item"]
        self.assertEqual(row["refresh_token_enc"], "ENC(rt-2)")
        api._sf_access_cache.clear()
        self.reply["token"] = (200, token_body(refresh="rt-3"))
        api._sf_access_token(ORG)
        self.assertEqual(self.posts[-1][1]["refresh_token"], "rt-2", "the successor is used next")

    def test_a_token_spent_by_another_lambda_uses_the_stored_successor(self):
        self.connected()
        stale = dict(self.t["sf_oauth"].get_item(Key={"workspace_id": ORG})["Item"])
        # Another Lambda refreshed first and stored rt-2.
        self.t["sf_oauth"].update_item(
            Key={"workspace_id": ORG}, UpdateExpression="SET refresh_token_enc = :n",
            ExpressionAttributeValues={":n": "ENC(rt-2)"})
        replies = iter([(400, {"error": "invalid_grant"}), (200, token_body(refresh="rt-3"))])
        with mock.patch.object(api, "_sf_oauth_post",
                               side_effect=lambda url, form: (self.posts.append((url, dict(form))),
                                                              next(replies))[1]), \
                mock.patch.object(api, "_sf_oauth_connection",
                                  side_effect=[stale, self.t["sf_oauth"].get_item(
                                      Key={"workspace_id": ORG})["Item"]]):
            self.assertEqual(api._sf_access_token(ORG), ("at-1", INSTANCE))
        self.assertEqual([f["refresh_token"] for _u, f in self.posts[-2:]], ["rt-1", "rt-2"])
        row = self.t["sf_oauth"].get_item(Key={"workspace_id": ORG})["Item"]
        self.assertEqual((row["status"], row["refresh_token_enc"]), ("connected", "ENC(rt-3)"))

    def test_a_different_org_on_refresh_is_refused(self):
        self.connected()
        self.reply["token"] = (200, token_body(refresh="", org="00D5g00000OTHERRAQ"))
        with self.assertRaises(api.SiteDeliveryApiError) as cm:
            api._sf_access_token(ORG)
        self.assertEqual(cm.exception.code, "org_mismatch")

    def test_disconnect_revokes_and_deletes(self):
        self.connected()
        status, _ = call(api.salesforce_oauth_disconnect, event(
            "DELETE", "/workspaces/{workspace_id}/salesforce/connection", {"workspace_id": ORG}))
        self.assertEqual(status, 200)
        url, form = self.posts[-1]
        self.assertEqual((url, form), (INSTANCE + "/services/oauth2/revoke", {"token": "rt-1"}))
        self.assertIsNone(self.t["sf_oauth"].get_item(Key={"workspace_id": ORG}).get("Item"))

    def test_the_status_carries_the_package_install_link(self):
        status = lambda: call(api.salesforce_oauth_status, event(
            "GET", "/workspaces/{workspace_id}/salesforce/connection", {"workspace_id": ORG}))[1]
        with mock.patch.object(api, "SF_PACKAGE_VERSION_ID", "04tgL000000Z5EXQA0"), \
                mock.patch.object(api, "SF_PACKAGE_RELEASED", False):
            install = status()["connection"]["install"]
        self.assertEqual(install, {
            "version_id": "04tgL000000Z5EXQA0", "released": False,
            "production_url": "https://login.salesforce.com/packaging/installPackage.apexp?p0=04tgL000000Z5EXQA0",
            "sandbox_url": "https://test.salesforce.com/packaging/installPackage.apexp?p0=04tgL000000Z5EXQA0"})
        for bad in ("", "04t", "https://evil.example/04tgL000000Z5EXQA0", "05igL0000006rXtQAI"):
            with self.subTest(bad=bad), mock.patch.object(api, "SF_PACKAGE_VERSION_ID", bad):
                self.assertIsNone(status()["connection"]["install"])

    # -- one-click setup ----------------------------------------------------

    def setup_org(self, answers):
        """Connect, then answer setup/secret per receiver address:
        {address suffix: (status, body)}; record what was asked."""
        self.asked = []

        def org(url, token, body):
            self.asked.append((url, token, body))
            for suffix, reply in answers.items():
                if url.endswith(suffix):
                    return reply
            return 404, {}
        patches = [mock.patch.object(api, "_sf_api_post_json", side_effect=org),
                   mock.patch.object(api, "_webhook_refresh_configuration")]
        for x in patches:
            x.start()
        self.addCleanup(lambda: [x.stop() for x in patches])

    def endpoint(self):
        return self.t["site_endpoints"].get_item(Key={"workspace_id": ORG}).get("Item")

    def test_connect_finds_the_package_receiver_and_sets_its_secret(self):
        self.setup_org({"/DevMinuteX/minutex/v2/setup/secret": (200, {"ok": True, "namespace": "DevMinuteX"})})
        self.reply["token"] = (200, token_body())
        resp, _ = self.connected()
        self.assertIn("setup=ready", resp["headers"]["Location"])
        url, token, body = self.asked[0]
        self.assertEqual(url, INSTANCE + "/services/apexrest/DevMinuteX/minutex/v2/setup/secret")
        self.assertEqual(token, "at-1")
        self.assertEqual(body["type"], "setup.secret")
        row = self.endpoint()
        self.assertEqual(row["url"], INSTANCE + "/services/apexrest/DevMinuteX/minutex/v2")
        self.assertTrue(row["enabled"])
        self.assertIn(body["secret"], row["secrets_enc"], "the secret the org now holds is the one saved")
        self.assertEqual(row["secret_last4"], body["secret"][-4:])

    def test_a_receiver_without_a_namespace_is_found_next(self):
        self.setup_org({"/services/apexrest/minutex/v2/setup/secret": (200, {"ok": True, "namespace": ""})})
        resp, _ = self.connected()
        self.assertIn("setup=ready", resp["headers"]["Location"])
        self.assertEqual([u.split("/apexrest/")[1] for u, _t, _b in self.asked],
                         ["DevMinuteX/minutex/v2/setup/secret", "minutex/v2/setup/secret"])
        self.assertEqual(self.endpoint()["url"], INSTANCE + "/services/apexrest/minutex/v2")

    def test_each_reason_setup_cannot_finish(self):
        cases = [({}, "no_receiver"),
                 ({"/DevMinuteX/minutex/v2/setup/secret": (403, {"error": {"code": "not_allowed"}})}, "not_allowed"),
                 ({"/DevMinuteX/minutex/v2/setup/secret": (401, {"error": {"code": "bad_signature"}})}, "manual"),
                 ({"/DevMinuteX/minutex/v2/setup/secret": (403, {"error": {"code": "receiver_disabled"}})}, "manual"),
                 ({"/DevMinuteX/minutex/v2/setup/secret": (500, {"error": {"code": "receiver_failed"}})}, "failed")]
        for answers, outcome in cases:
            with self.subTest(outcome=outcome):
                self.t["site_endpoints"].delete_item(Key={"workspace_id": ORG})
                self.setup_org(answers)
                resp, _ = self.connected()
                self.assertIn(f"setup={outcome}", resp["headers"]["Location"])
                self.assertIn("connected=1", resp["headers"]["Location"], "the login itself still worked")
                self.assertIsNone(self.endpoint(), "nothing saved unless the org took the secret")

    def test_finish_setup_reruns_it_and_keeps_the_recording_link_key(self):
        self.setup_org({})
        self.connected()
        self.t["site_endpoints"].put_item(Item={
            "workspace_id": ORG, "delivery_type": "webhook", "provider": "salesforce",
            "url": "https://acme.my.salesforce-sites.com/services/apexrest/minutex/v2", "enabled": True,
            "secrets_enc": 'ENC({"hmac_secret": "old", "audio_key": "keep-me"})', "configuration": {"status": "ok"}})
        self.setup_org({"/DevMinuteX/minutex/v2/setup/secret": (200, {"ok": True})})
        status, body = call(api.salesforce_setup, event(
            "POST", "/workspaces/{workspace_id}/salesforce/setup", {"workspace_id": ORG}, {}))
        self.assertEqual((status, body["setup"]["status"]), (200, "ready"))
        row = self.endpoint()
        self.assertIn('"audio_key": "keep-me"', row["secrets_enc"])
        self.assertNotIn('"old"', row["secrets_enc"])
        self.assertEqual(row["configuration"], {"status": "never"}, "a new address is read afresh")
        self.current_user = MEMBER
        status, _ = call(api.salesforce_setup, event(
            "POST", "/workspaces/{workspace_id}/salesforce/setup", {"workspace_id": ORG}, {}))
        self.assertEqual(status, 403)

    def test_switched_off_by_default(self):
        with mock.patch.object(api, "SALESFORCE_OAUTH_ENABLED", False):
            status, body = self.connect()
        self.assertEqual((status, body["code"]), (404, "salesforce_oauth_disabled"))

    # -- delivery as the integration user -------------------------------------

    def signer(self):
        with mock.patch.object(api, "_site_secrets", return_value={"hmac_secret": SECRET}):
            return api._webhook_signer({"workspace_id": ORG, "url": SITE_URL})

    def test_without_a_connection_the_receiver_is_called_as_before(self):
        self.assertIsInstance(self.signer(), whc.WebhookSigner)
        self.connected()
        with mock.patch.object(api, "SALESFORCE_OAUTH_ENABLED", False):
            self.assertIsInstance(self.signer(), whc.WebhookSigner)

    def test_with_a_connection_the_receiver_is_called_as_the_integration_user(self):
        self.connected()
        self.reply["token"] = (200, token_body(refresh=""))
        signer = self.signer()
        self.assertIsInstance(signer, whc.BearerSigner)
        self.assertEqual(signer.rebase(SITE_URL), INSTANCE + APEX_PATH)
        headers = signer.headers(b"{}", 1790000000)
        self.assertEqual(headers["Authorization"], "Bearer at-1")
        self.assertTrue(headers["X-MinuteX-Signature"].startswith("v1="), "still HMAC-signed")
        self.assertNotIn("at-1", repr(signer))

    def test_a_connection_needing_a_reconnect_is_a_permission_failure_not_a_401(self):
        self.connected()
        self.reply["token"] = (400, {"error": "invalid_grant"})
        with self.assertRaises(whc.WebhookDeliveryError) as cm:
            self.signer()
        self.assertEqual((cm.exception.category, cm.exception.code),
                         (whc.CATEGORY_PERMISSION, "salesforce_reconnect_needed"))


SECRET = "whsec_" + "x" * 40
SITE_URL = "https://acme.my.salesforce-sites.com/services/apexrest/ns/minutex/v2"
APEX_PATH = "/services/apexrest/ns/minutex/v2"


class _Transport:
    """Answers each POST with the next status; records url + headers."""

    def __init__(self, *statuses):
        self.statuses = list(statuses)
        self.posts = []

    def post(self, url, body, headers, timeout, max_bytes):
        self.posts.append((url, dict(headers)))
        status = self.statuses.pop(0)
        return status, b'{"ok": true}' if status == 200 else b"", {}, False


class BearerSignerTests(unittest.TestCase):
    def setUp(self):
        self.tokens = iter(["at-1", "at-2", "at-3"])
        self.forced = []

    def source(self, force):
        self.forced.append(force)
        return next(self.tokens), INSTANCE

    def query(self, transport, base=SITE_URL):
        signer = whc.BearerSigner(whc.WebhookSigner(SECRET), self.source)
        return whc.WebhookClient(transport).query(
            base, "/configuration", {"request_id": "r1"}, signer, timeout=5, max_bytes=10000)

    def test_calls_the_orgs_api_address_with_the_token(self):
        t = _Transport(200)
        self.query(t)
        url, headers = t.posts[0]
        self.assertEqual(url, INSTANCE + APEX_PATH + "/configuration")
        self.assertEqual(headers["Authorization"], "Bearer at-1")
        self.assertIn("X-MinuteX-Signature", headers)

    def test_an_expired_token_is_refreshed_once(self):
        t = _Transport(401, 200)
        self.query(t)
        self.assertEqual([h["Authorization"] for _u, h in t.posts], ["Bearer at-1", "Bearer at-2"])
        self.assertEqual(self.forced, [False, True])

    def test_a_second_401_is_not_retried_again(self):
        t = _Transport(401, 401)
        with self.assertRaises(whc.WebhookDeliveryError) as cm:
            self.query(t)
        self.assertEqual(cm.exception.category, whc.CATEGORY_PERMISSION)
        self.assertEqual(len(t.posts), 2)

    def test_only_an_apex_rest_receiver_address_is_rebased(self):
        with self.assertRaises(whc.WebhookDeliveryError) as cm:
            self.query(_Transport(200), base="https://hooks.example.com/minutex")
        self.assertEqual(cm.exception.code, "endpoint_not_apex_rest")

    def test_the_plain_signer_never_retries_a_401(self):
        t = _Transport(401)
        with self.assertRaises(whc.WebhookDeliveryError):
            whc.WebhookClient(t).query(SITE_URL, "/configuration", {"request_id": "r1"},
                                       whc.WebhookSigner(SECRET), timeout=5, max_bytes=10000)
        self.assertEqual(len(t.posts), 1)
        self.assertNotIn("Authorization", t.posts[0][1])


if __name__ == "__main__":
    unittest.main()
