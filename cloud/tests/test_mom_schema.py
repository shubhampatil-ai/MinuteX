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

ACTION ITEMS ARE THE EXCEPTION, and TestTaskBackedActionIdentity below is
where that lives. Positional identity is right for a table whose rows have no
life of their own, and wrong for one PROJECTING the Tasks table: deleting the
first Task shifts every later Task up a slot, so a user's edit stays on the
slot and lands on the wrong Task, while the vacated last slot keeps stale text
forever. Those rows are therefore keyed by Task id (mom_schema.TASK_REF) and a
row whose Task is gone is DROPPED — the only case where a regeneration removes
content, and deliberately narrow enough that a `user_added` row never is.
mom_version "1" MoMs predate all of this and are re-keyed by
migrate_action_identity; TestLegacyPositionalMom pins that.

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
            mom.ROLE_HIGHLIGHTS, mom.ROLE_DECISIONS,
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
        for absent in (mom.ROLE_DECISIONS, mom.ROLE_FOLLOWUPS):
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
        self.assertEqual(cell(got[mom.ROLE_ACTIONS], 0, "Owner"), "Rohan")

    def test_unnamed_speaker_reads_as_speaker_n(self):
        got = role_map(mom.build_sections(recording(), speaker_names={}))
        self.assertEqual(cell(got[mom.ROLE_ATTENDEES], 0, "Name"), "Speaker 0")

    def test_extracted_plain_name_is_not_mangled_by_speaker_lookup(self):
        """A plain NAME in the legacy extraction must survive the speaker
        lookup rather than being resolved as though it were a label."""
        item = recording()
        item["meeting_highlights"]["action_items"][0]["owner"] = "Meera"
        got = role_map(mom.build_sections(item, speaker_names=NAMES))
        self.assertEqual(cell(got[mom.ROLE_ACTIONS], 0, "Owner"), "Meera")

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

    def test_action_items_use_ai_tasks_before_the_legacy_extraction(self):
        """meeting_highlights.action_items is no longer generated, so a row
        analyzed but not yet task-seeded must render from `ai_tasks`. Both are
        present here; ai_tasks must win."""
        item = recording()
        item["ai_tasks"] = [{"task": "Send the revised quote",
                             "assignee": "Rakesh", "due_date": "Friday"}]
        got = role_map(mom.build_sections(item, tasks=[],
                                          speaker_names=NAMES))
        actions = got[mom.ROLE_ACTIONS]
        self.assertEqual(len(actions["rows"]), 1)
        self.assertEqual(cell(actions, 0, "Action Item"),
                         "Send the revised quote")
        self.assertEqual(cell(actions, 0, "Owner"), "Rakesh")
        self.assertEqual(cell(actions, 0, "Deadline"), "Friday")

    def test_ai_tasks_speaker_id_resolves_to_a_name(self):
        item = recording()
        item["ai_tasks"] = [{"task": "Draft the SOW", "assignee": "",
                             "assignee_speaker_id": "Speaker 0"}]
        got = role_map(mom.build_sections(item, tasks=[],
                                          speaker_names=NAMES))
        self.assertEqual(cell(got[mom.ROLE_ACTIONS], 0, "Owner"), "Rohan")

    def test_meeting_points_table_is_gone(self):
        """It duplicated Action Items — it printed the same string into both
        its "Discussion Point" and "Action Item" columns."""
        got = role_map(mom.build_sections(recording(), speaker_names=NAMES))
        self.assertNotIn(mom.ROLE_POINTS, got)

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


# ---------------------------------------------------------------------------
# Task-backed action identity. These use the REAL task payload shape that
# _public_task_v2 returns (id + assignee dict + due + due_date_normalized),
# not the legacy meeting_highlights.action_items shape the fixtures above use,
# because the authoritative path is the one that needed pinning.
# ---------------------------------------------------------------------------
def task(task_id, title, owner="", due="", normalized=""):
    """One task exactly as _public_task_v2 emits it (relevant keys only)."""
    return {
        "id": task_id,
        "task": title,
        "title": title,
        "assignee": {"name": owner, "display_name": owner} if owner else None,
        "assignee_speaker_id": "",
        "due": due,
        "due_date": due,
        "due_date_normalized": normalized,
    }


