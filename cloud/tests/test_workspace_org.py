#!/usr/bin/env python3
# =============================================================
# test_workspace_org.py — unit tests for Contacts, Folders, FolderContacts,
# MeetingParticipants and first-class Tasks.
#
# OFFLINE by design, like tests/test_ai_workspace.py: no AWS, no Groq, no
# network, no credentials. DynamoDB is replaced by tests/fake_dynamodb.py,
# which implements conditional writes, GSI queries (including sparse-index
# behavior) and UpdateExpression SET/REMOVE faithfully enough that a failure
# here means a real bug rather than a stub artifact.
#
# COVERAGE (mapped to the spec's section 34 checklist)
#   CONTACTS            create / read / update / delete, duplicate email,
#                       normalized phone, ambiguous name, cross-user access,
#                       unauthorized access
#   FOLDERS             create / rename / delete, duplicate name, move meeting,
#                       move between folders, move to General, cross-user,
#                       deletion behavior (meetings survive)
#   FOLDER CONTACTS     add / remove / duplicate association / multi-folder /
#                       cross-user
#   PARTICIPANTS        speaker mapping, remap, clear, invalid contact,
#                       cross-user, transcript never mutated
#   TASKS               create / update / status / assignment / due date /
#                       folder + meeting relationship / duplicate prevention /
#                       authorization / overdue computation
#   MIGRATION           embedded tasks migrate, no loss, rerun is idempotent,
#                       unresolved assignees preserved, deleted stays deleted
#   AI TASKS            detection seeds tasks, speaker-based assignee,
#                       resolution via speaker mapping, ambiguity, duplicate
#                       AI execution is a no-op
#   END TO END          the full section 35 scenario
#   ROUTER              every new route registered and reachable
#
# Run:  python tests/test_workspace_org.py
# =============================================================
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "functions/userapi"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_dynamodb as fdb  # noqa: E402

# Stub boto3/botocore BEFORE importing the Lambda: it builds resources at
# import time and must never touch AWS or look for credentials.
_fake_boto3 = mock.MagicMock()
sys.modules["boto3"] = _fake_boto3
sys.modules["boto3.dynamodb"] = mock.MagicMock()
_conditions = mock.MagicMock()
_conditions.Key = fdb.Key
sys.modules["boto3.dynamodb.conditions"] = _conditions
# Reuse the installed stub module when present (conftest.py under
# pytest) so every file shares ONE ClientError class; install this
# file's own only when running standalone.
_botocore_exc = sys.modules.get("botocore.exceptions") \
    or mock.MagicMock()
_botocore_exc.ClientError = fdb.ClientError
sys.modules.setdefault("botocore", mock.MagicMock())
sys.modules["botocore.exceptions"] = _botocore_exc
sys.modules["botocore.config"] = mock.MagicMock()

import lambda_function as api  # noqa: E402

USER = "u-1"
OTHER = "u-2"
KEY = "recordings/u-1/mobile/mobile-abc_1754300000.m4a"
KEY2 = "recordings/u-1/mobile/mobile-def_1754300001.m4a"

TRANSCRIPT = (
    "Speaker 0: I'll send the proposal tomorrow.\n\n"
    "Speaker 1: Great, and I'll review it before the client call."
)

BASE_RECORDING = {
    "audio_s3_key": KEY,
    "user_id": USER,
    "title": "Client Alpha kickoff",
    "created_at": "2026-08-04T09:15:00Z",
    "status": "complete",
    "transcript": TRANSCRIPT,
    "timestamps": [
        {"speaker": "0", "text": "I'll send the proposal tomorrow.",
         "start": 0.0, "end": 3.0},
        {"speaker": "1", "text": "Great, and I'll review it.",
         "start": 3.0, "end": 6.0},
    ],
    "ai_tasks": [
        {"task": "Send proposal", "assignee": "Rahul",
         "due_date": "2026-08-21", "priority": "High"},
    ],
}


def parse(resp):
    return resp["statusCode"], json.loads(resp["body"])


def event(method="GET", route="/contacts", body=None, path=None, qs=None,
          key=None):
    ev = {
        "routeKey": f"{method} {route}",
        "requestContext": {"http": {"method": method, "path": route}},
        "headers": {"authorization": "Bearer test-token"},
        "pathParameters": dict(path or {}),
    }
    if key is not None:
        ev["pathParameters"]["key"] = key
    if qs:
        ev["queryStringParameters"] = qs
    if body is not None:
        ev["body"] = json.dumps(body)
    return ev


def call(handler, ev):
    """Invoke a handler the way lambda_handler does — handlers raise ApiError
    and the ROUTER turns it into a response, so a test calling a handler
    directly would be asserting on an exception the client never sees."""
    try:
        return handler(ev)
    except api.ApiError as e:
        body = {"error": e.message}
        code = getattr(e, "code", "")
        if code:
            body["code"] = code
        candidates = getattr(e, "candidates", None)
        if candidates is not None:
            body["candidates"] = candidates
        return api._resp(e.status, body)


class OrgTestCase(unittest.TestCase):
    """Base: real in-memory tables, auth pinned to USER."""

    def setUp(self):
        self.t = fdb.build_tables()
        # The resource-level handle, for _linked_avatar_map's batch_get_item.
        # Keyed by the table NAMES the Lambda passes, not by our short keys.
        self.ddb = fdb.FakeResource({t.name: t for t in self.t.values()})
        # A presigner that returns a real, recognisable STRING. The default
        # MagicMock would make _avatar_view_url return a Mock, and every
        # assertion about an avatar URL would pass against an object that is
        # not a URL at all.
        self.s3 = mock.MagicMock()
        self.s3.generate_presigned_url.side_effect = (
            lambda op, Params=None, ExpiresIn=None:
            f"https://s3.test/{op}/{(Params or {}).get('Key', '')}"
        )
        self.patches = [
            mock.patch.object(api, "_ddb", self.ddb),
            mock.patch.object(api, "_s3", self.s3),
            mock.patch.object(api, "BUCKET_NAME", "test-bucket"),
            mock.patch.object(api, "USERS_TABLE", self.t["users"].name),
            mock.patch.object(api, "_recordings", self.t["recordings"]),
            mock.patch.object(api, "_contacts", self.t["contacts"]),
            mock.patch.object(api, "_meeting_participants", self.t["participants"]),
            mock.patch.object(api, "_tasks", self.t["tasks"]),
            mock.patch.object(api, "_users", self.t["users"]),
            mock.patch.object(api, "_require_auth", return_value=USER),
            mock.patch.object(api, "_owned_devices", return_value=[]),
            # transcript_store.hydrate would reach S3; the row already carries
            # its transcript inline in these fixtures.
            mock.patch.object(api.transcript_store, "hydrate",
                              side_effect=lambda s3, bucket, item: item),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])
        self.t["recordings"].put_item(Item=json.loads(json.dumps(BASE_RECORDING)))

    # -- helpers ---------------------------------------------------------
    def mk_contact(self, name="Rahul Sharma", email="rahul@company.com",
                   phone="", force=False):
        body = {"name": name}
        if email:
            body["email"] = email
        if phone:
            body["phone"] = phone
        if force:
            body["force"] = True
        return parse(call(api.create_contact,
                          event("POST", "/contacts", body=body)))

    def add_user(self, user_id, email):
        self.t["users"].put_item(Item={"user_id": user_id, "email": email})


