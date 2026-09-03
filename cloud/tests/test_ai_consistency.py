#!/usr/bin/env python3
# =============================================================
# test_ai_consistency.py — GET /tasks and the AI tools must agree.
#
# THE BUG THIS FILE EXISTS FOR. A user's dashboard listed three upcoming
# tasks — Sep 3, Sep 4, Sep 11 — while the assistant, asked in the same
# session, answered "I found no upcoming tasks for the next week."
#
# TWO INDEPENDENT CAUSES, both now covered here:
#
#   1. THE WRONG DATE FIELD. A task carries `due_date` (what was SAID:
#      "Wednesday", "next week") and `due_date_normalized` (that phrase
#      resolved to a calendar day). list_all_tasks._keep compares the
#      normalized one; the AI tools compared the RAW one and dropped every
#      spoken deadline for failing a YYYY-MM-DD regex. AI-extracted tasks are
#      exactly the ones with spoken deadlines, so the assistant was blind to
#      the majority of a meeting-driven backlog.
#
#   2. THE WRONG VISIBILITY RULE. The dashboard shows creator-OR-assignee.
#      The AI tools narrowed to _assigned_to_me, which EXCLUDES a task the
#      caller created and delegated. So a task on screen was unreachable to
#      the assistant — and the tools even disagreed with each other, because
#      search_my_tasks never applied the narrowing at all.
#
# THE INVARIANT THESE TESTS DEFEND, stated once: for one authenticated user,
# GET /tasks and the AI tools operate over the SAME accessible task universe.
# A tool may narrow it further only when the caller explicitly asks. Anything
# else is the assistant answering a different question than the one the user
# is looking at.
#
# HOW THE ASSERTIONS ARE WRITTEN. Wherever possible a test compares the two
# paths against EACH OTHER rather than against a hardcoded list — a test that
# pins "3 tasks" passes when both sides break the same way, which is precisely
# the failure that shipped.
#
# Run:  python tests/test_ai_consistency.py
# =============================================================
import datetime as real_datetime
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

# ORDER MATTERS — see test_ai_task_dashboard.py. This binds the same `api`
# module object and Key class every other suite uses.
from test_ai_assistant import (  # noqa: E402
    AIBase, KEY, OTHER, OTHER_KEY, USER, call, event, parse, api,
)
import groq_client  # noqa: E402
import prompts  # noqa: E402

# A fixed "now", so Sep 3 / Sep 4 / Sep 11 mean what they meant in the report.
# Sep 2 was the day the inconsistency was observed.
TODAY = real_datetime.datetime(2026, 9, 2, 10, 0, 0,
                               tzinfo=real_datetime.timezone.utc)


class FrozenDatetime(real_datetime.datetime):
    """datetime with now() pinned. Subclassed rather than MagicMock'd because
    the Lambda calls datetime.now(timezone.utc).date() and does arithmetic on
    the result — a mock would need every one of those stubbed."""

    @classmethod
    def now(cls, tz=None):
        return TODAY if tz else TODAY.replace(tzinfo=None)


class ConsistencyBase(AIBase):
    """AIBase with a frozen clock and both read paths one call away."""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(api, "datetime", FrozenDatetime)
        p.start()
        self.addCleanup(p.stop)
        # A contact in the caller's address book LINKED to another account:
        # what makes a delegated task have a real assignee.
        self.bob = {"contact_id": "c-bob", "owner_user_id": USER,
                    "name": "Bob", "email": "bob@corp.com",
                    "email_lc": "bob@corp.com", "minutex_user_id": OTHER,
                    "created_at": "2026-01-01T00:00:00Z"}
        self.t["contacts"].put_item(Item=self.bob)

    # -- fixtures ---------------------------------------------------------
    def spoken_task(self, title, phrase, day, **kw):
        """An AI-EXTRACTED task: the deadline was spoken, so `due_date` holds
        the phrase and `due_date_normalized` holds the resolved day. This is
        the shape the whole bug turned on."""
        row = api._new_task_row(
            USER, title, recording_key=KEY, due=phrase, due_normalized=day,
            source_type=api.TASK_SOURCE_AI, **kw)
        return api._write_task(row)

    def dated_task(self, title, day, **kw):
        """A task with a real ISO deadline — what the date picker writes."""
        row = api._new_task_row(USER, title, recording_key=KEY,
                                due=day, due_normalized=day, **kw)
        return api._write_task(row)

    def delegated_task(self, title, day):
        """Created by the caller, assigned to Bob. On the caller's dashboard,
        but NOT their own commitment."""
        return self.dated_task(title, day, assignee_contact=self.bob)

    # -- the two read paths ----------------------------------------------
    def dashboard(self, **qs):
        ev = event(method="GET", route="/tasks")
        ev["queryStringParameters"] = {k: str(v) for k, v in qs.items()}
        status, body = parse(call(api.list_all_tasks, ev))
        self.assertEqual(status, 200)
        return sorted(t["task"] for t in body["tasks"])

    def ai_titles(self, result):
        return sorted(t["title"] for t in result["tasks"])


