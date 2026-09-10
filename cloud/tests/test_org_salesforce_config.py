#!/usr/bin/env python3
"""test_org_salesforce_config.py — Phase 2D.2: Organisation Salesforce CRM
configuration (object/field discovery + mapping) and config safety across a
reconnect.

WHAT THIS FILE PINS.

  1. RBAC on configuration. Owner/Manager may read AND write the mapping;
     Member may read but not write. Same CAP_MANAGE_INTEGRATIONS gate as
     connect/disconnect (Phase 2D.1), now applied to PUT config.

  2. SCOPE ISOLATION. A mapping saved for one workspace is stored under
     THAT workspace's OrgCrmConnections row and is invisible to every other
     workspace, including a second organisation. Never read from or written
     to Personal's CrmConnections.

  3. RECONNECT SAFETY (the risk 2D.1 flagged and 2D.2 was asked to close).
     Reconnecting to the SAME Salesforce org (same org_id) preserves the
     existing mapping. Reconnecting to a DIFFERENT org clears it and leaves
     an explicit, one-time "config_cleared_reason" — never a silently stale
     mapping pointed at the wrong org's schema.

  4. GENERIC RULES REUSE. Every validation rule (object/field existence,
     filterable lookup field, writable long-text content target, one
     mapping per object, CRM_MAX_MAPPINGS) is IDENTICAL to Personal's — this
     file proves that by exercising the same failure modes against the
     workspace-scoped routes, not by re-deriving the rules.

  5. REGRESSION. Personal Salesforce configuration (test_ai_workspace.py's
     CRM test classes) is untouched — this file never imports or patches
     anything Personal-specific beyond the safety-net MagicMock the sibling
     OAuth suite already uses.

SECURITY POSTURE, as in every suite here: tests call the ROUTE FUNCTION
DIRECTLY with a forged identity/workspace.

OFFLINE: fake_dynamodb, no AWS, no network.

Run:  python -m pytest tests/test_org_salesforce_config.py
"""
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

# ORDER MATTERS — see test_task_permissions.py.
from test_ai_workspace import api  # noqa: E402
import fake_dynamodb as fdb  # noqa: E402
import workspace_schema as ws  # noqa: E402

OWNER_A, MANAGER_A, MEMBER_A = "u-oa", "u-ma", "u-mea"
OWNER_B = "u-ob"
OUTSIDER = "u-out"

ORG_A = "wso_c2aaaa11"
ORG_B = "wso_c2bbbb22"
NOW = "2026-09-08T10:00:00Z"

ORG_ID_1 = "00Dxx0000000001"
ORG_ID_2 = "00Dyy0000000002"

FAKE_TOKENS_ORG1 = {
    "access_token": "AT-1", "refresh_token": "RT-1",
    "instance_url": "https://abcrealty.my.salesforce.com",
    "id": f"https://login.salesforce.com/id/{ORG_ID_1}/005xx000000001",
}
FAKE_TOKENS_ORG2 = {
    "access_token": "AT-2", "refresh_token": "RT-2",
    "instance_url": "https://different-org.my.salesforce.com",
    "id": f"https://login.salesforce.com/id/{ORG_ID_2}/005yy000000002",
}

# A minimal but realistic Describe response for one custom object, reused by
# every mapping test — SiteVisit__c with a filterable identifier and two
# long-text fields, exactly the shape _validate_mapping expects.
SITE_VISIT_DESCRIBE = {
    "label": "Site Visit",
    "fields": [
        {"name": "Id", "label": "Record ID", "type": "id",
         "filterable": True, "updateable": False, "length": 18},
        {"name": "Site_Visit_Number__c", "label": "Site Visit Number",
         "type": "string", "filterable": True, "updateable": True,
         "length": 40, "unique": True},
        {"name": "Transcript__c", "label": "Transcript", "type": "textarea",
         "filterable": False, "updateable": True, "length": 131072,
         "calculated": False},
        {"name": "Summary__c", "label": "Summary", "type": "textarea",
         "filterable": False, "updateable": True, "length": 32768,
         "calculated": False},
        {"name": "ReadOnlyFormula__c", "label": "Computed", "type": "textarea",
         "filterable": False, "updateable": False, "length": 32768,
         "calculated": True},
        {"name": "ShortText__c", "label": "Short Text", "type": "string",
         "filterable": True, "updateable": True, "length": 40},
    ],
}