# ===========================================================================
# CONTACTS
# ===========================================================================
class TestContacts(OrgTestCase):
    def test_create_and_get(self):
        status, body = self.mk_contact()
        self.assertEqual(status, 201)
        self.assertEqual(body["contact"]["name"], "Rahul Sharma")
        self.assertEqual(body["contact"]["email"], "rahul@company.com")
        cid = body["contact"]["id"]

        status, body = parse(call(api.get_contact, event(
            "GET", "/contacts/{contact_id}", path={"contact_id": cid})))
        self.assertEqual(status, 200)
        self.assertEqual(body["contact"]["id"], cid)

    def test_name_required(self):
        status, body = parse(call(api.create_contact,
                                  event("POST", "/contacts", body={})))
        self.assertEqual(status, 400)
        self.assertIn("name", body["error"])

    def test_invalid_email_rejected(self):
        status, _ = self.mk_contact(email="not-an-email")
        self.assertEqual(status, 400)

    def test_duplicate_email_returns_existing(self):
        """A second contact with the SAME email is one person, not two rows."""
        _, first = self.mk_contact()
        status, second = self.mk_contact(name="R. Sharma")
        self.assertEqual(status, 200)
        self.assertTrue(second["existing"])
        self.assertEqual(second["contact"]["id"], first["contact"]["id"])
        self.assertEqual(len(self.t["contacts"].items), 1)

    def test_email_case_insensitive_dedupe(self):
        self.mk_contact(email="Rahul@Company.COM")
        status, body = self.mk_contact(name="Other", email="rahul@company.com")
        self.assertEqual(status, 200)
        self.assertTrue(body["existing"])

    def test_phone_normalized_dedupe(self):
        """Separators must not create a second person."""
        _, first = self.mk_contact(name="Neha", email="",
                                   phone="+91 98765 43210")
        status, second = self.mk_contact(name="Neha S", email="",
                                         phone="+91-98765-43210")
        self.assertEqual(status, 200)
        self.assertEqual(second["contact"]["id"], first["contact"]["id"])

    def test_ambiguous_name_is_409_with_candidates(self):
        """Two people can share a name — the system must ASK, never merge."""
        self.mk_contact(name="Rahul Sharma", email="rahul.a@company.com")
        status, body = parse(call(api.create_contact, event(
            "POST", "/contacts", body={"name": "Rahul Sharma"})))
        self.assertEqual(status, 409)
        self.assertEqual(body["code"], "contact_ambiguous")
        self.assertEqual(len(body["candidates"]), 1)
        # Nothing was written.
        self.assertEqual(len(self.t["contacts"].items), 1)

    def test_ambiguous_name_force_creates_second_person(self):
        self.mk_contact(name="Rahul Sharma", email="rahul.a@company.com")
        status, body = self.mk_contact(name="Rahul Sharma", email="", force=True)
        self.assertEqual(status, 201)
        self.assertEqual(len(self.t["contacts"].items), 2)

    def test_no_email_contacts_do_not_collide(self):
        """Sparse index: two email-less contacts must both exist, which only
        works because email_lc is OMITTED rather than written as ""."""
        self.mk_contact(name="Alpha One", email="")
        status, _ = self.mk_contact(name="Beta Two", email="")
        self.assertEqual(status, 201)
        self.assertEqual(len(self.t["contacts"].items), 2)
        for item in self.t["contacts"].items.values():
            self.assertNotIn("email_lc", item)

    def test_update_contact(self):
        _, created = self.mk_contact()
        cid = created["contact"]["id"]
        status, body = parse(call(api.update_contact, event(
            "PATCH", "/contacts/{contact_id}", path={"contact_id": cid},
            body={"company": "Acme", "role": "CTO"})))
        self.assertEqual(status, 200)
        self.assertEqual(body["contact"]["company"], "Acme")
        self.assertEqual(body["contact"]["role"], "CTO")

    def test_update_to_taken_email_conflicts(self):
        _, a = self.mk_contact(name="A One", email="a@x.com")
        _, b = self.mk_contact(name="B Two", email="b@x.com")
        status, body = parse(call(api.update_contact, event(
            "PATCH", "/contacts/{contact_id}",
            path={"contact_id": b["contact"]["id"]},
            body={"email": "a@x.com"})))
        self.assertEqual(status, 409)

    def test_clearing_email_drops_index_key_and_link(self):
        self.add_user("u-9", "rahul@company.com")
        _, created = self.mk_contact()
        cid = created["contact"]["id"]
        self.assertEqual(
            self.t["contacts"].items[(cid,)]["minutex_user_id"], "u-9")
        parse(call(api.update_contact, event(
            "PATCH", "/contacts/{contact_id}", path={"contact_id": cid},
            body={"email": ""})))
        row = self.t["contacts"].items[(cid,)]
        self.assertNotIn("email_lc", row)
        self.assertNotIn("minutex_user_id", row)

    def test_minutex_user_linked_when_account_exists(self):
        self.add_user("u-456", "rahul@company.com")
        _, body = self.mk_contact()
        self.assertEqual(body["contact"]["minutex_user_id"], "u-456")

    def test_minutex_user_absent_when_no_account(self):
        _, body = self.mk_contact()
        self.assertEqual(body["contact"]["minutex_user_id"], "")

    def test_cross_user_contact_is_404(self):
        _, created = self.mk_contact()
        cid = created["contact"]["id"]
        with mock.patch.object(api, "_require_auth", return_value=OTHER):
            for handler, method in ((api.get_contact, "GET"),
                                    (api.update_contact, "PATCH"),
                                    (api.delete_contact, "DELETE")):
                status, body = parse(call(handler, event(
                    method, "/contacts/{contact_id}",
                    path={"contact_id": cid}, body={"name": "Hijacked"})))
                # 404 not 403 — a 403 would confirm the id exists.
                self.assertEqual(status, 404, handler.__name__)
                self.assertNotIn("Rahul", json.dumps(body))

    def test_list_contacts_is_owner_scoped(self):
        self.mk_contact(name="Mine One", email="mine@x.com")
        self.t["contacts"].put_item(Item={
            "contact_id": "c-other", "owner_user_id": OTHER,
            "name": "Theirs", "email": "theirs@x.com",
            "email_lc": "theirs@x.com", "created_at": "2026-01-01T00:00:00Z"})
        status, body = parse(call(api.list_contacts, event("GET", "/contacts")))
        self.assertEqual(status, 200)
        names = [c["name"] for c in body["contacts"]]
        self.assertEqual(names, ["Mine One"])

    def test_search_filters_server_side(self):
        self.mk_contact(name="Rahul Sharma", email="rahul@x.com")
        self.mk_contact(name="Neha Shah", email="neha@x.com")
        status, body = parse(call(api.list_contacts, event(
            "GET", "/contacts", qs={"search": "neha"})))
        self.assertEqual(status, 200)
        self.assertEqual([c["name"] for c in body["contacts"]], ["Neha Shah"])

    def test_limit_is_bounded(self):
        status, body = parse(call(api.list_contacts, event(
            "GET", "/contacts", qs={"limit": "99999"})))
        self.assertEqual(status, 200)
        status, _ = parse(call(api.list_contacts, event(
            "GET", "/contacts", qs={"limit": "abc"})))
        self.assertEqual(status, 400)

    def test_delete_contact_keeps_tasks_and_unresolves_them(self):
        _, created = self.mk_contact()
        cid = created["contact"]["id"]
        _, task = parse(call(api.create_meeting_task, event(
            "POST", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"task": "Send proposal", "assignee_contact_id": cid})))
        tid = task["task"]["id"]

        status, body = parse(call(api.delete_contact, event(
            "DELETE", "/contacts/{contact_id}", path={"contact_id": cid})))
        self.assertEqual(status, 200)
        self.assertEqual(body["unassigned_tasks"], 1)

        row = self.t["tasks"].items[(tid,)]
        self.assertNotIn("assignee_contact_id", row)   # link gone
        self.assertEqual(row["resolution_status"], "UNRESOLVED")
        self.assertEqual(row["assignee_name_legacy"], "Rahul Sharma")
        self.assertEqual(row["title"], "Send proposal")   # work survives




# ===========================================================================
# MEETING PARTICIPANTS / SPEAKER MAPPING
# ===========================================================================
class TestParticipants(OrgTestCase):
    def setUp(self):
        super().setUp()
        _, c = self.mk_contact()
        self.cid = c["contact"]["id"]

    _UNSET = object()

    def _set(self, speaker="0", contact_id=_UNSET):
        """contact_id omitted -> map to self.cid; contact_id=None -> CLEAR.
        The two must be distinguishable: None is a meaningful value here (it is
        how the API clears a mapping), so it cannot double as "not supplied"."""
        body = {"speaker_id": speaker,
                "contact_id": (self.cid if contact_id is self._UNSET
                               else contact_id)}
        return parse(call(api.set_participant, event(
            "PUT", "/recordings/participants/{key+}", key=KEY, body=body)))

    def test_map_speaker_to_contact(self):
        status, body = self._set()
        self.assertEqual(status, 200)
        self.assertEqual(body["participant"]["contact_id"], self.cid)
        self.assertEqual(body["participant"]["contact"]["name"], "Rahul Sharma")

    def test_transcript_is_never_mutated(self):
        self._set()
        row = self.t["recordings"].items[(KEY,)]
        self.assertEqual(row["transcript"], TRANSCRIPT)
        # Diarization labels stay "0"/"1" in the segments forever.
        self.assertEqual([s["speaker"] for s in row["timestamps"]], ["0", "1"])

    def test_speaker_names_map_is_synced_and_version_bumped(self):
        """The existing display mechanism must follow the mapping, or every AI
        document would keep saying "Speaker 0"."""
        self._set()
        row = self.t["recordings"].items[(KEY,)]
        self.assertEqual(row["speaker_names"]["0"], "Rahul Sharma")
        self.assertEqual(row["speaker_mapping_version"], 1)

    def test_remap_replaces_not_duplicates(self):
        self._set()
        _, other = self.mk_contact(name="Neha Shah", email="neha@company.com")
        self._set(contact_id=other["contact"]["id"])
        rows = [i for i in self.t["participants"].items.values()
                if i["audio_s3_key"] == KEY and i["speaker_id"] == "0"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["contact_id"], other["contact"]["id"])

    def test_clear_mapping(self):
        self._set()
        status, body = self._set(contact_id=None)
        self.assertEqual(status, 200)
        self.assertTrue(body["cleared"])
        self.assertEqual(len(self.t["participants"].items), 0)
        self.assertNotIn("0", self.t["recordings"].items[(KEY,)]["speaker_names"])

    def test_invalid_contact_rejected(self):
        status, _ = self._set(contact_id="c-nope")
        self.assertEqual(status, 404)
        self.assertEqual(len(self.t["participants"].items), 0)

    def test_cannot_map_another_users_contact(self):
        self.t["contacts"].put_item(Item={
            "contact_id": "c-other", "owner_user_id": OTHER, "name": "Theirs",
            "created_at": "2026-01-01T00:00:00Z"})
        status, _ = self._set(contact_id="c-other")
        self.assertEqual(status, 404)

    def test_speaker_id_required(self):
        status, _ = parse(call(api.set_participant, event(
            "PUT", "/recordings/participants/{key+}", key=KEY,
            body={"contact_id": self.cid})))
        self.assertEqual(status, 400)

    def test_cross_user_recording_is_404(self):
        with mock.patch.object(api, "_require_auth", return_value=OTHER):
            status, _ = parse(call(api.list_participants, event(
                "GET", "/recordings/participants/{key+}", key=KEY)))
            self.assertEqual(status, 404)


