#!/usr/bin/env python3
"""test_member_contacts.py — organisation members ARE contacts (Phase 2D).

THE REQUIREMENT THIS PINS: a colleague must be taggable in ANY meeting run by
ANY member of the organisation, without anybody typing them in, and must be
labelled with their role.

WHAT THE DESIGN IS, because the tests only make sense against it: members are
PROJECTED into the address book from live membership on the READ path, and a
real Contacts row is MATERIALIZED only when somebody is actually tagged (a
participant mapping or a task assignee, which store contact_id as a foreign
key). Nothing is written when a member joins.

So the properties worth pinning are:

  1. VISIBILITY. Every active colleague appears in the organisation's contact
     list, for every member, without a write having happened.
  2. THE ROLE TAG IS LIVE. A promotion shows immediately, and a REMOVED
     member disappears — because the tag is resolved from membership, never
     stored on the contact row.
  3. ONE ROW PER PERSON. A hand-added colleague and the projection converge
     on the stored row (which is the one participant/task rows reference),
     never two entries for one human.
  4. TAGGING MATERIALIZES, and is idempotent under a race.
  5. TENANCY. A member of one organisation is never projected into, nor
     resolvable from, another — and no GET may write a row.
  6. THE ENTRY IS NOT EDITABLE OR DELETABLE, because it mirrors the person's
     own profile and would be re-projected anyway.

SECURITY POSTURE, as in every suite here: route functions are called DIRECTLY
with a forged identity, exactly as curl against the deployed API would.
Nothing here can be satisfied by hiding a button.

OFFLINE: fake_dynamodb, no AWS, no network.

Run:  python -m unittest tests.test_member_contacts
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "functions/userapi"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# ORDER MATTERS — see test_task_permissions.py. Importing the Phase 2C suite
# first is what puts shared/ and functions/userapi/ on sys.path and binds the
# `api` module, so workspace_schema cannot be imported above it.
from test_workspace_phase2c import (  # noqa: E402
    ORG_A, ORG_B, OWNER_A, MANAGER_A, MEMBER_A, MEMBER_A2, OWNER_B, OUTSIDER,
    REMOVED, NOW, Phase2CHarness, api, event, parse,
)
import fake_dynamodb as fdb  # noqa: E402
import workspace_schema as ws  # noqa: E402


class MemberContactHarness(Phase2CHarness):
    """Phase 2C's fixture, plus the profile detail the projection needs.

    ORG_A holds OWNER_A / MANAGER_A / MEMBER_A / MEMBER_A2 as ACTIVE and
    REMOVED as a member row this class demotes, which is exactly the shape
    the role tag and the tenancy tests need.
    """

    def setUp(self):
        super().setUp()
        # The RESOURCE-level handle, for _users_by_ids' batch_get_item — the
        # profile read the projection is built on. Phase 2C's harness patches
        # the Table handles only, so without this every colleague resolves to
        # "no profile" and is correctly (but uselessly) skipped.
        self.ddb = fdb.FakeResource({t.name: t for t in self.t.values()})
        for p in (mock.patch.object(api, "_ddb", self.ddb),
                  mock.patch.object(api, "USERS_TABLE",
                                    self.t["users"].name)):
            p.start()
            self.addCleanup(p.stop)

        # Real display names, so a projected contact can be told apart from
        # the user_id fallback.
        self.names = {
            OWNER_A: "Asha Owner", MANAGER_A: "Manav Manager",
            MEMBER_A: "Meera Member", MEMBER_A2: "Mohit Member",
            OWNER_B: "Bhavna Owner", OUTSIDER: "Omkar Outsider",
            REMOVED: "Rohit Removed",
        }
        for uid, name in self.names.items():
            self.t["users"].put_item(Item={
                "user_id": uid, "email": f"{uid}@work.com",
                "name": name, "created_at": NOW})
        # REMOVED is a former member: the row survives for audit, the access
        # does not. Phase 2C's fixture creates them ACTIVE.
        self.t["memberships"].put_item(Item={
            **ws.new_membership(ORG_A, REMOVED, ws.ROLE_MEMBER, NOW),
            "status": ws.MEMBERSHIP_REMOVED})

    def list_contacts(self, uid, workspace=ORG_A, **qs):
        self.as_user(uid)
        status, body = parse(api.list_contacts(
            event(workspace=workspace, qs=qs or None)))
        self.assertEqual(status, 200, body)
        return body["contacts"]

    def by_name(self, contacts):
        return {c["name"]: c for c in contacts}

    def stored_ids(self):
        return {row["contact_id"]
                for row in self.t["contacts"].scan().get("Items", [])}


class TestColleaguesAppear(MemberContactHarness):
    def test_every_active_colleague_is_in_the_list(self):
        """The requirement, directly: no manual entry, everyone present."""
        names = self.by_name(self.list_contacts(MEMBER_A))
        self.assertIn("Asha Owner", names)
        self.assertIn("Manav Manager", names)
        self.assertIn("Mohit Member", names)

    def test_the_caller_is_not_listed_as_their_own_contact(self):
        """Self-tagging has its own path (allowSelf / _self_contact_id)."""
        names = self.by_name(self.list_contacts(MEMBER_A))
        self.assertNotIn("Meera Member", names)

    def test_a_removed_member_is_not_listed(self):
        names = self.by_name(self.list_contacts(MEMBER_A))
        self.assertNotIn("Rohit Removed", names)

    def test_listing_writes_nothing(self):
        """A projection is a READ. If listing wrote rows, every contacts
        fetch would grow the table and a removed member would be left behind
        as a stored row nobody can explain."""
        self.assertEqual(self.stored_ids(), set())
        self.list_contacts(MEMBER_A)
        self.assertEqual(self.stored_ids(), set())

    def test_every_member_sees_the_same_colleagues(self):
        """The point of "any meeting by any member": the directory cannot
        depend on who is asking."""
        seen = {}
        for uid in (OWNER_A, MANAGER_A, MEMBER_A, MEMBER_A2):
            seen[uid] = {c["name"] for c in self.list_contacts(uid)}
        for uid, names in seen.items():
            expected = {self.names[o] for o in
                        (OWNER_A, MANAGER_A, MEMBER_A, MEMBER_A2)
                        if o != uid}
            self.assertEqual(names, expected, uid)

    def test_a_personal_workspace_projects_nobody(self):
        """You are not a contact of yourself, and a personal workspace has no
        colleagues to find."""
        contacts = self.list_contacts(MEMBER_A, workspace=None)
        self.assertEqual(contacts, [])

    def test_a_colleague_is_notification_ready(self):
        """minutex_user_id is what makes a task notifiable. A projected
        member IS an account, so it must never be blank — otherwise the whole
        point (assign work to a colleague) silently degrades."""
        names = self.by_name(self.list_contacts(MEMBER_A))
        self.assertEqual(names["Asha Owner"]["minutex_user_id"], OWNER_A)

    def test_a_member_with_no_profile_is_skipped_not_projected_blank(self):
        """An unnameable, un-notifiable entry is worse than one absent
        colleague."""
        self.t["users"].delete_item(Key={"user_id": MEMBER_A2})
        names = self.by_name(self.list_contacts(MEMBER_A))
        self.assertNotIn("Mohit Member", names)
        self.assertIn("Asha Owner", names)


class TestRoleTag(MemberContactHarness):
    def test_each_colleague_carries_their_role(self):
        names = self.by_name(self.list_contacts(MEMBER_A))
        self.assertEqual(names["Asha Owner"]["workspace_role"], ws.ROLE_OWNER)
        self.assertEqual(names["Manav Manager"]["workspace_role"],
                         ws.ROLE_MANAGER)
        self.assertEqual(names["Mohit Member"]["workspace_role"],
                         ws.ROLE_MEMBER)

    def test_a_promotion_shows_immediately(self):
        """THE reason the tag is not stored on the contact row. A tag that
        lags a role change is worse than no tag."""
        self.t["memberships"].update_item(
            Key={"workspace_id": ORG_A, "user_id": MEMBER_A2},
            UpdateExpression="SET #r = :r",
            ExpressionAttributeNames={"#r": "role"},
            ExpressionAttributeValues={":r": ws.ROLE_MANAGER})
        names = self.by_name(self.list_contacts(MEMBER_A))
        self.assertEqual(names["Mohit Member"]["workspace_role"],
                         ws.ROLE_MANAGER)

    def test_a_hand_added_colleague_gets_the_tag_too(self):
        """The tag is keyed on the LINKED ACCOUNT, not on who typed the row
        in — which is exactly the colleague added by hand before this
        shipped."""
        self.make_contact(MEMBER_A, "Manav Manager", workspace_id=ORG_A,
                          email=f"{MANAGER_A}@work.com")
        rows = [c for c in self.list_contacts(MEMBER_A)
                if c["name"] == "Manav Manager"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["workspace_role"], ws.ROLE_MANAGER)

    def test_an_ordinary_client_carries_no_role(self):
        """"" means "not a colleague", never "role unknown"."""
        self.make_contact(MEMBER_A, "John Client", workspace_id=ORG_A,
                          email="john@client.com")
        names = self.by_name(self.list_contacts(MEMBER_A))
        self.assertEqual(names["John Client"]["workspace_role"], "")

    def test_a_personal_contact_carries_no_role(self):
        self.make_contact(MEMBER_A, "My Friend", email="friend@home.com")
        contacts = self.list_contacts(MEMBER_A, workspace=None)
        names = self.by_name(contacts)
        self.assertEqual(names["My Friend"]["workspace_role"], "")


class TestOneRowPerPerson(MemberContactHarness):
    def test_a_hand_added_colleague_is_not_duplicated(self):
        """The stored row wins: it carries the phone/company/notes somebody
        entered, and its id is what participant and task rows reference."""
        stored = self.make_contact(
            MEMBER_A, "Manav Manager", workspace_id=ORG_A,
            email=f"{MANAGER_A}@work.com", phone="+919876543210")
        rows = [c for c in self.list_contacts(MEMBER_A)
                if c["email"] == f"{MANAGER_A}@work.com"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], stored["contact_id"])
        # The entered detail survived — the projection did not shadow it.
        self.assertEqual(rows[0]["phone"], "+919876543210")

    def test_a_materialized_member_is_not_duplicated(self):
        """Once tagged, the row is in `stored` AND still projectable. It must
        appear once."""
        api._ensure_member_contact(MEMBER_A, api._member_contact_id(OWNER_A),
                                   workspace_id=ORG_A)
        rows = [c for c in self.list_contacts(MEMBER_A)
                if c["email"] == f"{OWNER_A}@work.com"]
        self.assertEqual(len(rows), 1)


class TestTaggingMaterializes(MemberContactHarness):
    def test_a_projected_member_becomes_a_real_row(self):
        cid = api._member_contact_id(OWNER_A)
        self.assertNotIn(cid, self.stored_ids())
        row = api._ensure_member_contact(MEMBER_A, cid, workspace_id=ORG_A)
        self.assertEqual(row["contact_id"], cid)
        self.assertIn(cid, self.stored_ids())
        self.assertEqual(row["workspace_id"], ORG_A)
        self.assertEqual(row["source"], api.CONTACT_SOURCE_MEMBER)
        # The email GSI key must be written, or dedupe cannot find this row
        # and the next hand-add would create a second one for one person.
        self.assertEqual(row["email_lc"], f"{OWNER_A}@work.com")

    def test_materializing_twice_yields_one_row(self):
        """Two colleagues tagging the same person at once. The id is derived
        and the write is conditional, so this is idempotent."""
        cid = api._member_contact_id(OWNER_A)
        first = api._ensure_member_contact(MEMBER_A, cid, workspace_id=ORG_A)
        second = api._ensure_member_contact(MEMBER_A2, cid,
                                            workspace_id=ORG_A)
        self.assertEqual(first["contact_id"], second["contact_id"])
        self.assertEqual(
            len([r for r in self.t["contacts"].scan().get("Items", [])
                 if r["contact_id"] == cid]), 1)

    def test_materializing_reuses_a_hand_added_row(self):
        """Never a second row for one person — the hand-added one is
        canonical because task rows may already point at it."""
        stored = self.make_contact(MEMBER_A, "Asha Owner", workspace_id=ORG_A,
                                   email=f"{OWNER_A}@work.com")
        row = api._ensure_member_contact(
            MEMBER_A, api._member_contact_id(OWNER_A), workspace_id=ORG_A)
        self.assertEqual(row["contact_id"], stored["contact_id"])

    def test_the_owner_of_the_entry_is_the_member_themselves(self):
        """Nobody else's user_id may sit in owner_user_id: _owned_contact
        would then treat a colleague's entry as that person's own PERSONAL
        contact and let them rewrite it."""
        row = api._ensure_member_contact(
            MEMBER_A, api._member_contact_id(OWNER_A), workspace_id=ORG_A)
        self.assertEqual(row["owner_user_id"], OWNER_A)

    def test_a_projected_member_resolves_for_reading_before_being_tagged(self):
        """_owned_contact must answer for a colleague the picker just listed,
        even though no row exists — otherwise tagging 404s."""
        self.as_user(MEMBER_A)
        row = api._owned_contact(MEMBER_A, api._member_contact_id(OWNER_A))
        self.assertEqual(row["name"], "Asha Owner")
        # Still a READ: resolving must not have written anything.
        self.assertEqual(self.stored_ids(), set())


class TestTenancy(MemberContactHarness):
    def test_another_organisations_member_is_not_projected(self):
        names = self.by_name(self.list_contacts(MEMBER_A))
        self.assertNotIn("Bhavna Owner", names)

    def test_another_organisations_member_cannot_be_materialized(self):
        """The strongest form: even naming the derived id directly."""
        row = api._ensure_member_contact(
            MEMBER_A, api._member_contact_id(OWNER_B), workspace_id=ORG_A)
        self.assertIsNone(row)
        self.assertEqual(self.stored_ids(), set())

    def test_an_outsider_cannot_materialize_into_an_organisation(self):
        row = api._ensure_member_contact(
            OUTSIDER, api._member_contact_id(OWNER_A), workspace_id=ORG_A)
        self.assertIsNone(row)
        self.assertEqual(self.stored_ids(), set())

    def test_a_removed_member_cannot_be_materialized(self):
        row = api._ensure_member_contact(
            MEMBER_A, api._member_contact_id(REMOVED), workspace_id=ORG_A)
        self.assertIsNone(row)

    def test_a_removed_member_cannot_materialize_a_colleague(self):
        row = api._ensure_member_contact(
            REMOVED, api._member_contact_id(OWNER_A), workspace_id=ORG_A)
        self.assertIsNone(row)

    def test_an_outsider_cannot_resolve_a_member_projection(self):
        """404, so an outsider does not learn who is in the organisation."""
        self.as_user(OUTSIDER)
        with self.assertRaises(api.ApiError) as ctx:
            api._owned_contact(OUTSIDER, api._member_contact_id(OWNER_A))
        self.assertEqual(ctx.exception.status, 404)

    def test_a_member_of_another_org_cannot_resolve_a_projection(self):
        self.as_user(OWNER_B)
        with self.assertRaises(api.ApiError) as ctx:
            api._owned_contact(OWNER_B, api._member_contact_id(OWNER_A))
        self.assertEqual(ctx.exception.status, 404)

    def test_a_removed_member_cannot_resolve_a_projection(self):
        """Removal has to stop access to the directory too."""
        self.as_user(REMOVED)
        with self.assertRaises(api.ApiError) as ctx:
            api._owned_contact(REMOVED, api._member_contact_id(OWNER_A))
        self.assertEqual(ctx.exception.status, 404)


class TestTheEntryIsNotEditable(MemberContactHarness):
    def setUp(self):
        super().setUp()
        self.cid = api._ensure_member_contact(
            MEMBER_A, api._member_contact_id(MEMBER_A2),
            workspace_id=ORG_A)["contact_id"]

    def test_a_manager_cannot_rename_a_colleagues_entry(self):
        """It mirrors their profile, so an edit would be overwritten by the
        projection and the name shown would depend on which row won."""
        self.as_user(MANAGER_A)
        with self.assertRaises(api.ApiError) as ctx:
            api.update_contact(event(
                method="PATCH", route="/contacts/{contact_id}",
                path={"contact_id": self.cid}, workspace=ORG_A,
                body={"name": "Renamed By Manager"}))
        self.assertEqual(ctx.exception.status, 409)
        # A stable code, so the app hides Edit rather than parsing English.
        self.assertEqual(getattr(ctx.exception, "code", ""),
                         "contact_is_member")

    def test_a_manager_cannot_delete_a_colleagues_entry(self):
        """Deleting would unassign their tasks and unlink every meeting they
        were tagged in — and the next read would project them straight back."""
        self.as_user(MANAGER_A)
        with self.assertRaises(api.ApiError) as ctx:
            api.delete_contact(event(
                method="DELETE", route="/contacts/{contact_id}",
                path={"contact_id": self.cid}, workspace=ORG_A))
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(getattr(ctx.exception, "code", ""),
                         "contact_is_member")
        # And nothing was deleted on the way to the refusal.
        self.assertIn(self.cid, self.stored_ids())

    def test_the_owner_cannot_delete_it_either(self):
        self.as_user(OWNER_A)
        with self.assertRaises(api.ApiError) as ctx:
            api.delete_contact(event(
                method="DELETE", route="/contacts/{contact_id}",
                path={"contact_id": self.cid}, workspace=ORG_A))
        self.assertEqual(ctx.exception.status, 409)

    def test_a_projected_entry_refuses_writes_before_it_exists(self):
        """403 not 404: the caller can SEE the colleague, so 404 would be a
        lie that makes the UI unexplainable."""
        with self.assertRaises(api.ApiError) as ctx:
            api._owned_contact(MEMBER_A, api._member_contact_id(OWNER_A),
                               write=True)
        self.assertEqual(ctx.exception.status, 403)

    def test_an_ordinary_shared_contact_is_still_editable(self):
        """The guard must not have frozen the normal shared address book."""
        row = self.make_contact(MEMBER_A, "John Client", workspace_id=ORG_A,
                                email="john@client.com")
        self.as_user(MANAGER_A)
        status, _ = parse(api.update_contact(event(
            method="PATCH", route="/contacts/{contact_id}",
            path={"contact_id": row["contact_id"]}, workspace=ORG_A,
            body={"company": "Client Co"})))
        self.assertEqual(status, 200)


class TestSearchAndPaging(MemberContactHarness):
    def test_search_matches_a_projected_colleague(self):
        contacts = self.list_contacts(MEMBER_A, search="manav")
        self.assertEqual([c["name"] for c in contacts], ["Manav Manager"])

    def test_search_excludes_colleagues_who_do_not_match(self):
        """An unmatched colleague on a search result looks like a bug."""
        names = {c["name"] for c in self.list_contacts(MEMBER_A,
                                                       search="manav")}
        self.assertNotIn("Asha Owner", names)

    def test_colleagues_are_not_repeated_on_page_two(self):
        """The projection is not in the index the cursor walks, so re-adding
        it per page would repeat the same people forever."""
        for i in range(4):
            self.make_contact(MEMBER_A, f"Client {i}", workspace_id=ORG_A,
                              email=f"client{i}@x.com")
        first = self.list_contacts(MEMBER_A, limit=3)
        self.as_user(MEMBER_A)
        status, body = parse(api.list_contacts(event(
            workspace=ORG_A, qs={"limit": 3})))
        self.assertEqual(status, 200)
        cursor = body["next_cursor"]
        self.assertTrue(cursor, "expected a second page")
        status, page2 = parse(api.list_contacts(event(
            workspace=ORG_A, qs={"limit": 3, "cursor": cursor})))
        self.assertEqual(status, 200)
        projected = {self.names[MANAGER_A], self.names[OWNER_A]}
        self.assertFalse(projected & {c["name"] for c in page2["contacts"]},
                         "colleagues repeated on page 2")
        # ...but the role tag still resolves on a later page, for a
        # hand-added colleague who happens to land there.
        self.assertTrue(all("workspace_role" in c
                            for c in page2["contacts"]))
        self.assertTrue(first)

    def test_colleagues_come_first(self):
        self.make_contact(MEMBER_A, "Aaa Client", workspace_id=ORG_A,
                          email="aaa@x.com")
        contacts = self.list_contacts(MEMBER_A)
        self.assertTrue(contacts[0]["workspace_role"],
                        "expected a colleague first")


class TestNoWriteOnRead(MemberContactHarness):
    def test_filtering_tasks_by_a_colleague_writes_nothing(self):
        """A GET must never populate the address book as a side effect."""
        self.as_user(MEMBER_A)
        cid = api._member_contact_id(OWNER_A)
        api._owned_contact(MEMBER_A, cid)
        self.assertEqual(self.stored_ids(), set())

    def test_get_contact_serves_a_projection_without_writing(self):
        self.as_user(MEMBER_A)
        status, body = parse(api.get_contact(event(
            route="/contacts/{contact_id}",
            path={"contact_id": api._member_contact_id(MANAGER_A)},
            workspace=ORG_A)))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["contact"]["name"], "Manav Manager")
        self.assertEqual(body["contact"]["workspace_role"], ws.ROLE_MANAGER)
        self.assertEqual(self.stored_ids(), set())


class TestMembershipFailureIsSurvivable(MemberContactHarness):
    def test_a_membership_read_failure_still_serves_stored_contacts(self):
        """Fail SOFT on the projection: the stored address book is still
        perfectly serveable, and emptying it over a colleague lookup would be
        a much worse outage than one missing section."""
        self.make_contact(MEMBER_A, "John Client", workspace_id=ORG_A,
                          email="john@client.com")
        boom = api.ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "boom"}},
            "Query")
        with mock.patch.object(api, "_query_all", side_effect=boom):
            contacts = self.list_contacts(MEMBER_A)
        self.assertIn("John Client", self.by_name(contacts))


if __name__ == "__main__":
    unittest.main()
