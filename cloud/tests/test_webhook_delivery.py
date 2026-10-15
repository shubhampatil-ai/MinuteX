#!/usr/bin/env python3
"""test_webhook_delivery.py — Webhook Contract v2, end to end (offline).

    route -> CrmSyncJobs -> SQS (with DelaySeconds) -> crmSyncWorker
          -> execute_site_delivery_job -> _run_webhook_event_job
          -> WebhookClient -> FakeWebhookReceiver

Everything above the network is the REAL code, including the worker
Lambda's own handler. Time is a fake clock the queue honours, so debounce
windows, backoffs and the secret-rotation grace can be walked exactly.

Pins the invariants the plan names: the race cases, a sequence never bound
to two snapshots, fresh-message retries that never touch the DLQ, FAILED
never reopened automatically, Contact-vs-record independence, the v1/v2
guards, People Sync over v2, and that every write site marks the meeting.

Run:  python -m pytest tests/test_webhook_delivery.py
"""
import base64
import io
import json
import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "functions/userapi"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from salesforce_harness import (  # noqa: E402
    KEY, MEMBER, NOW, ORG, OUTSIDER, OWNER, SalesforceHarness, call, event, worker)
import site_contract as sc  # noqa: E402
import webhook_client as whc  # noqa: E402
import webhook_contract as wc  # noqa: E402
from fake_webhook_receiver import FakeWebhookReceiver, contact, lead, user  # noqa: E402

api = worker.userapi
BASE = "https://hooks.acme.com/services/apexrest/minutex/v2"
KEY2 = "recordings/u-own/mobile/mobile-site_1757000999.wav"
T0 = 1_790_000_000.0
JOHN_KEY = "salesforce#contact#003000000000000001"
MUMBAI = {"id": "a0P123", "display_name": "PV-2026-00452 — Mumbai Heights",
          "secondary_text": "Property · Pune"}