LEAD_DESCRIBE = {
    "label": "Lead",
    "fields": [
        {"name": "Id", "label": "Record ID", "type": "id",
         "filterable": True, "updateable": False, "length": 18},
        {"name": "Email", "label": "Email", "type": "email",
         "filterable": True, "updateable": True, "length": 80},
        {"name": "Description", "label": "Description", "type": "textarea",
         "filterable": False, "updateable": True, "length": 32768,
         "calculated": False},
    ],
}


def parse(resp):
    return resp["statusCode"], json.loads(resp["body"])


def event(method="GET", route="/workspaces/{workspace_id}/crm/salesforce/config",
          path=None, body=None, qs=None):
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


class OrgCrmConfigHarness(unittest.TestCase):
    def setUp(self):
        self.t = fdb.build_tables()
        self.current_user = OWNER_A
        self.patches = [
            mock.patch.object(api, "_workspaces", self.t["workspaces"]),
            mock.patch.object(api, "_memberships", self.t["memberships"]),
            mock.patch.object(api, "_org_crm_connections",
                              self.t["org_crm_connections"]),
            mock.patch.object(api, "_crm_connections", mock.MagicMock(
                side_effect=AssertionError(
                    "Organisation CRM config code path touched "
                    "CrmConnections (Personal) — scope leak"))),
            mock.patch.object(api, "_require_auth",
                              side_effect=lambda ev: self.current_user),
            mock.patch.object(api, "_jwt_secret", return_value="test-secret"),
            mock.patch.object(api, "SALESFORCE_CLIENT_ID", "cid-abc"),
            mock.patch.object(api, "ORG_SALESFORCE_REDIRECT_URI",
                              "https://api.example.com/crm/salesforce/org-callback"),
            mock.patch.object(api, "SALESFORCE_RETURN_URL",
                              "recorderapp://crm-connected"),
            mock.patch.object(api, "_kms_encrypt", side_effect=lambda pt: f"ENC({pt})"),
            mock.patch.object(api, "_kms_decrypt",
                              side_effect=lambda ct: ct[4:-1] if ct.startswith("ENC(") else ct),
            # Every discovery/config call goes through _sf_call_org, which
            # mints a fresh access token via refresh_access_token before
            # running the caller's fn(). Mocked here (not per-test) because
            # nearly every test in this file makes at least one such call,
            # and the real implementation would otherwise reach for
            # Secrets Manager. instance_url intentionally omitted from the
            # return value so _sf_call_org falls back to the connection's
            # STORED instance_url — proving that path too.
            mock.patch.object(api._salesforce, "refresh_access_token",
                              return_value={"access_token": "test-access-token"}),
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
            (ORG_B, OWNER_B, ws.ROLE_OWNER),
        ):
            self.t["memberships"].put_item(
                Item=ws.new_membership(wid, uid, role, NOW))

    def as_user(self, uid):
        self.current_user = uid

    # -- connect helpers ----------------------------------------------------
    @staticmethod
    def _qs(url):
        return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)

    def connect_and_callback(self, workspace_id=ORG_A, as_user=OWNER_A,
                             tokens=None, whoami=None):
        self.as_user(as_user)
        status, body = parse(call(api.org_salesforce_connect, event(
            "GET", "/workspaces/{workspace_id}/crm/salesforce/connect",
            path={"workspace_id": workspace_id})))
        assert status == 200, body
        state = self._qs(body["authorize_url"])["state"][0]
        tokens = tokens or dict(FAKE_TOKENS_ORG1)
        whoami = whoami if whoami is not None else {
            "organization_id": ORG_ID_1, "user_id": "005xx000000001",
            "preferred_username": "integration@abcrealty.com"}
        with mock.patch.object(api._salesforce, "exchange_code",
                               return_value=dict(tokens)), \
             mock.patch.object(api._salesforce, "whoami", return_value=whoami):
            return api.org_salesforce_callback(
                {"queryStringParameters": {"code": "auth-code", "state": state}})

    # -- config helpers -------------------------------------------------
    def get_objects(self, workspace_id=ORG_A):
        return parse(call(api.org_salesforce_list_objects, event(
            "GET", "/workspaces/{workspace_id}/crm/salesforce/objects",
            path={"workspace_id": workspace_id})))

    def get_fields(self, object_name, workspace_id=ORG_A):
        return parse(call(api.org_salesforce_list_fields, event(
            "GET", "/workspaces/{workspace_id}/crm/salesforce/fields/{object_name}",
            path={"workspace_id": workspace_id, "object_name": object_name})))

    def get_config(self, workspace_id=ORG_A):
        return parse(call(api.org_salesforce_get_config, event(
            "GET", "/workspaces/{workspace_id}/crm/salesforce/config",
            path={"workspace_id": workspace_id})))

    def put_config(self, mappings, workspace_id=ORG_A):
        return parse(call(api.org_salesforce_put_config, event(
            "PUT", "/workspaces/{workspace_id}/crm/salesforce/config",
            path={"workspace_id": workspace_id}, body={"mappings": mappings})))

    def save_site_visit_mapping(self, workspace_id=ORG_A, as_user=OWNER_A):
        self.as_user(as_user)
        with mock.patch.object(api._salesforce, "describe_object",
                               return_value=SITE_VISIT_DESCRIBE):
            return self.put_config(
                [{"object": "SiteVisit__c", "lookup_field": "Site_Visit_Number__c",
                  "transcript_field": "Transcript__c",
                  "summary_field": "Summary__c"}],
                workspace_id=workspace_id)


