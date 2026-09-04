#!/usr/bin/env python3
"""test_task_extraction.py — the AI task contract and spoken-deadline dates.

WHAT THIS PINS, and why these two things live in one file.

Both halves protect the same chain, which was broken in two places at once:

    transcript -> LLM -> coerce_analysis -> _seed_ai_tasks -> Task row
                              ^                    ^
                        (1) fields dropped   (2) date uncomparable

(1) THE CONTRACT. `_seed_ai_tasks` reads `assignee_speaker_id`, `confidence`
    and `evidence` off each raw AI row, and `_resolve_tasks_for_speaker` joins
    on the first of those to attach a real person once the user says who each
    speaker is. But `TASK_SPEC` did not list them, and `obj_list` builds every
    element STRICTLY from that spec — so all three were silently stripped
    before the seeder ever ran. Both ends of the feature were implemented and
    the middle threw the data away. Nothing failed loudly; tasks just never
    resolved to anyone.

(2) THE DEADLINE. prompts.py deliberately forbids the model from resolving a
    spoken date, so `due_date` holds "Friday" or "tomorrow" verbatim.
    `_is_overdue` parses ISO and returns False for anything else, so those
    tasks could never be overdue, never matched `due_before`, and would never
    fire a notification. spoken_dates.py resolves them against the MEETING's
    date into `due_date_normalized`, keeping the spoken text intact.

The refusals matter as much as the resolutions here. A wrong assignee mails
the wrong person; a wrong date mails them on the wrong day. Both are worse
than an empty field, so the tests below assert what the system must DECLINE to
guess just as hard as what it must get right.

Run:  python tests/test_task_extraction.py
      python -m pytest tests/test_task_extraction.py
"""
import json
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "functions/userapi"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import fake_dynamodb as fdb  # noqa: E402

# Reuse the installed stub module when present (conftest.py under pytest) so
# every file shares ONE ClientError class; install our own only standalone.
_botocore_exc = sys.modules.get("botocore.exceptions") or mock.MagicMock()
_botocore_exc.ClientError = fdb.ClientError
sys.modules.setdefault("boto3", mock.MagicMock())
sys.modules.setdefault("boto3.dynamodb", mock.MagicMock())
_conditions = sys.modules.get("boto3.dynamodb.conditions") or mock.MagicMock()
_conditions.Key = fdb.Key
sys.modules["boto3.dynamodb.conditions"] = _conditions
sys.modules.setdefault("botocore", mock.MagicMock())
sys.modules["botocore.exceptions"] = _botocore_exc
sys.modules.setdefault("botocore.config", mock.MagicMock())

import ai_schema  # noqa: E402
import spoken_dates  # noqa: E402
import lambda_function as api  # noqa: E402

USER = "u-1"
KEY = "recordings/u-1/mobile/mobile-abc_1754300000.m4a"

# 25 Aug 2026 is a TUESDAY. Every relative expectation below is hand-checked
# against that, so a change to the anchor breaks the tests loudly rather than
# quietly shifting what "Friday" means.
MEETING_DAY = date(2026, 8, 25)
MEETING_ISO = "2026-08-25T09:15:00Z"

TRANSCRIPT = (
    "Speaker 0: I'll send the proposal tomorrow.\n\n"
    "Speaker 1: Great, and I'll review it before the client call."
)

BASE_RECORDING = {
    "audio_s3_key": KEY,
    "user_id": USER,
    "title": "Client Alpha kickoff",
    "created_at": MEETING_ISO,
    "recorded_at": MEETING_ISO,
    "status": "complete",
    "transcript": TRANSCRIPT,
    "ai_tasks": [],
}


def parse(resp):
    return resp["statusCode"], json.loads(resp["body"])


def event(method="GET", route="/contacts", body=None, path=None, qs=None,
          key=None):
    ev = {
        "routeKey": f"{method} {route}",
        "requestContext": {"http": {"method": method, "path": route}},
        "headers": {"authorization": "Bearer test-token"},
        "pathParameters": dict(path or {}),
        "queryStringParameters": dict(qs or {}),
    }
    if key is not None:
        ev["pathParameters"]["key"] = key
    if body is not None:
        ev["body"] = json.dumps(body)
    return ev


def call(fn, ev):
    return fn(ev)


