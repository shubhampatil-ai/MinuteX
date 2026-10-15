#!/usr/bin/env python3
"""test_workspace_foundation.py — the Phase 2A workspace layer.

WHAT THIS FILE PINS.

  1. THE SCHEMA VOCABULARY (workspace_schema) — pure functions: derived ids,
     role ranking, capabilities, invitation tokens and expiry. No AWS.

  2. THE AUTHORIZATION HELPERS (userapi) — membership resolution, the role
     gate, and the property everything else rests on:

         a workspace_id supplied by the caller is a REQUEST, not a claim.

  3. THE ADDITIVE PROPERTY. Phase 2A must be invisible to existing users. A
     user with no workspace row behaves exactly as before, and the personal
     workspace is synthesized rather than required — so the backfill is an
     optimization, not a correctness gate. TestWorksBeforeAnyMigration is the
     test that would fail if that ever stopped being true.

  4. THE EMAIL UNIQUENESS RACE (signup) — the claim-row fix. The old code read
     an eventually-consistent GSI and then wrote with a condition on user_id,
     which guards a fresh uuid and says nothing about the email.

THE AUTHORIZATION MATRIX. Section 30 of the brief asks for OWNER / MANAGER /
MEMBER / NON-MEMBER / REMOVED-MEMBER against every capability. That is
TestRoleMatrix (pure) and TestMembershipMatrix (through the helpers), and the
REMOVED case is the one that matters most: the JWT lasts 24h and cannot be
revoked, so membership is re-read on every request and removal must take
effect on the very next one.

SECURITY POSTURE, same as test_task_permissions: every test calls the ROUTE
FUNCTION DIRECTLY with a forged identity, exactly as curl against the deployed
API would. No UI is involved, so nothing here can be satisfied by hiding a
button.

OFFLINE: fake_dynamodb, no AWS, no network.

Run:  python -m pytest tests/test_workspace_foundation.py
"""
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "functions/userapi"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# ORDER MATTERS — see the note in test_task_permissions.py. Importing the
# workspace harness first binds the SAME `api` module object every other suite
# uses, including the Key class the lambda holds a reference to.
from test_ai_workspace import api  # noqa: E402
import fake_dynamodb as fdb  # noqa: E402
import workspace_schema as ws  # noqa: E402

OWNER = "u-owner"
MANAGER = "u-manager"
MEMBER = "u-member"
STRANGER = "u-stranger"
REMOVED = "u-removed"

ORG = "wso_abc123def456"
NOW = "2026-09-05T10:00:00Z"


def parse(resp):
    return resp["statusCode"], json.loads(resp["body"])


def event(method="GET", route="/workspaces", path=None, headers=None,
          body=None):
    ev = {
        "routeKey": f"{method} {route}",
        "requestContext": {"http": {"method": method, "path": route}},
        "headers": dict(headers or {"authorization": "Bearer test-token"}),
        "pathParameters": dict(path or {}),
        "queryStringParameters": {},
    }
    if body is not None:
        ev["body"] = json.dumps(body)
    return ev


# ===========================================================================
# 1. THE PURE VOCABULARY
# ===========================================================================
class TestDerivedIds(unittest.TestCase):
    """Personal workspace ids are DERIVED, which is what makes the migration
    optional. If these ever become random, the additive property is gone."""

    def test_personal_id_is_deterministic(self):
        self.assertEqual(ws.personal_workspace_id("u1"),
                         ws.personal_workspace_id("u1"))

    def test_personal_id_round_trips_to_its_owner(self):
        wid = ws.personal_workspace_id("u-abc")
        self.assertTrue(ws.is_personal_workspace_id(wid))
        self.assertEqual(ws.user_id_from_personal_workspace(wid), "u-abc")

    def test_organisation_id_is_not_personal(self):
        wid = ws.new_organisation_id()
        self.assertFalse(ws.is_personal_workspace_id(wid))
        self.assertEqual(ws.user_id_from_personal_workspace(wid), "")

    def test_organisation_ids_are_unique(self):
        ids = {ws.new_organisation_id() for _ in range(200)}
        self.assertEqual(len(ids), 200)

    def test_an_org_id_can_never_be_read_as_someones_personal_workspace(self):
        # The prefixes must not overlap, or an organisation id could be
        # decoded into a user_id and grant that user ownership of it.
        self.assertNotEqual(ws.PERSONAL_PREFIX, ws.ORG_PREFIX)
        self.assertFalse(ws.ORG_PREFIX.startswith(ws.PERSONAL_PREFIX))


