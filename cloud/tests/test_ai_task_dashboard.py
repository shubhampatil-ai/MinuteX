#!/usr/bin/env python3
# =============================================================
# test_ai_task_dashboard.py — the AI Task Dashboard: grounded task context
# through PageIndex, structured task intelligence, and confirmation-gated
# task actions.
#
# OFFLINE by design, exactly like test_ai_assistant.py, whose AIBase harness
# this file reuses: no AWS, no Groq, no network. DynamoDB is fake_dynamodb and
# the model is a scripted stub, so a failure here is a real bug.
#
# WHAT IS ACTUALLY AT RISK IN THIS FEATURE, and therefore what these tests are
# mostly about. Three things, none of them the happy path:
#
#   1. A NEW READ PATH TO TRANSCRIPTS. get_meeting_context is the first AI tool
#      that returns meeting CONTENT. Every test that can ask "could the wrong
#      person reach this?" does. The sharpest case is the task ASSIGNEE: they
#      may legitimately see the task and the meeting's notes, but the assignee
#      meeting route pins transcript_enabled OFF, so the AI must not become a
#      second, weaker door to the raw transcript.
#
#   2. HISTORY MASQUERADING AS STATE. The transcript says "Priya will do it by
#      Friday"; the task row says Completed, assigned to someone else. The row
#      wins, always. That is enforced by the prompt, so the prompt is tested —
#      a rule nobody asserts on is a rule that silently disappears in an edit.
#
#   3. A MUTATION PATH THAT MUST NOT MUTATE. propose_task_change writes
#      nothing, ever. It is checked for that directly (the row is re-read after
#      the call), and for refusing a proposal the caller would not be allowed
#      to apply — a Confirm button whose only outcome is a 403 is worse than
#      no button.
#
# COVERAGE
#   TASK VISIBILITY   the assistant sees creator AND assignee tasks, matching
#                     the dashboard; a stranger's task stays invisible
#   PAGEINDEX         retrieval reached through a task, lazy index build,
#                     retrieval failure degrades, seg_N ids preserved
#   EVIDENCE          sources carry meeting_id, come from retrieval not the
#                     model, and are de-duplicated
#   PROPOSALS         nothing is written; validation; authorization reuse;
#                     the returned patch is the real route's shape
#   INTELLIGENCE      structured rows keyed by real task ids, invented ids
#                     dropped, no task STATE in the schema, empty short-circuit
#   SECURITY          identity args stripped on the new tools; no recording
#                     key parameter exists; unauthorized meeting refused
#   PROMPT            state-vs-history precedence and the propose-never-apply
#                     wording rules are present
#   ROUTES            registered, and the response keys are additive
#
# Run:  python tests/test_ai_task_dashboard.py
# =============================================================
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

# ORDER MATTERS. Importing the assistant harness first installs the shared
# boto3 stubs and binds the SAME `api` module object (and the same Key class
# the Lambda holds a reference to) that every other suite uses.
from test_ai_assistant import (  # noqa: E402
    AIBase, FakeModel, KEY, OTHER, OTHER_KEY, USER, call, event, parse,
    api, days_out,
)
import ai_schema  # noqa: E402
import groq_client  # noqa: E402
import pageindex  # noqa: E402
import prompts  # noqa: E402


# A transcript long enough to be worth indexing, with the answer deliberately
# in the SECOND HALF: retrieval that silently head-truncated would still pass a
# test whose answer sat in the first few lines, which is the exact bug
# PageIndex exists to fix.
def make_segments(count=40, seconds_each=12.0, answer_at=None, answer=""):
    segs, t = [], 0.0
    for i in range(count):
        text = f"Turn {i}: routine discussion of the quarter's plan."
        if answer_at is not None and i == answer_at:
            text = answer or "We decided Rahul owns the pricing proposal."
        segs.append({"speaker": str(i % 2), "text": text,
                     "start": t, "end": t + seconds_each})
        t += seconds_each
    return segs


def transcript_for(segments):
    """The prose transcript matching `segments`, in stt_result's shape —
    blank-line separated, one turn per line, which is what
    transcript_store.as_labelled_lines pairs against by position."""
    return "\n\n".join(f"Speaker {s['speaker']}: {s['text']}" for s in segments)


class TaskContextBase(AIBase):
    """AIBase plus a real transcript on the meeting and an S3 stub that round
    -trips a PageIndex tree, so retrieval runs for real rather than mocked."""

    def setUp(self):
        super().setUp()
        self.segments = make_segments(answer_at=31)
        self.transcript = transcript_for(self.segments)

        rec = self.t["recordings"].get_item(
            Key={"audio_s3_key": KEY})["Item"]
        rec["transcript"] = self.transcript
        rec["timestamps"] = self.segments
        rec["transcript_fingerprint"] = ai_schema.fingerprint(self.transcript)
        self.t["recordings"].put_item(Item=rec)

        self._objects = {}

        def _put(**kw):
            self._objects[kw["Key"]] = kw["Body"]
            return {}

        def _get(**kw):
            if kw["Key"] not in self._objects:
                raise RuntimeError("NoSuchKey")
            return {"Body": mock.MagicMock(
                read=mock.MagicMock(return_value=self._objects[kw["Key"]]))}

        s3 = mock.patch.object(api, "_s3")
        self.s3 = s3.start()
        self.addCleanup(s3.stop)
        self.s3.put_object.side_effect = _put
        self.s3.get_object.side_effect = _get

        bucket = mock.patch.object(api, "BUCKET_NAME", "test-bucket")
        bucket.start()
        self.addCleanup(bucket.stop)

        # The transcript lives on the row in these tests, so hydrate is a
        # pass-through — the S3 stub above is for the INDEX, not the text.
        hydrate = mock.patch.object(
            api.transcript_store, "hydrate",
            side_effect=lambda s3c, b, item: item)
        hydrate.start()
        self.addCleanup(hydrate.stop)

    def stub_retrieval_groq(self, nodes=None):
        """Groq for the retrieval path: the navigation call answers with node
        ids, anything else with prose. Keyed on `label`, the way the real calls
        differ, so a test cannot pass when the two are swapped."""
        p = mock.patch.object(groq_client, "complete")
        m = p.start()
        self.addCleanup(p.stop)

        def _fn(*args, **kwargs):
            if kwargs.get("label") == "pageindex-retrieval":
                return json.dumps({"nodes": nodes or ["node_0"]})
            return "prose"

        m.side_effect = _fn
        return m

    def tiny_budget(self, chars=800):
        """Force the RETRIEVAL path rather than the send-the-whole-transcript
        path. Retrieval only engages when the transcript does not fit, and a
        test that never triggered it would be asserting on the fallback."""
        p = mock.patch.object(api, "_transcript_budget_chars",
                              return_value=chars)
        p.start()
        self.addCleanup(p.stop)


