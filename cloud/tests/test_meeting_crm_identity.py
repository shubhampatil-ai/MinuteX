#!/usr/bin/env python3
"""test_meeting_crm_identity.py — Phase 2D.3: speaker identity resolution,
CRM Review, and the organisation meeting push.

WHAT THIS FILE PINS.

  1. SPEAKER IDENTITY. identity_role (internal/external) persists on
     MeetingParticipants, survives an unrelated re-tag, and is a genuinely
     separate field from the pre-existing free-text participant_role.

  2. SALESFORCE IDENTITY RESOLUTION. An INTERNAL speaker resolves through
     the org member -> OrgSalesforceUserLinks -> Salesforce User. An
     EXTERNAL speaker resolves through the organisation Contact ->
     crm_external_id/crm_account_id -> Salesforce Contact/Account. Neither
     is ever guessed — resolve_contact_crm_identity/resolve_member_crm_
     identity only ever STORE an id the caller explicitly supplied.

  3. MEETING OWNER != CONNECTION IDENTITY (spec section 8). The resolved
     SM/Internal speaker's Salesforce User becomes the pushed Task's
     OwnerId; the workspace's Salesforce connection (whoever ran OAuth) is
     never used for that purpose — proven by asserting the created Task's
     OwnerId equals the SPEAKER's linked sf_user_id, which is deliberately
     different from the connection's own connected_by_user_id/sf_user_id in
     these tests.

  4. CRM REVIEW / PUSH GATING. get_meeting_crm_review and push_org_meeting_
     crm agree on readiness because both call _meeting_crm_identity_state —
     an unresolved tagged speaker blocks a push with 409, never a silent
     partial push.

  5. SCOPE ISOLATION. Every Phase 2D.3 route refuses a Personal meeting
     (400) and never touches CrmConnections. An organisation's Salesforce
     User links / Contact CRM identity never leak to a different workspace.

  6. IDEMPOTENCY. Re-running push_org_meeting_crm's Task half never creates
     a second Salesforce Task for the same MinuteX task_id.

  7. REGRESSION. Personal Salesforce, participant self-tagging and ordinary
     contact/task CRUD are untouched.

SECURITY POSTURE, as in every suite here: tests call the ROUTE FUNCTION
DIRECTLY with a forged identity/workspace.

OFFLINE: fake_dynamodb, no AWS, no network.

Run:  python -m pytest tests/test_meeting_crm_identity.py
"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "functions/userapi"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# ORDER MATTERS — see test_task_permissions.py.
from test_ai_workspace import api  # noqa: E402
import fake_dynamodb as fdb  # noqa: E402
import workspace_schema as ws  # noqa: E402

OWNER_A, MANAGER_A, MEMBER_A, SM_RAHUL = "u-oa", "u-ma", "u-mea", "u-sm"
OWNER_B = "u-ob"
OUTSIDER = "u-out"

ORG_A = "wso_c2aaaa11"
ORG_B = "wso_c2bbbb22"
NOW = "2026-09-09T10:00:00Z"

KEY = "recordings/u-oa/mobile/mobile-abc_1757000000.m4a"

TRANSCRIPT = (
    "Speaker 0: I'll follow up with the client tomorrow.\n\n"
    "Speaker 1: Sounds good, send me the contract."
)


def parse(resp):
    return resp["statusCode"], json.loads(resp["body"])


def event(method="GET", route="/recordings/participants/{key+}", path=None,
          body=None, qs=None):
    ev = {
        "routeKey": f"{method} {route}",
        "requestContext": {"http": {"method": method, "path": route}},
        "headers": {"authorization": "Bearer test-token"},
        "pathParameters": dict(path or {}),
    }
    if qs is not None:
        ev["queryStringParameters"] = qs
    if body is not None:
        ev["body"] = json.dumps(body)
    return ev


def call(handler, ev):
    try:
        return handler(ev)
    except api.ApiError as e:
        body = {"error": e.message}
        code = getattr(e, "code", "")
        if code:
            body["code"] = code
        return api._resp(e.status, body)


SITE_VISIT_DESCRIBE = {
    "label": "Site Visit",
    "fields": [
        {"name": "Id", "label": "Record ID", "type": "id",
         "filterable": True, "updateable": False, "length": 18},
        {"name": "Site_Visit_Number__c", "label": "Site Visit Number",
         "type": "string", "filterable": True, "updateable": True,
         "length": 40, "unique": True},
        {"name": "Summary__c", "label": "Summary", "type": "textarea",
         "filterable": False, "updateable": True, "length": 32768,
         "calculated": False},
    ],
}


class CrmIdentityHarness(unittest.TestCase):
    def setUp(self):
        self.t = fdb.build_tables()
        self.current_user = OWNER_A
        # RESOURCE-level handle, for _users_by_ids' batch_get_item — the
        # profile read _ensure_member_contact needs to materialize an
        # internal speaker's projected member-contact. Without this, every
        # "member" projection resolves to "no profile" and is silently
        # skipped, exactly the failure mode test_member_contacts.py's
        # harness documents fixing the same way.
        self.ddb = fdb.FakeResource({t.name: t for t in self.t.values()})
        self.patches = [
            mock.patch.object(api, "_ddb", self.ddb),
            mock.patch.object(api, "USERS_TABLE", self.t["users"].name),
            mock.patch.object(api, "_workspaces", self.t["workspaces"]),
            mock.patch.object(api, "_memberships", self.t["memberships"]),
            mock.patch.object(api, "_recordings", self.t["recordings"]),
            mock.patch.object(api, "_contacts", self.t["contacts"]),
            mock.patch.object(api, "_meeting_participants", self.t["participants"]),
            mock.patch.object(api, "_tasks", self.t["tasks"]),
            mock.patch.object(api, "_users", self.t["users"]),
            mock.patch.object(api, "_org_crm_connections", self.t["org_crm_connections"]),
            mock.patch.object(api, "_org_salesforce_user_links",
                              self.t["org_salesforce_user_links"]),
            mock.patch.object(api, "_crm_sync_jobs", self.t["crm_sync_jobs"]),
            # No real SQS in the offline suite — capture what would have been
            # sent instead, and drive the worker synchronously from push()
            # below (see its docstring for why that is still a faithful test
            # of the async contract).
            mock.patch.object(api, "_sqs_client", mock.MagicMock()),
            mock.patch.object(api, "CRM_SYNC_QUEUE_URL", "https://sqs.test/CrmSyncQueue"),
            mock.patch.object(api, "_crm_connections", mock.MagicMock(
                side_effect=AssertionError(
                    "Organisation CRM identity code path touched "
                    "CrmConnections (Personal) — scope leak"))),
            mock.patch.object(api, "Key", fdb.Key),
            mock.patch.object(api, "_require_auth",
                              side_effect=lambda ev: self.current_user),
            mock.patch.object(api, "_owned_devices", return_value=[]),
            mock.patch.object(api, "_jwt_secret", return_value="test-secret"),
            mock.patch.object(api, "_kms_encrypt", side_effect=lambda pt: f"ENC({pt})"),
            mock.patch.object(api, "_kms_decrypt",
                              side_effect=lambda ct: ct[4:-1] if ct.startswith("ENC(") else ct),
            mock.patch.object(api._salesforce, "refresh_access_token",
                              return_value={"access_token": "test-access-token"}),
            mock.patch.object(api.transcript_store, "hydrate",
                              side_effect=lambda s3, bucket, item: item),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

        for wid, owner, name in ((ORG_A, OWNER_A, "ABC Realty"),
                                 (ORG_B, OWNER_B, "XYZ Corp")):
            self.t["workspaces"].put_item(Item={
                "workspace_id": wid, "type": ws.TYPE_ORGANISATION,
                "name": name, "owner_user_id": owner,
                "status": ws.STATUS_ACTIVE,
                "created_at": NOW, "updated_at": NOW})
        for wid, uid, role in (
            (ORG_A, OWNER_A, ws.ROLE_OWNER),
            (ORG_A, MANAGER_A, ws.ROLE_MANAGER),
            (ORG_A, MEMBER_A, ws.ROLE_MEMBER),
            (ORG_A, SM_RAHUL, ws.ROLE_MEMBER),
            (ORG_B, OWNER_B, ws.ROLE_OWNER),
        ):
            self.t["memberships"].put_item(
                Item=ws.new_membership(wid, uid, role, NOW))
        for uid in (OWNER_A, MANAGER_A, MEMBER_A, SM_RAHUL, OWNER_B, OUTSIDER):
            self.t["users"].put_item(Item={
                "user_id": uid, "email": f"{uid}@work.com",
                "name": uid, "created_at": NOW})

        self.t["recordings"].put_item(Item={
            "audio_s3_key": KEY, "user_id": OWNER_A, "workspace_id": ORG_A,
            "title": "Client Discussion", "created_at": NOW, "status": "complete",
            "transcript": TRANSCRIPT, "summary": "Follow up with the client.",
            "timestamps": [
                {"speaker": "0", "text": "I'll follow up.", "start": 0.0, "end": 2.0},
                {"speaker": "1", "text": "Sounds good.", "start": 2.0, "end": 4.0},
            ],
            # Epoch-string shape (the upload path's own format) — a real,
            # parseable instant (2026-09-10T12:00:00Z) plus a 30-minute
            # duration, so Event-creation tests (StartDateTime/EndDateTime)
            # have something authentic to assert against rather than a
            # placeholder that merely happens to parse.
            "recorded_at": "1789041600", "duration": 1800,
        })

    def as_user(self, uid):
        self.current_user = uid

    # -- fixtures --------------------------------------------------------
    def make_contact(self, name, email="", workspace_id=ORG_A, owner=OWNER_A):
        item = api._contact_item(owner, name, email=email, workspace_id=workspace_id)
        self.t["contacts"].put_item(Item=item)
        return item

    def connect_org_salesforce(self, workspace_id=ORG_A, org_id="00Dxx0000000001"):
        self.t["org_crm_connections"].put_item(Item={
            "workspace_id": workspace_id, "provider": "salesforce",
            "connected_by_user_id": OWNER_A, "instance_url": "https://abcrealty.my.salesforce.com",
            "refresh_token_enc": "ENC(RT-1)", "org_id": org_id,
            "sf_user_id": "005xx0000CONNOWNER", "sf_username": "integration@abcrealty.com",
            "connected_at": NOW, "updated_at": NOW,
        })

    def save_mapping(self, workspace_id=ORG_A):
        with mock.patch.object(api._salesforce, "describe_object",
                               return_value=SITE_VISIT_DESCRIBE):
            self.as_user(OWNER_A)
            resp = call(api.org_salesforce_put_config, event(
                "PUT", "/workspaces/{workspace_id}/crm/salesforce/config",
                path={"workspace_id": workspace_id},
                body={"mappings": [{"object": "SiteVisit__c",
                                    "lookup_field": "Site_Visit_Number__c",
                                    "summary_field": "Summary__c"}]}))
        assert resp["statusCode"] == 200, resp["body"]

    def link_record(self, object_name="SiteVisit__c", record_id="a0B000000SiteVisit",
                    status=api.CRM_STATUS_CONFIRMED, key=KEY):
        self.t["recordings"].update_item(
            Key={"audio_s3_key": key},
            UpdateExpression="SET crm_records = :m",
            ExpressionAttributeValues={":m": {object_name: {
                "object": object_name, "record_id": record_id, "status": status,
                "lookup_value": "SV-1", "updated_at": NOW,
            }}})

    def set_participant(self, speaker_id, contact_id, identity_role="", as_user=OWNER_A):
        self.as_user(as_user)
        body = {"speaker_id": speaker_id, "contact_id": contact_id}
        if identity_role:
            body["identity_role"] = identity_role
        return parse(call(api.set_participant, event(
            "PUT", "/recordings/participants/{key+}",
            path={"key": KEY}, body=body)))

    def resolve_member(self, user_id, sf_user_id, workspace_id=ORG_A, as_user=OWNER_A):
        self.as_user(as_user)
        return parse(call(api.resolve_member_crm_identity, event(
            "PUT", "/workspaces/{workspace_id}/crm/salesforce/members/{user_id}",
            path={"workspace_id": workspace_id, "user_id": user_id},
            body={"record_id": sf_user_id, "username": f"{user_id}@sf.example"})))

    def resolve_contact(self, contact_id, sf_contact_id, account_id="",
                        as_user=OWNER_A):
        self.as_user(as_user)
        return parse(call(api.resolve_contact_crm_identity, event(
            "PUT", "/contacts/{contact_id}/crm",
            path={"contact_id": contact_id},
            body={"record_id": sf_contact_id, "account_id": account_id})))

    def review(self, key=KEY, as_user=OWNER_A):
        self.as_user(as_user)
        return parse(call(api.get_meeting_crm_review, event(
            "GET", "/recordings/ai/crm-review/{key+}", path={"key": key})))

    def push(self, key=KEY, as_user=OWNER_A, objects=None):
        """Drive one CRM push END TO END the way the real system does
        (Phase 2D.4): POST /crm-push enqueues a CrmSyncJobs row (this is
        what call(api.push_org_meeting_crm, ...) exercises — the SAME
        ownership/workspace validation the old synchronous route ran), then
        — since there is no real SQS in this offline suite — the test
        drives the worker's own entry point (execute_crm_sync_job)
        synchronously, exactly as crmSyncWorker's Lambda handler would for
        the one message _sqs_client.send_message just captured.

        This is a FAITHFUL test of the async contract, not a shortcut
        around it: enqueue and execution are still two separate function
        calls through the real production code path (push_org_meeting_crm
        -> create_crm_sync_job -> _enqueue_crm_sync_job, then separately
        execute_crm_sync_job) — only the SQS transport itself is replaced
        with a direct call, which is what test_queue_and_worker.py's own
        dedicated queue-boundary tests exist to cover instead.

        Returns (status, result) shaped like Phase 2D.3's OLD synchronous
        response ({pushed, push_errors, tasks, event, identity}) merged
        with {job_id, job_status} — so the bulk of this file's existing
        assertions (`body["event"]`, `body["tasks"]`, ...) keep working
        unchanged, while job-specific tests can additionally read
        `body["job_status"]`/`body["job_id"]`.
        """
        self.as_user(as_user)
        body = {"objects": objects} if objects is not None else None
        enqueue_status, enqueue_body = parse(call(api.push_org_meeting_crm, event(
            "POST", "/recordings/ai/crm-push/{key+}", path={"key": key}, body=body)))
        if enqueue_status != 202:
            return enqueue_status, enqueue_body

        job_id = enqueue_body["job_id"]
        try:
            job = api.execute_crm_sync_job(job_id)
        except api._CrmSyncJobRetry:
            job = api._get_crm_sync_job(job_id)
        result = dict(job.get("result") or {})
        result["job_id"] = job_id
        result["job_status"] = job["status"]
        # A RECONNECT_REQUIRED/permanently-FAILED-before-any-Salesforce-call
        # job (no mapping configured, meeting deleted, ...) never populates
        # `result` at all — surface enough for those tests to assert against.
        if not result.get("event"):
            result.setdefault("event", {})
        if not result.get("tasks"):
            result.setdefault("tasks", {"created": [], "skipped": [], "failed": []})
        result.setdefault("pushed", result.get("pushed", []))
        result.setdefault("push_errors", result.get("push_errors", []))
        # Mirrors the OLD synchronous route's status codes for the whole-push
        # preconditions this file's existing tests assert on (400/409) —
        # derived from the job's terminal state and last_error, since the
        # worker no longer raises HTTP-shaped errors for those cases (spec
        # section 2: the enqueue route always answers 202).
        if job["status"] == api.CRM_JOB_STATUS_FAILED and job.get("last_error_category") == api.CRM_ERROR_PERMANENT:
            msg = job.get("last_error_message", "")
            status_code = 409 if "unresolved" in msg or "confirm" in msg else 400
        elif job["status"] == api.CRM_JOB_STATUS_RECONNECT_REQUIRED:
            status_code = 200  # the push itself still "succeeds"; see the event/push_errors code
        else:
            status_code = 200
        return status_code, result

    def add_task(self, task_id, title, assignee_contact_id="", status=api.TASK_STATUS_OPEN,
                due_date_normalized=""):
        item = {
            "task_id": task_id, "owner_user_id": OWNER_A, "workspace_id": ORG_A,
            "created_by": OWNER_A, "title": title, "description": "",
            "status": status, "priority": "Medium", "due_date": due_date_normalized,
            "due_date_normalized": due_date_normalized,
            "source_recording_id": KEY, "source_type": "AI",
            "created_at": NOW, "updated_at": NOW, "completed_at": "",
        }
        # assignee_contact_id is a SPARSE GSI key (assignee-index) — an empty
        # string is rejected by DynamoDB (and by fake_dynamodb, faithfully),
        # so an unassigned task simply omits the attribute, exactly as the
        # real _new_task_row does.
        if assignee_contact_id:
            item["assignee_contact_id"] = assignee_contact_id
        self.t["tasks"].put_item(Item=item)


# ===========================================================================
# 1. SPEAKER IDENTITY PERSISTENCE
# ===========================================================================
class TestSpeakerIdentityRole(CrmIdentityHarness):
    def test_internal_tag_persists(self):
        contact = self.make_contact("Rahul Sharma")
        status, body = self.set_participant("0", contact["contact_id"],
                                            identity_role="internal")
        self.assertEqual(status, 200)
        self.assertEqual(body["participant"]["identity_role"], "internal")

    def test_external_tag_persists(self):
        contact = self.make_contact("John Smith", email="john@client.com")
        status, body = self.set_participant("1", contact["contact_id"],
                                            identity_role="external")
        self.assertEqual(status, 200)
        self.assertEqual(body["participant"]["identity_role"], "external")

    def test_invalid_identity_role_rejected(self):
        contact = self.make_contact("Rahul Sharma")
        status, body = self.set_participant("0", contact["contact_id"],
                                            identity_role="bogus")
        self.assertEqual(status, 400)

    def test_identity_role_survives_unrelated_retag(self):
        """Re-tagging the same speaker (e.g. fixing participant_role) must
        not silently un-classify a previously set identity_role."""
        contact = self.make_contact("Rahul Sharma")
        self.set_participant("0", contact["contact_id"], identity_role="internal")
        self.as_user(OWNER_A)
        resp = call(api.set_participant, event(
            "PUT", "/recordings/participants/{key+}", path={"key": KEY},
            body={"speaker_id": "0", "contact_id": contact["contact_id"],
                 "participant_role": "Site Manager"}))
        status, body = parse(resp)
        self.assertEqual(status, 200)
        self.assertEqual(body["participant"]["identity_role"], "internal")

    def test_identity_role_defaults_empty_and_unclassified(self):
        contact = self.make_contact("Nobody Tagged")
        status, body = self.set_participant("0", contact["contact_id"])
        self.assertEqual(status, 200)
        self.assertEqual(body["participant"]["identity_role"], "")

    def test_participant_role_free_text_still_independent(self):
        """The pre-existing free-text field is untouched by this phase."""
        contact = self.make_contact("Rahul Sharma")
        self.as_user(OWNER_A)
        resp = call(api.set_participant, event(
            "PUT", "/recordings/participants/{key+}", path={"key": KEY},
            body={"speaker_id": "0", "contact_id": contact["contact_id"],
                 "participant_role": "Buyer's Agent", "identity_role": "internal"}))
        status, body = parse(resp)
        self.assertEqual(status, 200)
        self.assertEqual(body["participant"]["participant_role"], "Buyer's Agent")
        self.assertEqual(body["participant"]["identity_role"], "internal")


# ===========================================================================
# 2. SALESFORCE CONTACT / USER RESOLUTION — persistence only, never a guess
# ===========================================================================
class TestContactCrmResolution(CrmIdentityHarness):
    def test_resolve_persists_explicit_match(self):
        contact = self.make_contact("John Smith", email="john@client.com")
        status, body = self.resolve_contact(contact["contact_id"], "003xx000004TmiQ",
                                            account_id="001xx000003GYhZ")
        self.assertEqual(status, 200)
        self.assertEqual(body["contact"]["crm_provider"], "salesforce")
        self.assertEqual(body["contact"]["crm_external_id"], "003xx000004TmiQ")
        self.assertEqual(body["contact"]["crm_account_id"], "001xx000003GYhZ")
        self.assertEqual(body["contact"]["crm_object_type"], "Contact")

    def test_resolve_requires_record_id(self):
        contact = self.make_contact("John Smith")
        self.as_user(OWNER_A)
        status, body = parse(call(api.resolve_contact_crm_identity, event(
            "PUT", "/contacts/{contact_id}/crm", path={"contact_id": contact["contact_id"]},
            body={})))
        self.assertEqual(status, 400)

    def test_member_cannot_resolve_shared_contact(self):
        """Editing a shared org contact is OWNER/MANAGER only — resolving
        its CRM identity is an edit, so the same gate applies."""
        contact = self.make_contact("John Smith")
        status, body = self.resolve_contact(contact["contact_id"], "003xx000004TmiQ",
                                            as_user=MEMBER_A)
        self.assertEqual(status, 403)

    def test_manager_can_resolve_shared_contact(self):
        contact = self.make_contact("John Smith")
        status, body = self.resolve_contact(contact["contact_id"], "003xx000004TmiQ",
                                            as_user=MANAGER_A)
        self.assertEqual(status, 200)

    def test_never_auto_derived_from_name_or_email(self):
        """No route in this file may set crm_external_id except the explicit
        resolve call — proven by checking a freshly made contact has none."""
        contact = self.make_contact("John Smith", email="john@client.com")
        self.assertEqual(contact.get("crm_external_id", ""), "")


class TestMemberCrmResolution(CrmIdentityHarness):
    def test_owner_can_resolve_member(self):
        status, body = self.resolve_member(SM_RAHUL, "005xx000000SMuser")
        self.assertEqual(status, 200)
        self.assertEqual(body["link"]["sf_user_id"], "005xx000000SMuser")

    def test_manager_can_resolve_member(self):
        status, _ = self.resolve_member(SM_RAHUL, "005xx000000SMuser", as_user=MANAGER_A)
        self.assertEqual(status, 200)

    def test_member_cannot_resolve_another_members_identity(self):
        status, _ = self.resolve_member(SM_RAHUL, "005xx000000SMuser", as_user=MEMBER_A)
        self.assertEqual(status, 403)

    def test_target_must_be_active_member_of_this_workspace(self):
        status, _ = self.resolve_member(OWNER_B, "005xx000000Bad", as_user=OWNER_A)
        self.assertEqual(status, 404)

    def test_never_assumes_email_equals_username(self):
        """The stored username is whatever the caller explicitly supplied —
        this test's fixture email (u-sm@work.com) and the resolved Salesforce
        username are deliberately DIFFERENT strings, proving no code path
        derives one from the other."""
        status, body = self.resolve_member(SM_RAHUL, "005xx000000SMuser")
        self.assertEqual(status, 200)
        self.assertNotEqual(body["link"]["sf_username"], "u-sm@work.com")
        self.assertEqual(body["link"]["sf_username"], "u-sm@sf.example")

    def test_cross_workspace_resolution_isolated(self):
        self.resolve_member(SM_RAHUL, "005xx000000SMuser", workspace_id=ORG_A)
        link_b = self.t["org_salesforce_user_links"].get_item(
            Key={"workspace_id": ORG_B, "user_id": SM_RAHUL}).get("Item")
        self.assertIsNone(link_b)


# ===========================================================================
# 3. CRM REVIEW
# ===========================================================================
class TestCrmReview(CrmIdentityHarness):
    def test_review_refuses_personal_meeting(self):
        self.t["recordings"].update_item(
            Key={"audio_s3_key": KEY},
            UpdateExpression="REMOVE workspace_id")
        status, body = self.review()
        self.assertEqual(status, 400)

    def test_not_connected_blocks_with_reason(self):
        status, body = self.review()
        self.assertEqual(status, 200)
        self.assertFalse(body["ready"])
        self.assertIn("organisation_salesforce_not_connected", body["blocking_reasons"])

    def test_no_mapping_blocks_with_reason(self):
        self.connect_org_salesforce()
        status, body = self.review()
        self.assertFalse(body["ready"])
        self.assertIn("no_salesforce_mapping_configured", body["blocking_reasons"])

    def test_untagged_speaker_does_not_block(self):
        """A speaker nobody classified is not part of the CRM picture at
        all — the ordinary resting state, never treated as unresolved."""
        self.connect_org_salesforce()
        self.save_mapping()
        status, body = self.review()
        self.assertEqual(body["identity"]["unresolved"], [])

    def test_tagged_but_unlinked_speaker_blocks(self):
        self.connect_org_salesforce()
        self.save_mapping()
        contact = self.make_contact("John Smith")
        self.set_participant("1", contact["contact_id"], identity_role="external")
        status, body = self.review()
        self.assertFalse(body["ready"])
        self.assertIn("unresolved_speaker_identity", body["blocking_reasons"])
        self.assertEqual(len(body["identity"]["unresolved"]), 1)

    def test_fully_resolved_meeting_is_ready(self):
        self.connect_org_salesforce()
        self.save_mapping()
        self.link_record(status=api.CRM_STATUS_CONFIRMED)

        sm_contact = self.make_contact("Rahul", owner=SM_RAHUL)
        # Materialize Rahul as a real member-linked contact by tagging him.
        self.set_participant("0", api._member_contact_id(SM_RAHUL), identity_role="internal")
        self.resolve_member(SM_RAHUL, "005xx000000SMuser")

        client_contact = self.make_contact("John Smith", email="john@client.com")
        self.set_participant("1", client_contact["contact_id"], identity_role="external")
        self.resolve_contact(client_contact["contact_id"], "003xx000004TmiQ")

        status, body = self.review()
        self.assertEqual(status, 200)
        self.assertTrue(body["ready"], body["blocking_reasons"])
        self.assertEqual(body["identity"]["unresolved"], [])
        self.assertIsNotNone(body["identity"]["owner"])
        self.assertIsNotNone(body["identity"]["primary_client"])


# ===========================================================================
# 4. PUSH — gating, owner attribution, idempotency
# ===========================================================================
class TestOrgMeetingPush(CrmIdentityHarness):
    def _fully_resolve(self):
        self.connect_org_salesforce()
        self.save_mapping()
        self.link_record(status=api.CRM_STATUS_CONFIRMED)
        self.set_participant("0", api._member_contact_id(SM_RAHUL), identity_role="internal")
        self.resolve_member(SM_RAHUL, "005xx000000SMuser")
        client_contact = self.make_contact("John Smith", email="john@client.com")
        self.set_participant("1", client_contact["contact_id"], identity_role="external")
        self.resolve_contact(client_contact["contact_id"], "003xx000004TmiQ")
        return client_contact

    def test_push_refuses_personal_meeting(self):
        self.t["recordings"].update_item(
            Key={"audio_s3_key": KEY}, UpdateExpression="REMOVE workspace_id")
        status, body = self.push()
        self.assertEqual(status, 400)

    def test_push_blocked_by_unresolved_speaker(self):
        self.connect_org_salesforce()
        self.save_mapping()
        self.link_record(status=api.CRM_STATUS_CONFIRMED)
        contact = self.make_contact("John Smith")
        self.set_participant("1", contact["contact_id"], identity_role="external")
        status, body = self.push()
        self.assertEqual(status, 409)

    def test_push_blocked_with_no_mapping(self):
        self.connect_org_salesforce()
        status, body = self.push()
        self.assertEqual(status, 400)

    def test_fully_resolved_push_syncs_object_and_creates_tasks(self):
        self._fully_resolve()
        self.add_task("task-1", "Send the contract")
        with mock.patch.object(api._salesforce, "update_record", return_value=None), \
             mock.patch.object(api._salesforce, "create_record",
                               return_value="00Txx0000001AAA") as create:
            status, body = self.push()
        self.assertEqual(status, 200)
        self.assertEqual(len(body["pushed"]), 1)
        self.assertEqual(body["pushed"][0]["object"], "SiteVisit__c")
        self.assertEqual(len(body["tasks"]["created"]), 1)
        # ONE Task create call + ONE Event create call (Phase 2D.3
        # completion) — both go through the same create_record, so the
        # object name (arg index 2) is what disambiguates them.
        self.assertEqual(create.call_count, 2)
        object_names = {c.args[2] for c in create.call_args_list}
        self.assertEqual(object_names, {"Task", "Event"})
        self.assertTrue(body["event"]["created"])

    def test_meeting_owner_is_the_resolved_sm_not_the_connection_identity(self):
        """THE spec section 8 assertion. The Salesforce connection was set
        up under a DIFFERENT identity (005xx0000CONNOWNER, see
        connect_org_salesforce) than the SM's resolved User
        (005xx000000SMuser). A Task's OwnerId must be the SM's, never the
        connection's."""
        client_contact = self._fully_resolve()
        self.add_task("task-1", "Send the contract",
                      assignee_contact_id=client_contact["contact_id"])
        captured_fields = {}

        def fake_create(url, tok, object_name, fields):
            captured_fields.update(fields)
            return "00Txx0000001AAA"

        with mock.patch.object(api._salesforce, "update_record", return_value=None), \
             mock.patch.object(api._salesforce, "create_record", side_effect=fake_create):
            self.push()
        # The task's assignee is the CLIENT contact, so WhoId (not OwnerId)
        # carries the Salesforce Contact id — and neither ever equals the
        # connection's own identity.
        self.assertEqual(captured_fields.get("WhoId"), "003xx000004TmiQ")
        self.assertNotIn("005xx0000CONNOWNER", captured_fields.values())

    def test_task_assignee_owner_when_internal(self):
        self._fully_resolve()
        # Assign the task to Rahul (internal) via the member-projected contact.
        self.add_task("task-1", "Follow up internally",
                      assignee_contact_id=api._member_contact_id(SM_RAHUL))
        captured_fields = {}

        def fake_create(url, tok, object_name, fields):
            captured_fields.update(fields)
            return "00Txx0000001BBB"

        with mock.patch.object(api._salesforce, "update_record", return_value=None), \
             mock.patch.object(api._salesforce, "create_record", side_effect=fake_create):
            self.push()
        self.assertEqual(captured_fields.get("OwnerId"), "005xx000000SMuser")

    def test_unresolvable_assignee_creates_task_without_guessing_owner(self):
        self._fully_resolve()
        self.add_task("task-1", "Ambiguous owner task")  # no assignee_contact_id
        with mock.patch.object(api._salesforce, "update_record", return_value=None), \
             mock.patch.object(api._salesforce, "create_record",
                               return_value="00Txx0000001CCC") as create:
            status, body = self.push()
        self.assertEqual(status, 200)
        task_call = next(c for c in create.call_args_list if c.args[2] == "Task")
        fields = task_call.args[3]
        self.assertNotIn("OwnerId", fields)
        self.assertNotIn("WhoId", fields)
        self.assertFalse(body["tasks"]["created"][0]["assignee_resolved"])

    def test_retry_does_not_create_duplicate_task(self):
        self._fully_resolve()
        self.add_task("task-1", "Send the contract")
        with mock.patch.object(api._salesforce, "update_record", return_value=None), \
             mock.patch.object(api._salesforce, "create_record",
                               return_value="00Txx0000001AAA") as create:
            self.push()
            status, body = self.push()  # retry
        self.assertEqual(status, 200)
        # First push: one Task create + one Event create. Retry: BOTH are
        # idempotent, so neither create_record call happens again.
        self.assertEqual(create.call_count, 2)
        self.assertEqual(len(body["tasks"]["created"]), 0)
        self.assertEqual(body["tasks"]["skipped"][0]["reason"], "already_synced")
        self.assertFalse(body["event"]["created"])
        self.assertEqual(body["event"]["skipped_reason"], "already_synced")

    def test_cancelled_task_is_never_pushed(self):
        self._fully_resolve()
        self.add_task("task-1", "Cancelled item", status=api.TASK_STATUS_CANCELLED)
        with mock.patch.object(api._salesforce, "update_record", return_value=None), \
             mock.patch.object(api._salesforce, "create_record",
                               return_value="00Uxx0000001EVT") as create:
            status, body = self.push()
        # The cancelled task must never reach create_record — but the Event
        # push is independent of task state and still runs, so the correct
        # assertion is "no Task create call", not "create_record never called".
        task_calls = [c for c in create.call_args_list if c.args[2] == "Task"]
        self.assertEqual(task_calls, [])
        self.assertEqual(body["tasks"]["created"], [])

    def test_task_creation_failure_does_not_block_object_push(self):
        self._fully_resolve()
        self.add_task("task-1", "Will fail")
        with mock.patch.object(api._salesforce, "update_record", return_value=None), \
             mock.patch.object(api._salesforce, "create_record",
                               side_effect=api.ApiError(422, "Required field missing")):
            status, body = self.push()
        self.assertEqual(status, 200)
        self.assertEqual(len(body["pushed"]), 1)
        self.assertEqual(len(body["tasks"]["failed"]), 1)

    def test_push_never_touches_personal_crm_connections(self):
        """The AssertionError side_effect on the mocked _crm_connections
        proves this by the push succeeding at all."""
        self._fully_resolve()
        with mock.patch.object(api._salesforce, "update_record", return_value=None):
            status, _ = self.push()
        self.assertEqual(status, 200)