class WebhookHarness(SalesforceHarness):
    def setUp(self):
        super().setUp()
        self.now = T0
        self.q = []            # (due, n, body)
        self.n = 0
        self.batch_failures = []
        self.rx = FakeWebhookReceiver(
            clock=lambda: self.now,
            people=[contact("003000000000000001", "John Smith", "john@example.com")])
        self.sqs.send_message.side_effect = self._send
        patches = [
            mock.patch.object(api, "WEBHOOK_ENABLED", True),
            mock.patch.object(api, "_webhook_clock", lambda: self.now),
            mock.patch.object(api, "_webhook_client",
                              lambda: whc.WebhookClient(self.rx, clock=lambda: self.now)),
            mock.patch.object(api, "_authoritative_mom",
                              side_effect=lambda item: item.get("minutes_md", "")),
            mock.patch.object(api, "_delete_meeting_grants", lambda key: None),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

    # -- queue + clock -----------------------------------------------------
    def _send(self, **kw):
        self.n += 1
        self.q.append((self.now + int(kw.get("DelaySeconds") or 0), self.n,
                       kw["MessageBody"]))
        return {"MessageId": f"m{self.n}"}

    def pump(self, seconds=None, max_rounds=500):
        """Deliver due messages through the REAL worker, advancing the clock to
        each next due time (up to `seconds` from now, or until idle)."""
        deadline = None if seconds is None else self.now + seconds
        for _ in range(max_rounds):
            if not self.q:
                break
            self.q.sort(key=lambda m: (m[0], m[1]))
            due = self.q[0][0]
            if due > self.now:
                if deadline is not None and due > deadline:
                    break
                self.now = due
            ready = [m for m in self.q if m[0] <= self.now]
            self.q = [m for m in self.q if m[0] > self.now]
            records = [{"messageId": f"r{m[1]}", "receiptHandle": "rh", "body": m[2]}
                       for m in ready]
            out = worker.lambda_handler({"Records": records}, None)
            self.batch_failures += out["batchItemFailures"]
            failed = {f["itemIdentifier"] for f in out["batchItemFailures"]}
            for m in ready:
                if f"r{m[1]}" in failed:
                    self.q.append((self.now + 300, m[1], m[2]))
        if deadline is not None and self.now < deadline:
            self.now = deadline

    # -- routes ------------------------------------------------------------
    def ws(self, handler, method, suffix, body=None, qs=None, wid=ORG):
        return call(handler, event(method, "/workspaces/{workspace_id}/webhook" + suffix,
                                   {"workspace_id": wid}, body, qs))

    def connect(self, **extra):
        status, body = self.ws(api.webhook_put, "PUT", "", {"url": BASE, **extra})
        if body.get("secret"):
            self.rx.secrets = [body["secret"]]
        return status, body

    def choose(self, type_id, record=None, key=KEY):
        return call(api.webhook_context_put, event(
            "PUT", "/recordings/webhook-context/{key+}", {"key": key},
            {"meeting_type_id": type_id, "record": record}))

    def push(self, key=KEY):
        return call(api.webhook_meeting_push, event(
            "POST", "/recordings/webhook/{key+}", {"key": key}))

    def status(self, key=KEY):
        return call(api.webhook_meeting_status, event(
            "GET", "/recordings/webhook/{key+}", {"key": key}))

    def retry(self, key=KEY):
        return call(api.webhook_meeting_retry, event(
            "POST", "/recordings/webhook-retry/{key+}", {"key": key}))

    def edit_title(self, title, key=KEY):
        return call(api.patch_recording, event(
            "PATCH", "/recordings/{key+}", {"key": key}, {"title": title}))

    def job(self, key=KEY):
        return self.t["crm_sync_jobs"].get_item(
            Key={"job_id": api._webhook_job_id(key)}).get("Item")

    def events_for(self, key=KEY):
        mid = sc.opaque_meeting_id(key)
        return [e for e in self.rx.events if e["meeting_id"] == mid]

    def delivered_meeting(self, type_id="site_visit", record=MUMBAI, key=KEY):
        self.connect()
        self.assertEqual(self.choose(type_id, record, key)[0], 200)
        self.assertEqual(self.push(key)[0], 202)
        self.pump()
        self.assertEqual(self.job(key)["status"], "SYNCED")


# ===========================================================================
class ConfigurationTests(WebhookHarness):
    def test_secret_is_returned_once_and_never_again(self):
        status, body = self.connect()
        self.assertEqual(status, 200)
        secret = body["secret"]
        self.assertTrue(secret.startswith("mxwhsec_"))
        self.assertEqual(body["endpoint"]["secret_last4"], secret[-4:])
        _, got = self.ws(api.webhook_get, "GET", "")
        self.assertEqual(got["mode"], "webhook")
        self.assertNotIn(secret, json.dumps(got))
        self.assertTrue(got["endpoint"]["has_secret"])
        _, again = self.connect(enabled=True)
        self.assertNotIn("secret", again)
        stored = self.t["site_endpoints"].get_item(Key={"workspace_id": ORG})["Item"]
        self.assertEqual(stored["delivery_type"], "webhook")
        self.assertEqual(stored["provider"], "salesforce")
        self.assertTrue(stored["secrets_enc"].startswith("ENC("))

    def test_permissions(self):
        self.as_user(MEMBER)
        self.assertEqual(self.connect()[0], 403)
        self.as_user(OUTSIDER)
        self.assertEqual(self.ws(api.webhook_get, "GET", "")[0], 404)

    def test_url_rules_apply(self):
        status, body = self.ws(api.webhook_put, "PUT", "", {"url": "https://10.0.0.1/x"})
        self.assertEqual(status, 400)

    def test_test_route_reports_the_configuration_version(self):
        self.connect()
        self.rx.configuration_version = "17"
        status, body = self.ws(api.webhook_test, "POST", "/test")
        self.assertEqual(status, 200)
        self.assertTrue(body["test"]["ok"])
        self.assertEqual(body["test"]["configuration_version"], "17")
        self.assertEqual(body["configuration"]["configuration_version"], "17")
        self.assertEqual(len(body["configuration"]["meeting_types"]), 3)
        self.rx.enabled = False
        _, body = self.ws(api.webhook_test, "POST", "/test")
        self.assertFalse(body["test"]["ok"])
        self.assertEqual(body["configuration"]["status"], "stale")

    def test_stale_keeps_last_good_and_error_when_never_fetched(self):
        self.connect()
        self.ws(api.webhook_test, "POST", "/test")
        self.rx.enabled = False
        self.now += api.WEBHOOK_CONFIG_TTL_SECONDS + 1
        _, body = self.ws(api.webhook_configuration_get, "GET", "/configuration")
        cfg = body["configuration"]
        self.assertEqual(cfg["status"], "stale")
        self.assertEqual(cfg["configuration_version"], "1")
        self.assertEqual(len(cfg["meeting_types"]), 3)

    def test_configuration_unavailable_blocks_push(self):
        self.connect()
        self.rx.enabled = False
        status, body = self.push()
        self.assertEqual((status, body["code"]), (409, "configuration_unavailable"))

    def test_refresh_is_rate_limited(self):
        self.connect()
        _, first = self.ws(api.webhook_configuration_refresh, "POST", "/configuration/refresh")
        self.assertFalse(first["rate_limited"])
        _, second = self.ws(api.webhook_configuration_refresh, "POST", "/configuration/refresh")
        self.assertTrue(second["rate_limited"])
        self.now += api.WEBHOOK_CONFIG_REFRESH_MIN_SECONDS
        _, third = self.ws(api.webhook_configuration_refresh, "POST", "/configuration/refresh")
        self.assertFalse(third["rate_limited"])

    def test_search_returns_display_names_and_maps_failures_to_409(self):
        self.connect()
        status, body = self.ws(api.webhook_records_search, "GET", "/records",
                               qs={"meeting_type_id": "site_visit", "q": "PV-2026-00452"})
        self.assertEqual(status, 200)
        self.assertEqual(body["records"][0]["display_name"], MUMBAI["display_name"])
        status, body = self.ws(api.webhook_records_search, "GET", "/records",
                               qs={"meeting_type_id": "internal_meeting", "q": "x"})
        self.assertEqual((status, body["code"]), (422, "record_selection_disabled"))
        self.rx.secrets = ["mxwhsec_" + "z" * 43]  # the org no longer has our secret
        status, body = self.ws(api.webhook_records_search, "GET", "/records",
                               qs={"meeting_type_id": "site_visit", "q": "PV"})
        self.assertEqual(status, 409)  # never 401: that would sign the user out
        self.assertEqual(body["code"], "bad_signature")


# ===========================================================================
class ContextTests(WebhookHarness):
    def setUp(self):
        super().setUp()
        self.connect()

    def test_validation(self):
        self.assertEqual(self.push()[1]["code"], "meeting_type_required")
        before = sum(1 for p, _b, _h in self.rx.requests if p.endswith("/configuration"))
        self.now += 60
        status, body = self.choose("nope")
        self.assertEqual((status, body["code"]), (422, "unknown_meeting_type"))
        after = sum(1 for p, _b, _h in self.rx.requests if p.endswith("/configuration"))
        self.assertGreater(after, before)  # one synchronous refresh on a miss
        status, body = self.choose("internal_meeting", MUMBAI)
        self.assertEqual((status, body["code"]), (422, "record_selection_disabled"))
        self.assertEqual(self.choose("site_visit")[0], 200)
        self.assertEqual(self.push()[1]["code"], "record_required")
        self.assertEqual(self.choose("customer_call")[0], 200)  # optional record
        self.assertEqual(self.push()[0], 202)

    def test_a_removed_type_blocks_new_pushes_but_not_updates(self):
        self.choose("site_visit", MUMBAI)
        self.push()
        self.pump()
        self.rx.meeting_types = [t for t in self.rx.meeting_types if t["id"] != "site_visit"]
        self.rx.configuration_version = "2"
        self.now += api.WEBHOOK_CONFIG_TTL_SECONDS + 1
        self.edit_title("Edited after the type was removed")
        self.pump()
        self.assertEqual(self.rx.events[-1]["meeting_type"]["id"], "site_visit")
        self.assertEqual(self.push()[1]["code"], "meeting_type_unavailable")


# ===========================================================================
class DeliveryTests(WebhookHarness):
    def test_push_sends_created_then_edits_send_updated_after_debounce(self):
        self.delivered_meeting()
        created = self.rx.events[0]
        self.assertEqual(created["event"], "meeting.created")
        self.assertEqual(created["sequence"], 1)
        self.assertEqual(created["changed_fields"], [])
        self.assertEqual(created["meeting_type"], {"id": "site_visit", "name": "Site Visit"})
        self.assertEqual(created["record_context"]["record_id"], "a0P123")
        self.assertEqual(created["workspace_id"], ORG)
        _, st = self.status()
        self.assertEqual(st["job"]["delivery_status"], "delivered")
        self.assertEqual(st["job"]["external"]["url"], "https://acme.example/r/1")

        self.edit_title("Site walk — revised")
        self.assertEqual(self.job()["status"], "PENDING")
        self.pump(59)
        self.assertEqual(len(self.rx.events), 1)  # still debouncing
        self.pump(5)
        updated = self.rx.events[-1]
        self.assertEqual(updated["event"], "meeting.updated")
        self.assertEqual(updated["sequence"], 2)
        self.assertEqual(updated["changed_fields"], ["meeting"])
        self.assertEqual(updated["data"]["meeting"]["title"], "Site walk — revised")

    def test_send_now_skips_the_wait_for_quick_edits(self):
        self.delivered_meeting()
        self.edit_title("Site walk — revised")
        self.assertEqual(self.status()[1]["job"]["delivery_status"], "update_pending")
        self.push()
        self.pump(1)
        self.assertEqual(len(self.rx.events), 2, "sent now, not after the minute")
        self.assertEqual(self.rx.events[-1]["data"]["meeting"]["title"], "Site walk — revised")

    def test_send_now_tries_a_retrying_delivery_again_at_once(self):
        self.delivered_meeting()
        self.rx.faults = [("status", 503)]
        self.edit_title("Site walk — revised")
        self.pump(65)                       # the batching minute, then the 503
        self.assertEqual(self.job()["status"], "RETRYING")
        sent = len(self.rx.events)
        self.push()
        self.pump(1)
        self.assertEqual(self.job()["status"], "SYNCED")
        self.assertEqual(len(self.rx.events), sent + 1)

    def test_manual_push_with_no_changes_sends_nothing(self):
        self.delivered_meeting()
        self.push()
        self.pump()
        self.assertEqual(len(self.rx.events), 1)
        self.assertEqual(self.job()["result"]["note"], "no_changes")

    def test_race_a_change_during_delivery_becomes_a_follow_up(self):
        self.delivered_meeting()
        self.edit_title("A")

        def change_now(_env):
            self.now += 1
            self.edit_title("B")
        self.rx.before_event = change_now
        self.pump()
        titles = [(e["sequence"], e["data"]["meeting"]["title"]) for e in self.rx.events[1:]]
        self.assertEqual(titles, [(2, "A"), (3, "B")])
        self.assertEqual(self.job()["status"], "SYNCED")

    def test_race_b_a_change_then_a_failure_retries_with_the_latest(self):
        self.delivered_meeting()
        self.edit_title("A")
        self.rx.faults = [("status", 503)]

        def change_now(_env):
            self.now += 1
            self.edit_title("B")
        self.rx.before_event = change_now
        self.pump()
        attempts = [(e["sequence"], e["data"]["meeting"]["title"], e["event_id"])
                    for e in self.rx.events[1:]]
        self.assertEqual([(s, t) for s, t, _ in attempts], [(2, "A"), (3, "B")])
        self.assertNotEqual(attempts[0][2], attempts[1][2])
        self.assertEqual(self.rx.applied_sequence[sc.opaque_meeting_id(KEY)], 3)

    def test_race_c_edits_inside_the_window_send_one_webhook(self):
        self.delivered_meeting()
        for i in range(4):
            self.edit_title(f"edit {i}")
            self.pump(10)
        self.pump()
        updates = self.rx.events[1:]
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0]["data"]["meeting"]["title"], "edit 3")

    def test_continuous_edits_still_go_out_by_the_max_wait(self):
        self.delivered_meeting()
        start = self.now
        self.edit_title("e0")
        for i in range(1, 6):
            self.pump(50)
            self.edit_title(f"e{i}")
        self.pump(start + api.WEBHOOK_MAX_WAIT_SECONDS - self.now - 1)
        self.assertEqual(len(self.rx.events), 1)
        self.pump(3)
        self.assertEqual(len(self.rx.events), 2)
        self.assertEqual(self.rx.events[-1]["data"]["meeting"]["title"], "e5")

    def test_a_retry_with_unchanged_content_reuses_the_event_id(self):
        self.connect()
        self.choose("site_visit", MUMBAI)
        self.rx.faults = [("status", 503)]
        self.push()
        self.pump()
        ids = [e["event_id"] for e in self.rx.events]
        self.assertEqual(len(ids), 2)
        self.assertEqual(ids[0], ids[1])
        self.assertEqual(self.job()["status"], "SYNCED")

    def test_timeout_after_commit_is_answered_as_a_duplicate(self):
        self.connect()
        self.choose("site_visit", MUMBAI)
        self.rx.faults = ["timeout_after_commit"]
        self.push()
        self.pump()
        self.assertEqual(len(self.rx.handler_runs), 1)
        self.assertTrue(self.job()["result"]["duplicate"])

    def test_a_failed_handler_is_run_again_on_retry(self):
        self.connect()
        self.choose("site_visit", MUMBAI)
        self.rx.faults = ["handler_error"]
        self.push()
        self.pump()
        eid = self.rx.events[0]["event_id"]
        self.assertEqual(self.rx.handler_runs, [eid, eid])
        self.assertEqual(self.rx.processed[eid]["status"], "PROCESSED")

    def test_a_sequence_is_never_bound_to_two_snapshots(self):
        self.connect()
        self.choose("site_visit", MUMBAI)
        self.rx.faults = [("status", 503), "timeout_after_commit"]
        self.push()
        self.pump()
        self.edit_title("B")
        self.rx.faults = [("status", 500)]
        self.pump()
        self.edit_title("C")
        self.pump()
        by_seq = {}
        for mid, seq, eid, content in self.rx.log:
            by_seq.setdefault(seq, set()).add((eid, content))
        self.assertGreaterEqual(len(by_seq), 3)
        for seq, bound in by_seq.items():
            self.assertEqual(len(bound), 1, f"sequence {seq} carried {len(bound)} snapshots")

    def test_concurrent_allocation_cannot_share_a_sequence(self):
        self.delivered_meeting()
        stale = self.job()
        first = api._webhook_bind_event(stale, "meeting.updated", "content-1", [], "t")
        second = api._webhook_bind_event(stale, "meeting.updated", "content-2", [], "t")
        self.assertEqual(first["sequence"], stale["sequence"] + 1)
        self.assertEqual(second["sequence"], stale["sequence"] + 2)
        self.assertEqual(self.job()["pending_event"]["content_hash"], "content-2")

    def test_failed_is_not_reopened_by_edits_and_retry_sends_the_latest(self):
        self.connect()
        self.choose("site_visit", MUMBAI)
        self.rx.faults = [("status", 422)]
        self.push()
        self.pump()
        self.assertEqual(self.job()["status"], "FAILED")
        self.assertFalse(self.status()[1]["job"]["has_pending_changes"])
        self.edit_title("Changed after the failure")
        self.pump()
        self.assertEqual(len(self.rx.events), 1)  # nothing sent automatically
        _, st = self.status()
        self.assertEqual(st["job"]["delivery_status"], "failed")
        self.assertTrue(st["job"]["has_pending_changes"])
        self.assertEqual(self.push()[1]["code"], "delivery_failed")
        self.assertEqual(self.retry()[0], 202)
        self.pump()
        last = self.rx.events[-1]
        self.assertEqual(last["event"], "meeting.created")
        self.assertEqual(last["data"]["meeting"]["title"], "Changed after the failure")
        self.assertEqual(last["sequence"], 2)  # the failed sequence 1 is a gap
        self.assertEqual(self.job()["status"], "SYNCED")

    def test_eight_attempts_never_reach_the_dlq(self):
        self.connect()
        self.choose("site_visit", MUMBAI)
        self.rx.faults = [("status", 503)] * 10
        self.push()
        self.pump()
        self.assertEqual(len(self.rx.events), api.WEBHOOK_MAX_ATTEMPTS)
        self.assertEqual(self.batch_failures, [])
        job = self.job()
        self.assertEqual(job["status"], "FAILED")
        self.assertEqual(int(job["webhook_attempts"]), api.WEBHOOK_MAX_ATTEMPTS)
        headers = [h for p, _b, h in self.rx.requests if p.endswith("/events")]
        self.assertEqual(headers[-1]["X-MinuteX-Delivery-Attempt"], "8")

    def test_retry_after_is_honoured(self):
        self.connect()
        self.choose("site_visit", MUMBAI)
        self.rx.faults = [("status", 503, 600)]
        self.push()
        self.pump(1)
        job = self.job()
        self.assertEqual(job["status"], "RETRYING")
        self.assertAlmostEqual(api._webhook_ts(job["next_attempt_at"]) - self.now, 599, delta=2)
        self.pump(590)
        self.assertEqual(len(self.rx.events), 1)
        self.pump(20)
        self.assertEqual(len(self.rx.events), 2)

    def test_trash_restore_and_permanent_delete(self):
        self.delivered_meeting()
        call(api.delete_recording, event("DELETE", "/recordings/{key+}", {"key": KEY}))
        self.pump()
        trashed = self.rx.events[-1]
        self.assertEqual((trashed["event"], trashed["changed_fields"]),
                         ("meeting.updated", ["lifecycle"]))
        self.assertEqual(trashed["data"]["meeting"]["lifecycle"], "trashed")
        call(api.restore_recording, event("POST", "/recordings/restore/{key+}", {"key": KEY}))
        self.pump()
        self.assertEqual(self.rx.events[-1]["data"]["meeting"]["lifecycle"], "active")
        call(api.permanently_delete_recording,
             event("DELETE", "/recordings/permanent/{key+}", {"key": KEY}))
        self.pump()
        deleted = self.rx.events[-1]
        self.assertEqual(deleted["event"], "meeting.deleted")
        self.assertEqual(deleted["data"]["meeting"]["id"], sc.opaque_meeting_id(KEY))
        self.assertNotIn(sc.opaque_meeting_id(KEY), self.rx.meetings)
        self.assertEqual(self.job()["lifecycle"], "deleted")

    def test_deleted_before_any_delivery_sends_nothing(self):
        self.connect()
        self.choose("site_visit", MUMBAI)
        self.push()
        call(api.permanently_delete_recording,
             event("DELETE", "/recordings/permanent/{key+}", {"key": KEY}))
        self.pump()
        self.assertEqual(self.rx.events, [])
        self.assertEqual(self.job()["result"]["note"], "nothing_to_delete")

    def test_audio_link_carries_the_job_id_and_dies_with_the_endpoint(self):
        self.delivered_meeting()
        url = self.rx.events[0]["data"]["audio"]["url"]
        token = url.rsplit("/", 1)[1]
        claims = sc.read_audio_token(token)
        self.assertEqual(claims["j"], api._webhook_job_id(KEY))
        self.assertNotIn(KEY, urllib.parse.unquote(url))
        ev = {"pathParameters": {"token": token}}
        self.assertEqual(api.site_delivery_audio(ev)["statusCode"], 302)
        self.ws(api.webhook_delete, "DELETE", "")
        self.assertEqual(api.site_delivery_audio(ev)["statusCode"], 404)

    def test_status_after_disconnect_keeps_what_was_sent(self):
        self.delivered_meeting()
        self.ws(api.webhook_delete, "DELETE", "")
        status, body = self.status()
        self.assertEqual(status, 200)
        self.assertFalse(body["endpoint"]["enabled"])
        self.assertIsNone(body["configuration"])
        self.assertTrue(body["job"]["delivered"])
        self.assertTrue(body["job"]["last_delivered_at"])
        self.assertEqual(body["context"]["meeting_type_id"], "site_visit")
        self.assertIsNone(body["earlier_record"])

    def test_status_shows_a_meeting_the_retired_package_sent(self):
        self.t["recordings"].update_item(
            Key={"audio_s3_key": KEY},
            UpdateExpression="SET #m = :m", ExpressionAttributeNames={"#m": api.SITE_MEETING_ATTR},
            ExpressionAttributeValues={":m": {
                "record_id": "a01000000000001", "org_id": "00D000000000001", "title": "Board sync",
                "record_url": "https://x.my.salesforce.com/a01000000000001",
                "delivered_at": "2026-09-01T10:00:00Z"}})
        status, body = self.status()
        self.assertEqual(status, 200)
        self.assertIsNone(body["job"])
        rec = body["earlier_record"]
        self.assertEqual((rec["name"], rec["url"], rec["delivered_at"]),
                         ("Board sync", "https://x.my.salesforce.com/a01000000000001",
                          "2026-09-01T10:00:00Z"))

    def test_secret_rotation_dual_signs_then_expires(self):
        self.delivered_meeting()
        old = self.rx.secrets[0]
        _, body = self.ws(api.webhook_rotate_secret, "POST", "/secret")
        new = body["secret"]
        self.assertNotEqual(new, old)
        self.edit_title("during the grace window")
        self.rx.secrets = [old]          # the org has not updated yet
        self.pump()
        hdr = [h for p, _b, h in self.rx.requests if p.endswith("/events")][-1]
        self.assertEqual(len(hdr["X-MinuteX-Signature"].split(",")), 2)
        self.assertEqual(self.job()["status"], "SYNCED")
        self.now += api.WEBHOOK_SECRET_GRACE_SECONDS + 1
        self.edit_title("after the grace window")
        self.pump(61)
        hdr = [h for p, _b, h in self.rx.requests if p.endswith("/events")][-1]
        self.assertEqual(len(hdr["X-MinuteX-Signature"].split(",")), 1)
        self.assertEqual(self.job()["last_error_code"], "bad_signature")  # old rejected
        self.rx.secrets = [new]
        self.retry()
        self.pump()
        self.assertEqual(self.job()["status"], "SYNCED")


