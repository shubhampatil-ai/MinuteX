#!/usr/bin/env python3
"""test_task_permissions.py — who may do what to a task.

THE MODEL. A task has TWO people with rights over it, and conflating them is
the bug this file exists to prevent:

  CREATOR   `owner_user_id`. The authenticated user for a manual task; the
            MEETING OWNER for an AI-seeded one. Controls the task's
            CONFIGURATION — deadline, details, assignee, AI resolution.

  ASSIGNEE  `assignee_user_id`. Only ever written from a Contact linked to a
            real MinuteX account. EXECUTES the task and may change exactly one
            field: status.

  ANYONE ELSE  nothing. Not even the knowledge that the task exists.

THE BUG THIS PINS. Every task route gated on `owner_user_id` alone, which is
the CREATOR. That is right for "tasks I made" and silently wrong for "tasks I
must do": User A creates a task and assigns it to User B, and B — the one
person who actually has to do the work — got 404 opening it and never saw it
in their dashboard. The task existed, was correctly assigned, notified B, and
then denied B access to it.

WHY THE DASHBOARD NEEDED AN INDEX, NOT A FILTER. Tasks are partitioned by
CREATOR, so B's task lives in A's partition. No amount of post-filtering finds
it without a full table scan — hence assignee-user-index, keyed on
`assignee_user_id`. The pre-existing `assignee-index` could not serve: it is
keyed on `assignee_contact_id`, an ADDRESS-BOOK row, which is not an identity
and cannot authenticate (TestContactIsNotAnIdentity).

SECURITY POSTURE. Section 13 of the requirement: the frontend is never the
enforcement point. Every test here calls the ROUTE FUNCTION DIRECTLY with a
forged identity, exactly as a curl against the deployed API would — no UI is
involved, so nothing here can be satisfied by hiding a button.

ERROR CODES. An unrelated caller gets 404 everywhere (never 403 — that would
confirm the id exists, matching _owned_task/_owned_recording). An ASSIGNEE
attempting a creator-only action gets 403, because they can already see the
task and a 404 would be a lie to a permitted viewer.

OFFLINE: fake_dynamodb, no AWS, no network.

Run:  python -m pytest tests/test_task_permissions.py
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

# ORDER MATTERS. Importing the workspace harness first installs the shared
# boto3 stubs and binds the SAME `api` module object every other suite uses —
# including the Key condition class the lambda holds a reference to. Importing
# fake_dynamodb first would give this file a DIFFERENT Key, and every indexed
# query would arrive at the fake table as a plain tuple.
from test_ai_workspace import api, call  # noqa: E402
import fake_dynamodb as fdb  # noqa: E402

# The three actors of the requirement's test matrix.
USER_A = "u-creator"      # creates the meeting and the task
USER_B = "u-assignee"     # the task is assigned to them
USER_C = "u-stranger"     # no relationship to the task at all

KEY = "recordings/u-creator/mobile/meeting_1754300000.m4a"

RECORDING = {
    "audio_s3_key": KEY,
    "user_id": USER_A,
    "title": "Vendor sync",
    "recorded_at": "2026-08-25T10:00:00Z",
    "created_at": "2026-08-25T10:00:00Z",
    "status": "done",
    "transcript": "Speaker 0: Rahul, prepare the quotation by Friday.",
    "speaker_names": {},
    "ai_tasks": [],
}


def parse(resp):
    return resp["statusCode"], json.loads(resp["body"])


def event(method="GET", route="/tasks", body=None, path=None, qs=None,
          key=None):
    ev = {
        "routeKey": f"{method} {route}",
        "requestContext": {"http": {"method": method, "path": route}},
        "headers": {"authorization": "Bearer test-token"},
        "pathParameters": dict(path or {}),
        "queryStringParameters": dict(qs or {}),
    }
    if key is not None:
        ev["pathParameters"]["key"] = key
    if body is not None:
        ev["body"] = json.dumps(body)
    return ev


class PermissionHarness(unittest.TestCase):
    """A real task: created by A, assigned to B (who has an account)."""

    def setUp(self):
        self.t = fdb.build_tables()
        # Identity is swapped per call by self.as_user(), which is the whole
        # point: these tests are about WHO is calling.
        self.current_user = USER_A
        self.patches = [
            mock.patch.object(api, "_recordings", self.t["recordings"]),
            mock.patch.object(api, "_contacts", self.t["contacts"]),
            mock.patch.object(api, "_meeting_participants",
                              self.t["participants"]),
            mock.patch.object(api, "_tasks", self.t["tasks"]),
            # The workspace harness binds a tuple-returning _Key; the fake
            # tables need fdb's composable one to serve indexed queries. Same
            # patch every fdb-backed suite here applies.
            mock.patch.object(api, "Key", fdb.Key),
            mock.patch.object(api, "_users", self.t["users"]),
            mock.patch.object(api, "_require_auth",
                              side_effect=lambda ev: self.current_user),
            mock.patch.object(api, "_owned_devices", return_value=[]),
            mock.patch.object(api.transcript_store, "hydrate",
                              side_effect=lambda s3, bucket, item: item),
            # Notifications are a separate concern with their own suite; this
            # file is about authorization, and a real _notify would need the
            # notifications table wired up for every case.
            mock.patch.object(api, "_notify", return_value=None),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

        self.t["recordings"].put_item(Item=json.loads(json.dumps(RECORDING)))
        # User B's MinuteX account — what makes them an assignee who can act,
        # rather than a name in an address book.
        self.t["users"].put_item(Item={"user_id": USER_B,
                                       "email": "rahul@company.com"})
        self.contact_b = self._make_contact_for_b()
        self.task = self._make_task_assigned_to_b()
        self.task_id = self.task["task_id"]

    def as_user(self, user_id):
        self.current_user = user_id

    def _make_contact_for_b(self):
        """A contact in A's address book, LINKED to B's MinuteX account."""
        cid = "c-rahul"
        self.t["contacts"].put_item(Item={
            "contact_id": cid,
            "owner_user_id": USER_A,
            "name": "Rahul Sharma",
            "email": "rahul@company.com",
            "email_lc": "rahul@company.com",
            "minutex_user_id": USER_B,
            "created_at": "2026-08-25T09:00:00Z",
        })
        return cid

    def _make_task_assigned_to_b(self):
        contact = self.t["contacts"].get_item(
            Key={"contact_id": self.contact_b})["Item"]
        row = api._new_task_row(
            USER_A, "Prepare the quotation",
            recording_key=KEY,
            due="Friday",
            source_type=api.TASK_SOURCE_AI,
            assignee_contact=contact,
            ai_confidence="high",
            ai_evidence="Rahul, prepare the quotation by Friday.",
        )
        row["ai_evidence_segment_ids"] = ["3", "4"]
        api._write_task(row)
        return row

    # -- convenience callers -------------------------------------------
    def get_task(self):
        return parse(call(api.get_task, event(
            "GET", "/tasks/{task_id}", path={"task_id": self.task_id})))

    def patch_task(self, body):
        return parse(call(api.update_task_v2, event(
            "PATCH", "/tasks/{task_id}", body=body,
            path={"task_id": self.task_id})))

    def resolve(self, contact_id):
        return parse(call(api.resolve_task_assignee, event(
            "POST", "/tasks/{task_id}/resolve",
            body={"contact_id": contact_id},
            path={"task_id": self.task_id})))

    def list_tasks(self, qs=None):
        return parse(call(api.list_all_tasks, event("GET", "/tasks", qs=qs or {})))


# ===========================================================================
# 1. THE MODEL — creator and assignee are different people
# ===========================================================================
class TestCreatorVsAssignee(PermissionHarness):

    def test_ai_task_creator_is_the_meeting_owner_not_the_assignee(self):
        """The requirement's worked example, as stored.

        "Rahul, prepare the quotation by Friday" in User A's meeting must
        store creator=A and assignee=B. Using the assignee as the creator
        would hand control of the task to the person meant to execute it.
        """
        self.assertEqual(self.task["owner_user_id"], USER_A)
        self.assertEqual(self.task["assignee_user_id"], USER_B)
        self.assertNotEqual(self.task["owner_user_id"],
                            self.task["assignee_user_id"])

    def test_seeded_ai_tasks_take_the_creator_from_the_recording(self):
        """_seed_ai_tasks is handed the MEETING OWNER, never the event.

        This is what makes AI tasks obey the same model as manual ones: the
        creator is whoever owns the meeting that produced the task.
        """
        self.t["recordings"].items[(KEY,)]["ai_tasks"] = [{
            "task": "Send the revised quote",
            "assignee": "Rahul Sharma",
            "due_date": "Friday",
            "confidence": "high",
        }]
        api._seed_ai_tasks(USER_A, KEY, self.t["recordings"].items[(KEY,)])
        rows = [r for r in self.t["tasks"].items.values()
                if r["title"] == "Send the revised quote"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["owner_user_id"], USER_A)

    def test_permission_helpers_agree_with_the_row(self):
        self.assertTrue(api._is_task_creator(USER_A, self.task))
        self.assertFalse(api._is_task_creator(USER_B, self.task))
        self.assertTrue(api._is_task_assignee(USER_B, self.task))
        self.assertFalse(api._is_task_assignee(USER_A, self.task))
        self.assertFalse(api._is_task_creator(USER_C, self.task))
        self.assertFalse(api._is_task_assignee(USER_C, self.task))


# ===========================================================================
# 2. VIEW — A yes, B yes, C denied (requirement sections 5 and 12)
# ===========================================================================
class TestViewPermission(PermissionHarness):

    def test_creator_can_view(self):
        self.as_user(USER_A)
        status, body = self.get_task()
        self.assertEqual(status, 200)
        self.assertEqual(body["task"]["id"], self.task_id)

    def test_assignee_can_view(self):
        """The load-bearing one: B could not open their own task before."""
        self.as_user(USER_B)
        status, body = self.get_task()
        self.assertEqual(status, 200)
        self.assertEqual(body["task"]["id"], self.task_id)

    def test_stranger_is_denied_and_told_nothing(self):
        """404, not 403: a 403 would confirm this task id exists."""
        self.as_user(USER_C)
        status, body = self.get_task()
        self.assertEqual(status, 404)
        self.assertNotIn("task", body)

    def test_permissions_block_tells_the_client_what_it_may_do(self):
        self.as_user(USER_A)
        _, body = self.get_task()
        self.assertEqual(body["permissions"], {
            "is_creator": True, "is_assignee": False, "can_view": True,
            "can_change_status": True, "can_edit_details": True,
            "can_change_deadline": True, "can_change_assignee": True,
            "can_resolve_assignment": True, "can_delete": True,
        })
        self.as_user(USER_B)
        _, body = self.get_task()
        self.assertEqual(body["permissions"], {
            "is_creator": False, "is_assignee": True, "can_view": True,
            "can_change_status": True, "can_edit_details": False,
            "can_change_deadline": False, "can_change_assignee": False,
            "can_resolve_assignment": False, "can_delete": False,
        })


# ===========================================================================
# 3. STATUS — the ONE field an assignee owns (sections 6 and 12)
# ===========================================================================
class TestStatusPermission(PermissionHarness):

    def test_creator_can_change_status(self):
        self.as_user(USER_A)
        status, body = self.patch_task({"status": "In Progress"})
        self.assertEqual(status, 200)
        self.assertEqual(body["task"]["status"], "In Progress")

    def test_assignee_can_change_status(self):
        self.as_user(USER_B)
        status, body = self.patch_task({"status": "In Progress"})
        self.assertEqual(status, 200)
        self.assertEqual(body["task"]["status"], "In Progress")

    def test_stranger_cannot_change_status(self):
        self.as_user(USER_C)
        status, _ = self.patch_task({"status": "In Progress"})
        self.assertEqual(status, 404)
        self.assertEqual(
            self.t["tasks"].items[(self.task_id,)]["status"], "Open")

    def test_assignee_walks_the_full_lifecycle(self):
        """Pending -> In Progress -> Completed -> In Progress (section 6)."""
        self.as_user(USER_B)
        for want in ("In Progress", "Completed", "In Progress"):
            status, body = self.patch_task({"status": want})
            self.assertEqual(status, 200)
            self.assertEqual(body["task"]["status"], want)

    def test_completion_stamps_and_clears_completed_at(self):
        self.as_user(USER_B)
        _, body = self.patch_task({"status": "Completed"})
        self.assertTrue(body["task"]["completed_at"])
        _, body = self.patch_task({"status": "In Progress"})
        self.assertFalse(body["task"]["completed_at"])

    def test_invalid_status_is_still_rejected_for_the_assignee(self):
        """The assignee path must not be a way around status validation."""
        self.as_user(USER_B)
        status, _ = self.patch_task({"status": "Teleported"})
        self.assertEqual(status, 400)


# ===========================================================================
# 4. CREATOR-ONLY FIELDS — B is denied every one (sections 12 and 13)
# ===========================================================================
class TestCreatorOnlyFields(PermissionHarness):

    def test_creator_can_change_deadline(self):
        self.as_user(USER_A)
        status, body = self.patch_task({"due": "2026-09-04"})
        self.assertEqual(status, 200)
        self.assertEqual(body["task"]["due"], "2026-09-04")

    def test_assignee_cannot_change_deadline(self):
        """403 — B can see the task, so 404 would be a lie."""
        self.as_user(USER_B)
        status, body = self.patch_task({"due": "2026-12-31"})
        self.assertEqual(status, 403)
        self.assertEqual(
            self.t["tasks"].items[(self.task_id,)]["due_date"], "Friday")
        self.assertIn("creator", json.dumps(body).lower())

    def test_assignee_cannot_edit_details(self):
        self.as_user(USER_B)
        for field, value in (("task", "Something else"),
                             ("title", "Something else"),
                             ("description", "rewritten"),
                             ("priority", "High")):
            status, _ = self.patch_task({field: value})
            self.assertEqual(status, 403, f"{field} must be creator-only")
        self.assertEqual(
            self.t["tasks"].items[(self.task_id,)]["title"],
            "Prepare the quotation")

    def test_assignee_cannot_change_the_assignee(self):
        """Otherwise B could hand their work to somebody else."""
        self.as_user(USER_B)
        status, _ = self.patch_task({"assignee_contact_id": "c-someone"})
        self.assertEqual(status, 403)
        status, _ = self.patch_task({"assignee": {"name": "Someone Else"}})
        self.assertEqual(status, 403)
        self.assertEqual(
            self.t["tasks"].items[(self.task_id,)]["assignee_user_id"], USER_B)

    def test_a_forbidden_field_smuggled_alongside_status_is_refused(self):
        """The whole patch is rejected, not partially applied.

        A client that sends {"status": ..., "due": ...} must not get the
        status change with the deadline silently dropped, nor the deadline
        applied because status was 'allowed'. Nothing is written.
        """
        self.as_user(USER_B)
        status, _ = self.patch_task({"status": "Completed",
                                     "due": "2026-12-31"})
        self.assertEqual(status, 403)
        row = self.t["tasks"].items[(self.task_id,)]
        self.assertEqual(row["status"], "Open")
        self.assertEqual(row["due_date"], "Friday")

    def test_stranger_gets_404_not_403_on_a_forbidden_field(self):
        """C must not learn the task exists by probing its fields."""
        self.as_user(USER_C)
        status, _ = self.patch_task({"due": "2026-12-31"})
        self.assertEqual(status, 404)


# ===========================================================================
# 5. AI ASSIGNMENT RESOLUTION — creator only (section 12)
# ===========================================================================
class TestResolvePermission(PermissionHarness):

    def setUp(self):
        super().setUp()
        # A second contact for the resolution to move the task to.
        self.t["contacts"].put_item(Item={
            "contact_id": "c-other", "owner_user_id": USER_A,
            "name": "Priya Nair", "email": "priya@company.com",
            "email_lc": "priya@company.com",
            "created_at": "2026-08-25T09:00:00Z",
        })

    def test_creator_can_resolve(self):
        self.as_user(USER_A)
        status, body = self.resolve("c-other")
        self.assertEqual(status, 200)
        self.assertEqual(body["task"]["assignee_name"], "Priya Nair")

    def test_assignee_cannot_resolve(self):
        self.as_user(USER_B)
        status, _ = self.resolve("c-other")
        self.assertEqual(status, 404)
        self.assertEqual(
            self.t["tasks"].items[(self.task_id,)]["assignee_user_id"], USER_B)

    def test_stranger_cannot_resolve(self):
        self.as_user(USER_C)
        status, _ = self.resolve("c-other")
        self.assertEqual(status, 404)

    def test_assignee_cannot_read_candidate_contacts(self):
        """The candidate list is the CREATOR's address book."""
        self.as_user(USER_B)
        status, _ = parse(call(api.suggest_task_assignees, event(
            "GET", "/tasks/{task_id}/assignee-candidates",
            path={"task_id": self.task_id})))
        self.assertEqual(status, 404)


