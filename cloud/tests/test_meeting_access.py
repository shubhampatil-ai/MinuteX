#!/usr/bin/env python3
"""test_meeting_access.py — tagged MinuteX users and the recorder's control.

THE FEATURE. When the recorder tags a person who has a MinuteX account, that
person can open the meeting — READ-ONLY, on the shared view — at the level the
recorder picks per person:

    NONE  -> no access (stored, so re-tagging cannot undo the choice)
    NOTES -> notes only (the default on tagging)
    FULL  -> notes + transcript + audio (still read-only)

WHAT THIS FILE PINS.

  1. THE RECORDER IS UNCHANGED. They keep every existing owner read, write,
     and meeting-edit permission; these permissions are unchanged by
     participant grants. TestRecorderKeepsOwnerPermissions.

  2. THE SECURITY INVARIANT. MeetingAccess is never general meeting
     authorization: a NOTES or FULL grantee gets 404 from every owner route,
     sees nothing extra in the org Desk list, and _can_read_meeting (which
     feeds the AI assistant) says no. TestGrantIsNotMeetingAuthorization.

  3. FULL IS NEVER WRITE ACCESS. Every key-scoped write route in _ROUTES is
     driven as a FULL grantee and must refuse without changing anything.
     TestFullIsNeverWriteAccess iterates the table, so a new write route is
     covered the day it is added.

  4. WHAT EACH LEVEL SEES on the shared view — and that NOTES never mints an
     audio presign. TestSharedViewByLevel.

  5. THE LIFECYCLE: auto-grant on tag, no duplicate, NONE survives re-tag,
     untag-last revokes, contact delete revokes, org removal revokes,
     permanent delete drops grants, trash hides. TestTaggingLifecycle,
     TestRecorderControl, TestCleanup.

  6. SHARED-WITH-ME LIST: NONE and trash are left out, and every page of the
     index is followed. TestSharedWithMeList.

SECURITY POSTURE, as in every suite here: route functions are called directly
with a forged identity, exactly as curl against the deployed API would.

OFFLINE: fake_dynamodb, no AWS, no network.

Run:  python -m pytest tests/test_meeting_access.py
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

# ORDER MATTERS — the workspace harness binds the same `api` module object
# (and Key class) every other suite uses.
from test_ai_workspace import api  # noqa: E402
import fake_dynamodb as fdb  # noqa: E402
import workspace_schema as ws  # noqa: E402
import notification_schema as ns  # noqa: E402
from test_workspace_resources import (  # noqa: E402
    OrgHarness, event, parse, NOW,
    OWNER_A, MANAGER_A, MEMBER_A, MEMBER_A2, OWNER_B, OUTSIDER, ORG_A,
)

RECORDER = MEMBER_A          # records the meeting in ORG_A
COLLEAGUE = MEMBER_A2        # an org member with no role-based visibility
EXTERNAL = OWNER_B           # a MinuteX user in ANOTHER organisation
KEY = "recordings/u-mea/mobile/access_1.m4a"
SIGNED = "https://s3.example.com/signed?X-Amz-Signature=abc"


class AccessHarness(OrgHarness):
    """ORG_A meeting recorded by RECORDER, with contacts for two people."""

    def setUp(self):
        super().setUp()
        self.notified = []
        self.audits = []
        self.s3 = mock.MagicMock()
        self.s3.generate_presigned_url.return_value = SIGNED

        def _notify(user_id, notification_type, entity_id, **kw):
            self.notified.append((user_id, notification_type, entity_id, kw))
            return {"notification_id": "n"}

        def _users_by_ids(ids):
            out = {}
            for uid in ids or []:
                row = self.t["users"].get_item(Key={"user_id": uid}).get("Item")
                if row:
                    out[uid] = row
            return out

        extra = [
            mock.patch.object(api, "_notify", side_effect=_notify),
            mock.patch.object(api, "_users_by_ids", side_effect=_users_by_ids),
            mock.patch.object(api, "_s3", self.s3),
            mock.patch.object(api, "BUCKET_NAME", "bucket"),
            mock.patch.object(api, "_audit", side_effect=lambda action, uid,
                              ent, **kw: self.audits.append((action, kw))),
        ]
        for p in extra:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in extra])

        self.item = self.make_meeting(
            KEY, RECORDER, ORG_A,
            transcript="Speaker 0: The quote is 4.2 lakh.",
            timestamps=[{"speaker": "0", "start": 0, "end": 3,
                         "text": "The quote is 4.2 lakh."}],
            speaker_names={"0": "Ravi"},
            overview={"sections": [{"title": "Summary", "kind": "text",
                                    "content": "Quote reviewed."}]})
        # Contacts the recorder owns, linked to MinuteX accounts by email.
        for cid, uid in (("c-colleague", COLLEAGUE), ("c-external", EXTERNAL),
                         ("c-me", RECORDER)):
            self.t["contacts"].put_item(Item={
                "contact_id": cid, "owner_user_id": RECORDER,
                "name": uid, "email": f"{uid}@work.com",
                "email_lc": f"{uid}@work.com", "minutex_user_id": uid,
                "created_at": NOW, "updated_at": NOW})
        # A contact with no MinuteX account.
        self.t["contacts"].put_item(Item={
            "contact_id": "c-nobody", "owner_user_id": RECORDER,
            "name": "Nobody", "email": "nobody@elsewhere.com",
            "email_lc": "nobody@elsewhere.com",
            "created_at": NOW, "updated_at": NOW})
        self.as_user(RECORDER)

    # --- actions -------------------------------------------------------
    def tag(self, contact_id, speaker="0", attendance=False):
        self.as_user(RECORDER)
        body = ({"contact_id": contact_id, "attendance_only": True}
                if attendance else
                {"speaker_id": speaker, "contact_id": contact_id})
        return parse(api.set_participant(event(
            method="PUT", route="/recordings/participants/{key+}",
            key=KEY, body=body)))

    def untag(self, speaker_id):
        self.as_user(RECORDER)
        return parse(api.set_participant(event(
            method="PUT", route="/recordings/participants/{key+}",
            key=KEY, body={"speaker_id": speaker_id, "contact_id": None})))

    def set_level(self, user_id, level, *, as_user=RECORDER):
        self.as_user(as_user)
        return parse(api.set_meeting_access(event(
            method="PUT", route="/recordings/access/{key+}",
            key=KEY, body={"user_id": user_id, "level": level})))

    def shared_view(self, who):
        self.as_user(who)
        return parse(api.get_assignee_meeting(event(
            route="/recordings/shared-with-me/{key+}", key=KEY)))

    def level(self, uid):
        return ws.grant_level(api._meeting_access_row(KEY, uid))

    def access_notifications(self):
        return [n for n in self.notified
                if n[1] == ns.TYPE_MEETING_ACCESS_GRANTED]


# ===========================================================================
# 1. TAGGING
# ===========================================================================
class TestTaggingLifecycle(AccessHarness):
    def test_tagging_a_minutex_user_grants_notes_and_notifies_once(self):
        self.tag("c-colleague")
        self.assertEqual(self.level(COLLEAGUE), ws.ACCESS_LEVEL_NOTES)
        self.assertEqual([n[0] for n in self.access_notifications()],
                         [COLLEAGUE])
        # Re-tag (same slot, and again as an attendee): no duplicate grant,
        # no second notification.
        self.tag("c-colleague")
        self.tag("c-colleague", attendance=True)
        self.assertEqual(self.level(COLLEAGUE), ws.ACCESS_LEVEL_NOTES)
        self.assertEqual(len(self.access_notifications()), 1)
        self.assertEqual(len(api._meeting_access_rows(KEY)), 1)

    def test_the_notification_names_the_recorder_and_the_meeting(self):
        self.tag("c-colleague")
        (_, _, entity, kw), = self.access_notifications()
        self.assertEqual(entity, KEY)
        self.assertIn(RECORDER, kw["message"])       # users.name == uid here
        self.assertIn("Meeting", kw["message"])      # the meeting's title
        self.assertEqual(kw["actor_user_id"], RECORDER)

    def test_tagging_yourself_grants_nothing(self):
        self.tag("c-me")
        self.assertEqual(api._meeting_access_rows(KEY), {})
        self.assertEqual(self.access_notifications(), [])

    def test_a_contact_without_an_account_grants_nothing(self):
        self.tag("c-nobody")
        self.assertEqual(api._meeting_access_rows(KEY), {})

    def test_an_unlinked_contact_is_resolved_by_email_and_backfilled(self):
        self.t["contacts"].put_item(Item={
            "contact_id": "c-late", "owner_user_id": RECORDER,
            "name": "Late signup", "email": f"{COLLEAGUE}@work.com",
            "email_lc": f"{COLLEAGUE}@work.com",
            "created_at": NOW, "updated_at": NOW})
        self.tag("c-late")
        self.assertEqual(self.level(COLLEAGUE), ws.ACCESS_LEVEL_NOTES)
        self.assertEqual(self.t["contacts"].get_item(
            Key={"contact_id": "c-late"})["Item"]["minutex_user_id"],
            COLLEAGUE)

    def test_an_external_minutex_user_can_be_granted(self):
        """Any MinuteX user, not only members of the meeting's organisation."""
        self.tag("c-external")
        self.assertEqual(self.level(EXTERNAL), ws.ACCESS_LEVEL_NOTES)
        status, body = self.shared_view(EXTERNAL)
        self.assertEqual(status, 200)
        self.assertEqual(body["access"], "participant")

    def test_none_survives_re_tagging(self):
        self.tag("c-colleague")
        self.set_level(COLLEAGUE, "none")
        self.tag("c-colleague", speaker="1")
        self.tag("c-colleague", attendance=True)
        self.assertEqual(self.level(COLLEAGUE), ws.ACCESS_LEVEL_NONE)

    def test_untagging_the_last_row_revokes(self):
        self.tag("c-colleague")
        self.untag("0")
        self.assertIsNone(api._meeting_access_row(KEY, COLLEAGUE))
        with self.assertRaises(api.ApiError) as ctx:
            self.shared_view(COLLEAGUE)
        self.assertEqual(ctx.exception.status, 404)

    def test_untagging_one_of_two_rows_keeps_access(self):
        """Speaker + attendee rows for one person share one access state."""
        self.tag("c-colleague")
        _, body = self.tag("c-colleague", attendance=True)
        self.untag("0")
        self.assertEqual(self.level(COLLEAGUE), ws.ACCESS_LEVEL_NOTES)
        self.untag(body["participant"]["speaker_id"])
        self.assertIsNone(api._meeting_access_row(KEY, COLLEAGUE))

    def test_re_mapping_a_speaker_to_someone_else_moves_access(self):
        self.tag("c-colleague")
        self.tag("c-external")  # same slot "0"
        self.assertIsNone(api._meeting_access_row(KEY, COLLEAGUE))
        self.assertEqual(self.level(EXTERNAL), ws.ACCESS_LEVEL_NOTES)

    def test_participants_list_exposes_one_access_state_per_user(self):
        self.tag("c-colleague")
        self.tag("c-colleague", attendance=True)
        self.tag("c-nobody", speaker="1")
        self.tag("c-me", speaker="2")
        self.set_level(COLLEAGUE, "full")
        _, body = parse(api.list_participants(event(
            route="/recordings/participants/{key+}", key=KEY)))
        by_contact = {}
        for p in body["participants"]:
            by_contact.setdefault(p["contact_id"], []).append(p["access"])
        self.assertEqual(by_contact["c-colleague"],
                         [{"user_id": COLLEAGUE, "level": "full"}] * 2)
        self.assertEqual(by_contact["c-nobody"], [None])   # not on MinuteX
        self.assertEqual(by_contact["c-me"], [None])       # the recorder


