"""fake_webhook_receiver.py — an in-memory Webhook Contract v2 endpoint.

Plugs into webhook_client.WebhookClient as its TRANSPORT, so the real client
(URL validation, encoding, signing, response classification) runs
unmodified; only the network is replaced. It follows the RECEIVER rules of
docs/webhook-contract-v2.md, the same rules the Apex reference receiver
(salesforce/minutex-webhook) is tested against:

  * the body `type` must match the endpoint path;
  * any v1= signature matching any accepted secret, within the skew;
  * event_id dedupe ONLY for events processed successfully (a failed
    handler run is processed again on retry);
  * sequence: drop <= the highest APPLIED sequence per meeting; gaps fine;
  * created and updated are both upserts on meeting_id.

`log` records every delivered (meeting_id, sequence, event_id,
content_hash) so tests can assert that a sequence is never bound to two
different snapshots.

FAULT INJECTION: `faults` is consumed one per request:
  "timeout"               raise TransportTimeout BEFORE processing
  "timeout_after_commit"  process, then raise TransportTimeout
  "network"               raise TransportError
  "handler_error"         the handler fails: 500, NOT recorded as processed
  ("status", N[, retry_after])  answer HTTP N
"""
import hashlib
import json

import webhook_client as whc


def contact(external_id, name, email="", status="active"):
    return {"provider": "salesforce", "type": "contact", "external_id": external_id,
            "name": name, "email": email, "status": status}


def lead(external_id, name, email="", status="active"):
    return {"provider": "salesforce", "type": "lead", "external_id": external_id,
            "name": name, "email": email, "status": status}


def user(external_id, name, email="", status="active"):
    return {"provider": "salesforce", "type": "user", "external_id": external_id,
            "name": name, "email": email, "status": status}


