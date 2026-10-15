#!/usr/bin/env python3
"""test_crm_sync_worker_lambda.py — the crmSyncWorker Lambda's own handler.

The job executors are covered where they live (test_site_delivery.py and
test_site_salesforce.py drain the queue through this handler end to end).
This file pins the HANDLER itself, with the executor mocked: every message
goes to execute_site_delivery_job by job_id only, a _CrmSyncJobRetry becomes
an SQS ChangeMessageVisibility with the job's own backoff, and nothing else
does.

WHY THE IMPORT LOOKS UNUSUAL. crm-sync-worker/lambda_function.py does
`import userapi_worker as userapi` — in the DEPLOYED Lambda the deploy script
(scripts/65_deploy_crm_sync_worker.sh) packages a COPY of
functions/userapi/lambda_function.py under that name. This file registers the
SAME module object test_ai_workspace.py already imports (aliased `api`) as
"userapi_worker" before loading the worker, which is exactly what the
packaged deployment achieves, without a second file on disk.

OFFLINE: no AWS, no network.

Run:  python -m pytest tests/test_crm_sync_worker_lambda.py
"""
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
# name "lambda_function") exactly once, shared with every other test file.
from test_ai_workspace import api  # noqa: E402

sys.modules["userapi_worker"] = api
_worker_spec = importlib.util.spec_from_file_location(
    "crm_sync_worker_lambda",
    ROOT / "functions/crm-sync-worker/lambda_function.py")
worker = importlib.util.module_from_spec(_worker_spec)
_worker_spec.loader.exec_module(worker)

WORKSPACE = "wso_aaaaaaaaaaaaaaaa"
KEY = "recordings/u-own/mobile/mobile-site_1757000000.wav"


def sqs_record(job_id="job-1", workspace_id=WORKSPACE, recording_key=KEY,
               operation="salesforce_site_meeting", message_id="msg-1",
               receipt_handle="rh-1"):
    return {
        "messageId": message_id,
        "receiptHandle": receipt_handle,
        "body": json.dumps({
            "job_id": job_id, "workspace_id": workspace_id,
            "recording_key": recording_key, "operation": operation,
        }),
    }


