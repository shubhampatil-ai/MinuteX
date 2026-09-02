#!/usr/bin/env python3
"""test_pageindex.py — per-meeting retrieval: index, search, evidence, auth.

WHAT THIS PINS, and why each part is load-bearing.

THE BUG THE FEATURE FIXES. Meeting AI built its context with
prompts.build_context(..., transcript_budget_chars=N), which cuts the
transcript to its FIRST N characters. On a short meeting N is the whole thing
and nothing is wrong. On a two-hour meeting the model is handed the opening
thirty-five minutes and asked "what did we decide at the end?" — and correctly
answers that its record is partial. The words were in S3; nothing fetched them.

So the tests that matter most here are the ones that would have FAILED before:

  * LargeMeetingTests — a question about the END of a long meeting reaches the
    late transcript, not the head. This is the acceptance criterion of Part 21
    and the single reason the module exists.
  * MomAuthorityTests — a figure the user CORRECTED in the minutes is what the
    model is told, ranked above the transcript that still says the old number.

Everything else guards a property that is easy to break silently later:

  * SegmentIdentityTests — retrieval must return the SAME `seg_N` the
    transcript UI, task evidence and deep-link already use. A retrieval layer
    that minted its own ids would look fine in isolation and produce sources
    that scroll nowhere.
  * FingerprintTests / ReindexRuleTests — what rebuilds and what must NOT.
    A task status change or a speaker rename rebuilding every index would be
    invisible in tests and expensive in production.
  * AuthorizationTests — no path may resolve an index from a meeting key alone.
  * FailureTests — an index is an optimisation over a fallback that works;
    no failure in it may 500 a question or strand a meeting.

WHAT IS DELIBERATELY NOT TESTED. Groq itself is stubbed everywhere (as in every
other suite here) — these are offline tests of OUR logic, not of the model's
judgement about which sections are relevant.

Run:  python -m pytest tests/test_pageindex.py
"""
import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "functions/userapi"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_ai_workspace import AiTestCase, RECORDING, api, call, event, parse  # noqa: E402

import ai_schema        # noqa: E402
import groq_client      # noqa: E402
import pageindex        # noqa: E402
import pageindex_store  # noqa: E402
import prompts          # noqa: E402
import transcript_store  # noqa: E402

KEY = RECORDING["audio_s3_key"]


def make_segments(count, seconds_each=10, start_at=0.0, text=lambda i: None):
    """`count` consecutive speaker turns, evenly spaced.

    Deliberately built through the SAME shape hydrate() produces (no `id` key —
    ids are derived), so a test can never accidentally prove that retrieval
    works only when ids were pre-stamped.
    """
    segs = []
    t = start_at
    for i in range(count):
        segs.append({
            "speaker": str(i % 3),
            "text": text(i) or f"This is turn number {i} about routine matters.",
            "start": t,
            "end": t + seconds_each,
        })
        t += seconds_each
    return segs


def transcript_for(segments):
    """The prose transcript matching `segments`, in stt_result's shape.

    Blank-line separated, one turn per line — the format
    transcript_store.as_labelled_lines pairs against by position.
    """
    return "\n\n".join(f"Speaker {s['speaker']}: {s['text']}" for s in segments)


# ===========================================================================
# Tree construction
# ===========================================================================
class TreeBuildTests(unittest.TestCase):

    def test_builds_nodes_covering_every_segment(self):
        """No segment may fall between two nodes: an unindexed turn is one
        retrieval can never reach, and nothing downstream would report it."""
        segs = make_segments(120)
        tree = pageindex.build_tree(segs)
        covered = set()
        for node in tree["nodes"]:
            lo = int(node["first_segment"].split("_")[1])
            hi = int(node["last_segment"].split("_")[1])
            covered.update(range(lo, hi + 1))
        self.assertEqual(covered, set(range(120)))

    def test_nodes_are_contiguous_and_ordered(self):
        """Each node picks up exactly where the last left off. Gaps or overlaps
        would make a retrieved 'section' something other than a passage."""
        tree = pageindex.build_tree(make_segments(90))
        expected_next = 0
        for node in tree["nodes"]:
            lo = int(node["first_segment"].split("_")[1])
            self.assertEqual(lo, expected_next)
            expected_next = int(node["last_segment"].split("_")[1]) + 1

    def test_long_meeting_produces_many_nodes(self):
        """A 2-hour meeting must be cut finely enough that retrieval is
        selective. One giant node would reproduce the head-truncation bug one
        level up."""
        # 720 turns x 10s = 2 hours
        tree = pageindex.build_tree(make_segments(720))
        self.assertGreater(len(tree["nodes"]), 15)

    def test_untimed_transcript_still_splits(self):
        """Some transcripts arrive with every start/end at 0. Without the
        segment-count backstop that collapses to ONE node holding everything —
        exactly the failure this module removes."""
        segs = [{"speaker": "0", "text": f"turn {i}", "start": 0, "end": 0}
                for i in range(300)]
        tree = pageindex.build_tree(segs)
        self.assertGreater(len(tree["nodes"]), 1)

    def test_decimal_timestamps_are_tolerated(self):
        """A legacy inline DynamoDB row hands back Decimal, not float. Crashing
        on it would make exactly the oldest meetings unindexable."""
        segs = [{"speaker": "0", "text": "hello", "start": Decimal("1.5"),
                 "end": Decimal("4.25")}]
        tree = pageindex.build_tree(segs)
        self.assertEqual(len(tree["nodes"]), 1)
        self.assertIsInstance(tree["nodes"][0]["start"], float)

    def test_empty_transcript_is_an_empty_tree_not_an_error(self):
        """A recording whose transcript failed is a normal state the app already
        renders as 'not ready'. It must not become a crash."""
        self.assertEqual(pageindex.build_tree([])["nodes"], [])
        self.assertEqual(pageindex.build_tree(None)["nodes"], [])

    def test_tree_is_json_serialisable(self):
        """It is stored as gzipped JSON. A Decimal leaking into a node would
        fail at write time, long after the build looked successful."""
        tree = pageindex.build_tree(make_segments(50))
        json.dumps(tree)  # must not raise

    def test_tree_stores_ranges_not_transcript_copies(self):
        """The transcript stays the single source of the words. Copying text
        into the index would double the storage and create a second thing to
        keep in sync with a re-transcription."""
        tree = pageindex.build_tree(make_segments(60))
        blob = json.dumps(tree)
        # The summary is a deliberate short extract; the full text of every turn
        # must not be present. 60 turns of identical phrasing would repeat.
        self.assertLess(len(blob), len(transcript_for(make_segments(60))))


