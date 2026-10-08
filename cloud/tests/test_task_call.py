#!/usr/bin/env python3
"""test_task_call.py — the SEPARATE task call (transcribe's extract_tasks).

Tasks used to be one field of the single analysis call. They are now a second
call, fed the analysis that call just wrote (in English) ahead of the
transcript — see prompts.TASKS_SYSTEM for the measurement behind it. These
tests pin the parts a refactor could quietly break:

  * what the task call SENDS: the analysis digest first, the transcript second,
    the tasks prompt with the speaker roster, its own reduce and deadline;
  * how the pipeline USES it: tasks come only from this call, a failed task
    call costs the tasks and never the meeting, and a failed analysis skips it;
  * the coercion the call's reply goes through.

MUST RUN UNDER PYTEST: conftest.py installs the boto3/botocore stubs before
the Lambda is imported (same constraint as test_eager_seeding_e2e.py).

Run:  python -m pytest tests/test_task_call.py -q
"""
import importlib.util as _ilu
import json
import sys
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT / "shared", ROOT / "functions/userapi",
          Path(__file__).resolve().parent):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
from test_ai_workspace import RECORDING  # noqa: E402
import ai_schema  # noqa: E402
import prompts  # noqa: E402

_spec = _ilu.spec_from_file_location(
    "transcribe_lambda_function",
    str(ROOT / "functions/transcribe" / "lambda_function.py"))
tr = sys.modules.get("transcribe_lambda_function")
if tr is None:
    tr = _ilu.module_from_spec(_spec)
    sys.modules["transcribe_lambda_function"] = tr
    _spec.loader.exec_module(tr)

KEY = "recordings/u-1/mobile/meeting-A_1787738400.m4a"
TRANSCRIPT = "Speaker 0: I'll send the proposal tomorrow.\nSpeaker 1: Okay."
LABELLED = ("[seg_0] Speaker 0: I'll send the proposal tomorrow.\n"
            "[seg_1] Speaker 1: Okay.")
ANALYSIS = {
    "title": "Proposal Follow-up",
    "overview": {"sections": [{"title": "Proposal", "kind": "text",
                               "content": "Speaker 0 will send the proposal "
                                          "tomorrow.", "items": [],
                               "evidence_segment_ids": []}]},
    "participants": [{"speaker": "Speaker 0", "summary": "owns the proposal"}],
    "tasks": [],
    "meeting_highlights": {"decisions": [], "open_questions": [],
                           "deadlines": [{"what": "Proposal",
                                          "when": "tomorrow"}]},
}
TASK = {"task": "Send the proposal", "assignee": "",
        "assignee_speaker_id": "0", "due_date": "tomorrow",
        "confidence": "high", "evidence": "I'll send the proposal tomorrow.",
        "evidence_segment_ids": ["seg_0"]}


# ---------------------------------------------------------------------------
# 1. What the task call sends
# ---------------------------------------------------------------------------
def _run_extract(reply=None):
    seen = {}

    def fake_analyze(text, **kw):
        seen["text"] = text
        seen.update(kw)
        return (reply if reply is not None else {"tasks": [TASK]}), 1, 1

    with mock.patch.object(tr.groq_client, "analyze", side_effect=fake_analyze):
        tasks = tr.extract_tasks(LABELLED, ANALYSIS, {"seg_0", "seg_1"},
                                 roster_source=TRANSCRIPT,
                                 speaker_names={"0": "Ravi"},
                                 language="mar", duration=600)
    return tasks, seen


def test_the_analysis_goes_ahead_of_the_transcript():
    _, seen = _run_extract()
    text = seen["text"]
    assert text.index("=== MEETING ANALYSIS") < text.index("=== TRANSCRIPT ===")
    # The English overview the first call wrote is what the model reads first.
    assert "Speaker 0 will send the proposal tomorrow." in text
    assert "Proposal Follow-up" in text
    # The transcript arrives verbatim, segment ids included.
    assert LABELLED in text


def test_it_uses_the_task_prompts_and_its_own_deadline():
    _, seen = _run_extract()
    assert seen["map_prompt"].startswith(prompts.TASKS_SYSTEM)
    # The speaker roster rides along so assignee_speaker_id can be answered.
    assert 'speaker_id "0" = Ravi' in seen["map_prompt"]
    assert seen["reduce_prompt"] == prompts.TASKS_REDUCE_SYSTEM
    assert seen["deadline_seconds"] == tr.GROQ_TASKS_DEADLINE_SECONDS
    # An empty task list is a valid answer, not a failure to retry.
    assert seen.get("is_usable") is None


