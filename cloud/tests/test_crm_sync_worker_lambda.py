#!/usr/bin/env python3
"""test_crm_sync_worker_lambda.py — Phase 2D.4 deployment hardening: the
CRM sync worker Lambda's own lambda_handler, specifically its
ChangeMessageVisibility retry-timing fix.

WHY THIS FILE EXISTS SEPARATELY from test_crm_sync_jobs.py. Every other
CRM-sync test drives execute_crm_sync_job directly (simulating "the worker
already received this message and is now running the job"), which is the
right level for identity/idempotency/security coverage but never exercises
crm-sync-worker/lambda_function.py's own handler — the code that actually
turns a _CrmSyncJobRetry into an SQS ChangeMessageVisibility call. That is
exactly the code this hardening pass changed, so it needs its own direct
test.

WHY THE IMPORT LOOKS UNUSUAL. crm-sync-worker/lambda_function.py does
`import userapi_worker as userapi` — in the DEPLOYED Lambda, the deploy
script (scripts/65_deploy_crm_sync_worker.sh) packages a COPY of
functions/userapi/lambda_function.py under that exact module name, next to
the worker file, in the zip. There is no such file in the repo (deliberate
— a copy would drift from the source of truth; see the worker module's own
docstring). To test the worker's actual handler code without introducing
one, this file registers the SAME lambda_function module object that
test_ai_workspace.py already imports (aliased `api` there) under the name
"userapi_worker" in sys.modules BEFORE importing the worker module — this
is exactly what the packaged deployment achieves (a module literally
importable as `userapi_worker` that IS functions/userapi/lambda_function.py),
just without a second file on disk.

OFFLINE: fake_dynamodb, no AWS, no network. The worker's own _sqs_client
calls (ChangeMessageVisibility) go through the same patched MagicMock the
other CRM sync tests use.

Run:  python -m pytest tests/test_crm_sync_worker_lambda.py
"""
import importlib
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT / "functions/userapi"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# ORDER MATTERS — see test_task_permissions.py: importing test_ai_workspace
# first ensures functions/userapi/lambda_function.py is loaded (as module
# name "lambda_function") exactly once, shared with every other CRM-sync
# test file in this run rather than re-imported under a second identity.
from test_ai_workspace import api  # noqa: E402
from test_crm_sync_jobs import CrmSyncJobHarness, KEY, ORG_A  # noqa: E402

# See module docstring: make functions/userapi/lambda_function.py
# importable as "userapi_worker", exactly like the deployed zip's packaged
# copy would be, then import the worker's own handler module under a name
# that won't collide with the real package path (there is a dash in
# "crm-sync-worker", so it can't be imported as a normal dotted module).
sys.modules["userapi_worker"] = api
_worker_spec = importlib.util.spec_from_file_location(
    "crm_sync_worker_lambda",
    ROOT / "functions/crm-sync-worker/lambda_function.py")
worker = importlib.util.module_from_spec(_worker_spec)
_worker_spec.loader.exec_module(worker)


def sqs_record(job_id, workspace_id=ORG_A, recording_key=KEY,
               operation="organisation_meeting_crm", message_id="msg-1",
               receipt_handle="rh-1"):
    return {
        "messageId": message_id,
        "receiptHandle": receipt_handle,
        "body": json.dumps({
            "job_id": job_id, "workspace_id": workspace_id,
            "recording_key": recording_key, "operation": operation,
        }),
    }


