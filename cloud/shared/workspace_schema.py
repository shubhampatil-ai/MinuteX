"""workspace_schema.py — the Workspace / Membership / Invitation vocabulary.

PURE MODULE, exactly like share_schema and mom_schema: no boto3, no network,
no environment. Row shapes, validation, token handling and the role predicates
live here so they can be unit-tested without AWS and reused identically by the
Lambda and by any migration script. The Lambda owns the reads and writes; this
module owns what a valid row LOOKS like and who a role lets act.

WHY A THIRD TENANCY CONCEPT DOES NOT APPEAR HERE.
The codebase already states its tenancy rule (userapi, the AI TENANCY block):
`owner_user_id` is the boundary, and "if an org layer is added later, AIContext
is the one place that has to learn about it". This module is that org layer. It
deliberately does NOT redefine ownership for existing resources — Phase 2A adds
the workspace tables and the authorization helpers and touches nothing else.
Resource rows keep the owner fields they already have until Phase 2B.

THE THREE IDS, AND WHY THEY ARE DIFFERENT THINGS.

  workspace_id   WHERE a resource lives. The ownership/billing boundary.
  created_by     WHO made it. Never an authorization input on its own, but
                 the honest answer to "whose work was this", which survives
                 the creator leaving the organisation.
  user_id        WHO is asking. Comes from the JWT and nowhere else.

Conflating the first two is the bug this vocabulary exists to prevent: a
meeting recorded by Rahul for ABC Realty belongs to ABC Realty, so it must not
vanish when Rahul leaves, and it must not be readable from Rahul's personal
workspace.

PERSONAL WORKSPACE IDS ARE DERIVED, NOT RANDOM.
`personal_workspace_id(user_id)` is a pure function, so every reader can
compute a user's personal workspace without a lookup and without a migration
having run yet. That is what lets Phase 2A ship additively: code can resolve
"this row has no workspace_id, so it is its owner's personal workspace"
identically before, during and after any backfill. A random uuid would have
made the backfill a correctness gate instead of an optimization.
"""

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Workspace types
# ---------------------------------------------------------------------------
TYPE_PERSONAL = "PERSONAL"
TYPE_ORGANISATION = "ORGANISATION"
WORKSPACE_TYPES = (TYPE_PERSONAL, TYPE_ORGANISATION)

# ---------------------------------------------------------------------------
# Workspace status
# ---------------------------------------------------------------------------
STATUS_ACTIVE = "ACTIVE"
STATUS_SUSPENDED = "SUSPENDED"
STATUS_DELETED = "DELETED"
WORKSPACE_STATUSES = (STATUS_ACTIVE, STATUS_SUSPENDED, STATUS_DELETED)

# ---------------------------------------------------------------------------
# Roles.
#
# ORDERED weakest -> strongest. `role_at_least` compares by INDEX in this
# tuple, so adding a role between two existing ones is a one-line change and
# every comparison keeps working. A dict of numbers would have to be edited in
# two places and can drift.
# ---------------------------------------------------------------------------
ROLE_MEMBER = "MEMBER"
ROLE_MANAGER = "MANAGER"
ROLE_OWNER = "OWNER"
ROLES = (ROLE_MEMBER, ROLE_MANAGER, ROLE_OWNER)

# ---------------------------------------------------------------------------
# Membership status. REMOVED is kept as a row rather than deleted so that
# "was this person ever a member" stays answerable for audit, and so a
# re-invitation is an update rather than a resurrection with a new join date.
# Only ACTIVE grants access — see membership_is_active.
# ---------------------------------------------------------------------------
MEMBERSHIP_ACTIVE = "ACTIVE"
MEMBERSHIP_INVITED = "INVITED"
MEMBERSHIP_REMOVED = "REMOVED"
MEMBERSHIP_STATUSES = (MEMBERSHIP_ACTIVE, MEMBERSHIP_INVITED,
                       MEMBERSHIP_REMOVED)