# ===========================================================================
# 1. THE COERCION CONTRACT — what survives obj_list
# ===========================================================================
class TestTaskContract(unittest.TestCase):
    """coerce_analysis must PRESERVE every field _seed_ai_tasks reads."""

    def coerce_one(self, task):
        out = ai_schema.coerce_analysis({"tasks": [task]})
        self.assertEqual(len(out["tasks"]), 1)
        return out["tasks"][0]

    def test_self_commitment_keeps_speaker_id_evidence_confidence(self):
        """"I'll send the proposal tomorrow." — the whole point of the fix.

        The speaker owes the work, so the transcript label is the join key
        that later resolves to a real person.
        """
        got = self.coerce_one({
            "task": "Send the proposal",
            "assignee": "",
            "assignee_speaker_id": "Speaker 0",
            "due_date": "tomorrow",
            "priority": "High",
            "confidence": "high",
            "evidence": "I'll send the proposal tomorrow.",
        })
        self.assertEqual(got["assignee_speaker_id"], "Speaker 0")
        self.assertEqual(got["confidence"], "high")
        self.assertEqual(got["evidence"], "I'll send the proposal tomorrow.")
        self.assertEqual(got["due_date"], "tomorrow")
        self.assertEqual(got["priority"], "High")

    def test_explicit_assignment_keeps_name_without_speaker_id(self):
        """"Rahul, send the proposal." — the SPEAKER is assigning, not owing.

        Binding the speaker's label here would assign the work to whoever was
        talking, which is the exact wrong person.
        """
        got = self.coerce_one({
            "task": "Send the proposal",
            "assignee": "Rahul",
            "assignee_speaker_id": "",
            "due_date": "Friday",
            "confidence": "high",
            "evidence": "Rahul, send the proposal by Friday.",
        })
        self.assertEqual(got["assignee"], "Rahul")
        self.assertEqual(got["assignee_speaker_id"], "")

    def test_ambiguous_assignment_survives_as_low_confidence(self):
        """Ambiguity is DATA, not an error — it must reach the row so the UI
        can mark it, rather than being flattened into a confident guess."""
        got = self.coerce_one({
            "task": "Follow up with the vendor",
            "assignee": "",
            "assignee_speaker_id": "",
            "due_date": "",
            "confidence": "low",
            "evidence": "Someone should follow up with the vendor.",
        })
        self.assertEqual(got["confidence"], "low")
        self.assertEqual(got["assignee"], "")
        self.assertEqual(got["assignee_speaker_id"], "")

    def test_confidence_is_an_enum_not_a_number(self):
        """A model that returns 0.92 must not have it stored as confidence.

        A free-form score is noise dressed as precision; anything outside the
        three allowed values degrades to "" (no claim made).
        """
        for bad in (0.92, "very high", None, "certain", 1, True):
            with self.subTest(bad=bad):
                self.assertEqual(
                    self.coerce_one({"task": "x", "confidence": bad})[
                        "confidence"], "")
        for good in ("high", "medium", "low"):
            with self.subTest(good=good):
                self.assertEqual(
                    self.coerce_one({"task": "x", "confidence": good})[
                        "confidence"], good)

    def test_confidence_casing_is_the_same_claim(self):
        """"HIGH" and " High " mean what "high" means.

        These used to degrade to "" alongside the genuine noise above, which
        threw away a real signal: a task whose confidence silently became ""
        is indistinguishable from one the model never scored, and the
        assignment gate would then treat a high-confidence extraction as
        unscored. Casing is a formatting difference, not a different claim —
        unlike 0.92, which is a DIFFERENT KIND of answer and still degrades.
        """
        for variant in ("HIGH", " High ", "Medium", "LOW"):
            with self.subTest(variant=variant):
                self.assertEqual(
                    self.coerce_one({"task": "x", "confidence": variant})[
                        "confidence"], variant.strip().lower())

    def test_missing_new_fields_default_to_empty(self):
        """An older model response (four fields) must still coerce cleanly —
        the new keys are present and empty, never absent."""
        got = self.coerce_one({"task": "Send proposal", "assignee": "Rahul",
                               "due_date": "Friday", "priority": "High"})
        for field in ("assignee_speaker_id", "confidence", "evidence"):
            self.assertIn(field, got)
            self.assertEqual(got[field], "")

    def test_dedupe_folds_the_new_fields(self):
        """The same commitment said twice is ONE task, and the fuller copy
        must win for the new fields too — otherwise a duplicate mention could
        drop the speaker id that makes the task resolvable."""
        out = ai_schema.coerce_analysis({"tasks": [
            {"task": "Send proposal"},
            {"task": "send proposal", "assignee_speaker_id": "Speaker 0",
             "confidence": "high", "evidence": "I'll send the proposal."},
        ]})
        self.assertEqual(len(out["tasks"]), 1)
        self.assertEqual(out["tasks"][0]["assignee_speaker_id"], "Speaker 0")
        self.assertEqual(out["tasks"][0]["confidence"], "high")
        self.assertEqual(out["tasks"][0]["evidence"],
                         "I'll send the proposal.")

    def test_unknown_fields_are_still_dropped(self):
        """Widening the spec must not turn it into a pass-through: a field
        nobody declared still never reaches DynamoDB."""
        got = self.coerce_one({"task": "x", "sentiment": "positive",
                               "assignee_email": "a@b.c"})
        self.assertNotIn("sentiment", got)
        self.assertNotIn("assignee_email", got)


