#!/usr/bin/env python3
"""salesforce_harness.py — the shared offline harness for the Salesforce
tests (the webhook delivery the MinuteX package receives, and the
integration-user OAuth connection).

    route -> CrmSyncJobs -> SQS -> crmSyncWorker.lambda_handler
          -> execute_site_delivery_job -> receiver

Everything above the network is the REAL code: the userapi routes, the job
table and the worker Lambda's own handler. Each test module supplies its own
fake receiver.
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
import workspace_schema as ws  # noqa: E402

# The worker's own handler, loaded exactly as test_crm_sync_worker_lambda
# does (the deployed zip packages userapi as `userapi_worker`).
sys.modules["userapi_worker"] = api
_spec = importlib.util.spec_from_file_location(
    "crm_sync_worker_for_salesforce_tests",
    ROOT / "functions/crm-sync-worker/lambda_function.py")
worker = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(worker)

OWNER, MANAGER, MEMBER, OUTSIDER, COLLEAGUE = "u-own", "u-mgr", "u-mem", "u-out", "u-col"
ORG = "wso_site0001"
OTHER_ORG = "wso_site0002"
NOW = "2026-09-20T10:00:00Z"
KEY = "recordings/u-own/mobile/mobile-site_1757000000.wav"
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


class SalesforceHarness(unittest.TestCase):
    def setUp(self):
        self.t = fdb.build_tables()
        self.t["site_endpoints"] = fdb.FakeTable("SiteDeliveryEndpoints", "workspace_id")
        # Key schema mirrors scripts/77 exactly: PK+SK, no GSI.
        self.t["people"] = fdb.FakeTable("People", "workspace_id", "person_key")
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