# ===========================================================================
class PeopleAndContextSeparationTests(WebhookHarness):
    def tag(self, key, speaker_id="0"):
        """The app's path: live search -> resolve the pick to a contact
        (saving that one person) -> tag the speaker with the contact."""
        _, found = self.ws(api.webhook_people_search, "GET", "/people", qs={"q": "John"})
        person = next(p for p in found["people"] if p["person_key"] == JOHN_KEY)
        status, body = call(api.people_contact, event(
            "POST", "/workspaces/{workspace_id}/people/contact", {"workspace_id": ORG},
            {"person_key": JOHN_KEY, "person": person}))
        self.assertEqual(status, 200)
        return call(api.set_participant, event(
            "PUT", "/recordings/participants/{key+}", {"key": key},
            {"speaker_id": speaker_id, "contact_id": body["contact"]["id"]}))

    def test_picking_a_live_person_saves_only_that_person(self):
        self.connect()
        self.assertEqual(self.tag(KEY)[0], 200)
        people = self.t["people"].scan()["Items"]
        self.assertEqual([p["person_key"] for p in people], [JOHN_KEY])
        self.assertEqual(people[0]["name"], "John Smith")

    def test_contact_and_record_are_independent(self):
        self.put_meeting(key=KEY2)
        self.connect()
        self.tag(KEY)
        self.tag(KEY2)
        self.choose("internal_meeting", None, KEY)
        self.choose("site_visit", MUMBAI, KEY2)
        self.push(KEY)
        self.push(KEY2)
        self.pump()
        m1, m2 = self.events_for(KEY)[-1], self.events_for(KEY2)[-1]

        def john(ev):
            return next(p for p in ev["data"]["participants"] if p["speaker_id"] == "0")
        self.assertEqual(john(m1)["external_identity"], john(m2)["external_identity"])
        self.assertEqual(john(m1)["external_identity"]["type"], "contact")
        self.assertIsNone(m1["record_context"])
        self.assertEqual(m1["meeting_type"]["id"], "internal_meeting")
        self.assertEqual(m2["record_context"]["record_id"], "a0P123")

        # Clearing the record leaves the Contact untouched.
        self.choose("site_visit", None, KEY2)
        self.pump()
        cleared = self.events_for(KEY2)[-1]
        self.assertEqual(cleared["changed_fields"], ["record_context"])
        self.assertIsNone(cleared["record_context"])
        self.assertEqual(john(cleared)["external_identity"], john(m2)["external_identity"])