# ===========================================================================
# 2. THE PROMPT — the contract is only real if the model is asked for it
# ===========================================================================
class TestPromptContract(unittest.TestCase):
    """The schema in the prompt and TASK_SPEC must not drift apart."""

    def test_single_pass_prompt_requests_every_spec_field(self):
        import prompts
        for field in ai_schema.TASK_SPEC:
            with self.subTest(field=field):
                self.assertIn(field, prompts.SUMMARY_SYSTEM)

    def test_reduce_prompt_requests_every_spec_field(self):
        """The map-reduce OVERFLOW path returns tasks too; a schema that
        listed fewer fields there would silently drop them on long
        meetings — the exact class of bug this file exists to prevent."""
        import prompts
        for field in ai_schema.TASK_SPEC:
            with self.subTest(field=field):
                self.assertIn(field, prompts.SUMMARY_REDUCE_SYSTEM)

    def test_prompt_states_the_speaker_id_rules(self):
        import prompts
        text = prompts.SUMMARY_SYSTEM
        self.assertIn("SPEAKER-ID, EVIDENCE AND CONFIDENCE", text)
        # The three cases that decide who gets assigned the work.
        self.assertIn("Self-commitment", text)
        self.assertIn("Explicit assignment", text)
        self.assertIn("General discussion", text)


