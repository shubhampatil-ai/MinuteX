#!/usr/bin/env python3
"""test_site_salesforce.py — the Site architecture's org connection: org id,
object/field discovery, record search, manual Task mappings, Task
create/update, people sync and isolation — all through the workspace's
Salesforce Site endpoint and its pluggable auth mode (no External Client App,
no OAuth token).

The real code runs end to end: userapi routes, CrmSyncJobs, the real
crmSyncWorker handler (retry -> ChangeMessageVisibility) and the real
SiteDeliveryClient. The network is a router transport: upsert_meeting goes to
the FakeSiteReceiver (test_site_delivery), every other action to a
FakeSalesforceOrg mirroring the Apex rules that MinuteXSiteSalesforceTest pins
in a real org.

Run:  python -m pytest tests/test_site_salesforce.py
"""
import json
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_site_delivery import (SiteHarness, call, event, api, ORG, OTHER_ORG,  # noqa: E402
                                OWNER, MANAGER, MEMBER, OUTSIDER, KEY, SECRET)
import fake_salesforce_org as fso  # noqa: E402
import people_store  # noqa: E402
import site_delivery_client as sdc  # noqa: E402
import site_salesforce_schema as sss  # noqa: E402

W = "/workspaces/{workspace_id}/site-salesforce"
OTHER_SF_ORG = "00D5g00000ZZZZZZZZ"


def wpath(**extra):
    return {"workspace_id": ORG, **extra}


class Router:
    """One Site URL, one receiver: meeting delivery is answered by the
    FakeSiteReceiver, every org action by the FakeSalesforceOrg."""

    def __init__(self, receiver, org):
        self.receiver, self.org = receiver, org

    def post(self, url, body, headers, timeout):
        try:
            action = json.loads(body.decode("utf-8")).get("action")
        except ValueError:
            action = ""
        if action != "upsert_meeting":
            return self.org.post(url, body, headers, timeout)
        status, raw = self.receiver.post(url, body, headers, timeout)
        # One org: the meeting record the receiver created is a record Tasks
        # can be related to (MinuteX_Site_Meeting__c allows activities).
        try:
            reply = json.loads(raw)
            rid = (reply.get("result") or {}).get("record_id")
            if rid:
                title = json.loads(body)["payload"]["meeting"].get("title", "")
                self.org.records.setdefault("MinuteX_Site_Meeting__c", {})[rid] = title
        except (ValueError, KeyError, TypeError):
            pass
        return status, raw


# ===========================================================================
class SchemaModuleTests(unittest.TestCase):
    def test_due_dates(self):
        self.assertEqual(sss.compute_due_date({"type": "none"}), "")
        self.assertEqual(sss.compute_due_date({"type": "days_after_meeting", "days": 7},
                                              meeting_started_at="2026-09-20T10:00:00+00:00"),
                         "2026-09-27")
        self.assertEqual(sss.compute_due_date({"type": "days_after_creation", "days": 1},
                                              today=date(2026, 12, 31)), "2027-01-01")
        self.assertEqual(sss.meeting_started_at("1757000000")[:10], "2025-09-04")
        for bad in ("2026-02-30", "tomorrow", "2026/10/01"):
            with self.assertRaises(sss.SchemaError):
                sss.parse_iso_date(bad)

    def test_record_ids_must_match_the_object(self):
        self.assertTrue(sss.valid_record_id("a01000000000125AAA", "a01"))
        self.assertTrue(sss.valid_record_id("a01000000000125", "a01"))
        self.assertFalse(sss.valid_record_id("001000000000001AAA", "a01"))
        self.assertFalse(sss.valid_record_id("a01'; DROP", "a01"))
        self.assertFalse(sss.valid_record_id("a01000000000125AAA", ""))

    def test_fit_fields_trims_picklists_never_fields(self):
        big = [{"api_name": f"F{i}__c", "picklist_values":
                [{"value": "v" * 200, "label": "l", "default": False}] * 200}
               for i in range(20)]
        fitted, trimmed = sss.fit_fields(big, max_bytes=100_000)
        self.assertTrue(trimmed)
        self.assertEqual(len(fitted), 20)
        self.assertEqual(sss.fit_fields([{"api_name": "A"}]), ([{"api_name": "A"}], False))

    def test_api_names_are_strict(self):
        for good in ("Account", "Site_Visit__c", "ns__Obj__c"):
            self.assertTrue(sss.is_api_name(good))
        for bad in ("", "1Account", "Account; DELETE", "../x", "a" * 81, None):
            self.assertFalse(sss.is_api_name(bad))


# ===========================================================================
class SFHarness(SiteHarness):
    def setUp(self):
        super().setUp()
        self.org = fso.FakeSalesforceOrg()
        router = Router(self.receiver, self.org)
        p = mock.patch.object(api, "_site_client", side_effect=lambda timeout=None:
                              sdc.SiteDeliveryClient(router))
        p.start()
        self.addCleanup(p.stop)
        self.assertEqual(self.configure()[0], 200)  # the Site endpoint, auth_mode none
        self.put_meeting(recorded_at="1790000000")  # 2026-09-21T13:33:20Z

    # -- helpers -----------------------------------------------------------
    def connect(self, as_user=OWNER, wid=ORG, **body):
        self.as_user(as_user)
        out = call(api.site_sf_connect, event("PUT", W + "/connect", {"workspace_id": wid}, body))
        self.as_user(OWNER)
        return out

    def connection_row(self):
        return self.t["sf_connections"].get_item(Key={"workspace_id": ORG}).get("Item")

    def discover(self, wid=ORG):
        status, body = call(api.site_sf_discover_start,
                            event("POST", W + "/discover", {"workspace_id": wid}))
        self.assertEqual(status, 202, body)
        self.drain()
        return self.job(body["job"]["job_id"])

    def sync_people(self):
        status, body = call(api.site_people_sync_start, event(
            "POST", "/workspaces/{workspace_id}/site-delivery/people-sync", wpath()))
        self.assertEqual(status, 202, body)
        self.drain()
        return self.job(body["job"]["job_id"])

    def ready(self, **connect_kw):
        self.assertEqual(self.connect(**connect_kw)[0], 200)
        self.assertEqual(self.discover()["status"], "SYNCED")
        self.assertEqual(self.sync_people()["status"], "SYNCED")

    def person(self, user_id):
        return f"salesforce#user#{user_id}"

    def mapping(self, as_user=OWNER, **overrides):
        body = {"name": "Follow-up Call", "object_api_name": "Site_Visit__c",
                "subject": "Follow-up Call", "status": "Not Started", "priority": "High",
                "default_assignee_person_key": self.person(fso.REP_USER),
                "due_rule": {"type": "days_after_meeting", "days": 7}}
        body.update(overrides)
        self.as_user(as_user)
        out = call(api.site_sf_mapping_create, event("POST", W + "/task-mappings", wpath(), body))
        self.as_user(OWNER)
        return out

    def create_task(self, mapping_id, as_user=OWNER, **body):
        self.as_user(as_user)
        out = call(api.site_sf_task_create, event(
            "POST", W + "/tasks", wpath(), {"mapping_id": mapping_id, **body}))
        self.as_user(OWNER)
        return out

    def push_meeting(self):
        """Push Meeting, delivered; returns the meeting's Salesforce record id.
        (SiteHarness.push by name: TaskTests has its own push() for tasks.)"""
        from test_site_delivery import SiteHarness as _Base
        status, body = _Base.push(self)
        self.assertEqual(status, 202, body)
        self.drain()
        return self.t["recordings"].get_item(Key={"audio_s3_key": KEY})["Item"][
            "salesforce_meeting"]["record_id"]

    def task_row(self, ref):
        return self.t["sf_tasks"].get_item(Key={"workspace_id": ORG, "task_ref": ref})["Item"]

    def objects(self, **qs):
        """The whole catalog by default (scope=all); pass scope="visible" for
        the recommended + enabled short list."""
        qs = {"scope": "all", **qs}
        return call(api.site_sf_objects, event("GET", W + "/objects", wpath(), qs=qs))


