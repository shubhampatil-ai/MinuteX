#!/usr/bin/env python3
"""test_webhook_contract.py — Webhook Contract v2 shapes (pure).

Pins: event_id derivation, the snapshot (v1 payload minus relation /
destination, plus lifecycle and minutes), section hashing (audio excluded),
changed_fields, the Contact-vs-record separation in the envelope, and
response validation for configuration, record search and people.

Run:  python -m pytest tests/test_webhook_contract.py
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))

import site_contract as sc  # noqa: E402
import webhook_contract as wc  # noqa: E402

KEY = "recordings/u1/mobile/m_1757000000.wav"
ITEM = {"audio_s3_key": KEY, "title": "Site walk", "status": "complete",
        "transcript": "Speaker 0: The lobby needs paint.", "summary": "Paint lobby.",
        "created_at": "2026-09-20T10:00:00Z"}
JOHN = {"speaker_id": "0", "name": "John Smith", "email": "john@example.com",
        "tagged": True, "external_identity": {"provider": "salesforce",
                                              "type": "contact",
                                              "external_id": "003000000000000001"}}


def snapshot(**kw):
    kw.setdefault("participants", [JOHN])
    return wc.build_snapshot(ITEM, [], **kw)


class EventIdTests(unittest.TestCase):
    def test_deterministic_per_meeting_and_sequence(self):
        mid = sc.opaque_meeting_id(KEY)
        self.assertEqual(wc.event_id(mid, 7), wc.event_id(mid, 7))
        self.assertNotEqual(wc.event_id(mid, 7), wc.event_id(mid, 8))
        self.assertNotEqual(wc.event_id(mid, 7), wc.event_id(mid + "x", 7))
        self.assertRegex(wc.event_id(mid, 1), r"^evt_[0-9a-f]{32}$")

    def test_sequence_must_be_positive(self):
        with self.assertRaises(wc.ContractError):
            wc.event_id("mxm_x", 0)


class SnapshotTests(unittest.TestCase):
    def test_v1_payload_minus_v1_keys_plus_lifecycle_and_minutes(self):
        data = snapshot(minutes_markdown="## Decisions\n- Paint", lifecycle="trashed")
        for v1_only in ("relation", "destination", "acting_user_id"):
            self.assertNotIn(v1_only, data)
        self.assertEqual(data["meeting"]["id"], sc.opaque_meeting_id(KEY))
        self.assertEqual(data["meeting"]["lifecycle"], "trashed")
        self.assertEqual(data["minutes"], {"available": True, "truncated": False,
                                           "markdown": "## Decisions\n- Paint"})

    def test_transcript_is_whole_or_refused(self):
        with self.assertRaises(sc.TranscriptTooLarge):
            wc.build_snapshot({**ITEM, "transcript": "x" * (sc.MAX_TRANSCRIPT_CHARS + 1)})

    def test_minutes_over_the_cap_are_marked_truncated(self):
        data = snapshot(minutes_markdown="m" * (wc.MAX_MINUTES_CHARS + 5))
        self.assertTrue(data["minutes"]["truncated"])
        self.assertEqual(len(data["minutes"]["markdown"]), wc.MAX_MINUTES_CHARS)

    def test_contact_and_record_travel_separately(self):
        """docs §3.1: the tagged Contact is in participants, the business
        record in record_context — and either can be absent."""
        ctx = {"meeting_type_id": "site_visit", "meeting_type_name": "Site Visit",
               "record_id": "a0P123", "record_display_name": "PV-2026-00452 — Mumbai Heights"}
        env = wc.event_envelope(
            wc.EVENT_CREATED, meeting_id="mxm_1", sequence=1, occurred_at="t",
            meeting_type=wc.meeting_type_block(ctx),
            record_context=wc.record_context_block(ctx), data=snapshot())
        self.assertEqual(env["record_context"]["record_id"], "a0P123")
        ident = env["data"]["participants"][0]["external_identity"]
        self.assertEqual(ident, {"provider": "salesforce", "type": "contact",
                                 "external_id": "003000000000000001"})
        self.assertNotIn("a0P123", str(env["data"]["participants"]))
        cleared = {**ctx, "record_id": "", "record_display_name": ""}
        self.assertIsNone(wc.record_context_block(cleared))
        self.assertEqual(wc.meeting_type_block(cleared)["id"], "site_visit")
        self.assertIsNone(wc.meeting_type_block({}))

    def test_envelope_carries_no_provider_or_configuration_version(self):
        env = wc.event_envelope(wc.EVENT_UPDATED, meeting_id="mxm_1", sequence=2,
                                occurred_at="t", data=snapshot())
        self.assertNotIn("provider", env)
        self.assertNotIn("configuration_version", env)
        self.assertEqual(env["type"], "event")
        self.assertEqual(env["contract_version"], "2.0")
        with self.assertRaises(wc.ContractError):
            wc.event_envelope("meeting.exploded", meeting_id="m", sequence=1,
                              occurred_at="t")


class HashTests(unittest.TestCase):
    def test_audio_is_excluded_from_hashes(self):
        a = snapshot(audio=sc.audio_block("https://api/x?t=1", expires_at="2026-10-01"))
        b = snapshot(audio=sc.audio_block("https://api/x?t=2", expires_at="2026-10-02"))
        self.assertEqual(wc.section_hashes(a), wc.section_hashes(b))
        self.assertEqual(wc.content_hash(wc.section_hashes(a)),
                         wc.content_hash(wc.section_hashes(b)))

    def test_changed_fields_names_the_sections(self):
        before = wc.section_hashes(snapshot(minutes_markdown="v1"))
        after = wc.section_hashes(snapshot(minutes_markdown="v2", lifecycle="trashed"))
        self.assertEqual(wc.changed_fields(before, after), ["lifecycle", "minutes"])
        self.assertEqual(wc.changed_fields(before, before), [])
        self.assertEqual(wc.changed_fields({}, after), ["*"])

    def test_context_changes_are_their_own_sections(self):
        data = snapshot()
        base = wc.section_hashes(data, {"id": "site_visit", "name": "Site Visit"},
                                 {"record_id": "a0P123", "display_name": "x",
                                  "secondary_text": ""})
        cleared = wc.section_hashes(data, {"id": "site_visit", "name": "Site Visit"}, None)
        self.assertEqual(wc.changed_fields(base, cleared), ["record_context"])

    def test_content_hash_distinguishes_snapshots(self):
        a = wc.content_hash(wc.section_hashes(snapshot(minutes_markdown="A")))
        b = wc.content_hash(wc.section_hashes(snapshot(minutes_markdown="B")))
        self.assertNotEqual(a, b)


class ConfigurationTests(unittest.TestCase):
    def ok(self, **over):
        cfg = {"configuration_version": "17", "meeting_types": [
            {"id": "site_visit", "name": "Site Visit", "extra": "dropped",
             "record_selection": {"enabled": True, "required": True, "label": "Property"}},
            {"id": "internal_meeting", "name": "Internal Meeting"}]}
        cfg.update(over)
        return cfg

    def test_normalizes_and_drops_unknown_keys(self):
        out = wc.validate_configuration(self.ok())
        self.assertEqual(out["configuration_version"], "17")
        self.assertEqual(out["meeting_types"][0]["record_selection"],
                         {"enabled": True, "required": True, "label": "Property",
                          "placeholder": ""})
        self.assertNotIn("extra", out["meeting_types"][0])
        self.assertEqual(out["meeting_types"][1]["record_selection"]["enabled"], False)

    def test_configuration_version_is_required(self):
        with self.assertRaises(wc.ContractError):
            wc.validate_configuration(self.ok(configuration_version=""))
        with self.assertRaises(wc.ContractError):
            wc.validate_configuration(self.ok(configuration_version="x" * 65))

    def test_rejections(self):
        bad = [
            {"meeting_types": "nope"},
            {"meeting_types": [{"id": "bad id", "name": "X"}]},
            {"meeting_types": [{"id": "a", "name": "X"}, {"id": "a", "name": "Y"}]},
            {"meeting_types": [{"id": "a", "name": ""}]},
            {"meeting_types": [{"id": "a", "name": "n" * 81}]},
            {"meeting_types": [{"id": "a", "name": "X",
                                "record_selection": {"enabled": False, "required": True}}]},
            {"meeting_types": [{"id": f"t{i}", "name": "X"} for i in range(51)]},
        ]
        for over in bad:
            with self.subTest(over=str(over)[:60]):
                with self.assertRaises(wc.ContractError):
                    wc.validate_configuration(self.ok(**over))
        with self.assertRaises(wc.ContractError):
            wc.validate_configuration([])


class SearchAndPeopleTests(unittest.TestCase):
    def test_search_skips_unusable_rows(self):
        out = wc.validate_search_response({"records": [
            {"id": "a0P123", "display_name": "PV-2026-00452 — Mumbai Heights"},
            {"id": "bad id", "display_name": "x"},
            {"id": "a0P125", "display_name": ""},
            "not a dict"], "next_cursor": "c2"})
        self.assertEqual([r["id"] for r in out["records"]], ["a0P123"])
        self.assertEqual(out["next_cursor"], "c2")

    def test_search_limits(self):
        with self.assertRaises(wc.ContractError):
            wc.validate_search_response({"records": [
                {"id": f"a{i}", "display_name": "x"} for i in range(51)]})
        with self.assertRaises(wc.ContractError):
            wc.validate_search_response({})
        with self.assertRaises(wc.ContractError):
            wc.search_request(request_id="r", meeting_type_id="site_visit", query="q" * 101)
        req = wc.search_request(request_id="r", meeting_type_id="site_visit",
                                query="PV-2026", limit=999)
        self.assertEqual(req["limit"], wc.MAX_SEARCH_LIMIT)
        self.assertEqual(req["type"], "records.search")

    def test_people_page_shape(self):
        people, cursor, done = wc.validate_people_page(
            {"people": [{"provider": "salesforce", "type": "contact",
                         "external_id": "003A", "name": "John"}, "junk"],
             "next_cursor": "200", "done": False})
        self.assertEqual(len(people), 1)
        self.assertEqual((cursor, done), ("200", False))
        self.assertEqual(wc.validate_people_page({"people": []})[2], True)
        with self.assertRaises(wc.ContractError):
            wc.validate_people_page({"people": {}})

    def test_request_builders_carry_their_type(self):
        self.assertEqual(wc.configuration_request(request_id="r")["type"],
                         "configuration.request")
        self.assertEqual(wc.people_request(request_id="r", page_size=10_000)["page_size"],
                         wc.MAX_PEOPLE_PAGE_SIZE)


class LivePeopleContractTests(unittest.TestCase):
    def test_search_request(self):
        env = wc.people_search_request(request_id="r", query=" rah ", user_email="A@B.com")
        self.assertEqual((env["type"], env["query"], env["user_email"]),
                         ("people.search", "rah", "a@b.com"))
        self.assertEqual(env["types"], ["contact", "lead", "user"])
        with self.assertRaises(wc.ContractError):
            wc.people_search_request(request_id="r", query="r")

    def test_related_request_and_response(self):
        env = wc.people_related_request(request_id="r", record_ids=["a0P1", "a0P1", "bad id"])
        self.assertEqual(env["record_ids"], ["a0P1"])
        with self.assertRaises(wc.ContractError):
            wc.people_related_request(request_id="r", record_ids=[])
        people = wc.validate_people_related_response({"people": [
            {"provider": "salesforce", "type": "contact", "external_id": "0031",
             "name": "Priya", "relation": "Contact at Sharma Family"},
            {"provider": "salesforce", "type": "account", "external_id": "0011", "name": "X"}]})
        self.assertEqual([(p["name"], p["relation"]) for p in people],
                         [("Priya", "Contact at Sharma Family")])


if __name__ == "__main__":
    unittest.main()