# ===========================================================================
# 3. SPOKEN DATES — resolution AND refusal
# ===========================================================================
class TestSpokenDates(unittest.TestCase):
    """Ported from app/lib/spoken-dates.ts; the two must agree."""

    def r(self, phrase):
        return spoken_dates.resolve_spoken_date(phrase, MEETING_DAY)

    def test_relative_days(self):
        for phrase, expected in [
            ("today", "2026-08-25"),
            ("tomorrow", "2026-08-26"),
            ("day after tomorrow", "2026-08-27"),
            ("in 3 days", "2026-08-28"),
            ("in 2 weeks", "2026-09-08"),
        ]:
            with self.subTest(phrase=phrase):
                key, conf = self.r(phrase)
                self.assertEqual(key, expected)
                self.assertEqual(conf, "relative")

    def test_day_after_tomorrow_is_not_swallowed_by_tomorrow(self):
        """The longer phrase CONTAINS the shorter one. Matching "tomorrow"
        first silently loses a day — a whole day of someone's deadline."""
        self.assertEqual(self.r("day after tomorrow")[0], "2026-08-27")
        self.assertNotEqual(self.r("day after tomorrow")[0],
                            self.r("tomorrow")[0])

    def test_weekdays_anchor_forward_from_the_meeting(self):
        # Meeting is Tuesday 25 Aug 2026.
        for phrase, expected in [
            ("Friday", "2026-08-28"),      # later this week
            ("Monday", "2026-08-31"),      # already passed -> next one
            ("this Thursday", "2026-08-27"),
            ("next Tuesday", "2026-09-01"),
        ]:
            with self.subTest(phrase=phrase):
                self.assertEqual(self.r(phrase)[0], expected)

    def test_explicit_dates_are_exact(self):
        # Meeting is 25 Aug 2026. A bare month+day anchors FORWARD, the same
        # way a bare weekday does above: March is long past by August, so a
        # deadline of "15th March" is next March. An explicitly spoken year
        # is always taken at its word.
        for phrase, expected in [
            ("15th March", "2027-03-15"),
            ("March 15", "2027-03-15"),
            ("2026-03-15", "2026-03-15"),
            ("15 March 2027", "2027-03-15"),
            ("15th December", "2026-12-15"),   # still ahead — this year
        ]:
            with self.subTest(phrase=phrase):
                key, conf = self.r(phrase)
                self.assertEqual(key, expected)
                self.assertEqual(conf, "exact")

    def test_month_day_rolls_into_next_year_rather_than_the_past(self):
        """A December meeting agreeing "5th January" means the January three
        weeks out, not the one eleven months gone.

        Defaulting the year to the anchor's produced a deadline in the PAST
        stamped "exact" — the highest confidence — so the task was overdue the
        moment it was created and could fire an overdue notification for work
        nobody had started. The near past is deliberately left alone: "the
        15th" said on the 17th is a deadline just missed, not one 11.5 months
        away.
        """
        dec = date(2026, 12, 20)
        self.assertEqual(
            spoken_dates.resolve_spoken_date("5th January", dec)[0],
            "2027-01-05")
        # Just-missed stays in the anchor's year.
        self.assertEqual(
            spoken_dates.resolve_spoken_date("15th December", dec)[0],
            "2026-12-15")
        # An explicit year is never second-guessed, even into the past.
        self.assertEqual(
            spoken_dates.resolve_spoken_date("5th January 2026", dec)[0],
            "2026-01-05")

    def test_end_of_week_and_month(self):
        self.assertEqual(self.r("end of the week")[0], "2026-08-28")
        self.assertEqual(self.r("end of the month")[0], "2026-08-31")

    def test_month_end_handles_leap_years(self):
        self.assertEqual(
            spoken_dates.resolve_spoken_date(
                "end of the month", date(2028, 2, 3))[0], "2028-02-29")
        self.assertEqual(
            spoken_dates.resolve_spoken_date(
                "end of the month", date(2027, 2, 3))[0], "2027-02-28")

    def test_vague_horizons_are_refused(self):
        """These are REAL deadlines the user should see — and none is a day.
        Pinning them to a date nobody said is the failure mode this whole
        module exists to avoid."""
        for phrase in ("end of Q3", "soon", "shortly", "in a few weeks",
                       "before the holidays", "next quarter", "later"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.r(phrase), ("", "none"))

    def test_hedged_phrases_are_refused(self):
        """"sometime around the 15th" is a hope, not a deadline — however
        date-like the rest of the string looks."""
        for phrase in ("sometime around the 15th", "maybe Friday",
                       "possibly next Tuesday", "hopefully tomorrow"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.r(phrase), ("", "none"))

    def test_impossible_dates_are_refused(self):
        """"31st February" must not roll over into March."""
        for phrase in ("31st February", "30th February", "32nd March"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.r(phrase)[0], "")

    def test_ambiguous_numeric_dates_are_refused(self):
        """"3/4" is March 4th to an American and April 3rd to everyone else.
        A 50% chance of being a month wrong is not worth having."""
        for phrase in ("3/4", "04/03/2026"):
            with self.subTest(phrase=phrase):
                self.assertEqual(self.r(phrase)[0], "")

    def test_empty_and_missing_anchor(self):
        self.assertEqual(self.r(""), ("", "none"))
        self.assertEqual(
            spoken_dates.resolve_spoken_date("tomorrow", None), ("", "none"))
        self.assertEqual(spoken_dates.normalize_spoken_date("tomorrow", None),
                         "")

    def test_anchor_date_accepts_both_stored_shapes(self):
        """`recorded_at` is written as an epoch string by the upload path, but
        ISO timestamps are live in the table too. Guessing one shape would
        resolve every date against the wrong day."""
        self.assertEqual(
            spoken_dates.anchor_date("2026-08-25T09:15:00Z"), MEETING_DAY)
        epoch = str(int(datetime(2026, 8, 25, 9, 15,
                                 tzinfo=timezone.utc).timestamp()))
        self.assertEqual(spoken_dates.anchor_date(epoch), MEETING_DAY)
        # Falls back to created_at, then gives up rather than using "now".
        self.assertEqual(spoken_dates.anchor_date("", MEETING_ISO),
                         MEETING_DAY)
        self.assertIsNone(spoken_dates.anchor_date("", ""))
        self.assertIsNone(spoken_dates.anchor_date("not a date", ""))