# ===========================================================================
# TASKS
# ===========================================================================
class TestTasks(OrgTestCase):
    def test_create_manual_task(self):
        status, body = parse(call(api.create_meeting_task, event(
            "POST", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"task": "Draft the SOW", "priority": "High"})))
        self.assertEqual(status, 201)
        self.assertEqual(body["task"]["task"], "Draft the SOW")
        self.assertEqual(body["task"]["priority"], "High")
        self.assertEqual(body["task"]["status"], "Open")
        self.assertEqual(body["task"]["source_type"], "MANUAL")
        self.assertEqual(body["task"]["resolution_status"], "NONE")

    def test_manual_task_on_a_meeting_the_ai_found_nothing_in(self):
        """The reported situation: a short or silent recording yields no
        ai_tasks, so the meeting shows no tasks at all. The person who recorded
        it must still be able to write one down — the AI is deliberately
        conservative and misses real commitments, and on an empty transcript it
        extracts nothing."""
        row = self.t["recordings"].items[(KEY,)]
        row.pop("ai_tasks", None)
        row["transcript"] = ""

        _, before = parse(call(api.list_meeting_tasks, event(
            "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
        self.assertEqual(before["count"], 0)

        status, created = parse(call(api.create_meeting_task, event(
            "POST", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"task": "Chase the signed contract", "due": "Friday",
                  "priority": "High"})))
        self.assertEqual(status, 201)
        self.assertEqual(created["task"]["source_type"], "MANUAL")
        # No assignee is a normal state for a task you jotted down, not an error.
        self.assertEqual(created["task"]["resolution_status"], "NONE")

        _, after = parse(call(api.list_meeting_tasks, event(
            "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
        self.assertEqual(after["count"], 1)

    def test_manual_task_survives_ai_seeding(self):
        """A hand-written task must not be wiped or duplicated when the AI's
        own tasks are later seeded into the same meeting."""
        status, created = parse(call(api.create_meeting_task, event(
            "POST", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"task": "Hand-written task"})))
        self.assertEqual(status, 201)
        tid = created["task"]["id"]

        # Now the AI's extraction lands (a reprocess) and seeding runs.
        _, body = parse(call(api.list_meeting_tasks, event(
            "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
        titles = [t["task"] for t in body["tasks"]]
        self.assertIn("Hand-written task", titles)
        self.assertEqual(titles.count("Hand-written task"), 1)
        self.assertIn(tid, [t["id"] for t in body["tasks"]])

    def test_manual_task_with_contact_is_resolved(self):
        """Assigning at creation time produces a RESOLVED task, so it can be
        filtered by assignee and is notification-ready."""
        self.add_user("u-456", "rahul@company.com")
        _, c = self.mk_contact()
        status, created = parse(call(api.create_meeting_task, event(
            "POST", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"task": "Send revised quote",
                  "assignee_contact_id": c["contact"]["id"]})))
        self.assertEqual(status, 201)
        self.assertEqual(created["task"]["resolution_status"], "RESOLVED")
        self.assertEqual(created["task"]["assignee_name"], "Rahul Sharma")
        self.assertEqual(created["task"]["assignee_user_id"], "u-456")

    def test_task_text_required(self):
        status, _ = parse(call(api.create_meeting_task, event(
            "POST", "/recordings/ai/tasks/{key+}", key=KEY, body={})))
        self.assertEqual(status, 400)

    def test_task_is_mirrored_to_recording_row(self):
        """Dual-write: the legacy embedded map must stay in step."""
        _, body = parse(call(api.create_meeting_task, event(
            "POST", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"task": "Mirrored task"})))
        tid = body["task"]["id"]
        mirror = self.t["recordings"].items[(KEY,)]["tasks"][tid]
        self.assertEqual(mirror["task"], "Mirrored task")
        self.assertEqual(mirror["task_id"], tid)

    def test_status_transitions_stamp_completed_at(self):
        _, created = parse(call(api.create_meeting_task, event(
            "POST", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"task": "Finish me"})))
        tid = created["task"]["id"]

        _, done = parse(call(api.update_meeting_task, event(
            "PATCH", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"id": tid, "status": "Completed"})))
        self.assertEqual(done["task"]["status"], "Completed")
        self.assertTrue(done["task"]["completed_at"])

        _, reopened = parse(call(api.update_meeting_task, event(
            "PATCH", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"id": tid, "status": "Open"})))
        self.assertEqual(reopened["task"]["completed_at"], "")

    def test_cancelled_status_accepted(self):
        _, created = parse(call(api.create_meeting_task, event(
            "POST", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"task": "Drop me"})))
        _, body = parse(call(api.update_meeting_task, event(
            "PATCH", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"id": created["task"]["id"], "status": "CANCELLED"})))
        self.assertEqual(body["task"]["status"], "Cancelled")

    def test_bad_status_rejected(self):
        _, created = parse(call(api.create_meeting_task, event(
            "POST", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"task": "x"})))
        status, _ = parse(call(api.update_meeting_task, event(
            "PATCH", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"id": created["task"]["id"], "status": "Sideways"})))
        self.assertEqual(status, 400)

    def test_overdue_is_computed_not_stored(self):
        self.assertTrue(api._is_overdue("2020-01-01", "Open"))
        self.assertFalse(api._is_overdue("2020-01-01", "Completed"))
        self.assertFalse(api._is_overdue("2099-01-01", "Open"))
        self.assertFalse(api._is_overdue("", "Open"))
        # Unparseable AI phrasing must not crash or claim overdue.
        self.assertFalse(api._is_overdue("next Friday", "Open"))
        # A bare date is end-of-day, so "due today" is not overdue.
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.assertFalse(api._is_overdue(today, "Open"))
        # Nothing persisted an is_overdue flag.
        for row in self.t["tasks"].items.values():
            self.assertNotIn("is_overdue", row)

    def test_assign_to_contact_resolves_and_links_user(self):
        self.add_user("u-456", "rahul@company.com")
        _, c = self.mk_contact()
        cid = c["contact"]["id"]
        _, created = parse(call(api.create_meeting_task, event(
            "POST", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"task": "Send proposal", "assignee_contact_id": cid})))
        self.assertEqual(created["task"]["resolution_status"], "RESOLVED")
        self.assertEqual(created["task"]["assignee_contact_id"], cid)
        self.assertEqual(created["task"]["assignee_user_id"], "u-456")

    def test_assignee_without_account_has_no_user_id(self):
        _, c = self.mk_contact()
        _, created = parse(call(api.create_meeting_task, event(
            "POST", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"task": "Send proposal",
                  "assignee_contact_id": c["contact"]["id"]})))
        self.assertEqual(created["task"]["assignee_user_id"], "")
        self.assertEqual(created["task"]["resolution_status"], "RESOLVED")

    def test_legacy_assignee_name_stays_unresolved(self):
        """A NAME is not an identity, even from a trusted client."""
        _, created = parse(call(api.create_meeting_task, event(
            "POST", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"task": "Send proposal", "assignee": {"name": "Rahul"}})))
        self.assertEqual(created["task"]["resolution_status"], "UNRESOLVED")
        self.assertEqual(created["task"]["assignee_name_legacy"], "Rahul")
        self.assertEqual(created["task"]["assignee_contact_id"], "")

    def test_cannot_assign_another_users_contact(self):
        self.t["contacts"].put_item(Item={
            "contact_id": "c-other", "owner_user_id": OTHER, "name": "Theirs",
            "created_at": "2026-01-01T00:00:00Z"})
        status, _ = parse(call(api.create_meeting_task, event(
            "POST", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"task": "x", "assignee_contact_id": "c-other"})))
        self.assertEqual(status, 404)

    def test_notify_channels_accumulate(self):
        _, created = parse(call(api.create_meeting_task, event(
            "POST", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"task": "Notify me"})))
        tid = created["task"]["id"]
        parse(call(api.update_meeting_task, event(
            "PATCH", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"id": tid, "notify_channels": ["email"]})))
        _, body = parse(call(api.update_meeting_task, event(
            "PATCH", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"id": tid, "notify_channels": ["sms"]})))
        self.assertEqual(body["task"]["notified_via"], ["email", "sms"])

    def test_delete_task_removes_row_and_mirror(self):
        _, created = parse(call(api.create_meeting_task, event(
            "POST", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"task": "Delete me"})))
        tid = created["task"]["id"]
        status, _ = parse(call(api.delete_meeting_task, event(
            "DELETE", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"id": tid})))
        self.assertEqual(status, 200)
        self.assertNotIn((tid,), self.t["tasks"].items)
        self.assertNotIn(tid, self.t["recordings"].items[(KEY,)].get("tasks", {}))

    def test_delete_unknown_task_is_404(self):
        status, _ = parse(call(api.delete_meeting_task, event(
            "DELETE", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"id": "nope"})))
        self.assertEqual(status, 404)

    def test_max_tasks_enforced(self):
        with mock.patch.object(api, "MAX_TASKS", 2):
            for i in range(2):
                parse(call(api.create_meeting_task, event(
                    "POST", "/recordings/ai/tasks/{key+}", key=KEY,
                    body={"task": f"t{i}"})))
            status, _ = parse(call(api.create_meeting_task, event(
                "POST", "/recordings/ai/tasks/{key+}", key=KEY,
                body={"task": "one too many"})))
            self.assertEqual(status, 400)


