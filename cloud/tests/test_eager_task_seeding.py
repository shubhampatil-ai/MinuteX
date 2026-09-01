#!/usr/bin/env python3
"""test_eager_task_seeding.py — AI tasks exist when processing finishes.

THE BUG THIS PINS. Task seeding used to happen only when somebody opened a
task list, which made the Tasks table a function of who had browsed where. A
meeting could finish, extract five real action items, and have none of them
exist anywhere the product could see: absent from GET /tasks, no TASK_ASSIGNED
for the assignee, no AI_ACTION_REQUIRED for the owner. Everything corrected
itself the moment someone opened the meeting — which is exactly what made it
easy to miss, because anyone testing by opening the meeting they just recorded
sees a working system.

So the load-bearing test in this file is the one that NEVER OPENS THE MEETING
(TestTasksExistWithoutOpeningTheMeeting). Every other group here exists to show
that making seeding eager did not break the properties the lazy path relied on.

WHAT IS DELIBERATELY *NOT* RE-TESTED. The seeder's own rules — fingerprint
dedupe, tombstones, the speaker/contact/account resolution chain, spoken-date
normalisation — are covered in test_task_extraction.py and test_workspace_org.py
and are UNCHANGED by this work. Re-asserting them here would be duplicate
coverage that a future refactor has to maintain twice. What is tested here is
strictly the new seam:

  ORDERING      the seed happens after the terminal write and before the
                completion notification, so a notification can never announce a
                meeting whose tasks do not exist yet;
  GATING        only a genuinely complete run is seeded — a failed one has no
                analysis to seed, and asking anyway spends an invoke to do
                nothing;
  NON-BLOCKING  a seeding failure must not fail the pipeline, because the
                transcript and analysis are already paid for and persisted, and
                raising would make S3/Lambda re-run the WHOLE pipeline;
  TENANCY       the internal event names a recording, never a user — the owner
                comes off the row, so the invoke cannot write into another
                account;
  IDEMPOTENCE   eager THEN lazy, and eager twice, both produce one set of rows.

OFFLINE, like the rest of this directory: fake_dynamodb, no AWS, no network,
no Groq. Both Lambdas are loaded side by side (they share a module NAME, so the
transcribe one is imported under a distinct key — see test_async_stt.py, whose
preamble this mirrors).

Run:  python -m pytest tests/test_eager_task_seeding.py
"""
import importlib.util as _ilu
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "functions/userapi"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Installs the shared boto3/botocore stubs and binds the SAME `api` module
# object every other suite here uses — see conftest.py on why one stub identity
# matters.
from test_ai_workspace import RECORDING, api  # noqa: E402

import fake_dynamodb as fdb  # noqa: E402
import notification_schema as ns  # noqa: E402

# Both Lambdas' modules are named lambda_function.py, so the transcribe one is
# loaded under a distinct name rather than shadowing userApi in sys.modules.
_spec = _ilu.spec_from_file_location(
    "transcribe_lambda_function",
    str(ROOT / "functions/transcribe" / "lambda_function.py"))
transcribe = _ilu.module_from_spec(_spec)
sys.modules["transcribe_lambda_function"] = transcribe
_spec.loader.exec_module(transcribe)

KEY = "recordings/u-1/mobile/mobile-abc_1754300000.m4a"
OWNER = "u-1"
STRANGER = "u-2"
ASSIGNEE_USER = "u-3"          # a real MinuteX account behind a contact
CONTACT_LINKED = "c-linked"    # contact WITH an account
CONTACT_UNLINKED = "c-plain"   # contact with no account