# ===========================================================================
# 6. THE DASHBOARD — B must actually SEE their work (sections 8 and 15)
# ===========================================================================
class TestDashboardVisibility(PermissionHarness):

    def test_assignee_sees_the_task_in_my_tasks(self):
        self.as_user(USER_B)
        status, body = self.list_tasks({"assigned_to_me": "true"})
        self.assertEqual(status, 200)
        self.assertEqual([t["id"] for t in body["tasks"]], [self.task_id])

    def test_assignee_sees_the_task_in_the_unfiltered_list(self):
        """Not only under 'My tasks' — the default view must show it too."""
        self.as_user(USER_B)
        status, body = self.list_tasks()
        self.assertEqual(status, 200)
        self.assertIn(self.task_id, [t["id"] for t in body["tasks"]])

    def test_creator_still_sees_their_own_task(self):
        self.as_user(USER_A)
        _, body = self.list_tasks()
        self.assertIn(self.task_id, [t["id"] for t in body["tasks"]])

    def test_stranger_sees_nothing(self):
        self.as_user(USER_C)
        _, body = self.list_tasks()
        self.assertEqual(body["tasks"], [])
        _, body = self.list_tasks({"assigned_to_me": "true"})
        self.assertEqual(body["tasks"], [])

    def test_a_task_is_listed_once_when_you_created_AND_own_it(self):
        """Self-assignment puts a row in BOTH indexes; it must appear once."""
        self.t["contacts"].put_item(Item={
            "contact_id": "c-self", "owner_user_id": USER_A,
            "name": "Me", "email": "a@company.com",
            "email_lc": "a@company.com",
            "minutex_user_id": USER_A,
            "created_at": "2026-08-25T09:00:00Z",
        })
        contact = self.t["contacts"].get_item(
            Key={"contact_id": "c-self"})["Item"]
        row = api._new_task_row(USER_A, "My own task", recording_key=KEY,
                                assignee_contact=contact)
        api._write_task(row)
        self.as_user(USER_A)
        _, body = self.list_tasks()
        ids = [t["id"] for t in body["tasks"]]
        self.assertEqual(ids.count(row["task_id"]), 1)

    def test_paging_still_works_when_the_creator_has_many_tasks(self):
        """The union must not strand the creator's own paged tasks.

        REGRESSION. The merged page cannot emit a row-anchored cursor (its
        last row may come from the other index), so an early version
        suppressed the cursor on every unfiltered read — which silently cut
        pagination off after page one. The merge is now skipped once the
        creator's own rows already fill the page, so paging behaves exactly
        as it did before this feature.
        """
        for i in range(7):
            self.t["tasks"].put_item(Item={
                "task_id": f"pg-{i}", "owner_user_id": USER_A,
                "title": f"Paged {i}", "status": "Open", "priority": "Medium",
                "due_date": "", "created_at": f"2026-09-{i + 1:02d}T00:00:00Z"})
        self.as_user(USER_A)
        seen, cursor, pages = [], "", 0
        while pages < 12:
            qs = {"limit": "3"}
            if cursor:
                qs["cursor"] = cursor
            _, body = self.list_tasks(qs)
            seen += [t["id"] for t in body["tasks"]]
            cursor = body["next_cursor"]
            pages += 1
            if not cursor:
                break
        self.assertFalse(cursor, "paging did not terminate")
        for i in range(7):
            self.assertIn(f"pg-{i}", seen)
        self.assertEqual(len(seen), len(set(seen)), f"duplicates: {seen}")

    def test_unassigned_task_stays_out_of_the_assignee_index(self):
        """Sparse key: no assignee_user_id means not in the index at all."""
        row = api._new_task_row(USER_A, "Nobody's task", recording_key=KEY)
        api._write_task(row)
        self.assertNotIn("assignee_user_id", row)
        self.as_user(USER_B)
        _, body = self.list_tasks({"assigned_to_me": "true"})
        self.assertNotIn(row["task_id"], [t["id"] for t in body["tasks"]])


