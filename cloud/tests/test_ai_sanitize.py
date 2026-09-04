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


class CitationMarkerTests(unittest.TestCase):
    """THE SECOND LEAK — the markers seen in AI Chat, which carry no seg_N.

    Every string in `test_the_exact_markers_from_the_screenshot` is taken from
    a real AI Chat reply. None of them contains the substring "seg_", which is
    exactly why they shipped: strip_segment_ids() returns early unless it sees
    one, so the whole sanitizer was a no-op on this format.
    """

    def test_the_exact_markers_from_the_screenshot(self):
        """The reported strings, verbatim. 【】 / 【-6】 / 【-32】 / 【-94】."""
        cases = [
            (u"Harshal Sir said the Google integration uses an external ID "
             u"(the Google record ID) for each account \u3010\u3011 .",
             u"external ID"),
            (u"Every account is assigned a unique ID; this applies to both "
             u"manager accounts and sub-accounts \u3010\u3011 .",
             u"sub-accounts"),
            (u"In the Google case the unique ID is specifically the Google "
             u"customer ID \u3010\u3011 \u3010\u3011 .",
             u"customer ID"),
            (u"How a project's launch inventory is set (e.g., 100 flats "
             u"total, 20 flats for a Ganpati festival launch) \u3010-6\u3011",
             u"Ganpati festival launch"),
            (u"Budget sizing and cost-per-lead calculations (15k, 20k, 30k "
             u"examples) \u3010-32\u3011",
             u"cost-per-lead"),
            (u"Architecture for integrating Google and Facebook accounts, "
             u"using a manager-sub-account hierarchy \u3010-94\u3011",
             u"manager-sub-account hierarchy"),
        ]
        for text, must_keep in cases:
            with self.subTest(text=text):
                out = ai_sanitize.sanitize_ai_user_output(text)
                self.assertNotIn(u"\u3010", out)
                self.assertNotIn(u"\u3011", out)
                self.assertNotIn("[]", out)
                self.assertNotIn("[-", out)
                # The SENTENCE has to survive the marker's removal.
                self.assertIn(must_keep, out)

    def test_ascii_residue_forms(self):
        """`[]` and `[-N]` reaching us as plain ASCII, not wrapped in 【】."""
        self.assertEqual(
            ai_sanitize.sanitize_ai_user_output("Budget was approved []."),
            "Budget was approved.")
        self.assertEqual(
            ai_sanitize.sanitize_ai_user_output("Budget was approved [-6]."),
            "Budget was approved.")
        self.assertEqual(
            ai_sanitize.sanitize_ai_user_output(
                "Inventory [-6], budget [-32], architecture [-94]."),
            "Inventory, budget, architecture.")
        self.assertEqual(
            ai_sanitize.sanitize_ai_user_output("Spaced out [ -94 ] here."),
            "Spaced out here.")

    def test_bracketed_segment_id_marker(self):
        """`[seg_123]` — the form the old rules already covered, re-pinned
        here so a refactor of either pass cannot drop it."""
        out = ai_sanitize.sanitize_ai_user_output(
            "The team agreed a 12% discount [seg_123].")
        self.assertNotIn("seg_", out)
        self.assertNotIn("[]", out)
        self.assertEqual(out, "The team agreed a 12% discount.")

    def test_corner_bracket_wrapping_a_segment_id(self):
        """Both formats at once: 【seg_12】."""
        out = ai_sanitize.sanitize_ai_user_output(
            u"Every account gets a unique ID \u3010seg_12\u3011.")
        self.assertNotIn("seg_", out)
        self.assertNotIn(u"\u3010", out)
        self.assertEqual(out, "Every account gets a unique ID.")

    def test_prose_with_multiple_internal_references(self):
        """A whole reply, in the shape the screenshot showed: several bullets,
        each ending in a marker, plus Markdown that must survive."""
        reply = (
            u"The meeting was a **Marketing Campaign Integration** "
            u"discussion covering:\n"
            u"- How a project's launch inventory is set \u3010-6\u3011\n"
            u"- Configuration of the enquiry-source picklist "
            u"(Google, Facebook, Aggregators, etc.) \u3010\u3011\n"
            u"- Budget sizing and cost-per-lead calculations \u3010-32\u3011\n"
            u"- Architecture for integrating Google and Facebook \u3010-94\u3011"
        )
        out = ai_sanitize.sanitize_ai_user_output(reply)
        for leaked in (u"\u3010", u"\u3011", "[]", "[-6]", "[-32]", "[-94]"):
            self.assertNotIn(leaked, out)
        # Content and Markdown intact.
        self.assertIn("**Marketing Campaign Integration**", out)
        self.assertIn("- How a project's launch inventory is set", out)
        self.assertIn("(Google, Facebook, Aggregators, etc.)", out)
        self.assertIn("- Budget sizing and cost-per-lead calculations", out)
        # Four bullets in, four bullets out.
        self.assertEqual(out.count("\n- "), 4)
        self.assertEqual(len([l for l in out.split("\n")
                              if l.startswith("- ")]), 4)

    def test_unclosed_marker_from_a_truncated_reply(self):
        out = ai_sanitize.sanitize_ai_user_output(u"The budget was cut \u3010")
        self.assertNotIn(u"\u3010", out)
        self.assertEqual(out, "The budget was cut")

    def test_other_citation_payloads(self):
        """The payload is not parsed — the BRACKET is the signal. So the
        `4:2` and `turn0search1` styles go too."""
        for text in (u"Agreed \u30104:2\u3011.",
                     u"Agreed \u3010turn0search1\u3011.",
                     u"Agreed \u3010source: transcript\u3011."):
            with self.subTest(text=text):
                out = ai_sanitize.sanitize_ai_user_output(text)
                self.assertEqual(out, "Agreed.")

    def test_has_citation_markers_detects_and_does_not_false_positive(self):
        self.assertTrue(ai_sanitize.has_citation_markers(u"a \u3010-6\u3011"))
        self.assertTrue(ai_sanitize.has_citation_markers("a []"))
        self.assertTrue(ai_sanitize.has_citation_markers("a [-32]"))
        self.assertFalse(ai_sanitize.has_citation_markers("a [2026]"))
        self.assertFalse(ai_sanitize.has_citation_markers("plain prose"))
        self.assertFalse(ai_sanitize.has_citation_markers(""))