class TestTaskQueries(OrgTestCase):
    """The cross-meeting Task Tracker read side."""

    def setUp(self):
        super().setUp()
        _, c = self.mk_contact()
        self.cid = c["contact"]["id"]
        self.open_id = parse(call(api.create_meeting_task, event(
            "POST", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"task": "Open one", "assignee_contact_id": self.cid,
                  "due": "2020-01-01"})))[1]["task"]["id"]
        self.done_id = parse(call(api.create_meeting_task, event(
            "POST", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"task": "Done one", "status": "Completed"})))[1]["task"]["id"]

    def test_list_all_tasks(self):
        status, body = parse(call(api.list_all_tasks, event("GET", "/tasks")))
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 2)

    def test_filter_by_status(self):
        _, body = parse(call(api.list_all_tasks, event(
            "GET", "/tasks", qs={"status": "Open"})))
        self.assertEqual([t["id"] for t in body["tasks"]], [self.open_id])

    def test_filter_by_assignee(self):
        _, body = parse(call(api.list_all_tasks, event(
            "GET", "/tasks", qs={"assignee_contact_id": self.cid})))
        self.assertEqual([t["id"] for t in body["tasks"]], [self.open_id])

    def test_filter_overdue(self):
        _, body = parse(call(api.list_all_tasks, event(
            "GET", "/tasks", qs={"overdue": "true"})))
        self.assertEqual([t["id"] for t in body["tasks"]], [self.open_id])

    def test_other_users_tasks_never_listed(self):
        """Even on an index NOT keyed by owner, every row is re-checked."""
        self.t["tasks"].put_item(Item={
            "task_id": "t-other", "owner_user_id": OTHER,
            "title": "Their secret task", "status": "Open",
            "source_recording_id": KEY,
            "created_at": "2026-08-01T00:00:00Z"})
        for qs in ({}, {"recording_key": KEY}):
            _, body = parse(call(api.list_all_tasks,
                                 event("GET", "/tasks", qs=qs)))
            self.assertNotIn("Their secret task", json.dumps(body))

    def test_pagination_reaches_every_task(self):
        """A page-sized result must hand back a usable cursor.

        Regression: next_cursor was emitted as "" whenever the results
        OVERFLOWED the page, so a client with 7 matching tasks and limit=3 saw
        three and stopped — the other four unreachable, with no error. A task
        tracker that quietly hides work is worse than one that fails loudly.
        """
        for i in range(7):
            self.t["tasks"].put_item(Item={
                "task_id": f"pg-{i}", "owner_user_id": USER,
                "title": f"Paged {i}", "status": "Open", "priority": "Medium",
                "due_date": "", "created_at": f"2026-09-{i + 1:02d}T00:00:00Z"})

        seen, cursor, pages = [], "", 0
        while pages < 12:
            qs = {"limit": "3", "status": "Open"}
            if cursor:
                qs["cursor"] = cursor
            _, body = parse(call(api.list_all_tasks,
                                 event("GET", "/tasks", qs=qs)))
            seen += [t["id"] for t in body["tasks"]]
            cursor = body["next_cursor"]
            pages += 1
            if not cursor:
                break

        self.assertFalse(cursor, "paging did not terminate")
        # Every paged task is reachable...
        for i in range(7):
            self.assertIn(f"pg-{i}", seen)
        # ...exactly once. A cursor that resumed from the wrong row would
        # either skip rows or repeat them.
        self.assertEqual(len(seen), len(set(seen)), f"duplicates: {seen}")

    def test_pagination_on_a_filtered_index(self):
        """Same guarantee when the query runs on the folder index, whose
        cursor needs the index's own key attributes, not just task_id."""
        for i in range(5):
            self.t["tasks"].put_item(Item={
                "task_id": f"fp-{i}", "owner_user_id": USER,
                "title": f"Assignee paged {i}", "status": "Open",
                "priority": "Medium", "due_date": "",
                "assignee_contact_id": self.cid,
                "created_at": f"2026-10-{i + 1:02d}T00:00:00Z"})
        seen, cursor, pages = [], "", 0
        while pages < 12:
            qs = {"limit": "2", "assignee_contact_id": self.cid}
            if cursor:
                qs["cursor"] = cursor
            _, body = parse(call(api.list_all_tasks,
                                 event("GET", "/tasks", qs=qs)))
            seen += [t["id"] for t in body["tasks"]]
            cursor = body["next_cursor"]
            pages += 1
            if not cursor:
                break
        self.assertFalse(cursor, "paging did not terminate")
        for i in range(5):
            self.assertIn(f"fp-{i}", seen)
        self.assertEqual(len(seen), len(set(seen)), f"duplicates: {seen}")

    def test_get_task_detail_includes_relations(self):
        status, body = parse(call(api.get_task, event(
            "GET", "/tasks/{task_id}", path={"task_id": self.open_id})))
        self.assertEqual(status, 200)
        self.assertEqual(body["contact"]["id"], self.cid)
        self.assertEqual(body["recording"]["audio_s3_key"], KEY)

    def test_get_task_cross_user_is_404(self):
        with mock.patch.object(api, "_require_auth", return_value=OTHER):
            status, _ = parse(call(api.get_task, event(
                "GET", "/tasks/{task_id}", path={"task_id": self.open_id})))
            self.assertEqual(status, 404)

    def test_patch_task_by_id(self):
        status, body = parse(call(api.update_task_v2, event(
            "PATCH", "/tasks/{task_id}", path={"task_id": self.open_id},
            body={"status": "In Progress"})))
        self.assertEqual(status, 200)
        self.assertEqual(body["task"]["status"], "In Progress")

    def test_patch_task_cross_user_is_404(self):
        with mock.patch.object(api, "_require_auth", return_value=OTHER):
            status, _ = parse(call(api.update_task_v2, event(
                "PATCH", "/tasks/{task_id}", path={"task_id": self.open_id},
                body={"status": "Completed"})))
            self.assertEqual(status, 404)