# ===========================================================================
# Segment identity — the contract with the transcript UI and evidence layer
# ===========================================================================
class SegmentIdentityTests(unittest.TestCase):

    def test_retrieved_segments_carry_the_canonical_seg_ids(self):
        """`seg_N` is transcript_store's positional identity. Retrieval must
        return THAT id — a new numbering would produce sources that look valid
        and scroll to the wrong moment, which no validator could catch."""
        segs = make_segments(100)
        tree = pageindex.build_tree(segs)
        got = pageindex.segments_for_nodes(tree, segs, ["node_0"])
        self.assertTrue(got)
        for seg in got:
            self.assertTrue(seg["id"].startswith(transcript_store.SEGMENT_ID_PREFIX))
        # And they are the ids the transcript itself would derive.
        canonical = transcript_store.with_segment_ids(segs)
        self.assertEqual(got[0]["id"], canonical[0]["id"])

    def test_retrieved_segments_preserve_timestamps_and_speaker(self):
        """These four fields ARE the deep-link: without start the player cannot
        seek, without the id the transcript cannot highlight."""
        segs = make_segments(60)
        tree = pageindex.build_tree(segs)
        got = pageindex.segments_for_nodes(tree, segs, ["node_1"])
        original = transcript_store.with_segment_ids(segs)
        by_id = {s["id"]: s for s in original}
        for seg in got:
            src = by_id[seg["id"]]
            self.assertEqual(seg["start"], src["start"])
            self.assertEqual(seg["end"], src["end"])
            self.assertEqual(seg["speaker"], src["speaker"])
            self.assertEqual(seg["text"], src["text"])

    def test_evidence_shape_matches_the_documented_contract(self):
        """Part 9/13's shape, and the fields app-side navigation consumes."""
        segs = make_segments(20)
        tree = pageindex.build_tree(segs)
        got = pageindex.segments_for_nodes(tree, segs, ["node_0"])
        ev = pageindex.evidence_from_segments(got, limit=3)
        self.assertLessEqual(len(ev), 3)
        for row in ev:
            self.assertEqual(set(row), {"segment_id", "speaker_id",
                                        "start_time", "end_time"})
            self.assertIsInstance(row["start_time"], float)

    def test_evidence_exposes_no_pageindex_internals(self):
        """Part 11: node ids and tree shape are retrieval mechanics. Leaking
        them would make the index's layout part of an API contract the app
        pins, which is then expensive to change."""
        segs = make_segments(20)
        tree = pageindex.build_tree(segs)
        got = pageindex.segments_for_nodes(tree, segs, ["node_0"])
        blob = json.dumps(pageindex.evidence_from_segments(got))
        self.assertNotIn("node_", blob)
        self.assertNotIn("summary", blob)

    def test_unknown_node_ids_are_ignored_not_fatal(self):
        """Node ids come back from a MODEL. A hallucinated one must contribute
        nothing, never 500 the user's question."""
        segs = make_segments(30)
        tree = pageindex.build_tree(segs)
        self.assertEqual(
            pageindex.segments_for_nodes(tree, segs, ["node_999", "nonsense"]), [])
        mixed = pageindex.segments_for_nodes(tree, segs, ["node_0", "node_999"])
        self.assertTrue(mixed)

    def test_repeated_node_ids_do_not_duplicate_segments(self):
        segs = make_segments(30)
        tree = pageindex.build_tree(segs)
        once = pageindex.segments_for_nodes(tree, segs, ["node_0"])
        twice = pageindex.segments_for_nodes(tree, segs, ["node_0", "node_0"])
        self.assertEqual([s["id"] for s in once], [s["id"] for s in twice])

    def test_segments_return_in_meeting_order(self):
        """Even when the navigator names nodes out of order, the extract must
        read chronologically — a conversation shown out of sequence changes
        what it appears to say."""
        segs = make_segments(60)
        tree = pageindex.build_tree(segs)
        ids = [n["node_id"] for n in tree["nodes"]]
        got = pageindex.segments_for_nodes(tree, segs, list(reversed(ids[:3])))
        positions = [int(s["id"].split("_")[1]) for s in got]
        self.assertEqual(positions, sorted(positions))

    def test_rendered_lines_use_the_same_label_format_as_the_full_path(self):
        """The model reads ONE format whichever way context was built, so the
        prompt's evidence instructions hold on both paths."""
        segs = make_segments(10)
        tree = pageindex.build_tree(segs)
        got = pageindex.segments_for_nodes(tree, segs, ["node_0"])
        rendered = pageindex.render_segments(got, {})
        self.assertIn("[seg_0]", rendered)
        full = transcript_store.as_labelled_lines(transcript_for(segs), segs)
        self.assertIn("[seg_0]", full)