class TaskActionMixin:
    def actions(self, tasks, item=None):
        return mom._build_actions(item or {}, tasks, {})

    def cells(self, section):
        """[[cell, ...]] in column order, for readable assertions."""
        cols = section["columns"]
        return [[r["cells"][c["id"]] for c in cols] for r in section["rows"]]

    def col(self, section, label):
        return next(c["id"] for c in section["columns"]
                    if c["label"] == label)

    def edit(self, section, index, label, value):
        """Edit one cell the way the client does: change it, mark the ROW."""
        row = section["rows"][index]
        row["cells"][self.col(section, label)] = value
        row["source"] = mom.SOURCE_USER_EDITED

    def merge(self, stored, fresh):
        return mom._merge_rows(stored["rows"], fresh["rows"],
                               stored["columns"], set())

    def refs(self, rows):
        return [r.get(mom.TASK_REF) for r in rows]


class TestTaskBackedActionIdentity(TaskActionMixin, unittest.TestCase):
    """The row id must follow the TASK, not the array position."""

    def setUp(self):
        self.a = task("t-a", "Task A", "Amit", "Fri")
        self.b = task("t-b", "Task B", "Neha", "Mon")
        self.c = task("t-c", "Task C", "Ravi", "Wed")

    def test_row_id_is_derived_from_the_task_id(self):
        one = self.actions([self.a])
        two = self.actions([task("t-a", "Task A reworded", "Amit", "Fri")])
        # Same task, different text -> same id. That is what lets the merge
        # recognise a reworded action instead of duplicating it.
        self.assertEqual(one["rows"][0]["id"], two["rows"][0]["id"])
        self.assertEqual(one["rows"][0][mom.TASK_REF], "t-a")

    def test_row_id_is_not_positional(self):
        """The bug: under positional ids, row 0 of every meeting shared one
        id, so identity carried no information about WHICH task it was."""
        first = self.actions([self.a, self.b])["rows"]
        # Same two tasks, opposite order.
        second = self.actions([self.b, self.a])["rows"]
        self.assertEqual(first[0]["id"], second[1]["id"])
        self.assertEqual(first[1]["id"], second[0]["id"])

    def test_different_tasks_never_share_an_id(self):
        rows = self.actions([self.a, self.b, self.c])["rows"]
        self.assertEqual(len({r["id"] for r in rows}), 3)

    def test_fingerprint_is_the_fallback_when_there_is_no_id(self):
        t = task("", "Task X", "Amit")
        t["fingerprint"] = "fp-x"
        row = self.actions([t])["rows"][0]
        self.assertEqual(row[mom.TASK_REF], "fp-x")

    def test_a_task_with_no_identity_at_all_stays_positional(self):
        """Degrades to the old behaviour rather than colliding every id-less
        row onto one shared hash of ""."""
        t = task("", "Task Y", "Amit")
        row = self.actions([t])["rows"][0]
        self.assertNotIn(mom.TASK_REF, row)
        self.assertEqual(row["id"], mom._ident("r", mom.ROLE_ACTIONS, 0))

    def test_ai_tasks_tier_keeps_positional_identity(self):
        """Only real Tasks have an external identity to point at."""
        item = {"ai_tasks": [{"task": "Seeded soon", "assignee": "Amit"}]}
        row = self.actions([], item)["rows"][0]
        self.assertNotIn(mom.TASK_REF, row)

    def test_legacy_highlights_tier_keeps_positional_identity(self):
        item = {"meeting_highlights": {
            "action_items": [{"task": "Old row", "owner": "Amit",
                              "deadline": "Fri"}]}}
        row = self.actions([], item)["rows"][0]
        self.assertNotIn(mom.TASK_REF, row)


