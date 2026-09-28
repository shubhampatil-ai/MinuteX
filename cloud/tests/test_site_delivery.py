#!/usr/bin/env python3
"""test_site_delivery.py — Salesforce Site delivery, end to end (offline).

    route -> CrmSyncJobs -> SQS -> crmSyncWorker.lambda_handler
          -> execute_site_delivery_job -> SiteDeliveryClient -> receiver

Everything above the network is the REAL code: the userapi routes, the job
table, the worker Lambda's own handler (retry -> ChangeMessageVisibility),
the delivery client (encoding, signing, classification). The network is a
FakeSiteReceiver transport that mirrors the Apex receiver's contract, and SQS
is a list the test drains, redelivering batchItemFailures the way a
visibility timeout would.

Run:  python -m pytest tests/test_site_delivery.py
"""
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "functions/userapi"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_ai_workspace import api  # noqa: E402
import fake_dynamodb as fdb  # noqa: E402
import people_store  # noqa: E402
import site_contract as sc  # noqa: E402
import site_delivery_client as sdc  # noqa: E402
import workspace_schema as ws  # noqa: E402
from fake_site_receiver import FakeSiteReceiver, contact, user  # noqa: E402

# The worker's own handler, loaded exactly as test_crm_sync_worker_lambda
# does (the deployed zip packages userapi as `userapi_worker`).
sys.modules["userapi_worker"] = api
_spec = importlib.util.spec_from_file_location(
    "crm_sync_worker_for_site_tests",
    ROOT / "functions/crm-sync-worker/lambda_function.py")
worker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(worker)

OWNER, MANAGER, MEMBER, OUTSIDER, COLLEAGUE = "u-own", "u-mgr", "u-mem", "u-out", "u-col"
ORG = "wso_site0001"
OTHER_ORG = "wso_site0002"
NOW = "2026-09-20T10:00:00Z"
KEY = "recordings/u-own/mobile/mobile-site_1757000000.wav"
URL = "https://acme.my.salesforce-sites.com/services/apexrest/minutex/v1/"
SECRET = "k" * 40
TIMESTAMPS = [
    {"speaker": "0", "text": "I will send the deck.", "start": 0.0, "end": 2.0},
    {"speaker": "1", "text": "Great, thanks.", "start": 2.0, "end": 4.0},
]


def parse(resp):
    return resp["statusCode"], json.loads(resp["body"] or "{}")


def event(method, route, path=None, body=None, qs=None):
    ev = {"routeKey": f"{method} {route}",
          "requestContext": {"http": {"method": method, "path": route}},
          "headers": {"authorization": "Bearer t"},
          "pathParameters": dict(path or {})}
    if body is not None:
        ev["body"] = json.dumps(body)
    if qs is not None:
        ev["queryStringParameters"] = qs
    return ev


def call(handler, ev):
    try:
        return parse(handler(ev))
    except api.ApiError as e:
        return e.status, {"error": e.message, "code": getattr(e, "code", "")}