# ===========================================================================
class ConnectTests(SFHarness):
    def test_connect_pins_the_org_and_stores_no_credential(self):
        status, body = self.connect()
        self.assertEqual(status, 200, body)
        c = body["connection"]
        self.assertTrue(c["connected"])
        self.assertEqual((c["org_id"], c["org_name"]), (fso.ORG_ID, "Acme Realty"))
        self.assertEqual(c["integration_user"]["id"], fso.ADMIN_USER)
        self.assertTrue(c["task_permissions"]["createable"])
        self.assertEqual((c["site_endpoint_configured"], c["auth_mode"]), (True, "none"))
        row = self.connection_row()
        self.assertEqual(row["delivery_type"], "salesforce_site")
        for word in ("secret", "token", "client_id", "my_domain"):
            self.assertNotIn(word, json.dumps(row))
            self.assertNotIn(word, json.dumps(c))
        _user, env = self.org.calls[-1]
        self.assertEqual(env["action"], "org_info")

    def test_connect_needs_an_enabled_site_endpoint(self):
        self.configure(enabled=False)
        status, body = self.connect()
        self.assertEqual((status, body["code"]), (409, api.SITE_NOT_CONFIGURED_CODE))
        self.t["site_endpoints"].delete_item(Key={"workspace_id": ORG})
        self.assertEqual(self.connect()[1]["code"], api.SITE_NOT_CONFIGURED_CODE)
        self.assertIsNone(self.connection_row())
        _, got = call(api.site_sf_get, event("GET", W, wpath()))
        self.assertEqual((got["connection"]["connected"],
                          got["connection"]["site_endpoint_configured"]), (False, False))

    def test_integration_user_must_be_configured_in_salesforce(self):
        self.org.integration_user = None
        status, body = self.connect()
        self.assertEqual((status, body["code"]), (409, "integration_user_not_configured"))
        self.assertIsNone(self.connection_row())

    def test_rbac(self):
        self.assertEqual(self.connect(as_user=MEMBER)[0], 403)
        self.assertEqual(self.connect(as_user=OUTSIDER)[0], 404)
        self.assertEqual(self.connect(as_user=MANAGER)[0], 200)

    def test_hmac_mode_signs_every_org_call(self):
        self.org.secret = SECRET
        self.configure(auth_mode="hmac_sha256", secret=SECRET)
        status, body = self.connect()
        self.assertEqual(status, 200, body)
        self.assertEqual(body["connection"]["auth_mode"], "hmac_sha256")
        self.assertEqual(self.discover()["status"], "SYNCED")
        self.org.secret = "a-different-secret-" + "z" * 30
        status, body = self.connect()
        self.assertEqual((status, body["code"]), (409, "auth_failed"))

    def test_auth_modes_are_pluggable(self):
        self.assertIn("none", sdc.auth_modes())
        self.assertIn("hmac_sha256", sdc.auth_modes())
        status, _ = self.configure(auth_mode="mtls_someday")
        self.assertEqual(status, 400, "unregistered modes are refused")

    def test_switching_orgs_needs_confirmation_and_clears_org_data(self):
        self.ready()
        self.assertEqual(self.mapping()[0], 201)
        self.org.org_id = OTHER_SF_ORG
        status, body = self.connect()
        self.assertEqual((status, body["code"]), (409, "org_mismatch"))
        status, body = self.connect(allow_org_change=True)
        self.assertEqual(status, 200)
        self.assertEqual(body["connection"]["org_id"], OTHER_SF_ORG)
        _, mappings = call(api.site_sf_mappings_list, event("GET", W + "/task-mappings", wpath()))
        self.assertEqual(mappings["mappings"], [])
        self.assertEqual(self.objects()[1]["objects"], [])

    def test_an_answer_from_another_org_is_refused(self):
        self.ready()
        self.org.org_id = OTHER_SF_ORG  # same Site URL now answers for another org
        status, body = call(api.site_sf_object_records, event(
            "GET", W + "/objects/{object}/records", wpath(object="Account"), qs={"q": ""}))
        self.assertEqual((status, body["code"]), (409, "org_mismatch"))
        status, body = call(api.site_sf_discover_start,
                            event("POST", W + "/discover", wpath()))
        self.drain()
        job = self.job(body["job"]["job_id"])
        self.assertEqual(job["status"], "FAILED")
        self.assertEqual(self.connection_row()["org_id"], fso.ORG_ID, "pin unchanged")

    def test_receiver_missing_is_reported_clearly(self):
        self.org.faults = [("raw", 404, b'[{"errorCode":"NOT_FOUND",'
                                        b'"message":"Could not find a match for URL"}]')]
        status, body = self.connect()
        self.assertEqual((status, body["code"]), (409, "endpoint_not_found"))

    def test_disconnect_keeps_mappings_and_the_site_endpoint(self):
        self.ready()
        self.mapping()
        status, _ = call(api.site_sf_disconnect, event("DELETE", W, wpath()))
        self.assertEqual(status, 200)
        _, body = call(api.site_sf_get, event("GET", W, wpath()))
        self.assertFalse(body["connection"]["connected"])
        self.assertTrue(body["connection"]["site_endpoint_configured"])
        self.assertEqual(self.objects()[1]["objects"], [])
        _, mappings = call(api.site_sf_mappings_list, event("GET", W + "/task-mappings", wpath()))
        self.assertEqual(len(mappings["mappings"]), 1)
        self.assertIsNotNone(self.t["site_endpoints"].get_item(
            Key={"workspace_id": ORG}).get("Item"))

    def test_workspaces_are_isolated(self):
        self.ready()
        _, body = call(api.site_sf_get, event("GET", W, {"workspace_id": OTHER_ORG}))
        self.assertFalse(body["connection"]["connected"])
        status, body = call(api.site_sf_objects, event("GET", W + "/objects",
                                                       {"workspace_id": OTHER_ORG}))
        self.assertEqual(body["objects"], [])
        self.as_user(OUTSIDER)
        self.assertEqual(self.objects()[0], 404)


# ===========================================================================
class DiscoveryTests(SFHarness):
    def test_discovers_standard_and_custom_objects_with_permissions_and_fields(self):
        self.connect()
        job = self.discover()
        self.assertEqual(job["status"], "SYNCED")
        _, body = self.objects()
        objs = {o["api_name"]: o for o in body["objects"]}
        self.assertTrue(objs["Site_Visit__c"]["is_custom"])
        self.assertEqual(objs["Site_Visit__c"]["task_relation"], "what")
        self.assertEqual(objs["Account"]["task_relation"], "what")
        self.assertEqual(objs["Contact"]["task_relation"], "who")
        # Offered: standard business objects + EVERY readable custom object,
        # including Booking__c whose activities are off (a meeting can still
        # be about one). Not offered: a standard object outside the business
        # list (Idea), MinuteX's own object, Task, User.
        self.assertEqual(list(objs), fso.OFFERED, "standard in business order, then custom")
        self.assertEqual(objs["Booking__c"]["task_relation"], "", "shown, but no Tasks")
        self.assertTrue(objs["Booking__c"]["meeting_relationship_supported"])
        self.assertFalse(objs["Booking__c"]["task_relationship_supported"])
        self.assertTrue(objs["Site_Visit__c"]["task_relationship_supported"])
        for hidden in ("Idea", "MinuteX_Site_Meeting__c", "Task", "User"):
            self.assertNotIn(hidden, objs)
        self.assertEqual(objs["Account"]["category"], "standard")
        self.assertEqual(objs["Site_Visit__c"]["category"], "custom")
        self.assertTrue(objs["Lead"]["createable"])
        # Fields are described up front only for the recommended objects (and
        # Task); a custom object's fields are described live on first view.
        self.assertTrue(all(objs[n]["fields_synced_at"] for n in sss.RECOMMENDED_OBJECTS
                            if n in objs))
        self.assertFalse(objs["Site_Visit__c"]["fields_synced_at"])
        _, fields = call(api.site_sf_object_fields, event(
            "GET", W + "/objects/{object}/fields", wpath(object="Site_Visit__c")))
        names = {f["api_name"]: f for f in fields["fields"]}
        self.assertEqual(set(names), {"Name", "Visit_Date__c", "Customer__c",
                                      "Assigned_User__c", "Status__c"})
        self.assertEqual(names["Customer__c"]["reference_to"], ["Account"])
        self.assertTrue(names["Visit_Date__c"]["is_custom"])
        self.assertEqual([v["value"] for v in names["Status__c"]["picklist_values"]],
                         ["Planned", "Done"])
        summary = self.t["sf_connections"].get_item(Key={"workspace_id": ORG})["Item"]["discovery"]
        self.assertEqual(summary["status"], "SYNCED")
        self.assertEqual(summary["counts"]["objects"], len(fso.OFFERED))
        # Not stored at all (Idea, MinuteX_Site_Meeting__c, User): counted
        # only, by reason. Task is stored for its fields but never offered.
        self.assertEqual(summary["counts"]["objects_excluded"], 3)
        self.assertEqual(summary["counts"]["excluded_minutex"], 1)
        self.assertEqual(summary["counts"]["objects_custom"], 2)
        stored = {row["item_key"] for row in self.t["sf_schema"].items.values()
                  if row["item_key"].startswith("OBJ#")}
        self.assertNotIn("OBJ#idea", stored)
        self.assertIn("OBJ#task", stored)

    def test_limited_user_sees_only_what_salesforce_grants(self):
        self.org.integration_user = fso.LIMITED_USER
        self.connect()
        self.discover()
        _, body = self.objects()
        objs = {o["api_name"]: o for o in body["objects"]}
        self.assertNotIn("Opportunity", objs, "no read access -> not offered")
        self.assertNotIn("Contact", objs)
        self.assertTrue(objs["Lead"]["queryable"])
        self.assertFalse(objs["Lead"]["createable"], "Lead is read-only for this user")
        self.assertFalse(objs["Lead"]["updateable"])
        conn = self.t["sf_connections"].get_item(Key={"workspace_id": ORG})["Item"]
        self.assertFalse(conn["task_permissions"]["createable"])
        self.assertGreater(conn["discovery"]["counts"]["objects_inaccessible"], 0)
        _, fields = call(api.site_sf_object_fields, event(
            "GET", W + "/objects/{object}/fields", wpath(object="Lead")))
        self.assertFalse(any(f["createable"] for f in fields["fields"]))

    def test_rediscovery_removes_objects_that_became_inaccessible(self):
        self.connect()
        self.discover()
        self.org.perms[fso.ADMIN_USER]["Case"] = {}
        job = self.discover()
        self.assertEqual(job["result"]["counts"]["removed"], 2)  # OBJ# + FLD#
        self.assertNotIn("Case", {o["api_name"] for o in self.objects()[1]["objects"]})

    def test_segmented_discovery_and_transient_failures_complete(self):
        self.connect()
        self.org.faults = [None, None, ("status", 503)]
        with mock.patch.object(api, "SITE_SF_DISCOVER_BUDGET_SECONDS", -1):
            job = self.discover()
        self.assertEqual(job["status"], "SYNCED")
        self.assertEqual(len(self.objects()[1]["objects"]), len(fso.OFFERED))

    def test_object_list_filters_and_unknown_objects(self):
        self.connect()
        self.discover()
        _, body = self.objects(task_relatable="true")
        names = {o["api_name"] for o in body["objects"]}
        self.assertIn("Site_Visit__c", names)
        self.assertNotIn("Booking__c", names)
        self.assertNotIn("Task", names)
        self.assertEqual({o["api_name"] for o in self.objects(custom="true")[1]["objects"]},
                         {"Site_Visit__c", "Booking__c"})
        self.assertEqual([o["api_name"] for o in self.objects(q="visit")[1]["objects"]],
                         ["Site_Visit__c"])
        status, body = call(api.site_sf_object_fields, event(
            "GET", W + "/objects/{object}/fields", wpath(object="Nope__c")))
        self.assertEqual((status, body["code"]), (404, "unknown_object"))
        status, _ = call(api.site_sf_object_fields, event(
            "GET", W + "/objects/{object}/fields", wpath(object="../../etc")))
        self.assertEqual(status, 404)

    def test_fields_refresh_is_live(self):
        self.connect()
        self.discover()
        # First view describes the object live and caches it.
        call(api.site_sf_object_fields, event(
            "GET", W + "/objects/{object}/fields", wpath(object="Site_Visit__c")))
        fso.FIELDS["Site_Visit__c"].append(fso._f("Notes__c", "Notes", custom=True))
        self.addCleanup(fso.FIELDS["Site_Visit__c"].pop)
        _, cached = call(api.site_sf_object_fields, event(
            "GET", W + "/objects/{object}/fields", wpath(object="Site_Visit__c")))
        self.assertNotIn("Notes__c", {f["api_name"] for f in cached["fields"]})
        _, fresh = call(api.site_sf_object_fields, event(
            "GET", W + "/objects/{object}/fields", wpath(object="Site_Visit__c"),
            qs={"refresh": "true"}))
        self.assertIn("Notes__c", {f["api_name"] for f in fresh["fields"]})