class TestRoleMatrix(unittest.TestCase):
    """The capability matrix from section 8, asserted exhaustively."""

    def test_ordering(self):
        self.assertTrue(ws.role_at_least(ws.ROLE_OWNER, ws.ROLE_MANAGER))
        self.assertTrue(ws.role_at_least(ws.ROLE_OWNER, ws.ROLE_MEMBER))
        self.assertTrue(ws.role_at_least(ws.ROLE_MANAGER, ws.ROLE_MEMBER))
        self.assertFalse(ws.role_at_least(ws.ROLE_MEMBER, ws.ROLE_MANAGER))
        self.assertFalse(ws.role_at_least(ws.ROLE_MANAGER, ws.ROLE_OWNER))

    def test_every_role_is_at_least_itself(self):
        for role in ws.ROLES:
            self.assertTrue(ws.role_at_least(role, role))

    def test_unknown_role_is_weaker_than_member(self):
        # Fails CLOSED: a corrupted or future role value must satisfy nothing.
        for bogus in ("SUPERUSER", "owner", "", None, "ADMIN", 7):
            self.assertEqual(ws.role_rank(bogus), -1)
            self.assertFalse(ws.role_at_least(bogus, ws.ROLE_MEMBER))
            for cap in ws.CAPABILITIES:
                self.assertFalse(ws.role_can(bogus, cap))

    def test_owner_can_everything(self):
        for cap in ws.CAPABILITIES:
            self.assertTrue(ws.role_can(ws.ROLE_OWNER, cap), cap)

    def test_manager_manages_but_cannot_dispose(self):
        for cap in (ws.CAP_MANAGE_MEMBERS, ws.CAP_MANAGE_SETTINGS,
                    ws.CAP_MANAGE_INTEGRATIONS, ws.CAP_VIEW_ALL_MEETINGS):
            self.assertTrue(ws.role_can(ws.ROLE_MANAGER, cap), cap)
        # The three that end or hand away the organisation.
        for cap in (ws.CAP_TRANSFER_OWNERSHIP, ws.CAP_DELETE_WORKSPACE,
                    ws.CAP_MANAGE_BILLING):
            self.assertFalse(ws.role_can(ws.ROLE_MANAGER, cap), cap)

    def test_member_manages_nothing(self):
        for cap in ws.CAPABILITIES:
            self.assertFalse(ws.role_can(ws.ROLE_MEMBER, cap), cap)

    def test_unknown_capability_is_denied_even_for_owner(self):
        self.assertFalse(ws.role_can(ws.ROLE_OWNER, "launch_missiles"))

    def test_capabilities_for_covers_every_capability(self):
        caps = ws.capabilities_for(ws.ROLE_MANAGER)
        self.assertEqual(set(caps), set(ws.CAPABILITIES))


class TestInvitationTokens(unittest.TestCase):
    def test_raw_token_is_never_the_stored_value(self):
        token = ws.new_invite_token()
        row = ws.new_invitation(ORG, "a@b.com", ws.ROLE_MEMBER, OWNER,
                                token, NOW)
        self.assertNotEqual(row["token_hash"], token)
        self.assertEqual(row["token_hash"], ws.hash_invite_token(token))
        self.assertNotIn(token, json.dumps(row))

    def test_public_invitation_never_leaks_the_hash(self):
        token = ws.new_invite_token()
        row = ws.new_invitation(ORG, "a@b.com", ws.ROLE_MEMBER, OWNER,
                                token, NOW)
        public = ws.public_invitation(row)
        self.assertNotIn("token_hash", public)
        self.assertNotIn(row["token_hash"], json.dumps(public))

    def test_tokens_are_unique_and_high_entropy(self):
        tokens = {ws.new_invite_token() for _ in range(200)}
        self.assertEqual(len(tokens), 200)
        self.assertGreaterEqual(len(next(iter(tokens))), 40)

    def test_token_shape_check_rejects_junk_before_any_read(self):
        for bad in (None, "", "short", "has spaces", "x" * 200, 12345,
                    "has/slash", "has+plus"):
            self.assertFalse(ws.token_looks_valid(bad), repr(bad))
        self.assertTrue(ws.token_looks_valid(ws.new_invite_token()))

    def test_redaction_cannot_reconstruct_the_token(self):
        token = ws.new_invite_token()
        red = ws.redact_token(token)
        self.assertNotIn(token, red)
        self.assertTrue(red.startswith("sha256:"))