# ===========================================================================
# TASK VISIBILITY — the assistant must see what the dashboard sees.
# ===========================================================================
class TestAssistantSeesAssignedWork(AIBase):
    def test_a_task_assigned_to_me_by_someone_else_is_visible(self):
        """THE BUG THIS PINS. Tasks live in the CREATOR's partition, so a task
        User B created for User A is not in A's owner-index. Reading only that
        index made the assistant answer "you have no tasks" to a user whose
        whole workload was delegated to them — while GET /tasks, which reads
        both partitions, listed every one."""
        self.mk_task("Bob's task for Alice", owner=OTHER,
                     default_key=OTHER_KEY, assignee_user_id=USER)
        rows = api._ai_owner_tasks(self.ctx())
        self.assertEqual([r["title"] for r in rows], ["Bob's task for Alice"])

    def test_a_task_i_created_is_still_visible(self):
        self.mk_task("My own task")
        rows = api._ai_owner_tasks(self.ctx())
        self.assertEqual([r["title"] for r in rows], ["My own task"])

    def test_a_task_that_is_neither_mine_nor_assigned_to_me_is_invisible(self):
        self.mk_task("Bob's private task", owner=OTHER,
                     default_key=OTHER_KEY, assignee_user_id=OTHER)
        self.assertEqual(api._ai_owner_tasks(self.ctx()), [])

    def test_a_task_appearing_in_both_partitions_is_returned_once(self):
        """Created by me AND assigned to me: two index reads, one task."""
        self.mk_task("Mine both ways", assignee_user_id=USER)
        rows = api._ai_owner_tasks(self.ctx())
        self.assertEqual(len(rows), 1)

    def test_get_task_detail_works_for_an_assignee(self):
        """Listing a task and then 404-ing on its detail would be the
        assistant contradicting itself — which _owned_task (creator-only) did
        before this used _visible_task."""
        row = self.mk_task("Delegated to Alice", owner=OTHER,
                           default_key=OTHER_KEY, assignee_user_id=USER)
        out = api.tool_get_task(self.ctx(), task_id=row["task_id"])
        self.assertEqual(out["task"]["title"], "Delegated to Alice")

    def test_a_strangers_task_id_is_still_not_found(self):
        row = self.mk_task("Bob's", owner=OTHER, default_key=OTHER_KEY,
                           assignee_user_id=OTHER)
        with self.assertRaises(api.AIToolError):
            api.tool_get_task(self.ctx(), task_id=row["task_id"])


# ===========================================================================
# PAGEINDEX MEETING CONTEXT — the task's "why", authorized through the task.
# ===========================================================================
class TestMeetingContextAuthorization(TaskContextBase):
    def test_the_tool_has_no_recording_key_parameter(self):
        """THE STRUCTURAL GUARANTEE. Transcript access is reachable only
        through a task the caller can see, so there is no argument a model
        could fill in to reach an arbitrary meeting's index."""
        schema = next(s for s in api.AI_TOOL_SCHEMAS
                      if s["function"]["name"] == "get_meeting_context")
        props = set(schema["function"]["parameters"]["properties"])
        self.assertEqual(props, {"task_id", "question"})
        self.assertNotIn("recording_key", props)
        # And the dispatcher's allowlist agrees — a schema and an allowlist
        # that disagreed would be a widening nobody reviewed.
        self.assertEqual(set(api.AI_TOOLS["get_meeting_context"][1]),
                         {"task_id", "question"})

    def test_another_users_task_is_not_found(self):
        row = self.mk_task("Bob's", owner=OTHER, default_key=OTHER_KEY,
                           assignee_user_id=OTHER)
        with self.assertRaises(api.AIToolError) as cm:
            api.tool_get_meeting_context(self.ctx(), task_id=row["task_id"])
        self.assertIn("no task with that id", str(cm.exception).lower())

    def test_an_invented_task_id_is_not_found(self):
        with self.assertRaises(api.AIToolError):
            api.tool_get_meeting_context(self.ctx(), task_id="t-does-not-exist")

    def test_a_missing_task_id_is_refused(self):
        with self.assertRaises(api.AIToolError):
            api.tool_get_meeting_context(self.ctx(), task_id="")

    def test_an_assignee_cannot_read_the_transcript_through_the_ai(self):
        """THE MOST IMPORTANT TEST IN THIS FILE.

        A task assignee may open the meeting's NOTES — and
        get_assignee_meeting pins transcript_enabled and audio_enabled OFF
        precisely so they cannot reach the raw record. If the AI handed them
        labelled transcript segments it would route around that decision, and
        the assistant would be the weaker of the two doors.
        """
        row = self.mk_task("Delegated to Alice", owner=OTHER,
                           default_key=OTHER_KEY, assignee_user_id=USER)
        # Give Bob's meeting a transcript too, so the refusal cannot be an
        # accident of there being nothing to read.
        rec = self.t["recordings"].get_item(
            Key={"audio_s3_key": OTHER_KEY})["Item"]
        rec["transcript"] = self.transcript
        rec["timestamps"] = self.segments
        self.t["recordings"].put_item(Item=rec)

        with self.assertRaises(api.AIToolError) as cm:
            api.tool_get_meeting_context(self.ctx(), task_id=row["task_id"])
        self.assertIn("not available", str(cm.exception).lower())

    def test_a_task_with_no_source_meeting_says_so(self):
        """A task carrying no source_recording_id is an ordinary answer, not
        an error the model should apologize around.

        Written by hand rather than through _new_task_row because
        source_recording_id is a GSI key and every production path populates
        it (see AIBase.mk_task). This row shape reaches the tool from a LEGACY
        task written before the meeting-index existed — which is exactly the
        case worth covering, since a modern one cannot produce it.
        """
        self.t["tasks"].put_item(Item={
            "task_id": "t-legacy-manual", "owner_user_id": USER,
            "title": "Typed by hand", "status": api.TASK_STATUS_OPEN,
            "priority": "Medium", "created_at": days_out(-3) + "T00:00:00Z"})
        with self.assertRaises(api.AIToolError) as cm:
            api.tool_get_meeting_context(self.ctx(),
                                         task_id="t-legacy-manual")
        self.assertIn("manually", str(cm.exception).lower())

    def test_a_trashed_meeting_is_no_longer_readable(self):
        """A trashed meeting stops being readable through a task exactly as it
        stops being readable through a share link."""
        row = self.mk_task("From the meeting")
        rec = self.t["recordings"].get_item(Key={"audio_s3_key": KEY})["Item"]
        rec["recording_status"] = "trashed"
        rec["trashed_at"] = days_out(0) + "T00:00:00Z"
        self.t["recordings"].put_item(Item=rec)
        with self.assertRaises(api.AIToolError) as cm:
            api.tool_get_meeting_context(self.ctx(), task_id=row["task_id"])
        self.assertIn("no longer available", str(cm.exception).lower())