# ===========================================================================
# 6b. WHAT THE ASSIGNEE SEES — context, not the creator's records
# ===========================================================================
class TestAssigneeContext(PermissionHarness):
    """The task must not read as "Nobody is assigned" to its own assignee.

    THE BUG. The related entities on GET /tasks/{id} — contact,
    recording — are all resolved against the CALLER. They belong to the
    creator, so an assignee got none of them: the detail screen fell through
    to its "Nobody is assigned to this task" empty state and offered to assign
    someone, on a task that WAS assigned, to the very person reading it. The
    assignee also had no idea which meeting it came from.
    """

    def test_assignee_sees_themselves_as_the_assignee(self):
        self.as_user(USER_B)
        status, body = self.get_task()
        self.assertEqual(status, 200)
        self.assertEqual(body["contact"]["name"], "Rahul Sharma")
        # RESOLVED + a name is what the UI needs to render the assignee card
        # rather than its "nobody is assigned" branch.
        self.assertEqual(body["task"]["resolution_status"],
                         api.RESOLUTION_RESOLVED)
        self.assertTrue(body["task"]["assignee_name"])

    def test_the_assignee_card_carries_no_contact_id(self):
        """It addresses a contact in the CREATOR's address book.

        Sending the id would put a tappable row on screen that 404s, and it
        would leak an identifier for a record the assignee cannot read.
        """
        self.as_user(USER_B)
        _, body = self.get_task()
        self.assertEqual(body["contact"]["id"], "")

    def test_assignee_sees_which_meeting_it_came_from(self):
        """Provenance, so the task is accountable work not an anonymous ask."""
        self.as_user(USER_B)
        _, body = self.get_task()
        self.assertEqual(body["recording"]["title"], "Vendor sync")
        self.assertTrue(body["recording"]["recorded_at"])

    def test_but_not_a_route_into_the_creators_meeting(self):
        """The key is sent, and it opens the READ-ONLY notes — nothing more.

        This test used to assert audio_s3_key == "": the assignee was given no
        key at all, so the meeting row on the task screen could not be tapped.
        The key is now sent deliberately, because an assignee may open the
        meeting's NOTES at /recordings/shared-with-me/{key+} (see
        test_assignee_meeting_access.py for that route's own boundary tests).

        What has NOT changed is the thing this test exists to protect: holding
        the key must not open the CREATOR's meeting screen, with its audio,
        transcript and AI surfaces. `access` says which of the two the key is
        good for, and get_recording — the route backing the full screen — is
        asserted below to still refuse.
        """
        self.as_user(USER_B)
        _, body = self.get_task()
        self.assertEqual(body["recording"]["audio_s3_key"], KEY)
        self.assertEqual(body["recording"]["access"], "assignee")
        # Unchanged: empty speaker_names keeps _public_task_v2 on the stored
        # assignee string.
        self.assertEqual(body["recording"]["speaker_names"], {})
        self.assertNotIn("folder", body)

    def test_the_key_does_not_unlock_the_full_meeting_route(self):
        """The other half of the rule above, enforced where it matters.

        An assignee now holds a real recording key. get_recording is what
        backs the owner's meeting screen (presigned audio, transcript, every
        AI route), and it must keep 404ing for them — the widened access is
        the notes route alone.
        """
        self.as_user(USER_B)
        status, _ = parse(call(api.get_recording, event(
            "GET", "/recordings/{key+}", key=KEY)))
        self.assertEqual(status, 404)

    def test_assignee_is_told_who_assigned_it(self):
        self.t["users"].put_item(Item={"user_id": USER_A,
                                       "email": "anita@company.com",
                                       "name": "Anita Desai"})
        self.as_user(USER_B)
        _, body = self.get_task()
        self.assertEqual(body["assigned_by"]["name"], "Anita Desai")
        # Name and photo only: an email is contact detail, not provenance.
        self.assertNotIn("email", body["assigned_by"])

    def test_creator_is_not_told_they_assigned_their_own_task(self):
        self.as_user(USER_A)
        _, body = self.get_task()
        self.assertNotIn("assigned_by", body)

    def test_creator_still_gets_the_full_related_entities(self):
        """The creator's view is unchanged by any of the above."""
        self.as_user(USER_A)
        _, body = self.get_task()
        self.assertEqual(body["contact"]["id"], self.contact_b)
        self.assertEqual(body["recording"]["audio_s3_key"], KEY)

    def test_a_stranger_still_gets_nothing(self):
        self.as_user(USER_C)
        status, body = self.get_task()
        self.assertEqual(status, 404)
        for leaked in ("contact", "recording", "assigned_by", "task"):
            self.assertNotIn(leaked, body)


