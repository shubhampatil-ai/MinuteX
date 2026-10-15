"""salesforce_oauth.py — connecting a workspace to the customer's Salesforce
org through an integration user (OAuth 2.0 web-server flow + PKCE).

Pure: no AWS, no network, no clock except where passed in. userApi does the
HTTP (one patchable function) and the storage; this module builds the
requests and refuses anything unsafe in the responses.

WHY THESE CHECKS. The token response tells MinuteX where the org lives
(`instance_url`) and every later call goes there carrying a bearer token.
So `instance_url` must be a real Salesforce host over HTTPS, or a tampered
response could send the token anywhere. The org and user ids come from the
response's `id` URL and are pinned on the connection, so a reconnect to a
different org is visible, never silent.
"""
import base64
import hashlib
import hmac
import re
import urllib.parse

LOGIN_PRODUCTION = "https://login.salesforce.com"
LOGIN_SANDBOX = "https://test.salesforce.com"
ENVIRONMENTS = ("production", "sandbox", "my_domain")

# What the integration user grants MinuteX: API access and a lasting
# connection. Nothing broader.
SCOPES = "api refresh_token"

AUTHORIZE_PATH = "/services/oauth2/authorize"
TOKEN_PATH = "/services/oauth2/token"
REVOKE_PATH = "/services/oauth2/revoke"

_SF_HOST_SUFFIXES = (".salesforce.com", ".force.com")
_MY_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]*(\.[a-z0-9-]+)*\.my\.salesforce\.com$")
_ID_URL_RE = re.compile(r"/id/(00D[A-Za-z0-9]{12,15})/(005[A-Za-z0-9]{12,15})/?$")


class OAuthError(ValueError):
    """Something MinuteX refuses. `code` is stable; the message is safe to
    show and never contains a token, code or secret."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _s(v):
    return "" if v is None else str(v).strip()


def is_salesforce_url(url):
    """HTTPS on the default port, no credentials, on a Salesforce host."""
    try:
        parts = urllib.parse.urlsplit(_s(url))
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return (parts.scheme == "https" and not parts.username and not parts.password
            and parts.port in (None, 443) and host.endswith(_SF_HOST_SUFFIXES))


def login_host(environment, my_domain=""):
    """The login host for the admin's choice: production, sandbox, or the
    org's own My Domain (e.g. acme.my.salesforce.com or
    acme--dev.sandbox.my.salesforce.com)."""
    env = _s(environment).lower() or "production"
    if env == "production":
        return LOGIN_PRODUCTION
    if env == "sandbox":
        return LOGIN_SANDBOX
    if env == "my_domain":
        host = _s(my_domain).lower()
        host = re.sub(r"^https://", "", host).split("/")[0]
        if not _MY_DOMAIN_RE.match(host):
            raise OAuthError("invalid_my_domain",
                             "Enter your Salesforce My Domain, like acme.my.salesforce.com.")
        return "https://" + host
    raise OAuthError("invalid_environment", "Choose production, sandbox or My Domain.")


def authorize_url(host, *, client_id, redirect_uri, state, code_challenge):
    """The Salesforce consent page. `prompt=login consent` makes the admin
    sign in as the integration user and approve, even with a session open."""
    if not is_salesforce_url(host):
        raise OAuthError("invalid_login_host", "That isn't a Salesforce login address.")
    query = urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri,
        "scope": SCOPES, "state": state, "code_challenge": code_challenge,
        "code_challenge_method": "S256", "prompt": "login consent",
    })
    return f"{host}{AUTHORIZE_PATH}?{query}"


def token_form(*, code, code_verifier, client_id, client_secret, redirect_uri):
    form = {"grant_type": "authorization_code", "code": code,
            "code_verifier": code_verifier, "client_id": client_id,
            "redirect_uri": redirect_uri}
    if client_secret:
        form["client_secret"] = client_secret
    return form


def refresh_form(*, refresh_token, client_id, client_secret):
    form = {"grant_type": "refresh_token", "refresh_token": refresh_token,
            "client_id": client_id}
    if client_secret:
        form["client_secret"] = client_secret
    return form


def signature_ok(token, client_secret):
    """Salesforce signs id + issued_at with the client secret. Checked when a
    secret is configured; a mismatch means the response wasn't Salesforce's."""
    sig = _s(token.get("signature"))
    if not client_secret or not sig:
        return not client_secret
    mac = hmac.new(client_secret.encode("utf-8"),
                   (_s(token.get("id")) + _s(token.get("issued_at"))).encode("utf-8"),
                   hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(mac).decode("ascii"), sig)


def parse_token(obj, *, require_refresh, client_secret=""):
    """The fields MinuteX keeps from a token response, or OAuthError.

    Returns {access_token, refresh_token, instance_url, org_id, user_id,
    issued_at}. On a refresh response `refresh_token` is "" unless the app
    has Refresh Token Rotation on; then it is the successor to store."""
    if not isinstance(obj, dict):
        raise OAuthError("invalid_token_response", "Salesforce sent an unexpected answer.")
    if obj.get("error"):
        code = "invalid_grant" if obj.get("error") == "invalid_grant" else "token_refused"
        raise OAuthError(code, "Salesforce refused the sign-in. Connect again.")
    access = _s(obj.get("access_token"))
    refresh = _s(obj.get("refresh_token"))
    instance = _s(obj.get("instance_url")).rstrip("/")
    if not access:
        raise OAuthError("invalid_token_response", "Salesforce didn't return access.")
    if require_refresh and not refresh:
        raise OAuthError("no_refresh_token",
                         "Salesforce didn't return a lasting connection. Check that the "
                         "MinuteX app allows the refresh_token scope.")
    if not is_salesforce_url(instance):
        raise OAuthError("invalid_instance_url", "Salesforce sent an address MinuteX won't use.")
    match = _ID_URL_RE.search(_s(obj.get("id")))
    if not match:
        raise OAuthError("invalid_token_response", "Salesforce didn't say which org this is.")
    if not signature_ok(obj, client_secret):
        raise OAuthError("bad_signature", "Salesforce's answer couldn't be verified.")
    return {"access_token": access, "refresh_token": refresh, "instance_url": instance,
            "org_id": match.group(1), "user_id": match.group(2),
            "issued_at": _s(obj.get("issued_at"))}


def same_org(a, b):
    """15- and 18-character forms of one org id are the same org."""
    return _s(a)[:15] == _s(b)[:15] and bool(_s(a))