class SeedingBase(unittest.TestCase):
    """Real fake tables — every test here is a round trip.

    A MagicMock cannot tell a working state transition from a broken one, and
    what is under test IS a transition: seed, then seed again, and assert the
    second one created nothing.
    """

    def setUp(self):
        self.item = json.loads(json.dumps(RECORDING))
        self.item["user_id"] = OWNER
        self.item["status"] = "complete"
        self.item.pop("ai_tasks", None)

        self.tasks = fdb.FakeTable("Tasks", "task_id", indexes={
            "owner-index": ("owner_user_id", "created_at"),
            "meeting-index": ("source_recording_id", "created_at"),
            "folder-index": ("folder_id", "created_at"),
            "assignee-index": ("assignee_contact_id", "created_at"),
            "dedupe-index": ("owner_user_id", "fingerprint")})
        self.recordings = fdb.FakeTable("Recordings", "audio_s3_key")
        self.contacts = fdb.FakeTable("Contacts", "contact_id")
        self.participants = fdb.FakeTable(
            "MeetingParticipants", "audio_s3_key", "speaker_id")
        self.notifications = fdb.FakeTable(
            "Notifications", "notification_id", indexes={
                "user-index": ("user_id", "created_at"),
                "user-unread-index": ("user_id", "unread_marker")})
        self.dedupe = fdb.FakeTable("NotificationDedupe", "dedupe_key")

        self.recordings.items[(KEY,)] = self.item
        self.contacts.items[(CONTACT_LINKED,)] = {
            "contact_id": CONTACT_LINKED, "owner_user_id": OWNER,
            "name": "Rahul Sharma", "email": "rahul@example.com",
            "minutex_user_id": ASSIGNEE_USER}
        self.contacts.items[(CONTACT_UNLINKED,)] = {
            "contact_id": CONTACT_UNLINKED, "owner_user_id": OWNER,
            "name": "Neha Shah", "email": "neha@example.com"}

        for p in [
            mock.patch.object(api, "Key", fdb.Key),
            mock.patch.object(api, "_tasks", self.tasks),
            mock.patch.object(api, "_recordings", self.recordings),
            mock.patch.object(api, "_contacts", self.contacts),
            mock.patch.object(api, "_meeting_participants", self.participants),
            mock.patch.object(api, "_notifications", self.notifications),
            mock.patch.object(api, "_notification_dedupe", self.dedupe),
            mock.patch.object(api, "_owned_devices", return_value=[]),
        ]:
            p.start()
            self.addCleanup(p.stop)

    # -- helpers ----------------------------------------------------------
    def set_ai_tasks(self, rows):
        self.item["ai_tasks"] = rows
        self.recordings.items[(KEY,)] = self.item

    def seed_event(self, key=KEY, **extra):
        return {"type": api.INTERNAL_SEED_TASKS_EVENT,
                "audio_s3_key": key, **extra}

    def eager_seed(self, **extra):
        """What the pipeline's invoke does, through the REAL entry point."""
        return api.lambda_handler(self.seed_event(**extra), None)

    def task_titles(self):
        return sorted(r["title"] for r in self.tasks.items.values())

    def notif_types(self, user_id=None):
        rows = self.notifications.items.values()
        if user_id is not None:
            rows = [r for r in rows if r.get("user_id") == user_id]
        return sorted(r["type"] for r in rows)