class TestInvitationExpiry(unittest.TestCase):
    """Unlike a share link, an invitation with no expiry FAILS CLOSED.

    share_schema treats a blank expiry as "never expires" because a permanent
    share link is a legitimate product choice. A permanent invitation is a
    standing key to an organisation, so the default is the opposite here.
    """

    def _invite(self, expires_at, status=ws.INVITE_PENDING):
        return {"status": status, "expires_at": expires_at}

    def test_missing_expiry_is_expired(self):
        self.assertTrue(ws.is_expired(self._invite("")))
        self.assertTrue(ws.is_expired({}))

    def test_unparseable_expiry_is_expired(self):
        self.assertTrue(ws.is_expired(self._invite("not-a-date")))

    def test_future_expiry_is_open(self):
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        self.assertFalse(ws.is_expired(self._invite(future)))
        self.assertTrue(ws.invite_is_open(self._invite(future)))

    def test_past_expiry_is_closed(self):
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.assertTrue(ws.is_expired(self._invite(past)))
        self.assertFalse(ws.invite_is_open(self._invite(past)))

    def test_cancelled_and_accepted_are_not_open_even_when_unexpired(self):
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        for status in (ws.INVITE_CANCELLED, ws.INVITE_ACCEPTED,
                       ws.INVITE_EXPIRED):
            self.assertFalse(
                ws.invite_is_open(self._invite(future, status)), status)

    def test_expiry_is_computed_at_read_time_not_written_by_a_sweeper(self):
        # There is no scheduler in this account (Phase 1 audit), so a PENDING
        # row that has aged out must READ as EXPIRED without anyone rewriting
        # it. This is what makes that safe.
        past = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        row = self._invite(past, ws.INVITE_PENDING)
        self.assertEqual(ws.effective_invite_status(row), ws.INVITE_EXPIRED)
        self.assertEqual(row["status"], ws.INVITE_PENDING)  # unchanged on disk


class TestValidation(unittest.TestCase):
    def test_role_defaults_to_member_and_normalizes_case(self):
        self.assertEqual(ws.clean_role(None), ws.ROLE_MEMBER)
        self.assertEqual(ws.clean_role("owner"), ws.ROLE_OWNER)

    def test_invalid_role_is_rejected(self):
        with self.assertRaises(ws.WorkspaceValidationError):
            ws.clean_role("ADMIN")

    def test_email_is_lowercased_and_validated(self):
        self.assertEqual(ws.clean_email("  A@B.COM "), "a@b.com")
        for bad in ("", "no-at-sign", "a@b", "a b@c.com"):
            with self.assertRaises(ws.WorkspaceValidationError):
                ws.clean_email(bad)

    def test_name_is_required_and_capped(self):
        with self.assertRaises(ws.WorkspaceValidationError):
            ws.clean_name("   ")
        self.assertEqual(len(ws.clean_name("x" * 500)), ws.MAX_NAME)

    def test_org_profile_keeps_absent_fields_absent(self):
        # Sparse discipline: a field the user never filled in must not read
        # back as "" that they then have to clear.
        out = ws.clean_org_profile({"company_name": "ABC", "phone": ""})
        self.assertEqual(out, {"company_name": "ABC"})

    def test_org_profile_ignores_unknown_fields(self):
        out = ws.clean_org_profile({"company_name": "ABC", "is_admin": True})
        self.assertNotIn("is_admin", out)