class TestTaskDeletionRemovesTheRow(TaskActionMixin, unittest.TestCase):
    """A deleted Task must not leave an orphan asserting work nobody owns."""

    def setUp(self):
        self.a = task("t-a", "Task A", "Amit", "Fri")
        self.b = task("t-b", "Task B", "Neha", "Mon")
        self.c = task("t-c", "Task C", "Ravi", "Wed")

    def test_deleting_the_middle_task_leaves_the_other_two(self):
        stored = self.actions([self.a, self.b, self.c])
        out = self.merge(stored, self.actions([self.a, self.c]))
        self.assertEqual(self.refs(out), ["t-a", "t-c"])

    def test_deleting_the_first_task_does_not_duplicate_the_second(self):
        """THE REGRESSION. Positionally, deleting A shifted B into slot 0 and
        C into slot 1, leaving slot 2's stale text behind — so B or C
        rendered twice."""
        stored = self.actions([self.a, self.b, self.c])
        out = self.merge(stored, self.actions([self.b, self.c]))
        self.assertEqual(self.refs(out), ["t-b", "t-c"])
        titles = [c[1] for c in [[r["cells"][col["id"]]
                                  for col in stored["columns"]] for r in out]]
        self.assertEqual(titles, ["Task B", "Task C"])
        self.assertEqual(len(titles), len(set(titles)))

    def test_a_user_edited_row_goes_when_its_task_goes(self):
        """The MoM must not keep asserting a commitment whose Task was
        deleted, even one the user retyped — but see the user_added test for
        the content that DOES survive."""
        stored = self.actions([self.a, self.b])
        self.edit(stored, 1, "Owner", "Priya")
        out = self.merge(stored, self.actions([self.a]))
        self.assertEqual(self.refs(out), ["t-a"])

    def test_deleting_every_task_empties_the_table(self):
        """Through the FULL merge, because _build_actions returns None when
        there are no tasks — so the section never reaches _merge_rows at all
        and only _merge_section can prune it. Calling _merge_rows directly
        here would pass while the real path kept every stale row."""
        item = {"title": "X",
                "participants": [{"speaker": "0", "summary": "led"}]}
        stored = mom.coerce_mom({
            "mom_version": mom.MOM_VERSION,
            "sections": mom.build_sections(item, tasks=[self.a, self.b])})
        merged = mom.merge_generated(stored,
                                     mom.build_sections(item, tasks=[]))
        section = next(s for s in merged["sections"]
                       if s["role"] == mom.ROLE_ACTIONS)
        self.assertEqual(section["rows"], [])
        # An empty table renders no heading at all.
        self.assertNotIn("## Action Items", mom.render_markdown(merged))

    def test_deleting_every_task_keeps_a_user_added_row(self):
        item = {"title": "X",
                "participants": [{"speaker": "0", "summary": "led"}]}
        stored = mom.coerce_mom({
            "mom_version": mom.MOM_VERSION,
            "sections": mom.build_sections(item, tasks=[self.a])})
        section = next(s for s in stored["sections"]
                       if s["role"] == mom.ROLE_ACTIONS)
        section["rows"].append({
            "id": "r_mine", "visible": True, "source": mom.SOURCE_USER_ADDED,
            "cells": {c["id"]: ("Mine" if c["label"] == "Action Item" else "")
                      for c in section["columns"]},
        })
        merged = mom.merge_generated(stored,
                                     mom.build_sections(item, tasks=[]))
        out = next(s for s in merged["sections"]
                   if s["role"] == mom.ROLE_ACTIONS)
        self.assertEqual(self.refs(out["rows"]), [None])
        self.assertEqual(out["rows"][0]["source"], mom.SOURCE_USER_ADDED)

    def test_a_user_added_row_survives_a_task_deletion(self):
        """It never had a Task, so no Task's absence can remove it."""
        stored = self.actions([self.a, self.b])
        stored["rows"].append({
            "id": "r_mine", "visible": True, "source": mom.SOURCE_USER_ADDED,
            "cells": {c["id"]: "" for c in stored["columns"]},
        })
        stored["rows"][-1]["cells"][self.col(stored, "Action Item")] = "Mine"
        out = self.merge(stored, self.actions([self.b]))
        self.assertEqual(self.refs(out), ["t-b", None])
        self.assertEqual(out[-1]["source"], mom.SOURCE_USER_ADDED)

    def test_a_non_projection_table_still_keeps_its_missing_rows(self):
        """Attendees follows the speaker roster and has no external key, so
        the old keep-everything rule must still apply there — this is what
        stops the new prune leaking into every other table."""
        item = recording()
        stored = mom._build_attendees(item, {})
        fewer = mom._build_attendees(
            {"participants": item["participants"][:1]}, {})
        out = mom._merge_rows(stored["rows"], fewer["rows"],
                              stored["columns"], set())
        self.assertEqual(len(out), len(stored["rows"]))