# ===========================================================================
class GuardAndFlagTests(WebhookHarness):
    def test_an_old_site_endpoint_needs_replace_site(self):
        # A row left by the retired Salesforce Site path.
        self.t["site_endpoints"].put_item(Item={
            "workspace_id": ORG, "delivery_type": sc.DELIVERY_TYPE_SALESFORCE_SITE,
            "url": "https://acme.my.salesforce-sites.com/services/apexrest/minutex/v1/",
            "auth_mode": "none", "enabled": True, "created_at": NOW, "updated_at": NOW})
        status, body = self.connect()
        self.assertEqual((status, body["code"]), (409, "site_mode_active"))
        status, body = self.connect(replace_site=True)
        self.assertEqual(status, 200)
        self.assertEqual(self.t["site_endpoints"].get_item(
            Key={"workspace_id": ORG})["Item"]["delivery_type"], "webhook")

    def test_the_site_routes_are_gone(self):
        site = [k for k in api._ROUTES
                if "/site-delivery" in k[1] or "/site-salesforce" in k[1]
                or "/site-participants" in k[1] or k[1] == "/salesforce/pair"]
        self.assertEqual(site, [])
        # The worker runs webhook events only; a leftover Site job is dropped.
        self.assertEqual(api.SITE_DELIVERY_OPERATIONS, frozenset({api.SITE_OP_WEBHOOK_EVENT}))

    def test_flag_off_disables_routes_and_hooks(self):
        self.delivered_meeting()
        with mock.patch.object(api, "WEBHOOK_ENABLED", False):
            status, body = self.push()
            self.assertEqual((status, body["code"]), (404, "webhook_disabled"))
            before = dict(self.job())
            self.edit_title("while switched off")
            self.assertEqual(self.job()["dirty_version"], before["dirty_version"])


