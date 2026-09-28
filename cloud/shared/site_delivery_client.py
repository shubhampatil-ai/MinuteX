"""site_delivery_client.py — HTTPS delivery to a customer's Salesforce Site.

WHAT THIS IS. The one place MinuteX makes an outbound call to a Salesforce
Site receiver: endpoint validation, request authentication, the POST itself,
and turning whatever comes back into either a DeliveryResponse or a
SiteDeliveryError with a retry decision attached.

It is deliberately independent of salesforce_client / SalesforceClient (the
OAuth REST client): no access token, no instance URL, no refresh. A Site
receiver is just an HTTPS endpoint that speaks site_contract.

ERROR CATEGORIES use the same vocabulary as the CRM job layer in userapi
("transient", "permission", "validation", "permanent") so a job outcome reads
the same whichever delivery path produced it. Only "transient" is retried.

SSRF. The URL is typed in by a customer admin and this code POSTs meeting
content to it from inside AWS, so validate_endpoint_url accepts only https,
port 443, a Salesforce-owned host suffix, no IP literal, no credentials in the
URL, and the receiver's own path. Redirects are never followed: a Site that
redirects is almost always sending an unauthorised guest to a login page, and
following it would also defeat the host check.
"""
import hashlib
import hmac
import ipaddress
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

import site_contract

CATEGORY_TRANSIENT = "transient"
CATEGORY_PERMISSION = "permission"
CATEGORY_VALIDATION = "validation"
CATEGORY_PERMANENT = "permanent"

AUTH_MODE_NONE = "none"
AUTH_MODE_HMAC = "hmac_sha256"
AUTH_MODES = (AUTH_MODE_NONE, AUTH_MODE_HMAC)
MIN_SECRET_CHARS = 32

RECEIVER_PATH = "/services/apexrest/minutex/v1/"

# Hosts Salesforce serves Sites and Experience Cloud sites from. A suffix
# match on a leading dot, so "evil-force.com" never matches ".force.com".
DEFAULT_ALLOWED_HOST_SUFFIXES = (
    ".my.salesforce-sites.com",
    ".salesforce-sites.com",
    ".my.site.com",
    ".force.com",
)

DEFAULT_TIMEOUT_SECONDS = 20
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
USER_AGENT = "MinuteX-Site-Delivery/1.0"


class SiteDeliveryError(Exception):
    def __init__(self, category, code, message, *, http_status=0, field="",
                 request_id=""):
        super().__init__(message)
        self.category = category
        self.code = code
        self.message = message
        self.http_status = http_status
        self.field = field
        self.request_id = request_id

    @property
    def retryable(self):
        return self.category == CATEGORY_TRANSIENT

    def as_dict(self):
        return {"category": self.category, "code": self.code,
                "message": self.message, "http_status": self.http_status,
                "field": self.field, "request_id": self.request_id}


class DeliveryResponse:
    def __init__(self, http_status, body, request_id, elapsed_ms):
        self.http_status = http_status
        self.body = body
        self.request_id = request_id
        self.elapsed_ms = elapsed_ms

    @property
    def result(self):
        result = self.body.get("result")
        return result if isinstance(result, dict) else {}

    @property
    def duplicate(self):
        return self.body.get("duplicate") is True

    @property
    def receiver_version(self):
        return str(self.body.get("receiver_version") or "")


# ---------------------------------------------------------------------------
# Endpoint validation
# ---------------------------------------------------------------------------

def validate_endpoint_url(url, allowed_host_suffixes=DEFAULT_ALLOWED_HOST_SUFFIXES):
    """The normalized receiver URL, or ValueError with a user-facing reason."""
    raw = str(url or "").strip()
    if not raw or len(raw) > 500:
        raise ValueError("Enter the Salesforce Site receiver URL.")
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme != "https":
        raise ValueError("The receiver URL must start with https://.")
    if parsed.username or parsed.password:
        raise ValueError("The receiver URL must not contain credentials.")
    if parsed.query or parsed.fragment:
        raise ValueError("The receiver URL must not contain a query string.")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError("The receiver URL has no host.")
    if _is_ip_literal(host):
        raise ValueError("The receiver URL must use the Site's domain name, "
                         "not an IP address.")
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("The receiver URL has an invalid port.")
    if port not in (None, 443):
        raise ValueError("The receiver URL must use the standard HTTPS port.")
    suffixes = tuple(s.lower() for s in allowed_host_suffixes if s)
    if not any(host.endswith(s) and len(host) > len(s) for s in suffixes):
        raise ValueError("The receiver URL must be a Salesforce Site domain "
                         f"(ending in {', '.join(suffixes)}).")
    path = parsed.path or "/"
    if not path.endswith("/"):
        path += "/"
    if not path.endswith(RECEIVER_PATH):
        raise ValueError("The receiver URL must end with "
                         f"{RECEIVER_PATH} (the MinuteX Apex REST endpoint).")
    return urllib.parse.urlunsplit(("https", host, path, "", ""))


