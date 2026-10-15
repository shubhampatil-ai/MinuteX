#!/usr/bin/env bash
# =============================================================
# 63_create_crm_sync_jobs_table.sh — durable CRM sync job state (Phase 2D.4).
#
# Table: CrmSyncJobs  (override with CRM_SYNC_JOBS_TABLE in .env)
#   Partition key: job_id  (String, uuid4 hex)
#   Billing:       PAY_PER_REQUEST
#
# WHY THIS TABLE EXISTS. Phase 2D.3's push_org_meeting_crm ran every
# Salesforce call INLINE, inside the API request — correct, but it means a
# slow or unavailable Salesforce org makes the app wait, and there is
# nowhere durable to record "this push is queued/retrying/dead" once the
# HTTP response has already gone back to the client. This table is the
# single source of truth for one asynchronous CRM push attempt, read by:
#   - the API route that enqueues it (push_org_meeting_crm, now a fast
#     "create the job, return immediately" call)
#   - the CRM worker Lambda (crmSyncWorker) that performs the actual
#     Salesforce operations
#   - the status route the app polls for "queued / syncing / synced / failed
#     / reconnect required"
#
# THIS DOES NOT REPLACE crm_event_synced / crm_tasks_synced / crm_records.
# Those remain the per-OPERATION idempotency record on the Recordings row
# (Phase 2D.3) — a Task or Event is still only ever created once, checked
# there, regardless of how many times a job or its SQS message is retried.
# CrmSyncJobs is the OUTER envelope: one row per push ATTEMPT, so the app has
# something to show status for and the worker has somewhere to record
# progress across retries. See docs/WORKSPACE_PHASE2D.md Phase 2D.4 section
# for the full state machine.
#
#   Item shape:
#     job_id              PK — uuid4 hex, ALSO the deterministic
#                          idempotency key material (see below)
#     workspace_id         the organisation this job belongs to
#     recording_key         the meeting (Recordings.audio_s3_key)
#     requested_by_user_id   who triggered the push
#     operation             "organisation_meeting_crm" (room for a narrower
#                          operation later, e.g. re-syncing one object only)
#     status                PENDING | SYNCING | SYNCED | RETRYING | FAILED |
#                          RECONNECT_REQUIRED  (CRM_JOB_STATUS_* in
#                          lambda_function.py)
#     attempt_count          integer, starts at 0
#     idempotency_key        deterministic: "{workspace_id}:{recording_key}:
#                          {operation}" — see the "idempotency key" note in
#                          lambda_function.py's create_crm_sync_job
#     next_attempt_at        ISO timestamp, when the worker may next try
#                          (backoff — the worker/queue visibility timeout is
#                          the actual delay mechanism; this field is the
#                          durable record of intent, read by the status API)
#     last_error_code        stable machine code (e.g.
#                          "salesforce_reconnect_required"), "" when none yet
#     last_error_message      human-readable, Salesforce's own wording where
#                          available
#     result                 {} — the same {pushed, push_errors, tasks,
#                          event} shape push_org_meeting_crm already returns
#                          synchronously in 2D.3, so the frontend polling
#                          route needs no new response shape to learn
#     created_at / updated_at / completed_at
#
# No GSI: the worker and the status route both look up by job_id directly.
# "List jobs for this meeting" (used to detect an ALREADY-QUEUED job before
# creating a duplicate — see create_crm_sync_job) queries by
# recording_key via a GSI, added below because that access pattern is real
# and would otherwise require a table scan.
#
# Idempotent: no-op if the table already exists.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

CRM_SYNC_JOBS_TABLE="${CRM_SYNC_JOBS_TABLE:-CrmSyncJobs}"

echo ">> Region: $AWS_REGION"
echo ">> Table:  $CRM_SYNC_JOBS_TABLE"

if aws dynamodb describe-table --table-name "$CRM_SYNC_JOBS_TABLE" >/dev/null 2>&1; then
  status="$(aws dynamodb describe-table --table-name "$CRM_SYNC_JOBS_TABLE" \
              --query 'Table.TableStatus' --output text)"
  echo ">> Table '$CRM_SYNC_JOBS_TABLE' already exists (status: $status). Nothing to do."
  exit 0
fi

echo ">> Creating table '$CRM_SYNC_JOBS_TABLE' ..."
aws dynamodb create-table \
  --table-name "$CRM_SYNC_JOBS_TABLE" \
  --attribute-definitions \
      AttributeName=job_id,AttributeType=S \
      AttributeName=recording_key,AttributeType=S \
      AttributeName=created_at,AttributeType=S \
  --key-schema \
      AttributeName=job_id,KeyType=HASH \
  --global-secondary-indexes \
      '[{
        "IndexName": "recording-index",
        "KeySchema": [
          {"AttributeName": "recording_key", "KeyType": "HASH"},
          {"AttributeName": "created_at", "KeyType": "RANGE"}
        ],
        "Projection": {"ProjectionType": "ALL"}
      }]' \
  --billing-mode PAY_PER_REQUEST >/dev/null

echo ">> Waiting for table to become ACTIVE ..."
aws dynamodb wait table-exists --table-name "$CRM_SYNC_JOBS_TABLE"
echo ">> Table '$CRM_SYNC_JOBS_TABLE' is ACTIVE."