# ===========================================================================
class RecordSearchTests(SFHarness):
    def search(self, obj, q=""):
        return call(api.site_sf_object_records, event(
            "GET", W + "/objects/{object}/records", wpath(object=obj), qs={"q": q}))

    def test_search_custom_and_standard_objects(self):
        self.connect()
        self.discover()
        status, body = self.search("Site_Visit__c", "sv-0012")
        self.assertEqual(status, 200)
        self.assertEqual([r["name"] for r in body["records"]], ["SV-00125", "SV-00126"])
        self.assertEqual([r["name"] for r in self.search("Account", "mumbai")[1]["records"]],
                         ["Acme Mumbai"])

    def test_only_discovered_queryable_objects_can_be_searched(self):
        self.org.integration_user = fso.LIMITED_USER
        self.connect()
        self.discover()
        self.assertEqual(self.search("Opportunity")[0], 404)  # never discovered
        self.org.perms[fso.LIMITED_USER]["Lead"] = {}           # revoked since
        status, body = self.search("Lead")
        self.assertEqual((status, body["code"]), (403, "salesforce_permission_denied"))
        status, _ = self.search("Account", "x" * 101)
        self.assertEqual(status, 400)


# ===========================================================================
class MappingTests(SFHarness):
    def test_mapping_needs_discovery_first(self):
        self.connect()
        self.assertEqual(self.mapping()[1]["code"], "discovery_required")

    def test_create_list_update_delete(self):
        self.ready()
        status, body = self.mapping()
        self.assertEqual(status, 201, body)
        m = body["mapping"]
        self.assertEqual(m["object_api_name"], "Site_Visit__c")
        self.assertEqual(m["org_id"], fso.ORG_ID)
        self.assertEqual(m["problems"], [])
        path = wpath(mapping_id=m["mapping_id"])
        status, body = call(api.site_sf_mapping_update, event(
            "PUT", W + "/task-mappings/{mapping_id}", path, {"subject": "Site revisit"}))
        self.assertEqual((status, body["mapping"]["subject"]), (200, "Site revisit"))
        self.assertEqual(body["mapping"]["object_api_name"], "Site_Visit__c")
        status, _ = call(api.site_sf_mapping_update, event(
            "PUT", W + "/task-mappings/{mapping_id}", path, {"status": "Dreaming"}))
        self.assertEqual(status, 400)
        self.as_user(MEMBER)
        _, listed = call(api.site_sf_mappings_list, event("GET", W + "/task-mappings", wpath()))
        self.assertEqual(listed["mappings"][0]["subject"], "Site revisit")
        self.assertEqual(self.mapping(as_user=MEMBER)[0], 403)
        self.as_user(OWNER)
        self.assertEqual(call(api.site_sf_mapping_delete, event(
            "DELETE", W + "/task-mappings/{mapping_id}", path))[0], 200)

    def test_invalid_mappings_are_refused(self):
        self.ready()
        cases = [
            ({"object_api_name": "Booking__c"}, "invalid_object"),
            ({"object_api_name": "Nope__c"}, "invalid_object"),
            ({"object_api_name": "Account; DELETE"}, "invalid_object"),
            ({"extra_fields": {"Nope__c": "x"}}, "invalid_field"),
            ({"extra_fields": {"WhatId": "001000000000001AAA"}}, "invalid_field"),
            ({"extra_fields": {"Type": "Carrier pigeon"}}, "invalid_field"),
            ({"default_assignee_person_key": "salesforce#contact#003000000000001AAA"},
             "invalid_assignee"),
            ({"default_assignee_person_key": self.person(fso.INACTIVE_USER)}, "invalid_assignee"),
            ({"default_assignee_person_key": "salesforce#user#nobody"}, "invalid_assignee"),
            ({"status": "Dreaming"}, "invalid_status"),
            ({"priority": "Urgent!!"}, "invalid_priority"),
            ({"due_rule": {"type": "whenever"}}, "invalid_due_rule"),
            ({"due_rule": {"type": "days_after_meeting", "days": 9999}}, "invalid_due_rule"),
            ({"default_record_id": "001000000000001AAA"}, "invalid_record"),
            ({"subject": ""}, "invalid_mapping"),
        ]
        for overrides, code in cases:
            status, body = self.mapping(**overrides)
            self.assertEqual((status, body.get("code")), (400, code), overrides)
        self.assertEqual(self.mapping(extra_fields={"Type": "Call"})[0], 201)

    def test_list_flags_mappings_whose_object_disappeared(self):
        self.ready()
        self.mapping(object_api_name="Account", name="Account follow-up")
        self.org.perms[fso.ADMIN_USER]["Account"] = {}
        self.discover()
        _, listed = call(api.site_sf_mappings_list, event("GET", W + "/task-mappings", wpath()))
        self.assertEqual(listed["mappings"][0]["problems"], ["object_not_discovered"])