class TestTombstonesUnderTaskIdentity(TaskActionMixin, unittest.TestCase):
    """`mom.deleted_ids` and `deleted_task_fingerprints` stay SEPARATE
    concepts, and neither may resurrect a deleted Task.

    They answer different questions — "did the user remove this line from the
    document?" versus "did the user delete this Task?" — and this change does
    not merge them (that is a tracked follow-up). What it does change is what
    a MoM tombstone MEANS: it used to name a SLOT, so deleting row 0 blanked
    position 0 forever and silently suppressed whatever unrelated Task later
    occupied it. Now it names a Task.
    """

    def setUp(self):
        self.a = task("t-a", "Task A", "Amit")
        self.b = task("t-b", "Task B", "Neha")

    def test_a_deleted_mom_row_stays_out_while_its_task_still_exists(self):
        stored = self.actions([self.a, self.b])
        row_a = next(r for r in stored["rows"]
                     if r[mom.TASK_REF] == "t-a")
        out = mom._merge_rows(stored["rows"],
                              self.actions([self.a, self.b])["rows"],
                              stored["columns"], {row_a["id"]})
        self.assertEqual(self.refs(out), ["t-b"])

    def test_a_tombstone_suppresses_that_task_not_that_position(self):
        """THE REGRESSION. A positional tombstone on slot 0 used to hide any
        future row that landed in slot 0, whatever Task it belonged to."""
        stored = self.actions([self.a])
        tombstone = stored["rows"][0]["id"]
        # A completely different Task, now first in the list.
        fresh = self.actions([self.b])
        out = mom._merge_rows([], fresh["rows"], stored["columns"],
                              {tombstone})
        self.assertEqual(self.refs(out), ["t-b"])

    def test_deleting_the_task_and_the_row_is_not_a_conflict(self):
        """Both tombstone systems firing at once must simply agree."""
        stored = self.actions([self.a, self.b])
        row_a = next(r for r in stored["rows"]
                     if r[mom.TASK_REF] == "t-a")
        out = mom._merge_rows(stored["rows"],
                              self.actions([self.b])["rows"],
                              stored["columns"], {row_a["id"]})
        self.assertEqual(self.refs(out), ["t-b"])

    def test_a_legacy_positional_tombstone_still_applies_to_legacy_rows(self):
        """A "1" MoM's stored tombstones must keep working on its own rows —
        they are positional ids and so are the rows they name."""
        cols = mom._columns(mom.ROLE_ACTIONS,
                            ["Sr. No", "Action Item", "Owner", "Deadline"])
        rows = mom._rows(mom.ROLE_ACTIONS, cols,
                         [["1", "Old A", "Amit", ""],
                          ["2", "Old B", "Neha", ""]])
        out = mom._merge_rows(rows, [], cols, {rows[0]["id"]})
        texts = [r["cells"][cols[1]["id"]] for r in out]
        self.assertEqual(texts, ["Old B"])


