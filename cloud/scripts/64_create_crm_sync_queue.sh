#!/usr/bin/env bash
# =============================================================
# 64_create_crm_sync_queue.sh — the CRM sync SQS queue + DLQ (Phase 2D.4).
#
# THIS IS THE FIRST SQS USAGE IN THIS CODEBASE. Verified before writing this
# script: AWS_ARCHITECTURE.md's read-only capture of the live account
# explicitly lists SQS as a service confirmed ABSENT, and no script anywhere
# creates a queue. There is no existing queue-naming or redrive-policy
# convention to match — this script establishes one, following the same
# aws.sh/idempotency style every other resource-creation script in this
# repo uses.
#
# STANDARD queue, not FIFO. FIFO buys strict ordering and exactly-once
# delivery semantics that this workload does not need: every operation the
# worker performs (crm_sync_record's per-object push, Task creation, Event
# creation) is ALREADY individually idempotent via durable DynamoDB state
# (crm_records status, crm_tasks_synced, crm_event_synced) — see Phase 2D.3.
# FIFO's throughput ceiling (300 msg/s per message group without batching)
# and added operational complexity (message group ids, dedup ids) would be
# pure cost for a queue whose correctness never depended on ordering in the
# first place. Standard SQS's "at-least-once, possibly out-of-order,
# possibly duplicated" delivery model is exactly what the worker is already
# built to tolerate.
#
# Resources created:
#   1. CrmSyncDLQ    — dead-letter queue. Long retention (14 days, SQS's
#                      maximum) so a human has real time to investigate
#                      before a poison message is lost for good.
#   2. CrmSyncQueue   — source queue, with a redrive policy pointing at the
#                      DLQ and maxReceiveCount from CRM_SYNC_MAX_RECEIVES
#                      (default 5 — see docs/WORKSPACE_PHASE2D.md's Phase
#                      2D.4 retry-policy section for why 5, not some other
#                      number). Visibility timeout is set generously above
#                      the worker Lambda's own timeout (see
#                      65_deploy_crm_sync_worker.sh) so SQS never returns a
#                      message to the queue WHILE the worker is still
#                      legitimately processing it — the single most common
#                      cause of an accidental duplicate delivery.
#
# Idempotent: no-op if the queue already exists (by name).
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

CRM_SYNC_QUEUE_NAME="${CRM_SYNC_QUEUE_NAME:-CrmSyncQueue}"
CRM_SYNC_DLQ_NAME="${CRM_SYNC_DLQ_NAME:-CrmSyncDLQ}"
# Must exceed the worker Lambda's own --timeout (65_deploy_crm_sync_worker.sh
# sets 120s) with real headroom — SQS visibility timeout is "how long a
# message is invisible to other receivers while one worker holds it", not a
# processing deadline of its own. Too short and a slow-but-succeeding
# Salesforce call causes SQS to hand the SAME message to a second worker
# before the first one finishes — a duplicate-delivery scenario the
# idempotency layer tolerates, but avoiding it outright is cheaper.
#
# This default is also the FALLBACK retry delay if the worker's own
# ChangeMessageVisibility call (applied per-message on a retryable failure,
# see crm-sync-worker/lambda_function.py) doesn't run or fails — e.g. a
# receipt handle that expired because the attempt itself took the full
# visibility window. The job's actual intended backoff
# (CRM_JOB_BACKOFF_SECONDS = 60s/300s/900s in userapi/lambda_function.py) is
# applied on top of this default via that per-message override, so a first
# retry really does wait ~60s rather than always ~300s.
CRM_SYNC_VISIBILITY_TIMEOUT="${CRM_SYNC_VISIBILITY_TIMEOUT:-300}"
# After this many DELIVERY attempts (not app-level retries — see the
# worker's own attempt_count bookkeeping, which is a separate, job-level
# counter persisted in CrmSyncJobs) without a successful deletion, SQS
# moves the message to the DLQ rather than redelivering forever.
CRM_SYNC_MAX_RECEIVES="${CRM_SYNC_MAX_RECEIVES:-5}"
# Long polling — avoids empty-receive cost/latency; 20s is SQS's own maximum.
CRM_SYNC_WAIT_TIME_SECONDS="${CRM_SYNC_WAIT_TIME_SECONDS:-20}"