# ===========================================================================
# Navigator reply parsing
# ===========================================================================
class NavigationParsingTests(unittest.TestCase):

    def setUp(self):
        self.tree = pageindex.build_tree(make_segments(120))

    def test_parses_the_json_we_ask_for(self):
        got = pageindex.parse_node_ids('{"nodes": ["node_0", "node_2"]}', self.tree)
        self.assertEqual(got, ["node_0", "node_2"])

    def test_scrapes_ids_out_of_prose(self):
        """Models wrap JSON in explanations often enough that failing the whole
        retrieval over a stray fence is not worth it."""
        got = pageindex.parse_node_ids(
            "Sure! I'd look at node_1 and node_3 for this.", self.tree)
        self.assertEqual(got, ["node_1", "node_3"])

    def test_filters_ids_that_do_not_exist(self):
        """This is what makes a hallucinated id harmless rather than an error
        further down."""
        got = pageindex.parse_node_ids('{"nodes": ["node_0", "node_9999"]}', self.tree)
        self.assertEqual(got, ["node_0"])

    def test_deduplicates_preserving_order(self):
        got = pageindex.parse_node_ids(
            '{"nodes": ["node_2", "node_0", "node_2"]}', self.tree)
        self.assertEqual(got, ["node_2", "node_0"])

    def test_garbage_yields_no_ids_rather_than_raising(self):
        for junk in ("", None, "¯\\_(ツ)_/¯", "{broken json"):
            self.assertEqual(pageindex.parse_node_ids(junk, self.tree), [])

    def test_table_of_contents_is_far_smaller_than_the_transcript(self):
        """The entire economic argument for the module: the navigator reads the
        index, not the meeting.

        Uses REALISTIC turn lengths deliberately. The tiny synthetic turns the
        other tests use make the fixed-size node descriptions look expensive by
        comparison, which is an artifact of the fixture rather than a property
        of the index — a real meeting's turns are sentences, not six words.
        """
        real = ("So what I wanted to raise is the delivery schedule for the "
                "second phase, because the vendor came back to us yesterday "
                "saying the lead time has moved out by about three weeks and "
                "that affects everything downstream.")
        segs = make_segments(720, text=lambda i: real)
        tree = pageindex.build_tree(segs)
        toc = pageindex.table_of_contents(tree, {})
        self.assertLess(len(toc), len(transcript_for(segs)) / 10)

    def test_table_of_contents_resolves_display_names_at_read_time(self):
        """Names are NOT baked into the tree — that is what lets a rename take
        effect with no rebuild (Part 18)."""
        segs = make_segments(30)
        tree = pageindex.build_tree(segs)
        self.assertIn("Priya", pageindex.table_of_contents(tree, {"1": "Priya"}))
        self.assertNotIn("Priya", json.dumps(tree))


# ===========================================================================
# Storage, versioning and the rebuild rules
# ===========================================================================
class FingerprintTests(unittest.TestCase):

    def setUp(self):
        self.fp = ai_schema.fingerprint("some transcript")
        self.item = {pageindex_store.PAGEINDEX_ATTR: {
            "status": "ready", "s3_key": "pageindex/x.json.gz",
            "transcript_fingerprint": self.fp,
            "version": pageindex.PAGEINDEX_VERSION,
        }}

    def test_matching_fingerprint_is_fresh(self):
        self.assertTrue(pageindex_store.is_fresh(self.item, self.fp))

    def test_changed_fingerprint_is_stale(self):
        """A re-transcription rewrites the words; the old tree describes text
        that no longer exists."""
        self.assertFalse(pageindex_store.is_fresh(
            self.item, ai_schema.fingerprint("different transcript")))

    def test_building_record_is_not_fresh(self):
        """An in-flight claim is not an index. Serving it would mean retrieving
        against a tree that does not exist yet."""
        self.item[pageindex_store.PAGEINDEX_ATTR]["status"] = "building"
        self.assertFalse(pageindex_store.is_fresh(self.item, self.fp))

    def test_failed_record_is_not_fresh_so_it_can_be_retried(self):
        self.item[pageindex_store.PAGEINDEX_ATTR]["status"] = "failed"
        self.assertFalse(pageindex_store.is_fresh(self.item, self.fp))

    def test_older_index_version_is_stale(self):
        """Changing how nodes are cut invalidates old trees WITHOUT disturbing
        the transcript fingerprint, which must keep meaning only 'the words
        changed'."""
        self.item[pageindex_store.PAGEINDEX_ATTR]["version"] = 0
        self.assertFalse(pageindex_store.is_fresh(self.item, self.fp))

    def test_blank_fingerprint_never_matches(self):
        """'We don't know what this was built from' is not freshness."""
        self.item[pageindex_store.PAGEINDEX_ATTR]["transcript_fingerprint"] = ""
        self.assertFalse(pageindex_store.is_fresh(self.item, ""))

    def test_missing_metadata_is_not_fresh(self):
        self.assertFalse(pageindex_store.is_fresh({}, self.fp))


