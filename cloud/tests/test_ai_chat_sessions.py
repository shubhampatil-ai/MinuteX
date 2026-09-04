#!/usr/bin/env python3
# =============================================================
# test_ai_chat_sessions.py — persistent, resumable workspace AI conversations.
#
# OFFLINE by design, like every suite here: no AWS, no Groq, no network.
# DynamoDB is fake_dynamodb and the model is a scripted stub.
#
# WHAT IS ACTUALLY AT RISK, and therefore what these tests are mostly about.
#
#   1. AN UNBOUNDED DYNAMODB ITEM. A conversation is ONE item holding a turn
#      list, so "chat forever" and "400 KB item limit" are on a collision
#      course. The bound is not a comment: test_the_item_cannot_grow_without
#      _bound sends 60 maximum-size exchanges and measures the stored item.
#      If _append_turns ever stops slicing, that test fails loudly.
#
#   2. A STORED CONVERSATION BECOMING AN AUTHORIZATION BYPASS. Persisted
#      assistant replies contain task and meeting information the caller
#      could read AT THE TIME. Access can be revoked later. So session
#      authorization is mandatory on every route, and replaying history must
#      never re-run a tool or re-read a task — the tests assert both the
#      404s and the absence of tool calls on replay.
#
#   3. BREAKING THE DEPLOYED APP. The shipped client sends {message, history}
#      and no session_id. That request must behave exactly as it did before,
#      which is why the no-session path is tested as carefully as the new one.
#
#   4. LOSING A GOOD ANSWER TO A BAD WRITE. The model has already been paid
#      for by the time persistence runs. A DynamoDB blip must not turn a
#      correct answer into a generic AI failure (spec section 14).
#
# COVERAGE (mapped to spec section 16)
#   CREATION       new chat creates a session; title derived; turns persisted
#   CONTINUATION   session loads; follow-up gets prior history; turns append
#   AUTHORIZATION  own session yes; another user's session no, on all routes;
#                  identity injection ignored
#   LIMITS         turn cap, message length, reply length, list page size,
#                  and the item-size bound measured end to end
#   API            GET list, GET one, DELETE, POST with and without session_id
#   FAILURE        missing session, malformed id, read failure, write failure,
#                  empty/invalid message
#   SEPARATION     meeting chat persistence is untouched
#
# Run:  python tests/test_ai_chat_sessions.py
# =============================================================
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

# ORDER MATTERS — see test_ai_task_dashboard.py. Binds the same `api` module
# object and Key class every other suite uses.
from test_ai_assistant import (  # noqa: E402
    AIBase, FakeModel, KEY, OTHER, USER, api, call, event, parse,
)
import fake_dynamodb as fdb  # noqa: E402
import groq_client  # noqa: E402


class SessionBase(AIBase):
    """AIBase plus a scripted model and one-call session helpers.

    AIBase already wires api._chat_sessions to the fake table, so a chat
    request here exercises the real persistence path rather than the
    failure path.
    """

    def setUp(self):
        super().setUp()
        # Identity is swapped per call by as_user(): these tests are about
        # WHO is asking.
        self.current_user = USER
        p = mock.patch.object(api, "_require_auth",
                              side_effect=lambda ev: self.current_user)
        p.start()
        self.addCleanup(p.stop)
        self.sessions = self.t["chat_sessions"]

    def as_user(self, user_id):
        self.current_user = user_id

    # -- the routes, one call each ---------------------------------------
    def chat(self, message, session_id=None, history=None, reply="An answer."):
        body = {"message": message}
        if session_id is not None:
            body["session_id"] = session_id
        if history is not None:
            body["history"] = history
        fake = FakeModel([{"content": reply}])
        with mock.patch.object(api.groq_client, "complete_with_tools", fake):
            status, out = parse(call(api.ai_chat, event(body=body)))
        return status, out, fake

    def list_sessions(self, **qs):
        ev = event(method="GET", route="/ai/chat/sessions")
        ev["queryStringParameters"] = {k: str(v) for k, v in qs.items()}
        return parse(call(api.ai_list_sessions, ev))

    def get_session(self, session_id):
        ev = event(method="GET", route="/ai/chat/sessions/{session_id}")
        ev["pathParameters"] = {"session_id": session_id}
        return parse(call(api.ai_get_session, ev))

    def delete_session(self, session_id):
        ev = event(method="DELETE", route="/ai/chat/sessions/{session_id}")
        ev["pathParameters"] = {"session_id": session_id}
        return parse(call(api.ai_delete_session, ev))

    def stored(self, session_id):
        return self.sessions.get_item(
            Key={"session_id": session_id}).get("Item")


