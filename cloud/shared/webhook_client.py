"""webhook_client.py — signed, SSRF-safe HTTPS delivery for Webhook Contract v2.

WHAT THIS IS. The one place MinuteX makes an outbound call to a customer's
v2 webhook endpoint: URL validation, DNS resolution with a public-address
check, a connection PINNED to the address that was checked, HMAC signing
(dual-signed during secret rotation), the POST, and turning whatever comes
back into a WebhookResponse or a WebhookDeliveryError with a retry decision.

WHY NOT site_delivery_client. v1 only ever talks to a Salesforce-owned host
(suffix allowlist) on MinuteX's own receiver path, and requires the body to
confirm `ok: true`. v2 accepts ANY customer HTTPS endpoint, so the host
check cannot be an allowlist: it has to be "resolves only to public
addresses, and we connect to exactly the address we checked". HmacSha256Auth
and the error-category vocabulary are reused from v1 unchanged.

SSRF. The URL is typed in by a customer admin and this code POSTs meeting
content to it from inside AWS. Defences, all required:
  * https only, port 443, no credentials/query/fragment, no IP literal in
    any spelling, no single-label or local-only names;
  * getaddrinfo, and REFUSE if ANY returned address is non-public (a mixed
    answer is how rebinding attacks hide a private address);
  * connect to the validated IP while SNI, certificate verification and the
    Host header all use the hostname — so a DNS change between check and
    connect (rebinding) cannot redirect the socket;
  * never follow redirects; ignore proxy environment variables (http.client
    never reads them); cap the response read.
"""
import email.utils
import hashlib
import hmac
import http.client
import ipaddress
import json
import secrets
import socket
import ssl
import time
import urllib.parse

import site_delivery_client as sdc
import webhook_contract as wc

CATEGORY_TRANSIENT = sdc.CATEGORY_TRANSIENT
CATEGORY_PERMISSION = sdc.CATEGORY_PERMISSION
CATEGORY_VALIDATION = sdc.CATEGORY_VALIDATION
CATEGORY_PERMANENT = sdc.CATEGORY_PERMANENT

USER_AGENT = "MinuteX-Webhooks/2.0"
MIN_SECRET_CHARS = sdc.MIN_SECRET_CHARS
SECRET_PREFIX = "mxwhsec_"

EVENT_TIMEOUT_SECONDS = 20
CONFIGURATION_TIMEOUT_SECONDS = 10
SEARCH_TIMEOUT_SECONDS = 8
PEOPLE_TIMEOUT_SECONDS = 20
CONNECT_TIMEOUT_SECONDS = 5
MAX_EVENT_RESPONSE_BYTES = 64 * 1024
MAX_RETRY_AFTER_SECONDS = 3600
MAX_URL_CHARS = 2048

_LOCAL_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain",
                   ".home.arpa", ".lan", ".intranet", ".corp")
_LOCAL_NAMES = ("localhost", "localhost.localdomain")

# Explicit denials on top of `not is_global`. Belt and braces: the stdlib's
# idea of "global" has changed between Python versions, and these are the
# ranges an attacker actually aims at from inside AWS (127/8 includes the
# Lambda runtime API; 169.254/16 includes instance metadata).
_DENIED_NETWORKS = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
    "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
    "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
    "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4", "255.255.255.255/32",
    "::/128", "::1/128", "::ffff:0:0/96", "64:ff9b::/96", "64:ff9b:1::/48",
    "100::/64", "2001::/23", "2001:db8::/32", "2002::/16", "fc00::/7",
    "fe80::/10", "fec0::/10", "ff00::/8",
))


class WebhookDeliveryError(Exception):
    """Same shape as site_delivery_client.SiteDeliveryError (so the job layer
    reads both the same way), plus `retry_after` from the endpoint."""

    def __init__(self, category, code, message, *, http_status=0, field="",
                 request_id="", retry_after=0):
        super().__init__(message)
        self.category = category
        self.code = code
        self.message = message
        self.http_status = http_status
        self.field = field
        self.request_id = request_id
        self.retry_after = int(retry_after or 0)

    @property
    def retryable(self):
        return self.category == CATEGORY_TRANSIENT

    def as_dict(self):
        return {"category": self.category, "code": self.code,
                "message": self.message, "http_status": self.http_status,
                "field": self.field, "request_id": self.request_id,
                "retry_after": self.retry_after}