class ReindexRuleTests(unittest.TestCase):
    """Part 18/19: what must NOT rebuild the index.

    The fingerprint is computed from the TRANSCRIPT TEXT alone, so these are
    properties of the data model rather than of a rule someone has to remember
    to apply. Pinned anyway, because a future change that folded mutable state
    into the fingerprint would be silent and expensive.
    """

    def setUp(self):
        self.segments = make_segments(40)
        self.transcript = transcript_for(self.segments)
        self.item = {
            "transcript": self.transcript,
            "transcript_fingerprint": ai_schema.fingerprint(self.transcript),
            pageindex_store.PAGEINDEX_ATTR: {
                "status": "ready", "s3_key": "k",
                "transcript_fingerprint": ai_schema.fingerprint(self.transcript),
                "version": pageindex.PAGEINDEX_VERSION,
            },
        }

    def _still_fresh(self):
        return pageindex_store.is_fresh(
            self.item, self.item["transcript_fingerprint"])

    def test_task_status_change_does_not_invalidate(self):
        self.item["tasks"] = {"t-1": {"status": "done"}}
        self.assertTrue(self._still_fresh())

    def test_task_assignee_change_does_not_invalidate(self):
        self.item["tasks"] = {"t-1": {"assignee_contact_id": "c-9"}}
        self.assertTrue(self._still_fresh())

    def test_task_deadline_and_priority_changes_do_not_invalidate(self):
        self.item["tasks"] = {"t-1": {"due_date": "2026-09-30",
                                      "priority": "High"}}
        self.assertTrue(self._still_fresh())

    def test_speaker_rename_does_not_invalidate(self):
        """The tree stores speaker IDS; the display name is resolved at read
        time from the row, which is where it is authoritative anyway."""
        self.item["speaker_names"] = {"0": "Ravi", "1": "Priya"}
        self.item["speaker_mapping_version"] = 7
        self.assertTrue(self._still_fresh())

    def test_mom_edit_does_not_invalidate(self):
        """Part 12: the MoM is mutable and is handed to the model separately.
        Rebuilding the TRANSCRIPT index when the minutes change would be both
        expensive and wrong — the transcript did not change."""
        self.item["mom"] = {"sections": [{"title": "Decisions"}]}
        self.assertTrue(self._still_fresh())

    def test_new_chat_turn_does_not_invalidate(self):
        self.item["chat_history"] = [{"role": "user", "content": "hi"}]
        self.assertTrue(self._still_fresh())

    def test_retranscription_does_invalidate(self):
        """The one thing that MUST rebuild."""
        self.item["transcript"] = self.transcript + "\n\nSpeaker 0: One more thing."
        self.assertFalse(pageindex_store.is_fresh(
            self.item, ai_schema.fingerprint(self.item["transcript"])))


class StorageTests(unittest.TestCase):

    def setUp(self):
        self.s3 = mock.MagicMock()
        self.table = mock.MagicMock()
        self.table.get_item.return_value = {"Item": {}}
        self.tree = pageindex.build_tree(make_segments(60))
        self.fp = "fp-abc"

    def test_save_writes_s3_before_dynamodb(self):
        """The two stores are not atomic. An orphaned S3 object is harmless;
        a pointer to an object that was never written is an index that reads as
        ready and then produces nothing."""
        order = []
        self.s3.put_object.side_effect = lambda **kw: order.append("s3")
        self.table.update_item.side_effect = lambda **kw: order.append("ddb")
        pageindex_store.save(self.s3, "bucket", self.table, KEY, self.tree,
                             self.fp, "u-1")
        self.assertEqual(order, ["s3", "ddb"])

    def test_saved_object_round_trips(self):
        pageindex_store.save(self.s3, "bucket", self.table, KEY, self.tree,
                             self.fp, "u-1")
        body = self.s3.put_object.call_args[1]["Body"]
        self.assertEqual(body[:2], b"\x1f\x8b")  # gzipped
        item = {pageindex_store.PAGEINDEX_ATTR: {
            "status": "ready",
            "s3_key": pageindex_store.s3_key_for(KEY),
        }}
        self.s3.get_object.return_value = {"Body": mock.MagicMock(
            read=mock.MagicMock(return_value=body))}
        loaded = pageindex_store.load(self.s3, "bucket", item)
        self.assertEqual(loaded["nodes"], self.tree["nodes"])

    def test_metadata_is_small_and_holds_no_tree(self):
        """The whole reason the tree is not a row attribute: DynamoDB's 400 KB
        item ceiling, which the transcript already had to be moved out of."""
        pageindex_store.save(self.s3, "bucket", self.table, KEY, self.tree,
                             self.fp, "u-1")
        record = self.table.update_item.call_args[1][
            "ExpressionAttributeValues"][":rec"]
        self.assertLess(len(json.dumps(record)), 500)
        self.assertNotIn("nodes", record)
        self.assertEqual(record["s3_key"], pageindex_store.s3_key_for(KEY))
        self.assertEqual(record["owner_user_id"], "u-1")

    def test_index_key_is_distinct_from_the_transcript_key(self):
        """They live in the same bucket; a collision would have one silently
        overwrite the other."""
        self.assertNotEqual(pageindex_store.s3_key_for(KEY),
                            transcript_store.s3_key_for(KEY))

    def test_s3_failure_records_failed_and_returns_none(self):
        self.s3.put_object.side_effect = RuntimeError("s3 down")
        got = pageindex_store.save(self.s3, "bucket", self.table, KEY,
                                   self.tree, self.fp)
        self.assertIsNone(got)
        record = self.table.update_item.call_args[1][
            "ExpressionAttributeValues"][":rec"]
        self.assertEqual(record["status"], "failed")

    def test_load_of_a_corrupt_object_returns_none_not_an_exception(self):
        """A corrupt index must degrade to 'no index' — which still answers the
        user — rather than 500 a request the system can serve."""
        self.s3.get_object.return_value = {"Body": mock.MagicMock(
            read=mock.MagicMock(return_value=b"not gzip, not json"))}
        item = {pageindex_store.PAGEINDEX_ATTR: {"status": "ready", "s3_key": "k"}}
        self.assertIsNone(pageindex_store.load(self.s3, "bucket", item))

    def test_load_without_a_pointer_is_none(self):
        self.assertIsNone(pageindex_store.load(self.s3, "bucket", {}))

    def test_error_text_is_bounded(self):
        """This rides on a row with a 400 KB ceiling; an exception repr can be
        arbitrarily long."""
        pageindex_store.mark_failed(self.table, KEY, self.fp, "x" * 5000)
        record = self.table.update_item.call_args[1][
            "ExpressionAttributeValues"][":rec"]
        self.assertLessEqual(len(record["error"]), 500)


