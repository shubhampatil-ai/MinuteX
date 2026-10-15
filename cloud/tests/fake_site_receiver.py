"""fake_site_receiver.py — an in-memory Salesforce Site receiver, for tests.

Plugs into site_delivery_client.SiteDeliveryClient as its TRANSPORT, so the
real client code (headers, signing, encoding, response classification) runs
unmodified; only the network is replaced.

It mirrors the Apex receiver in salesforce/minutex-site (MinuteXSiteReceiver /
MinuteXSiteMeetings / MinuteXSitePeople) closely enough that the offline suite
can exercise the whole MinuteX -> receiver contract: idempotency replay,
create-then-update keyed on the meeting id, stale-payload protection, the
131,072-character transcript limit counted in UTF-16 units, https-only audio
URLs, and cursor-paged people. The Apex class's own test suite
(MinuteXSiteReceiverTest) is what pins the real receiver to the same rules.

FAULT INJECTION: `faults` is a list consumed one per request:
  "timeout"               raise TransportTimeout BEFORE processing
  "timeout_after_commit"  PROCESS the request, then raise TransportTimeout —
                          the "Salesforce committed but MinuteX never heard"
                          case that idempotency exists for
  "network"               raise TransportError
  ("status", 503)         answer with that HTTP status and an error body
  ("html", 200)           answer with an HTML page (a Site login redirect page)
  None                    behave normally
"""
import hashlib
import hmac
import json

import site_delivery_client as sdc

MAX_TRANSCRIPT = 131072


def _utf16(text):
    return len(str(text or "").encode("utf-16-le")) // 2


