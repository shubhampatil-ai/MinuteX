#!/usr/bin/env python3
# =============================================================
# test_ai_api.py — LIVE end-to-end check of the AI Meeting Workspace API.
#
# The offline unit suite is tests/test_ai_workspace.py (104 tests, no network).
# This file is its live counterpart: it proves the deployed routes, the API
# Gateway route templates and the real Groq calls actually work together —
# which is exactly what unit tests with a stubbed Groq cannot tell you.
#
# Checks:
#   1.  GET  .../documents        route reachable, advertises all 8 types
#   2.  POST .../highlights       real Groq -> six structured sections
#   3.  POST .../highlights again -> cached:true (no second Groq spend)
#   4.  POST .../documents        real Groq -> Markdown minutes
#   5.  POST .../documents again  -> cached:true
#   6.  POST .../quick            aliased action reuses the document cache
#   7.  POST .../quick            standalone extraction generates
#   8.  PATCH .../documents       edit is stored and marked edited
#   9.  POST .../documents        regenerate=false does NOT clobber the edit
#   10. POST .../chat             real Groq answers from the meeting
#   11. GET  .../chat             history persisted + suggestions advertised
#   12. DELETE .../chat           clears the thread
#   13. auth: no token -> 401
#   14. isolation: another user's recording -> 404 (never 403)
#   15. bad input: unknown document type / empty chat message -> 400
#
# COSTS REAL GROQ QUOTA. Roughly 5-6 completions per run, which on the free
# 12k-TPM tier means it can 429 if run back to back — that is the rate limit
# working, not a failure. Re-run after a minute.
#
# Requires: API_URL (from .env) and a recording that has finished transcribing.
#   python tests/test_ai_api.py --live
#   python tests/test_ai_api.py --live --key "recordings/<uid>/mobile/<id>.m4a"
#
# Without --key it picks the newest `complete` recording the test user owns.
# With no such recording it says so and exits 0 rather than reporting failures
# that are really "nothing to test against".
# =============================================================
import json
import os
import sys
import time
import urllib.parse
import uuid
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_tests import check, load_dotenv, _results  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

# Generous: a cold Lambda plus a real Groq completion on a long transcript.
# Below API Gateway's own 29s cut-off there is no point waiting longer.
TIMEOUT = 40


def ai_url(api_url: str, action: str, key: str) -> str:
    """Build an AI route URL.

    The action comes BEFORE the key and the key is percent-encoded whole
    (including its slashes): API Gateway only allows a greedy path variable in
    the FINAL position, so the route is /recordings/ai/{action}/{key+}.
    """
    return f"{api_url}/recordings/ai/{action}/{urllib.parse.quote(key, safe='')}"


def hdrs(token: str) -> dict:
    return {"Authorization": f"Bearer {token}",
            "Content-Type": "application/json"}


def pick_recording(api_url: str, token: str) -> dict | None:
    """The newest recording with a transcript the AI can work from."""
    r = requests.get(f"{api_url}/recordings", headers=hdrs(token), timeout=TIMEOUT)
    if r.status_code != 200:
        print(f"  ! GET /recordings -> {r.status_code}: {r.text[:200]}")
        return None
    rows = r.json().get("recordings", [])
    ready = [x for x in rows if x.get("status") in ("complete", "transcribed")]
    return ready[0] if ready else None


