#!/usr/bin/env python3
"""test_participant_self_tag.py — attendance without speech.

THE GAP THIS CLOSES. MeetingParticipants is keyed (audio_s3_key, speaker_id),
which modelled participation as speaker->contact and ONLY that. Someone who sat
in a meeting without saying anything — or anyone at all in a meeting where
diarization produced no labels — had no way to record that they were there.
`speaker_id` was required, so there was nothing to attach them to.

THE REPRESENTATION. A non-speaking attendee is stored in the SAME table under a
reserved speaker_id, "self:<contact_id>". No new table, no schema change, no
migration.

WHY IT CANNOT COLLIDE. Diarization labels come from stt_result._speaker_label,
which strips a "speaker_" prefix and otherwise passes the provider's id
through; in production they are compact ("0", "1", "2", "7"). A colon never
appears in one. TestSentinelCannotCollide pins that, and also pins that a
CLIENT cannot hand-craft the prefix to fake a speaker mapping.

THE TWO RULES THAT MAKE IT ATTENDANCE RATHER THAN SPEECH, both enforced by NOT
calling something rather than by a flag a later change could forget:

  * it is never written into the recording's `speaker_names`, so
    _speaker_labels (which falls back to that map) can never surface it as a
    speaker and no generated document can name it as one;
  * it never reaches _resolve_tasks_for_speaker, so "I was in the room" can
    never make the AI assign someone work they never agreed to.

That second rule is the load-bearing one — TestSelfTagNeverResolvesTasks is the
test to look at first if this file ever fails.

OFFLINE: fake_dynamodb, no AWS, no network.

Run:  python -m pytest tests/test_participant_self_tag.py
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

# ORDER MATTERS — see test_task_permissions.py's note. The workspace harness
# installs the shared stubs and binds the same `api` module object.
from test_ai_workspace import api, call  # noqa: E402
import fake_dynamodb as fdb  # noqa: E402

OWNER = "u-owner"
OTHER = "u-other"
KEY = "recordings/u-owner/mobile/meeting_1754300000.m4a"

RECORDING = {
    "audio_s3_key": KEY,
    "user_id": OWNER,
    "title": "Vendor sync",
    "recorded_at": "2026-08-25T10:00:00Z",
    "created_at": "2026-08-25T10:00:00Z",
    "status": "complete",
    "transcript": "Speaker 0: Rahul, prepare the quotation by Friday.",
    "timestamps": [{"speaker": "0", "start": 0.0, "end": 4.0},
                   {"speaker": "1", "start": 4.0, "end": 8.0}],
    "speaker_names": {},
    "ai_tasks": [],
}


def parse(resp):
    return resp["statusCode"], json.loads(resp["body"])


def event(method="PUT", route="/recordings/participants/{key+}", body=None,
          key=None):
    ev = {
        "routeKey": f"{method} {route}",
        "requestContext": {"http": {"method": method, "path": route}},
        "headers": {"authorization": "Bearer test-token"},
        "pathParameters": {"key": key} if key is not None else {},
        "queryStringParameters": {},
    }
    if body is not None:
        ev["body"] = json.dumps(body)
    return ev


class SelfTagHarness(unittest.TestCase):

    def setUp(self):
        self.t = fdb.build_tables()
        self.current_user = OWNER
        self.patches = [
            mock.patch.object(api, "_recordings", self.t["recordings"]),
            mock.patch.object(api, "_contacts", self.t["contacts"]),
            mock.patch.object(api, "_folders", self.t["folders"]),
            mock.patch.object(api, "_folder_contacts",
                              self.t["folder_contacts"]),
            mock.patch.object(api, "_meeting_participants",
                              self.t["participants"]),
            mock.patch.object(api, "_tasks", self.t["tasks"]),
            mock.patch.object(api, "_users", self.t["users"]),
            mock.patch.object(api, "Key", fdb.Key),
            mock.patch.object(api, "_require_auth",
                              side_effect=lambda ev: self.current_user),
            mock.patch.object(api, "_owned_devices", return_value=[]),
            mock.patch.object(api.transcript_store, "hydrate",
                              side_effect=lambda s3, bucket, item: item),
            mock.patch.object(api, "_notify", return_value=None),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

        self.t["recordings"].put_item(Item=json.loads(json.dumps(RECORDING)))
        self.t["contacts"].put_item(Item={
            "contact_id": "c-me", "owner_user_id": OWNER,
            "name": "Shubham Patil", "email": "me@company.com",
            "email_lc": "me@company.com",
            "created_at": "2026-08-25T09:00:00Z",
        })

    def as_user(self, uid):
        self.current_user = uid

    def self_tag(self, contact_id="c-me"):
        return parse(call(api.set_participant, event(
            body={"contact_id": contact_id, "attendance_only": True},
            key=KEY)))

    def map_speaker(self, speaker_id, contact_id):
        return parse(call(api.set_participant, event(
            body={"speaker_id": speaker_id, "contact_id": contact_id},
            key=KEY)))

    def list_participants(self):
        return parse(call(api.list_participants, event(
            "GET", "/recordings/participants/{key+}", key=KEY)))

    def rows(self):
        return list(self.t["participants"].items.values())


# ===========================================================================
# 1. THE TWO CASES — with speakers, and without
# ===========================================================================
class TestSelfTagBothCases(SelfTagHarness):

    def test_self_tag_without_a_speaker_id(self):
        """CASE B: no speaker to claim, and the user was still there."""
        status, body = self.self_tag()
        self.assertEqual(status, 200)
        self.assertTrue(body["participant"]["attendance_only"])
        self.assertEqual(body["participant"]["contact_id"], "c-me")

    def test_self_tag_alongside_real_speakers(self):
        """CASE A: attending a well-diarized meeting without speaking."""
        self.map_speaker("0", "c-me")
        self.t["contacts"].put_item(Item={
            "contact_id": "c-quiet", "owner_user_id": OWNER,
            "name": "Quiet Colleague", "email": "q@company.com",
            "email_lc": "q@company.com", "created_at": "2026-08-25T09:00:00Z",
        })
        status, _ = self.self_tag("c-quiet")
        self.assertEqual(status, 200)
        _, body = self.list_participants()
        kinds = {p["contact_id"]: p["attendance_only"]
                 for p in body["participants"]}
        self.assertFalse(kinds["c-me"], "a mapped speaker is not attendance")
        self.assertTrue(kinds["c-quiet"], "the quiet one is attendance")

    def test_speaker_id_is_still_required_for_a_speaker_mapping(self):
        """The old contract is unchanged for everything that is not a
        self-tag — omitting the speaker is still a 400."""
        status, _ = parse(call(api.set_participant, event(
            body={"contact_id": "c-me"}, key=KEY)))
        self.assertEqual(status, 400)


# ===========================================================================
# 2. IDEMPOTENCE — a double tap must not add a person twice
# ===========================================================================
class TestSelfTagIsIdempotent(SelfTagHarness):

    def test_tagging_twice_writes_one_row(self):
        self.self_tag()
        self.self_tag()
        attendance = [r for r in self.rows()
                      if str(r["speaker_id"]).startswith("self:")]
        self.assertEqual(len(attendance), 1)

    def test_the_key_is_derived_from_the_contact(self):
        """Which is WHY it is idempotent — same person, same primary key."""
        self.self_tag()
        row = [r for r in self.rows()
               if str(r["speaker_id"]).startswith("self:")][0]
        self.assertEqual(row["speaker_id"], "self:c-me")

    def test_a_retag_preserves_when_they_were_added(self):
        self.self_tag()
        first = [r for r in self.rows()
                 if str(r["speaker_id"]).startswith("self:")][0]["created_at"]
        self.self_tag()
        again = [r for r in self.rows()
                 if str(r["speaker_id"]).startswith("self:")][0]["created_at"]
        self.assertEqual(first, again)

    def test_two_different_people_are_two_rows(self):
        self.t["contacts"].put_item(Item={
            "contact_id": "c-two", "owner_user_id": OWNER,
            "name": "Second Person", "email": "two@company.com",
            "email_lc": "two@company.com", "created_at": "2026-08-25T09:00:00Z",
        })
        self.self_tag("c-me")
        self.self_tag("c-two")
        attendance = [r for r in self.rows()
                      if str(r["speaker_id"]).startswith("self:")]
        self.assertEqual(len(attendance), 2)


# ===========================================================================
# 3. THE LOAD-BEARING RULE — attendance never assigns AI work
# ===========================================================================
class TestSelfTagNeverResolvesTasks(SelfTagHarness):
    """Tagging yourself as present must not make the AI's work yours.

    The scenario from the requirement: the AI extracted "Prepare the
    quotation" from a speaker it could not identify. A user says "I was in
    this meeting". That task must still be unassigned — the user claimed
    presence, not the sentence.
    """

    def setUp(self):
        super().setUp()
        self.task = api._new_task_row(
            OWNER, "Prepare the quotation", recording_key=KEY,
            source_type=api.TASK_SOURCE_AI, assignee_name="Rahul",
            assignee_speaker_id="0")
        api._write_task(self.task)

    def test_self_tag_leaves_the_task_unassigned(self):
        status, body = self.self_tag()
        self.assertEqual(status, 200)
        self.assertEqual(body.get("tasks_resolved"), 0)
        row = self.t["tasks"].items[(self.task["task_id"],)]
        self.assertNotIn("assignee_contact_id", row)
        self.assertNotIn("assignee_user_id", row)
        self.assertEqual(row["resolution_status"], api.RESOLUTION_UNRESOLVED)

    def test_the_guard_holds_at_the_function_itself(self):
        """Defence in depth: not merely a call site that returns early."""
        contact = self.t["contacts"].get_item(
            Key={"contact_id": "c-me"})["Item"]
        self.assertEqual(
            api._resolve_tasks_for_speaker(OWNER, KEY, "self:c-me", contact),
            0)
        row = self.t["tasks"].items[(self.task["task_id"],)]
        self.assertNotIn("assignee_contact_id", row)

    def test_a_real_speaker_mapping_STILL_resolves(self):
        """The guard must not have broken the feature it sits next to."""
        status, body = self.map_speaker("0", "c-me")
        self.assertEqual(status, 200)
        self.assertEqual(body.get("tasks_resolved"), 1)
        row = self.t["tasks"].items[(self.task["task_id"],)]
        self.assertEqual(row["assignee_contact_id"], "c-me")


# ===========================================================================
# 4. IT IS NOT A SPEAKER — no invented voice, anywhere
# ===========================================================================
class TestSelfTagIsNotASpeaker(SelfTagHarness):

    def test_it_never_enters_speaker_names(self):
        """speaker_names is what every rendered surface reads. A sentinel in
        there would become a speaker in documents, highlights and the
        transcript view."""
        self.self_tag()
        rec = self.t["recordings"].items[(KEY,)]
        self.assertEqual(rec.get("speaker_names") or {}, {})

    def test_it_never_appears_in_the_speaker_roster(self):
        self.self_tag()
        _, body = self.list_participants()
        self.assertEqual(body["speakers"], ["0", "1"])
        self.assertNotIn("self:c-me", body["speakers"])

    def test_it_does_not_mark_documents_stale(self):
        """speaker_mapping_version invalidates generated documents. Attendance
        changes no name in them, so bumping it would regenerate for nothing."""
        before = self.t["recordings"].items[(KEY,)].get(
            "speaker_mapping_version")
        self.self_tag()
        after = self.t["recordings"].items[(KEY,)].get(
            "speaker_mapping_version")
        self.assertEqual(before, after)

    def test_it_creates_no_transcript_segments(self):
        self.self_tag()
        rec = self.t["recordings"].items[(KEY,)]
        speakers = {s["speaker"] for s in rec["timestamps"]}
        self.assertEqual(speakers, {"0", "1"})


# ===========================================================================
# 5. THE SENTINEL — collision and forgery
# ===========================================================================
class TestSentinelCannotCollide(SelfTagHarness):

    def test_real_diarization_labels_never_contain_the_prefix(self):
        """stt_result._speaker_label strips "speaker_" and passes the rest
        through. Nothing it can emit starts with "self:"."""
        import stt_result
        for raw in ("speaker_0", "speaker_11", "agent", "customer", 0, 7,
                    None, "SPEAKER_2"):
            label = stt_result._speaker_label(raw)
            self.assertFalse(
                api._is_self_speaker(label),
                f"{raw!r} produced a colliding label {label!r}")

    def test_a_client_cannot_forge_the_prefix_as_a_speaker_mapping(self):
        """Otherwise the sentinel becomes a way to fake a speaker — the one
        path by which attendance could gain speech."""
        status, _ = parse(call(api.set_participant, event(
            body={"speaker_id": "self:c-me", "contact_id": "c-me"}, key=KEY)))
        self.assertEqual(status, 400)
        rec = self.t["recordings"].items[(KEY,)]
        self.assertEqual(rec.get("speaker_names") or {}, {})

    def test_the_helpers_agree(self):
        self.assertEqual(api._self_speaker_id("c-x"), "self:c-x")
        self.assertTrue(api._is_self_speaker("self:c-x"))
        for real in ("0", "1", "2", "7", "agent", ""):
            self.assertFalse(api._is_self_speaker(real))


# ===========================================================================
# 6. TENANCY — someone else's meeting is not yours to populate
# ===========================================================================
class TestSelfTagIsTenantIsolated(SelfTagHarness):

    def test_another_user_cannot_self_tag_this_meeting(self):
        self.as_user(OTHER)
        status, _ = self.self_tag()
        self.assertEqual(status, 404)
        self.assertEqual(self.rows(), [])

    def test_another_user_cannot_read_the_participants(self):
        self.self_tag()
        self.as_user(OTHER)
        status, _ = self.list_participants()
        self.assertEqual(status, 404)

    def test_a_contact_from_another_account_is_refused(self):
        self.t["contacts"].put_item(Item={
            "contact_id": "c-theirs", "owner_user_id": OTHER,
            "name": "Their Person", "email": "t@elsewhere.com",
            "email_lc": "t@elsewhere.com", "created_at": "2026-08-25T09:00:00Z",
        })
        status, _ = self.self_tag("c-theirs")
        self.assertEqual(status, 404)


# ===========================================================================
# 7. REMOVAL, and the processing state the UI branches on
# ===========================================================================
class TestRemovalAndStatus(SelfTagHarness):

    def test_an_attendance_row_can_be_cleared(self):
        self.self_tag()
        status, _ = parse(call(api.set_participant, event(
            body={"speaker_id": "self:c-me", "contact_id": None}, key=KEY)))
        self.assertEqual(status, 200)
        self.assertEqual(
            [r for r in self.rows()
             if str(r["speaker_id"]).startswith("self:")], [])

    def test_clearing_an_attendance_row_does_not_bump_the_version(self):
        self.self_tag()
        before = self.t["recordings"].items[(KEY,)].get(
            "speaker_mapping_version")
        parse(call(api.set_participant, event(
            body={"speaker_id": "self:c-me", "contact_id": None}, key=KEY)))
        after = self.t["recordings"].items[(KEY,)].get(
            "speaker_mapping_version")
        self.assertEqual(before, after)

    def test_participants_reports_the_recording_status(self):
        """So the screen can tell "no speakers YET" from "no speakers, ever"
        instead of promising voices that will never arrive."""
        _, body = self.list_participants()
        self.assertEqual(body["recording_status"], "complete")


# ===========================================================================
# 8. MOM — attendance appears, speech is never invented
# ===========================================================================
class TestMomAttendees(SelfTagHarness):
    """The Attendees table must be able to say "was there" without saying
    "said this". A self-tagged person gets a fixed "Attended" contribution —
    a blank cell would be indistinguishable from a speaker whose summary the
    model happened to omit."""

    def _sections(self):
        item = self.t["recordings"].items[(KEY,)]
        return api._build_fresh_sections(OWNER, KEY, item)

    def _attendees_table(self):
        import mom_schema
        for sec in self._sections():
            if sec.get("role") == mom_schema.ROLE_ATTENDEES:
                return sec
        return None

    def _attendee_rows(self):
        """[(name, contribution)] in table order.

        Rows are dicts keyed by COLUMN ID (see mom_schema._rows), so the cells
        are read through the section's own columns rather than by position —
        a positional read would break the moment a column is added.
        """
        table = self._attendees_table()
        if not table:
            return []
        cols = [c["id"] for c in table["columns"]]
        return [(r["cells"][cols[1]], r["cells"][cols[2]])
                for r in table["rows"]]

    def setUp(self):
        super().setUp()
        # A real speaker roster, as ai_schema would have filtered it.
        self.t["recordings"].items[(KEY,)]["participants"] = [
            {"speaker": "0", "summary": "Presented the quarterly figures"},
        ]
        self.t["recordings"].items[(KEY,)]["speaker_names"] = {"0": "Anita"}

    def test_self_tagged_attendee_appears(self):
        self.self_tag()
        self.assertIsNotNone(self._attendees_table())
        names = [n for n, _ in self._attendee_rows()]
        self.assertIn("Anita", names)
        self.assertIn("Shubham Patil", names)

    def test_no_speech_is_attributed_to_them(self):
        import mom_schema
        self.self_tag()
        by_name = dict(self._attendee_rows())
        self.assertEqual(by_name["Shubham Patil"], mom_schema.ATTENDED_ONLY)
        # The speaker keeps their real, model-written contribution.
        self.assertEqual(by_name["Anita"], "Presented the quarterly figures")

    def test_speakers_come_first(self):
        self.self_tag()
        rows = self._attendee_rows()
        self.assertEqual(rows[0][0], "Anita")
        self.assertEqual(rows[-1][0], "Shubham Patil")

    def test_someone_who_spoke_is_not_listed_twice(self):
        """A person can map to a speaker AND have self-tagged. The speaker row
        is the truer record and wins."""
        self.t["recordings"].items[(KEY,)]["speaker_names"] = {
            "0": "Shubham Patil"}
        self.self_tag()
        rows = self._attendee_rows()
        names = [n for n, _ in rows]
        self.assertEqual(names.count("Shubham Patil"), 1)
        # And it is the SPEAKING row that survived.
        self.assertEqual(rows[0][1], "Presented the quarterly figures")

    def test_attendees_only_still_produces_a_table(self):
        """No speakers at all, one self-tag: the meeting still had someone in
        it, and the Minutes should say so."""
        self.t["recordings"].items[(KEY,)]["participants"] = []
        self.self_tag()
        self.assertIsNotNone(self._attendees_table())
        self.assertEqual([n for n, _ in self._attendee_rows()],
                         ["Shubham Patil"])

    def test_no_participants_and_no_attendees_yields_no_table(self):
        """Unchanged from before: an empty section is worse than none."""
        self.t["recordings"].items[(KEY,)]["participants"] = []
        self.assertIsNone(self._attendees_table())


if __name__ == "__main__":
    unittest.main(verbosity=2)