class SingleFlightTests(unittest.TestCase):
    """Part 6: two requests must not build the same index at once."""

    def setUp(self):
        self.table = mock.MagicMock()
        self.fp = "fp-1"

    def test_first_caller_wins_the_claim(self):
        self.table.get_item.return_value = {"Item": {}}
        self.assertTrue(pageindex_store.claim(self.table, KEY, self.fp, "u-1"))

    def test_second_caller_loses_while_a_claim_is_live(self):
        """The loser falls back for that one request rather than duplicating a
        build — which on the LLM-summary path would mean two sets of Groq calls
        racing the same quota."""
        import time as _t
        self.table.get_item.return_value = {"Item": {
            pageindex_store.PAGEINDEX_ATTR: {
                "status": "building", "transcript_fingerprint": self.fp,
                "claimed_at_epoch": int(_t.time()),
            }}}
        self.assertFalse(pageindex_store.claim(self.table, KEY, self.fp, "u-1"))

    def test_an_expired_claim_is_reclaimable(self):
        """A Lambda killed mid-build must not lock the meeting out of ever being
        indexed. A stuck lock costs one wasted rebuild, never the feature."""
        import time as _t
        self.table.get_item.return_value = {"Item": {
            pageindex_store.PAGEINDEX_ATTR: {
                "status": "building", "transcript_fingerprint": self.fp,
                "claimed_at_epoch": int(_t.time())
                - pageindex_store.LOCK_TTL_SECONDS - 60,
            }}}
        self.assertTrue(pageindex_store.claim(self.table, KEY, self.fp, "u-1"))

    def test_a_fresh_index_is_not_reclaimed(self):
        self.table.get_item.return_value = {"Item": {
            pageindex_store.PAGEINDEX_ATTR: {
                "status": "ready", "s3_key": "k",
                "transcript_fingerprint": self.fp,
                "version": pageindex.PAGEINDEX_VERSION,
            }}}
        self.assertFalse(pageindex_store.claim(self.table, KEY, self.fp, "u-1"))

    def test_claim_failure_returns_false_rather_than_raising(self):
        """A caller that cannot claim must degrade, not error."""
        self.table.get_item.return_value = {"Item": {}}
        self.table.update_item.side_effect = RuntimeError("conditional failed")
        self.assertFalse(pageindex_store.claim(self.table, KEY, self.fp, "u-1"))


# ===========================================================================
# The chat endpoint, end to end
# ===========================================================================
class ChatRetrievalTestCase(AiTestCase):
    """Base: a real S3 stub serving a real index for the item under test."""

    def setUp(self):
        super().setUp()
        self.p_s3 = mock.patch.object(api, "_s3")
        self.s3 = self.p_s3.start()
        self.addCleanup(self.p_s3.stop)
        self.p_bucket = mock.patch.object(api, "BUCKET_NAME", "test-bucket")
        self.p_bucket.start()
        self.addCleanup(self.p_bucket.stop)
        self._stored_objects = {}

        def _put(**kw):
            self._stored_objects[kw["Key"]] = kw["Body"]
            return {}

        def _get(**kw):
            if kw["Key"] not in self._stored_objects:
                raise RuntimeError("NoSuchKey")
            return {"Body": mock.MagicMock(read=mock.MagicMock(
                return_value=self._stored_objects[kw["Key"]]))}

        self.s3.put_object.side_effect = _put
        self.s3.get_object.side_effect = _get

    def load_meeting(self, segments, mom=None):
        """Put a meeting of `segments` on the row under test."""
        transcript = transcript_for(segments)
        self.item["transcript"] = transcript
        self.item["timestamps"] = segments
        self.item["transcript_fingerprint"] = ai_schema.fingerprint(transcript)
        self.item["status"] = "complete"
        if mom is not None:
            self.item["mom"] = mom
        return transcript

    def _stamped_index_record(self):
        """The PageIndex pointer this test's writes stamped, if any.

        The table mock records only the LAST update_item, and the chat handler
        writes chat_history after the index pointer — so a test that wants the
        pointer has to look at every call, not at `self.saved`.
        """
        for call_args in self.table.update_item.call_args_list:
            values = call_args[1].get("ExpressionAttributeValues") or {}
            record = values.get(":rec")
            if isinstance(record, dict) and record.get("status") == "ready":
                return record
        return None

    def ask(self, question, reply="It was decided in the final session."):
        """POST the chat route and return (status, body, groq_mock)."""
        groq = mock.patch.object(groq_client, "complete")
        m = groq.start()
        self.addCleanup(groq.stop)
        m.side_effect = self._groq_side_effect(reply)
        ev = event(route="/recordings/ai/chat/{key+}", body={"message": question})
        status, body = parse(call(api.chat, ev))
        return status, body, m

    def _groq_side_effect(self, reply):
        """Answer the navigation call with node ids, the chat call with prose.

        Distinguished by label, the way the real calls differ — so a test
        cannot pass by accident when the two calls are swapped.
        """
        def _fn(*args, **kwargs):
            if kwargs.get("label") == "pageindex-retrieval":
                return json.dumps({"nodes": self.nav_nodes})
            return reply
        self.nav_nodes = getattr(self, "nav_nodes", ["node_0"])
        return _fn