def main() -> int:
    if "--live" not in sys.argv:
        print("This test hits the REAL API and spends Groq quota.")
        print("Run it with --live:   python tests/test_ai_api.py --live")
        return 0

    load_dotenv(ROOT / ".env")
    api_url = os.environ.get("API_URL", "").rstrip("/")
    email = os.environ.get("TEST_EMAIL", "")
    password = os.environ.get("TEST_PASSWORD", "")

    if not api_url:
        print("ERROR: API_URL not set (put it in .env)")
        return 2

    # --- auth -------------------------------------------------
    # Prefer a real account (it owns real recordings). Fall back to an
    # ephemeral signup, which can still exercise auth/validation but will have
    # no recordings to generate from.
    token = ""
    if email and password:
        r = requests.post(f"{api_url}/login",
                          json={"email": email, "password": password},
                          timeout=TIMEOUT)
        if r.status_code == 200:
            token = r.json()["token"]
            print(f"Logged in as {email}")
        else:
            print(f"  ! login failed ({r.status_code}) — falling back to signup")
    if not token:
        tmp_email = f"aitest-{uuid.uuid4().hex[:8]}@example.com"
        r = requests.post(f"{api_url}/signup",
                          json={"email": tmp_email, "password": "Test-1234!"},
                          timeout=TIMEOUT)
        if r.status_code not in (200, 201):
            print(f"ERROR: signup failed {r.status_code}: {r.text[:200]}")
            return 2
        token = r.json()["token"]
        print(f"Signed up ephemeral user {tmp_email}")
        print("NOTE: an ephemeral user owns no recordings — set TEST_EMAIL /"
              " TEST_PASSWORD in .env to exercise the generation paths.")

    print(f"API: {api_url}")
    print("-" * 60)

    # --- pick a recording -------------------------------------
    key = ""
    if "--key" in sys.argv:
        key = sys.argv[sys.argv.index("--key") + 1]
    else:
        rec = pick_recording(api_url, token)
        if rec:
            key = rec.get("audio_s3_key", "")
            print(f"Testing against: {key}")
            print(f"  title={rec.get('title')!r} status={rec.get('status')}")

    if not key:
        print("\nNo transcribed recording available for this user — the")
        print("generation checks need one. Upload and process a recording, or")
        print("pass --key. Running only the auth/validation checks.\n")

    # ===== auth + isolation (no recording needed) =============
    probe = key or "recordings/nobody/mobile/none_0.m4a"

    try:
        r = requests.get(ai_url(api_url, "documents", probe), timeout=TIMEOUT)
        check("13. no token -> 401", r.status_code == 401, f"got {r.status_code}")
    except Exception as e:
        check("13. no token -> 401", False, f"exception: {e}")

    try:
        # A key that certainly isn't ours must be 404, never 403 — a 403 would
        # confirm the recording exists.
        alien = "recordings/00000000-0000-0000-0000-000000000000/mobile/x_1.m4a"
        r = requests.get(ai_url(api_url, "documents", alien),
                         headers=hdrs(token), timeout=TIMEOUT)
        check("14. another user's recording -> 404 (not 403)",
              r.status_code == 404, f"got {r.status_code}")
    except Exception as e:
        check("14. another user's recording -> 404", False, f"exception: {e}")

    if not key:
        return summarize()

    # ===== 1. documents list ==================================
    try:
        r = requests.get(ai_url(api_url, "documents", key),
                         headers=hdrs(token), timeout=TIMEOUT)
        body = r.json() if r.status_code == 200 else {}
        available = body.get("available", [])
        check("1. GET documents advertises all 8 types",
              r.status_code == 200 and len(available) == 8,
              f"status={r.status_code} count={len(available)}")
    except Exception as e:
        check("1. GET documents advertises all 8 types", False, f"exception: {e}")

    # ===== 2/3. highlights (generate, then cached) ============
    try:
        t0 = time.time()
        r = requests.post(ai_url(api_url, "highlights", key),
                          headers=hdrs(token), json={}, timeout=TIMEOUT)
        body = r.json() if r.status_code == 200 else {}
        hl = body.get("meeting_highlights", {})
        sections = ("decisions", "action_items", "deadlines",
                    "important_numbers", "open_questions", "risks")
        shaped = isinstance(hl, dict) and all(
            isinstance(hl.get(s), list) for s in sections)
        check("2. POST highlights -> six structured sections",
              r.status_code == 200 and shaped,
              f"status={r.status_code} {round(time.time() - t0, 1)}s "
              + ", ".join(f"{len(hl.get(s, []))} {s}" for s in sections))
    except Exception as e:
        check("2. POST highlights -> six structured sections", False,
              f"exception: {e}")

    try:
        r = requests.post(ai_url(api_url, "highlights", key),
                          headers=hdrs(token), json={}, timeout=TIMEOUT)
        cached = r.json().get("cached") if r.status_code == 200 else None
        # cached=False here is legitimate when the meeting genuinely has nothing
        # to highlight (an all-empty result is deliberately NOT stored, so the
        # cache can't serve emptiness forever).
        check("3. POST highlights again -> cached",
              r.status_code == 200 and cached is True,
              f"status={r.status_code} cached={cached} "
              "(cached=False is OK for an empty-highlights meeting)")
    except Exception as e:
        check("3. POST highlights again -> cached", False, f"exception: {e}")

    # ===== 4/5. a document (generate, then cached) ============
    try:
        t0 = time.time()
        r = requests.post(ai_url(api_url, "documents", key),
                          headers=hdrs(token),
                          json={"type": "minutes_of_meeting"}, timeout=TIMEOUT)
        body = r.json() if r.status_code == 200 else {}
        doc = body.get("document", {})
        content = doc.get("content", "")
        check("4. POST documents -> Markdown minutes",
              r.status_code == 200 and len(content) > 100
              and doc.get("format") == "markdown",
              f"status={r.status_code} {round(time.time() - t0, 1)}s "
              f"{len(content)} chars cached={body.get('cached')}")
    except Exception as e:
        check("4. POST documents -> Markdown minutes", False, f"exception: {e}")

    try:
        r = requests.post(ai_url(api_url, "documents", key),
                          headers=hdrs(token),
                          json={"type": "minutes_of_meeting"}, timeout=TIMEOUT)
        check("5. POST documents again -> cached (no Groq spend)",
              r.status_code == 200 and r.json().get("cached") is True,
              f"status={r.status_code} cached={r.json().get('cached')}")
    except Exception as e:
        check("5. POST documents again -> cached", False, f"exception: {e}")

    # ===== 6. an aliased Quick action shares that cache ======
    try:
        r = requests.post(ai_url(api_url, "quick", key), headers=hdrs(token),
                          json={"action": "minutes_of_meeting"}, timeout=TIMEOUT)
        body = r.json() if r.status_code == 200 else {}
        check("6. quick minutes reuses the document cache",
              r.status_code == 200 and body.get("cached") is True
              and body.get("document", {}).get("type") == "minutes_of_meeting",
              f"status={r.status_code} cached={body.get('cached')}")
    except Exception as e:
        check("6. quick minutes reuses the document cache", False,
              f"exception: {e}")

    # ===== 7. a standalone Quick extraction =================
    try:
        t0 = time.time()
        r = requests.post(ai_url(api_url, "quick", key), headers=hdrs(token),
                          json={"action": "decisions"}, timeout=TIMEOUT)
        body = r.json() if r.status_code == 200 else {}
        content = body.get("document", {}).get("content", "")
        check("7. quick 'decisions' generates",
              r.status_code == 200 and len(content) > 10,
              f"status={r.status_code} {round(time.time() - t0, 1)}s "
              f"{len(content)} chars")
    except Exception as e:
        check("7. quick 'decisions' generates", False, f"exception: {e}")

    # ===== 8/9. edit is stored AND survives a cache read =====
    marker = f"EDITED-{uuid.uuid4().hex[:6]}"
    try:
        r = requests.patch(ai_url(api_url, "documents", key), headers=hdrs(token),
                           json={"type": "minutes_of_meeting",
                                 "content": f"## My own minutes\n\n{marker}"},
                           timeout=TIMEOUT)
        doc = r.json().get("document", {}) if r.status_code == 200 else {}
        check("8. PATCH documents stores the edit and marks it",
              r.status_code == 200 and doc.get("edited") is True
              and marker in doc.get("content", ""),
              f"status={r.status_code} edited={doc.get('edited')}")
    except Exception as e:
        check("8. PATCH documents stores the edit", False, f"exception: {e}")

    try:
        # A plain (non-regenerate) request must serve the EDIT, not overwrite
        # it — the user's own text is never silently replaced.
        r = requests.post(ai_url(api_url, "documents", key), headers=hdrs(token),
                          json={"type": "minutes_of_meeting"}, timeout=TIMEOUT)
        content = r.json().get("document", {}).get("content", "")
        check("9. a normal request never clobbers a user edit",
              r.status_code == 200 and marker in content,
              f"status={r.status_code} edit_preserved={marker in content}")
    except Exception as e:
        check("9. a normal request never clobbers a user edit", False,
              f"exception: {e}")

    # ===== 10/11/12. chat ====================================
    try:
        t0 = time.time()
        r = requests.post(ai_url(api_url, "chat", key), headers=hdrs(token),
                          json={"message": "In one sentence, what was this "
                                           "meeting about?"}, timeout=TIMEOUT)
        body = r.json() if r.status_code == 200 else {}
        reply = body.get("reply", "")
        check("10. POST chat -> an answer from the meeting",
              r.status_code == 200 and len(reply) > 10,
              f"status={r.status_code} {round(time.time() - t0, 1)}s "
              f"reply={reply[:80]!r}")
    except Exception as e:
        check("10. POST chat -> an answer", False, f"exception: {e}")

    try:
        r = requests.get(ai_url(api_url, "chat", key), headers=hdrs(token),
                         timeout=TIMEOUT)
        body = r.json() if r.status_code == 200 else {}
        turns = body.get("chat_history", [])
        groups = {g.get("group") for g in body.get("suggestions", [])}
        check("11. GET chat -> history persisted + suggestions",
              r.status_code == 200 and len(turns) >= 2
              and {"Meeting", "Business", "Sales"} <= groups,
              f"status={r.status_code} turns={len(turns)} groups={sorted(groups)}")
    except Exception as e:
        check("11. GET chat -> history + suggestions", False, f"exception: {e}")

    try:
        r = requests.delete(ai_url(api_url, "chat", key), headers=hdrs(token),
                            timeout=TIMEOUT)
        ok = r.status_code == 200 and r.json().get("cleared") is True
        after = requests.get(ai_url(api_url, "chat", key), headers=hdrs(token),
                             timeout=TIMEOUT)
        emptied = after.status_code == 200 and not after.json().get("chat_history")
        check("12. DELETE chat clears the thread", ok and emptied,
              f"status={r.status_code} emptied={emptied}")
    except Exception as e:
        check("12. DELETE chat clears the thread", False, f"exception: {e}")

    # ===== 15. input validation =============================
    try:
        r1 = requests.post(ai_url(api_url, "documents", key), headers=hdrs(token),
                           json={"type": "not_a_document"}, timeout=TIMEOUT)
        r2 = requests.post(ai_url(api_url, "chat", key), headers=hdrs(token),
                           json={"message": "   "}, timeout=TIMEOUT)
        r3 = requests.post(ai_url(api_url, "quick", key), headers=hdrs(token),
                           json={"action": "nope"}, timeout=TIMEOUT)
        check("15. bad input -> 400 on all three",
              r1.status_code == 400 and r2.status_code == 400
              and r3.status_code == 400,
              f"doc={r1.status_code} chat={r2.status_code} quick={r3.status_code}")
    except Exception as e:
        check("15. bad input -> 400", False, f"exception: {e}")

    return summarize()


def summarize() -> int:
    print("-" * 60)
    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print(f"RESULT: {passed}/{total} passed")
    if passed != total:
        print("\nA 502 on a generation check usually means Groq rate-limited "
              "the account (12k TPM on the free tier). Wait a minute and "
              "re-run before treating it as a real failure.")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