class SiteHarness(unittest.TestCase):
    def setUp(self):
        self.t = fdb.build_tables()
        self.t["site_endpoints"] = fdb.FakeTable("SiteDeliveryEndpoints", "workspace_id")
        # Key schema mirrors scripts/77 exactly: PK+SK, no GSI.
        self.t["people"] = fdb.FakeTable("People", "workspace_id", "person_key")
        # The authenticated-connection tables (scripts/79).
        self.t["sf_connections"] = fdb.FakeTable("SiteSalesforceConnections", "workspace_id")
        self.t["sf_schema"] = fdb.FakeTable("SiteSalesforceSchema", "workspace_id", "item_key")
        self.t["sf_mappings"] = fdb.FakeTable("SiteSalesforceTaskMappings", "workspace_id",
                                              "mapping_id")
        self.t["sf_tasks"] = fdb.FakeTable("SiteSalesforceTasks", "workspace_id", "task_ref")
        self.receiver = FakeSiteReceiver(
            contacts=[contact(f"003{i:015d}", f"Client {i}", f"client{i}@acme.com")
                      for i in range(5)],
            users=[user("005000000000000001", "Colleague", "u-col@work.com"),
                   user("005000000000000002", "Former", "former@work.com", active=False),
                   user("005000000000000003", "Sales Rep", "rep@work.com")])
        self.queue = []
        self.sqs = mock.MagicMock()
        self.sqs.send_message.side_effect = \
            lambda **kw: self.queue.append(kw["MessageBody"]) or {"MessageId": "m"}
        self.s3 = mock.MagicMock()
        self.s3.head_object.return_value = {"ContentLength": 85_000_000,
                                            "ContentType": "audio/wav"}
        self.s3.generate_presigned_url.return_value = "https://s3.example/presigned"
        self.current_user = OWNER

        patches = [
            mock.patch.object(api, "_ddb", fdb.FakeResource({t.name: t for t in self.t.values()})),
            mock.patch.object(api, "USERS_TABLE", self.t["users"].name),
            mock.patch.object(api, "_workspaces", self.t["workspaces"]),
            mock.patch.object(api, "_memberships", self.t["memberships"]),
            mock.patch.object(api, "_recordings", self.t["recordings"]),
            mock.patch.object(api, "_contacts", self.t["contacts"]),
            mock.patch.object(api, "_meeting_participants", self.t["participants"]),
            mock.patch.object(api, "_tasks", self.t["tasks"]),
            mock.patch.object(api, "_users", self.t["users"]),
            mock.patch.object(api, "_crm_sync_jobs", self.t["crm_sync_jobs"]),
            mock.patch.object(api, "_site_endpoints", self.t["site_endpoints"]),
            mock.patch.object(api, "_people", self.t["people"]),
            mock.patch.object(api, "_site_sf_connections", self.t["sf_connections"]),
            mock.patch.object(api, "_site_sf_schema", self.t["sf_schema"]),
            mock.patch.object(api, "_site_sf_mappings", self.t["sf_mappings"]),
            mock.patch.object(api, "_site_sf_tasks", self.t["sf_tasks"]),
            mock.patch.object(api, "Key", fdb.Key),
            mock.patch.object(api, "_require_auth", side_effect=lambda ev: self.current_user),
            mock.patch.object(api, "_owned_devices", return_value=[]),
            mock.patch.object(api, "_jwt_secret", return_value="test-secret"),
            mock.patch.object(api, "_integration_kms_encrypt", side_effect=lambda pt: f"ENC({pt})"),
            mock.patch.object(api, "_integration_kms_decrypt", side_effect=lambda ct: ct[4:-1]),
            mock.patch.object(api.transcript_store, "hydrate", side_effect=lambda s3, b, item: item),
            mock.patch.object(api, "_sqs_client", self.sqs),
            mock.patch.object(api, "CRM_SYNC_QUEUE_URL", "https://sqs.test/CrmSyncQueue"),
            mock.patch.object(api, "_s3", self.s3),
            mock.patch.object(api, "BUCKET_NAME", "bucket"),
            mock.patch.object(api, "SITE_AUDIO_BASE_URL", "https://api.example.com"),
            mock.patch.object(api, "_site_client", side_effect=lambda timeout=None:
                              sdc.SiteDeliveryClient(self.receiver)),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        api._site_secret_cache.clear()

        for wid, name in ((ORG, "ABC Realty"), (OTHER_ORG, "XYZ Corp")):
            self.t["workspaces"].put_item(Item={
                "workspace_id": wid, "type": ws.TYPE_ORGANISATION, "name": name,
                "owner_user_id": OWNER, "status": ws.STATUS_ACTIVE,
                "created_at": NOW, "updated_at": NOW})
        for wid, uid, role in ((ORG, OWNER, ws.ROLE_OWNER), (ORG, MANAGER, ws.ROLE_MANAGER),
                               (ORG, MEMBER, ws.ROLE_MEMBER), (ORG, COLLEAGUE, ws.ROLE_MEMBER),
                               (OTHER_ORG, OWNER, ws.ROLE_OWNER)):
            self.t["memberships"].put_item(Item=ws.new_membership(wid, uid, role, NOW))
        for uid in (OWNER, MANAGER, MEMBER, OUTSIDER, COLLEAGUE):
            self.t["users"].put_item(Item={"user_id": uid, "email": f"{uid}@work.com",
                                           "name": uid.title(), "created_at": NOW})
        self.put_meeting()

    # -- fixtures --------------------------------------------------------
    def put_meeting(self, key=KEY, workspace_id=ORG, transcript="Speaker 0: I will send the deck.",
                    **extra):
        row = {"audio_s3_key": key, "user_id": OWNER, "workspace_id": workspace_id,
               "title": "Client Discussion", "status": "complete", "source": "mobile",
               "transcript": transcript, "summary": "Deck to follow.",
               "timestamps": TIMESTAMPS, "recorded_at": "1757000000", "duration": 1800,
               "participants": [{"speaker": "0", "summary": "Owns the follow-up"}],
               "created_at": NOW}
        row.update(extra)
        self.t["recordings"].put_item(Item=row)

    def as_user(self, uid):
        self.current_user = uid

    def configure(self, workspace_id=ORG, auth_mode="none", secret=None, enabled=True,
                  as_user=OWNER, url=URL):
        self.as_user(as_user)
        body = {"url": url, "auth_mode": auth_mode, "enabled": enabled}
        if secret is not None:
            body["shared_secret"] = secret
        out = call(api.site_delivery_put, event(
            "PUT", "/workspaces/{workspace_id}/site-delivery",
            {"workspace_id": workspace_id}, body))
        self.as_user(OWNER)
        return out

    def push(self, key=KEY):
        return call(api.site_meeting_push, event(
            "POST", "/recordings/site-delivery/{key+}", {"key": key}))

    def status(self, key=KEY):
        return call(api.site_meeting_status, event(
            "GET", "/recordings/site-delivery/{key+}", {"key": key}))

    def drain(self, max_rounds=20):
        """Deliver queued messages through the REAL worker handler;
        batchItemFailures are redelivered next round (visibility timeout)."""
        rounds = 0
        while self.queue and rounds < max_rounds:
            batch, self.queue[:] = list(self.queue), []
            records = [{"messageId": f"r{rounds}-{i}", "receiptHandle": f"rh{i}",
                        "body": body} for i, body in enumerate(batch)]
            out = worker.lambda_handler({"Records": records}, None)
            failed = {f["itemIdentifier"] for f in out["batchItemFailures"]}
            self.queue.extend(r["body"] for r in records if r["messageId"] in failed)
            rounds += 1
        return rounds

    def job(self, job_id):
        return self.t["crm_sync_jobs"].get_item(Key={"job_id": job_id})["Item"]

    def delivered(self, key=KEY):
        return self.receiver.meetings.get(sc.opaque_meeting_id(key))


# ===========================================================================
class ConfigTests(SiteHarness):
    def test_owner_configures_and_secret_is_never_returned(self):
        status, body = self.configure(auth_mode="hmac_sha256", secret=SECRET)
        self.assertEqual(status, 200)
        self.assertEqual(body["endpoint"]["url"], URL)
        self.assertTrue(body["endpoint"]["has_shared_secret"])
        self.as_user(MEMBER)
        status, body = call(api.site_delivery_get, event(
            "GET", "/workspaces/{workspace_id}/site-delivery", {"workspace_id": ORG}))
        self.assertEqual(status, 200)
        self.assertNotIn(SECRET, json.dumps(body))
        self.assertNotIn("secrets_enc", json.dumps(body))
        stored = self.t["site_endpoints"].get_item(Key={"workspace_id": ORG})["Item"]
        self.assertEqual(stored["delivery_type"], "salesforce_site")
        self.assertTrue(stored["secrets_enc"].startswith("ENC("))

    def test_member_cannot_configure_and_outsider_cannot_see(self):
        self.assertEqual(self.configure(as_user=MEMBER)[0], 403)
        self.as_user(OUTSIDER)
        status, _ = call(api.site_delivery_get, event(
            "GET", "/workspaces/{workspace_id}/site-delivery", {"workspace_id": ORG}))
        self.assertEqual(status, 404)

    def test_url_must_be_a_salesforce_site(self):
        status, body = self.configure(url="https://evil.example.com/services/apexrest/minutex/v1/")
        self.assertEqual(status, 400)
        self.assertIn("Salesforce Site domain", body["error"])
        self.assertIsNone(self.t["site_endpoints"].get_item(Key={"workspace_id": ORG}).get("Item"))

    def test_hmac_requires_a_secret_then_keeps_it_on_edit(self):
        self.assertEqual(self.configure(auth_mode="hmac_sha256")[0], 400)
        self.assertEqual(self.configure(auth_mode="hmac_sha256", secret="short")[0], 400)
        self.assertEqual(self.configure(auth_mode="hmac_sha256", secret=SECRET)[0], 200)
        first = self.t["site_endpoints"].get_item(Key={"workspace_id": ORG})["Item"]
        self.assertEqual(self.configure(auth_mode="hmac_sha256")[0], 200)  # secret kept
        after = self.t["site_endpoints"].get_item(Key={"workspace_id": ORG})["Item"]
        self.assertEqual(json.loads(first["secrets_enc"][4:-1]),
                         json.loads(after["secrets_enc"][4:-1]))

    def test_none_mode_is_allowed_with_a_warning(self):
        status, body = self.configure(auth_mode="none")
        self.assertEqual(status, 200)
        self.assertTrue(body["warnings"])

    def test_delete_removes_the_endpoint(self):
        self.configure()
        status, _ = call(api.site_delivery_delete, event(
            "DELETE", "/workspaces/{workspace_id}/site-delivery", {"workspace_id": ORG}))
        self.assertEqual(status, 200)
        self.assertIsNone(self.t["site_endpoints"].get_item(Key={"workspace_id": ORG}).get("Item"))

    def test_health_ok_and_failure_are_recorded(self):
        self.configure()
        route = "/workspaces/{workspace_id}/site-delivery/health"
        status, body = call(api.site_delivery_health, event("POST", route, {"workspace_id": ORG}))
        self.assertEqual(status, 200)
        self.assertTrue(body["health"]["ok"])
        self.assertEqual(body["health"]["max_transcript_chars"], 131072)
        self.receiver.enabled = False
        status, body = call(api.site_delivery_health, event("POST", route, {"workspace_id": ORG}))
        self.assertEqual(status, 200)
        self.assertFalse(body["health"]["ok"])
        self.assertEqual(body["health"]["code"], "receiver_disabled")
        stored = self.t["site_endpoints"].get_item(Key={"workspace_id": ORG})["Item"]
        self.assertFalse(stored["last_health"]["ok"])

    def test_health_signs_with_hmac(self):
        self.receiver.secret = SECRET
        self.configure(auth_mode="hmac_sha256", secret=SECRET)
        _, body = call(api.site_delivery_health, event(
            "POST", "/workspaces/{workspace_id}/site-delivery/health", {"workspace_id": ORG}))
        self.assertTrue(body["health"]["ok"])
        self.assertIn("X-MinuteX-Signature", self.receiver.headers_seen[-1])


# ===========================================================================
class MeetingDeliveryTests(SiteHarness):
    def test_push_enqueues_a_site_job_and_calls_nothing_synchronously(self):
        self.configure()
        status, body = self.push()
        self.assertEqual(status, 202)
        self.assertEqual(body["job"]["operation"], "salesforce_site_meeting")
        self.assertEqual(body["job"]["delivery_type"], "salesforce_site")
        self.assertEqual(len(self.queue), 1)
        self.assertEqual(json.loads(self.queue[0])["operation"], "salesforce_site_meeting")
        self.assertEqual(self.receiver.calls, [])

    def test_first_delivery_creates_later_delivery_updates(self):
        self.configure()
        _, body = self.push()
        self.drain()
        job = self.job(body["job"]["job_id"])
        self.assertEqual(job["status"], "SYNCED")
        self.assertEqual(job["result"]["operation"], "created")
        self.assertFalse(job["result"]["duplicate"])
        record_id = job["result"]["external_record_id"]

        self.put_meeting(title="Client Discussion (edited)")
        _, body2 = self.push()
        self.assertNotEqual(body2["job"]["job_id"], body["job"]["job_id"])
        self.drain()
        job2 = self.job(body2["job"]["job_id"])
        self.assertEqual(job2["result"]["operation"], "updated")
        self.assertEqual(job2["result"]["external_record_id"], record_id)
        self.assertEqual(len(self.receiver.meetings), 1)
        self.assertEqual(self.delivered()["payload"]["meeting"]["title"],
                         "Client Discussion (edited)")

    def test_delivered_payload(self):
        self.t["tasks"].put_item(Item={
            "task_id": "t1", "title": "Send deck", "status": "Open",
            "source_recording_id": KEY, "created_at": NOW, "owner_user_id": OWNER})
        self.t["tasks"].put_item(Item={
            "task_id": "t2", "title": "Dropped", "status": "Cancelled",
            "source_recording_id": KEY, "created_at": NOW, "owner_user_id": OWNER})
        self.configure()
        self.push()
        self.drain()
        env = self.receiver.calls[-1]
        self.assertEqual(env["action"], "upsert_meeting")
        self.assertEqual(env["contract_version"], sc.CONTRACT_VERSION)
        self.assertTrue(env["idempotency_key"].startswith("minutex:"))
        p = env["payload"]
        self.assertEqual(p["meeting"]["id"], sc.opaque_meeting_id(KEY))
        self.assertEqual(p["meeting"]["workspace"], {"id": ORG, "name": "ABC Realty"})
        self.assertEqual(p["transcript"]["text"], "Speaker 0: I will send the deck.")
        self.assertEqual([a["title"] for a in p["action_items"]], ["Send deck"])
        self.assertEqual([x["speaker_id"] for x in p["participants"]], ["0", "1"])
        self.assertFalse(p["participants"][0]["tagged"])
        audio = p["audio"]
        self.assertTrue(audio["url"].startswith("https://api.example.com/delivery/audio/v1."))
        self.assertEqual(audio["size_bytes"], 85_000_000)
        self.assertEqual(audio["content_type"], "audio/wav")
        self.assertEqual(audio["duration_seconds"], 1800)
        raw = json.dumps(env)
        for forbidden in (OWNER, "recordings/", "__c", "salesforce_user_id", "crm_"):
            self.assertNotIn(forbidden, raw, forbidden)

    def test_push_refused_when_not_configured_or_disabled(self):
        status, body = self.push()
        self.assertEqual((status, body["code"]), (409, "site_delivery_not_configured"))
        self.configure(enabled=False)
        status, body = self.push()
        self.assertEqual((status, body["code"]), (409, "site_delivery_not_configured"))
        self.assertEqual(self.queue, [])

    def test_push_refuses_an_oversized_transcript_up_front(self):
        self.configure()
        self.put_meeting(transcript="a" * (sc.MAX_TRANSCRIPT_CHARS + 1))
        status, body = self.push()
        self.assertEqual((status, body["code"]), (422, "transcript_too_large"))
        self.assertIn("131,072", body["error"])
        self.assertEqual(self.queue, [])

    def test_transcript_at_the_limit_is_delivered_whole(self):
        self.configure()
        text = "a" * sc.MAX_TRANSCRIPT_CHARS
        self.put_meeting(transcript=text)
        self.push()
        self.drain()
        self.assertEqual(self.delivered()["payload"]["transcript"]["text"], text)

    def test_transcript_that_grew_after_enqueue_fails_as_validation_without_retry(self):
        self.configure()
        _, body = self.push()
        self.put_meeting(transcript="a" * (sc.MAX_TRANSCRIPT_CHARS + 5))
        rounds = self.drain()
        job = self.job(body["job"]["job_id"])
        self.assertEqual(job["status"], "FAILED")
        self.assertEqual(job["last_error_category"], "validation")
        self.assertEqual(job["last_error_code"], "transcript_too_large")
        self.assertEqual(job["attempt_count"], 1)
        self.assertEqual(rounds, 1)
        self.assertEqual(self.receiver.calls, [])

    def test_double_push_reuses_the_in_flight_job(self):
        self.configure()
        _, a = self.push()
        _, b = self.push()
        self.assertEqual(a["job"]["job_id"], b["job"]["job_id"])
        self.assertEqual(len(self.queue), 1)

    def test_endpoint_disabled_after_enqueue_fails_permanently(self):
        self.configure()
        _, body = self.push()
        self.configure(enabled=False)
        self.drain()
        job = self.job(body["job"]["job_id"])
        self.assertEqual(job["status"], "FAILED")
        self.assertEqual(job["last_error_code"], "site_delivery_not_configured")
        self.assertEqual(self.receiver.calls, [])

    def test_meeting_moved_to_another_workspace_is_not_delivered(self):
        self.configure()
        _, body = self.push()
        self.put_meeting(workspace_id=OTHER_ORG)
        self.drain()
        self.assertEqual(self.job(body["job"]["job_id"])["last_error_code"], "workspace_mismatch")
        self.assertEqual(self.receiver.calls, [])

    def test_personal_workspace_delivery(self):
        personal = ws.personal_workspace_id(OWNER)
        key = "recordings/u-own/mobile/personal_1.wav"
        self.put_meeting(key=key, workspace_id=personal)
        self.configure(workspace_id=personal)
        status, body = self.push(key)
        self.assertEqual(status, 202)
        self.drain()
        self.assertEqual(self.job(body["job"]["job_id"])["status"], "SYNCED")
        payload = self.delivered(key)["payload"]
        self.assertEqual(payload["meeting"]["workspace"], {"id": "", "name": ""})
        self.assertNotIn(OWNER, json.dumps(payload["meeting"]))

    def test_status_route(self):
        self.configure()
        self.assertEqual(self.status()[1]["job"], None)
        self.push()
        self.drain()
        status, body = self.status()
        self.assertEqual(status, 200)
        self.assertEqual(body["job"]["status"], "SYNCED")
        self.assertTrue(body["endpoint"]["enabled"])

    def test_no_audio_link_without_a_public_base_url(self):
        self.configure()
        with mock.patch.object(api, "SITE_AUDIO_BASE_URL", ""), \
                mock.patch.object(api, "SHARE_BASE_URL", ""):
            self.push()
            self.drain()
        self.assertFalse(self.delivered()["payload"]["audio"]["available"])


# ===========================================================================
class RetryAndIdempotencyTests(SiteHarness):
    def test_timeout_after_commit_is_retried_and_deduplicated(self):
        self.configure()
        self.receiver.faults = ["timeout_after_commit"]
        _, body = self.push()
        self.drain()
        job = self.job(body["job"]["job_id"])
        self.assertEqual(job["status"], "SYNCED")
        self.assertEqual(job["attempt_count"], 2)
        self.assertTrue(job["result"]["duplicate"])
        self.assertEqual(self.delivered()["deliveries"], 1)
        keys = [c["idempotency_key"] for c in self.receiver.calls]
        self.assertEqual(len(set(keys)), 1)  # same key on the retry
        request_ids = [c["request_id"] for c in self.receiver.calls]
        self.assertEqual(len(set(request_ids)), 2)  # new request id per attempt
        self.sqs.change_message_visibility.assert_called_once()
        self.assertEqual(self.sqs.change_message_visibility.call_args.kwargs["VisibilityTimeout"], 60)

    def test_transient_5xx_retries_then_succeeds(self):
        self.configure()
        self.receiver.faults = [("status", 503), "network"]
        _, body = self.push()
        self.drain()
        job = self.job(body["job"]["job_id"])
        self.assertEqual(job["status"], "SYNCED")
        self.assertEqual(job["attempt_count"], 3)
        self.assertEqual(job["result"]["operation"], "created")

    def test_permanent_failure_is_not_retried(self):
        self.configure()
        self.receiver.faults = [("status", 422)]
        _, body = self.push()
        rounds = self.drain()
        job = self.job(body["job"]["job_id"])
        self.assertEqual(job["status"], "FAILED")
        self.assertEqual(job["last_error_category"], "validation")
        self.assertEqual(job["attempt_count"], 1)
        self.assertEqual(rounds, 1)
        self.sqs.change_message_visibility.assert_not_called()

    def test_auth_failure_is_permission_not_retried(self):
        self.receiver.secret = SECRET
        self.configure(auth_mode="none")
        _, body = self.push()
        self.drain()
        job = self.job(body["job"]["job_id"])
        self.assertEqual((job["status"], job["last_error_category"]), ("FAILED", "permission"))

    def test_retries_are_bounded(self):
        self.configure()
        self.receiver.faults = ["timeout"] * 10
        _, body = self.push()
        self.drain()
        job = self.job(body["job"]["job_id"])
        self.assertEqual(job["status"], "FAILED")
        self.assertEqual(job["attempt_count"], api.CRM_JOB_MAX_ATTEMPTS)
        self.assertEqual(job["last_error_code"], "timeout")
        self.assertEqual(len(self.receiver.meetings), 0)

    def test_duplicate_worker_execution_after_success_makes_no_call(self):
        self.configure()
        _, body = self.push()
        message = self.queue[0]
        self.drain()
        calls = len(self.receiver.calls)
        self.queue.append(message)  # SQS at-least-once: the same message again
        self.drain()
        self.assertEqual(len(self.receiver.calls), calls)
        self.assertEqual(self.job(body["job"]["job_id"])["attempt_count"], 1)

    def test_duplicate_messages_in_one_batch_deliver_once(self):
        self.configure()
        self.push()
        self.queue.append(self.queue[0])
        self.drain()
        self.assertEqual(self.delivered()["deliveries"], 1)

    def test_manual_retry_is_a_new_intent(self):
        self.configure()
        self.receiver.faults = [("status", 400)]
        _, body = self.push()
        self.drain()
        job_id = body["job"]["job_id"]
        first_key = self.job(job_id)["result"]["idempotency_key"]
        self.assertTrue(first_key.endswith(":g0:upsert_meeting"))
        self.assertEqual(self.job(job_id)["status"], "FAILED")
        status, retry = call(api.site_meeting_retry, event(
            "POST", "/recordings/site-delivery-retry/{key+}", {"key": KEY}))
        self.assertEqual(status, 202)
        self.assertEqual(retry["job"]["job_id"], job_id)
        self.drain()
        job = self.job(job_id)
        self.assertEqual(job["status"], "SYNCED")
        self.assertEqual(job["delivery_generation"], 1)
        self.assertTrue(job["result"]["idempotency_key"].endswith(":g1:upsert_meeting"))
        self.assertNotEqual(job["result"]["idempotency_key"], first_key)

    def test_retry_refuses_a_job_that_is_not_failed(self):
        self.configure()
        self.push()
        self.drain()
        status, _ = call(api.site_meeting_retry, event(
            "POST", "/recordings/site-delivery-retry/{key+}", {"key": KEY}))
        self.assertEqual(status, 409)


# ===========================================================================
class IsolationTests(SiteHarness):
    def test_every_message_goes_to_the_site_executor_by_job_id(self):
        # A message still labelled with the retired organisation-push
        # operation is handed over too; the executor drops it after reading
        # the row (next tests), so nothing legacy can run.
        with mock.patch.object(api, "execute_site_delivery_job") as site:
            worker.lambda_handler({"Records": [{
                "messageId": "m1", "receiptHandle": "r",
                "body": json.dumps({"job_id": "j", "workspace_id": ORG,
                                    "recording_key": KEY,
                                    "operation": "organisation_meeting_crm"})}]}, None)
        site.assert_called_once_with("j")

    def test_the_old_executor_no_longer_exists(self):
        self.assertFalse(hasattr(api, "execute_crm_sync_job"))
        self.configure()
        self.push()
        self.drain()
        self.assertEqual(len(self.receiver.meetings), 1)

    def test_site_executor_drops_a_row_that_is_not_a_site_job(self):
        self.t["crm_sync_jobs"].put_item(Item={
            "job_id": "legacy1", "workspace_id": ORG, "recording_key": KEY,
            "operation": "organisation_meeting_crm", "status": "PENDING",
            "attempt_count": 0, "created_at": NOW, "updated_at": NOW})
        out = api.execute_site_delivery_job("legacy1")
        self.assertTrue(out["dropped"])
        self.assertEqual(self.job("legacy1")["status"], "PENDING")
        self.assertEqual(self.receiver.calls, [])

    def test_meeting_row_gains_no_crm_state(self):
        self.configure()
        self.push()
        self.drain()
        row = self.t["recordings"].get_item(Key={"audio_s3_key": KEY})["Item"]
        self.assertFalse([k for k in row if k.startswith("crm_")])


# ===========================================================================
class AudioLinkTests(SiteHarness):
    def delivered_token(self):
        self.configure()
        _, body = self.push()
        self.job_id = body["job"]["job_id"]
        self.drain()
        return self.delivered()["payload"]["audio"]["url"].rsplit("/", 1)[1]

    def test_token_carries_the_job_not_the_recording(self):
        token = self.delivered_token()
        self.assertEqual(sc.read_audio_token(token)["j"], self.job_id)
        self.assertNotIn("recordings", json.dumps(self.delivered()["payload"]["audio"]))

    def fetch(self, token):
        return api.site_delivery_audio(event(
            "GET", "/delivery/audio/{token}", {"token": token}))

    def test_valid_link_redirects_to_a_fresh_short_presign(self):
        resp = self.fetch(self.delivered_token())
        self.assertEqual(resp["statusCode"], 302)
        self.assertEqual(resp["headers"]["Location"], "https://s3.example/presigned")
        self.assertIn("no-store", resp["headers"]["Cache-Control"])
        kwargs = self.s3.generate_presigned_url.call_args.kwargs
        self.assertEqual(kwargs["Params"], {"Bucket": "bucket", "Key": KEY})
        self.assertEqual(kwargs["ExpiresIn"], 900)

    def test_tampered_token_is_refused(self):
        token = self.delivered_token()
        forged = sc.sign_audio_token(self.job_id, 2_000_000_000, "guessed-key")
        self.assertEqual(self.fetch(forged)["statusCode"], 404)
        self.assertEqual(self.fetch(token[:-2] + "AA")["statusCode"], 404)
        self.assertEqual(self.fetch("garbage")["statusCode"], 404)

    def test_expired_link_is_gone(self):
        self.delivered_token()
        audio_key = json.loads(self.t["site_endpoints"].get_item(
            Key={"workspace_id": ORG})["Item"]["secrets_enc"][4:-1])["audio_key"]
        expired = sc.sign_audio_token(self.job_id, 1_000, audio_key)
        self.assertEqual(self.fetch(expired)["statusCode"], 410)

    def test_a_validly_signed_token_for_a_non_site_job_is_refused(self):
        self.delivered_token()
        audio_key = json.loads(self.t["site_endpoints"].get_item(
            Key={"workspace_id": ORG})["Item"]["secrets_enc"][4:-1])["audio_key"]
        self.t["crm_sync_jobs"].put_item(Item={
            "job_id": "legacy9", "workspace_id": ORG, "recording_key": KEY,
            "operation": "organisation_meeting_crm", "status": "SYNCED",
            "created_at": NOW, "updated_at": NOW})
        token = sc.sign_audio_token("legacy9", 2_000_000_000, audio_key)
        self.assertEqual(self.fetch(token)["statusCode"], 404)

    def test_disabling_rotating_or_deleting_kills_the_link(self):
        token = self.delivered_token()
        self.configure(enabled=False)
        self.assertEqual(self.fetch(token)["statusCode"], 404)
        self.configure(enabled=True)
        self.assertEqual(self.fetch(token)["statusCode"], 302)
        self.as_user(OWNER)
        call(api.site_delivery_put, event(
            "PUT", "/workspaces/{workspace_id}/site-delivery", {"workspace_id": ORG},
            {"url": URL, "auth_mode": "none", "rotate_audio_links": True}))
        self.assertEqual(self.fetch(token)["statusCode"], 404)

    def test_deleted_meeting_link_is_dead(self):
        token = self.delivered_token()
        self.t["recordings"].delete_item(Key={"audio_s3_key": KEY})
        self.assertEqual(self.fetch(token)["statusCode"], 404)


# ===========================================================================
class PeopleSyncTests(SiteHarness):
    def start_sync(self, as_user=OWNER):
        self.as_user(as_user)
        out = call(api.site_people_sync_start, event(
            "POST", "/workspaces/{workspace_id}/site-delivery/people-sync",
            {"workspace_id": ORG}))
        self.as_user(OWNER)
        return out

    def people(self):
        rows = self.t["people"].query(KeyConditionExpression=fdb.Key("workspace_id").eq(ORG))
        return {r["person_key"]: r for r in rows["Items"]}

    def test_sync_walks_every_page_into_the_people_store(self):
        self.configure()
        with mock.patch.object(api, "SITE_PEOPLE_PAGE_SIZE", 3):
            status, body = self.start_sync()
            self.assertEqual(status, 202)
            self.drain()
        job = self.job(body["job"]["job_id"])
        self.assertEqual(job["status"], "SYNCED")
        self.assertEqual(job["result"]["counts"]["created"], 8)
        self.assertEqual(job["result"]["pages"], 3)
        people = self.people()
        self.assertEqual(len(people), 8)
        self.assertEqual(people["salesforce#contact#003000000000000001"]["email"],
                         "client1@acme.com")
        self.assertEqual(people["salesforce#user#005000000000000002"]["status"], "inactive")
        summary = self.t["site_endpoints"].get_item(Key={"workspace_id": ORG})["Item"]["people_sync"]
        self.assertEqual(summary["status"], "SYNCED")

    def test_resync_is_idempotent_and_marks_removed_people_deleted(self):
        self.configure()
        self.start_sync()
        self.drain()
        self.receiver.contacts.pop(0)
        self.receiver.contacts[0]["name"] = "Client One Renamed"
        _, body = self.start_sync()
        self.drain()
        counts = self.job(body["job"]["job_id"])["result"]["counts"]
        self.assertEqual(counts["created"], 0)
        self.assertEqual(counts["updated"], 1)
        self.assertEqual(counts["unchanged"], 6)
        self.assertEqual(counts["deleted"], 1)
        self.assertEqual(self.people()["salesforce#contact#003000000000000000"]["status"],
                         "deleted")

    def test_large_sync_continues_in_segments(self):
        self.configure()
        with mock.patch.object(api, "SITE_PEOPLE_PAGE_SIZE", 2), \
                mock.patch.object(api, "SITE_PEOPLE_SYNC_BUDGET_SECONDS", -1):
            _, body = self.start_sync()
            rounds = self.drain()
        job = self.job(body["job"]["job_id"])
        self.assertEqual(job["status"], "SYNCED")
        self.assertEqual(len(self.people()), 8)
        self.assertGreaterEqual(rounds, 4)  # one invocation per segment

    def test_transient_failure_resumes_from_the_saved_cursor(self):
        self.configure()
        with mock.patch.object(api, "SITE_PEOPLE_PAGE_SIZE", 3):
            self.receiver.faults = [None, ("status", 503)]
            _, body = self.start_sync()
            self.drain()
        job = self.job(body["job"]["job_id"])
        self.assertEqual(job["status"], "SYNCED")
        cursors = [c["payload"]["cursor"] for c in self.receiver.calls]
        # The failed page-2 request never reached the receiver; the retry
        # resumed at page 2 rather than starting over.
        self.assertEqual(cursors[:2], ["", "contact:003000000000000002"])
        self.assertEqual(len(cursors), len(set(cursors)))
        self.assertEqual(len(self.people()), 8)
        self.assertEqual(job["result"]["counts"]["created"], 8)

    def test_failure_while_finalizing_does_not_rewalk(self):
        self.configure()
        real = people_store.mark_missing
        calls = {"n": 0}

        def flaky(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("DynamoDB hiccup")
            return real(*a, **kw)

        with mock.patch.object(api, "SITE_PEOPLE_PAGE_SIZE", 3),                 mock.patch.object(people_store, "mark_missing", side_effect=flaky):
            _, body = self.start_sync()
            self.drain()
        job = self.job(body["job"]["job_id"])
        self.assertEqual(job["status"], "SYNCED")
        self.assertEqual(len(self.receiver.calls), 3)  # three pages, walked once
        self.assertEqual(calls["n"], 2)

    def test_only_managers_start_a_sync_but_members_search(self):
        self.configure()
        self.assertEqual(self.start_sync(as_user=MEMBER)[0], 403)
        self.start_sync()
        self.drain()
        self.as_user(MEMBER)
        status, body = call(api.people_search, event(
            "GET", "/workspaces/{workspace_id}/people", {"workspace_id": ORG},
            qs={"q": "client 3"}))
        self.assertEqual(status, 200)
        self.assertEqual([p["name"] for p in body["people"]], ["Client 3"])
        _, body = call(api.people_search, event(
            "GET", "/workspaces/{workspace_id}/people", {"workspace_id": ORG},
            qs={"type": "user"}))
        self.assertEqual({p["name"] for p in body["people"]}, {"Colleague", "Sales Rep"})
        _, body = call(api.people_search, event(
            "GET", "/workspaces/{workspace_id}/people", {"workspace_id": ORG},
            qs={"type": "user", "include_inactive": "true"}))
        self.assertIn("Former", {p["name"] for p in body["people"]})
        self.as_user(OUTSIDER)
        status, _ = call(api.people_search, event(
            "GET", "/workspaces/{workspace_id}/people", {"workspace_id": ORG}, qs={}))
        self.assertEqual(status, 404)

    def test_people_are_scoped_to_their_workspace(self):
        self.configure()
        self.start_sync()
        self.drain()
        _, body = call(api.people_search, event(
            "GET", "/workspaces/{workspace_id}/people", {"workspace_id": OTHER_ORG}, qs={}))
        self.assertEqual(body["people"], [])


# ===========================================================================
class TaggingTests(SiteHarness):
    def setUp(self):
        super().setUp()
        self.configure()
        call(api.site_people_sync_start, event(
            "POST", "/workspaces/{workspace_id}/site-delivery/people-sync",
            {"workspace_id": ORG}))
        self.drain()
        self.receiver.calls.clear()

    def tag(self, speaker_id, person_key, **extra):
        return call(api.site_participant_tag, event(
            "PUT", "/recordings/site-participants/{key+}", {"key": KEY},
            {"speaker_id": speaker_id, "person_key": person_key, **extra}))

    def test_tagging_a_contact_creates_one_linked_contact_without_calling_salesforce(self):
        status, body = self.tag("0", "salesforce#contact#003000000000000002")
        self.assertEqual(status, 200)
        contact_id = body["participant"]["contact_id"]
        row = self.t["contacts"].get_item(Key={"contact_id": contact_id})["Item"]
        self.assertEqual(row["email"], "client2@acme.com")
        self.assertEqual(row["workspace_id"], ORG)
        self.assertEqual(row["source"], "people_sync")
        self.assertEqual(row["people_external_id"], "003000000000000002")
        self.assertEqual(body["participant"]["identity_role"], "external")
        rec = self.t["recordings"].get_item(Key={"audio_s3_key": KEY})["Item"]
        self.assertEqual(rec["speaker_names"]["0"], "Client 2")
        self.assertEqual(self.receiver.calls, [])  # local People Store only
        person = people_store.get_person(self.t["people"], ORG,
                                         "salesforce#contact#003000000000000002")
        self.assertEqual(person["linked_contact_id"], contact_id)

        _, again = self.tag("1", "salesforce#contact#003000000000000002")
        self.assertEqual(again["participant"]["contact_id"], contact_id)

    def test_existing_contact_with_the_same_email_is_linked_not_duplicated(self):
        existing = api._contact_item(OWNER, "Client Four", email="client4@acme.com",
                                     workspace_id=ORG)
        self.t["contacts"].put_item(Item=existing)
        _, body = self.tag("0", "salesforce#contact#003000000000000004")
        self.assertEqual(body["participant"]["contact_id"], existing["contact_id"])
        row = self.t["contacts"].get_item(Key={"contact_id": existing["contact_id"]})["Item"]
        self.assertEqual(row["people_key"], "salesforce#contact#003000000000000004")

    def test_a_salesforce_user_who_is_a_member_tags_as_that_member(self):
        _, body = self.tag("1", "salesforce#user#005000000000000001")
        self.assertEqual(body["participant"]["contact_id"], api._member_contact_id(COLLEAGUE))
        self.assertEqual(body["participant"]["identity_role"], "internal")

    def test_deleted_or_unknown_people_cannot_be_tagged(self):
        self.t["people"].update_item(
            Key={"workspace_id": ORG, "person_key": "salesforce#contact#003000000000000003"},
            UpdateExpression="SET #s = :d", ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":d": "deleted"})
        self.assertEqual(self.tag("0", "salesforce#contact#003000000000000003")[0], 404)
        self.assertEqual(self.tag("0", "salesforce#contact#nope")[0], 404)
        self.assertEqual(self.tag("0", "")[0], 400)

    def test_only_the_meeting_owner_can_tag(self):
        self.as_user(MEMBER)
        self.assertEqual(self.tag("0", "salesforce#contact#003000000000000002")[0], 404)

    def test_end_to_end_sync_tag_push(self):
        """Salesforce Contacts+Users -> People Sync -> People Store -> tag ->
        push -> CrmSyncJobs -> SQS -> worker -> Site -> record."""
        self.tag("0", "salesforce#contact#003000000000000002")
        self.tag("1", "salesforce#user#005000000000000003")
        _, body = self.push()
        self.drain()
        self.assertEqual(self.job(body["job"]["job_id"])["status"], "SYNCED")
        parts = self.delivered()["payload"]["participants"]
        by_speaker = {p["speaker_id"]: p for p in parts}
        self.assertEqual(by_speaker["0"]["name"], "Client 2")
        self.assertEqual(by_speaker["0"]["email"], "client2@acme.com")
        self.assertEqual(by_speaker["0"]["external_identity"],
                         {"provider": "salesforce", "type": "contact",
                          "external_id": "003000000000000002"})
        self.assertEqual(by_speaker["0"]["contribution_summary"], "Owns the follow-up")
        self.assertEqual(by_speaker["1"]["external_identity"]["type"], "user")
        self.assertEqual(by_speaker["1"]["identity_role"], "internal")
        self.assertTrue(all(p["tagged"] for p in parts))


if __name__ == "__main__":
    unittest.main()