class LargeMeetingTests(ChatRetrievalTestCase):
    """Part 21 — THE acceptance test. A question about the END of a long
    meeting must reach the late transcript, not the head.

    This is the case that was broken: build_context() cut the transcript to its
    first N characters, so the answer to "what did we decide near the end?" was
    never in the model's context at all.
    """

    def _long_meeting(self):
        """~2 hours, with the pricing decision ONLY in the final minutes."""
        segs = make_segments(
            700, text=lambda i: (
                "Final pricing is agreed at four point one lakh, signed off."
                if i >= 690 else
                f"Routine discussion point number {i} about scheduling."))
        return segs

    def test_the_full_transcript_does_not_fit_so_retrieval_engages(self):
        """Guards the premise of every test below: if this meeting fit the
        budget, the retrieval path would never run and they would all pass
        vacuously."""
        segs = self._long_meeting()
        transcript = self.load_meeting(segs)
        budget = api._transcript_budget_chars(prompts.CHAT_SYSTEM)
        self.assertGreater(len(transcript), budget)

    def test_late_content_is_retrievable_and_head_truncation_would_have_missed_it(self):
        """The bug, stated as a test. The pricing decision sits past the point
        where the old head-truncation cut."""
        segs = self._long_meeting()
        transcript = self.load_meeting(segs)
        budget = api._transcript_budget_chars(prompts.CHAT_SYSTEM)

        # What the OLD code would have sent: the first `budget` characters.
        self.assertNotIn("four point one lakh", transcript[:budget])

        # What retrieval sends: the node covering the end.
        tree = pageindex.build_tree(segs)
        last_node = tree["nodes"][-1]["node_id"]
        got = pageindex.segments_for_nodes(tree, segs, [last_node])
        self.assertIn("four point one lakh",
                      pageindex.render_segments(got, {}))

    def test_chat_answers_from_the_end_of_the_meeting(self):
        """End to end through the route: the context handed to Groq contains
        the late decision."""
        segs = self._long_meeting()
        self.load_meeting(segs)
        tree = pageindex.build_tree(segs)
        self.nav_nodes = [tree["nodes"][-1]["node_id"]]

        status, body, groq = self.ask(
            "What did we decide about the final pricing near the end?")
        self.assertEqual(status, 200)
        # The ANSWER call is the one without the retrieval label.
        answer_calls = [c for c in groq.call_args_list
                        if c[1].get("label") == "chat"]
        self.assertEqual(len(answer_calls), 1)
        system = answer_calls[0][0][0]
        self.assertIn("four point one lakh", system)

    def test_context_respects_the_token_budget(self):
        """Part 20: retrieval must not be a way to send unlimited transcript."""
        segs = self._long_meeting()
        self.load_meeting(segs)
        tree = pageindex.build_tree(segs)
        # Ask for EVERY node — the budget, not the navigator, must bound this.
        self.nav_nodes = [n["node_id"] for n in tree["nodes"]]

        status, _, groq = self.ask("Summarise everything.")
        self.assertEqual(status, 200)
        system = [c for c in groq.call_args_list
                  if c[1].get("label") == "chat"][0][0][0]
        budget = api._transcript_budget_chars(prompts.CHAT_SYSTEM)
        self.assertLess(len(system), budget * 2)

    def test_retrieval_returns_sources_with_real_segment_ids(self):
        segs = self._long_meeting()
        self.load_meeting(segs)
        tree = pageindex.build_tree(segs)
        self.nav_nodes = [tree["nodes"][-1]["node_id"]]

        status, body, _ = self.ask("What was decided at the end?")
        self.assertEqual(status, 200)
        self.assertTrue(body["sources"])
        valid = transcript_store.valid_segment_ids(segs)
        for src in body["sources"]:
            self.assertIn(src["segment_id"], valid)

    def test_index_is_built_once_and_reused(self):
        """Part 5: the same transcript must not rebuild on every question.

        The pointer stamped by the first question is reflected back onto the
        row, which is what DynamoDB does for real — the AiTestCase table is a
        mock that records writes rather than applying them, so without this the
        second question would see a row that never learned it had an index and
        the test would prove nothing.
        """
        segs = self._long_meeting()
        self.load_meeting(segs)
        self.nav_nodes = ["node_0"]

        self.ask("First question?")
        self.assertEqual(self.s3.put_object.call_count, 1)

        record = self._stamped_index_record()
        self.assertIsNotNone(record, "no index pointer was stamped")
        self.item[pageindex_store.PAGEINDEX_ATTR] = record

        self.ask("Second question?")
        self.assertEqual(self.s3.put_object.call_count, 1)
        self.assertEqual(self.s3.get_object.call_count, 1)


class ShortMeetingTests(ChatRetrievalTestCase):
    """A meeting that FITS the budget must still be sent whole.

    Retrieval could only subtract there, and answering from the complete record
    is strictly better than answering from a chosen part of it.
    """

    def test_short_meeting_sends_the_whole_transcript_and_skips_navigation(self):
        segs = make_segments(20)
        self.load_meeting(segs)
        status, body, groq = self.ask("What was discussed?")
        self.assertEqual(status, 200)
        # No navigation call at all.
        self.assertEqual(
            [c for c in groq.call_args_list
             if c[1].get("label") == "pageindex-retrieval"], [])
        system = groq.call_args_list[-1][0][0]
        for i in range(20):
            self.assertIn(f"turn number {i} about", system)

    def test_short_meeting_context_is_labelled_so_evidence_still_works(self):
        """Evidence is not a retrieval-only feature: the full-transcript path
        carries segment ids too, so the model can cite on either path."""
        self.load_meeting(make_segments(20))
        status, _, groq = self.ask("What was discussed?")
        self.assertEqual(status, 200)
        self.assertIn("[seg_0]", groq.call_args_list[-1][0][0])

    def test_no_index_is_built_for_a_meeting_that_does_not_need_one(self):
        self.load_meeting(make_segments(20))
        self.ask("What was discussed?")
        self.assertEqual(self.s3.put_object.call_count, 0)