class FakeWebhookReceiver:
    def __init__(self, *, secrets=None, clock=None, meeting_types=None,
                 records=None, people=None, configuration_version="1"):
        self.secrets = list(secrets or [])
        self.clock = clock
        self.configuration_version = configuration_version
        self.meeting_types = list(meeting_types if meeting_types is not None else [
            {"id": "site_visit", "name": "Site Visit",
             "record_selection": {"enabled": True, "required": True,
                                  "label": "Property"}},
            {"id": "internal_meeting", "name": "Internal Meeting",
             "record_selection": {"enabled": False, "required": False}},
            {"id": "customer_call", "name": "Customer Call",
             "record_selection": {"enabled": True, "required": False}},
        ])
        self.records = list(records if records is not None else [
            {"id": "a0P123", "display_name": "PV-2026-00452 — Mumbai Heights",
             "secondary_text": "Property · Pune", "object_type": "Property__c"},
            {"id": "a0P124", "display_name": "PV-2026-00453 — Pune Greens",
             "secondary_text": "Property · Pune", "object_type": "Property__c"},
        ])
        self.people = list(people or [])
        self.faults = []
        self.requests = []          # (path, parsed body, headers)
        self.meetings = {}          # meeting_id -> latest applied envelope
        self.applied_sequence = {}  # meeting_id -> highest applied sequence
        self.processed = {}         # event_id -> {"status": ...}
        self.handler_runs = []      # event_ids the "handler" ran for
        self.log = []               # (meeting_id, sequence, event_id, content_hash)
        self.enabled = True
        # Live people search: False answers 404 like a receiver that doesn't
        # have the route yet. `hidden_from` maps a user email to external ids
        # that user may not see (the receiver's record-access check).
        self.people_search_supported = True
        self.hidden_from = {}
        # record id -> people "on" that record, for /people/related.
        self.related = {}
        # Linking (Contract v3): None = this receiver doesn't support it.
        self.linkable_objects = None
        # /files: key -> {kind, sha256, filename, size_bytes, content}
        self.files = {}
        self.file_fault = None     # "status" to answer 503 to /files
        # Called once with the parsed event, BEFORE it is processed: how a
        # test makes a change land "while the webhook is being delivered".
        self.before_event = None

    # -- helpers for tests -------------------------------------------------
    @property
    def events(self):
        return [b for p, b, _h in self.requests if p.endswith("/events")]

    # -- transport interface ---------------------------------------------
    def post(self, url, body, headers, timeout, max_bytes):
        fault = self.faults.pop(0) if self.faults else None
        path = "/" + url.split("/", 3)[3] if url.count("/") >= 3 else "/"
        parsed = json.loads(body.decode("utf-8"))
        self.requests.append((path, parsed, dict(headers)))
        if path.endswith("/events") and self.before_event is not None:
            callback, self.before_event = self.before_event, None
            callback(parsed)
        if fault == "timeout":
            raise whc.TransportTimeout("timed out")
        if fault == "network":
            raise whc.TransportError("connection reset")
        if isinstance(fault, tuple) and fault[0] == "status":
            hdrs = {"retry-after": str(fault[2])} if len(fault) > 2 else {}
            return fault[1], json.dumps({"ok": False, "error": {
                "code": "injected", "message": f"injected {fault[1]}"}}).encode(), \
                hdrs, False
        status, reply = self._handle(path, body, parsed, headers, fault)
        if fault == "timeout_after_commit":
            raise whc.TransportTimeout("timed out after commit")
        raw = json.dumps(reply).encode("utf-8")
        return status, raw[:max_bytes], {}, len(raw) > max_bytes

    # -- receiver --------------------------------------------------------
    def _handle(self, path, body, parsed, headers, fault):
        if not self.enabled:
            return 403, {"ok": False, "error": {"code": "disabled", "message": "off"}}
        if self.secrets:
            now = self.clock() if self.clock else None
            if not whc.verify_signature(headers.get("X-MinuteX-Signature"), body,
                                        headers.get("X-MinuteX-Timestamp"),
                                        self.secrets, now=now):
                return 401, {"ok": False, "error": {"code": "bad_signature",
                                                    "message": "bad signature"}}
        expected = {"/events": "event", "/configuration": "configuration.request",
                    "/records/search": "records.search", "/people": "people.sync",
                    "/people/search": "people.search",
                    "/people/related": "people.related", "/files": "file.upload"}
        suffix = next((s for s in expected if path.endswith(s)), None)
        if suffix is None or parsed.get("type") != expected[suffix]:
            return 400, {"ok": False, "error": {"code": "type_mismatch",
                                                "message": "wrong endpoint"}}
        if suffix == "/configuration":
            body = {"contract_version": "2.0",
                    "configuration_version": self.configuration_version,
                    "meeting_types": self.meeting_types}
            if self.linkable_objects is not None:
                body["linkable_objects"] = self.linkable_objects
            return 200, body
        if suffix == "/records/search":
            q = str(parsed.get("query") or "").lower()
            objects = parsed.get("objects")
            hits = [r for r in self.records
                    if (q in r["display_name"].lower() or q == r["id"].lower())
                    and (objects is None or r.get("object_type") in objects)]
            return 200, {"records": hits[: int(parsed.get("limit") or 10)],
                         "next_cursor": None}
        if suffix == "/files":
            if self.file_fault:
                return 503, {"ok": False, "error": {"code": "busy", "message": "try later"}}
            import base64
            content = base64.b64decode(parsed["content_base64"])
            if hashlib.sha256(content).hexdigest() != parsed["sha256"]:
                return 400, {"ok": False, "error": {"code": "hash_mismatch", "message": "bad hash"}}
            self.files[parsed["key"]] = {"kind": parsed["kind"], "sha256": parsed["sha256"],
                                         "filename": parsed["filename"],
                                         "size_bytes": parsed["size_bytes"], "content": content}
            return 200, {"ok": True, "content_document_id": "069" + "0" * 15}
        if suffix == "/people/related":
            if not self.people_search_supported:
                return 404, {"ok": False, "error": {"code": "not_found",
                                                    "message": "unknown route"}}
            ids = set(parsed.get("record_ids") or [])
            hits = [{**p, "relation": "Contact at " + rid}
                    for rid, people in self.related.items() if rid in ids
                    for p in people]
            return 200, {"people": hits[: int(parsed.get("limit") or 50)]}
        if suffix == "/people/search":
            if not self.people_search_supported:
                return 404, {"ok": False, "error": {"code": "not_found",
                                                    "message": "unknown route"}}
            q = str(parsed.get("query") or "").lower()
            types = set(parsed.get("types") or [])
            hidden = set(self.hidden_from.get(parsed.get("user_email") or "", []))
            hits = [p for p in self.people
                    if p.get("type") in types and p["external_id"] not in hidden
                    and (q in str(p.get("name", "")).lower()
                         or q in str(p.get("email", "")).lower())]
            return 200, {"people": hits[: int(parsed.get("limit") or 20)]}
        if suffix == "/people":
            size = int(parsed.get("page_size") or 200)
            start = int(parsed.get("cursor") or 0)
            page = self.people[start:start + size]
            nxt = start + size
            done = nxt >= len(self.people)
            return 200, {"people": page, "next_cursor": "" if done else str(nxt),
                         "done": done}
        return self._event(parsed, fault)

    def _event(self, env, fault):
        eid = env["event_id"]
        mid = env["meeting_id"]
        seq = int(env["sequence"])
        # The snapshot's content, minus audio (its signed URL changes on every
        # build) — what a sequence number must stay bound to.
        data = dict(env.get("data") or {})
        data.pop("audio", None)
        content = hashlib.sha256(json.dumps(
            {"meeting_type": env.get("meeting_type"),
             "record_context": env.get("record_context"), "data": data},
            sort_keys=True, default=str).encode()).hexdigest()
        self.log.append((mid, seq, eid, content))
        prior = self.processed.get(eid)
        if prior and prior["status"] in ("PROCESSED", "SKIPPED_STALE"):
            return 200, {"ok": True, "duplicate": True}
        if seq <= self.applied_sequence.get(mid, 0):
            self.processed[eid] = {"status": "SKIPPED_STALE"}
            return 200, {"ok": True, "skipped": "stale"}
        self.handler_runs.append(eid)
        if fault == "handler_error":
            self.processed[eid] = {"status": "FAILED"}
            return 500, {"ok": False, "error": {"code": "handler_failed",
                                                "message": "handler threw"}}
        if env["event"] == "meeting.deleted":
            self.meetings.pop(mid, None)
        else:
            self.meetings[mid] = env
        self.applied_sequence[mid] = seq
        self.processed[eid] = {"status": "PROCESSED"}
        return 200, {"ok": True, "result": {"record_id": "a0S" + mid[-12:],
                                            "url": "https://acme.example/r/1"}}
