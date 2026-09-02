#!/usr/bin/env python3
"""test_evidence_segments.py — evidence segment ids, end to end.

THE BUG THIS PINS. Segment ids were derivable (`with_segment_ids`), the schema
had a field for them, the validator was written, the Task row could store them
and the app could have rendered them — and every real extraction still came back
with `evidence_segment_ids: []`. The reason was the one link nobody owned: the
model was handed the PLAIN prose transcript ("Speaker 0: ..."), which contains
no ids at all. It had nothing to copy. Both ends of the feature were built and
the middle was missing.

So the load-bearing test here is `LabelledTranscriptTests` — that the transcript
the model reads actually carries the ids, and that line N really is segment N.

WHY THAT ALIGNMENT IS SAFE TO ASSUME. stt_result builds the prose lines and the
timestamps array from the same word stream with the same speaker-turn grouping
(build_diarized_text and build_timestamps are the same loop), so position is a
reliable join. Verified against 10 real production transcripts before relying on
it. The renderer still refuses to label whenever the counts disagree — a
mislabelled line would point evidence at the wrong moment while looking
perfectly valid, and no downstream validator could catch that because the id
would genuinely exist.

WHAT IS NOT RE-TESTED. The shape/membership rules of `_evidence_ids` already
had coverage; what is added here is the cases the requirement names explicitly
(mixed valid/invalid, duplicates, wrong meeting, speaker-label-as-id) and the
end-to-end persistence into a Task row.

Run:  python -m pytest tests/test_evidence_segments.py
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

from test_ai_workspace import RECORDING, api  # noqa: E402

import ai_schema  # noqa: E402
import fake_dynamodb as fdb  # noqa: E402
import transcript_store as ts  # noqa: E402

KEY = "recordings/u-1/mobile/mobile-abc_1754300000.m4a"
OWNER = "u-1"

TRANSCRIPT = (
    "Speaker 0: Right, where are we on the proposal?\n\n"
    "Speaker 1: I'll send it tomorrow.\n\n"
    "Speaker 0: Rahul, prepare the quotation by Friday.\n\n"
    "Speaker 2: Sure, I'll have it ready."
)
TIMESTAMPS = [
    {"speaker": "0", "text": "Right, where are we on the proposal?",
     "start": 0, "end": 3},
    {"speaker": "1", "text": "I'll send it tomorrow.", "start": 3, "end": 6},
    {"speaker": "0", "text": "Rahul, prepare the quotation by Friday.",
     "start": 6, "end": 9},
    {"speaker": "2", "text": "Sure, I'll have it ready.", "start": 9, "end": 11},
]


# ---------------------------------------------------------------------------
# THE MISSING LINK — the transcript the model actually reads.
# ---------------------------------------------------------------------------
class LabelledTranscriptTests(unittest.TestCase):

    def test_every_line_carries_its_segment_id(self):
        out = ts.as_labelled_lines(TRANSCRIPT, TIMESTAMPS)
        lines = [ln for ln in out.split("\n") if ln.strip()]
        self.assertEqual(len(lines), len(TIMESTAMPS))
        for i, line in enumerate(lines):
            self.assertTrue(line.startswith(f"[seg_{i}] "), line)

    def test_the_id_on_a_line_matches_that_lines_segment(self):
        """The property everything else depends on: position IS the join."""
        out = ts.as_labelled_lines(TRANSCRIPT, TIMESTAMPS)
        lines = [ln for ln in out.split("\n") if ln.strip()]
        for i, line in enumerate(lines):
            self.assertIn(TIMESTAMPS[i]["text"], line)

    def test_the_speaker_label_survives_labelling(self):
        """The prompt's attribution rules are written against "Speaker N:", so
        the label must still be there after the id is prefixed."""
        out = ts.as_labelled_lines(TRANSCRIPT, TIMESTAMPS)
        self.assertIn("[seg_1] Speaker 1: I'll send it tomorrow.", out)

    def test_no_timestamps_degrades_to_the_plain_transcript(self):
        """A legacy row, or a non-diarized STT result. Losing the references is
        acceptable; inventing them is not."""
        self.assertEqual(ts.as_labelled_lines(TRANSCRIPT, []), TRANSCRIPT)
        self.assertEqual(ts.as_labelled_lines(TRANSCRIPT, None), TRANSCRIPT)

    def test_a_count_mismatch_refuses_to_label(self):
        """THE dangerous case. If the two sides ever drift, a label would point
        at the WRONG line while looking perfectly valid — and no validator
        downstream could catch it, because the id genuinely exists."""
        self.assertEqual(
            ts.as_labelled_lines(TRANSCRIPT, TIMESTAMPS[:2]), TRANSCRIPT)
        self.assertEqual(
            ts.as_labelled_lines(TRANSCRIPT, TIMESTAMPS + [{"text": "x"}]),
            TRANSCRIPT)

    def test_an_empty_transcript_is_handled(self):
        self.assertEqual(ts.as_labelled_lines("", TIMESTAMPS), "")
        self.assertEqual(ts.as_labelled_lines(None, TIMESTAMPS), "")

    def test_valid_segment_ids_matches_what_was_rendered(self):
        """The allow-list the validator uses and the ids the model was shown
        must be the same set, or a legitimately-copied id would be discarded."""
        rendered = {f"seg_{i}" for i in range(len(TIMESTAMPS))}
        self.assertEqual(ts.valid_segment_ids(TIMESTAMPS), rendered)

    def test_valid_segment_ids_is_empty_without_timestamps(self):
        self.assertEqual(ts.valid_segment_ids([]), set())
        self.assertEqual(ts.valid_segment_ids(None), set())

    def test_labelling_is_stable_across_runs(self):
        """Ids are derived from position, never generated — so two renderings
        of the same transcript produce identical references. A random id would
        break every stored reference on the next reprocess."""
        a = ts.as_labelled_lines(TRANSCRIPT, TIMESTAMPS)
        b = ts.as_labelled_lines(TRANSCRIPT, TIMESTAMPS)
        self.assertEqual(a, b)
        self.assertEqual(ts.segment_id(2), "seg_2")


# ---------------------------------------------------------------------------
# VALIDATION — the model is never trusted.
# ---------------------------------------------------------------------------
class EvidenceValidationTests(unittest.TestCase):
    VALID = {"seg_0", "seg_1", "seg_2", "seg_3"}

    def coerce(self, ids):
        got = ai_schema.coerce_analysis(
            {"tasks": [{"task": "T", "evidence_segment_ids": ids}]},
            valid_ids=self.VALID)
        return got["tasks"][0]["evidence_segment_ids"]

    def test_valid_ids_are_kept(self):
        self.assertEqual(self.coerce(["seg_1", "seg_2"]), ["seg_1", "seg_2"])

    def test_invalid_ids_are_dropped_and_valid_ones_kept(self):
        """The requirement's exact case: ["seg_2","seg_fake"] -> ["seg_2"]."""
        self.assertEqual(self.coerce(["seg_2", "seg_fake"]), ["seg_2"])

    def test_an_out_of_range_id_is_dropped(self):
        """A model will happily return seg_999 for a 4-segment meeting."""
        self.assertEqual(self.coerce(["seg_999"]), [])

    def test_all_invalid_yields_an_empty_list_not_a_failure(self):
        """A bad reference must never cost the task itself."""
        self.assertEqual(self.coerce(["nope", "seg_42"]), [])

    def test_duplicates_are_collapsed(self):
        self.assertEqual(self.coerce(["seg_1", "seg_1", "seg_2"]),
                         ["seg_1", "seg_2"])

    def test_a_speaker_label_is_not_a_segment_id(self):
        """Different vocabularies. Accepting one as the other would point the
        reader at an arbitrary line."""
        self.assertEqual(self.coerce(["Speaker 1", "0", "1"]), [])

    def test_junk_shapes_are_survived(self):
        self.assertEqual(self.coerce([None, 42, {"a": 1}, [], "seg_0"]),
                         ["seg_0"])
        self.assertEqual(self.coerce("seg_1"), [])
        self.assertEqual(self.coerce(None), [])

    def test_ids_from_another_meeting_do_not_survive(self):
        """Ids are validated against THIS meeting's segment set, so a
        reference to a different (shorter) meeting's segment is dropped."""
        other_meeting_valid = {"seg_0", "seg_1"}
        got = ai_schema.coerce_analysis(
            {"tasks": [{"task": "T",
                        "evidence_segment_ids": ["seg_3", "seg_0"]}]},
            valid_ids=other_meeting_valid)
        self.assertEqual(got["tasks"][0]["evidence_segment_ids"], ["seg_0"])

    def test_without_an_allow_list_shape_is_still_enforced(self):
        """A legacy row whose segments cannot be read: membership cannot be
        checked, but a malformed id is still refused."""
        got = ai_schema.coerce_analysis(
            {"tasks": [{"task": "T",
                        "evidence_segment_ids": ["seg_7", "Speaker 1"]}]})
        self.assertEqual(got["tasks"][0]["evidence_segment_ids"], ["seg_7"])


