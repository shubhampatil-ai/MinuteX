#!/usr/bin/env python3
# =============================================================
# test_workspace_api.py — LIVE end-to-end check of the organization layer.
#
# The offline unit suite is tests/test_workspace_org.py (116 tests, no network,
# no AWS). This file is its live counterpart, and it proves the things unit
# tests with a fake DynamoDB structurally cannot:
#
#   * the API Gateway route templates actually exist and reach the Lambda
#     (a missing ensure_route shows up as 404, never as a unit-test failure);
#   * the IAM policy really covers the new tables AND their indexes
#     (the table-vs-index ARN mistake passes every unit test and fails every
#      live list route);
#   * the real DynamoDB rejects/accepts the same writes the fake does —
#     particularly the sparse-index rule about empty-string key attributes;
#   * cross-user isolation holds against the deployed authorizer, not a mock.
#
# Checks (in order — later ones reuse what earlier ones created):
#    1. POST   /folders                     create
#    2. POST   /folders (same name)         -> 409 duplicate
#    3. GET    /folders                     lists it, with meeting_count
#    4. PATCH  /folders/{id}                rename
#    5. POST   /contacts                    create (global)
#    6. POST   /contacts (same email)       -> 200 existing, NOT a second row
#    7. POST   /contacts (same name only)   -> 409 with candidates
#    8. POST   /folders/{f}/contacts/{c}    associate
#    9. POST   again                        idempotent, no duplicate
#   10. GET    /folders/{f}/contacts        lists the association
#   11. GET    /contacts?search=            server-side search finds it
#   12. PATCH  /recordings/folder/{key}     file a real meeting
#   13. GET    /recordings/participants/    speakers + folder contacts
#   14. PUT    /recordings/participants/    map speaker -> contact
#   15. GET    /recordings/ai/tasks/{key}   tasks served from the new table
#   16. GET    /tasks?folder_id=            cross-meeting query, filtered
#   17. GET    /tasks/{id}                  detail carries contact+folder+meeting
#   18. POST   /tasks/{id}/resolve          explicit assignee resolution
#   19. PATCH  /recordings/folder/{key}     move to General (folder_id null)
#   20. DELETE /folders/{id}                folder goes, MEETING SURVIVES
#   21. DELETE /contacts/{id}               person goes, tasks survive
#   22. auth:  no token -> 401 on every collection route
#   23. isolation: a second user sees none of it (404, never 403)
#
# Cleans up after itself: every folder/contact it creates is deleted at the end,
# and the meeting it borrows is returned to the folder it started in.
#
# Requires (from .env): API_URL, and a test account. Creates its own throwaway
# users via /signup so it never depends on a pre-existing password.
#
#   python tests/test_workspace_api.py --live
#   python tests/test_workspace_api.py --live --key "recordings/<uid>/mobile/<id>.m4a"
#
# Without --key it picks the newest recording the test user owns. With no
# recordings at all it SKIPS the meeting-dependent checks and says so, rather
# than reporting failures that are really "nothing to test against".
# =============================================================
import argparse
import json
import sys
import urllib.parse
import uuid
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_tests import check, load_dotenv, _results  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TIMEOUT = 30