class TestMeetingContextRetrieval(TaskContextBase):
    def test_retrieval_returns_transcript_with_seg_ids_preserved(self):
        """seg_N is the identity the app deep-links on. If retrieval returned
        text without the ids, every source row would scroll nowhere."""
        row = self.mk_task("Pricing proposal")
        self.tiny_budget()
        self.stub_retrieval_groq()
        out = api.tool_get_meeting_context(
            self.ctx(), task_id=row["task_id"], question="who owns pricing?")
        self.assertIn("seg_", out["historical_discussion"])

    def test_the_index_is_built_lazily_on_the_first_question(self):
        """An old meeting with no index must not be a user-visible error: the
        feature repairs itself on demand and the user just gets an answer."""
        row = self.mk_task("Pricing proposal")
        self.tiny_budget()
        self.stub_retrieval_groq()
        self.assertEqual(self._objects, {})
        api.tool_get_meeting_context(self.ctx(), task_id=row["task_id"],
                                     question="pricing")
        self.assertTrue(any("pageindex" in k for k in self._objects),
                        "no index object was written")

    def test_evidence_rows_carry_the_meeting_id(self):
        """The workspace assistant can cite several meetings in one answer, so
        a bare segment id would not say which transcript to open. This is the
        one field the per-meeting chat's sources never needed."""
        row = self.mk_task("Pricing proposal")
        self.tiny_budget()
        self.stub_retrieval_groq()
        ctx = self.ctx()
        out = api.tool_get_meeting_context(ctx, task_id=row["task_id"],
                                          question="pricing")
        self.assertTrue(out["evidence"], "retrieval produced no evidence")
        for ev in out["evidence"]:
            self.assertEqual(ev["meeting_id"], KEY)
            self.assertTrue(ev["segment_id"].startswith("seg_"))
            # The documented shape, so the app can render and seek.
            self.assertIn("start_time", ev)
            self.assertIn("end_time", ev)

    def test_sources_are_recorded_on_the_context_for_the_response(self):
        row = self.mk_task("Pricing proposal")
        self.tiny_budget()
        self.stub_retrieval_groq()
        ctx = self.ctx()
        api.tool_get_meeting_context(ctx, task_id=row["task_id"],
                                     question="pricing")
        self.assertTrue(ctx.sources)
        self.assertTrue(all(s["meeting_id"] == KEY for s in ctx.sources))

    def test_sources_are_deduplicated_across_two_calls(self):
        """Two questions about the same task retrieve overlapping segments;
        the response must not list the same source twice."""
        row = self.mk_task("Pricing proposal")
        self.tiny_budget()
        self.stub_retrieval_groq()
        ctx = self.ctx()
        api.tool_get_meeting_context(ctx, task_id=row["task_id"], question="a")
        first = len(ctx.sources)
        api.tool_get_meeting_context(ctx, task_id=row["task_id"], question="b")
        self.assertEqual(len(ctx.sources), first)

    def test_the_result_restates_the_tasks_current_state(self):
        """Repeated INSIDE the retrieval result on purpose: this is the tool
        whose output most invites a model to describe stale transcript talk as
        the task's present state, and the correction works best next to the
        temptation."""
        row = self.mk_task("Pricing proposal", status="In Progress",
                           due=days_out(3))
        self.tiny_budget()
        self.stub_retrieval_groq()
        out = api.tool_get_meeting_context(self.ctx(), task_id=row["task_id"])
        state = out["current_task_state"]
        self.assertEqual(state["status"], "In Progress")
        self.assertEqual(state["due_date"], days_out(3))
        self.assertIn("current", state["note"].lower())

    def test_the_discussion_is_labelled_as_history_not_state(self):
        """The KEY NAME is part of the contract: a field called `transcript`
        invites the model to treat it as the record, `historical_discussion`
        says what it is."""
        row = self.mk_task("Pricing proposal")
        self.tiny_budget()
        self.stub_retrieval_groq()
        out = api.tool_get_meeting_context(self.ctx(), task_id=row["task_id"])
        self.assertIn("historical_discussion", out)
        self.assertNotIn("transcript", out)

    def test_a_question_defaults_to_the_task_title(self):
        """A model that forgets to pass a question must still retrieve
        something pointed at THIS task, not at the meeting in general."""
        row = self.mk_task("Pricing proposal")
        self.tiny_budget()
        groq = self.stub_retrieval_groq()
        api.tool_get_meeting_context(self.ctx(), task_id=row["task_id"],
                                     question="")
        asked = "".join(str(c) for c in groq.call_args_list)
        self.assertIn("Pricing proposal", asked)

    def test_a_retrieval_failure_is_reported_not_raised(self):
        """A Groq failure inside a tool must not fail the whole conversation:
        the model is told what happened and answers from the task row."""
        row = self.mk_task("Pricing proposal")
        with mock.patch.object(
                api, "retrieve_meeting_context",
                side_effect=groq_client.GroqError("groq down")):
            with self.assertRaises(api.AIToolError) as cm:
                api.tool_get_meeting_context(self.ctx(),
                                             task_id=row["task_id"])
        self.assertIn("could not be retrieved", str(cm.exception).lower())

    def test_a_short_transcript_skips_retrieval_and_still_returns_context(self):
        """Whole-meeting-fits is the common case and must not need an index —
        retrieval could only subtract from a complete record."""
        row = self.mk_task("Pricing proposal")
        self.stub_retrieval_groq()
        out = api.tool_get_meeting_context(self.ctx(), task_id=row["task_id"],
                                           question="pricing")
        self.assertTrue(out["historical_discussion"].strip())

    def test_the_excerpt_is_bounded(self):
        """The tool result rides inside an agent conversation that already
        holds a system prompt, history and earlier results — an unbounded
        excerpt would crowd out the answer."""
        row = self.mk_task("Pricing proposal")
        self.stub_retrieval_groq()
        out = api.tool_get_meeting_context(self.ctx(), task_id=row["task_id"])
        self.assertLessEqual(len(out["historical_discussion"]),
                             api.AI_CONTEXT_MAX_CHARS)