# ===========================================================================
# 4. END TO END — the chain the audit found broken, start to finish
# ===========================================================================
class TestSeedingChain(unittest.TestCase):
    """AI output -> Task row -> speaker mapping -> a named, dated task."""

    def setUp(self):
        self.t = fdb.build_tables()
        self.patches = [
            mock.patch.object(api, "_recordings", self.t["recordings"]),
            mock.patch.object(api, "_contacts", self.t["contacts"]),
            mock.patch.object(api, "_folders", self.t["folders"]),
            mock.patch.object(api, "_folder_contacts",
                              self.t["folder_contacts"]),
            mock.patch.object(api, "_meeting_participants",
                              self.t["participants"]),
            mock.patch.object(api, "_tasks", self.t["tasks"]),
            mock.patch.object(api, "_users", self.t["users"]),
            mock.patch.object(api, "_require_auth", return_value=USER),
            mock.patch.object(api, "_owned_devices", return_value=[]),
            mock.patch.object(api.transcript_store, "hydrate",
                              side_effect=lambda s3, bucket, item: item),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])
        self.t["recordings"].put_item(
            Item=json.loads(json.dumps(BASE_RECORDING)))

    def set_ai_tasks(self, tasks):
        self.t["recordings"].items[(KEY,)]["ai_tasks"] = tasks

    def seed(self):
        return parse(call(api.list_meeting_tasks, event(
            "GET", "/recordings/ai/tasks/{key+}", key=KEY)))

    def mk_contact(self, name="Rahul Sharma", email="rahul@company.com"):
        status, body = parse(call(api.create_contact, event(
            "POST", "/contacts", body={"name": name, "email": email})))
        self.assertEqual(status, 201)
        return body["contact"]["id"]

    # -- the scenario from the plan, verified end to end -------------------
    def test_self_commitment_resolves_to_a_person_and_a_date(self):
        """Meeting 25 Aug 2026. "Speaker 0: I'll send the proposal tomorrow."

        Once the user says Speaker 0 is Rahul Sharma, the task must name him
        AND be due 26 Aug — the two halves of this file meeting in one row.
        """
        self.set_ai_tasks([{
            "task": "Send the proposal",
            "assignee": "",
            "assignee_speaker_id": "Speaker 0",
            "due_date": "tomorrow",
            "priority": "High",
            "confidence": "high",
            "evidence": "I'll send the proposal tomorrow.",
        }])

        status, body = self.seed()
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 1)
        task = body["tasks"][0]

        # The AI named no person, only a speaker. resolution_status is NONE
        # rather than UNRESOLVED by design: UNRESOLVED means "a bare NAME we
        # kept verbatim to resolve later" (see _new_task_row), and there is no
        # name here. The speaker id is what carries the identity forward.
        self.assertEqual(task["resolution_status"], "NONE")
        # STORED NORMALIZED. The AI copies the transcript's own "Speaker 0"
        # label, but everything that joins on a speaker — participant rows, the
        # speaker_names display map, _resolve_tasks_for_speaker — keys on the
        # compact "0". Storing the raw label is what made this task unmatchable
        # in production, where participant rows really do hold "0".
        self.assertEqual(task["assignee_speaker_id"], "0")

        row = self.t["tasks"].items[(task["id"],)]
        self.assertEqual(row["ai_confidence"], "high")
        self.assertEqual(row["ai_evidence"], "I'll send the proposal tomorrow.")
        # The spoken text is preserved; the resolved day sits beside it.
        self.assertEqual(row["due_date"], "tomorrow")
        self.assertEqual(row["due_date_normalized"], "2026-08-26")

        # Now the user maps Speaker 0 -> Rahul Sharma.
        cid = self.mk_contact()
        status, _ = parse(call(api.set_participant, event(
            "PUT", "/recordings/participants/{key+}", key=KEY,
            # The app sends the COMPACT id, exactly as the participants
            # screen does — using "Speaker 0" on both sides is what previously
            # hid the production mismatch this normalization fixes.
            body={"speaker_id": "0", "contact_id": cid})))
        self.assertEqual(status, 200)

        _, after = self.seed()
        resolved = after["tasks"][0]
        self.assertEqual(resolved["resolution_status"], "RESOLVED")
        self.assertEqual(resolved["assignee"]["name"], "Rahul Sharma")
        self.assertEqual(resolved["assignee"]["email"], "rahul@company.com")

    def test_speaker_already_mapped_resolves_at_seed_time(self):
        """When the mapping exists FIRST, seeding must resolve immediately
        rather than leaving the task unresolved until something re-runs."""
        cid = self.mk_contact()
        parse(call(api.set_participant, event(
            "PUT", "/recordings/participants/{key+}", key=KEY,
            # The app sends the COMPACT id, exactly as the participants
            # screen does — using "Speaker 0" on both sides is what previously
            # hid the production mismatch this normalization fixes.
            body={"speaker_id": "0", "contact_id": cid})))
        self.set_ai_tasks([{
            "task": "Send the proposal",
            "assignee_speaker_id": "Speaker 0",
            "due_date": "Friday",
            "confidence": "high",
            "evidence": "I'll send the proposal on Friday.",
        }])
        _, body = self.seed()
        task = body["tasks"][0]
        self.assertEqual(task["resolution_status"], "RESOLVED")
        self.assertEqual(task["assignee"]["name"], "Rahul Sharma")
        # Tuesday's meeting -> that same week's Friday.
        self.assertEqual(
            self.t["tasks"].items[(task["id"],)]["due_date_normalized"],
            "2026-08-28")

    def test_explicit_assignment_stays_a_name(self):
        """"Rahul, send the proposal." names a person but no speaker, so the
        task is UNRESOLVED and keeps the name verbatim — never guessed into
        a contact."""
        self.set_ai_tasks([{
            "task": "Send the proposal",
            "assignee": "Rahul",
            "assignee_speaker_id": "",
            "due_date": "Friday",
            "confidence": "high",
            "evidence": "Rahul, send the proposal by Friday.",
        }])
        _, body = self.seed()
        task = body["tasks"][0]
        self.assertEqual(task["resolution_status"], "UNRESOLVED")
        self.assertEqual(task["assignee_name_legacy"], "Rahul")
        self.assertEqual(task["assignee_contact_id"], "")

    def test_unplaceable_deadline_is_stored_but_not_invented(self):
        """"end of Q3" is a real deadline with no day. The spoken text must
        survive for the user to read; the normalized field stays empty rather
        than pinning a date nobody said."""
        self.set_ai_tasks([{"task": "Close the quarter plan",
                            "due_date": "end of Q3", "confidence": "medium"}])
        _, body = self.seed()
        row = self.t["tasks"].items[(body["tasks"][0]["id"],)]
        self.assertEqual(row["due_date"], "end of Q3")
        self.assertEqual(row["due_date_normalized"], "")

    def test_dates_anchor_to_the_meeting_not_to_today(self):
        """The whole reason resolution happens server-side with an anchor.

        Anchoring to "now" would re-point an old meeting's deadline every
        time the row was read.
        """
        old_day = "2026-01-06T09:00:00Z"   # a Tuesday
        self.t["recordings"].items[(KEY,)]["recorded_at"] = old_day
        self.t["recordings"].items[(KEY,)]["created_at"] = old_day
        self.set_ai_tasks([{"task": "Send the proposal",
                            "due_date": "tomorrow"}])
        _, body = self.seed()
        row = self.t["tasks"].items[(body["tasks"][0]["id"],)]
        self.assertEqual(row["due_date_normalized"], "2026-01-07")