# ---------------------------------------------------------------------------
# Invitation status
# ---------------------------------------------------------------------------
INVITE_PENDING = "PENDING"
INVITE_ACCEPTED = "ACCEPTED"
INVITE_EXPIRED = "EXPIRED"
INVITE_CANCELLED = "CANCELLED"
# IN-FLIGHT, not a product state. The acceptance route claims the invitation
# (PENDING -> ACCEPTING) before it writes the membership, so single-use is
# decided before anything is created and a failure can be compensated back to
# PENDING. See accept_invitation.
#
# It is deliberately NOT open (invite_is_open returns False), so a token
# stranded here by a crash between the claim and the finalize FAILS CLOSED —
# it cannot be replayed, and an owner re-invites instead. The alternative,
# treating it as open, would turn every mid-flight failure into a replayable
# token.
INVITE_ACCEPTING = "ACCEPTING"
INVITE_STATUSES = (INVITE_PENDING, INVITE_ACCEPTING, INVITE_ACCEPTED,
                   INVITE_EXPIRED, INVITE_CANCELLED)

# 32 bytes, the same size share_schema uses. token_urlsafe(32) is ~43 chars.
INVITE_TOKEN_BYTES = 32
INVITE_TTL_DAYS = 7

MAX_NAME = 100
MAX_COMPANY_FIELD = 200
MAX_ADDRESS = 500

# Organisation profile fields. Free-form and entirely OPTIONAL — none of them
# is an identity or an authorization input, so none is validated beyond a
# length cap. `domain` in particular is stored for display only: treating it as
# proof of employment would be an authentication decision, and this module does
# not make those (see the EMAIL / IDENTITY note at the bottom of this file).
ORG_PROFILE_FIELDS = ("company_name", "company_email", "domain", "phone",
                      "address", "industry")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# A personal workspace id is derived from the user id, so it must be
# recognisable on sight and impossible to confuse with an organisation's.
PERSONAL_PREFIX = "wsp_"
ORG_PREFIX = "wso_"


# ---------------------------------------------------------------------------
# Ids
# ---------------------------------------------------------------------------
def personal_workspace_id(user_id):
    """The DERIVED personal workspace id for a user. Pure and total.

    Deterministic on purpose (see the module docstring): any reader can
    compute it, so a row with no workspace_id can be resolved to its owner's
    personal workspace without a lookup and without the backfill having run.
    """
    return f"{PERSONAL_PREFIX}{user_id}"


def is_personal_workspace_id(workspace_id):
    return str(workspace_id or "").startswith(PERSONAL_PREFIX)


def user_id_from_personal_workspace(workspace_id):
    """The owner's user_id, or "" when this is not a personal workspace id."""
    wid = str(workspace_id or "")
    if not wid.startswith(PERSONAL_PREFIX):
        return ""
    return wid[len(PERSONAL_PREFIX):]


def new_organisation_id():
    """A fresh organisation workspace id.

    Random, unlike the personal one: an organisation is not derived from any
    single user (its owner can change), so there is nothing to derive it from.
    """
    return f"{ORG_PREFIX}{secrets.token_hex(8)}"


def new_invitation_id():
    return secrets.token_hex(8)


# ---------------------------------------------------------------------------
# Invitation tokens.
#
# Same reasoning as share_schema.hash_token: a 256-bit random token has nothing
# to guess, so plain SHA-256 is correct and a password KDF would only add
# latency. The RAW token leaves the server exactly once, in the response that
# creates the invitation; only the hash is ever stored.
# ---------------------------------------------------------------------------
def new_invite_token():
    return secrets.token_urlsafe(INVITE_TOKEN_BYTES)


def hash_invite_token(token):
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def token_looks_valid(token):
    """Cheap shape check before any database read — mirrors share_schema."""
    if not isinstance(token, str):
        return False
    if not (20 <= len(token) <= 128):
        return False
    return re.fullmatch(r"[A-Za-z0-9_-]+", token) is not None


def redact_token(token):
    """A loggable stand-in. Raw invitation tokens must never reach CloudWatch."""
    if not token:
        return "<none>"
    return f"sha256:{hash_invite_token(token)[:12]}"


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------
def _now(now=None):
    return now or datetime.now(timezone.utc)


def _iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value):
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def invite_expiry(now=None, days=INVITE_TTL_DAYS):
    return _iso(_now(now) + timedelta(days=days))