# ===========================================================================
# 1. OBJECT / FIELD DISCOVERY
# ===========================================================================
class TestDiscovery(OrgCrmConfigHarness):
    def setUp(self):
        super().setUp()
        self.connect_and_callback()

    def test_member_can_list_objects(self):
        self.as_user(MEMBER_A)
        raw_objects = [
            {"name": "SiteVisit__c", "label": "Site Visit", "custom": True,
             "updateable": True, "queryable": True},
            {"name": "SiteVisitShare", "label": "Site Visit Share",
             "custom": True, "updateable": True, "queryable": True},
        ]
        with mock.patch.object(api._salesforce, "list_objects",
                               return_value=raw_objects):
            status, body = self.get_objects()
        self.assertEqual(status, 200)
        names = {o["name"] for o in body["objects"]}
        self.assertIn("SiteVisit__c", names)
        self.assertNotIn("SiteVisitShare", names)  # filtered like Personal
        self.assertEqual(body["suggested"], "SiteVisit__c")

    def test_member_can_list_fields(self):
        self.as_user(MEMBER_A)
        with mock.patch.object(api._salesforce, "describe_object",
                               return_value=SITE_VISIT_DESCRIBE):
            status, body = self.get_fields("SiteVisit__c")
        self.assertEqual(status, 200)
        number_names = {f["name"] for f in body["number_fields"]}
        self.assertIn("Site_Visit_Number__c", number_names)
        long_text_names = {f["name"] for f in body["long_text_fields"]}
        self.assertIn("Transcript__c", long_text_names)
        # Read-only calculated field must never be offered as a target.
        self.assertNotIn("ReadOnlyFormula__c", long_text_names)

    def test_invalid_object_name_rejected(self):
        self.as_user(OWNER_A)
        status, body = self.get_fields("bad name!")
        self.assertEqual(status, 400)

    def test_discovery_requires_connection(self):
        """A workspace with no Salesforce connection at all — discovery must
        surface the same not-connected error the sync path uses."""
        self.as_user(OWNER_B)
        status, body = self.get_objects(workspace_id=ORG_B)
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "organisation_salesforce_not_connected")