# ===========================================================================
# AI TASK SEEDING + RESOLUTION
# ===========================================================================
class TestAiTasks(OrgTestCase):
    def test_ai_tasks_seed_as_first_class_tasks(self):
        status, body = parse(call(api.list_meeting_tasks, event(
            "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 1)
        task = body["tasks"][0]
        self.assertEqual(task["task"], "Send proposal")
        self.assertEqual(task["source_type"], "AI")
        self.assertTrue(task["from_action_item"])
        # The AI gave a NAME. It stays unresolved.
        self.assertEqual(task["resolution_status"], "UNRESOLVED")
        self.assertEqual(task["assignee_name_legacy"], "Rahul")
        self.assertEqual(task["assignee_contact_id"], "")

    def test_seeding_is_idempotent(self):
        for _ in range(3):
            parse(call(api.list_meeting_tasks, event(
                "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
        self.assertEqual(len(self.t["tasks"].items), 1)

    def test_duplicate_ai_run_creates_nothing(self):
        """A reprocess / retried invocation must not double the tasks."""
        parse(call(api.list_meeting_tasks, event(
            "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
        created = api._seed_ai_tasks(USER, KEY,
                                    self.t["recordings"].items[(KEY,)])
        self.assertEqual(created, 0)
        self.assertEqual(len(self.t["tasks"].items), 1)

    def test_deleted_seeded_task_stays_deleted(self):
        """Fingerprint dedupe is stronger than the old is-the-map-empty check:
        a task the user deleted must not reappear on the next read."""
        _, body = parse(call(api.list_meeting_tasks, event(
            "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
        tid = body["tasks"][0]["id"]
        parse(call(api.delete_meeting_task, event(
            "DELETE", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"id": tid})))
        _, after = parse(call(api.list_meeting_tasks, event(
            "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
        self.assertEqual(after["count"], 0)

    def test_deletion_tombstone_is_recorded_and_bounded(self):
        """The tombstone is what makes a deletion stick. Verify it is written,
        and that it cannot grow the recording row without limit."""
        _, body = parse(call(api.list_meeting_tasks, event(
            "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
        tid = body["tasks"][0]["id"]
        fp = self.t["tasks"].items[(tid,)]["fingerprint"]
        parse(call(api.delete_meeting_task, event(
            "DELETE", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"id": tid})))
        row = self.t["recordings"].items[(KEY,)]
        self.assertIn(fp, row[api.DELETED_TASK_FINGERPRINTS_ATTR])

        with mock.patch.object(api, "MAX_DELETED_FINGERPRINTS", 3):
            for i in range(6):
                api._tombstone_fingerprint(
                    KEY, self.t["recordings"].items[(KEY,)], f"fp-{i}")
            kept = self.t["recordings"].items[(KEY,)][
                api.DELETED_TASK_FINGERPRINTS_ATTR]
            self.assertEqual(len(kept), 3)
            self.assertEqual(kept, ["fp-3", "fp-4", "fp-5"])

    def test_manual_task_deletion_leaves_no_tombstone(self):
        """Only AI-seeded tasks carry a fingerprint, so only they need one — a
        manual task cannot be re-seeded and must not consume the bounded list."""
        _, created = parse(call(api.create_meeting_task, event(
            "POST", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"task": "Manual one"})))
        parse(call(api.delete_meeting_task, event(
            "DELETE", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"id": created["task"]["id"]})))
        row = self.t["recordings"].items[(KEY,)]
        self.assertEqual(row.get(api.DELETED_TASK_FINGERPRINTS_ATTR, []), [])

    def test_identical_text_in_two_meetings_is_two_tasks(self):
        self.t["recordings"].put_item(Item={
            **json.loads(json.dumps(BASE_RECORDING)),
            "audio_s3_key": KEY2, "created_at": "2026-08-05T00:00:00Z"})
        for key in (KEY, KEY2):
            parse(call(api.list_meeting_tasks, event(
                "GET", "/recordings/ai/tasks/{key+}", key=key)))
        self.assertEqual(len(self.t["tasks"].items), 2)

    def test_speaker_mapping_resolves_ai_task(self):
        """The full section 18 chain: speaker -> contact -> user."""
        self.add_user("u-456", "rahul@company.com")
        self.t["recordings"].items[(KEY,)]["ai_tasks"] = [
            {"task": "Send proposal", "assignee": "Rahul",
             "assignee_speaker_id": "0", "due_date": "2026-08-21"}]
        _, seeded = parse(call(api.list_meeting_tasks, event(
            "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
        tid = seeded["tasks"][0]["id"]
        self.assertEqual(seeded["tasks"][0]["resolution_status"], "UNRESOLVED")

        _, c = self.mk_contact()
        cid = c["contact"]["id"]
        _, mapped = parse(call(api.set_participant, event(
            "PUT", "/recordings/participants/{key+}", key=KEY,
            body={"speaker_id": "0", "contact_id": cid})))
        self.assertEqual(mapped["tasks_resolved"], 1)

        row = self.t["tasks"].items[(tid,)]
        self.assertEqual(row["resolution_status"], "RESOLVED")
        self.assertEqual(row["assignee_contact_id"], cid)
        self.assertEqual(row["assignee_user_id"], "u-456")
        self.assertNotIn("assignee_name_legacy", row)

    def test_speaker_mapping_does_not_override_manual_assignment(self):
        _, a = self.mk_contact(name="Rahul Sharma", email="rahul@company.com")
        _, b = self.mk_contact(name="Neha Shah", email="neha@company.com")
        self.t["recordings"].items[(KEY,)]["ai_tasks"] = [
            {"task": "Send proposal", "assignee_speaker_id": "0"}]
        _, seeded = parse(call(api.list_meeting_tasks, event(
            "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
        tid = seeded["tasks"][0]["id"]
        # The user hand-assigns to Neha.
        parse(call(api.update_meeting_task, event(
            "PATCH", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"id": tid, "assignee_contact_id": b["contact"]["id"]})))
        # Mapping speaker 0 to Rahul must not steal Neha's task.
        parse(call(api.set_participant, event(
            "PUT", "/recordings/participants/{key+}", key=KEY,
            body={"speaker_id": "0", "contact_id": a["contact"]["id"]})))
        self.assertEqual(self.t["tasks"].items[(tid,)]["assignee_contact_id"],
                         b["contact"]["id"])

    def test_seeding_uses_existing_mapping_immediately(self):
        _, c = self.mk_contact()
        cid = c["contact"]["id"]
        parse(call(api.set_participant, event(
            "PUT", "/recordings/participants/{key+}", key=KEY,
            body={"speaker_id": "0", "contact_id": cid})))
        self.t["recordings"].items[(KEY,)]["ai_tasks"] = [
            {"task": "Send proposal", "assignee_speaker_id": "0"}]
        _, body = parse(call(api.list_meeting_tasks, event(
            "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
        self.assertEqual(body["tasks"][0]["resolution_status"], "RESOLVED")
        self.assertEqual(body["tasks"][0]["assignee_contact_id"], cid)


class TestRenamedSpeakerReachesTasks(OrgTestCase):
    """A rename must reach the tasks a speaker owns.

    The rename lands in ONE place — `speaker_names` on the recording — and the
    task stores the join key (`assignee_speaker_id`). The display name is
    resolved at READ time from those two, so every task the speaker owns reads
    correctly on the next request without the rename writing to a single task
    row. That is what these tests pin: the name moves, nothing is written, and
    a task a human assigned is never re-pointed.
    """

    def _ai_task_from_speaker(self, speaker_id="0"):
        """Re-seed the meeting with an AI task pinned to a SPEAKER rather than
        a spoken name — the case a rename has to reach."""
        self.t["recordings"].items[(KEY,)]["ai_tasks"] = [
            {"task": "Send revised quote",
             "assignee": f"Speaker {speaker_id}",
             "assignee_speaker_id": speaker_id,
             "due_date": "2026-08-21", "priority": "High"}]

    def _rename(self, names):
        """Rename in the STORE, not on a snapshot: the fake table deep-copies
        on get_item, so mutating a previously-read row would change nothing
        that a later request can see."""
        self.t["recordings"].items[(KEY,)]["speaker_names"] = names

    def _list(self):
        status, body = parse(call(api.list_meeting_tasks, event(
            "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
        self.assertEqual(status, 200)
        return body["tasks"]

    def test_an_unnamed_speaker_reads_as_its_label(self):
        self._ai_task_from_speaker()
        t = self._list()[0]
        self.assertEqual(t["speaker_name"], "Speaker 0")
        self.assertEqual(t["assignee"]["name"], "Speaker 0")

    def test_renaming_the_speaker_renames_the_task_assignee(self):
        """The bug this fixes: the transcript, participants and documents all
        picked a rename up, and the task went on saying "Speaker 0"."""
        self._ai_task_from_speaker()
        self._list()                          # seed while still unnamed
        tasks_before = copy.deepcopy(self.t["tasks"].items)

        self._rename({"0": "Ravi"})
        t = self._list()[0]
        self.assertEqual(t["speaker_name"], "Ravi")
        self.assertEqual(t["assignee"]["name"], "Ravi")

        # NOTHING was written to reach that name — the whole design.
        self.assertEqual(self.t["tasks"].items, tasks_before)

    def test_the_stored_extraction_is_untouched_by_a_rename(self):
        """Resolution is DISPLAY only: the string the AI produced stays on the
        row, so the extraction remains inspectable and the rename reversible."""
        self._ai_task_from_speaker()
        self._list()
        self._rename({"0": "Ravi"})
        t = self._list()[0]
        self.assertEqual(t["assignee_name_legacy"], "Speaker 0")
        self.assertEqual(t["assignee_speaker_id"], "0")

    def test_clearing_the_name_puts_the_label_back(self):
        self._ai_task_from_speaker()
        self._rename({"0": "Ravi"})
        self.assertEqual(self._list()[0]["assignee"]["name"], "Ravi")
        self._rename({})
        self.assertEqual(self._list()[0]["assignee"]["name"], "Speaker 0")

    def test_renaming_again_moves_it_again(self):
        self._ai_task_from_speaker()
        self._rename({"0": "Ravi"})
        self.assertEqual(self._list()[0]["assignee"]["name"], "Ravi")
        self._rename({"0": "Rahul Verma"})
        self.assertEqual(self._list()[0]["assignee"]["name"], "Rahul Verma")

    def test_a_rename_never_repoints_a_hand_assigned_task(self):
        """The guard that matters most. Once a human has said who owns a task,
        renaming the speaker who happened to say the sentence must not move it
        — the same rule _resolve_tasks_for_speaker applies when it skips
        already-resolved tasks."""
        self.add_user("u-456", "rahul@company.com")
        _, c = parse(call(api.create_contact, event(
            "POST", "/contacts", body={"name": "Priya Sharma",
                                       "email": "priya@company.com"})))
        contact_id = c["contact"]["id"]

        self._ai_task_from_speaker()
        task_id = self._list()[0]["id"]
        status, _ = parse(call(api.resolve_task_assignee, event(
            "POST", "/tasks/{task_id}/resolve",
            path={"task_id": task_id}, body={"contact_id": contact_id})))
        self.assertEqual(status, 200)

        self._rename({"0": "Ravi"})
        t = self._list()[0]
        self.assertEqual(t["assignee"]["name"], "Priya Sharma")
        self.assertEqual(t["resolution_status"], "RESOLVED")
        # Provenance is still reported, so the UI can say where it came from.
        self.assertEqual(t["speaker_name"], "Ravi")

    def test_a_task_naming_a_real_person_is_not_a_speaker_task(self):
        """The default fixture's AI task names "Rahul", not a label — a rename
        of any speaker must leave it completely alone."""
        self._rename({"0": "Ravi"})
        t = self._list()[0]
        self.assertEqual(t["assignee_name_legacy"], "Rahul")
        self.assertEqual(t["assignee"]["name"], "Rahul")
        self.assertEqual(t["speaker_name"], "")

    def test_the_cross_meeting_tracker_resolves_too(self):
        """GET /tasks has no recording in hand, so it must read speaker_names
        per meeting itself — otherwise the Task Tracker would be the one screen
        still showing the old label."""
        self._ai_task_from_speaker()
        self._list()
        self._rename({"0": "Ravi"})
        status, body = parse(call(api.list_all_tasks, event("GET", "/tasks")))
        self.assertEqual(status, 200)
        names = [t["assignee"]["name"] for t in body["tasks"]
                 if t["assignee"]]
        self.assertIn("Ravi", names)

    def test_the_task_detail_route_resolves_and_returns_the_map(self):
        self._ai_task_from_speaker()
        task_id = self._list()[0]["id"]
        self._rename({"0": "Ravi"})
        status, body = parse(call(api.get_task, event(
            "GET", "/tasks/{task_id}", path={"task_id": task_id})))
        self.assertEqual(status, 200)
        self.assertEqual(body["task"]["speaker_name"], "Ravi")
        self.assertEqual(body["task"]["assignee"]["name"], "Ravi")
        # Handed to the detail screen so it can name the source speaker
        # without a second participants call.
        self.assertEqual(body["recording"]["speaker_names"], {"0": "Ravi"})


class TestAmbiguityResolution(OrgTestCase):
    def test_two_rahuls_are_reported_ambiguous(self):
        self.mk_contact(name="Rahul Sharma", email="rahul.sharma@x.com")
        self.mk_contact(name="Rahul Patil", email="rahul.patil@x.com")
        _, body = parse(call(api.list_meeting_tasks, event(
            "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
        tid = body["tasks"][0]["id"]

        status, cands = parse(call(api.suggest_task_assignees, event(
            "GET", "/tasks/{task_id}/assignee-candidates",
            path={"task_id": tid})))
        self.assertEqual(status, 200)
        # "Rahul" matches neither full name exactly -> nothing to offer,
        # which is still better than guessing.
        self.assertIn(cands["status"], ("none", "ambiguous"))
        self.assertEqual(cands["searched_name"], "Rahul")

    def test_single_name_match_is_still_ambiguous_not_auto_assigned(self):
        self.mk_contact(name="Rahul", email="rahul@x.com")
        _, body = parse(call(api.list_meeting_tasks, event(
            "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
        tid = body["tasks"][0]["id"]
        _, cands = parse(call(api.suggest_task_assignees, event(
            "GET", "/tasks/{task_id}/assignee-candidates",
            path={"task_id": tid})))
        self.assertEqual(cands["status"], "ambiguous")
        self.assertEqual(len(cands["candidates"]), 1)
        # Crucially: the task itself was NOT assigned.
        self.assertEqual(self.t["tasks"].items[(tid,)]["resolution_status"],
                         "UNRESOLVED")

    def test_resolve_assigns_chosen_contact(self):
        self.add_user("u-456", "rahul@company.com")
        _, c = self.mk_contact()
        _, body = parse(call(api.list_meeting_tasks, event(
            "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
        tid = body["tasks"][0]["id"]
        status, resolved = parse(call(api.resolve_task_assignee, event(
            "POST", "/tasks/{task_id}/resolve", path={"task_id": tid},
            body={"contact_id": c["contact"]["id"]})))
        self.assertEqual(status, 200)
        self.assertEqual(resolved["task"]["resolution_status"], "RESOLVED")
        self.assertEqual(resolved["task"]["assignee_user_id"], "u-456")

    def test_resolve_with_another_users_contact_is_404(self):
        self.t["contacts"].put_item(Item={
            "contact_id": "c-other", "owner_user_id": OTHER, "name": "T",
            "created_at": "2026-01-01T00:00:00Z"})
        _, body = parse(call(api.list_meeting_tasks, event(
            "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
        tid = body["tasks"][0]["id"]
        status, _ = parse(call(api.resolve_task_assignee, event(
            "POST", "/tasks/{task_id}/resolve", path={"task_id": tid},
            body={"contact_id": "c-other"})))
        self.assertEqual(status, 404)


# ===========================================================================
# MIGRATION
# ===========================================================================
class TestMigration(OrgTestCase):
    LEGACY = {
        "legacy-1": {"task": "Chase the vendor", "due": "Friday",
                     "priority": "High", "status": "In Progress",
                     "assignee": {"name": "Rahul", "source": "manual"},
                     "notified_via": ["email"],
                     "created_at": "2026-07-01T10:00:00Z",
                     "updated_at": "2026-07-02T10:00:00Z",
                     "from_action_item": False},
        "legacy-2": {"task": "Book the site visit", "due": "",
                     "priority": "Medium", "status": "Open",
                     "assignee": None, "notified_via": [],
                     "created_at": "2026-07-01T11:00:00Z",
                     "updated_at": "2026-07-01T11:00:00Z",
                     "from_action_item": True},
    }

    def setUp(self):
        super().setUp()
        row = self.t["recordings"].items[(KEY,)]
        row["tasks"] = json.loads(json.dumps(self.LEGACY))
        # No ai_tasks, so this test class isolates migration from seeding.
        row.pop("ai_tasks", None)

    def test_legacy_tasks_are_migrated_on_read(self):
        status, body = parse(call(api.list_meeting_tasks, event(
            "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 2)
        titles = sorted(t["task"] for t in body["tasks"])
        self.assertEqual(titles, ["Book the site visit", "Chase the vendor"])

    def test_no_task_is_lost(self):
        parse(call(api.list_meeting_tasks, event(
            "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
        self.assertEqual(len(self.t["tasks"].items), len(self.LEGACY))

    def test_original_fields_and_timestamps_preserved(self):
        _, body = parse(call(api.list_meeting_tasks, event(
            "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
        by_title = {t["task"]: t for t in body["tasks"]}
        chase = by_title["Chase the vendor"]
        self.assertEqual(chase["status"], "In Progress")
        self.assertEqual(chase["priority"], "High")
        self.assertEqual(chase["due"], "Friday")
        self.assertEqual(chase["notified_via"], ["email"])
        self.assertEqual(chase["created_at"], "2026-07-01T10:00:00Z")

    def test_legacy_assignee_is_unresolved_never_guessed(self):
        """Even with exactly one matching contact, the name is NOT resolved."""
        self.mk_contact(name="Rahul", email="rahul@company.com")
        _, body = parse(call(api.list_meeting_tasks, event(
            "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
        chase = [t for t in body["tasks"] if t["task"] == "Chase the vendor"][0]
        self.assertEqual(chase["resolution_status"], "UNRESOLVED")
        self.assertEqual(chase["assignee_name_legacy"], "Rahul")
        self.assertEqual(chase["assignee_contact_id"], "")

    def test_source_type_distinguishes_ai_from_manual(self):
        _, body = parse(call(api.list_meeting_tasks, event(
            "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
        by_title = {t["task"]: t for t in body["tasks"]}
        self.assertEqual(by_title["Chase the vendor"]["source_type"], "LEGACY")
        self.assertEqual(by_title["Book the site visit"]["source_type"], "AI")

    def test_migration_is_idempotent(self):
        for _ in range(4):
            parse(call(api.list_meeting_tasks, event(
                "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
        self.assertEqual(len(self.t["tasks"].items), len(self.LEGACY))

    def test_embedded_map_is_never_destroyed(self):
        """The legacy store stays intact as the rollback path."""
        parse(call(api.list_meeting_tasks, event(
            "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
        stored = self.t["recordings"].items[(KEY,)]["tasks"]
        for legacy_id in self.LEGACY:
            self.assertIn(legacy_id, stored)
            self.assertEqual(stored[legacy_id]["task"],
                             self.LEGACY[legacy_id]["task"])

    def test_legacy_id_still_works_for_updates(self):
        """A client holding an id from the OLD API must keep working."""
        parse(call(api.list_meeting_tasks, event(
            "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
        status, body = parse(call(api.update_meeting_task, event(
            "PATCH", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"id": "legacy-1", "status": "Completed"})))
        self.assertEqual(status, 200)
        self.assertEqual(body["task"]["status"], "Completed")

    def test_malformed_entries_are_skipped_not_fatal(self):
        self.t["recordings"].items[(KEY,)]["tasks"]["bad-1"] = "not a dict"
        self.t["recordings"].items[(KEY,)]["tasks"]["bad-2"] = {"task": "   "}
        status, body = parse(call(api.list_meeting_tasks, event(
            "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 2)

    def test_migration_and_ai_seeding_do_not_both_create_the_same_task(self):
        """THE bug real data exposed: 5 tasks read back as 10.

        The embedded map was itself seeded from `ai_tasks` long ago, so on a
        real row BOTH paths run: migration (idempotent on legacy_task_id) and
        AI seeding (idempotent on fingerprint). Neither could see the other's
        rows, so each created its own copy of every task.

        The unit tests missed it because TestMigration removes `ai_tasks` to
        isolate migration — which is exactly the condition that hides it. This
        test keeps both, like production.
        """
        row = self.t["recordings"].items[(KEY,)]
        row["ai_tasks"] = [
            {"task": "Send proposal", "assignee": "Rahul",
             "due_date": "2026-08-21", "priority": "High"},
            {"task": "Book the site visit", "assignee": "",
             "due_date": "", "priority": "Medium"},
        ]
        # The embedded map as it would look after the OLD client seeded it from
        # those same ai_tasks: same text, same assignee, from_action_item true.
        row["tasks"] = {
            "legacy-a": {"task": "Send proposal", "due": "2026-08-21",
                         "priority": "High", "status": "Open",
                         "assignee": {"name": "Rahul", "source": "manual"},
                         "notified_via": [], "from_action_item": True,
                         "created_at": "2026-07-01T10:00:00Z",
                         "updated_at": "2026-07-01T10:00:00Z"},
            "legacy-b": {"task": "Book the site visit", "due": "",
                         "priority": "Medium", "status": "Open",
                         "assignee": None, "notified_via": [],
                         "from_action_item": True,
                         "created_at": "2026-07-01T11:00:00Z",
                         "updated_at": "2026-07-01T11:00:00Z"},
        }

        _, body = parse(call(api.list_meeting_tasks, event(
            "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
        titles = sorted(t["task"] for t in body["tasks"])
        self.assertEqual(body["count"], 2,
                         f"expected 2 tasks, got {body['count']}: {titles}")
        self.assertEqual(titles, ["Book the site visit", "Send proposal"])

        # Repeat reads stay at 2.
        for _ in range(3):
            _, again = parse(call(api.list_meeting_tasks, event(
                "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
            self.assertEqual(again["count"], 2)

        # The migrated rows won, keeping their original timestamps and ids —
        # migration runs first, and seeding must defer to what it created.
        rows = list(self.t["tasks"].items.values())
        self.assertTrue(all(r.get("legacy_task_id") for r in rows),
                        "seeding created a second copy alongside migration")
        # ...and they carry a fingerprint, which is what makes seeding skip them.
        self.assertTrue(all(r.get("fingerprint") for r in rows),
                        "migrated AI tasks need a fingerprint or seeding "
                        "cannot recognize them")

    def test_reassigned_task_is_not_duplicated(self):
        """The SECOND production bug: a task the user reassigned came back twice.

        The AI extracted the task with assignee "Speaker 2". The user corrected
        it to a real name, and the embedded map recorded that. When the
        fingerprint included the assignee, the migrated row hashed differently
        from the AI original, so the seeder saw no match and created a twin.

        Found on three real meetings. The fingerprint now covers MEETING + TEXT
        only, precisely because the assignee is the field users edit most.
        """
        row = self.t["recordings"].items[(KEY,)]
        row["ai_tasks"] = [
            {"task": "Develop device hardware", "assignee": "Speaker 2",
             "due_date": "", "priority": "Medium"},
        ]
        # The embedded copy the user has since REASSIGNED.
        row["tasks"] = {
            "legacy-reassigned": {
                "task": "Develop device hardware", "due": "",
                "priority": "Medium", "status": "Open",
                "assignee": {"name": "Siddhesh Gawade", "source": "manual"},
                "notified_via": [], "from_action_item": True,
                "created_at": "2026-07-01T10:00:00Z",
                "updated_at": "2026-07-02T10:00:00Z"},
        }

        _, body = parse(call(api.list_meeting_tasks, event(
            "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
        self.assertEqual(body["count"], 1,
                         f"reassigned task duplicated: "
                         f"{[t['task'] for t in body['tasks']]}")
        # The USER'S name survives — the AI's "Speaker 2" must not win.
        self.assertEqual(body["tasks"][0]["assignee_name_legacy"],
                         "Siddhesh Gawade")
        # Stable across repeat reads.
        for _ in range(3):
            _, again = parse(call(api.list_meeting_tasks, event(
                "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
            self.assertEqual(again["count"], 1)

    def test_fingerprint_ignores_the_assignee(self):
        """Stated directly, because this is the property that keeps a
        reassignment from duplicating a task."""
        a = api._task_fingerprint(KEY, "Send proposal", "Speaker 2")
        b = api._task_fingerprint(KEY, "Send proposal", "Rahul Sharma")
        c = api._task_fingerprint(KEY, "Send proposal", "")
        self.assertEqual(a, b)
        self.assertEqual(b, c)
        # Meeting and text still separate tasks.
        self.assertNotEqual(a, api._task_fingerprint(KEY2, "Send proposal"))
        self.assertNotEqual(a, api._task_fingerprint(KEY, "Other task"))

    def test_backfill_script_fingerprint_matches_the_lambda(self):
        """The eager and lazy migrations MUST agree, or running the backfill
        after the lazy path duplicates everything it already migrated."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "backfill32",
            str(Path(__file__).resolve().parents[1] / "scripts"
                / "32_backfill_tasks.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for text, hint in (("Send proposal", "Rahul"),
                           ("  SEND   proposal ", "Speaker 2"),
                           ("Book the site visit", "")):
            self.assertEqual(
                mod.fingerprint(KEY, text, hint),
                api._task_fingerprint(KEY, text, hint),
                f"fingerprint drift on {text!r}")

    def test_manual_legacy_task_gets_no_fingerprint(self):
        """A hand-typed task has no ai_tasks counterpart, so giving it a
        fingerprint could let it suppress a genuine AI task."""
        row = self.t["recordings"].items[(KEY,)]
        row.pop("ai_tasks", None)
        row["tasks"] = {
            "legacy-manual": {"task": "Typed by hand", "due": "",
                              "priority": "Medium", "status": "Open",
                              "assignee": None, "notified_via": [],
                              "from_action_item": False,
                              "created_at": "2026-07-01T10:00:00Z",
                              "updated_at": "2026-07-01T10:00:00Z"},
        }
        parse(call(api.list_meeting_tasks, event(
            "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
        migrated = [r for r in self.t["tasks"].items.values()
                    if r.get("legacy_task_id") == "legacy-manual"]
        self.assertEqual(len(migrated), 1)
        self.assertFalse(migrated[0].get("fingerprint"),
                         "a manual task must not carry an AI fingerprint")
        self.assertEqual(migrated[0]["source_type"], "LEGACY")

    def test_mirror_entries_are_not_re_migrated(self):
        """A task the new API wrote appears in the map as a mirror; migrating
        it would duplicate the very task it came from."""
        parse(call(api.create_meeting_task, event(
            "POST", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"task": "New API task"})))
        before = len(self.t["tasks"].items)
        api._migrate_embedded_tasks(USER, KEY,
                                    self.t["recordings"].items[(KEY,)])
        # Only the two genuine legacy entries migrate; the mirror does not.
        self.assertEqual(len(self.t["tasks"].items), before + 2)
        titles = [r["title"] for r in self.t["tasks"].items.values()]
        self.assertEqual(titles.count("New API task"), 1)


# ===========================================================================
# IDOR / CROSS-TENANT ISOLATION (spec section 22)
#
# The tests above check isolation per route. These check the STRUCTURAL risk:
# three of the Tasks GSIs (folder / assignee / meeting) are NOT keyed by owner,
# so a query against them returns other tenants' rows and the handler is the
# only thing standing between that and a leak. A future filter that forgets the
# owner check would pass every other test in this file.
# ===========================================================================
class TestCrossTenantIsolation(OrgTestCase):
    def setUp(self):
        super().setUp()
        # A complete parallel universe owned by OTHER, deliberately sharing the
        # contact id / recording key with the caller's own data so that a
        # missing owner filter leaks rather than simply missing.
        _, c = self.mk_contact()
        self.cid = c["contact"]["id"]
        self.t["tasks"].put_item(Item={
            "task_id": "t-theirs", "owner_user_id": OTHER,
            "title": "THEIR SECRET TASK", "status": "Open",
            "priority": "Medium", "due_date": "",
            "assignee_contact_id": self.cid,     # same contact id
            "source_recording_id": KEY,          # same recording key
            "fingerprint": "fp-theirs",
            "created_at": "2026-08-01T00:00:00Z"})

    def _assert_no_leak(self, body):
        self.assertNotIn("THEIR SECRET TASK", json.dumps(body))
        self.assertNotIn("t-theirs", json.dumps(body))

    def test_no_leak_via_assignee_index(self):
        _, body = parse(call(api.list_all_tasks, event(
            "GET", "/tasks", qs={"assignee_contact_id": self.cid})))
        self._assert_no_leak(body)

    def test_no_leak_via_meeting_index(self):
        _, body = parse(call(api.list_all_tasks, event(
            "GET", "/tasks", qs={"recording_key": KEY})))
        self._assert_no_leak(body)

    def test_no_leak_via_owner_index(self):
        _, body = parse(call(api.list_all_tasks, event("GET", "/tasks")))
        self._assert_no_leak(body)

    def test_no_leak_in_meeting_task_list(self):
        """The per-meeting list reads the meeting-index too."""
        _, body = parse(call(api.list_meeting_tasks, event(
            "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
        self._assert_no_leak(body)

    def test_cannot_read_their_task_by_id(self):
        status, body = parse(call(api.get_task, event(
            "GET", "/tasks/{task_id}", path={"task_id": "t-theirs"})))
        self.assertEqual(status, 404)
        self._assert_no_leak(body)

    def test_cannot_mutate_their_task_by_id(self):
        for handler, method in ((api.update_task_v2, "PATCH"),
                                (api.resolve_task_assignee, "POST")):
            status, _ = parse(call(handler, event(
                method, "/tasks/{task_id}", path={"task_id": "t-theirs"},
                body={"status": "Completed", "contact_id": self.cid})))
            self.assertEqual(status, 404, handler.__name__)
        # Untouched.
        self.assertEqual(self.t["tasks"].items[("t-theirs",)]["status"], "Open")

    def test_cannot_mutate_their_task_via_meeting_route(self):
        status, _ = parse(call(api.update_meeting_task, event(
            "PATCH", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"id": "t-theirs", "status": "Completed"})))
        self.assertEqual(status, 404)
        self.assertEqual(self.t["tasks"].items[("t-theirs",)]["status"], "Open")

    def test_cannot_delete_their_task_via_meeting_route(self):
        status, _ = parse(call(api.delete_meeting_task, event(
            "DELETE", "/recordings/ai/tasks/{key+}", key=KEY,
            body={"id": "t-theirs"})))
        self.assertEqual(status, 404)
        self.assertIn(("t-theirs",), self.t["tasks"].items)

    def test_contact_delete_does_not_touch_their_tasks(self):
        """delete_contact walks the assignee-index, same exposure."""
        parse(call(api.delete_contact, event(
            "DELETE", "/contacts/{contact_id}", path={"contact_id": self.cid})))
        theirs = self.t["tasks"].items[("t-theirs",)]
        self.assertEqual(theirs["assignee_contact_id"], self.cid)
        self.assertNotIn("resolution_status", theirs)

    def test_speaker_mapping_does_not_resolve_their_tasks(self):
        """_resolve_tasks_for_speaker walks the meeting-index."""
        self.t["tasks"].items[("t-theirs",)]["assignee_speaker_id"] = "0"
        del self.t["tasks"].items[("t-theirs",)]["assignee_contact_id"]
        parse(call(api.set_participant, event(
            "PUT", "/recordings/participants/{key+}", key=KEY,
            body={"speaker_id": "0", "contact_id": self.cid})))
        self.assertNotIn("assignee_contact_id",
                         self.t["tasks"].items[("t-theirs",)])

    def test_their_fingerprint_does_not_block_our_seeding(self):
        """The dedupe index is owner-keyed, so their identical task must not
        suppress ours — the inverse leak (denial rather than disclosure)."""
        fp = api._task_fingerprint(KEY, "Send proposal", "Rahul")
        self.t["tasks"].put_item(Item={
            "task_id": "t-theirs-2", "owner_user_id": OTHER,
            "title": "Send proposal", "status": "Open",
            "source_recording_id": KEY, "fingerprint": fp,
            "created_at": "2026-08-01T00:00:00Z"})
        _, body = parse(call(api.list_meeting_tasks, event(
            "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
        self.assertEqual(body["count"], 1)
        self._assert_no_leak(body)


# ===========================================================================
# END-TO-END (spec section 35)
# ===========================================================================
class TestEndToEnd(OrgTestCase):
    def test_full_scenario(self):
        # 1. Contact + 13. MinuteX account exists
        self.add_user("u-456", "rahul@company.com")
        _, c = self.mk_contact("Rahul Sharma", "rahul@company.com")
        cid = c["contact"]["id"]
        self.assertEqual(c["contact"]["minutex_user_id"], "u-456")

        # 2/3. AI detected the task, attributed to speaker_0
        self.t["recordings"].items[(KEY,)]["ai_tasks"] = [{
            "task": "Send proposal", "assignee_speaker_id": "0",
            "due_date": "2026-08-21", "priority": "High",
            "evidence": "I'll send the proposal tomorrow.",
            "confidence": "high"}]
        _, seeded = parse(call(api.list_meeting_tasks, event(
            "GET", "/recordings/ai/tasks/{key+}", key=KEY)))
        tid = seeded["tasks"][0]["id"]
        self.assertEqual(seeded["tasks"][0]["assignee_speaker_id"], "0")

        # 4/5. Map Speaker 0 -> Rahul; the chain resolves
        _, mapped = parse(call(api.set_participant, event(
            "PUT", "/recordings/participants/{key+}", key=KEY,
            body={"speaker_id": "0", "contact_id": cid})))
        self.assertEqual(mapped["tasks_resolved"], 1)

        # 6. The task now names a person, a meeting and a due date
        _, detail = parse(call(api.get_task, event(
            "GET", "/tasks/{task_id}", path={"task_id": tid})))
        task = detail["task"]
        self.assertEqual(task["task"], "Send proposal")
        self.assertEqual(task["assignee_name"], "Rahul Sharma")
        self.assertEqual(task["assignee_contact_id"], cid)
        self.assertEqual(task["assignee_user_id"], "u-456")   # 13
        self.assertEqual(task["source_recording_id"], KEY)
        self.assertEqual(task["due"], "2026-08-21")
        self.assertEqual(task["ai_evidence"], "I'll send the proposal tomorrow.")
        self.assertEqual(detail["contact"]["email"], "rahul@company.com")

        # 7. It appears in the dashboard queries
        _, by_assignee = parse(call(api.list_all_tasks, event(
            "GET", "/tasks", qs={"assignee_contact_id": cid})))
        self.assertIn(tid, [t["id"] for t in by_assignee["tasks"]])

        # 8. Rahul is still ONE global contact, and the meeting was never
        #    duplicated by anything above.
        self.assertEqual(len(self.t["recordings"].items), 1)
        self.assertEqual(len(self.t["contacts"].items), 1)

        # The transcript was never touched throughout.
        self.assertEqual(self.t["recordings"].items[(KEY,)]["transcript"],
                         TRANSCRIPT)


# ===========================================================================
# ROUTER
# ===========================================================================
class TestRouter(unittest.TestCase):
    EXPECTED = [
        ("POST", "/contacts"), ("GET", "/contacts"),
        ("GET", "/contacts/{contact_id}"), ("PATCH", "/contacts/{contact_id}"),
        ("DELETE", "/contacts/{contact_id}"),
        ("GET", "/tasks"), ("GET", "/tasks/{task_id}"),
        ("PATCH", "/tasks/{task_id}"), ("POST", "/tasks/{task_id}/resolve"),
        ("GET", "/tasks/{task_id}/assignee-candidates"),
        ("GET", "/recordings/participants/{key+}"),
        ("PUT", "/recordings/participants/{key+}"),
    ]

    def test_every_new_route_is_registered(self):
        for route in self.EXPECTED:
            self.assertIn(route, api._ROUTES, f"{route} not registered")

    def test_task_routes_point_at_first_class_handlers(self):
        self.assertIs(api._ROUTES[("GET", "/recordings/ai/tasks/{key+}")],
                      api.list_meeting_tasks)
        self.assertIs(api._ROUTES[("POST", "/recordings/ai/tasks/{key+}")],
                      api.create_meeting_task)
        self.assertIs(api._ROUTES[("PATCH", "/recordings/ai/tasks/{key+}")],
                      api.update_meeting_task)
        self.assertIs(api._ROUTES[("DELETE", "/recordings/ai/tasks/{key+}")],
                      api.delete_meeting_task)

    def test_unknown_route_is_404(self):
        resp = api.lambda_handler(
            {"routeKey": "GET /nope",
             "requestContext": {"http": {"method": "GET", "path": "/nope"}}},
            None)
        self.assertEqual(resp["statusCode"], 404)

    def test_ambiguous_contact_candidates_reach_the_client(self):
        """The 409 must carry candidates through the ROUTER, not just the
        handler — that is what makes "which Rahul?" renderable."""
        tables = fdb.build_tables()
        with mock.patch.object(api, "_contacts", tables["contacts"]), \
             mock.patch.object(api, "_users", tables["users"]), \
             mock.patch.object(api, "_require_auth", return_value=USER):
            tables["contacts"].put_item(Item={
                "contact_id": "c-1", "owner_user_id": USER,
                "name": "Rahul Sharma", "name_lc": "rahul sharma",
                "created_at": "2026-08-01T00:00:00Z"})
            resp = api.lambda_handler(
                {"routeKey": "POST /contacts",
                 "requestContext": {"http": {"method": "POST",
                                             "path": "/contacts"}},
                 "headers": {"authorization": "Bearer t"},
                 "body": json.dumps({"name": "Rahul Sharma"})}, None)
        self.assertEqual(resp["statusCode"], 409)
        body = json.loads(resp["body"])
        self.assertEqual(body["code"], "contact_ambiguous")
        self.assertEqual(len(body["candidates"]), 1)


# ===========================================================================
# NORMALIZATION UNITS
# ===========================================================================
class TestNormalization(unittest.TestCase):
    def test_email(self):
        self.assertEqual(api._norm_email(" Rahul@Company.COM "),
                         "rahul@company.com")
        self.assertEqual(api._norm_email("nope"), "")
        self.assertEqual(api._norm_email(None), "")
        # Provider-specific canonicalization is deliberately NOT applied.
        self.assertNotEqual(api._norm_email("a.b@gmail.com"),
                            api._norm_email("ab@gmail.com"))

    def test_phone(self):
        self.assertEqual(api._norm_phone("+91 98765 43210"), "+919876543210")
        self.assertEqual(api._norm_phone("+91-98765-43210"), "+919876543210")
        self.assertEqual(api._norm_phone("(020) 1234-5678"), "02012345678")
        self.assertEqual(api._norm_phone("123"), "")     # too short
        self.assertEqual(api._norm_phone(""), "")
        # A local number is NOT expanded into an international one — guessing
        # a country code would be exactly the silent inference we forbid.
        self.assertNotEqual(api._norm_phone("9876543210"),
                            api._norm_phone("+919876543210"))

    def test_name(self):
        self.assertEqual(api._norm_name("  Rahul   Sharma "), "rahul sharma")
        self.assertEqual(api._norm_name("RAHUL SHARMA"), "rahul sharma")

    def test_fingerprint_is_stable_and_scoped(self):
        a = api._task_fingerprint(KEY, "Send proposal", "Rahul")
        b = api._task_fingerprint(KEY, "  send   PROPOSAL ", "rahul")
        self.assertEqual(a, b)
        self.assertNotEqual(a, api._task_fingerprint(KEY2, "Send proposal",
                                                     "Rahul"))
        # Field-boundary safety: a NUL separator means these cannot collide.
        self.assertNotEqual(api._task_fingerprint("a", "b", ""),
                            api._task_fingerprint("a", "", "b"))

    def test_cursor_roundtrip(self):
        key = {"owner_user_id": "u-1", "created_at": "2026-08-01T00:00:00Z"}
        self.assertEqual(api._decode_cursor(api._encode_cursor(key)), key)
        self.assertEqual(api._encode_cursor(None), "")
        with self.assertRaises(api.ApiError):
            api._decode_cursor("!!!not-base64!!!")


if __name__ == "__main__":
    unittest.main(verbosity=2)
