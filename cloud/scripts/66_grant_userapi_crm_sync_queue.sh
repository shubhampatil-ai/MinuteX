#!/usr/bin/env bash
# =============================================================
# 66_grant_userapi_crm_sync_queue.sh — extend userApi's role for the CRM
# sync job/queue (Phase 2D.4).
#
# push_org_meeting_crm (the enqueue route) and retry_crm_sync_job now write
# to CrmSyncJobs and call sqs:SendMessage on the CRM sync queue — userApi's
# EXISTING role needs exactly those two additions. Same additive-inline-
# policy pattern every other userApi-extending script uses (18, 19, 20, 23,
# 40, 58, 61); does not touch or replace any policy those scripts already
# attached.
#
# Grants ONLY:
#   - CrmSyncJobs: Get/Put/Update/Query (create/read/retry a job)
#   - sqs:SendMessage on the CRM sync queue (enqueue)
# Explicitly NOT granted to userApi: sqs:ReceiveMessage/DeleteMessage (only
# the worker consumes the queue — userApi only ever produces onto it) and
# nothing on the DLQ (userApi never reads it; that is an operator/console
# concern, see docs/WORKSPACE_PHASE2D.md's DLQ-inspection section).
#
# Idempotent: put-role-policy overwrites.
#
# Prerequisites: scripts/63_create_crm_sync_jobs_table.sh,
# scripts/64_create_crm_sync_queue.sh (CRM_SYNC_QUEUE_ARN/CRM_SYNC_QUEUE_URL
# in .env), userApi already deployed (scripts/17+).
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"
CRM_SYNC_JOBS_TABLE="${CRM_SYNC_JOBS_TABLE:-CrmSyncJobs}"
: "${CRM_SYNC_QUEUE_ARN:?CRM_SYNC_QUEUE_ARN not set — run scripts/64_create_crm_sync_queue.sh first}"
: "${CRM_SYNC_QUEUE_URL:?CRM_SYNC_QUEUE_URL not set — run scripts/64_create_crm_sync_queue.sh first}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

echo ">> Region: $AWS_REGION"
echo ">> userApi: $USERAPI_LAMBDA_NAME"

USERAPI_ROLE="$(aws lambda get-function --function-name "$USERAPI_LAMBDA_NAME" \
                  --query 'Configuration.Role' --output text)"
USERAPI_ROLE="${USERAPI_ROLE##*/}"
echo ">> userApi role: $USERAPI_ROLE"

POLICY_FILE="$(mktemp)"
trap 'rm -f "$POLICY_FILE"' EXIT
cat > "$POLICY_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "CrmSyncJobsReadWrite", "Effect": "Allow",
      "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
                 "dynamodb:Query"],
      "Resource": [
        "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${CRM_SYNC_JOBS_TABLE}",
        "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${CRM_SYNC_JOBS_TABLE}/index/*"
      ] },
    { "Sid": "CrmSyncQueueProduce", "Effect": "Allow",
      "Action": ["sqs:SendMessage"],
      "Resource": "${CRM_SYNC_QUEUE_ARN}" }
  ]
}
EOF
aws iam put-role-policy \
  --role-name "$USERAPI_ROLE" \
  --policy-name "userApi-crm-sync-queue" \
  --policy-document "file://$(winpath "$POLICY_FILE")"
echo ">> userApi-crm-sync-queue inline policy set (CrmSyncJobs, SQS SendMessage)."

# -------------------------------------------------------------
# Env vars: CRM_SYNC_JOBS_TABLE / CRM_SYNC_QUEUE_URL, merged into whatever
# userApi already has (same merge-not-replace pattern as every other
# userApi-extending deploy script).
# -------------------------------------------------------------
CURRENT_ENV="$(aws lambda get-function-configuration \
                 --function-name "$USERAPI_LAMBDA_NAME" \
                 --query 'Environment.Variables' --output json)"
MERGED_ENV="$(python - "$CURRENT_ENV" "$CRM_SYNC_JOBS_TABLE" "$CRM_SYNC_QUEUE_URL" <<'PY'
import json, sys
env_json, jobs_table, queue_url = sys.argv[1:4]
env = json.loads(env_json) if env_json not in ("null", "") else {}
env.update({
    "CRM_SYNC_JOBS_TABLE": jobs_table,
    "CRM_SYNC_QUEUE_URL": queue_url,
})
print(json.dumps({"Variables": env}))
PY
)"
aws lambda update-function-configuration \
  --function-name "$USERAPI_LAMBDA_NAME" \
  --environment "$MERGED_ENV" \
  --query "[FunctionName,Environment.Variables.CRM_SYNC_QUEUE_URL]" --output text
aws lambda wait function-updated-v2 --function-name "$USERAPI_LAMBDA_NAME"

echo
echo ">> Done. Redeploy userApi's code (script 19) if this is the first time"
echo ">> these routes are being shipped, so the new handlers are actually live."
