#!/usr/bin/env python3
"""test_site_contract.py — the Salesforce Site delivery contract (pure).

Pins: the opaque meeting id, the 131,072-character transcript rule (counted
the way Salesforce counts), the payload shape and what it must NEVER carry,
idempotency-key semantics, body encoding, audio-link tokens, and People
normalization. No AWS, no network.

Run:  python -m pytest tests/test_site_contract.py
"""
import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))

import site_contract as sc  # noqa: E402

KEY = "recordings/u-owner/mobile/mobile-abc_1757000000.wav"


def item(**overrides):
    row = {
        "audio_s3_key": KEY, "user_id": "u-owner", "workspace_id": "wso_x",
        "device_id": "dev-1", "title": "Quarterly review", "status": "complete",
        "source": "mobile", "language": "en", "recorded_at": "1757000000",
        "duration": Decimal("3600"), "transcript": "Speaker 0: hello",
        "summary": "We agreed.",
        "overview": {"sections": [{"id": "s1", "title": "Plan", "kind": "list",
                                   "items": ["ship"]}]},
        "meeting_highlights": {"decisions": [{"decision": "Ship it",
                                              "speaker_id": "0"}],
                               "deadlines": [], "open_questions": []},
        "participants": [{"speaker": "0", "summary": "Led the call"}],
        "speaker_names": {"0": "Asha"},
        "crm_records": {"x": "must never ship"},
        "chat_history": ["must never ship"],
    }
    row.update(overrides)
    return row


TASK = {"task_id": "t1", "title": "Send deck", "status": "Open",
        "assignee_name": "Asha", "assignee_email": "asha@example.com",
        "assignee_user_id": "u-internal", "assignee_contact_id": "c-internal"}


class OpaqueIdTests(unittest.TestCase):
    def test_stable_opaque_and_prefixed(self):
        a = sc.opaque_meeting_id(KEY)
        self.assertEqual(a, sc.opaque_meeting_id(KEY))
        self.assertTrue(a.startswith("mxm_"))
        self.assertEqual(len(a), 36)
        self.assertNotIn("u-owner", a)
        self.assertNotEqual(a, sc.opaque_meeting_id(KEY + "x"))
        self.assertEqual(sc.opaque_meeting_id(""), "")

    def test_matches_receiver_id_pattern(self):
        import re
        self.assertRegex(sc.opaque_meeting_id(KEY), re.compile(r"^[A-Za-z0-9_:.\-]{1,255}$"))


class TranscriptLimitTests(unittest.TestCase):
    def test_at_limit_is_sent_whole(self):
        text = "a" * sc.MAX_TRANSCRIPT_CHARS
        p = sc.build_meeting_payload(item(transcript=text))
        self.assertEqual(p["transcript"]["text"], text)
        self.assertEqual(p["transcript"]["chars"], 131072)

    def test_one_over_limit_is_refused_not_truncated(self):
        with self.assertRaises(sc.TranscriptTooLarge) as ctx:
            sc.build_meeting_payload(item(transcript="a" * (sc.MAX_TRANSCRIPT_CHARS + 1)))
        self.assertEqual(ctx.exception.code, "transcript_too_large")
        self.assertEqual(ctx.exception.chars, 131073)
        self.assertIn("131,072", ctx.exception.message)

    def test_counts_utf16_units_like_salesforce(self):
        # 65,536 emoji = 131,072 UTF-16 units: exactly at the limit.
        self.assertEqual(sc.text_length("\U0001F600" * 65536), 131072)
        sc.build_meeting_payload(item(transcript="\U0001F600" * 65536))
        # One more BMP character tips it over, though len() says 65,537.
        with self.assertRaises(sc.TranscriptTooLarge):
            sc.build_meeting_payload(item(transcript="\U0001F600" * 65536 + "a"))

    def test_devanagari_counts_one_per_character(self):
        self.assertEqual(sc.text_length("नमस्ते"), len("नमस्ते"))


