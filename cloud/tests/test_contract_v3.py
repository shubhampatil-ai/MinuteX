#!/usr/bin/env python3
"""test_contract_v3.py — Salesforce Contract v3 shapes (pure).

Pins: links (1-5, exactly one primary, no duplicates, any object name),
included outputs (an excluded output is absent from `data`, contributions
only with participants), the per-task related record, recorded_by, the
transcript travelling only as a File (never refusing the snapshot), file
keys and the POST /files body, section hashing, and the envelope.

Run:  python -m pytest tests/test_contract_v3.py
"""
import base64
import hashlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))

import contract_v3 as c3  # noqa: E402
import site_contract as sc  # noqa: E402
import webhook_contract as wc  # noqa: E402

KEY = "recordings/u1/mobile/m_1757000000.wav"
MID = sc.opaque_meeting_id(KEY)
ITEM = {"audio_s3_key": KEY, "title": "Site visit", "status": "complete",
        "transcript": "Speaker 0: The lobby needs paint.", "summary": "Paint lobby.",
        "created_at": "2026-09-20T10:00:00Z", "language": "en"}
TASKS = [{"task_id": "tsk_1", "title": "Share the floor plan", "status": "open"},
         {"task_id": "tsk_2", "title": "Send loan options", "status": "open"}]
RAHUL = {"speaker_id": "1", "name": "Rahul Sharma", "email": "rahul@example.com",
         "contribution_summary": "Asked about parking.", "tagged": True}
PROPERTY = {"object": "Property__c", "id": "a0X5g00000AbCdEAAZ", "name": "Mumbai Heights",
            "primary": True}
LEAD = {"object": "Lead", "id": "00Q5g00000XyZabEAF", "name": "Rahul Sharma"}
ALL = list(c3.OUTPUTS)


def snapshot(outputs=ALL, **kw):
    kw.setdefault("participants", [RAHUL])
    return c3.build_snapshot(ITEM, TASKS, included_outputs=outputs, **kw)


class LinkTests(unittest.TestCase):
    def test_one_link_becomes_primary(self):
        links = c3.normalize_links([{**LEAD}])
        self.assertEqual(links, [{"object": "Lead", "id": LEAD["id"],
                                  "name": "Rahul Sharma", "primary": True}])

    def test_any_object_name_works(self):
        for obj in ("Lead", "Property__c", "acme__Unit__c", "Opportunity"):
            with self.subTest(obj=obj):
                self.assertEqual(c3.normalize_links(
                    [{"object": obj, "id": "a0X5g00000AbCdE"}])[0]["object"], obj)

    @unittest.skipIf(c3.MAX_LINKS < 2, "one record per meeting: no primary to choose")
    def test_several_links_need_exactly_one_primary(self):
        self.assertEqual([l["primary"] for l in c3.normalize_links([PROPERTY, LEAD])],
                         [True, False])
        for bad in ([{**PROPERTY, "primary": False}, LEAD],
                    [PROPERTY, {**LEAD, "primary": True}]):
            with self.assertRaises(c3.ContractError) as cm:
                c3.normalize_links(bad)
            self.assertEqual(cm.exception.code, "invalid_primary")

    def test_limits_and_duplicates(self):
        over = [{"object": "Lead", "id": f"00Q5g00000XyZa{i}"} for i in range(c3.MAX_LINKS + 1)]
        over[0]["primary"] = True
        with self.assertRaises(c3.ContractError) as cm:
            c3.normalize_links(over)
        self.assertEqual(cm.exception.code, "too_many_links")
        self.assertIn(c3.records(c3.MAX_LINKS), cm.exception.message)
        self.assertEqual(len(c3.normalize_links(over[:c3.MAX_LINKS])), c3.MAX_LINKS)
        self.assertEqual(c3.link_config_block([])["max_links"], c3.MAX_LINKS)
        with self.assertRaises(c3.ContractError) as cm:
            c3.normalize_links([])
        self.assertEqual(cm.exception.code, "invalid_links")
        # The 15- and 18-character forms of one id are the same record.
        if c3.MAX_LINKS >= 2:
            with self.assertRaises(c3.ContractError) as cm:
                c3.normalize_links([PROPERTY, {"object": "Property__c", "id": PROPERTY["id"][:15]}])
            self.assertEqual(cm.exception.code, "duplicate_link")

    def test_rejects_bad_ids_and_object_names(self):
        for bad in ({"object": "Lead", "id": "00Q123"},
                    {"object": "Lead; DELETE", "id": LEAD["id"]},
                    {"object": "", "id": LEAD["id"]},
                    "00Q5g00000XyZabEAF"):
            with self.subTest(bad=bad), self.assertRaises(c3.ContractError):
                c3.normalize_links([bad])