# ===========================================================================
# CREATION
# ===========================================================================
class TestSessionCreation(SessionBase):
    def test_a_chat_with_no_session_id_creates_one(self):
        status, out, _ = self.chat("What should I work on this week?")
        self.assertEqual(status, 200)
        self.assertTrue(out["session_id"])
        self.assertIsNotNone(self.stored(out["session_id"]))

    def test_the_title_comes_from_the_first_user_message(self):
        """Deterministic and free (spec section 7): no extra Groq call for a
        label, which would double the cost of starting a conversation."""
        _, out, _ = self.chat("What should I work on this week?")
        row = self.stored(out["session_id"])
        self.assertEqual(row["title"], "What should I work on this week?")

    def test_a_long_first_message_is_trimmed_on_a_word_boundary(self):
        long = ("Please tell me everything about the payment integration "
                "work and what is still outstanding across every meeting")
        _, out, _ = self.chat(long)
        title = self.stored(out["session_id"])["title"]
        self.assertLessEqual(len(title), api.MAX_SESSION_TITLE_CHARS + 1)
        self.assertTrue(title.endswith("…"))
        # Cut between words, not mid-word — the title reads as a phrase.
        self.assertFalse(title[:-1].endswith(" "))
        self.assertTrue(long.startswith(title[:-1]))

    def test_the_title_can_never_carry_meaningful_content(self):
        """60 characters cannot hold a transcript excerpt. That bound is the
        actual protection, not a style choice."""
        _, out, _ = self.chat("x" * 1900)
        self.assertLessEqual(
            len(self.stored(out["session_id"])["title"]),
            api.MAX_SESSION_TITLE_CHARS + 1)

    def test_both_turns_are_persisted(self):
        _, out, _ = self.chat("Question one?", reply="Answer one.")
        turns = self.stored(out["session_id"])["turns"]
        self.assertEqual([(t["role"], t["content"]) for t in turns],
                         [("user", "Question one?"),
                          ("assistant", "Answer one.")])
        self.assertTrue(all(t.get("at") for t in turns))

    def test_the_session_records_the_authenticated_user(self):
        _, out, _ = self.chat("Mine")
        self.assertEqual(self.stored(out["session_id"])["user_id"], USER)

    def test_message_count_and_preview_are_maintained(self):
        _, out, _ = self.chat("Question?", reply="A helpful answer.")
        row = self.stored(out["session_id"])
        self.assertEqual(row["message_count"], 2)
        self.assertEqual(row["last_message_preview"], "A helpful answer.")

    def test_no_row_is_written_when_the_answer_fails(self):
        """An abandoned generation must not leave an empty conversation in
        the user's list — the session is created only once there is an
        exchange worth keeping."""
        with mock.patch.object(
                api.groq_client, "complete_with_tools",
                side_effect=groq_client.GroqError("down", retryable=True)):
            status, _ = parse(call(api.ai_chat,
                                   event(body={"message": "hi"})))
        self.assertEqual(status, 502)
        self.assertEqual(self.list_sessions()[1]["count"], 0)

    def test_an_empty_reply_creates_no_session(self):
        fake = FakeModel([{"content": ""}])
        with mock.patch.object(api.groq_client, "complete_with_tools", fake):
            status, _ = parse(call(api.ai_chat,
                                   event(body={"message": "hi"})))
        self.assertEqual(status, 502)
        self.assertEqual(self.list_sessions()[1]["count"], 0)