echo ">> Region: $AWS_REGION"
echo ">> Queue:  $CRM_SYNC_QUEUE_NAME  (DLQ: $CRM_SYNC_DLQ_NAME)"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

# -------------------------------------------------------------
# 1. Dead-letter queue.
# -------------------------------------------------------------
DLQ_URL="$(aws sqs get-queue-url --queue-name "$CRM_SYNC_DLQ_NAME" \
             --query 'QueueUrl' --output text 2>/dev/null || true)"
if [[ -n "$DLQ_URL" && "$DLQ_URL" != "None" ]]; then
  echo ">> DLQ already exists: $DLQ_URL"
else
  DLQ_URL="$(aws sqs create-queue \
      --queue-name "$CRM_SYNC_DLQ_NAME" \
      --attributes "MessageRetentionPeriod=1209600" \
      --query 'QueueUrl' --output text)"
  echo ">> Created DLQ: $DLQ_URL"
fi
DLQ_ARN="arn:aws:sqs:${AWS_REGION}:${ACCOUNT_ID}:${CRM_SYNC_DLQ_NAME}"

# -------------------------------------------------------------
# 2. Source queue, with redrive policy pointing at the DLQ.
#
# --attributes is built as ONE JSON OBJECT (via python -c json.dumps, the
# same pattern this repo already uses for Lambda --environment payloads —
# see e.g. 23_deploy_salesforce_oauth.sh) rather than the CLI's comma-
# separated shorthand form. Shorthand splits on top-level "," and "=" with
# no awareness that RedrivePolicy's OWN value is a JSON object containing
# both — it fails to parse (ParamValidationError) the moment it reaches
# RedrivePolicy={"..."cursor. SQS's Attributes map requires every value to
# be a STRING regardless of syntax used, so RedrivePolicy's value here is
# itself json.dumps'd (a JSON object encoded AS a string), matching exactly
# what the shorthand form was always trying (and failing) to express.
# -------------------------------------------------------------
ATTRS_JSON="$(python - "$CRM_SYNC_VISIBILITY_TIMEOUT" "$CRM_SYNC_WAIT_TIME_SECONDS" \
    "$DLQ_ARN" "$CRM_SYNC_MAX_RECEIVES" <<'PY'
import json, sys
visibility_timeout, wait_time, dlq_arn, max_receives = sys.argv[1:5]
redrive_policy = json.dumps({
    "deadLetterTargetArn": dlq_arn,
    "maxReceiveCount": int(max_receives),
})
print(json.dumps({
    "VisibilityTimeout": visibility_timeout,
    "ReceiveMessageWaitTimeSeconds": wait_time,
    "RedrivePolicy": redrive_policy,
}))
PY
)"

QUEUE_URL="$(aws sqs get-queue-url --queue-name "$CRM_SYNC_QUEUE_NAME" \
               --query 'QueueUrl' --output text 2>/dev/null || true)"
if [[ -n "$QUEUE_URL" && "$QUEUE_URL" != "None" ]]; then
  echo ">> Queue already exists: $QUEUE_URL — updating its redrive/visibility attributes"
  aws sqs set-queue-attributes \
    --queue-url "$QUEUE_URL" \
    --attributes "$ATTRS_JSON" \
    >/dev/null
else
  QUEUE_URL="$(aws sqs create-queue \
      --queue-name "$CRM_SYNC_QUEUE_NAME" \
      --attributes "$ATTRS_JSON" \
      --query 'QueueUrl' --output text)"
  echo ">> Created queue: $QUEUE_URL"
fi
QUEUE_ARN="arn:aws:sqs:${AWS_REGION}:${ACCOUNT_ID}:${CRM_SYNC_QUEUE_NAME}"

echo
echo ">> Done."
echo ">> Add these to .env:"
echo "     CRM_SYNC_QUEUE_URL=${QUEUE_URL}"
echo "     CRM_SYNC_QUEUE_ARN=${QUEUE_ARN}"
echo "     CRM_SYNC_DLQ_URL=${DLQ_URL}"
echo "     CRM_SYNC_DLQ_ARN=${DLQ_ARN}"
