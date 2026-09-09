#!/usr/bin/env python3
# =============================================================
# test_async_stt.py — the asynchronous ElevenLabs STT flow, the unified Groq
# analysis, and the raised upload limits.
#
# OFFLINE by design, same contract as test_ai_workspace.py: no AWS, no
# ElevenLabs, no Groq, no network, no credentials. Every boundary is stubbed, so
# this runs on a laptop with no .env and in about a second.
#
# WHAT IS COVERED (and why each one is here rather than left to production)
#
#   STT start
#     * webhook=true is actually sent, with source_url — the whole point of the
#       change is that we no longer hold a socket open for the transcription.
#     * request_id is persisted BEFORE the Lambda exits. Without that write the
#       job is untrackable and a lost webhook is unrecoverable.
#     * webhook_metadata carries the DynamoDB partition key, the bucket and a
#       version, as a JSON string.
#     * A rejected job still marks the row failed (a queue failure is real).
#     * The handler never blocks on a transcript.
#
#   Webhook
#     * A correct signature is accepted; a wrong one, a missing one, a replayed
#       timestamp and an unconfigured secret are all rejected. This endpoint is
#       PUBLIC, so these are the tests that keep a forged transcript out.
#     * Envelope validation: version and bucket.
#     * Staleness: an older job's late delivery must not overwrite a newer one.
#     * Idempotency: a retried delivery must not run the analysis twice.
#     * The transcript's text, timestamps, speakers and language survive intact.
#     * Analysis is invoked exactly once, asynchronously.
#
#   Unified analysis
#     * ONE Groq call produces title + summary + highlights + tasks +
#       participants + meeting_highlights + crm_identifiers. The transcript is
#       sent ONCE, not three times.
#     * Participants come from the transcript's diarization; a task assignee who
#       never spoke is NOT promoted to a participant.
#     * A transcript within the single-pass budget takes the single-pass path;
#       one that exceeds it falls back to map-reduce, which is the only way
#       SUMMARY_REDUCE_SYSTEM is reachable.
#
#   Limits
#     * The 3 GB / 10 h boundaries, on both sides, including the exact values.
#
# Run:  python tests/test_async_stt.py
# =============================================================
import hashlib
import hmac
import importlib.util as _ilu
import json
import sys
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "functions/userapi"))

# ---------------------------------------------------------------------------
# Stub boto3/botocore BEFORE importing either Lambda: both build clients and
# table resources at import time, and a real boto3 would go looking for
# credentials (and a region) the moment it is touched.
# ---------------------------------------------------------------------------
_fake_boto3 = mock.MagicMock()
sys.modules["boto3"] = _fake_boto3
sys.modules["boto3.dynamodb"] = mock.MagicMock()
_conditions = mock.MagicMock()


class _Key:
    def __init__(self, name):
        self.name = name

    def eq(self, value):
        return (self.name, "eq", value)


_conditions.Key = _Key
sys.modules["boto3.dynamodb.conditions"] = _conditions


class _ClientError(Exception):
    """Faithful stand-in for botocore.exceptions.ClientError.

    `response` must be a real dict: the webhook's idempotency claim branches on
    response["Error"]["Code"] == "ConditionalCheckFailedException", and a
    MagicMock would make that comparison silently falsy — the test would pass
    while production treated a duplicate delivery as a hard failure.
    """

    def __init__(self, response=None, operation_name=""):
        self.response = response or {"Error": {"Code": "Unknown"}}
        err = self.response.get("Error", {})
        super().__init__(
            f"An error occurred ({err.get('Code', 'Unknown')}) when calling "
            f"the {operation_name} operation: {err.get('Message', '')}")


_botocore_exc = mock.MagicMock()
# Adopt an already-installed ClientError when there is one (conftest.py
# installs the canonical stub under pytest). Overwriting it here would
# give this file a ClientError that the Lambda's `except ClientError`
# cannot catch, which is precisely the cross-file collision conftest.py
# exists to prevent. Standalone runs still install this file's own.
_installed = sys.modules.get("botocore.exceptions")
if _installed is not None and getattr(_installed, "ClientError", None):
    _ClientError = _installed.ClientError
    _botocore_exc = _installed
else:
    _botocore_exc.ClientError = _ClientError
    sys.modules["botocore"] = mock.MagicMock()
    sys.modules["botocore.exceptions"] = _botocore_exc
sys.modules["botocore.config"] = mock.MagicMock()

import ai_schema          # noqa: E402
import groq_client        # noqa: E402
import prompts            # noqa: E402
import stt_result         # noqa: E402
import lambda_function as api  # noqa: E402  (functions/userapi)

# Both Lambdas' modules are named lambda_function.py, so the transcribe one is
# loaded under a distinct name rather than shadowing userApi in sys.modules.
_spec = _ilu.spec_from_file_location(
    "transcribe_lambda_function",
    str(ROOT / "functions/transcribe" / "lambda_function.py"))
transcribe = _ilu.module_from_spec(_spec)
sys.modules["transcribe_lambda_function"] = transcribe
_spec.loader.exec_module(transcribe)

KEY = "recordings/u-1/mobile/mobile-abc_1754300000.m4a"
BUCKET = "meeting-recorder-shubham-aps1"
SECRET = "wsec_test_secret"

# A realistic ElevenLabs words[] payload: two speakers, real timings, and the
# non-word token types (spacing, audio_event) the parser must drop.
WORDS = [
    {"type": "word", "text": "The", "speaker_id": "speaker_0", "start": 0.0, "end": 0.2},
    {"type": "spacing", "text": " ", "start": 0.2, "end": 0.21},
    {"type": "word", "text": "quotation", "speaker_id": "speaker_0", "start": 0.21, "end": 0.9},
    {"type": "word", "text": "is", "speaker_id": "speaker_0", "start": 0.9, "end": 1.0},
    {"type": "word", "text": "4.2", "speaker_id": "speaker_0", "start": 1.0, "end": 1.4},
    {"type": "word", "text": "lakh", "speaker_id": "speaker_0", "start": 1.4, "end": 1.8},
    {"type": "audio_event", "text": "(cough)", "start": 1.8, "end": 1.9},
    {"type": "word", "text": "Over", "speaker_id": "speaker_1", "start": 2.0, "end": 2.3},
    {"type": "word", "text": "budget", "speaker_id": "speaker_1", "start": 2.3, "end": 2.8},
]
TRANSCRIPTION = {"language_code": "en", "language_probability": 0.98,
                 "text": "The quotation is 4.2 lakh Over budget",
                 "words": WORDS}


def sign(body, secret=SECRET, timestamp=None):
    """The ElevenLabs-Signature header for `body`.

    Mirrors the scheme verified against the official SDK: HMAC-SHA256 over
    "{t}.{raw_body}", hex, announced as "t=<unix>,v0=<hex>".
    """
    ts = str(int(time.time()) if timestamp is None else timestamp)
    mac = hmac.new(secret.encode(), f"{ts}.{body}".encode(),
                   hashlib.sha256).hexdigest()
    return f"t={ts},v0={mac}"


def webhook_event(payload=None, *, secret=SECRET, timestamp=None,
                  signature=None, raw=None):
    """An API Gateway v2 event for POST /webhooks/elevenlabs/stt."""
    if raw is None:
        raw = json.dumps(payload if payload is not None else full_payload())
    headers = {}
    sig = signature if signature is not None else sign(raw, secret, timestamp)
    if sig:
        headers["ElevenLabs-Signature"] = sig
    return {
        "routeKey": "POST /webhooks/elevenlabs/stt",
        "requestContext": {"http": {"method": "POST",
                                    "path": "/webhooks/elevenlabs/stt"}},
        "headers": headers,
        "body": raw,
    }


def full_payload(request_id="req-A", key=KEY, bucket=BUCKET, version=1,
                 transcription=None, **extra):
    data = {
        "request_id": request_id,
        "webhook_metadata": {"audio_s3_key": key, "bucket": bucket,
                             "v": version},
    }
    if transcription is not False:
        data["transcription"] = transcription or TRANSCRIPTION
    data.update(extra)
    return {"type": "speech_to_text_transcription", "data": data}