# ---------------------------------------------------------------------------
# PERSISTENCE — validated ids reach the Task row and the API.
# ---------------------------------------------------------------------------
class EvidencePersistenceTests(unittest.TestCase):

    def setUp(self):
        self.item = json.loads(json.dumps(RECORDING))
        self.item["user_id"] = OWNER
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

    def seed(self, task):
        self.item["ai_tasks"] = [task]
        self.recordings.items[(KEY,)] = self.item
        api._seed_ai_tasks(OWNER, KEY, self.item)
        return next(iter(self.tasks.items.values()))

    def test_ids_are_persisted_on_the_task_row(self):
        row = self.seed({"task": "Send it", "confidence": "high",
                         "evidence": "I'll send it tomorrow.",
                         "evidence_segment_ids": ["seg_1"]})
        self.assertEqual(row["ai_evidence_segment_ids"], ["seg_1"])
        self.assertEqual(row["ai_evidence"], "I'll send it tomorrow.")

    def test_multiple_ids_are_persisted_in_order(self):
        row = self.seed({"task": "Send it", "confidence": "high",
                         "evidence_segment_ids": ["seg_1", "seg_2"]})
        self.assertEqual(row["ai_evidence_segment_ids"], ["seg_1", "seg_2"])

    def test_no_ids_leaves_the_attribute_ABSENT(self):
        """Absent, not an empty list. The UI keys its "View in transcript"
        affordance off this, and an empty list is indistinguishable from a
        missing one only if you remember to check both."""
        row = self.seed({"task": "Send it", "confidence": "high",
                         "evidence": "I'll send it."})
        self.assertNotIn("ai_evidence_segment_ids", row)

    def test_the_quote_survives_without_any_ids(self):
        """The evidence text is the primary artefact; the references are an
        enhancement on top of it."""
        row = self.seed({"task": "Send it", "confidence": "high",
                         "evidence": "I'll send it.",
                         "evidence_segment_ids": []})
        self.assertEqual(row["ai_evidence"], "I'll send it.")
        self.assertNotIn("ai_evidence_segment_ids", row)

    def test_ids_are_bounded(self):
        row = self.seed({"task": "Send it", "confidence": "high",
                         "evidence_segment_ids": [f"seg_{i}" for i in range(60)]})
        self.assertLessEqual(len(row["ai_evidence_segment_ids"]),
                             api.MAX_EVIDENCE_SEGMENTS)

    def test_the_api_exposes_the_ids(self):
        row = self.seed({"task": "Send it", "confidence": "high",
                         "evidence_segment_ids": ["seg_1"]})
        pub = api._public_task_v2(row, {})
        self.assertEqual(pub["ai_evidence_segment_ids"], ["seg_1"])

    def test_a_legacy_task_reports_an_empty_list_not_a_crash(self):
        """Rows written before this feature have no attribute at all. The API
        must still answer with a shape the app can render."""
        row = api._new_task_row(OWNER, "Old task", recording_key=KEY)
        api._write_task(row)
        pub = api._public_task_v2(row, {})
        self.assertEqual(pub["ai_evidence_segment_ids"], [])

    def test_a_manual_task_has_no_evidence_at_all(self):
        row = api._new_task_row(OWNER, "Typed by hand", recording_key=KEY)
        pub = api._public_task_v2(row, {})
        self.assertEqual(pub["ai_evidence"], "")
        self.assertEqual(pub["ai_evidence_segment_ids"], [])
        self.assertFalse(pub["from_action_item"])


