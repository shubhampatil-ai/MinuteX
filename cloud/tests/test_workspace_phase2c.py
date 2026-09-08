#!/usr/bin/env python3
"""test_workspace_phase2c.py — Phase 2C: shared contacts, the identity gate on
organisation creation, and workspace-aware AI context.

WHAT THIS FILE PINS.

  1. SHARED ORGANISATION CONTACTS. Every active member reads and creates;
     OWNER/MANAGER edit and delete. Personal contacts stay private, and a
     removed member loses access even to rows they created themselves.

  2. WORKSPACE-SCOPED DEDUPE. Two colleagues adding the same client converge
     on ONE row. This is the correctness bug Phase 2B documented as a STOP:
     owner-email-index is partitioned by the OWNER, so it cannot answer "does
     this organisation already have this person".

  3. THE ORGANISATION-CREATION IDENTITY GATE. An identity that has already
     USED its personal workspace cannot also become an organisation owner —
     that is the silent conversion the identity model forbids. Enforced on
     personal RESOURCES, not on the derived workspace row (which every
     identity has from signup, making the literal reading unenforceable).

  4. AI WORKSPACE CONTEXT, and the line between CONTEXT and AUTHORIZATION.
     The tools are scoped to the active workspace so answers are about the
     right place; authorization is still decided by application predicates,
     never by the prompt.

  5. NO FOLDERS IN ORGANISATIONS — enforced on the backend, not just hidden.

SECURITY POSTURE, as in every suite here: tests call the ROUTE FUNCTION
DIRECTLY with a forged identity, exactly as curl against the deployed API
would. Nothing here can be satisfied by hiding a button.

OFFLINE: fake_dynamodb, no AWS, no network.

Run:  python -m pytest tests/test_workspace_phase2c.py
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

# ORDER MATTERS — see test_task_permissions.py.
from test_ai_workspace import api  # noqa: E402
import fake_dynamodb as fdb  # noqa: E402
import workspace_schema as ws  # noqa: E402

OWNER_A, MANAGER_A, MEMBER_A, MEMBER_A2 = "u-oa", "u-ma", "u-mea", "u-mea2"
OWNER_B = "u-ob"
OUTSIDER = "u-out"
REMOVED = "u-rm"

ORG_A = "wso_c2aaaa11"
ORG_B = "wso_c2bbbb22"
NOW = "2026-09-07T10:00:00Z"


def parse(resp):
    return resp["statusCode"], json.loads(resp["body"])


def event(method="GET", route="/contacts", path=None, body=None,
          workspace=None, qs=None):
    ev = {
        "routeKey": f"{method} {route}",
        "requestContext": {"http": {"method": method, "path": route}},
        "headers": {"authorization": "Bearer test-token"},
        "pathParameters": dict(path or {}),
        "queryStringParameters": dict(qs or {}),
    }
    if workspace is not None:
        ev["headers"][api.WORKSPACE_HEADER] = workspace
    if body is not None:
        ev["body"] = json.dumps(body)
    return ev


class Phase2CHarness(unittest.TestCase):
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
                                 (ORG_B, OWNER_B, "XYZ Corp")):
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

    def make_contact(self, owner, name, workspace_id="", **extra):
        item = api._contact_item(owner, name, workspace_id=workspace_id,
                                 **extra)
        self.t["contacts"].put_item(Item=item)
        return item

    def remove_from_org_a(self, uid):
        keeper = self.current_user
        self.as_user(OWNER_A)
        api.remove_member(event(
            method="DELETE",
            route="/workspaces/{workspace_id}/members/{user_id}",
            path={"workspace_id": ORG_A, "user_id": uid}))
        self.as_user(keeper)


# ===========================================================================
# 1. SHARED ORGANISATION CONTACTS
# ===========================================================================
class TestSharedContactPermissions(Phase2CHarness):
    """The matrix from section 4, asserted through the real gate."""

    def setUp(self):
        super().setUp()
        self.shared = self.make_contact(MEMBER_A, "John Smith",
                                        workspace_id=ORG_A,
                                        company="ABC Construction")
        self.cid = self.shared["contact_id"]

    def test_every_active_member_can_read(self):
        for who in (OWNER_A, MANAGER_A, MEMBER_A, MEMBER_A2):
            with self.subTest(who=who):
                self.assertEqual(
                    api._owned_contact(who, self.cid)["contact_id"], self.cid)

    def test_only_owner_and_manager_can_write(self):
        for who, allowed in ((OWNER_A, True), (MANAGER_A, True),
                             (MEMBER_A, False), (MEMBER_A2, False)):
            with self.subTest(who=who):
                if allowed:
                    self.assertEqual(
                        api._owned_contact(who, self.cid, write=True)[
                            "contact_id"], self.cid)
                else:
                    with self.assertRaises(api.ApiError) as ctx:
                        api._owned_contact(who, self.cid, write=True)
                    # 403, not 404: they can SEE it, so 404 would be a lie.
                    self.assertEqual(ctx.exception.status, 403)

    def test_the_creator_still_cannot_edit_if_only_a_member(self):
        """Creating a shared contact does not grant ownership of it."""
        self.assertEqual(self.shared["created_by"], MEMBER_A)
        with self.assertRaises(api.ApiError) as ctx:
            api._owned_contact(MEMBER_A, self.cid, write=True)
        self.assertEqual(ctx.exception.status, 403)

    def test_non_member_gets_404(self):
        for who in (OUTSIDER, OWNER_B):
            with self.subTest(who=who):
                with self.assertRaises(api.ApiError) as ctx:
                    api._owned_contact(who, self.cid)
                self.assertEqual(ctx.exception.status, 404)

    def test_removed_member_loses_access(self):
        self.remove_from_org_a(MEMBER_A2)
        with self.assertRaises(api.ApiError) as ctx:
            api._owned_contact(MEMBER_A2, self.cid)
        self.assertEqual(ctx.exception.status, 404)

    def test_removed_creator_loses_access_too(self):
        row = self.make_contact(REMOVED, "Theirs", workspace_id=ORG_A)
        self.remove_from_org_a(REMOVED)
        with self.assertRaises(api.ApiError) as ctx:
            api._owned_contact(REMOVED, row["contact_id"])
        self.assertEqual(ctx.exception.status, 404)

    def test_update_route_enforces_the_write_rule(self):
        self.as_user(MEMBER_A2)
        with self.assertRaises(api.ApiError) as ctx:
            api.update_contact(event(
                method="PATCH", route="/contacts/{contact_id}",
                path={"contact_id": self.cid}, body={"company": "hijack"}))
        self.assertEqual(ctx.exception.status, 403)

        self.as_user(MANAGER_A)
        status, _ = parse(api.update_contact(event(
            method="PATCH", route="/contacts/{contact_id}",
            path={"contact_id": self.cid}, body={"company": "New Co"})))
        self.assertEqual(status, 200)

    def test_delete_route_enforces_the_write_rule(self):
        self.as_user(MEMBER_A)
        with self.assertRaises(api.ApiError) as ctx:
            api.delete_contact(event(
                method="DELETE", route="/contacts/{contact_id}",
                path={"contact_id": self.cid}))
        self.assertEqual(ctx.exception.status, 403)

        self.as_user(OWNER_A)
        self.assertEqual(parse(api.delete_contact(event(
            method="DELETE", route="/contacts/{contact_id}",
            path={"contact_id": self.cid})))[0], 200)

    def test_avatar_presign_matches_the_patch_it_enables(self):
        """Presigning for someone who cannot make the follow-up PATCH would
        hand out a usable upload URL for an edit that will be refused."""
        self.as_user(MEMBER_A2)
        # BUCKET_NAME and the format check run BEFORE the ownership test, so
        # both are satisfied here — otherwise this would assert a 500/400 and
        # prove nothing about authorization.
        with mock.patch.object(api, "BUCKET_NAME", "bkt"):
            with self.assertRaises(api.ApiError) as ctx:
                api.request_avatar_upload(event(
                    method="POST", route="/avatars/upload-request",
                    body={"scope": "contact", "contact_id": self.cid,
                          "format": "jpg"}))
            self.assertEqual(ctx.exception.status, 403)

            # A MANAGER is presigned, which is what makes the 403 above a
            # permission decision rather than a broken route.
            self.as_user(MANAGER_A)
            with mock.patch.object(api._s3, "generate_presigned_url",
                                   return_value="https://signed"):
                self.assertEqual(parse(api.request_avatar_upload(event(
                    method="POST", route="/avatars/upload-request",
                    body={"scope": "contact", "contact_id": self.cid,
                          "format": "jpg"})))[0], 200)


class TestPersonalContactsStayPrivate(Phase2CHarness):
    def test_a_personal_contact_is_invisible_to_colleagues(self):
        row = self.make_contact(MEMBER_A, "My Friend")
        cid = row["contact_id"]
        self.assertEqual(api._owned_contact(MEMBER_A, cid)["name"],
                         "My Friend")
        for who in (OWNER_A, MANAGER_A, MEMBER_A2, OUTSIDER):
            with self.subTest(who=who):
                with self.assertRaises(api.ApiError) as ctx:
                    api._owned_contact(who, cid)
                # 404 — a personal contact's existence is not disclosed.
                self.assertEqual(ctx.exception.status, 404)

    def test_the_owner_may_always_edit_their_personal_contact(self):
        row = self.make_contact(MEMBER_A, "Mine")
        self.assertEqual(
            api._owned_contact(MEMBER_A, row["contact_id"], write=True)[
                "name"], "Mine")

    def test_personal_contacts_do_not_require_membership(self):
        """The wrong fix would make personal data need an organisation."""
        self.remove_from_org_a(MEMBER_A)
        row = self.make_contact(MEMBER_A, "Still Mine")
        self.assertEqual(
            api._owned_contact(MEMBER_A, row["contact_id"], write=True)[
                "name"], "Still Mine")


class TestSharedContactListing(Phase2CHarness):
    def setUp(self):
        super().setUp()
        self.make_contact(MEMBER_A, "Shared One", workspace_id=ORG_A)
        self.make_contact(MANAGER_A, "Shared Two", workspace_id=ORG_A)
        self.make_contact(MEMBER_A, "My Private")            # personal
        self.make_contact(OWNER_B, "Their Client", workspace_id=ORG_B)

    def _names(self, resp):
        return {c["name"] for c in parse(resp)[1]["contacts"]}

    def test_members_see_the_shared_book_not_personal_rows(self):
        self.as_user(MEMBER_A2)
        got = self._names(api.list_contacts(event(workspace=ORG_A)))
        self.assertEqual(got, {"Shared One", "Shared Two"})

    def test_personal_listing_excludes_organisation_rows(self):
        self.as_user(MEMBER_A)
        got = self._names(api.list_contacts(event()))
        self.assertEqual(got, {"My Private"})

    def test_another_organisation_is_never_visible(self):
        self.as_user(OWNER_B)
        got = self._names(api.list_contacts(event(workspace=ORG_B)))
        self.assertEqual(got, {"Their Client"})

    def test_outsider_asking_for_a_workspace_gets_404(self):
        self.as_user(OUTSIDER)
        with self.assertRaises(api.ApiError) as ctx:
            api.list_contacts(event(workspace=ORG_A))
        self.assertEqual(ctx.exception.status, 404)

    def test_removed_member_cannot_list(self):
        self.remove_from_org_a(MEMBER_A2)
        self.as_user(MEMBER_A2)
        with self.assertRaises(api.ApiError) as ctx:
            api.list_contacts(event(workspace=ORG_A))
        self.assertEqual(ctx.exception.status, 404)


class TestWorkspaceScopedDedupe(Phase2CHarness):
    """The correctness bug Phase 2B recorded as a STOP.

    owner-email-index is partitioned by the OWNER, so it cannot answer "does
    this ORGANISATION already have this person". Without a workspace-scoped
    lookup two colleagues adding the same client each get a row and
    create_contact reports 201 "created" instead of 200 "existing".
    """

    def _create(self, who, body, workspace=None):
        self.as_user(who)
        return parse(api.create_contact(event(
            method="POST", route="/contacts", body=body,
            workspace=workspace)))

    def test_two_members_adding_the_same_client_converge(self):
        status, first = self._create(
            MEMBER_A, {"name": "John Smith", "email": "john@client.com"},
            workspace=ORG_A)
        self.assertEqual(status, 201)

        status, second = self._create(
            MANAGER_A, {"name": "John Smith", "email": "john@client.com"},
            workspace=ORG_A)
        self.assertEqual(status, 200)
        self.assertTrue(second.get("existing"))
        self.assertEqual(second["contact"]["id"], first["contact"]["id"])

        rows = [c for c in self.t["contacts"].items.values()
                if c.get("email") == "john@client.com"]
        self.assertEqual(len(rows), 1, "duplicate shared contact")

    def test_dedupe_by_phone_also_converges(self):
        self._create(MEMBER_A, {"name": "Sarah", "phone": "+919876543210"},
                     workspace=ORG_A)
        status, second = self._create(
            MEMBER_A2, {"name": "Sarah J", "phone": "+919876543210"},
            workspace=ORG_A)
        self.assertEqual(status, 200)
        self.assertTrue(second.get("existing"))

    def test_a_name_alone_never_merges(self):
        """Section 5: name/company only -> NEVER an automatic merge."""
        self._create(MEMBER_A, {"name": "Rahul Sharma"}, workspace=ORG_A)
        self.as_user(MEMBER_A2)
        with self.assertRaises(api.AmbiguousContact):
            api.create_contact(event(
                method="POST", route="/contacts",
                body={"name": "Rahul Sharma"}, workspace=ORG_A))

    def test_personal_and_organisation_namespaces_do_not_collide(self):
        """The same person may legitimately be in my private book AND the
        organisation's."""
        self._create(MEMBER_A, {"name": "Dual", "email": "dual@x.com"})
        status, _ = self._create(
            MEMBER_A, {"name": "Dual", "email": "dual@x.com"},
            workspace=ORG_A)
        self.assertEqual(status, 201)
        rows = [c for c in self.t["contacts"].items.values()
                if c.get("email") == "dual@x.com"]
        self.assertEqual(len(rows), 2)

    def test_another_organisation_is_not_consulted_for_dedupe(self):
        self._create(OWNER_B, {"name": "Cross", "email": "cross@x.com"},
                     workspace=ORG_B)
        status, _ = self._create(
            MEMBER_A, {"name": "Cross", "email": "cross@x.com"},
            workspace=ORG_A)
        self.assertEqual(status, 201)