# ---------------------------------------------------------------------------
# THE POINT OF THE WHOLE CHANGE.
# ---------------------------------------------------------------------------
class TestTasksExistWithoutOpeningTheMeeting(SeedingBase):

    def test_tasks_are_in_the_tracker_before_anyone_opens_the_meeting(self):
        """The primary acceptance test.

        A meeting is processed and NOBODY opens it. GET /tasks must already
        show its tasks. Under lazy seeding this returned an empty list, which
        is the entire bug.
        """
        self.set_ai_tasks([
            {"task": "Send the revised quote", "assignee": "",
             "assignee_speaker_id": "0", "due_date": "tomorrow",
             "confidence": "high", "evidence": "I'll send the quote."},
            {"task": "Book the venue", "assignee": "", "confidence": "medium",
             "evidence": "We need the venue booked."},
        ])
        self.eager_seed()

        # Straight to the Task Tracker — the meeting route is never called.
        with mock.patch.object(api, "_require_auth", return_value=OWNER):
            resp = api.lambda_handler({
                "routeKey": "GET /tasks",
                "requestContext": {"http": {"method": "GET", "path": "/tasks"}},
                "headers": {"authorization": "Bearer t"},
            }, None)
        body = json.loads(resp["body"])

        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(body["count"], 2)
        self.assertEqual(sorted(t["task"] for t in body["tasks"]),
                         ["Book the venue", "Send the revised quote"])

    def test_assignee_is_notified_without_the_meeting_being_opened(self):
        """Section 12: a resolvable self-commitment reaches its owner.

        The notification is raised BY the seeder, so before this change it
        could not exist until someone browsed to the meeting.
        """
        self.participants.items[(KEY, "0")] = {
            "audio_s3_key": KEY, "speaker_id": "0",
            "contact_id": CONTACT_LINKED}
        self.set_ai_tasks([
            {"task": "Send the proposal", "assignee": "",
             "assignee_speaker_id": "0", "due_date": "tomorrow",
             "confidence": "high", "evidence": "I'll send the proposal."},
        ])
        self.eager_seed()

        row = next(iter(self.tasks.items.values()))
        self.assertEqual(row["resolution_status"], api.RESOLUTION_RESOLVED)
        self.assertEqual(row["assignee_user_id"], ASSIGNEE_USER)
        self.assertEqual(self.notif_types(ASSIGNEE_USER),
                         [ns.TYPE_TASK_ASSIGNED])

    def test_unresolved_assignee_asks_the_owner_without_opening(self):
        """Section 13: "Rahul, please send it" where Rahul cannot be resolved.

        The task exists immediately, stays UNRESOLVED, and the OWNER — not the
        unidentified Rahul — is asked to confirm.
        """
        self.set_ai_tasks([
            {"task": "Send the proposal", "assignee": "Rahul",
             "assignee_speaker_id": "", "confidence": "high",
             "evidence": "Rahul, please send the proposal tomorrow."},
        ])
        self.eager_seed()

        row = next(iter(self.tasks.items.values()))
        self.assertEqual(row["resolution_status"], api.RESOLUTION_UNRESOLVED)
        self.assertEqual(row["assignee_name_legacy"], "Rahul")
        self.assertNotIn("assignee_user_id", row)
        self.assertEqual(self.notif_types(OWNER),
                         [ns.TYPE_AI_ACTION_REQUIRED])

    def test_a_task_with_no_assignee_notifies_nobody(self):
        """An unassigned task is a normal state, not an ambiguity to review."""
        self.set_ai_tasks([{"task": "Book the venue", "confidence": "low"}])
        self.eager_seed()

        row = next(iter(self.tasks.items.values()))
        self.assertEqual(row["resolution_status"], api.RESOLUTION_NONE)
        self.assertEqual(self.notif_types(), [])