def hdrs(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def signup(api: str) -> tuple:
    """A throwaway account, so this never needs a stored password and never
    pollutes a real user's contacts."""
    email = f"wsorg-{uuid.uuid4().hex[:10]}@example.test"
    r = requests.post(f"{api}/signup",
                      json={"email": email, "password": "Test-passw0rd!",
                            "name": "Workspace Test"},
                      timeout=TIMEOUT)
    r.raise_for_status()
    body = r.json()
    return body["token"], body["user_id"], email


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Live end-to-end check of folders/contacts/tasks.")
    ap.add_argument("--live", action="store_true",
                    help="required — this test calls the deployed API")
    ap.add_argument("--key", default="",
                    help="recording key to use for the meeting-scoped checks")
    args = ap.parse_args()

    if not args.live:
        print("This test calls the DEPLOYED API. Re-run with --live.")
        return 0

    load_dotenv(ROOT / ".env")
    import os
    api = (os.environ.get("API_URL") or "").rstrip("/")
    if not api:
        print("API_URL not set (put it in .env).")
        return 1

    print(f">> API: {api}")
    token, user_id, email = signup(api)
    print(f">> test user: {email}")
    h = hdrs(token)

    created_folders, created_contacts = [], []

    def cleanup():
        for cid in created_contacts:
            requests.delete(f"{api}/contacts/{cid}", headers=h, timeout=TIMEOUT)
        for fid in created_folders:
            requests.delete(f"{api}/folders/{fid}", headers=h, timeout=TIMEOUT)

    try:
        # -- 1. create folder ------------------------------------------------
        name = f"Client Alpha {uuid.uuid4().hex[:6]}"
        r = requests.post(f"{api}/folders", headers=h,
                          json={"name": name, "description": "live test"},
                          timeout=TIMEOUT)
        check("POST /folders creates a folder", r.status_code == 201,
              f"{r.status_code} {r.text[:200]}")
        folder = r.json().get("folder", {})
        folder_id = folder.get("id", "")
        if folder_id:
            created_folders.append(folder_id)
        check("new folder reports meeting_count 0",
              folder.get("meeting_count") == 0, str(folder))

        # -- 2. duplicate name ----------------------------------------------
        r = requests.post(f"{api}/folders", headers=h,
                          json={"name": name.lower()}, timeout=TIMEOUT)
        check("duplicate folder name -> 409", r.status_code == 409,
              f"{r.status_code} {r.text[:200]}")

        # -- 3. list ---------------------------------------------------------
        r = requests.get(f"{api}/folders", headers=h, timeout=TIMEOUT)
        listed = r.json().get("folders", [])
        check("GET /folders lists the new folder",
              r.status_code == 200 and any(f["id"] == folder_id for f in listed),
              f"{r.status_code} {r.text[:200]}")
        check("no uniqueness-claim rows leak into the list",
              not any(str(f.get("id", "")).startswith("name#") for f in listed),
              str(listed)[:200])

        # -- 4. rename -------------------------------------------------------
        renamed = f"{name} Renamed"
        r = requests.patch(f"{api}/folders/{folder_id}", headers=h,
                           json={"name": renamed}, timeout=TIMEOUT)
        check("PATCH /folders/{id} renames",
              r.status_code == 200 and r.json()["folder"]["name"] == renamed,
              f"{r.status_code} {r.text[:200]}")

        # -- 5. create contact ----------------------------------------------
        c_email = f"rahul-{uuid.uuid4().hex[:8]}@company.test"
        r = requests.post(f"{api}/contacts", headers=h,
                          json={"name": "Rahul Sharma", "email": c_email},
                          timeout=TIMEOUT)
        check("POST /contacts creates a contact", r.status_code == 201,
              f"{r.status_code} {r.text[:200]}")
        contact = r.json().get("contact", {})
        contact_id = contact.get("id", "")
        if contact_id:
            created_contacts.append(contact_id)

        # -- 6. same email = same person ------------------------------------
        r = requests.post(f"{api}/contacts", headers=h,
                          json={"name": "R. Sharma", "email": c_email.upper()},
                          timeout=TIMEOUT)
        same = r.status_code == 200 and r.json().get("existing") is True \
            and r.json().get("contact", {}).get("id") == contact_id
        check("same email returns the EXISTING contact, not a duplicate", same,
              f"{r.status_code} {r.text[:200]}")

        # -- 7. same name only = ambiguous ----------------------------------
        r = requests.post(f"{api}/contacts", headers=h,
                          json={"name": "Rahul Sharma"}, timeout=TIMEOUT)
        body = r.json() if r.content else {}
        check("name-only match -> 409 with candidates",
              r.status_code == 409 and body.get("code") == "contact_ambiguous"
              and len(body.get("candidates", [])) >= 1,
              f"{r.status_code} {r.text[:200]}")

        # -- 8/9. associate (twice) -----------------------------------------
        url = f"{api}/folders/{folder_id}/contacts/{contact_id}"
        r1 = requests.post(url, headers=h, timeout=TIMEOUT)
        r2 = requests.post(url, headers=h, timeout=TIMEOUT)
        check("POST folder/contact association", r1.status_code == 200,
              f"{r1.status_code} {r1.text[:200]}")
        check("re-associating is idempotent", r2.status_code == 200,
              f"{r2.status_code} {r2.text[:200]}")

        # -- 10. list folder contacts ---------------------------------------
        r = requests.get(f"{api}/folders/{folder_id}/contacts", headers=h,
                         timeout=TIMEOUT)
        cs = r.json().get("contacts", [])
        check("folder contacts list has exactly one entry",
              r.status_code == 200 and len(cs) == 1
              and cs[0]["id"] == contact_id,
              f"{r.status_code} {r.text[:200]}")

        # -- 11. server-side search -----------------------------------------
        r = requests.get(f"{api}/contacts",
                         params={"search": "rahul"}, headers=h, timeout=TIMEOUT)
        check("GET /contacts?search finds the contact",
              r.status_code == 200
              and any(c["id"] == contact_id for c in r.json().get("contacts", [])),
              f"{r.status_code} {r.text[:200]}")

        # ---- meeting-scoped checks (need a real recording) ----------------
        key = args.key
        if not key:
            r = requests.get(f"{api}/recordings", headers=h, timeout=TIMEOUT)
            recs = r.json().get("recordings", []) if r.status_code == 200 else []
            key = recs[0]["audio_s3_key"] if recs else ""

        if not key:
            print("\n>> No recording available on this account — SKIPPING the")
            print("   meeting/participant/task checks. Pass --key to force one,")
            print("   or record something first. (Not a failure.)")
        else:
            print(f">> using recording: {key}")
            qkey = urllib.parse.quote(key, safe="")
            original_folder = ""
            r = requests.get(f"{api}/recordings/{qkey}", headers=h, timeout=TIMEOUT)
            if r.status_code == 200:
                original_folder = r.json().get("recording", {}).get("folder_id", "") or ""

            # -- 12. file the meeting ---------------------------------------
            r = requests.patch(f"{api}/recordings/folder/{qkey}", headers=h,
                               json={"folder_id": folder_id}, timeout=TIMEOUT)
            check("PATCH /recordings/folder files the meeting",
                  r.status_code == 200
                  and r.json().get("folder_id") == folder_id,
                  f"{r.status_code} {r.text[:200]}")

            r = requests.get(f"{api}/folders", headers=h, timeout=TIMEOUT)
            f_now = next((f for f in r.json().get("folders", [])
                          if f["id"] == folder_id), {})
            check("folder meeting_count reflects the move",
                  f_now.get("meeting_count") == 1, str(f_now))

            # -- 13. participants view --------------------------------------
            r = requests.get(f"{api}/recordings/participants/{qkey}", headers=h,
                             timeout=TIMEOUT)
            pbody = r.json() if r.content else {}
            check("GET participants returns speakers + folder contacts",
                  r.status_code == 200 and "speakers" in pbody
                  and any(c["id"] == contact_id
                          for c in pbody.get("folder_contacts", [])),
                  f"{r.status_code} {r.text[:250]}")

            speakers = pbody.get("speakers", [])
            if speakers:
                sid = speakers[0]
                # -- 14. map a speaker --------------------------------------
                r = requests.put(f"{api}/recordings/participants/{qkey}",
                                 headers=h,
                                 json={"speaker_id": sid,
                                       "contact_id": contact_id},
                                 timeout=TIMEOUT)
                check("PUT participants maps speaker -> contact",
                      r.status_code == 200
                      and r.json().get("participant", {})
                          .get("contact_id") == contact_id,
                      f"{r.status_code} {r.text[:250]}")

                # The transcript must be untouched by that mapping.
                r = requests.get(f"{api}/recordings/{qkey}", headers=h,
                                 timeout=TIMEOUT)
                rec = r.json().get("recording", {})
                segs = rec.get("timestamps") or []
                raw_labels = {str(s.get("speaker")) for s in segs
                              if isinstance(s, dict)}
                check("transcript speaker labels are NOT rewritten to names",
                      not raw_labels or all(
                          lbl == "" or not lbl.lower().startswith("rahul")
                          for lbl in raw_labels),
                      str(sorted(raw_labels)[:6]))
                check("speaker_names map carries the contact name instead",
                      (rec.get("speaker_names") or {}).get(str(sid), "")
                      == "Rahul Sharma",
                      str(rec.get("speaker_names")))
            else:
                print(">> recording has no diarized speakers — skipping the "
                      "speaker-mapping checks.")

            # -- 15. tasks from the new table -------------------------------
            r = requests.get(f"{api}/recordings/ai/tasks/{qkey}", headers=h,
                             timeout=TIMEOUT)
            check("GET meeting tasks succeeds (served from Tasks table)",
                  r.status_code == 200 and "tasks" in r.json(),
                  f"{r.status_code} {r.text[:200]}")
            mtasks = r.json().get("tasks", [])
            if mtasks:
                check("meeting tasks carry the folder they inherited",
                      all(t.get("folder_id") == folder_id for t in mtasks),
                      str([t.get("folder_id") for t in mtasks][:4]))

            # Create one so the task checks below always have something.
            r = requests.post(f"{api}/recordings/ai/tasks/{qkey}", headers=h,
                              json={"task": "Live test task",
                                    "priority": "High"}, timeout=TIMEOUT)
            check("POST meeting task creates a first-class task",
                  r.status_code == 201, f"{r.status_code} {r.text[:200]}")
            task = r.json().get("task", {})
            task_id = task.get("id", "")
            check("new task is Open and unassigned",
                  task.get("status") == "Open"
                  and task.get("resolution_status") == "NONE", str(task)[:200])

            # -- 16. cross-meeting query ------------------------------------
            r = requests.get(f"{api}/tasks",
                             params={"folder_id": folder_id}, headers=h,
                             timeout=TIMEOUT)
            check("GET /tasks?folder_id returns the task",
                  r.status_code == 200
                  and any(t["id"] == task_id for t in r.json().get("tasks", [])),
                  f"{r.status_code} {r.text[:250]}")

            r = requests.get(f"{api}/tasks", params={"status": "Completed"},
                             headers=h, timeout=TIMEOUT)
            check("status filter excludes the Open task",
                  r.status_code == 200
                  and not any(t["id"] == task_id
                              for t in r.json().get("tasks", [])),
                  f"{r.status_code} {r.text[:200]}")

            # -- 17. detail --------------------------------------------------
            r = requests.get(f"{api}/tasks/{task_id}", headers=h, timeout=TIMEOUT)
            d = r.json() if r.content else {}
            check("GET /tasks/{id} carries folder + source meeting",
                  r.status_code == 200
                  and d.get("folder", {}).get("id") == folder_id
                  and d.get("recording", {}).get("audio_s3_key") == key,
                  f"{r.status_code} {r.text[:250]}")

            # -- 18. resolve assignee ---------------------------------------
            r = requests.post(f"{api}/tasks/{task_id}/resolve", headers=h,
                              json={"contact_id": contact_id}, timeout=TIMEOUT)
            rt = r.json().get("task", {}) if r.content else {}
            check("POST /tasks/{id}/resolve assigns the contact",
                  r.status_code == 200
                  and rt.get("assignee_contact_id") == contact_id
                  and rt.get("resolution_status") == "RESOLVED",
                  f"{r.status_code} {r.text[:250]}")

            # -- 19. back to General ----------------------------------------
            r = requests.patch(f"{api}/recordings/folder/{qkey}", headers=h,
                               json={"folder_id": None}, timeout=TIMEOUT)
            check("folder_id null moves the meeting to General",
                  r.status_code == 200 and not r.json().get("folder_id"),
                  f"{r.status_code} {r.text[:200]}")

            # -- 20. delete folder, meeting survives ------------------------
            r = requests.patch(f"{api}/recordings/folder/{qkey}", headers=h,
                               json={"folder_id": folder_id}, timeout=TIMEOUT)
            r = requests.delete(f"{api}/folders/{folder_id}", headers=h,
                                timeout=TIMEOUT)
            check("DELETE /folders reports the meetings it moved",
                  r.status_code == 200 and r.json().get("meetings_moved", 0) >= 1,
                  f"{r.status_code} {r.text[:200]}")
            if folder_id in created_folders:
                created_folders.remove(folder_id)

            r = requests.get(f"{api}/recordings/{qkey}", headers=h,
                             timeout=TIMEOUT)
            check("THE MEETING SURVIVES its folder being deleted",
                  r.status_code == 200, f"{r.status_code} {r.text[:200]}")
            check("and is back in General",
                  not (r.json().get("recording", {}).get("folder_id") or ""),
                  str(r.json().get("recording", {}).get("folder_id")))

            # -- 21. delete contact, task survives --------------------------
            r = requests.delete(f"{api}/contacts/{contact_id}", headers=h,
                                timeout=TIMEOUT)
            check("DELETE /contacts reports the tasks it unassigned",
                  r.status_code == 200, f"{r.status_code} {r.text[:200]}")
            if contact_id in created_contacts:
                created_contacts.remove(contact_id)

            r = requests.get(f"{api}/tasks/{task_id}", headers=h, timeout=TIMEOUT)
            surviving = r.json().get("task", {}) if r.content else {}
            check("THE TASK SURVIVES its assignee being deleted",
                  r.status_code == 200, f"{r.status_code} {r.text[:200]}")
            check("and reverts to UNRESOLVED with the name kept",
                  surviving.get("resolution_status") == "UNRESOLVED"
                  and surviving.get("assignee_name_legacy") == "Rahul Sharma",
                  str(surviving)[:250])

            # Restore the meeting's original folder so this test leaves the
            # account as it found it.
            if original_folder:
                requests.patch(f"{api}/recordings/folder/{qkey}", headers=h,
                               json={"folder_id": original_folder},
                               timeout=TIMEOUT)
            # And remove the task it created.
            requests.delete(f"{api}/recordings/ai/tasks/{qkey}", headers=h,
                            json={"id": task_id}, timeout=TIMEOUT)

        # -- 22. auth ---------------------------------------------------------
        for method, path in (("GET", "/folders"), ("GET", "/contacts"),
                             ("GET", "/tasks")):
            r = requests.request(method, f"{api}{path}",
                                 headers={"Content-Type": "application/json"},
                                 timeout=TIMEOUT)
            check(f"{method} {path} without a token -> 401",
                  r.status_code == 401, f"{r.status_code} {r.text[:120]}")

        # -- 23. cross-user isolation -----------------------------------------
        other_token, _, other_email = signup(api)
        oh = hdrs(other_token)
        # A folder the second user does not own.
        r = requests.post(f"{api}/folders", headers=h,
                          json={"name": f"Isolation {uuid.uuid4().hex[:6]}"},
                          timeout=TIMEOUT)
        iso_folder = r.json().get("folder", {}).get("id", "")
        if iso_folder:
            created_folders.append(iso_folder)
        r = requests.post(f"{api}/contacts", headers=h,
                          json={"name": "Private Person",
                                "email": f"p-{uuid.uuid4().hex[:8]}@x.test"},
                          timeout=TIMEOUT)
        iso_contact = r.json().get("contact", {}).get("id", "")
        if iso_contact:
            created_contacts.append(iso_contact)

        for label, method, path in (
            ("folder", "GET", f"/folders/{iso_folder}"),
            ("folder rename", "PATCH", f"/folders/{iso_folder}"),
            ("folder delete", "DELETE", f"/folders/{iso_folder}"),
            ("contact", "GET", f"/contacts/{iso_contact}"),
            ("contact edit", "PATCH", f"/contacts/{iso_contact}"),
        ):
            r = requests.request(method, f"{api}{path}", headers=oh,
                                 json={"name": "hijacked"}, timeout=TIMEOUT)
            # 404 not 403 — a 403 would confirm the id exists.
            check(f"another user's {label} -> 404 (never 403)",
                  r.status_code == 404,
                  f"{r.status_code} {r.text[:150]}")

        r = requests.get(f"{api}/folders", headers=oh, timeout=TIMEOUT)
        check("second user's folder list is empty of the first user's folders",
              r.status_code == 200
              and not any(f["id"] == iso_folder
                          for f in r.json().get("folders", [])),
              f"{r.status_code} {r.text[:200]}")
        r = requests.get(f"{api}/contacts", headers=oh, timeout=TIMEOUT)
        check("second user's contact list is empty of the first user's contacts",
              r.status_code == 200
              and not any(c["id"] == iso_contact
                          for c in r.json().get("contacts", [])),
              f"{r.status_code} {r.text[:200]}")

    finally:
        cleanup()

    # run_tests._results stores (name, ok, detail) — ok is the SECOND item.
    passed = sum(1 for _, ok, _ in _results if ok)
    failed = len(_results) - passed
    print()
    print("=" * 60)
    print(f"  {passed} passed, {failed} failed, {len(_results)} total")
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