# ===========================================================================
# 2. THE RECORDER'S CONTROL
# ===========================================================================
class TestRecorderControl(AccessHarness):
    def setUp(self):
        super().setUp()
        self.tag("c-colleague")
        self.notified.clear()
        self.audits.clear()

    def test_levels_round_trip_and_are_audited(self):
        for level in ("full", "none", "notes"):
            status, body = self.set_level(COLLEAGUE, level)
            self.assertEqual(status, 200)
            self.assertEqual(body["access"]["level"], level)
            self.assertEqual(self.level(COLLEAGUE), level.upper())
        self.assertEqual([a for a, _ in self.audits],
                         ["meeting.access_set"] * 3)

    def test_notifies_only_on_absent_or_none_to_access(self):
        self.set_level(COLLEAGUE, "full")      # NOTES -> FULL: no notify
        self.assertEqual(self.access_notifications(), [])
        self.set_level(COLLEAGUE, "none")
        self.set_level(COLLEAGUE, "notes")     # NONE -> NOTES: notify
        self.assertEqual(len(self.access_notifications()), 1)

    def test_only_the_recorder_may_call_it(self):
        self.set_level(COLLEAGUE, "full")
        for who in (COLLEAGUE, OWNER_A, MANAGER_A, EXTERNAL, OUTSIDER):
            with self.subTest(who=who):
                with self.assertRaises(api.ApiError) as ctx:
                    self.set_level(COLLEAGUE, "notes", as_user=who)
                self.assertEqual(ctx.exception.status, 404)
        self.assertEqual(self.level(COLLEAGUE), ws.ACCESS_LEVEL_FULL)

    def test_a_non_participant_cannot_be_granted(self):
        with self.assertRaises(api.ApiError) as ctx:
            self.set_level(OUTSIDER, "full")
        self.assertEqual(ctx.exception.status, 404)
        self.assertIsNone(api._meeting_access_row(KEY, OUTSIDER))

    def test_invalid_input_is_400(self):
        for body in ({"user_id": COLLEAGUE, "level": "owner"},
                     {"user_id": COLLEAGUE, "level": ""},
                     {"user_id": "", "level": "notes"},
                     {"user_id": RECORDER, "level": "notes"}):
            with self.subTest(body=body):
                with self.assertRaises(api.ApiError) as ctx:
                    self.as_user(RECORDER)
                    api.set_meeting_access(event(
                        method="PUT", route="/recordings/access/{key+}",
                        key=KEY, body=body))
                self.assertEqual(ctx.exception.status, 400)