# ===========================================================================
# 2. RBAC — read vs write on configuration
# ===========================================================================
class TestConfigRbac(OrgCrmConfigHarness):
    def setUp(self):
        super().setUp()
        self.connect_and_callback()

    def test_owner_can_save_config(self):
        status, body = self.save_site_visit_mapping(as_user=OWNER_A)
        self.assertEqual(status, 200)
        self.assertEqual(len(body["config"]["mappings"]), 1)
        self.assertEqual(body["config"]["mappings"][0]["object"], "SiteVisit__c")

    def test_manager_can_save_config(self):
        status, body = self.save_site_visit_mapping(as_user=MANAGER_A)
        self.assertEqual(status, 200)

    def test_member_cannot_save_config(self):
        self.as_user(MEMBER_A)
        with mock.patch.object(api._salesforce, "describe_object",
                               return_value=SITE_VISIT_DESCRIBE):
            status, body = self.put_config(
                [{"object": "SiteVisit__c",
                  "lookup_field": "Site_Visit_Number__c"}])
        self.assertEqual(status, 403)
        # Nothing was written.
        status, body = self.get_config()
        self.assertEqual(body["config"]["mappings"], [])

    def test_member_can_read_config(self):
        self.save_site_visit_mapping(as_user=OWNER_A)
        self.as_user(MEMBER_A)
        status, body = self.get_config()
        self.assertEqual(status, 200)
        self.assertEqual(len(body["config"]["mappings"]), 1)

    def test_outsider_gets_404(self):
        self.as_user(OUTSIDER)
        status, _ = self.get_config()
        self.assertEqual(status, 404)

    def test_config_requires_connection(self):
        self.as_user(OWNER_B)
        status, body = self.get_config(workspace_id=ORG_B)
        self.assertEqual(status, 400)
        self.assertEqual(body["code"], "organisation_salesforce_not_connected")