# ===========================================================================
class HookCoverageTests(WebhookHarness):
    """Every write that changes the snapshot marks the meeting. Parametrised
    over the call sites; a future writer that forgets the hook is caught by
    the snapshot tests above only if it is also exercised there."""

    def setUp(self):
        super().setUp()
        self.marks = mock.MagicMock()
        p = mock.patch.object(api, "_webhook_mark_meeting_dirty", self.marks)
        p.start()
        self.addCleanup(p.stop)

    def assert_marked(self, reason, key=KEY):
        reasons = [c.args[1] for c in self.marks.call_args_list if c.args[0] == key]
        self.assertIn(reason, reasons, f"{reason!r} not marked; got {reasons}")

    def contact_id(self):
        row = api._contact_item(OWNER, "Asha Rao", email="asha@acme.com", workspace_id=ORG)
        self.t["contacts"].put_item(Item=row)
        return row["contact_id"]

    def test_hooks(self):
        cases = {
            "title": (lambda: self.edit_title("renamed"), "meeting"),
            "trash": (lambda: call(api.delete_recording, event(
                "DELETE", "/recordings/{key+}", {"key": KEY})), "lifecycle"),
            "restore": (lambda: (self.put_meeting(recording_status="trashed"),
                                 call(api.restore_recording, event(
                                     "POST", "/recordings/restore/{key+}", {"key": KEY}))),
                        "lifecycle"),
            "minutes_saved": (lambda: api._persist_mom(
                KEY, self.t["recordings"].get_item(Key={"audio_s3_key": KEY})["Item"],
                api.mom_schema.empty_mom()), "minutes"),
            "minutes_deleted": (lambda: (self.put_meeting(mom=api.mom_schema.empty_mom()),
                                         call(api.delete_mom, event(
                                             "DELETE", "/recordings/ai/mom/{key+}",
                                             {"key": KEY}))), "minutes"),
            "highlights": (lambda: (mock.patch.object(
                api.groq_client, "complete_json",
                return_value={"decisions": [{"decision": "Paint"}]}).start(),
                call(api.regenerate_highlights, event(
                    "POST", "/recordings/ai/highlights/{key+}", {"key": KEY},
                    {"regenerate": True}))), "summary"),
            "task_mirror": (lambda: api._mirror_task_to_recording(
                KEY, {"task_id": "t1", "title": "Send deck"}), "action_items"),
            "task_deleted": (lambda: (self.t["tasks"].put_item(Item={
                "task_id": "t9", "title": "x", "owner_user_id": OWNER,
                "source_recording_id": KEY, "status": "Open", "created_at": NOW}),
                call(api.delete_meeting_task, event(
                    "DELETE", "/recordings/ai/tasks/{key+}", {"key": KEY},
                    {"id": "t9"}))), "action_items"),
            "participant_set": (lambda: call(api.set_participant, event(
                "PUT", "/recordings/participants/{key+}", {"key": KEY},
                {"speaker_id": "1", "contact_id": self.contact_id()})), "participants"),
            "participant_cleared": (lambda: call(api.set_participant, event(
                "PUT", "/recordings/participants/{key+}", {"key": KEY},
                {"speaker_id": "1", "contact_id": None})), "participants"),
            "pipeline": (lambda: api.handle_seed_tasks_event({"audio_s3_key": KEY}),
                         "reprocessed"),
        }
        for name, (action, reason) in cases.items():
            with self.subTest(site=name):
                self.marks.reset_mock()
                action()
                self.assert_marked(reason)

    def test_contact_delete_marks_each_meeting_it_was_tagged_in(self):
        cid = self.contact_id()
        call(api.set_participant, event("PUT", "/recordings/participants/{key+}",
                                        {"key": KEY}, {"speaker_id": "0", "contact_id": cid}))
        self.marks.reset_mock()
        call(api.delete_contact, event("DELETE", "/contacts/{contact_id}",
                                       {"contact_id": cid}))
        self.assert_marked("participants")

    def test_permanent_delete_marks_before_the_row_goes(self):
        seen = []
        self.marks.side_effect = lambda key, reason, **kw: seen.append(
            (reason, kw.get("lifecycle"),
             bool(self.t["recordings"].get_item(Key={"audio_s3_key": key}).get("Item"))))
        call(api.permanently_delete_recording,
             event("DELETE", "/recordings/permanent/{key+}", {"key": KEY}))
        self.assertEqual(seen, [("deleted", "deleted", True)])


if __name__ == "__main__":
    unittest.main()