def parse(resp):
    return resp["statusCode"], json.loads(resp["body"])


# ===========================================================================
# STT START — the S3 entry point queues the job and exits
# ===========================================================================
class StartTranscriptionTests(unittest.TestCase):
    def setUp(self):
        self.posts = []
        self.updates = []

        def fake_post(url, headers, fields, timeout):
            self.posts.append({"url": url, "headers": headers,
                               "fields": fields, "timeout": timeout})
            return 200, {"message": "Request accepted.",
                         "request_id": "req-A",
                         "transcription_id": "tr-A"}

        self.post_patch = mock.patch.object(transcribe, "_post_multipart",
                                            side_effect=fake_post)
        self.post_patch.start()
        self.addCleanup(self.post_patch.stop)

        self.s3 = mock.MagicMock()
        self.s3.generate_presigned_url.return_value = (
            f"https://{BUCKET}.s3.amazonaws.com/{KEY}?X-Amz-Signature=abc")
        self.addCleanup(mock.patch.object(transcribe, "_s3", self.s3).start)
        mock.patch.object(transcribe, "_s3", self.s3).start()

        table = mock.MagicMock()

        def fake_update(**kwargs):
            self.updates.append(kwargs)
            return {}

        table.update_item.side_effect = fake_update
        table.get_item.return_value = {"Item": {}}
        self.addCleanup(mock.patch.object(transcribe, "_table", table).start)
        mock.patch.object(transcribe, "_table", table).start()

        self.env = mock.patch.dict("os.environ",
                                   {"ELEVENLABS_API_KEY": "sk_test"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def s3_event(self, key=KEY, bucket=BUCKET):
        return {"Records": [{"s3": {"bucket": {"name": bucket},
                                    "object": {"key": key}}}]}

    def written(self):
        """Every field written across all update_item calls, flattened."""
        out = {}
        for kw in self.updates:
            names = kw.get("ExpressionAttributeNames") or {}
            values = kw.get("ExpressionAttributeValues") or {}
            expr = kw.get("UpdateExpression", "")
            for placeholder, attr in names.items():
                # "#f0 = :v0" -> map attr to the matching value
                token = expr.split(placeholder + " = ")
                if len(token) > 1:
                    vkey = token[1].split(",")[0].strip().split(" ")[0]
                    if vkey in values:
                        out[attr] = values[vkey]
        return out

    # --- the request itself ------------------------------------------------
    def test_webhook_true_is_sent(self):
        """The request must be ASYNC. Without webhook=true ElevenLabs holds the
        response until the transcript is ready, which is the 290s-socket failure
        this whole phase removes."""
        transcribe.handle_s3_event(self.s3_event())
        self.assertEqual(self.posts[0]["fields"]["webhook"], "true")

    def test_source_url_mode_is_preserved(self):
        """Remote-URL mode keeps a 3 GB recording out of the Lambda entirely."""
        fields = (transcribe.handle_s3_event(self.s3_event()),
                  self.posts[0]["fields"])[1]
        self.assertIn("source_url", fields)
        self.assertNotIn("file", fields)
        self.assertTrue(fields["source_url"].startswith("https://"))

    def test_diarization_and_model_unchanged(self):
        transcribe.handle_s3_event(self.s3_event())
        self.assertEqual(self.posts[0]["fields"]["diarize"], "true")
        self.assertEqual(self.posts[0]["fields"]["model_id"],
                         transcribe.ELEVENLABS_MODEL)

    def test_language_is_still_auto_detected(self):
        transcribe.handle_s3_event(self.s3_event())
        self.assertNotIn("language_code", self.posts[0]["fields"])

    def test_start_timeout_is_short_not_290s(self):
        """The queue call is a fast control-plane request. A long timeout here
        would reintroduce a socket the transcription can outlive."""
        transcribe.handle_s3_event(self.s3_event())
        self.assertLessEqual(self.posts[0]["timeout"], 60)

    def test_presign_outlives_the_queue_wait(self):
        """ElevenLabs may fetch minutes after accepting. A 900s URL expiring
        first fails the DOWNLOAD, which looks like bad audio but isn't."""
        transcribe.handle_s3_event(self.s3_event())
        expiry = self.s3.generate_presigned_url.call_args.kwargs["ExpiresIn"]
        self.assertGreaterEqual(expiry, 3600)

    # --- webhook_metadata --------------------------------------------------
    def test_metadata_carries_partition_key_bucket_and_version(self):
        transcribe.handle_s3_event(self.s3_event())
        meta = json.loads(self.posts[0]["fields"]["webhook_metadata"])
        self.assertEqual(meta["audio_s3_key"], KEY)
        self.assertEqual(meta["bucket"], BUCKET)
        self.assertEqual(meta["v"], transcribe.WEBHOOK_METADATA_VERSION)

    def test_metadata_is_a_json_string(self):
        """The API documents webhook_metadata as a JSON STRING; a dict would be
        rejected or stringified as a Python repr."""
        transcribe.handle_s3_event(self.s3_event())
        self.assertIsInstance(self.posts[0]["fields"]["webhook_metadata"], str)

    def test_metadata_key_is_the_dynamodb_key_not_the_recording_id(self):
        """audio_s3_key is authoritative — it IS the partition key. A
        recording_id lookup would not find the row."""
        transcribe.handle_s3_event(self.s3_event())
        meta = json.loads(self.posts[0]["fields"]["webhook_metadata"])
        self.assertEqual(meta["audio_s3_key"], KEY)
        self.assertNotIn("recording_id", meta)

    # --- persistence -------------------------------------------------------
    def test_request_id_is_stored(self):
        transcribe.handle_s3_event(self.s3_event())
        self.assertEqual(self.written().get("stt_request_id"), "req-A")

    def test_transcription_id_is_stored_when_returned(self):
        transcribe.handle_s3_event(self.s3_event())
        self.assertEqual(self.written().get("stt_transcription_id"), "tr-A")

    def test_status_stays_transcribing(self):
        transcribe.handle_s3_event(self.s3_event())
        self.assertEqual(self.written().get("status"), "transcribing")

    def test_started_at_is_stored_for_reconciliation(self):
        transcribe.handle_s3_event(self.s3_event())
        self.assertTrue(self.written().get("stt_started_at"))

    def test_returns_without_a_transcript(self):
        """The Lambda must not wait: no transcript, no analysis, no Groq."""
        out = transcribe.handle_s3_event(self.s3_event())
        self.assertEqual(out["queued"], 1)
        self.assertEqual(out["outcomes"][0]["status"], "transcribing")
        self.assertNotIn("transcript", out["outcomes"][0])

    def test_no_groq_call_on_the_start_path(self):
        with mock.patch.object(transcribe, "analyze_meeting") as analyze:
            transcribe.handle_s3_event(self.s3_event())
        analyze.assert_not_called()

    # --- failures ----------------------------------------------------------
    def test_rejected_job_marks_the_row_failed(self):
        """A REJECTION (bad audio, exhausted quota) is still an immediate,
        real failure — only the DURATION stopped being our problem."""
        with mock.patch.object(transcribe, "_post_multipart",
                               return_value=(401, '{"detail":"quota_exceeded"}')):
            with self.assertRaises(RuntimeError):
                transcribe.handle_s3_event(self.s3_event())
        self.assertEqual(self.written().get("status"), "failed")
        self.assertIn("401", self.written().get("error", ""))

    def test_accepted_but_no_request_id_is_a_failure(self):
        """Without an id, no later delivery could be matched or distinguished
        from a stale one — so this must fail loudly at queue time."""
        with mock.patch.object(transcribe, "_post_multipart",
                               return_value=(200, {"message": "ok"})):
            with self.assertRaises(RuntimeError):
                transcribe.handle_s3_event(self.s3_event())
        self.assertEqual(self.written().get("status"), "failed")

    def test_202_is_accepted_as_success(self):
        """202 is the documented async status. Treating it as a failure would
        double-charge on the retry for a job that WAS queued."""
        with mock.patch.object(transcribe, "_post_multipart",
                               return_value=(202, {"request_id": "req-Z"})):
            transcribe.handle_s3_event(self.s3_event())
        self.assertEqual(self.written().get("stt_request_id"), "req-Z")

    def test_unsupported_extension_is_skipped(self):
        out = transcribe.handle_s3_event(self.s3_event(key="notes/readme.txt"))
        self.assertEqual(out["queued"], 0)
        self.assertEqual(self.posts, [])

    # --- synchronous fallback --------------------------------------------
    # ElevenLabs REFUSES webhook=true when the workspace has no STT webhook
    # registered, which is a real deployment-ordering window: registering one
    # needs an API key with `webhooks_write`, granted out-of-band. Without a
    # fallback, every upload in that window fails — a working pipeline replaced
    # by a broken one for a reason the user has nothing to do with.
    NO_WEBHOOK_BODY = ('{"detail":{"type":"validation_error",'
                       '"code":"invalid_parameters","message":"No speech-to-text'
                       ' webhooks are configured for this workspace.",'
                       '"status":"no_webhooks_configured","param":"webhook"}}')

    def test_no_webhook_configured_is_detected(self):
        """Verified against the live API: this error comes back as HTTP 400."""
        self.assertTrue(transcribe._is_no_webhook_configured(
            400, self.NO_WEBHOOK_BODY))
        self.assertTrue(transcribe._is_no_webhook_configured(
            422, self.NO_WEBHOOK_BODY))

    def test_other_errors_are_not_mistaken_for_it(self):
        """A quota or auth failure must still fail loudly — silently retrying
        those synchronously would burn the invocation and hide a real problem."""
        self.assertFalse(transcribe._is_no_webhook_configured(
            401, '{"detail":{"code":"quota_exceeded"}}'))
        self.assertFalse(transcribe._is_no_webhook_configured(
            400, '{"detail":{"code":"invalid_audio"}}'))

    def test_falls_back_to_a_synchronous_transcription(self):
        calls = []

        def fake_post(url, headers, fields, timeout):
            calls.append(fields)
            if fields.get("webhook") == "true":
                return 400, self.NO_WEBHOOK_BODY
            return 200, {"language_code": "en", "words": WORDS}

        with mock.patch.object(transcribe, "_post_multipart",
                               side_effect=fake_post), \
             mock.patch.object(transcribe, "analyze_and_persist",
                               return_value={"key": KEY, "status": "complete"}
                               ) as analyze:
            out = transcribe.handle_s3_event(self.s3_event())

        # Tried async first, then retried WITHOUT the webhook parameter.
        self.assertEqual(calls[0].get("webhook"), "true")
        self.assertNotIn("webhook", calls[1])
        analyze.assert_called_once()
        self.assertEqual(out["outcomes"][0]["status"], "complete")

    def test_fallback_passes_the_real_transcript_through(self):
        def fake_post(url, headers, fields, timeout):
            if fields.get("webhook") == "true":
                return 400, self.NO_WEBHOOK_BODY
            return 200, {"language_code": "en", "words": WORDS}

        with mock.patch.object(transcribe, "_post_multipart",
                               side_effect=fake_post), \
             mock.patch.object(transcribe, "analyze_and_persist",
                               return_value={"key": KEY, "status": "complete"}
                               ) as analyze:
            transcribe.handle_s3_event(self.s3_event())

        _, _, transcript, timestamps, language = analyze.call_args.args
        self.assertEqual(transcript, "Speaker 0: The quotation is 4.2 lakh\n\n"
                                     "Speaker 1: Over budget")
        self.assertEqual(len(timestamps), 2)
        self.assertEqual(language, "en")

    def test_fallback_failure_marks_the_row_failed(self):
        def fake_post(url, headers, fields, timeout):
            if fields.get("webhook") == "true":
                return 400, self.NO_WEBHOOK_BODY
            return 400, '{"detail":{"code":"invalid_audio"}}'

        with mock.patch.object(transcribe, "_post_multipart",
                               side_effect=fake_post):
            with self.assertRaises(RuntimeError):
                transcribe.handle_s3_event(self.s3_event())
        self.assertEqual(self.written().get("status"), "failed")


# ===========================================================================
# WEBHOOK — signature, envelope, staleness, idempotency
# ===========================================================================
class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.item = {"audio_s3_key": KEY, "user_id": "u-1",
                     "status": "transcribing", "stt_request_id": "req-A"}
        self.updates = []
        self.claim_fails = False

        self.recordings = mock.MagicMock()
        self.recordings.get_item.side_effect = \
            lambda **kw: {"Item": dict(self.item)} if self.item else {}

        def fake_update(**kwargs):
            if "stt_completed_request_id = :rid" in kwargs.get(
                    "UpdateExpression", "") and self.claim_fails:
                raise _ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException"}},
                    "UpdateItem")
            self.updates.append(kwargs)
            return {}

        self.recordings.update_item.side_effect = fake_update
        mock.patch.object(api, "_recordings", self.recordings).start()
        self.addCleanup(mock.patch.stopall)

        self.lam = mock.MagicMock()
        mock.patch.object(api, "_lambda_client", self.lam).start()
        self.s3 = mock.MagicMock()
        mock.patch.object(api, "_s3", self.s3).start()
        mock.patch.object(api, "BUCKET_NAME", BUCKET).start()
        mock.patch.object(api, "_elevenlabs_webhook_secret",
                          return_value=SECRET).start()

    # --- 1. signature -----------------------------------------------------
    def test_valid_signature_is_accepted(self):
        status, body = parse(api.stt_webhook(webhook_event()))
        self.assertEqual(status, 200)
        self.assertTrue(body.get("accepted"))

    def test_wrong_secret_is_rejected(self):
        """The endpoint is public. A signature computed with the wrong secret is
        exactly what a forged transcript looks like."""
        with self.assertRaises(api.ApiError) as cm:
            api.stt_webhook(webhook_event(secret="wrong-secret"))
        self.assertEqual(cm.exception.status, 401)
        self.lam.invoke.assert_not_called()

    def test_missing_signature_header_is_rejected(self):
        with self.assertRaises(api.ApiError) as cm:
            api.stt_webhook(webhook_event(signature=""))
        self.assertEqual(cm.exception.status, 401)

    def test_malformed_signature_header_is_rejected(self):
        with self.assertRaises(api.ApiError) as cm:
            api.stt_webhook(webhook_event(signature="garbage"))
        self.assertEqual(cm.exception.status, 401)

    def test_tampered_body_is_rejected(self):
        """Signature over the ORIGINAL body must not validate a modified one."""
        raw = json.dumps(full_payload())
        sig = sign(raw)
        tampered = raw.replace("4.2 lakh", "9.9 lakh")
        ev = webhook_event(raw=tampered, signature=sig)
        with self.assertRaises(api.ApiError) as cm:
            api.stt_webhook(ev)
        self.assertEqual(cm.exception.status, 401)

    def test_replayed_old_timestamp_is_rejected(self):
        old = int(time.time()) - (api.ELEVENLABS_WEBHOOK_TOLERANCE + 60)
        with self.assertRaises(api.ApiError) as cm:
            api.stt_webhook(webhook_event(timestamp=old))
        self.assertEqual(cm.exception.status, 401)

    def test_unconfigured_secret_fails_closed(self):
        """An unset secret must never mean "accept everything" — that would
        leave the endpoint open if a deploy forgot it."""
        with mock.patch.object(api, "_elevenlabs_webhook_secret",
                               return_value=""):
            with self.assertRaises(api.ApiError) as cm:
                api.stt_webhook(webhook_event())
        self.assertEqual(cm.exception.status, 401)

    def test_signature_is_checked_before_any_db_read(self):
        """Cheap crypto first: an unsigned flood must cost no DynamoDB traffic."""
        with self.assertRaises(api.ApiError):
            api.stt_webhook(webhook_event(secret="wrong"))
        self.recordings.get_item.assert_not_called()

    # --- 2. envelope ------------------------------------------------------
    def test_wrong_metadata_version_is_rejected(self):
        with self.assertRaises(api.ApiError) as cm:
            api.stt_webhook(webhook_event(full_payload(version=99)))
        self.assertEqual(cm.exception.status, 400)

    def test_missing_metadata_is_rejected(self):
        payload = full_payload()
        del payload["data"]["webhook_metadata"]
        with self.assertRaises(api.ApiError) as cm:
            api.stt_webhook(webhook_event(payload))
        self.assertEqual(cm.exception.status, 400)

    def test_foreign_bucket_is_rejected(self):
        """Two environments sharing one ElevenLabs workspace must not deliver
        each other's transcripts onto same-named keys."""
        with self.assertRaises(api.ApiError) as cm:
            api.stt_webhook(webhook_event(
                full_payload(bucket="someone-elses-bucket")))
        self.assertEqual(cm.exception.status, 400)
        self.lam.invoke.assert_not_called()

    def test_metadata_as_json_string_is_tolerated(self):
        payload = full_payload()
        payload["data"]["webhook_metadata"] = json.dumps(
            payload["data"]["webhook_metadata"])
        status, body = parse(api.stt_webhook(webhook_event(payload)))
        self.assertEqual(status, 200)
        self.assertTrue(body.get("accepted"))

    def test_missing_request_id_is_rejected(self):
        payload = full_payload()
        del payload["data"]["request_id"]
        with self.assertRaises(api.ApiError) as cm:
            api.stt_webhook(webhook_event(payload))
        self.assertEqual(cm.exception.status, 400)

    def test_other_event_types_are_ignored_not_retried(self):
        payload = full_payload()
        payload["type"] = "some_other_webhook"
        status, body = parse(api.stt_webhook(webhook_event(payload)))
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ignored"))

    # --- 3. staleness -----------------------------------------------------
    def test_stale_request_id_is_rejected(self):
        """Job A's delivery arriving AFTER job B was queued must be dropped —
        an old transcript must never overwrite a newer one."""
        self.item["stt_request_id"] = "req-B"
        status, body = parse(api.stt_webhook(
            webhook_event(full_payload(request_id="req-A"))))
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ignored"))
        self.assertEqual(body.get("reason"), "stale request_id")
        self.lam.invoke.assert_not_called()

    def test_current_request_id_is_processed(self):
        self.item["stt_request_id"] = "req-B"
        status, body = parse(api.stt_webhook(
            webhook_event(full_payload(request_id="req-B"))))
        self.assertEqual(status, 200)
        self.assertTrue(body.get("accepted"))
        self.lam.invoke.assert_called_once()

    def test_row_with_no_pending_job_is_not_applied(self):
        """No stored id means we cannot prove the delivery is current."""
        self.item.pop("stt_request_id")
        status, body = parse(api.stt_webhook(webhook_event()))
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ignored"))
        self.lam.invoke.assert_not_called()

    def test_unknown_recording_is_ignored(self):
        self.item = None
        status, body = parse(api.stt_webhook(webhook_event()))
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ignored"))
        self.lam.invoke.assert_not_called()

    # --- 4. idempotency ---------------------------------------------------
    def test_duplicate_delivery_does_not_reprocess(self):
        """ElevenLabs retries webhooks. A second delivery must not run the
        analysis again (double Groq spend, possible double CRM push)."""
        self.claim_fails = True
        status, body = parse(api.stt_webhook(webhook_event()))
        self.assertEqual(status, 200)
        self.assertTrue(body.get("duplicate"))
        self.lam.invoke.assert_not_called()

    def test_claim_is_conditional_on_the_request_id(self):
        api.stt_webhook(webhook_event())
        claim = next(kw for kw in self.updates
                     if "stt_completed_request_id" in kw.get(
                         "UpdateExpression", ""))
        self.assertIn("stt_request_id = :rid", claim["ConditionExpression"])

    def test_analysis_invoked_exactly_once(self):
        api.stt_webhook(webhook_event())
        self.assertEqual(self.lam.invoke.call_count, 1)

    def test_analysis_is_async(self):
        """Inline Groq would blow API Gateway's 29s ceiling, and a timed-out
        webhook is one ElevenLabs retries — starting a second analysis."""
        api.stt_webhook(webhook_event())
        self.assertEqual(self.lam.invoke.call_args.kwargs["InvocationType"],
                         "Event")

    # --- 5. transcript fidelity ------------------------------------------
    def test_transcript_speakers_and_text_are_preserved(self):
        api.stt_webhook(webhook_event())
        payload = json.loads(self.lam.invoke.call_args.kwargs["Payload"])
        self.assertEqual(payload["transcript"],
                         "Speaker 0: The quotation is 4.2 lakh\n\n"
                         "Speaker 1: Over budget")

    def test_timestamps_are_preserved_with_real_timing(self):
        api.stt_webhook(webhook_event())
        payload = json.loads(self.lam.invoke.call_args.kwargs["Payload"])
        segs = payload["timestamps"]
        self.assertEqual(len(segs), 2)
        self.assertEqual(segs[0]["speaker"], "0")
        self.assertEqual(float(segs[0]["start"]), 0.0)
        self.assertEqual(float(segs[0]["end"]), 1.8)
        self.assertEqual(segs[1]["speaker"], "1")

    def test_language_is_preserved(self):
        api.stt_webhook(webhook_event())
        payload = json.loads(self.lam.invoke.call_args.kwargs["Payload"])
        self.assertEqual(payload["language"], "en")

    def test_transcript_goes_to_s3_not_dynamodb(self):
        """Inline, transcript+timestamps were 96% of the 400 KB item limit."""
        api.stt_webhook(webhook_event())
        self.s3.put_object.assert_called_once()
        for kw in self.updates:
            values = kw.get("ExpressionAttributeValues") or {}
            for v in values.values():
                self.assertNotIsInstance(v, list)

    def test_transcript_is_stored_before_analysis_is_invoked(self):
        """If the analysis never starts, the transcript must still be durable —
        the difference between "reprocess the AI" and "pay for STT again"."""
        order = []
        self.s3.put_object.side_effect = lambda **kw: order.append("s3")
        self.lam.invoke.side_effect = lambda **kw: order.append("invoke")
        api.stt_webhook(webhook_event())
        self.assertEqual(order, ["s3", "invoke"])

    def test_s3_failure_releases_the_claim_and_502s(self):
        """Otherwise ElevenLabs' retry would be rejected as a duplicate and the
        transcript lost for good."""
        self.s3.put_object.side_effect = RuntimeError("s3 down")
        with self.assertRaises(api.ApiError) as cm:
            api.stt_webhook(webhook_event())
        self.assertEqual(cm.exception.status, 502)
        self.assertTrue(any("REMOVE stt_completed_request_id" in
                            kw.get("UpdateExpression", "")
                            for kw in self.updates))

    def test_completion_event_shape_matches_the_transcribe_lambda(self):
        """The two halves must agree on the event contract."""
        api.stt_webhook(webhook_event())
        payload = json.loads(self.lam.invoke.call_args.kwargs["Payload"])
        self.assertEqual(payload["type"], transcribe.STT_COMPLETED_EVENT)
        self.assertEqual(payload["audio_s3_key"], KEY)
        self.assertEqual(payload["bucket"], BUCKET)

    # --- reported failures ------------------------------------------------
    def test_reported_failure_marks_the_row_failed(self):
        """A failed transcription must reach a TERMINAL state, not sit at
        "transcribing" — the dead end this phase exists to remove."""
        payload = full_payload(transcription=False, error="invalid_audio")
        status, _ = parse(api.stt_webhook(webhook_event(payload)))
        self.assertEqual(status, 200)
        written = {}
        for kw in self.updates:
            names = kw.get("ExpressionAttributeNames") or {}
            values = kw.get("ExpressionAttributeValues") or {}
            for ph, attr in names.items():
                expr = kw.get("UpdateExpression", "")
                if f"{ph} = " in expr:
                    vkey = expr.split(f"{ph} = ")[1].split(",")[0].strip()
                    if vkey in values:
                        written[attr] = values[vkey]
        self.assertEqual(written.get("status"), "failed")

    def test_stale_failure_does_not_fail_a_requeued_recording(self):
        self.item["stt_request_id"] = "req-B"
        payload = full_payload(request_id="req-A", transcription=False,
                               error="invalid_audio")
        status, body = parse(api.stt_webhook(webhook_event(payload)))
        self.assertEqual(status, 200)
        self.assertTrue(body.get("ignored"))

    # --- routing ----------------------------------------------------------
    def test_route_is_registered(self):
        self.assertIs(api._ROUTES[("POST", "/webhooks/elevenlabs/stt")],
                      api.stt_webhook)

    def test_webhook_route_requires_no_jwt(self):
        """ElevenLabs has no MinuteX token; identity is the HMAC signature."""
        ev = webhook_event()
        self.assertNotIn("authorization", {k.lower() for k in ev["headers"]})
        status, _ = parse(api.stt_webhook(ev))
        self.assertEqual(status, 200)


# ===========================================================================
# REPROCESS — must invalidate an in-flight job
# ===========================================================================
class ReprocessInvalidationTests(unittest.TestCase):
    def test_reprocess_clears_the_old_stt_markers(self):
        """Two live jobs for one recording is the stale-overwrite scenario.
        Clearing the old id makes the first job's late delivery unmatchable."""
        item = {"audio_s3_key": KEY, "user_id": "u-1", "status": "failed",
                "stt_request_id": "req-A"}
        recordings = mock.MagicMock()
        recordings.get_item.return_value = {"Item": item}
        updates = []
        recordings.update_item.side_effect = \
            lambda **kw: updates.append(kw) or {}
        lam = mock.MagicMock()
        with mock.patch.object(api, "_recordings", recordings), \
             mock.patch.object(api, "_lambda_client", lam), \
             mock.patch.object(api, "BUCKET_NAME", BUCKET), \
             mock.patch.object(api, "_require_auth", return_value="u-1"):
            api.reprocess_recording({
                "routeKey": "POST /recordings/ai/reprocess/{key+}",
                "requestContext": {"http": {"method": "POST", "path": "/"}},
                "pathParameters": {"key": KEY},
                "headers": {"authorization": "Bearer t"},
            })
        expr = updates[0]["UpdateExpression"]
        self.assertIn("REMOVE", expr)
        self.assertIn("stt_request_id", expr.split("REMOVE")[1])
        self.assertIn("stt_completed_request_id", expr.split("REMOVE")[1])


# ===========================================================================
# UNIFIED ANALYSIS — one call, all fields
# ===========================================================================
# One reply from the unified call. The section TITLES here are deliberately
# specific to this meeting ("Quotation", "Cost Reduction") rather than generic
# template headings — the fixture doubles as documentation of what the dynamic
# overview is supposed to look like.
UNIFIED_REPLY = {
    "title": "Fit-out quotation review",
    "overview": {"sections": [
        {"title": "Quotation", "kind": "text",
         "content": "The 4.2 lakh quotation was over the approved 3.8 lakh "
                    "budget.",
         "items": [], "evidence_segment_ids": ["seg_0"]},
        {"title": "Cost Reduction", "kind": "list", "content": "",
         "items": ["Drop imported fittings to reach 3.9 lakh",
                   "Six-week lead time pushes past handover"],
         "evidence_segment_ids": ["seg_1", "seg_2"]},
    ]},
    "tasks": [{"task": "Send the revised quote", "assignee": "Rakesh",
               "due_date": "Friday", "priority": "High",
               "evidence_segment_ids": ["seg_0"]}],
    "participants": [
        {"speaker": "Speaker 0", "summary": "Presented the quotation"},
        {"speaker": "Speaker 1", "summary": "Held the budget line"},
    ],
    "meeting_highlights": {
        "decisions": [{"decision": "Drop imported fittings",
                       "context": "to reach 3.9 lakh"}],
        "action_items": [{"task": "Send revised quote", "owner": "Rakesh",
                          "deadline": "Friday"}],
        "deadlines": [{"what": "Site visit", "when": "15th March"}],
        "open_questions": ["Who chases the fittings lead time?"],
    },
}

DIARIZED = ("Speaker 0: The quotation is 4.2 lakh. Rakesh will send the "
            "revised quote by Friday.\n\n"
            "Speaker 1: That's over the 3.8 we approved.\n\n"
            "Speaker 2: Lead time is six weeks.\n\n"
            "Speaker 3: Noted, I'll track it.")

MAPPINGS = [{"object": "Site_Visit__c", "object_label": "Site Visit",
             "label": "Site Visit Number", "lookup_field": "Name"}]


class UnifiedAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def fake_complete(system_prompt, user_content, **kwargs):
            self.calls.append({"system": system_prompt, "user": user_content,
                               "kwargs": kwargs})
            return json.dumps(UNIFIED_REPLY)

        mock.patch.object(groq_client, "complete",
                          side_effect=fake_complete).start()
        mock.patch.dict("os.environ", {"GROQ_API_KEY": "gsk_test"}).start()
        self.addCleanup(mock.patch.stopall)

    def test_one_groq_call_produces_everything(self):
        """The headline: several transcript sends become one."""
        out = transcribe.analyze_meeting(DIARIZED)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(out["title"])
        self.assertFalse(ai_schema.overview_empty(out["overview"]))
        self.assertTrue(out["tasks"])
        self.assertTrue(out["participants"])
        self.assertFalse(ai_schema.highlights_empty(out["meeting_highlights"]))

    def test_transcript_is_sent_exactly_once(self):
        transcribe.analyze_meeting(DIARIZED)
        sent = [c for c in self.calls if DIARIZED in c["user"]]
        self.assertEqual(len(sent), 1)

    def test_every_output_arrives_in_one_reply(self):
        out = transcribe.analyze_meeting(DIARIZED)
        self.assertEqual(out["title"], "Fit-out quotation review")
        sections = out["overview"]["sections"]
        self.assertEqual([s["title"] for s in sections],
                         ["Quotation", "Cost Reduction"])
        self.assertIn("4.2 lakh", sections[0]["content"])
        self.assertEqual(out["tasks"][0]["assignee"], "Rakesh")
        self.assertEqual(out["meeting_highlights"]["deadlines"][0]["when"],
                         "15th March")

    def test_the_prompt_never_asks_for_crm_identifiers(self):
        """CRM extraction left the analysis path: it made every meeting carry
        per-mapping rules for a feature that is not built out, and it is the one
        output whose failure mode is writing a customer's notes onto a
        stranger's record."""
        transcribe.analyze_meeting(DIARIZED)
        self.assertNotIn("crm_identifiers", self.calls[0]["system"])

    def test_no_crm_identifiers_in_the_result(self):
        out = transcribe.analyze_meeting(DIARIZED)
        self.assertNotIn("crm_identifiers", out)

    def test_evidence_ids_are_validated_against_the_transcript(self):
        """The model's segment references are checked against the ids the app
        will actually resolve; an unresolvable one is dropped rather than
        offered as a jump-to-moment that goes nowhere."""
        out = transcribe.analyze_meeting(DIARIZED, valid_ids={"seg_0", "seg_1"})
        sections = out["overview"]["sections"]
        self.assertEqual(sections[0]["evidence_segment_ids"], ["seg_0"])
        # seg_2 is not in the valid set, so it goes.
        self.assertEqual(sections[1]["evidence_segment_ids"], ["seg_1"])
        # ...and the sections themselves survive regardless.
        self.assertEqual(len(sections), 2)

    def test_deadline_is_the_whole_budget_not_a_split(self):
        transcribe.analyze_meeting(DIARIZED)
        # complete() receives an absolute deadline; just assert the call was
        # given one derived from the full budget rather than 60% of it.
        self.assertIsNotNone(self.calls[0]["kwargs"].get("deadline"))

    def test_empty_transcript_returns_the_empty_unified_shape(self):
        out = transcribe.analyze_meeting("   ")
        self.assertTrue(ai_schema.overview_empty(out["overview"]))
        self.assertTrue(ai_schema.highlights_empty(out["meeting_highlights"]))
        self.assertEqual(self.calls, [])


# ===========================================================================
# SPEAKER_NAMES WIRING — analyze_meeting() must actually forward the row's
# rename map into coerce_unified/merge_unified, not just accept the kwarg.
# ai_schema._roster_filtered's own matching is covered in
# test_speaker_confidence.py; this proves the Lambda's entry point actually
# reaches it end to end (the gap the audit found: the parameter existed on
# ai_schema but nothing upstream ever passed it).
# ===========================================================================
NAMED_REPLY = {
    "title": "Fit-out quotation review",
    "overview": {"sections": []},
    "tasks": [],
    "participants": [
        # The model answers with the HUMAN NAME it read off a renamed
        # transcript line, exactly as it would for a real renamed meeting.
        {"speaker": "Yuvraj Sir", "summary": "Presented the quotation"},
        {"speaker": "Speaker 1", "summary": "Held the budget line"},
    ],
    "meeting_highlights": {"decisions": [], "action_items": [],
                           "deadlines": [], "open_questions": []},
}


class SpeakerNamesWiringTests(unittest.TestCase):
    def setUp(self):
        def fake_complete(system_prompt, user_content, **kwargs):
            return json.dumps(NAMED_REPLY)

        mock.patch.object(groq_client, "complete",
                          side_effect=fake_complete).start()
        mock.patch.dict("os.environ", {"GROQ_API_KEY": "gsk_test"}).start()
        self.addCleanup(mock.patch.stopall)

    def test_renamed_speaker_resolves_when_names_are_passed(self):
        """The whole point of the wiring: analyze_meeting(speaker_names=...)
        must let a human-name reply land on the right roster entry instead of
        being dropped as an unmatched name."""
        names = {"0": "Yuvraj Sir"}
        out = transcribe.analyze_meeting(DIARIZED, speaker_names=names)
        by_label = {p["speaker"]: p["summary"] for p in out["participants"]}
        self.assertEqual(by_label["Speaker 0"], "Presented the quotation")
        self.assertEqual(by_label["Speaker 1"], "Held the budget line")

    def test_renamed_speaker_is_dropped_without_the_map(self):
        """Without speaker_names, "Yuvraj Sir" cannot resolve to "Speaker 0" —
        confirms the previous test is actually exercising the wiring, not a
        default that would pass either way."""
        out = transcribe.analyze_meeting(DIARIZED)
        by_label = {p["speaker"]: p["summary"] for p in out["participants"]}
        self.assertEqual(by_label["Speaker 0"], "")

    def test_bare_and_underscored_labels_still_match_through_analyze_meeting(self):
        """The normalized-matching half of the fix, exercised through the same
        Lambda entry point (not just ai_schema directly)."""
        reply = {**NAMED_REPLY,
                 "participants": [{"speaker": "speaker_0", "summary": "Led."},
                                  {"speaker": "1", "summary": "Asked."}]}
        with mock.patch.object(groq_client, "complete",
                               side_effect=lambda *a, **k: json.dumps(reply)):
            out = transcribe.analyze_meeting(DIARIZED)
        by_label = {p["speaker"]: p["summary"] for p in out["participants"]}
        self.assertEqual(by_label["Speaker 0"], "Led.")
        self.assertEqual(by_label["Speaker 1"], "Asked.")


# ===========================================================================
# PARTICIPANTS — completeness, and participants != assignees
# ===========================================================================
class ParticipantTests(unittest.TestCase):
    def test_all_four_speakers_appear(self):
        """Every unique Speaker N in the transcript is a participant, even one
        the model forgot to describe."""
        roster = ai_schema.speaker_roster(DIARIZED)
        self.assertEqual(roster, ["Speaker 0", "Speaker 1", "Speaker 2",
                                  "Speaker 3"])
        out = ai_schema.coerce_unified(UNIFIED_REPLY, roster)
        self.assertEqual([p["speaker"] for p in out["participants"]], roster)

    def test_speaker_the_model_skipped_is_added_back(self):
        roster = ai_schema.speaker_roster(DIARIZED)
        out = ai_schema.coerce_unified(UNIFIED_REPLY, roster)
        by_label = {p["speaker"]: p for p in out["participants"]}
        self.assertIn("Speaker 2", by_label)
        self.assertEqual(by_label["Speaker 2"]["summary"], "")

    def test_task_assignee_who_never_spoke_is_not_a_participant(self):
        """Speaker 0 assigns work to Rakesh. Rakesh must own the task and must
        NOT appear as an attendee — the app renders that list as "who was in
        this meeting", so an invented attendee is indistinguishable from a real
        one."""
        roster = ai_schema.speaker_roster(DIARIZED)
        out = ai_schema.coerce_unified(UNIFIED_REPLY, roster)
        labels = [p["speaker"] for p in out["participants"]]
        self.assertNotIn("Rakesh", labels)
        self.assertEqual(out["tasks"][0]["assignee"], "Rakesh")

    def test_model_volunteered_non_speaker_is_dropped(self):
        reply = json.loads(json.dumps(UNIFIED_REPLY))
        reply["participants"].append({"speaker": "Rakesh",
                                     "summary": "will send the quote"})
        roster = ai_schema.speaker_roster(DIARIZED)
        out = ai_schema.coerce_unified(reply, roster)
        self.assertNotIn("Rakesh", [p["speaker"] for p in out["participants"]])

    def test_non_diarized_transcript_keeps_the_models_list(self):
        """With no speaker labels there is no structural evidence to enforce, and
        dropping every participant would be worse than trusting the prompt."""
        out = ai_schema.coerce_unified(UNIFIED_REPLY, [])
        self.assertEqual([p["speaker"] for p in out["participants"]],
                         ["Speaker 0", "Speaker 1"])


# ===========================================================================
# CONTEXT — single pass vs the map-reduce fallback
# ===========================================================================
class ContextRoutingTests(unittest.TestCase):
    def setUp(self):
        self.labels = []

        def fake_complete(system_prompt, user_content, **kwargs):
            self.labels.append(kwargs.get("label", ""))
            return json.dumps(UNIFIED_REPLY)

        mock.patch.object(groq_client, "complete",
                          side_effect=fake_complete).start()
        mock.patch.dict("os.environ", {"GROQ_API_KEY": "gsk_test"}).start()
        self.addCleanup(mock.patch.stopall)

    def test_transcript_within_budget_takes_a_single_pass(self):
        prompt = prompts.unified_analysis_system(("Speaker 0",))
        budget = groq_client.single_pass_budget_chars(prompt)
        self.assertLess(len(DIARIZED), budget)
        transcribe.analyze_meeting(DIARIZED)
        self.assertEqual(len(self.labels), 1)
        self.assertTrue(all("chunk" not in lbl for lbl in self.labels))

    def test_oversized_transcript_falls_back_to_map_reduce(self):
        """The ONLY path that reaches the reduce prompt."""
        prompt = prompts.unified_analysis_system(())
        budget = groq_client.single_pass_budget_chars(prompt)
        huge = "Speaker 0: over budget again.\n" * (budget // 20)
        self.assertGreater(len(huge), budget)
        transcribe.analyze_meeting(huge)
        self.assertTrue(any("chunk" in lbl for lbl in self.labels),
                        f"expected chunked labels, got {self.labels}")

    def test_summary_reduce_system_still_exists(self):
        """Explicitly NOT retired — it is the overflow reduce, and the unified
        reduce prompt is built on top of it."""
        self.assertTrue(prompts.SUMMARY_REDUCE_SYSTEM)
        self.assertIn(prompts.SUMMARY_REDUCE_SYSTEM,
                      prompts.unified_reduce_system())

    def test_reduce_prompt_covers_the_unified_sections(self):
        body = prompts.unified_reduce_system()
        self.assertIn("meeting_highlights", body)
        self.assertIn("overview", body)
        self.assertNotIn("crm_identifiers", body)

    def test_reduce_prompt_redecides_sections_rather_than_concatenating(self):
        """Chunks are split by LENGTH, not by subject, so one subject is spread
        across several of them. Concatenating each chunk's sections would give a
        stack of partial overviews instead of one meeting's overview."""
        body = prompts.unified_reduce_system()
        self.assertIn("Re-decide the sections for the WHOLE meeting", body)
        self.assertIn("split by LENGTH, not by subject", body)

    def test_merge_unified_folds_every_section(self):
        partials = [
            UNIFIED_REPLY,
            {"title": "",
             "overview": {"sections": [
                 # Same subject as chunk 1 -> folds into it.
                 {"title": "Quotation", "kind": "text",
                  "content": "Second half.", "items": [],
                  "evidence_segment_ids": []},
                 # A subject only this chunk saw -> survives on its own.
                 {"title": "Handover", "kind": "text",
                  "content": "Slips to April.", "items": [],
                  "evidence_segment_ids": []},
             ]},
             "tasks": [], "participants": [{"speaker": "Speaker 2",
                                            "summary": "raised lead time"}],
             "meeting_highlights": {
                 **ai_schema.empty_highlights(),
                 "open_questions": ["A different question"]}},
        ]
        roster = ai_schema.speaker_roster(DIARIZED)
        merged = ai_schema.merge_unified(partials, roster)
        titles = [s["title"] for s in merged["overview"]["sections"]]
        self.assertEqual(titles, ["Quotation", "Cost Reduction", "Handover"])
        self.assertIn("Second half.", merged["overview"]["sections"][0]["content"])
        self.assertEqual([p["speaker"] for p in merged["participants"]], roster)
        self.assertIn("A different question",
                      merged["meeting_highlights"]["open_questions"])
        self.assertNotIn("crm_identifiers", merged)


# ===========================================================================
# UPLOAD LIMITS — 3 GB / 10 h, and the boundaries around them
# ===========================================================================
class UploadLimitTests(unittest.TestCase):
    def test_backend_limits_are_the_provider_limits(self):
        self.assertEqual(api.MAX_UPLOAD_BYTES, 3 * 1000 * 1000 * 1000)
        self.assertEqual(api.MAX_DURATION_SECONDS, 10 * 3600)

    def test_decimal_not_binary_gigabytes(self):
        """3 GiB would sit ~7% ABOVE the provider's ceiling, turning a rejected
        upload into a paid transcription that fails after the bytes are in S3."""
        self.assertLess(api.MAX_UPLOAD_BYTES, 3 * 1024 * 1024 * 1024)

    def _request(self, **body):
        payload = {"source": "UPLOAD", "format": "m4a"}
        payload.update(body)
        recordings = mock.MagicMock()
        recordings.get_item.return_value = {}
        s3 = mock.MagicMock()
        s3.generate_presigned_url.return_value = "https://example/put"
        with mock.patch.object(api, "_require_auth", return_value="u-1"), \
             mock.patch.object(api, "_recordings", recordings), \
             mock.patch.object(api, "_s3", s3), \
             mock.patch.object(api, "BUCKET_NAME", BUCKET):
            try:
                return parse(api.request_upload({
                    "routeKey": "POST /recordings/upload-request",
                    "requestContext": {"http": {"method": "POST",
                                                "path": "/"}},
                    "headers": {"authorization": "Bearer t"},
                    "body": json.dumps(payload),
                }))
            except api.ApiError as err:
                return err.status, {"error": err.message}

    # --- duration ---------------------------------------------------------
    def test_below_four_hours_is_accepted(self):
        status, _ = self._request(duration=3 * 3600)
        self.assertEqual(status, 200)

    def test_between_four_and_ten_hours_is_now_accepted(self):
        """The headline of Phase 2: a 6-hour recording used to be refused."""
        status, body = self._request(duration=6 * 3600)
        self.assertEqual(status, 200, body)

    def test_exactly_ten_hours_is_accepted(self):
        status, body = self._request(duration=10 * 3600)
        self.assertEqual(status, 200, body)

    def test_just_over_ten_hours_is_rejected(self):
        status, body = self._request(duration=10 * 3600 + 1)
        self.assertEqual(status, 400)
        self.assertIn("10 hours", body["error"])

    def test_well_over_ten_hours_is_rejected(self):
        status, _ = self._request(duration=12 * 3600)
        self.assertEqual(status, 400)

    # --- size -------------------------------------------------------------
    def test_below_three_gb_is_accepted(self):
        status, body = self._request(size=2_500_000_000)
        self.assertEqual(status, 200, body)

    def test_exactly_three_gb_is_accepted(self):
        status, body = self._request(size=3_000_000_000)
        self.assertEqual(status, 200, body)

    def test_just_over_three_gb_is_rejected(self):
        status, body = self._request(size=3_000_000_001)
        self.assertEqual(status, 413)
        self.assertIn("3 GB", body["error"])

    def test_old_two_gb_ceiling_no_longer_rejects(self):
        status, body = self._request(size=2_200_000_000)
        self.assertEqual(status, 200, body)

    def test_error_message_uses_gb_not_mb(self):
        """"max 2048 MB" could not be reconciled with the "up to 2 GB" the
        upload screen advertised."""
        _, body = self._request(size=4_000_000_000)
        self.assertIn("GB", body["error"])
        self.assertNotIn("MB", body["error"])

    def test_frontend_mirrors_the_backend_exactly(self):
        """A mismatch either rejects uploads the backend would accept, or lets
        bytes reach S3 only to be refused afterwards."""
        # The app tree is a SIBLING of cloud/ (ROOT here is <repo>/cloud), so
        # resolve from the repo root. Both the post-reorg layout (app/lib) and
        # the legacy nested one (app/recorder-app/lib) are accepted so this
        # keeps working whichever way the app tree is arranged.
        repo = ROOT.parent
        for rel in (("app", "lib", "uploads.tsx"),
                    ("app", "recorder-app", "lib", "uploads.tsx")):
            cand = repo.joinpath(*rel)
            if cand.exists():
                break
        else:
            self.skipTest("app tree not present — frontend mirror check skipped")
        src = cand.read_text(encoding="utf-8")
        self.assertIn("MAX_UPLOAD_BYTES = 3 * 1000 * 1000 * 1000", src)
        self.assertIn("MAX_DURATION_SECONDS = 10 * 3600", src)


# ===========================================================================
# SHARED STT PARSER — one implementation for both Lambdas
# ===========================================================================
class SttResultTests(unittest.TestCase):
    def test_both_lambdas_use_the_same_parser(self):
        """Two copies would eventually disagree about where a turn ends, and the
        symptom (tap-to-seek hitting the wrong sentence) is hard to trace."""
        self.assertIs(transcribe.build_diarized_text,
                      stt_result.build_diarized_text)
        self.assertIs(transcribe.build_timestamps, stt_result.build_timestamps)

    def test_non_word_tokens_are_dropped(self):
        text, _, _ = stt_result.parse(TRANSCRIPTION)
        self.assertNotIn("(cough)", text)

    def test_timestamps_use_decimal_for_dynamodb(self):
        _, segs, _ = stt_result.parse(TRANSCRIPTION)
        self.assertIsInstance(segs[0]["start"], Decimal)

    def test_flat_text_fallback_when_not_diarized(self):
        text, segs, lang = stt_result.parse({"text": "just flat text"})
        self.assertEqual(text, "just flat text")
        self.assertEqual(segs, [])
        self.assertEqual(lang, "unknown")

    def test_webhook_and_polled_result_parse_identically(self):
        """A reconciled job and a webhook-delivered one must produce the same
        row — same shape in, same shape out."""
        self.assertEqual(stt_result.parse(TRANSCRIPTION),
                         stt_result.parse(dict(TRANSCRIPTION)))


# ===========================================================================
# BACKWARD COMPATIBILITY — the row's shape must not change
#
# The unified call restructured how the analysis is PRODUCED, not what is
# STORED. userApi reads these attributes by name (list view, documents, chat,
# task seeding, CRM push) and the mobile app reads userApi's JSON, so a renamed
# or dropped attribute breaks a deployed client that cannot be updated in step.
# ===========================================================================
class BackwardCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.written = {}
        self.removed = []

        def fake_upsert(key, fields, remove=()):
            self.written.update(fields)
            self.removed.extend(remove)

        mock.patch.object(transcribe, "_upsert",
                          side_effect=fake_upsert).start()
        mock.patch.object(transcribe, "analyze_meeting",
                          return_value=ai_schema.coerce_unified(
                              UNIFIED_REPLY,
                              ai_schema.speaker_roster(DIARIZED))).start()
        table = mock.MagicMock()
        table.get_item.return_value = {"Item": {}}
        mock.patch.object(transcribe, "_table", table).start()
        s3 = mock.MagicMock()
        mock.patch.object(transcribe, "_s3", s3).start()
        self.addCleanup(mock.patch.stopall)

        transcribe.analyze_and_persist(
            BUCKET, KEY, DIARIZED,
            [{"speaker": "0", "start": Decimal("0"), "end": Decimal("2"),
              "text": "hi"}], "en")

    def test_every_attribute_userapi_reads_is_still_written(self):
        for attr in ("overview", "summary", "ai_tasks", "participants",
                     "language", "status", "meeting_highlights",
                     "transcript_fingerprint", "ai_version",
                     "source", "meeting_id", "recorded_at", "recording_id",
                     "s3_key", "created_at"):
            self.assertIn(attr, self.written, f"{attr} is no longer written")

    def test_the_overview_is_what_gets_written(self):
        sections = self.written["overview"]["sections"]
        self.assertEqual([s["title"] for s in sections],
                         ["Quotation", "Cost Reduction"])

    def test_the_flat_highlights_list_is_no_longer_written(self):
        """Superseded by the overview. Still REMOVED from old rows so a
        reprocessed recording does not keep a stale copy forever."""
        self.assertNotIn("highlights", self.written)
        self.assertIn("highlights", self.removed)

    def test_summary_is_derived_from_the_overview_not_generated(self):
        """`summary` survives as a flattened PREVIEW because the meetings list
        renders a snippet per row and the Salesforce push writes one long-text
        field. It costs no extra tokens and makes no claim the overview does
        not already make."""
        summary = self.written["summary"]
        self.assertIn("Quotation", summary)
        self.assertIn("4.2 lakh", summary)
        self.assertLessEqual(len(summary), transcribe.SUMMARY_PREVIEW_CHARS)

    def test_crm_records_is_not_written_by_the_analysis(self):
        """Extraction left this path. A stored value may be the user's own
        confirmed link, so it is neither written nor removed here."""
        self.assertNotIn("crm_records", self.written)
        self.assertNotIn("crm_records", self.removed)

    def test_ai_tasks_is_a_list_not_the_tasks_map(self):
        """`tasks` is a DIFFERENT, user-editable attribute behind Task
        Detail/Assign/Notify. Writing the analysis there would clobber it."""
        self.assertIsInstance(self.written["ai_tasks"], list)
        self.assertNotIn("tasks", self.written)

    def test_meeting_highlights_keeps_all_its_sections(self):
        self.assertEqual(set(self.written["meeting_highlights"]),
                         set(ai_schema.HIGHLIGHT_SECTIONS))

    def test_participants_keep_speaker_and_summary_keys(self):
        for p in self.written["participants"]:
            self.assertEqual(set(p), {"speaker", "summary"})

    def test_retired_attributes_are_still_removed(self):
        """A row written under the old shape must have them cleared, or the
        stale copy survives forever (a SET-only update never drops a field)."""
        for attr in ("agenda", "key_points", "decisions",
                     "pending_discussions", "action_items"):
            self.assertIn(attr, self.removed)

    def test_status_reaches_a_terminal_value(self):
        self.assertEqual(self.written["status"], "complete")

    def test_stt_request_id_is_not_cleared_on_completion(self):
        """It is the staleness fence: a late duplicate of THIS job must still be
        recognisable, and an older job's delivery still rejectable."""
        self.assertNotIn("stt_request_id", self.removed)


# ===========================================================================
# DEGRADATION — a failed analysis must still keep the transcript
# ===========================================================================
class DegradationTests(unittest.TestCase):
    def _run(self, analyze_side_effect):
        written = {}
        mock.patch.object(transcribe, "_upsert",
                          side_effect=lambda k, f, remove=(): written.update(f)
                          ).start()
        mock.patch.object(transcribe, "analyze_meeting",
                          side_effect=analyze_side_effect).start()
        mock.patch.object(transcribe, "_crm_mappings_for_user",
                          return_value=[]).start()
        table = mock.MagicMock()
        table.get_item.return_value = {"Item": {}}
        mock.patch.object(transcribe, "_table", table).start()
        mock.patch.object(transcribe, "_s3", mock.MagicMock()).start()
        self.addCleanup(mock.patch.stopall)
        transcribe.analyze_and_persist(BUCKET, KEY, DIARIZED, [], "en")
        return written

    def test_groq_failure_still_persists_the_transcript(self):
        written = self._run(lambda *a, **k: (_ for _ in ()).throw(
            groq_client.GroqError("429", status=429, retryable=True)))
        self.assertEqual(written["status"], "transcribed")
        self.assertIn("transcript_s3_key", written)

    def test_empty_summary_degrades_like_an_exception(self):
        """status="complete" with summary="" is indistinguishable from "still
        generating" to the app, which then spins forever."""
        empty = ai_schema.empty_unified()
        written = self._run(lambda *a, **k: empty)
        self.assertEqual(written["status"], "transcribed")

    def test_empty_meeting_highlights_are_not_stored(self):
        """Storing emptiness makes the workspace cache serve it forever instead
        of regenerating on first open."""
        written = self._run(lambda *a, **k: ai_schema.empty_unified())
        self.assertNotIn("meeting_highlights", written)


# ===========================================================================
# analyze_and_persist READS speaker_names OFF THE ROW — the other half of the
# wiring gap: analyze_meeting() accepting the kwarg is useless if the caller
# never fetches it from DynamoDB. Proves the row's rename map actually reaches
# analyze_meeting, not just that analyze_meeting can accept one.
# ===========================================================================
class SpeakerNamesRowWiringTests(unittest.TestCase):
    def _run(self, item):
        self.analyze_kwargs = None

        def fake_analyze(transcript, valid_ids=None, roster_source=None,
                         speaker_names=None):
            self.analyze_kwargs = {"speaker_names": speaker_names}
            return ai_schema.empty_unified()

        mock.patch.object(transcribe, "_upsert",
                          side_effect=lambda k, f, remove=(): None).start()
        mock.patch.object(transcribe, "analyze_meeting",
                          side_effect=fake_analyze).start()
        table = mock.MagicMock()
        table.get_item.return_value = {"Item": item}
        mock.patch.object(transcribe, "_table", table).start()
        mock.patch.object(transcribe, "_s3", mock.MagicMock()).start()
        self.addCleanup(mock.patch.stopall)
        transcribe.analyze_and_persist(BUCKET, KEY, DIARIZED, [], "en")

    def test_speaker_names_on_the_row_reaches_analyze_meeting(self):
        self._run({"audio_s3_key": KEY, "speaker_names": {"0": "Yuvraj Sir"}})
        self.assertEqual(self.analyze_kwargs["speaker_names"],
                         {"0": "Yuvraj Sir"})

    def test_no_speaker_names_on_the_row_passes_an_empty_map(self):
        """No renames yet — must not crash, and must not invent a mapping."""
        self._run({"audio_s3_key": KEY})
        self.assertEqual(self.analyze_kwargs["speaker_names"], {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