# ===========================================================================
# 3. VALIDATION RULES — reused verbatim from Personal via _validate_mapping
# ===========================================================================
class TestMappingValidation(OrgCrmConfigHarness):
    def setUp(self):
        super().setUp()
        self.connect_and_callback()
        self.as_user(OWNER_A)

    def test_unknown_field_rejected(self):
        with mock.patch.object(api._salesforce, "describe_object",
                               return_value=SITE_VISIT_DESCRIBE):
            status, body = self.put_config(
                [{"object": "SiteVisit__c", "lookup_field": "Nonexistent__c"}])
        self.assertEqual(status, 400)

    def test_non_filterable_lookup_field_rejected(self):
        bad_describe = dict(SITE_VISIT_DESCRIBE, fields=[
            {"name": "Notes__c", "label": "Notes", "type": "textarea",
             "filterable": False, "updateable": True, "length": 5000},
        ])
        with mock.patch.object(api._salesforce, "describe_object",
                               return_value=bad_describe):
            status, body = self.put_config(
                [{"object": "SiteVisit__c", "lookup_field": "Notes__c"}])
        self.assertEqual(status, 400)

    def test_readonly_content_target_rejected(self):
        with mock.patch.object(api._salesforce, "describe_object",
                               return_value=SITE_VISIT_DESCRIBE):
            status, body = self.put_config(
                [{"object": "SiteVisit__c", "lookup_field": "Site_Visit_Number__c",
                  "transcript_field": "ReadOnlyFormula__c"}])
        self.assertEqual(status, 400)

    def test_duplicate_object_rejected(self):
        with mock.patch.object(api._salesforce, "describe_object",
                               return_value=SITE_VISIT_DESCRIBE):
            status, body = self.put_config(
                [{"object": "SiteVisit__c", "lookup_field": "Site_Visit_Number__c"},
                 {"object": "SiteVisit__c", "lookup_field": "ShortText__c"}])
        self.assertEqual(status, 400)

    def test_field_reused_across_targets_rejected(self):
        with mock.patch.object(api._salesforce, "describe_object",
                               return_value=SITE_VISIT_DESCRIBE):
            status, body = self.put_config(
                [{"object": "SiteVisit__c", "lookup_field": "Site_Visit_Number__c",
                  "transcript_field": "Transcript__c",
                  "summary_field": "Transcript__c"}])
        self.assertEqual(status, 400)

    def test_too_many_mappings_rejected(self):
        mappings = [{"object": f"Obj{i}__c", "lookup_field": "X"}
                   for i in range(api.CRM_MAX_MAPPINGS + 1)]
        status, body = self.put_config(mappings)
        self.assertEqual(status, 400)

    def test_empty_mappings_list_is_valid(self):
        """Saving [] turns record linking off without disconnecting —
        identical semantics to Personal."""
        status, body = self.put_config([])
        self.assertEqual(status, 200)
        self.assertEqual(body["config"]["mappings"], [])
        self.assertFalse(body["config"]["configured"])

    def test_multiple_objects_can_be_mapped(self):
        def describe(url, tok, object_name):
            return SITE_VISIT_DESCRIBE if object_name == "SiteVisit__c" else LEAD_DESCRIBE

        with mock.patch.object(api._salesforce, "describe_object",
                               side_effect=describe):
            status, body = self.put_config([
                {"object": "SiteVisit__c", "lookup_field": "Site_Visit_Number__c"},
                {"object": "Lead", "lookup_field": "Email"},
            ])
        self.assertEqual(status, 200)
        self.assertEqual(len(body["config"]["mappings"]), 2)


# ===========================================================================
# 4. SCOPE ISOLATION — mappings never leak across workspaces or to Personal
# ===========================================================================
class TestConfigScopeIsolation(OrgCrmConfigHarness):
    def test_mapping_stored_under_correct_workspace(self):
        self.connect_and_callback(workspace_id=ORG_A, as_user=OWNER_A)
        self.save_site_visit_mapping(workspace_id=ORG_A, as_user=OWNER_A)
        row = self.t["org_crm_connections"].get_item(
            Key={"workspace_id": ORG_A, "provider": "salesforce"}).get("Item")
        self.assertEqual(row["config"]["mappings"][0]["object"], "SiteVisit__c")

    def test_org_b_config_unaffected_by_org_a_save(self):
        self.connect_and_callback(workspace_id=ORG_A, as_user=OWNER_A)
        self.connect_and_callback(workspace_id=ORG_B, as_user=OWNER_B,
                                  tokens=dict(FAKE_TOKENS_ORG2),
                                  whoami={"organization_id": ORG_ID_2,
                                         "user_id": "005yy0",
                                         "preferred_username": "b@xyzcorp.com"})
        self.save_site_visit_mapping(workspace_id=ORG_A, as_user=OWNER_A)

        self.as_user(OWNER_B)
        status, body = self.get_config(workspace_id=ORG_B)
        self.assertEqual(status, 200)
        self.assertEqual(body["config"]["mappings"], [])

    def test_config_never_touches_personal_crm_connections(self):
        """The _crm_connections MagicMock in setUp raises AssertionError if
        CALLED — proving no config code path here reaches Personal's table.
        A successful full round-trip (connect, discover, save, read) with
        that mock installed IS the proof."""
        self.connect_and_callback()
        self.save_site_visit_mapping()
        status, body = self.get_config()
        self.assertEqual(status, 200)
        self.assertEqual(len(body["config"]["mappings"]), 1)