class CitationMarkerSafetyTests(unittest.TestCase):
    """Requirement 10 — the half that matters more.

    A rule that ate every `[...]` would silently corrupt correct answers, and
    unlike the marker bug nobody would be able to see that it happened. So the
    ASCII pass removes ONLY the two shapes that cannot be content: an empty
    pair, and a negative-only number.
    """

    def test_legitimate_bracketed_content_survives(self):
        for text in ("Sources included [Google, Facebook] as channels.",
                     "The target year is [2026].",
                     "See the [Launch Configuration] section.",
                     "Footnote [6] and reference [32] stay.",
                     "A [note] with [several] bracketed [words].",
                     "The range is [10-20] units.",
                     "Markdown link [text](https://example.com) is intact.",
                     "Array access items[0] and config[key].",
                     "[Bracketed] at the very start of a line."):
            with self.subTest(text=text):
                self.assertEqual(
                    ai_sanitize.sanitize_ai_user_output(text), text)

    def test_positive_bracketed_numbers_are_content_not_citations(self):
        """`[6]` is deliberately NOT stripped — a user's own footnote or index
        looks exactly like that, and the observed defect was NEGATIVE."""
        self.assertEqual(
            ai_sanitize.sanitize_ai_user_output("Refer to clause [6] today."),
            "Refer to clause [6] today.")

    def test_ordinary_reply_still_passes_through_unchanged(self):
        text = ("The client approved the proposal.\n\n"
                "- Budget: $8,000\n- Deadline: Friday\n\n"
                "**Priya** owns the follow-up.")
        self.assertEqual(ai_sanitize.sanitize_ai_user_output(text), text)

    def test_markdown_formatting_is_not_regressed(self):
        """Requirement 11 — the `\\*` fix must still work, and must still work
        ALONGSIDE the new pass."""
        out = ai_sanitize.sanitize_ai_user_output(
            u"The \\*budget\\* was approved \u3010-6\u3011 and "
            u"\\*\\*Priya\\*\\* owns it [].")
        self.assertEqual(out, "The *budget* was approved and **Priya** owns it.")

    def test_non_markdown_escapes_still_survive_the_new_pass(self):
        text = r"Path C:\Users\test and regex \d+ stay."
        self.assertEqual(ai_sanitize.sanitize_ai_user_output(text), text)


class CitationMarkerOverviewTests(unittest.TestCase):
    """The overview boundary gets the new pass too — and STILL keeps its
    evidence array, which is the whole reason the ids exist."""

    def test_markers_stripped_from_prose_evidence_untouched(self):
        out = ai_sanitize.sanitize_overview({"sections": [{
            "title": u"Pricing \u3010-6\u3011",
            "content": u"The team agreed a 12% discount \u3010-32\u3011.",
            "items": [u"Contract signed []", u"Rollout in Q3 \u3010-94\u3011"],
            "evidence_segment_ids": ["seg_1", "seg_2"],
        }]})
        section = out["sections"][0]
        self.assertEqual(section["title"], "Pricing")
        self.assertEqual(section["content"],
                         "The team agreed a 12% discount.")
        self.assertEqual(section["items"],
                         ["Contract signed", "Rollout in Q3"])
        # THE POINT: the structured evidence is NOT sanitized.
        self.assertEqual(section["evidence_segment_ids"], ["seg_1", "seg_2"])


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

    def test_chat_prompts_forbid_citation_markers(self):
        """The SECOND leak's primary fix. The 【】 markers were never named in
        any prompt, so nothing told the model not to emit them."""
        for name in ("CHAT_SYSTEM", "GROUNDED_CHAT_RULES",
                     "ASSISTANT_SYSTEM"):
            with self.subTest(prompt=name):
                text = getattr(self.prompts, name)
                self.assertIn("citation markers", text)
                # The exact glyphs, so the model sees what it must not write.
                self.assertIn(u"\u3010", text)
                self.assertIn(u"\u3011", text)
                self.assertIn("[-6]", text)

    def test_grounded_rules_separate_evidence_from_prose(self):
        """Requirement 8: the SOURCES line is the ONLY citation channel, and
        the prompt now says so rather than leaving it implied."""
        text = self.prompts.GROUNDED_CHAT_RULES
        self.assertIn("MACHINE-READABLE", text)
        self.assertIn("NOWHERE else", text)
        # The SOURCES contract itself must survive — the structured `sources`
        # array is built from it.
        self.assertIn("SOURCES:", text)

    def test_overview_prompt_confines_ids_to_the_evidence_field(self):
        system = self.prompts.unified_analysis_system()
        self.assertIn("THIS FIELD IS THE ONLY PLACE AN ID MAY APPEAR", system)


if __name__ == "__main__":
    unittest.main()
