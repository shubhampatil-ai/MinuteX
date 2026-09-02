#!/usr/bin/env python3
"""test_speaker_confidence.py — speaker normalization and the confidence gate.

TWO CHANGES, ONE FILE, because they are two halves of a single guarantee:
MinuteX must attach work to the right person, or to nobody.

PART 1 — SPEAKER NORMALIZATION. The transcript handed to the model reads
"Speaker 0: ..." and the prompt tells it to copy that label verbatim. Every
join in the system, however, keys on the compact "0" that `_speaker_label`
produces — participant rows, the speaker_names display map, the later
speaker->contact resolution. Those two never matched, so SELF-ASSIGNMENT — the
most common and most confidently-extracted kind of task there is — silently
produced an unassigned task, no TASK_ASSIGNED, and a display reading the literal
"Speaker 0" instead of the mapped person's name.

The old tests could not catch it because they used "Speaker 0" on BOTH sides.
So the tests here deliberately mismatch the formats the way production does.

PART 2 — THE CONFIDENCE GATE. The model already reported "high"/"medium"/"low";
nothing acted on it. A LOW reading means the model itself is telling us the
owner is ambiguous, and resolving a speaker on an ambiguous utterance is exactly
how a confidently-wrong assignee gets created. The gate withholds the
assignment and routes it to the review state that already existed.

WHAT IS NOT RE-TESTED HERE. Fingerprint dedupe, tombstones, deadline
normalisation and the notification engine are covered in
test_task_extraction.py, test_workspace_org.py, test_notifications.py and
test_eager_task_seeding.py, and are UNCHANGED by this work. What IS asserted is
that normalization did not disturb them — the fingerprint test below is the one
that matters, because a fingerprint that shifted would silently duplicate every
pre-existing AI task.

Run:  python -m pytest tests/test_speaker_confidence.py
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

from test_ai_workspace import RECORDING, api, parse  # noqa: E402

import ai_schema  # noqa: E402
import fake_dynamodb as fdb  # noqa: E402
import notification_schema as ns  # noqa: E402
import stt_result  # noqa: E402

KEY = "recordings/u-1/mobile/mobile-abc_1754300000.m4a"
OWNER = "u-1"
ASSIGNEE_USER = "u-ravi"
CONTACT = "c-ravi"


# ---------------------------------------------------------------------------
# PART 1a — the normalizer itself.
# ---------------------------------------------------------------------------
class NormalizerTests(unittest.TestCase):

    def test_every_speaker_prefix_form_reduces_to_the_compact_id(self):
        for raw in ("Speaker 0", "speaker 0", "SPEAKER 0", "  speaker 0  ",
                    "speaker_0", "Speaker-0", "0", " 0 "):
            with self.subTest(raw=raw):
                self.assertEqual(stt_result.normalize_speaker_id(raw), "0")

    def test_multi_digit_labels_survive(self):
        self.assertEqual(stt_result.normalize_speaker_id("Speaker 12"), "12")
        self.assertEqual(stt_result.normalize_speaker_id("12"), "12")

    def test_word_labels_are_left_alone(self):
        """ElevenLabs can emit "agent"/"customer" (see stt_result._speaker_label).
        Stripping characters out of those would invent a match."""
        for raw in ("agent", "customer", "Speakerphone", "Speaker Two"):
            with self.subTest(raw=raw):
                self.assertEqual(stt_result.normalize_speaker_id(raw), raw)

    def test_empty_and_none_are_empty(self):
        for raw in ("", "   ", None):
            with self.subTest(raw=raw):
                self.assertEqual(stt_result.normalize_speaker_id(raw), "")

    def test_normalization_is_idempotent(self):
        """Applied at several call sites, so it must never matter whether a
        value has already been through it."""
        for raw in ("Speaker 0", "0", "agent", "", "Speaker 12"):
            once = stt_result.normalize_speaker_id(raw)
            self.assertEqual(stt_result.normalize_speaker_id(once), once, raw)


# ---------------------------------------------------------------------------
# PART 1b — the join, with production's real format mismatch.
# ---------------------------------------------------------------------------
class SeedingBase(unittest.TestCase):

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
        self.contacts.items[(CONTACT,)] = {
            "contact_id": CONTACT, "owner_user_id": OWNER,
            "name": "Ravi Kumar", "email": "ravi@example.com",
            "minutex_user_id": ASSIGNEE_USER}

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

    def map_speaker(self, speaker_id="0", contact_id=CONTACT):
        """A participant row exactly as the app writes it — COMPACT id."""
        self.participants.items[(KEY, speaker_id)] = {
            "audio_s3_key": KEY, "speaker_id": speaker_id,
            "contact_id": contact_id}

    def seed(self, ai_tasks):
        self.item["ai_tasks"] = ai_tasks
        self.recordings.items[(KEY,)] = self.item
        return api._seed_ai_tasks(OWNER, KEY, self.item)

    def only_task(self):
        return next(iter(self.tasks.items.values()))

    def notif_types(self, user_id=None):
        rows = self.notifications.items.values()
        if user_id is not None:
            rows = [r for r in rows if r.get("user_id") == user_id]
        return sorted(r["type"] for r in rows)


class SelfAssignmentTests(SeedingBase):

    def test_the_production_mismatch_now_resolves(self):
        """THE P0. Participant row holds "0"; the AI emits "Speaker 0".

        Before normalization this produced resolution_status NONE, no assignee
        and no notification — for the single most common kind of task.
        """
        self.map_speaker("0")
        self.seed([{"task": "Finish the API", "assignee": "",
                    "assignee_speaker_id": "Speaker 0",
                    "confidence": "high", "evidence": "I'll finish the API."}])

        row = self.only_task()
        self.assertEqual(row["resolution_status"], api.RESOLUTION_RESOLVED)
        self.assertEqual(row["assignee_user_id"], ASSIGNEE_USER)
        self.assertEqual(row["assignee_name"], "Ravi Kumar")
        self.assertEqual(self.notif_types(ASSIGNEE_USER),
                         [ns.TYPE_TASK_ASSIGNED])

    def test_every_ai_label_format_resolves_against_a_compact_participant(self):
        for label in ("Speaker 0", "speaker 0", "SPEAKER 0", " speaker 0 ",
                      "speaker_0", "0"):
            with self.subTest(label=label):
                self.setUp()
                self.map_speaker("0")
                self.seed([{"task": "Finish the API", "assignee": "",
                            "assignee_speaker_id": label,
                            "confidence": "high", "evidence": "I'll do it."}])
                row = self.only_task()
                self.assertEqual(row["assignee_user_id"], ASSIGNEE_USER, label)
                self.assertEqual(row["assignee_speaker_id"], "0", label)

    def test_the_speaker_id_is_stored_normalized(self):
        """So the LATER join (_resolve_tasks_for_speaker, when the user maps a
        speaker after the fact) matches too."""
        self.seed([{"task": "Finish the API", "assignee_speaker_id": "Speaker 0",
                    "confidence": "high"}])
        self.assertEqual(self.only_task()["assignee_speaker_id"], "0")

    def test_mapping_a_speaker_afterwards_reaches_the_task(self):
        """The user records the task first and names the speaker later."""
        self.seed([{"task": "Finish the API", "assignee_speaker_id": "Speaker 0",
                    "confidence": "high"}])
        self.assertEqual(self.only_task()["resolution_status"],
                         api.RESOLUTION_NONE)

        contact = self.contacts.items[(CONTACT,)]
        resolved = api._resolve_tasks_for_speaker(OWNER, KEY, "0", contact)

        self.assertEqual(resolved, 1)
        row = self.only_task()
        self.assertEqual(row["resolution_status"], api.RESOLUTION_RESOLVED)
        self.assertEqual(row["assignee_user_id"], ASSIGNEE_USER)

    def test_an_unknown_speaker_resolves_to_nobody(self):
        """Speaker 3 was never mapped. Not a failure — just nobody to name."""
        self.map_speaker("0")
        self.seed([{"task": "Finish the API",
                    "assignee_speaker_id": "Speaker 3",
                    "confidence": "high"}])
        row = self.only_task()
        self.assertEqual(row["resolution_status"], api.RESOLUTION_NONE)
        self.assertNotIn("assignee_user_id", row)
        self.assertEqual(self.notif_types(), [])


class ThirdPartyAndAmbiguousTests(SeedingBase):

    def test_a_spoken_name_is_never_auto_matched_to_a_contact(self):
        """"Rahul, please send it." A contact named Ravi exists, and a NAME
        must still not be promoted to an identity — only a mapped SPEAKER is
        proof of who someone is."""
        self.map_speaker("0")
        self.seed([{"task": "Send the proposal", "assignee": "Ravi Kumar",
                    "assignee_speaker_id": "", "confidence": "high",
                    "evidence": "Ravi, please send the proposal."}])
        row = self.only_task()
        self.assertEqual(row["resolution_status"], api.RESOLUTION_UNRESOLVED)
        self.assertEqual(row["assignee_name_legacy"], "Ravi Kumar")
        self.assertNotIn("assignee_user_id", row)
        self.assertEqual(self.notif_types(OWNER),
                         [ns.TYPE_AI_ACTION_REQUIRED])

    def test_an_unassigned_task_stays_unassigned_and_silent(self):
        """"Someone should send the proposal." Nothing claimed, nobody told."""
        self.seed([{"task": "Send the proposal", "assignee": "",
                    "assignee_speaker_id": "", "confidence": "low",
                    "evidence": "Someone should send the proposal."}])
        row = self.only_task()
        self.assertEqual(row["resolution_status"], api.RESOLUTION_NONE)
        self.assertEqual(self.notif_types(), [])


class CrossTenantTests(SeedingBase):

    def test_a_contact_owned_by_someone_else_never_resolves(self):
        """The participant row points at a contact belonging to another user.
        Normalization must not widen what the ownership check lets through."""
        self.contacts.items[("c-foreign",)] = {
            "contact_id": "c-foreign", "owner_user_id": "u-someone-else",
            "name": "Not Yours", "minutex_user_id": "u-stranger"}
        self.map_speaker("0", "c-foreign")
        self.seed([{"task": "Finish the API",
                    "assignee_speaker_id": "Speaker 0", "confidence": "high"}])

        row = self.only_task()
        self.assertEqual(row["resolution_status"], api.RESOLUTION_NONE)
        self.assertNotIn("assignee_user_id", row)
        self.assertEqual(self.notif_types(), [])


class FingerprintStabilityTests(SeedingBase):

    def test_normalization_does_not_move_the_fingerprint(self):
        """THE regression that would hurt most.

        The fingerprint deliberately ignores the assignee hint, so a task
        already seeded under the raw "Speaker 0" must still dedupe against the
        same task seeded under "0". If it did not, this change would silently
        duplicate every pre-existing AI task on the next reprocess.
        """
        a = api._task_fingerprint(KEY, "Finish the API", "Speaker 0")
        b = api._task_fingerprint(KEY, "Finish the API", "0")
        c = api._task_fingerprint(KEY, "Finish the API", "")
        self.assertEqual(a, b)
        self.assertEqual(b, c)

    def test_reseeding_after_the_change_creates_no_duplicates(self):
        self.map_speaker("0")
        rows = [{"task": "Finish the API", "assignee_speaker_id": "Speaker 0",
                 "confidence": "high"}]
        self.seed(rows)
        self.assertEqual(len(self.tasks.items), 1)
        for _ in range(3):
            self.seed(rows)
        self.assertEqual(len(self.tasks.items), 1)

    def test_a_row_written_before_normalization_still_resolves(self):
        """A task already in the table carrying the raw label must be reachable
        when the user maps that speaker — both sides of the join normalize."""
        row = api._new_task_row(
            OWNER, "Legacy task", recording_key=KEY,
            assignee_speaker_id="Speaker 0")
        api._write_task(row)

        contact = self.contacts.items[(CONTACT,)]
        self.assertEqual(
            api._resolve_tasks_for_speaker(OWNER, KEY, "0", contact), 1)
        fresh = self.tasks.items[(row["task_id"],)]
        self.assertEqual(fresh["assignee_user_id"], ASSIGNEE_USER)


class DisplayNameTests(SeedingBase):

    def test_display_labels_are_unchanged_by_normalization(self):
        """INTERNAL only. An unmapped speaker still reads "Speaker 0"."""
        self.assertEqual(api._speaker_display_name("0", {}), "Speaker 0")
        self.assertEqual(api._speaker_display_name("Speaker 0", {}), "Speaker 0")
        self.assertEqual(api._speaker_display_name("agent", {}), "agent")

    def test_a_legacy_raw_label_now_renders_the_mapped_name(self):
        """speaker_names is keyed "0". A row holding "Speaker 0" used to render
        the literal label forever, even after the user named that speaker."""
        names = {"0": "Ravi Kumar"}
        self.assertEqual(api._speaker_display_name("Speaker 0", names),
                         "Ravi Kumar")
        self.assertEqual(api._speaker_display_name("0", names), "Ravi Kumar")


# ---------------------------------------------------------------------------
# PART 2 — confidence coercion and the assignment gate.
# ---------------------------------------------------------------------------
class ConfidenceCoercionTests(unittest.TestCase):

    def test_the_three_bands_pass_through(self):
        for level in ("high", "medium", "low"):
            self.assertEqual(ai_schema.coerce_confidence(level), level)

    def test_casing_and_whitespace_are_the_same_claim(self):
        """These used to degrade to "", making a scored task look unscored."""
        for raw, want in (("HIGH", "high"), (" High ", "high"),
                          ("Medium", "medium"), ("LOW", "low")):
            with self.subTest(raw=raw):
                self.assertEqual(ai_schema.coerce_confidence(raw), want)

    def test_numbers_are_refused_rather_than_banded(self):
        """Mapping 0.87 onto a band would invent a threshold nobody chose."""
        for raw in (0.87, 1, 0, True, False, "0.87"):
            with self.subTest(raw=raw):
                self.assertEqual(ai_schema.coerce_confidence(raw), "")

    def test_unknown_words_make_no_claim(self):
        for raw in ("certain", "very high", "", None, "unsure"):
            with self.subTest(raw=raw):
                self.assertEqual(ai_schema.coerce_confidence(raw), "")


class ConfidenceGateTests(SeedingBase):

    def seed_with(self, confidence, **over):
        payload = {"task": "Send the proposal", "assignee": "",
                   "assignee_speaker_id": "Speaker 0",
                   "confidence": confidence,
                   "evidence": "I'll send the proposal."}
        payload.update(over)
        self.map_speaker("0")
        self.seed([payload])
        return self.only_task()

    def test_high_confidence_assigns(self):
        row = self.seed_with("high")
        self.assertEqual(row["resolution_status"], api.RESOLUTION_RESOLVED)
        self.assertEqual(row["assignee_user_id"], ASSIGNEE_USER)
        self.assertEqual(self.notif_types(ASSIGNEE_USER),
                         [ns.TYPE_TASK_ASSIGNED])

    def test_medium_confidence_assigns(self):
        """Deliberately NOT gated: "the work is clear, the owner needs context"
        describes ordinary meeting speech, and demoting it would bury real
        assignments under review noise."""
        row = self.seed_with("medium")
        self.assertEqual(row["resolution_status"], api.RESOLUTION_RESOLVED)
        self.assertEqual(row["assignee_user_id"], ASSIGNEE_USER)

    def test_an_unscored_task_assigns(self):
        """Absence of a score is not evidence of doubt — gating these would put
        the whole back catalogue into review."""
        row = self.seed_with("")
        self.assertEqual(row["resolution_status"], api.RESOLUTION_RESOLVED)

    def test_low_confidence_never_auto_assigns(self):
        """THE gate. The model itself says the owner is ambiguous, so the
        speaker match — however clean — is not acted on."""
        row = self.seed_with("low")
        self.assertEqual(row["resolution_status"], api.RESOLUTION_UNRESOLVED)
        self.assertNotIn("assignee_user_id", row)
        self.assertNotIn("assignee_contact_id", row)
        # The owner is asked; the near-miss person is NOT told.
        self.assertEqual(self.notif_types(OWNER), [ns.TYPE_AI_ACTION_REQUIRED])
        self.assertEqual(self.notif_types(ASSIGNEE_USER), [])

    def test_a_gated_task_keeps_the_candidate_name_for_the_reviewer(self):
        """Throwing the name away would make the review harder than the
        extraction — the reviewer needs to know who it nearly was."""
        row = self.seed_with("low")
        self.assertEqual(row["assignee_name_legacy"], "Ravi Kumar")

    def test_a_gated_task_is_still_created(self):
        """The work was discussed. Dropping it would lose real information."""
        self.seed_with("low")
        self.assertEqual(len(self.tasks.items), 1)

    def test_low_confidence_with_nothing_claimed_is_not_a_review_item(self):
        """"Someone should do it" — nothing to withhold, so nothing to review."""
        self.seed([{"task": "Send the proposal", "assignee": "",
                    "assignee_speaker_id": "", "confidence": "low"}])
        row = self.only_task()
        self.assertEqual(row["resolution_status"], api.RESOLUTION_NONE)
        self.assertEqual(self.notif_types(), [])

    def test_the_stored_confidence_is_the_models_claim_not_the_gate(self):
        """The two are separate facts: a row must still say the model scored it
        low, even though the gate is what changed the outcome."""
        row = self.seed_with("LOW")
        self.assertEqual(row["ai_confidence"], "low")


class EvidenceTests(SeedingBase):

    def test_evidence_and_its_segment_ids_are_persisted(self):
        self.map_speaker("0")
        self.seed([{"task": "Send the proposal",
                    "assignee_speaker_id": "Speaker 0", "confidence": "high",
                    "evidence": "I'll send the proposal tomorrow.",
                    "evidence_segment_ids": ["seg_3", "seg_4"]}])
        row = self.only_task()
        self.assertEqual(row["ai_evidence"], "I'll send the proposal tomorrow.")
        self.assertEqual(row["ai_evidence_segment_ids"], ["seg_3", "seg_4"])

    def test_segment_ids_are_absent_rather_than_empty_when_unavailable(self):
        """An absent attribute is honest; an empty list reads as "we looked and
        there were none", and would make the UI offer a dead affordance."""
        self.seed([{"task": "Send the proposal", "confidence": "high",
                    "evidence": "I'll send it."}])
        self.assertNotIn("ai_evidence_segment_ids", self.only_task())

    def test_segment_ids_are_bounded(self):
        self.seed([{"task": "Send the proposal", "confidence": "high",
                    "evidence_segment_ids": [f"seg_{i}" for i in range(50)]}])
        self.assertLessEqual(len(self.only_task()["ai_evidence_segment_ids"]),
                             api.MAX_EVIDENCE_SEGMENTS)

    def test_a_missing_evidence_quote_does_not_block_the_task(self):
        """A missing quote is a formatting failure, not a statement about who
        owes the work — the gate is what guards assignment."""
        self.map_speaker("0")
        self.seed([{"task": "Send the proposal",
                    "assignee_speaker_id": "Speaker 0", "confidence": "high"}])
        row = self.only_task()
        self.assertEqual(row["ai_evidence"], "")
        self.assertEqual(row["resolution_status"], api.RESOLUTION_RESOLVED)


