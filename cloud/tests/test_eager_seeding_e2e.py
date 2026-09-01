#!/usr/bin/env python3
"""test_eager_seeding_e2e.py — the whole eager flow, end to end.

WHY THIS EXISTS ALONGSIDE test_eager_task_seeding.py. That file tests the seam
in units: the invoke is made, the event is handled, the seeder is idempotent.
This one walks the PRODUCT CLAIM — a meeting is processed, NOBODY OPENS IT, and
its tasks and notifications are already there.

It runs the real `analyze_and_persist`, and its stubbed `_lambda_client` hands
the payload straight to userApi's real `lambda_handler`, which is exactly what
AWS does with an async invoke. So the transcribe side, the event contract, the
userApi dispatch, the seeder, the resolution chain, the date anchoring, the
notifications and the two read APIs are all exercised as one path. A break
anywhere in that chain fails here.

Two things the fixture gets right on purpose, because getting them wrong makes
the test lie:

  * The S3 key's EPOCH SUFFIX is the meeting date. `analyze_and_persist`
    derives `recorded_at` from the key, so a fixture whose key and whose
    asserted deadline disagree would "prove" an anchoring bug that does not
    exist (it did, during development).
  * `due_date_normalized` is asserted against the STORED ROW, not the API
    response — it is deliberately internal (it drives `is_overdue`) and is not
    part of the public task payload.

MUST RUN UNDER PYTEST: conftest.py installs the shared boto3/botocore stubs
before either Lambda is imported. See test_notifications_e2e.py for the same
constraint and the reason.

Run:  python -m pytest tests/test_eager_seeding_e2e.py -q
"""
import importlib.util as _ilu, json, sys
from pathlib import Path
from unittest import mock
ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT/"shared", ROOT/"functions/userapi", Path(__file__).resolve().parent):
    if str(p) not in sys.path: sys.path.insert(0, str(p))
from test_ai_workspace import RECORDING, api
import fake_dynamodb as fdb, notification_schema as ns
_spec=_ilu.spec_from_file_location("transcribe_lambda_function",
    str(ROOT/"functions/transcribe"/"lambda_function.py"))
tr=_ilu.module_from_spec(_spec); sys.modules["transcribe_lambda_function"]=tr
_spec.loader.exec_module(tr)

# The epoch suffix IS the meeting date: analyze_and_persist derives
# recorded_at from the key, so the fixture must be self-consistent.
KEY="recordings/u-1/mobile/meeting-A_1787738400.m4a"; OWNER="u-1"; ASSIGNEE="u-ravi"