class OutputTests(unittest.TestCase):
    def test_canonical_order_and_dedupe(self):
        self.assertEqual(c3.normalize_outputs(["tasks", "overview", "tasks"]),
                         ["overview", "tasks"])

    def test_unknown_output_and_orphan_contributions_are_refused(self):
        for bad in (["summary"], ["participant_contributions"], "overview"):
            with self.subTest(bad=bad), self.assertRaises(c3.ContractError):
                c3.normalize_outputs(bad)

    def test_excluded_outputs_are_absent_not_empty(self):
        data = snapshot(["overview"])
        self.assertIn("summary", data)
        for absent in ("minutes", "action_items", "participants", "audio"):
            self.assertNotIn(absent, data)
        self.assertFalse(data["transcript"]["included"])

    def test_everything_included(self):
        data = snapshot(ALL, minutes_markdown="## Minutes")
        for present in ("summary", "action_items", "participants", "audio"):
            self.assertIn(present, data)
        # The MoM is a PDF File only: its text never travels.
        self.assertNotIn("minutes", data)
        self.assertNotIn("## Minutes", str(data))
        self.assertTrue(data["transcript"]["included"])
        self.assertEqual(data["participants"][0]["contribution_summary"],
                         "Asked about parking.")

    def test_participants_without_contributions(self):
        data = snapshot(["participants"])
        self.assertEqual(data["participants"][0]["name"], "Rahul Sharma")
        self.assertEqual(data["participants"][0]["contribution_summary"], "")