# ===========================================================================
# CONTINUATION
# ===========================================================================
class TestSessionContinuation(SessionBase):
    def test_a_follow_up_reuses_the_same_session(self):
        _, first, _ = self.chat("What should I work on?")
        sid = first["session_id"]
        _, second, _ = self.chat("And which is most urgent?", session_id=sid)
        self.assertEqual(second["session_id"], sid)

    def test_the_follow_up_receives_the_prior_conversation(self):
        """THE POINT OF THE FEATURE. The model must see the earlier exchange
        even though the client sent no history at all."""
        _, first, _ = self.chat("What should I work on?", reply="Task A.")
        _, _, fake = self.chat("And which is most urgent?",
                               session_id=first["session_id"])
        sent = [(m["role"], m["content"]) for m in fake.seen[0]["messages"]
                if m["role"] != "system"]
        self.assertEqual(sent, [
            ("user", "What should I work on?"),
            ("assistant", "Task A."),
            ("user", "And which is most urgent?"),
        ])

    def test_turns_are_appended_not_replaced(self):
        _, first, _ = self.chat("One?")
        sid = first["session_id"]
        self.chat("Two?", session_id=sid)
        self.chat("Three?", session_id=sid)
        row = self.stored(sid)
        self.assertEqual(len(row["turns"]), 6)
        self.assertEqual(row["message_count"], 6)
        self.assertEqual([t["content"] for t in row["turns"]
                          if t["role"] == "user"], ["One?", "Two?", "Three?"])

    def test_the_stored_history_wins_over_client_supplied_history(self):
        """The server's copy is authoritative. A client replaying a stale or
        differently-trimmed thread must not be able to rewrite what the
        model believes was said."""
        _, first, _ = self.chat("Real question?", reply="Real answer.")
        _, _, fake = self.chat(
            "Follow up?", session_id=first["session_id"],
            history=[{"role": "user", "content": "FABRICATED history"}])
        blob = json.dumps(fake.seen[0]["messages"])
        self.assertIn("Real question?", blob)
        self.assertNotIn("FABRICATED", blob)

    def test_continuing_a_session_advances_its_updated_at(self):
        """What makes the list order meaningful: the conversation you just
        used moves to the top.

        Asserted on the STORED timestamp rather than on list position: the
        ordering guarantee has its own test in TestSessionApis, and pinning
        position here would couple this test to the tie-break too.
        """
        _, a, _ = self.chat("First conversation")
        sid = a["session_id"]
        created = self.stored(sid)["created_at"]
        # Backdated so the next write is unambiguously later: the clock can
        # return the same value twice within a test on a coarse platform.
        row = self.stored(sid)
        row["updated_at"] = "2026-01-01T00:00:00.000000Z"
        self.sessions.put_item(Item=row)
        before = self.stored(sid)["updated_at"]

        self.chat("another turn", session_id=sid)
        after = self.stored(sid)["updated_at"]
        self.assertGreater(after, before)
        # created_at is NOT touched — a conversation's age is not its
        # last-used time, and the list needs both.
        self.assertEqual(self.stored(sid)["created_at"], created)

    def test_replaying_a_session_runs_no_tools(self):
        """SPEC SECTION 11. History is conversational context, never a data
        source: reading a session back must not re-run a tool or re-read a
        task, so a turn that once described Task A cannot re-expose it."""
        _, out, _ = self.chat("What about task A?", reply="Task A is open.")
        with mock.patch.object(api, "_ai_dispatch") as dispatch:
            status, body = self.get_session(out["session_id"])
        self.assertEqual(status, 200)
        dispatch.assert_not_called()
        self.assertEqual(len(body["session"]["turns"]), 2)