class TestUserEditSurvivesReorderAndDeletion(TaskActionMixin,
                                             unittest.TestCase):
    """The exact scenarios from the fix request."""

    def setUp(self):
        self.a = task("t-a", "Task A", "Amit", "Fri")
        self.b = task("t-b", "Task B", "Neha", "Mon")
        self.c = task("t-c", "Task C", "Ravi", "Wed")

    def owner_of(self, rows, ref, columns):
        row = next(r for r in rows if r.get(mom.TASK_REF) == ref)
        return row["cells"][next(c["id"] for c in columns
                                 if c["label"] == "Owner")]

    def test_edit_survives_deletion_of_an_earlier_task(self):
        """A / B(edited) / C, delete A -> B keeps Priya, C untouched, no dupes.

        Positionally this was the corruption case: B's edit sat on slot 1,
        which after the shift belonged to C.
        """
        stored = self.actions([self.a, self.b, self.c])
        self.edit(stored, 1, "Owner", "Priya")
        out = self.merge(stored, self.actions([self.b, self.c]))
        self.assertEqual(self.refs(out), ["t-b", "t-c"])
        self.assertEqual(self.owner_of(out, "t-b", stored["columns"]), "Priya")
        self.assertEqual(self.owner_of(out, "t-c", stored["columns"]), "Ravi")

    def test_edit_survives_a_full_reorder(self):
        """A / B(edited) / C regenerating as C, A, B."""
        stored = self.actions([self.a, self.b, self.c])
        self.edit(stored, 1, "Owner", "Priya")
        out = self.merge(stored, self.actions([self.c, self.a, self.b]))
        self.assertEqual(len(out), 3)
        self.assertEqual(sorted(self.refs(out)), ["t-a", "t-b", "t-c"])
        self.assertEqual(self.owner_of(out, "t-b", stored["columns"]), "Priya")
        self.assertEqual(self.owner_of(out, "t-a", stored["columns"]), "Amit")

    def test_an_untouched_row_still_refreshes_from_its_task(self):
        stored = self.actions([self.a])
        out = self.merge(stored,
                         self.actions([task("t-a", "Task A", "Sunita", "Fri")]))
        self.assertEqual(self.owner_of(out, "t-a", stored["columns"]),
                         "Sunita")

    def test_reordering_does_not_move_an_edit_between_tasks(self):
        """The sharpest form: every row edited, then fully reversed."""
        stored = self.actions([self.a, self.b, self.c])
        for i, name in enumerate(("E-A", "E-B", "E-C")):
            self.edit(stored, i, "Owner", name)
        out = self.merge(stored, self.actions([self.c, self.b, self.a]))
        cols = stored["columns"]
        self.assertEqual(self.owner_of(out, "t-a", cols), "E-A")
        self.assertEqual(self.owner_of(out, "t-b", cols), "E-B")
        self.assertEqual(self.owner_of(out, "t-c", cols), "E-C")


class TestNormalizedDeadline(TaskActionMixin, unittest.TestCase):
    """The Deadline cell prefers the RESOLVED date, never invents one."""

    def deadline(self, tasks):
        section = self.actions(tasks)
        col = self.col(section, "Deadline")
        return [r["cells"][col] for r in section["rows"]]

    def test_normalized_date_wins_over_the_spoken_phrase(self):
        self.assertEqual(
            self.deadline([task("t", "T", "A", "tomorrow", "2026-09-09")]),
            ["2026-09-09"])

    def test_spoken_phrase_is_used_when_normalization_failed(self):
        self.assertEqual(
            self.deadline([task("t", "T", "A", "Next Meeting", "")]),
            ["Next Meeting"])

    def test_unresolvable_phrase_is_not_replaced_by_a_guess(self):
        for phrase in ("2 to 3 days", "weekend", "ongoing"):
            with self.subTest(phrase=phrase):
                self.assertEqual(
                    self.deadline([task("t", "T", "A", phrase, "")]), [phrase])

    def test_no_due_date_at_all_renders_blank(self):
        self.assertEqual(self.deadline([task("t", "T", "A", "", "")]), [""])

    def test_a_task_with_only_a_normalized_date_uses_it(self):
        t = task("t", "T", "A", "", "2026-09-11")
        self.assertEqual(self.deadline([t]), ["2026-09-11"])

    def test_the_spoken_phrase_is_still_available_on_the_task(self):
        """The MoM shows the resolved date; nothing DELETES the original."""
        t = task("t", "T", "A", "tomorrow", "2026-09-09")
        self.assertEqual(t["due"], "tomorrow")