# ===========================================================================
class TaskTests(SFHarness):
    def setUp(self):
        super().setUp()
        self.ready()
        self.mapping_id = self.mapping()[1]["mapping"]["mapping_id"]
        self.meeting_record = self.push_meeting()

    def test_task_on_a_custom_object_record_assigned_and_due(self):
        status, body = self.create_task(self.mapping_id, record_id="a01000000000125AAA",
                                        record_name="SV-00125", meeting_key=KEY)
        self.assertEqual(status, 202, body)
        ref = body["task"]["task_ref"]
        self.drain()
        row = self.task_row(ref)
        self.assertEqual(row["sync_status"], "SYNCED")
        sf = self.org.tasks[row["salesforce_task_id"]]
        self.assertEqual(sf["what_id"], "a01000000000125AAA")
        self.assertEqual(sf["related_object_type"], "Site_Visit__c")
        self.assertEqual(sf["owner_id"], fso.REP_USER)
        self.assertEqual(sf["due_date"], "2026-09-28")  # meeting 2026-09-21 + 7
        self.assertEqual((sf["subject"], sf["status"], sf["priority"]),
                         ("Follow-up Call", "Not Started", "High"))

    def test_task_on_a_standard_object_and_on_a_contact(self):
        acct = self.mapping(object_api_name="Account", name="Account call")[1]["mapping"]
        who = self.mapping(object_api_name="Contact", name="Contact call")[1]["mapping"]
        _, a = self.create_task(acct["mapping_id"], record_id="001000000000002AAA",
                                due_date="2026-10-01")
        _, c = self.create_task(who["mapping_id"], record_id="003000000000001AAA")
        self.drain()
        ta = self.org.tasks[self.task_row(a["task"]["task_ref"])["salesforce_task_id"]]
        tc = self.org.tasks[self.task_row(c["task"]["task_ref"])["salesforce_task_id"]]
        self.assertEqual((ta["what_id"], ta["due_date"]), ("001000000000002AAA", "2026-10-01"))
        self.assertEqual(tc["who_id"], "003000000000001AAA")

    def test_invalid_task_requests_are_refused_before_salesforce(self):
        cases = [
            ({}, "record_required"),
            ({"record_id": "001000000000001AAA"}, "invalid_record"),
            ({"record_id": "a01000000000125AAA", "assignee_person_key":
              self.person(fso.INACTIVE_USER)}, "invalid_assignee"),
            ({"record_id": "a01000000000125AAA", "assignee_person_key":
              "salesforce#contact#003000000000001AAA"}, "invalid_assignee"),
            ({"record_id": "a01000000000125AAA", "due_date": "2026-02-31"}, "invalid_due_date"),
            ({"record_id": "a01000000000125AAA", "status": "Dreaming"}, "invalid_status"),
        ]
        for extra, code in cases:
            status, body = self.create_task(self.mapping_id, **extra)
            self.assertEqual((status, body.get("code")), (400, code), extra)
        no_default = self.mapping(name="No assignee", default_assignee_person_key="")[1]["mapping"]
        status, body = self.create_task(no_default["mapping_id"], record_id="a01000000000125AAA")
        self.assertEqual(body["code"], "assignee_required")
        self.assertEqual(self.org.tasks, {})
        self.assertEqual(self.queue, [])

    def test_record_not_visible_in_salesforce_fails_clearly(self):
        _, body = self.create_task(self.mapping_id, record_id="a01000000000999AAA")
        self.drain()
        row = self.task_row(body["task"]["task_ref"])
        self.assertEqual(row["sync_status"], "FAILED")
        self.assertEqual(row["last_error"]["code"], "invalid_record")
        job = self.job(row["job_id"])
        self.assertEqual((job["last_error_category"], job["attempt_count"]), ("validation", 1))

    def test_permission_denied_for_a_limited_integration_user(self):
        self.org.integration_user = fso.LIMITED_USER
        self.connect()
        self.discover()
        status, body = self.create_task(self.mapping_id, record_id="a01000000000125AAA")
        self.assertEqual((status, body["code"]), (403, "salesforce_permission_denied"))
        self.assertEqual(body["error"],
                         "Salesforce permissions do not allow Task creation for this object/user.")

    def test_salesforce_refusal_after_permissions_change_is_not_silent(self):
        _, body = self.create_task(self.mapping_id, record_id="a01000000000125AAA")
        self.org.perms[fso.ADMIN_USER]["Task"] = {"read": True}  # revoked after discovery
        self.drain()
        row = self.task_row(body["task"]["task_ref"])
        self.assertEqual(row["sync_status"], "FAILED")
        self.assertEqual(row["last_error"]["code"], "permission_denied")
        self.assertIn("do not allow Task creation", row["last_error"]["message"])

    def test_duplicate_request_creates_one_task(self):
        a = self.create_task(self.mapping_id, record_id="a01000000000125AAA",
                             client_request_id="tap-1")
        b = self.create_task(self.mapping_id, record_id="a01000000000125AAA",
                             client_request_id="tap-1")
        self.assertEqual(a[0], 202)
        self.assertEqual((b[0], b[1]["duplicate"]), (200, True))
        self.assertEqual(a[1]["task"]["task_ref"], b[1]["task"]["task_ref"])
        self.drain()
        self.assertEqual(len(self.org.tasks), 1)

    def test_transient_failures_retry_with_the_same_idempotency_key(self):
        self.org.faults = [("status", 503), "timeout_after_commit"]
        _, body = self.create_task(self.mapping_id, record_id="a01000000000125AAA")
        self.drain()
        row = self.task_row(body["task"]["task_ref"])
        self.assertEqual(row["sync_status"], "SYNCED")
        self.assertEqual(len(self.org.tasks), 1, "the committed-then-timed-out attempt was replayed")
        keys = {env["idempotency_key"] for _u, env in self.org.calls
                if env["action"] == "create_task"}
        self.assertEqual(len(keys), 1)
        self.assertTrue(self.job(row["job_id"])["result"]["duplicate"])

    def test_failed_task_can_be_retried_as_a_new_intent(self):
        self.org.faults = [("status", 422)]
        _, body = self.create_task(self.mapping_id, record_id="a01000000000125AAA")
        ref = body["task"]["task_ref"]
        self.drain()
        self.assertEqual(self.task_row(ref)["sync_status"], "FAILED")
        status, _ = call(api.site_sf_task_retry, event(
            "POST", W + "/tasks/{task_ref}/retry", wpath(task_ref=ref)))
        self.assertEqual(status, 202)
        self.drain()
        row = self.task_row(ref)
        self.assertEqual((row["sync_status"], row["generation"]), ("SYNCED", 1))

    def test_update_changes_the_salesforce_task(self):
        _, body = self.create_task(self.mapping_id, record_id="a01000000000125AAA")
        ref = body["task"]["task_ref"]
        self.drain()
        path = wpath(task_ref=ref)
        status, body = call(api.site_sf_task_update, event(
            "PATCH", W + "/tasks/{task_ref}", path,
            {"subject": "Follow-up Call (moved)", "due_date": "2026-10-05",
             "assignee_person_key": self.person(fso.ADMIN_USER)}))
        self.assertEqual(status, 202, body)
        status, again = call(api.site_sf_task_update, event(
            "PATCH", W + "/tasks/{task_ref}", path, {"subject": "x"}))
        self.assertEqual((status, again["code"]), (409, "task_in_flight"))
        self.drain()
        row = self.task_row(ref)
        sf = self.org.tasks[row["salesforce_task_id"]]
        self.assertEqual((sf["subject"], sf["due_date"], sf["owner_id"]),
                         ("Follow-up Call (moved)", "2026-10-05", fso.ADMIN_USER))
        self.assertEqual(sf["what_id"], "a01000000000125AAA", "relationship kept")
        self.assertEqual((row["version"], row["sync_status"]), (2, "SYNCED"))
        self.assertEqual(len(self.org.tasks), 1)

    def test_only_the_creator_or_a_manager_can_change_a_task(self):
        _, body = self.create_task(self.mapping_id, as_user=MEMBER, record_id="a01000000000125AAA")
        self.drain()
        path = wpath(task_ref=body["task"]["task_ref"])
        self.as_user(OUTSIDER)
        self.assertEqual(call(api.site_sf_task_get, event("GET", W + "/tasks/{task_ref}", path))[0], 404)
        self.as_user(MANAGER)
        self.assertEqual(call(api.site_sf_task_update, event(
            "PATCH", W + "/tasks/{task_ref}", path, {"subject": "ok"}))[0], 202)

    def test_a_task_never_reaches_a_different_org(self):
        _, body = self.create_task(self.mapping_id, record_id="a01000000000125AAA")
        self.org.org_id = OTHER_SF_ORG
        self.connect(allow_org_change=True)
        self.drain()
        row = self.task_row(body["task"]["task_ref"])
        self.assertEqual((row["sync_status"], row["last_error"]["code"]),
                         ("FAILED", "org_mismatch"))
        self.assertEqual(self.org.tasks, {})

    def adhoc(self, **body):
        self.as_user(MEMBER)
        out = call(api.site_sf_task_create, event("POST", W + "/tasks", wpath(), body))
        self.as_user(OWNER)
        return out

    def test_task_from_a_meeting_without_a_mapping(self):
        status, body = self.adhoc(object_api_name="Site_Visit__c", record_id="a01000000000125AAA",
                                  record_name="SV-00125", subject="Follow-up Call",
                                  assignee_person_key=self.person(fso.REP_USER),
                                  due_date="2026-09-28", status="Not Started",
                                  priority="High", meeting_key=KEY, client_request_id="m-1")
        self.assertEqual(status, 202, body)
        self.assertEqual((body["task"]["mapping_id"], body["task"]["meeting_key"]), ("", KEY))
        self.drain()
        row = self.task_row(body["task"]["task_ref"])
        self.assertEqual(row["sync_status"], "SYNCED")
        sf = self.org.tasks[row["salesforce_task_id"]]
        self.assertEqual((sf["what_id"], sf["owner_id"], sf["subject"], sf["due_date"]),
                         ("a01000000000125AAA", fso.REP_USER, "Follow-up Call", "2026-09-28"))
        again = self.adhoc(object_api_name="Site_Visit__c", record_id="a01000000000125AAA",
                           subject="Follow-up Call", assignee_person_key=self.person(fso.REP_USER),
                           client_request_id="m-1")
        self.assertEqual((again[0], again[1]["duplicate"]), (200, True))
        self.assertEqual(len(self.org.tasks), 1)

    def test_task_without_a_mapping_has_no_defaults_to_fall_back_on(self):
        base = {"object_api_name": "Site_Visit__c", "record_id": "a01000000000125AAA",
                "subject": "Call", "assignee_person_key": self.person(fso.REP_USER)}
        cases = [
            ({"object_api_name": ""}, 400, "invalid_object"),
            ({"object_api_name": "Account; DELETE"}, 400, "invalid_object"),
            ({"object_api_name": "Booking__c", "record_id": ""}, 400, "object_not_task_relatable"),
            ({"object_api_name": "Nope__c", "record_id": ""}, 409, "mapping_invalid"),
            ({"record_id": ""}, 400, "record_required"),
            ({"assignee_person_key": ""}, 400, "assignee_required"),
            ({"subject": ""}, 400, "invalid_task"),
        ]
        for overrides, status, code in cases:
            got = self.adhoc(**{**base, **overrides})
            self.assertEqual((got[0], got[1].get("code")), (status, code), overrides)
        self.assertEqual(self.org.tasks, {})

    # -- pushing EXISTING MinuteX tasks ---------------------------------------
    def mx_task(self, task_id="t-ai-1", owner=OWNER, **over):
        row = {"task_id": task_id, "owner_user_id": owner, "source_recording_id": KEY,
               "title": "Send the revised quotation", "description": "Include GST.",
               "status": "Open", "priority": "Medium", "due_date": "2026-09-30",
               "due_date_normalized": "2026-09-30", "source_type": "AI",
               "created_at": "2026-09-21T13:40:00Z"}
        row.update(over)
        row = {k: v for k, v in row.items() if v != "" or k != "source_recording_id"}
        self.t["tasks"].put_item(Item=row)
        return row

    def push(self, task_id="t-ai-1", as_user=OWNER, **body):
        self.as_user(as_user)
        out = call(api.site_sf_task_create, event("POST", W + "/tasks", wpath(), {
            "minutex_task_id": task_id, "object_api_name": "Site_Visit__c",
            "record_id": "a01000000000125AAA", "record_name": "SV-00125",
            "assignee_person_key": self.person(fso.REP_USER), **body}))
        self.as_user(OWNER)
        return out

    def test_an_ai_task_is_pushed_with_its_own_content(self):
        self.mx_task()
        status, body = self.push()
        self.assertEqual(status, 202, body)
        self.assertEqual((body["task"]["minutex_task_id"], body["task"]["meeting_key"]),
                         ("t-ai-1", KEY))
        self.drain()
        sf = self.org.tasks[self.task_row(body["task"]["task_ref"])["salesforce_task_id"]]
        self.assertEqual((sf["subject"], sf["due_date"], sf["what_id"], sf["owner_id"]),
                         ("Send the revised quotation", "2026-09-30",
                          "a01000000000125AAA", fso.REP_USER))
        # MinuteX Medium/Open -> this org's Normal/Not Started.
        self.assertEqual((sf["priority"], sf["status"]), ("Normal", "Not Started"))
        self.assertIn("Include GST.", sf["description"])
        self.assertIn("Client Discussion", sf["description"])

    def test_pushing_the_same_task_again_updates_its_salesforce_task(self):
        self.mx_task()
        _, first = self.push()
        self.drain()
        self.t["tasks"].update_item(
            Key={"task_id": "t-ai-1"}, UpdateExpression="SET #s = :s, title = :t",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":s": "Completed", ":t": "Send the final quotation"})
        status, again = self.push()
        self.assertEqual((status, again["updated"]), (202, True))
        self.assertEqual(again["task"]["task_ref"], first["task"]["task_ref"])
        self.drain()
        self.assertEqual(len(self.org.tasks), 1)
        sf = next(iter(self.org.tasks.values()))
        self.assertEqual((sf["subject"], sf["status"]), ("Send the final quotation", "Completed"))

    def test_a_push_in_flight_is_not_sent_twice(self):
        self.mx_task()
        self.push()
        status, body = self.push()
        self.assertEqual((status, body["code"]), (409, "task_in_flight"))

    def test_only_a_visible_meeting_task_can_be_pushed(self):
        self.mx_task(task_id="t-other", owner=OUTSIDER)
        self.assertEqual(self.push(task_id="t-other")[0], 404)
        self.assertEqual(self.push(task_id="nope")[0], 404)
        self.mx_task(task_id="t-loose", source_recording_id="")
        status, body = self.push(task_id="t-loose")
        self.assertEqual((status, body["code"]), (400, "task_not_from_meeting"))
        self.assertEqual(self.org.tasks, {})

    def test_an_unresolved_spoken_due_date_is_kept_as_text_not_guessed(self):
        self.mx_task(due_date="next week", due_date_normalized="")
        _, body = self.push()
        self.drain()
        sf = self.org.tasks[self.task_row(body["task"]["task_ref"])["salesforce_task_id"]]
        self.assertEqual(sf["due_date"], "")
        self.assertIn("Due (as said in the meeting): next week", sf["description"])

    def test_task_list_by_meeting(self):
        self.create_task(self.mapping_id, record_id="a01000000000125AAA", meeting_key=KEY)
        self.create_task(self.mapping_id, record_id="a01000000000126AAA")
        _, body = call(api.site_sf_tasks_list, event("GET", W + "/tasks", wpath(),
                                                     qs={"meeting_key": KEY}))
        self.assertEqual(len(body["tasks"]), 1)
        status, _ = self.create_task(self.mapping_id, record_id="a01000000000125AAA",
                                     meeting_key="recordings/other/none.wav")
        self.assertEqual(status, 404)