# ===========================================================================
# AUTHORIZATION — the critical section.
# ===========================================================================
class TestSessionAuthorization(SessionBase):
    def setUp(self):
        super().setUp()
        _, out, _ = self.chat("Alice's private conversation",
                              reply="Alice's private answer.")
        self.alice_session = out["session_id"]

    def test_the_owner_can_read_it(self):
        status, body = self.get_session(self.alice_session)
        self.assertEqual(status, 200)
        self.assertEqual(body["session"]["session_id"], self.alice_session)

    def test_another_user_cannot_read_it(self):
        self.as_user(OTHER)
        status, body = self.get_session(self.alice_session)
        self.assertEqual(status, 404)
        self.assertNotIn("private", json.dumps(body))

    def test_another_user_cannot_continue_it(self):
        """The sharpest case: continuing someone else's session would feed
        their conversation into a reply the attacker receives."""
        self.as_user(OTHER)
        status, body, _ = self.chat("What did she say?",
                                    session_id=self.alice_session)
        self.assertEqual(status, 404)
        self.assertNotIn("private", json.dumps(body))

    def test_another_user_cannot_delete_it(self):
        self.as_user(OTHER)
        status, _ = self.delete_session(self.alice_session)
        self.assertEqual(status, 404)
        # And it is still there.
        self.assertIsNotNone(self.stored(self.alice_session))

    def test_it_is_404_and_never_403(self):
        """A 403 would confirm the id exists, which is exactly what someone
        enumerating ids wants. Matches _owned_task / _owned_folder."""
        self.as_user(OTHER)
        for status, _ in (self.get_session(self.alice_session),
                          self.delete_session(self.alice_session)):
            self.assertEqual(status, 404)

    def test_another_users_session_is_absent_from_the_list(self):
        self.as_user(OTHER)
        self.assertEqual(self.list_sessions()[1]["count"], 0)

    def test_the_list_only_returns_the_callers_own(self):
        self.as_user(OTHER)
        self.chat("Bob's conversation")
        self.as_user(USER)
        sessions = self.list_sessions()[1]["sessions"]
        self.assertEqual([s["session_id"] for s in sessions],
                         [self.alice_session])

    def test_a_body_supplied_user_id_is_ignored(self):
        """Identity comes ONLY from the token. A client naming another user
        gets its own session anyway (spec section 12)."""
        fake = FakeModel([{"content": "ok"}])
        with mock.patch.object(api.groq_client, "complete_with_tools", fake):
            status, out = parse(call(api.ai_chat, event(body={
                "message": "hi", "user_id": OTHER, "owner_user_id": OTHER,
                "org_id": "org-evil", "workspace_id": "ws-evil"})))
        self.assertEqual(status, 200)
        self.assertEqual(self.stored(out["session_id"])["user_id"], USER)

    def test_a_body_supplied_identity_cannot_reach_another_session(self):
        self.as_user(OTHER)
        fake = FakeModel([{"content": "ok"}])
        with mock.patch.object(api.groq_client, "complete_with_tools", fake):
            status, _ = parse(call(api.ai_chat, event(body={
                "message": "steal", "session_id": self.alice_session,
                "user_id": USER})))
        self.assertEqual(status, 404)

    def test_the_owner_can_delete_their_own(self):
        status, body = self.delete_session(self.alice_session)
        self.assertEqual(status, 200)
        self.assertTrue(body["deleted"])
        self.assertIsNone(self.stored(self.alice_session))

    def test_deleting_removes_the_whole_conversation(self):
        """One item holds every turn, so there is no separate message store
        left orphaned — a direct benefit of the single-item design."""
        self.delete_session(self.alice_session)
        self.assertEqual(self.list_sessions()[1]["count"], 0)
        self.assertEqual(self.get_session(self.alice_session)[0], 404)