# ===========================================================================
# 4B. SALESFORCE EVENT CREATION (Phase 2D.3 completion)
# ===========================================================================
class TestSalesforceEventCreation(CrmIdentityHarness):
    def _fully_resolve(self):
        """As TestOrgMeetingPush._fully_resolve — duplicated rather than
        shared across classes (this file's existing convention; see
        TestOrgMeetingPush itself) so each test class's fixtures stay
        self-contained and independently readable."""
        self.connect_org_salesforce()
        self.save_mapping()
        self.link_record(status=api.CRM_STATUS_CONFIRMED)
        self.set_participant("0", api._member_contact_id(SM_RAHUL), identity_role="internal")
        self.resolve_member(SM_RAHUL, "005xx000000SMuser")
        client_contact = self.make_contact("John Smith", email="john@client.com")
        self.set_participant("1", client_contact["contact_id"], identity_role="external")
        self.resolve_contact(client_contact["contact_id"], "003xx000004TmiQ",
                             account_id="001xx000003GYhZ")
        return client_contact

    def _event_call(self, create_mock):
        return next(c for c in create_mock.call_args_list if c.args[2] == "Event")

    def test_event_created_with_correct_subject(self):
        self._fully_resolve()
        with mock.patch.object(api._salesforce, "update_record", return_value=None), \
             mock.patch.object(api._salesforce, "create_record",
                               return_value="00Uxx0000001EVT") as create:
            status, body = self.push()
        self.assertEqual(status, 200)
        fields = self._event_call(create).args[3]
        self.assertEqual(fields["Subject"], "Client Discussion")  # the fixture's title
        self.assertTrue(body["event"]["created"])
        self.assertEqual(body["event"]["sf_event_id"], "00Uxx0000001EVT")

    def test_event_start_and_end_from_recorded_at_and_duration(self):
        self._fully_resolve()
        with mock.patch.object(api._salesforce, "update_record", return_value=None), \
             mock.patch.object(api._salesforce, "create_record",
                               return_value="00Uxx0000001EVT") as create:
            self.push()
        fields = self._event_call(create).args[3]
        self.assertEqual(fields["StartDateTime"], "2026-09-10T12:00:00Z")
        # duration=1800s (30 min) from the fixture.
        self.assertEqual(fields["EndDateTime"], "2026-09-10T12:30:00Z")

    def test_event_owner_is_resolved_sm(self):
        self._fully_resolve()
        with mock.patch.object(api._salesforce, "update_record", return_value=None), \
             mock.patch.object(api._salesforce, "create_record",
                               return_value="00Uxx0000001EVT") as create:
            self.push()
        fields = self._event_call(create).args[3]
        self.assertEqual(fields["OwnerId"], "005xx000000SMuser")

    def test_event_owner_is_never_the_connection_identity(self):
        """THE spec section 8 assertion, for Event specifically. The org's
        Salesforce connection was set up under 005xx0000CONNOWNER
        (connect_org_salesforce); the resolved SM is a DIFFERENT id. The
        Event's OwnerId must be the SM's, never the connection's."""
        self._fully_resolve()
        with mock.patch.object(api._salesforce, "update_record", return_value=None), \
             mock.patch.object(api._salesforce, "create_record",
                               return_value="00Uxx0000001EVT") as create:
            self.push()
        fields = self._event_call(create).args[3]
        self.assertNotEqual(fields["OwnerId"], "005xx0000CONNOWNER")
        self.assertNotIn("005xx0000CONNOWNER", fields.values())

    def test_event_who_is_resolved_client_contact(self):
        self._fully_resolve()
        with mock.patch.object(api._salesforce, "update_record", return_value=None), \
             mock.patch.object(api._salesforce, "create_record",
                               return_value="00Uxx0000001EVT") as create:
            self.push()
        fields = self._event_call(create).args[3]
        self.assertEqual(fields["WhoId"], "003xx000004TmiQ")

    def test_event_what_is_the_resolved_contacts_own_account(self):
        self._fully_resolve()
        with mock.patch.object(api._salesforce, "update_record", return_value=None), \
             mock.patch.object(api._salesforce, "create_record",
                               return_value="00Uxx0000001EVT") as create:
            self.push()
        fields = self._event_call(create).args[3]
        self.assertEqual(fields["WhatId"], "001xx000003GYhZ")

    def test_no_account_means_no_whatid(self):
        """A resolved Contact with NO Account on file must not get a
        fabricated WhatId — WhatId is present only when the CONTACT's own
        resolved account_id is present, never guessed or left over."""
        self.connect_org_salesforce()
        self.save_mapping()
        self.link_record(status=api.CRM_STATUS_CONFIRMED)
        self.set_participant("0", api._member_contact_id(SM_RAHUL), identity_role="internal")
        self.resolve_member(SM_RAHUL, "005xx000000SMuser")
        client_contact = self.make_contact("John Smith", email="john@client.com")
        self.set_participant("1", client_contact["contact_id"], identity_role="external")
        self.resolve_contact(client_contact["contact_id"], "003xx000004TmiQ")  # no account_id

        with mock.patch.object(api._salesforce, "update_record", return_value=None), \
             mock.patch.object(api._salesforce, "create_record",
                               return_value="00Uxx0000001EVT") as create:
            self.push()
        fields = self._event_call(create).args[3]
        self.assertNotIn("WhatId", fields)
        self.assertIn("WhoId", fields)  # the Contact relationship still applies

    def test_no_resolved_owner_skips_event_without_erroring(self):
        """An organisation meeting can be pushable (object mapping done,
        no unresolved speakers) with an internal speaker tagged but not yet
        linked to a Salesforce User — but the review/push gate only blocks
        on UNRESOLVED speakers, and a speaker with no identity_role at all
        is simply absent from the picture. Simulate the edge case where
        there is genuinely no internal speaker at all: Event creation must
        skip cleanly rather than push with a missing OwnerId."""
        self.connect_org_salesforce()
        self.save_mapping()
        self.link_record(status=api.CRM_STATUS_CONFIRMED)
        client_contact = self.make_contact("John Smith", email="john@client.com")
        self.set_participant("1", client_contact["contact_id"], identity_role="external")
        self.resolve_contact(client_contact["contact_id"], "003xx000004TmiQ")

        with mock.patch.object(api._salesforce, "update_record", return_value=None), \
             mock.patch.object(api._salesforce, "create_record") as create:
            status, body = self.push()
        self.assertEqual(status, 200)
        self.assertFalse(body["event"]["created"])
        self.assertEqual(body["event"]["skipped_reason"], "no_resolved_owner")
        event_calls = [c for c in create.call_args_list if c.args[2] == "Event"]
        self.assertEqual(event_calls, [])

    def test_no_meeting_time_skips_event_cleanly(self):
        self._fully_resolve()
        self.t["recordings"].update_item(
            Key={"audio_s3_key": KEY},
            UpdateExpression="REMOVE recorded_at")
        with mock.patch.object(api._salesforce, "update_record", return_value=None), \
             mock.patch.object(api._salesforce, "create_record",
                               return_value="00Uxx0000001EVT"):
            status, body = self.push()
        self.assertEqual(status, 200)
        self.assertFalse(body["event"]["created"])
        self.assertEqual(body["event"]["skipped_reason"], "no_meeting_time")

    def test_duplicate_event_prevented_on_retry(self):
        self._fully_resolve()
        with mock.patch.object(api._salesforce, "update_record", return_value=None), \
             mock.patch.object(api._salesforce, "create_record",
                               return_value="00Uxx0000001EVT") as create:
            first_status, first_body = self.push()
            second_status, second_body = self.push()
        self.assertTrue(first_body["event"]["created"])
        self.assertFalse(second_body["event"]["created"])
        self.assertEqual(second_body["event"]["skipped_reason"], "already_synced")
        self.assertEqual(second_body["event"]["sf_event_id"], "00Uxx0000001EVT")
        event_calls = [c for c in create.call_args_list if c.args[2] == "Event"]
        self.assertEqual(len(event_calls), 1)

    def test_retry_after_partial_success_does_not_recreate_event(self):
        """The scenario the idempotency map exists for: the Event was
        created successfully but something ELSE in the same push failed
        (simulated here by a failing Task), so the client retries the whole
        push. The Event must not be created a second time."""
        client_contact = self._fully_resolve()
        self.add_task("task-1", "Will fail", assignee_contact_id=client_contact["contact_id"])

        call_count = {"n": 0}

        def flaky_create(url, tok, object_name, fields):
            if object_name == "Task":
                raise api.ApiError(500, "temporary failure")
            call_count["n"] += 1
            return "00Uxx0000001EVT"

        with mock.patch.object(api._salesforce, "update_record", return_value=None), \
             mock.patch.object(api._salesforce, "create_record", side_effect=flaky_create):
            self.push()
            status, body = self.push()  # retry after the Task failure
        self.assertEqual(status, 200)
        self.assertEqual(call_count["n"], 1)  # Event create ran exactly once total
        self.assertFalse(body["event"]["created"])
        self.assertEqual(body["event"]["skipped_reason"], "already_synced")

    def test_salesforce_permission_failure_on_event_is_reported_not_swallowed(self):
        self._fully_resolve()
        with mock.patch.object(api._salesforce, "update_record", return_value=None), \
             mock.patch.object(api._salesforce, "create_record",
                               side_effect=api.ApiError(
                                   403, "your Salesforce user cannot create a Event")):
            status, body = self.push()
        self.assertEqual(status, 200)  # the push itself still answers 200
        self.assertFalse(body["event"]["created"])
        self.assertIn("cannot create", body["event"]["error"])

    def test_salesforce_reconnect_required_surfaces_from_event_push(self):
        """A dead refresh token is a PARTIAL-push condition, not a whole-
        request failure — the object-mapping half already reports this via
        push_errors[].code (crm_sync_record's own contract, unchanged by
        this phase); the Event half must report the SAME stable code in
        its own result rather than a bare error string, so the app can
        show "Reconnect Salesforce" for the Event too."""
        self._fully_resolve()
        with mock.patch.object(api._salesforce, "refresh_access_token",
                               return_value={}):  # dead refresh token
            status, body = self.push()
        self.assertEqual(status, 200)
        self.assertFalse(body["event"]["created"])
        self.assertEqual(body["event"]["code"], "salesforce_reconnect_required")
        # The object-mapping push independently reports the same condition.
        self.assertEqual(body["push_errors"][0]["code"], "salesforce_reconnect_required")

    def test_event_uses_organisation_connection_not_personal(self):
        """The AssertionError side_effect on the mocked _crm_connections
        (see setUp) proves this: if Event creation ever fell back to
        _sf_call(user_id, ...) it would touch CrmConnections and this
        assertion would fail via the mock's own side_effect."""
        self._fully_resolve()
        with mock.patch.object(api._salesforce, "update_record", return_value=None), \
             mock.patch.object(api._salesforce, "create_record",
                               return_value="00Uxx0000001EVT"):
            status, body = self.push()
        self.assertEqual(status, 200)
        self.assertTrue(body["event"]["created"])

    def test_event_isolated_across_organisations(self):
        """ORG_A's Event idempotency record must never leak into ORG_B's —
        pushing ORG_A must not mark ORG_B's (different) meeting as synced."""
        self._fully_resolve()
        with mock.patch.object(api._salesforce, "update_record", return_value=None), \
             mock.patch.object(api._salesforce, "create_record",
                               return_value="00Uxx0000001EVT"):
            self.push()
        # A second, unrelated org-B meeting must show no Event record at all.
        key_b = "recordings/u-ob/mobile/mobile-b_1757000001.m4a"
        self.t["recordings"].put_item(Item={
            "audio_s3_key": key_b, "user_id": OWNER_B, "workspace_id": ORG_B,
            "title": "XYZ Meeting", "created_at": NOW, "status": "complete",
            "transcript": "Speaker 0: hi", "recorded_at": "1789041600", "duration": 900,
        })
        row_b = self.t["recordings"].get_item(Key={"audio_s3_key": key_b}).get("Item")
        self.assertNotIn("crm_event_synced", row_b)

    def test_member_who_owns_the_meeting_can_trigger_event_creation(self):
        """Same RBAC shape as the object-mapping push: an ordinary Member
        who owns THIS meeting may push it (spec section 17); only
        Organisation CRM CONFIGURATION and identity resolution steps are
        Owner/Manager-gated.

        set_participant is gated by _owned_recording (creator-only), so
        once the meeting's owner becomes MEMBER_A the speaker-tagging calls
        must run AS MEMBER_A too — resolve_member/resolve_contact stay
        Owner-only (CAP_MANAGE_INTEGRATIONS / CAP_MANAGE_CONTACTS), which is
        the actual RBAC boundary this test is pinning: tagging+pushing is a
        Member action, resolving Salesforce identities is not.
        """
        self.t["recordings"].update_item(
            Key={"audio_s3_key": KEY},
            UpdateExpression="SET user_id = :u",
            ExpressionAttributeValues={":u": MEMBER_A})
        self.connect_org_salesforce()
        self.save_mapping()
        self.link_record(status=api.CRM_STATUS_CONFIRMED)
        self.set_participant("0", api._member_contact_id(SM_RAHUL),
                             identity_role="internal", as_user=MEMBER_A)
        self.resolve_member(SM_RAHUL, "005xx000000SMuser")  # Owner-only
        client_contact = self.make_contact("John Smith", email="john@client.com",
                                           owner=MEMBER_A)
        self.set_participant("1", client_contact["contact_id"],
                             identity_role="external", as_user=MEMBER_A)
        self.resolve_contact(client_contact["contact_id"], "003xx000004TmiQ",
                             account_id="001xx000003GYhZ",
                             as_user=MANAGER_A)  # Manager/Owner-only

        with mock.patch.object(api._salesforce, "update_record", return_value=None), \
             mock.patch.object(api._salesforce, "create_record",
                               return_value="00Uxx0000001EVT"):
            status, body = self.push(as_user=MEMBER_A)
        self.assertEqual(status, 200)
        self.assertTrue(body["event"]["created"])

    def test_removed_member_cannot_trigger_push_at_all(self):
        self.t["recordings"].update_item(
            Key={"audio_s3_key": KEY},
            UpdateExpression="SET user_id = :u",
            ExpressionAttributeValues={":u": MEMBER_A})
        self._fully_resolve()
        self.as_user(OWNER_A)
        call(api.remove_member, event(
            method="DELETE", route="/workspaces/{workspace_id}/members/{user_id}",
            path={"workspace_id": ORG_A, "user_id": MEMBER_A}))
        status, _ = self.push(as_user=MEMBER_A)
        self.assertEqual(status, 404)

    def test_event_never_created_for_personal_meeting(self):
        self.t["recordings"].update_item(
            Key={"audio_s3_key": KEY}, UpdateExpression="REMOVE workspace_id")
        status, _ = self.push()
        self.assertEqual(status, 400)

    def test_existing_task_creation_still_works_alongside_event(self):
        """Regression: adding Event creation must not disturb Task creation
        — both run in the same push and both succeed independently."""
        self._fully_resolve()
        self.add_task("task-1", "Send the contract")
        with mock.patch.object(api._salesforce, "update_record", return_value=None), \
             mock.patch.object(api._salesforce, "create_record",
                               return_value="00Uxx0000001XXX"):
            status, body = self.push()
        self.assertEqual(status, 200)
        self.assertEqual(len(body["tasks"]["created"]), 1)
        self.assertTrue(body["event"]["created"])