def is_expired(invitation, now=None):
    """True when expires_at has passed.

    An UNPARSEABLE expiry counts as EXPIRED, and so does a MISSING one. Both
    are the safe direction, and note this differs from share_schema, where a
    blank expiry means "never expires": a share link is deliberately allowed
    to be permanent, but an invitation that never expires is a standing key to
    an organisation. Fail closed.
    """
    raw = invitation.get("expires_at")
    if not raw:
        return True
    at = _parse_iso(raw)
    if at is None:
        return True
    now = _now(now)
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return at <= now


def invite_is_open(invitation, now=None):
    """True when this invitation can still be accepted.

    PENDING *and* unexpired. A row is not rewritten to EXPIRED by a timer —
    there is no scheduler in this account (see the Phase 1 audit) — so expiry
    is always computed at read time and the stored status is only ever moved
    by an explicit action (accept, cancel).
    """
    if not invitation:
        return False
    if invitation.get("status") != INVITE_PENDING:
        return False
    return not is_expired(invitation, now=now)


def effective_invite_status(invitation, now=None):
    """The status to SHOW, which is not always the status stored.

    A PENDING row whose expiry has passed reads as EXPIRED everywhere without
    needing a write. Keeps the API honest without inventing a sweeper.
    """
    stored = invitation.get("status")
    if stored == INVITE_PENDING and is_expired(invitation, now=now):
        return INVITE_EXPIRED
    return stored


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------
def role_rank(role):
    """Index in ROLES, or -1 for anything unrecognised.

    -1, not 0: an unknown role must be WEAKER than MEMBER, so a corrupted or
    future role value can never satisfy a role_at_least check. Failing closed
    matters more here than tolerating bad data.
    """
    try:
        return ROLES.index(role)
    except ValueError:
        return -1


def role_at_least(role, minimum):
    """Is `role` at least as strong as `minimum`?"""
    r = role_rank(role)
    return r >= 0 and r >= role_rank(minimum)


def is_valid_role(role):
    return role in ROLES


def membership_is_active(membership):
    return bool(membership) and membership.get("status") == MEMBERSHIP_ACTIVE


def workspace_is_active(workspace):
    return bool(workspace) and workspace.get("status") == STATUS_ACTIVE


# ---------------------------------------------------------------------------
# Capabilities.
#
# ACTIONS are separate from VISIBILITY, deliberately (the brief calls this out
# and the existing task layer already works this way: _task_permissions decides
# what you may DO, while _visible_task decides what you may SEE). This table
# answers only the first question. Which meetings a member can read is a
# resource-level question and is NOT answered here — it arrives in Phase 2B.
# ---------------------------------------------------------------------------
CAP_MANAGE_MEMBERS = "manage_members"
CAP_MANAGE_SETTINGS = "manage_settings"
CAP_MANAGE_INTEGRATIONS = "manage_integrations"
CAP_VIEW_ALL_MEETINGS = "view_all_meetings"
CAP_TRANSFER_OWNERSHIP = "transfer_ownership"
CAP_DELETE_WORKSPACE = "delete_workspace"
CAP_MANAGE_BILLING = "manage_billing"
# EDIT/DELETE a shared organisation contact (Phase 2C). Note what this is
# NOT: reading and CREATING an organisation contact need no capability at
# all — every active member may do both, because a shared address book
# nobody can add to is useless. Only changing or removing someone else's
# entry is privileged.
CAP_MANAGE_CONTACTS = "manage_contacts"

# Minimum role for each capability. OWNER-only entries are the ones that can
# end the organisation or hand it away — a MANAGER manages, but cannot dispose.
_CAP_MIN_ROLE = {
    CAP_MANAGE_MEMBERS: ROLE_MANAGER,
    CAP_MANAGE_SETTINGS: ROLE_MANAGER,
    CAP_MANAGE_INTEGRATIONS: ROLE_MANAGER,
    CAP_VIEW_ALL_MEETINGS: ROLE_MANAGER,
    CAP_MANAGE_CONTACTS: ROLE_MANAGER,
    CAP_TRANSFER_OWNERSHIP: ROLE_OWNER,
    CAP_DELETE_WORKSPACE: ROLE_OWNER,
    CAP_MANAGE_BILLING: ROLE_OWNER,
}
CAPABILITIES = tuple(_CAP_MIN_ROLE)