class PayloadShapeTests(unittest.TestCase):
    def setUp(self):
        self.p = sc.build_meeting_payload(
            item(), [TASK],
            workspace={"id": "wso_x", "name": "ABC Realty"},
            meeting_url="https://api.example.com/m/1",
            audio=sc.audio_block("https://api.example.com/delivery/audio/t",
                                 size_bytes=85_000_000, duration_seconds=3600,
                                 expires_at="2026-10-01T00:00:00Z"))

    def test_top_level_sections(self):
        self.assertEqual(set(self.p), {"meeting", "participants", "summary",
                                       "action_items", "transcript", "audio", "relation"})
        self.assertEqual(self.p["relation"], {"who": None, "what": None},
                         "no relationship unless one is passed")
        self.assertEqual(set(self.p["summary"]),
                         {"text", "overview", "highlights", "decisions"})

    def test_meeting_block(self):
        m = self.p["meeting"]
        self.assertEqual(m["id"], sc.opaque_meeting_id(KEY))
        self.assertEqual(m["title"], "Quarterly review")
        self.assertEqual(m["started_at"], "2025-09-04T15:33:20Z")
        self.assertEqual(m["ended_at"], "2025-09-04T16:33:20Z")
        self.assertEqual(m["duration_seconds"], 3600)
        self.assertEqual(m["workspace"], {"id": "wso_x", "name": "ABC Realty"})

    def test_summary_reuses_existing_shaping(self):
        s = self.p["summary"]
        self.assertEqual(s["text"], "We agreed.")
        self.assertEqual(s["decisions"][0]["decision"], "Ship it")
        self.assertIn("Plan:", s["overview"]["text"])

    def test_audio_is_a_url_never_bytes(self):
        a = self.p["audio"]
        self.assertEqual(a["size_bytes"], 85_000_000)
        self.assertEqual(a["content_type"], "audio/wav")
        self.assertTrue(a["url"].startswith("https://"))
        self.assertTrue(a["available"])

    def test_never_carries_internal_or_destination_ids(self):
        body = json.dumps(self.p)
        for forbidden in ("u-owner", "dev-1", "recordings/", "crm_records",
                          "must never ship", "salesforce_", "__c",
                          "u-internal", "c-internal", "minutex_user_id",
                          "minutex_contact_id", "audio_s3_key"):
            self.assertNotIn(forbidden, body, forbidden)

    def test_participants_fall_back_to_ai_speakers(self):
        self.assertEqual(self.p["participants"][0]["speaker_id"], "0")
        self.assertEqual(self.p["participants"][0]["name"], "Asha")
        self.assertIsNone(self.p["participants"][0]["external_identity"])

    def test_explicit_participants_are_allow_listed(self):
        p = sc.build_meeting_payload(item(), participants=[{
            "speaker_id": "0", "name": "Asha", "email": "ASHA@EXAMPLE.COM",
            "contact_id": "secret-contact", "owner_user_id": "u-owner",
            "external_identity": {"provider": "salesforce", "type": "contact",
                                  "external_id": "003AAA"}}])
        part = p["participants"][0]
        self.assertEqual(part["email"], "asha@example.com")
        self.assertEqual(part["external_identity"]["external_id"], "003AAA")
        self.assertNotIn("contact_id", part)
        self.assertNotIn("owner_user_id", part)

    def test_personal_meeting_has_empty_workspace(self):
        p = sc.build_meeting_payload(item())
        self.assertEqual(p["meeting"]["workspace"], {"id": "", "name": ""})
        self.assertFalse(p["audio"]["available"])

    def test_missing_key_is_a_contract_error(self):
        with self.assertRaises(sc.ContractError):
            sc.build_meeting_payload(item(audio_s3_key=""))


class EnvelopeTests(unittest.TestCase):
    def test_idempotency_key_is_per_intent_not_per_attempt(self):
        self.assertEqual(sc.meeting_idempotency_key("j1"),
                         sc.meeting_idempotency_key("j1", 0))
        self.assertNotEqual(sc.meeting_idempotency_key("j1", 0),
                            sc.meeting_idempotency_key("j1", 1))
        self.assertNotEqual(sc.meeting_idempotency_key("j1"),
                            sc.meeting_idempotency_key("j2"))
        self.assertLessEqual(len(sc.meeting_idempotency_key("f" * 32, 99)), 255)

    def test_request_ids_are_unique_and_receiver_safe(self):
        ids = {sc.new_request_id() for _ in range(100)}
        self.assertEqual(len(ids), 100)
        self.assertTrue(all(len(i) <= 64 and i.isalnum() for i in ids))

    def test_envelope_shape(self):
        env = sc.envelope(sc.ACTION_UPSERT_MEETING, {"a": 1}, request_id="r1",
                          idempotency_key="k1", sent_at="2026-01-01T00:00:00Z")
        self.assertEqual(env["contract_version"], sc.CONTRACT_VERSION)
        self.assertEqual(env["idempotency_key"], "k1")
        self.assertNotIn("idempotency_key",
                         sc.envelope(sc.ACTION_HEALTH, request_id="r"))
        with self.assertRaises(sc.ContractError):
            sc.envelope("drop_tables", request_id="r")

    def test_encode_body_turns_decimal_into_numbers(self):
        body = json.loads(sc.encode_body({"a": Decimal("3600"), "b": Decimal("1.5")}))
        self.assertEqual(body, {"a": 3600, "b": 1.5})

    def test_encode_body_is_utf8_not_ascii_escaped(self):
        self.assertIn("नमस्ते".encode("utf-8"), sc.encode_body({"t": "नमस्ते"}))