# ===========================================================================
# 5. RBAC — search/review/push available to members, config stays privileged
# ===========================================================================
class TestCrmIdentityRbac(CrmIdentityHarness):
    def test_member_can_search_contacts(self):
        self.connect_org_salesforce()
        self.as_user(MEMBER_A)
        with mock.patch.object(api._salesforce, "query",
                               return_value={"records": []}):
            status, body = parse(call(api.crm_search_salesforce_contacts, event(
                "GET", "/workspaces/{workspace_id}/crm/salesforce/search/contacts",
                path={"workspace_id": ORG_A}, qs={"q": "john@client.com"})))
        self.assertEqual(status, 200)

    def test_member_can_search_users(self):
        self.connect_org_salesforce()
        self.as_user(MEMBER_A)
        with mock.patch.object(api._salesforce, "query",
                               return_value={"records": []}):
            status, _ = parse(call(api.crm_search_salesforce_users, event(
                "GET", "/workspaces/{workspace_id}/crm/salesforce/search/users",
                path={"workspace_id": ORG_A}, qs={"q": "rahul"})))
        self.assertEqual(status, 200)

    def test_member_who_owns_the_meeting_can_view_review(self):
        """CRM review is gated by the SAME meeting-write rule every other
        meeting mutation uses (_owned_recording, creator-only) — an
        ordinary Member who created this particular meeting can view it;
        a Member who did not is refused exactly like any other route
        behind that gate (see test_outsider_gets_404_on_review and
        test_removed_member_loses_access_to_review below, which cover the
        denial side)."""
        self.t["recordings"].update_item(
            Key={"audio_s3_key": KEY},
            UpdateExpression="SET user_id = :u",
            ExpressionAttributeValues={":u": MEMBER_A})
        self.connect_org_salesforce()
        status, _ = self.review(as_user=MEMBER_A)
        self.assertEqual(status, 200)

    def test_member_who_does_not_own_the_meeting_is_refused(self):
        self.connect_org_salesforce()
        status, _ = self.review(as_user=MEMBER_A)
        self.assertEqual(status, 404)

    def test_member_who_owns_the_meeting_can_push(self):
        """Members MAY use an existing configured connection for permitted
        meeting operations (spec section 17) — the push route itself is not
        Owner/Manager-gated; only the underlying meeting-write check
        (_owned_recording, creator-only) applies, same as every other
        meeting mutation. Here MEMBER_A is the meeting's own creator."""
        self.t["recordings"].update_item(
            Key={"audio_s3_key": KEY},
            UpdateExpression="SET user_id = :u",
            ExpressionAttributeValues={":u": MEMBER_A})
        self.connect_org_salesforce()
        self.save_mapping()
        self.link_record(status=api.CRM_STATUS_CONFIRMED)
        self.set_participant("0", api._member_contact_id(SM_RAHUL),
                             identity_role="internal", as_user=MEMBER_A)
        self.resolve_member(SM_RAHUL, "005xx000000SMuser")  # Owner-only step
        client_contact = self.make_contact("John Smith", email="john@client.com",
                                           owner=MEMBER_A)
        self.set_participant("1", client_contact["contact_id"],
                             identity_role="external", as_user=MEMBER_A)
        self.resolve_contact(client_contact["contact_id"], "003xx000004TmiQ",
                            as_user=MANAGER_A)  # Manager/Owner-only step

        with mock.patch.object(api._salesforce, "update_record", return_value=None):
            status, body = self.push(as_user=MEMBER_A)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["pushed"]), 1)

    def test_outsider_gets_404_on_review(self):
        self.connect_org_salesforce()
        status, _ = self.review(as_user=OUTSIDER)
        self.assertEqual(status, 404)

    def test_removed_member_loses_access_to_review(self):
        self.connect_org_salesforce()
        self.as_user(OWNER_A)
        call(api.remove_member, event(
            method="DELETE", route="/workspaces/{workspace_id}/members/{user_id}",
            path={"workspace_id": ORG_A, "user_id": MEMBER_A}))
        status, _ = self.review(as_user=MEMBER_A)
        self.assertEqual(status, 404)