class SnapshotTests(unittest.TestCase):
    def test_the_transcript_goes_as_text_only_when_included(self):
        data = c3.build_snapshot({**ITEM, "transcript": "Speaker 0: hello"}, [],
                                 included_outputs=["overview", "transcript"], participants=[],
                                 transcript_body="[00:00:05] Amit: hello")
        self.assertEqual(data["transcript"]["text"], "[00:00:05] Amit: hello")
        self.assertEqual(data["transcript"]["parts"], ["[00:00:05] Amit: hello"])
        self.assertFalse(data["transcript"]["truncated"])
        self.assertEqual(data["files"], [], "never a file")
        off = c3.build_snapshot({**ITEM, "transcript": "Speaker 0: hello"}, [],
                                included_outputs=["overview"], participants=[])
        self.assertNotIn("text", off["transcript"])
        with self.assertRaises(c3.ContractError):
            c3.file_key("mxm_1", "transcript")

    @staticmethod
    def lines(n):
        return "\n".join(f"[00:{i % 60:02d}:00] Amit: line {i} " + "x" * 80 for i in range(n))

    def test_a_long_transcript_fills_the_next_fields_at_line_ends(self):
        text = self.lines(3000)                                  # ~300k: three fields
        huge = {**ITEM, "transcript": text}
        with self.assertRaises(sc.TranscriptTooLarge):       # v2 refuses it
            wc.build_snapshot(huge, [], participants=[])
        data = c3.build_snapshot(huge, [], included_outputs=ALL, participants=[])
        parts = data["transcript"]["parts"]
        self.assertEqual(len(parts), 3)
        self.assertFalse(data["transcript"]["truncated"])
        self.assertEqual("\n".join(parts), text, "nothing lost, nothing repeated")
        for p in parts:
            self.assertLessEqual(sc.text_length(p), c3.MAX_TRANSCRIPT_CHARS)
            self.assertTrue(p.startswith("[00:") and p.endswith("x"), "split at line ends")
        self.assertEqual(data["transcript"]["text"], parts[0], "part 1 for older receivers")

    def test_past_the_last_field_it_is_cut_with_a_note_never_refused(self):
        text = self.lines(6000)                                  # ~600k: more than four
        data = c3.build_snapshot({**ITEM, "transcript": text}, [], included_outputs=ALL,
                                 participants=[])
        parts = data["transcript"]["parts"]
        self.assertEqual(len(parts), c3.TRANSCRIPT_FIELDS)
        self.assertTrue(data["transcript"]["truncated"])
        self.assertTrue(parts[-1].endswith(c3.TRANSCRIPT_CUT_NOTE))
        self.assertLessEqual(sc.text_length(parts[-1]), c3.MAX_TRANSCRIPT_CHARS)
        self.assertEqual(data["transcript"]["chars"], sc.text_length(text))

    def test_recorded_by(self):
        data = snapshot(recorded_by={"name": " Amit Verma ", "email": "Amit@Example.com"})
        self.assertEqual(data["meeting"]["recorded_by"],
                         {"name": "Amit Verma", "email": "amit@example.com"})
        self.assertIsNone(snapshot()["meeting"]["recorded_by"])
        self.assertEqual(c3.normalize_recorded_by({"name": "A", "email": "not-an-email"}),
                         {"name": "A", "email": ""})

    def test_task_related_defaults_to_primary(self):
        data = snapshot(task_relations={"tsk_2": {"object": "Lead", "id": LEAD["id"]}})
        related = {t["id"]: t["related"] for t in data["action_items"]}
        self.assertIsNone(related["tsk_1"])
        self.assertEqual(related["tsk_2"], {"object": "Lead", "id": LEAD["id"]})
        with self.assertRaises(c3.ContractError):
            snapshot(task_relations={"tsk_1": {"object": "Lead", "id": "bad"}})

    def test_meeting_identity_is_v2s(self):
        self.assertEqual(snapshot()["meeting"]["id"], MID)


class FileTests(unittest.TestCase):
    def test_file_key_is_stable_per_meeting_and_kind(self):
        self.assertEqual(c3.file_key(MID, "mom_pdf"), f"{MID}:mom_pdf")
        with self.assertRaises(c3.ContractError):
            c3.file_key(MID, "audio")

    def test_file_request_body(self):
        body = c3.file_request(meeting_id=MID, kind="mom_pdf", content=b"%PDF-1.7",
                               filename="MoM.pdf", request_id="r1")
        self.assertEqual(body["type"], "file.upload")
        self.assertEqual(body["content_type"], "application/pdf")
        self.assertEqual(base64.b64decode(body["content_base64"]), b"%PDF-1.7")
        self.assertEqual(body["sha256"], hashlib.sha256(b"%PDF-1.7").hexdigest())
        self.assertEqual(body["key"], f"{MID}:mom_pdf")

    def test_empty_and_oversize_files_are_refused(self):
        with self.assertRaises(c3.ContractError) as cm:
            c3.file_request(meeting_id=MID, kind="mom_pdf", content=b"",
                            filename="x.pdf", request_id="r")
        self.assertEqual(cm.exception.code, "empty_file")
        with self.assertRaises(c3.ContractError) as cm:
            c3.file_request(meeting_id=MID, kind="mom_pdf",
                            content=b"x" * (c3.MAX_FILE_BYTES + 1),
                            filename="x.pdf", request_id="r")
        self.assertEqual(cm.exception.code, "file_too_large")

    def test_files_only_with_their_output(self):
        ref = c3.file_ref(MID, "mom_pdf", b"%PDF")
        self.assertEqual(snapshot(files=[ref])["files"][0]["key"], f"{MID}:mom_pdf")
        with self.assertRaises(c3.ContractError):
            snapshot(["overview"], files=[ref])
        with self.assertRaises(c3.ContractError):
            snapshot(files=[{**ref, "key": "mxm_other:mom_pdf"}])