# ===========================================================================
# LIMITS — the unbounded-item question, answered by measurement.
# ===========================================================================
class TestSessionLimits(SessionBase):
    def test_the_turn_list_is_capped(self):
        _, out, _ = self.chat("start")
        sid = out["session_id"]
        for i in range(40):
            self.chat(f"turn {i}", session_id=sid)
        row = self.stored(sid)
        self.assertEqual(len(row["turns"]), api.MAX_SESSION_TURNS)

    def test_the_oldest_turns_are_the_ones_dropped(self):
        _, out, _ = self.chat("THE VERY FIRST MESSAGE")
        sid = out["session_id"]
        for i in range(40):
            self.chat(f"turn {i}", session_id=sid)
        blob = json.dumps(self.stored(sid)["turns"])
        self.assertNotIn("THE VERY FIRST MESSAGE", blob)
        self.assertIn("turn 39", blob)

    def test_message_count_keeps_counting_past_the_cap(self):
        """The user did send those messages. A count that reset would be a
        lie; it is metadata, not an index into the array."""
        _, out, _ = self.chat("start")
        sid = out["session_id"]
        for i in range(30):
            self.chat(f"turn {i}", session_id=sid)
        row = self.stored(sid)
        self.assertEqual(row["message_count"], 62)
        self.assertEqual(len(row["turns"]), api.MAX_SESSION_TURNS)

    def test_the_item_cannot_grow_without_bound(self):
        """THE LOAD-BEARING TEST OF THIS FILE.

        60 exchanges at the maximum message and reply size — far more than a
        real conversation — and the stored item must stay well inside
        DynamoDB's 400 KB ceiling. If _append_turns ever stops slicing before
        it writes, this is what catches it.
        """
        big_reply = "r" * 10_000          # over MAX_STORED_REPLY_CHARS
        big_message = "m" * api.MAX_AI_MESSAGE_CHARS
        sid = None
        for _ in range(60):
            _, out, _ = self.chat(big_message, session_id=sid,
                                  reply=big_reply)
            sid = out["session_id"]
        row = self.stored(sid)
        size = len(json.dumps(row, default=str))
        self.assertLessEqual(len(row["turns"]), api.MAX_SESSION_TURNS)
        self.assertLess(size, 400_000, f"item grew to {size} bytes")
        # And with real headroom, not just barely inside.
        self.assertLess(size, 200_000)

    def test_a_stored_reply_is_truncated(self):
        _, out, _ = self.chat("q", reply="r" * 50_000)
        turns = self.stored(out["session_id"])["turns"]
        self.assertEqual(len(turns[-1]["content"]),
                         api.MAX_STORED_REPLY_CHARS)

    def test_an_oversize_message_is_refused_before_anything_is_written(self):
        status, body = parse(call(api.ai_chat, event(body={
            "message": "x" * (api.MAX_AI_MESSAGE_CHARS + 1)})))
        self.assertEqual(status, 400)
        self.assertIn("too long", body["error"])
        self.assertEqual(self.list_sessions()[1]["count"], 0)

    def test_a_stored_user_message_is_bounded(self):
        _, out, _ = self.chat("m" * api.MAX_AI_MESSAGE_CHARS)
        turns = self.stored(out["session_id"])["turns"]
        self.assertLessEqual(len(turns[0]["content"]),
                             api.MAX_AI_MESSAGE_CHARS)

    def test_the_list_is_paged(self):
        for i in range(8):
            self.chat(f"conversation {i}")
        status, body = self.list_sessions(limit=3)
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 3)
        self.assertTrue(body["next_cursor"])

    def test_the_list_limit_is_capped(self):
        """A client asking for everything must not get everything."""
        status, body = self.list_sessions(limit=10_000)
        self.assertEqual(status, 200)
        self.assertLessEqual(body["count"], api.SESSIONS_PAGE_MAX)

    def test_a_bad_limit_is_rejected(self):
        self.assertEqual(self.list_sessions(limit="lots")[0], 400)
        self.assertEqual(self.list_sessions(limit=0)[0], 400)

    def test_the_model_context_is_narrower_than_storage(self):
        """STORAGE AND CONTEXT ARE SEPARATE CONCERNS (spec section 8). 40
        turns are kept so the user can scroll back; the model gets
        AI_HISTORY_TURNS exchanges, or the window would fill with history
        instead of an answer."""
        _, out, _ = self.chat("start")
        sid = out["session_id"]
        for i in range(20):
            self.chat(f"turn {i}", session_id=sid)
        _, _, fake = self.chat("final", session_id=sid)
        history = [m for m in fake.seen[0]["messages"]
                   if m["role"] in ("user", "assistant")]
        # The new user turn plus at most AI_HISTORY_TURNS exchanges.
        self.assertLessEqual(len(history), api.AI_HISTORY_TURNS * 2 + 1)
        self.assertEqual(len(self.stored(sid)["turns"]),
                         api.MAX_SESSION_TURNS)