def _is_ip_literal(host):
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def allowed_suffixes_from_env(value):
    """Parse SITE_DELIVERY_ALLOWED_HOST_SUFFIXES (comma-separated)."""
    parts = [p.strip().lower() for p in str(value or "").split(",") if p.strip()]
    return tuple(p if p.startswith(".") else "." + p for p in parts) \
        or DEFAULT_ALLOWED_HOST_SUFFIXES


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

class NoAuth:
    mode = AUTH_MODE_NONE

    def headers(self, body, timestamp):
        return {}


class HmacSha256Auth:
    """Signs "{unix seconds}.{raw body}" — MinuteXSiteAuth verifies it."""
    mode = AUTH_MODE_HMAC

    def __init__(self, secret):
        if not secret or len(secret) < MIN_SECRET_CHARS:
            raise ValueError(f"the shared secret must be at least "
                             f"{MIN_SECRET_CHARS} characters")
        self._secret = secret.encode("utf-8")

    def signature(self, body, timestamp):
        mac = hmac.new(self._secret, f"{timestamp}.".encode("utf-8") + body,
                       hashlib.sha256)
        return "v1=" + mac.hexdigest()

    def headers(self, body, timestamp):
        return {"X-MinuteX-Timestamp": str(timestamp),
                "X-MinuteX-Signature": self.signature(body, timestamp)}

    def __repr__(self):  # never print the secret
        return "HmacSha256Auth(<redacted>)"


# PLUGGABLE AUTHENTICATION. An auth mode is a factory (secret -> object with
# .mode and .headers(body_bytes, unix_ts)). `none` is for development only;
# `hmac_sha256` is built in; a production mode is added with
# register_auth_mode() here and a matching Verifier in the Apex receiver's
# MinuteXSiteAuth.VERIFIERS — nothing else in the delivery path changes.
_AUTH_FACTORIES = {
    AUTH_MODE_NONE: lambda secret: NoAuth(),
    AUTH_MODE_HMAC: lambda secret: HmacSha256Auth(secret),
}


def register_auth_mode(mode, factory):
    _AUTH_FACTORIES[str(mode)] = factory


def auth_modes():
    return tuple(_AUTH_FACTORIES)


def make_auth(mode, secret=""):
    factory = _AUTH_FACTORIES.get(mode)
    if factory is None:
        raise ValueError(f"unsupported auth mode {mode!r}")
    return factory(secret)


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

class TransportTimeout(Exception):
    pass


class TransportError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # surfaces the 3xx as an HTTPError instead of following


