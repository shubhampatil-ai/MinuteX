#!/usr/bin/env python3
"""test_webhook_client.py — the v2 HTTPS client (the SSRF boundary).

Pins: URL rules; the resolver refusing every non-public address (including
IPv4-mapped / NAT64 spellings and MIXED answers); the pinned connection
dialling the validated IP while SNI uses the hostname; headers and dual
signatures; the event and query response classification; Retry-After; and
the secret-rotation lifecycle as the receiver rule sees it.

Run:  python -m pytest tests/test_webhook_client.py
"""
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import webhook_client as whc  # noqa: E402
import webhook_contract as wc  # noqa: E402
from fake_webhook_receiver import FakeWebhookReceiver  # noqa: E402

BASE = "https://hooks.acme.com/services/apexrest/minutex/v2"
SECRET_A = "mxwhsec_" + "a" * 43
SECRET_B = "mxwhsec_" + "b" * 43


def addrinfo(*ips):
    def resolver(host, port, type=None):  # noqa: A002
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM,
                 6, "", (ip, port)) for ip in ips]
    return resolver


class Scripted:
    def __init__(self, status=200, body=b"", headers=None, truncated=False, exc=None):
        self.status, self.body, self.headers = status, body, headers or {}
        self.truncated, self.exc, self.seen = truncated, exc, []

    def post(self, url, body, headers, timeout, max_bytes):
        self.seen.append((url, body, headers, timeout, max_bytes))
        if self.exc:
            raise self.exc
        return self.status, self.body, self.headers, self.truncated


def event_env(seq=1):
    return wc.event_envelope(wc.EVENT_CREATED, meeting_id="mxm_1", sequence=seq,
                             occurred_at="2026-09-28T00:00:00Z", data={"meeting": {}})


class UrlTests(unittest.TestCase):
    def test_accepts_and_normalizes(self):
        self.assertEqual(whc.validate_webhook_url(BASE + "/"), BASE)
        self.assertEqual(whc.validate_webhook_url("https://Hooks.ACME.com:443/x/"),
                         "https://hooks.acme.com/x")
        self.assertEqual(whc.validate_webhook_url("https://hooks.acme.com"),
                         "https://hooks.acme.com")

    def test_rejects(self):
        for url in ["http://hooks.acme.com/x", "https://u:p@hooks.acme.com/x",
                    "https://hooks.acme.com/x?a=1", "https://hooks.acme.com/x#f",
                    "https://hooks.acme.com:8443/x", "https://127.0.0.1/x",
                    "https://[::1]/x", "https://2130706433/x", "https://0x7f000001/x",
                    "https://0177.0.0.1/x", "https://127.1/x", "https://localhost/x",
                    "https://intranet/x", "https://printer.local/x",
                    "https://db.internal/x", "https://router.home.arpa/x",
                    "https://hooks.acme.com./x", "https://" + "a" * 2050 + ".com",
                    "ftp://hooks.acme.com", "", "https:///x"]:
            with self.subTest(url=url[:50]):
                with self.assertRaises(ValueError):
                    whc.validate_webhook_url(url)


class ResolverTests(unittest.TestCase):
    def test_public_address_passes(self):
        self.assertEqual(whc.resolve_public_ip("h", resolver=addrinfo("93.184.216.34")),
                         "93.184.216.34")
        self.assertEqual(whc.resolve_public_ip(
            "h", resolver=addrinfo("2606:2800:220:1:248:1893:25c8:1946")),
            "2606:2800:220:1:248:1893:25c8:1946")

    def test_non_public_addresses_are_blocked(self):
        for ip in ["127.0.0.1", "127.0.0.2", "169.254.169.254", "169.254.100.1",
                   "10.0.0.5", "172.16.3.4", "192.168.1.1", "100.64.0.1",
                   "0.0.0.0", "198.18.0.1", "224.0.0.1", "255.255.255.255",
                   "::1", "fc00::1", "fd12::1", "fe80::1", "::ffff:10.0.0.1",
                   "::ffff:127.0.0.1", "64:ff9b::a00:1", "2002:0a00:0001::1",
                   "2001:db8::1", "ff02::1"]:
            with self.subTest(ip=ip):
                with self.assertRaises(whc.EndpointBlocked):
                    whc.resolve_public_ip("h", resolver=addrinfo(ip))

    def test_a_mixed_answer_is_blocked(self):
        with self.assertRaises(whc.EndpointBlocked):
            whc.resolve_public_ip("h", resolver=addrinfo("93.184.216.34", "10.0.0.1"))
        with self.assertRaises(whc.EndpointBlocked):
            whc.resolve_public_ip("h", resolver=addrinfo("10.0.0.1", "93.184.216.34"))

    def test_dns_failure_is_an_os_error(self):
        def boom(*a, **k):
            raise socket.gaierror(-2, "Name or service not known")
        with self.assertRaises(OSError):
            whc.resolve_public_ip("h", resolver=boom)


