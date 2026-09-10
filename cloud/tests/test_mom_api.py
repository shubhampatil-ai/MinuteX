#!/usr/bin/env python3
"""test_mom_api.py — the structured MoM ROUTES, end to end against stubs.

test_mom_schema.py pins the data model in isolation. This file pins the four
routes that expose it, and above all the two things a unit test of the schema
cannot see:

  * THE MIRROR. Every structured write must also rewrite
    documents.minutes_of_meeting. If it doesn't, the editor and the exported
    DOCX show different documents — the worst failure this feature could have,
    and an invisible one until a user opens the file. Every write test here
    asserts on the mirror, not just on the structure.

  * OWNERSHIP. All four routes resolve the recording through _owned_recording,
    so another user's key must 404 (never 403 — the existing routes report a
    miss and a forbidden identically so the endpoint cannot be used to probe
    for other users' recordings).

OFFLINE by design, same as test_ai_workspace.py, whose event/parse/call
helpers and boto3 stubbing this file reuses rather than duplicating. No Groq
stub is needed anywhere below, which is itself the point: generating a MoM
runs no model, so these routes cannot fail on a rate limit.

Run:  python -m pytest tests/test_mom_api.py
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "functions/userapi"))

# Importing test_ai_workspace installs the boto3/Groq stubs and gives us the
# same `api` module object every other test in this directory binds — see
# conftest.py on why one shared stub identity matters.
from test_ai_workspace import (  # noqa: E402
    HIGHLIGHTS, RECORDING, api, call, event, parse,
)

import mom_schema  # noqa: E402


KEY = "recordings/u-1/mobile/mobile-abc_1754300000.m4a"


def mom_event(method, body=None, key=KEY, auth=True):
    return event(key=key, body=body, method=method,
                 route="/recordings/ai/mom/{key+}", auth=auth)


class MomRouteTestCase(unittest.TestCase):
    """Stubs auth, device ownership and the Recordings table.

    Unlike AiTestCase this table stub APPLIES the writes it receives, because
    every test here is about a round trip: generate, then read back, then
    save, then read back again. A stub that only records the last call cannot
    tell a working mirror from a broken one.
    """

    def setUp(self):
        self.item = json.loads(json.dumps(RECORDING))
        self.item["meeting_highlights"] = json.loads(json.dumps(HIGHLIGHTS))

        self.p_auth = mock.patch.object(api, "_require_auth", return_value="u-1")
        self.p_devices = mock.patch.object(api, "_owned_devices", return_value=[])
        self.p_table = mock.patch.object(api, "_recordings")
        # No tasks table read: the builder falls back to the highlights
        # extraction, which is the legacy-row path and keeps this file focused
        # on the MoM routes rather than on task seeding.
        self.p_tasks = mock.patch.object(api, "_tasks_for_recording",
                                         return_value=[])
        self.p_auth.start()
        self.p_devices.start()
        self.p_tasks.start()
        self.table = self.p_table.start()

        self.table.get_item.side_effect = \
            lambda **kw: {"Item": self.item} if self.item else {}
        self.table.update_item.side_effect = self._apply_update

        for p in (self.p_auth, self.p_devices, self.p_table, self.p_tasks):
            self.addCleanup(p.stop)

    def _apply_update(self, **kw):
        """Apply the handful of UpdateExpression shapes these routes emit.

        Deliberately a tiny interpreter rather than a real evaluator: the
        routes only ever SET a whole attribute, SET one nested document path,
        or REMOVE one attribute, and hand-rolling those three keeps the test
        honest about what is actually written without pulling in moto.
        """
        expr = kw.get("UpdateExpression", "")
        names = kw.get("ExpressionAttributeNames", {}) or {}
        values = kw.get("ExpressionAttributeValues", {}) or {}

        if "REMOVE #mom" in expr:
            self.item.pop(names.get("#mom", "mom"), None)
        if "SET #mom = :mom" in expr:
            self.item[names.get("#mom", "mom")] = values[":mom"]
        if "SET #docs = :empty" in expr:
            self.item.setdefault(names.get("#docs", "documents"), {})
        if "SET #docs.#t = :doc" in expr:
            docs = self.item.setdefault(names.get("#docs", "documents"), {})
            docs[names["#t"]] = values[":doc"]
        if "REMOVE #docs.#t" in expr:
            self.item.get(names.get("#docs", "documents"), {}).pop(names["#t"], None)
        return {}

    # -- helpers ------------------------------------------------------------
    def generate(self):
        status, body = parse(call(api.generate_mom, mom_event("POST", {})))
        self.assertEqual(status, 200, body)
        return body

    def read(self):
        status, body = parse(call(api.get_mom, mom_event("GET")))
        self.assertEqual(status, 200, body)
        return body

    def save(self, mom):
        return parse(call(api.save_mom, mom_event("PUT", {"mom": mom})))

    def stored_document(self):
        return (self.item.get("documents") or {}).get("minutes_of_meeting")

    def section(self, mom, role):
        return next(s for s in mom["sections"] if s["role"] == role)


# ===========================================================================
# Routing + ownership
# ===========================================================================
class TestRoutingAndAccess(MomRouteTestCase):
    def test_all_four_routes_are_registered(self):
        for method, handler in (("GET", api.get_mom), ("POST", api.generate_mom),
                                ("PUT", api.save_mom), ("DELETE", api.delete_mom)):
            self.assertIs(api._ROUTES[(method, "/recordings/ai/mom/{key+}")],
                          handler)

    def test_another_users_recording_is_404_not_403(self):
        # 404 for a miss AND for a forbidden, so the route cannot be used to
        # probe whether another user's recording exists.
        self.item["user_id"] = "u-2"
        for handler, method, body in (
            (api.get_mom, "GET", None),
            (api.generate_mom, "POST", {}),
            (api.save_mom, "PUT", {"mom": {"sections": []}}),
            (api.delete_mom, "DELETE", None),
        ):
            status, body_out = parse(call(handler, mom_event(method, body)))
            self.assertEqual(status, 404, f"{method}: {body_out}")
            self.assertNotIn("mom", body_out)

    def test_missing_recording_is_404(self):
        self.item = None
        status, _ = parse(call(api.get_mom, mom_event("GET")))
        self.assertEqual(status, 404)

    def test_generate_requires_a_transcript(self):
        # 409, not 400: the request is well-formed and will succeed later.
        self.item["transcript"] = ""
        self.item["status"] = "processing"
        status, body = parse(call(api.generate_mom, mom_event("POST", {})))
        self.assertEqual(status, 409)
        self.assertIn("transcript", body["error"].lower())


# ===========================================================================
# Generate
# ===========================================================================
class TestGenerate(MomRouteTestCase):
    def test_generate_builds_sections_and_mirrors_the_document(self):
        body = self.generate()
        roles = [s["role"] for s in body["mom"]["sections"]]
        self.assertIn(mom_schema.ROLE_ATTENDEES, roles)
        self.assertIn(mom_schema.ROLE_SUMMARY, roles)

        # The mirror is the load-bearing assertion.
        doc = self.stored_document()
        self.assertIsNotNone(doc)
        self.assertIn("## Attendees", doc["content"])
        self.assertEqual(doc["content"], mom_schema.render_markdown(body["mom"]))
        self.assertEqual(body["document"]["type"], "minutes_of_meeting")

    def test_generate_costs_no_groq_call(self):
        # The whole point of building from the existing analysis. A model call
        # here would reintroduce the rate-limit failure mode this design avoids.
        with mock.patch.object(api.groq_client, "complete") as complete:
            self.generate()
        complete.assert_not_called()

    def test_speaker_names_are_resolved_in_the_output(self):
        body = self.generate()
        attendees = self.section(body["mom"], mom_schema.ROLE_ATTENDEES)
        names = [r["cells"][attendees["columns"][1]["id"]] for r in attendees["rows"]]
        self.assertEqual(names, ["Ravi", "Priya"])

    def test_mirrored_document_is_marked_edited_so_regenerate_cannot_clobber_it(self):
        # `edited` is what the EXISTING _is_fresh rule reads to mean "never
        # silently overwrite". Without it, a Regenerate on the Documents
        # screen would replace the structured MoM with a prompt-generated blob.
        self.generate()
        self.assertTrue(self.stored_document()["edited"])
        self.assertTrue(api._is_fresh(self.stored_document(), "any-fingerprint"))

    def test_second_generate_reports_it_regenerated(self):
        self.assertFalse(self.generate()["regenerated"])
        self.assertTrue(self.generate()["regenerated"])

    def test_generate_refuses_when_there_is_nothing_to_write(self):
        # Meeting Details alone IS a usable MoM, so the title and date have to
        # go as well for the document to be genuinely empty. `ai_tasks` counts
        # too: Action Items now renders from it when no first-class Tasks have
        # been seeded yet, so leaving it populated would be a real document.
        self.item.update(summary="", highlights=[], participants=[],
                         meeting_highlights=None, title="", started_at="",
                         created_at="", ai_tasks=[])
        status, body = parse(call(api.generate_mom, mom_event("POST", {})))
        self.assertEqual(status, 409)
        self.assertIn("enough", body["error"])

    def test_regeneration_preserves_a_user_edit(self):
        """The end-to-end version of the schema's merge tests: edit through
        the real PUT route, regenerate through the real POST route."""
        mom = self.generate()["mom"]
        summary = self.section(mom, mom_schema.ROLE_SUMMARY)
        summary["text"] = "MY OWN SUMMARY"
        summary["source"] = mom_schema.SOURCE_USER_EDITED
        self.save(mom)

        after = self.generate()["mom"]
        self.assertEqual(
            self.section(after, mom_schema.ROLE_SUMMARY)["text"],
            "MY OWN SUMMARY")
        # And the mirror agrees — a preserved edit that never reaches the
        # document is not preserved as far as the user is concerned.
        self.assertIn("MY OWN SUMMARY", self.stored_document()["content"])

    def test_regeneration_does_not_resurrect_a_deleted_section(self):
        mom = self.generate()["mom"]
        target = self.section(mom, mom_schema.ROLE_DECISIONS)
        mom["sections"] = [s for s in mom["sections"] if s["id"] != target["id"]]
        mom["deleted_ids"] = [target["id"]]
        self.save(mom)

        after = self.generate()["mom"]
        self.assertNotIn(mom_schema.ROLE_DECISIONS,
                         [s["role"] for s in after["sections"]])
        self.assertNotIn("## Decisions", self.stored_document()["content"])


# ===========================================================================
# Read
# ===========================================================================
class TestRead(MomRouteTestCase):
    def test_reading_before_generating_does_not_write(self):
        # Opening the editor on a recording the user was only browsing must
        # not cost a write.
        body = self.read()
        self.assertFalse(body["exists"])
        self.assertEqual(body["mom"]["sections"], [])
        self.assertIsNone(body["document"])
        self.table.update_item.assert_not_called()

    def test_read_returns_what_generate_stored(self):
        generated = self.generate()["mom"]
        got = self.read()
        self.assertTrue(got["exists"])
        self.assertEqual([s["id"] for s in got["mom"]["sections"]],
                         [s["id"] for s in generated["sections"]])
        self.assertEqual(got["document"]["type"], "minutes_of_meeting")

    def test_corrupt_stored_structure_reads_as_empty_not_500(self):
        self.item["mom"] = {"sections": "not a list"}
        body = self.read()
        self.assertEqual(body["mom"]["sections"], [])


# ===========================================================================
# Save — every editor operation goes through this one route.
# ===========================================================================
class TestSave(MomRouteTestCase):
    def test_edit_a_field_persists_and_mirrors(self):
        mom = self.generate()["mom"]
        details = self.section(mom, mom_schema.ROLE_DETAILS)
        details["fields"][0]["value"] = "1 January 2027"
        details["fields"][0]["source"] = mom_schema.SOURCE_USER_EDITED
        status, body = self.save(mom)
        self.assertEqual(status, 200)
        self.assertIn("1 January 2027", self.stored_document()["content"])
        self.assertIn("1 January 2027",
                      mom_schema.render_markdown(body["mom"]))

    def test_add_a_custom_field(self):
        mom = self.generate()["mom"]
        details = self.section(mom, mom_schema.ROLE_DETAILS)
        details["fields"].append({
            "id": "f_client", "label": "Client", "value": "ABC Technologies",
            "visible": True, "source": mom_schema.SOURCE_USER_ADDED})
        self.save(mom)
        self.assertIn("**Client:** ABC Technologies",
                      self.stored_document()["content"])

    def test_delete_a_field(self):
        mom = self.generate()["mom"]
        details = self.section(mom, mom_schema.ROLE_DETAILS)
        dropped = details["fields"][0]
        details["fields"] = details["fields"][1:]
        mom["deleted_ids"] = [dropped["id"]]
        self.save(mom)
        self.assertNotIn(f"**{dropped['label']}:**",
                         self.stored_document()["content"])

    def test_add_a_section(self):
        mom = self.generate()["mom"]
        mom["sections"].append({
            "id": "s_notes", "kind": "text", "title": "Client Notes",
            "visible": True, "source": mom_schema.SOURCE_USER_ADDED,
            "role": "", "text": "Renewal due in Q4."})
        self.save(mom)
        content = self.stored_document()["content"]
        self.assertIn("## Client Notes", content)
        self.assertIn("Renewal due in Q4.", content)

    def test_reorder_sections_changes_the_document_order(self):
        mom = self.generate()["mom"]
        actions = self.section(mom, mom_schema.ROLE_ACTIONS)
        mom["sections"].remove(actions)
        mom["sections"].insert(0, actions)
        self.save(mom)
        content = self.stored_document()["content"]
        self.assertLess(content.index("## Action Items"),
                        content.index("## Attendees"))

    def test_hide_a_section_removes_it_from_the_document_but_keeps_it_stored(self):
        mom = self.generate()["mom"]
        self.section(mom, mom_schema.ROLE_SUMMARY)["visible"] = False
        body = self.save(mom)[1]
        self.assertNotIn("## Summary", self.stored_document()["content"])
        # Still in the structure, so the user can unhide it.
        self.assertIn(mom_schema.ROLE_SUMMARY,
                      [s["role"] for s in body["mom"]["sections"]])

    def test_edit_a_table_cell(self):
        mom = self.generate()["mom"]
        attendees = self.section(mom, mom_schema.ROLE_ATTENDEES)
        name_col = attendees["columns"][1]["id"]
        attendees["rows"][0]["cells"][name_col] = "Ravi Kumar"
        attendees["rows"][0]["source"] = mom_schema.SOURCE_USER_EDITED
        self.save(mom)
        self.assertIn("Ravi Kumar", self.stored_document()["content"])

    def test_add_and_delete_a_table_row(self):
        mom = self.generate()["mom"]
        attendees = self.section(mom, mom_schema.ROLE_ATTENDEES)
        cols = [c["id"] for c in attendees["columns"]]
        attendees["rows"].append({
            "id": "r_new", "visible": True, "source": mom_schema.SOURCE_USER_ADDED,
            "cells": {cols[0]: "3", cols[1]: "Meera", cols[2]: "Observer"}})
        self.save(mom)
        self.assertIn("Meera", self.stored_document()["content"])

        mom = self.read()["mom"]
        attendees = self.section(mom, mom_schema.ROLE_ATTENDEES)
        dropped = attendees["rows"][0]
        attendees["rows"] = attendees["rows"][1:]
        mom["deleted_ids"] = [dropped["id"]]
        self.save(mom)
        # Scoped to the Attendees table: "Ravi" legitimately still appears as
        # an Owner in Meeting Points, so a document-wide assertion would pass
        # or fail for the wrong reason.
        attendees_block = self.stored_document()["content"]             .split("## Attendees")[1].split("##")[0]
        self.assertNotIn("Ravi", attendees_block)
        self.assertIn("Meera", attendees_block)

    def test_add_a_column_gives_every_existing_row_a_blank_cell(self):
        mom = self.generate()["mom"]
        attendees = self.section(mom, mom_schema.ROLE_ATTENDEES)
        attendees["columns"].append({"id": "c_email", "label": "Email",
                                     "source": mom_schema.SOURCE_USER_ADDED})
        body = self.save(mom)[1]
        saved = self.section(body["mom"], mom_schema.ROLE_ATTENDEES)
        for row in saved["rows"]:
            self.assertIn("c_email", row["cells"])
        header = self.stored_document()["content"].splitlines()
        self.assertTrue(any("| Email |" in line for line in header))

    def test_delete_a_column_does_not_shift_the_other_cells(self):
        # The regression cell-keying prevents: with position-keyed cells,
        # deleting a middle column moves every later value one place left.
        mom = self.generate()["mom"]
        attendees = self.section(mom, mom_schema.ROLE_ATTENDEES)
        keep_last = attendees["columns"][2]["id"]
        expected = attendees["rows"][0]["cells"][keep_last]
        attendees["columns"] = [attendees["columns"][0], attendees["columns"][2]]
        body = self.save(mom)[1]
        saved = self.section(body["mom"], mom_schema.ROLE_ATTENDEES)
        self.assertEqual(saved["rows"][0]["cells"][keep_last], expected)
        self.assertEqual(len(saved["columns"]), 2)

    def test_rename_a_column(self):
        mom = self.generate()["mom"]
        attendees = self.section(mom, mom_schema.ROLE_ATTENDEES)
        attendees["columns"][1]["label"] = "Attendee"
        self.save(mom)
        self.assertIn("| Attendee |", self.stored_document()["content"])

    def test_rename_a_section(self):
        mom = self.generate()["mom"]
        self.section(mom, mom_schema.ROLE_ATTENDEES)["title"] = "Participants"
        self.save(mom)
        content = self.stored_document()["content"]
        self.assertIn("## Participants", content)
        self.assertNotIn("## Attendees", content)

    def test_save_rejects_a_missing_or_empty_structure(self):
        for payload in ({}, {"mom": "nope"}, {"mom": {"sections": []}}):
            status, _ = parse(call(api.save_mom, mom_event("PUT", payload)))
            self.assertEqual(status, 400, payload)

    def test_save_survives_a_hostile_payload(self):
        # Coercion, not validation-by-rejection: junk inside a valid envelope
        # is normalised away rather than 500ing.
        mom = self.generate()["mom"]
        mom["sections"].append({"kind": "table", "title": "Junk",
                                "columns": [None, 5], "rows": ["x", {}, None]})
        status, body = self.save(mom)
        self.assertEqual(status, 200)
        junk = next(s for s in body["mom"]["sections"] if s["title"] == "Junk")
        self.assertEqual(junk["columns"], [])
        self.assertEqual(junk["rows"], [])

    def test_a_very_long_mom_truncates_the_mirror_not_the_structure(self):
        mom = self.generate()["mom"]
        mom["sections"].append({
            "id": "s_big", "kind": "text", "title": "Big",
            "visible": True, "source": mom_schema.SOURCE_USER_ADDED, "role": "",
            "text": "x" * (mom_schema.MAX_TEXT_CHARS)})
        for i in range(3):
            mom["sections"].append({
                "id": f"s_big{i}", "kind": "text", "title": f"Big {i}",
                "visible": True, "source": mom_schema.SOURCE_USER_ADDED,
                "role": "", "text": "y" * mom_schema.MAX_TEXT_CHARS})
        status, body = self.save(mom)
        self.assertEqual(status, 200)
        content = self.stored_document()["content"]
        self.assertLessEqual(len(content), api.MAX_DOCUMENT_CHARS + 40)
        self.assertIn("[Output truncated.]", content)
        # The structure itself is intact — it is the source of truth.
        self.assertIn("s_big", [s["id"] for s in body["mom"]["sections"]])


# ===========================================================================
# Delete
# ===========================================================================
class TestDelete(MomRouteTestCase):
    def test_delete_removes_the_structure_but_keeps_the_document(self):
        self.generate()
        status, body = parse(call(api.delete_mom, mom_event("DELETE")))
        self.assertEqual(status, 200)
        self.assertTrue(body["deleted"])
        self.assertNotIn("mom", self.item)
        # The exported/listed document survives — deleting the editor's
        # structure must not destroy a document the user may still want.
        self.assertIsNotNone(self.stored_document())

    def test_delete_without_a_mom_is_404(self):
        status, _ = parse(call(api.delete_mom, mom_event("DELETE")))
        self.assertEqual(status, 404)

    def test_generate_after_delete_starts_clean(self):
        mom = self.generate()["mom"]
        self.section(mom, mom_schema.ROLE_SUMMARY)["text"] = "MY SUMMARY"
        self.section(mom, mom_schema.ROLE_SUMMARY)["source"] = \
            mom_schema.SOURCE_USER_EDITED
        self.save(mom)
        call(api.delete_mom, mom_event("DELETE"))

        after = self.generate()["mom"]
        self.assertNotEqual(
            self.section(after, mom_schema.ROLE_SUMMARY)["text"], "MY SUMMARY")


class TestTaskBackedRoutes(MomRouteTestCase):
    """The AUTHORITATIVE path: real Tasks, not the legacy highlights fallback.

    Every other test in this file stubs _tasks_for_recording to [] so the
    builder falls through to meeting_highlights.action_items. That kept those
    tests focused, but it also meant the source the product actually uses was
    never exercised at the route level. These re-point the stub at real task
    rows in the shape _tasks_for_recording returns them.
    """

    def task_row(self, task_id, title, owner="", due="", normalized=""):
        """A Tasks-table ROW (not the public payload) — _public_task_v2 turns
        this into what the MoM builder sees, so the whole chain is covered."""
        return {
            "task_id": task_id,
            "owner_user_id": "u-1",
            "recording_key": KEY,
            "title": title,
            "status": "open",
            "priority": "Medium",
            "due_date": due,
            "due_date_normalized": normalized,
            "assignee_name": owner,
            "assignee_speaker_id": "",
            "created_at": f"2026-09-10T00:00:0{task_id[-1]}Z",
            "source_type": "AI",
        }

    def use_tasks(self, rows):
        """Re-point the task fetch. The base class stubs it to [] so the
        builder uses the legacy fallback; these tests want the real tier."""
        self.p_tasks.stop()
        self.p_tasks = mock.patch.object(api, "_tasks_for_recording",
                                         return_value=rows)
        self.p_tasks.start()
        self.addCleanup(self.p_tasks.stop)

    def action_section(self, payload):
        return next(s for s in payload["mom"]["sections"]
                    if s["role"] == mom_schema.ROLE_ACTIONS)

    def cell(self, section, row, label):
        col = next(c["id"] for c in section["columns"]
                   if c["label"] == label)
        return row["cells"][col]

    def test_generated_rows_carry_the_task_id(self):
        self.use_tasks([self.task_row("t-a", "Confirm the rate card", "Amit")])
        body = self.generate()
        row = self.action_section(body)["rows"][0]
        self.assertEqual(row[mom_schema.TASK_REF], "t-a")

    def test_the_task_id_is_not_rendered_into_the_document(self):
        """It is internal identity — the mirror must not leak it."""
        self.use_tasks([self.task_row("t-a", "Confirm the rate card", "Amit")])
        body = self.generate()
        self.assertNotIn("t-a", body["document"]["content"])
        self.assertNotIn(mom_schema.TASK_REF, body["document"]["content"])

    def test_normalized_due_date_is_shown_not_the_spoken_phrase(self):
        self.use_tasks([self.task_row("t-a", "Ship the build", "Amit",
                                      due="tomorrow",
                                      normalized="2026-09-11")])
        body = self.generate()
        section = self.action_section(body)
        self.assertEqual(self.cell(section, section["rows"][0], "Deadline"),
                         "2026-09-11")
        self.assertIn("2026-09-11", body["document"]["content"])

    def test_unresolvable_due_date_falls_back_to_the_phrase(self):
        self.use_tasks([self.task_row("t-a", "Ship the build", "Amit",
                                      due="Next Meeting", normalized="")])
        section = self.action_section(self.generate())
        self.assertEqual(self.cell(section, section["rows"][0], "Deadline"),
                         "Next Meeting")

    def test_deleting_a_task_removes_its_row_on_regeneration(self):
        rows = [self.task_row("t-a", "Task A", "Amit"),
                self.task_row("t-b", "Task B", "Neha")]
        self.use_tasks(rows)
        self.generate()
        # The user deletes Task A in the Tasks screen.
        self.use_tasks(rows[1:])
        section = self.action_section(self.generate())
        self.assertEqual([r[mom_schema.TASK_REF] for r in section["rows"]],
                         ["t-b"])

    def test_a_user_edit_survives_deletion_of_an_earlier_task(self):
        rows = [self.task_row("t-a", "Task A", "Amit"),
                self.task_row("t-b", "Task B", "Neha"),
                self.task_row("t-c", "Task C", "Ravi")]
        self.use_tasks(rows)
        body = self.generate()
        doc = body["mom"]
        section = next(s for s in doc["sections"]
                       if s["role"] == mom_schema.ROLE_ACTIONS)
        owner = next(c["id"] for c in section["columns"]
                     if c["label"] == "Owner")
        target = next(r for r in section["rows"]
                      if r[mom_schema.TASK_REF] == "t-b")
        target["cells"][owner] = "Priya"
        target["source"] = mom_schema.SOURCE_USER_EDITED
        self.save(doc)

        self.use_tasks(rows[1:])          # Task A deleted
        after = self.action_section(self.generate())
        self.assertEqual([r[mom_schema.TASK_REF] for r in after["rows"]],
                         ["t-b", "t-c"])
        kept = next(r for r in after["rows"]
                    if r[mom_schema.TASK_REF] == "t-b")
        self.assertEqual(self.cell(after, kept, "Owner"), "Priya")

    def test_a_new_mom_is_stamped_with_the_current_version(self):
        self.use_tasks([self.task_row("t-a", "Task A", "Amit")])
        body = self.generate()
        self.assertEqual(body["mom"]["mom_version"], mom_schema.MOM_VERSION)


class TestMomIsStructuredOnly(MomRouteTestCase):
    """The generic document routes must not author a second MoM."""

    def doc_event(self, body):
        return event(key=KEY, body=body, method="POST",
                     route="/recordings/ai/documents/{key+}")

    def generate_doc(self, body):
        return parse(call(api.generate_document, self.doc_event(body)))

    def test_generate_document_refuses_minutes_of_meeting(self):
        status, body = self.generate_doc({"type": "minutes_of_meeting",
                                          "regenerate": True})
        self.assertEqual(status, 409, body)
        self.assertIn("structured", body["error"].lower())

    def test_quick_action_refuses_minutes_of_meeting(self):
        """Quick AI aliases the same document key, so it is a second door."""
        ev = event(key=KEY, body={"action": "minutes_of_meeting",
                                  "regenerate": True},
                   method="POST", route="/recordings/ai/quick/{key+}")
        status, body = parse(call(api.quick_action, ev))
        self.assertEqual(status, 409, body)

    def test_other_document_types_are_unaffected(self):
        """The guard must be scoped to the MoM, not to the route."""
        with mock.patch.object(
                api, "_generate_document",
                return_value={"content": "x", "format": "markdown",
                              "edited": False}) as gen:
            status, body = self.generate_doc({"type": "action_items",
                                              "regenerate": True})
        self.assertEqual(status, 200, body)
        self.assertTrue(gen.called)

    def test_a_stored_legacy_markdown_mom_is_still_readable(self):
        """Path A documents already in production must keep working: the
        guard sits AFTER the cache check so a read still serves them."""
        self.item["documents"] = {"minutes_of_meeting": {
            "content": "## Minutes\n\nLegacy Path A body.",
            "format": "markdown",
            "edited": True,
        }}
        status, body = self.generate_doc({"type": "minutes_of_meeting"})
        self.assertEqual(status, 200, body)
        self.assertIn("Legacy Path A body.", body["document"]["content"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