# ===========================================================================
# THE REPORTED BUG, reproduced from the screenshot and pinned fixed.
# ===========================================================================
class TestTheReportedInconsistency(ConsistencyBase):
    def seed_the_screenshot(self):
        """The exact tasks from the report, with the deadlines spoken in a
        meeting the way an AI-extracted task actually stores them."""
        self.spoken_task("Provide update on payment integration",
                         "Wednesday", "2026-09-03")
        self.spoken_task("Send Salesforce Key", "Thursday", "2026-09-04")
        self.spoken_task("Test the new API", "next week", "2026-09-11")

    def test_the_dashboard_sees_them(self):
        """The control. If this ever fails the fixture is wrong, not the AI."""
        self.seed_the_screenshot()
        self.assertEqual(
            self.dashboard(due_before="2026-09-30"),
            ["Provide update on payment integration", "Send Salesforce Key",
             "Test the new API"])

    def test_the_ai_now_sees_them_too(self):
        """THE BUG. This returned zero tasks: every deadline was a spoken
        phrase, and the tool compared the raw phrase against a date regex."""
        self.seed_the_screenshot()
        got = self.ai_titles(api.tool_get_upcoming_tasks(self.ctx(), days=14))
        self.assertEqual(
            got,
            ["Provide update on payment integration", "Send Salesforce Key",
             "Test the new API"])

    def test_the_two_paths_agree_on_the_same_window(self):
        """Compared against EACH OTHER rather than a hardcoded list: a test
        pinning a count passes when both sides break together."""
        self.seed_the_screenshot()
        self.assertEqual(
            self.dashboard(due_before="2026-09-30"),
            self.ai_titles(api.tool_get_upcoming_tasks(
                self.ctx(), due_before="2026-09-30")))

    def test_a_seven_day_window_excludes_only_what_falls_outside_it(self):
        """Sep 3 and Sep 4 are inside a week of Sep 2; Sep 11 is not. The
        NARROWING must come from the date, never from the date's format."""
        self.seed_the_screenshot()
        got = self.ai_titles(api.tool_get_upcoming_tasks(self.ctx(), days=7))
        self.assertEqual(got, ["Provide update on payment integration",
                               "Send Salesforce Key"])

    def test_the_model_is_shown_a_comparable_date(self):
        """Returning the task is not enough: a model handed due_date
        "Wednesday" cannot answer "what is due this week" about it, and will
        either ignore it or invent a day."""
        self.seed_the_screenshot()
        out = api.tool_get_upcoming_tasks(self.ctx(), days=14)
        for view in out["tasks"]:
            with self.subTest(task=view["title"]):
                self.assertRegex(view["due_date"], r"^\d{4}-\d{2}-\d{2}$")

    def test_the_spoken_phrase_is_still_available_to_the_model(self):
        """The user said "Wednesday" and may say it back. Keeping the phrase
        beside the resolved day stops the assistant's wording drifting from
        the deadline the user actually set."""
        self.seed_the_screenshot()
        out = api.tool_get_upcoming_tasks(self.ctx(), days=14)
        by_title = {v["title"]: v for v in out["tasks"]}
        self.assertEqual(by_title["Send Salesforce Key"]["due_date_spoken"],
                         "Thursday")
        self.assertEqual(by_title["Send Salesforce Key"]["due_date"],
                         "2026-09-04")


