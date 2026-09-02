#!/usr/bin/env python3
"""test_assignee_meeting_access.py — the read-only meeting a task assignee sees.

THE FEATURE. A task carries a "from this meeting" line. Before this, that line
was inert text for the assignee: get_task deliberately blanked `audio_s3_key`
so the row could not be tapped, and every meeting route gated on ownership.
The person actually doing the work could see a deadline and a title and had to
go and ask the owner what the meeting had actually decided.

Now the assignee may OPEN the meeting — its notes, and nothing else — through
GET /recordings/shared-with-me/{key+}.

WHY THIS FILE IS MOSTLY REFUSALS. Granting a non-owner read access to someone
else's meeting is the widest authorization change in the task system, so the
tests that matter are the ones pinning what does NOT come out and who does NOT
get in. The groups below each correspond to a real way this could go wrong:

  ACCESS        The grant is DERIVED from the current task assignment, never
                stored. A stranger must 404 (never 403 — that confirms the key
                exists, matching _owned_recording/_owned_task). The owner still
                gets through, because rejecting them would make the route lie
                about a recording they demonstrably own.

  REVOCATION    This is the whole reason the grant is derived. Reassigning the
                task must revoke access on the very next request, with no sweep
                and no revoke UI. Same for deleting the task, and for the
                meeting going to Trash. A stored grant would pass a "can read"
                test today and fail these.

  BOUNDARY      No transcript, no audio, no presigned URL — asserted against
                the RESPONSE BODY, not against a rendered screen. "Not shown"
                and "not present" are very different when the caller can read
                raw JSON, and the app is never the enforcement point.

  LEAKAGE       public_payload() ASSEMBLES its output, so a new attribute on
                the recording row cannot leak by being forgotten. The test that
                proves it stuffs the row with owner_user_id, device ids, CRM
                records and chat history, then asserts none of it comes back.
                A hand-rolled deny-list would pass today and fail the day
                someone adds a field — which is exactly why the route reuses
                the share assembler instead of building its own payload.

  NO MUTATION   Reading a meeting must not become a way to write to it. The
                route is a pure read, and an assignee hitting the owner's
                write routes still 404s.

OFFLINE: fake_dynamodb, no AWS, no network, no Groq.

Run:  python -m pytest tests/test_assignee_meeting_access.py
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

# ORDER MATTERS — same reason as test_task_permissions: importing the workspace
# harness first installs the shared boto3 stubs and binds the SAME `api` module
# object (and the same Key class the lambda holds a reference to).
from test_ai_workspace import api, call  # noqa: E402
import fake_dynamodb as fdb  # noqa: E402
import share_schema  # noqa: E402

OWNER = "u-owner"        # recorded the meeting, created the task
ASSIGNEE = "u-assignee"  # the task is assigned to them
STRANGER = "u-stranger"  # no relationship to the meeting or the task

KEY = "recordings/u-owner/mobile/meeting_1754300000.m4a"

TRANSCRIPT = (
    "Speaker 0: The quotation came in at 4.2 lakh, which is over budget.\n"
    "Speaker 1: Rahul, prepare a revised quotation by Friday."
)

RECORDING = {
    "audio_s3_key": KEY,
    "user_id": OWNER,
    "device_id": "esp32-owner-1",
    "title": "Fit-out quotation review",
    "recorded_at": "2026-08-25T10:00:00Z",
    "created_at": "2026-08-25T10:00:00Z",
    "duration": 900,
    "language": "en",
    "status": "done",
    "transcript": TRANSCRIPT,
    "summary": "The 4.2 lakh quotation was rejected as over budget.",
    "highlights": ["Quote came in 0.4 lakh over budget"],
    "speaker_names": {"0": "Ravi", "1": "Priya"},
    "ai_tasks": [],
    # The AI Overview is what the owner's screen renders first, so it is what
    # the assignee should see — via _overview_sections, filtered by the config.
    "overview": {
        "sections": [
            {"title": "Summary", "kind": "text",
             "content": "The revised 3.9 lakh quote was approved in principle."},
            {"title": "Decisions", "kind": "list",
             "items": ["Drop imported fittings", "Revised quote by Friday"]},
        ]
    },
}


def parse(resp):
    return resp["statusCode"], json.loads(resp["body"])


def event(key=KEY, method="GET", route="/recordings/shared-with-me/{key+}"):
    return {
        "routeKey": f"{method} {route}",
        "requestContext": {"http": {"method": method, "path": route}},
        "headers": {"authorization": "Bearer test-token"},
        "pathParameters": {"key": key} if key is not None else {},
        "queryStringParameters": {},
    }


class AssigneeAccessHarness(unittest.TestCase):
    """A meeting owned by OWNER, with one task assigned to ASSIGNEE."""

    def setUp(self):
        self.t = fdb.build_tables()
        self.current_user = ASSIGNEE

        self.s3 = mock.MagicMock()
        # If anything ever calls this, the presign assertions below must fail
        # loudly rather than silently returning a MagicMock.
        self.s3.generate_presigned_url.return_value = \
            "https://s3.example.com/signed?X-Amz-Signature=LEAKED"

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
            mock.patch.object(api, "_s3", self.s3),
            mock.patch.object(api, "_require_auth",
                              side_effect=lambda ev: self.current_user),
            mock.patch.object(api, "_owned_devices",
                              side_effect=self._owned_devices),
            mock.patch.object(api.transcript_store, "hydrate",
                              side_effect=lambda s3, bucket, item: item),
            mock.patch.object(api, "_notify", return_value=None),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

        self.t["recordings"].put_item(Item=json.loads(json.dumps(RECORDING)))
        self.t["users"].put_item(Item={"user_id": OWNER,
                                       "name": "Meeting Owner",
                                       "email": "owner@company.com"})
        self.t["users"].put_item(Item={"user_id": ASSIGNEE,
                                       "name": "Rahul Sharma",
                                       "email": "rahul@company.com"})
        self.contact = self._make_contact()
        self.task = self._make_task()
        self.task_id = self.task["task_id"]

    def _owned_devices(self, user_id):
        """Only the OWNER owns the device. This is what keeps the legacy
        device-ownership branch of the auth check honest in these tests."""
        return ["esp32-owner-1"] if user_id == OWNER else []

    def as_user(self, user_id):
        self.current_user = user_id

    def _make_contact(self):
        cid = "c-rahul"
        self.t["contacts"].put_item(Item={
            "contact_id": cid,
            "owner_user_id": OWNER,
            "name": "Rahul Sharma",
            "email": "rahul@company.com",
            "email_lc": "rahul@company.com",
            "minutex_user_id": ASSIGNEE,
            "created_at": "2026-08-25T09:00:00Z",
        })
        return cid

    def _make_task(self):
        contact = self.t["contacts"].get_item(
            Key={"contact_id": self.contact})["Item"]
        row = api._new_task_row(
            OWNER, "Prepare the revised quotation",
            recording_key=KEY,
            due="Friday",
            source_type=api.TASK_SOURCE_AI,
            assignee_contact=contact,
            ai_confidence="high",
            ai_evidence="Rahul, prepare a revised quotation by Friday.",
        )
        api._write_task(row)
        return row

    # -- convenience callers -------------------------------------------
    def fetch(self, key=KEY):
        return parse(call(api.get_assignee_meeting, event(key=key)))

    def get_task_detail(self):
        ev = {
            "routeKey": "GET /tasks/{task_id}",
            "requestContext": {"http": {"method": "GET", "path": "/tasks"}},
            "headers": {"authorization": "Bearer test-token"},
            "pathParameters": {"task_id": self.task_id},
            "queryStringParameters": {},
        }
        return parse(call(api.get_task, ev))


# ===========================================================================
# 1. ACCESS — who gets in, and who must not learn the meeting exists
# ===========================================================================
class TestAccess(AssigneeAccessHarness):

    def test_assignee_can_read_the_meeting_notes(self):
        """The feature itself: the person doing the work sees the context."""
        status, body = self.fetch()
        self.assertEqual(status, 200)
        self.assertEqual(body["access"], "assignee")
        self.assertEqual(body["meeting"]["title"], "Fit-out quotation review")

    def test_assignee_sees_the_meetings_actual_content(self):
        """Not an empty shell. A payload with a title and no sections would
        pass a naive 200-check while delivering none of the context that is
        the entire point of the feature."""
        _, body = self.fetch()
        overview = body["meeting"]["overview"]
        titles = [s["title"] for s in overview]
        self.assertIn("Summary", titles)
        self.assertIn("Decisions", titles)
        joined = json.dumps(overview)
        self.assertIn("3.9 lakh", joined)

    def test_stored_mom_is_shown_when_there_is_no_overview(self):
        """The OTHER content path. A meeting with no AI Overview falls back to
        the MoM structure, and the assignee must get the owner's EDITED MoM —
        the same thing the share page shows — not an empty body.

        Worth its own test because the two paths return differently shaped
        sections (`overview[]` vs `sections[]`), and the screen renders each
        with different code.
        """
        item = self.t["recordings"].get_item(
            Key={"audio_s3_key": KEY})["Item"]
        del item["overview"]
        item[api.MOM_ATTR] = {
            "sections": [
                {"id": "s1", "role": "summary", "kind": "text",
                 "title": "Summary", "visible": True,
                 "text": "Vendor agreed to revise the quotation."},
                {"id": "s2", "role": "decisions", "kind": "list",
                 "title": "Decisions", "visible": True,
                 "items": [{"text": "Drop imported fittings",
                            "visible": True}]},
            ],
        }
        self.t["recordings"].put_item(Item=item)

        _, body = self.fetch()
        self.assertEqual(body["meeting"]["overview"], [])
        titles = [s["title"] for s in body["meeting"]["sections"]]
        self.assertIn("Summary", titles)
        self.assertIn("Decisions", titles)
        self.assertIn("revise the quotation", json.dumps(body["meeting"]))

    def test_mom_is_resolved_against_the_owner_not_the_caller(self):
        """A subtle one that would silently ship an empty meeting.

        The MoM's Action Items and attendees are read with an owner_user_id
        filter. Resolving them as the CALLER — the natural mistake, since the
        caller is who the route authenticated — matches nothing and returns a
        MoM with its task table empty. The owner's id is the right argument,
        and this pins it.
        """
        item = self.t["recordings"].get_item(
            Key={"audio_s3_key": KEY})["Item"]
        del item["overview"]
        self.t["recordings"].put_item(Item=item)

        with mock.patch.object(api, "_build_fresh_sections",
                               return_value=[]) as build:
            self.fetch()
        build.assert_called_once()
        self.assertEqual(build.call_args[0][0], OWNER)

    def test_stranger_gets_404_not_403(self):
        """A 403 would confirm the key names a real meeting. The whole API
        reports a miss and a forbidden identically for exactly this reason."""
        self.as_user(STRANGER)
        status, body = self.fetch()
        self.assertEqual(status, 404)
        self.assertNotIn("Fit-out", json.dumps(body))

    def test_owner_can_also_read_it(self):
        """Rejecting the owner would make the route lie about a recording they
        demonstrably own — and the app may reach it from their own task."""
        self.as_user(OWNER)
        status, body = self.fetch()
        self.assertEqual(status, 200)
        self.assertEqual(body["access"], "owner")

    def test_unknown_recording_key_404s(self):
        status, _ = self.fetch(key="recordings/u-owner/mobile/nope.m4a")
        self.assertEqual(status, 404)

    def test_missing_key_is_a_400(self):
        status, _ = self.fetch(key="")
        self.assertEqual(status, 400)

    def test_creator_of_an_unrelated_task_gets_nothing(self):
        """Being a MinuteX user with tasks of your own is not access. The
        stranger here has a task — just not one from this meeting."""
        other = api._new_task_row(STRANGER, "Unrelated work",
                                  recording_key="recordings/u-stranger/x.m4a")
        api._write_task(other)
        self.as_user(STRANGER)
        self.assertEqual(self.fetch()[0], 404)

    def test_contact_assignment_without_an_account_is_not_access(self):
        """A contact is an address-book row, not an identity. A task assigned
        to a contact with no linked MinuteX account grants nobody access —
        the same distinction the task permission model rests on."""
        self.t["contacts"].put_item(Item={
            "contact_id": "c-nolink",
            "owner_user_id": OWNER,
            "name": "Unlinked Person",
            "email": "nolink@company.com",
            "email_lc": "nolink@company.com",
            "created_at": "2026-08-25T09:00:00Z",
        })
        contact = self.t["contacts"].get_item(
            Key={"contact_id": "c-nolink"})["Item"]
        row = api._new_task_row(OWNER, "Unlinked task", recording_key=KEY,
                                assignee_contact=contact)
        api._write_task(row)
        self.assertNotIn("assignee_user_id", row)
        self.as_user(STRANGER)
        self.assertEqual(self.fetch()[0], 404)


# ===========================================================================
# 2. REVOCATION — the reason the grant is derived and never stored
# ===========================================================================
class TestRevocation(AssigneeAccessHarness):

    def test_reassignment_revokes_immediately(self):
        """THE test for the derived-grant design. Move the task to someone
        else and the previous assignee loses the meeting on the very next
        request — no sweep, no revoke UI, no stale grant row."""
        self.assertEqual(self.fetch()[0], 200)

        self.t["tasks"].update_item(
            Key={"task_id": self.task_id},
            UpdateExpression="SET assignee_user_id = :u",
            ExpressionAttributeValues={":u": STRANGER},
        )

        self.assertEqual(self.fetch()[0], 404)
        # ...and the new assignee has it.
        self.as_user(STRANGER)
        self.assertEqual(self.fetch()[0], 200)

    def test_unassigning_revokes(self):
        """Clearing the assignee, not swapping it, must revoke too."""
        self.assertEqual(self.fetch()[0], 200)
        self.t["tasks"].update_item(
            Key={"task_id": self.task_id},
            UpdateExpression="REMOVE assignee_user_id",
        )
        self.assertEqual(self.fetch()[0], 404)

    def test_deleting_the_task_revokes(self):
        self.assertEqual(self.fetch()[0], 200)
        self.t["tasks"].delete_item(Key={"task_id": self.task_id})
        self.assertEqual(self.fetch()[0], 404)

    def test_trashed_meeting_is_not_readable(self):
        """A meeting in the Trash stops being readable through a task exactly
        as it stops being readable through a share link."""
        self.t["recordings"].update_item(
            Key={"audio_s3_key": KEY},
            UpdateExpression="SET recording_status = :s",
            ExpressionAttributeValues={":s": api.RECORDING_TRASHED},
        )
        self.assertEqual(self.fetch()[0], 404)

    def test_a_second_task_keeps_access_alive(self):
        """Access is per-MEETING, not per-task: losing one task while still
        holding another from the same meeting must not revoke."""
        contact = self.t["contacts"].get_item(
            Key={"contact_id": self.contact})["Item"]
        second = api._new_task_row(OWNER, "Also chase the vendor",
                                   recording_key=KEY,
                                   assignee_contact=contact)
        api._write_task(second)

        self.t["tasks"].delete_item(Key={"task_id": self.task_id})
        self.assertEqual(self.fetch()[0], 200)

    def test_completed_task_still_grants_access(self):
        """Finishing the work does not erase the need to refer back to why it
        was asked for. Only reassignment or deletion revokes."""
        self.t["tasks"].update_item(
            Key={"task_id": self.task_id},
            UpdateExpression="SET #s = :s",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": api.TASK_STATUS_COMPLETED},
        )
        self.assertEqual(self.fetch()[0], 200)


# ===========================================================================
# 3. BOUNDARY — notes only. Asserted on the response body, not on a screen.
# ===========================================================================
class TestSecurityBoundary(AssigneeAccessHarness):

    def test_no_transcript_in_the_payload(self):
        """Not hidden — ABSENT. The recording has a transcript; the assignee
        must not receive a word of it."""
        _, body = self.fetch()
        self.assertIsNone(body["meeting"]["transcript"])
        blob = json.dumps(body)
        self.assertNotIn("over budget, which is", blob)
        self.assertNotIn("Speaker 0:", blob)

    def test_no_speaker_blocks(self):
        """The diarized transcript is a transcript too — a per-speaker
        rendering of it must not slip through the other door."""
        _, body = self.fetch()
        self.assertEqual(body["meeting"]["speaker_blocks"], [])

    def test_no_audio_url(self):
        _, body = self.fetch()
        self.assertIsNone(body["meeting"]["audio_url"])
        self.assertNotIn("X-Amz-Signature", json.dumps(body))

    def test_no_presign_is_ever_generated(self):
        """Stronger than "no URL in the response": the presign must never be
        MINTED. It is a bearer credential, and one created and then dropped
        still exists in this Lambda's memory and in any future log line."""
        self.fetch()
        self.s3.generate_presigned_url.assert_not_called()

    def test_toggles_cannot_be_widened_by_the_request(self):
        """The config is synthetic and fixed. There is no request field that
        turns the transcript on — this pins that no future refactor quietly
        starts reading one from the query string or body."""
        ev = event()
        ev["queryStringParameters"] = {"transcript_enabled": "true",
                                       "audio_enabled": "true"}
        ev["body"] = json.dumps({"transcript_enabled": True,
                                 "audio_enabled": True})
        status, body = parse(call(api.get_assignee_meeting, ev))
        self.assertEqual(status, 200)
        self.assertIsNone(body["meeting"]["transcript"])
        self.assertIsNone(body["meeting"]["audio_url"])

    def test_config_pins_transcript_and_audio_off(self):
        """The unit-level statement of the same rule, so a failure points at
        the config rather than at the whole route."""
        config = api._assignee_share_config()
        self.assertFalse(config["transcript_enabled"])
        self.assertFalse(config["audio_enabled"])

    def test_config_is_valid_for_every_declared_toggle(self):
        """Built through coerce_config, so a NEW toggle added to share_schema
        arrives here at its default instead of being missing — a hand-rolled
        dict would KeyError (or silently read as False) the day one is added."""
        config = api._assignee_share_config()
        for name, _default, _roles in share_schema.TOGGLES:
            self.assertIn(name, config)

    def test_participants_are_included(self):
        """A deliberate product decision, consistent with Meeting Share: the
        assignee sees who was in the meeting. Pinned so it is changed on
        purpose rather than lost in a refactor."""
        config = api._assignee_share_config()
        self.assertTrue(config["participants_enabled"])