# ===========================================================================
# THE SESSION APIs
# ===========================================================================
class TestSessionApis(SessionBase):
    def test_the_list_is_lightweight(self):
        """No turns in a list (spec section 6): a list that carried every
        conversation's full text would be the most expensive read in the app
        and is never what a list needs."""
        self.chat("A question", reply="An answer.")
        _, body = self.list_sessions()
        row = body["sessions"][0]
        self.assertNotIn("turns", row)
        self.assertEqual(
            set(row),
            {"session_id", "title", "created_at", "updated_at",
             "message_count", "last_message_preview"})

    def test_the_list_is_most_recently_updated_first(self):
        """Ordered by updated_at descending — the index's own order.

        The timestamps are set explicitly rather than relying on three
        chats to produce three distinct ones. They do not: the clock's
        resolution is about a millisecond on some platforms (Windows), so
        writes inside one test routinely TIE and the order then falls to the
        tie-break. That is correct behaviour, and it has its own test — but
        it means a test that assumed strict creation order would be
        asserting the tie-break by accident.
        """
        ids = []
        for i, title in enumerate(("oldest", "middle", "newest")):
            _, out, _ = self.chat(title)
            sid = out["session_id"]
            ids.append(sid)
            row = self.stored(sid)
            row["updated_at"] = f"2026-09-0{i + 1}T00:00:00.000000Z"
            self.sessions.put_item(Item=row)
        got = [x["session_id"] for x in self.list_sessions()[1]["sessions"]]
        self.assertEqual(got, list(reversed(ids)))

    def test_a_continued_conversation_moves_to_the_top(self):
        """What makes the ordering useful, from the list's point of view.

        The other sessions are backdated so "moved to the top" is a real
        claim rather than a coin-flip against a tied timestamp.
        """
        _, a, _ = self.chat("first")
        self.chat("second")
        self.chat("third")
        for sess in self.list_sessions()[1]["sessions"]:
            row = self.stored(sess["session_id"])
            row["updated_at"] = "2026-01-01T00:00:00.000000Z"
            self.sessions.put_item(Item=row)

        self.chat("another turn", session_id=a["session_id"])
        ids = [x["session_id"] for x in self.list_sessions()[1]["sessions"]]
        self.assertEqual(ids[0], a["session_id"])

    def test_the_order_is_stable_when_timestamps_tie(self):
        """Two sessions CAN share an updated_at — the value has microsecond
        resolution but nothing guarantees uniqueness. Without a tie-break the
        list would reshuffle between refreshes, which reads as a bug even
        when every row is correct."""
        for i in range(4):
            self.chat(f"conversation {i}")
        # Force a genuine tie rather than relying on the clock.
        for sess in self.list_sessions()[1]["sessions"]:
            row = self.stored(sess["session_id"])
            row["updated_at"] = "2026-09-02T00:00:00.000000Z"
            self.sessions.put_item(Item=row)

        first = [s["session_id"] for s in self.list_sessions()[1]["sessions"]]
        second = [s["session_id"] for s in self.list_sessions()[1]["sessions"]]
        self.assertEqual(first, second)
        # Descending session_id is the documented tie-break.
        self.assertEqual(first, sorted(first, reverse=True))

    def test_an_empty_list_is_not_an_error(self):
        status, body = self.list_sessions()
        self.assertEqual(status, 200)
        self.assertEqual(body["sessions"], [])
        self.assertEqual(body["count"], 0)

    def test_get_session_returns_turns_chronologically(self):
        _, out, _ = self.chat("First?", reply="First answer.")
        self.chat("Second?", session_id=out["session_id"],
                  reply="Second answer.")
        _, body = self.get_session(out["session_id"])
        self.assertEqual([t["content"] for t in body["session"]["turns"]],
                         ["First?", "First answer.",
                          "Second?", "Second answer."])

    def test_get_session_carries_the_metadata_too(self):
        _, out, _ = self.chat("A question")
        _, body = self.get_session(out["session_id"])
        s = body["session"]
        self.assertEqual(s["title"], "A question")
        self.assertEqual(s["message_count"], 2)
        self.assertTrue(s["created_at"] and s["updated_at"])

    def test_no_tool_arguments_or_proposals_are_persisted(self):
        """SPEC SECTION 4. A stored task id is a durable reference to data
        whose access may be revoked, and a replayed proposal would be an
        offer the user may no longer be allowed to accept."""
        row = api._new_task_row(USER, "Send the proposal", recording_key=KEY,
                                due="2026-09-04",
                                due_normalized="2026-09-04")
        api._write_task(row)
        fake = FakeModel([
            {"calls": [("propose_task_change",
                        {"task_id": row["task_id"], "status": "Completed"})]},
            {"content": "Confirm and I'll apply it."},
        ])
        with mock.patch.object(api.groq_client, "complete_with_tools", fake):
            status, out = parse(call(api.ai_chat, event(
                body={"message": "mark it done"})))
        self.assertEqual(status, 200)
        self.assertTrue(out["proposals"])          # returned to the client
        stored = self.stored(out["session_id"])
        blob = json.dumps(stored)
        self.assertNotIn(row["task_id"], blob)     # but never persisted
        self.assertNotIn("proposals", stored)
        self.assertNotIn("tools_used", stored)
        for turn in stored["turns"]:
            self.assertEqual(set(turn), {"role", "content", "at"})

    def test_routes_are_registered(self):
        self.assertIs(api._ROUTES[("GET", "/ai/chat/sessions")],
                      api.ai_list_sessions)
        self.assertIs(
            api._ROUTES[("GET", "/ai/chat/sessions/{session_id}")],
            api.ai_get_session)
        self.assertIs(
            api._ROUTES[("DELETE", "/ai/chat/sessions/{session_id}")],
            api.ai_delete_session)

    def test_the_session_routes_do_not_shadow_the_chat_route(self):
        """API Gateway matches the FULL path, so /ai/chat/sessions and
        /ai/chat are distinct — pinned because a prefix-matching router
        would break both."""
        self.assertIs(api._ROUTES[("POST", "/ai/chat")], api.ai_chat)
        self.assertIn(("GET", "/ai/chat/sessions"), api._ROUTES)


