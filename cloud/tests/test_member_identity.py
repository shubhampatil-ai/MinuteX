#!/usr/bin/env python3
"""test_member_identity.py — human names, meeting attribution, per-user filter.

WHAT THIS FILE PINS.

  1. NO RAW UUID EVER REACHES A CLIENT AS A NAME. `name` was optional on every
     account created before the onboarding gate, so most Users rows carry
     name: "". A members list, a task assignee and a meeting's "recorded by"
     all need a human label, and with none the only thing left to render is
     the user_id — an identifier, not a name. workspace_schema.display_name is
     the single fallback chain (chosen -> derived from email -> email), and
     TestDisplayName pins every rung of it.

  2. THE DERIVED NAME IS NEVER WRITTEN BACK. It is a display fallback, so the
     stored row keeps its empty `name` and the moment the person sets a real
     one it wins with no migration. TestSuggestedNameIsAdvisory is the test
     that fails if that ever stops being true.

  3. THE GATE CAN TELL "NEVER CHOSE" FROM "CHOSE". GET /me returns the RAW
     stored name plus `name_set`, precisely so the onboarding gate does not
     mistake a derived name for a chosen one and let everyone through.

  4. MEETING ATTRIBUTION IS BATCHED. An organisation list resolves every
     recorder's profile in ONE BatchGetItem, not one read per row — the N+1
     the brief's performance section rules out.

  5. ?user_id= NARROWS, IT NEVER WIDENS. The filter is applied AFTER
     _can_read_meeting, so a MEMBER who passes a colleague's id gets the
     intersection with what they could already read — not a way to enumerate
     someone else's meetings. TestUserFilterCannotWiden is the security test
     here, and it is the reason the ordering in _list_workspace_recordings is
     not an implementation detail.

SECURITY POSTURE, same as test_workspace_phase2c: every test calls the ROUTE
FUNCTION DIRECTLY with a forged identity, exactly as curl against the deployed
API would. No UI is involved, so nothing here can be satisfied by hiding a
control.

OFFLINE: fake_dynamodb, no AWS, no network.

Run:  python -m pytest tests/test_member_identity.py
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

OWNER, MANAGER, MEMBER, MEMBER2 = "u-own", "u-mgr", "u-mem", "u-mem2"
OUTSIDER = "u-out"
ORG = "wso_ident01"
NOW = "2026-09-07T10:00:00Z"


def parse(resp):
    return resp["statusCode"], json.loads(resp["body"])


def event(method="GET", route="/recordings", path=None, body=None,
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


# ===========================================================================
# 1. THE FALLBACK CHAIN (pure — no AWS)
# ===========================================================================
class TestDisplayName(unittest.TestCase):
    """workspace_schema.display_name: chosen -> derived -> email -> "".

    Pure function, so this is the cheapest place to pin the whole contract.
    """

    def test_chosen_name_always_wins(self):
        self.assertEqual(
            ws.display_name({"name": "Priya Sharma",
                             "email": "someone.else@x.com"}),
            "Priya Sharma")

    def test_derives_a_name_from_a_dotted_local_part(self):
        self.assertEqual(
            ws.display_name({"email": "shubham.patil@exceller.tech"}),
            "Shubham Patil")

    def test_derives_across_every_separator(self):
        for local in ("shubham_patil", "shubham-patil", "shubham.patil"):
            with self.subTest(local=local):
                self.assertEqual(
                    ws.display_name({"email": f"{local}@x.com"}),
                    "Shubham Patil")

    def test_does_not_mangle_a_local_part_it_cannot_split(self):
        """A guess that makes things worse is not worth making.

        "jsmith2" capitalized is still not a name, so the email — which at
        least identifies the person — is the better answer.
        """
        for local in ("jsmith2", "kalyan", "x1.y2"):
            with self.subTest(local=local):
                self.assertEqual(
                    ws.display_name({"email": f"{local}@x.com"}),
                    f"{local}@x.com")

    def test_blank_name_falls_through_rather_than_returning_whitespace(self):
        self.assertEqual(
            ws.display_name({"name": "   ", "email": "a.b@x.com"}),
            "A B")

    def test_returns_the_fallback_only_when_asked(self):
        """No silent uuid.

        The fallback is OPT-IN so a caller chooses between an id and an empty
        cell, rather than having an id forced into a name field.
        """
        self.assertEqual(ws.display_name({}, fallback_user_id="u-1"), "u-1")
        self.assertEqual(ws.display_name({}), "")
        self.assertEqual(ws.display_name(None), "")

    def test_clamps_to_the_stored_limit(self):
        long_name = "Q" * (ws.MAX_NAME + 50)
        self.assertEqual(len(ws.display_name({"name": long_name})),
                         ws.MAX_NAME)


class TestPublicMemberNeverLeaksAnId(unittest.TestCase):
    """public_member's `name` is a DISPLAY name, never a raw uuid."""

    def test_nameless_user_gets_a_derived_name_not_an_id(self):
        out = ws.public_member(
            {"user_id": "3f9a1c72-dead-beef", "role": ws.ROLE_MEMBER},
            {"user_id": "3f9a1c72-dead-beef", "name": "",
             "email": "asha.rao@x.com"})
        self.assertEqual(out["name"], "Asha Rao")
        self.assertNotIn("3f9a1c72", out["name"])
        # …and the client is still told it was a guess.
        self.assertFalse(out["name_set"])

    def test_chosen_name_is_reported_as_chosen(self):
        out = ws.public_member(
            {"user_id": "u-1", "role": ws.ROLE_OWNER},
            {"user_id": "u-1", "name": "Asha Rao", "email": "a@x.com"})
        self.assertEqual(out["name"], "Asha Rao")
        self.assertTrue(out["name_set"])

    def test_no_profile_row_leaves_the_client_to_decide(self):
        """A failed profile read must not manufacture a name."""
        out = ws.public_member({"user_id": "u-1", "role": ws.ROLE_MEMBER})
        self.assertNotIn("name", out)

    def test_never_copies_password_material(self):
        out = ws.public_member(
            {"user_id": "u-1", "role": ws.ROLE_MEMBER},
            {"user_id": "u-1", "email": "a@x.com", "name": "A",
             "password_hash": "SECRET", "salt": "SECRET"})
        self.assertNotIn("password_hash", out)
        self.assertNotIn("salt", out)


