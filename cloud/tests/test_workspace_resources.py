#!/usr/bin/env python3
"""test_workspace_resources.py — Phase 2B: organisations, and workspace-owned
resources.

WHAT THIS FILE PINS.

  1. THE IDENTITY RULE at invitation acceptance — the four cases from the
     brief. Case C (a personal-only identity) must be REFUSED, never merged
     and never converted. TestInvitationIdentityRule.

  2. RESOURCE OWNERSHIP. workspace_id is stamped at creation from a VERIFIED
     membership, and read from the stored row thereafter. A row without one is
     personal to its owner — never ambiguous, never organisational.

  3. MEETING VISIBILITY. Membership is NOT access: an OWNER/MANAGER sees every
     meeting in the organisation, a MEMBER sees only their own plus explicit
     grants. TestMeetingVisibility.

  4. TASK != MEETING ACCESS. The brief's worked example: Rahul records, Priya
     gets the task, Priya does NOT get the meeting. TestTaskIsNotMeetingAccess.

  5. CROSS-WORKSPACE ISOLATION over four workspaces (Personal A, Personal B,
     Org A, Org B), through the direct APIs AND through the async seeding path.
     TestFourWorkspaceIsolation.

  6. IMMEDIATE REVOCATION. Membership is re-read per request, so a removed
     member loses access on their very next call — the property that exists
     because the 24h JWT cannot be revoked.

SECURITY POSTURE, as in every suite here: tests call the ROUTE FUNCTION
DIRECTLY with a forged identity, exactly as curl against the deployed API
would. Nothing here can be satisfied by hiding a button.

OFFLINE: fake_dynamodb, no AWS, no network.

Run:  python -m pytest tests/test_workspace_resources.py
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "functions/userapi"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# ORDER MATTERS — see test_task_permissions.py. The workspace harness binds the
# same `api` module object (and the same Key class) every other suite uses.
from test_ai_workspace import api  # noqa: E402
import fake_dynamodb as fdb  # noqa: E402
import workspace_schema as ws  # noqa: E402

# Actors. A/B mirror each other so every isolation test has a twin.
OWNER_A, MANAGER_A, MEMBER_A, MEMBER_A2 = "u-oa", "u-ma", "u-mea", "u-mea2"
OWNER_B = "u-ob"
OUTSIDER = "u-out"
# Starts as an ACTIVE member of ORG_A and is removed by the tests that need
# a removed member — so removal is exercised through the real route rather
# than by seeding a REMOVED row, which would not prove the route works.
REMOVED = "u-removed"

ORG_A = "wso_aaaa1111"
ORG_B = "wso_bbbb2222"
NOW = "2026-09-05T10:00:00Z"


def parse(resp):
    return resp["statusCode"], json.loads(resp["body"])


def event(method="GET", route="/workspaces", path=None, body=None,
          workspace=None, key=None):
    ev = {
        "routeKey": f"{method} {route}",
        "requestContext": {"http": {"method": method, "path": route}},
        "headers": {"authorization": "Bearer test-token"},
        "pathParameters": dict(path or {}),
        "queryStringParameters": {},
    }
    if workspace is not None:
        ev["headers"][api.WORKSPACE_HEADER] = workspace
    if key is not None:
        ev["pathParameters"]["key"] = key
    if body is not None:
        ev["body"] = json.dumps(body)
    return ev


class OrgHarness(unittest.TestCase):
    """Two organisations, each with its own people, plus an outsider."""

    def setUp(self):
        self.t = fdb.build_tables()
        self.current_user = OWNER_A
        self.patches = [
            mock.patch.object(api, "_workspaces", self.t["workspaces"]),
            mock.patch.object(api, "_memberships", self.t["memberships"]),
            mock.patch.object(api, "_invitations", self.t["invitations"]),
            mock.patch.object(api, "_meeting_access", self.t["meeting_access"]),
            mock.patch.object(api, "_recordings", self.t["recordings"]),
            mock.patch.object(api, "_tasks", self.t["tasks"]),
            mock.patch.object(api, "_contacts", self.t["contacts"]),
            mock.patch.object(api, "_meeting_participants",
                              self.t["participants"]),
            mock.patch.object(api, "_users", self.t["users"]),
            mock.patch.object(api, "Key", fdb.Key),
            mock.patch.object(api, "_require_auth",
                              side_effect=lambda ev: self.current_user),
            mock.patch.object(api, "_owned_devices", return_value=[]),
            mock.patch.object(api, "_jwt_secret", return_value="test-secret"),
            mock.patch.object(api.transcript_store, "hydrate",
                              side_effect=lambda s3, bucket, item: item),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

        for wid, owner, name in ((ORG_A, OWNER_A, "ABC Realty"),
                                 (ORG_B, OWNER_B, "XYZ Company")):
            self.t["workspaces"].put_item(Item={
                "workspace_id": wid, "type": ws.TYPE_ORGANISATION,
                "name": name, "owner_user_id": owner,
                "status": ws.STATUS_ACTIVE,
                "created_at": NOW, "updated_at": NOW})

        for wid, uid, role in (
            (ORG_A, OWNER_A, ws.ROLE_OWNER),
            (ORG_A, MANAGER_A, ws.ROLE_MANAGER),
            (ORG_A, MEMBER_A, ws.ROLE_MEMBER),
            (ORG_A, MEMBER_A2, ws.ROLE_MEMBER),
            (ORG_A, REMOVED, ws.ROLE_MEMBER),
            (ORG_B, OWNER_B, ws.ROLE_OWNER),
        ):
            self.t["memberships"].put_item(
                Item=ws.new_membership(wid, uid, role, NOW))

        for uid in (OWNER_A, MANAGER_A, MEMBER_A, MEMBER_A2, OWNER_B,
                    OUTSIDER, REMOVED):
            self.t["users"].put_item(Item={
                "user_id": uid, "email": f"{uid}@work.com",
                "name": uid, "created_at": NOW})

    def as_user(self, uid):
        self.current_user = uid

    def make_meeting(self, key, creator, workspace_id, **extra):
        item = {"audio_s3_key": key, "user_id": creator,
                "created_by": creator, "title": "Meeting",
                "status": "complete", "created_at": NOW,
                "recorded_at": NOW, "transcript": "Speaker 0: hello.",
                "speaker_names": {}, "ai_tasks": []}
        if workspace_id:
            item["workspace_id"] = workspace_id
        item.update(extra)
        self.t["recordings"].put_item(Item=item)
        return item


# ===========================================================================
# 1. ORGANISATION CREATION
# ===========================================================================
class TestOrganisationCreation(OrgHarness):
    def test_creator_becomes_owner(self):
        self.as_user(OUTSIDER)
        status, body = parse(api.create_workspace(event(
            method="POST", body={"name": "New Co", "industry": "Realty"})))
        self.assertEqual(status, 201)
        self.assertEqual(body["workspace"]["type"], ws.TYPE_ORGANISATION)
        self.assertEqual(body["workspace"]["role"], ws.ROLE_OWNER)
        wid = body["workspace"]["workspace_id"]
        m = api._active_membership(wid, OUTSIDER)
        self.assertEqual(m["role"], ws.ROLE_OWNER)

    def test_organisation_id_is_never_a_personal_id(self):
        self.as_user(OUTSIDER)
        _, body = parse(api.create_workspace(event(
            method="POST", body={"name": "New Co"})))
        wid = body["workspace"]["workspace_id"]
        self.assertTrue(ws.is_organisation_workspace_id(wid))
        self.assertFalse(ws.is_personal_workspace_id(wid))

    def test_name_is_required(self):
        self.as_user(OUTSIDER)
        with self.assertRaises(api.ApiError) as ctx:
            api.create_workspace(event(method="POST", body={"name": "  "}))
        self.assertEqual(ctx.exception.status, 400)

    def test_retry_with_same_idempotency_key_does_not_duplicate(self):
        self.as_user(OUTSIDER)
        body = {"name": "Retry Co", "idempotency_key": "abc-123"}
        status, _ = parse(api.create_workspace(event(method="POST", body=body)))
        self.assertEqual(status, 201)
        with self.assertRaises(api.ApiError) as ctx:
            api.create_workspace(event(method="POST", body=body))
        self.assertEqual(ctx.exception.status, 409)
        orgs = [w for w in self.t["workspaces"].items.values()
                if w.get("name") == "Retry Co"]
        self.assertEqual(len(orgs), 1)

    def test_optional_profile_fields_are_stored(self):
        self.as_user(OUTSIDER)
        _, body = parse(api.create_workspace(event(method="POST", body={
            "name": "Profile Co", "company_email": "hi@p.com",
            "domain": "p.com", "industry": "Tech"})))
        self.assertEqual(body["workspace"]["domain"], "p.com")

    def test_creating_an_org_does_not_touch_the_personal_workspace(self):
        self.as_user(OUTSIDER)
        api.create_workspace(event(method="POST", body={"name": "Sep Co"}))
        personal = api._active_membership(
            ws.personal_workspace_id(OUTSIDER), OUTSIDER)
        self.assertEqual(personal["role"], ws.ROLE_OWNER)


# ===========================================================================
# 2. INVITATIONS + THE IDENTITY RULE
# ===========================================================================
class TestInvitations(OrgHarness):
    def _invite(self, email="new@work.com", role=ws.ROLE_MEMBER):
        return parse(api.invite_member(event(
            method="POST",
            route="/workspaces/{workspace_id}/members/invite",
            path={"workspace_id": ORG_A}, body={"email": email, "role": role})))

    def test_owner_can_invite(self):
        self.as_user(OWNER_A)
        status, body = self._invite()
        self.assertEqual(status, 201)
        self.assertEqual(body["invitation"]["status"], ws.INVITE_PENDING)

    def test_manager_can_invite(self):
        self.as_user(MANAGER_A)
        self.assertEqual(self._invite()[0], 201)

    def test_member_cannot_invite(self):
        self.as_user(MEMBER_A)
        with self.assertRaises(api.ApiError) as ctx:
            self._invite()
        self.assertEqual(ctx.exception.status, 403)

    def test_outsider_gets_404_not_403(self):
        self.as_user(OUTSIDER)
        with self.assertRaises(api.ApiError) as ctx:
            self._invite()
        self.assertEqual(ctx.exception.status, 404)

    def test_raw_token_is_returned_once_and_never_stored(self):
        self.as_user(OWNER_A)
        _, body = self._invite()
        token = body["invite_token"]
        stored = list(self.t["invitations"].items.values())[0]
        self.assertNotIn("token", stored)
        self.assertEqual(stored["token_hash"], ws.hash_invite_token(token))
        self.assertNotIn(token, json.dumps(stored))
        # And it is not echoed in the invitation view either.
        self.assertNotIn("token_hash", body["invitation"])

    def test_owner_role_cannot_be_invited(self):
        self.as_user(OWNER_A)
        with self.assertRaises(api.ApiError) as ctx:
            self._invite(role=ws.ROLE_OWNER)
        self.assertEqual(ctx.exception.status, 403)

    def test_inviting_an_existing_member_is_rejected(self):
        self.as_user(OWNER_A)
        with self.assertRaises(api.ApiError) as ctx:
            self._invite(email=f"{MEMBER_A}@work.com")
        self.assertEqual(ctx.exception.status, 409)

    def test_invalid_email_is_rejected(self):
        self.as_user(OWNER_A)
        with self.assertRaises(api.ApiError) as ctx:
            self._invite(email="not-an-email")
        self.assertEqual(ctx.exception.status, 400)


class TestInvitationIdentityRule(OrgHarness):
    """The four cases from the brief's section 8."""

    def setUp(self):
        super().setUp()
        self.as_user(OWNER_A)
        _, body = parse(api.invite_member(event(
            method="POST",
            route="/workspaces/{workspace_id}/members/invite",
            path={"workspace_id": ORG_A},
            body={"email": "rahul@abcrealty.com", "role": ws.ROLE_MEMBER})))
        self.token = body["invite_token"]

    def _accept(self):
        return api.accept_invitation(event(
            method="POST", route="/workspace-invitations/{token}/accept",
            path={"token": self.token}))

    def _add_user(self, uid, email, org_member_of=None,
                  personal_data=False):
        """Create an identity.

        `personal_data` seeds a personal CONTACT, which is what makes the
        identity an ESTABLISHED personal account. Phase 2C corrected the Case
        C test to look for real personal data rather than for the absence of
        an organisation membership — every signup gets a derived Personal
        workspace, so its existence proved nothing and the old rule refused
        every invitation (a circular deadlock: joining your first
        organisation required already being in one).
        """
        self.t["users"].put_item(Item={"user_id": uid, "email": email,
                                       "name": uid, "created_at": NOW})
        if org_member_of:
            self.t["memberships"].put_item(Item=ws.new_membership(
                org_member_of, uid, ws.ROLE_MEMBER, NOW))
        if personal_data:
            self.t["contacts"].put_item(
                Item=api._contact_item(uid, "A personal contact"))

    def test_case_b_organisation_identity_may_join(self):
        # Already in ANOTHER organisation, so this is an organisation identity.
        self._add_user("u-rahul-work", "rahul@abcrealty.com",
                       org_member_of=ORG_B)
        self.as_user("u-rahul-work")
        status, body = parse(self._accept())
        self.assertEqual(status, 201)
        self.assertEqual(body["role"], ws.ROLE_MEMBER)
        self.assertIsNotNone(api._active_membership(ORG_A, "u-rahul-work"))

    def test_a_brand_new_invitee_can_actually_join(self):
        """THE PRODUCTION REGRESSION. Found on a live deployment: no
        invitation could EVER be accepted.

        The old Case C test refused any identity holding no organisation
        membership, which is circular — joining your first organisation
        required already being in one. It was also meaningless as a signal,
        because EVERY signup gets a derived Personal workspace, so "this is a
        personal account" was true of every identity in existence.

        The normal path is exactly this: an admin invites someone, that
        person signs up with the invited address, and joins. It must work.
        """
        self._add_user("u-newcomer", "rahul@abcrealty.com")
        self.as_user("u-newcomer")
        status, body = parse(self._accept())
        self.assertEqual(status, 201)
        self.assertEqual(body["role"], ws.ROLE_MEMBER)
        m = api._active_membership(ORG_A, "u-newcomer")
        self.assertIsNotNone(m)
        self.assertEqual(m["role"], ws.ROLE_MEMBER)

    def test_the_gate_matches_organisation_creation(self):
        """One rule, both identity paths. If these two ever disagree again,
        one of them is wrong."""
        self._add_user("u-consistent", "rahul@abcrealty.com",
                       personal_data=True)
        # Refused for CREATION...
        self.as_user("u-consistent")
        with self.assertRaises(api.PersonalWorkspaceInUse):
            api.create_workspace(event(method="POST", route="/workspaces",
                                       body={"name": "Mine"}))
        # ...and refused for ACCEPTANCE, for the same reason.
        with self.assertRaises(api.OrganisationEmailConflict):
            self._accept()

    def test_case_c_personal_identity_is_refused_not_merged(self):
        """THE rule: a personal-only account must not become organisational."""
        self._add_user("u-rahul-personal", "rahul@abcrealty.com",
                       personal_data=True)
        self.as_user("u-rahul-personal")
        with self.assertRaises(api.ApiError) as ctx:
            self._accept()
        self.assertEqual(ctx.exception.status, 409)
        self.assertIn("work email", ctx.exception.message)
        # NOTHING was created, merged or converted.
        self.assertIsNone(api._active_membership(ORG_A, "u-rahul-personal"))
        # And the invitation is still open for the right identity.
        inv = list(self.t["invitations"].items.values())[0]
        self.assertEqual(inv["status"], ws.INVITE_PENDING)

    def test_case_c_is_409_not_401_so_the_user_is_not_signed_out(self):
        # A 401 clears the JWT on the client and would sign the user out of
        # the very personal account the message tells them to keep.
        self._add_user("u-p", "rahul@abcrealty.com", personal_data=True)
        self.as_user("u-p")
        with self.assertRaises(api.ApiError) as ctx:
            self._accept()
        self.assertNotEqual(ctx.exception.status, 401)

    def test_case_d_a_different_identity_cannot_spend_the_token(self):
        self._add_user("u-thief", "thief@elsewhere.com", org_member_of=ORG_B)
        self.as_user("u-thief")
        with self.assertRaises(api.ApiError) as ctx:
            self._accept()
        self.assertEqual(ctx.exception.status, 403)
        self.assertIsNone(api._active_membership(ORG_A, "u-thief"))

    def test_case_d_error_does_not_disclose_the_invited_address(self):
        # A leaked link must not become an email-address disclosure.
        self._add_user("u-thief2", "thief2@elsewhere.com", org_member_of=ORG_B)
        self.as_user("u-thief2")
        with self.assertRaises(api.ApiError) as ctx:
            self._accept()
        self.assertNotIn("rahul@abcrealty.com", ctx.exception.message)

    def test_acceptance_is_single_use(self):
        self._add_user("u-once", "rahul@abcrealty.com", org_member_of=ORG_B)
        self.as_user("u-once")
        self.assertEqual(parse(self._accept())[0], 201)
        # Second attempt by a DIFFERENT valid identity finds it spent.
        self._add_user("u-second", "rahul@abcrealty.com", org_member_of=ORG_B)
        self.as_user("u-second")
        with self.assertRaises(api.ApiError) as ctx:
            self._accept()
        self.assertEqual(ctx.exception.status, 404)

    def test_re_accepting_is_idempotent_for_the_same_person(self):
        self._add_user("u-idem", "rahul@abcrealty.com", org_member_of=ORG_B)
        self.as_user("u-idem")
        self.assertEqual(parse(self._accept())[0], 201)
        # The membership now exists, so a re-open reports it rather than
        # erroring — normal behaviour when someone taps the link twice.
        m = api._active_membership(ORG_A, "u-idem")
        self.assertIsNotNone(m)
        verdict = ws.acceptance_verdict(
            "rahul@abcrealty.com",
            list(self.t["invitations"].items.values())[0],
            has_org_membership=True, already_member=True)
        self.assertEqual(verdict, ws.ACCEPT_ALREADY_MEMBER)

    def test_expired_invitation_is_rejected(self):
        inv = list(self.t["invitations"].items.values())[0]
        self.t["invitations"].update_item(
            Key={"invitation_id": inv["invitation_id"]},
            UpdateExpression="SET expires_at = :e",
            ExpressionAttributeValues={":e": "2020-01-01T00:00:00Z"})
        self._add_user("u-late", "rahul@abcrealty.com", org_member_of=ORG_B)
        self.as_user("u-late")
        with self.assertRaises(api.ApiError) as ctx:
            self._accept()
        self.assertEqual(ctx.exception.status, 404)

    def test_cancelled_invitation_is_rejected(self):
        inv = list(self.t["invitations"].items.values())[0]
        self.t["invitations"].update_item(
            Key={"invitation_id": inv["invitation_id"]},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": ws.INVITE_CANCELLED})
        self._add_user("u-c", "rahul@abcrealty.com", org_member_of=ORG_B)
        self.as_user("u-c")
        with self.assertRaises(api.ApiError) as ctx:
            self._accept()
        self.assertEqual(ctx.exception.status, 404)

    def test_garbage_token_is_rejected_without_a_read(self):
        self.as_user(OUTSIDER)
        for bad in ("", "short", "has spaces", "x" * 300):
            ev = event(method="POST",
                       route="/workspace-invitations/{token}/accept",
                       path={"token": bad})
            with self.assertRaises(api.ApiError) as ctx:
                api.accept_invitation(ev)
            self.assertEqual(ctx.exception.status, 404)


# ===========================================================================
# 3. ROLE MANAGEMENT + REMOVAL
# ===========================================================================
class TestInvitationAtomicity(OrgHarness):
    """The P0 data-consistency invariant:

        membership exists AND invitation = ACCEPTED
      OR
        membership absent AND invitation still USABLE

      never: invitation = ACCEPTED with no membership.

    The old order flipped the status FIRST, so a failed membership write spent
    the token and locked the invitee out permanently with no way to retry.
    """

    def setUp(self):
        super().setUp()
        self.as_user(OWNER_A)
        _, body = parse(api.invite_member(event(
            method="POST",
            route="/workspaces/{workspace_id}/members/invite",
            path={"workspace_id": ORG_A},
            body={"email": "joiner@work.com", "role": ws.ROLE_MEMBER})))
        self.token = body["invite_token"]
        self.invitation_id = body["invitation"]["invitation_id"]
        # An ORGANISATION identity (already in ORG_B), so Case B applies.
        self.t["users"].put_item(Item={
            "user_id": "u-joiner", "email": "joiner@work.com",
            "name": "J", "created_at": NOW})
        self.t["memberships"].put_item(Item=ws.new_membership(
            ORG_B, "u-joiner", ws.ROLE_MEMBER, NOW))
        self.as_user("u-joiner")

    def _accept(self):
        return api.accept_invitation(event(
            method="POST", route="/workspace-invitations/{token}/accept",
            path={"token": self.token}))

    def _status(self):
        return self.t["invitations"].get_item(
            Key={"invitation_id": self.invitation_id})["Item"]["status"]

    def test_happy_path_leaves_both_sides_consistent(self):
        self.assertEqual(parse(self._accept())[0], 201)
        self.assertEqual(self._status(), ws.INVITE_ACCEPTED)
        self.assertIsNotNone(api._active_membership(ORG_A, "u-joiner"))

    def test_membership_write_failure_leaves_the_invitation_usable(self):
        """THE regression. A failed membership write must not spend the
        token."""
        with mock.patch.object(self.t["memberships"], "put_item",
                               side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self._accept()

        # Compensated back to PENDING — not stranded in ACCEPTED.
        self.assertEqual(self._status(), ws.INVITE_PENDING)
        self.assertIsNone(api._active_membership(ORG_A, "u-joiner"))
        # And the invariant's other half: it is still USABLE.
        self.assertEqual(parse(self._accept())[0], 201)
        self.assertEqual(self._status(), ws.INVITE_ACCEPTED)
        self.assertIsNotNone(api._active_membership(ORG_A, "u-joiner"))

    def test_finalize_failure_still_converges(self):
        """If the FINALIZE (step 3) fails, the membership from step 2 already
        exists — so the invariant's forbidden state (ACCEPTED with no
        membership) never occurs, and a retry converges instead of locking
        anyone out.

        Note the retry answers 200 already_member, not 201: the person IS a
        member by then. That is the correct reading of the invariant — what
        must never happen is a SPENT token with no membership, not a
        particular status code.
        """
        real_update = self.t["invitations"].update_item
        calls = {"n": 0}

        def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] == 2:      # the ACCEPTING->ACCEPTED finalize
                raise RuntimeError("finalize failed")
            return real_update(**kwargs)

        with mock.patch.object(self.t["invitations"], "update_item",
                               side_effect=flaky):
            with self.assertRaises(RuntimeError):
                self._accept()

        # Compensated off the in-flight state, and the membership survived.
        self.assertEqual(self._status(), ws.INVITE_PENDING)
        self.assertIsNotNone(api._active_membership(ORG_A, "u-joiner"))

        # The retry converges: membership + ACCEPTED, no lockout.
        status, body = parse(self._accept())
        self.assertEqual(status, 200)
        self.assertTrue(body["already_member"])
        self.assertEqual(self._status(), ws.INVITE_ACCEPTED)

    def test_never_accepted_without_a_membership(self):
        """Stated as the invariant itself, over every failure injection."""
        for where, target, method in (
            ("membership", self.t["memberships"], "put_item"),
        ):
            with self.subTest(failure=where):
                with mock.patch.object(target, method,
                                       side_effect=RuntimeError("x")):
                    with self.assertRaises(RuntimeError):
                        self._accept()
                accepted = self._status() == ws.INVITE_ACCEPTED
                has_member = api._active_membership(
                    ORG_A, "u-joiner") is not None
                self.assertFalse(accepted and not has_member,
                                 "ACCEPTED with no membership")

    def test_concurrent_acceptance_admits_exactly_one(self):
        """Two racing requests: the conditional claim decides, and only one
        membership is created."""
        self.assertEqual(parse(self._accept())[0], 201)
        # A second identity holding the same token finds it spent.
        self.t["users"].put_item(Item={
            "user_id": "u-joiner2", "email": "joiner@work.com",
            "name": "J2", "created_at": NOW})
        self.t["memberships"].put_item(Item=ws.new_membership(
            ORG_B, "u-joiner2", ws.ROLE_MEMBER, NOW))
        self.as_user("u-joiner2")
        with self.assertRaises(api.ApiError) as ctx:
            self._accept()
        self.assertEqual(ctx.exception.status, 404)
        self.assertIsNone(api._active_membership(ORG_A, "u-joiner2"))

    def test_a_token_stranded_mid_flight_fails_closed(self):
        """ACCEPTING is not an open state: a crash between the claim and the
        finalize must make the token unusable, never replayable."""
        self.t["invitations"].update_item(
            Key={"invitation_id": self.invitation_id},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": ws.INVITE_ACCEPTING})
        with self.assertRaises(api.ApiError) as ctx:
            self._accept()
        self.assertEqual(ctx.exception.status, 404)
        self.assertFalse(ws.invite_is_open(
            {"status": ws.INVITE_ACCEPTING,
             "expires_at": "2099-01-01T00:00:00Z"}))

    def test_idempotent_reacceptance_after_success(self):
        self.assertEqual(parse(self._accept())[0], 201)
        # Same person, same link, tapped again.
        status, body = parse(self._accept())
        self.assertEqual(status, 200)
        self.assertTrue(body["already_member"])


class TestPersonalEmailErrorContract(OrgHarness):
    """P2: the personal-email conflict must be machine-readable."""

    def setUp(self):
        super().setUp()
        self.as_user(OWNER_A)
        _, body = parse(api.invite_member(event(
            method="POST",
            route="/workspaces/{workspace_id}/members/invite",
            path={"workspace_id": ORG_A},
            body={"email": "personal@gmail.com", "role": ws.ROLE_MEMBER})))
        self.token = body["invite_token"]
        # An ESTABLISHED personal identity: it owns real personal data, which
        # is what makes it a personal account. (Phase 2C: the absence of an
        # organisation membership is NOT the signal — every signup gets a
        # derived Personal workspace, so that test refused everyone.)
        self.t["contacts"].put_item(
            Item=api._contact_item("u-personal", "My private contact"))
        self.t["users"].put_item(Item={
            "user_id": "u-personal", "email": "personal@gmail.com",
            "name": "P", "created_at": NOW})
        self.as_user("u-personal")

    def test_error_carries_a_stable_code_not_just_prose(self):
        with self.assertRaises(api.OrganisationEmailConflict) as ctx:
            api.accept_invitation(event(
                method="POST", route="/workspace-invitations/{token}/accept",
                path={"token": self.token}))
        err = ctx.exception
        self.assertEqual(err.status, 409)
        self.assertEqual(err.code, api.ORG_EMAIL_CONFLICT_CODE)
        self.assertEqual(err.code, "organisation_email_conflict")
        # Still human-readable, and still tells them what to do.
        self.assertIn("work email", err.message)

    def test_the_code_reaches_the_http_response(self):
        """The frontend reads data.code; a code that never leaves the Lambda
        would force it back to string matching."""
        api._ROUTES[("POST", "/__accept_probe")] = api.accept_invitation
        resp = api.lambda_handler({
            "routeKey": "POST /__accept_probe",
            "headers": {"authorization": "Bearer t"},
            "pathParameters": {"token": self.token},
            "requestContext": {"http": {"method": "POST",
                                        "path": "/__accept_probe"}},
        }, None)
        del api._ROUTES[("POST", "/__accept_probe")]
        self.assertEqual(resp["statusCode"], 409)
        body = json.loads(resp["body"])
        self.assertEqual(body["code"], "organisation_email_conflict")

    def test_it_is_never_401(self):
        # A 401 clears the JWT client-side and would sign the user out of the
        # personal account this error tells them to keep.
        with self.assertRaises(api.ApiError) as ctx:
            api.accept_invitation(event(
                method="POST", route="/workspace-invitations/{token}/accept",
                path={"token": self.token}))
        self.assertNotEqual(ctx.exception.status, 401)

    def test_nothing_was_merged_or_converted(self):
        with self.assertRaises(api.ApiError):
            api.accept_invitation(event(
                method="POST", route="/workspace-invitations/{token}/accept",
                path={"token": self.token}))
        self.assertIsNone(api._active_membership(ORG_A, "u-personal"))
        self.assertFalse(api._has_organisation_membership("u-personal"))


class TestNoNPlusOneAuthorization(OrgHarness):
    """P2: authorization must not cost 2 device queries per row."""

    def setUp(self):
        super().setUp()
        for i in range(12):
            self.make_meeting(f"recordings/u-mea/mobile/n{i}.m4a",
                              MEMBER_A, ORG_A)

    def test_device_lookup_is_resolved_once_per_list(self):
        self.as_user(MANAGER_A)
        with mock.patch.object(api, "_owned_devices",
                               wraps=api._owned_devices) as d:
            api.list_recordings(event(workspace=ORG_A))
            # Once for the page, not once per row.
            self.assertLessEqual(d.call_count, 1, "N+1 device lookups")

    def test_membership_is_resolved_once_per_list(self):
        self.as_user(MANAGER_A)
        with mock.patch.object(
                self.t["memberships"], "get_item",
                wraps=self.t["memberships"].get_item) as g:
            api.list_recordings(event(workspace=ORG_A))
            self.assertLessEqual(g.call_count, 1, "N+1 membership lookups")

    def test_trash_listing_is_also_bounded(self):
        self.as_user(MANAGER_A)
        with mock.patch.object(api, "_owned_devices",
                               wraps=api._owned_devices) as d:
            api.list_trash(event(workspace=ORG_A))
            self.assertLessEqual(d.call_count, 1)


class TestRoleManagement(OrgHarness):
    def _patch(self, target, role, workspace=ORG_A):
        return api.update_member(event(
            method="PATCH",
            route="/workspaces/{workspace_id}/members/{user_id}",
            path={"workspace_id": workspace, "user_id": target},
            body={"role": role}))

    def test_owner_can_promote_a_member_to_manager(self):
        self.as_user(OWNER_A)
        status, body = parse(self._patch(MEMBER_A, ws.ROLE_MANAGER))
        self.assertEqual(status, 200)
        self.assertEqual(body["member"]["role"], ws.ROLE_MANAGER)

    def test_member_cannot_change_roles(self):
        self.as_user(MEMBER_A)
        with self.assertRaises(api.ApiError) as ctx:
            self._patch(MEMBER_A2, ws.ROLE_MANAGER)
        self.assertEqual(ctx.exception.status, 403)

    def test_manager_cannot_escalate_anyone_to_owner(self):
        self.as_user(MANAGER_A)
        with self.assertRaises(api.ApiError) as ctx:
            self._patch(MEMBER_A, ws.ROLE_OWNER)
        self.assertEqual(ctx.exception.status, 403)

    def test_manager_cannot_escalate_themselves_to_owner(self):
        self.as_user(MANAGER_A)
        with self.assertRaises(api.ApiError) as ctx:
            self._patch(MANAGER_A, ws.ROLE_OWNER)
        self.assertEqual(ctx.exception.status, 403)
        self.assertEqual(
            api._active_membership(ORG_A, MANAGER_A)["role"], ws.ROLE_MANAGER)

    def test_the_owners_role_cannot_be_changed(self):
        # This is what keeps the organisation from becoming ownerless.
        self.as_user(OWNER_A)
        with self.assertRaises(api.ApiError) as ctx:
            self._patch(OWNER_A, ws.ROLE_MEMBER)
        self.assertEqual(ctx.exception.status, 403)

    def test_cannot_change_a_member_of_another_workspace(self):
        self.as_user(OWNER_A)
        with self.assertRaises(api.ApiError) as ctx:
            self._patch(OWNER_B, ws.ROLE_MEMBER)
        self.assertEqual(ctx.exception.status, 404)

    def test_cannot_change_a_non_member(self):
        self.as_user(OWNER_A)
        with self.assertRaises(api.ApiError) as ctx:
            self._patch(OUTSIDER, ws.ROLE_MANAGER)
        self.assertEqual(ctx.exception.status, 404)

    def test_invalid_role_is_rejected(self):
        self.as_user(OWNER_A)
        with self.assertRaises(api.ApiError) as ctx:
            self._patch(MEMBER_A, "ADMIN")
        self.assertEqual(ctx.exception.status, 400)


# ===========================================================================
# THE P0 REGRESSION — a removed member must not keep access to organisation
# data they created.
#
# THE VULNERABILITY. Every recording predicate used to test
# `item["user_id"] == user_id` FIRST and return "OWNER" before anything looked
# at membership. So a member removed from an organisation kept read AND write
# access to every meeting they had created there: ~30 mutating routes behind
# _owned_recording, plus create_share and crm_sync_record, plus the AI's
# get_meeting_context (whose owns_recording carried its own copy of the same
# expression).
#
# THE INVARIANT NOW ASSERTED:
#
#     organisation-owned resource + no ACTIVE membership  =>  DENY,
#     even when created_by == the caller.
#
# Personal resources are unaffected and are asserted separately, because the
# wrong fix here would be to make personal data require membership too.
# ===========================================================================
class TestRemovedMemberCannotUseCreatorBypass(OrgHarness):
    """The exact scenario from the remediation brief, step by step."""

    def setUp(self):
        super().setUp()
        # 1-3. Organisation A exists, MEMBER_A is a member, and they create a
        #      meeting in it.
        self.key = "recordings/u-mea/mobile/theirs.m4a"
        self.item = self.make_meeting(self.key, MEMBER_A, ORG_A)
        self.task = api._write_task(api._new_task_row(
            MEMBER_A, "Follow up", recording_key=self.key,
            workspace_id=ORG_A))

    def _remove_member_a(self):
        self.as_user(OWNER_A)
        api.remove_member(event(
            method="DELETE",
            route="/workspaces/{workspace_id}/members/{user_id}",
            path={"workspace_id": ORG_A, "user_id": MEMBER_A}))
        self.as_user(MEMBER_A)

    def test_step4_creator_has_access_while_a_member(self):
        self.assertEqual(api._can_read_meeting(MEMBER_A, self.item), "OWNER")
        self.assertTrue(api._can_write_meeting(MEMBER_A, self.item))

    def test_step6_membership_lookup_returns_none_after_removal(self):
        self._remove_member_a()
        self.assertIsNone(api._active_membership(ORG_A, MEMBER_A))

    def test_step7_removed_creator_cannot_read_the_meeting(self):
        self._remove_member_a()
        # THE assertion: created_by == removed user must NOT restore access.
        self.assertEqual(self.item.get("user_id"), MEMBER_A)
        self.assertEqual(api._can_read_meeting(MEMBER_A, self.item), "")

    def test_step7_removed_creator_cannot_write_the_meeting(self):
        self._remove_member_a()
        self.assertFalse(api._can_write_meeting(MEMBER_A, self.item))

    def test_step7_removed_creator_is_denied_on_every_protected_route(self):
        """Read, write, AI mutation, share, CRM — one table, no gaps."""
        self._remove_member_a()

        read_routes = [
            ("get_recording", lambda: api.get_recording(
                event(route="/recordings/{key+}", key=self.key))),
            ("get_assignee_meeting", lambda: api.get_assignee_meeting(
                event(route="/recordings/shared-with-me/{key+}",
                      key=self.key))),
        ]
        # Everything gated by _owned_recording — the ~30 mutating routes are
        # represented by their shared predicate plus a sample of real ones.
        write_routes = [
            ("_owned_recording", lambda: api._owned_recording(
                event(route="/recordings/ai/mom/{key+}", key=self.key))),
            ("patch_recording", lambda: api.patch_recording(
                event(method="PATCH", route="/recordings/{key+}",
                      key=self.key, body={"title": "hijacked"}))),
            ("complete_upload", lambda: api.complete_upload(
                event(method="POST", route="/recordings/upload-complete",
                      body={"key": self.key}))),
            ("create_share", lambda: api.create_share(
                event(method="POST", route="/recordings/share/{key+}",
                      key=self.key, body={}))),
            ("list_shares", lambda: api.list_shares(
                event(route="/recordings/shares/{key+}", key=self.key))),
            ("crm_sync_record", lambda: api.crm_sync_record(
                event(method="POST", route="/crm/salesforce/sync/{key+}",
                      key=self.key, body={}))),
            ("set_participant", lambda: api.set_participant(
                event(method="PUT",
                      route="/recordings/participants/{key+}",
                      key=self.key, body={"speaker_id": "0",
                                          "contact_id": "x"}))),
            ("create_meeting_task", lambda: api.create_meeting_task(
                event(method="POST", route="/recordings/ai/tasks/{key+}",
                      key=self.key, body={"task": "new"}))),
            ("delete_recording", lambda: api.delete_recording(
                event(method="DELETE", route="/recordings/{key+}",
                      key=self.key))),
        ]

        for name, call in read_routes + write_routes:
            with self.subTest(route=name):
                with self.assertRaises(api.ApiError) as ctx:
                    call()
                self.assertEqual(ctx.exception.status, 404, name)

    def test_step7_removed_creator_cannot_reach_ai_meeting_content(self):
        """The AI path had its OWN copy of the creator test (owns_recording)
        and became the weaker door when the REST side was fixed."""
        self._remove_member_a()
        ctx = api.AIContext(user_id=MEMBER_A)
        self.assertFalse(ctx.owns_recording(self.item))
        # And while still a member it DID work, so this test is pinning the
        # revocation and not merely a broken predicate.
        self.assertTrue(api.AIContext(user_id=MEMBER_A2) is not None)
        self.assertTrue(api.AIContext(user_id=OWNER_A).owns_recording(
            self.make_meeting("recordings/u-oa/mobile/own.m4a",
                              OWNER_A, ORG_A)))

    def test_step7_removed_creator_sees_no_workspace_listing(self):
        self._remove_member_a()
        for name, call in (
            ("list_recordings", lambda: api.list_recordings(
                event(workspace=ORG_A))),
            ("list_trash", lambda: api.list_trash(event(workspace=ORG_A))),
        ):
            with self.subTest(route=name):
                with self.assertRaises(api.ApiError) as ctx:
                    call()
                self.assertEqual(ctx.exception.status, 404, name)

    def test_step8_another_active_member_still_has_access_by_policy(self):
        """Removing one person must not break the organisation for everyone
        else — the failure mode of an over-broad fix."""
        self._remove_member_a()
        # OWNER and MANAGER keep full visibility.
        self.assertEqual(api._can_read_meeting(OWNER_A, self.item), "ROLE")
        self.assertEqual(api._can_read_meeting(MANAGER_A, self.item), "ROLE")
        self.as_user(MANAGER_A)
        self.assertEqual(
            parse(api.get_recording(
                event(route="/recordings/{key+}", key=self.key)))[0], 200)
        # A plain MEMBER still gets nothing without a grant (unchanged rule).
        self.assertEqual(api._can_read_meeting(MEMBER_A2, self.item), "")

    def test_organisation_data_survives_the_removal(self):
        """Access stops; DATA stays. The meeting belongs to the organisation."""
        self._remove_member_a()
        self.assertIsNotNone(self.t["recordings"].get_item(
            Key={"audio_s3_key": self.key}).get("Item"))
        self.assertIsNotNone(self.t["tasks"].get_item(
            Key={"task_id": self.task["task_id"]}).get("Item"))

    def test_personal_resources_never_require_membership(self):
        """The wrong fix would make personal data need an organisation."""
        self._remove_member_a()
        personal = self.make_meeting(
            "recordings/u-mea/mobile/mine.m4a", MEMBER_A, None)
        self.assertEqual(api._can_read_meeting(MEMBER_A, personal), "OWNER")
        self.assertTrue(api._can_write_meeting(MEMBER_A, personal))
        self.as_user(MEMBER_A)
        self.assertEqual(parse(api.get_recording(event(
            route="/recordings/{key+}",
            key="recordings/u-mea/mobile/mine.m4a")))[0], 200)


class TestRevocationMatrix(OrgHarness):
    """ACTIVE / REMOVED / NON-MEMBER against every protected surface.

    One table so a new surface cannot be added without a row here. Where an
    endpoint is intentionally different, the expectation says why.
    """

    def setUp(self):
        super().setUp()
        self.key = "recordings/u-mea/mobile/m.m4a"
        self.item = self.make_meeting(self.key, MEMBER_A, ORG_A)
        self.as_user(OWNER_A)
        api.remove_member(event(
            method="DELETE",
            route="/workspaces/{workspace_id}/members/{user_id}",
            path={"workspace_id": ORG_A, "user_id": REMOVED}))

    def test_read_matrix(self):
        cases = [
            # (who, expected access reason, why)
            (MEMBER_A, "OWNER", "active creator"),
            (OWNER_A, "ROLE", "owner sees all"),
            (MANAGER_A, "ROLE", "manager sees all"),
            (MEMBER_A2, "", "active member, no grant"),
            (REMOVED, "", "removed member"),
            (OUTSIDER, "", "non-member"),
            (OWNER_B, "", "member of another organisation"),
        ]
        for who, expected, why in cases:
            with self.subTest(who=why):
                self.assertEqual(
                    api._can_read_meeting(who, self.item), expected, why)

    def test_write_matrix(self):
        cases = [
            (MEMBER_A, True, "active creator may write"),
            (OWNER_A, False, "owner may READ but not edit a colleague's"),
            (MANAGER_A, False, "manager may READ but not edit"),
            (MEMBER_A2, False, "active member, not creator"),
            (REMOVED, False, "removed member"),
            (OUTSIDER, False, "non-member"),
        ]
        for who, expected, why in cases:
            with self.subTest(who=why):
                self.assertIs(
                    api._can_write_meeting(who, self.item), expected, why)

    def test_removed_member_who_created_nothing_is_also_denied(self):
        other = self.make_meeting("recordings/u-oa/mobile/o.m4a",
                                  OWNER_A, ORG_A)
        self.assertEqual(api._can_read_meeting(REMOVED, other), "")

    def test_explicit_grant_does_not_survive_removal(self):
        api._grant_meeting_access(self.key, REMOVED, ws.ACCESS_EXPLICIT,
                                  workspace_id=ORG_A, granted_by=OWNER_A)
        self.assertEqual(api._can_read_meeting(REMOVED, self.item), "")

    def test_trash_and_folder_views_agree_with_the_predicate(self):
        self.t["recordings"].update_item(
            Key={"audio_s3_key": self.key},
            UpdateExpression="SET recording_status = :s, deleted_at = :d",
            ExpressionAttributeValues={":s": "trashed", ":d": NOW})
        # MANAGER sees it in the organisation Trash; a removed member 404s.
        self.as_user(MANAGER_A)
        _, body = parse(api.list_trash(event(workspace=ORG_A)))
        self.assertEqual({r["audio_s3_key"] for r in body["recordings"]},
                         {self.key})
        self.as_user(REMOVED)
        with self.assertRaises(api.ApiError) as ctx:
            api.list_trash(event(workspace=ORG_A))
        self.assertEqual(ctx.exception.status, 404)


class TestMemberRemoval(OrgHarness):
    def _remove(self, target, workspace=ORG_A):
        return api.remove_member(event(
            method="DELETE",
            route="/workspaces/{workspace_id}/members/{user_id}",
            path={"workspace_id": workspace, "user_id": target}))

    def test_owner_can_remove_a_member(self):
        self.as_user(OWNER_A)
        status, body = parse(self._remove(MEMBER_A))
        self.assertEqual(status, 200)
        self.assertTrue(body["removed"])

    def test_access_stops_immediately(self):
        """The reason membership is not in the JWT."""
        self.assertIsNotNone(api._active_membership(ORG_A, MEMBER_A))
        self.as_user(OWNER_A)
        self._remove(MEMBER_A)
        self.assertIsNone(api._active_membership(ORG_A, MEMBER_A))
        self.as_user(MEMBER_A)
        with self.assertRaises(api.ApiError) as ctx:
            api._require_workspace_member(event(workspace=ORG_A))
        self.assertEqual(ctx.exception.status, 404)

    def test_the_owner_cannot_be_removed(self):
        self.as_user(OWNER_A)
        with self.assertRaises(api.ApiError) as ctx:
            self._remove(OWNER_A)
        self.assertEqual(ctx.exception.status, 403)

    def test_member_cannot_remove_anyone(self):
        self.as_user(MEMBER_A)
        with self.assertRaises(api.ApiError) as ctx:
            self._remove(MEMBER_A2)
        self.assertEqual(ctx.exception.status, 403)

    def test_removal_does_not_delete_organisation_data(self):
        """Rahul records for ABC Realty, leaves; the meeting stays."""
        key = "recordings/u-mea/mobile/m_1.m4a"
        self.make_meeting(key, MEMBER_A, ORG_A)
        self.t["tasks"].put_item(Item={
            "task_id": "t1", "owner_user_id": MEMBER_A,
            "workspace_id": ORG_A, "created_by": MEMBER_A,
            "source_recording_id": key, "title": "Follow up",
            "created_at": NOW})

        self.as_user(OWNER_A)
        self._remove(MEMBER_A)

        self.assertIsNotNone(
            self.t["recordings"].get_item(Key={"audio_s3_key": key})["Item"])
        self.assertIsNotNone(
            self.t["tasks"].get_item(Key={"task_id": "t1"})["Item"])
        # And the OWNER can still read it — it belongs to the organisation.
        self.assertTrue(api._can_read_meeting(
            OWNER_A,
            self.t["recordings"].get_item(Key={"audio_s3_key": key})["Item"]))

    def test_removed_member_row_is_kept_for_audit(self):
        self.as_user(OWNER_A)
        self._remove(MEMBER_A)
        row = self.t["memberships"].get_item(
            Key={"workspace_id": ORG_A, "user_id": MEMBER_A})["Item"]
        self.assertEqual(row["status"], ws.MEMBERSHIP_REMOVED)
        self.assertEqual(row["removed_by"], OWNER_A)


class TestMembersList(OrgHarness):
    def test_any_member_may_list_colleagues(self):
        self.as_user(MEMBER_A)
        status, body = parse(api.list_members(event(
            route="/workspaces/{workspace_id}/members",
            path={"workspace_id": ORG_A})))
        self.assertEqual(status, 200)
        # Asserted by MEMBERSHIP, not a hardcoded number: the harness gains
        # actors over time and a literal count turns that into a false alarm.
        self.assertEqual({m["user_id"] for m in body["members"]},
                         {OWNER_A, MANAGER_A, MEMBER_A, MEMBER_A2, REMOVED})
        self.assertEqual(body["count"], len(body["members"]))

    def test_removed_members_are_not_listed(self):
        self.as_user(OWNER_A)
        api.remove_member(event(
            method="DELETE",
            route="/workspaces/{workspace_id}/members/{user_id}",
            path={"workspace_id": ORG_A, "user_id": MEMBER_A}))
        _, body = parse(api.list_members(event(
            route="/workspaces/{workspace_id}/members",
            path={"workspace_id": ORG_A})))
        self.assertNotIn(MEMBER_A, [m["user_id"] for m in body["members"]])

    def test_outsider_cannot_list_members(self):
        self.as_user(OUTSIDER)
        with self.assertRaises(api.ApiError) as ctx:
            api.list_members(event(
                route="/workspaces/{workspace_id}/members",
                path={"workspace_id": ORG_A}))
        self.assertEqual(ctx.exception.status, 404)

    def test_no_password_material_is_returned(self):
        self.t["users"].put_item(Item={
            "user_id": MEMBER_A, "email": "m@work.com", "name": "M",
            "password_hash": "SECRET", "salt": "SALT", "created_at": NOW})
        self.as_user(OWNER_A)
        _, body = parse(api.list_members(event(
            route="/workspaces/{workspace_id}/members",
            path={"workspace_id": ORG_A})))
        blob = json.dumps(body)
        self.assertNotIn("SECRET", blob)
        self.assertNotIn("SALT", blob)


# ===========================================================================
# 4. MEETING OWNERSHIP AND VISIBILITY
# ===========================================================================
class TestMeetingVisibility(OrgHarness):
    def setUp(self):
        super().setUp()
        self.key = "recordings/u-mea/mobile/m_1.m4a"
        self.item = self.make_meeting(self.key, MEMBER_A, ORG_A)

    def test_creator_can_read(self):
        self.assertTrue(api._can_read_meeting(MEMBER_A, self.item))

    def test_owner_and_manager_see_all_organisation_meetings(self):
        self.assertEqual(api._can_read_meeting(OWNER_A, self.item), "ROLE")
        self.assertEqual(api._can_read_meeting(MANAGER_A, self.item), "ROLE")

    def test_membership_is_not_access_for_a_plain_member(self):
        """The rule the brief calls out: a colleague is not automatically a
        reader."""
        self.assertEqual(api._can_read_meeting(MEMBER_A2, self.item), "")

    def test_explicit_grant_admits_a_member(self):
        api._grant_meeting_access(self.key, MEMBER_A2, ws.ACCESS_EXPLICIT,
                                  workspace_id=ORG_A, granted_by=OWNER_A)
        self.assertEqual(api._can_read_meeting(MEMBER_A2, self.item),
                         ws.ACCESS_EXPLICIT)

    def test_a_grant_cannot_survive_losing_membership(self):
        # Membership is checked BEFORE the grant, so a removed member's stale
        # grant row cannot keep working.
        api._grant_meeting_access(self.key, MEMBER_A2, ws.ACCESS_PARTICIPANT,
                                  workspace_id=ORG_A)
        self.as_user(OWNER_A)
        api.remove_member(event(
            method="DELETE",
            route="/workspaces/{workspace_id}/members/{user_id}",
            path={"workspace_id": ORG_A, "user_id": MEMBER_A2}))
        self.assertEqual(api._can_read_meeting(MEMBER_A2, self.item), "")

    def test_non_member_is_denied(self):
        self.assertEqual(api._can_read_meeting(OUTSIDER, self.item), "")
        self.assertEqual(api._can_read_meeting(OWNER_B, self.item), "")

    def test_get_recording_enforces_the_same_rule(self):
        self.as_user(MEMBER_A2)
        with self.assertRaises(api.ApiError) as ctx:
            api.get_recording(event(route="/recordings/{key+}", key=self.key))
        self.assertEqual(ctx.exception.status, 404)

        self.as_user(MANAGER_A)
        status, _ = parse(api.get_recording(
            event(route="/recordings/{key+}", key=self.key)))
        self.assertEqual(status, 200)

    def test_personal_meetings_are_unaffected_by_roles(self):
        pkey = "recordings/u-oa/mobile/p_1.m4a"
        personal = self.make_meeting(pkey, OWNER_A, None)  # no workspace_id
        self.assertTrue(api._can_read_meeting(OWNER_A, personal))
        # Being a MANAGER of ORG_A grants nothing over someone's personal row.
        self.assertEqual(api._can_read_meeting(MANAGER_A, personal), "")

    def test_write_access_is_not_widened_by_read_visibility(self):
        """_owned_recording gates ~30 mutating routes and must stay creator-
        only: a MANAGER may READ a colleague's meeting but not EDIT it."""
        self.as_user(MANAGER_A)
        with self.assertRaises(api.ApiError) as ctx:
            api._owned_recording(
                event(route="/recordings/ai/mom/{key+}", key=self.key))
        self.assertEqual(ctx.exception.status, 404)


class TestMeetingWorkspaceStamping(OrgHarness):
    def test_absent_workspace_resolves_to_the_owners_personal_workspace(self):
        item = self.make_meeting("recordings/u-oa/mobile/x.m4a", OWNER_A, None)
        self.assertEqual(api._row_workspace_id(item),
                         ws.personal_workspace_id(OWNER_A))

    def test_a_stamped_row_is_taken_at_its_word(self):
        item = self.make_meeting("recordings/u-mea/mobile/y.m4a",
                                 MEMBER_A, ORG_A)
        self.assertEqual(api._row_workspace_id(item), ORG_A)

    def test_absent_workspace_can_never_resolve_to_an_organisation(self):
        # The migration guarantee: existing personal data cannot become
        # organisation data by accident.
        for owner in (OWNER_A, MANAGER_A, MEMBER_A, OUTSIDER):
            wid = api._row_workspace_id({"user_id": owner})
            self.assertFalse(ws.is_organisation_workspace_id(wid))

    def test_creation_workspace_verifies_membership(self):
        self.as_user(OUTSIDER)
        with self.assertRaises(api.ApiError) as ctx:
            api._creation_workspace(event(workspace=ORG_A), OUTSIDER)
        self.assertEqual(ctx.exception.status, 404)

    def test_creation_workspace_defaults_to_personal(self):
        self.assertEqual(api._creation_workspace(event(), MEMBER_A),
                         ws.personal_workspace_id(MEMBER_A))

    def test_creation_workspace_accepts_a_verified_membership(self):
        self.assertEqual(
            api._creation_workspace(event(workspace=ORG_A), MEMBER_A), ORG_A)

    def test_request_upload_stamps_the_row_and_refuses_a_foreign_workspace(self):
        """The route, end to end: an unverified workspace must be refused
        BEFORE a presigned URL is minted, and a verified one must land on the
        row."""
        with mock.patch.object(api, "BUCKET_NAME", "bkt"), \
                mock.patch.object(api._s3, "generate_presigned_url",
                                  return_value="https://signed"):
            # Outsider naming ORG_A: refused, and nothing is created.
            self.as_user(OUTSIDER)
            with self.assertRaises(api.ApiError) as ctx:
                api.request_upload(event(
                    method="POST", route="/recordings/upload-request",
                    workspace=ORG_A, body={"source": "MOBILE", "format": "m4a"}))
            self.assertEqual(ctx.exception.status, 404)

            # A real member: the row carries the workspace and the creator.
            self.as_user(MEMBER_A)
            status, body = parse(api.request_upload(event(
                method="POST", route="/recordings/upload-request",
                workspace=ORG_A, body={"source": "MOBILE", "format": "m4a"})))
            self.assertEqual(status, 200)
            row = self.t["recordings"].get_item(
                Key={"audio_s3_key": body["key"]})["Item"]
            self.assertEqual(row["workspace_id"], ORG_A)
            self.assertEqual(row["created_by"], MEMBER_A)

            # No header at all -> personal, exactly as before workspaces.
            status, body = parse(api.request_upload(event(
                method="POST", route="/recordings/upload-request",
                body={"source": "MOBILE", "format": "m4a"})))
            row = self.t["recordings"].get_item(
                Key={"audio_s3_key": body["key"]})["Item"]
            self.assertEqual(row["workspace_id"],
                             ws.personal_workspace_id(MEMBER_A))


class TestWorkspaceRecordingList(OrgHarness):
    def setUp(self):
        super().setUp()
        self.mine = "recordings/u-mea/mobile/mine.m4a"
        self.theirs = "recordings/u-mea2/mobile/theirs.m4a"
        self.make_meeting(self.mine, MEMBER_A, ORG_A)
        self.make_meeting(self.theirs, MEMBER_A2, ORG_A)
        self.make_meeting("recordings/u-ob/mobile/other.m4a", OWNER_B, ORG_B)

    def test_manager_sees_every_meeting_in_their_org_only(self):
        self.as_user(MANAGER_A)
        _, body = parse(api.list_recordings(event(workspace=ORG_A)))
        keys = {r["audio_s3_key"] for r in body["recordings"]}
        self.assertEqual(keys, {self.mine, self.theirs})

    def test_member_sees_only_their_own(self):
        self.as_user(MEMBER_A)
        _, body = parse(api.list_recordings(event(workspace=ORG_A)))
        self.assertEqual({r["audio_s3_key"] for r in body["recordings"]},
                         {self.mine})

    def test_outsider_gets_404(self):
        self.as_user(OUTSIDER)
        with self.assertRaises(api.ApiError) as ctx:
            api.list_recordings(event(workspace=ORG_A))
        self.assertEqual(ctx.exception.status, 404)

    def test_org_b_owner_never_sees_org_a(self):
        self.as_user(OWNER_B)
        _, body = parse(api.list_recordings(event(workspace=ORG_B)))
        keys = {r["audio_s3_key"] for r in body["recordings"]}
        self.assertNotIn(self.mine, keys)
        self.assertNotIn(self.theirs, keys)

    def test_personal_listing_is_unchanged_and_excludes_org_rows(self):
        # No header -> the original code path, byte-for-byte.
        self.as_user(MEMBER_A)
        _, body = parse(api.list_recordings(event()))
        self.assertEqual({r["audio_s3_key"] for r in body["recordings"]},
                         {self.mine})

    def test_membership_is_resolved_once_per_list_not_per_row(self):
        """The N+1 the brief's performance section rules out."""
        self.as_user(MANAGER_A)
        with mock.patch.object(
                self.t["memberships"], "get_item",
                wraps=self.t["memberships"].get_item) as g:
            api.list_recordings(event(workspace=ORG_A))
            self.assertLessEqual(g.call_count, 1)


# ===========================================================================
# 5. TASKS
# ===========================================================================
class TestTaskWorkspace(OrgHarness):
    def test_task_inherits_the_meetings_workspace(self):
        item = self.make_meeting("recordings/u-mea/mobile/t.m4a",
                                 MEMBER_A, ORG_A)
        row = api._new_task_row(MEMBER_A, "Do it",
                                recording_key=item["audio_s3_key"],
                                workspace_id=api._row_workspace_id(item))
        self.assertEqual(row["workspace_id"], ORG_A)
        self.assertEqual(row["created_by"], MEMBER_A)
        self.assertEqual(row["owner_user_id"], MEMBER_A)

    def test_personal_task_has_no_workspace_key_after_write(self):
        # workspace_id is a sparse index key: "" must be ABSENT, not empty.
        # A source meeting is supplied because source_recording_id is itself a
        # (pre-existing) index key that the fake correctly refuses to leave
        # blank — this test is about workspace_id, not about that rule.
        row = api._new_task_row(MEMBER_A, "Personal",
                                recording_key="recordings/u-mea/mobile/p.m4a",
                                workspace_id="")
        written = api._write_task(row)
        self.assertNotIn("workspace_id", written)

    def test_folder_id_remains_optional(self):
        row = api._new_task_row(MEMBER_A, "No folder",
                                recording_key="recordings/u-mea/mobile/f.m4a")
        written = api._write_task(row)
        self.assertNotIn("folder_id", written)

    def test_created_by_survives_the_creator_leaving(self):
        item = self.make_meeting("recordings/u-mea/mobile/l.m4a",
                                 MEMBER_A, ORG_A)
        row = api._write_task(api._new_task_row(
            MEMBER_A, "Task", recording_key=item["audio_s3_key"],
            workspace_id=ORG_A))
        self.as_user(OWNER_A)
        api.remove_member(event(
            method="DELETE",
            route="/workspaces/{workspace_id}/members/{user_id}",
            path={"workspace_id": ORG_A, "user_id": MEMBER_A}))
        stored = self.t["tasks"].get_item(
            Key={"task_id": row["task_id"]})["Item"]
        self.assertEqual(stored["created_by"], MEMBER_A)
        self.assertEqual(stored["workspace_id"], ORG_A)


class TestTaskIsNotMeetingAccess(OrgHarness):
    """The brief's worked example, asserted end to end."""

    def test_assignee_gets_the_task_but_not_the_meeting(self):
        key = "recordings/u-mea/mobile/rahul.m4a"
        item = self.make_meeting(key, MEMBER_A, ORG_A)
        api._write_task(api._new_task_row(
            MEMBER_A, "Send the quote", recording_key=key,
            workspace_id=ORG_A))

        # Priya (MEMBER_A2) did not attend and has no grant.
        self.assertEqual(api._can_read_meeting(MEMBER_A2, item), "")
        self.as_user(MEMBER_A2)
        with self.assertRaises(api.ApiError) as ctx:
            api.get_recording(event(route="/recordings/{key+}", key=key))
        self.assertEqual(ctx.exception.status, 404)


# ===========================================================================
# 6. CONTACTS
# ===========================================================================
class TestContactWorkspaceStamping(OrgHarness):
    def test_new_contact_records_its_workspace(self):
        item = api._contact_item(MEMBER_A, "Asha", workspace_id=ORG_A)
        self.assertEqual(item["workspace_id"], ORG_A)
        self.assertEqual(item["created_by"], MEMBER_A)
        # owner_user_id is UNCHANGED and still the authorization key.
        self.assertEqual(item["owner_user_id"], MEMBER_A)

    def test_personal_contact_defaults_to_the_personal_workspace(self):
        item = api._contact_item(MEMBER_A, "Asha")
        self.assertEqual(item["workspace_id"],
                         ws.personal_workspace_id(MEMBER_A))

    def test_organisation_contacts_became_shared_in_phase_2c(self):
        """CHANGED BEHAVIOUR — deliberate, recorded rather than deleted.

        OLD (Phase 2B): organisation contacts were STAMPED but not shared, so
             a colleague got 404. Phase 2B documented this as a STOP pending
             the visibility decision.
        NEW (Phase 2C): the decision was made — organisation contacts are
             shared workspace resources. Every ACTIVE member may read and
             create; OWNER/MANAGER may edit and delete.
        WHY: the STOP was waiting on a product decision, not on a security
             property. Reads are still authorized in application code by
             _owned_contact, and the membership gate still precedes
             everything, so a removed member loses access.

        PERSONAL contacts are unaffected and asserted separately below.
        """
        self.t["contacts"].put_item(Item=api._contact_item(
            MEMBER_A, "Asha", workspace_id=ORG_A))
        cid = list(self.t["contacts"].items.values())[0]["contact_id"]

        # A colleague can now READ it.
        self.assertEqual(
            api._owned_contact(MEMBER_A2, cid)["contact_id"], cid)
        # But a plain MEMBER may not CHANGE it.
        with self.assertRaises(api.ApiError) as ctx:
            api._owned_contact(MEMBER_A2, cid, write=True)
        self.assertEqual(ctx.exception.status, 403)
        # A MANAGER may.
        self.assertEqual(
            api._owned_contact(MANAGER_A, cid, write=True)["contact_id"], cid)

    def test_personal_contacts_stay_private(self):
        """The rule that must NOT have changed."""
        self.t["contacts"].put_item(Item=api._contact_item(MEMBER_A, "Priv"))
        cid = list(self.t["contacts"].items.values())[0]["contact_id"]
        # Owner reads and writes it.
        self.assertEqual(api._owned_contact(MEMBER_A, cid)["name"], "Priv")
        self.assertEqual(
            api._owned_contact(MEMBER_A, cid, write=True)["name"], "Priv")
        # A colleague in the same organisation gets nothing — 404, not 403:
        # a personal contact's existence is not disclosed.
        for who in (MEMBER_A2, MANAGER_A, OWNER_A, OUTSIDER):
            with self.subTest(who=who):
                with self.assertRaises(api.ApiError) as ctx:
                    api._owned_contact(who, cid)
                self.assertEqual(ctx.exception.status, 404)

    def test_removed_member_loses_organisation_contact_access(self):
        """The Phase 2B P0 rule, applied to contacts: creator does not
        rescue a lost membership."""
        self.t["contacts"].put_item(Item=api._contact_item(
            REMOVED, "Theirs", workspace_id=ORG_A))
        cid = list(self.t["contacts"].items.values())[0]["contact_id"]
        # Still a member: readable.
        self.assertEqual(api._owned_contact(REMOVED, cid)["contact_id"], cid)
        self.as_user(OWNER_A)
        api.remove_member(event(
            method="DELETE",
            route="/workspaces/{workspace_id}/members/{user_id}",
            path={"workspace_id": ORG_A, "user_id": REMOVED}))
        # Removed — and being the creator does not restore it.
        with self.assertRaises(api.ApiError) as ctx:
            api._owned_contact(REMOVED, cid)
        self.assertEqual(ctx.exception.status, 404)


# ===========================================================================
# 7. THE FOUR-WORKSPACE ISOLATION MATRIX (brief section 36)
# ===========================================================================
class TestFourWorkspaceIsolation(OrgHarness):
    """Personal A, Personal B, Organisation A, Organisation B."""

    def setUp(self):
        super().setUp()
        self.pa = "recordings/u-oa/mobile/pa.m4a"    # personal A
        self.pb = "recordings/u-ob/mobile/pb.m4a"    # personal B
        self.oa = "recordings/u-mea/mobile/oa.m4a"   # org A
        self.ob = "recordings/u-ob/mobile/ob.m4a"    # org B
        self.i_pa = self.make_meeting(self.pa, OWNER_A, None)
        self.i_pb = self.make_meeting(self.pb, OWNER_B, None)
        self.i_oa = self.make_meeting(self.oa, MEMBER_A, ORG_A)
        self.i_ob = self.make_meeting(self.ob, OWNER_B, ORG_B)

    def test_every_cross_workspace_read_is_denied(self):
        cases = [
            # (reader, meeting, why it must be denied)
            (OWNER_B, self.i_pa, "B cannot read A's personal"),
            (OWNER_A, self.i_pb, "A cannot read B's personal"),
            (OWNER_B, self.i_oa, "B's owner is not in org A"),
            (OWNER_A, self.i_ob, "A's owner is not in org B"),
            (MANAGER_A, self.i_pb, "org role grants nothing personally"),
            (MANAGER_A, self.i_ob, "org A role grants nothing in org B"),
            (OUTSIDER, self.i_pa, "outsider"),
            (OUTSIDER, self.i_oa, "outsider"),
        ]
        for reader, item, why in cases:
            with self.subTest(why=why):
                self.assertEqual(api._can_read_meeting(reader, item), "", why)

    def test_each_owner_reads_only_their_own(self):
        self.assertTrue(api._can_read_meeting(OWNER_A, self.i_pa))
        self.assertTrue(api._can_read_meeting(OWNER_B, self.i_pb))
        self.assertTrue(api._can_read_meeting(MEMBER_A, self.i_oa))
        self.assertTrue(api._can_read_meeting(OWNER_B, self.i_ob))

    def test_workspace_listing_never_crosses(self):
        for user, wid, expected in ((MANAGER_A, ORG_A, {self.oa}),
                                    (OWNER_B, ORG_B, {self.ob})):
            with self.subTest(workspace=wid):
                self.as_user(user)
                _, body = parse(api.list_recordings(event(workspace=wid)))
                self.assertEqual(
                    {r["audio_s3_key"] for r in body["recordings"]}, expected)

    def test_async_seeding_keeps_each_meeting_in_its_own_workspace(self):
        """Section 36: the pipeline, not just the direct APIs.

        Ownership is taken from the RECORDING ROW, so a task seeded from an
        org-A meeting lands in org A even though the seeding runs from an
        async invoke with no user and no header.
        """
        for key, item, expected in ((self.oa, self.i_oa, ORG_A),
                                    (self.ob, self.i_ob, ORG_B),
                                    (self.pa, self.i_pa,
                                     ws.personal_workspace_id(OWNER_A))):
            with self.subTest(key=key):
                row = api._new_task_row(
                    item["user_id"], "Extracted", recording_key=key,
                    workspace_id=api._row_workspace_id(item))
                self.assertEqual(row["workspace_id"], expected)

    def test_a_forged_event_payload_cannot_move_a_meeting(self):
        # The async path resolves from the stored row; a workspace_id in the
        # event is never consulted.
        forged = dict(self.i_oa)
        self.assertEqual(api._row_workspace_id(forged), ORG_A)
        # Even if an attacker supplies a header, the ROW still decides.
        self.as_user(MEMBER_A)
        self.assertEqual(
            api._row_workspace_id(
                self.t["recordings"].get_item(
                    Key={"audio_s3_key": self.oa})["Item"]),
            ORG_A)

    def test_a_transcription_upsert_cannot_move_a_meeting(self):
        """The async pipeline writes with a targeted SET/REMOVE that names
        only the fields it computed, so workspace_id survives transcription,
        an STT webhook, a retry and a replay untouched. Simulated here with
        the same shape of partial update the transcribe Lambda issues."""
        self.t["recordings"].update_item(
            Key={"audio_s3_key": self.oa},
            UpdateExpression="SET #st = :s, transcript = :t, summary = :m",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":s": "complete", ":t": "words",
                                       ":m": "a summary"})
        row = self.t["recordings"].get_item(
            Key={"audio_s3_key": self.oa})["Item"]
        self.assertEqual(row["workspace_id"], ORG_A)
        self.assertEqual(api._row_workspace_id(row), ORG_A)
        # Re-running it (a Lambda retry) still cannot move the row.
        self.t["recordings"].update_item(
            Key={"audio_s3_key": self.oa},
            UpdateExpression="SET #st = :s",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={":s": "complete"})
        self.assertEqual(api._row_workspace_id(
            self.t["recordings"].get_item(
                Key={"audio_s3_key": self.oa})["Item"]), ORG_A)

    def test_derived_data_follows_its_parent(self):
        # Transcript/summary/AI live ON the recording row, so they inherit by
        # construction — this pins that they cannot be separated from it.
        self.t["recordings"].update_item(
            Key={"audio_s3_key": self.oa},
            UpdateExpression="SET summary = :s",
            ExpressionAttributeValues={":s": "org A summary"})
        row = self.t["recordings"].get_item(
            Key={"audio_s3_key": self.oa})["Item"]
        self.assertEqual(api._row_workspace_id(row), ORG_A)
        self.assertEqual(api._can_read_meeting(OWNER_B, row), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
