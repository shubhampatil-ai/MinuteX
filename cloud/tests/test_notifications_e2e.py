#!/usr/bin/env python3
"""test_notifications_e2e.py — the real end-to-end in-app flows.

WHY THIS EXISTS ALONGSIDE test_notifications.py. That file tests the engine's
UNITS — the schema, the dedupe rule, each event site, each route handler. This
one walks the six product flows the way the app does, dispatching through
`api.lambda_handler` rather than calling handlers directly.

That distinction is not ceremony:

  * a handler can be correct while its ROUTE is unregistered or misspelled, so
    the feature is dead on the handset and every unit test still passes;
  * the deadline sweep only runs as a SIDE EFFECT of GET /tasks, so
    "one notification per day however often the app opens" is only really
    proven by opening the list repeatedly through the router;
  * the router's own error translation (ApiError -> status + body) is what a
    client actually sees, and calling a handler directly bypasses it.

MUST RUN UNDER PYTEST, not as a bare script: conftest.py installs the shared
boto3/botocore stubs BEFORE the Lambda is imported, so `except ClientError` in
the product catches what fake_dynamodb raises. Run standalone and the two
ClientError classes differ, the conditional-write branch is never taken, and
the dedupe assertions below fail for a reason that is entirely the harness's
— exactly the identity hazard conftest.py's header documents.

Run:  python -m pytest tests/test_notifications_e2e.py -q
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

CLOUD = Path(__file__).resolve().parents[1]
for _p in (CLOUD / "shared", CLOUD / "functions/userapi",
           Path(__file__).resolve().parent):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from test_ai_workspace import RECORDING, api  # noqa: E402
import fake_dynamodb as fdb  # noqa: E402
import notification_schema as ns  # noqa: E402

KEY = "recordings/u-1/mobile/mobile-abc_1754300000.m4a"
OWNER, ASSIGNEE, STRANGER = "u-1", "u-3", "u-2"
CONTACT = "c-linked"

item = json.loads(json.dumps(RECORDING))
item["user_id"] = OWNER
item["title"] = "Client Discussion"
item.pop("ai_tasks", None)

notifications = fdb.FakeTable("Notifications", "notification_id", indexes={
    "user-index": ("user_id", "created_at"),
    "user-unread-index": ("user_id", "unread_marker")})
dedupe = fdb.FakeTable("NotificationDedupe", "dedupe_key")
recordings = fdb.FakeTable("Recordings", "audio_s3_key")
contacts = fdb.FakeTable("Contacts", "contact_id")
tasks = fdb.FakeTable("Tasks", "task_id", indexes={
    "owner-index": ("owner_user_id", "created_at"),
    "meeting-index": ("source_recording_id", "created_at"),
    "folder-index": ("folder_id", "created_at"),
    "assignee-index": ("assignee_contact_id", "created_at"),
    "dedupe-index": ("owner_user_id", "fingerprint")})
participants = fdb.FakeTable("MeetingParticipants", "audio_s3_key", "speaker_id")

recordings.items[(KEY,)] = item
contacts.items[(CONTACT,)] = {
    "contact_id": CONTACT, "owner_user_id": OWNER, "name": "Rahul Sharma",
    "email": "rahul@example.com", "minutex_user_id": ASSIGNEE}

CALLER = {"id": OWNER}

def _patches():
    """The stubs this walk runs against.

    Built and entered INSIDE the test rather than at import time. Module-level
    mock.patch(...).start() with no matching stop leaks into every test module
    imported afterwards — pytest imports this file once and the patches would
    then still be live for the rest of the session, which is how a suite that
    passes alone starts failing in a full run.
    """
    return [
        mock.patch.object(api, "Key", fdb.Key),
        mock.patch.object(api, "_notifications", notifications),
        mock.patch.object(api, "_notification_dedupe", dedupe),
        mock.patch.object(api, "_recordings", recordings),
        mock.patch.object(api, "_contacts", contacts),
        mock.patch.object(api, "_tasks", tasks),
        mock.patch.object(api, "_meeting_participants", participants),
        mock.patch.object(api, "_owned_devices", return_value=[]),
        mock.patch.object(api, "_require_auth",
                          side_effect=lambda ev: CALLER["id"]),
    ]


def route(method, path, body=None, params=None, qs=None):
    """Dispatch through the REAL router, the way API Gateway does."""
    ev = {
        "routeKey": f"{method} {path}",
        "requestContext": {"http": {"method": method, "path": path}},
        "pathParameters": params or {},
        "headers": {"authorization": "Bearer t"},
    }
    if qs is not None:
        ev["queryStringParameters"] = qs
    if body is not None:
        ev["body"] = json.dumps(body)
    resp = api.lambda_handler(ev, None)
    return resp["statusCode"], json.loads(resp["body"])


ok = lambda label: print(f"  PASS  {label}")
fails = []


def check(cond, label):
    if cond:
        ok(label)
    else:
        fails.append(label)
        print(f"  FAIL  {label}")


def test_end_to_end_notification_flows():
    active = _patches()
    for _p in active:
        _p.start()
    try:
        print("\n=== TEST 1 — Meeting processed -> notification -> opens meeting ===")
        api._notify(OWNER, ns.TYPE_MEETING_PROCESSING_COMPLETED, KEY,
                    subject="Client Discussion")
        s, body = route("GET", "/notifications")
        check(s == 200, "GET /notifications answers 200 through the router")
        n = body["notifications"][0]
        check(n["title"] == "Meeting processing completed", "title reads correctly")
        check(n["message"] == "Client Discussion is ready", "message names the meeting")
        check(body["unread_count"] == 1, "badge shows 1")
        check(n["entity_type"] == "meeting" and n["entity_id"] == KEY,
              "deep-links to the meeting")

        print("\n=== TEST 2 — Task assigned -> assignee notified -> opens task ===")
        s, created = route("POST", "/recordings/ai/tasks/{key+}",
                           {"task": "Complete API integration",
                            "assignee_contact_id": CONTACT},
                           params={"key": KEY})
        check(s == 201, "task created")
        task_id = created["task"]["id"]
        CALLER["id"] = ASSIGNEE
        s, body = route("GET", "/notifications")
        assigned = [x for x in body["notifications"] if x["type"] == "TASK_ASSIGNED"]
        check(len(assigned) == 1, "assignee sees exactly one TASK_ASSIGNED")
        check(assigned[0]["title"] == "New task assigned to you", "title reads correctly")
        check(assigned[0]["message"] == "Complete API integration", "message is the task")
        check(assigned[0]["entity_type"] == "task" and assigned[0]["entity_id"] == task_id,
              "deep-links to Task Details")
        check(assigned[0]["priority"] == "HIGH", "priority is HIGH")

        print("\n=== TEST 3/4 — Deadlines: due today, then overdue, no duplicates ===")
        CALLER["id"] = OWNER
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = api._new_task_row(OWNER, "Send proposal", recording_key=KEY,
                                due="today", due_normalized=today)
        row["assignee_user_id"] = OWNER
        api._write_task(row)
        for _ in range(5):                       # simulate five app opens
            route("GET", "/tasks")
        due = [x for x in notifications.items.values()
               if x["type"] == "TASK_DUE_TODAY" and x["user_id"] == OWNER]
        check(len(due) == 1, "due-today raised exactly once across five list reads")

        overdue_row = api._new_task_row(OWNER, "Ship the build", recording_key=KEY,
                                        due="2020-01-01", due_normalized="2020-01-01")
        overdue_row["assignee_user_id"] = OWNER
        api._write_task(overdue_row)
        for _ in range(5):
            route("GET", "/tasks")
        od = [x for x in notifications.items.values() if x["type"] == "TASK_OVERDUE"]
        check(len(od) == 1, "overdue raised exactly once across five list reads")
        check(od[0]["title"] == "Task overdue", "overdue title reads correctly")

        print("\n=== TEST 5 — Ambiguous AI task -> AI needs your confirmation ===")
        item["ai_tasks"] = [{"task": "Prepare proposal", "assignee": "Ambiguous Name"}]
        api._seed_ai_tasks(OWNER, KEY, item)
        ai = [x for x in notifications.items.values()
              if x["type"] == "AI_ACTION_REQUIRED"]
        check(len(ai) == 1, "one AI_ACTION_REQUIRED raised")
        check(ai[0]["title"] == "AI needs your confirmation", "title reads correctly")
        check("Prepare proposal" in ai[0]["message"] and "review" in ai[0]["message"],
              "message asks for review of the named task")
        check(ai[0]["entity_type"] == "task", "deep-links to the task to review")
        api._seed_ai_tasks(OWNER, KEY, item)     # re-open the meeting
        check(len([x for x in notifications.items.values()
                   if x["type"] == "AI_ACTION_REQUIRED"]) == 1,
              "re-opening the meeting does not duplicate it")

        print("\n=== TEST 6 — Security: user A cannot touch user B's notifications ===")
        mine = [x for x in notifications.items.values() if x["user_id"] == ASSIGNEE][0]
        CALLER["id"] = STRANGER
        s, body = route("GET", "/notifications")
        check(body["count"] == 0, "a stranger sees none of them")
        s, _ = route("POST", "/notifications/{notification_id}/read",
                     params={"notification_id": mine["notification_id"]})
        check(s == 404, "a stranger marking another user's notification read gets 404")
        s2, _ = route("POST", "/notifications/{notification_id}/read",
                      params={"notification_id": "totally-made-up"})
        check(s == s2, "a foreign id is indistinguishable from a missing one")
        check(notifications.items[(mine["notification_id"],)]["is_read"] is False,
              "the notification really is untouched")

        print("\n=== Unread / mark read / mark all read ===")
        CALLER["id"] = OWNER
        s, before = route("GET", "/notifications/unread-count")
        check(s == 200 and before["unread_count"] > 0, "unread-count route answers")
        target = [x for x in notifications.items.values()
                  if x["user_id"] == OWNER and not x["is_read"]][0]
        s, body = route("POST", "/notifications/{notification_id}/read",
                        params={"notification_id": target["notification_id"]})
        check(s == 200 and body["notification"]["is_read"], "mark one read works")
        check(body["unread_count"] == before["unread_count"] - 1, "badge drops by one")
        check("unread_marker" not in notifications.items[(target["notification_id"],)],
              "the sparse marker is removed, so the badge stops counting it")
        s, body = route("POST", "/notifications/read-all")
        check(s == 200 and body["unread_count"] == 0, "mark all read clears the badge")
        s, body = route("GET", "/notifications/unread-count")
        check(body["unread_count"] == 0, "unread-count agrees afterwards")
        kept = [x for x in notifications.items.values() if x["user_id"] == OWNER]
        check(len(kept) > 0, "read notifications are KEPT, not deleted")

        print("\n=== Pagination ===")
        seen, cursor, guard = [], "", 0
        while guard < 20:
            qs = {"limit": "2"}
            if cursor:
                qs["cursor"] = cursor
            s, page = route("GET", "/notifications", qs=qs)
            seen += [x["notification_id"] for x in page["notifications"]]
            cursor = page["next_cursor"]
            guard += 1
            if not cursor:
                break
        check(len(seen) == len(set(seen)), "pagination never repeats a row")
        check(len(seen) == len(kept), f"pagination reaches every row ({len(seen)})")

        print("\n" + "=" * 62)
        assert not fails, "end-to-end flows failed:\n  - " + "\n  - ".join(fails)
        print("ALL END-TO-END FLOWS PASSED")

    finally:
        for _p in reversed(active):
            _p.stop()