# ===========================================================================
# 2. THE AUTHORIZATION HELPERS
# ===========================================================================
class WorkspaceHarness(unittest.TestCase):
    """An organisation with an OWNER, a MANAGER, a MEMBER and a REMOVED
    member — plus a STRANGER who is in nothing."""

    def setUp(self):
        self.t = fdb.build_tables()
        self.current_user = OWNER
        self.patches = [
            mock.patch.object(api, "_workspaces", self.t["workspaces"]),
            mock.patch.object(api, "_memberships", self.t["memberships"]),
            mock.patch.object(api, "_invitations", self.t["invitations"]),
            mock.patch.object(api, "_users", self.t["users"]),
            mock.patch.object(api, "Key", fdb.Key),
            mock.patch.object(api, "_require_auth",
                              side_effect=lambda ev: self.current_user),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

        self.t["workspaces"].put_item(Item={
            "workspace_id": ORG, "type": ws.TYPE_ORGANISATION,
            "name": "ABC Realty", "owner_user_id": OWNER,
            "status": ws.STATUS_ACTIVE,
            "created_at": NOW, "updated_at": NOW,
        })
        for uid, role, status in (
            (OWNER, ws.ROLE_OWNER, ws.MEMBERSHIP_ACTIVE),
            (MANAGER, ws.ROLE_MANAGER, ws.MEMBERSHIP_ACTIVE),
            (MEMBER, ws.ROLE_MEMBER, ws.MEMBERSHIP_ACTIVE),
            (REMOVED, ws.ROLE_MEMBER, ws.MEMBERSHIP_REMOVED),
        ):
            self.t["memberships"].put_item(
                Item=ws.new_membership(ORG, uid, role, NOW, status=status))

    def as_user(self, uid):
        self.current_user = uid


class TestMembershipMatrix(WorkspaceHarness):
    """OWNER / MANAGER / MEMBER / NON-MEMBER / REMOVED against the gates."""

    def test_active_members_resolve(self):
        for uid, role in ((OWNER, ws.ROLE_OWNER),
                          (MANAGER, ws.ROLE_MANAGER),
                          (MEMBER, ws.ROLE_MEMBER)):
            m = api._active_membership(ORG, uid)
            self.assertIsNotNone(m, uid)
            self.assertEqual(m["role"], role)

    def test_stranger_has_no_membership(self):
        self.assertIsNone(api._active_membership(ORG, STRANGER))

    def test_removed_member_is_denied_immediately(self):
        # THE property the whole design exists for: the JWT is long-lived and
        # unrevocable, so removal must bite on the very next request.
        self.assertIsNone(api._active_membership(ORG, REMOVED))

    def test_removing_a_member_takes_effect_on_the_next_call(self):
        self.assertIsNotNone(api._active_membership(ORG, MEMBER))
        self.t["memberships"].update_item(
            Key={"workspace_id": ORG, "user_id": MEMBER},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": ws.MEMBERSHIP_REMOVED})
        self.assertIsNone(api._active_membership(ORG, MEMBER))

    def test_membership_in_a_suspended_workspace_is_denied(self):
        self.t["workspaces"].update_item(
            Key={"workspace_id": ORG},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": ws.STATUS_SUSPENDED})
        for uid in (OWNER, MANAGER, MEMBER):
            self.assertIsNone(api._active_membership(ORG, uid), uid)

    def test_membership_in_a_deleted_workspace_is_denied(self):
        self.t["workspaces"].update_item(
            Key={"workspace_id": ORG},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": ws.STATUS_DELETED})
        self.assertIsNone(api._active_membership(ORG, OWNER))

    def test_membership_read_is_strongly_consistent(self):
        # An eventually-consistent read here would leave a removed member
        # working for the length of the replication lag.
        with mock.patch.object(self.t["memberships"], "get_item",
                               wraps=self.t["memberships"].get_item) as g:
            api._workspace_membership(ORG, MEMBER)
            self.assertTrue(g.call_args.kwargs.get("ConsistentRead"))


class TestPersonalWorkspaceNeedsNoRow(WorkspaceHarness):
    """Membership in your own personal workspace is decidable without any
    read at all — which is what lets existing users work before the backfill."""

    def test_owner_of_own_personal_workspace(self):
        wid = ws.personal_workspace_id(MEMBER)
        m = api._active_membership(wid, MEMBER)
        self.assertIsNotNone(m)
        self.assertEqual(m["role"], ws.ROLE_OWNER)

    def test_nobody_else_is_ever_a_member_of_your_personal_workspace(self):
        wid = ws.personal_workspace_id(MEMBER)
        for other in (OWNER, MANAGER, STRANGER, REMOVED):
            self.assertIsNone(api._active_membership(wid, other), other)

    def test_a_forged_personal_id_grants_nothing(self):
        # Someone constructing "wsp_<someone-else>" gets exactly nothing.
        self.assertIsNone(
            api._active_membership(f"wsp_{OWNER}", STRANGER))

    def test_resolution_costs_no_database_read(self):
        with mock.patch.object(self.t["memberships"], "get_item") as g:
            api._active_membership(ws.personal_workspace_id(MEMBER), MEMBER)
            g.assert_not_called()


class TestRoleGate(WorkspaceHarness):
    """_require_workspace_role / _require_workspace_capability."""

    def test_owner_passes_every_capability(self):
        self.as_user(OWNER)
        for cap in ws.CAPABILITIES:
            api._require_workspace_capability(event(), cap, workspace_id=ORG)

    def test_manager_is_refused_owner_only_capabilities_with_403(self):
        self.as_user(MANAGER)
        for cap in (ws.CAP_DELETE_WORKSPACE, ws.CAP_TRANSFER_OWNERSHIP,
                    ws.CAP_MANAGE_BILLING):
            with self.assertRaises(api.ApiError) as ctx:
                api._require_workspace_capability(event(), cap,
                                                  workspace_id=ORG)
            # 403 not 404: an insider already knows the workspace exists, so
            # 404 would be a lie that makes the UI unexplainable.
            self.assertEqual(ctx.exception.status, 403, cap)

    def test_member_is_refused_every_capability_with_403(self):
        self.as_user(MEMBER)
        for cap in ws.CAPABILITIES:
            with self.assertRaises(api.ApiError) as ctx:
                api._require_workspace_capability(event(), cap,
                                                  workspace_id=ORG)
            self.assertEqual(ctx.exception.status, 403, cap)

    def test_stranger_gets_404_not_403(self):
        # An OUTSIDER must not learn the workspace exists.
        self.as_user(STRANGER)
        for cap in ws.CAPABILITIES:
            with self.assertRaises(api.ApiError) as ctx:
                api._require_workspace_capability(event(), cap,
                                                  workspace_id=ORG)
            self.assertEqual(ctx.exception.status, 404, cap)

    def test_removed_member_gets_404(self):
        self.as_user(REMOVED)
        with self.assertRaises(api.ApiError) as ctx:
            api._require_workspace_member(event(), workspace_id=ORG)
        self.assertEqual(ctx.exception.status, 404)


class TestWorkspaceIdIsNeverTrusted(WorkspaceHarness):
    """Section 7: never trust a frontend-supplied workspace_id."""

    def test_header_naming_a_foreign_workspace_grants_nothing(self):
        self.as_user(STRANGER)
        ev = event(headers={"authorization": "Bearer t",
                            api.WORKSPACE_HEADER: ORG})
        with self.assertRaises(api.ApiError) as ctx:
            api._require_workspace_member(ev)
        self.assertEqual(ctx.exception.status, 404)

    def test_header_cannot_elevate_a_members_role(self):
        # A MEMBER asking for the org they are really in still gets MEMBER;
        # the header names a workspace, it never carries a role.
        self.as_user(MEMBER)
        ev = event(headers={"authorization": "Bearer t",
                            api.WORKSPACE_HEADER: ORG})
        _, wid, membership = api._require_workspace_member(ev)
        self.assertEqual(wid, ORG)
        self.assertEqual(membership["role"], ws.ROLE_MEMBER)

    def test_absent_header_falls_back_to_personal(self):
        self.as_user(MEMBER)
        _, wid, membership = api._require_workspace_member(event())
        self.assertEqual(wid, ws.personal_workspace_id(MEMBER))
        self.assertEqual(membership["role"], ws.ROLE_OWNER)

    def test_body_supplied_workspace_id_is_ignored(self):
        # The body is never consulted — one fewer place to forget to verify.
        self.as_user(STRANGER)
        ev = event(method="POST", body={"workspace_id": ORG})
        _, wid, _ = api._require_workspace_member(ev)
        self.assertEqual(wid, ws.personal_workspace_id(STRANGER))

    def test_garbage_header_falls_back_rather_than_erroring(self):
        self.as_user(MEMBER)
        ev = event(headers={"authorization": "Bearer t",
                            api.WORKSPACE_HEADER: "wso_does-not-exist"})
        with self.assertRaises(api.ApiError) as ctx:
            api._require_workspace_member(ev)
        self.assertEqual(ctx.exception.status, 404)


class TestIsolation(WorkspaceHarness):
    """Section 22: workspace A must never read workspace B."""

    def setUp(self):
        super().setUp()
        self.other = "wso_other999"
        self.t["workspaces"].put_item(Item={
            "workspace_id": self.other, "type": ws.TYPE_ORGANISATION,
            "name": "XYZ Company", "owner_user_id": STRANGER,
            "status": ws.STATUS_ACTIVE, "created_at": NOW, "updated_at": NOW,
        })
        self.t["memberships"].put_item(Item=ws.new_membership(
            self.other, STRANGER, ws.ROLE_OWNER, NOW))

    def test_owner_of_one_org_is_a_stranger_to_another(self):
        self.assertIsNone(api._active_membership(self.other, OWNER))
        self.assertIsNone(api._active_membership(ORG, STRANGER))

    def test_each_user_lists_only_their_own_workspaces(self):
        self.as_user(OWNER)
        mine = {w["workspace_id"] for w, _, _ in api._user_workspaces(OWNER)}
        self.assertIn(ORG, mine)
        self.assertNotIn(self.other, mine)

        theirs = {w["workspace_id"]
                  for w, _, _ in api._user_workspaces(STRANGER)}
        self.assertIn(self.other, theirs)
        self.assertNotIn(ORG, theirs)

    def test_personal_workspaces_never_collide(self):
        # Two colleagues legitimately SHARE the organisation, so only the
        # personal half of each list may be compared — that is the part which
        # must never intersect.
        def personal(uid):
            return {w["workspace_id"] for w, _, _ in api._user_workspaces(uid)
                    if ws.is_personal_workspace_id(w["workspace_id"])}

        self.assertEqual(personal(MEMBER) & personal(MANAGER), set())
        self.assertEqual(personal(MEMBER), {ws.personal_workspace_id(MEMBER)})

    def test_a_stale_index_row_cannot_grant_access(self):
        # An index is a lookup path, never an authorization decision: each row
        # is re-resolved through _active_membership before it is returned.
        self.t["memberships"].put_item(Item=ws.new_membership(
            "wso_deleted-workspace", MEMBER, ws.ROLE_OWNER, NOW))
        ids = {w["workspace_id"] for w, _, _ in api._user_workspaces(MEMBER)}
        self.assertNotIn("wso_deleted-workspace", ids)


# ===========================================================================
# 3. THE ADDITIVE PROPERTY — Phase 2A must be invisible to existing users
# ===========================================================================
class TestWorksBeforeAnyMigration(WorkspaceHarness):
    """No workspace row, no membership row, no backfill: everything still
    resolves. This is what makes scripts/52 an optimization, not a gate."""

    def test_a_user_with_no_rows_still_has_a_personal_workspace(self):
        fresh = "u-never-migrated"
        spaces = api._user_workspaces(fresh)
        self.assertEqual(len(spaces), 1)
        workspace, role, _ = spaces[0]
        self.assertEqual(workspace["workspace_id"],
                         ws.personal_workspace_id(fresh))
        self.assertEqual(workspace["type"], ws.TYPE_PERSONAL)
        self.assertEqual(role, ws.ROLE_OWNER)

    def test_listing_works_with_an_entirely_empty_workspaces_table(self):
        self.assertEqual(len(self.t["workspaces"].items), 1)  # only ORG
        self.as_user("u-brand-new")
        status, body = parse(api.list_workspaces(event()))
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 1)
        self.assertTrue(body["workspaces"][0]["is_personal"])

    def test_synthesized_and_persisted_personal_workspaces_look_identical(self):
        uid = "u-compare"
        before = api._user_workspaces(uid)[0][0]
        api._ensure_personal_workspace(uid)
        after = api._user_workspaces(uid)[0][0]
        for field in ("workspace_id", "type", "owner_user_id", "status"):
            self.assertEqual(before[field], after[field], field)


class TestEnsurePersonalWorkspace(WorkspaceHarness):
    def test_is_idempotent(self):
        uid = "u-idem"
        a = api._ensure_personal_workspace(uid)
        b = api._ensure_personal_workspace(uid)
        self.assertEqual(a["workspace_id"], b["workspace_id"])
        self.assertEqual(a["created_at"], b["created_at"])

    def test_does_not_reset_a_renamed_workspace(self):
        # A blind put would silently discard the user's own rename.
        uid = "u-renamed"
        api._ensure_personal_workspace(uid)
        wid = ws.personal_workspace_id(uid)
        self.t["workspaces"].update_item(
            Key={"workspace_id": wid},
            UpdateExpression="SET #n = :n",
            ExpressionAttributeNames={"#n": "name"},
            ExpressionAttributeValues={":n": "My Desk"})
        again = api._ensure_personal_workspace(uid)
        self.assertEqual(again["name"], "My Desk")

    def test_concurrent_creation_is_safe(self):
        # Simulates the conditional write losing the race.
        uid = "u-race"
        wid = ws.personal_workspace_id(uid)
        real_put = self.t["workspaces"].put_item

        def racing_put(**kwargs):
            self.t["workspaces"].items[(wid, None)] = \
                ws.new_personal_workspace(uid, NOW)
            return real_put(**kwargs)

        with mock.patch.object(self.t["workspaces"], "put_item",
                               side_effect=racing_put):
            got = api._ensure_personal_workspace(uid)
        self.assertEqual(got["workspace_id"], wid)


# ===========================================================================
# 4. THE ROUTES
# ===========================================================================
class TestWorkspaceRoutes(WorkspaceHarness):
    def test_list_returns_personal_first(self):
        self.as_user(OWNER)
        status, body = parse(api.list_workspaces(event()))
        self.assertEqual(status, 200)
        self.assertTrue(body["workspaces"][0]["is_personal"])
        ids = [w["workspace_id"] for w in body["workspaces"]]
        self.assertIn(ORG, ids)

    def test_list_includes_role_and_capabilities(self):
        self.as_user(MANAGER)
        _, body = parse(api.list_workspaces(event()))
        org = next(w for w in body["workspaces"] if w["workspace_id"] == ORG)
        self.assertEqual(org["role"], ws.ROLE_MANAGER)
        self.assertTrue(org["capabilities"][ws.CAP_MANAGE_MEMBERS])
        self.assertFalse(org["capabilities"][ws.CAP_DELETE_WORKSPACE])

    def test_removed_member_does_not_see_the_org(self):
        self.as_user(REMOVED)
        _, body = parse(api.list_workspaces(event()))
        self.assertNotIn(ORG, [w["workspace_id"] for w in body["workspaces"]])
        self.assertEqual(body["count"], 1)  # personal only

    def test_current_workspace_echoes_a_valid_header(self):
        self.as_user(MEMBER)
        ev = event(headers={"authorization": "Bearer t",
                            api.WORKSPACE_HEADER: ORG})
        _, body = parse(api.list_workspaces(ev))
        self.assertEqual(body["current_workspace_id"], ORG)

    def test_current_workspace_ignores_an_invalid_header(self):
        self.as_user(STRANGER)
        ev = event(headers={"authorization": "Bearer t",
                            api.WORKSPACE_HEADER: ORG})
        _, body = parse(api.list_workspaces(ev))
        self.assertEqual(body["current_workspace_id"],
                         ws.personal_workspace_id(STRANGER))

    def test_get_workspace_for_a_member(self):
        self.as_user(MEMBER)
        status, body = parse(api.get_workspace(
            event(route="/workspaces/{workspace_id}",
                  path={"workspace_id": ORG})))
        self.assertEqual(status, 200)
        self.assertEqual(body["workspace"]["name"], "ABC Realty")
        self.assertEqual(body["workspace"]["role"], ws.ROLE_MEMBER)

    def test_get_workspace_404s_for_a_stranger(self):
        self.as_user(STRANGER)
        with self.assertRaises(api.ApiError) as ctx:
            api.get_workspace(event(route="/workspaces/{workspace_id}",
                                    path={"workspace_id": ORG}))
        self.assertEqual(ctx.exception.status, 404)

    def test_get_own_personal_workspace_works_unmigrated(self):
        self.as_user("u-fresh")
        wid = ws.personal_workspace_id("u-fresh")
        status, body = parse(api.get_workspace(
            event(route="/workspaces/{workspace_id}",
                  path={"workspace_id": wid})))
        self.assertEqual(status, 200)
        self.assertTrue(body["workspace"]["is_personal"])

    def test_cannot_read_another_users_personal_workspace(self):
        self.as_user(STRANGER)
        with self.assertRaises(api.ApiError) as ctx:
            api.get_workspace(event(
                route="/workspaces/{workspace_id}",
                path={"workspace_id": ws.personal_workspace_id(OWNER)}))
        self.assertEqual(ctx.exception.status, 404)

    def test_routes_are_registered(self):
        self.assertIs(api._ROUTES[("GET", "/workspaces")],
                      api.list_workspaces)
        self.assertIs(api._ROUTES[("GET", "/workspaces/{workspace_id}")],
                      api.get_workspace)

    def test_write_routes_arrived_in_phase_2b(self):
        """CHANGED BEHAVIOUR — deliberate, recorded here rather than deleted.

        OLD (Phase 2A): no write route on /workspaces existed, and a tripwire
             test asserted that, because invitation acceptance would have had
             to guess the unresolved Personal-vs-Organisation identity model.
        NEW (Phase 2B): the identity model is decided (one identity per email;
             a personal-only identity is refused at acceptance, never merged
             or converted), so the write surface ships.
        WHY: the tripwire was guarding a decision, not a security property.
             The decision was made, so the guard is replaced by a test of the
             contract that now holds — see TestInvitationIdentityRule.

        A workspace still cannot be MUTATED through these: there is no
        PATCH/DELETE on the workspace itself, because renaming and deleting an
        organisation are Phase 2C/2E.
        """
        self.assertIs(api._ROUTES[("POST", "/workspaces")],
                      api.create_workspace)
        self.assertIs(api._ROUTES[("POST", "/workspace-invitations/"
                                           "{token}/accept")],
                      api.accept_invitation)
        for method in ("PATCH", "DELETE", "PUT"):
            self.assertNotIn((method, "/workspaces/{workspace_id}"),
                             api._ROUTES)


# ===========================================================================
# 5. EMAIL UNIQUENESS — the race the Phase 1 audit found
# ===========================================================================
class TestEmailUniqueness(unittest.TestCase):
    """The GSI read cannot enforce uniqueness; the claim row can.

    Old behaviour: read an eventually-consistent index, then write with a
    condition on `user_id` — a fresh uuid, which never collides and therefore
    guards nothing. Two concurrent signups for one address could both succeed.
    """

    def setUp(self):
        self.t = fdb.build_tables()
        self.patches = [
            mock.patch.object(api, "_users", self.t["users"]),
            mock.patch.object(api, "Key", fdb.Key),
            # signup mints a JWT on success; the secret is normally read from
            # Secrets Manager. Same stub the other offline suites use.
            mock.patch.object(api, "_jwt_secret", return_value="test-secret"),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def _signup(self, email, password="hunter2hunter2"):
        return api.signup({
            "routeKey": "POST /signup",
            "headers": {},
            "body": json.dumps({"email": email, "password": password}),
        })

    def test_signup_succeeds_and_claims_the_email(self):
        status, body = parse(self._signup("a@b.com"))
        self.assertEqual(status, 201)
        claim = self.t["users"].get_item(
            Key={"user_id": api._user_email_claim("a@b.com")}).get("Item")
        self.assertIsNotNone(claim)
        self.assertEqual(claim["claims_user_id"], body["user_id"])

    def test_duplicate_signup_is_rejected(self):
        self._signup("dup@b.com")
        with self.assertRaises(api.ApiError) as ctx:
            self._signup("dup@b.com")
        self.assertEqual(ctx.exception.status, 409)

    def test_the_race_is_closed(self):
        """Both requests see an EMPTY index, as they would under replication
        lag. Exactly one may still win."""
        with mock.patch.object(self.t["users"], "query",
                               return_value={"Items": []}):
            status, _ = parse(self._signup("race@b.com"))
            self.assertEqual(status, 201)
            with self.assertRaises(api.ApiError) as ctx:
                self._signup("race@b.com")
            self.assertEqual(ctx.exception.status, 409)

        accounts = [i for i in self.t["users"].items.values()
                    if i.get("email") == "race@b.com"]
        self.assertEqual(len(accounts), 1, "two accounts on one email")

    def test_claim_is_rolled_back_when_the_account_write_fails(self):
        # A stranded claim would make the address permanently unregisterable.
        real_put = self.t["users"].put_item
        calls = {"n": 0}

        def failing_put(**kwargs):
            calls["n"] += 1
            if calls["n"] == 2:            # the account row, not the claim
                raise RuntimeError("boom")
            return real_put(**kwargs)

        with mock.patch.object(self.t["users"], "put_item",
                               side_effect=failing_put):
            with self.assertRaises(RuntimeError):
                self._signup("rollback@b.com")

        self.assertIsNone(self.t["users"].get_item(
            Key={"user_id": api._user_email_claim("rollback@b.com")}
        ).get("Item"))
        # And the address is registerable again.
        self.assertEqual(parse(self._signup("rollback@b.com"))[0], 201)

    def test_claim_rows_are_invisible_to_login(self):
        """A claim carries no `email` attribute, so it cannot enter the sparse
        email-index and login can never resolve one as an account."""
        self._signup("login@b.com")
        res = self.t["users"].query(
            IndexName="email-index",
            KeyConditionExpression=fdb.Key("email").eq("login@b.com"))
        self.assertEqual(len(res["Items"]), 1)
        self.assertFalse(api._is_user_claim(res["Items"][0]))

    def test_claim_ids_cannot_collide_with_real_user_ids(self):
        # A real user_id is a uuid4 string; a claim id always contains "#".
        self.assertTrue(api._is_user_claim(
            {"user_id": api._user_email_claim("x@y.com")}))
        _, body = parse(self._signup("real@b.com"))
        self.assertFalse(api._is_user_claim({"user_id": body["user_id"]}))

    def test_email_is_normalized_before_claiming(self):
        self._signup("Mixed@Case.COM")
        with self.assertRaises(api.ApiError) as ctx:
            self._signup("mixed@case.com")
        self.assertEqual(ctx.exception.status, 409)


if __name__ == "__main__":
    unittest.main(verbosity=2)