def test_it_returns_the_coerced_tasks():
    tasks, seen = _run_extract()
    assert [t["task"] for t in tasks] == ["Send the proposal"]
    # The call's coerce is the task coercer, validating ids against the
    # transcript: an invented segment id is dropped, the task is kept.
    got = seen["coerce"]({"tasks": [dict(TASK, evidence_segment_ids=[
        "seg_0", "seg_99"])]})
    assert got["tasks"][0]["evidence_segment_ids"] == ["seg_0"]


def test_no_tasks_is_an_empty_list():
    tasks, _ = _run_extract(reply={"tasks": []})
    assert tasks == []


# ---------------------------------------------------------------------------
# 2. How the pipeline uses it
# ---------------------------------------------------------------------------
def _persist(analysis, extract=None, extract_error=None):
    """Run the real analyze_and_persist with Groq and AWS stubbed out.
    Returns (outcome, the merged row fields, the extract_tasks mock)."""
    item = json.loads(json.dumps(RECORDING))
    item.pop("ai_tasks", None)
    written = {}

    def apply_upsert(k, fields, remove=()):
        written.update(fields)

    ext = mock.MagicMock(return_value=extract if extract is not None else [])
    if extract_error is not None:
        ext.side_effect = extract_error
    with mock.patch.object(tr, "_table") as table, \
         mock.patch.object(tr, "analyze_meeting", return_value=analysis), \
         mock.patch.object(tr, "extract_tasks", ext), \
         mock.patch.object(tr, "transcript_store") as store, \
         mock.patch.object(tr, "_upsert", side_effect=apply_upsert), \
         mock.patch.object(tr, "_notify_processing_outcome"), \
         mock.patch.object(tr, "_lambda_client"):
        table.get_item.return_value = {"Item": item}
        store.with_segment_ids.return_value = []
        store.put.return_value = {}
        store.as_labelled_lines.return_value = LABELLED
        store.valid_segment_ids.return_value = {"seg_0", "seg_1"}
        out = tr.analyze_and_persist("bucket", KEY, TRANSCRIPT, [], "en")
    return out, written, ext


def test_tasks_come_from_the_task_call():
    out, written, ext = _persist(dict(ANALYSIS), extract=[TASK])
    assert out["status"] == "complete"
    assert ext.called
    assert [t["task"] for t in written["ai_tasks"]] == ["Send the proposal"]
    # It was handed the labelled transcript and the analysis just produced.
    args, kwargs = ext.call_args
    assert args[0] == LABELLED
    assert args[1]["title"] == "Proposal Follow-up"


def test_a_failed_task_call_keeps_the_meeting():
    """The overview the user is waiting for is already in hand — a task-call
    failure must cost the tasks, never the meeting's status."""
    out, written, _ = _persist(dict(ANALYSIS),
                               extract_error=RuntimeError("groq 503"))
    assert out["status"] == "complete"
    assert written["ai_tasks"] == []
    assert written["overview"]["sections"]


def test_a_failed_analysis_skips_the_task_call():
    """The task call reads the analysis; without one there is nothing to read,
    and a 'transcribed' row is repaired by a reprocess as a whole."""
    empty = dict(ANALYSIS, overview={"sections": []})
    out, written, ext = _persist(empty, extract=[TASK])
    assert out["status"] == "transcribed"
    assert not ext.called
    assert written.get("ai_tasks", []) == []


# ---------------------------------------------------------------------------
# 3. Coercion of the task call's reply
# ---------------------------------------------------------------------------
def test_coerce_task_reply_shapes():
    assert ai_schema.coerce_task_reply(None) == {"tasks": []}
    assert ai_schema.coerce_task_reply({"tasks": "nope"}) == {"tasks": []}
    got = ai_schema.coerce_task_reply(
        {"tasks": [{"task": ""}, {"assignee": "x"}, TASK]})
    assert [t["task"] for t in got["tasks"]] == ["Send the proposal"]


def test_merge_tasks_keeps_the_fuller_copy():
    """Overflow path: a later chunk's due date fills an earlier bare mention
    of the same task instead of being thrown away."""
    merged = ai_schema.merge_tasks([
        {"tasks": [{"task": "Send the proposal", "due_date": ""}]},
        {"tasks": [{"task": "send the proposal", "due_date": "Friday"}]},
        None,
    ])
    assert len(merged["tasks"]) == 1
    assert merged["tasks"][0]["due_date"] == "Friday"


def test_the_analysis_no_longer_produces_tasks():
    """coerce_analysis still accepts a reply carrying tasks (older callers),
    but the current analysis reply has none, and yields []."""
    got = ai_schema.coerce_unified({"title": "t", "overview": {"sections": []}})
    assert got["tasks"] == []