class MomAuthorityTests(ChatRetrievalTestCase):
    """Part 12 — a correction the user made in the minutes must outrank the
    transcript, which still records what was originally SAID."""

    def _meeting_with_corrected_mom(self):
        segs = make_segments(
            20, text=lambda i: ("The client approved ten thousand dollars."
                                if i == 5 else f"Point {i}."))
        self.load_meeting(segs, mom={
            "sections": [{"title": "Decisions",
                          "items": [{"text": "Client approved $8,000."}]}],
        })

    def test_the_edited_mom_reaches_the_model(self):
        self._meeting_with_corrected_mom()
        with mock.patch.object(api.mom_schema, "render_markdown",
                               return_value="## Decisions\n- Client approved $8,000."):
            status, _, groq = self.ask("How much did the client approve?")
        self.assertEqual(status, 200)
        system = groq.call_args_list[-1][0][0]
        self.assertIn("$8,000", system)

    def test_the_mom_is_marked_authoritative_above_the_transcript(self):
        """Both figures are present; what decides the answer is the ORDER and
        the instruction, so both are pinned."""
        self._meeting_with_corrected_mom()
        with mock.patch.object(api.mom_schema, "render_markdown",
                               return_value="## Decisions\n- Client approved $8,000."):
            status, _, groq = self.ask("How much did the client approve?")
        self.assertEqual(status, 200)
        system = groq.call_args_list[-1][0][0]
        self.assertIn("THE MINUTES ARE RIGHT", system)
        self.assertLess(system.index("$8,000"), system.index("ten thousand"))

    def test_a_meeting_without_a_mom_gets_no_mom_rules(self):
        """An instruction about minutes that do not exist is a rule the model
        would reason from anyway."""
        self.load_meeting(make_segments(20))
        status, _, groq = self.ask("What happened?")
        self.assertEqual(status, 200)
        self.assertNotIn("THE MINUTES ARE RIGHT", groq.call_args_list[-1][0][0])

    def test_a_broken_mom_does_not_fail_the_question(self):
        self._meeting_with_corrected_mom()
        with mock.patch.object(api.mom_schema, "render_markdown",
                               side_effect=RuntimeError("bad mom")):
            status, _, _ = self.ask("How much was approved?")
        self.assertEqual(status, 200)


class SourceCitationTests(ChatRetrievalTestCase):
    """The SOURCES line: stripped from the prose, resolved into structure."""

    def setUp(self):
        super().setUp()
        self.segs = make_segments(700)
        self.load_meeting(self.segs)
        tree = pageindex.build_tree(self.segs)
        self.nav_nodes = [tree["nodes"][0]["node_id"]]

    def test_sources_line_is_stripped_from_the_visible_reply(self):
        """It is machinery, not prose. A user seeing 'SOURCES: seg_3' at the
        bottom of an answer would read it as the model malfunctioning."""
        status, body, _ = self.ask("q", reply="They agreed.\n\nSOURCES: seg_3")
        self.assertEqual(status, 200)
        self.assertEqual(body["reply"], "They agreed.")
        self.assertNotIn("SOURCES", body["reply"])

    def test_cited_ids_become_the_sources(self):
        status, body, _ = self.ask("q", reply="They agreed.\n\nSOURCES: seg_3")
        self.assertEqual([s["segment_id"] for s in body["sources"]], ["seg_3"])

    def test_ids_outside_the_retrieved_set_are_discarded(self):
        """A source row that scrolled to a moment the answer never saw would be
        worse than no source row — it looks like a citation and is not one."""
        status, body, _ = self.ask(
            "q", reply="They agreed.\n\nSOURCES: seg_3, seg_99999")
        self.assertEqual([s["segment_id"] for s in body["sources"]], ["seg_3"])

    def test_sources_none_yields_no_sources(self):
        status, body, _ = self.ask(
            "q", reply="That was not discussed.\n\nSOURCES: none")
        self.assertEqual(body["sources"], [])
        self.assertEqual(body["reply"], "That was not discussed.")

    def test_a_missing_sources_line_falls_back_to_what_was_retrieved(self):
        """The answer DID come from those segments; a working 'view in
        transcript' beats a missing one."""
        status, body, _ = self.ask("q", reply="They agreed.")
        self.assertEqual(status, 200)
        self.assertTrue(body["sources"])

    def test_sources_are_persisted_on_the_stored_turn(self):
        """So reopening the screen restores tappable sources rather than an
        answer whose evidence evaporated."""
        status, body, _ = self.ask("q", reply="Agreed.\n\nSOURCES: seg_3")
        self.assertEqual(status, 200)
        stored = body["chat_history"][-1]
        self.assertEqual(stored["role"], "assistant")
        self.assertEqual([s["segment_id"] for s in stored["sources"]], ["seg_3"])

    def test_persisted_timestamps_are_decimal_not_float(self):
        """boto3's resource layer raises 'Float types are not supported'
        outright — this would 500 the request at the very last write, AFTER
        Groq had already been paid for the answer."""
        self.ask("q", reply="Agreed.\n\nSOURCES: seg_3")
        turns = self.saved["ExpressionAttributeValues"][":h"]
        for src in turns[-1]["sources"]:
            self.assertIsInstance(src["start_time"], Decimal)
            self.assertIsInstance(src["end_time"], Decimal)

    def test_a_reply_that_is_only_a_sources_line_is_an_error_not_a_blank_bubble(self):
        status, body, _ = self.ask("q", reply="SOURCES: seg_3")
        self.assertEqual(status, 502)


class FailureTests(ChatRetrievalTestCase):
    """Part 23 — an index is an optimisation over a fallback that works. No
    failure in it may 500 a question or strand a meeting."""

    def setUp(self):
        super().setUp()
        self.segs = make_segments(700)
        self.load_meeting(self.segs)

    def test_a_missing_index_does_not_500_it_is_built_lazily(self):
        """Part 7/23: an old meeting with no index must answer, not error."""
        self.assertNotIn(pageindex_store.PAGEINDEX_ATTR, self.item)
        status, body, _ = self.ask("What was decided?")
        self.assertEqual(status, 200)
        self.assertTrue(body["reply"])
        self.assertEqual(self.s3.put_object.call_count, 1)

    def test_an_s3_write_failure_still_answers_the_question(self):
        self.s3.put_object.side_effect = RuntimeError("s3 down")
        status, body, _ = self.ask("What was decided?")
        self.assertEqual(status, 200)
        self.assertTrue(body["reply"])

    def test_a_lost_claim_falls_back_rather_than_failing(self):
        """The request that loses single-flight must still answer."""
        with mock.patch.object(pageindex_store, "claim", return_value=False):
            status, body, _ = self.ask("What was decided?")
        self.assertEqual(status, 200)
        self.assertTrue(body["reply"])

    def test_a_navigation_failure_spreads_across_the_meeting(self):
        """Degrading to the FIRST nodes would reproduce the head-truncation bug
        this module exists to remove, so the fallback spreads instead."""
        tree = pageindex.build_tree(self.segs)
        node_ids, mode = api._navigate(
            tree, "q", self.item,
            __import__("time").monotonic() + 5)
        # With Groq unstubbed here the call raises, so this is the spread path.
        self.assertEqual(mode, "spread")
        self.assertGreater(len(node_ids), 1)
        positions = [int(n.split("_")[1]) for n in node_ids]
        self.assertGreater(max(positions), len(tree["nodes"]) / 2)

    def test_a_corrupt_stored_index_is_rebuilt_rather_than_trusted(self):
        self.item[pageindex_store.PAGEINDEX_ATTR] = {
            "status": "ready", "s3_key": "pageindex/gone.json.gz",
            "transcript_fingerprint": self.item["transcript_fingerprint"],
            "version": pageindex.PAGEINDEX_VERSION,
        }
        status, body, _ = self.ask("What was decided?")
        self.assertEqual(status, 200)
        self.assertEqual(self.s3.put_object.call_count, 1)

    def test_no_transcript_is_still_a_409_not_a_500(self):
        """Unchanged behaviour: the app renders 409 as its progress state."""
        self.item["transcript"] = ""
        self.item["timestamps"] = []
        self.item["status"] = "transcribing"
        status, _, _ = self.ask("What was decided?")
        self.assertEqual(status, 409)