class TestContactCreationScope(Phase2CHarness):
    def test_every_active_member_may_create(self):
        for who in (OWNER_A, MANAGER_A, MEMBER_A, MEMBER_A2):
            with self.subTest(who=who):
                self.as_user(who)
                status, body = parse(api.create_contact(event(
                    method="POST", route="/contacts",
                    body={"name": f"Client {who}",
                          "email": f"c-{who}@x.com"},
                    workspace=ORG_A)))
                self.assertEqual(status, 201)
                self.assertEqual(body["contact"]["workspace_id"], ORG_A)

    def test_outsider_cannot_create_in_a_workspace(self):
        self.as_user(OUTSIDER)
        with self.assertRaises(api.ApiError) as ctx:
            api.create_contact(event(
                method="POST", route="/contacts",
                body={"name": "Nope"}, workspace=ORG_A))
        self.assertEqual(ctx.exception.status, 404)

# ===========================================================================
# 2. THE ORGANISATION-CREATION IDENTITY GATE
# ===========================================================================
class TestOrganisationCreationIdentityGate(Phase2CHarness):
    """An identity that has USED its personal workspace cannot also own an
    organisation — the silent conversion the identity model forbids.

    Tested on personal RESOURCES rather than on the derived workspace row:
    every identity has one of those from signup, so the literal reading of
    the rule would reject 100% of organisation creation.
    """

    def _create_org(self, who, name="New Co"):
        self.as_user(who)
        return api.create_workspace(event(
            method="POST", route="/workspaces", body={"name": name}))

    def test_a_fresh_identity_may_create(self):
        status, body = parse(self._create_org("u-fresh"))
        self.assertEqual(status, 201)
        self.assertEqual(body["workspace"]["role"], ws.ROLE_OWNER)

    def test_an_identity_with_a_personal_recording_is_refused(self):
        self.t["recordings"].put_item(Item={
            "audio_s3_key": "recordings/u-has/mobile/a.m4a",
            "user_id": "u-has", "created_at": NOW})
        with self.assertRaises(api.PersonalWorkspaceInUse) as ctx:
            self._create_org("u-has")
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(ctx.exception.code, "personal_workspace_in_use")

    def test_nothing_is_written_when_creation_is_refused(self):
        self.t["contacts"].put_item(Item=api._contact_item("u-x", "C"))
        before = len(self.t["workspaces"].items)
        with self.assertRaises(api.PersonalWorkspaceInUse):
            self._create_org("u-x")
        self.assertEqual(len(self.t["workspaces"].items), before)
        self.assertIsNone(api._active_membership(
            ws.personal_workspace_id("u-x"), "u-x") and None)

    def test_an_existing_organisation_member_may_create_another(self):
        """They were an organisation identity already — nothing to convert."""
        self.t["contacts"].put_item(Item=api._contact_item(
            MEMBER_A, "Shared", workspace_id=ORG_A))
        self.assertEqual(parse(self._create_org(MEMBER_A, "Second Co"))[0],
                         201)

    def test_the_error_code_reaches_the_http_response(self):
        self.t["contacts"].put_item(Item=api._contact_item("u-http", "C"))
        api._ROUTES[("POST", "/__mkorg")] = api.create_workspace
        self.as_user("u-http")
        resp = api.lambda_handler({
            "routeKey": "POST /__mkorg", "headers": {},
            "body": json.dumps({"name": "Co"}),
            "requestContext": {"http": {"method": "POST",
                                        "path": "/__mkorg"}},
        }, None)
        del api._ROUTES[("POST", "/__mkorg")]
        self.assertEqual(resp["statusCode"], 409)
        self.assertEqual(json.loads(resp["body"])["code"],
                         "personal_workspace_in_use")

    def test_it_is_a_different_code_from_the_invitation_conflict(self):
        # The two need different screens: "wrong invited address" vs
        # "this account has personal data".
        self.assertNotEqual(api.PERSONAL_WORKSPACE_IN_USE_CODE,
                            api.ORG_EMAIL_CONFLICT_CODE)