class FakeSiteReceiver:
    def __init__(self, *, secret=None, contacts=None, users=None):
        self.secret = secret
        self.meetings = {}          # meeting_id -> record dict
        self.requests = {}          # idempotency_key -> stored result
        self.contacts = list(contacts or [])
        self.users = list(users or [])
        self.faults = []
        self.calls = []             # every parsed envelope received
        self.headers_seen = []
        self.enabled = True

    # -- transport interface ----------------------------------------------
    def post(self, url, body, headers, timeout):
        fault = self.faults.pop(0) if self.faults else None
        self.headers_seen.append(dict(headers))
        if fault == "timeout":
            raise sdc.TransportTimeout("timed out")
        if fault == "network":
            raise sdc.TransportError("connection reset")
        if isinstance(fault, tuple) and fault[0] == "status":
            return fault[1], json.dumps({"ok": False, "error": {
                "code": "injected", "message": f"injected {fault[1]}"}}).encode()
        if isinstance(fault, tuple) and fault[0] == "html":
            return fault[1], b"<html><body>Please log in</body></html>"

        status, reply = self._handle(body, headers)
        if fault == "timeout_after_commit":
            raise sdc.TransportTimeout("timed out after commit")
        return status, json.dumps(reply).encode("utf-8")

    # -- receiver ------------------------------------------------------------
    def _error(self, status, action, request_id, code, message, field=""):
        return status, {"ok": False, "action": action, "request_id": request_id,
                        "error": {"code": code, "message": message, "field": field}}

    def _handle(self, body, headers):
        if not self.enabled:
            return self._error(403, "", "", "receiver_disabled", "disabled")
        raw = body.decode("utf-8")
        if self.secret is not None:
            ts = headers.get("X-MinuteX-Timestamp", "")
            sig = headers.get("X-MinuteX-Signature", "")
            want = "v1=" + hmac.new(self.secret.encode(),
                                    f"{ts}.{raw}".encode(),
                                    hashlib.sha256).hexdigest()
            if not hmac.compare_digest(want, sig):
                return self._error(401, "", "", "auth_failed", "bad signature")
        try:
            env = json.loads(raw)
        except ValueError:
            return self._error(400, "", "", "malformed_json", "bad json")
        self.calls.append(env)
        action = env.get("action", "")
        request_id = env.get("request_id", "")
        if action == "health":
            return 200, self._ok(action, request_id, {
                "status": "ok", "receiver_version": "1.0.0",
                "auth_mode": "hmac_sha256" if self.secret else "none",
                "max_transcript_chars": MAX_TRANSCRIPT})
        if action == "upsert_meeting":
            return self._upsert(env, request_id)
        if action == "people_sync":
            return self._people(env, request_id)
        return self._error(400, action, request_id, "unsupported_action", "no")

    def _ok(self, action, request_id, result, duplicate=False):
        return {"ok": True, "action": action, "request_id": request_id,
                "duplicate": duplicate, "receiver_version": "1.0.0",
                "result": result}

    def _upsert(self, env, request_id):
        key = env.get("idempotency_key") or ""
        if not key:
            return self._error(400, "upsert_meeting", request_id,
                               "invalid_request", "idempotency_key required",
                               "idempotency_key")
        payload = env.get("payload") or {}
        meeting = payload.get("meeting") or {}
        meeting_id = meeting.get("id") or ""
        if not meeting_id:
            return self._error(422, "upsert_meeting", request_id,
                               "validation_failed", "id required",
                               "payload.meeting.id")
        text = (payload.get("transcript") or {}).get("text") or ""
        if _utf16(text) > MAX_TRANSCRIPT:
            return self._error(422, "upsert_meeting", request_id,
                               "transcript_too_large", "too large",
                               "payload.transcript.text")
        audio_url = (payload.get("audio") or {}).get("url") or ""
        if audio_url and not audio_url.startswith("https://"):
            return self._error(422, "upsert_meeting", request_id,
                               "invalid_audio_url", "https only",
                               "payload.audio.url")
        if key in self.requests:
            return 200, self._ok("upsert_meeting", request_id,
                                 dict(self.requests[key]), duplicate=True)

        sent_at = env.get("sent_at") or ""
        existing = self.meetings.get(meeting_id)
        if existing is None:
            record_id = f"a0X{len(self.meetings):015d}"
            self.meetings[meeting_id] = {"record_id": record_id, "payload": payload,
                                         "sent_at": sent_at, "deliveries": 1}
            operation = "created"
        elif sent_at and existing["sent_at"] and sent_at < existing["sent_at"]:
            record_id, operation = existing["record_id"], "stale_ignored"
        else:
            existing.update(payload=payload, sent_at=sent_at,
                            deliveries=existing["deliveries"] + 1)
            record_id, operation = existing["record_id"], "updated"
        result = {"operation": operation, "meeting_id": meeting_id,
                  "record_id": record_id}
        self.requests[key] = result
        return 200, self._ok("upsert_meeting", request_id, dict(result))

    def _people(self, env, request_id):
        payload = env.get("payload") or {}
        size = int(payload.get("page_size") or 200)
        cursor = payload.get("cursor") or ""
        phase, _, after = cursor.partition(":") if cursor else ("contact", "", "")
        if phase not in ("contact", "user"):
            return self._error(400, "people_sync", request_id, "invalid_cursor",
                               "bad cursor", "payload.cursor")
        people, next_cursor = [], ""

        def page(rows, after_id, n):
            rows = sorted(rows, key=lambda r: r["external_id"])
            return [r for r in rows if not after_id or r["external_id"] > after_id][:n]

        if phase == "contact":
            chunk = page(self.contacts, after, size)
            people.extend(chunk)
            if len(chunk) == size:
                next_cursor = "contact:" + chunk[-1]["external_id"]
            else:
                phase, after = "user", ""
        if phase == "user" and not next_cursor:
            remaining = size - len(people)
            if remaining > 0:
                chunk = page(self.users, after, remaining)
                people.extend(chunk)
                if len(chunk) == remaining:
                    next_cursor = "user:" + chunk[-1]["external_id"]
            else:
                next_cursor = "user:"
        return 200, self._ok("people_sync", request_id, {
            "people": people, "count": len(people),
            "next_cursor": next_cursor, "done": not next_cursor})


def contact(external_id, name, email="", company="Acme"):
    return {"provider": "salesforce", "type": "contact",
            "external_id": external_id, "name": name, "email": email,
            "phone": "", "title": "", "company": company, "status": "active"}


def user(external_id, name, email="", active=True):
    return {"provider": "salesforce", "type": "user",
            "external_id": external_id, "name": name, "email": email,
            "phone": "", "title": "Sales", "company": "",
            "status": "active" if active else "inactive"}