# ===========================================================================
# 3. WHAT EACH LEVEL SEES
# ===========================================================================
class TestSharedViewByLevel(AccessHarness):
    def setUp(self):
        super().setUp()
        self.tag("c-colleague")

    def test_notes_gets_neither_transcript_nor_audio_and_no_presign(self):
        status, body = self.shared_view(COLLEAGUE)
        self.assertEqual(status, 200)
        self.assertEqual((body["access"], body["level"]),
                         ("participant", "notes"))
        m = body["meeting"]
        self.assertIsNone(m["transcript"])
        self.assertEqual(m["speaker_blocks"], [])
        self.assertIsNone(m["audio_url"])
        self.s3.generate_presigned_url.assert_not_called()
        self.assertNotIn("4.2 lakh", json.dumps(body))
        self.assertEqual(body["shared_by_name"], RECORDER)

    def test_full_gets_transcript_and_audio(self):
        self.set_level(COLLEAGUE, "full")
        _, body = self.shared_view(COLLEAGUE)
        self.assertEqual(body["level"], "full")
        m = body["meeting"]
        self.assertIn("4.2 lakh", m["transcript"])
        # Speaker 0 was tagged as the colleague, so the block carries the
        # contact's name (speaker_names sync), not the original "Ravi".
        self.assertEqual(m["speaker_blocks"][0]["speaker"], COLLEAGUE)
        self.assertEqual(m["audio_url"], SIGNED)
        self.assertNotIn(KEY, json.dumps(m))  # the raw S3 key never leaks

    def test_none_is_404(self):
        self.set_level(COLLEAGUE, "none")
        with self.assertRaises(api.ApiError) as ctx:
            self.shared_view(COLLEAGUE)
        self.assertEqual(ctx.exception.status, 404)

    def test_a_trashed_meeting_is_404(self):
        self.set_level(COLLEAGUE, "full")
        self.t["recordings"].update_item(
            Key={"audio_s3_key": KEY},
            UpdateExpression="SET recording_status = :s, deleted_at = :d",
            ExpressionAttributeValues={":s": "trashed", ":d": NOW})
        with self.assertRaises(api.ApiError) as ctx:
            self.shared_view(COLLEAGUE)
        self.assertEqual(ctx.exception.status, 404)

    def test_a_stranger_is_404(self):
        with self.assertRaises(api.ApiError) as ctx:
            self.shared_view(OUTSIDER)
        self.assertEqual(ctx.exception.status, 404)