# ---------------------------------------------------------------------------
# THE PIPELINE SEAM — where the invoke is made, and when it is not.
# ---------------------------------------------------------------------------
class TestPipelineIntegration(unittest.TestCase):

    def test_a_complete_run_requests_seeding(self):
        with mock.patch.object(transcribe, "_lambda_client") as client:
            self.assertTrue(transcribe._seed_tasks_for(KEY, "complete"))
        client.invoke.assert_called_once()
        kwargs = client.invoke.call_args.kwargs
        payload = json.loads(kwargs["Payload"].decode("utf-8"))

        # Async, so the pipeline never waits for the task layer.
        self.assertEqual(kwargs["InvocationType"], "Event")
        self.assertEqual(payload["type"], transcribe.SEED_TASKS_EVENT)
        self.assertEqual(payload["audio_s3_key"], KEY)

    def test_the_event_type_matches_what_userapi_dispatches_on(self):
        """A typo here is a SILENT no-op — userApi would ignore the event and
        the tasks would quietly go back to being lazy-only. Pinning the two
        constants against each other is what makes that impossible."""
        self.assertEqual(transcribe.SEED_TASKS_EVENT,
                         api.INTERNAL_SEED_TASKS_EVENT)

    def test_the_event_never_carries_a_user_id(self):
        """Tenancy comes off the recording ROW. An event that named its own
        user would be a way to write tasks into someone else's account."""
        with mock.patch.object(transcribe, "_lambda_client") as client:
            transcribe._seed_tasks_for(KEY, "complete")
        payload = json.loads(
            client.invoke.call_args.kwargs["Payload"].decode("utf-8"))
        for forbidden in ("user_id", "owner_user_id", "assignee_user_id"):
            self.assertNotIn(forbidden, payload)

    def test_a_failed_run_is_not_seeded(self):
        """A failed row has no analysis to seed; asking spends an invoke to do
        nothing."""
        with mock.patch.object(transcribe, "_lambda_client") as client:
            self.assertFalse(transcribe._seed_tasks_for(KEY, "failed"))
        client.invoke.assert_not_called()

    def test_a_degraded_run_is_not_seeded(self):
        """"transcribed" means the Groq analysis did not survive — the very
        thing that would have carried the tasks."""
        with mock.patch.object(transcribe, "_lambda_client") as client:
            self.assertFalse(transcribe._seed_tasks_for(KEY, "transcribed"))
        client.invoke.assert_not_called()

    def test_an_invoke_failure_never_fails_the_pipeline(self):
        """THE non-blocking rule. The transcript and analysis are already
        persisted and already paid for; raising would make S3/Lambda re-run the
        entire pipeline for a recording that succeeded."""
        with mock.patch.object(transcribe, "_lambda_client") as client:
            client.invoke.side_effect = RuntimeError("lambda unreachable")
            self.assertFalse(transcribe._seed_tasks_for(KEY, "complete"))

    def test_seeding_is_requested_before_the_completion_notification(self):
        """Ordering. The completion notification is what sends the user to the
        meeting, so the tasks should already be on their way when it fires."""
        calls = []
        with mock.patch.object(transcribe, "_seed_tasks_for",
                               side_effect=lambda *a: calls.append("seed")), \
             mock.patch.object(transcribe, "_notify_processing_outcome",
                               side_effect=lambda *a: calls.append("notify")), \
             mock.patch.object(transcribe, "_upsert",
                               side_effect=lambda *a, **k: calls.append("write")), \
             mock.patch.object(transcribe, "_table") as table, \
             mock.patch.object(transcribe, "analyze_meeting") as analyze, \
             mock.patch.object(transcribe, "transcript_store") as store:
            table.get_item.return_value = {"Item": {}}
            analyze.return_value = {
                "title": "T", "overview": {"sections": [{"kind": "text",
                    "heading": "H", "content": "c"}]},
                "tasks": [], "participants": [], "meeting_highlights": {}}
            store.with_segment_ids.return_value = []
            store.put.return_value = {}
            transcribe.analyze_and_persist(
                "bucket", KEY, "Speaker 0: hello.", [], "en")

        # There is an earlier _upsert too (status -> "generating_ai"), so the
        # assertion is on the TAIL: the last write is the terminal one, and
        # seeding sits between it and the announcement.
        self.assertEqual(calls[-3:], ["write", "seed", "notify"],
                         f"expected terminal write -> seed -> notify, got {calls}")
        self.assertEqual(calls.count("seed"), 1)
        self.assertEqual(calls.count("notify"), 1)


# ---------------------------------------------------------------------------
# THE INTERNAL EVENT — tenancy and robustness of the new entry point.
# ---------------------------------------------------------------------------
class TestInternalSeedEvent(SeedingBase):

    def test_the_owner_comes_from_the_row_not_the_event(self):
        """A forged user_id on the event must be ignored outright."""
        self.set_ai_tasks([{"task": "Send the quote", "confidence": "high"}])
        self.eager_seed(user_id=STRANGER, owner_user_id=STRANGER)

        row = next(iter(self.tasks.items.values()))
        self.assertEqual(row["owner_user_id"], OWNER)

    def test_an_unknown_recording_is_reported_not_raised(self):
        out = self.eager_seed(key="recordings/u-9/mobile/nope.m4a")
        self.assertEqual(out["seeded"], 0)
        self.assertIn("error", out)
        self.assertEqual(len(self.tasks.items), 0)

    def test_a_missing_key_is_reported_not_raised(self):
        out = api.lambda_handler(
            {"type": api.INTERNAL_SEED_TASKS_EVENT}, None)
        self.assertEqual(out["seeded"], 0)
        self.assertIn("error", out)

    def test_a_legacy_row_without_an_owner_is_left_to_the_lazy_path(self):
        """Ownership of those rows is only resolvable through the UserDevices
        join, which needs the JWT. Guessing an owner would be worse than
        waiting."""
        self.item.pop("user_id")
        self.set_ai_tasks([{"task": "Send the quote", "confidence": "high"}])
        out = self.eager_seed()
        self.assertEqual(out["seeded"], 0)
        self.assertEqual(len(self.tasks.items), 0)

    def test_the_seed_event_is_not_reachable_over_http(self):
        """It carries no routeKey, so an HTTP request can never take that
        branch — the router 404s anything it does not recognise."""
        resp = api.lambda_handler({
            "routeKey": "POST /tasks/seed",
            "requestContext": {"http": {"method": "POST",
                                        "path": "/tasks/seed"}},
            "headers": {},
        }, None)
        self.assertEqual(resp["statusCode"], 404)