# ===========================================================================
# 5. RECONNECT SAFETY — the risk 2D.1 flagged
# ===========================================================================
class TestReconnectConfigSafety(OrgCrmConfigHarness):
    def test_reconnect_to_same_org_preserves_config(self):
        self.connect_and_callback(tokens=dict(FAKE_TOKENS_ORG1),
                                  whoami={"organization_id": ORG_ID_1,
                                         "user_id": "u1",
                                         "preferred_username": "a@abcrealty.com"})
        self.save_site_visit_mapping(as_user=OWNER_A)

        # Reconnect — SAME org_id, different token (e.g. re-authorizing).
        resp = self.connect_and_callback(
            tokens={**FAKE_TOKENS_ORG1, "refresh_token": "RT-1-rotated"},
            whoami={"organization_id": ORG_ID_1, "user_id": "u1",
                   "preferred_username": "a@abcrealty.com"})
        self.assertIn("connected=1", resp["headers"]["Location"])

        status, body = self.get_config()
        self.assertEqual(status, 200)
        self.assertEqual(len(body["config"]["mappings"]), 1)
        self.assertEqual(body["config"]["mappings"][0]["object"], "SiteVisit__c")

    def test_reconnect_to_different_org_clears_config(self):
        self.connect_and_callback(tokens=dict(FAKE_TOKENS_ORG1),
                                  whoami={"organization_id": ORG_ID_1,
                                         "user_id": "u1",
                                         "preferred_username": "a@abcrealty.com"})
        self.save_site_visit_mapping(as_user=OWNER_A)

        # Reconnect to a DIFFERENT Salesforce org.
        resp = self.connect_and_callback(
            tokens=dict(FAKE_TOKENS_ORG2),
            whoami={"organization_id": ORG_ID_2, "user_id": "u2",
                   "preferred_username": "b@different.com"})
        self.assertIn("connected=1", resp["headers"]["Location"])

        status, body = self.get_config()
        self.assertEqual(status, 200)
        self.assertEqual(body["config"]["mappings"], [],
                         "a mapping from the OLD org must never silently "
                         "apply to the NEW org's schema")

    def test_org_change_leaves_an_explicit_status_message(self):
        self.connect_and_callback(tokens=dict(FAKE_TOKENS_ORG1),
                                  whoami={"organization_id": ORG_ID_1,
                                         "user_id": "u1", "preferred_username": "a@x.com"})
        self.save_site_visit_mapping(as_user=OWNER_A)
        self.connect_and_callback(tokens=dict(FAKE_TOKENS_ORG2),
                                  whoami={"organization_id": ORG_ID_2,
                                         "user_id": "u2", "preferred_username": "b@y.com"})

        self.as_user(OWNER_A)
        status, body = parse(call(api.org_salesforce_status, event(
            "GET", "/workspaces/{workspace_id}/crm/salesforce/status",
            path={"workspace_id": ORG_A})))
        self.assertEqual(status, 200)
        self.assertIn("config_cleared_reason", body)
        self.assertIn(ORG_ID_1, body["config_cleared_reason"])
        self.assertIn(ORG_ID_2, body["config_cleared_reason"])
        self.assertFalse(body["configured"])

    def test_first_ever_connect_has_no_cleared_reason(self):
        """No PRIOR connection existed — nothing was cleared, so there is
        nothing to explain. Distinguishes 'first connect' from 'org change'."""
        self.connect_and_callback()
        self.as_user(OWNER_A)
        status, body = parse(call(api.org_salesforce_status, event(
            "GET", "/workspaces/{workspace_id}/crm/salesforce/status",
            path={"workspace_id": ORG_A})))
        self.assertEqual(status, 200)
        self.assertNotIn("config_cleared_reason", body)

    def test_saving_new_config_clears_the_stale_notice(self):
        """Once Owner/Manager has reviewed and re-saved, the notice must not
        keep reappearing — it already served its purpose."""
        self.connect_and_callback(tokens=dict(FAKE_TOKENS_ORG1),
                                  whoami={"organization_id": ORG_ID_1,
                                         "user_id": "u1", "preferred_username": "a@x.com"})
        self.save_site_visit_mapping(as_user=OWNER_A)
        self.connect_and_callback(tokens=dict(FAKE_TOKENS_ORG2),
                                  whoami={"organization_id": ORG_ID_2,
                                         "user_id": "u2", "preferred_username": "b@y.com"})
        # Confirm the notice is present before the fix.
        self.as_user(OWNER_A)
        _, before = parse(call(api.org_salesforce_status, event(
            "GET", "/workspaces/{workspace_id}/crm/salesforce/status",
            path={"workspace_id": ORG_A})))
        self.assertIn("config_cleared_reason", before)

        with mock.patch.object(api._salesforce, "describe_object",
                               return_value=LEAD_DESCRIBE):
            self.put_config([{"object": "Lead", "lookup_field": "Email"}])

        _, after = parse(call(api.org_salesforce_status, event(
            "GET", "/workspaces/{workspace_id}/crm/salesforce/status",
            path={"workspace_id": ORG_A})))
        self.assertNotIn("config_cleared_reason", after)
        self.assertTrue(after["configured"])

    def test_disconnect_and_fresh_connect_to_different_org_also_clears(self):
        """Not just a same-callback reconnect — an explicit disconnect
        followed by a fresh connect to a different org must behave
        identically (the stored PREVIOUS org_id is what matters, not
        whether disconnect ran in between)."""
        self.connect_and_callback(tokens=dict(FAKE_TOKENS_ORG1),
                                  whoami={"organization_id": ORG_ID_1,
                                         "user_id": "u1", "preferred_username": "a@x.com"})
        self.save_site_visit_mapping(as_user=OWNER_A)

        self.as_user(OWNER_A)
        call(api.org_salesforce_disconnect, event(
            "DELETE", "/workspaces/{workspace_id}/crm/salesforce",
            path={"workspace_id": ORG_A}))
        # Disconnect deletes the WHOLE row (including config) — confirm that
        # baseline first, since it changes what "previous" means below.
        self.assertIsNone(self.t["org_crm_connections"].get_item(
            Key={"workspace_id": ORG_A, "provider": "salesforce"}).get("Item"))

        self.connect_and_callback(tokens=dict(FAKE_TOKENS_ORG2),
                                  whoami={"organization_id": ORG_ID_2,
                                         "user_id": "u2", "preferred_username": "b@y.com"})
        status, body = self.get_config()
        self.assertEqual(status, 200)
        self.assertEqual(body["config"]["mappings"], [])