# ===========================================================================
class PeopleThroughTheSiteTests(SFHarness):
    def test_users_and_contacts_reach_the_people_store(self):
        self.connect()
        self.assertEqual(self.sync_people()["status"], "SYNCED")
        rep = people_store.get_person(self.t["people"], ORG, self.person(fso.REP_USER))
        self.assertEqual((rep["name"], rep["status"]), ("Shubham Patil", "active"))
        self.assertIsNotNone(people_store.get_person(
            self.t["people"], ORG, "salesforce#contact#003000000000001AAA"))
        _, st = call(api.site_people_sync_status, event(
            "GET", "/workspaces/{workspace_id}/site-delivery/people-sync", wpath()))
        self.assertEqual(st["people_sync"]["status"], "SYNCED")
        counts = st["people_sync"]["counts"]
        self.assertEqual(counts["contacts"], len(self.org.records["Contact"]))
        self.assertGreater(counts["users"], 0)
        self.assertEqual(counts["users"] + counts["contacts"],
                         counts["received"] - counts["skipped"])

    def test_people_sync_needs_the_site_endpoint(self):
        self.configure(enabled=False)
        status, body = call(api.site_people_sync_start, event(
            "POST", "/workspaces/{workspace_id}/site-delivery/people-sync", wpath()))
        self.assertEqual((status, body["code"]), (409, api.SITE_NOT_CONFIGURED_CODE))

    def test_tagging_still_uses_the_local_store(self):
        self.connect()
        self.sync_people()
        calls = len(self.org.calls)
        status, _ = call(api.site_participant_tag, event(
            "PUT", "/recordings/site-participants/{key+}", {"key": KEY},
            {"speaker_id": "0", "person_key": "salesforce#contact#003000000000001AAA"}))
        self.assertEqual(status, 200)
        self.assertEqual(len(self.org.calls), calls, "no Salesforce call while tagging")


# ===========================================================================
class EndToEndTests(SFHarness):
    def test_connect_discover_sync_map_select_assign_create_verify(self):
        # Connect through the Site (auth_mode none) -> Org ID
        status, body = self.connect()
        self.assertEqual(body["connection"]["org_id"], fso.ORG_ID)
        # Discover objects and fields
        self.assertEqual(self.discover()["status"], "SYNCED")
        relatable = {o["api_name"] for o in self.objects(task_relatable="true")[1]["objects"]}
        self.assertTrue({"Account", "Site_Visit__c"} <= relatable)
        # Users + Contacts -> People Store
        self.assertEqual(self.sync_people()["status"], "SYNCED")
        # Select object (manual mapping)
        mapping = self.mapping()[1]["mapping"]
        # Select record
        _, found = call(api.site_sf_object_records, event(
            "GET", W + "/objects/{object}/records", wpath(object="Site_Visit__c"),
            qs={"q": "SV-00125"}))
        record = found["records"][0]
        # Select assignee from the People Store
        self.as_user(MEMBER)
        _, users = call(api.people_search, event(
            "GET", "/workspaces/{workspace_id}/people", wpath(), qs={"type": "user", "q": "shubham"}))
        assignee = users["people"][0]
        # Create the Task
        status, body = self.create_task(mapping["mapping_id"], as_user=MEMBER,
                                        record_id=record["id"], record_name=record["name"],
                                        assignee_person_key=assignee["person_key"],
                                        client_request_id="e2e-1")
        self.assertEqual(status, 202)
        self.drain()
        # Verify the Task in Salesforce
        row = self.task_row(body["task"]["task_ref"])
        sf = self.org.tasks[row["salesforce_task_id"]]
        self.assertEqual((sf["what_id"], sf["owner_id"], sf["subject"]),
                         (record["id"], fso.REP_USER, "Follow-up Call"))
        # Duplicate request -> still one Task
        self.assertTrue(self.create_task(mapping["mapping_id"], as_user=MEMBER,
                                         record_id=record["id"],
                                         client_request_id="e2e-1")[1]["duplicate"])
        self.assertEqual(len(self.org.tasks), 1)
        # Meeting delivery through the same Site endpoint still works alongside.
        self.push()
        self.drain()
        self.assertEqual(len(self.receiver.meetings), 1)