# ===========================================================================
# CONFIRMATION-GATED ACTIONS — a mutation path that must not mutate.
# ===========================================================================
class TestProposeTaskChange(AIBase):
    def test_nothing_is_written(self):
        """The whole contract in one assertion: the row is re-read after the
        call and must be byte-identical."""
        row = self.mk_task("Send the proposal", status="Open")
        before = json.dumps(
            self.t["tasks"].get_item(Key={"task_id": row["task_id"]})["Item"],
            sort_keys=True, default=str)
        api.tool_propose_task_change(self.ctx(), task_id=row["task_id"],
                                     status="Completed")
        after = json.dumps(
            self.t["tasks"].get_item(Key={"task_id": row["task_id"]})["Item"],
            sort_keys=True, default=str)
        self.assertEqual(before, after)

    def test_the_result_says_it_was_not_applied(self):
        """`applied: False` is stated rather than implied — a model reading
        this result must have no way to conclude the change went through."""
        row = self.mk_task("Send the proposal")
        out = api.tool_propose_task_change(self.ctx(), task_id=row["task_id"],
                                           status="Completed")
        self.assertFalse(out["applied"])
        self.assertTrue(out["requires_user_confirmation"])

    def test_the_current_value_is_returned_beside_the_proposed_one(self):
        """So the app can render "Open -> Completed" rather than an
        unanchored assertion the user cannot check."""
        row = self.mk_task("Send the proposal", status="Open")
        out = api.tool_propose_task_change(self.ctx(), task_id=row["task_id"],
                                           status="Completed")
        self.assertEqual(out["current"]["status"], "Open")
        self.assertEqual(out["proposed"]["status"], "Completed")

    def test_the_confirm_body_is_the_real_routes_shape(self):
        """The confirmation must be a pass-through to PATCH /tasks/{id}, not a
        re-derivation by the client of what the AI meant. `due` (not
        `due_date`) is what that route accepts."""
        row = self.mk_task("Send the proposal")
        out = api.tool_propose_task_change(
            self.ctx(), task_id=row["task_id"],
            status="Completed", due_date=days_out(5), priority="High")
        self.assertEqual(out["confirm"]["method"], "PATCH")
        self.assertEqual(out["confirm"]["path"], f"/tasks/{row['task_id']}")
        self.assertEqual(out["confirm"]["body"],
                         {"status": "Completed", "due": days_out(5),
                          "priority": "High"})

    def test_the_confirm_body_is_accepted_by_the_real_route(self):
        """END TO END, and the point of the previous test: the patch the tool
        hands back is applied by update_task_v2 without translation. A
        proposal the real route rejects would be a Confirm button that 400s."""
        row = self.mk_task("Send the proposal", status="Open")
        out = api.tool_propose_task_change(self.ctx(), task_id=row["task_id"],
                                           status="Completed")
        with mock.patch.object(api, "_notify", return_value=None):
            status, body = parse(call(api.update_task_v2, event(
                method="PATCH", route="/tasks/{task_id}",
                body=out["confirm"]["body"])
                | {"pathParameters": {"task_id": row["task_id"]}}))
        self.assertEqual(status, 200)
        self.assertEqual(body["task"]["status"], "Completed")

    def test_an_invalid_status_is_refused(self):
        row = self.mk_task("Send the proposal")
        with self.assertRaises(api.AIToolError) as cm:
            api.tool_propose_task_change(self.ctx(), task_id=row["task_id"],
                                         status="nearly done")
        self.assertIn("not a task status", str(cm.exception).lower())

    def test_a_relative_date_is_refused_rather_than_guessed(self):
        """"next Friday" must not become a deadline nobody asked for."""
        row = self.mk_task("Send the proposal")
        with self.assertRaises(api.AIToolError):
            api.tool_propose_task_change(self.ctx(), task_id=row["task_id"],
                                         due_date="next Friday")

    def test_an_invalid_priority_is_refused(self):
        row = self.mk_task("Send the proposal")
        with self.assertRaises(api.AIToolError):
            api.tool_propose_task_change(self.ctx(), task_id=row["task_id"],
                                         priority="Urgent")

    def test_an_empty_proposal_is_refused(self):
        row = self.mk_task("Send the proposal")
        with self.assertRaises(api.AIToolError) as cm:
            api.tool_propose_task_change(self.ctx(), task_id=row["task_id"])
        self.assertIn("nothing was proposed", str(cm.exception).lower())

    def test_an_omitted_deadline_never_clears_one(self):
        """An omitted argument and a deliberate "" are indistinguishable by
        the time they reach the tool, so honouring "" as "remove the
        deadline" would let a model that simply left the field out silently
        drop a date the user still needs."""
        row = self.mk_task("Send the proposal", due=days_out(4))
        out = api.tool_propose_task_change(self.ctx(), task_id=row["task_id"],
                                           status="In Progress", due_date="")
        self.assertNotIn("due", out["confirm"]["body"])
        self.assertNotIn("due_date", out["proposed"])

    def test_another_users_task_cannot_be_proposed_against(self):
        row = self.mk_task("Bob's", owner=OTHER, default_key=OTHER_KEY,
                           assignee_user_id=OTHER)
        with self.assertRaises(api.AIToolError):
            api.tool_propose_task_change(self.ctx(), task_id=row["task_id"],
                                         status="Completed")

    def test_an_assignee_may_propose_a_status_change(self):
        """Status is the one field an assignee owns, and the proposal path
        must agree with _authorize_task_patch rather than inventing a rule."""
        row = self.mk_task("Delegated to Alice", owner=OTHER,
                           default_key=OTHER_KEY, assignee_user_id=USER)
        out = api.tool_propose_task_change(self.ctx(), task_id=row["task_id"],
                                           status="Completed")
        self.assertEqual(out["proposed"]["status"], "Completed")

    def test_an_assignee_may_not_propose_a_deadline_change(self):
        """REFUSED HERE, before the user is offered a button whose only
        possible outcome is a 403. Reuses _authorize_task_patch, so this can
        never diverge from what the route enforces."""
        row = self.mk_task("Delegated to Alice", owner=OTHER,
                           default_key=OTHER_KEY, assignee_user_id=USER)
        with self.assertRaises(api.AIToolError) as cm:
            api.tool_propose_task_change(self.ctx(), task_id=row["task_id"],
                                         due_date=days_out(9))
        self.assertIn("cannot make that change", str(cm.exception).lower())

    def test_the_proposal_is_recorded_on_the_context(self):
        row = self.mk_task("Send the proposal")
        ctx = self.ctx()
        api.tool_propose_task_change(ctx, task_id=row["task_id"],
                                     status="Completed")
        self.assertEqual(len(ctx.proposals), 1)
        self.assertEqual(ctx.proposals[0]["task"]["id"], row["task_id"])

    def test_an_identical_proposal_is_not_recorded_twice(self):
        row = self.mk_task("Send the proposal")
        ctx = self.ctx()
        for _ in range(2):
            api.tool_propose_task_change(ctx, task_id=row["task_id"],
                                         status="Completed")
        self.assertEqual(len(ctx.proposals), 1)

    def test_there_is_no_assignee_field_to_propose(self):
        """Reassignment needs a resolved Contact, and a model naming "Priya"
        is exactly the ambiguity the human resolution flow exists to settle.
        A proposal carrying an unresolved name would be unapplyable."""
        schema = next(s for s in api.AI_TOOL_SCHEMAS
                      if s["function"]["name"] == "propose_task_change")
        props = set(schema["function"]["parameters"]["properties"])
        self.assertNotIn("assignee", props)
        self.assertNotIn("assignee_contact_id", props)