# ---------------------------------------------------------------------------
# IDEMPOTENCE — the property that makes eager + lazy safe together.
# ---------------------------------------------------------------------------
class TestIdempotence(SeedingBase):

    def setUp(self):
        super().setUp()
        self.set_ai_tasks([
            {"task": "Send the revised quote", "assignee": "Rahul",
             "confidence": "high", "evidence": "Rahul, send the quote."},
            {"task": "Book the venue", "confidence": "medium"},
        ])

    def test_reprocessing_creates_no_duplicates(self):
        """Section 14: the same recording processed twice stays N, not 2N."""
        self.eager_seed()
        first = self.task_titles()
        self.eager_seed()
        self.assertEqual(self.task_titles(), first)
        self.assertEqual(len(self.tasks.items), 2)

    def test_the_lazy_path_after_eager_seeding_creates_nothing(self):
        """Section 15: the retained lazy call must be a no-op once the pipeline
        has already seeded. This is what makes keeping it safe."""
        self.eager_seed()
        before = len(self.tasks.items)
        notifs_before = len(self.notifications.items)

        with mock.patch.object(api, "_require_auth", return_value=OWNER):
            resp = api.lambda_handler({
                "routeKey": "GET /recordings/ai/tasks/{key+}",
                "requestContext": {"http": {"method": "GET", "path": "/"}},
                "pathParameters": {"key": KEY},
                "headers": {"authorization": "Bearer t"},
            }, None)

        self.assertEqual(resp["statusCode"], 200)
        self.assertEqual(len(self.tasks.items), before)
        self.assertEqual(len(self.notifications.items), notifs_before)
        self.assertEqual(json.loads(resp["body"])["count"], 2)

    def test_notifications_are_not_repeated_across_seed_runs(self):
        self.eager_seed()
        types_once = self.notif_types()
        for _ in range(3):
            self.eager_seed()
        self.assertEqual(self.notif_types(), types_once)

    def test_a_partial_seed_is_completed_by_the_retry(self):
        """Section 10: attempt 1 writes one task then dies; the retry creates
        only the MISSING one."""
        calls = {"n": 0}
        real_write = api._write_task

        def flaky(row):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("transient dynamo failure")
            return real_write(row)

        with mock.patch.object(api, "_write_task", side_effect=flaky):
            with self.assertRaises(RuntimeError):
                self.eager_seed()
        self.assertEqual(len(self.tasks.items), 1)   # partial

        self.eager_seed()                            # the retry
        self.assertEqual(len(self.tasks.items), 2)   # completed, not doubled
        self.assertEqual(self.task_titles(),
                         ["Book the venue", "Send the revised quote"])

    def test_concurrent_eager_and_lazy_seeding_produce_one_set(self):
        """Section 16: the pipeline and a meeting screen racing each other.

        Interleaved rather than threaded — the fake table is not thread-safe,
        and the property under test is the fingerprint check, not the GIL.
        """
        self.eager_seed()
        with mock.patch.object(api, "_require_auth", return_value=OWNER):
            for _ in range(3):
                api.lambda_handler({
                    "routeKey": "GET /recordings/ai/tasks/{key+}",
                    "requestContext": {"http": {"method": "GET", "path": "/"}},
                    "pathParameters": {"key": KEY},
                    "headers": {"authorization": "Bearer t"},
                }, None)
                self.eager_seed()
        self.assertEqual(len(self.tasks.items), 2)

    def test_a_task_the_user_deleted_is_not_resurrected_by_eager_seeding(self):
        """The tombstone must outrank the pipeline, or deleting an AI task
        would become impossible: it would come back on the next reprocess."""
        self.eager_seed()
        victim = next(r for r in self.tasks.items.values()
                      if r["title"] == "Book the venue")

        with mock.patch.object(api, "_require_auth", return_value=OWNER):
            api.lambda_handler({
                "routeKey": "DELETE /recordings/ai/tasks/{key+}",
                "requestContext": {"http": {"method": "DELETE", "path": "/"}},
                "pathParameters": {"key": KEY},
                "headers": {"authorization": "Bearer t"},
                "body": json.dumps({"id": victim["task_id"]}),
            }, None)
        self.assertEqual(len(self.tasks.items), 1)

        self.eager_seed()
        self.assertEqual(self.task_titles(), ["Send the revised quote"])