class PublicShapeTests(SeedingBase):

    def test_the_api_exposes_confidence_evidence_and_the_review_flag(self):
        self.map_speaker("0")
        self.seed([{"task": "Send the proposal",
                    "assignee_speaker_id": "Speaker 0", "confidence": "high",
                    "evidence": "I'll send it.",
                    "evidence_segment_ids": ["seg_3"]}])
        pub = api._public_task_v2(self.only_task(), {})

        self.assertEqual(pub["ai_confidence"], "high")
        self.assertEqual(pub["ai_evidence"], "I'll send it.")
        self.assertEqual(pub["ai_evidence_segment_ids"], ["seg_3"])
        self.assertFalse(pub["needs_review"])
        self.assertTrue(pub["from_action_item"])

    def test_a_gated_task_reports_needs_review(self):
        self.map_speaker("0")
        self.seed([{"task": "Send the proposal",
                    "assignee_speaker_id": "Speaker 0", "confidence": "low"}])
        self.assertTrue(api._public_task_v2(self.only_task(), {})["needs_review"])

    def test_a_manual_task_carries_no_ai_metadata(self):
        """The UI keys AI affordances off these, so a manual task must not
        arrive looking like an extraction."""
        row = api._new_task_row(OWNER, "Typed by hand", recording_key=KEY)
        api._write_task(row)
        pub = api._public_task_v2(row, {})
        self.assertEqual(pub["ai_confidence"], "")
        self.assertEqual(pub["ai_evidence"], "")
        self.assertEqual(pub["ai_evidence_segment_ids"], [])
        self.assertFalse(pub["needs_review"])
        self.assertFalse(pub["from_action_item"])

    def test_an_unassigned_task_is_not_a_review_item(self):
        """NONE is a normal outcome, not an open question — treating it as one
        would fill the review queue with work nobody ever claimed."""
        row = api._new_task_row(OWNER, "Nobody's task", recording_key=KEY)
        self.assertEqual(row["resolution_status"], api.RESOLUTION_NONE)
        self.assertFalse(api._public_task_v2(row, {})["needs_review"])