# ---------------------------------------------------------------------------
# THE PIPELINE SEAM — the model is handed the labelled form.
# ---------------------------------------------------------------------------
class PipelineWiringTests(unittest.TestCase):
    """The regression that produced the bug was structural: the renderer
    existed and was simply never called. These read the source, because a
    behavioural test would need a live model."""

    def _source(self):
        return (ROOT / "functions/transcribe" / "lambda_function.py").read_text(
            encoding="utf-8")

    def test_the_pipeline_labels_the_transcript_before_analysis(self):
        src = self._source()
        self.assertIn("as_labelled_lines", src)
        self.assertIn("analyze_meeting(labelled", src)

    def test_the_allow_list_comes_from_the_same_module(self):
        """The ids the model is shown and the ids the validator accepts must be
        one definition, or a correctly-copied id could still be discarded."""
        self.assertIn("valid_segment_ids", self._source())

    def test_the_roster_is_read_from_the_unlabelled_transcript(self):
        """speaker_roster's regex is anchored at line start, so the "[seg_N] "
        prefix would make it match nothing and silently empty the participants
        list. Caught before shipping; pinned so it cannot come back."""
        self.assertIn("roster_source=transcript", self._source())

    def test_the_prompt_tells_the_model_where_the_ids_are(self):
        """The field was in the contract for a while WITHOUT this, and the
        model returned [] every time — it had been asked for ids but never told
        they were on the line."""
        import prompts
        live = prompts.unified_analysis_system(["Speaker 0"])
        for phrase in ("square brackets", "[seg_12]",
                       "copy the id from the START", "NEVER invent"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, live)


if __name__ == "__main__":
    unittest.main(verbosity=2)