def role_can(role, capability):
    """May this role perform this capability? Unknown capability -> False."""
    minimum = _CAP_MIN_ROLE.get(capability)
    if minimum is None:
        return False
    return role_at_least(role, minimum)


def capabilities_for(role):
    """The capability map handed to clients so the UI need not re-derive it.

    Presentation only. Every route still enforces its own requirement — the
    same contract _task_permissions has with the task screens.
    """
    return {cap: role_can(role, cap) for cap in CAPABILITIES}


# ===========================================================================
# RESOURCE OWNERSHIP (Phase 2B)
#
# Every workspace-owned resource carries two independent fields:
#
#   workspace_id   WHERE it lives — the ownership boundary.
#   created_by     WHO made it — provenance, never an authorization input on
#                  its own, and it must SURVIVE the creator leaving.
#
# THE ABSENT-WORKSPACE RULE. Existing rows predate this feature and have no
# workspace_id. `resolve_workspace_id` maps those onto the owner's DERIVED
# personal workspace, so an unmigrated row is not ambiguous and not orphaned —
# it is personal, which is what it has always been. That is the property that
# makes the Phase 2B backfill an optimization rather than a correctness gate,
# exactly as in Phase 2A.
#
# THE RULE IS ONE-WAY. An absent workspace_id resolves to PERSONAL and can
# never resolve to an organisation: there is no input to this function that
# produces an ORG_PREFIX id out of a missing value. Existing personal data
# therefore cannot become organisation data by accident, which is the
# migration guarantee the product requires.
# ===========================================================================
def resolve_workspace_id(row, owner_field="user_id"):
    """The workspace a stored row belongs to. Total, and never guesses.

    A row that carries workspace_id is taken at its word — it was stamped at
    creation from a VERIFIED membership. A row without one is personal to its
    owner, which is the only thing it could have been before workspaces
    existed.

    Returns "" only when the row has neither, which means the row is
    ownerless (a legacy device recording whose device was never paired). The
    caller must treat "" as "no workspace", never as "any workspace".
    """
    if not row:
        return ""
    stored = str(row.get("workspace_id") or "").strip()
    if stored:
        return stored
    owner = str(row.get(owner_field) or "").strip()
    return personal_workspace_id(owner) if owner else ""


def is_organisation_workspace_id(workspace_id):
    return str(workspace_id or "").startswith(ORG_PREFIX)


def stamp(row, workspace_id, created_by=""):
    """Add the ownership fields to a row being created. Mutates and returns.

    `created_by` is only written when supplied, so a caller that has no
    meaningful creator (an async pipeline upsert) does not stamp an empty one
    over a real value written earlier.
    """
    wid = str(workspace_id or "").strip()
    if wid:
        row["workspace_id"] = wid
    creator = str(created_by or "").strip()
    if creator:
        row["created_by"] = creator
    return row


# ---------------------------------------------------------------------------
# Meeting access (Phase 2B).
#
# ACCESS TYPES are a RECORD OF WHY someone can reach a meeting, not a
# permission grant in themselves — the same separation the task layer already
# keeps between _task_permissions (what you may do) and _visible_task (what
# you may see).
#
# ORGANISATION VISIBILITY, from the brief:
#     OWNER / MANAGER  -> every meeting in the workspace
#     CREATOR          -> their own
#     PARTICIPANT      -> meetings they were tagged in
#     EXPLICIT         -> meetings shared with them directly
#     other MEMBER     -> denied
#
# Membership alone is NOT access. A MEMBER of ABC Realty cannot read a
# colleague's meeting merely by being in the same organisation.
# ---------------------------------------------------------------------------
ACCESS_CREATOR = "CREATOR"
ACCESS_PARTICIPANT = "PARTICIPANT"
ACCESS_EXPLICIT = "EXPLICIT"
ACCESS_TYPES = (ACCESS_CREATOR, ACCESS_PARTICIPANT, ACCESS_EXPLICIT)


def role_sees_all_meetings(role):
    """True for the roles that may read every meeting in their workspace."""
    return role_can(role, CAP_VIEW_ALL_MEETINGS)