# ===========================================================================
# DATE CLASSIFICATION — do not assume the arithmetic is right.
# ===========================================================================
class TestDateClassification(ConsistencyBase):
    def test_today_is_upcoming_not_overdue(self):
        """A task due TODAY is still due. Excluding it from upcoming while
        also not being overdue would make it invisible to both tools."""
        self.dated_task("Due today", "2026-09-02")
        self.assertEqual(
            self.ai_titles(api.tool_get_upcoming_tasks(self.ctx(), days=7)),
            ["Due today"])
        self.assertEqual(
            api.tool_get_overdue_tasks(self.ctx())["count"], 0)

    def test_yesterday_is_overdue_not_upcoming(self):
        self.dated_task("Due yesterday", "2026-09-01")
        self.assertEqual(
            self.ai_titles(api.tool_get_overdue_tasks(self.ctx())),
            ["Due yesterday"])
        self.assertEqual(
            api.tool_get_upcoming_tasks(self.ctx(), days=7)["count"], 0)

    def test_the_horizon_day_is_included(self):
        """days=7 from Sep 2 must include Sep 9. An exclusive bound would
        silently drop the last day of every window the user asks about."""
        self.dated_task("On the horizon", "2026-09-09")
        self.assertEqual(
            self.ai_titles(api.tool_get_upcoming_tasks(self.ctx(), days=7)),
            ["On the horizon"])

    def test_one_day_past_the_horizon_is_excluded(self):
        self.dated_task("Past the horizon", "2026-09-10")
        self.assertEqual(
            api.tool_get_upcoming_tasks(self.ctx(), days=7)["count"], 0)

    def test_an_unplaceable_phrase_is_flagged_not_silently_undated(self):
        """"end of Q3" normalizes to nothing. It must not be reported as a
        task with no deadline — the user did set one, it just cannot be
        plotted."""
        self.spoken_task("Finish the audit", "end of Q3", "")
        out = api.tool_get_my_tasks(self.ctx())
        view = out["tasks"][0]
        self.assertEqual(view["due_date"], "")
        self.assertTrue(view["due_date_unplaceable"])
        self.assertEqual(view["due_date_spoken"], "end of Q3")

    def test_an_unplaceable_phrase_is_in_neither_date_window(self):
        """Correct on both sides: it cannot be compared, so it is neither
        upcoming nor overdue rather than being guessed into one."""
        self.spoken_task("Finish the audit", "end of Q3", "")
        self.assertEqual(
            api.tool_get_upcoming_tasks(self.ctx(), days=365)["count"], 0)
        self.assertEqual(api.tool_get_overdue_tasks(self.ctx())["count"], 0)

    def test_a_full_iso_timestamp_compares_on_its_date_part(self):
        self.dated_task("Timestamped", "2026-09-04T17:00:00Z")
        self.assertEqual(
            self.ai_titles(api.tool_get_upcoming_tasks(self.ctx(), days=7)),
            ["Timestamped"])

    def test_the_ai_sorts_by_the_resolved_day_not_the_phrase(self):
        """Sorting raw strings put every spoken phrase in front of every real
        date ("Wednesday" < "2026-..." is false, but "next week" vs
        "2026-09-11" is arbitrary) — a silently wrong order, which is worse
        than a visible error."""
        self.spoken_task("Third", "next week", "2026-09-11")
        self.spoken_task("First", "Wednesday", "2026-09-03")
        self.dated_task("Second", "2026-09-04")
        out = api.tool_get_upcoming_tasks(self.ctx(), days=14)
        self.assertEqual([v["title"] for v in out["tasks"]],
                         ["First", "Second", "Third"])

    def test_undated_tasks_sort_last(self):
        self.spoken_task("No firm date", "sometime", "")
        self.dated_task("Has a date", "2026-09-04")
        out = api.tool_get_my_tasks(self.ctx())
        self.assertEqual([v["title"] for v in out["tasks"]],
                         ["Has a date", "No firm date"])

    def test_overdue_uses_the_resolved_day_too(self):
        """Same field bug, other tool. get_overdue_tasks already passed both
        fields to _is_overdue, so this pins that it stays that way."""
        self.spoken_task("Late", "last Monday", "2026-08-24")
        self.assertEqual(
            self.ai_titles(api.tool_get_overdue_tasks(self.ctx())), ["Late"])

    def test_overdue_agrees_with_the_dashboard_filter(self):
        self.spoken_task("Late spoken", "last Monday", "2026-08-24")
        self.dated_task("Late dated", "2026-08-25")
        self.dated_task("Not late", "2026-09-20")
        self.assertEqual(self.dashboard(overdue="true"),
                         self.ai_titles(api.tool_get_overdue_tasks(self.ctx())))