# ===========================================================================
# Phase 2: People, filtered objects, the meeting relationship, Task
# inheritance and sync bookkeeping.
# ===========================================================================
ASHA = "salesforce#contact#003000000000001AAA"


class PeopleSyncPagingTests(SFHarness):
    def test_contacts_and_users_across_many_pages(self):
        self.connect()
        self.org.extra_contacts = 450   # 451 Contacts + 4 Users, 100 per page
        with mock.patch.object(api, "SITE_PEOPLE_PAGE_SIZE", 100):
            job = self.sync_people()
        self.assertEqual(job["status"], "SYNCED")
        self.assertEqual(job["result"]["pages"], 5)
        counts = job["result"]["counts"]
        self.assertEqual((counts["contacts"], counts["users"]), (451, 4))
        rows = list(self.t["people"].items.values())
        self.assertEqual(len(rows), 455)
        asha = people_store.get_person(self.t["people"], ORG, ASHA)
        self.assertEqual((asha["provider"], asha["type"], asha["external_id"]),
                         ("salesforce", "contact", "003000000000001AAA"),
                         "Salesforce record id preserved as external_id")
        rep = people_store.get_person(self.t["people"], ORG, self.person(fso.REP_USER))
        self.assertEqual((rep["type"], rep["external_id"]), ("user", fso.REP_USER))
        self.assertTrue(all(r["provider"] == "salesforce" for r in rows))
        self.assertEqual({r["type"] for r in rows}, {"contact", "user"})

    def test_people_search_reports_counts_by_type(self):
        self.connect()
        self.sync_people()
        _, body = call(api.people_search, event(
            "GET", "/workspaces/{workspace_id}/people", wpath(), qs={"type": "user"}))
        # Inactive users are not offered, and not counted.
        self.assertEqual(body["counts"], {"contacts": 1, "users": 3})
        self.assertEqual({p["type"] for p in body["people"]}, {"user"})
        _, body = call(api.people_search, event(
            "GET", "/workspaces/{workspace_id}/people", wpath(), qs={"type": "contact"}))
        self.assertEqual([p["name"] for p in body["people"]], ["Asha Rao"])

    def test_a_synced_person_resolves_to_one_contact(self):
        self.connect()
        self.sync_people()
        route = "/workspaces/{workspace_id}/people/contact"
        status, first = call(api.people_contact, event("POST", route, wpath(),
                                                       {"person_key": ASHA}))
        self.assertEqual(status, 200, first)
        c = first["contact"]
        self.assertEqual((c["name"], c["people_provider"], c["people_type"]),
                         ("Asha Rao", "salesforce", "contact"))
        self.assertNotIn("003000000000001AAA", json.dumps(first), "no Salesforce id to the app")
        _, again = call(api.people_contact, event("POST", route, wpath(), {"person_key": ASHA}))
        self.assertEqual(again["contact"]["id"], c["id"], "found once, never duplicated")
        status, _ = call(api.people_contact, event("POST", route, wpath(),
                                                   {"person_key": "salesforce#contact#nope"}))
        self.assertEqual(status, 404)
        self.as_user(OUTSIDER)
        status, _ = call(api.people_contact, event("POST", route, wpath(), {"person_key": ASHA}))
        self.assertEqual(status, 404)


class ObjectFilterTests(unittest.TestCase):
    def obj(self, name, relation="what", custom=False, queryable=True):
        return {"api_name": name, "label": name, "is_custom": custom,
                "queryable": queryable, "task_relation": relation}

    def test_standard_allowlist_plus_every_usable_custom_object(self):
        cat = sss.object_category
        self.assertEqual(cat(self.obj("Opportunity")), "standard")
        self.assertEqual(cat(self.obj("Contract")), "standard")
        self.assertEqual(cat(self.obj("Idea")), "", "standard but not a business object")
        self.assertEqual(cat(self.obj("AccountFeed", relation="")), "")
        self.assertEqual(cat(self.obj("Opportunity", queryable=False)), "", "no read access")
        self.assertEqual(cat(self.obj("Warranty__c", custom=True)), "custom")
        self.assertEqual(cat(self.obj("acme__Project__c", custom=True)), "custom",
                         "a managed package's object needs no allowlist entry")
        self.assertEqual(cat(self.obj("Booking__c", relation="", custom=True)), "custom",
                         "activities off: still a meeting's record, just no Tasks")
        self.assertFalse(sss.supports_tasks(self.obj("Booking__c", relation="", custom=True)))
        self.assertEqual(cat(self.obj("Invoice")), "standard")
        self.assertEqual(cat(self.obj("ApprovalSubmission")), "", "plumbing that allows activities")
        self.assertEqual(cat(self.obj("Event__e", custom=True)), "")
        self.assertEqual(cat(self.obj("MinuteX_Site_Meeting__c", custom=True)), "")
        self.assertTrue(sss.keep_in_discovery({"api_name": "Task"}))
        self.assertFalse(sss.keep_in_discovery(self.obj("Idea")))

    def test_relationship_rules(self):
        contact = sss.relation_ref(
            {"api_name": "Contact", "label": "Contact", "key_prefix": "003", "queryable": True,
             "task_relation": "who"}, "003000000000001AAA", "Asha Rao")
        opp = sss.relation_ref(
            {"api_name": "Opportunity", "label": "Opportunity", "key_prefix": "006",
             "queryable": True, "task_relation": "what"}, "006000000000001AAA", "Big Deal")
        lead = sss.relation_ref(
            {"api_name": "Lead", "label": "Lead", "key_prefix": "00Q", "queryable": True,
             "task_relation": "who"}, "00Q000000000001AAA", "Lead One")
        both = sss.combine_relations({"who": contact, "what": None}, opp)
        self.assertEqual((both["who"]["record_name"], both["what"]["record_name"]),
                         ("Asha Rao", "Big Deal"))
        self.assertEqual(sss.primary_relation(both)["object_api_name"], "Opportunity")
        with_lead = sss.combine_relations(both, lead)
        self.assertEqual((with_lead["who"]["object_api_name"], with_lead["what"]),
                         ("Lead", None), "a Lead cannot also have a What")
        back = sss.combine_relations(with_lead, opp)
        self.assertEqual((back["who"], back["what"]["object_api_name"]), (None, "Opportunity"))
        with self.assertRaises(sss.SchemaError):
            sss.relation_ref({"api_name": "Opportunity", "key_prefix": "006", "queryable": True,
                              "task_relation": "what"}, "001000000000001AAA")


class RelationHarness(SFHarness):
    R = W + "/meeting-relation"

    def setUp(self):
        super().setUp()
        self.ready()

    def tag(self, person_key=ASHA, speaker="0"):
        status, body = call(api.site_participant_tag, event(
            "PUT", "/recordings/site-participants/{key+}", {"key": KEY},
            {"speaker_id": speaker, "person_key": person_key}))
        self.assertEqual(status, 200, body)

    def get_rel(self):
        status, body = call(api.site_sf_meeting_relation_get, event(
            "GET", self.R, wpath(), qs={"meeting_key": KEY}))
        self.assertEqual(status, 200, body)
        return body

    def put_rel(self, **body):
        return call(api.site_sf_meeting_relation_put, event(
            "PUT", self.R, wpath(), {"meeting_key": KEY, **body}))



class MeetingRelationTests(RelationHarness):
    def test_tagging_a_salesforce_contact_relates_the_meeting_automatically(self):
        self.assertIsNone(self.get_rel()["relation"]["who"])
        self.tag()
        body = self.get_rel()
        rel = body["relation"]
        self.assertEqual((rel["who"]["name"], rel["who"]["object_label"], rel["automatic"]),
                         ("Asha Rao", "Contact", True))
        self.assertEqual([c["name"] for c in rel["candidates"]], ["Asha Rao"])
        # person_key is the opaque handle the app sends back to choose a
        # person; nothing else may carry a record id or an API name.
        raw = json.dumps(body).replace(ASHA, "<person_key>")
        for secret in ("003000000000001AAA", "object_api_name", "record_id", "external_id"):
            self.assertNotIn(secret, raw, "no ids or API names reach the app")

    def test_change_salesforce_record_to_another_object(self):
        self.tag()
        status, body = self.put_rel(object_api_name="Opportunity",
                                    record_id="006000000000001AAA", record_name="Big Deal")
        self.assertEqual(status, 200, body)
        rel = body["relation"]
        self.assertEqual((rel["who"]["name"], rel["what"]["name"]), ("Asha Rao", "Big Deal"))
        # A custom object works the same way.
        _, body = self.put_rel(object_api_name="Site_Visit__c",
                               record_id="a01000000000125AAA", record_name="SV-00125")
        self.assertEqual(body["relation"]["what"]["object_label"], "Site Visit")
        # Refused: an object never offered, a record of the wrong object.
        self.assertEqual(self.put_rel(object_api_name="Idea", record_id="087000000000001AAA")[1]
                         ["code"], "invalid_object")
        self.assertEqual(self.put_rel(object_api_name="Opportunity",
                                      record_id="001000000000001AAA")[1]["code"],
                         "invalid_record")
        # A Lead replaces the Who and drops the What.
        _, body = self.put_rel(object_api_name="Lead", record_id="00Q000000000001AAA",
                               record_name="Lead One")
        self.assertEqual((body["relation"]["who"]["name"], body["relation"]["what"]),
                         ("Lead One", None))
        # Reset: back to the tagged Contact, automatically.
        _, body = self.put_rel(reset="all")
        self.assertEqual((body["relation"]["who"]["name"], body["relation"]["automatic"]),
                         ("Asha Rao", True))

    def test_choosing_a_synced_contact_directly(self):
        _, body = self.put_rel(person_key=ASHA)
        self.assertEqual((body["relation"]["who"]["name"], body["relation"]["automatic"]),
                         ("Asha Rao", False))
        status, body = self.put_rel(person_key=self.person(fso.REP_USER))
        self.assertEqual((status, body["code"]), (400, "invalid_person"),
                         "a User is an assignee, not who a meeting is with")

    def test_only_an_editor_changes_it_and_other_orgs_choices_are_ignored(self):
        self.as_user(MANAGER)  # can READ a colleague's meeting, not edit it
        self.assertEqual(self.get_rel()["connected"], True)
        status, _ = self.put_rel(person_key=ASHA)
        self.assertEqual(status, 404)
        self.as_user(OWNER)
        self.put_rel(object_api_name="Opportunity", record_id="006000000000001AAA",
                     record_name="Big Deal")
        self.t["recordings"].update_item(
            Key={"audio_s3_key": KEY}, UpdateExpression="SET salesforce_relation.org_id = :o",
            ExpressionAttributeValues={":o": OTHER_SF_ORG})
        self.assertIsNone(self.get_rel()["relation"]["what"])

    def test_the_meeting_is_delivered_with_its_relationship(self):
        self.tag()
        self.put_rel(object_api_name="Account", record_id="001000000000001AAA",
                     record_name="Acme Mumbai")
        self.push()
        self.drain()
        payload = next(iter(self.receiver.meetings.values()))["payload"]
        self.assertEqual(payload["relation"], {
            "who": {"object_type": "Contact", "record_id": "003000000000001AAA",
                    "name": "Asha Rao"},
            "what": {"object_type": "Account", "record_id": "001000000000001AAA",
                     "name": "Acme Mumbai"}})