# ===========================================================================
# 4. THE SECURITY INVARIANT
# ===========================================================================
OWNER_READ_ROUTES = [
    ("GET", route) for (method, route) in api._ROUTES
    if method == "GET" and route.endswith("{key+}")
    and "shared-with-me" not in route
]


class TestGrantIsNotMeetingAuthorization(AccessHarness):
    def setUp(self):
        super().setUp()
        self.tag("c-colleague")
        self.tag("c-external", speaker="1")

    def _levels(self):
        return (ws.ACCESS_LEVEL_NOTES, ws.ACCESS_LEVEL_FULL)

    def test_can_read_meeting_ignores_grants(self):
        for level in self._levels():
            self.set_level(COLLEAGUE, level.lower())
            self.set_level(EXTERNAL, level.lower())
            with self.subTest(level=level):
                self.assertEqual(api._can_read_meeting(COLLEAGUE, self.item), "")
                self.assertEqual(api._can_read_meeting(EXTERNAL, self.item), "")
                self.assertFalse(api._can_write_meeting(COLLEAGUE, self.item))

    def test_every_owner_read_route_404s_for_a_grantee(self):
        """GET /recordings/{key}, MoM, tasks, documents, chat, participants,
        shares, Gmail recipients, Salesforce status — none opens for a
        grantee at any level."""
        self.assertIn(("GET", "/recordings/{key+}"), OWNER_READ_ROUTES)
        self.assertIn(("GET", "/recordings/participants/{key+}"),
                      OWNER_READ_ROUTES)
        for level in ("notes", "full"):
            self.set_level(COLLEAGUE, level)
            for method, route in OWNER_READ_ROUTES:
                handler = api._ROUTES[(method, route)]
                with self.subTest(level=level, route=route):
                    self.as_user(COLLEAGUE)
                    try:
                        status, _ = parse(handler(event(
                            method=method, route=route, key=KEY)))
                    except api.ApiError as err:
                        status = err.status
                    self.assertNotEqual(status // 100, 2, route)

    def test_grantee_does_not_see_it_in_the_org_desk_list(self):
        self.set_level(COLLEAGUE, "full")
        self.as_user(COLLEAGUE)
        _, body = parse(api.list_recordings(event(
            route="/recordings", workspace=ORG_A)))
        self.assertNotIn(KEY, {r["audio_s3_key"] for r in body["recordings"]})


# ===========================================================================
# 5. FULL IS NEVER WRITE ACCESS
# ===========================================================================
WRITE_ROUTES = [
    (method, route) for (method, route) in api._ROUTES
    if method in ("POST", "PUT", "PATCH", "DELETE")
    and route.endswith("{key+}")
]


class TestFullIsNeverWriteAccess(AccessHarness):
    """A FULL grantee who knows the key is refused by every write route, and
    nothing about the meeting changes."""

    def setUp(self):
        super().setUp()
        self.tag("c-colleague")
        self.set_level(COLLEAGUE, "full")

    def test_the_route_table_has_the_write_routes_we_expect(self):
        for expected in (("PATCH", "/recordings/{key+}"),
                         ("DELETE", "/recordings/permanent/{key+}"),
                         ("PUT", "/recordings/ai/mom/{key+}"),
                         ("POST", "/recordings/ai/tasks/{key+}"),
                         ("PUT", "/recordings/participants/{key+}"),
                         ("POST", "/recordings/share/{key+}"),
                         ("PUT", "/recordings/access/{key+}")):
            self.assertIn(expected, WRITE_ROUTES)

    def test_every_write_route_refuses_a_full_grantee(self):
        before_row = json.dumps(self.t["recordings"].get_item(
            Key={"audio_s3_key": KEY})["Item"], sort_keys=True, default=str)
        before_parts = sorted(json.dumps(r, sort_keys=True) for r in
                              api._participant_rows(KEY))
        before_grants = json.dumps(api._meeting_access_rows(KEY),
                                   sort_keys=True)
        body = {"title": "Hijacked", "user_id": COLLEAGUE, "level": "full",
                "speaker_id": "0", "contact_id": "c-external",
                "message": "hi", "sections": [], "id": "t1"}
        for method, route in WRITE_ROUTES:
            handler = api._ROUTES[(method, route)]
            with self.subTest(route=f"{method} {route}"):
                self.as_user(COLLEAGUE)
                try:
                    status, _ = parse(handler(event(
                        method=method, route=route, key=KEY, body=body)))
                except api.ApiError as err:
                    status = err.status
                self.assertNotEqual(status // 100, 2, f"{method} {route}")
        self.assertEqual(json.dumps(self.t["recordings"].get_item(
            Key={"audio_s3_key": KEY})["Item"], sort_keys=True, default=str),
            before_row)
        self.assertEqual(sorted(json.dumps(r, sort_keys=True) for r in
                                api._participant_rows(KEY)), before_parts)
        self.assertEqual(json.dumps(api._meeting_access_rows(KEY),
                                    sort_keys=True), before_grants)


# ===========================================================================
# 6. THE RECORDER KEEPS OWNER PERMISSIONS
# ===========================================================================
class TestRecorderKeepsOwnerPermissions(AccessHarness):
    def setUp(self):
        super().setUp()
        self.tag("c-colleague")
        self.tag("c-external", speaker="1")
        self.set_level(COLLEAGUE, "full")

    def test_recorder_still_reads_and_writes(self):
        self.assertEqual(api._can_read_meeting(RECORDER, self.item), "OWNER")
        self.assertTrue(api._can_write_meeting(RECORDER, self.item))
        self.as_user(RECORDER)
        status, _ = parse(api.get_recording(event(
            route="/recordings/{key+}", key=KEY)))
        self.assertEqual(status, 200)

    def test_recorder_can_rename_and_tag(self):
        self.as_user(RECORDER)
        status, _ = parse(api.patch_recording(event(
            method="PATCH", route="/recordings/{key+}", key=KEY,
            body={"title": "Renamed by recorder"})))
        self.assertEqual(status, 200)
        self.assertEqual(self.t["recordings"].get_item(
            Key={"audio_s3_key": KEY})["Item"]["title"], "Renamed by recorder")
        status, _ = self.tag("c-nobody", speaker="2")
        self.assertEqual(status, 200)

    def test_recorder_passes_the_owner_gate_every_write_route_uses(self):
        # _owned_recording guards MoM save, task create and ~30 more; it must
        # still admit the recorder with grantees on the meeting.
        self.as_user(RECORDER)
        uid, key, _ = api._owned_recording(event(
            route="/recordings/ai/mom/{key+}", key=KEY))
        self.assertEqual((uid, key), (RECORDER, KEY))


# ===========================================================================
# 7. CLEANUP
# ===========================================================================
class TestCleanup(AccessHarness):
    def test_removed_org_member_loses_their_org_grants_only(self):
        self.tag("c-colleague")
        other_key = "recordings/u-ob/mobile/other.m4a"
        api._grant_meeting_access(other_key, COLLEAGUE, ws.ACCESS_PARTICIPANT,
                                  workspace_id="wso_bbbb2222")
        self.as_user(OWNER_A)
        api.remove_member(event(
            method="DELETE",
            route="/workspaces/{workspace_id}/members/{user_id}",
            path={"workspace_id": ORG_A, "user_id": COLLEAGUE}))
        self.assertIsNone(api._meeting_access_row(KEY, COLLEAGUE))
        self.assertIsNotNone(api._meeting_access_row(other_key, COLLEAGUE))

    def test_deleting_the_contact_revokes(self):
        self.tag("c-external")
        self.as_user(RECORDER)
        api.delete_contact(event(method="DELETE",
                                 route="/contacts/{contact_id}",
                                 path={"contact_id": "c-external"}))
        self.assertIsNone(api._meeting_access_row(KEY, EXTERNAL))

    def test_permanent_delete_drops_every_grant(self):
        self.tag("c-colleague")
        self.tag("c-external", speaker="1")
        self.as_user(RECORDER)
        api.permanently_delete_recording(event(
            method="DELETE", route="/recordings/permanent/{key+}", key=KEY))
        self.assertEqual(api._meeting_access_rows(KEY), {})


# ===========================================================================
# 8. SHARED WITH ME
# ===========================================================================
class TestSharedWithMeList(AccessHarness):
    def list_for(self, who):
        self.as_user(who)
        return parse(api.list_shared_with_me(event(
            route="/recordings/shared-with-me")))[1]["recordings"]

    def test_lists_granted_meetings_with_level_and_recorder(self):
        self.tag("c-colleague")
        rows = self.list_for(COLLEAGUE)
        self.assertEqual([r["audio_s3_key"] for r in rows], [KEY])
        self.assertEqual(rows[0]["access_level"], "notes")
        self.assertEqual(rows[0]["recorded_by"], RECORDER)

    def test_none_and_trash_are_left_out(self):
        self.tag("c-colleague")
        self.set_level(COLLEAGUE, "none")
        self.assertEqual(self.list_for(COLLEAGUE), [])
        self.set_level(COLLEAGUE, "full")
        self.t["recordings"].update_item(
            Key={"audio_s3_key": KEY},
            UpdateExpression="SET recording_status = :s, deleted_at = :d",
            ExpressionAttributeValues={":s": "trashed", ":d": NOW})
        self.assertEqual(self.list_for(COLLEAGUE), [])

    def test_follows_every_page_of_the_index(self):
        keys = []
        for i in range(5):
            k = f"recordings/u-mea/mobile/page_{i}.m4a"
            self.make_meeting(k, RECORDER, ORG_A)
            api._grant_meeting_access(k, COLLEAGUE, ws.ACCESS_PARTICIPANT,
                                      workspace_id=ORG_A)
            keys.append(k)
        real_query = self.t["meeting_access"].query
        calls = []

        def paged(**kw):
            # One row per page, forcing the LastEvaluatedKey path.
            calls.append(kw.get("ExclusiveStartKey"))
            return real_query(**{**kw, "Limit": 1})

        with mock.patch.object(self.t["meeting_access"], "query",
                               side_effect=paged):
            rows = self.list_for(COLLEAGUE)
        self.assertEqual(sorted(r["audio_s3_key"] for r in rows), sorted(keys))
        self.assertGreater(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
