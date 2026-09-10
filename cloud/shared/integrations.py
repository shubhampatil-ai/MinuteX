"""integrations.py — the generic per-user integration model.

WHAT THIS IS. One place that answers "which external applications has this
user connected, and is the connection usable right now?" — for Gmail today,
and for WhatsApp / Google Calendar / Google Tasks / Slack / whatever comes
next without a second table, a second status vocabulary or a second OAuth
implementation.

WHY IT IS NOT CrmConnections. CrmConnections already stores a per-user OAuth
credential keyed (user_id, provider), and its docstring explicitly leaves the
sort key open "for hubspot/zoho later". That is the right shape and this table
copies it deliberately. What it does NOT copy is the SCOPE: CrmConnections
carries Salesforce-shaped attributes (instance_url, org_id, sf_username) and
a CRM field-mapping config, and every consumer of it reads those. Widening it
to hold Gmail would mean either Salesforce columns sitting empty on Gmail rows
or a union type nobody can reason about — and, worse, it would put the CRM
mapping config and the mail credential in one item that two unrelated features
both write. So: same proven shape, separate table, and Salesforce stays where
it is. Migrating Salesforce onto this table later is a data move, not a
redesign — see PROVIDERS below.

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
PROVIDER_SALESFORCE = "salesforce"
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
        # Salesforce WORKS — it predates this system and is connected through
        # /crm/salesforce/* against the CrmConnections table. So it is a fully
        # available integration, NOT a Coming Soon card, and its real
        # connection state is read from that table (see salesforce_row_status
        # below) rather than being reported as "not connected" because this
        # system does not own the row.
        #
        # `managed_elsewhere` is what stops the generic flow being applied to
        # it: /integrations/salesforce/connect does not exist, so the app
        # routes this card to the existing Salesforce screen, which owns both
        # connecting and the field mapping. The flag is about WHICH FLOW runs,
        # never about whether the integration works.
        "provider": PROVIDER_SALESFORCE,
        "name": "Salesforce",
        "category": "crm",
        "description": "Push meeting notes onto your CRM records.",
        "available": True,
        "managed_elsewhere": True,
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

# Providers whose connect flow THIS system runs.
#
# `available` alone is not the test: Salesforce is available (it genuinely
# works) but is connected through /crm/salesforce/*, so
# POST /integrations/salesforce/connect must still be refused. Excluding
# managed_elsewhere here is what keeps "this integration works" and "this
# integration uses the generic flow" as two separate facts — the day
# Salesforce is migrated onto the Integrations table, dropping its
# managed_elsewhere flag is the only change needed.
CONNECTABLE = {p["provider"] for p in PROVIDERS
               if p.get("available") and not p.get("managed_elsewhere")}

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
        "managed_elsewhere": bool(meta.get("managed_elsewhere")),
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


def salesforce_row(crm_row) -> dict:
    """A CrmConnections (PERSONAL) row in this module's row shape.

    Salesforce predates the Integrations table and stores different attribute
    names (`sf_username`, no `status`, no `scopes`). Translating here — rather
    than special-casing Salesforce inside public_status — keeps the catalog
    uniform: every card downstream reads one shape, and the Integrations
    screen needs no knowledge that one provider's row lives elsewhere.

    STATUS IS INFERRED, and only two of the four states are reachable.
    CrmConnections has no status attribute: the row exists or it does not.
    A dead Salesforce refresh token is discovered by _sf_call at CALL time and
    surfaced as 409 there, so this cannot report REAUTH_REQUIRED without
    making a network call the catalog has no business making. Reporting
    CONNECTED for a row whose token later turns out to be dead is the honest
    limit of what a cheap read can know — and the Salesforce screen itself
    already probes and shows "Reconnect required" when the user opens it.
    """
    if not crm_row:
        return None
    return {
        "status": STATUS_CONNECTED,
        "status_message": "",
        "refresh_token_enc": crm_row.get("refresh_token_enc", ""),
        # The org username is what the user recognises, exactly as the Gmail
        # address is for Gmail.
        "account_identifier": crm_row.get("sf_username", ""),
        "account_name": crm_row.get("instance_url", ""),
        "scopes": [],
        "connected_at": crm_row.get("connected_at", ""),
        "updated_at": crm_row.get("updated_at", ""),
    }


def org_salesforce_row(org_crm_row) -> dict:
    """An OrgCrmConnections (ORGANISATION, Phase 2D.1/2D.2) row in this
    module's row shape — the workspace-scoped twin of salesforce_row above.

    Same translation, same inference limits (see salesforce_row's docstring
    for why only CONNECTED/NOT_CONNECTED are reachable from a cheap read).
    Kept as a SEPARATE function rather than a `scope` parameter on
    salesforce_row: the two source rows have different shapes
    (`connected_by_user_id` here, no such concept for Personal) and different
    tables, and a shared function would have to branch on scope internally
    for no real reuse gained — the translation logic itself is three lines.
    """
    if not org_crm_row:
        return None
    return {
        "status": STATUS_CONNECTED,
        "status_message": "",
        "refresh_token_enc": org_crm_row.get("refresh_token_enc", ""),
        "account_identifier": org_crm_row.get("sf_username", ""),
        "account_name": org_crm_row.get("instance_url", ""),
        "scopes": [],
        "connected_at": org_crm_row.get("connected_at", ""),
        "updated_at": org_crm_row.get("updated_at", ""),
    }


def public_org_salesforce_status(org_crm_row=None) -> dict:
    """The ORGANISATION Salesforce card, in the SAME shape public_status()
    returns for every other provider — so the frontend renders the
    Personal-Salesforce card and the Organisation-Salesforce card with one
    component, never two.

    Deliberately its own function rather than a `workspace_id` parameter
    threaded through public_status()/catalog(): those two stay exactly what
    they already are — the per-USER catalog, unaware that workspaces exist.
    An organisation's Salesforce connection is not a row in that catalog and
    must never be merged into it (see the module docstring's table-separation
    rationale) — it is a second, independent status the workspace-aware
    screen fetches and renders alongside the personal one.
    """
    out = public_status(PROVIDER_SALESFORCE, org_salesforce_row(org_crm_row))
    out["scope"] = "organisation"
    # configured / config_cleared_reason are ORGANISATION-CONFIG concepts
    # (Phase 2D.2) with no Personal equivalent surfaced here — added only
    # when present so a Personal-shaped consumer of public_status() (which
    # never sees this function) is not required to know about them.
    if org_crm_row:
        out["configured"] = bool(org_crm_row.get("config", {}).get("mappings")) \
            if isinstance(org_crm_row.get("config"), dict) else False
        if org_crm_row.get("config_cleared_reason"):
            out["config_cleared_reason"] = org_crm_row["config_cleared_reason"]
    return out


def catalog(rows_by_provider=None) -> list:
    """Every integration MinuteX knows about, with this user's status on each.

    The catalog is the full PROVIDERS list, not just the connected ones: the
    Integrations screen must render "Coming Soon" cards too, and deriving that
    list on the client would mean shipping a new build to announce a new
    integration.

    `rows_by_provider` may legitimately mix sources — the caller passes the
    Integrations table's rows plus a translated CrmConnections row for
    Salesforce (see salesforce_row). That is the whole reason this takes a
    mapping rather than reading a table itself.
    """
    rows_by_provider = rows_by_provider or {}
    return [public_status(p["provider"], rows_by_provider.get(p["provider"]))
            for p in PROVIDERS]
