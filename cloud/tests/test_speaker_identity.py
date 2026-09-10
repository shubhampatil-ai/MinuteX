#!/usr/bin/env python3
"""test_speaker_identity.py — one identity, resolved everywhere.

THE INVARIANT UNDER TEST: DISPLAY NAME IS NOT IDENTITY.

    speaker_id          "0"                 stable, never rewritten
    speaker_names["0"]  "Rahul" -> "Amit"   display metadata, mutable

Everything here exists because those two were being conflated. The failure
that motivated the work is subtle and expensive: transcript_store renders the
user's names INTO the lines the model reads ("Rahul: I'll send it"), while the
schema asks for a speaker id — so on a RENAMED meeting the model answered
"Rahul" in `assignee_speaker_id`. A name in an id field matches no participant
row, resolves to no contact, and can never be repaired by a later rename,
because nothing records that it ever meant speaker "0". Renaming a meeting
therefore made task ownership WORSE, silently.

WHY THE AMBIGUITY TESTS MATTER MOST. Resolving a name to an id is only safe
when the answer is certain. A task assigned to the WRONG person is invisible
and wrong; an unassigned task is a visible, fixable gap. So every ambiguous
case here asserts "" rather than a plausible guess, and that is the assertion
most worth keeping if this file is ever trimmed.

Run:  python -m pytest tests/test_speaker_identity.py
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "functions/userapi"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ai_schema  # noqa: E402
import mom_schema  # noqa: E402
import prompts  # noqa: E402
import speaker_identity as si  # noqa: E402


# ---------------------------------------------------------------------------
# 1-4. The canonical id, and what an unnamed speaker is called.
# ---------------------------------------------------------------------------
class NormalizationTests(unittest.TestCase):

    def test_every_written_form_reduces_to_the_canonical_id(self):
        for raw in ("0", "Speaker 0", "speaker 0", "SPEAKER 0", "speaker_0",
                    "Speaker-0", " Speaker 0 ", " 0 "):
            with self.subTest(raw=raw):
                self.assertEqual(si.normalize_speaker_id(raw), "0")

    def test_word_labels_keep_their_own_identity(self):
        """ElevenLabs can emit "agent"/"customer". Stripping characters out of
        those would invent a match that is not there."""
        for raw in ("agent", "customer", "Speakerphone", "Speaker Two"):
            with self.subTest(raw=raw):
                self.assertEqual(si.normalize_speaker_id(raw), raw)

    def test_a_persons_name_is_never_mistaken_for_an_id(self):
        self.assertEqual(si.normalize_speaker_id("Rahul"), "Rahul")
        self.assertFalse(si.is_canonical_speaker_id("Speaker 0"))
        self.assertTrue(si.is_canonical_speaker_id("0"))
        self.assertTrue(si.is_canonical_speaker_id("agent"))

    def test_normalization_is_idempotent(self):
        for raw in ("Speaker 0", "0", "agent", "", "Speaker 12"):
            once = si.normalize_speaker_id(raw)
            self.assertEqual(si.normalize_speaker_id(once), once, raw)

    def test_unknown_speaker_falls_back_to_the_transcripts_own_label(self):
        """"Speaker 0", not "Participant 1" and not "Unknown" — the chip has to
        agree with the transcript line the user is reading beside it."""
        self.assertEqual(si.resolve_speaker_display("0", {}), "Speaker 0")
        self.assertEqual(si.resolve_speaker_display("0", None), "Speaker 0")
        self.assertEqual(si.resolve_speaker_display("agent", {}), "agent")
        self.assertEqual(si.resolve_speaker_display("", {}), "")

    def test_resolve_speaker_reports_whether_a_human_name_exists(self):
        self.assertEqual(
            si.resolve_speaker("Speaker 0", {"0": "Rahul"}),
            {"speaker_id": "0", "display_name": "Rahul", "resolved": True})
        self.assertEqual(
            si.resolve_speaker("2", {"0": "Rahul"}),
            {"speaker_id": "2", "display_name": "Speaker 2",
             "resolved": False})


# ---------------------------------------------------------------------------
# 2. Legacy name-keyed maps still resolve (backward compatibility).
# ---------------------------------------------------------------------------
class NameMapTests(unittest.TestCase):

    def test_a_map_stored_under_a_display_key_still_resolves(self):
        """A client that sent {"Speaker 0": "Rahul"} wrote a key no reader
        could find. Those rows exist; they must not stay invisible."""
        self.assertEqual(
            si.resolve_speaker_display("0", {"Speaker 0": "Rahul"}), "Rahul")
        self.assertEqual(
            si.normalize_speaker_names({"Speaker 0": "Rahul", "1": "Priya"}),
            {"0": "Rahul", "1": "Priya"})

    def test_the_canonical_key_wins_regardless_of_dict_order(self):
        for mapping in ({"0": "Rahul", "Speaker 0": "Stale"},
                        {"Speaker 0": "Stale", "0": "Rahul"}):
            with self.subTest(mapping=mapping):
                self.assertEqual(si.normalize_speaker_names(mapping),
                                 {"0": "Rahul"})

    def test_blank_names_are_dropped_not_stored(self):
        self.assertEqual(
            si.normalize_speaker_names({"0": "  ", "1": "Priya", "2": None}),
            {"1": "Priya"})


# ---------------------------------------------------------------------------
# 5. A rename is just a different answer from the same map.
# ---------------------------------------------------------------------------
class RenamePropagationTests(unittest.TestCase):

    def test_renaming_changes_every_dynamic_surface_with_no_regeneration(self):
        before, after = {"0": "Rahul"}, {"0": "Amit"}
        self.assertEqual(si.resolve_speaker_display("0", before), "Rahul")
        self.assertEqual(si.resolve_speaker_display("0", after), "Amit")
        # The same map drives MoM and the API's own resolver.
        self.assertEqual(mom_schema.speaker_display_name("Speaker 0", after),
                         "Amit")

    def test_the_id_itself_never_moves(self):
        """Rahul -> Amit is the SAME speaker. If the id shifted, every stored
        reference to that person would silently re-point."""
        for names in ({"0": "Rahul"}, {"0": "Amit"}, {}):
            self.assertEqual(si.resolve_speaker("0", names)["speaker_id"], "0")


# ---------------------------------------------------------------------------
# 6-8. THE CORE FIX: an id field holds an id, or nothing.
# ---------------------------------------------------------------------------
class TaskSpeakerIdentityTests(unittest.TestCase):

    ROSTER = ["Speaker 0", "Speaker 1"]
    NAMES = {"0": "Rahul", "1": "Priya"}

    def _coerce(self, tasks, roster=None, names=None):
        obj = {"title": "t", "overview": {"sections": []}, "tasks": tasks,
               "participants": []}
        out = ai_schema.coerce_unified(
            obj, self.ROSTER if roster is None else roster, None,
            self.NAMES if names is None else names)
        return out["tasks"]

    def test_a_canonical_id_survives_untouched(self):
        got = self._coerce([{"task": "Send it", "assignee_speaker_id": "0"}])
        self.assertEqual(got[0]["assignee_speaker_id"], "0")

    def test_a_display_label_is_reduced_to_the_id(self):
        got = self._coerce(
            [{"task": "Send it", "assignee_speaker_id": "Speaker 1"}])
        self.assertEqual(got[0]["assignee_speaker_id"], "1")

    def test_a_name_the_model_read_off_a_renamed_transcript_resolves(self):
        """THE REGRESSION THIS WHOLE CHANGE EXISTS FOR. The model sees
        "Rahul:" because the transcript was renamed, and answers "Rahul"."""
        got = self._coerce(
            [{"task": "Send proposal", "assignee_speaker_id": "Rahul"}])
        self.assertEqual(got[0]["assignee_speaker_id"], "0")

    def test_an_ambiguous_name_is_never_guessed(self):
        """Two speakers called Rahul: unassigned is correct, and a coin-flip
        is not. This is the assertion most worth keeping."""
        got = self._coerce(
            [{"task": "Send it", "assignee_speaker_id": "Rahul"}],
            roster=["0", "1"], names={"0": "Rahul", "1": "Rahul"})
        self.assertEqual(got[0]["assignee_speaker_id"], "")

    def test_a_name_belonging_to_nobody_on_the_roster_is_dropped(self):
        """An external assignee is a real case — it just is not a SPEAKER.
        The name stays in `assignee`, which is where a name belongs."""
        got = self._coerce([{"task": "Review", "assignee": "Zara",
                             "assignee_speaker_id": "Zara"}])
        self.assertEqual(got[0]["assignee_speaker_id"], "")
        self.assertEqual(got[0]["assignee"], "Zara")

    def test_an_off_roster_number_is_refused(self):
        """"9" is well-formed but this meeting has no speaker 9."""
        got = self._coerce([{"task": "x", "assignee_speaker_id": "9"}])
        self.assertEqual(got[0]["assignee_speaker_id"], "")

    def test_empty_stays_empty_and_is_a_supported_answer(self):
        got = self._coerce([{"task": "x", "assignee_speaker_id": ""},
                            {"task": "y"}])
        self.assertEqual([t["assignee_speaker_id"] for t in got], ["", ""])

    def test_the_spoken_assignee_name_is_never_rewritten(self):
        """`assignee` is legitimately a NAME — the confidence gate and the
        task fingerprint both read it, so touching it would change task
        identity for every already-seeded row."""
        got = self._coerce([{"task": "Send it", "assignee": "Rahul",
                             "assignee_speaker_id": "Speaker 0"}])
        self.assertEqual(got[0]["assignee"], "Rahul")
        self.assertEqual(got[0]["assignee_speaker_id"], "0")

    def test_a_meeting_with_no_roster_still_coerces(self):
        """A non-diarized transcript has no roster to check against; the
        shape must still be enforced rather than the analysis failing."""
        got = self._coerce([{"task": "x", "assignee_speaker_id": "Speaker 0"}],
                           roster=[], names={})
        self.assertEqual(got[0]["assignee_speaker_id"], "0")


# ---------------------------------------------------------------------------
# 13. Structured attribution on decisions, without breaking old rows.
# ---------------------------------------------------------------------------
class DecisionAttributionTests(unittest.TestCase):

    def test_a_decision_can_carry_a_resolved_speaker_id(self):
        got = ai_schema.coerce_highlights(
            {"decisions": [{"decision": "Approved", "speaker_id": "Rahul"}]},
            ["0", "1"], {"0": "Rahul", "1": "Priya"})
        self.assertEqual(got["decisions"][0]["speaker_id"], "0")

    def test_a_collective_decision_carries_no_speaker(self):
        got = ai_schema.coerce_highlights(
            {"decisions": [{"decision": "Approved", "speaker_id": ""}]},
            ["0"], {"0": "Rahul"})
        self.assertEqual(got["decisions"][0]["speaker_id"], "")

    def test_a_decision_stored_before_the_field_existed_still_coerces(self):
        got = ai_schema.coerce_highlights(
            {"decisions": [{"decision": "Approved", "context": "why"}]})
        self.assertEqual(got["decisions"][0]["decision"], "Approved")
        self.assertEqual(got["decisions"][0]["speaker_id"], "")

    def test_coerce_highlights_still_works_with_no_roster_arguments(self):
        """Every existing caller passes one argument. That must keep working."""
        got = ai_schema.coerce_highlights(
            {"decisions": [{"decision": "Approved"}],
             "deadlines": [{"what": "Ship", "when": "Friday"}],
             "open_questions": ["Who signs?"]})
        self.assertEqual(len(got["decisions"]), 1)
        self.assertEqual(len(got["deadlines"]), 1)
        self.assertEqual(got["open_questions"], ["Who signs?"])


# ---------------------------------------------------------------------------
# 10-11. MoM renders the CURRENT name from the id.
# ---------------------------------------------------------------------------
class MomResolutionTests(unittest.TestCase):

    def test_mom_renders_the_current_name_after_a_rename(self):
        item = {"meeting_highlights": {"decisions": [
            {"decision": "Approved the quote", "speaker_id": "0"}]}}
        before = mom_schema._build_decisions(item, {"0": "Rahul"})
        after = mom_schema._build_decisions(item, {"0": "Amit"})
        self.assertIn("Rahul", before["items"][0]["text"])
        self.assertIn("Amit", after["items"][0]["text"])
        # The decision TEXT itself is never rewritten — only the attribution.
        for section in (before, after):
            self.assertIn("Approved the quote", section["items"][0]["text"])

    def test_a_decision_without_a_speaker_renders_exactly_as_before(self):
        item = {"meeting_highlights": {"decisions": [
            {"decision": "Approved the quote", "context": "budget"}]}}
        got = mom_schema._build_decisions(item, {"0": "Rahul"})
        self.assertEqual(got["items"][0]["text"],
                         "Approved the quote (budget)")

    def test_an_unnamed_speaker_adds_no_attribution_noise(self):
        """"decided by Speaker 0" tells a reader nothing a decision needs."""
        item = {"meeting_highlights": {"decisions": [
            {"decision": "Approved", "speaker_id": "0"}]}}
        got = mom_schema._build_decisions(item, {})
        self.assertEqual(got["items"][0]["text"], "Approved")

    def test_mom_attendees_and_owners_resolve_through_the_shared_layer(self):
        self.assertEqual(
            mom_schema.speaker_display_name("Speaker 0", {"0": "Amit"}),
            "Amit")
        self.assertEqual(mom_schema.speaker_display_name("Speaker 0", {}),
                         "Speaker 0")
        # A legacy display-form key resolves too.
        self.assertEqual(
            mom_schema.speaker_display_name("0", {"Speaker 0": "Amit"}),
            "Amit")

    def test_normalize_speaker_label_keeps_its_published_contract(self):
        for raw, want in (("Speaker 0", "0"), ("speaker_0", "0"),
                          ("0", "0"), ("", "")):
            with self.subTest(raw=raw):
                self.assertEqual(mom_schema.normalize_speaker_label(raw), want)


# ---------------------------------------------------------------------------
# 12. The prompt states the mapping outright.
# ---------------------------------------------------------------------------
class PromptRosterTests(unittest.TestCase):

    def test_the_roster_block_maps_ids_to_names(self):
        block = prompts.speaker_roster_block(["Speaker 0", "Speaker 1"],
                                             {"0": "Rahul"})
        self.assertIn('speaker_id "0" = Rahul', block)
        self.assertIn('speaker_id "1" = Speaker 1 (not yet named)', block)
        self.assertIn("not \"Rahul\"", block)

    def test_no_roster_costs_no_tokens(self):
        self.assertEqual(prompts.speaker_roster_block([], {"0": "Rahul"}), "")
        self.assertEqual(prompts.unified_analysis_system(),
                         prompts.unified_analysis_system((), None))

    def test_the_analysis_prompt_carries_the_mapping_when_given_one(self):
        got = prompts.unified_analysis_system(["Speaker 0"], {"0": "Rahul"})
        self.assertIn('speaker_id "0" = Rahul', got)

    def test_the_overview_is_told_to_lead_with_the_subject(self):
        """Prose that names a speaker for a general topic is prose that goes
        stale on rename — the overview has no id to re-resolve from."""
        text = prompts.unified_analysis_system(["Speaker 0"], {})
        self.assertIn("WRITE THE SUBJECT, NOT THE SPEAKER", text)
        self.assertIn("An unidentified participant", text)
        # Attribution is still required where identity is the point.
        for kept in ("PROPOSAL", "COMMITMENT", "DECISION", "OWNERSHIP"):
            self.assertIn(kept, text)


# ---------------------------------------------------------------------------
# 16. PageIndex/chat ids are untouched by any of this.
# ---------------------------------------------------------------------------
class StructuredIdsSurviveTests(unittest.TestCase):

    def test_pageindex_resolves_names_at_read_time_from_stored_ids(self):
        import pageindex
        tree = {"nodes": [{"node_id": "n1", "speakers": ["0", "1"],
                           "start": 0, "end": 60, "summary": "Pricing"}]}
        self.assertIn("Rahul", pageindex.table_of_contents(tree, {"0": "Rahul"}))
        self.assertIn("Amit", pageindex.table_of_contents(tree, {"0": "Amit"}))
        # Unnamed speakers still read as the transcript's own label.
        self.assertIn("Speaker 1", pageindex.table_of_contents(tree, {}))

    def test_chat_sources_keep_the_raw_speaker_id(self):
        import pageindex
        got = pageindex.evidence_from_segments(
            [{"id": "seg_0", "speaker": "0", "start": 1, "end": 2}])
        self.assertEqual(got[0]["speaker_id"], "0")
        self.assertEqual(got[0]["segment_id"], "seg_0")


# ---------------------------------------------------------------------------
# 9, 11, 14-15. The API layer: key normalization and display precedence.
# ---------------------------------------------------------------------------
from test_ai_workspace import api  # noqa: E402


class SpeakerKeyNormalizationTests(unittest.TestCase):
    """_clean_speaker_names is the WRITE boundary. Every reader normalizes
    its lookup to "0", so a key stored in any other spelling was a name the
    product had accepted and could never show again."""

    def test_every_spelling_is_stored_under_the_canonical_key(self):
        for sent in ("0", "Speaker 0", "speaker_0", " Speaker 0 "):
            with self.subTest(sent=sent):
                self.assertEqual(api._clean_speaker_names({sent: "Rahul"}),
                                 {"0": "Rahul"})

    def test_word_labels_are_stored_unchanged(self):
        self.assertEqual(api._clean_speaker_names({"agent": "Support"}),
                         {"agent": "Support"})

    def test_a_blank_name_still_clears_the_mapping(self):
        self.assertEqual(api._clean_speaker_names({"0": "  "}), {})

    def test_what_is_written_is_what_the_reader_finds(self):
        """The whole point: write in any dialect, read in one."""
        stored = api._clean_speaker_names({"Speaker 0": "Rahul"})
        self.assertEqual(api._speaker_display_name("0", stored), "Rahul")


class TaskDisplayPrecedenceTests(unittest.TestCase):

    def _row(self, **over):
        row = {"task_id": "t1", "title": "Send it",
               "assignee_speaker_id": "0"}
        row.update(over)
        return row

    def test_the_live_speaker_name_beats_a_stale_stored_string(self):
        """A LEGACY row can carry `assignee_name` with no contact behind it.
        That string used to shadow the speaker map, so renaming the speaker
        changed nothing — the one case live resolution exists for."""
        pub = api._public_task_v2(
            self._row(assignee_name="Rahul"), {"0": "Amit"})
        self.assertEqual(pub["assignee"]["name"], "Amit")

    def test_a_contact_assigned_task_is_never_re_pointed_by_a_rename(self):
        """A human made this choice. Renaming the speaker who happened to say
        the sentence must not reassign their work."""
        pub = api._public_task_v2(
            self._row(assignee_contact_id="c-1", assignee_name="Rahul Kumar"),
            {"0": "Amit"})
        self.assertEqual(pub["assignee"]["name"], "Rahul Kumar")

    def test_an_unnamed_speaker_still_reads_as_the_transcript_label(self):
        pub = api._public_task_v2(self._row(), {})
        self.assertEqual(pub["assignee"]["name"], "Speaker 0")

    def test_the_ai_extraction_is_kept_when_there_is_no_speaker(self):
        pub = api._public_task_v2(
            self._row(assignee_speaker_id="", assignee_name_legacy="Zara"),
            {"0": "Amit"})
        self.assertEqual(pub["assignee"]["name"], "Zara")

    def test_a_rename_moves_every_task_that_speaker_owns(self):
        row = self._row()
        self.assertEqual(
            api._public_task_v2(row, {"0": "Rahul"})["assignee"]["name"],
            "Rahul")
        self.assertEqual(
            api._public_task_v2(row, {"0": "Amit"})["assignee"]["name"],
            "Amit")
        # and the stored identity never moved
        self.assertEqual(row["assignee_speaker_id"], "0")


if __name__ == "__main__":
    unittest.main()