# ===========================================================================
# SECURITY — the rules the whole tool layer rests on, on the NEW tools.
# ===========================================================================
class TestNewToolSecurity(AIBase):
    def test_no_schema_exposes_an_identity_parameter(self):
        """The structural guarantee, re-asserted now that two tools were
        added: the model has nowhere to put a user id."""
        banned = set(api._AI_FORBIDDEN_ARGS)
        for schema in api.AI_TOOL_SCHEMAS:
            props = set(schema["function"]["parameters"].get("properties", {}))
            with self.subTest(tool=schema["function"]["name"]):
                self.assertFalse(banned & props)

    def test_the_forbidden_list_covers_the_hand_written_banned_set(self):
        """test_ai_assistant's identity test uses a hand-written literal.
        This pins that _AI_FORBIDDEN_ARGS is a superset of it, so the two
        cannot drift into disagreeing about what identity means."""
        expected = {"user_id", "owner_user_id", "contact_id",
                    "organization_id", "org_id", "tenant_id",
                    "assignee_user_id", "account_id", "email"}
        self.assertTrue(expected <= set(api._AI_FORBIDDEN_ARGS))

    def test_identity_args_are_stripped_from_the_new_tools(self):
        row = self.mk_task("Mine")
        out = api._ai_dispatch(
            self.ctx(), "propose_task_change",
            {"task_id": row["task_id"], "status": "Completed",
             "user_id": OTHER, "owner_user_id": OTHER})
        self.assertNotIn("error", out)
        self.assertFalse(out["applied"])

    def test_identity_args_are_stripped_from_meeting_context(self):
        out = api._ai_dispatch(
            self.ctx(), "get_meeting_context",
            {"task_id": "t-nope", "user_id": OTHER, "organization_id": "org"})
        # Refused because the task does not exist — NOT because it belonged to
        # someone else, which is what an honoured user_id would have produced.
        self.assertIn("error", out)

    def test_every_schema_still_has_an_implementation(self):
        named = {s["function"]["name"] for s in api.AI_TOOL_SCHEMAS}
        self.assertEqual(named, set(api.AI_TOOLS))

    def test_no_tool_name_claims_to_mutate(self):
        """The read-only-name invariant test_ai_assistant asserts, kept true
        by naming the action tool "propose_" — so the name itself cannot
        mislead a model into reporting a change as done."""
        for name in api.AI_TOOLS:
            with self.subTest(tool=name):
                self.assertFalse(name.startswith(
                    ("create_", "update_", "delete_", "complete_", "assign_")))

    def test_the_proposal_tool_cannot_reach_a_write_helper(self):
        """Belt and braces on "writes nothing": the table's write methods are
        replaced with a fuse for the duration of the call."""
        row = self.mk_task("Mine")
        with mock.patch.object(self.t["tasks"], "put_item") as put, \
             mock.patch.object(self.t["tasks"], "update_item") as upd:
            api.tool_propose_task_change(self.ctx(), task_id=row["task_id"],
                                         status="Completed")
        put.assert_not_called()
        upd.assert_not_called()