class AudioTokenTests(unittest.TestCase):
    def test_round_trip(self):
        tok = sc.sign_audio_token("job123", 2_000_000_000, "k" * 43)
        self.assertEqual(sc.read_audio_token(tok), {"j": "job123", "e": 2_000_000_000})
        self.assertTrue(sc.verify_audio_token(tok, "k" * 43))

    def test_token_reveals_no_recording_key_or_user(self):
        import base64
        tok = sc.sign_audio_token("0f" * 16, 2_000_000_000, "key")
        decoded = base64.urlsafe_b64decode(tok.split(".")[1] + "==").decode()
        self.assertNotIn("recordings", decoded)
        self.assertEqual(set(json.loads(decoded)), {"j", "e"})

    def test_wrong_key_and_tampering_fail(self):
        tok = sc.sign_audio_token("job-a", 2_000_000_000, "key-a")
        self.assertFalse(sc.verify_audio_token(tok, "key-b"))
        other = sc.sign_audio_token("job-b", 2_000_000_000, "key-a")
        forged = ".".join([tok.split(".")[0], other.split(".")[1], tok.split(".")[2]])
        self.assertFalse(sc.verify_audio_token(forged, "key-a"))
        self.assertFalse(sc.verify_audio_token(tok, ""))

    def test_malformed_tokens_read_as_none(self):
        for bad in ("", "v1", "v2.a.b", "v1.@@@.x", "v1." + "a" * 3000 + ".x"):
            self.assertIsNone(sc.read_audio_token(bad), bad)


class PeopleTests(unittest.TestCase):
    def test_normalizes_and_keys(self):
        p = sc.normalize_person({"provider": "salesforce", "type": "contact",
                                 "external_id": "003AAA", "name": " Asha ",
                                 "email": "ASHA@X.COM", "status": "weird"})
        self.assertEqual(p["person_key"], "salesforce#contact#003AAA")
        self.assertEqual(p["email"], "asha@x.com")
        self.assertEqual(p["name"], "Asha")
        self.assertEqual(p["status"], "active")

    def test_builds_name_from_parts_and_drops_bad_email(self):
        p = sc.normalize_person({"provider": "salesforce", "type": "user",
                                 "external_id": "005A", "first_name": "Ravi",
                                 "last_name": "K", "email": "not-an-email",
                                 "status": "inactive"})
        self.assertEqual(p["name"], "Ravi K")
        self.assertEqual(p["email"], "")
        self.assertEqual(p["status"], "inactive")

    def test_rejects_unstorable_rows(self):
        for bad in (None, "x", {}, {"provider": "salesforce", "type": "account",
                                    "external_id": "1", "name": "n"},
                    {"provider": "Sales Force", "type": "contact",
                     "external_id": "1", "name": "n"},
                    {"provider": "salesforce", "type": "contact",
                     "external_id": "has space", "name": "n"},
                    {"provider": "salesforce", "type": "contact",
                     "external_id": "003", "name": "", "email": ""}):
            self.assertIsNone(sc.normalize_person(bad), bad)

    def test_leads_are_people(self):
        # Live people search returns Leads alongside Contacts and Users.
        p = sc.normalize_person({"provider": "salesforce", "type": "lead",
                                 "external_id": "00Q000000000000001", "name": "Rahul"})
        self.assertEqual(p["person_key"], "salesforce#lead#00Q000000000000001")


if __name__ == "__main__":
    unittest.main()