# ===========================================================================
# 2. THROUGH THE ROUTES
# ===========================================================================
class IdentityHarness(unittest.TestCase):
    def setUp(self):
        self.t = fdb.build_tables()
        self.resource = fdb.FakeResource({
            "Users": self.t["users"],
            "Recordings": self.t["recordings"],
        })
        self.current_user = OWNER
        self.patches = [
            mock.patch.object(api, "_workspaces", self.t["workspaces"]),
            mock.patch.object(api, "_memberships", self.t["memberships"]),
            mock.patch.object(api, "_meeting_access", self.t["meeting_access"]),
            mock.patch.object(api, "_recordings", self.t["recordings"]),
            mock.patch.object(api, "_users", self.t["users"]),
            mock.patch.object(api, "_ddb", self.resource),
            mock.patch.object(api, "USERS_TABLE", "Users"),
            mock.patch.object(api, "Key", fdb.Key),
            mock.patch.object(api, "_require_auth",
                              side_effect=lambda ev: self.current_user),
            mock.patch.object(api, "_owned_devices", return_value=[]),
            # Presigning is a local signing op in prod; stubbed so a test
            # asserts on the SHAPE (a signed url was produced) not on S3.
            mock.patch.object(api, "_avatar_view_url",
                              side_effect=lambda k: f"signed:{k}" if k else ""),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

        self.t["workspaces"].put_item(Item={
            "workspace_id": ORG, "type": ws.TYPE_ORGANISATION,
            "name": "ABC Realty", "owner_user_id": OWNER,
            "status": ws.STATUS_ACTIVE, "created_at": NOW, "updated_at": NOW})
        for uid, role in ((OWNER, ws.ROLE_OWNER),
                          (MANAGER, ws.ROLE_MANAGER),
                          (MEMBER, ws.ROLE_MEMBER),
                          (MEMBER2, ws.ROLE_MEMBER)):
            self.t["memberships"].put_item(
                Item=ws.new_membership(ORG, uid, role, NOW))

        # DELIBERATELY MIXED profile state — the real table after years of an
        # optional `name`: one chosen, one derivable, one that resists a guess.
        self.t["users"].put_item(Item={
            "user_id": OWNER, "email": "meera.iyer@abc.com",
            "name": "Meera Iyer", "avatar_url": "avatars/u-own/p.jpg",
            "created_at": NOW})
        self.t["users"].put_item(Item={
            "user_id": MANAGER, "email": "shubham.patil@abc.com",
            "name": "", "avatar_url": "", "created_at": NOW})
        self.t["users"].put_item(Item={
            "user_id": MEMBER, "email": "jsmith2@abc.com",
            "name": "", "avatar_url": "", "created_at": NOW})
        self.t["users"].put_item(Item={
            "user_id": MEMBER2, "email": "ravi.kumar@abc.com",
            "name": "", "avatar_url": "", "created_at": NOW})

    def as_user(self, uid):
        self.current_user = uid

    def add_meeting(self, key, recorder, created_at=NOW, **extra):
        item = {
            "audio_s3_key": key, "user_id": recorder,
            "created_by": recorder, "workspace_id": ORG,
            "recording_status": "complete", "created_at": created_at,
            "title": f"Meeting {key}",
        }
        item.update(extra)
        self.t["recordings"].put_item(Item=item)
        return item


class TestMembersListShowsNames(IdentityHarness):
    """GET /workspaces/{id}/members — the screen that was showing uuids."""

    def members(self):
        status, body = parse(api.list_members(event(
            route="/workspaces/{workspace_id}/members",
            path={"workspace_id": ORG}, workspace=ORG)))
        self.assertEqual(status, 200)
        return {m["user_id"]: m for m in body["members"]}

    def test_no_member_is_rendered_as_a_uuid(self):
        """THE REGRESSION. Every row has something human in `name`."""
        for uid, m in self.members().items():
            with self.subTest(user=uid):
                self.assertTrue(m["name"])
                self.assertNotEqual(m["name"], uid)

    def test_each_row_gets_the_best_label_available(self):
        rows = self.members()
        self.assertEqual(rows[OWNER]["name"], "Meera Iyer")     # chosen
        self.assertEqual(rows[MANAGER]["name"], "Shubham Patil")  # derived
        self.assertEqual(rows[MEMBER]["name"], "jsmith2@abc.com")  # email

    def test_name_set_distinguishes_chosen_from_derived(self):
        rows = self.members()
        self.assertTrue(rows[OWNER]["name_set"])
        self.assertFalse(rows[MANAGER]["name_set"])

    def test_avatar_is_presigned_not_a_raw_key(self):
        """A raw S3 key renders nothing — the client needs a signed GET."""
        rows = self.members()
        self.assertEqual(rows[OWNER]["avatar_view_url"],
                         "signed:avatars/u-own/p.jpg")
        self.assertEqual(rows[MEMBER]["avatar_view_url"], "")

    def test_profiles_are_read_in_one_batch(self):
        """4 members, ONE BatchGetItem — not a read per row."""
        self.resource.batch_calls = 0
        self.members()
        self.assertEqual(self.resource.batch_calls, 1)

    def test_a_member_may_see_their_colleagues(self):
        self.as_user(MEMBER)
        self.assertTrue(self.members())

    def test_an_outsider_learns_nothing(self):
        self.as_user(OUTSIDER)
        with self.assertRaises(api.ApiError) as caught:
            api.list_members(event(
                route="/workspaces/{workspace_id}/members",
                path={"workspace_id": ORG}, workspace=ORG))
        self.assertEqual(caught.exception.status, 404)


class TestProfilePhotosAreMutuallyVisible(IdentityHarness):
    """Every MinuteX user's photo is visible to the people they work with.

    A photo is the one profile field whose whole purpose is being seen by
    OTHERS — a members list of identical grey glyphs is the failure this
    pins. The rule is deliberately narrow: the PHOTO crosses the account
    boundary, nothing else does.

    Presigning is what makes it work at all. The stored value is an S3 key and
    the bucket is private, so handing a client the key renders nothing; the
    API signs a short-lived GET on every read.
    """

    def members(self, as_user):
        self.as_user(as_user)
        status, body = parse(api.list_members(event(
            route="/workspaces/{workspace_id}/members",
            path={"workspace_id": ORG}, workspace=ORG)))
        self.assertEqual(status, 200)
        return {m["user_id"]: m for m in body["members"]}

    def test_every_member_sees_every_other_members_photo(self):
        """Not just the owner — a MEMBER sees their colleagues' faces too."""
        for viewer in (OWNER, MANAGER, MEMBER, MEMBER2):
            with self.subTest(viewer=viewer):
                rows = self.members(viewer)
                self.assertEqual(rows[OWNER]["avatar_view_url"],
                                 "signed:avatars/u-own/p.jpg")

    def test_the_photo_is_presigned_never_the_raw_key(self):
        """A raw S3 key against a private bucket renders nothing."""
        rows = self.members(MEMBER)
        self.assertTrue(rows[OWNER]["avatar_view_url"].startswith("signed:"))

    def test_a_member_with_no_photo_reports_empty_not_missing(self):
        """"" is the client's cue to draw initials — an ABSENT key would make
        it read `undefined` and render a broken image."""
        rows = self.members(OWNER)
        self.assertEqual(rows[MANAGER]["avatar_view_url"], "")

    def test_the_photo_travels_but_nothing_else_extra_does(self):
        """The cross-account read stays narrow.

        Sharing a face is the point; sharing account state is not. Only the
        display fields a members list needs may appear.
        """
        allowed = {"user_id", "role", "status", "joined_at", "created_at",
                   "invited_by", "email", "name", "name_set",
                   "avatar_url", "avatar_view_url"}
        for uid, m in self.members(MEMBER).items():
            with self.subTest(user=uid):
                self.assertEqual(set(m) - allowed, set())

    def test_an_outsider_gets_no_photos_at_all(self):
        """Mutual visibility is scoped to the WORKSPACE, not the internet."""
        self.as_user(OUTSIDER)
        with self.assertRaises(api.ApiError) as caught:
            api.list_members(event(
                route="/workspaces/{workspace_id}/members",
                path={"workspace_id": ORG}, workspace=ORG))
        self.assertEqual(caught.exception.status, 404)


class TestMeetingAttribution(IdentityHarness):
    """An owner/manager can see WHO recorded each meeting."""

    def recordings(self, qs=None):
        status, body = parse(api.list_recordings(
            event(workspace=ORG, qs=qs)))
        self.assertEqual(status, 200)
        return {r["audio_s3_key"]: r for r in body["recordings"]}

    def test_owner_sees_the_recorder_name_on_every_meeting(self):
        self.add_meeting("rec/a.m4a", MANAGER)
        self.add_meeting("rec/b.m4a", MEMBER2)
        rows = self.recordings()
        self.assertEqual(rows["rec/a.m4a"]["recorded_by_name"],
                         "Shubham Patil")
        self.assertEqual(rows["rec/b.m4a"]["recorded_by_name"], "Ravi Kumar")

    def test_recorder_is_never_a_bare_uuid(self):
        self.add_meeting("rec/a.m4a", MANAGER)
        row = self.recordings()["rec/a.m4a"]
        self.assertNotEqual(row["recorded_by_name"], MANAGER)

    def test_recorder_avatar_is_presigned(self):
        self.add_meeting("rec/a.m4a", OWNER)
        self.assertEqual(self.recordings()["rec/a.m4a"]["recorded_by_avatar"],
                         "signed:avatars/u-own/p.jpg")

    def test_attribution_is_one_batch_for_the_whole_page(self):
        """20 meetings must not cost 20 profile reads."""
        for i in range(20):
            self.add_meeting(f"rec/{i}.m4a", MANAGER,
                             created_at=f"2026-09-07T10:{i:02d}:00Z")
        self.resource.batch_calls = 0
        self.assertEqual(len(self.recordings()), 20)
        self.assertEqual(self.resource.batch_calls, 1)

    def test_falls_back_to_user_id_when_created_by_is_absent(self):
        """Rows predating the workspace stamp still attribute correctly."""
        self.t["recordings"].put_item(Item={
            "audio_s3_key": "rec/legacy.m4a", "user_id": MANAGER,
            "workspace_id": ORG, "recording_status": "complete",
            "created_at": NOW, "title": "Legacy"})
        self.assertEqual(
            self.recordings()["rec/legacy.m4a"]["recorded_by_name"],
            "Shubham Patil")

    def test_personal_workspace_gets_no_attribution_column(self):
        """Every row there is yours — the column would be your own name."""
        self.t["recordings"].put_item(Item={
            "audio_s3_key": "rec/mine.m4a", "user_id": OWNER,
            "recording_status": "complete", "created_at": NOW,
            "title": "Mine"})
        status, body = parse(api.list_recordings(event()))
        self.assertEqual(status, 200)
        row = body["recordings"][0]
        self.assertNotIn("recorded_by_name", row)


class TestPerUserMeetingFilter(IdentityHarness):
    """?user_id= — "show me only this person's meetings"."""

    def keys_for(self, uid=None):
        qs = {"user_id": uid} if uid else None
        status, body = parse(api.list_recordings(
            event(workspace=ORG, qs=qs)))
        self.assertEqual(status, 200)
        return {r["audio_s3_key"] for r in body["recordings"]}

    def setUp(self):
        super().setUp()
        self.add_meeting("rec/mgr-1.m4a", MANAGER, "2026-09-07T10:01:00Z")
        self.add_meeting("rec/mgr-2.m4a", MANAGER, "2026-09-07T10:02:00Z")
        self.add_meeting("rec/mem-1.m4a", MEMBER, "2026-09-07T10:03:00Z")
        self.add_meeting("rec/own-1.m4a", OWNER, "2026-09-07T10:04:00Z")

    def test_no_filter_returns_everything_the_caller_may_see(self):
        self.assertEqual(self.keys_for(), {
            "rec/mgr-1.m4a", "rec/mgr-2.m4a",
            "rec/mem-1.m4a", "rec/own-1.m4a"})

    def test_owner_can_narrow_to_one_person(self):
        self.assertEqual(self.keys_for(MANAGER),
                         {"rec/mgr-1.m4a", "rec/mgr-2.m4a"})

    def test_manager_can_narrow_too(self):
        self.as_user(MANAGER)
        self.assertEqual(self.keys_for(MEMBER), {"rec/mem-1.m4a"})

    def test_an_unknown_id_returns_nothing_rather_than_everything(self):
        """A filter that fails OPEN would be worse than no filter."""
        self.assertEqual(self.keys_for("u-nobody"), set())


class TestUserFilterCannotWiden(IdentityHarness):
    """THE SECURITY PROPERTY. ?user_id= narrows; it never grants.

    The filter is applied AFTER _can_read_meeting, so it can only ever remove
    rows from what the caller was already allowed to see. If that ordering
    were ever reversed, this class is what fails.
    """

    def setUp(self):
        super().setUp()
        self.add_meeting("rec/mgr-secret.m4a", MANAGER)
        self.add_meeting("rec/mem-own.m4a", MEMBER)

    def keys_as(self, uid, filter_uid=None):
        self.as_user(uid)
        qs = {"user_id": filter_uid} if filter_uid else None
        status, body = parse(api.list_recordings(
            event(workspace=ORG, qs=qs)))
        self.assertEqual(status, 200)
        return {r["audio_s3_key"] for r in body["recordings"]}

    def test_a_member_cannot_see_a_colleagues_meeting_unfiltered(self):
        """The baseline the filter must not be able to beat."""
        self.assertEqual(self.keys_as(MEMBER), {"rec/mem-own.m4a"})

    def test_filtering_by_a_colleague_yields_nothing_not_their_meetings(self):
        self.assertEqual(self.keys_as(MEMBER, MANAGER), set())

    def test_a_member_filtering_to_themselves_still_only_sees_their_own(self):
        self.assertEqual(self.keys_as(MEMBER, MEMBER), {"rec/mem-own.m4a"})

    def test_an_outsider_gets_404_filter_or_no_filter(self):
        for filter_uid in (None, MANAGER):
            with self.subTest(filter=filter_uid):
                self.as_user(OUTSIDER)
                qs = {"user_id": filter_uid} if filter_uid else None
                with self.assertRaises(api.ApiError) as caught:
                    api.list_recordings(event(workspace=ORG, qs=qs))
                self.assertEqual(caught.exception.status, 404)


class TestFilterIsAManagerControl(IdentityHarness):
    """WHO the filter is FOR — the capability the client gates its UI on.

    A MEMBER cannot read a colleague's meetings, so a person-filter would be
    a row of chips that all return nothing. The client therefore hides it
    unless `view_all_meetings` is set, and it reads that from the server's own
    capability map rather than re-deriving the role rule. These tests pin the
    map so that gating cannot silently drift from what the read check does.
    """

    def caps_for(self, uid):
        self.as_user(uid)
        status, body = parse(api.get_workspace(event(
            route="/workspaces/{workspace_id}",
            path={"workspace_id": ORG}, workspace=ORG)))
        self.assertEqual(status, 200)
        return body["workspace"]["capabilities"]

    def test_owner_and_manager_may_see_every_meeting(self):
        for uid in (OWNER, MANAGER):
            with self.subTest(user=uid):
                self.assertTrue(self.caps_for(uid)["view_all_meetings"])

    def test_a_member_may_not(self):
        self.assertFalse(self.caps_for(MEMBER)["view_all_meetings"])

    def test_the_capability_matches_what_the_read_check_enforces(self):
        """The UI gate and the authorization gate must not diverge.

        If CAP_VIEW_ALL_MEETINGS ever moved to a different minimum role, a
        client gating on the capability would follow automatically — this
        asserts the two really are the same rule.
        """
        for role in (ws.ROLE_OWNER, ws.ROLE_MANAGER, ws.ROLE_MEMBER):
            with self.subTest(role=role):
                self.assertEqual(
                    ws.capabilities_for(role)["view_all_meetings"],
                    ws.role_sees_all_meetings(role))


# ===========================================================================
# 3. THE ONBOARDING GATE'S INPUTS
# ===========================================================================
class TestMeTellsTheGateWhatItNeeds(IdentityHarness):
    """GET /me — `name` raw, `name_set` decisive, `suggested_name` advisory."""

    def me_as(self, uid):
        self.as_user(uid)
        status, body = parse(api.get_me(event(route="/me")))
        self.assertEqual(status, 200)
        return body["user"]

    def test_name_stays_raw_so_the_gate_can_tell_unset(self):
        """A DERIVED name here would make every account look complete."""
        user = self.me_as(MANAGER)
        self.assertEqual(user["name"], "")
        self.assertFalse(user["name_set"])

    def test_a_chosen_name_reports_as_set(self):
        user = self.me_as(OWNER)
        self.assertEqual(user["name"], "Meera Iyer")
        self.assertTrue(user["name_set"])

    def test_suggested_name_pre_fills_the_gate(self):
        self.assertEqual(self.me_as(MANAGER)["suggested_name"],
                         "Shubham Patil")

    def test_no_suggestion_once_a_name_exists(self):
        """Nothing to suggest — and offering one would invite overwriting."""
        self.assertEqual(self.me_as(OWNER)["suggested_name"], "")

    def test_never_exposes_password_material(self):
        self.t["users"].put_item(Item={
            "user_id": MEMBER, "email": "jsmith2@abc.com", "name": "",
            "password_hash": "SECRET", "salt": "SECRET", "created_at": NOW})
        user = self.me_as(MEMBER)
        self.assertNotIn("password_hash", user)
        self.assertNotIn("salt", user)


class TestSuggestedNameIsAdvisory(IdentityHarness):
    """The derived name is DISPLAY only — it is never written to the row.

    This is what makes the fallback safe to ship without a migration: the
    stored `name` stays empty, so the onboarding gate still fires and whatever
    the user actually types wins.
    """

    def test_listing_members_does_not_persist_a_derived_name(self):
        api.list_members(event(
            route="/workspaces/{workspace_id}/members",
            path={"workspace_id": ORG}, workspace=ORG))
        stored = self.t["users"].get_item(
            Key={"user_id": MANAGER})["Item"]
        self.assertEqual(stored.get("name"), "")

    def test_reading_me_does_not_persist_a_derived_name(self):
        self.as_user(MANAGER)
        api.get_me(event(route="/me"))
        stored = self.t["users"].get_item(
            Key={"user_id": MANAGER})["Item"]
        self.assertEqual(stored.get("name"), "")

    def test_a_saved_name_beats_the_derivation_afterwards(self):
        self.as_user(MANAGER)
        api.patch_me(event(method="PATCH", route="/me",
                           body={"name": "Shubh P"}))
        user = json.loads(api.get_me(event(route="/me"))["body"])["user"]
        self.assertEqual(user["name"], "Shubh P")
        self.assertTrue(user["name_set"])
        self.assertEqual(user["suggested_name"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