# ===========================================================================
# 7. CONTACTS WITHOUT ACCOUNTS (section 11)
# ===========================================================================
class TestContactIsNotAnIdentity(PermissionHarness):

    def test_contact_without_an_account_gets_no_assignee_user_id(self):
        """An external contact cannot act in MinuteX — there is no account.

        The task is still fully manageable BY ITS CREATOR, which is the point:
        assigning work to someone outside MinuteX must not strand the task.
        """
        self.t["contacts"].put_item(Item={
            "contact_id": "c-external", "owner_user_id": USER_A,
            "name": "External Vendor", "email": "vendor@elsewhere.com",
            "email_lc": "vendor@elsewhere.com",
            "created_at": "2026-08-25T09:00:00Z",
        })
        contact = self.t["contacts"].get_item(
            Key={"contact_id": "c-external"})["Item"]
        row = api._new_task_row(USER_A, "Chase the vendor",
                                recording_key=KEY, assignee_contact=contact)
        api._write_task(row)
        self.assertNotIn("assignee_user_id", row)
        self.assertEqual(row["resolution_status"], api.RESOLUTION_RESOLVED)
        # The creator retains full control.
        self.assertTrue(api._is_task_creator(USER_A, row))
        self.assertFalse(api._is_task_assignee(USER_B, row))

    def test_contact_id_is_never_an_authorization_input(self):
        """Holding a contact id must not grant access to a task.

        Contacts are per-owner address-book rows, so a contact id proves
        nothing about who is calling.
        """
        self.as_user(USER_C)
        status, _ = self.get_task()
        self.assertEqual(status, 404)