class UrlLibTransport:
    """The real network transport. Returns (status, body_bytes)."""

    def __init__(self):
        self._opener = urllib.request.build_opener(_NoRedirect)

    def post(self, url, body, headers, timeout):
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers=headers)
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                return resp.status, resp.read(MAX_RESPONSE_BYTES)
        except urllib.error.HTTPError as e:
            try:
                data = e.read(MAX_RESPONSE_BYTES)
            except Exception:  # noqa: BLE001
                data = b""
            return e.code, data
        except (socket.timeout, TimeoutError) as e:
            raise TransportTimeout(str(e)) from e
        except urllib.error.URLError as e:
            if isinstance(e.reason, (socket.timeout, TimeoutError)):
                raise TransportTimeout(str(e.reason)) from e
            raise TransportError(str(e.reason)) from e
        except OSError as e:
            raise TransportError(str(e)) from e


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class SiteDeliveryClient:
    def __init__(self, transport=None, *, timeout=DEFAULT_TIMEOUT_SECONDS,
                 allowed_host_suffixes=DEFAULT_ALLOWED_HOST_SUFFIXES,
                 clock=time.time):
        self.transport = transport or UrlLibTransport()
        self.timeout = timeout
        self.allowed_host_suffixes = allowed_host_suffixes
        self.clock = clock

    def post(self, url, auth, env):
        """POST one envelope. Returns DeliveryResponse or raises
        SiteDeliveryError. Never logs the body or any credential."""
        request_id = str(env.get("request_id") or "")
        try:
            url = validate_endpoint_url(url, self.allowed_host_suffixes)
        except ValueError as e:
            raise SiteDeliveryError(CATEGORY_PERMANENT, "invalid_endpoint",
                                    str(e), request_id=request_id)
        body = site_contract.encode_body(env)
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "X-MinuteX-Request-Id": request_id,
        }
        headers.update(auth.headers(body, int(self.clock())))

        started = time.monotonic()
        try:
            status, raw = self.transport.post(url, body, headers, self.timeout)
        except TransportTimeout:
            # AMBIGUOUS, not failed: the receiver may have committed. That is
            # safe to retry only because the retry carries the same
            # idempotency_key — see site_contract.meeting_idempotency_key.
            raise SiteDeliveryError(
                CATEGORY_TRANSIENT, "timeout",
                f"Salesforce did not respond within {self.timeout}s.",
                request_id=request_id)
        except TransportError as e:
            raise SiteDeliveryError(
                CATEGORY_TRANSIENT, "network_error",
                f"Could not reach the Salesforce Site ({str(e)[:200]}).",
                request_id=request_id)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return classify_response(status, raw, request_id, elapsed_ms)


def _parse_json(raw):
    try:
        return json.loads((raw or b"").decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def classify_response(status, raw, request_id="", elapsed_ms=0):
    """(HTTP status, body bytes) -> DeliveryResponse, or SiteDeliveryError."""
    parsed = _parse_json(raw)
    body = parsed if isinstance(parsed, dict) else {}
    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    code = str(err.get("code") or "")[:64]
    message = str(err.get("message") or "")[:500]
    field = str(err.get("field") or "")[:200]

    def fail(category, default_code, default_message):
        raise SiteDeliveryError(category, code or default_code,
                                message or default_message, http_status=status,
                                field=field, request_id=request_id)

    if 200 <= status < 300:
        if body.get("ok") is True:
            return DeliveryResponse(status, body, request_id, elapsed_ms)
        if parsed is None:
            fail(CATEGORY_PERMANENT, "invalid_response",
                 "The Site answered with a page that is not the MinuteX "
                 "receiver. Check the URL and that the Site guest user has "
                 "the MinuteX Site Guest Access permission set.")
        fail(CATEGORY_PERMANENT, "invalid_response",
             "The receiver answered without confirming success.")
    if 300 <= status < 400:
        fail(CATEGORY_PERMANENT, "site_redirected",
             "The Site redirected the request, which usually means the Site "
             "guest user cannot access the MinuteX Apex class.")
    if status == 401:
        # Salesforce's own 401 (a list body, INVALID_SESSION_ID) means the
        # URL points at the org's My Domain rather than at the public Site.
        if isinstance(parsed, list):
            fail(CATEGORY_PERMISSION, "site_requires_login",
                 "The URL requires a Salesforce login. Use the public Site "
                 "URL, not the org's My Domain URL.")
        fail(CATEGORY_PERMISSION, "auth_failed",
             "The receiver rejected MinuteX's credentials.")
    if status == 403:
        fail(CATEGORY_PERMISSION, "forbidden",
             "The receiver refused the request.")
    if status == 404:
        fail(CATEGORY_PERMANENT, "endpoint_not_found",
             "No MinuteX receiver was found at this URL.")
    if status in (400, 413, 422):
        fail(CATEGORY_VALIDATION, "validation_failed",
             "The receiver rejected the request as invalid.")
    if status in (408, 425, 429) or status >= 500:
        fail(CATEGORY_TRANSIENT, "receiver_unavailable",
             f"The Salesforce Site returned HTTP {status}; will retry.")
    fail(CATEGORY_PERMANENT, "unexpected_status",
         f"The Salesforce Site returned HTTP {status}.")
