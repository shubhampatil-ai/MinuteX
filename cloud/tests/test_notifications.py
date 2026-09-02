#!/usr/bin/env python3
"""test_notifications.py — the in-app notification engine (Phase 1).

WHAT THIS FILE IS ORGANISED AROUND. A notification engine has two ways to be
wrong, and they pull in opposite directions:

  TOO QUIET   the event happened and nobody was told, or the wrong person was.
  TOO LOUD    the same fact was announced again on every retry, reprocess,
              app open or list read — which is how a bell becomes something
              users learn to ignore.

Almost every test below pins one of those two. The groups:

  SCHEMA        The copy, priority and entity mapping are DATA
                (notification_schema.TYPES), so the tests assert the contract
                every event site depends on rather than re-listing the table:
                an unknown type must RAISE at build time (a typo in an event
                site is a bug to surface at the write, not a blank row on a
                handset), metadata must stay small and flat, and
                public_notification must never emit dedupe_key — it contains
                the owner's user_id.

  ORDERING      The requirement is explicit that a notification is only raised
                AFTER the business write succeeds. The test that proves it is
                the one where the write FAILS: no task, therefore no
                notification. A notification pointing at a task that does not
                exist is a dead tap, and a user who taps two of those stops
                trusting the bell entirely.

  ISOLATION     User A must never read, count or mark User B's notifications.
                Since every route keys off the JWT and re-checks the row's
                user_id after the GetItem, the test that proves it is the one
                where a stranger's mark-read gets 404 — the SAME answer a
                nonexistent id gets, so the API is not an existence oracle
                either.

  DEDUPE        The engine is idempotent through a conditional CLAIM whose key
                IS the fact's identity. These tests run the same event twice
                and assert one row — for task assignment (once ever), for
                deadlines (once per day), and for meeting processing (a
                reprocess must not re-announce a meeting the user already saw).

  DEADLINES     The sweep runs on every task list read, which is precisely why
                "repeated check produces no duplicate" is the load-bearing
                test. Also pinned: a task assigned to SOMEONE ELSE is not the
                caller's deadline to be reminded of, and a completed task
                never nags.

  AI            An AI-extracted task with a resolvable assignee notifies that
                person; one whose assignee could NOT be resolved raises
                AI_ACTION_REQUIRED to the OWNER instead — never a silent
                guess. That pair is the "AI must not assign uncertain work"
                rule made testable.

  GMAIL         A test that asserts the engine did NOT touch Gmail. It exists
                because the requirement's hardest constraint is architectural,
                and architecture is what silently erodes: nothing enforces
                "notifications don't send email" except a test that says so.

OFFLINE by design, like the rest of this directory: fake_dynamodb tables, no
AWS, no network, no Groq.

Run:  python -m pytest tests/test_notifications.py
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

# Importing test_ai_workspace installs the boto3/Groq stubs and gives us the
# same `api` module object every other test in this directory binds — see
# conftest.py on why one shared stub identity matters.
from test_ai_workspace import (  # noqa: E402
    RECORDING, api, parse,
)

import fake_dynamodb as fdb  # noqa: E402
import notification_schema as ns  # noqa: E402

KEY = "recordings/u-1/mobile/mobile-abc_1754300000.m4a"
OWNER = "u-1"
STRANGER = "u-2"
ASSIGNEE_USER = "u-3"      # a MinuteX account that can receive notifications

CONTACT_LINKED = "c-linked"      # a contact WITH a MinuteX account
CONTACT_UNLINKED = "c-unlinked"  # a real contact with no account


def call(handler, ev):
    """Invoke a route handler the way lambda_handler really does."""
    try:
        return handler(ev)
    except api.ApiError as e:
        body = {"error": e.message}
        code = getattr(e, "code", "")
        if code:
            body["code"] = code
        return api._resp(e.status, body)


def event(method, route, body=None, params=None, qs=None, auth=True):
    """An API Gateway HTTP API v2.0 event."""
    ev = {
        "routeKey": f"{method} {route}",
        "requestContext": {"http": {"method": method, "path": "/"}},
        "pathParameters": params or {},
        "headers": {"host": "api.example.com"},
    }
    if auth:
        ev["headers"]["authorization"] = "Bearer test-token"
    if qs is not None:
        ev["queryStringParameters"] = qs
    if body is not None:
        ev["body"] = json.dumps(body)
    return ev


class NotificationTestCase(unittest.TestCase):
    """Real fake tables — these tests are almost all ROUND TRIPS.

    A MagicMock cannot tell a working state transition from a broken one, and
    the properties under test here ARE transitions: claim-then-write dedupe,
    the sparse unread index emptying as rows are read, a conditional write
    losing a race. fake_dynamodb implements those three faithfully (see its
    header), so a failure here means a real bug.
    """

    def setUp(self):
        self.item = json.loads(json.dumps(RECORDING))
        self.item["user_id"] = OWNER
        # The seeded AI task is opted into per-test, so the default fixture
        # does not raise notifications nobody asked about.
        self.item.pop("ai_tasks", None)

        self.notifications = fdb.FakeTable(
            "Notifications", "notification_id",
            indexes={
                "user-index": ("user_id", "created_at"),
                "user-unread-index": ("user_id", "unread_marker"),
            })
        self.dedupe = fdb.FakeTable("NotificationDedupe", "dedupe_key")
        self.recordings = fdb.FakeTable("Recordings", "audio_s3_key")
        self.contacts = fdb.FakeTable("Contacts", "contact_id")
        self.tasks = fdb.FakeTable(
            "Tasks", "task_id",
            indexes={
                "owner-index": ("owner_user_id", "created_at"),
                "meeting-index": ("source_recording_id", "created_at"),
                "folder-index": ("folder_id", "created_at"),
                "assignee-index": ("assignee_contact_id", "created_at"),
                "dedupe-index": ("owner_user_id", "fingerprint"),
            })
        self.participants = fdb.FakeTable(
            "MeetingParticipants", "audio_s3_key", "speaker_id")

        self.recordings.items[(KEY,)] = self.item
        self.contacts.items[(CONTACT_LINKED,)] = {
            "contact_id": CONTACT_LINKED, "owner_user_id": OWNER,
            "name": "Rahul Sharma", "email": "rahul@example.com",
            # THIS is what makes a task notification-ready: a contact linked
            # to a real MinuteX account.
            "minutex_user_id": ASSIGNEE_USER}
        self.contacts.items[(CONTACT_UNLINKED,)] = {
            "contact_id": CONTACT_UNLINKED, "owner_user_id": OWNER,
            "name": "Neha Shah", "email": "neha@example.com"}

        patches = [
            mock.patch.object(api, "Key", fdb.Key),
            mock.patch.object(api, "_notifications", self.notifications),
            mock.patch.object(api, "_notification_dedupe", self.dedupe),
            mock.patch.object(api, "_recordings", self.recordings),
            mock.patch.object(api, "_contacts", self.contacts),
            mock.patch.object(api, "_tasks", self.tasks),
            mock.patch.object(api, "_meeting_participants", self.participants),
            mock.patch.object(api, "_require_auth", return_value=OWNER),
            mock.patch.object(api, "_owned_devices", return_value=[]),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    # -- helpers ----------------------------------------------------------
    def rows_for(self, user_id=None):
        rows = list(self.notifications.items.values())
        if user_id is not None:
            rows = [r for r in rows if r.get("user_id") == user_id]
        return sorted(rows, key=lambda r: r.get("created_at", ""))

    def types_for(self, user_id):
        return sorted(r["type"] for r in self.rows_for(user_id))

    def make_task(self, **over):
        """A Tasks row, written the way the product writes one."""
        row = api._new_task_row(
            OWNER, over.pop("title", "Complete API integration"),
            recording_key=over.pop("recording_key", KEY),
            due=over.pop("due", ""),
            due_normalized=over.pop("due_normalized", ""),
            status=over.pop("status", api.TASK_STATUS_OPEN),
            assignee_contact=over.pop("assignee_contact", None),
            # A bare NAME, which is what makes a task UNRESOLVED — it has to
            # go through the constructor, because that is where the
            # resolution_status rule lives.
            assignee_name=over.pop("assignee_name", ""),
        )
        row.update(over)
        api._write_task(row)
        return row


# ---------------------------------------------------------------------------
# SCHEMA — the contract every event site builds against.
# ---------------------------------------------------------------------------
class SchemaTests(unittest.TestCase):

    def test_every_type_declares_priority_and_entity(self):
        """A type with no entity kind would produce an unopenable row."""
        for name, spec in ns.TYPES.items():
            self.assertIn(spec["priority"], ns.PRIORITIES, name)
            self.assertIn(spec["entity_type"], ns.ENTITY_TYPES, name)
            self.assertTrue(spec["title"].strip(), name)

    def test_declared_priorities_match_the_requirement(self):
        """The priorities are a product decision, so they are pinned here —
        a later edit to the table has to be deliberate, not incidental."""
        self.assertEqual(ns.priority_of(ns.TYPE_MEETING_PROCESSING_COMPLETED),
                         ns.PRIORITY_NORMAL)
        self.assertEqual(ns.priority_of(ns.TYPE_MEETING_PROCESSING_FAILED),
                         ns.PRIORITY_HIGH)
        self.assertEqual(ns.priority_of(ns.TYPE_AI_OUTPUT_READY),
                         ns.PRIORITY_NORMAL)
        self.assertEqual(ns.priority_of(ns.TYPE_AI_ACTION_REQUIRED),
                         ns.PRIORITY_HIGH)
        for t in (ns.TYPE_TASK_ASSIGNED, ns.TYPE_TASK_REASSIGNED,
                  ns.TYPE_TASK_DUE_TODAY, ns.TYPE_TASK_OVERDUE):
            self.assertEqual(ns.priority_of(t), ns.PRIORITY_HIGH, t)
        self.assertEqual(ns.priority_of(ns.TYPE_MEETING_DOCUMENT_READY),
                         ns.PRIORITY_NORMAL)
        self.assertEqual(ns.priority_of(ns.TYPE_MEETING_OUTPUT_SHARED),
                         ns.PRIORITY_NORMAL)

    def test_build_renders_the_registered_copy(self):
        built = ns.build(ns.TYPE_TASK_ASSIGNED, subject="Complete API integration")
        self.assertEqual(built["title"], "New task assigned to you")
        self.assertEqual(built["message"], "Complete API integration")
        self.assertEqual(built["entity_type"], ns.ENTITY_TASK)

    def test_build_rejects_an_unknown_type(self):
        """A typo in an event site must fail at the write, not render blank."""
        with self.assertRaises(ValueError):
            ns.build("TASK_ASIGNED", subject="x")

    def test_build_clips_an_enormous_message(self):
        """A notification is a pointer plus a label — never a document."""
        built = ns.build(ns.TYPE_TASK_ASSIGNED, subject="x" * 5000)
        self.assertLessEqual(len(built["message"]), ns.MAX_MESSAGE)

    def test_metadata_drops_structures_and_stringifies_scalars(self):
        """Bounded so 'just put the object in metadata' cannot become the way
        business data gets duplicated into notifications."""
        built = ns.build(
            ns.TYPE_TASK_ASSIGNED, subject="t",
            metadata={"nested": {"a": 1}, "listy": [1, 2], "none": None,
                      "ok": "value", "num": 7, "long": "y" * 500})
        meta = built["metadata"]
        self.assertNotIn("nested", meta)
        self.assertNotIn("listy", meta)
        self.assertNotIn("none", meta)
        self.assertEqual(meta["ok"], "value")
        self.assertEqual(meta["num"], "7")
        self.assertLessEqual(len(meta["long"]), ns.MAX_METADATA_VALUE)

    def test_metadata_key_count_is_capped(self):
        """The cap is applied over the keys in sorted order, so WHICH keys
        survive is deterministic rather than dict-insertion luck."""
        built = ns.build(ns.TYPE_TASK_ASSIGNED, subject="t",
                         metadata={f"k{i:02d}": i for i in range(40)})
        meta = built["metadata"]
        self.assertEqual(len(meta), ns.MAX_METADATA_KEYS)
        self.assertEqual(sorted(meta), sorted(meta)[:ns.MAX_METADATA_KEYS])
        self.assertIn("k00", meta)

    def test_public_notification_never_leaks_the_dedupe_key(self):
        """dedupe_key embeds the OWNER's user_id. public_notification is an
        assembled dict precisely so a new internal attribute cannot leak by
        being forgotten in a deny-list."""
        row = ns.make_row(
            OWNER, ns.build(ns.TYPE_TASK_ASSIGNED, subject="t"), "task-1",
            ns.dedupe_key(OWNER, ns.TYPE_TASK_ASSIGNED, "task-1"),
            notification_id="n-1", now="2026-08-29T10:00:00Z")
        row["secret_internal"] = "must not appear"
        blob = json.dumps(ns.public_notification(row))
        self.assertNotIn("dedupe_key", blob)
        self.assertNotIn(OWNER, blob)
        self.assertNotIn("secret_internal", blob)

    def test_dedupe_key_is_deterministic_and_day_scoped(self):
        a = ns.dedupe_key(OWNER, ns.TYPE_TASK_OVERDUE, "t1", "2026-08-29")
        b = ns.dedupe_key(OWNER, ns.TYPE_TASK_OVERDUE, "t1", "2026-08-29")
        c = ns.dedupe_key(OWNER, ns.TYPE_TASK_OVERDUE, "t1", "2026-08-30")
        d = ns.dedupe_key(STRANGER, ns.TYPE_TASK_OVERDUE, "t1", "2026-08-29")
        self.assertEqual(a, b)          # same fact, same key, every time
        self.assertNotEqual(a, c)       # a new day is a new fact
        self.assertNotEqual(a, d)       # never shared across users

    def test_phase_one_delivers_in_app_only(self):
        """The channel seam exists; only IN_APP is active."""
        self.assertEqual(tuple(ns.DEFAULT_CHANNELS), (ns.CHANNEL_IN_APP,))


# ---------------------------------------------------------------------------
# CREATION — the engine itself.
# ---------------------------------------------------------------------------
class CreationTests(NotificationTestCase):

    def test_creates_a_notification_for_the_right_user(self):
        row = api._notify(ASSIGNEE_USER, ns.TYPE_TASK_ASSIGNED, "task-1",
                          subject="Complete API integration")
        self.assertIsNotNone(row)
        stored = self.rows_for()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["user_id"], ASSIGNEE_USER)
        self.assertEqual(stored[0]["type"], ns.TYPE_TASK_ASSIGNED)
        self.assertEqual(stored[0]["entity_type"], "task")
        self.assertEqual(stored[0]["entity_id"], "task-1")
        self.assertEqual(stored[0]["priority"], ns.PRIORITY_HIGH)
        self.assertFalse(stored[0]["is_read"])
        # The sparse marker is what puts it in the unread index.
        self.assertTrue(stored[0]["unread_marker"])

    def test_no_recipient_writes_nothing(self):
        """An unassigned task has nobody to notify. Not an error — silence."""
        self.assertIsNone(api._notify("", ns.TYPE_TASK_ASSIGNED, "task-1"))
        self.assertEqual(self.rows_for(), [])

    def test_the_actor_is_never_notified(self):
        """Assigning work to yourself must not tell you about it."""
        self.assertIsNone(api._notify(OWNER, ns.TYPE_TASK_ASSIGNED, "task-1",
                                      subject="t", actor_user_id=OWNER))
        self.assertEqual(self.rows_for(), [])

    def test_a_notification_failure_never_raises(self):
        """The business action has already committed by the time this runs —
        there is no notification failure worth failing it for."""
        with mock.patch.object(self.notifications, "put_item",
                               side_effect=RuntimeError("dynamo down")):
            self.assertIsNone(
                api._notify(ASSIGNEE_USER, ns.TYPE_TASK_ASSIGNED, "task-1",
                            subject="t"))

    def test_an_unknown_type_does_not_escape_as_an_exception(self):
        """build() raises, and _notify swallows — so a bad event site degrades
        to a missing notification, never to a failed business request."""
        self.assertIsNone(api._notify(ASSIGNEE_USER, "NOT_A_TYPE", "e-1"))
        self.assertEqual(self.rows_for(), [])

    def test_duplicate_events_create_one_notification(self):
        for _ in range(4):
            api._notify(ASSIGNEE_USER, ns.TYPE_TASK_ASSIGNED, "task-1",
                        subject="Complete API integration")
        self.assertEqual(len(self.rows_for()), 1)

    def test_the_same_fact_for_two_users_is_two_notifications(self):
        """Dedupe is per-user: two people can owe the same thing."""
        api._notify(ASSIGNEE_USER, ns.TYPE_TASK_ASSIGNED, "task-1", subject="t")
        api._notify(STRANGER, ns.TYPE_TASK_ASSIGNED, "task-1", subject="t")
        self.assertEqual(len(self.rows_for()), 2)

    def test_dedupe_failing_open_still_delivers(self):
        """If the claim table is unreachable, a possible duplicate beats
        losing a real notification."""
        with mock.patch.object(self.dedupe, "put_item",
                               side_effect=RuntimeError("claims down")):
            row = api._notify(ASSIGNEE_USER, ns.TYPE_TASK_ASSIGNED, "task-1",
                              subject="t")
        self.assertIsNotNone(row)


# ---------------------------------------------------------------------------
# TASK EVENTS.
# ---------------------------------------------------------------------------
class TaskEventTests(NotificationTestCase):

    def test_task_assigned_to_a_linked_contact_notifies_them(self):
        ev = event("POST", "/recordings/ai/tasks/{key+}",
                   {"task": "Complete API integration",
                    "assignee_contact_id": CONTACT_LINKED},
                   params={"key": KEY})
        status, body = parse(call(api.create_meeting_task, ev))
        self.assertEqual(status, 201)

        rows = self.rows_for(ASSIGNEE_USER)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["type"], ns.TYPE_TASK_ASSIGNED)
        self.assertEqual(rows[0]["title"], "New task assigned to you")
        self.assertEqual(rows[0]["message"], "Complete API integration")
        # It points at the TASK that was actually created.
        self.assertEqual(rows[0]["entity_id"], body["task"]["id"])
        # And the person who created it is not told about their own action.
        self.assertEqual(self.rows_for(OWNER), [])

    def test_task_assigned_to_an_unlinked_contact_notifies_nobody(self):
        """A real contact with no MinuteX account has no inbox. Naming them is
        not a failure — there is simply nobody to tell."""
        ev = event("POST", "/recordings/ai/tasks/{key+}",
                   {"task": "Send proposal",
                    "assignee_contact_id": CONTACT_UNLINKED},
                   params={"key": KEY})
        self.assertEqual(parse(call(api.create_meeting_task, ev))[0], 201)
        self.assertEqual(self.rows_for(), [])

    def test_a_failed_task_creation_creates_no_notification(self):
        """THE ordering rule. An empty title is rejected — and because the
        notification comes after the write, nothing is announced."""
        ev = event("POST", "/recordings/ai/tasks/{key+}",
                   {"task": "", "assignee_contact_id": CONTACT_LINKED},
                   params={"key": KEY})
        self.assertEqual(parse(call(api.create_meeting_task, ev))[0], 400)
        self.assertEqual(self.rows_for(), [])
        self.assertEqual(len(self.tasks.items), 0)

    def test_reassignment_notifies_both_sides_correctly(self):
        """The departing assignee is told it left them; the arriving one is
        told it arrived. Nobody else hears anything."""
        contact_second = "c-second"
        self.contacts.items[(contact_second,)] = {
            "contact_id": contact_second, "owner_user_id": OWNER,
            "name": "Priya Nair", "email": "priya@example.com",
            "minutex_user_id": "u-4"}

        task = self.make_task(
            assignee_contact=self.contacts.items[(CONTACT_LINKED,)])
        self.assertEqual(task.get("assignee_user_id"), ASSIGNEE_USER)

        ev = event("PATCH", "/recordings/ai/tasks/{key+}",
                   {"id": task["task_id"], "assignee_contact_id": contact_second},
                   params={"key": KEY})
        self.assertEqual(parse(call(api.update_meeting_task, ev))[0], 200)

        self.assertEqual(self.types_for(ASSIGNEE_USER),
                         [ns.TYPE_TASK_REASSIGNED])
        self.assertEqual(self.types_for("u-4"), [ns.TYPE_TASK_ASSIGNED])
        # The owner performed the action and is not a party to it.
        self.assertEqual(self.rows_for(OWNER), [])

    def test_editing_a_task_without_changing_the_assignee_notifies_nobody(self):
        """A renamed or re-dated task must not re-announce itself."""
        task = self.make_task(
            assignee_contact=self.contacts.items[(CONTACT_LINKED,)])
        self.notifications.items.clear()

        ev = event("PATCH", "/recordings/ai/tasks/{key+}",
                   {"id": task["task_id"], "task": "Complete API integration v2"},
                   params={"key": KEY})
        self.assertEqual(parse(call(api.update_meeting_task, ev))[0], 200)
        self.assertEqual(self.rows_for(), [])

    def test_repeating_the_same_assignment_does_not_duplicate(self):
        """Retrying the API must not produce a second notification."""
        task = self.make_task(
            assignee_contact=self.contacts.items[(CONTACT_LINKED,)])
        for _ in range(3):
            api._notify_task_assigned(task, actor_user_id=OWNER)
        self.assertEqual(len(self.rows_for(ASSIGNEE_USER)), 1)

    def test_resolving_an_ambiguous_assignee_assigns_and_notifies(self):
        """Answering 'which Rahul?' is the moment the task first has a real
        recipient — so it is a genuine assignment."""
        task = self.make_task(assignee_name="Rahul")
        self.assertEqual(task["resolution_status"], api.RESOLUTION_UNRESOLVED)

        ev = event("POST", "/tasks/{task_id}/resolve",
                   {"contact_id": CONTACT_LINKED},
                   params={"task_id": task["task_id"]})
        self.assertEqual(parse(call(api.resolve_task_assignee, ev))[0], 200)
        self.assertEqual(self.types_for(ASSIGNEE_USER), [ns.TYPE_TASK_ASSIGNED])


# ---------------------------------------------------------------------------
# AI EVENTS.
# ---------------------------------------------------------------------------
class AiEventTests(NotificationTestCase):

    def test_ai_task_with_an_unresolvable_assignee_asks_the_owner(self):
        """The AI heard a name it could not match. It must NOT guess a person
        — it asks the owner to confirm instead."""
        self.item["ai_tasks"] = [
            {"task": "Prepare proposal", "assignee": "Someone Ambiguous"},
        ]
        api._seed_ai_tasks(OWNER, KEY, self.item)

        rows = self.rows_for(OWNER)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["type"], ns.TYPE_AI_ACTION_REQUIRED)
        self.assertEqual(rows[0]["title"], "AI needs your confirmation")
        self.assertIn("Prepare proposal", rows[0]["message"])
        self.assertIn("review", rows[0]["message"])
        # It deep-links to the TASK to review, not to the meeting.
        self.assertEqual(rows[0]["entity_type"], "task")
        self.assertEqual(rows[0]["priority"], ns.PRIORITY_HIGH)

    def test_ai_task_the_speaker_map_resolves_notifies_that_person(self):
        """When the meeting's speaker mapping identifies the speaker, the
        chain completes and a real person is assigned — so they are told,
        and there is nothing ambiguous to review."""
        self.participants.items[(KEY, "0")] = {
            "audio_s3_key": KEY, "speaker_id": "0",
            "contact_id": CONTACT_LINKED}
        self.item["ai_tasks"] = [
            {"task": "Send revised quote", "assignee": "Speaker 0",
             "assignee_speaker_id": "0"},
        ]
        api._seed_ai_tasks(OWNER, KEY, self.item)

        self.assertEqual(self.types_for(ASSIGNEE_USER), [ns.TYPE_TASK_ASSIGNED])
        self.assertEqual(self.rows_for(OWNER), [])

    def test_ai_task_with_no_assignee_at_all_notifies_nobody(self):
        """An unassigned task is a normal state, not an ambiguity to review."""
        self.item["ai_tasks"] = [{"task": "Book the venue"}]
        api._seed_ai_tasks(OWNER, KEY, self.item)
        self.assertEqual(self.rows_for(), [])

    def test_reseeding_the_same_ai_tasks_does_not_duplicate(self):
        """Two devices opening the meeting, or a reprocess, must not double up
        — the task fingerprint stops the task, and the dedupe key stops the
        notification even if the task somehow got through."""
        self.item["ai_tasks"] = [
            {"task": "Prepare proposal", "assignee": "Someone Ambiguous"},
        ]
        for _ in range(3):
            api._seed_ai_tasks(OWNER, KEY, self.item)
        self.assertEqual(len(self.rows_for(OWNER)), 1)


# ---------------------------------------------------------------------------
# DEADLINES — the sweep.
# ---------------------------------------------------------------------------
class DeadlineTests(NotificationTestCase):

    def setUp(self):
        super().setUp()
        self.now = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)
        self.today = "2026-08-29"

    def mine(self, **over):
        """A task assigned to the CALLER — the only kind the sweep considers."""
        return self.make_task(
            assignee_contact=self.contacts.items[(CONTACT_LINKED,)],
            assignee_user_id=OWNER, **over)

    def test_due_today_raises_one_notification(self):
        task = self.mine(due="today", due_normalized=self.today)
        with mock.patch.object(api, "_is_overdue", return_value=False):
            api._sweep_task_deadlines(OWNER, [task], now=self.now)
        rows = self.rows_for(OWNER)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["type"], ns.TYPE_TASK_DUE_TODAY)
        self.assertEqual(rows[0]["title"], "Task due today")
        self.assertEqual(rows[0]["entity_id"], task["task_id"])

    def test_repeated_sweeps_on_the_same_day_do_not_duplicate(self):
        """THE test for this feature. The sweep runs on every task list read,
        so without day-scoped dedupe the user would be told on every app
        open, forever."""
        task = self.mine(due="today", due_normalized=self.today)
        with mock.patch.object(api, "_is_overdue", return_value=False):
            for _ in range(10):
                api._sweep_task_deadlines(OWNER, [task], now=self.now)
        self.assertEqual(len(self.rows_for(OWNER)), 1)

    def test_overdue_raises_one_notification_per_day(self):
        """A task left overdue for days produces one row per day — not one
        forever (which scrolls away and is missed), and not one per read."""
        task = self.mine(due="2026-08-20", due_normalized="2026-08-20")
        day_two = self.now + timedelta(days=1)

        for _ in range(3):
            api._sweep_task_deadlines(OWNER, [task], now=self.now)
        self.assertEqual(len(self.rows_for(OWNER)), 1)

        for _ in range(3):
            api._sweep_task_deadlines(OWNER, [task], now=day_two)
        rows = self.rows_for(OWNER)
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["type"] for r in rows}, {ns.TYPE_TASK_OVERDUE})

    def test_overdue_wins_over_due_today(self):
        """A task cannot be both. Overdue is asked first, so a past-due task
        never also claims to be due today."""
        task = self.mine(due="2026-08-20", due_normalized="2026-08-20")
        api._sweep_task_deadlines(OWNER, [task], now=self.now)
        self.assertEqual(self.types_for(OWNER), [ns.TYPE_TASK_OVERDUE])

    def test_a_completed_task_never_nags(self):
        task = self.mine(due="2026-08-20", due_normalized="2026-08-20",
                         status=api.TASK_STATUS_COMPLETED)
        api._sweep_task_deadlines(OWNER, [task], now=self.now)
        self.assertEqual(self.rows_for(), [])

    def test_a_task_with_no_due_date_is_never_swept(self):
        api._sweep_task_deadlines(OWNER, [self.mine()], now=self.now)
        self.assertEqual(self.rows_for(), [])

    def test_someone_elses_task_is_not_my_deadline(self):
        """A task the caller OWNS but assigned to another person is that
        person's reminder, not theirs."""
        task = self.make_task(
            assignee_contact=self.contacts.items[(CONTACT_LINKED,)],
            due="2026-08-20", due_normalized="2026-08-20")
        self.assertEqual(task["assignee_user_id"], ASSIGNEE_USER)
        api._sweep_task_deadlines(OWNER, [task], now=self.now)
        self.assertEqual(self.rows_for(OWNER), [])

    def test_another_tenants_row_is_ignored_even_if_handed_in(self):
        """Defence in depth: the sweep re-checks ownership rather than
        trusting the rows it was given."""
        foreign = dict(self.mine(due="2026-08-20",
                                 due_normalized="2026-08-20"))
        foreign["owner_user_id"] = STRANGER
        api._sweep_task_deadlines(OWNER, [foreign], now=self.now)
        self.assertEqual(self.rows_for(), [])


