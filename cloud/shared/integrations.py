"""integrations.py — the generic per-user integration model.

WHAT THIS IS. One place that answers "which external applications has this
user connected, and is the connection usable right now?" — for Gmail today,
and for WhatsApp / Google Calendar / Google Tasks / Slack / whatever comes
next without a second table, a second status vocabulary or a second OAuth
implementation.

One row per (user_id, provider) in the Integrations table. Salesforce is not
an integration here: it is reached through the workspace's Salesforce Site
endpoint (the site-delivery / site-salesforce routes), which is configured per
workspace, not per user.

THE STATUS VOCABULARY IS THE POINT. The product requirement is that the app
never guesses; the backend is the source of truth. A row's mere existence is
NOT "connected" — a revoked refresh token leaves the row in place and the
honest answer is REAUTH_REQUIRED. So status is stored, transitions are
explicit, and every send path checks it. The four states:

    NOT_CONNECTED     no row (or a disconnected one)
    CONNECTED         a credential we have no reason to doubt
    REAUTH_REQUIRED   the credential is dead — the user must reconnect
    ERROR             the provider rejected us for a reason reconnecting
                      will not fix (scope revoked at the org level, etc.)

NOT_CONNECTED is never stored: it is what the absence of a row means. The
other three are.

TOKENS NEVER LEAVE THE BACKEND. This module stores a refresh token only as
KMS ciphertext, under `refresh_token_enc`, and public_status() below is an
assembled dict — like share_schema.public_payload(), it can only emit what it
explicitly lists, so a new attribute cannot leak by being forgotten in a
deny-list.
"""

from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Providers. The registry is data, not code: adding WhatsApp later means one
# entry here plus a provider class in the Lambda — no change to the table, the
# status routes, the app's integration list rendering, or this module.
#
# `available` is what separates a working integration from a card the UI shows
# as "Coming Soon". It is deliberately server-driven: the day WhatsApp ships,
# flipping this flag lights the card up for every client already installed,
# and an old build cannot offer a connect button for something the backend
# cannot yet honour.
# ---------------------------------------------------------------------------
PROVIDER_GMAIL = "gmail"
PROVIDER_WHATSAPP = "whatsapp"
PROVIDER_GOOGLE_CALENDAR = "google_calendar"
PROVIDER_GOOGLE_TASKS = "google_tasks"

# Ordered — the app renders the list in exactly this order, so the ordering
# decision lives in one place rather than being duplicated per client.
PROVIDERS = [
    {
        "provider": PROVIDER_GMAIL,
        "name": "Gmail",
        "category": "communication",
        "description": "Send meeting documents and meeting-related "
                       "communication from your own address.",
        "available": True,
    },
    {
        "provider": PROVIDER_WHATSAPP,
        "name": "WhatsApp",
        "category": "communication",
        "description": "Share meeting summaries and action items over WhatsApp.",
        "available": False,
    },
    {
        "provider": PROVIDER_GOOGLE_CALENDAR,
        "name": "Google Calendar",
        "category": "productivity",
        "description": "See scheduled meetings and attach minutes to events.",
        "available": False,
    },
    {
        "provider": PROVIDER_GOOGLE_TASKS,
        "name": "Google Tasks",
        "category": "productivity",
        "description": "Sync MinuteX action items to your task list.",
        "available": False,
    },
]

PROVIDERS_BY_ID = {p["provider"]: p for p in PROVIDERS}

# Providers whose connect flow this system runs: the available ones.
CONNECTABLE = {p["provider"] for p in PROVIDERS if p.get("available")}

# ---------------------------------------------------------------------------
# Status.
# ---------------------------------------------------------------------------
STATUS_NOT_CONNECTED = "NOT_CONNECTED"
STATUS_CONNECTED = "CONNECTED"
STATUS_REAUTH_REQUIRED = "REAUTH_REQUIRED"
STATUS_ERROR = "ERROR"

STORED_STATUSES = (STATUS_CONNECTED, STATUS_REAUTH_REQUIRED, STATUS_ERROR)

# The one status that permits an outbound operation. Checked server-side on
# every send — hiding the button in the app is presentation, not enforcement.
USABLE_STATUSES = (STATUS_CONNECTED,)


def is_usable(row) -> bool:
    """True when this connection row may be used to act on the user's behalf.

    Deliberately conservative and deliberately NOT "does a token exist": a row
    whose refresh token was revoked still has ciphertext in it. Status is the
    authority, and a row missing one is treated as unusable rather than
    assumed good.
    """
    if not row:
        return False
    if not row.get("refresh_token_enc"):
        return False
    return row.get("status") in USABLE_STATUSES


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def public_status(provider: str, row=None) -> dict:
    """The status of ONE integration, in API shape.

    ASSEMBLED, never filtered. The row holds `refresh_token_enc`, and the only
    reason it cannot reach a client is that this function never mentions it.
    A deny-list would work today and fail the first time the row grows a field.

    `account_identifier` is the connected account as the user knows it — the
    Gmail address, later a phone number or an org name. One generic field
    rather than a per-provider one, because the UI renders it identically.
    """
    meta = PROVIDERS_BY_ID.get(provider, {})
    out = {
        "provider": provider,
        "name": meta.get("name", provider),
        "category": meta.get("category", ""),
        "description": meta.get("description", ""),
        "available": bool(meta.get("available")),
        "status": STATUS_NOT_CONNECTED,
        "connected": False,
        "account_identifier": "",
        "account_name": "",
        "scopes": [],
        "connected_at": "",
        "updated_at": "",
        # Present only when status is REAUTH_REQUIRED/ERROR: the reason, in
        # words a user can act on. Never a provider error code — those go to
        # the log. See gmail_error_message() in the Lambda.
        "message": "",
    }
    if not row:
        return out

    status = row.get("status") or STATUS_NOT_CONNECTED
    if status not in STORED_STATUSES:
        status = STATUS_NOT_CONNECTED
    out.update({
        "status": status,
        "connected": status == STATUS_CONNECTED,
        "account_identifier": row.get("account_identifier", ""),
        "account_name": row.get("account_name", ""),
        "scopes": list(row.get("scopes") or []),
        "connected_at": row.get("connected_at", ""),
        "updated_at": row.get("updated_at", ""),
        "message": row.get("status_message", "") if status != STATUS_CONNECTED else "",
    })
    return out


def catalog(rows_by_provider=None) -> list:
    """Every integration MinuteX knows about, with this user's status on each.

    The catalog is the full PROVIDERS list, not just the connected ones: the
    Integrations screen must render "Coming Soon" cards too, and deriving that
    list on the client would mean shipping a new build to announce a new
    integration.

    `rows_by_provider` is the caller's Integrations rows keyed by provider;
    taking a mapping keeps this module free of any table access.
    """
    rows_by_provider = rows_by_provider or {}
    return [public_status(p["provider"], rows_by_provider.get(p["provider"]))
            for p in PROVIDERS]