# ===========================================================================
# VISIBILITY — the second cause. Delegated work.
# ===========================================================================
class TestDelegatedTaskVisibility(ConsistencyBase):
    def test_a_delegated_task_is_on_the_dashboard(self):
        """The control: the caller created it, so they can see it."""
        self.delegated_task("Bob's review", "2026-09-04")
        self.assertEqual(self.dashboard(), ["Bob's review"])

    def test_a_delegated_task_reaches_get_upcoming_tasks(self):
        """THE SECOND HALF OF THE BUG. _assigned_to_me is False for a task
        assigned to someone else, so this tool could never return it — not
        even while the user was looking at it on their dashboard."""
        self.delegated_task("Bob's review", "2026-09-04")
        self.assertEqual(
            self.ai_titles(api.tool_get_upcoming_tasks(self.ctx(), days=7)),
            ["Bob's review"])

    def test_a_delegated_overdue_task_reaches_get_overdue_tasks(self):
        self.delegated_task("Bob's late review", "2026-08-20")
        self.assertEqual(
            self.ai_titles(api.tool_get_overdue_tasks(self.ctx())),
            ["Bob's late review"])

    def test_a_delegated_task_is_searchable(self):
        self.delegated_task("Bob's invoice review", "2026-09-04")
        self.assertEqual(
            self.ai_titles(api.tool_search_my_tasks(self.ctx(),
                                                    query="invoice")),
            ["Bob's invoice review"])

    def test_every_date_tool_agrees_with_the_dashboard_on_the_same_set(self):
        """THE INVARIANT, over a mixed backlog: same user, same window, same
        tasks. Compared path-to-path, not to a literal."""
        self.spoken_task("Mine spoken", "Wednesday", "2026-09-03")
        self.dated_task("Mine dated", "2026-09-04")
        self.delegated_task("Delegated", "2026-09-05")
        self.assertEqual(
            self.dashboard(due_before="2026-09-09"),
            self.ai_titles(api.tool_get_upcoming_tasks(
                self.ctx(), due_before="2026-09-09")))

    def test_the_tools_agree_with_each_other(self):
        """search_my_tasks never applied the _assigned_to_me narrowing that
        get_my_tasks did, so the two tools disagreed about the same task —
        the assistant contradicting itself within one conversation."""
        self.delegated_task("Delegated review", "2026-09-04")
        searched = self.ai_titles(
            api.tool_search_my_tasks(self.ctx(), query="Delegated"))
        upcoming = self.ai_titles(
            api.tool_get_upcoming_tasks(self.ctx(), days=7))
        self.assertEqual(searched, upcoming)

    def test_my_commitments_can_still_be_asked_for_specifically(self):
        """The narrowing did not disappear — it became OPT-IN. "What do I owe
        myself" is a real question and still answerable."""
        self.dated_task("Mine", "2026-09-04")
        self.delegated_task("Bob's", "2026-09-05")
        self.assertEqual(
            self.ai_titles(api.tool_get_upcoming_tasks(
                self.ctx(), days=7, include_assigned_to_others=False)),
            ["Mine"])
        self.assertEqual(
            self.ai_titles(api.tool_get_overdue_tasks(
                self.ctx(), include_assigned_to_others=False)), [])

    def test_get_my_tasks_still_defaults_to_my_own_commitments(self):
        """"My tasks" means my commitments — that default is CORRECT and is
        not what was broken. Pinned so the fix above does not over-reach."""
        self.dated_task("Mine", "2026-09-04")
        self.delegated_task("Bob's", "2026-09-05")
        self.assertEqual(self.ai_titles(api.tool_get_my_tasks(self.ctx())),
                         ["Mine"])
        self.assertEqual(
            self.ai_titles(api.tool_get_my_tasks(
                self.ctx(), include_assigned_to_others=True)),
            ["Bob's", "Mine"])

    def test_a_task_assigned_to_me_by_someone_else_reaches_every_tool(self):
        """The other direction of delegation, and the case the previous audit
        fixed in _ai_owner_tasks. Re-checked per TOOL, since a tool can undo
        that fix with its own filter."""
        row = api._new_task_row(OTHER, "Assigned to Alice",
                                recording_key=OTHER_KEY, due="2026-09-04",
                                due_normalized="2026-09-04")
        row["assignee_user_id"] = USER
        api._write_task(row)
        ctx = self.ctx()
        self.assertEqual(
            self.ai_titles(api.tool_get_upcoming_tasks(ctx, days=7)),
            ["Assigned to Alice"])
        self.assertEqual(self.ai_titles(api.tool_get_my_tasks(ctx)),
                         ["Assigned to Alice"])
        self.assertEqual(
            self.ai_titles(api.tool_search_my_tasks(ctx, query="Alice")),
            ["Assigned to Alice"])

    def test_status_filtering_matches_the_dashboard(self):
        self.dated_task("Open one", "2026-09-04")
        self.dated_task("Done one", "2026-09-05",
                        status=api.TASK_STATUS_COMPLETED)
        self.assertEqual(self.dashboard(status="Open"), ["Open one"])
        self.assertEqual(self.ai_titles(api.tool_get_my_tasks(self.ctx())),
                         ["Open one"])

    def test_a_completed_task_is_in_no_date_window(self):
        self.dated_task("Done", "2026-09-04",
                        status=api.TASK_STATUS_COMPLETED)
        self.assertEqual(
            api.tool_get_upcoming_tasks(self.ctx(), days=7)["count"], 0)
        self.dated_task("Done late", "2026-08-04",
                        status=api.TASK_STATUS_COMPLETED)
        self.assertEqual(api.tool_get_overdue_tasks(self.ctx())["count"], 0)