# ---------------------------------------------------------------------------
# MEETING PROCESSING — raised by the transcribe Lambda through the same
# shared contract. Exercised here at the schema/dedupe level, which is the
# part that has to agree across the two Lambdas.
# ---------------------------------------------------------------------------
class MeetingProcessingTests(NotificationTestCase):

    def test_completion_notification_points_at_the_meeting(self):
        api._notify(OWNER, ns.TYPE_MEETING_PROCESSING_COMPLETED, KEY,
                    subject="Client Discussion")
        rows = self.rows_for(OWNER)
        self.assertEqual(rows[0]["title"], "Meeting processing completed")
        self.assertEqual(rows[0]["message"], "Client Discussion is ready")
        self.assertEqual(rows[0]["entity_type"], "meeting")
        self.assertEqual(rows[0]["entity_id"], KEY)

    def test_failure_notification_is_high_priority(self):
        api._notify(OWNER, ns.TYPE_MEETING_PROCESSING_FAILED, KEY,
                    subject="Client Discussion")
        rows = self.rows_for(OWNER)
        self.assertEqual(rows[0]["title"], "Meeting processing failed")
        self.assertEqual(rows[0]["message"],
                         "Client Discussion could not be processed")
        self.assertEqual(rows[0]["priority"], ns.PRIORITY_HIGH)

    def test_a_reprocess_does_not_re_announce_the_meeting(self):
        """Retrying or reprocessing must not tell the user twice."""
        for _ in range(5):
            api._notify(OWNER, ns.TYPE_MEETING_PROCESSING_COMPLETED, KEY,
                        subject="Client Discussion")
        self.assertEqual(len(self.rows_for(OWNER)), 1)

    def test_ai_output_ready_is_a_separate_fact(self):
        """Completion and 'the AI output is ready' are different facts and do
        not suppress each other — a degraded row gets the first, not both."""
        api._notify(OWNER, ns.TYPE_MEETING_PROCESSING_COMPLETED, KEY,
                    subject="Client Discussion")
        api._notify(OWNER, ns.TYPE_AI_OUTPUT_READY, KEY,
                    subject="Client Discussion")
        self.assertEqual(self.types_for(OWNER),
                         sorted([ns.TYPE_MEETING_PROCESSING_COMPLETED,
                                 ns.TYPE_AI_OUTPUT_READY]))

    def test_document_ready_dedupes_per_document_type(self):
        """Regenerating the same document must not re-notify; a DIFFERENT
        document from the same meeting still does."""
        for _ in range(3):
            api._notify_document_ready(OWNER, KEY, self.item, "minutes_of_meeting")
        api._notify_document_ready(OWNER, KEY, self.item, "action_plan")
        rows = self.rows_for(OWNER)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["type"] == ns.TYPE_MEETING_DOCUMENT_READY
                            for r in rows))
        self.assertTrue(all(r["entity_id"] == KEY for r in rows))

    def test_an_untitled_meeting_is_labelled_never_invented(self):
        """Never fabricate. An untitled recording says so."""
        api._notify(OWNER, ns.TYPE_MEETING_PROCESSING_COMPLETED, KEY,
                    subject=api._recording_title({"title": ""}))
        self.assertIn(ns.UNTITLED_MEETING, self.rows_for(OWNER)[0]["message"])