class HashAndEnvelopeTests(unittest.TestCase):
    def test_link_and_output_changes_are_their_own_sections(self):
        data = snapshot()
        links = c3.normalize_links([PROPERTY])
        base = c3.section_hashes(data, links, ALL)
        other = [PROPERTY, LEAD] if c3.MAX_LINKS >= 2 else [LEAD]
        more = c3.section_hashes(data, c3.normalize_links(other), ALL)
        fewer = c3.section_hashes(snapshot(["overview"]), links, ["overview"])
        self.assertEqual(c3.changed_fields(base, more), ["links"])
        self.assertIn("included_outputs", c3.changed_fields(base, fewer))
        self.assertNotEqual(c3.content_hash(base), c3.content_hash(more))
        self.assertEqual(c3.changed_fields({}, base), ["*"])

    def test_audio_is_excluded_from_hashes(self):
        a = snapshot(audio=sc.audio_block("https://x/a1"))
        b = snapshot(audio=sc.audio_block("https://x/a2"))
        self.assertEqual(c3.section_hashes(a), c3.section_hashes(b))

    def test_envelope(self):
        env = c3.event_envelope(c3.EVENT_UPDATED, meeting_id=MID, sequence=4,
                                occurred_at="2026-10-03T10:00:00Z",
                                links=[PROPERTY, LEAD][:c3.MAX_LINKS],
                                included_outputs=["tasks", "overview"], data=snapshot())
        self.assertEqual(env["contract_version"], "3.0")
        self.assertEqual(env["event_id"], wc.event_id(MID, 4))
        self.assertEqual(env["included_outputs"], ["overview", "tasks"])
        self.assertEqual(len(env["links"]), min(2, c3.MAX_LINKS))
        for v2_only in ("meeting_type", "record_context"):
            self.assertNotIn(v2_only, env)

    def test_deleted_envelope_needs_no_links(self):
        env = c3.event_envelope(c3.EVENT_DELETED, meeting_id=MID, sequence=9,
                                occurred_at="t", data=c3.deleted_snapshot(MID, "t"))
        self.assertEqual((env["links"], env["included_outputs"]), ([], []))

    def test_live_event_needs_links(self):
        with self.assertRaises(c3.ContractError):
            c3.event_envelope(c3.EVENT_UPDATED, meeting_id=MID, sequence=1,
                              occurred_at="t", included_outputs=ALL, data={})


class OverviewTests(unittest.TestCase):
    def test_only_the_overview_text_travels(self):
        data = c3.build_snapshot(ITEM, [], included_outputs=["overview"], participants=[],
                                 overview_body="Decisions:\n- Paint the lobby")
        self.assertEqual(data["summary"], {"text": "Decisions:\n- Paint the lobby", "truncated": False})
        for gone in ("overview", "highlights", "decisions"):
            self.assertNotIn(gone, data["summary"])

    def test_without_an_overview_the_stored_summary_is_used(self):
        data = c3.build_snapshot(ITEM, [], included_outputs=["overview"], participants=[])
        self.assertEqual(data["summary"]["text"], "Paint lobby.")

    def test_a_long_overview_is_cut_at_a_line_end_with_a_note(self):
        body = "\n".join(f"- point {i} " + "y" * 90 for i in range(600))   # ~60k
        data = c3.build_snapshot(ITEM, [], included_outputs=["overview"], participants=[],
                                 overview_body=body)
        text = data["summary"]["text"]
        self.assertTrue(data["summary"]["truncated"])
        self.assertLessEqual(sc.text_length(text), c3.MAX_OVERVIEW_CHARS)
        self.assertTrue(text.endswith(c3.OVERVIEW_CUT_NOTE))
        self.assertTrue(text[:-len(c3.OVERVIEW_CUT_NOTE)].rstrip().endswith("y"))

    def test_not_sent_when_unticked(self):
        data = c3.build_snapshot(ITEM, [], included_outputs=["tasks"], participants=[],
                                 overview_body="Decisions:\n- Paint")
        self.assertNotIn("summary", data)