# ===========================================================================
# ISOLATION — the fix must not have widened access.
# ===========================================================================
class TestIsolationSurvivesTheFix(ConsistencyBase):
    def setUp(self):
        super().setUp()
        # A task that is neither created by nor assigned to the caller.
        row = api._new_task_row(OTHER, "Bob's private work",
                                recording_key=OTHER_KEY, due="2026-09-04",
                                due_normalized="2026-09-04")
        row["assignee_user_id"] = OTHER
        self.stranger = api._write_task(row)

    def test_the_dashboard_cannot_see_it(self):
        self.assertEqual(self.dashboard(), [])

    def test_no_ai_tool_can_see_it(self):
        ctx = self.ctx()
        for name, out in (
            ("get_my_tasks", api.tool_get_my_tasks(ctx)),
            ("get_my_tasks(all)",
             api.tool_get_my_tasks(ctx, include_assigned_to_others=True)),
            ("get_upcoming", api.tool_get_upcoming_tasks(ctx, days=30)),
            ("get_overdue", api.tool_get_overdue_tasks(ctx)),
            ("search", api.tool_search_my_tasks(ctx, query="private")),
        ):
            with self.subTest(tool=name):
                self.assertEqual(out["count"], 0)

    def test_it_is_not_found_by_id(self):
        with self.assertRaises(api.AIToolError):
            api.tool_get_task(self.ctx(), task_id=self.stranger["task_id"])

    def test_task_intelligence_cannot_see_it(self):
        with mock.patch.object(groq_client, "complete_json",
                               return_value={"rows": []}) as groq:
            status, _ = parse(call(api.ai_task_intelligence,
                                   event(route="/ai/task-intelligence")))
        self.assertEqual(status, 200)
        # Not merely absent from the OUTPUT — never sent to the model at all.
        groq.assert_not_called()

    def test_include_assigned_to_others_does_not_widen_the_tenant(self):
        """The new parameter widens SCOPE within what the caller can see. It
        must not be mistakable for a way out of the tenant."""
        out = api._ai_dispatch(self.ctx(), "get_upcoming_tasks",
                               {"days": 30, "include_assigned_to_others": True})
        self.assertEqual(out["count"], 0)

    def test_identity_arguments_are_still_stripped(self):
        """The tools gained parameters, so the injection guard is re-asserted
        against the new signatures.

        Two tasks, so every tool has something of the CALLER's to return: one
        future (upcoming) and one past (overdue). What each assertion checks
        is that the answer is the caller's own work and never Bob's private
        task — and never an error the model could narrate as someone else's
        absence of data.
        """
        self.dated_task("Mine future", "2026-09-04")
        self.dated_task("Mine past", "2026-08-04")
        for tool, args, expected in (
            ("get_upcoming_tasks", {"days": 30}, ["Mine future"]),
            ("get_overdue_tasks", {}, ["Mine past"]),
            ("get_my_tasks", {}, ["Mine future", "Mine past"]),
            ("search_my_tasks", {"query": "Mine"},
             ["Mine future", "Mine past"]),
        ):
            with self.subTest(tool=tool):
                out = api._ai_dispatch(self.ctx(), tool, dict(args, **{
                    "user_id": OTHER, "owner_user_id": OTHER,
                    "assignee_user_id": OTHER, "organization_id": "org-evil",
                    "contact_id": "c-bob", "email": "bob@corp.com"}))
                self.assertNotIn("error", out)
                self.assertEqual(sorted(t["title"] for t in out["tasks"]),
                                 sorted(expected))

    def test_no_schema_exposes_an_identity_parameter(self):
        banned = set(api._AI_FORBIDDEN_ARGS)
        for schema in api.AI_TOOL_SCHEMAS:
            props = set(schema["function"]["parameters"].get("properties", {}))
            with self.subTest(tool=schema["function"]["name"]):
                self.assertFalse(banned & props)

    def test_every_new_parameter_is_on_the_dispatcher_allowlist(self):
        """A schema parameter the allowlist drops is a silently ignored
        argument: the model asks for a wider scope, is told nothing, and
        reports the narrow answer as complete."""
        for schema in api.AI_TOOL_SCHEMAS:
            name = schema["function"]["name"]
            props = set(schema["function"]["parameters"].get("properties", {}))
            allowed = set(api.AI_TOOLS[name][1])
            with self.subTest(tool=name):
                self.assertTrue(props <= allowed,
                                f"{name}: schema offers {props - allowed} "
                                f"but the dispatcher drops it")