class WebhookResponse:
    def __init__(self, http_status, body, request_id, elapsed_ms, *,
                 duplicate=False):
        self.http_status = http_status
        self.body = body if isinstance(body, dict) else {}
        self.request_id = request_id
        self.elapsed_ms = elapsed_ms
        self.duplicate = duplicate

    @property
    def result(self):
        result = self.body.get("result")
        return result if isinstance(result, dict) else {}


# ---------------------------------------------------------------------------
# URL + address validation
# ---------------------------------------------------------------------------

class EndpointBlocked(Exception):
    """The endpoint resolves to an address MinuteX will not connect to."""


def _is_ip_literal(host):
    """True for any spelling a resolver might turn into an address:
    dotted, IPv6, and the legacy decimal/hex/octal/short forms inet_aton
    accepts ("2130706433", "0x7f000001", "0177.1", "127.1")."""
    h = host.strip("[]")
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        pass
    try:
        socket.inet_aton(h)
        return True
    except (OSError, ValueError):
        return False


def validate_webhook_url(url):
    """The normalized base URL (no trailing slash), or ValueError with a
    user-facing reason. Static checks only — see resolve_public_ip."""
    raw = str(url or "").strip()
    if not raw:
        raise ValueError("Enter the webhook URL.")
    if len(raw) > MAX_URL_CHARS:
        raise ValueError("The webhook URL is too long.")
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme != "https":
        raise ValueError("The webhook URL must start with https://.")
    if parsed.username or parsed.password or "@" in parsed.netloc:
        raise ValueError("The webhook URL must not contain credentials.")
    if parsed.query or parsed.fragment:
        raise ValueError("The webhook URL must not contain a query string "
                         "or fragment.")
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("The webhook URL has an invalid port.")
    if port not in (None, 443):
        raise ValueError("The webhook URL must use the standard HTTPS port (443).")
    host = (parsed.hostname or "").strip()
    if not host:
        raise ValueError("The webhook URL has no host.")
    if host.endswith("."):
        raise ValueError("The webhook URL host must not end with a dot.")
    if _is_ip_literal(host):
        raise ValueError("The webhook URL must use a domain name, not an IP "
                         "address.")
    try:
        ascii_host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError("The webhook URL host is not a valid domain name.")
    if "." not in ascii_host or ascii_host in _LOCAL_NAMES \
            or any(ascii_host.endswith(s) for s in _LOCAL_SUFFIXES):
        raise ValueError("The webhook URL must use a public domain name.")
    path = urllib.parse.quote(urllib.parse.unquote(parsed.path or ""),
                              safe="/-._~!$&'()*+,;=:@%")
    while path.endswith("/"):
        path = path[:-1]
    if "/../" in path + "/" or "/./" in path + "/":
        raise ValueError("The webhook URL path is not valid.")
    return urllib.parse.urlunsplit(("https", ascii_host, path, "", ""))