# ===========================================================================
class PeopleSearchTests(WebhookHarness):
    """Live search of the customer's people for speaker tagging: nothing is
    copied in bulk, the caller's email travels for the access check, and an
    older receiver without the route degrades to a clear 409."""

    def setUp(self):
        super().setUp()
        self.rx.people = [
            contact("003000000000000001", "Rahul Sharma", "rahul@client.com"),
            lead("00Q000000000000001", "Rahul Mehta", "rmehta@web.com"),
            user("005000000000000001", "Rahul Verma", "rverma@acme.com"),
            contact("003000000000000002", "Priya Nair", "priya@client.com"),
        ]
        self.connect()

    def search(self, q, **qs):
        return self.ws(api.webhook_people_search, "GET", "/people", qs={"q": q, **qs})

    def test_finds_contacts_leads_and_users_live(self):
        status, body = self.search("rahul")
        self.assertEqual(status, 200)
        self.assertTrue(body["live"])
        self.assertEqual(sorted(p["type"] for p in body["people"]),
                         ["contact", "lead", "user"])
        self.assertEqual(body["people"][0]["person_key"],
                         "salesforce#contact#003000000000000001")
        sent = [b for p, b, _h in self.rx.requests if p.endswith("/people/search")][-1]
        self.assertEqual(sent["type"], "people.search")
        self.assertEqual(sent["user_email"], f"{OWNER}@work.com")
        # Nothing was written to the people store by searching.
        self.assertEqual(self.t["people"].scan()["Items"], [])

    def test_types_filter(self):
        _, body = self.search("rahul", types="lead")
        self.assertEqual([p["type"] for p in body["people"]], ["lead"])

    def test_receiver_hides_people_the_caller_cannot_see(self):
        self.rx.hidden_from[f"{OWNER}@work.com"] = ["003000000000000001"]
        _, body = self.search("rahul")
        self.assertNotIn("salesforce#contact#003000000000000001",
                         [p["person_key"] for p in body["people"]])

    def test_short_query_is_refused_before_sending(self):
        before = len(self.rx.requests)
        status, body = self.search("r")
        self.assertEqual((status, body["code"]), (400, "query_too_short"))
        self.assertEqual(len(self.rx.requests), before)

    def test_older_receiver_without_the_route(self):
        self.rx.people_search_supported = False
        status, body = self.search("rahul")
        self.assertEqual((status, body["code"]), (409, "people_search_unsupported"))

    def test_unusable_rows_are_skipped(self):
        self.rx.people.append({"provider": "salesforce", "type": "account",
                               "external_id": "001000000000000001", "name": "Rahul Corp"})
        _, body = self.search("rahul")
        self.assertEqual(len(body["people"]), 3)

    def test_receiver_failure_is_409_never_401(self):
        self.rx.enabled = False
        status, _ = self.search("rahul")
        self.assertEqual(status, 409)

    def pick(self, person, send_person=True):
        body = {"person_key": f"salesforce#{person['type']}#{person['external_id']}"}
        if send_person:
            body["person"] = person
        return call(api.people_contact, event(
            "POST", "/workspaces/{workspace_id}/people/contact",
            {"workspace_id": ORG}, body))

    def test_picking_a_live_result_saves_only_that_person(self):
        rahul = self.rx.people[1]                       # a Lead, never synced
        status, body = self.pick(rahul)
        self.assertEqual(status, 200)
        self.assertEqual(body["contact"]["name"], "Rahul Mehta")
        rows = self.t["people"].scan()["Items"]
        self.assertEqual([r["person_key"] for r in rows],
                         ["salesforce#lead#00Q000000000000001"])
        self.assertEqual(rows[0]["last_sync_run"], "live_search")
        # The same pick again resolves to the same contact.
        self.assertEqual(self.pick(rahul)[1]["contact"]["id"], body["contact"]["id"])

    def test_a_live_pick_must_match_its_key(self):
        status, _ = call(api.people_contact, event(
            "POST", "/workspaces/{workspace_id}/people/contact", {"workspace_id": ORG},
            {"person_key": "salesforce#contact#003000000000000009",
             "person": self.rx.people[0]}))
        self.assertEqual(status, 400)
        self.assertEqual(self.pick(self.rx.people[0], send_person=False)[0], 404)

    def test_a_sync_never_deletes_a_live_picked_person(self):
        self.pick(self.rx.people[1])
        people_store = api.people_store
        marked = people_store.mark_missing(self.t["people"], api.Key, ORG,
                                           "salesforce", "some-sync-run", "t")
        self.assertEqual(marked, 0)

    # -- people on the meeting's linked record ------------------------------
    def related(self, key=KEY):
        return call(api.webhook_people_related, event(
            "GET", "/recordings/webhook-people/{key+}", {"key": key}))

    def test_related_people_come_from_the_linked_record(self):
        self.rx.related = {"a0P123": [self.rx.people[3]]}       # Priya, on Mumbai Heights
        self.assertEqual(self.choose("site_visit", MUMBAI)[0], 200)
        status, body = self.related()
        self.assertEqual(status, 200)
        self.assertEqual([(p["name"], p["relation"]) for p in body["people"]],
                         [("Priya Nair", "Contact at a0P123")])
        sent = [b for p, b, _h in self.rx.requests if p.endswith("/people/related")][-1]
        self.assertEqual((sent["type"], sent["record_ids"]), ("people.related", ["a0P123"]))
        self.assertEqual(self.t["people"].scan()["Items"], [])     # nothing saved

    def test_no_linked_record_means_no_call(self):
        before = len(self.rx.requests)
        status, body = self.related()
        self.assertEqual((status, body["people"]), (200, []))
        self.assertEqual(len(self.rx.requests), before)

    def test_older_receiver_without_related_route(self):
        self.choose("site_visit", MUMBAI)
        self.rx.people_search_supported = False
        status, body = self.related()
        self.assertEqual((status, body["code"]), (409, "people_related_unsupported"))


# ===========================================================================
LEAD_ID = "00Q5g00000XyZabEAF"
PROP_ID = "a0X5g00000AbCdEAAZ"
ALL_OUT = list(api.contract_v3.OUTPUTS)
LINKABLE = [
    {"object": "Property__c", "label": "Property", "can_be_primary": True,
     "outputs_allowed": ALL_OUT, "outputs_default_on": ["overview", "mom", "tasks", "participants"],
     "outputs_user_can_change": ALL_OUT},
    {"object": "Lead", "label": "Lead", "can_be_primary": True,
     "outputs_allowed": ["overview", "mom", "tasks", "participants"],
     "outputs_default_on": ["overview", "tasks"],
     "outputs_user_can_change": ["overview", "mom", "participants"]},
    {"object": "Account", "label": "Account", "can_be_primary": False,
     "outputs_allowed": ["overview"], "outputs_default_on": ["overview"],
     "outputs_user_can_change": ["overview"]},
]