# ===========================================================================
# TASK INTELLIGENCE — it must succeed whenever the task list does.
# ===========================================================================
class TestIntelligenceSucceedsWhenListingDoes(ConsistencyBase):
    def stub_json(self, payload):
        p = mock.patch.object(groq_client, "complete_json",
                              return_value=payload)
        m = p.start()
        self.addCleanup(p.stop)
        return m

    def test_it_analyzes_the_same_tasks_the_dashboard_lists(self):
        """THE CONSISTENCY RULE applied to the third path. Every task the
        dashboard shows must be offered to the analysis, including delegated
        ones — the endpoint filtered on visibility only after this pass."""
        self.spoken_task("Mine spoken", "Wednesday", "2026-09-03")
        self.dated_task("Mine dated", "2026-09-04")
        self.delegated_task("Delegated", "2026-09-05")
        groq = self.stub_json({"rows": []})
        status, body = parse(call(api.ai_task_intelligence,
                                  event(route="/ai/task-intelligence")))
        self.assertEqual(status, 200)
        self.assertEqual(body["analyzed"], len(self.dashboard()))
        sent = str(groq.call_args)
        for title in ("Mine spoken", "Mine dated", "Delegated"):
            with self.subTest(title=title):
                self.assertIn(title, sent)

    def test_the_model_receives_resolved_dates(self):
        """A model asked to judge urgency cannot compare "Wednesday" to
        today. It was being sent the raw phrase."""
        self.spoken_task("Spoken deadline", "Wednesday", "2026-09-03")
        groq = self.stub_json({"rows": []})
        call(api.ai_task_intelligence, event(route="/ai/task-intelligence"))
        payload = json.loads(str(groq.call_args[0][1]).split("JSON:\n\n", 1)[1]
                             .rsplit("\n\nAnalyse", 1)[0])
        self.assertEqual(payload["tasks"][0]["due_date"], "2026-09-03")

    def test_a_large_backlog_is_trimmed_to_the_token_budget(self):
        """WHY THIS MATTERS: 60 fat task views measured ~15.6k tokens, over
        the 12k TPM a default-configured account has. The resulting 429 is
        not retryable inside the request deadline, and it surfaced to the
        user as "Could not analyze your tasks" — indistinguishable from the
        missing-route failure."""
        for i in range(60):
            api._write_task(api._new_task_row(
                USER, f"Task {i} with a long descriptive title about work",
                recording_key=KEY, due="2026-09-20",
                due_normalized="2026-09-20",
                description="d" * 300,
                ai_evidence="e" * 200, source_type=api.TASK_SOURCE_AI))
        groq = self.stub_json({"rows": []})
        status, body = parse(call(api.ai_task_intelligence,
                                  event(route="/ai/task-intelligence")))
        self.assertEqual(status, 200)
        sent_chars = len(str(groq.call_args[0][1]))
        budget = groq_client.single_pass_budget_chars(
            prompts.TASK_INTELLIGENCE_SYSTEM)
        self.assertLessEqual(sent_chars, budget)
        # And it says the analysis was partial rather than implying it saw all.
        self.assertTrue(body["partial"])
        self.assertGreater(body["analyzed"], 0)

    def test_a_normal_backlog_is_not_trimmed(self):
        """The guard must not silently shrink an ordinary request."""
        for i in range(8):
            self.dated_task(f"Task {i}", "2026-09-20")
        self.stub_json({"rows": []})
        _, body = parse(call(api.ai_task_intelligence,
                             event(route="/ai/task-intelligence")))
        self.assertEqual(body["analyzed"], 8)
        self.assertFalse(body["partial"])

    def test_rows_survive_for_real_tasks(self):
        """End to end: listing works, so analysis works and returns rows the
        app can key by task id."""
        row = self.spoken_task("Send Salesforce Key", "Thursday",
                               "2026-09-04")
        self.stub_json({"summary": "One task due Thursday.",
                        "rows": [{"task_id": row["task_id"],
                                  "kind": "priority",
                                  "reason": "due 2026-09-04",
                                  "recommendation": "send it today"}]})
        status, body = parse(call(api.ai_task_intelligence,
                                  event(route="/ai/task-intelligence")))
        self.assertEqual(status, 200)
        self.assertEqual([r["task_id"] for r in body["rows"]],
                         [row["task_id"]])

    def test_the_route_is_registered_in_the_lambda(self):
        """Half of what was wrong: the handler was in _ROUTES. The other half
        was API Gateway, which no offline test can assert — see
        scripts/48_deploy_ai_task_dashboard.sh, which now verifies the route
        exists after creating it."""
        self.assertIs(api._ROUTES[("POST", "/ai/task-intelligence")],
                      api.ai_task_intelligence)