def _unwrap(ip):
    """The IPv4 address hiding inside an IPv4-mapped or 6to4 IPv6 address."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return ip.ipv4_mapped
        if ip.sixtofour:
            return ip.sixtofour
    return ip


def is_public_ip(value):
    try:
        ip = ipaddress.ip_address(str(value).split("%", 1)[0])
    except ValueError:
        return False
    for candidate in {ip, _unwrap(ip)}:
        if not candidate.is_global or candidate.is_private \
                or candidate.is_loopback or candidate.is_link_local \
                or candidate.is_multicast or candidate.is_reserved \
                or candidate.is_unspecified:
            return False
        if any(candidate in net for net in _DENIED_NETWORKS
               if net.version == candidate.version):
            return False
    return True


def resolve_public_ip(host, port=443, resolver=socket.getaddrinfo):
    """The address to connect to, or EndpointBlocked.

    Refuses when ANY answer is non-public, not just the first one: a record
    set mixing a public and a private address is a rebinding setup, and
    picking "the good one" would be one resolver-order change away from
    connecting to the private one.
    """
    try:
        infos = resolver(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise OSError(f"DNS lookup failed: {e}") from e
    addrs = [info[4][0] for info in infos or [] if info and info[4]]
    if not addrs:
        raise OSError("DNS lookup returned no addresses")
    for addr in addrs:
        if not is_public_ip(addr):
            raise EndpointBlocked("the webhook host resolves to a non-public "
                                  "address")
    return addrs[0]


# ---------------------------------------------------------------------------
# Signing
# ---------------------------------------------------------------------------

def new_secret():
    return SECRET_PREFIX + secrets.token_urlsafe(32)


class WebhookSigner:
    """Signs "{ts}.{raw body}" with the current secret and, during a rotation
    window, the previous one too: `v1=<current>,v1=<previous>`."""

    def __init__(self, secret, previous_secret=""):
        self._current = sdc.HmacSha256Auth(secret)
        self._previous = sdc.HmacSha256Auth(previous_secret) \
            if previous_secret else None

    @property
    def dual(self):
        return self._previous is not None

    def headers(self, body, timestamp):
        sigs = [self._current.signature(body, timestamp)]
        if self._previous is not None:
            sigs.append(self._previous.signature(body, timestamp))
        return {"X-MinuteX-Timestamp": str(timestamp),
                "X-MinuteX-Signature": ",".join(sigs)}

    def __repr__(self):  # never print a secret
        return f"WebhookSigner(<redacted>, dual={self.dual})"


APEX_REST_PREFIX = "/services/apexrest/"


class BearerSigner:
    """The HMAC signature PLUS the integration user's OAuth access token,
    sent to the org's own API address instead of the Site address.

    A Site runs the receiver as the guest user, who cannot own what it
    creates (so participants, tasks and files on a new meeting fail). The
    same receiver class, called at `{instance_url}/services/apexrest/...`
    with a Bearer token, runs as the integration user instead.

    `token_source(force)` -> (access_token, instance_url); `force=True`
    skips any cache. It raises WebhookDeliveryError when the org can't be
    reached as the integration user (reconnect needed, wrong org)."""

    def __init__(self, inner, token_source):
        self._inner = inner
        self._source = token_source
        self._token, self._instance = token_source(False)

    @property
    def dual(self):
        return self._inner.dual

    def headers(self, body, timestamp):
        out = self._inner.headers(body, timestamp)
        out["Authorization"] = "Bearer " + self._token
        return out

    def rebase(self, base_url):
        """The Site address's Apex REST path (namespace included) on the
        org's own API host."""
        path = urllib.parse.urlsplit(base_url).path.rstrip("/")
        if not path.startswith(APEX_REST_PREFIX):
            raise WebhookDeliveryError(
                CATEGORY_PERMANENT, "endpoint_not_apex_rest",
                "The receiver address is not a Salesforce Apex REST address.")
        return self._instance.rstrip("/") + path

    def refresh(self):
        """A fresh token after Salesforce said the current one expired."""
        self._token, self._instance = self._source(True)

    def __repr__(self):  # never print a token or secret
        return f"BearerSigner(<redacted>, dual={self.dual})"


def verify_signature(header, body, timestamp, secret_list, *, now=None,
                     max_skew=300):
    """The RECEIVER rule (docs §1.2), for tests and reference code: any v1=
    value matches any accepted secret, in constant time, within the skew."""
    try:
        ts = int(str(timestamp))
    except (TypeError, ValueError):
        return False
    if now is not None and abs(int(now) - ts) > max_skew:
        return False
    given = [p.strip()[3:] for p in str(header or "").split(",")
             if p.strip().startswith("v1=")]
    ok = False
    for secret in secret_list or []:
        if not secret:
            continue
        want = hmac.new(secret.encode("utf-8"), f"{ts}.".encode("utf-8") + body,
                        hashlib.sha256).hexdigest()
        for g in given:
            ok = hmac.compare_digest(g, want) or ok
    return ok


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