# ===========================================================================
# 4. LEAKAGE — the payload is assembled, so nothing can leak by omission
# ===========================================================================
class TestNoLeakage(AssigneeAccessHarness):

    def test_private_row_attributes_never_reach_the_assignee(self):
        """Stuff the recording row with everything private the product stores
        and assert none of it comes back. This is the test that justifies
        reusing public_payload instead of writing a second builder: a
        deny-list would pass today and fail the day an attribute is added."""
        self.t["recordings"].put_item(Item={
            **json.loads(json.dumps(RECORDING)),
            "owner_user_id": OWNER,
            "device_id": "esp32-secret-serial",
            "s3_bucket": "minutex-private-bucket",
            "crm_records": [{"salesforce_id": "0061234567", "type": "Opp"}],
            "chat_history": [{"role": "user", "content": "internal question"}],
            "folder_id": "fld-private",
            "notes_private": "do not share",
        })
        _, body = self.fetch()
        blob = json.dumps(body)
        for secret in ("esp32-secret-serial", "minutex-private-bucket",
                       "0061234567", "internal question", "fld-private",
                       "do not share", OWNER):
            self.assertNotIn(secret, blob)

    def test_the_raw_s3_key_is_not_echoed(self):
        """The key is an object path in a private bucket. The assignee already
        holds it (it is how they addressed this route), but the payload has no
        reason to restate it, and public_payload never does."""
        _, body = self.fetch()
        self.assertNotIn(KEY, json.dumps(body["meeting"]))

    def test_payload_keys_are_exactly_the_share_contract(self):
        """The response shape is the share assembler's, not an ad-hoc dict.
        If someone adds a key here by hand, this fails and makes them justify
        it against the assembled-not-filtered rule."""
        _, body = self.fetch()
        self.assertEqual(
            set(body["meeting"]),
            {"title", "recorded_at", "duration", "language", "overview",
             "sections", "transcript", "speaker_blocks", "audio_url",
             "expires_at"},
        )


