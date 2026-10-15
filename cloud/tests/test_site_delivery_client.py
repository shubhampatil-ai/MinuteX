#!/usr/bin/env python3
"""test_site_delivery_client.py — the HTTPS delivery client (pure + socket).

Pins: endpoint validation (the SSRF boundary), HMAC signing byte-for-byte,
headers, response classification for every status family, timeout and
network failures, and — against a real local HTTP server — that the urllib
transport returns bodies for error statuses, never follows redirects, and
times out.

Run:  python -m pytest tests/test_site_delivery_client.py
"""
import hashlib
import hmac
import http.server
import json
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import site_contract as sc  # noqa: E402
import site_delivery_client as sdc  # noqa: E402
from fake_site_receiver import FakeSiteReceiver  # noqa: E402

URL = "https://acme.my.salesforce-sites.com/services/apexrest/minutex/v1/"
SECRET = "s" * 40


def env(action="health"):
    return sc.envelope(action, request_id="req123", sent_at="2026-09-01T00:00:00Z")


class ScriptedTransport:
    def __init__(self, status=200, body=b"", exc=None):
        self.status, self.body, self.exc = status, body, exc
        self.sent = []

    def post(self, url, body, headers, timeout):
        self.sent.append((url, body, headers, timeout))
        if self.exc:
            raise self.exc
        return self.status, self.body


def ok_body(result=None, duplicate=False):
    return json.dumps({"ok": True, "duplicate": duplicate,
                       "receiver_version": "1.0.0",
                       "result": result or {}}).encode()


class EndpointValidationTests(unittest.TestCase):
    def test_accepts_site_domains_and_normalizes(self):
        self.assertEqual(sdc.validate_endpoint_url(URL), URL)
        self.assertEqual(sdc.validate_endpoint_url(URL.rstrip("/").upper().replace("HTTPS", "https")
                                                   .replace("/SERVICES/APEXREST/MINUTEX/V1", "/services/apexrest/minutex/v1")),
                         URL)
        prefixed = "https://acme.my.site.com/receiver/services/apexrest/minutex/v1/"
        self.assertEqual(sdc.validate_endpoint_url(prefixed), prefixed)

    def test_rejects_everything_else(self):
        bad = [
            "", "http://acme.my.salesforce-sites.com/services/apexrest/minutex/v1/",
            "https://evil.com/services/apexrest/minutex/v1/",
            "https://evilforce.com/services/apexrest/minutex/v1/",
            "https://force.com/services/apexrest/minutex/v1/",
            "https://acme.my.salesforce-sites.com.evil.com/services/apexrest/minutex/v1/",
            "https://169.254.169.254/services/apexrest/minutex/v1/",
            "https://[::1]/services/apexrest/minutex/v1/",
            "https://user:pw@acme.my.salesforce-sites.com/services/apexrest/minutex/v1/",
            "https://acme.my.salesforce-sites.com:8443/services/apexrest/minutex/v1/",
            "https://acme.my.salesforce-sites.com/services/apexrest/other/",
            "https://acme.my.salesforce-sites.com/services/apexrest/minutex/v1/?x=1",
            "https://acme.my.salesforce-sites.com/services/apexrest/minutex/v1/#f",
            "https://acme.my.salesforce-sites.com:notaport/services/apexrest/minutex/v1/",
        ]
        for url in bad:
            with self.assertRaises(ValueError, msg=url):
                sdc.validate_endpoint_url(url)

    def test_env_suffix_parsing(self):
        self.assertEqual(sdc.allowed_suffixes_from_env(""), sdc.DEFAULT_ALLOWED_HOST_SUFFIXES)
        self.assertEqual(sdc.allowed_suffixes_from_env("my.site.com, .force.com"),
                         (".my.site.com", ".force.com"))


class AuthTests(unittest.TestCase):
    def test_hmac_matches_the_apex_algorithm(self):
        auth = sdc.HmacSha256Auth(SECRET)
        body = b'{"action":"health"}'
        want = "v1=" + hmac.new(SECRET.encode(), b"1700000000." + body,
                                hashlib.sha256).hexdigest()
        self.assertEqual(auth.headers(body, 1700000000),
                         {"X-MinuteX-Timestamp": "1700000000",
                          "X-MinuteX-Signature": want})
        self.assertNotIn(SECRET, repr(auth))

    def test_short_secret_and_unknown_mode_rejected(self):
        with self.assertRaises(ValueError):
            sdc.HmacSha256Auth("short")
        with self.assertRaises(ValueError):
            sdc.make_auth("basic")
        self.assertEqual(sdc.make_auth("none").headers(b"x", 1), {})