# ===========================================================================
# STRUCTURED TASK INTELLIGENCE — the schema, and the endpoint.
# ===========================================================================
class TestTaskIntelligenceSchema(unittest.TestCase):
    def test_a_row_naming_an_unknown_task_is_dropped(self):
        """A model that invents "task_99" would otherwise produce a card the
        user can tap and that goes nowhere — the same failure _evidence_ids
        prevents for segment references, one level up."""
        out = ai_schema.coerce_task_intelligence(
            {"rows": [{"task_id": "t-99", "kind": "priority",
                       "reason": "invented", "recommendation": "x"}]},
            valid_task_ids={"t-1"})
        self.assertEqual(out["rows"], [])

    def test_a_row_naming_a_real_task_is_kept(self):
        out = ai_schema.coerce_task_intelligence(
            {"summary": "Busy week.",
             "rows": [{"task_id": "t-1", "kind": "priority",
                       "reason": "due tomorrow", "recommendation": "do it"}]},
            valid_task_ids={"t-1"})
        self.assertEqual(len(out["rows"]), 1)
        self.assertEqual(out["summary"], "Busy week.")

    def test_no_task_state_field_survives_coercion(self):
        """THE SCHEMA ENFORCES "recommendations must not overwrite task
        state": there is no field to overwrite it with. A model that emits
        status/assignee/due_date has them dropped."""
        out = ai_schema.coerce_task_intelligence(
            {"rows": [{"task_id": "t-1", "kind": "stale", "reason": "old",
                       "recommendation": "chase", "status": "Completed",
                       "assignee_name": "Priya", "due_date": "2026-01-01",
                       "priority": "High"}]},
            valid_task_ids={"t-1"})
        row = out["rows"][0]
        for field in ("status", "assignee_name", "due_date", "priority"):
            with self.subTest(field=field):
                self.assertNotIn(field, row)

    def test_an_unknown_kind_falls_back_rather_than_rendering_nowhere(self):
        out = ai_schema.coerce_task_intelligence(
            {"rows": [{"task_id": "t-1", "kind": "vibes", "reason": "r",
                       "recommendation": ""}]},
            valid_task_ids={"t-1"})
        self.assertEqual(out["rows"][0]["kind"], "needs_attention")

    def test_a_row_with_nothing_to_say_is_dropped(self):
        """A card with a heading and no content reads as an assertion the
        model never made."""
        out = ai_schema.coerce_task_intelligence(
            {"rows": [{"task_id": "t-1", "kind": "priority",
                       "reason": "", "recommendation": ""}]},
            valid_task_ids={"t-1"})
        self.assertEqual(out["rows"], [])

    def test_the_same_task_and_kind_is_not_repeated(self):
        out = ai_schema.coerce_task_intelligence(
            {"rows": [
                {"task_id": "t-1", "kind": "priority", "reason": "a",
                 "recommendation": "a"},
                {"task_id": "t-1", "kind": "priority", "reason": "b",
                 "recommendation": "b"}]},
            valid_task_ids={"t-1"})
        self.assertEqual(len(out["rows"]), 1)

    def test_a_task_may_appear_under_two_different_kinds(self):
        out = ai_schema.coerce_task_intelligence(
            {"rows": [
                {"task_id": "t-1", "kind": "priority", "reason": "a",
                 "recommendation": "a"},
                {"task_id": "t-1", "kind": "overdue_risk", "reason": "b",
                 "recommendation": "b"}]},
            valid_task_ids={"t-1"})
        self.assertEqual(len(out["rows"]), 2)

    def test_an_invented_related_task_is_stripped_without_losing_the_row(self):
        """A duplicate claim pointing at an id that is not a real task would
        be a broken link; the JUDGEMENT is still worth showing."""
        out = ai_schema.coerce_task_intelligence(
            {"rows": [{"task_id": "t-1", "kind": "duplicate",
                       "reason": "same work", "recommendation": "merge",
                       "related_task_id": "t-77"}]},
            valid_task_ids={"t-1"})
        self.assertEqual(len(out["rows"]), 1)
        self.assertNotIn("related_task_id", out["rows"][0])

    def test_a_real_related_task_is_kept(self):
        out = ai_schema.coerce_task_intelligence(
            {"rows": [{"task_id": "t-1", "kind": "duplicate",
                       "reason": "same work", "recommendation": "merge",
                       "related_task_id": "t-2"}]},
            valid_task_ids={"t-1", "t-2"})
        self.assertEqual(out["rows"][0]["related_task_id"], "t-2")

    def test_a_task_cannot_be_its_own_duplicate(self):
        out = ai_schema.coerce_task_intelligence(
            {"rows": [{"task_id": "t-1", "kind": "duplicate", "reason": "r",
                       "recommendation": "x", "related_task_id": "t-1"}]},
            valid_task_ids={"t-1"})
        self.assertNotIn("related_task_id", out["rows"][0])

    def test_invalid_evidence_ids_are_dropped(self):
        out = ai_schema.coerce_task_intelligence(
            {"rows": [{"task_id": "t-1", "kind": "priority", "reason": "r",
                       "recommendation": "x",
                       "evidence_segment_ids": ["seg_3", "nonsense", 7]}]},
            valid_task_ids={"t-1"})
        self.assertEqual(out["rows"][0]["evidence_segment_ids"], ["seg_3"])

    def test_the_row_count_is_bounded(self):
        rows = [{"task_id": f"t-{i}", "kind": "needs_attention",
                 "reason": "r", "recommendation": "x"} for i in range(40)]
        out = ai_schema.coerce_task_intelligence(
            {"rows": rows}, valid_task_ids={f"t-{i}" for i in range(40)})
        self.assertLessEqual(len(out["rows"]), ai_schema.MAX_INTEL_ROWS)

    def test_text_is_bounded(self):
        out = ai_schema.coerce_task_intelligence(
            {"summary": "s" * 5000,
             "rows": [{"task_id": "t-1", "kind": "priority",
                       "reason": "r" * 5000,
                       "recommendation": "x" * 5000}]},
            valid_task_ids={"t-1"})
        self.assertLessEqual(len(out["summary"]),
                             ai_schema.MAX_INTEL_SUMMARY_CHARS)
        self.assertLessEqual(len(out["rows"][0]["reason"]),
                             ai_schema.MAX_INTEL_REASON_CHARS)

    def test_hostile_output_never_raises(self):
        """The coercer contract in this module: whatever the model returns
        comes out as the right shape with empty defaults."""
        for junk in (None, "a string", 7, [], {}, {"rows": "not a list"},
                     {"rows": [None, 3, "x", []]}, [1, 2, 3]):
            with self.subTest(junk=junk):
                out = ai_schema.coerce_task_intelligence(
                    junk, valid_task_ids={"t-1"})
                self.assertEqual(out["rows"], [])
                self.assertEqual(out["summary"], "")

    def test_a_bare_list_of_rows_is_accepted(self):
        """A model that skips the envelope and answers with the rows alone is
        a routine event, not a failed generation."""
        out = ai_schema.coerce_task_intelligence(
            [{"task_id": "t-1", "kind": "priority", "reason": "r",
              "recommendation": "x"}],
            valid_task_ids={"t-1"})
        self.assertEqual(len(out["rows"]), 1)

    def test_no_valid_ids_drops_everything(self):
        """The safe direction: no cards rather than cards that go nowhere."""
        out = ai_schema.coerce_task_intelligence(
            {"rows": [{"task_id": "t-1", "kind": "priority", "reason": "r",
                       "recommendation": "x"}]})
        self.assertEqual(out["rows"], [])

    def test_the_kinds_match_what_the_prompt_asks_for(self):
        """A kind the prompt requests but the schema clamps away would be a
        section the model fills and the app never renders."""
        for kind in ai_schema.TASK_INTEL_KINDS:
            with self.subTest(kind=kind):
                self.assertIn(kind, prompts.TASK_INTELLIGENCE_SYSTEM)