class TestLegacyPositionalMom(TaskActionMixin, unittest.TestCase):
    """A stored mom_version "1" document must survive the identity change."""

    def setUp(self):
        self.a = task("t-a", "Task A", "Amit", "Fri")
        self.b = task("t-b", "Task B", "Neha", "Mon")
        self.cols = mom._columns(
            mom.ROLE_ACTIONS, ["Sr. No", "Action Item", "Owner", "Deadline"])

    def legacy(self, values, version="1", extra_sections=()):
        rows = mom._rows(mom.ROLE_ACTIONS, self.cols, values)
        sections = [mom._section(mom.ROLE_ACTIONS, mom.KIND_TABLE,
                                 "Action Items", columns=self.cols, rows=rows)]
        sections.extend(extra_sections)
        return {"mom_version": version, "sections": sections}, rows

    def test_legacy_rows_are_positional_and_carry_no_ref(self):
        _, rows = self.legacy([["1", "Task A", "Amit", "Fri"]])
        self.assertNotIn(mom.TASK_REF, rows[0])

    def test_migration_assigns_task_refs_by_action_text(self):
        stored, _ = self.legacy([["1", "Task A", "Amit", "Fri"],
                                 ["2", "Task B", "Neha", "Mon"]])
        fresh = mom.build_sections({}, tasks=[self.a, self.b])
        merged = mom.merge_generated(stored, fresh)
        section = next(s for s in merged["sections"]
                       if s["role"] == mom.ROLE_ACTIONS)
        self.assertEqual(self.refs(section["rows"]), ["t-a", "t-b"])

    def test_migration_stamps_the_new_version(self):
        stored, _ = self.legacy([["1", "Task A", "Amit", "Fri"]])
        merged = mom.merge_generated(
            stored, mom.build_sections({}, tasks=[self.a]))
        self.assertEqual(merged["mom_version"], mom.MOM_VERSION)

    def test_migration_preserves_a_legacy_user_edit(self):
        stored, rows = self.legacy([["1", "Task A", "Amit", "Fri"],
                                    ["2", "Task B", "Neha", "Mon"]])
        rows[1]["cells"][self.col({"columns": self.cols}, "Owner")] = "Priya"
        rows[1]["source"] = mom.SOURCE_USER_EDITED
        merged = mom.merge_generated(
            stored, mom.build_sections({}, tasks=[self.a, self.b]))
        section = next(s for s in merged["sections"]
                       if s["role"] == mom.ROLE_ACTIONS)
        owner = self.col({"columns": self.cols}, "Owner")
        edited = next(r for r in section["rows"]
                      if r.get(mom.TASK_REF) == "t-b")
        self.assertEqual(edited["cells"][owner], "Priya")

    def test_an_ambiguous_legacy_row_is_left_positional_not_guessed(self):
        """Two Tasks with the same action text carry no information about
        which one a stored row meant, so it keeps its positional id and is
        never pruned as a deleted Task."""
        stored, _ = self.legacy([["1", "Same text", "Amit", "Fri"]])
        dupes = [task("t-1", "Same text", "Amit"),
                 task("t-2", "Same text", "Neha")]
        merged = mom.merge_generated(stored,
                                     mom.build_sections({}, tasks=dupes))
        section = next(s for s in merged["sections"]
                       if s["role"] == mom.ROLE_ACTIONS)
        stale = [r for r in section["rows"] if not r.get(mom.TASK_REF)]
        self.assertEqual(len(stale), 1)

    def test_an_unmatched_legacy_row_is_kept_not_dropped(self):
        stored, _ = self.legacy([["1", "Vanished action", "Amit", "Fri"]])
        merged = mom.merge_generated(
            stored, mom.build_sections({}, tasks=[self.a]))
        section = next(s for s in merged["sections"]
                       if s["role"] == mom.ROLE_ACTIONS)
        texts = [r["cells"][self.col({"columns": self.cols}, "Action Item")]
                 for r in section["rows"]]
        self.assertIn("Vanished action", texts)

    def test_a_current_version_mom_is_not_re_keyed(self):
        stored = {"mom_version": mom.MOM_VERSION,
                  "sections": mom.build_sections({}, tasks=[self.a])}
        before = mom.coerce_mom(stored)
        after = mom.migrate_action_identity(stored, [])
        self.assertEqual(after["sections"], before["sections"])

    def test_retired_meeting_points_section_is_dropped(self):
        """Its builder is gone, so the merge would otherwise keep it as a
        section 'the AI stopped producing' — forever."""
        points = mom._section("meeting_points", mom.KIND_TABLE,
                              "Meeting Points", columns=self.cols,
                              rows=mom._rows("meeting_points", self.cols,
                                             [["1", "x", "y", "z"]]))
        stored, _ = self.legacy([["1", "Task A", "Amit", "Fri"]],
                                extra_sections=[points])
        merged = mom.merge_generated(
            stored, mom.build_sections({}, tasks=[self.a]))
        self.assertNotIn("meeting_points",
                         [s.get("role") for s in merged["sections"]])

    def test_a_user_touched_meeting_points_section_is_kept(self):
        """Never delete content the user has adopted."""
        rows = mom._rows("meeting_points", self.cols, [["1", "x", "y", "z"]])
        rows[0]["source"] = mom.SOURCE_USER_EDITED
        points = mom._section("meeting_points", mom.KIND_TABLE,
                              "Meeting Points", columns=self.cols, rows=rows)
        stored, _ = self.legacy([["1", "Task A", "Amit", "Fri"]],
                                extra_sections=[points])
        merged = mom.merge_generated(
            stored, mom.build_sections({}, tasks=[self.a]))
        self.assertIn("meeting_points",
                      [s.get("role") for s in merged["sections"]])

    def test_legacy_mom_still_renders(self):
        stored, _ = self.legacy([["1", "Task A", "Amit", "Fri"]])
        out = mom.render_markdown(mom.coerce_mom(stored))
        self.assertIn("## Action Items", out)
        self.assertIn("Task A", out)

    def test_task_ref_is_never_rendered_to_the_user(self):
        """It is identity, not content: it must not reach the document."""
        section = self.actions([self.a])
        out = mom.render_markdown(
            mom.coerce_mom({"sections": [section]}))
        self.assertNotIn("t-a", out)
        self.assertNotIn(mom.TASK_REF, out)

    def test_task_ref_survives_a_coerce_round_trip(self):
        """coerce_mom returns a FIXED dict, so an unlisted key is destroyed.
        If this breaks, every regeneration silently reverts to positional."""
        section = self.actions([self.a])
        again = mom.coerce_mom({"sections": [section]})
        row = again["sections"][0]["rows"][0]
        self.assertEqual(row.get(mom.TASK_REF), "t-a")