# ===========================================================================
# BACKWARD COMPATIBILITY — the deployed app must keep working.
# ===========================================================================
class TestBackwardCompatibility(SessionBase):
    def test_a_client_that_sends_no_session_id_still_works(self):
        status, out, _ = self.chat("What are my tasks?")
        self.assertEqual(status, 200)
        self.assertTrue(out["reply"])
        # Every pre-existing key is still present and unchanged in meaning.
        for key in ("reply", "tools_used", "sources", "proposals"):
            self.assertIn(key, out)

    def test_client_supplied_history_is_still_honoured_without_a_session(self):
        """The shipped app's exact request shape."""
        _, _, fake = self.chat(
            "And the next one?",
            history=[{"role": "user", "content": "First question"},
                     {"role": "assistant", "content": "First answer"}])
        sent = [(m["role"], m["content"]) for m in fake.seen[0]["messages"]
                if m["role"] != "system"]
        self.assertEqual(sent, [("user", "First question"),
                                ("assistant", "First answer"),
                                ("user", "And the next one?")])

    def test_a_history_only_client_still_gets_a_session_it_may_ignore(self):
        _, out, _ = self.chat("hello", history=[])
        self.assertTrue(out["session_id"])

    def test_meeting_chat_persistence_is_untouched(self):
        """SPEC SECTION 15. The meeting chat stores history on the RECORDING
        row; workspace sessions are a separate table. Neither writes to the
        other's store."""
        _, out, _ = self.chat("workspace question")
        rec = self.t["recordings"].get_item(
            Key={"audio_s3_key": KEY}).get("Item") or {}
        self.assertNotIn("chat_history", rec)
        self.assertIsNotNone(self.stored(out["session_id"]))

    def test_the_meeting_chat_routes_are_unchanged(self):
        for route, handler in (
            (("GET", "/recordings/ai/chat/{key+}"), api.get_chat),
            (("POST", "/recordings/ai/chat/{key+}"), api.chat),
            (("DELETE", "/recordings/ai/chat/{key+}"), api.clear_chat),
        ):
            with self.subTest(route=route):
                self.assertIs(api._ROUTES[route], handler)