def test_e2e_eager_pipeline():
    tasks=fdb.FakeTable("Tasks","task_id",indexes={
        "owner-index":("owner_user_id","created_at"),"meeting-index":("source_recording_id","created_at"),
        "folder-index":("folder_id","created_at"),"assignee-index":("assignee_contact_id","created_at"),
        "dedupe-index":("owner_user_id","fingerprint")})
    recs=fdb.FakeTable("Recordings","audio_s3_key"); cons=fdb.FakeTable("Contacts","contact_id")
    parts=fdb.FakeTable("MeetingParticipants","audio_s3_key","speaker_id")
    notifs=fdb.FakeTable("Notifications","notification_id",indexes={
        "user-index":("user_id","created_at"),"user-unread-index":("user_id","unread_marker")})
    ded=fdb.FakeTable("NotificationDedupe","dedupe_key")

    item=json.loads(json.dumps(RECORDING)); item["user_id"]=OWNER
    item.pop("ai_tasks",None)   # recorded_at comes from the key
    recs.items[(KEY,)]=item
    cons.items[("c-ravi",)]={"contact_id":"c-ravi","owner_user_id":OWNER,"name":"Ravi Kumar",
                             "email":"ravi@x.com","minutex_user_id":ASSIGNEE}
    parts.items[(KEY,"0")]={"audio_s3_key":KEY,"speaker_id":"0","contact_id":"c-ravi"}

    ps=[mock.patch.object(api,"Key",fdb.Key),mock.patch.object(api,"_tasks",tasks),
        mock.patch.object(api,"_recordings",recs),mock.patch.object(api,"_contacts",cons),
        mock.patch.object(api,"_meeting_participants",parts),
        mock.patch.object(api,"_notifications",notifs),
        mock.patch.object(api,"_notification_dedupe",ded),
        mock.patch.object(api,"_owned_devices",return_value=[])]
    for p in ps: p.start()
    ok=lambda m: print(f"  PASS  {m}"); fails=[]
    def check(c,m):
        (ok(m) if c else (fails.append(m), print(f"  FAIL  {m}")))

    # --- run the REAL pipeline tail; the invoke is routed into userApi ---
    print("\n=== STEP 1: pipeline processes Meeting A (nobody opens it) ===")
    analysis={"title":"Client Discussion",
        "overview":{"sections":[{"kind":"text","heading":"Summary","content":"ok"}]},
        "tasks":[
            {"task":"Send the proposal","assignee":"","assignee_speaker_id":"0",
             "due_date":"tomorrow","confidence":"high","evidence":"I'll send the proposal tomorrow."},
            {"task":"Prepare the quotation","assignee":"Rahul","assignee_speaker_id":"",
             "confidence":"high","evidence":"Rahul, please prepare the quotation."}],
        "participants":[],"meeting_highlights":{}}

    def fake_invoke(**kw):
        # What AWS does with an async invoke: hand the payload to userApi.
        api.lambda_handler(json.loads(kw["Payload"].decode("utf-8")), None)
        return {"StatusCode":202}

    with mock.patch.object(tr,"_table") as table, \
         mock.patch.object(tr,"analyze_meeting",return_value=analysis), \
         mock.patch.object(tr,"transcript_store") as store, \
         mock.patch.object(tr,"_upsert") as upsert, \
         mock.patch.object(tr,"_notify_processing_outcome") as notify_done, \
         mock.patch.object(tr,"_lambda_client") as lc:
        table.get_item.return_value={"Item":item}
        store.with_segment_ids.return_value=[]; store.put.return_value={}
        lc.invoke.side_effect=fake_invoke
        def apply_upsert(k,fields,remove=()):
            item.update(fields); recs.items[(KEY,)]=item
        upsert.side_effect=apply_upsert
        out=tr.analyze_and_persist("bucket",KEY,"Speaker 0: I'll send the proposal tomorrow.",[],"en")

    check(out["status"]=="complete","pipeline reached status=complete")
    check(lc.invoke.called,"pipeline requested task seeding")
    check(notify_done.called,"completion notification still fired")

    print("\n=== STEP 2: GET /tasks — meeting NEVER opened ===")
    with mock.patch.object(api,"_require_auth",return_value=OWNER):
        r=api.lambda_handler({"routeKey":"GET /tasks",
            "requestContext":{"http":{"method":"GET","path":"/tasks"}},
            "headers":{"authorization":"Bearer t"}},None)
    body=json.loads(r["body"])
    check(r["statusCode"]==200,"GET /tasks answers 200")
    check(body["count"]==2,f"both tasks already present (got {body['count']})")
    titles=sorted(t["task"] for t in body["tasks"])
    check(titles==["Prepare the quotation","Send the proposal"],"correct task titles")

    print("\n=== STEP 3: assignment + deadline correctness ===")
    by={t["task"]:t for t in body["tasks"]}
    sp=by["Send the proposal"]
    check(sp["resolution_status"]=="RESOLVED","self-commitment resolved to the mapped contact")
    check(sp.get("assignee_user_id")==ASSIGNEE,"assignee_user_id is the real account")
    check(sp["due_date"]=="tomorrow","spoken due preserved verbatim")
    # due_date_normalized is deliberately INTERNAL (it drives is_overdue and is
    # not in the public payload), so this reads the stored row rather than the
    # API response.
    _stored={r["title"]:r for r in tasks.items.values()}["Send the proposal"]
    check(_stored.get("due_date_normalized")=="2026-08-27",
          f"anchored on the MEETING date, not now (got {_stored.get('due_date_normalized')!r})")
    pq=by["Prepare the quotation"]
    check(pq["resolution_status"]=="UNRESOLVED","unresolved name stays unresolved")
    check(not pq.get("assignee_user_id"),"unresolved name NOT promoted to a user")

    print("\n=== STEP 4: notifications, without opening the meeting ===")
    got={(n["user_id"],n["type"]) for n in notifs.items.values()}
    check((ASSIGNEE,ns.TYPE_TASK_ASSIGNED) in got,"assignee got TASK_ASSIGNED")
    check((OWNER,ns.TYPE_AI_ACTION_REQUIRED) in got,"owner got AI_ACTION_REQUIRED")
    check(not any(u==OWNER and t==ns.TYPE_TASK_ASSIGNED for u,t in got),
          "owner (the actor) not spammed with TASK_ASSIGNED")

    print("\n=== STEP 5: reprocess -> no duplicates ===")
    before=len(tasks.items); nbefore=len(notifs.items)
    api.lambda_handler({"type":api.INTERNAL_SEED_TASKS_EVENT,"audio_s3_key":KEY},None)
    check(len(tasks.items)==before,f"reprocess created no new tasks ({before})")
    check(len(notifs.items)==nbefore,"reprocess created no new notifications")

    print("\n=== STEP 6: lazy fallback after eager -> still no duplicates ===")
    with mock.patch.object(api,"_require_auth",return_value=OWNER):
        r2=api.lambda_handler({"routeKey":"GET /recordings/ai/tasks/{key+}",
            "requestContext":{"http":{"method":"GET","path":"/"}},
            "pathParameters":{"key":KEY},"headers":{"authorization":"Bearer t"}},None)
    check(len(tasks.items)==before,"opening the meeting created nothing new")
    check(json.loads(r2["body"])["count"]==2,"meeting view shows the same 2 tasks")
    check(len(notifs.items)==nbefore,"no duplicate notifications from the lazy path")

    for p in ps: p.stop()
    print("\n"+"="*60)
    assert not fails, "E2E failures:\n  - "+"\n  - ".join(fails)
    print("EAGER SEEDING E2E: ALL CHECKS PASSED")