class PinnedConnectionTests(unittest.TestCase):
    def test_dials_the_validated_ip_with_the_hostname_for_sni(self):
        sock = mock.MagicMock()
        ctx = mock.MagicMock()
        with mock.patch.object(whc.socket, "create_connection", return_value=sock) as cc:
            conn = whc._PinnedHTTPSConnection("hooks.acme.com", "93.184.216.34",
                                              timeout=20, context=ctx)
            conn.connect()
        self.assertEqual(cc.call_args[0][0], ("93.184.216.34", 443))
        ctx.wrap_socket.assert_called_once_with(sock, server_hostname="hooks.acme.com")
        self.assertEqual(conn.host, "hooks.acme.com")  # the Host header

    def test_tls_context_verifies(self):
        ctx = whc._tls_context()
        self.assertTrue(ctx.check_hostname)
        self.assertEqual(ctx.verify_mode, whc.ssl.CERT_REQUIRED)
        self.assertGreaterEqual(ctx.minimum_version, whc.ssl.TLSVersion.TLSv1_2)

    def test_transport_refuses_private_before_connecting(self):
        made = []

        class Conn:
            def __init__(self, *a, **k):
                made.append(a)
        transport = whc.PinnedHTTPSTransport(resolver=addrinfo("10.0.0.1"),
                                             connection_cls=Conn)
        client = whc.WebhookClient(transport)
        with self.assertRaises(whc.WebhookDeliveryError) as cm:
            client.post_event(BASE, event_env(), whc.WebhookSigner(SECRET_A))
        self.assertEqual(cm.exception.code, "endpoint_blocked")
        self.assertFalse(cm.exception.retryable)
        self.assertEqual(made, [])

    def test_transport_sends_to_the_path_and_caps_the_read(self):
        seen = {}

        class Resp:
            status = 200

            def read(self, n):
                seen["read"] = n
                return b"x" * n

            def getheaders(self):
                return [("Retry-After", "5")]

        class Conn:
            def __init__(self, host, ip, timeout, context):
                seen.update(host=host, ip=ip, timeout=timeout)

            def request(self, method, path, body, headers):
                seen.update(method=method, path=path)

            def getresponse(self):
                return Resp()

            def close(self):
                seen["closed"] = True
        transport = whc.PinnedHTTPSTransport(resolver=addrinfo("93.184.216.34"),
                                             connection_cls=Conn)
        status, raw, headers, truncated = transport.post(
            BASE + "/events", b"{}", {}, 20, 100)
        self.assertEqual((seen["host"], seen["ip"], seen["method"], seen["path"]),
                         ("hooks.acme.com", "93.184.216.34", "POST",
                          "/services/apexrest/minutex/v2/events"))
        self.assertEqual(seen["read"], 101)
        self.assertTrue(truncated)
        self.assertEqual(len(raw), 100)
        self.assertEqual(headers["retry-after"], "5")
        self.assertTrue(seen["closed"])