# ===========================================================================
# 5. NO MUTATION — reading a meeting is not a way into writing to it
# ===========================================================================
class TestReadOnly(AssigneeAccessHarness):

    def test_reading_does_not_write_to_the_recording(self):
        """A viewer opening a meeting must not cause a write to the owner's
        row — the same rule _shared_mom follows for the public page."""
        before = json.dumps(
            self.t["recordings"].get_item(Key={"audio_s3_key": KEY})["Item"],
            sort_keys=True)
        self.fetch()
        after = json.dumps(
            self.t["recordings"].get_item(Key={"audio_s3_key": KEY})["Item"],
            sort_keys=True)
        self.assertEqual(before, after)

    def test_assignee_still_cannot_open_the_owners_meeting_route(self):
        """The new read does NOT widen the existing owner routes. get_recording
        is what backs the full meeting screen (audio, transcript, AI), and it
        must keep 404ing for an assignee."""
        ev = event(route="/recordings/{key+}")
        status, _ = parse(call(api.get_recording, ev))
        self.assertEqual(status, 404)

    def test_assignee_cannot_patch_the_meeting(self):
        ev = event(method="PATCH", route="/recordings/{key+}")
        ev["body"] = json.dumps({"title": "Renamed by the assignee"})
        status, _ = parse(call(api.patch_recording, ev))
        self.assertEqual(status, 404)
        self.assertEqual(
            self.t["recordings"].get_item(
                Key={"audio_s3_key": KEY})["Item"]["title"],
            "Fit-out quotation review")

    def test_assignee_cannot_create_a_share_link_for_someone_elses_meeting(self):
        """Read access must not become the power to publish the meeting to
        the open internet."""
        ev = event(method="POST", route="/recordings/share/{key+}")
        ev["body"] = json.dumps({})
        ev["headers"]["host"] = "api.example.com"
        with mock.patch.object(api, "_shares", fdb.FakeTable(
                "Shares", "share_id",
                indexes={"token-index": ("token_hash", None),
                         "recording-index": ("recording_key", "created_at")})):
            status, _ = parse(call(api.create_share, ev))
        self.assertEqual(status, 404)