# ---------------------------------------------------------------------------
# The seeder's own behaviour is unchanged — spot-checked, not re-proved.
# ---------------------------------------------------------------------------
class TestExistingBehaviourPreserved(SeedingBase):

    def test_deadlines_anchor_on_the_meeting_not_on_now(self):
        """Section 6. `recorded_at` is the anchor, so re-running the seeder
        years later resolves "tomorrow" to the day after the MEETING."""
        self.item["recorded_at"] = "2026-08-26T10:00:00Z"
        self.set_ai_tasks([
            {"task": "Send the quote", "due_date": "tomorrow",
             "confidence": "high"},
            {"task": "Review the draft", "due_date": "Friday",
             "confidence": "high"},
            {"task": "Ship it", "due_date": "end of Q3", "confidence": "low"},
            {"task": "No deadline here", "confidence": "medium"},
        ])
        self.eager_seed()

        by_title = {r["title"]: r for r in self.tasks.items.values()}
        self.assertEqual(by_title["Send the quote"]["due_date_normalized"],
                         "2026-08-27")
        self.assertEqual(by_title["Review the draft"]["due_date_normalized"],
                         "2026-08-28")
        # Unplaceable stays unplaceable — the spoken text is never destroyed.
        self.assertEqual(by_title["Ship it"]["due_date_normalized"], "")
        self.assertEqual(by_title["Ship it"]["due_date"], "end of Q3")
        self.assertEqual(by_title["No deadline here"]["due_date"], "")

    def test_evidence_and_confidence_survive_eager_seeding(self):
        self.set_ai_tasks([
            {"task": "Send the quote", "confidence": "high",
             "evidence": "I'll send the quote tomorrow."},
        ])
        self.eager_seed()
        row = next(iter(self.tasks.items.values()))
        self.assertEqual(row["ai_confidence"], "high")
        self.assertEqual(row["ai_evidence"], "I'll send the quote tomorrow.")
        self.assertEqual(row["source_type"], api.TASK_SOURCE_AI)

    def test_a_contact_without_an_account_is_not_promoted_to_a_user(self):
        """Section 5: a real contact with no MinuteX account has no inbox.
        Resolved as a person, but no assignee_user_id and nobody notified."""
        self.participants.items[(KEY, "0")] = {
            "audio_s3_key": KEY, "speaker_id": "0",
            "contact_id": CONTACT_UNLINKED}
        self.set_ai_tasks([
            {"task": "Send the quote", "assignee_speaker_id": "0",
             "confidence": "high"},
        ])
        self.eager_seed()

        row = next(iter(self.tasks.items.values()))
        self.assertEqual(row["resolution_status"], api.RESOLUTION_RESOLVED)
        self.assertNotIn("assignee_user_id", row)
        self.assertEqual(self.notif_types(), [])

    def test_the_meeting_link_is_stored_on_every_seeded_task(self):
        self.set_ai_tasks([{"task": "Send the quote", "confidence": "high"}])
        self.eager_seed()
        row = next(iter(self.tasks.items.values()))
        self.assertEqual(row["source_recording_id"], KEY)


if __name__ == "__main__":
    unittest.main(verbosity=2)