class SigningTests(unittest.TestCase):
    def test_single_and_dual_signatures(self):
        body, ts = b'{"a":1}', 1_750_000_000
        one = whc.WebhookSigner(SECRET_A).headers(body, ts)
        self.assertRegex(one["X-MinuteX-Signature"], r"^v1=[0-9a-f]{64}$")
        two = whc.WebhookSigner(SECRET_B, SECRET_A).headers(body, ts)
        parts = two["X-MinuteX-Signature"].split(",")
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[1], one["X-MinuteX-Signature"])
        self.assertNotIn(SECRET_A, repr(whc.WebhookSigner(SECRET_A)))
        with self.assertRaises(ValueError):
            whc.WebhookSigner("short")

    def test_secret_rotation_lifecycle(self):
        """A -> rotate -> B with A as previous: both signatures verify at a
        receiver holding either secret. After the window MinuteX signs with B
        only, and a receiver that has dropped A rejects an A signature."""
        body, ts = b"{}", 1_750_000_000
        sig = whc.WebhookSigner(SECRET_B, SECRET_A).headers(body, ts)["X-MinuteX-Signature"]
        self.assertTrue(whc.verify_signature(sig, body, ts, [SECRET_A]))
        self.assertTrue(whc.verify_signature(sig, body, ts, [SECRET_B]))
        after = whc.WebhookSigner(SECRET_B).headers(body, ts)["X-MinuteX-Signature"]
        self.assertTrue(whc.verify_signature(after, body, ts, [SECRET_B]))
        self.assertFalse(whc.verify_signature(after, body, ts, [SECRET_A]))
        old_only = whc.WebhookSigner(SECRET_A).headers(body, ts)["X-MinuteX-Signature"]
        self.assertFalse(whc.verify_signature(old_only, body, ts, [SECRET_B]))

    def test_receiver_rule_enforces_skew_and_raw_bytes(self):
        body, ts = b"{}", 1_750_000_000
        sig = whc.WebhookSigner(SECRET_A).headers(body, ts)["X-MinuteX-Signature"]
        self.assertTrue(whc.verify_signature(sig, body, ts, [SECRET_A], now=ts + 299))
        self.assertFalse(whc.verify_signature(sig, body, ts, [SECRET_A], now=ts + 301))
        self.assertFalse(whc.verify_signature(sig, b"{ }", ts, [SECRET_A]))

    def test_new_secret_shape(self):
        s = whc.new_secret()
        self.assertTrue(s.startswith("mxwhsec_"))
        self.assertGreaterEqual(len(s), whc.MIN_SECRET_CHARS)
        self.assertNotEqual(s, whc.new_secret())