class LinkingTests(WebhookHarness):
    """Contract v3 linking end to end: the receiver's linkable objects, the
    search across them, saving links under the admin's rules, and delivery
    as a v3 event."""

    def setUp(self):
        super().setUp()
        self.rx.linkable_objects = LINKABLE
        self.rx.records = [
            {"id": PROP_ID, "display_name": "Mumbai Heights", "secondary_text": "Andheri",
             "object_type": "Property__c"},
            {"id": LEAD_ID, "display_name": "Rahul Sharma", "secondary_text": "Web",
             "object_type": "Lead"},
            {"id": "5005g00000Cas01AAA", "display_name": "Rahul's case",
             "object_type": "Case"},
        ]
        self.connect()

    def links(self, body, key=KEY):
        return call(api.webhook_links_put, event(
            "PUT", "/recordings/webhook-links/{key+}", {"key": key}, body))

    def body(self, **over):
        b = {"links": [{"object": "Property__c", "id": PROP_ID, "name": "Mumbai Heights",
                        "primary": True},
                       {"object": "Lead", "id": LEAD_ID, "name": "Rahul Sharma"}],
             "included_outputs": ["overview", "mom", "tasks", "participants"],
             "task_relations": {}}
        b.update(over)
        return b

    def test_link_config_carries_the_objects_and_the_one_limit(self):
        status, body = self.ws(api.webhook_link_config, "GET", "/link-config")
        self.assertEqual(status, 200)
        self.assertTrue(body["supported"])
        self.assertEqual(body["max_links"], api.contract_v3.MAX_LINKS)
        self.assertEqual([o["object"] for o in body["objects"]], ["Property__c", "Lead", "Account"])

    def test_a_receiver_without_linking_says_so(self):
        self.rx.linkable_objects = None
        self.now += api.WEBHOOK_CONFIG_REFRESH_MIN_SECONDS
        self.ws(api.webhook_configuration_refresh, "POST", "/configuration/refresh")
        _, body = self.ws(api.webhook_link_config, "GET", "/link-config")
        self.assertFalse(body["supported"])
        status, body = self.links(self.body())
        self.assertEqual((status, body["code"]), (409, "linking_unsupported"))

    def test_search_covers_linkable_objects_only(self):
        status, body = self.ws(api.webhook_link_records, "GET", "/link-records", qs={"q": "rahul"})
        self.assertEqual(status, 200)
        self.assertEqual([r["object_type"] for r in body["records"]], ["Lead"])
        sent = [b for p, b, _h in self.rx.requests if p.endswith("/records/search")][-1]
        self.assertEqual(sorted(sent["objects"]), ["Account", "Lead", "Property__c"])
        self.assertEqual(sent["user_email"], f"{OWNER}@work.com")

    def test_saving_links_applies_the_admins_rules(self):
        status, body = self.links(self.body(
            included_outputs=["overview", "participants"]))       # tasks unticked, mom unticked
        self.assertEqual(status, 200)
        got = body["links"]
        self.assertEqual([l["primary"] for l in got["links"]], [True, False])
        # Tasks is locked for Lead but changeable for Property, so the user's
        # untick stands; nothing locked is forced on here.
        self.assertEqual(got["included_outputs"], ["overview", "participants"])

    def test_refusals(self):
        cases = [
            (self.body(links=[{"object": "Case", "id": "5005g00000Cas01AAA", "name": "x"}]),
             "object_not_linkable"),
            (self.body(links=[{"object": "Account", "id": "0015g00000Acc01AAA", "name": "x"}]),
             "invalid_primary"),
            (self.body(links=[{"object": "Lead", "id": LEAD_ID, "name": "x"}],
                       included_outputs=["participant_contributions", "participants"]),
             "output_not_allowed"),
        ]
        for body, code in cases:
            with self.subTest(code=code):
                status, out = self.links(body)
                self.assertEqual((status, out["code"]), (422, code))

    def test_linked_meeting_is_delivered_as_a_v3_event(self):
        self.assertEqual(self.links(self.body(task_relations={
            "t1": {"object": "Lead", "id": LEAD_ID}}))[0], 200)
        self.assertEqual(self.push()[0], 202)     # no meeting type needed
        self.pump()
        self.assertEqual(self.job()["status"], "SYNCED")
        env = self.events_for()[-1]
        self.assertEqual(env["contract_version"], "3.0")
        self.assertEqual([l["id"] for l in env["links"]], [PROP_ID, LEAD_ID])
        self.assertEqual(env["included_outputs"], ["overview", "mom", "tasks", "participants"])
        self.assertNotIn("meeting_type", env)
        self.assertEqual(env["data"]["meeting"]["recorded_by"]["email"], f"{OWNER}@work.com")
        self.assertNotIn("audio", env["data"])                 # recording not included

    def test_changing_links_sends_an_update(self):
        self.links(self.body())
        self.push()
        self.pump()
        first = len(self.events_for())
        self.links(self.body(included_outputs=["overview"]))
        self.pump()
        env = self.events_for()[-1]
        self.assertEqual(len(self.events_for()), first + 1)
        self.assertEqual(env["event"], "meeting.updated")
        self.assertIn("included_outputs", env["changed_fields"])
        self.assertNotIn("minutes", env["data"])

    def test_related_people_cover_every_linked_record(self):
        self.links(self.body())
        call(api.webhook_people_related, event(
            "GET", "/recordings/webhook-people/{key+}", {"key": KEY}))
        sent = [b for p, b, _h in self.rx.requests if p.endswith("/people/related")][-1]
        self.assertEqual(sent["record_ids"], [PROP_ID, LEAD_ID])

    def test_status_returns_the_links(self):
        self.links(self.body())
        _, body = self.status()
        self.assertEqual([l["name"] for l in body["links"]["links"]],
                         ["Mumbai Heights", "Rahul Sharma"])