class AuthorizationTests(ChatRetrievalTestCase):
    """Part 22 — retrieval must not be a way around ownership."""

    def test_another_users_meeting_is_404_and_no_index_is_touched(self):
        """404 not 403, matching every other AI route, so the endpoint cannot
        be used to probe for the existence of other users' recordings."""
        self.load_meeting(make_segments(700))
        self.item["user_id"] = "someone-else"
        status, body, _ = self.ask("What was decided?")
        self.assertEqual(status, 404)
        self.assertEqual(self.s3.get_object.call_count, 0)
        self.assertEqual(self.s3.put_object.call_count, 0)

    def test_a_missing_recording_is_404(self):
        self.item = None
        status, _, _ = self.ask("What was decided?")
        self.assertEqual(status, 404)

    def test_retrieval_takes_an_authorized_item_not_a_bare_key(self):
        """The structural guarantee: there is no code path that resolves an
        index from a meeting id alone, so authorization cannot be forgotten —
        it has already happened by the time an item exists to pass in."""
        import inspect
        params = list(inspect.signature(
            api.retrieve_meeting_context).parameters)
        self.assertEqual(params[0], "item")


class MetricsTests(ChatRetrievalTestCase):
    """Part 20/25 — instrumentation, without leaking meeting content."""

    def test_a_metrics_line_is_emitted_with_the_required_fields(self):
        self.load_meeting(make_segments(700))
        with mock.patch("builtins.print") as p:
            status, _, _ = self.ask("What was decided?")
        self.assertEqual(status, 200)
        lines = [c[0][0] for c in p.call_args_list
                 if isinstance(c[0][0], str) and c[0][0].startswith("[ai_metrics]")]
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0][len("[ai_metrics] "):])
        for field in ("meeting_id", "index_status", "transcript_fingerprint",
                      "retrieval_mode", "retrieved_segments", "retrieval_ms",
                      "est_context_tokens", "est_input_tokens",
                      "est_output_tokens", "est_total_tokens",
                      "llm_ms", "total_ms"):
            self.assertIn(field, payload)

    def test_metrics_never_contain_transcript_content(self):
        """These logs are retained and broadly readable. A meeting's contents
        must not leak into them."""
        self.load_meeting(make_segments(
            700, text=lambda i: f"SECRET-CONTENT-{i} was discussed."))
        with mock.patch("builtins.print") as p:
            self.ask("What about SECRET-CONTENT-5?",
                     reply="SECRET-CONTENT-5 was mentioned.")
        lines = [c[0][0] for c in p.call_args_list
                 if isinstance(c[0][0], str) and c[0][0].startswith("[ai_metrics]")]
        self.assertNotIn("SECRET-CONTENT", lines[0])

    def test_mode_distinguishes_the_full_and_retrieval_paths(self):
        self.load_meeting(make_segments(20))
        with mock.patch("builtins.print") as p:
            self.ask("q")
        line = [c[0][0] for c in p.call_args_list
                if isinstance(c[0][0], str) and c[0][0].startswith("[ai_metrics]")][0]
        self.assertEqual(json.loads(line[len("[ai_metrics] "):])["retrieval_mode"],
                         "full")


# ===========================================================================
# Task integration — Part 16/17/18
# ===========================================================================
class TaskIntegrationTests(unittest.TestCase):
    """Tasks stay DB-authoritative; PageIndex only supplies HISTORY."""

    def test_task_evidence_uses_the_same_segment_ids_retrieval_returns(self):
        """Part 17: a task links to its source meeting through segment ids, and
        those are the same ids retrieval produces — so 'why was this task
        created?' and a chat answer land on the same transcript moment through
        ONE navigation mechanism, not two."""
        segs = make_segments(100)
        tree = pageindex.build_tree(segs)
        retrieved = pageindex.segments_for_nodes(tree, segs, ["node_0"])
        retrieval_ids = {s["id"] for s in retrieved}
        task_valid_ids = transcript_store.valid_segment_ids(segs)
        self.assertTrue(retrieval_ids)
        self.assertTrue(retrieval_ids.issubset(task_valid_ids))

    def test_the_index_holds_no_mutable_task_state(self):
        """Part 16: status/assignee/deadline/priority live in DynamoDB. Copying
        them into the index would create a second, silently-stale answer to
        'what is the status of this task?'."""
        tree = pageindex.build_tree(make_segments(60))
        blob = json.dumps(tree)
        for field in ("status", "assignee", "due_date", "priority", "task_id"):
            self.assertNotIn(f'"{field}"', blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)