class TestModernStructuredPath(unittest.TestCase):
    """Cover the CURRENT pipeline shape: a dynamic overview plus real Tasks,
    end to end through build -> merge -> render. The fixtures above carry
    `summary`/`highlights` and so exercise the legacy prose branch."""

    def item(self):
        return {
            "title": "Billing review",
            "participants": [{"speaker": "0", "summary": "Led the review"}],
            "overview": {"sections": [
                {"id": "section_0", "title": "Billing Model",
                 "kind": "list", "content": "",
                 "items": ["Developer effort billed at 15%"],
                 "source": "ai", "evidence_segment_ids": []},
                {"id": "section_1", "title": "Open Risks",
                 "kind": "text",
                 "content": "Rate card unconfirmed for Q3.",
                 "items": [], "source": "ai", "evidence_segment_ids": []},
            ]},
            "meeting_highlights": {"decisions": [
                {"decision": "Bill BA effort at a fixed 10%",
                 "context": "agreed by both leads"}]},
        }

    def test_overview_sections_become_mom_sections(self):
        sections = mom.build_sections(self.item(), tasks=[])
        roles = [s["role"] for s in sections]
        self.assertIn("overview:section_0", roles)
        self.assertIn("overview:section_1", roles)

    def test_overview_path_excludes_the_legacy_prose_sections(self):
        """A row WITH an overview must not also render summary/highlights."""
        roles = [s["role"] for s in mom.build_sections(self.item(), tasks=[])]
        self.assertNotIn(mom.ROLE_SUMMARY, roles)
        self.assertNotIn(mom.ROLE_HIGHLIGHTS, roles)

    def test_tasks_to_render_end_to_end(self):
        tasks = [task("t-a", "Confirm the Q3 rate card", "Amit",
                      "tomorrow", "2026-09-11")]
        built = mom.coerce_mom({
            "sections": mom.build_sections(self.item(), tasks=tasks)})
        out = mom.render_markdown(built)
        self.assertIn("## Billing Model", out)
        self.assertIn("## Decisions", out)
        self.assertIn("Confirm the Q3 rate card", out)
        # The resolved date, not "tomorrow".
        self.assertIn("2026-09-11", out)
        self.assertNotIn("tomorrow", out)

    def test_regeneration_through_the_modern_path_is_stable(self):
        tasks = [task("t-a", "Confirm the Q3 rate card", "Amit")]
        first = mom.coerce_mom({
            "mom_version": mom.MOM_VERSION,
            "sections": mom.build_sections(self.item(), tasks=tasks)})
        merged = mom.merge_generated(
            first, mom.build_sections(self.item(), tasks=tasks))
        self.assertEqual(len(merged["sections"]), len(first["sections"]))
        self.assertEqual(mom.render_markdown(merged),
                         mom.render_markdown(first))


if __name__ == "__main__":
    unittest.main(verbosity=2)