class TestWorkerAppliesComputedBackoff(CrmSyncJobHarness):
    """The core deployment-hardening fix: a retryable failure's actual SQS
    redelivery delay must match CRM_JOB_BACKOFF_SECONDS' per-attempt value
    (60s first, not the queue's fixed 300s VisibilityTimeout)."""

    def test_first_retry_sets_visibility_to_60_seconds_not_the_queue_default(self):
        self._fully_resolve()
        _, job = self.enqueue()
        job_id = job["job_id"]
        api._sqs_client.reset_mock()

        with mock.patch.object(api._salesforce, "update_record", return_value=None), \
             mock.patch.object(api._salesforce, "create_record",
                               side_effect=api.ApiError(500, "Salesforce unavailable")):
            result = worker.lambda_handler(
                {"Records": [sqs_record(job_id, receipt_handle="rh-attempt-1")]}, None)

        # Message reported as needing redelivery.
        self.assertEqual(result["batchItemFailures"], [{"itemIdentifier": "msg-1"}])
        # The ACTUAL delay applied is the job's first backoff entry (60s),
        # not the queue's fixed 300s VisibilityTimeout default.
        api._sqs_client.change_message_visibility.assert_called_once_with(
            QueueUrl=api.CRM_SYNC_QUEUE_URL,
            ReceiptHandle="rh-attempt-1",
            VisibilityTimeout=api.CRM_JOB_BACKOFF_SECONDS[0],
        )
        self.assertEqual(api.CRM_JOB_BACKOFF_SECONDS[0], 60)

    def test_second_retry_uses_the_second_backoff_value(self):
        self._fully_resolve()
        _, job = self.enqueue()
        job_id = job["job_id"]

        with mock.patch.object(api._salesforce, "update_record", return_value=None), \
             mock.patch.object(api._salesforce, "create_record",
                               side_effect=api.ApiError(500, "Salesforce unavailable")):
            worker.lambda_handler(
                {"Records": [sqs_record(job_id, receipt_handle="rh-attempt-1")]}, None)
            api._sqs_client.reset_mock()
            worker.lambda_handler(
                {"Records": [sqs_record(job_id, receipt_handle="rh-attempt-2")]}, None)

        api._sqs_client.change_message_visibility.assert_called_once_with(
            QueueUrl=api.CRM_SYNC_QUEUE_URL,
            ReceiptHandle="rh-attempt-2",
            VisibilityTimeout=api.CRM_JOB_BACKOFF_SECONDS[1],
        )
        self.assertEqual(api.CRM_JOB_BACKOFF_SECONDS[1], 300)

    def test_final_terminal_failure_does_not_call_change_message_visibility(self):
        # A FAILED (non-retryable / attempts-exhausted) outcome never raises
        # _CrmSyncJobRetry, so there is nothing to apply a delay to — the
        # message is reported successful (deleted), not held for redelivery.
        self._fully_resolve()
        _, job = self.enqueue()
        job_id = job["job_id"]

        with mock.patch.object(api._salesforce, "update_record", return_value=None), \
             mock.patch.object(api._salesforce, "create_record",
                               side_effect=api.ApiError(403, "no permission")):
            api._sqs_client.reset_mock()
            result = worker.lambda_handler(
                {"Records": [sqs_record(job_id)]}, None)

        self.assertEqual(result["batchItemFailures"], [])
        api._sqs_client.change_message_visibility.assert_not_called()

    def test_reconnect_required_does_not_call_change_message_visibility(self):
        self._fully_resolve()
        _, job = self.enqueue()
        job_id = job["job_id"]

        with mock.patch.object(api._salesforce, "refresh_access_token",
                               return_value={}):  # dead refresh token
            api._sqs_client.reset_mock()
            result = worker.lambda_handler(
                {"Records": [sqs_record(job_id)]}, None)

        self.assertEqual(result["batchItemFailures"], [])
        api._sqs_client.change_message_visibility.assert_not_called()

    def test_successful_sync_does_not_call_change_message_visibility(self):
        self._fully_resolve()
        _, job = self.enqueue()
        job_id = job["job_id"]

        with mock.patch.object(api._salesforce, "update_record", return_value=None), \
             mock.patch.object(api._salesforce, "create_record",
                               return_value={"id": "a0B000000NewEvt"}):
            api._sqs_client.reset_mock()
            result = worker.lambda_handler(
                {"Records": [sqs_record(job_id)]}, None)

        self.assertEqual(result["batchItemFailures"], [])
        api._sqs_client.change_message_visibility.assert_not_called()
        final = api._get_crm_sync_job(job_id)
        self.assertEqual(final["status"], api.CRM_JOB_STATUS_SYNCED)

    def test_change_message_visibility_failure_does_not_crash_the_handler(self):
        # A best-effort call: if the receipt handle already expired (the
        # attempt itself ran long) ChangeMessageVisibility can legitimately
        # fail. The message must still be reported for redelivery via the
        # queue's own default timeout rather than the handler raising.
        self._fully_resolve()
        _, job = self.enqueue()
        job_id = job["job_id"]

        with mock.patch.object(api._salesforce, "update_record", return_value=None), \
             mock.patch.object(api._salesforce, "create_record",
                               side_effect=api.ApiError(500, "Salesforce unavailable")):
            api._sqs_client.change_message_visibility.side_effect = Exception(
                "ReceiptHandleIsInvalid")
            result = worker.lambda_handler(
                {"Records": [sqs_record(job_id)]}, None)

        self.assertEqual(result["batchItemFailures"], [{"itemIdentifier": "msg-1"}])

    def test_missing_receipt_handle_skips_the_call_without_crashing(self):
        self._fully_resolve()
        _, job = self.enqueue()
        job_id = job["job_id"]

        record = sqs_record(job_id)
        del record["receiptHandle"]
        with mock.patch.object(api._salesforce, "update_record", return_value=None), \
             mock.patch.object(api._salesforce, "create_record",
                               side_effect=api.ApiError(500, "Salesforce unavailable")):
            api._sqs_client.reset_mock()
            result = worker.lambda_handler({"Records": [record]}, None)

        self.assertEqual(result["batchItemFailures"], [{"itemIdentifier": "msg-1"}])
        api._sqs_client.change_message_visibility.assert_not_called()


class TestWorkerSecurityUnchanged(CrmSyncJobHarness):
    """Confirms this hardening pass did not weaken the worker's existing
    security posture: the message body is still never trusted for
    authorization, only job_id is used to look up authoritative state."""

    def test_worker_ignores_a_forged_workspace_id_in_the_message_body(self):
        self._fully_resolve()
        _, job = self.enqueue()
        job_id = job["job_id"]

        forged = sqs_record(job_id, workspace_id="wso_forgedforged")
        with mock.patch.object(api._salesforce, "update_record", return_value=None), \
             mock.patch.object(api._salesforce, "create_record",
                               return_value={"id": "a0B000000NewEvt"}):
            result = worker.lambda_handler({"Records": [forged]}, None)

        self.assertEqual(result["batchItemFailures"], [])
        final = api._get_crm_sync_job(job_id)
        self.assertEqual(final["status"], api.CRM_JOB_STATUS_SYNCED)
        self.assertEqual(final["workspace_id"], ORG_A)  # the JOB ROW's own value, not the forged one


if __name__ == "__main__":
    unittest.main()