# ===========================================================================
# 6. THE TASK DETAIL HANDSHAKE — the app must be told the key is now usable
# ===========================================================================
class TestTaskDetailIntegration(AssigneeAccessHarness):

    def test_assignee_now_receives_the_recording_key(self):
        """The bug this feature fixes. get_task used to blank audio_s3_key so
        the meeting row could not be tapped; it now carries the key plus the
        access marker saying WHICH screen that key is good for."""
        status, body = self.get_task_detail()
        self.assertEqual(status, 200)
        rec = body["recording"]
        self.assertEqual(rec["audio_s3_key"], KEY)
        self.assertEqual(rec["access"], "assignee")
        self.assertEqual(rec["title"], "Fit-out quotation review")

    def test_assignee_still_gets_no_folder_or_speaker_names(self):
        """The folder is the owner's workspace and addresses a screen the
        assignee cannot open; empty speaker_names is what keeps
        _public_task_v2 on the stored assignee string."""
        _, body = self.get_task_detail()
        self.assertEqual(body["recording"]["folder_id"], "")
        self.assertEqual(body["recording"]["speaker_names"], {})

    def test_owner_is_marked_as_owner(self):
        self.as_user(OWNER)
        _, body = self.get_task_detail()
        self.assertEqual(body["recording"]["access"], "owner")
        self.assertEqual(body["recording"]["audio_s3_key"], KEY)

    def test_trashed_meeting_is_not_offered_to_the_assignee(self):
        """No point handing the app a key that the read route will 404 on —
        the provenance row would become a tap that leads nowhere."""
        self.t["recordings"].update_item(
            Key={"audio_s3_key": KEY},
            UpdateExpression="SET recording_status = :s",
            ExpressionAttributeValues={":s": api.RECORDING_TRASHED},
        )
        _, body = self.get_task_detail()
        self.assertNotIn("recording", body)

    def test_the_key_the_task_hands_out_actually_opens(self):
        """End to end, as the app performs it: read the task, take the key it
        was given, open the meeting with it. Pins the two halves together so
        a change to either cannot leave the app with a dead tap."""
        _, task_body = self.get_task_detail()
        key = task_body["recording"]["audio_s3_key"]
        status, meeting_body = self.fetch(key=key)
        self.assertEqual(status, 200)
        self.assertEqual(meeting_body["meeting"]["title"],
                         task_body["recording"]["title"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