def new_meeting_access(meeting_id, user_id, access_type, now_iso,
                       workspace_id="", granted_by=""):
    item = {
        "meeting_id": meeting_id,
        "user_id": user_id,
        "access_type": access_type,
        "created_at": now_iso,
    }
    if workspace_id:
        item["workspace_id"] = workspace_id
    if granted_by:
        item["granted_by"] = granted_by
    return item


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
class WorkspaceValidationError(ValueError):
    """Raised for a malformed workspace/membership/invitation input.

    A plain ValueError subclass rather than the Lambda's ApiError, because
    this module must stay importable without the Lambda. The caller maps it
    onto a 400.
    """


def clean_name(raw, *, field="name", required=True, limit=MAX_NAME):
    name = str(raw or "").strip()
    if not name:
        if required:
            raise WorkspaceValidationError(f"{field} required")
        return ""
    return name[:limit]


def clean_email(raw, *, required=True):
    email = str(raw or "").strip().lower()
    if not email:
        if required:
            raise WorkspaceValidationError("email required")
        return ""
    if not _EMAIL_RE.match(email):
        raise WorkspaceValidationError("valid email required")
    return email


def clean_role(raw, *, default=ROLE_MEMBER):
    role = str(raw or default or "").strip().upper()
    if not is_valid_role(role):
        raise WorkspaceValidationError(
            f"role must be one of {', '.join(ROLES)}")
    return role


def clean_org_profile(data):
    """The optional organisation profile fields, trimmed and length-capped.

    Absent keys stay absent rather than becoming "" — the same sparse-attribute
    discipline the rest of the codebase follows, so a field the user never
    filled in does not read back as an empty string they have to clear.
    """
    out = {}
    for field in ORG_PROFILE_FIELDS:
        if field not in data:
            continue
        limit = MAX_ADDRESS if field == "address" else MAX_COMPANY_FIELD
        value = str(data.get(field) or "").strip()[:limit]
        if value:
            out[field] = value
    return out


# ---------------------------------------------------------------------------
# Row builders. These return plain dicts; the Lambda writes them.
# ---------------------------------------------------------------------------
def new_personal_workspace(user_id, now_iso, name="Personal"):
    """The row for a user's own workspace. Id is derived, never random."""
    return {
        "workspace_id": personal_workspace_id(user_id),
        "type": TYPE_PERSONAL,
        "name": name,
        "owner_user_id": user_id,
        "status": STATUS_ACTIVE,
        "created_at": now_iso,
        "updated_at": now_iso,
    }