# ===========================================================================
# 6. ROUTER
# ===========================================================================
class TestRouter(unittest.TestCase):
    def test_org_config_routes_registered(self):
        self.assertIs(api._ROUTES[
            ("GET", "/workspaces/{workspace_id}/crm/salesforce/objects")],
            api.org_salesforce_list_objects)
        self.assertIs(api._ROUTES[
            ("GET", "/workspaces/{workspace_id}/crm/salesforce/fields/{object_name}")],
            api.org_salesforce_list_fields)
        self.assertIs(api._ROUTES[
            ("GET", "/workspaces/{workspace_id}/crm/salesforce/config")],
            api.org_salesforce_get_config)
        self.assertIs(api._ROUTES[
            ("PUT", "/workspaces/{workspace_id}/crm/salesforce/config")],
            api.org_salesforce_put_config)

    def test_personal_config_routes_untouched(self):
        self.assertIs(api._ROUTES[("GET", "/crm/salesforce/objects")],
                      api.salesforce_list_objects)
        self.assertIs(api._ROUTES[("GET", "/crm/salesforce/fields/{object_name}")],
                      api.salesforce_list_fields)
        self.assertIs(api._ROUTES[("GET", "/crm/salesforce/config")],
                      api.salesforce_get_config)
        self.assertIs(api._ROUTES[("PUT", "/crm/salesforce/config")],
                      api.salesforce_put_config)


if __name__ == "__main__":
    unittest.main()