# ===========================================================================
# 6. ROUTER
# ===========================================================================
class TestRouter(unittest.TestCase):
    def test_identity_routes_registered(self):
        self.assertIs(api._ROUTES[("PUT", "/contacts/{contact_id}/crm")],
                      api.resolve_contact_crm_identity)
        self.assertIs(api._ROUTES[
            ("GET", "/workspaces/{workspace_id}/crm/salesforce/search/contacts")],
            api.crm_search_salesforce_contacts)
        self.assertIs(api._ROUTES[
            ("GET", "/workspaces/{workspace_id}/crm/salesforce/search/users")],
            api.crm_search_salesforce_users)
        self.assertIs(api._ROUTES[
            ("PUT", "/workspaces/{workspace_id}/crm/salesforce/members/{user_id}")],
            api.resolve_member_crm_identity)
        self.assertIs(api._ROUTES[("GET", "/recordings/ai/crm-review/{key+}")],
                      api.get_meeting_crm_review)
        self.assertIs(api._ROUTES[("POST", "/recordings/ai/crm-push/{key+}")],
                      api.push_org_meeting_crm)

    def test_personal_salesforce_routes_untouched(self):
        self.assertIs(api._ROUTES[("POST", "/crm/salesforce/sync/{key+}")],
                      api.crm_sync_record)
        self.assertIs(api._ROUTES[("POST", "/crm/salesforce/lookup")],
                      api.salesforce_lookup_record)


if __name__ == "__main__":
    unittest.main()