# ===========================================================================
# 3. AI WORKSPACE CONTEXT
# ===========================================================================
class TestAIWorkspaceContext(Phase2CHarness):
    def _ctx(self, who, workspace=None):
        self.as_user(who)
        return api._ai_context(event(workspace=workspace))

    def test_personal_context_by_default(self):
        ctx = self._ctx(MEMBER_A)
        self.assertEqual(ctx.workspace_id, ws.personal_workspace_id(MEMBER_A))
        self.assertFalse(ctx.is_organisation)
        self.assertEqual(ctx.role, ws.ROLE_OWNER)

    def test_organisation_context_when_a_member_asks(self):
        ctx = self._ctx(MANAGER_A, workspace=ORG_A)
        self.assertEqual(ctx.workspace_id, ORG_A)
        self.assertTrue(ctx.is_organisation)
        self.assertEqual(ctx.role, ws.ROLE_MANAGER)
        self.assertEqual(ctx.workspace_name, "ABC Realty")

    def test_a_non_member_header_falls_back_to_personal(self):
        """An invalid hint must not break the assistant, and must not grant
        anything either."""
        ctx = self._ctx(OUTSIDER, workspace=ORG_A)
        self.assertEqual(ctx.workspace_id,
                         ws.personal_workspace_id(OUTSIDER))
        self.assertFalse(ctx.is_organisation)

    def test_a_removed_member_falls_back_to_personal(self):
        self.remove_from_org_a(REMOVED)
        ctx = self._ctx(REMOVED, workspace=ORG_A)
        self.assertFalse(ctx.is_organisation)

    def test_meetings_are_scoped_to_the_active_workspace(self):
        self.t["recordings"].put_item(Item={
            "audio_s3_key": "recordings/u-mea/mobile/org.m4a",
            "user_id": MEMBER_A, "workspace_id": ORG_A,
            "created_at": NOW, "recorded_at": NOW, "title": "Org"})
        self.t["recordings"].put_item(Item={
            "audio_s3_key": "recordings/u-mea/mobile/per.m4a",
            "user_id": MEMBER_A, "created_at": NOW,
            "recorded_at": NOW, "title": "Personal"})

        org = api._ai_recent_meetings(self._ctx(MEMBER_A, workspace=ORG_A))
        self.assertEqual({r["title"] for r in org}, {"Org"})

        personal = api._ai_recent_meetings(self._ctx(MEMBER_A))
        self.assertEqual({r["title"] for r in personal}, {"Personal"})

    def test_a_member_does_not_see_a_colleagues_meeting_via_ai(self):
        """Membership is not access — the same rule the REST list applies."""
        self.t["recordings"].put_item(Item={
            "audio_s3_key": "recordings/u-mea/mobile/theirs.m4a",
            "user_id": MEMBER_A, "workspace_id": ORG_A,
            "created_at": NOW, "recorded_at": NOW, "title": "Theirs"})
        got = api._ai_recent_meetings(self._ctx(MEMBER_A2, workspace=ORG_A))
        self.assertEqual(got, [])
        # But a MANAGER does.
        got = api._ai_recent_meetings(self._ctx(MANAGER_A, workspace=ORG_A))
        self.assertEqual({r["title"] for r in got}, {"Theirs"})

    def test_tasks_are_scoped_to_the_active_workspace(self):
        api._write_task(api._new_task_row(
            MEMBER_A, "Org task", recording_key="k1", workspace_id=ORG_A))
        api._write_task(api._new_task_row(
            MEMBER_A, "Personal task", recording_key="k2"))

        org = api._ai_owner_tasks(self._ctx(MEMBER_A, workspace=ORG_A))
        self.assertEqual({t["title"] for t in org}, {"Org task"})

        per = api._ai_owner_tasks(self._ctx(MEMBER_A))
        self.assertEqual({t["title"] for t in per}, {"Personal task"})

    def test_a_delegated_task_is_still_visible(self):
        """The regression the workspace filter caused once: a task created BY
        someone else and assigned to me carries THEIR owner_user_id, so
        resolving an unstamped row would hide work that is legitimately
        mine."""
        row = api._new_task_row(MANAGER_A, "Assigned to me",
                                recording_key="k3")
        row["assignee_user_id"] = MEMBER_A
        api._write_task(row)
        got = api._ai_owner_tasks(self._ctx(MEMBER_A))
        self.assertIn("Assigned to me", {t["title"] for t in got})

    def test_self_contact_resolves_in_the_active_workspace(self):
        self.t["contacts"].put_item(Item=api._contact_item(
            MEMBER_A, "Me at work", email=f"{MEMBER_A}@work.com",
            workspace_id=ORG_A))
        ctx = self._ctx(MEMBER_A, workspace=ORG_A)
        self.assertTrue(ctx.contact_id)

    def test_the_model_can_never_supply_a_workspace(self):
        """Identity and tenancy arrive from the JWT and the verified header,
        never from a tool argument."""
        for banned in ("workspace_id", "organization_id", "org_id",
                       "tenant_id", "user_id", "owner_user_id"):
            with self.subTest(arg=banned):
                self.assertIn(banned, api._AI_FORBIDDEN_ARGS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
