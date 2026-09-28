"""fake_salesforce_org.py — an in-memory Salesforce org behind the MinuteX
Site receiver, used as a TRANSPORT by the real SiteDeliveryClient (only the
network is replaced).

It mirrors the Apex receiver's org actions (MinuteXSiteSchema /
MinuteXSiteRecords / MinuteXSiteTasks), which MinuteXSiteSalesforceTest pins
in a real org: every org action works within the CONFIGURED INTEGRATION
USER's permissions (`integration_user`; None -> 409
integration_user_not_configured), Task relation via WhatId/WhoId, record
visibility, active assignee, picklists, idempotency replay. Every response
carries `org_id`, as the real receiver's does.

AUTH is the Site's pluggable mode: `secret=None` accepts unsigned requests
(auth_mode none); a secret makes it verify the HMAC headers.

FAULTS (one per request): "timeout", "timeout_after_commit",
("status", 503), ("raw", status, bytes) (a platform-shaped response, e.g.
Salesforce's own 404 list body).
"""
import hashlib
import hmac
import json

import site_delivery_client as sdc

ORG_ID = "00D5g00000ABCDEEAY"
ADMIN_USER = "005000000000001AAA"
LIMITED_USER = "005000000000002AAA"
REP_USER = "005000000000003AAA"
INACTIVE_USER = "005000000000004AAA"

OBJECTS = {
    "Account": ("Account", "Accounts", "001", False, "what"),
    "Contact": ("Contact", "Contacts", "003", False, "who"),
    "Lead": ("Lead", "Leads", "00Q", False, "who"),
    "Opportunity": ("Opportunity", "Opportunities", "006", False, "what"),
    "Task": ("Task", "Tasks", "00T", False, ""),
    "Site_Visit__c": ("Site Visit", "Site Visits", "a01", True, "what"),
    "Booking__c": ("Booking", "Bookings", "a02", True, ""),   # activities off
    "User": ("User", "Users", "005", False, ""),
    "Case": ("Case", "Cases", "500", False, "what"),
    # Allow activities but are never OFFERED: a standard object outside the
    # business list, and MinuteX's own receiver object.
    "Idea": ("Idea", "Ideas", "087", False, "what"),
    "MinuteX_Site_Meeting__c": ("MinuteX Site Meeting", "MinuteX Site Meetings", "a03",
                                True, "what"),
}
# What a person is offered from the objects above (site_salesforce_schema):
# standard business objects in business order, then EVERY readable custom
# object by label — Booking__c too, although Tasks cannot be attached to it.
OFFERED = ["Account", "Contact", "Lead", "Opportunity", "Case", "Booking__c", "Site_Visit__c"]
FULL = {"read": True, "create": True, "edit": True, "delete": True}
PERMS = {
    ADMIN_USER: {name: FULL for name in OBJECTS},
    LIMITED_USER: {
        "Account": {"read": True}, "Lead": {"read": True}, "Site_Visit__c": {"read": True},
        "User": {"read": True}, "Task": {"read": True},
    },
}


def _f(name, label, ftype="string", *, createable=True, updateable=True, required=False,
       name_field=False, custom=False, ref=(), picklist=None, length=255):
    out = {"api_name": name, "label": label, "type": ftype, "createable": createable,
           "updateable": updateable, "nillable": not required, "defaulted_on_create": False,
           "required": required, "is_custom": custom, "name_field": name_field,
           "filterable": True, "length": length, "reference_to": list(ref),
           "relationship_name": None}
    if picklist is not None:
        out["picklist_values"] = [{"value": v, "label": v, "default": i == 0}
                                  for i, v in enumerate(picklist)]
    return out


FIELDS = {
    "Account": [_f("Name", "Account Name", required=True, name_field=True),
                _f("OwnerId", "Owner", "reference", ref=["User"])],
    "Contact": [_f("Name", "Full Name", createable=False, updateable=False, name_field=True),
                _f("Email", "Email", "email")],
    "Lead": [_f("Name", "Full Name", createable=False, updateable=False, name_field=True),
             _f("Company", "Company", required=True)],
    "Opportunity": [_f("Name", "Name", required=True, name_field=True)],
    "Site_Visit__c": [
        _f("Name", "Site Visit Number", "string", createable=False, updateable=False,
           name_field=True),
        _f("Visit_Date__c", "Visit Date", "date", custom=True),
        _f("Customer__c", "Customer", "reference", custom=True, ref=["Account"]),
        _f("Assigned_User__c", "Assigned User", "reference", custom=True, ref=["User"]),
        _f("Status__c", "Status", "picklist", custom=True, picklist=["Planned", "Done"]),
    ],
    "Booking__c": [_f("Name", "Booking Number", name_field=True)],
    "Task": [
        _f("Subject", "Subject", required=True),
        _f("Description", "Description", "textarea", length=32000),
        _f("ActivityDate", "Due Date", "date"),
        _f("Status", "Status", "picklist", picklist=["Not Started", "In Progress", "Completed"]),
        _f("Priority", "Priority", "picklist", picklist=["Normal", "High", "Low"]),
        _f("OwnerId", "Assigned To", "reference", ref=["User"]),
        _f("WhatId", "Related To", "reference",
           ref=[n for n, o in OBJECTS.items() if o[4] == "what"]),
        _f("WhoId", "Name", "reference", ref=["Contact", "Lead"]),
        _f("CallDisposition", "Call Result"),
        _f("Type", "Type", "picklist", picklist=["Call", "Email", "Meeting"]),
    ],
    "User": [_f("Name", "Full Name", createable=False, updateable=False, name_field=True)],
}


