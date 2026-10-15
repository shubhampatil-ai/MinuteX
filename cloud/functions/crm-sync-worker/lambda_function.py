# cloud/functions/crm-sync-worker/lambda_function.py — the job worker for
# the Salesforce Site flow (delivery_type = salesforce_site).
#
# WHAT THIS IS. An SQS-triggered Lambda that runs one CrmSyncJobs job per
# message — meeting delivery, people sync, object discovery, Salesforce Task
# create/update (userapi.SITE_DELIVERY_OPERATIONS) — so the HTTP routes can
# enqueue a job and return immediately instead of blocking on Salesforce.
#
# WHY THIS FILE IMPORTS userapi's lambda_function DIRECTLY. The job logic is
# deeply intertwined with userapi's table clients and helpers; a second copy
# here would be a state machine that must never drift from the first. The
# deploy script bundles a copy of userapi's lambda_function.py (plus
# shared/) into THIS Lambda's own zip, exactly like every other Lambda here
# bundles shared/ flat.
#
# THIS LAMBDA HAS NO HTTP ROUTES OF ITS OWN. It is never reachable through
# API Gateway; its only trigger is the CRM sync SQS queue (event source
# mapping, see scripts/65_deploy_crm_sync_worker.sh). There is no
# `_ROUTES` dispatch here — SQS-triggered Lambdas receive a batch of queue
# messages, not an HTTP event, so this file's shape is deliberately
# different from userapi's/transcribe's dual (HTTP + internal-invoke) entry
# points.
#
# ERROR REPORTING: PARTIAL BATCH FAILURE. SQS can deliver up to 10 messages
# per invocation. Returning a bare exception (or nothing useful) from this
# handler would make Lambda/SQS treat the WHOLE BATCH as failed and
# redeliver every message, including ones that already succeeded — a
# needless duplicate-processing risk this codebase's idempotency layer
# would absorb but that costs real receiver calls and worker time for no
# reason. Instead this handler reports exactly which message ids need
# retrying via `batchItemFailures`, which requires the event source mapping
# to be created with `FunctionResponseTypes=["ReportBatchItemFailures"]`
# (see the deploy script) — a message not listed there is treated as
# successfully processed and deleted from the queue even if OTHER messages
# in the same batch failed.
import json
import sys
import traceback
from pathlib import Path

# The deploy script (scripts/65_deploy_crm_sync_worker.sh) packages a copy
# of functions/userapi/lambda_function.py at THIS Lambda's own archive
# root, alongside shared/*.py — so this import resolves exactly like every
# other Lambda's `import ai_schema`/`import workspace_schema` does, via the
# runtime's own working directory being on sys.path. The explicit
# sys.path insert below is a LOCAL-DEV/TEST convenience only (running this
# file's tests without the packaged layout); it is a no-op in the deployed
# Lambda, where the file already sits next to userapi_worker.py at the
# archive root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import userapi_worker as userapi  # noqa: E402  (see the note above)


def lambda_handler(event, context):
    """SQS trigger. `event["Records"]` is a batch of CRM sync queue
    messages (see userapi.CRM_SYNC_QUEUE_URL / _enqueue_crm_sync_job for the
    message shape: job_id, workspace_id, recording_key, operation — no
    credential, no meeting content, ever).
    """
    batch_item_failures = []

    for record in (event or {}).get("Records", []):
        message_id = record.get("messageId", "")
        try:
            payload = json.loads(record.get("body", "{}"))
        except (TypeError, ValueError) as e:
            # A malformed message can never succeed no matter how many times
            # it is redelivered — do NOT report it as a failure (that would
            # just redeliver the same unparseable body forever until the DLQ
            # catches it anyway); log it and move on. The DLQ's own
            # redrive-count-based catch is the real backstop for this case,
            # not an infinite local retry.
            print(f"[crm-sync-worker] malformed message {message_id}: "
                  f"{type(e).__name__}: {e}")
            continue

        job_id = str(payload.get("job_id") or "")
        if not job_id:
            print(f"[crm-sync-worker] message {message_id} has no job_id — dropping")
            continue

        # SECURITY: workspace_id/recording_key/operation in the message are
        # NEVER trusted for authorization — only job_id is used to look up
        # the job, and execute_site_delivery_job re-reads the AUTHORITATIVE
        # operation/workspace/meeting from the CrmSyncJobs row and
        # re-validates that state itself (a row that is not a Site job is
        # dropped there). The message
        # body fields exist only for structured logging here, before the
        # job row has even been read.
        workspace_id = str(payload.get("workspace_id") or "")
        recording_key = str(payload.get("recording_key") or "")
        operation = str(payload.get("operation") or "")
        print(f"[crm-sync-worker] received job={job_id} workspace={workspace_id} "
              f"meeting={recording_key} operation={operation} message={message_id}")

        try:
            userapi.execute_site_delivery_job(job_id)
        except userapi._CrmSyncJobRetry as retry:
            # A RETRYING outcome — the job row already records the intended
            # next_attempt_at. Reporting this message as a batch item
            # failure is what keeps it in the queue (SQS does not delete
            # it), but the QUEUE's fixed VisibilityTimeout (300s, see
            # scripts/64_create_crm_sync_queue.sh) is not the same number as
            # the job's own computed backoff (60s/300s/900s) — a naive first
            # retry would always wait ~300s instead of the intended 60s.
            # ChangeMessageVisibility overrides the remaining visibility
            # window for THIS message only, so the actual redelivery delay
            # matches retry_after_seconds. Best-effort: if this call fails
            # (e.g. the receipt handle expired because processing itself
            # took longer than the visibility timeout), the message still
            # redelivers via the queue's own default timeout — never worse
            # than before this change, and the job-level attempt/backoff
            # bookkeeping in CrmSyncJobs is authoritative either way.
            delay = max(0, int(retry.retry_after_seconds or 0))
            receipt_handle = record.get("receiptHandle", "")
            if delay and receipt_handle:
                try:
                    userapi._sqs_client.change_message_visibility(
                        QueueUrl=userapi.CRM_SYNC_QUEUE_URL,
                        ReceiptHandle=receipt_handle,
                        VisibilityTimeout=delay,
                    )
                    print(f"[crm-sync-worker] job={job_id} retry delay set to "
                          f"{delay}s via ChangeMessageVisibility")
                except Exception as e:  # noqa: BLE001
                    print(f"[crm-sync-worker] job={job_id} ChangeMessageVisibility "
                          f"failed, falling back to queue default: "
                          f"{type(e).__name__}: {e}")
            print(f"[crm-sync-worker] job={job_id} scheduled for retry "
                  f"(delay={delay}s)")
            batch_item_failures.append({"itemIdentifier": message_id})
        except Exception as e:  # noqa: BLE001
            # An exception ESCAPING the executor means something failed
            # before it could persist a FAILED/RETRYING state itself (e.g. a
            # DynamoDB outage) — report it as a failure so SQS retries; the
            # job row (still SYNCING) tells the next attempt/an operator that
            # this run was interrupted uncleanly.
            print(f"[crm-sync-worker] job={job_id} worker-level failure: "
                  f"{type(e).__name__}: {e}\n{traceback.format_exc()}")
            batch_item_failures.append({"itemIdentifier": message_id})
        else:
            print(f"[crm-sync-worker] job={job_id} completed")

    return {"batchItemFailures": batch_item_failures}