class TestTaskIntelligenceEndpoint(AIBase):
    def stub_json(self, payload):
        p = mock.patch.object(groq_client, "complete_json",
                              return_value=payload)
        m = p.start()
        self.addCleanup(p.stop)
        return m

    def test_no_open_tasks_answers_without_calling_groq(self):
        """Paying for a model to say "nothing to do" would be slower AND an
        invitation to invent something."""
        groq = self.stub_json({})
        status, body = parse(call(api.ai_task_intelligence,
                                  event(route="/ai/task-intelligence")))
        self.assertEqual(status, 200)
        self.assertEqual(body["rows"], [])
        self.assertEqual(body["analyzed"], 0)
        groq.assert_not_called()

    def test_rows_are_returned_for_real_tasks(self):
        row = self.mk_task("Send the proposal", due=days_out(1))
        self.stub_json({"summary": "One urgent task.",
                        "rows": [{"task_id": row["task_id"],
                                  "kind": "priority", "reason": "due tomorrow",
                                  "recommendation": "start today"}]})
        status, body = parse(call(api.ai_task_intelligence,
                                  event(route="/ai/task-intelligence")))
        self.assertEqual(status, 200)
        self.assertEqual(len(body["rows"]), 1)
        self.assertEqual(body["rows"][0]["task_id"], row["task_id"])
        self.assertEqual(body["summary"], "One urgent task.")
        self.assertEqual(body["analyzed"], 1)

    def test_a_row_about_another_users_task_is_dropped(self):
        """Even if the model somehow named it: the coercer only accepts ids
        that were actually SENT, and another tenant's task never is."""
        mine = self.mk_task("Mine", due=days_out(1))
        theirs = self.mk_task("Theirs", owner=OTHER, default_key=OTHER_KEY,
                              assignee_user_id=OTHER)
        self.stub_json({"rows": [
            {"task_id": theirs["task_id"], "kind": "priority",
             "reason": "leak", "recommendation": "x"},
            {"task_id": mine["task_id"], "kind": "priority",
             "reason": "ok", "recommendation": "y"}]})
        _, body = parse(call(api.ai_task_intelligence,
                             event(route="/ai/task-intelligence")))
        self.assertEqual([r["task_id"] for r in body["rows"]],
                         [mine["task_id"]])

    def test_completed_tasks_are_not_analyzed(self):
        self.mk_task("Done already", status="Completed")
        groq = self.stub_json({})
        _, body = parse(call(api.ai_task_intelligence,
                             event(route="/ai/task-intelligence")))
        self.assertEqual(body["analyzed"], 0)
        groq.assert_not_called()

    def test_the_prompt_never_receives_another_users_task(self):
        self.mk_task("Mine")
        self.mk_task("Theirs", owner=OTHER, default_key=OTHER_KEY,
                     assignee_user_id=OTHER)
        groq = self.stub_json({"rows": []})
        call(api.ai_task_intelligence, event(route="/ai/task-intelligence"))
        sent = str(groq.call_args)
        self.assertIn("Mine", sent)
        self.assertNotIn("Theirs", sent)

    def test_partial_is_flagged_when_there_are_more_tasks_than_analyzed(self):
        """A partial analysis the UI presents as complete is a claim the data
        does not support — the same rule _tool_result's `truncated` follows."""
        with mock.patch.object(api, "AI_INTEL_MAX_TASKS", 2):
            for i in range(4):
                self.mk_task(f"Task {i}", due=days_out(i + 1))
            self.stub_json({"rows": []})
            _, body = parse(call(api.ai_task_intelligence,
                                 event(route="/ai/task-intelligence")))
        self.assertTrue(body["partial"])
        self.assertEqual(body["analyzed"], 2)

    def test_a_groq_failure_is_a_502_not_a_500(self):
        self.mk_task("Send the proposal")
        with mock.patch.object(
                groq_client, "complete_json",
                side_effect=groq_client.GroqError("boom", retryable=True)):
            status, _ = parse(call(api.ai_task_intelligence,
                                   event(route="/ai/task-intelligence")))
        self.assertEqual(status, 502)

    def test_a_malformed_model_reply_yields_no_rows_rather_than_a_crash(self):
        self.mk_task("Send the proposal")
        self.stub_json({"_raw": "the model wrote prose"})
        status, body = parse(call(api.ai_task_intelligence,
                                  event(route="/ai/task-intelligence")))
        self.assertEqual(status, 200)
        self.assertEqual(body["rows"], [])

    def test_client_supplied_identity_is_ignored(self):
        self.mk_task("Mine", due=days_out(1))
        self.stub_json({"rows": []})
        status, _ = parse(call(api.ai_task_intelligence, event(
            route="/ai/task-intelligence",
            body={"user_id": OTHER, "organization_id": "org-evil"})))
        self.assertEqual(status, 200)

    def test_the_task_view_carries_no_transcript(self):
        """The intelligence prompt judges urgency and duplication from task
        rows; sending transcripts would blow the budget and is not what the
        model is being asked."""
        row = self.mk_task("Send the proposal", evidence="Rahul will send it.")
        view = api._ai_intel_task_view(row, self.ctx())
        self.assertNotIn("transcript", view)
        # The evidence SENTENCE is fine and useful — it is one line the task
        # already stores, not the meeting record.
        self.assertIn("evidence", view)


# ===========================================================================
# THE PROMPT RULES — a rule nobody asserts on silently disappears.
# ===========================================================================
class TestAssistantTaskRules(unittest.TestCase):
    def test_the_rules_are_attached_to_the_assistant_prompt(self):
        system = (prompts.ASSISTANT_SYSTEM + prompts.ASSISTANT_TASK_RULES)
        self.assertIn("CURRENT TASK STATE", system)

    def test_the_task_row_is_declared_authoritative_over_the_transcript(self):
        rules = prompts.ASSISTANT_TASK_RULES
        self.assertIn("TASK ROW IS RIGHT", rules)
        self.assertIn("NEVER state a task's status", rules)

    def test_the_transcript_is_named_as_history(self):
        self.assertIn("HISTORY", prompts.ASSISTANT_TASK_RULES)

    def test_the_model_is_told_it_cannot_apply_a_change(self):
        rules = prompts.ASSISTANT_TASK_RULES
        self.assertIn("cannot change a task yourself", rules)
        self.assertIn("confirm", rules.lower())

    def test_the_model_is_told_not_to_claim_a_change_was_made(self):
        """The rule is about the WORDING of the reply, not just tool choice: a
        model that calls a tool named "propose" still says "I've marked it
        complete" unless told otherwise."""
        self.assertIn("never say you have completed",
                      prompts.ASSISTANT_TASK_RULES.lower())

    def test_reassignment_is_directed_back_to_the_user(self):
        self.assertIn("reassign", prompts.ASSISTANT_TASK_RULES.lower())

    def test_segment_citation_is_requested(self):
        self.assertIn("seg_N", prompts.ASSISTANT_TASK_RULES)

    def test_the_intelligence_prompt_asks_for_no_task_state(self):
        """The cheapest possible place to prevent a model being believed over
        the database: don't ask it for the field."""
        system = prompts.TASK_INTELLIGENCE_SYSTEM
        self.assertIn("Do NOT restate a task's status", system)

    def test_the_intelligence_prompt_forbids_invented_ids(self):
        self.assertIn("Copy task_id EXACTLY", prompts.TASK_INTELLIGENCE_SYSTEM)

    def test_the_intelligence_prompt_allows_an_empty_answer(self):
        """A model that must produce rows will produce padding."""
        self.assertIn("empty rows array", prompts.TASK_INTELLIGENCE_SYSTEM)