def new_organisation_workspace(owner_user_id, name, now_iso, profile=None):
    item = {
        "workspace_id": new_organisation_id(),
        "type": TYPE_ORGANISATION,
        "name": clean_name(name),
        "owner_user_id": owner_user_id,
        "status": STATUS_ACTIVE,
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    item.update(clean_org_profile(profile or {}))
    return item


def new_membership(workspace_id, user_id, role, now_iso,
                   status=MEMBERSHIP_ACTIVE, invited_by=""):
    item = {
        "workspace_id": workspace_id,
        "user_id": user_id,
        "role": role,
        "status": status,
        "joined_at": now_iso if status == MEMBERSHIP_ACTIVE else "",
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    if invited_by:
        item["invited_by"] = invited_by
    return item


def new_invitation(workspace_id, email, role, invited_by, token, now_iso,
                   expires_at=None, now=None):
    """An invitation row. Stores the token HASH; the raw token is returned
    separately by the caller and never persisted."""
    return {
        "invitation_id": new_invitation_id(),
        "workspace_id": workspace_id,
        "email": clean_email(email),
        "role": clean_role(role),
        "invited_by": invited_by,
        "token_hash": hash_invite_token(token),
        "status": INVITE_PENDING,
        "expires_at": expires_at or invite_expiry(now=now),
        "created_at": now_iso,
        "updated_at": now_iso,
    }


# ---------------------------------------------------------------------------
# API shapes. Kept here so the Lambda and any future reader agree on exactly
# which fields leave the server — token_hash in particular must never appear.
# ---------------------------------------------------------------------------
def public_workspace(workspace, role="", membership=None):
    out = {
        "workspace_id": workspace.get("workspace_id", ""),
        "type": workspace.get("type", ""),
        "name": workspace.get("name", ""),
        "status": workspace.get("status", ""),
        "is_personal": workspace.get("type") == TYPE_PERSONAL,
        "created_at": workspace.get("created_at", ""),
        "updated_at": workspace.get("updated_at", ""),
    }
    for field in ORG_PROFILE_FIELDS:
        if workspace.get(field):
            out[field] = workspace[field]
    if role:
        out["role"] = role
        out["capabilities"] = capabilities_for(role)
    if membership:
        out["joined_at"] = membership.get("joined_at", "")
    return out


def display_name(user, *, fallback_user_id=""):
    """The best human label for a user row, never a raw id.

    Order: the name they set, then a name DERIVED from the email local-part,
    then the email itself. The derived form exists because `name` is optional
    on every account created before the profile gate, and a members list that
    renders "3f9a1c72-..." is unusable — an id is an identifier, not a name.

    DERIVED ONLY, never written back: the Users row keeps its empty `name`, so
    the moment the person sets a real one it wins here with no migration and
    no risk of this guess being mistaken for something they typed. That is
    also why a deliberately-blank name is not overwritten.

    Returns "" when there is nothing at all to show, so a caller can decide
    between an id and an empty cell rather than having one forced on it.
    """
    row = user or {}
    name = str(row.get("name") or "").strip()
    if name:
        return name[:MAX_NAME]

    email = str(row.get("email") or "").strip()
    if email:
        local = email.split("@", 1)[0]
        # "shubham.patil" / "shubham_patil" / "shubham-patil" -> "Shubham Patil".
        # Digits and single-token locals are left alone rather than mangled:
        # "jsmith2" is not improved by becoming "Jsmith2".
        parts = [p for p in re.split(r"[._\-]+", local) if p]
        if len(parts) > 1 and all(p.isalpha() for p in parts):
            return " ".join(p.capitalize() for p in parts)[:MAX_NAME]
        return email

    return str(fallback_user_id or "")


def public_member(membership, user=None):
    """One row of the Members list.

    The user's email and name come from the Users table and are included
    because a members list without them is unusable — but nothing else from
    that row is copied, and no password material can reach here.
    """
    out = {
        "user_id": membership.get("user_id", ""),
        "role": membership.get("role", ""),
        "status": membership.get("status", ""),
        "joined_at": membership.get("joined_at", ""),
        "created_at": membership.get("created_at", ""),
    }
    if membership.get("invited_by"):
        out["invited_by"] = membership["invited_by"]
    if user:
        out["email"] = user.get("email", "")
        # `name` is the DISPLAY name, so a client never has to decide between
        # a blank and an id. `name_set` tells it whether the person actually
        # chose this — the profile gate needs that distinction, a members list
        # does not.
        out["name"] = display_name(user, fallback_user_id=out["user_id"])
        out["name_set"] = bool(str(user.get("name") or "").strip())
        out["avatar_url"] = user.get("avatar_url", "")
    return out


def public_invitation(invitation, now=None):
    """An invitation as the API returns it.

    `token_hash` is deliberately NOT copied. The raw token appears exactly
    once, in the create response, and is never readable again — the same
    one-shot contract share_schema gives share tokens.
    """
    return {
        "invitation_id": invitation.get("invitation_id", ""),
        "workspace_id": invitation.get("workspace_id", ""),
        "email": invitation.get("email", ""),
        "role": invitation.get("role", ""),
        "invited_by": invitation.get("invited_by", ""),
        "status": effective_invite_status(invitation, now=now),
        "expires_at": invitation.get("expires_at", ""),
        "created_at": invitation.get("created_at", ""),
    }


# ===========================================================================
# IDENTITY (DECIDED — Phase 2B)
#
# THE MODEL, as agreed:
#
#   1. One MinuteX identity == one email.
#   2. Personal and Organisation identities are SEPARATE when the emails
#      differ. rahul@gmail.com and rahul@abcrealty.com are two identities.
#   3. One email can never be both a Personal and an Organisation identity.
#   4. Never silently merge. Never convert a Personal account.
#
# HOW THIS IS ENFORCED WITHOUT CHANGING AUTHENTICATION.
#
# The decisive question was whether rule 3 needs a new "account kind" column
# on Users, and it does NOT — because the rule is not a property of an account
# in isolation. It is a property of the (email, workspace-type) pair, and the
# system already records the workspace type of every membership.
#
#   An identity is ORGANISATION-BOUND     <=> it holds >= 1 ORGANISATION
#                                            membership.
#   An identity is PERSONAL-ONLY          <=> it holds none.
#
# Both are derivable from WorkspaceMemberships, which is already read on the
# authorization path. So:
#
#   * signup, login, password handling, the JWT and _require_auth are all
#     UNCHANGED. No new column, no second Users row, no account kind.
#   * The rule is applied at exactly one moment — invitation acceptance —
#     where a PERSONAL-ONLY identity is refused with a clear product error
#     rather than being merged or converted.
#
# WHY THAT IS THE RIGHT PLACE. Acceptance is the only operation that would
# otherwise cause a personal identity to become organisational. Enforcing it
# at signup instead would mean predicting, at account creation, which kind an
# email will turn out to be — which is exactly the guess the product forbids.
#
# WHAT THIS DOES NOT DO: it does not stop a person from OWNING an organisation
# with their personal email. Creating an organisation is a deliberate act by
# an authenticated user, not an invitation, and the brief's rule is about
# invitations ("an organisation invitation is associated with the invited
# work/company email"). Restricting creation as well would be a second product
# decision nobody has made, so it is recorded here and not invented.
# ===========================================================================
# The four cases from the brief, returned by the acceptance check so the route
# can map each onto the right status code and message.
ACCEPT_OK = "OK"
ACCEPT_WRONG_IDENTITY = "WRONG_IDENTITY"       # Case D — email mismatch
ACCEPT_PERSONAL_IDENTITY = "PERSONAL_IDENTITY"  # Case C — personal-only email
ACCEPT_ALREADY_MEMBER = "ALREADY_MEMBER"        # idempotent re-acceptance


def identity_matches_invitation(user_email, invitation):
    """Case D. The authenticated identity must BE the invited one.

    Compared on the normalized email, because that is what the invitation was
    addressed to and what the Users row stores. A different identity holding a
    valid token must not be able to spend it — the token proves possession of
    the link, not entitlement to the seat.
    """
    invited = str((invitation or {}).get("email") or "").strip().lower()
    holder = str(user_email or "").strip().lower()
    return bool(invited) and invited == holder


def acceptance_verdict(user_email, invitation, *, has_org_membership,
                       already_member, has_personal_data=False):
    """Which of the four cases this acceptance is. Pure; decides nothing else.

    THE BUG THIS FIXES (found in production, Phase 2C).
    ---------------------------------------------------
    This used to refuse acceptance whenever `has_org_membership` was False,
    reasoning that an identity holding no organisation membership must be a
    "personal-only" identity. That is CIRCULAR and made the invitation flow
    completely unusable:

        to join your first organisation you had to already be in one,
        and the only way into your first one is to join it.

    Nobody could ever accept an invitation. Worse, the signal was meaningless
    on its own: EVERY signup gets a Personal workspace unconditionally (the id
    is derived — see personal_workspace_id), so "this is a personal account"
    is true of every identity that has ever existed, including one created
    ten seconds ago specifically to accept an invitation.

    THE RULE NOW, and it is the SAME ONE organisation creation already uses:
    what the product is protecting is a person's EXISTING PERSONAL DATA, not
    the existence of a derived workspace row. So the question is

        has this identity actually USED its personal workspace?

    answered by `has_personal_data` — a recording, task, folder or contact it
    owns (the caller computes it with _has_personal_resources, the same probe
    create_workspace uses). One rule, both paths, no circularity:

        fresh identity, no personal data   -> may join      (Case A/B)
        established personal account        -> refused      (Case C)

    `has_org_membership` is kept as an EXEMPTION rather than a requirement: an
    identity that is already in some organisation has demonstrably not been
    converted, so it may join another even if it has accumulated data there.
    """
    if not identity_matches_invitation(user_email, invitation):
        return ACCEPT_WRONG_IDENTITY
    if already_member:
        return ACCEPT_ALREADY_MEMBER
    # Case C — an established PERSONAL account must not be converted. Skipped
    # for an identity that is already organisational (see above).
    if has_personal_data and not has_org_membership:
        return ACCEPT_PERSONAL_IDENTITY
    return ACCEPT_OK


PERSONAL_IDENTITY_MESSAGE = (
    "This email is registered as a personal MinuteX account, so it cannot be "
    "used to join an organisation. Ask your administrator to send the "
    "invitation to your work email address, then sign up with that address to "
    "accept it. Your personal account and its data are unaffected."
)


# ---------------------------------------------------------------------------
# ORGANISATION CREATION AND THE PERSONAL-WORKSPACE RULE (Phase 2C)
#
# THE RULE, as decided: an identity may create an organisation only if it does
# not already have a Personal Workspace.
#
# WHY THAT CANNOT BE READ LITERALLY. A personal workspace id is DERIVED
# (personal_workspace_id is a pure function of the user id) and synthesized on
# read, which is what makes the Phase 2A migration additive. So every identity
# "has" one from the moment it signs up, and the literal reading would reject
# 100% of organisation creation — including the flow the product asks for.
#
# WHAT IT MEANS INSTEAD (confirmed product decision): the rule guards a
# person's EXISTING PERSONAL DATA, not the existence of a derived row. An
# identity that has actually USED its personal workspace — it owns a
# recording, a task, a folder or a contact — is a personal account, and
# turning it into an organisation owner is the silent conversion the identity
# model forbids. A brand-new identity created in order to start an
# organisation has nothing to convert, so it may proceed.
#
#   no personal resources  -> may create an organisation
#   any personal resource  -> 409, use a separate work identity
#
# This is deliberately NOT an email-domain check. Domain classification is a
# guess (company mail on a custom domain, freelancers on Gmail, regional
# providers), and a wrong guess here either blocks a legitimate business or
# waves through exactly the conversion the rule exists to prevent. Whether
# the identity has personal data is a FACT the database can answer.
#
# The probe itself lives in the Lambda (_has_personal_resources) because it
# reads tables; this module only owns the vocabulary and the message.
# ---------------------------------------------------------------------------
PERSONAL_WORKSPACE_IN_USE_MESSAGE = (
    "This account already has personal meetings, tasks or contacts in your "
    "Personal workspace, so it cannot also become an organisation owner. "
    "Your personal data is unaffected. To create an organisation, sign up "
    "with your work email address and create it from that account."
)


# ===========================================================================
# HISTORICAL NOTE — the conflict this replaced, kept for reviewers
#
# Phase 2A recorded the following as unresolved and refused to implement
# invitation acceptance because of it. The decision above resolves it: model
# (1) is adopted for MEMBERSHIP, and model (2)'s separation rule is enforced
# at acceptance rather than by splitting the account table.
# ---------------------------------------------------------------------------
# EMAIL / IDENTITY — AN UNRESOLVED PRODUCT DECISION, DELIBERATELY NOT DECIDED
#
# Two stated requirements contradict each other:
#
#   (1) "One MinuteX user can belong to multiple workspaces."
#   (2) "Personal and Organisation accounts are completely separate, use
#        different emails, and the same email cannot be used for both."
#
# (1) is a MEMBERSHIP model: one Users row, many WorkspaceMembership rows.
#     That is what this module implements, because it is the only one of the
#     two that the existing schema already supports — Users.user_id is the
#     single identity every one of the 102 routes resolves through.
#
# (2) is an ACCOUNT-SEPARATION model. It requires either a second Users row
#     per human (so one person has two logins and two JWTs), or an account
#     "kind" attribute that makes some emails ineligible for some workspaces.
#     Both change authentication, which Phase 2A is explicitly forbidden to do.
#
# The conflict is REAL, not a wording difference: under (1) rahul@gmail.com
# joins ABC Realty and keeps one login; under (2) that is prohibited and he
# must be invited as rahul@company.com, a different account entirely.
#
# WHAT THIS MODULE THEREFORE DOES NOT DO:
#   - It does not mark any email as personal-only or organisation-only.
#   - It does not merge, link or split accounts.
#   - It does not decide what happens when an invited email already exists.
#
# Invitation acceptance is where the decision became unavoidable, so that
# route was NOT implemented in Phase 2A. RESOLVED in Phase 2B — see the
# IDENTITY (DECIDED) block above, which is the authority. This text is kept
# only to explain why the acceptance route arrived a phase later than the
# tables it uses.
# ===========================================================================
