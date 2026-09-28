#!/usr/bin/env python3
"""test_meeting_payload.py — the generic MinuteX meeting payload contract.

WHY THIS FILE HAS NO AWS STUBS. shared/meeting_payload.py is deliberately
pure: no boto3, no network, no Salesforce client, no DynamoDB. That is the
whole point of extracting it, and this file proves it — it imports the module
directly and drives it with plain dicts. If a future change to
meeting_payload.py makes this file need a stub, that change has broken the
property the module exists to guarantee.

WHAT IS BEING PROTECTED HERE:
  1. The CONTRACT. A customer's Salesforce Flow reads these key names. A
     rename is a breaking change for every customer at once, so the golden
     test asserts the exact top-level shape rather than just spot-checking.
  2. The ALLOW-LIST. The recording row carries user_id, device_id, S3 keys
     and chat history. The payload must never contain them, and a future
     author adding an attribute to the row must not be able to leak it by
     forgetting this file exists.
  3. The LEGACY SHAPES. Production rows exist in three highlight shapes and
     two recorded_at formats. Delivering an empty block for the older ones is
     exactly the silent-empty class of bug the transcript hydration fix
     addressed, so each shape has a test.

Run:  python -m pytest tests/test_meeting_payload.py
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))

import meeting_payload as mp  # noqa: E402


KEY = "recordings/user-1/mobile/meet-abc_1750000000"


def realistic_item(**overrides):
    """A meeting row shaped like production: offloaded transcript hydrated
    back in, Dynamic Overview, structured highlights, speaker names."""
    item = {
        "audio_s3_key": KEY,
        "user_id": "user-1",
        "device_id": "dev-9",
        "workspace_id": "wso_1234567890abcdef",
        "title": "Q3 pipeline review with Northwind",
        "status": "complete",
        "source": "MOBILE",
        "language": "en",
        "recorded_at": "1750000000",
        "duration_seconds": 1800,
        "created_at": "2025-06-15T10:00:00Z",
        "summary": "Northwind confirmed budget for Q3 and asked for revised pricing.",
        "overview": {
            "sections": [
                {"id": "s1", "title": "Budget", "kind": "text",
                 "content": "Budget approved at 120k.", "items": []},
                {"id": "s2", "title": "Risks", "kind": "list", "content": "",
                 "items": ["Legal review may slip", "Competitor evaluation ongoing"]},
            ]
        },
        "meeting_highlights": {
            "decisions": [
                {"decision": "Proceed with the 120k package",
                 "context": "After reviewing the tiering", "speaker_id": "0"},
            ],
            "deadlines": [{"what": "Send revised pricing", "when": "Friday"}],
            "open_questions": ["Who signs off on legal?"],
        },
        "participants": [
            {"speaker": "0", "summary": "Led the pricing discussion."},
            {"speaker": "1", "summary": "Raised the legal timeline."},
        ],
        "speaker_names": {"0": "Priya Raman", "1": "Tom Shaw"},
        "transcript": "Speaker 1: Let us start.\nSpeaker 2: Agreed.",
        "timestamps": [
            {"id": "seg_0", "speaker": "0", "start": 0.0, "end": 2.5,
             "text": "Let us start."},
            {"id": "seg_1", "speaker": "1", "start": 2.5, "end": 4.0,
             "text": "Agreed."},
        ],
        # Attributes that must NEVER reach the payload.
        "transcript_s3_key": f"transcripts/{KEY}.transcript.json.gz",
        "chat_history": {"t1": "something private"},
        "crm_records": {"Lead": {"record_id": "00Q1"}},
    }
    item.update(overrides)
    return item


def realistic_tasks():
    return [
        {"task_id": "t-1", "title": "Send revised pricing",
         "description": "Include the 120k tier.", "status": "OPEN",
         "priority": "HIGH", "due_date": "Friday",
         "due_date_normalized": "2025-06-20",
         "assignee_name": "Priya Raman", "assignee_email": "priya@acme.test",
         "assignee_speaker_id": "0", "assignee_contact_id": "c-1",
         "assignee_user_id": "user-1", "resolution_status": "RESOLVED",
         "source_type": "ai", "created_at": "2025-06-15T11:00:00Z"},
        {"task_id": "t-2", "title": "Book legal review", "status": "OPEN",
         "assignee_name": "Tom Shaw", "resolution_status": "UNRESOLVED"},
        # No title -> must be dropped, not delivered as a blank action item.
        {"task_id": "t-3", "title": "   "},
    ]


def realistic_identity():
    return {
        "speakers": [
            {"speaker_id": "0", "identity_role": "INTERNAL", "status": "RESOLVED",
             "contact_id": "c-1", "minutex_user_id": "user-1",
             "sf_user_id": "005xx0000012345", "contact_name": "Priya Raman"},
            {"speaker_id": "1", "identity_role": "EXTERNAL", "status": "NOT_LINKED",
             "contact_id": "c-2", "contact_name": "Tom Shaw"},
        ],
    }


class PayloadContract(unittest.TestCase):
    """The shape customers build automation against."""

    def setUp(self):
        self.payload = mp.build_meeting_payload(
            realistic_item(), realistic_tasks(), realistic_identity(),
            meeting_url="https://app.minutex.test/m/abc",
            workspace_id="wso_1234567890abcdef")

    def test_top_level_shape_is_the_contract(self):
        # GOLDEN. Changing this set is a customer-facing breaking change.
        self.assertEqual(sorted(self.payload), sorted([
            "payload_version", "source", "meeting", "summary", "overview",
            "highlights", "decisions", "participants", "action_items",
            "transcript",
        ]))

    def test_payload_version_is_declared(self):
        self.assertEqual(self.payload["payload_version"], "1.0")
        self.assertEqual(self.payload["source"], "minutex")

    def test_meeting_block(self):
        m = self.payload["meeting"]
        self.assertEqual(m["minutex_meeting_id"], KEY)
        self.assertEqual(m["title"], "Q3 pipeline review with Northwind")
        self.assertEqual(m["status"], "complete")
        self.assertEqual(m["duration_seconds"], 1800)
        self.assertEqual(m["minutex_url"], "https://app.minutex.test/m/abc")
        self.assertEqual(m["minutex_workspace_id"], "wso_1234567890abcdef")

    def test_epoch_recorded_at_becomes_iso_with_derived_end(self):
        m = self.payload["meeting"]
        self.assertTrue(m["started_at"].endswith("Z"), m["started_at"])
        self.assertTrue(m["started_at"].startswith("2025-"), m["started_at"])
        # end = start + 1800s, so the two must differ and both be ISO.
        self.assertTrue(m["ended_at"].endswith("Z"))
        self.assertNotEqual(m["started_at"], m["ended_at"])

    def test_iso_recorded_at_is_accepted_too(self):
        """Older rows carry ISO, not epoch. Both are live in production."""
        p = mp.build_meeting_payload(
            realistic_item(recorded_at="2025-06-15T10:00:00Z"))
        self.assertEqual(p["meeting"]["started_at"], "2025-06-15T10:00:00Z")

    def test_unparseable_recorded_at_does_not_fail_delivery(self):
        p = mp.build_meeting_payload(realistic_item(recorded_at="not a date"))
        self.assertEqual(p["meeting"]["started_at"], "")
        self.assertEqual(p["meeting"]["ended_at"], "")


class SensitiveAttributes(unittest.TestCase):
    """The allow-list property: assembled, never filtered."""

    def test_internal_attributes_never_appear_anywhere(self):
        payload = mp.build_meeting_payload(
            realistic_item(), realistic_tasks(), realistic_identity())
        import json
        blob = json.dumps(payload)
        # NB: "user-1" is legitimately present as an assignee/participant
        # correlation hint, so it is not in this list. The device id, the S3
        # pointers, chat history and CRM linkage state have no business in a
        # customer-facing payload at all.
        for forbidden in ("dev-9", "transcripts/", "chat_history",
                          "something private", "crm_records", "00Q1"):
            self.assertNotIn(forbidden, blob,
                             f"{forbidden!r} leaked into the payload")

    def test_no_s3_key_or_presigned_url_in_payload(self):
        import json
        blob = json.dumps(mp.build_meeting_payload(realistic_item()))
        self.assertNotIn("X-Amz-Signature", blob)
        self.assertNotIn(".transcript.json.gz", blob)

    def test_payload_is_json_serializable_with_decimals(self):
        """DynamoDB hands back Decimal; json.dumps cannot serialize it, and a
        delivery that fails at serialize time is a failure _num removes."""
        from decimal import Decimal
        import json
        item = realistic_item(duration_seconds=Decimal("1800"))
        item["timestamps"][0]["start"] = Decimal("0")
        item["timestamps"][0]["end"] = Decimal("2.5")
        json.dumps(mp.build_meeting_payload(item))  # must not raise


class Outputs(unittest.TestCase):
    def test_overview_emits_structure_and_text(self):
        o = mp.build_meeting_payload(realistic_item())["overview"]
        self.assertEqual(len(o["sections"]), 2)
        self.assertEqual(o["sections"][1]["items"][0], "Legal review may slip")
        self.assertIn("Budget:", o["text"])
        self.assertIn("- Legal review may slip", o["text"])

    def test_decisions_are_promoted_to_top_level(self):
        d = mp.build_meeting_payload(realistic_item())["decisions"]
        self.assertEqual(len(d), 1)
        self.assertEqual(d[0]["decision"], "Proceed with the 120k package")
        self.assertEqual(d[0]["speaker_id"], "0")

    def test_highlights_carry_deadlines_and_questions(self):
        h = mp.build_meeting_payload(realistic_item())["highlights"]
        self.assertEqual(h["deadlines"][0]["what"], "Send revised pricing")
        self.assertEqual(h["open_questions"], ["Who signs off on legal?"])

    def test_legacy_flat_highlights_still_deliver(self):
        """A row analysed before the structured schema must not deliver an
        empty highlights block."""
        item = realistic_item()
        del item["meeting_highlights"]
        item["highlights"] = ["Budget approved", "Legal pending"]
        p = mp.build_meeting_payload(item)
        self.assertEqual(p["highlights"]["items"],
                         ["Budget approved", "Legal pending"])
        self.assertEqual(p["decisions"], [])

    def test_action_items_use_authoritative_task_rows(self):
        items = mp.build_meeting_payload(
            realistic_item(), realistic_tasks())["action_items"]
        self.assertEqual(len(items), 2)  # the untitled one is dropped
        first = items[0]
        self.assertEqual(first["minutex_task_id"], "t-1")
        self.assertEqual(first["due_date_normalized"], "2025-06-20")
        self.assertEqual(first["assignee"]["email"], "priya@acme.test")

    def test_action_items_fall_back_to_embedded_map(self):
        item = realistic_item(tasks={"t-9": {"task_id": "t-9",
                                             "title": "Legacy embedded task"}})
        items = mp.build_meeting_payload(item, tasks=None)["action_items"]
        self.assertEqual([i["title"] for i in items], ["Legacy embedded task"])

    def test_action_items_carry_no_salesforce_field_names(self):
        """The contract must not smuggle the old hardcoded Task mapping."""
        import json
        blob = json.dumps(mp.build_meeting_payload(
            realistic_item(), realistic_tasks()))
        for sf_field in ("Subject", "ActivityDate", "OwnerId", "WhoId", "WhatId"):
            self.assertNotIn(f'"{sf_field}"', blob)


class Participants(unittest.TestCase):
    def test_identity_is_hints_not_a_gate(self):
        """An UNRESOLVED participant must still be delivered, with whatever
        hints exist — never withheld, and never withholding the meeting."""
        p = mp.build_meeting_payload(
            realistic_item(), realistic_tasks(), realistic_identity())
        people = {x["speaker_id"]: x for x in p["participants"]}
        self.assertEqual(set(people), {"0", "1"})

        resolved = people["0"]
        self.assertEqual(resolved["display_name"], "Priya Raman")
        self.assertEqual(resolved["salesforce_user_id"], "005xx0000012345")
        self.assertEqual(resolved["identity_role"], "INTERNAL")

        unresolved = people["1"]
        self.assertEqual(unresolved["resolution_status"], "NOT_LINKED")
        self.assertEqual(unresolved["salesforce_contact_id"], "")
        # Still delivered with its useful hints.
        self.assertEqual(unresolved["display_name"], "Tom Shaw")
        self.assertEqual(unresolved["contribution_summary"],
                         "Raised the legal timeline.")

    def test_missing_identity_costs_hints_not_the_meeting(self):
        p = mp.build_meeting_payload(realistic_item(), realistic_tasks(),
                                     identity=None)
        self.assertEqual(len(p["participants"]), 2)
        self.assertEqual(p["participants"][0]["display_name"], "Priya Raman")
        self.assertEqual(p["participants"][0]["salesforce_user_id"], "")

    def test_speaker_known_only_to_identity_is_still_delivered(self):
        item = realistic_item(participants=[], speaker_names={})
        p = mp.build_meeting_payload(item, identity=realistic_identity())
        self.assertEqual({x["speaker_id"] for x in p["participants"]}, {"0", "1"})


class Transcript(unittest.TestCase):
    def test_hydrated_transcript_is_delivered_with_segments(self):
        t = mp.build_meeting_payload(realistic_item())["transcript"]
        self.assertTrue(t["available"])
        self.assertFalse(t["truncated"])
        self.assertIn("Let us start.", t["text"])
        self.assertEqual(t["segment_count"], 2)
        self.assertEqual(t["segments"][0]["id"], "seg_0")
        self.assertEqual(t["segments"][0]["speaker_id"], "0")

    def test_unhydrated_row_reports_available_but_empty(self):
        """THE REGRESSION SHAPE. A row that still has only the S3 pointer must
        report available=True with empty text — never available=False, which
        would read as 'this meeting had no transcript' and is exactly how the
        original hydration bug stayed invisible."""
        item = realistic_item()
        del item["transcript"]
        del item["timestamps"]
        t = mp.build_meeting_payload(item)["transcript"]
        self.assertTrue(t["available"], "pointer row must report available")
        self.assertEqual(t["text"], "")
        self.assertEqual(t["total_chars"], 0)

    def test_oversized_transcript_is_truncated_and_says_so(self):
        # .strip()ped by _s, so the stored length is the trimmed one — assert
        # against that rather than the raw literal.
        big = ("word " * 20000).strip()
        t = mp.build_meeting_payload(realistic_item(transcript=big))["transcript"]
        self.assertTrue(t["truncated"])
        self.assertEqual(len(t["text"]), mp.MAX_INLINE_TRANSCRIPT_CHARS)
        self.assertEqual(t["total_chars"], len(big))
        self.assertTrue(t["available"])

    def test_transcript_can_be_excluded_by_policy(self):
        p = mp.build_meeting_payload(realistic_item(), include_transcript=False)
        t = p["transcript"]
        self.assertEqual(t["text"], "")
        self.assertTrue(t["excluded_by_policy"])
        # Still honest that a transcript exists — just not delivered.
        self.assertTrue(t["available"])
        # Everything else is unaffected by the privacy control.
        self.assertEqual(p["summary"], realistic_item()["summary"])
        self.assertEqual(len(p["decisions"]), 1)

    def test_included_transcript_marks_policy_false(self):
        t = mp.build_meeting_payload(realistic_item())["transcript"]
        self.assertFalse(t["excluded_by_policy"])


class Robustness(unittest.TestCase):
    def test_empty_meeting_still_produces_a_valid_payload(self):
        p = mp.build_meeting_payload({})
        self.assertEqual(p["payload_version"], "1.0")
        self.assertEqual(p["meeting"]["minutex_meeting_id"], "")
        self.assertEqual(p["participants"], [])
        self.assertEqual(p["action_items"], [])
        self.assertFalse(p["transcript"]["available"])

    def test_non_dict_input_does_not_raise(self):
        self.assertEqual(mp.build_meeting_payload(None)["payload_version"], "1.0")

    def test_malformed_nested_rows_are_skipped_not_fatal(self):
        item = realistic_item(
            participants=["not a dict", {"speaker": "0"}],
            overview={"sections": ["bad", {"title": "Good", "kind": "text",
                                           "content": "ok", "items": []}]},
        )
        p = mp.build_meeting_payload(item, tasks=["bad", {"title": "Fine"}])
        self.assertEqual(len(p["overview"]["sections"]), 1)
        self.assertEqual([i["title"] for i in p["action_items"]], ["Fine"])

    def test_title_is_bounded(self):
        p = mp.build_meeting_payload(realistic_item(title="x" * 900))
        self.assertEqual(len(p["meeting"]["title"]), mp.MAX_TITLE_CHARS)

    def test_payload_counts_summarizes_without_leaking_content(self):
        counts = mp.payload_counts(mp.build_meeting_payload(
            realistic_item(), realistic_tasks(), realistic_identity()))
        self.assertEqual(counts["participants"], 2)
        self.assertEqual(counts["action_items"], 2)
        self.assertEqual(counts["decisions"], 1)
        self.assertEqual(counts["overview_sections"], 2)
        self.assertFalse(counts["transcript_truncated"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