class DocumentFileTests(unittest.TestCase):
    def test_a_document_file_is_keyed_by_its_type(self):
        self.assertEqual(c3.file_key("mxm_1", c3.FILE_DOCUMENT, "follow_up_email"),
                         "mxm_1:document:follow_up_email")
        self.assertEqual(c3.split_file_id(c3.file_id(c3.FILE_DOCUMENT, "custom_ab12")),
                         ("document", "custom_ab12"))
        req = c3.file_request(meeting_id="mxm_1", kind=c3.FILE_DOCUMENT, content=b"%PDF",
                              filename="Follow-up.pdf", request_id="r", name="follow_up_email")
        self.assertEqual((req["key"], req["filename"], req["content_type"]),
                         ("mxm_1:document:follow_up_email", "Follow-up.pdf", "application/pdf"))

    def test_the_mom_mirror_and_bad_types_are_not_documents(self):
        for bad in ("", "minutes_of_meeting", "Has Spaces", "x" * 65):
            with self.subTest(bad=bad), self.assertRaises(c3.ContractError):
                c3.file_key("mxm_1", c3.FILE_DOCUMENT, bad)

    def test_several_documents_may_travel_together(self):
        refs = [c3.file_ref_for("mxm_1", c3.file_id(c3.FILE_DOCUMENT, t), b"%PDF")
                for t in ("executive_summary", "follow_up_email")]
        self.assertEqual(len(c3._normalize_files(refs, ["documents"], "mxm_1")), 2)
        with self.assertRaises(c3.ContractError):
            c3._normalize_files(refs, ["overview"], "mxm_1")


if __name__ == "__main__":
    unittest.main()


class LinkPolicyTests(unittest.TestCase):
    OBJS = c3.normalize_linkable_objects([
        {"object": "Property__c", "label": "Property", "can_be_primary": True,
         "outputs_allowed": list(c3.OUTPUTS), "outputs_default_on": ["overview", "tasks"],
         "outputs_user_can_change": ["overview", "mom"]},
        {"object": "Lead", "label": "Lead", "can_be_primary": True,
         "outputs_allowed": ["overview", "tasks"], "outputs_default_on": ["overview"],
         "outputs_user_can_change": ["overview", "tasks"]},
        {"object": "bad name!", "label": "x"}, "junk",
    ])

    def test_normalize_skips_bad_rows_and_bounds_outputs(self):
        self.assertEqual([o["object"] for o in self.OBJS], ["Property__c", "Lead"])
        self.assertIsNone(c3.normalize_linkable_objects(None))

    def test_locked_outputs_take_the_default(self):
        prop = [{"object": "Property__c", "id": "a0X5g00000AbCdEAAZ", "primary": True}]
        _l, outs, _r = c3.apply_link_policy(prop, ["overview"], {}, self.OBJS)
        # tasks is locked on Property and on by default; participants locked and off.
        self.assertEqual(outs, ["overview", "tasks"])

    def test_an_output_changeable_on_any_linked_object_follows_the_user(self):
        both = [{"object": "Property__c", "id": "a0X5g00000AbCdEAAZ", "primary": True},
                {"object": "Lead", "id": "00Q5g00000XyZabEAF"}]
        _l, outs, _r = c3.apply_link_policy(both, ["overview"], {}, self.OBJS)
        self.assertEqual(outs, ["overview"])     # Lead lets users change tasks

    def test_link_search_request(self):
        env = c3.link_search_request(request_id="r", query="rah", objects=["Lead", "bad!"])
        self.assertEqual((env["type"], env["objects"]), ("records.search", ["Lead"]))
        with self.assertRaises(c3.ContractError):
            c3.link_search_request(request_id="r", query="rah", objects=[])