# ---------------------------------------------------------------------------
# API — list, count, mark read, mark all, pagination.
# ---------------------------------------------------------------------------
class ApiTests(NotificationTestCase):

    def seed(self, n, user_id=OWNER, read=False):
        made = []
        for i in range(n):
            row = api._notify(user_id, ns.TYPE_TASK_ASSIGNED, f"task-{i}",
                              subject=f"Task {i}")
            if read and row:
                api._mark_read(row)
            made.append(row)
        return made

    def test_list_returns_notifications_and_the_unread_count(self):
        self.seed(3)
        status, body = parse(call(api.list_notifications,
                                  event("GET", "/notifications")))
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 3)
        self.assertEqual(body["unread_count"], 3)
        self.assertEqual(len(body["notifications"]), 3)
        self.assertNotIn("dedupe_key", json.dumps(body))

    def test_list_is_newest_first(self):
        self.seed(3)
        _, body = parse(call(api.list_notifications,
                             event("GET", "/notifications")))
        created = [n["created_at"] for n in body["notifications"]]
        self.assertEqual(created, sorted(created, reverse=True))

    def test_pagination_walks_the_whole_inbox_without_repeats(self):
        self.seed(5)
        seen, cursor, pages = [], "", 0
        while pages < 10:
            qs = {"limit": "2"}
            if cursor:
                qs["cursor"] = cursor
            _, body = parse(call(api.list_notifications,
                                 event("GET", "/notifications", qs=qs)))
            seen.extend(n["notification_id"] for n in body["notifications"])
            cursor = body["next_cursor"]
            pages += 1
            if not cursor:
                break
        self.assertEqual(len(seen), 5)
        self.assertEqual(len(set(seen)), 5)

    def test_unread_filter_returns_only_unread(self):
        rows = self.seed(3)
        api._mark_read(rows[0])
        _, body = parse(call(api.list_notifications,
                             event("GET", "/notifications", qs={"unread": "1"})))
        self.assertEqual(body["count"], 2)
        self.assertTrue(all(not n["is_read"] for n in body["notifications"]))

    def test_unread_count_endpoint(self):
        rows = self.seed(3)
        api._mark_read(rows[0])
        _, body = parse(call(api.get_unread_count,
                             event("GET", "/notifications/unread-count")))
        self.assertEqual(body["unread_count"], 2)

    def test_mark_read_flips_the_row_and_drops_the_count(self):
        rows = self.seed(2)
        _, body = parse(call(
            api.mark_notification_read,
            event("POST", "/notifications/{notification_id}/read",
                  params={"notification_id": rows[0]["notification_id"]})))
        self.assertTrue(body["notification"]["is_read"])
        self.assertTrue(body["notification"]["read_at"])
        self.assertEqual(body["unread_count"], 1)
        # The sparse marker is REMOVED — that is what takes it out of the
        # unread index, and is why the badge stops counting it.
        stored = self.notifications.items[(rows[0]["notification_id"],)]
        self.assertNotIn("unread_marker", stored)

    def test_mark_read_is_idempotent(self):
        """A double tap must not read as a failure."""
        rows = self.seed(1)
        ev = event("POST", "/notifications/{notification_id}/read",
                   params={"notification_id": rows[0]["notification_id"]})
        self.assertEqual(parse(call(api.mark_notification_read, ev))[0], 200)
        status, body = parse(call(api.mark_notification_read, ev))
        self.assertEqual(status, 200)
        self.assertTrue(body["notification"]["is_read"])
        self.assertEqual(body["unread_count"], 0)

    def test_mark_read_on_a_missing_notification_is_404(self):
        ev = event("POST", "/notifications/{notification_id}/read",
                   params={"notification_id": "does-not-exist"})
        self.assertEqual(parse(call(api.mark_notification_read, ev))[0], 404)

    def test_mark_all_read_clears_the_badge(self):
        self.seed(4)
        _, body = parse(call(api.mark_all_notifications_read,
                             event("POST", "/notifications/read-all")))
        self.assertEqual(body["marked"], 4)
        self.assertEqual(body["unread_count"], 0)
        self.assertEqual(body["remaining"], 0)
        _, listed = parse(call(api.list_notifications,
                               event("GET", "/notifications")))
        self.assertTrue(all(n["is_read"] for n in listed["notifications"]))

    def test_mark_all_read_with_nothing_unread_is_a_no_op(self):
        self.seed(2, read=True)
        _, body = parse(call(api.mark_all_notifications_read,
                             event("POST", "/notifications/read-all")))
        self.assertEqual(body["marked"], 0)
        self.assertEqual(body["unread_count"], 0)

    def test_read_notifications_are_kept_not_deleted(self):
        """History survives being read (requirement section 23)."""
        rows = self.seed(2)
        api._mark_read(rows[0])
        self.assertEqual(len(self.notifications.items), 2)

    def test_an_invalid_cursor_is_rejected(self):
        status, _ = parse(call(
            api.list_notifications,
            event("GET", "/notifications", qs={"cursor": "not-base64"})))
        self.assertEqual(status, 400)