# ===========================================================================
# THE AGENT LOOP AND THE ENDPOINT — the additive response keys.
# ===========================================================================
class TestChatResponseShape(AIBase):
    def run_chat(self, script, message="What's overdue?"):
        fake = FakeModel(script)
        with mock.patch.object(api.groq_client, "complete_with_tools", fake):
            return parse(call(api.ai_chat, event(body={"message": message})))

    def test_sources_and_proposals_are_always_present(self):
        """ADDITIVE keys: an answer with neither still carries both as empty
        arrays, so no client has to tell "no sources" apart from "old
        backend"."""
        status, body = self.run_chat([{"content": "Nothing is overdue."}])
        self.assertEqual(status, 200)
        self.assertEqual(body["sources"], [])
        self.assertEqual(body["proposals"], [])

    def test_the_existing_keys_are_unchanged(self):
        status, body = self.run_chat([{"content": "Nothing is overdue."}])
        self.assertEqual(status, 200)
        self.assertEqual(body["reply"], "Nothing is overdue.")
        self.assertIn("tools_used", body)

    def test_a_proposal_reaches_the_response(self):
        row = self.mk_task("Send the proposal")
        status, body = self.run_chat([
            {"calls": [("propose_task_change",
                        {"task_id": row["task_id"], "status": "Completed",
                         "reason": "you said it is done"})]},
            {"content": "I can mark that complete — confirm and I'll apply it."},
        ], message="mark the proposal task done")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["proposals"]), 1)
        prop = body["proposals"][0]
        self.assertFalse(prop["applied"])
        self.assertEqual(prop["proposed"]["status"], "Completed")
        self.assertEqual(prop["confirm"]["body"], {"status": "Completed"})

    def test_the_task_was_not_mutated_by_the_conversation(self):
        """The end-to-end version of the propose-never-apply rule."""
        row = self.mk_task("Send the proposal", status="Open")
        self.run_chat([
            {"calls": [("propose_task_change",
                        {"task_id": row["task_id"], "status": "Completed"})]},
            {"content": "Confirm and I'll apply it."},
        ])
        stored = self.t["tasks"].get_item(
            Key={"task_id": row["task_id"]})["Item"]
        self.assertEqual(stored["status"], "Open")

    def test_a_failed_proposal_does_not_break_the_conversation(self):
        status, body = self.run_chat([
            {"calls": [("propose_task_change",
                        {"task_id": "t-nope", "status": "Completed"})]},
            {"content": "I could not find that task."},
        ])
        self.assertEqual(status, 200)
        self.assertEqual(body["proposals"], [])

    def test_the_task_rules_are_in_the_system_turn(self):
        fake = FakeModel([{"content": "ok"}])
        with mock.patch.object(api.groq_client, "complete_with_tools", fake):
            call(api.ai_chat, event(body={"message": "hi"}))
        system = fake.seen[0]["messages"][0]["content"]
        self.assertIn("CURRENT TASK STATE", system)
        self.assertIn("cannot change a task yourself", system)

    def test_the_new_tools_are_advertised_to_the_model(self):
        fake = FakeModel([{"content": "ok"}])
        with mock.patch.object(api.groq_client, "complete_with_tools", fake):
            call(api.ai_chat, event(body={"message": "hi"}))
        offered = {t["function"]["name"] for t in fake.seen[0]["tools"]}
        self.assertIn("get_meeting_context", offered)
        self.assertIn("propose_task_change", offered)


class TestSuggestions(AIBase):
    def test_the_workspace_catalogue_is_served(self):
        status, body = parse(call(api.ai_suggestions,
                                  event(method="GET", route="/ai/suggestions")))
        self.assertEqual(status, 200)
        self.assertEqual(body["suggestions"], api.AI_SUGGESTIONS)

    def test_a_task_id_narrows_the_catalogue(self):
        status, body = parse(call(api.ai_suggestions, event(
            method="GET", route="/ai/suggestions", qs={"task_id": "t-1"})))
        self.assertEqual(status, 200)
        self.assertEqual(body["suggestions"], api.AI_TASK_SUGGESTIONS)

    def test_the_dashboard_chips_are_all_in_the_catalogue(self):
        """The app's fallback chips (task-action-center.tsx AI_PROMPTS) are
        sent VERBATIM as the assistant's first message, so each must be a
        question this catalogue would itself suggest."""
        for chip in ("What needs my attention?", "What's overdue?",
                     "What should I work on this week?"):
            with self.subTest(chip=chip):
                self.assertIn(chip, api.AI_SUGGESTIONS)


class TestRoutes(unittest.TestCase):
    def test_the_task_intelligence_route_is_registered(self):
        self.assertIs(api._ROUTES[("POST", "/ai/task-intelligence")],
                      api.ai_task_intelligence)

    def test_the_existing_ai_routes_are_untouched(self):
        self.assertIs(api._ROUTES[("POST", "/ai/chat")], api.ai_chat)
        self.assertIs(api._ROUTES[("GET", "/ai/suggestions")],
                      api.ai_suggestions)


# ===========================================================================
# EVIDENCE HELPERS — the shape the app deep-links on.
# ===========================================================================
class TestEvidenceShape(unittest.TestCase):
    def test_evidence_from_segments_tolerates_nothing(self):
        self.assertEqual(pageindex.evidence_from_segments([]), [])
        self.assertEqual(pageindex.evidence_from_segments(None or []), [])

    def test_evidence_skips_segments_with_no_id(self):
        segs = [{"speaker": "0", "start": 1.0, "end": 2.0},
                {"id": "seg_1", "speaker": "0", "start": 2.0, "end": 3.0}]
        out = pageindex.evidence_from_segments(segs)
        self.assertEqual([e["segment_id"] for e in out], ["seg_1"])

    def test_note_sources_never_takes_an_id_from_the_model(self):
        """_ai_note_sources is the ONLY writer of ctx.sources and it writes
        what RETRIEVAL returned. A row with no segment_id — which is what a
        hallucination reduced to shape looks like — is dropped."""
        ctx = api.AIContext(user_id=USER)
        api._ai_note_sources(ctx, KEY, [{"segment_id": ""},
                                        {"no_segment": "at all"},
                                        {"segment_id": "seg_4",
                                         "start_time": 1.0, "end_time": 2.0}])
        self.assertEqual([s["segment_id"] for s in ctx.sources], ["seg_4"])
        self.assertEqual(ctx.sources[0]["meeting_id"], KEY)


if __name__ == "__main__":
    unittest.main(verbosity=2)