# ===========================================================================
# FAILURE MODES
# ===========================================================================
class TestSessionFailures(SessionBase):
    def test_a_missing_session_is_404(self):
        self.assertEqual(self.get_session("doesnotexist")[0], 404)
        self.assertEqual(self.delete_session("doesnotexist")[0], 404)
        self.assertEqual(self.chat("hi", session_id="doesnotexist")[0], 404)

    def test_an_empty_session_id_is_treated_as_a_new_conversation(self):
        """A client that sends "" (an unset field) means "no session", not
        "session named empty string" — and must not get a 400 for it."""
        status, out, _ = self.chat("hello", session_id="")
        self.assertEqual(status, 200)
        self.assertTrue(out["session_id"])

    def test_a_malformed_session_id_is_not_found_rather_than_a_500(self):
        for bad in ("../../etc/passwd", "x" * 500, "a b c", "{}"):
            with self.subTest(bad=bad):
                self.assertEqual(self.get_session(bad)[0], 404)

    def test_an_empty_message_is_400(self):
        status, body = parse(call(api.ai_chat, event(body={"message": "  "})))
        self.assertEqual(status, 400)
        self.assertIn("message required", body["error"])

    def test_a_session_read_failure_is_not_a_silent_new_session(self):
        """SPEC SECTION 14. Treating a read failure as "not found" would
        start a fresh conversation while the user's own one still exists —
        and would look identical to another user's session being absent."""
        _, out, _ = self.chat("real conversation")
        err = fdb.ClientError({"Error": {"Code": "ProvisionedThroughput"
                                                 "ExceededException"}},
                              "GetItem")
        with mock.patch.object(self.sessions, "get_item", side_effect=err):
            status, body = self.get_session(out["session_id"])
        self.assertEqual(status, 503)
        self.assertNotEqual(status, 404)
        self.assertIn("retry", body["error"].lower())

    def test_a_persistence_failure_still_returns_the_answer(self):
        """THE MODEL HAS ALREADY BEEN PAID FOR. Turning a correct answer into
        a generic AI failure because a write blipped would lose the user real
        work and misattribute the fault (spec section 14)."""
        err = fdb.ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}},
            "PutItem")
        with mock.patch.object(self.sessions, "put_item", side_effect=err):
            status, out, _ = self.chat("a question", reply="a good answer")
        self.assertEqual(status, 200)
        self.assertEqual(out["reply"], "a good answer")
        # Flagged, so the app can tell the user this turn was not kept.
        self.assertFalse(out["persisted"])
        # And no session id is claimed, because there is nothing to resume.
        self.assertEqual(out["session_id"], "")

    def test_an_append_failure_on_an_existing_session_still_answers(self):
        _, first, _ = self.chat("first")
        err = fdb.ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException"}},
            "UpdateItem")
        with mock.patch.object(self.sessions, "update_item", side_effect=err):
            status, out, _ = self.chat("second",
                                       session_id=first["session_id"],
                                       reply="still answered")
        self.assertEqual(status, 200)
        self.assertEqual(out["reply"], "still answered")
        self.assertFalse(out["persisted"])

    def test_a_successful_turn_reports_persisted(self):
        """The flag is only sent on failure, so its ABSENCE means success —
        pinned so a future edit does not invert it."""
        _, out, _ = self.chat("a question")
        self.assertNotIn("persisted", out)

    def test_a_missing_index_degrades_to_an_empty_list(self):
        """An environment that has not run the create-table script gets an
        empty list and a log line, not a 500 — the chat itself works without
        a list."""
        err = fdb.ClientError(
            {"Error": {"Code": "ValidationException"}}, "Query")
        with mock.patch.object(self.sessions, "query", side_effect=err):
            status, body = self.list_sessions()
        self.assertEqual(status, 200)
        self.assertEqual(body["sessions"], [])

    def test_a_corrupt_turn_list_reads_as_an_empty_conversation(self):
        """A corrupt session must degrade to "no history" and keep
        answering, never 500 the chat route."""
        _, out, _ = self.chat("start")
        sid = out["session_id"]
        row = self.stored(sid)
        row["turns"] = "not a list"
        self.sessions.put_item(Item=row)
        status, _, fake = self.chat("continue", session_id=sid)
        self.assertEqual(status, 200)
        history = [m for m in fake.seen[0]["messages"]
                   if m["role"] in ("user", "assistant")]
        self.assertEqual(len(history), 1)

    def test_observability_logs_no_conversation_content(self):
        """SPEC SECTION 17. These logs are retained and broadly readable:
        ids, counts and outcomes only."""
        with mock.patch("builtins.print") as printed:
            self.chat("a very secret question",
                      reply="a very secret answer")
        logged = " ".join(str(c) for c in printed.call_args_list)
        self.assertNotIn("secret question", logged)
        self.assertNotIn("secret answer", logged)
        self.assertIn("persistence_success", logged)
        self.assertIn(USER, logged)


if __name__ == "__main__":
    unittest.main(verbosity=2)