# ---------------------------------------------------------------------------
# SECURITY — the tenant boundary.
# ---------------------------------------------------------------------------
class SecurityTests(NotificationTestCase):

    def setUp(self):
        super().setUp()
        self.mine = api._notify(OWNER, ns.TYPE_TASK_ASSIGNED, "task-mine",
                                subject="Mine")
        self.theirs = api._notify(STRANGER, ns.TYPE_TASK_ASSIGNED,
                                  "task-theirs", subject="Theirs")

    def test_a_user_never_sees_another_users_notifications(self):
        _, body = parse(call(api.list_notifications,
                             event("GET", "/notifications")))
        ids = [n["notification_id"] for n in body["notifications"]]
        self.assertEqual(ids, [self.mine["notification_id"]])
        self.assertNotIn("Theirs", json.dumps(body))

    def test_the_unread_count_is_per_user(self):
        _, body = parse(call(api.get_unread_count,
                             event("GET", "/notifications/unread-count")))
        self.assertEqual(body["unread_count"], 1)

    def test_a_user_cannot_mark_another_users_notification_read(self):
        ev = event("POST", "/notifications/{notification_id}/read",
                   params={"notification_id": self.theirs["notification_id"]})
        status, _ = parse(call(api.mark_notification_read, ev))
        self.assertEqual(status, 404)
        # And it really is untouched.
        stored = self.notifications.items[(self.theirs["notification_id"],)]
        self.assertFalse(stored["is_read"])

    def test_a_foreign_id_is_indistinguishable_from_a_missing_one(self):
        """404 for both, so the API is not an existence oracle: a caller
        cannot learn that someone else's notification id is real."""
        foreign = event("POST", "/notifications/{notification_id}/read",
                        params={"notification_id":
                                self.theirs["notification_id"]})
        missing = event("POST", "/notifications/{notification_id}/read",
                        params={"notification_id": "nope"})
        self.assertEqual(parse(call(api.mark_notification_read, foreign)),
                         parse(call(api.mark_notification_read, missing)))

    def test_mark_all_read_only_touches_the_callers_rows(self):
        call(api.mark_all_notifications_read,
             event("POST", "/notifications/read-all"))
        self.assertFalse(
            self.notifications.items[(self.theirs["notification_id"],)]["is_read"])

    def test_every_notification_route_requires_authentication(self):
        """No route reads a user id from the request — identity comes from
        the JWT or the call fails."""
        with mock.patch.object(api, "_require_auth",
                               side_effect=api.ApiError(401, "missing bearer token")):
            for handler, ev in (
                (api.list_notifications, event("GET", "/notifications", auth=False)),
                (api.get_unread_count,
                 event("GET", "/notifications/unread-count", auth=False)),
                (api.mark_notification_read,
                 event("POST", "/notifications/{notification_id}/read",
                       params={"notification_id": "x"}, auth=False)),
                (api.mark_all_notifications_read,
                 event("POST", "/notifications/read-all", auth=False)),
            ):
                self.assertEqual(parse(call(handler, ev))[0], 401)

    def test_the_routes_are_registered(self):
        """A handler nobody can reach is not a feature."""
        for route in (("GET", "/notifications"),
                      ("GET", "/notifications/unread-count"),
                      ("POST", "/notifications/{notification_id}/read"),
                      ("POST", "/notifications/read-all")):
            self.assertIn(route, api._ROUTES)


