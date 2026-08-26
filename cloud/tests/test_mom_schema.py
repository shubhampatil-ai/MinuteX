#!/usr/bin/env python3
"""test_mom_schema.py — the structured MoM: coercion, build, merge, render.

WHAT THIS PINS. The MoM editor lets a user rewrite AI output, and then lets
them press Regenerate. Everything about whether that is safe lives in this
module, so these tests are mostly about what regeneration must REFUSE to do:

  * never replace text the user wrote (`user_edited` / `user_added`);
  * never resurrect a section, field, row or item the user deleted;
  * never duplicate an item just because the AI reworded it;
  * never reorder a document the user has arranged;
  * never invent a detail (an end time with no duration, an attendee who
    only got MENTIONED, a "Not recorded" placeholder row).

The merge-identity tests are the load-bearing ones. Ids are content-derived
almost everywhere in this module — deliberately, so regeneration is stable —
but for list items and table rows the CONTENT is precisely what a
regeneration changes, so those two use positional ids instead. Get that
backwards and the merge sees every reworded line as an unrelated insert: the
stale copy and the fresh copy both survive, silently doubling the document.
test_regeneration_does_not_duplicate_reworded_content is the regression.

Run:  python -m pytest tests/test_mom_schema.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "shared"))

import mom_schema as mom  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures — one realistic recording row, shaped exactly like the pipeline
# writes it (participants filtered to the speaker roster, meeting_highlights
# from the stage-2 extraction, tasks from the seeder).
# ---------------------------------------------------------------------------
def recording(**over):
    item = {
        "title": "Internal Task Portal",
        "summary": "The team reviewed portal progress and agreed a ship date.",
        "started_at": "2026-08-19T15:30:00Z",
        "duration_seconds": 1800,
        "highlights": ["OAuth is nearly done", "Launch slips by a week"],
        "participants": [
            {"speaker": "Speaker 0", "summary": "Led the standup"},
            {"speaker": "Speaker 1", "summary": "Reported on the backend"},
        ],
        "meeting_highlights": {
            "decisions": [{"decision": "Ship on the 27th", "context": "after QA"}],
            "action_items": [
                {"task": "Complete OAuth integration", "owner": "Speaker 0",
                 "deadline": "20 Aug"},
                {"task": "Write the migration script", "owner": "Speaker 1",
                 "deadline": ""},
            ],
            "deadlines": [{"what": "QA signoff", "when": "26 August"}],
            "open_questions": ["Who owns the rollback plan?"],
            "important_numbers": [],
            "risks": [],
        },
    }
    item.update(over)
    return item


NAMES = {"0": "Rohan", "1": "Priya"}


def role_map(sections):
    return {s["role"]: s for s in sections}


def cell(section, row_index, column_label):
    col = next(c for c in section["columns"] if c["label"] == column_label)
    return section["rows"][row_index]["cells"][col["id"]]


def set_cell(section, row_index, column_label, value):
    col = next(c for c in section["columns"] if c["label"] == column_label)
    row = dict(section["rows"][row_index])
    row["cells"] = dict(row["cells"], **{col["id"]: value})
    row["source"] = mom.SOURCE_USER_EDITED
    section["rows"] = [row if i == row_index else r
                       for i, r in enumerate(section["rows"])]


# ===========================================================================
# Coercion — never raises, whatever is in DynamoDB.
# ===========================================================================
class TestCoercion(unittest.TestCase):
    def test_garbage_coerces_to_empty(self):
        for junk in (None, "", 0, [], "a string", {"sections": "not a list"},
                     {"sections": [1, 2, 3]}):
            got = mom.coerce_mom(junk)
            self.assertEqual(got["sections"], [], f"for {junk!r}")
            self.assertEqual(got["deleted_ids"], [])

    def test_section_without_title_is_dropped(self):
        got = mom.coerce_mom({"sections": [{"kind": "text", "title": "",
                                            "text": "orphan"}]})
        self.assertEqual(got["sections"], [])

    def test_unknown_kind_falls_back_to_text(self):
        got = mom.coerce_mom({"sections": [{"kind": "wat", "title": "T"}]})
        self.assertEqual(got["sections"][0]["kind"], mom.KIND_TEXT)

    def test_unknown_source_defaults_to_ai_not_user(self):
        # An item with no provenance predates source tracking. Treating it as
        # AI means regeneration may refresh it; the opposite default would
        # freeze old content permanently.
        got = mom.coerce_mom({"sections": [
            {"kind": "text", "title": "T", "source": "nonsense"}]})
        self.assertEqual(got["sections"][0]["source"], mom.SOURCE_AI)

    def test_duplicate_ids_are_made_unique(self):
        # Two identical table rows are legitimate; sharing an id would make
        # the editor edit both at once.
        got = mom.coerce_mom({"sections": [
            {"kind": "list", "title": "T", "items": [
                {"id": "i_same", "text": "one"},
                {"id": "i_same", "text": "two"}]}]})
        ids = [i["id"] for i in got["sections"][0]["items"]]
        self.assertEqual(len(set(ids)), 2)

    def test_row_cells_are_keyed_by_column_id_not_position(self):
        # The regression this prevents: deleting a middle column shifts every
        # value one place left when cells are position-keyed.
        got = mom.coerce_mom({"sections": [{
            "kind": "table", "title": "T",
            "columns": [{"id": "c_a", "label": "A"}, {"id": "c_b", "label": "B"}],
            "rows": [{"id": "r_1", "cells": {"c_a": "1", "c_b": "2"}}],
        }]})
        section = got["sections"][0]
        section["columns"] = [section["columns"][1]]
        re_coerced = mom.coerce_mom({"sections": [section]})
        self.assertEqual(re_coerced["sections"][0]["rows"][0]["cells"],
                         {"c_b": "2"})

    def test_limits_are_enforced(self):
        many = [{"kind": "text", "title": f"S{i}"} for i in range(mom.MAX_SECTIONS + 20)]
        got = mom.coerce_mom({"sections": many})
        self.assertEqual(len(got["sections"]), mom.MAX_SECTIONS)

    def test_long_value_is_clipped_not_dropped(self):
        got = mom.coerce_mom({"sections": [{
            "kind": "fields", "title": "T",
            "fields": [{"label": "L", "value": "x" * (mom.MAX_VALUE_CHARS + 500)}]}]})
        self.assertEqual(len(got["sections"][0]["fields"][0]["value"]),
                         mom.MAX_VALUE_CHARS)


# ===========================================================================
# Build — from data the pipeline already produced. No AI call.
# ===========================================================================
class TestBuild(unittest.TestCase):
    def test_full_meeting_builds_every_section(self):
        got = role_map(mom.build_sections(recording(), speaker_names=NAMES))
        self.assertEqual(set(got), {
            mom.ROLE_DETAILS, mom.ROLE_ATTENDEES, mom.ROLE_SUMMARY,
            mom.ROLE_HIGHLIGHTS, mom.ROLE_POINTS, mom.ROLE_DECISIONS,
            mom.ROLE_ACTIONS, mom.ROLE_FOLLOWUPS})

    def test_empty_recording_builds_nothing(self):
        self.assertEqual(mom.build_sections({}), [])

    def test_missing_participants_omits_attendees_rather_than_faking_it(self):
        got = role_map(mom.build_sections(recording(participants=[]),
                                          speaker_names=NAMES))
        self.assertNotIn(mom.ROLE_ATTENDEES, got)

    def test_empty_summary_omits_the_section(self):
        got = role_map(mom.build_sections(recording(summary=""),
                                          speaker_names=NAMES))
        self.assertNotIn(mom.ROLE_SUMMARY, got)

    def test_missing_insights_still_builds_what_exists(self):
        got = role_map(mom.build_sections(
            recording(meeting_highlights=None), speaker_names=NAMES))
        self.assertIn(mom.ROLE_SUMMARY, got)
        self.assertIn(mom.ROLE_ATTENDEES, got)
        for absent in (mom.ROLE_POINTS, mom.ROLE_DECISIONS, mom.ROLE_FOLLOWUPS):
            self.assertNotIn(absent, got)

    def test_no_placeholder_rows_are_invented(self):
        # A sparse meeting yields a SHORT MoM, not one padded with
        # "Not recorded" — the same judgement the minutes_of_meeting prompt
        # already makes.
        rendered = mom.render_markdown(mom.coerce_mom({"sections": mom.build_sections(
            recording(summary="", highlights=[], participants=[],
                      meeting_highlights=None))}))
        self.assertNotIn("Not recorded", rendered)
        self.assertNotIn("N/A", rendered)

    def test_speaker_labels_resolve_to_current_names(self):
        got = role_map(mom.build_sections(recording(), speaker_names=NAMES))
        self.assertEqual(cell(got[mom.ROLE_ATTENDEES], 0, "Name"), "Rohan")
        self.assertEqual(cell(got[mom.ROLE_ATTENDEES], 1, "Name"), "Priya")
        self.assertEqual(cell(got[mom.ROLE_POINTS], 0, "Owner"), "Rohan")

    def test_unnamed_speaker_reads_as_speaker_n(self):
        got = role_map(mom.build_sections(recording(), speaker_names={}))
        self.assertEqual(cell(got[mom.ROLE_ATTENDEES], 0, "Name"), "Speaker 0")

    def test_extracted_plain_name_is_not_mangled_by_speaker_lookup(self):
        item = recording()
        item["meeting_highlights"]["action_items"][0]["owner"] = "Meera"
        got = role_map(mom.build_sections(item, speaker_names=NAMES))
        self.assertEqual(cell(got[mom.ROLE_POINTS], 0, "Owner"), "Meera")

    def test_action_items_prefer_real_tasks_over_extraction(self):
        tasks = [{"task": "Ship the release", "assignee": {"name": "Dev"},
                  "due": "2026-08-27"}]
        got = role_map(mom.build_sections(recording(), tasks=tasks,
                                          speaker_names=NAMES))
        actions = got[mom.ROLE_ACTIONS]
        self.assertEqual(len(actions["rows"]), 1)
        self.assertEqual(cell(actions, 0, "Action Item"), "Ship the release")
        self.assertEqual(cell(actions, 0, "Owner"), "Dev")

    def test_action_items_fall_back_to_extraction_for_legacy_rows(self):
        got = role_map(mom.build_sections(recording(), tasks=[],
                                          speaker_names=NAMES))
        self.assertEqual(len(got[mom.ROLE_ACTIONS]["rows"]), 2)

    def test_time_range_needs_a_duration(self):
        # An end time invented from a start time is exactly the plausible-
        # looking wrong detail a MoM must never carry.
        with_duration = role_map(mom.build_sections(recording()))[mom.ROLE_DETAILS]
        times = {f["label"]: f["value"] for f in with_duration["fields"]}
        self.assertEqual(times["Time"], "3:30 PM - 4:00 PM")

        without = role_map(mom.build_sections(
            recording(duration_seconds=0)))[mom.ROLE_DETAILS]
        times = {f["label"]: f["value"] for f in without["fields"]}
        self.assertEqual(times["Time"], "3:30 PM")

    def test_unparseable_date_does_not_crash_or_invent(self):
        got = role_map(mom.build_sections(recording(started_at="whenever")))
        fields = {f["label"]: f["value"] for f in got[mom.ROLE_DETAILS]["fields"]}
        self.assertEqual(fields["Date"], "whenever")
        self.assertNotIn("Time", fields)

    def test_everything_generated_is_marked_ai(self):
        for section in mom.build_sections(recording(), speaker_names=NAMES):
            self.assertEqual(section["source"], mom.SOURCE_AI)
            for item in (section.get("fields") or []) + (section.get("items") or []) \
                    + (section.get("rows") or []):
                self.assertEqual(item["source"], mom.SOURCE_AI)


# ===========================================================================
# Merge — the rule that makes Regenerate safe.
# ===========================================================================
class TestMerge(unittest.TestCase):
    def setUp(self):
        self.v1 = mom.coerce_mom({"sections": mom.build_sections(
            recording(), speaker_names=NAMES)})
        # A second generation in which the AI reworded everything.
        changed = recording(
            summary="A COMPLETELY REWRITTEN SUMMARY",
            highlights=["OAuth REWORDED", "Launch REWORDED"])
        changed["meeting_highlights"]["action_items"] = [
            {"task": "Complete OAuth integration REWORDED", "owner": "Speaker 0",
             "deadline": "21 Aug"},
            {"task": "Write the migration script REWORDED", "owner": "Speaker 1",
             "deadline": ""},
        ]
        self.fresh = mom.build_sections(changed, speaker_names=NAMES)

    def test_untouched_ai_content_is_refreshed(self):
        got = role_map(mom.merge_generated(self.v1, self.fresh)["sections"])
        self.assertEqual(got[mom.ROLE_SUMMARY]["text"],
                         "A COMPLETELY REWRITTEN SUMMARY")
        self.assertEqual(got[mom.ROLE_HIGHLIGHTS]["items"][0]["text"],
                         "OAuth REWORDED")

    def test_user_edited_text_survives_regeneration(self):
        sections = [dict(s) for s in self.v1["sections"]]
        by = role_map(sections)
        by[mom.ROLE_SUMMARY].update(text="MY OWN SUMMARY",
                                    source=mom.SOURCE_USER_EDITED)
        got = role_map(mom.merge_generated(
            dict(self.v1, sections=sections), self.fresh)["sections"])
        self.assertEqual(got[mom.ROLE_SUMMARY]["text"], "MY OWN SUMMARY")

    def test_user_edited_list_item_survives_but_its_siblings_refresh(self):
        sections = [dict(s) for s in self.v1["sections"]]
        highlights = role_map(sections)[mom.ROLE_HIGHLIGHTS]
        highlights["items"] = [
            dict(highlights["items"][0], text="MY EDIT",
                 source=mom.SOURCE_USER_EDITED),
            highlights["items"][1],
        ]
        got = role_map(mom.merge_generated(
            dict(self.v1, sections=sections), self.fresh)["sections"])
        items = got[mom.ROLE_HIGHLIGHTS]["items"]
        self.assertEqual(items[0]["text"], "MY EDIT")
        self.assertEqual(items[1]["text"], "Launch REWORDED")

    def test_user_edited_table_row_survives(self):
        sections = [dict(s) for s in self.v1["sections"]]
        actions = role_map(sections)[mom.ROLE_ACTIONS]
        set_cell(actions, 0, "Owner", "MY OWNER")
        got = role_map(mom.merge_generated(
            dict(self.v1, sections=sections), self.fresh)["sections"])
        self.assertEqual(cell(got[mom.ROLE_ACTIONS], 0, "Owner"), "MY OWNER")

    def test_regeneration_does_not_duplicate_reworded_content(self):
        """THE regression. Content-derived ids for list items and table rows
        made every reworded line look like a new item, so the stale copy and
        the fresh copy both survived and the document doubled in length."""
        merged = mom.merge_generated(self.v1, self.fresh)
        got = role_map(merged["sections"])
        self.assertEqual(len(got[mom.ROLE_HIGHLIGHTS]["items"]), 2)
        self.assertEqual(len(got[mom.ROLE_ACTIONS]["rows"]), 2)
        # And a third generation must not grow it either.
        again = mom.merge_generated(merged, self.fresh)
        got = role_map(again["sections"])
        self.assertEqual(len(got[mom.ROLE_HIGHLIGHTS]["items"]), 2)
        self.assertEqual(len(got[mom.ROLE_ACTIONS]["rows"]), 2)

    def test_deleted_section_is_not_resurrected(self):
        target = role_map(self.v1["sections"])[mom.ROLE_DECISIONS]
        stored = dict(self.v1,
                      sections=[s for s in self.v1["sections"] if s["id"] != target["id"]],
                      deleted_ids=[target["id"]])
        got = role_map(mom.merge_generated(stored, self.fresh)["sections"])
        self.assertNotIn(mom.ROLE_DECISIONS, got)

    def test_deleted_row_is_not_resurrected(self):
        sections = [dict(s) for s in self.v1["sections"]]
        actions = role_map(sections)[mom.ROLE_ACTIONS]
        dropped = actions["rows"][0]["id"]
        actions["rows"] = actions["rows"][1:]
        stored = dict(self.v1, sections=sections, deleted_ids=[dropped])
        got = role_map(mom.merge_generated(stored, self.fresh)["sections"])
        self.assertEqual(len(got[mom.ROLE_ACTIONS]["rows"]), 1)
        self.assertNotIn(dropped, [r["id"] for r in got[mom.ROLE_ACTIONS]["rows"]])

    def test_user_added_section_survives_and_keeps_its_place(self):
        custom = {"id": "s_custom", "kind": mom.KIND_TEXT, "title": "Client Notes",
                  "visible": True, "source": mom.SOURCE_USER_ADDED, "role": "",
                  "text": "ABC Technologies"}
        sections = [custom] + [dict(s) for s in self.v1["sections"]]
        got = mom.merge_generated(dict(self.v1, sections=sections), self.fresh)
        self.assertEqual(got["sections"][0]["title"], "Client Notes")
        self.assertEqual(got["sections"][0]["text"], "ABC Technologies")

    def test_user_order_is_preserved(self):
        reordered = list(self.v1["sections"])
        actions = next(s for s in reordered if s["role"] == mom.ROLE_ACTIONS)
        reordered.remove(actions)
        reordered.insert(0, actions)
        got = mom.merge_generated(dict(self.v1, sections=reordered), self.fresh)
        self.assertEqual(got["sections"][0]["role"], mom.ROLE_ACTIONS)

    def test_newly_generated_section_is_appended_not_inserted(self):
        # Inserting at its catalogue position would reshuffle a document the
        # user has already arranged.
        without = [s for s in self.v1["sections"] if s["role"] != mom.ROLE_DETAILS]
        got = mom.merge_generated(dict(self.v1, sections=without), self.fresh)
        self.assertEqual(got["sections"][-1]["role"], mom.ROLE_DETAILS)

    def test_section_absent_from_a_regeneration_is_kept_not_blanked(self):
        sparse = mom.build_sections(recording(meeting_highlights=None),
                                    speaker_names=NAMES)
        got = role_map(mom.merge_generated(self.v1, sparse)["sections"])
        self.assertIn(mom.ROLE_DECISIONS, got)
        self.assertTrue(got[mom.ROLE_DECISIONS]["items"])

    def test_hidden_stays_hidden_across_regeneration(self):
        sections = [dict(s) for s in self.v1["sections"]]
        role_map(sections)[mom.ROLE_SUMMARY]["visible"] = False
        got = role_map(mom.merge_generated(
            dict(self.v1, sections=sections), self.fresh)["sections"])
        self.assertFalse(got[mom.ROLE_SUMMARY]["visible"])

    def test_renamed_section_is_matched_by_role_not_title(self):
        sections = [dict(s) for s in self.v1["sections"]]
        by = role_map(sections)
        by[mom.ROLE_ATTENDEES].update(title="Participants",
                                      source=mom.SOURCE_USER_EDITED)
        got = mom.merge_generated(dict(self.v1, sections=sections), self.fresh)
        titles = [s["title"] for s in got["sections"]]
        self.assertIn("Participants", titles)
        self.assertNotIn("Attendees", titles)

    def test_merge_does_not_mutate_its_arguments(self):
        before = mom.render_markdown(self.v1)
        mom.merge_generated(self.v1, self.fresh)
        self.assertEqual(mom.render_markdown(self.v1), before)

    def test_mark_edited_promotes_ai_but_leaves_user_added(self):
        self.assertEqual(mom.mark_edited({"source": mom.SOURCE_AI})["source"],
                         mom.SOURCE_USER_EDITED)
        self.assertEqual(mom.mark_edited({"source": mom.SOURCE_USER_ADDED})["source"],
                         mom.SOURCE_USER_ADDED)


# ===========================================================================
# Render — the Markdown mirror, inside the subset every renderer handles.
# ===========================================================================
class TestRender(unittest.TestCase):
    def setUp(self):
        self.mom = mom.coerce_mom({"sections": mom.build_sections(
            recording(), speaker_names=NAMES)})

    def test_renders_headings_and_tables(self):
        out = mom.render_markdown(self.mom)
        self.assertIn("## Meeting Details", out)
        self.assertIn("| Sr. No | Name | Role / Contribution |", out)
        self.assertIn("|---|---|---|", out)
        self.assertIn("- Who owns the rollback plan?", out)

    def test_hidden_section_is_omitted_entirely(self):
        sections = [dict(s) for s in self.mom["sections"]]
        role_map(sections)[mom.ROLE_SUMMARY]["visible"] = False
        out = mom.render_markdown(dict(self.mom, sections=sections))
        self.assertNotIn("## Summary", out)

    def test_hidden_row_and_field_are_omitted(self):
        sections = [dict(s) for s in self.mom["sections"]]
        attendees = role_map(sections)[mom.ROLE_ATTENDEES]
        attendees["rows"] = [dict(attendees["rows"][0], visible=False),
                             attendees["rows"][1]]
        out = mom.render_markdown(dict(self.mom, sections=sections))
        self.assertNotIn("Rohan | Led the standup", out)
        self.assertIn("Priya", out)

    def test_section_with_no_visible_content_drops_its_heading(self):
        # An empty "## Decisions" reads as a mistake, not as "no decisions".
        sections = [dict(s) for s in self.mom["sections"]]
        role_map(sections)[mom.ROLE_DECISIONS]["items"] = []
        out = mom.render_markdown(dict(self.mom, sections=sections))
        self.assertNotIn("## Decisions", out)

    def test_pipe_in_a_cell_is_escaped_not_dropped(self):
        sections = [dict(s) for s in self.mom["sections"]]
        set_cell(role_map(sections)[mom.ROLE_ACTIONS], 0, "Owner", "A|B")
        out = mom.render_markdown(dict(self.mom, sections=sections))
        self.assertIn("A\\|B", out)

    def test_newline_in_a_cell_does_not_break_the_table(self):
        sections = [dict(s) for s in self.mom["sections"]]
        set_cell(role_map(sections)[mom.ROLE_ACTIONS], 0, "Owner", "one\ntwo")
        out = mom.render_markdown(dict(self.mom, sections=sections))
        for line in out.splitlines():
            if line.startswith("|"):
                self.assertTrue(line.endswith("|"), line)

    def test_render_is_stable_across_repeated_calls(self):
        # The mirror is rewritten on every save; drifting whitespace would
        # make every save look like a content change.
        once = mom.render_markdown(self.mom)
        twice = mom.render_markdown(mom.coerce_mom(self.mom))
        self.assertEqual(once, twice)
        self.assertFalse(once.endswith("\n"))

    def test_empty_mom_renders_empty(self):
        self.assertEqual(mom.render_markdown(mom.empty_mom()), "")
        self.assertTrue(mom.is_empty(mom.empty_mom()))
        self.assertFalse(mom.is_empty(self.mom))

    def test_long_content_renders_every_row(self):
        many = recording()
        many["meeting_highlights"]["action_items"] = [
            {"task": f"Task number {i}", "owner": f"Owner {i}", "deadline": ""}
            for i in range(60)
        ]
        many["participants"] = [
            {"speaker": f"Speaker {i}", "summary": "x" * 300} for i in range(40)
        ]
        built = mom.coerce_mom({"sections": mom.build_sections(many)})
        out = mom.render_markdown(built)
        self.assertIn("Task number 59", out)
        self.assertIn("Speaker 39", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