# ===========================================================================
# THE SHARED HELPER — one date rule, used by both paths.
# ===========================================================================
class TestDueDayHelper(unittest.TestCase):
    def test_the_normalized_day_wins(self):
        self.assertEqual(
            api._ai_due_day({"due_date": "Wednesday",
                             "due_date_normalized": "2026-09-03"}),
            "2026-09-03")

    def test_the_raw_value_is_the_fallback(self):
        """Rows written before normalization existed, and manual tasks the
        picker already stores as a real day."""
        self.assertEqual(api._ai_due_day({"due_date": "2026-09-04"}),
                         "2026-09-04")

    def test_an_unplaceable_phrase_yields_nothing(self):
        self.assertEqual(
            api._ai_due_day({"due_date": "end of Q3",
                             "due_date_normalized": ""}), "")

    def test_a_timestamp_is_cut_to_its_day(self):
        self.assertEqual(
            api._ai_due_day({"due_date": "2026-09-04T17:00:00Z"}),
            "2026-09-04")

    def test_junk_never_raises(self):
        for row in (None, {}, {"due_date": None},
                    {"due_date": 7}, {"due_date_normalized": []},
                    {"due_date": "not a date"}):
            with self.subTest(row=row):
                self.assertEqual(api._ai_due_day(row), "")

    def test_it_matches_the_dashboards_own_fallback_order(self):
        """The rule is duplicated in list_all_tasks._keep by necessity (it is
        a closure over query params). Pinning the ORDER here is what stops
        the two drifting apart again."""
        source = Path(api.__file__).read_text(encoding="utf-8")
        keep = source[source.index("    def _keep(row):"):]
        keep = keep[:keep.index("    out, last_key = [], None")]
        self.assertIn('row.get("due_date_normalized")', keep)
        self.assertIn('or str(row.get("due_date") or "")', keep)


if __name__ == "__main__":
    unittest.main(verbosity=2)