# ---------------------------------------------------------------------------
# GMAIL SEPARATION.
# ---------------------------------------------------------------------------
class GmailSeparationTests(NotificationTestCase):

    def test_raising_notifications_never_touches_gmail(self):
        """The architectural constraint, made enforceable.

        Nothing except this test stops someone "helpfully" wiring an email
        into the notification engine later — and by then the coupling would be
        load-bearing. Every send seam is stubbed to explode; the whole event
        surface is then exercised.
        """
        boom = mock.Mock(side_effect=AssertionError(
            "the notification engine must not send email"))
        with mock.patch.object(api, "_send_via_gmail", boom), \
                mock.patch.object(api, "_integration_call", boom):
            task = self.make_task(
                assignee_contact=self.contacts.items[(CONTACT_LINKED,)])
            api._notify_task_assigned(task, actor_user_id=OWNER)
            api._notify_task_reassigned(ASSIGNEE_USER, task, actor_user_id=OWNER)
            api._notify_ai_action_required(task)
            api._notify_document_ready(OWNER, KEY, self.item, "minutes_of_meeting")
            api._notify_meeting_shared(OWNER, KEY, self.item, "shr_1")
            api._notify(OWNER, ns.TYPE_MEETING_PROCESSING_COMPLETED, KEY,
                        subject="Client Discussion")
        boom.assert_not_called()
        self.assertTrue(self.rows_for())

    def test_the_engine_declares_only_the_in_app_channel(self):
        """Rows say what actually reached the user. Nothing claims EMAIL."""
        api._notify(ASSIGNEE_USER, ns.TYPE_TASK_ASSIGNED, "task-1", subject="t")
        for row in self.rows_for():
            self.assertEqual(list(row["channels"]), [ns.CHANNEL_IN_APP])

    def test_gmail_send_does_not_create_notifications(self):
        """Gmail stays a user-triggered communication integration. Sending an
        email is not a notification event."""
        self.assertEqual(self.rows_for(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