class WorkerHarness(unittest.TestCase):
    def setUp(self):
        self.sqs = mock.MagicMock()
        patches = [
            mock.patch.object(api, "_sqs_client", self.sqs),
            mock.patch.object(api, "CRM_SYNC_QUEUE_URL", "https://sqs.test/CrmSyncQueue"),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

    def run_with(self, records, side_effect=None):
        with mock.patch.object(api, "execute_site_delivery_job",
                               side_effect=side_effect) as execute:
            out = worker.lambda_handler({"Records": records}, None)
        return out, execute


class TestWorkerAppliesComputedBackoff(WorkerHarness):
    """A retryable outcome's real SQS redelivery delay must be the job's own
    backoff (60s first), not the queue's fixed 300s VisibilityTimeout."""

    def test_retry_sets_visibility_to_the_jobs_backoff(self):
        out, _ = self.run_with(
            [sqs_record(receipt_handle="rh-attempt-1")],
            side_effect=api._CrmSyncJobRetry("job-1", api.CRM_JOB_BACKOFF_SECONDS[0]))
        self.assertEqual(out["batchItemFailures"], [{"itemIdentifier": "msg-1"}])
        self.sqs.change_message_visibility.assert_called_once_with(
            QueueUrl="https://sqs.test/CrmSyncQueue", ReceiptHandle="rh-attempt-1",
            VisibilityTimeout=60)

    def test_second_retry_uses_the_second_backoff_value(self):
        self.run_with([sqs_record()],
                      side_effect=api._CrmSyncJobRetry("job-1", api.CRM_JOB_BACKOFF_SECONDS[1]))
        self.assertEqual(self.sqs.change_message_visibility.call_args.kwargs["VisibilityTimeout"], 300)

    def test_finished_job_is_deleted_without_a_visibility_change(self):
        # SYNCED, FAILED and RECONNECT_REQUIRED all return normally.
        out, execute = self.run_with([sqs_record()])
        self.assertEqual(out["batchItemFailures"], [])
        execute.assert_called_once_with("job-1")
        self.sqs.change_message_visibility.assert_not_called()

    def test_visibility_failure_still_reports_the_message_for_redelivery(self):
        self.sqs.change_message_visibility.side_effect = Exception("ReceiptHandleIsInvalid")
        out, _ = self.run_with([sqs_record()], side_effect=api._CrmSyncJobRetry("job-1", 60))
        self.assertEqual(out["batchItemFailures"], [{"itemIdentifier": "msg-1"}])

    def test_missing_receipt_handle_skips_the_call_without_crashing(self):
        record = sqs_record()
        del record["receiptHandle"]
        out, _ = self.run_with([record], side_effect=api._CrmSyncJobRetry("job-1", 60))
        self.assertEqual(out["batchItemFailures"], [{"itemIdentifier": "msg-1"}])
        self.sqs.change_message_visibility.assert_not_called()

    def test_an_escaping_exception_is_a_batch_item_failure(self):
        out, _ = self.run_with([sqs_record()], side_effect=RuntimeError("DynamoDB down"))
        self.assertEqual(out["batchItemFailures"], [{"itemIdentifier": "msg-1"}])


class TestWorkerRoutingAndSecurity(WorkerHarness):
    def test_only_job_id_reaches_the_executor(self):
        # A forged workspace/meeting/operation in the body is logged, never
        # used: the executor re-reads the authoritative job row by job_id.
        _, execute = self.run_with([sqs_record(workspace_id="wso_forgedforged",
                                               recording_key="recordings/other/x.wav",
                                               operation="anything")])
        execute.assert_called_once_with("job-1")

    def test_malformed_and_id_less_messages_are_dropped(self):
        bad = {"messageId": "m-bad", "receiptHandle": "rh", "body": "{not json"}
        no_id = sqs_record(job_id="", message_id="m-noid")
        out, execute = self.run_with([bad, no_id])
        self.assertEqual(out["batchItemFailures"], [])
        execute.assert_not_called()

    def test_each_message_in_a_batch_is_reported_on_its_own(self):
        def outcome(job_id):
            if job_id == "job-retry":
                raise api._CrmSyncJobRetry(job_id, 60)
        out, execute = self.run_with(
            [sqs_record(job_id="job-ok", message_id="m1"),
             sqs_record(job_id="job-retry", message_id="m2")], side_effect=outcome)
        self.assertEqual(out["batchItemFailures"], [{"itemIdentifier": "m2"}])
        self.assertEqual(execute.call_count, 2)


if __name__ == "__main__":
    unittest.main()


class TestWebhookJobsNeverUseTheQueueRetry(WorkerHarness):
    """Webhook Contract v2: a retry is a FRESH delayed message sent by the
    runner itself, never a batch item failure — the queue's maxReceiveCount
    must not be able to DLQ a webhook still inside its attempt budget."""

    def test_a_retrying_webhook_job_is_not_a_batch_item_failure(self):
        job = {"job_id": "whk_1", "operation": api.SITE_OP_WEBHOOK_EVENT,
               "status": api.CRM_JOB_STATUS_RETRYING}
        with mock.patch.object(api, "_get_crm_sync_job", return_value=job), \
                mock.patch.object(api, "_run_webhook_event_job",
                                  return_value=job) as runner:
            out = worker.lambda_handler({"Records": [sqs_record(
                job_id="whk_1", operation=api.SITE_OP_WEBHOOK_EVENT)]}, None)
        runner.assert_called_once_with(job)
        self.assertEqual(out["batchItemFailures"], [])
        self.sqs.change_message_visibility.assert_not_called()