class FileDeliveryTests(LinkingTests):
    """The transcript (text) and the MoM PDF (uploaded by the app) are sent
    as files BEFORE the event, only when changed, and only with their output."""

    def setUp(self):
        super().setUp()
        self.objects = {}
        self.s3.put_object.side_effect = lambda **kw: self.objects.__setitem__(kw["Key"], kw["Body"])
        self.s3.get_object.side_effect = lambda **kw: {"Body": io.BytesIO(self.objects[kw["Key"]])}
        self.t["recordings"].update_item(
            Key={"audio_s3_key": KEY},
            UpdateExpression="SET speaker_names = :n, timestamps = :t, transcript = :tx",
            ExpressionAttributeValues={
                ":n": {"0": "Amit Verma"},
                ":t": [{"speaker": "0", "start": 5}, {"speaker": "1", "start": 72}],
                ":tx": "Speaker 0: I will send the deck.\n\nSpeaker 1: Thanks."})

    def upload_pdf(self, content=b"%PDF-1.4 minutes"):
        return call(api.webhook_mom_pdf_put, event(
            "PUT", "/recordings/webhook-mom-pdf/{key+}", {"key": KEY},
            {"content_base64": base64.b64encode(content).decode(), "filename": "MoM – Site visit"}))

    def deliver(self, outputs):
        self.assertEqual(self.links(self.body(included_outputs=outputs))[0], 200)
        self.push()
        self.pump()
        self.assertEqual(self.job()["status"], "SYNCED")

    def test_the_transcript_goes_as_text_in_the_event_never_a_file(self):
        self.deliver(["overview", "transcript"])
        self.assertFalse(any(p.endswith("/files") for p, _b, _h in self.rx.requests))
        env = self.events_for()[-1]
        self.assertEqual(env["data"]["files"], [])
        t = env["data"]["transcript"]
        self.assertTrue(t["included"])
        self.assertEqual(t["text"], "[00:00:05] Amit Verma: I will send the deck.\n"
                                    "[00:01:12] Speaker 1: Thanks.")
        self.assertEqual(t["parts"], [t["text"]])
        self.assertFalse(t["truncated"])

    def test_mom_pdf_upload_then_delivery(self):
        self.assertEqual(self.upload_pdf()[0], 200)
        self.deliver(["overview", "mom"])
        (f,) = self.rx.files.values()
        self.assertEqual((f["kind"], f["filename"], f["content"]),
                         ("mom_pdf", "MoM – Site visit.pdf", b"%PDF-1.4 minutes"))

    def test_upload_refusals(self):
        status, body = self.upload_pdf(b"not a pdf")
        self.assertEqual((status, body["code"]), (422, "invalid_pdf"))
        with mock.patch.object(api.contract_v3, "MAX_FILE_BYTES", 10):
            status, body = self.upload_pdf()
        self.assertEqual((status, body["code"]), (422, "file_too_large"))

    def test_the_full_overview_goes_with_speaker_names(self):
        self.t["recordings"].update_item(
            Key={"audio_s3_key": KEY},
            UpdateExpression="SET overview = :o, highlights = :h",
            ExpressionAttributeValues={
                ":o": {"sections": [
                    {"title": "Decisions", "items": ["Speaker 0 will send the deck"]},
                    {"title": "Next steps", "content": "Speaker 1 books the visit."}]},
                ":h": ["an old highlight"]})
        self.deliver(["overview"])
        summary = self.events_for()[-1]["data"]["summary"]
        self.assertEqual(summary["text"], "Decisions:\n- Amit Verma will send the deck\n\n"
                                          "Next steps:\nSpeaker 1 books the visit.")
        self.assertEqual(set(summary), {"text", "truncated"}, "no sections, highlights or decisions")

    def test_unchanged_files_are_not_sent_again(self):
        self.upload_pdf()
        self.deliver(["overview", "mom"])
        files_calls = lambda: sum(1 for p, _b, _h in self.rx.requests if p.endswith("/files"))
        n_files = files_calls()
        self.edit_title("Client Discussion, revised")
        self.pump()
        self.assertEqual(files_calls(), n_files, "the same PDF is not sent again")
        self.upload_pdf(b"%PDF-1.4 minutes, edited")
        self.pump()
        self.assertEqual(files_calls(), n_files + 1, "a changed PDF is a new version")
        self.links(self.body(included_outputs=["overview"]))   # MoM off
        self.pump()
        self.assertEqual(files_calls(), n_files + 1)
        self.assertEqual(self.events_for()[-1]["data"]["files"], [])

    # -- generated documents, as PDFs only ------------------------------------

    def set_docs(self, **docs):
        self.t["recordings"].update_item(
            Key={"audio_s3_key": KEY}, UpdateExpression="SET documents = :d",
            ExpressionAttributeValues={":d": {
                t: {"type": t, "label": t.replace("_", " ").title(), "content": c}
                for t, c in docs.items()}})

    def upload_doc(self, doc_type, content=b"%PDF-1.4 doc"):
        return call(api.webhook_doc_pdf_put, event(
            "PUT", "/recordings/webhook-doc-pdf/{key+}", {"key": KEY},
            {"doc_type": doc_type, "content_base64": base64.b64encode(content).decode()}))

    def stale(self):
        status, body = call(api.webhook_links_get, event(
            "GET", "/recordings/webhook-links/{key+}", {"key": KEY}))
        self.assertEqual(status, 200)
        return body["links"]["stale_documents"]

    def test_documents_go_as_pdfs_and_never_the_mom_mirror(self):
        self.set_docs(executive_summary="# Summary", minutes_of_meeting="# MoM text")
        self.links(self.body(included_outputs=["overview", "documents"]))
        self.assertEqual(self.stale(), ["executive_summary"], "the MoM mirror is not a document")
        self.assertEqual(self.upload_doc("executive_summary")[0], 200)
        self.assertEqual(self.stale(), [])
        status, body = self.upload_doc("minutes_of_meeting")
        self.assertEqual((status, body["code"]), (404, "document_not_found"))
        self.push()
        self.pump()
        (f,) = self.rx.files.values()
        self.assertEqual((f["kind"], f["content"]), ("document", b"%PDF-1.4 doc"))
        env = self.events_for()[-1]
        self.assertEqual([(x["kind"], x.get("name")) for x in env["data"]["files"]],
                         [("document", "executive_summary")])
        self.assertNotIn("# Summary", json.dumps(env), "documents never travel as text")

    def test_the_same_text_again_changes_nothing_and_new_text_waits_for_its_pdf(self):
        self.set_docs(follow_up_email="Hi team")
        self.links(self.body(included_outputs=["overview", "documents"]))
        self.upload_doc("follow_up_email")
        status, body = self.upload_doc("follow_up_email", b"%PDF-1.4 rendered again")
        self.assertTrue(body["unchanged"], "a re-render of the same text is not a change")
        self.set_docs(follow_up_email="Hi team, updated")
        self.assertEqual(self.stale(), ["follow_up_email"])
        self.push()
        self.pump()
        self.assertEqual(self.events_for()[-1]["data"]["files"], [],
                         "a PDF of older text is never sent")
        self.upload_doc("follow_up_email", b"%PDF-1.4 updated")
        self.pump()
        self.assertEqual(self.events_for()[-1]["data"]["files"][0]["name"], "follow_up_email")

    def test_documents_are_sent_only_with_their_output(self):
        self.set_docs(executive_summary="# Summary")
        self.links(self.body(included_outputs=["overview"]))
        self.upload_doc("executive_summary")
        self.assertEqual(self.stale(), [], "nothing to make when Documents is off")
        self.push()
        self.pump()
        self.assertEqual(self.events_for()[-1]["data"]["files"], [])

    def test_only_the_documents_the_user_picked_go(self):
        self.set_docs(executive_summary="# Summary", follow_up_email="Hi team")
        status, body = self.links(self.body(included_outputs=["overview", "documents"],
                                            included_documents=["follow_up_email"]))
        self.assertEqual(status, 200)
        self.assertEqual(body["links"]["included_documents"], ["follow_up_email"])
        self.assertEqual(self.stale(), ["follow_up_email"], "an unpicked document isn't made")
        self.upload_doc("executive_summary")
        self.upload_doc("follow_up_email")
        self.push()
        self.pump()
        self.assertEqual([x.get("name") for x in self.events_for()[-1]["data"]["files"]],
                         ["follow_up_email"])

    def test_without_a_pick_every_document_goes(self):
        """Meetings linked before documents could be picked keep sending all."""
        self.set_docs(executive_summary="# Summary", follow_up_email="Hi team")
        status, body = self.links(self.body(included_outputs=["overview", "documents"]))
        self.assertIsNone(body["links"]["included_documents"])
        self.assertEqual(self.stale(), ["executive_summary", "follow_up_email"])

    def test_picking_no_documents_turns_documents_off(self):
        status, body = self.links(self.body(included_outputs=["overview", "documents"],
                                            included_documents=[]))
        self.assertEqual(status, 200)
        self.assertEqual(body["links"]["included_outputs"], ["overview"])
        status, body = self.links(self.body(included_documents="executive_summary"))
        self.assertEqual((status, body["code"]), (422, "invalid_documents"))

    def test_a_file_failure_retries_and_holds_the_event(self):
        self.rx.file_fault = "status"
        self.upload_pdf()
        self.links(self.body(included_outputs=["overview", "mom"]))
        self.push()
        self.pump(seconds=120)
        self.assertEqual(self.events_for(), [])
        self.assertIn(self.job()["status"], ("RETRYING", "PENDING"))
        self.rx.file_fault = None
        self.pump()
        self.assertEqual(self.job()["status"], "SYNCED")
        self.assertEqual(len(self.events_for()), 1)