TransportTimeout = sdc.TransportTimeout
TransportError = sdc.TransportError


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """An HTTPS connection to `host` whose socket goes to `pinned_ip`.

    Everything that proves identity uses the HOSTNAME — the Host header
    (http.client sets it from `host`), SNI and certificate verification
    (`server_hostname`) — while the TCP connection goes to the address that
    passed resolve_public_ip. A DNS answer that changes after the check can
    therefore not move the connection somewhere else.
    """

    def __init__(self, host, pinned_ip, *, timeout, context):
        super().__init__(host, 443, timeout=timeout, context=context)
        self._pinned_ip = pinned_ip

    def connect(self):
        sock = socket.create_connection(
            (self._pinned_ip, 443),
            timeout=min(self.timeout or CONNECT_TIMEOUT_SECONDS,
                        CONNECT_TIMEOUT_SECONDS))
        sock.settimeout(self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def _tls_context():
    ctx = ssl.create_default_context()
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


class PinnedHTTPSTransport:
    """The real network transport.
    post(url, body, headers, timeout, max_bytes) ->
        (status, body_bytes, response_headers, truncated)."""

    def __init__(self, resolver=socket.getaddrinfo, connection_cls=None):
        self.resolver = resolver
        self.connection_cls = connection_cls or _PinnedHTTPSConnection

    def post(self, url, body, headers, timeout, max_bytes):
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        try:
            ip = resolve_public_ip(host, 443, self.resolver)
        except OSError as e:  # DNS failure; EndpointBlocked propagates as-is
            raise TransportError(str(e)) from e
        conn = self.connection_cls(host, ip, timeout=timeout,
                                   context=_tls_context())
        path = parsed.path or "/"
        try:
            conn.request("POST", path, body=body, headers=headers)
            resp = conn.getresponse()
            data = resp.read(max_bytes + 1)
            truncated = len(data) > max_bytes
            return resp.status, data[:max_bytes], \
                {k.lower(): v for k, v in resp.getheaders()}, truncated
        except (socket.timeout, TimeoutError) as e:
            raise TransportTimeout(str(e)) from e
        except ssl.SSLError as e:
            raise TransportError(f"TLS error: {e}") from e
        except (OSError, http.client.HTTPException) as e:
            raise TransportError(str(e)) from e
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

def parse_retry_after(value, now=None):
    """Seconds from a Retry-After header (delta or HTTP date), capped."""
    raw = str(value or "").strip()
    if not raw:
        return 0
    if raw.isdigit():
        return min(int(raw), MAX_RETRY_AFTER_SECONDS)
    try:
        when = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return 0
    if when is None:
        return 0
    delta = int(when.timestamp() - (now if now is not None else time.time()))
    return max(0, min(delta, MAX_RETRY_AFTER_SECONDS))


def _parse_json(raw):
    try:
        return json.loads((raw or b"").decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


class WebhookClient:
    def __init__(self, transport=None, *, clock=time.time):
        self.transport = transport or PinnedHTTPSTransport()
        self.clock = clock

    def _post(self, base_url, path, env, signer, *, timeout, max_bytes,
              event_id="", attempt=0):
        request_id = str(env.get("request_id") or env.get("event_id") or "")
        rebase = getattr(signer, "rebase", None)
        try:
            base = validate_webhook_url(rebase(base_url) if rebase else base_url)
        except ValueError as e:
            raise WebhookDeliveryError(CATEGORY_PERMANENT, "invalid_endpoint",
                                       str(e), request_id=request_id)
        body = wc.encode_body(env)
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "X-MinuteX-Version": wc.CONTRACT_VERSION,
            "X-MinuteX-Request-Id": request_id,
        }
        if event_id:
            headers["X-MinuteX-Event-Id"] = event_id
            headers["X-MinuteX-Delivery-Attempt"] = str(int(attempt or 1))
        refresh = getattr(signer, "refresh", None)

        started = time.monotonic()
        try:
            for tries_left in (1, 0):
                signed = dict(headers, **signer.headers(body, int(self.clock())))
                status, raw, resp_headers, truncated = self.transport.post(
                    base + path, body, signed, timeout, max_bytes)
                # Salesforce answers 401 for an expired access token, before
                # the receiver runs: one fresh token, one more try.
                if status != 401 or not refresh or not tries_left:
                    break
                refresh()
        except EndpointBlocked as e:
            raise WebhookDeliveryError(CATEGORY_PERMANENT, "endpoint_blocked",
                                       str(e), request_id=request_id)
        except TransportTimeout:
            # AMBIGUOUS, not failed: the endpoint may have committed. Safe to
            # retry only because the retry carries the same event_id.
            raise WebhookDeliveryError(
                CATEGORY_TRANSIENT, "timeout",
                f"The endpoint did not respond within {timeout}s.",
                request_id=request_id)
        except TransportError as e:
            raise WebhookDeliveryError(
                CATEGORY_TRANSIENT, "network_error",
                f"Could not reach the endpoint ({str(e)[:200]}).",
                request_id=request_id)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return status, raw, resp_headers or {}, truncated, request_id, elapsed_ms

    def post_event(self, base_url, env, signer, *, attempt=1,
                   timeout=EVENT_TIMEOUT_SECONDS):
        status, raw, headers, truncated, request_id, elapsed = self._post(
            base_url, wc.PATH_EVENTS, env, signer, timeout=timeout,
            max_bytes=MAX_EVENT_RESPONSE_BYTES, event_id=env.get("event_id", ""),
            attempt=attempt)
        return classify_event_response(
            status, None if truncated else raw, headers, request_id, elapsed,
            now=self.clock())

    def query(self, base_url, path, env, signer, *, timeout, max_bytes):
        """configuration / records/search / people: the JSON body, or error."""
        status, raw, headers, truncated, request_id, elapsed = self._post(
            base_url, path, env, signer, timeout=timeout, max_bytes=max_bytes)
        if 200 <= status < 300:
            if truncated:
                raise WebhookDeliveryError(
                    CATEGORY_VALIDATION, "response_too_large",
                    f"The endpoint's response is larger than {max_bytes} bytes.",
                    http_status=status, request_id=request_id)
            parsed = _parse_json(raw)
            if not isinstance(parsed, dict):
                raise WebhookDeliveryError(
                    CATEGORY_VALIDATION, "invalid_response",
                    "The endpoint did not answer with a JSON object.",
                    http_status=status, request_id=request_id)
            if parsed.get("ok") is False:
                _raise_for_status(422, parsed, headers, request_id,
                                  now=self.clock())
            return WebhookResponse(status, parsed, request_id, elapsed)
        _raise_for_status(status, _parse_json(raw), headers, request_id,
                          now=self.clock())


def _raise_for_status(status, parsed, headers, request_id, now=None):
    body = parsed if isinstance(parsed, dict) else {}
    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    code = str(err.get("code") or "")[:64]
    message = str(err.get("message") or "")[:500]
    field = str(err.get("field") or "")[:200]
    retry_after = parse_retry_after(headers.get("retry-after"), now)

    def fail(category, default_code, default_message):
        raise WebhookDeliveryError(category, code or default_code,
                                   message or default_message,
                                   http_status=status, field=field,
                                   request_id=request_id,
                                   retry_after=retry_after)

    if 300 <= status < 400:
        fail(CATEGORY_PERMANENT, "redirect_not_followed",
             "The endpoint redirected the request. MinuteX does not follow "
             "redirects; use the final URL.")
    if status in (401, 403):
        fail(CATEGORY_PERMISSION, "auth_rejected",
             "The endpoint rejected MinuteX's signature. Check that it has "
             "the current signing secret.")
    if status == 404:
        fail(CATEGORY_PERMANENT, "endpoint_not_found",
             "No MinuteX webhook receiver was found at this URL.")
    if status == 410:
        fail(CATEGORY_PERMANENT, "endpoint_gone",
             "The endpoint says it no longer accepts MinuteX events.")
    if status in (400, 413, 422):
        fail(CATEGORY_VALIDATION, "validation_failed",
             "The endpoint rejected the request as invalid.")
    if status in (408, 425, 429) or status >= 500:
        fail(CATEGORY_TRANSIENT, "endpoint_unavailable",
             f"The endpoint returned HTTP {status}; will retry.")
    fail(CATEGORY_PERMANENT, "unexpected_status",
         f"The endpoint returned HTTP {status}.")


def classify_event_response(status, raw, headers=None, request_id="",
                            elapsed_ms=0, now=None):
    """(HTTP status, body bytes|None) for an EVENT -> WebhookResponse, or
    WebhookDeliveryError. Any 2xx is accepted with an optional body; 409 is
    a duplicate ("already processed this event_id") and also accepted."""
    headers = headers or {}
    parsed = _parse_json(raw) if raw else None
    if 200 <= status < 300:
        if isinstance(parsed, dict) and parsed.get("ok") is False:
            _raise_for_status(422, parsed, headers, request_id, now=now)
        body = parsed if isinstance(parsed, dict) else {}
        return WebhookResponse(status, body, request_id, elapsed_ms,
                               duplicate=body.get("duplicate") is True)
    if status == 409:
        body = parsed if isinstance(parsed, dict) else {}
        return WebhookResponse(status, body, request_id, elapsed_ms,
                               duplicate=True)
    _raise_for_status(status, parsed, headers, request_id, now=now)