class ClientTests(unittest.TestCase):
    def test_event_headers(self):
        t = Scripted(200, b"")
        whc.WebhookClient(t, clock=lambda: 1_750_000_000).post_event(
            BASE, event_env(), whc.WebhookSigner(SECRET_A), attempt=3)
        url, body, headers, timeout, _max = t.seen[0]
        self.assertEqual(url, BASE + "/events")
        self.assertEqual(headers["X-MinuteX-Version"], "2.0")
        self.assertEqual(headers["X-MinuteX-Event-Id"], wc.event_id("mxm_1", 1))
        self.assertEqual(headers["X-MinuteX-Delivery-Attempt"], "3")
        self.assertEqual(headers["X-MinuteX-Timestamp"], "1750000000")
        self.assertEqual(headers["User-Agent"], "MinuteX-Webhooks/2.0")
        self.assertTrue(whc.verify_signature(headers["X-MinuteX-Signature"], body,
                                             1_750_000_000, [SECRET_A]))
        self.assertEqual(timeout, whc.EVENT_TIMEOUT_SECONDS)

    def classify(self, status, body=b"", headers=None):
        return whc.WebhookClient(Scripted(status, body, headers)).post_event(
            BASE, event_env(), whc.WebhookSigner(SECRET_A))

    def test_event_classification(self):
        self.assertEqual(self.classify(204).http_status, 204)
        ok = self.classify(200, b'{"result":{"record_id":"a0S1","url":"https://x"}}')
        self.assertEqual(ok.result["record_id"], "a0S1")
        self.assertFalse(ok.duplicate)
        self.assertTrue(self.classify(409).duplicate)
        self.assertTrue(self.classify(200, b'{"duplicate":true}').duplicate)
        self.assertEqual(self.classify(200, b"<html>").http_status, 200)
        table = [(302, "permanent", "redirect_not_followed"),
                 (401, "permission", "auth_rejected"), (403, "permission", "auth_rejected"),
                 (404, "permanent", "endpoint_not_found"), (410, "permanent", "endpoint_gone"),
                 (400, "validation", "validation_failed"),
                 (413, "validation", "validation_failed"),
                 (422, "validation", "validation_failed"),
                 (408, "transient", "endpoint_unavailable"),
                 (425, "transient", "endpoint_unavailable"),
                 (429, "transient", "endpoint_unavailable"),
                 (500, "transient", "endpoint_unavailable"),
                 (503, "transient", "endpoint_unavailable"),
                 (418, "permanent", "unexpected_status")]
        for status, category, code in table:
            with self.subTest(status=status):
                with self.assertRaises(whc.WebhookDeliveryError) as cm:
                    self.classify(status)
                self.assertEqual((cm.exception.category, cm.exception.code),
                                 (category, code))
        with self.assertRaises(whc.WebhookDeliveryError) as cm:
            self.classify(200, b'{"ok":false,"error":{"code":"bad_type","message":"no"}}')
        self.assertEqual((cm.exception.category, cm.exception.code),
                         ("validation", "bad_type"))

    def test_retry_after(self):
        with self.assertRaises(whc.WebhookDeliveryError) as cm:
            self.classify(503, b"", {"retry-after": "120"})
        self.assertEqual(cm.exception.retry_after, 120)
        with self.assertRaises(whc.WebhookDeliveryError) as cm:
            self.classify(429, b"", {"retry-after": "999999"})
        self.assertEqual(cm.exception.retry_after, whc.MAX_RETRY_AFTER_SECONDS)
        self.assertEqual(whc.parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT",
                                               now=1445412480 - 30), 30)
        self.assertEqual(whc.parse_retry_after("garbage"), 0)

    def test_transport_failures_are_transient(self):
        for exc, code in ((whc.TransportTimeout("t"), "timeout"),
                          (whc.TransportError("reset"), "network_error")):
            with self.subTest(code=code):
                with self.assertRaises(whc.WebhookDeliveryError) as cm:
                    whc.WebhookClient(Scripted(exc=exc)).post_event(
                        BASE, event_env(), whc.WebhookSigner(SECRET_A))
                self.assertEqual(cm.exception.code, code)
                self.assertTrue(cm.exception.retryable)

    def test_invalid_url_is_permanent_and_never_sent(self):
        t = Scripted(200)
        with self.assertRaises(whc.WebhookDeliveryError) as cm:
            whc.WebhookClient(t).post_event("http://x.com", event_env(),
                                            whc.WebhookSigner(SECRET_A))
        self.assertEqual(cm.exception.code, "invalid_endpoint")
        self.assertEqual(t.seen, [])

    def test_query_requires_a_json_object_within_the_cap(self):
        env = wc.configuration_request(request_id="r1")
        signer = whc.WebhookSigner(SECRET_A)
        resp = whc.WebhookClient(Scripted(200, b'{"configuration_version":"1",'
                                               b'"meeting_types":[]}')) \
            .query(BASE, wc.PATH_CONFIGURATION, env, signer, timeout=10, max_bytes=100)
        self.assertEqual(resp.body["configuration_version"], "1")
        for scripted, code in ((Scripted(200, b"[]"), "invalid_response"),
                               (Scripted(200, b"x", truncated=True), "response_too_large"),
                               (Scripted(503), "endpoint_unavailable"),
                               (Scripted(200, b'{"ok":false}'), "validation_failed")):
            with self.subTest(code=code):
                with self.assertRaises(whc.WebhookDeliveryError) as cm:
                    whc.WebhookClient(scripted).query(
                        BASE, wc.PATH_CONFIGURATION, env, signer,
                        timeout=10, max_bytes=100)
                self.assertEqual(cm.exception.code, code)

    def test_against_the_fake_receiver(self):
        rx = FakeWebhookReceiver(secrets=[SECRET_A])
        client = whc.WebhookClient(rx)
        resp = client.query(BASE, wc.PATH_CONFIGURATION,
                            wc.configuration_request(request_id="r"),
                            whc.WebhookSigner(SECRET_A), timeout=10, max_bytes=65536)
        self.assertEqual(len(resp.body["meeting_types"]), 3)
        with self.assertRaises(whc.WebhookDeliveryError) as cm:
            client.post_event(BASE, event_env(), whc.WebhookSigner(SECRET_B))
        self.assertEqual(cm.exception.code, "bad_signature")
        # A body replayed against the wrong endpoint is refused.
        with self.assertRaises(whc.WebhookDeliveryError) as cm:
            client.query(BASE, wc.PATH_SEARCH, wc.configuration_request(request_id="r"),
                         whc.WebhookSigner(SECRET_A), timeout=8, max_bytes=65536)
        self.assertEqual(cm.exception.code, "type_mismatch")


if __name__ == "__main__":
    unittest.main()