# ===========================================================================
# 8. AI PROVENANCE SURVIVES A STATUS CHANGE (section 16)
# ===========================================================================
class TestProvenancePreserved(PermissionHarness):

    def test_assignee_status_change_preserves_every_ai_field(self):
        """Moving a task along must not cost it its evidence.

        The assignee's write path is deliberately narrow (status +
        completed_at + updated_at); this proves it, field by field, rather
        than trusting the implementation to have stayed narrow.
        """
        before = dict(self.t["tasks"].items[(self.task_id,)])
        self.as_user(USER_B)
        status, _ = self.patch_task({"status": "Completed"})
        self.assertEqual(status, 200)
        after = self.t["tasks"].items[(self.task_id,)]
        for field in ("source_type", "ai_confidence", "ai_evidence",
                      "ai_evidence_segment_ids", "source_recording_id",
                      "assignee_speaker_id", "resolution_status",
                      "assignee_contact_id", "assignee_user_id",
                      "assignee_name", "due_date", "due_date_normalized",
                      "owner_user_id", "created_at", "title",
                      "priority", "fingerprint"):
            self.assertEqual(after.get(field), before.get(field),
                             f"{field} must survive a status change")

    def test_creator_deadline_change_preserves_ai_provenance(self):
        before = dict(self.t["tasks"].items[(self.task_id,)])
        self.as_user(USER_A)
        status, _ = self.patch_task({"due": "2026-09-04"})
        self.assertEqual(status, 200)
        after = self.t["tasks"].items[(self.task_id,)]
        for field in ("source_type", "ai_confidence", "ai_evidence",
                      "ai_evidence_segment_ids", "assignee_speaker_id",
                      "source_recording_id", "owner_user_id"):
            self.assertEqual(after.get(field), before.get(field),
                             f"{field} must survive a deadline change")


if __name__ == "__main__":
    unittest.main(verbosity=2)
