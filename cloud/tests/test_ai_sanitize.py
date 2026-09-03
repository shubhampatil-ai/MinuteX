#!/usr/bin/env python3
"""test_ai_sanitize.py — internal metadata and escaped Markdown never ship.

THE TWO BUGS THIS PINS.

1. `\\*` ON SCREEN. Chat replies were showing literal `\\*important\\*` instead of
   emphasis. The app renders a hand-written Markdown SUBSET
   (app/lib/document-renderer.tsx) whose emphasis pass splits on the BARE
   asterisk, so an escaped `\\*text\\*` produced fragments that failed the
   emphasis test and the backslashes reached the user verbatim.

2. `seg_N` IN PROSE. The transcript is handed to the model as
   `[seg_42] Speaker 0: ...` lines, because seg_N is the identity the app
   deep-links on (transcript_store.segment_id). Those ids are RETRIEVAL
   METADATA and belong in the structured `sources` / `evidence_segment_ids`
   arrays. A model reading an id on every line cites them anyway — "According
   to seg_24, the team decided…" — which reads to a user like a debug dump.

WHAT MATTERS MOST HERE. The prompts are the primary fix; this sanitizer is the
backstop for when a model ignores an instruction, which models do. So the
load-bearing tests are the NEGATIVE ones — `SafetyTests` below. A sanitizer
that also eats "we discussed three segments", a Windows path or a regex is a
worse bug than the one it fixes, because it corrupts correct answers silently
and there is no way for a reader to tell.

Run:  python -m pytest tests/test_ai_sanitize.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))

import ai_sanitize  # noqa: E402
import ai_schema  # noqa: E402


class UnescapeMarkdownTests(unittest.TestCase):
    """Escaped Markdown punctuation -> the punctuation itself."""

    def test_escaped_emphasis_becomes_real_emphasis(self):
        # The exact string from the bug report.
        self.assertEqual(
            ai_sanitize.unescape_markdown(r"\*Important decision\*"),
            "*Important decision*")

    def test_escaped_bold_becomes_real_bold(self):
        self.assertEqual(
            ai_sanitize.unescape_markdown(r"\*\*Important decision\*\*"),
            "**Important decision**")

    def test_already_correct_markdown_is_untouched(self):
        for text in ("**Important decision**",
                     "*italic*",
                     "- First point\n- Second point",
                     "1. First item\n2. Second item",
                     "## Heading",
                     "| a | b |\n|---|---|\n| 1 | 2 |"):
            with self.subTest(text=text):
                self.assertEqual(ai_sanitize.unescape_markdown(text), text)

    def test_escaped_list_and_heading_markers(self):
        self.assertEqual(ai_sanitize.unescape_markdown(r"\- First point"),
                         "- First point")
        self.assertEqual(ai_sanitize.unescape_markdown(r"\## Heading"),
                         "## Heading")
        self.assertEqual(ai_sanitize.unescape_markdown(r"1\. First item"),
                         "1. First item")

    def test_plain_prose_is_untouched(self):
        text = "The client approved the proposal on Tuesday."
        self.assertEqual(ai_sanitize.unescape_markdown(text), text)

    def test_empty_and_none(self):
        self.assertEqual(ai_sanitize.unescape_markdown(""), "")
        self.assertEqual(ai_sanitize.unescape_markdown(None), "")


class SegmentIdTests(unittest.TestCase):
    """The internal identifier never survives into prose."""

    def test_leading_citation_clause_is_removed_whole(self):
        # The clause goes WITH its connective. Removing only the id would
        # leave "According to , the client approved" — worse than the bug.
        self.assertEqual(
            ai_sanitize.strip_segment_ids(
                "According to seg_123, the client approved the proposal."),
            "The client approved the proposal.")

    def test_multiple_ids_in_one_clause(self):
        self.assertEqual(
            ai_sanitize.strip_segment_ids(
                "The team agreed (seg_1, seg_25 and seg_1234) to ship."),
            "The team agreed to ship.")

    def test_id_in_each_position(self):
        """Start, middle and end of the sentence."""
        cases = {
            "seg_9 shows the decision.": "Shows the decision.",
            "The **key** point per seg_2 was cost.":
                "The **key** point was cost.",
            "Budget was cut [seg_7].": "Budget was cut.",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(ai_sanitize.strip_segment_ids(raw), want)

    def test_bracketed_transcript_form(self):
        """`[seg_12]` is how the model SEES the line, so it is what gets
        copied out most often."""
        self.assertEqual(
            ai_sanitize.strip_segment_ids("[seg_12] Pricing was agreed."),
            "Pricing was agreed.")

    def test_varied_id_widths(self):
        for ident in ("seg_1", "seg_25", "seg_1234", "seg_001", "seg_0"):
            with self.subTest(ident=ident):
                out = ai_sanitize.strip_segment_ids(
                    f"The deal closed, see {ident}.")
                self.assertNotIn("seg_", out)
                self.assertIn("The deal closed", out)

    def test_ids_across_multiple_lines(self):
        out = ai_sanitize.strip_segment_ids(
            "Line one.\n\nAccording to seg_5, we ship.\n- point per seg_6")
        self.assertNotIn("seg_", out)
        self.assertIn("We ship.", out)
        self.assertIn("- point", out)

    def test_has_segment_ids_detects_and_declines(self):
        self.assertTrue(ai_sanitize.has_segment_ids("see seg_4"))
        self.assertTrue(ai_sanitize.has_segment_ids("[seg_40]"))
        self.assertFalse(ai_sanitize.has_segment_ids("we split it into "
                                                     "segments"))
        self.assertFalse(ai_sanitize.has_segment_ids(""))


class SafetyTests(unittest.TestCase):
    """THE LOAD-BEARING HALF. An over-eager sanitizer corrupts correct
    answers, and unlike the original bug nobody can see that it happened."""

    def test_the_word_segment_is_never_touched(self):
        for text in (
                "We reviewed the segment 4 rollout and all segments.",
                "The customer segment is enterprise.",
                "Segment the audience by region.",
                "That segment of the call was inaudible.",
                "We discussed three segments of the market."):
            with self.subTest(text=text):
                self.assertEqual(ai_sanitize.strip_segment_ids(text), text)

    def test_identifier_lookalikes_are_not_ids(self):
        """Only `seg_` + DIGITS, bounded. Anything else stays."""
        for text in ("The seg_id column is indexed.",
                     "Use seg_alpha for the pilot.",
                     "Rename seg_12abc before the migration."):
            with self.subTest(text=text):
                self.assertEqual(ai_sanitize.strip_segment_ids(text), text)

    def test_non_markdown_escapes_survive(self):
        """A backslash before an ordinary character is NOT Markdown escaping —
        a path, a regex or a code sample must come through intact."""
        for text in (r"Path C:\Users\test and regex \d+ stay.",
                     r"Match \s+ then \w and finish.",
                     r"The literal \n newline escape."):
            with self.subTest(text=text):
                self.assertEqual(ai_sanitize.unescape_markdown(text), text)

    def test_ordinary_reply_passes_through_unchanged(self):
        text = ("The client approved the proposal.\n\n"
                "- Budget: $8,000\n- Deadline: Friday\n\n"
                "**Priya** owns the follow-up.")
        self.assertEqual(ai_sanitize.sanitize_ai_user_output(text), text)


class BoundaryTests(unittest.TestCase):
    """sanitize_ai_user_output — the one function the routes call."""

    def test_both_defects_in_one_reply(self):
        self.assertEqual(
            ai_sanitize.sanitize_ai_user_output(
                r"According to seg_24, the \*budget\* was approved."),
            "The *budget* was approved.")

    def test_formatting_survives_id_removal(self):
        out = ai_sanitize.sanitize_ai_user_output(
            "Decisions (seg_3):\n"
            "- **Budget** approved\n"
            "- *Timeline* per seg_9 is Friday\n"
            "1. First item\n"
            "2. Second item")
        self.assertNotIn("seg_", out)
        self.assertIn("- **Budget** approved", out)
        self.assertIn("*Timeline*", out)
        self.assertIn("1. First item", out)
        self.assertIn("2. Second item", out)

    def test_drop_segment_ids_false_keeps_ids_but_still_unescapes(self):
        out = ai_sanitize.sanitize_ai_user_output(
            r"see seg_3 for \*this\*", drop_segment_ids=False)
        self.assertIn("seg_3", out)
        self.assertIn("*this*", out)

    def test_empty_input(self):
        self.assertEqual(ai_sanitize.sanitize_ai_user_output(""), "")
        self.assertEqual(ai_sanitize.sanitize_ai_user_output(None), "")


class OverviewTests(unittest.TestCase):
    """The Dynamic Overview — prose sanitized, EVIDENCE PRESERVED.

    The evidence array is the whole reason the ids exist: the app turns it into
    a tappable "jump to this moment" link. Stripping it here would fix the
    cosmetic bug by deleting the feature.
    """

    def test_prose_cleaned_and_evidence_kept(self):
        out = ai_sanitize.sanitize_overview({"sections": [{
            "id": "section_0",
            "title": "Pricing (seg_4)",
            "kind": "list",
            "content": r"As noted in seg_12, the \*discount\* was agreed.",
            "items": ["Rate cut per seg_7", "Contract signed"],
            "evidence_segment_ids": ["seg_4", "seg_12"],
        }]})
        section = out["sections"][0]
        self.assertEqual(section["title"], "Pricing")
        self.assertNotIn("seg_", section["content"])
        self.assertIn("*discount*", section["content"])
        self.assertEqual(section["items"], ["Rate cut", "Contract signed"])
        # The point of the whole exercise.
        self.assertEqual(section["evidence_segment_ids"], ["seg_4", "seg_12"])

    def test_input_is_not_mutated(self):
        original = {"sections": [{"title": "T (seg_1)", "content": "",
                                  "items": [], "evidence_segment_ids": []}]}
        ai_sanitize.sanitize_overview(original)
        self.assertEqual(original["sections"][0]["title"], "T (seg_1)")

    def test_malformed_shapes_pass_through(self):
        for bad in (None, "", [], {"sections": "nope"}, {}):
            with self.subTest(bad=bad):
                ai_sanitize.sanitize_overview(bad)  # must not raise


class CoercionIntegrationTests(unittest.TestCase):
    """ai_schema._overview_section is where the fix actually lands.

    Sanitizing at COERCION means a leaked id never enters DynamoDB, so the
    stored row is clean and every reader benefits without remembering to strip.
    """

    def test_coerce_overview_cleans_prose_and_keeps_evidence(self):
        out = ai_schema.coerce_overview(
            {"sections": [{
                "title": "Decisions",
                "content": r"According to seg_2, the \*deal\* closed.",
                "items": ["Signed per seg_3"],
                "evidence_segment_ids": ["seg_2", "seg_3"],
            }]},
            valid_ids={"seg_2", "seg_3"})
        section = out["sections"][0]
        self.assertEqual(section["content"], "The *deal* closed.")
        self.assertEqual(section["items"], ["Signed"])
        self.assertEqual(section["evidence_segment_ids"], ["seg_2", "seg_3"])

    def test_item_that_was_only_an_id_is_dropped_not_left_blank(self):
        """A bullet reduced to "" renders as an empty dot."""
        out = ai_schema.coerce_overview({"sections": [{
            "title": "Notes",
            "content": "Real content here.",
            "items": ["seg_5", "A genuine point"],
        }]})
        self.assertEqual(out["sections"][0]["items"], ["A genuine point"])

    def test_section_whose_only_content_was_an_id_is_dropped(self):
        """Consistent with the existing emptiness rule: a heading with nothing
        under it reads as a claim the transcript may not support."""
        out = ai_schema.coerce_overview({"sections": [{
            "title": "Risks", "content": "seg_9", "items": [],
        }]})
        self.assertEqual(out["sections"], [])

    def test_ordinary_overview_is_unchanged(self):
        out = ai_schema.coerce_overview({"sections": [{
            "title": "Pricing",
            "content": "The team agreed a 12% discount for enterprise.",
            "items": ["Contract signed", "Rollout in Q3"],
            "evidence_segment_ids": ["seg_1"],
        }]}, valid_ids={"seg_1"})
        section = out["sections"][0]
        self.assertEqual(section["title"], "Pricing")
        self.assertEqual(section["content"],
                         "The team agreed a 12% discount for enterprise.")
        self.assertEqual(section["items"], ["Contract signed", "Rollout in Q3"])


class PromptRuleTests(unittest.TestCase):
    """The prompts are the PRIMARY fix — the sanitizer is the backstop.

    Pinned because a prompt edit that silently drops these rules would put the
    whole load on the sanitizer, and the failure would only show up as ugly
    answers in production.
    """

    def setUp(self):
        import prompts
        self.prompts = prompts

    def test_chat_prompts_forbid_exposing_ids(self):
        for name in ("CHAT_SYSTEM", "GROUNDED_CHAT_RULES",
                     "ASSISTANT_SYSTEM", "ASSISTANT_TASK_RULES"):
            with self.subTest(prompt=name):
                text = getattr(self.prompts, name)
                self.assertIn("NEVER", text.upper())
                self.assertIn("seg_", text)

    def test_task_rules_no_longer_ask_the_model_to_cite_ids(self):
        """This rule USED to say "Mention the segment ids you relied on" —
        it was instructing the exact leak it now forbids."""
        self.assertNotIn("Mention the segment ids",
                         self.prompts.ASSISTANT_TASK_RULES)
        self.assertIn("must NOT write one into your reply",
                      self.prompts.ASSISTANT_TASK_RULES)

    def test_overview_prompt_confines_ids_to_the_evidence_field(self):
        system = self.prompts.unified_analysis_system()
        self.assertIn("THIS FIELD IS THE ONLY PLACE AN ID MAY APPEAR", system)


if __name__ == "__main__":
    unittest.main()