class FakeSalesforceOrg:
    def __init__(self, org_id=ORG_ID):
        self.org_id = org_id
        self.integration_user = ADMIN_USER
        self.secret = None
        self.users = {
            ADMIN_USER: {"name": "Integration Admin", "email": "admin@acme.test", "active": True},
            LIMITED_USER: {"name": "Limited Integration", "email": "lim@acme.test", "active": True},
            REP_USER: {"name": "Shubham Patil", "email": "shubham@acme.test", "active": True},
            INACTIVE_USER: {"name": "Former Rep", "email": "former@acme.test", "active": False},
        }
        self.records = {
            "Account": {"001000000000001AAA": "Acme Mumbai", "001000000000002AAA": "Beta Pune"},
            "Case": {"500000000000001AAA": "Case 0001"},
            "Contact": {"003000000000001AAA": "Asha Rao"},
            "Lead": {"00Q000000000001AAA": "Lead One"},
            "Opportunity": {"006000000000001AAA": "Big Deal"},
            "Site_Visit__c": {"a01000000000125AAA": "SV-00125", "a01000000000126AAA": "SV-00126",
                              "a01000000000131AAA": "SV-00131"},
            "Booking__c": {"a02000000000001AAA": "BK-1"},
        }
        self.tasks = {}
        self.requests = {}
        self.faults = []
        self.calls = []
        self.perms = {u: dict(p) for u, p in PERMS.items()}
        # False = a 1.1 receiver, which ignores related_records.
        self.supports_related_records = True
        # Standard objects reported as platform machinery (mru_enabled False).
        self.machinery = set()
        self.clock = 0
        # Extra synthetic Contacts, to exercise people_sync paging.
        self.extra_contacts = 0

    # -- helpers --------------------------------------------------------------
    def can(self, user, obj, op):
        return bool(self.perms.get(user, {}).get(obj, {}).get(op))

    def _json(self, status, body):
        return status, json.dumps(body).encode("utf-8")

    def _err(self, status, code, message, field=""):
        return self._json(status, {"ok": False, "org_id": self.org_id, "error": {
            "code": code, "message": message, "field": field}})

    def _ok(self, action, request_id, result, duplicate=False):
        return self._json(200, {"ok": True, "action": action, "request_id": request_id,
                                "duplicate": duplicate, "receiver_version": "1.2.0",
                                "org_id": self.org_id, "result": result})

    # -- transport ------------------------------------------------------------
    def post(self, url, body, headers, timeout):
        fault = self.faults.pop(0) if self.faults else None
        if fault == "timeout":
            raise sdc.TransportTimeout("timed out")
        if isinstance(fault, tuple) and fault[0] == "status":
            return self._err(fault[1], "injected", f"injected {fault[1]}")
        if isinstance(fault, tuple) and fault[0] == "raw":
            return fault[1], fault[2]
        raw = body.decode("utf-8")
        if self.secret is not None:
            ts = headers.get("X-MinuteX-Timestamp", "")
            want = "v1=" + hmac.new(self.secret.encode(), f"{ts}.{raw}".encode(),
                                    hashlib.sha256).hexdigest()
            if not hmac.compare_digest(want, headers.get("X-MinuteX-Signature", "")):
                return self._err(401, "auth_failed", "Missing or invalid request signature.")
        env = json.loads(raw)
        self.calls.append((self.integration_user, env))
        out = self._handle(env)
        if fault == "timeout_after_commit":
            raise sdc.TransportTimeout("timed out after commit")
        return out

    # -- receiver ----------------------------------------------------------------
    ORG_ACTIONS = ("org_info", "object_schema", "record_search", "create_task", "update_task")

    def _handle(self, env):
        action, rid = env.get("action"), env.get("request_id", "")
        payload = env.get("payload") or {}
        user = self.integration_user
        if action == "health":
            return self._ok(action, rid, {"status": "ok", "org_id": self.org_id,
                                          "integration_user_configured": bool(user)})
        if action == "people_sync":
            return self._people(rid, payload)
        if action not in self.ORG_ACTIONS:
            return self._err(400, "unsupported_action", "no")
        if not user:
            return self._err(409, "integration_user_not_configured",
                             "Set MinuteX Site Settings > Integration Username.")
        if action == "org_info":
            return self._ok(action, rid, {
                "org": {"org_id": self.org_id, "org_name": "Acme Realty",
                        "is_sandbox": False, "edition": "Enterprise Edition"},
                "user": {"id": user, "username": f"{user}@acme.test",
                         "name": self.users[user]["name"]},
                "task": {"accessible": True,
                         "createable": self.can(user, "Task", "create"),
                         "updateable": self.can(user, "Task", "edit"),
                         "what_objects": sorted(n for n, o in OBJECTS.items() if o[4] == "what"),
                         "who_objects": ["Contact", "Lead"]}})
        if action == "object_schema":
            return self._schema(user, rid, payload)
        if action == "record_search":
            return self._search(user, rid, payload)
        return self._task(user, rid, env, action)

    def _schema(self, user, rid, payload):
        if payload.get("mode") == "objects":
            names = sorted(OBJECTS, key=str.lower)
            cursor = (payload.get("cursor") or "").lower()
            size = int(payload.get("page_size") or 200)
            todo = [n for n in names if not cursor or n.lower() > cursor]
            page, more = todo[:size], len(todo) > size
            objs, skipped = [], 0
            for n in page:
                if not self.can(user, n, "read"):
                    skipped += 1
                    continue
                label, plural, prefix, custom, _rel = OBJECTS[n]
                objs.append({"api_name": n, "label": label, "label_plural": plural,
                             "key_prefix": prefix, "is_custom": custom, "queryable": True,
                             "mru_enabled": n not in self.machinery,
                             "searchable": True, "createable": self.can(user, n, "create"),
                             "updateable": self.can(user, n, "edit"),
                             "deletable": self.can(user, n, "delete")})
            return self._ok("object_schema", rid, {
                "objects": objs, "next_cursor": page[-1].lower() if more else "",
                "done": not more, "skipped_inaccessible": skipped, "skipped_excluded": 0})
        described, unknown, inaccessible = [], [], []
        for n in payload.get("objects") or []:
            canonical = next((o for o in OBJECTS if o.lower() == str(n).lower()), None)
            if canonical is None:
                unknown.append(n)
            elif not self.can(user, canonical, "read"):
                inaccessible.append(canonical)
            else:
                fields = [dict(f, createable=f["createable"] and self.can(user, canonical, "create"),
                               updateable=f["updateable"] and self.can(user, canonical, "edit"))
                          for f in FIELDS.get(canonical, [])]
                described.append({"api_name": canonical, "fields": fields,
                                  "inaccessible_field_count": 0})
        return self._ok("object_schema", rid, {"objects": described, "unknown": unknown,
                                               "inaccessible": inaccessible})

    def _search(self, user, rid, payload):
        name = next((o for o in OBJECTS if o.lower() == str(payload.get("object")).lower()), None)
        if name is None:
            return self._err(422, "invalid_object", "no such object", "payload.object")
        if not self.can(user, name, "read"):
            return self._err(403, "permission_denied",
                             f"Salesforce permissions do not allow reading {OBJECTS[name][1]}.")
        q = str(payload.get("query") or "").lower()
        recs = [{"id": i, "name": n} for i, n in sorted(self.records.get(name, {}).items(),
                                                          key=lambda kv: kv[1])
                if not q or q in n.lower() or q == i.lower()]
        return self._ok("record_search", rid, {"object": name, "name_field": "Name",
                                               "records": recs[:int(payload.get("limit") or 20)]})

    def _task(self, user, rid, env, action):
        key = env.get("idempotency_key") or ""
        if not key:
            return self._err(400, "invalid_request", "idempotency_key required")
        if key in self.requests:
            return self._ok(action, rid, dict(self.requests[key]), duplicate=True)
        spec = (env.get("payload") or {}).get("task") or {}
        creating = action == "create_task"
        if not self.can(user, "Task", "create" if creating else "edit"):
            return self._err(403, "permission_denied",
                             "Salesforce permissions do not allow Task creation for this "
                             "object/user." if creating else
                             "Salesforce permissions do not allow Task updates for this user.")
        if creating:
            task = {}
        else:
            task = self.tasks.get(spec.get("salesforce_task_id"))
            if task is None:
                return self._err(404, "task_not_found", "no such task")
        pairs = None
        if self.supports_related_records and isinstance(spec.get("related_records"), list):
            pairs = [(r.get("object_type"), r.get("record_id")) for r in spec["related_records"]]
            # 1.2 semantics: related_records IS the relationship (replace).
            task.pop("what_id", None)
            task.pop("who_id", None)
        elif spec.get("related_object_type") or spec.get("related_record_id"):
            pairs = [(spec.get("related_object_type"), spec.get("related_record_id"))]
        if pairs:
            slots = set()
            for rel_type, rel_id in pairs:
                if rel_type not in OBJECTS:
                    return self._err(422, "invalid_object", "no such object")
                relation = OBJECTS[rel_type][4]
                if not relation:
                    return self._err(422, "object_not_task_relatable", "activities off")
                if relation in slots:
                    return self._err(422, "invalid_relationship", "two records for one field")
                slots.add(relation)
                if not self.can(user, rel_type, "read") \
                        or rel_id not in self.records.get(rel_type, {}):
                    return self._err(422, "invalid_record", "record not found or not visible")
                task["what_id" if relation == "what" else "who_id"] = rel_id
                task["related_object_type"] = rel_type
            if task.get("what_id") and str(task.get("who_id", "")).startswith("00Q"):
                return self._err(422, "invalid_relationship", "a Lead cannot have a What")
        elif creating and spec.get("record_required") is not False:
            return self._err(422, "record_required", "record required")
        for k in ("subject", "description", "due_date"):
            if k in spec:
                task[k] = spec[k]
        for k, allowed in (("status", ["Not Started", "In Progress", "Completed"]),
                           ("priority", ["Normal", "High", "Low"])):
            if spec.get(k):
                if spec[k] not in allowed:
                    return self._err(422, f"invalid_{k}", "bad picklist")
                task[k] = spec[k]
        if spec.get("assigned_user_id"):
            u = self.users.get(spec["assigned_user_id"])
            if not u or not u["active"]:
                return self._err(422, "invalid_assignee", "not an active user")
            task["owner_id"] = spec["assigned_user_id"]
        elif creating:
            task["owner_id"] = user
        for k, v in (spec.get("extra_fields") or {}).items():
            if k not in ("CallDisposition", "Type"):
                return self._err(422, "invalid_field", f"no field {k}")
            task[k] = v
        if creating:
            if not task.get("subject"):
                return self._err(422, "validation_failed", "subject required")
            task_id = f"00T{len(self.tasks) + 1:015d}"
            task["id"] = task_id
            task["created_by"] = user
            self.tasks[task_id] = task
        self.clock += 1
        task["last_modified_at"] = f"2026-09-25T10:00:{self.clock:02d}.000Z"
        result = {"operation": "created" if creating else "updated", "task_id": task["id"],
                  "last_modified_at": task["last_modified_at"]}
        self.requests[key] = result
        return self._ok(action, rid, dict(result))

    def _people(self, rid, payload):
        # Through the Site the receiver reads Contacts/Users in system mode,
        # one page at a time: Contacts by Id, then Users by Id, with a
        # "contact:<id>" / "user:<id>" cursor (MinuteXSitePeople).
        contacts = dict(self.records["Contact"])
        for i in range(self.extra_contacts):
            contacts[f"003{900000000000000 + i:015d}"] = f"Bulk Contact {i:04d}"
        rows = [("contact", i, {"provider": "salesforce", "type": "contact", "external_id": i,
                                "name": n, "email": f"{n.split()[0].lower()}{k}@client.test",
                                "status": "active"})
                for k, (i, n) in enumerate(sorted(contacts.items()))]
        rows += [("user", i, {"provider": "salesforce", "type": "user", "external_id": i,
                              "name": u["name"], "email": u["email"],
                              "status": "active" if u["active"] else "inactive"})
                 for i, u in sorted(self.users.items())]
        size = max(1, min(500, int(payload.get("page_size") or 200)))
        cursor = str(payload.get("cursor") or "")
        start = 0
        if cursor:
            phase, _, after = cursor.partition(":")
            start = next((n + 1 for n, (p, i, _r) in enumerate(rows)
                          if p == phase and i == after), len(rows))
        page = rows[start:start + size]
        more = start + size < len(rows)
        return self._ok("people_sync", rid, {
            "people": [r for _p, _i, r in page], "count": len(page),
            "next_cursor": f"{page[-1][0]}:{page[-1][1]}" if more and page else "",
            "done": not more})