class TaskInheritanceTests(RelationHarness):
    def mx_task(self, task_id="t-ai-1", **over):
        row = {"task_id": task_id, "owner_user_id": OWNER, "source_recording_id": KEY,
               "title": "Send the revised quotation", "status": "Open", "priority": "High",
               "due_date_normalized": "2026-09-30", "created_at": "2026-09-21T13:40:00Z"}
        row.update(over)
        self.t["tasks"].put_item(Item=row)

    def push_task(self, task_id="t-ai-1", **body):
        return call(api.site_sf_task_create, event("POST", W + "/tasks", wpath(), {
            "minutex_task_id": task_id, "assignee_person_key": self.person(fso.REP_USER),
            **body}))

    def sf_task(self, ref):
        return self.org.tasks[self.task_row(ref)["salesforce_task_id"]]

    def test_tasks_wait_for_the_meeting_to_be_pushed(self):
        self.tag()
        self.mx_task()
        status, body = self.push_task()
        self.assertEqual((status, body["code"]), (409, "meeting_not_pushed"))
        self.assertEqual(self.org.tasks, {})

    def test_a_task_is_related_to_the_meeting_record_and_the_tagged_contact(self):
        self.tag()
        record = self.push_meeting()
        self.mx_task()
        status, body = self.push_task()
        self.assertEqual(status, 202, body)
        self.assertEqual(body["task"]["related_to"], [
            {"slot": "who", "object_label": "Contact", "name": "Asha Rao"},
            {"slot": "what", "object_label": "MinuteX Meeting", "name": "Client Discussion"}])
        self.drain()
        sf = self.sf_task(body["task"]["task_ref"])
        self.assertEqual((sf["who_id"], sf["what_id"], sf["owner_id"]),
                         ("003000000000001AAA", record, fso.REP_USER))
        _, env = [c for c in self.org.calls if c[1]["action"] == "create_task"][-1]
        self.assertEqual(env["contract_version"], "1.2")
        self.assertEqual(env["payload"]["task"]["related_object_type"], "MinuteX_Site_Meeting__c")
        self.assertEqual(len(env["payload"]["task"]["related_records"]), 2)

    def test_a_business_record_stays_on_the_meeting_record(self):
        self.tag()
        self.put_rel(object_api_name="Opportunity", record_id="006000000000001AAA",
                     record_name="Big Deal")
        record = self.push_meeting()
        payload = next(iter(self.receiver.meetings.values()))["payload"]
        self.assertEqual(payload["relation"]["what"]["object_type"], "Opportunity")
        self.mx_task()
        _, body = self.push_task()
        self.drain()
        sf = self.sf_task(body["task"]["task_ref"])
        self.assertEqual((sf["who_id"], sf["what_id"]), ("003000000000001AAA", record))

    def test_without_a_tagged_contact_the_task_is_on_the_meeting_record(self):
        record = self.push_meeting()
        self.mx_task()
        _, body = self.push_task()
        self.drain()
        sf = self.sf_task(body["task"]["task_ref"])
        self.assertEqual((sf.get("who_id"), sf["what_id"]), (None, record))

    def test_change_related_record_for_a_task_keeps_the_other_side(self):
        self.tag()
        self.push_meeting()
        self.mx_task()
        _, body = self.push_task(object_api_name="Account", record_id="001000000000002AAA",
                                 record_name="Beta Pune")
        self.drain()
        sf = self.sf_task(body["task"]["task_ref"])
        self.assertEqual((sf["who_id"], sf["what_id"]),
                         ("003000000000001AAA", "001000000000002AAA"))

    def test_the_meeting_status_says_where_the_meeting_is(self):
        self.tag()
        self.put_rel(object_api_name="Account", record_id="001000000000001AAA",
                     record_name="Acme Mumbai")
        status, body = self.status()
        self.assertIsNone(body["meeting_record"], "not pushed yet")
        self.push_meeting()
        _, body = self.status()
        rec = body["meeting_record"]
        self.assertEqual((rec["object_label"], rec["name"]), ("MinuteX Meeting", "Client Discussion"))
        self.assertEqual([r["name"] for r in rec["related_to"]], ["Asha Rao", "Acme Mumbai"])
        raw = json.dumps(rec)
        for secret in ("a0X", "003000000000001AAA", "001000000000001AAA"):
            self.assertNotIn(secret, raw, "no Salesforce ids in what the app shows")
        # The relationship route carries it too (read access is enough).
        self.assertEqual(self.get_rel()["meeting_record"]["name"], "Client Discussion")

    def test_assignee_must_be_an_active_salesforce_user(self):
        self.tag()
        self.push_meeting()
        self.mx_task()
        for key in ("", ASHA, self.person(fso.INACTIVE_USER)):
            status, body = self.push_task(assignee_person_key=key)
            self.assertEqual(status, 400, key)
            self.assertIn(body["code"], ("assignee_required", "invalid_assignee"))

    def test_a_meeting_about_a_record_without_activities_still_gets_tasks(self):
        # The meeting is about a Booking (activities off in Salesforce): that
        # stays on the meeting record; Tasks go to the meeting record itself.
        status, body = self.put_rel(object_api_name="Booking__c", record_id="a02000000000001AAA",
                                    record_name="BK-1")
        self.assertEqual(status, 200, body)
        self.assertFalse(body["relation"]["what"]["tasks_supported"])
        record = self.push_meeting()
        payload = next(iter(self.receiver.meetings.values()))["payload"]
        self.assertEqual(payload["relation"]["what"]["object_type"], "Booking__c")
        self.mx_task()
        _, body = self.push_task()
        self.drain()
        self.assertEqual(self.sf_task(body["task"]["task_ref"])["what_id"], record)

    def test_a_1_1_receiver_still_gets_the_primary_record(self):
        self.org.supports_related_records = False
        self.tag()
        record = self.push_meeting()
        self.mx_task()
        _, body = self.push_task()
        self.drain()
        sf = self.sf_task(body["task"]["task_ref"])
        self.assertEqual((sf.get("who_id"), sf["what_id"]), (None, record))

    def test_sync_bookkeeping_links_both_tasks_and_repush_updates_in_place(self):
        self.tag()
        record = self.push_meeting()
        self.mx_task()
        _, body = self.push_task()
        ref = body["task"]["task_ref"]
        self.drain()
        row = self.task_row(ref)
        self.assertEqual((row["origin"], row["synced_version"]), ("minutex", 1))
        self.assertEqual(row["synced_snapshot"]["who_record_id"], "003000000000001AAA")
        self.assertEqual(row["synced_snapshot"]["subject"], "Send the revised quotation")
        self.assertTrue(row["sf_last_modified_at"])
        link = self.t["tasks"].get_item(Key={"task_id": "t-ai-1"})["Item"]["salesforce_sync"]
        self.assertEqual((link["salesforce_task_id"], link["task_ref"], link["synced_version"]),
                         (row["salesforce_task_id"], ref, 1))
        # Edited in MinuteX; pushing again updates the SAME Salesforce Task,
        # still on the meeting record.
        self.t["tasks"].update_item(Key={"task_id": "t-ai-1"}, UpdateExpression="SET title = :t",
                                    ExpressionAttributeValues={":t": "Send the final quotation"})
        status, again = self.push_task()
        self.assertEqual((status, again["updated"]), (202, True))
        self.drain()
        self.assertEqual(len(self.org.tasks), 1)
        sf = self.sf_task(ref)
        self.assertEqual((sf["subject"], sf["who_id"], sf["what_id"]),
                         ("Send the final quotation", "003000000000001AAA", record))
        self.assertEqual(self.task_row(ref)["synced_version"], 2)
        link = self.t["tasks"].get_item(Key={"task_id": "t-ai-1"})["Item"]["salesforce_sync"]
        self.assertEqual(link["synced_version"], 2)