# ===========================================================================
# 5. THE PAYOFF — a spoken deadline is finally comparable
# ===========================================================================
class TestOverdueUsesNormalized(unittest.TestCase):
    """_is_overdue could never fire on a spoken date before this."""

    def test_spoken_date_can_now_be_overdue(self):
        past = (datetime.now(timezone.utc) - timedelta(days=3)).date()
        # The raw phrase alone is still uncomparable...
        self.assertFalse(api._is_overdue("Friday", "Open"))
        # ...but with the resolved day it behaves like any real deadline.
        self.assertTrue(api._is_overdue("Friday", "Open", past.isoformat()))

    def test_future_normalized_date_is_not_overdue(self):
        future = (datetime.now(timezone.utc) + timedelta(days=3)).date()
        self.assertFalse(
            api._is_overdue("Friday", "Open", future.isoformat()))

    def test_completed_task_is_never_overdue(self):
        past = (datetime.now(timezone.utc) - timedelta(days=3)).date()
        self.assertFalse(
            api._is_overdue("Friday", "Completed", past.isoformat()))

    def test_unresolvable_phrase_is_still_not_claimed(self):
        """"end of Q3" resolves to "", so it must remain not-overdue rather
        than becoming overdue-by-default."""
        self.assertFalse(api._is_overdue("end of Q3", "Open", ""))

    def test_legacy_rows_without_the_new_field_are_unchanged(self):
        """Backward compatibility: a row written before normalization existed
        has no normalized value and must behave exactly as it always did."""
        self.assertTrue(api._is_overdue("2020-01-01", "Open"))
        self.assertFalse(api._is_overdue("2099-01-01", "Open"))
        self.assertFalse(api._is_overdue("", "Open"))
        self.assertFalse(api._is_overdue("next Friday", "Open"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