class LivePromptContractTests(unittest.TestCase):
    """The prompt the PIPELINE actually uses must request every field the
    schema reads.

    THIS IS THE TEST THAT WAS MISSING, and its absence is why the bug shipped.
    `unified_analysis_system` is what analyze_meeting sends (NOT SUMMARY_SYSTEM,
    which is a different, older prompt that a previous audit checked instead).
    Its task contract said EXACTLY {task, assignee, due_date, priority} — four
    fields — while ai_schema.TASK_SPEC, the Tasks table and the assignment gate
    all expected seven. The model complied with the prompt, so every extracted
    task arrived with no speaker id, no confidence and no evidence, and the
    resolution chain had nothing to work with. Confirmed against the real model
    on a real transcript, before and after the fix.

    A string check cannot prove the model behaves — only a real run can, and
    one was done. What it CAN prove is that the two halves of the contract
    still name the same fields, which is the specific way they drifted.
    """

    def _prompts(self):
        import prompts
        return (prompts.unified_analysis_system(["Speaker 0", "Speaker 1"]),
                prompts.unified_reduce_system())

    def test_the_live_prompt_requests_every_field_the_schema_reads(self):
        live, _ = self._prompts()
        for field in ai_schema.TASK_SPEC:
            with self.subTest(field=field):
                self.assertIn(field, live,
                              f"{field} is in TASK_SPEC but the live prompt "
                              f"never asks the model for it, so it is always "
                              f"dropped")

    def test_the_reduce_prompt_preserves_them_too(self):
        """map_reduce runs on any transcript over the single-pass budget — a
        field the reduce step forgets is lost on exactly the long meetings that
        produce the most tasks."""
        _, reduce_prompt = self._prompts()
        for field in ("assignee_speaker_id", "confidence", "evidence"):
            with self.subTest(field=field):
                self.assertIn(field, reduce_prompt)

    def test_the_live_prompt_states_the_attribution_rules(self):
        """The self / third-party distinction is the whole basis of safe
        assignment. Losing this wording silently re-creates the failure where
        a speaker label lands in the `assignee` NAME field."""
        live, _ = self._prompts()
        for phrase in ("Self-commitment", "assignee_speaker_id",
                       "NEVER put a speaker label"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, live)

    def test_the_live_prompt_states_the_confidence_enum(self):
        live, _ = self._prompts()
        self.assertIn('"high" | "medium" | "low"', live)


if __name__ == "__main__":
    unittest.main(verbosity=2)