# ===========================================================================
# The object catalog: broad discovery, recommended + enabled, search,
# workspace preferences, separate meeting / Task capabilities.
# ===========================================================================
class ObjectCatalogTests(SFHarness):
    PREFS = W + "/object-preferences"

    def prefs(self, as_user=OWNER, **body):
        self.as_user(as_user)
        out = call(api.site_sf_object_prefs, event("PUT", self.PREFS, wpath(), body))
        self.as_user(OWNER)
        return out

    def test_more_than_a_thousand_objects_are_discovered_without_truncation(self):
        extra = {f"Custom_{i:04d}__c": (f"Custom {i:04d}", f"Customs {i:04d}", f"b{i:02d}"[:3],
                                        True, "what" if i % 2 else "")
                 for i in range(1200)}
        extra.update({f"Acct{i}Share": ("x", "x", "0Z0", False, "") for i in range(3)})
        with mock.patch.dict(fso.OBJECTS, extra), \
                mock.patch.dict(fso.PERMS[fso.ADMIN_USER], {n: fso.FULL for n in extra}):
            self.org.perms[fso.ADMIN_USER].update({n: fso.FULL for n in extra})
            self.connect()
            job = self.discover()
            self.assertEqual(job["status"], "SYNCED")
            _, body = self.objects(limit=1000)
            self.assertEqual(body["catalog_size"], len(fso.OFFERED) + 1200, "nothing truncated")
            self.assertTrue(body["truncated"], "the page limit is reported, not silent")
            _, found = self.objects(q="custom 1199")
            self.assertEqual([o["label"] for o in found["objects"]], ["Custom 1199"])
            _, share = self.objects(q="acct0share")
            self.assertEqual(share["objects"], [], "sharing companions are never catalogued")
            # Hundreds of catalogued objects, but fields only for a handful.
            self.assertLess(job["result"]["counts"]["fields_objects"], 10)
            self.assertEqual(job["result"]["counts"]["objects_task_capable"],
                             600 + len([n for n in fso.OFFERED if fso.OBJECTS[n][4]]))

    def test_unqueryable_and_system_objects_are_excluded(self):
        self.org.integration_user = fso.LIMITED_USER   # cannot read Opportunity, Contact
        self.connect()
        self.discover()
        names = {o["api_name"] for o in self.objects()[1]["objects"]}
        self.assertNotIn("Opportunity", names)
        for kept in ("Account", "Lead", "Site_Visit__c"):
            self.assertIn(kept, names)
        for bad in ({"api_name": "ApexClass", "queryable": True},
                    {"api_name": "AccountHistory", "queryable": True},
                    {"api_name": "Rule__mdt", "queryable": True, "is_custom": True},
                    {"api_name": "Order_Event__e", "queryable": True, "is_custom": True},
                    {"api_name": "MinuteX_Meeting__c", "queryable": True, "is_custom": True},
                    {"api_name": "Property__c", "queryable": False, "is_custom": True},
                    {"api_name": "BatchApexErrorEvent", "queryable": True},
                    {"api_name": "LoginAnomalyEventStore", "queryable": True},
                    {"api_name": "Warehouse", "queryable": True, "mru_enabled": False}):
            self.assertTrue(sss.catalog_exclusion(bad), bad)
        for good in ({"api_name": "Property__c", "queryable": True, "is_custom": True},
                     {"api_name": "acme__Deal__c", "queryable": True, "is_custom": True},
                     {"api_name": "Report_Card__c", "queryable": True, "is_custom": True},
                     {"api_name": "Warehouse", "queryable": True, "mru_enabled": True},
                     {"api_name": "Building__c", "queryable": True, "is_custom": True,
                      "mru_enabled": False},
                     {"api_name": "Asset", "queryable": True}):
            self.assertEqual(sss.catalog_exclusion(good), "", good)

    def test_standard_machinery_is_excluded_by_the_receivers_signal(self):
        self.org.machinery = {"Case"}   # pretend Case were platform machinery
        self.connect()
        self.discover()
        names = {o["api_name"] for o in self.objects()[1]["objects"]}
        self.assertNotIn("Case", names)
        self.assertIn("Account", names)
        self.assertIn("Booking__c", names, "custom objects never depend on it")

    def test_the_short_list_is_recommended_plus_enabled_and_the_rest_is_search(self):
        self.connect()
        self.discover()
        _, visible = self.objects(scope="visible")
        self.assertEqual([o["api_name"] for o in visible["objects"]],
                         ["Account", "Contact", "Lead", "Opportunity", "Case"])
        self.assertTrue(all(o["recommended"] for o in visible["objects"]))
        _, found = self.objects(q="site")
        self.assertEqual([o["label"] for o in found["objects"]], ["Site Visit"])
        self.assertFalse(found["objects"][0]["enabled"])
        # Enable it: it joins the short list, for everyone, across rediscovery.
        status, body = self.prefs(as_user=MEMBER, enable=["Site_Visit__c"])
        self.assertEqual(status, 200, body)
        self.discover()
        _, visible = self.objects(scope="visible")
        self.assertEqual([o["api_name"] for o in visible["objects"]][-1], "Site_Visit__c")
        self.assertTrue(visible["objects"][-1]["enabled"])
        # Unknown objects are refused; removing needs a manager.
        self.assertEqual(self.prefs(enable=["Nope__c"])[1]["code"], "unknown_object")
        self.assertEqual(self.prefs(as_user=MEMBER, disable=["Site_Visit__c"])[0], 403)
        self.assertEqual(self.prefs(disable=["Site_Visit__c"])[1]["enabled"], [])

    def test_disconnect_keeps_preferences_and_another_org_ignores_them(self):
        self.connect()
        self.discover()
        self.prefs(enable=["Site_Visit__c"])
        call(api.site_sf_disconnect, event("DELETE", W, wpath()))
        self.connect()
        self.discover()
        self.assertIn("Site_Visit__c", [o["api_name"] for o in self.objects(scope="visible")[1]["objects"]])
        self.org.org_id = OTHER_SF_ORG
        self.connect(allow_org_change=True)
        self.discover()
        self.assertNotIn("Site_Visit__c",
                         [o["api_name"] for o in self.objects(scope="visible")[1]["objects"]])

    def test_task_lists_offer_only_task_capable_objects_meeting_lists_offer_all(self):
        self.connect()
        self.discover()
        meeting = {o["api_name"] for o in self.objects(capability="meeting")[1]["objects"]}
        task = {o["api_name"] for o in self.objects(capability="task")[1]["objects"]}
        self.assertIn("Booking__c", meeting)
        self.assertNotIn("Booking__c", task)
        self.assertIn("Site_Visit__c", task)


class CatalogRelationTests(RelationHarness):
    def test_an_object_without_tasks_is_linked_to_a_meeting_and_remembered(self):
        status, body = self.put_rel(object_api_name="Booking__c",
                                    record_id="a02000000000001AAA", record_name="BK-1")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["relation"]["what"]["object_label"], "Booking")
        _, visible = self.objects(scope="visible")
        self.assertIn("Booking__c", [o["api_name"] for o in visible["objects"]],
                      "using an object enables it for the workspace")
        # A Task override cannot use it.
        self.t["tasks"].put_item(Item={"task_id": "t-1", "owner_user_id": OWNER,
                                       "source_recording_id": KEY, "title": "Call back",
                                       "status": "Open", "created_at": "2026-09-21T13:40:00Z"})
        self.push_meeting()
        status, body = call(api.site_sf_task_create, event("POST", W + "/tasks", wpath(), {
            "minutex_task_id": "t-1", "assignee_person_key": self.person(fso.REP_USER),
            "object_api_name": "Booking__c", "record_id": "a02000000000001AAA"}))
        self.assertEqual((status, body["code"]), (400, "object_not_task_relatable"))

    def test_contact_tagging_and_task_inheritance_still_work(self):
        self.tag()
        self.assertEqual(self.get_rel()["relation"]["who"]["name"], "Asha Rao")
        record = self.push_meeting()
        self.t["tasks"].put_item(Item={"task_id": "t-2", "owner_user_id": OWNER,
                                       "source_recording_id": KEY, "title": "Send deck",
                                       "status": "Open", "created_at": "2026-09-21T13:40:00Z"})
        _, body = call(api.site_sf_task_create, event("POST", W + "/tasks", wpath(), {
            "minutex_task_id": "t-2", "assignee_person_key": self.person(fso.REP_USER)}))
        self.drain()
        sf = self.org.tasks[self.task_row(body["task"]["task_ref"])["salesforce_task_id"]]
        self.assertEqual((sf["who_id"], sf["what_id"]), ("003000000000001AAA", record))


if __name__ == "__main__":
    unittest.main()