class ClientTests(unittest.TestCase):
    def client(self, transport):
        return sdc.SiteDeliveryClient(transport, clock=lambda: 1700000000)

    def test_success_sends_signed_request_with_ids(self):
        t = ScriptedTransport(200, ok_body({"status": "ok"}))
        resp = self.client(t).post(URL, sdc.HmacSha256Auth(SECRET), env())
        self.assertEqual(resp.result, {"status": "ok"})
        self.assertEqual(resp.receiver_version, "1.0.0")
        url, body, headers, timeout = t.sent[0]
        self.assertEqual(url, URL)
        self.assertEqual(headers["X-MinuteX-Request-Id"], "req123")
        self.assertEqual(headers["Content-Type"], "application/json; charset=utf-8")
        self.assertEqual(json.loads(body)["action"], "health")
        self.assertEqual(headers["X-MinuteX-Signature"],
                         sdc.HmacSha256Auth(SECRET).signature(body, 1700000000))
        self.assertEqual(timeout, sdc.DEFAULT_TIMEOUT_SECONDS)

    def test_duplicate_flag(self):
        t = ScriptedTransport(200, ok_body({"operation": "created"}, duplicate=True))
        self.assertTrue(self.client(t).post(URL, sdc.NoAuth(), env()).duplicate)

    def expect(self, transport, category, code=None, http_status=None):
        with self.assertRaises(sdc.SiteDeliveryError) as ctx:
            self.client(transport).post(URL, sdc.NoAuth(), env())
        e = ctx.exception
        self.assertEqual(e.category, category)
        if code:
            self.assertEqual(e.code, code)
        if http_status is not None:
            self.assertEqual(e.http_status, http_status)
        self.assertEqual(e.request_id, "req123")
        self.assertEqual(e.retryable, category == sdc.CATEGORY_TRANSIENT)
        return e

    def test_timeout_is_transient(self):
        self.expect(ScriptedTransport(exc=sdc.TransportTimeout("t")),
                    sdc.CATEGORY_TRANSIENT, "timeout")

    def test_network_error_is_transient(self):
        self.expect(ScriptedTransport(exc=sdc.TransportError("reset")),
                    sdc.CATEGORY_TRANSIENT, "network_error")

    def test_5xx_and_429_are_transient(self):
        for status in (500, 502, 503, 504, 429, 408):
            self.expect(ScriptedTransport(status, b"oops"), sdc.CATEGORY_TRANSIENT,
                        http_status=status)

    def test_4xx_validation_carries_receiver_code_and_field(self):
        body = json.dumps({"ok": False, "error": {
            "code": "transcript_too_large", "message": "too big",
            "field": "payload.transcript.text"}}).encode()
        e = self.expect(ScriptedTransport(422, body), sdc.CATEGORY_VALIDATION,
                        "transcript_too_large", 422)
        self.assertEqual(e.field, "payload.transcript.text")
        self.expect(ScriptedTransport(400, b"{}"), sdc.CATEGORY_VALIDATION)
        self.expect(ScriptedTransport(413, b""), sdc.CATEGORY_VALIDATION)

    def test_auth_failures_are_permission(self):
        self.expect(ScriptedTransport(401, b'{"ok":false,"error":{"code":"auth_failed"}}'),
                    sdc.CATEGORY_PERMISSION, "auth_failed")
        self.expect(ScriptedTransport(401, b'[{"errorCode":"INVALID_SESSION_ID"}]'),
                    sdc.CATEGORY_PERMISSION, "site_requires_login")
        self.expect(ScriptedTransport(403, b'{"ok":false,"error":{"code":"receiver_disabled"}}'),
                    sdc.CATEGORY_PERMISSION, "receiver_disabled")

    def test_misrouted_endpoints_are_permanent(self):
        self.expect(ScriptedTransport(404, b""), sdc.CATEGORY_PERMANENT, "endpoint_not_found")
        self.expect(ScriptedTransport(302, b""), sdc.CATEGORY_PERMANENT, "site_redirected")
        self.expect(ScriptedTransport(200, b"<html>login</html>"), sdc.CATEGORY_PERMANENT,
                    "invalid_response")
        self.expect(ScriptedTransport(200, b'{"ok":false}'), sdc.CATEGORY_PERMANENT,
                    "invalid_response")
        self.expect(ScriptedTransport(418, b""), sdc.CATEGORY_PERMANENT, "unexpected_status")

    def test_invalid_url_never_reaches_the_transport(self):
        t = ScriptedTransport(200, ok_body())
        with self.assertRaises(sdc.SiteDeliveryError) as ctx:
            self.client(t).post("https://evil.com/services/apexrest/minutex/v1/",
                                sdc.NoAuth(), env())
        self.assertEqual(ctx.exception.code, "invalid_endpoint")
        self.assertEqual(t.sent, [])

    def test_against_fake_receiver_with_hmac(self):
        receiver = FakeSiteReceiver(secret=SECRET)
        resp = sdc.SiteDeliveryClient(receiver).post(URL, sdc.HmacSha256Auth(SECRET), env())
        self.assertEqual(resp.result["auth_mode"], "hmac_sha256")
        with self.assertRaises(sdc.SiteDeliveryError) as ctx:
            sdc.SiteDeliveryClient(receiver).post(URL, sdc.HmacSha256Auth("x" * 40), env())
        self.assertEqual(ctx.exception.code, "auth_failed")


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path == "/ok":
            self._send(200, ok_body({"status": "ok"}))
        elif self.path == "/fail":
            self._send(503, b'{"ok":false,"error":{"code":"down"}}')
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
        elif self.path == "/slow":
            time.sleep(2)
            self._send(200, ok_body())

    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class UrlLibTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_success(self):
        status, body = sdc.UrlLibTransport().post(self.base + "/ok", b"{}", {}, 5)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_error_status_returns_body(self):
        status, body = sdc.UrlLibTransport().post(self.base + "/fail", b"{}", {}, 5)
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body)["error"]["code"], "down")

    def test_redirect_is_not_followed(self):
        status, _ = sdc.UrlLibTransport().post(self.base + "/redirect", b"{}", {}, 5)
        self.assertEqual(status, 302)

    def test_timeout(self):
        with self.assertRaises(sdc.TransportTimeout):
            sdc.UrlLibTransport().post(self.base + "/slow", b"{}", {}, 0.5)

    def test_connection_refused_is_a_transport_failure(self):
        # Linux refuses at once (TransportError); Windows retries the SYN for
        # ~2s and surfaces a timeout. Both classify as transient — that is
        # the property that matters.
        server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        port = server.server_address[1]
        server.server_close()
        with self.assertRaises((sdc.TransportError, sdc.TransportTimeout)):
            sdc.UrlLibTransport().post(f"http://127.0.0.1:{port}/ok", b"{}", {}, 5)


if __name__ == "__main__":
    unittest.main()
