#!/usr/bin/env bash
# =============================================================
# 65_deploy_crm_sync_worker.sh — create/update the job worker Lambda
# (crmSyncWorker) and wire it to the CrmSyncQueue SQS queue.
#
# The worker runs the Salesforce Site flow's CrmSyncJobs jobs (meeting
# delivery, people sync, discovery, Salesforce Task create/update); see
# functions/crm-sync-worker/lambda_function.py.
#
# Resources:
#   1. IAM role `crmSyncWorkerRole` + inline policy crmSyncWorker-least-privilege:
#        - CrmSyncJobs: Get/Put/Update/Query (job state)
#        - Recordings: Get/Update
#        - Contacts, Tasks, Workspaces, WorkspaceMemberships,
#          MeetingParticipants: Get/Query (read-only)
#        - SQS Receive/Delete/GetQueueAttributes/ChangeMessageVisibility on
#          the queue ONLY (ChangeMessageVisibility applies the job's own
#          backoff to a retried message)
#        - CloudWatch Logs
#      The Site-specific grants (Site tables, endpoint-credential KMS
#      decrypt, transcript S3 read) are separate inline policies owned by
#      scripts/78, 79 and 74.
#   2. Lambda `crmSyncWorker` — functions/crm-sync-worker/lambda_function.py
#      plus a COPY of functions/userapi/lambda_function.py (renamed
#      userapi_worker.py) and every shared/*.py, flat at the archive root.
#   3. SQS event source mapping with `ReportBatchItemFailures`.
#
# Idempotent. On an existing function the environment is MERGED into the
# live one (so variables set by scripts/74, 78 and 79 survive), and the
# retired Salesforce OAuth variables are removed.
#
# Prerequisites:
#   - scripts/63_create_crm_sync_jobs_table.sh (CrmSyncJobs table)
#   - scripts/64_create_crm_sync_queue.sh (queue + DLQ; CRM_SYNC_QUEUE_URL/
#     CRM_SYNC_QUEUE_ARN in .env)
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

CRM_SYNC_WORKER_LAMBDA_NAME="${CRM_SYNC_WORKER_LAMBDA_NAME:-crmSyncWorker}"
CRM_SYNC_WORKER_ROLE_NAME="${CRM_SYNC_WORKER_ROLE_NAME:-crmSyncWorkerRole}"
USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"
CRM_SYNC_JOBS_TABLE="${CRM_SYNC_JOBS_TABLE:-CrmSyncJobs}"
RECORDINGS_TABLE="${RECORDINGS_TABLE:-Recordings}"
CONTACTS_TABLE="${CONTACTS_TABLE:-Contacts}"
TASKS_TABLE="${TASKS_TABLE:-Tasks}"
WORKSPACES_TABLE="${WORKSPACES_TABLE:-Workspaces}"
WORKSPACE_MEMBERSHIPS_TABLE="${WORKSPACE_MEMBERSHIPS_TABLE:-WorkspaceMemberships}"
# Read when a meeting is delivered (participants); see scripts/72.
MEETING_PARTICIPANTS_TABLE="${MEETING_PARTICIPANTS_TABLE:-MeetingParticipants}"
: "${CRM_SYNC_QUEUE_ARN:?CRM_SYNC_QUEUE_ARN not set — run scripts/64_create_crm_sync_queue.sh first and add it to .env}"
: "${CRM_SYNC_QUEUE_URL:?CRM_SYNC_QUEUE_URL not set — run scripts/64_create_crm_sync_queue.sh first and add it to .env}"
CRM_SYNC_WORKER_TIMEOUT="${CRM_SYNC_WORKER_TIMEOUT:-120}"
CRM_SYNC_WORKER_MEMORY="${CRM_SYNC_WORKER_MEMORY:-256}"
# Must be LESS than the queue's VisibilityTimeout (300s, scripts/64) —
# otherwise SQS could make the message visible to a second worker before
# this Lambda's own timeout even fires, guaranteeing the very duplicate-
# delivery race the visibility timeout exists to avoid.
CRM_SYNC_BATCH_SIZE="${CRM_SYNC_BATCH_SIZE:-5}"

echo ">> Region: $AWS_REGION"
echo ">> Worker: $CRM_SYNC_WORKER_LAMBDA_NAME"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

# -------------------------------------------------------------
# 1. IAM role — create if absent, then set/overwrite its inline policy
#    either way (idempotent update, same as every other role-policy script).
# -------------------------------------------------------------
TRUST_POLICY_FILE="$SCRIPT_DIR/iam/trust-policy.json"
if [[ ! -f "$TRUST_POLICY_FILE" ]]; then
  echo "ERROR: $TRUST_POLICY_FILE not found." >&2
  exit 1
fi

if aws iam get-role --role-name "$CRM_SYNC_WORKER_ROLE_NAME" >/dev/null 2>&1; then
  echo ">> Role already exists: $CRM_SYNC_WORKER_ROLE_NAME"
else
  aws iam create-role \
    --role-name "$CRM_SYNC_WORKER_ROLE_NAME" \
    --assume-role-policy-document "file://$(winpath "$TRUST_POLICY_FILE")" \
    --description "Execution role for crmSyncWorker - least-privilege, scoped to CrmSyncJobs jobs" \
    >/dev/null
  echo ">> Created role: $CRM_SYNC_WORKER_ROLE_NAME"
  echo ">> Waiting for IAM role propagation ..."
  sleep 10
fi
ROLE_ARN="$(aws iam get-role --role-name "$CRM_SYNC_WORKER_ROLE_NAME" \
              --query 'Role.Arn' --output text)"

POLICY_FILE="$(mktemp)"
trap 'rm -f "$POLICY_FILE"' EXIT
cat > "$POLICY_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "Logs", "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "arn:aws:logs:${AWS_REGION}:${ACCOUNT_ID}:log-group:/aws/lambda/${CRM_SYNC_WORKER_LAMBDA_NAME}*" },
    { "Sid": "CrmSyncJobsReadWrite", "Effect": "Allow",
      "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
                 "dynamodb:Query"],
      "Resource": [
        "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${CRM_SYNC_JOBS_TABLE}",
        "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${CRM_SYNC_JOBS_TABLE}/index/*"
      ] },
    { "Sid": "RecordingsReadWrite", "Effect": "Allow",
      "Action": ["dynamodb:GetItem", "dynamodb:UpdateItem"],
      "Resource": "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${RECORDINGS_TABLE}" },
    { "Sid": "IdentityReadOnly", "Effect": "Allow",
      "Action": ["dynamodb:GetItem", "dynamodb:Query"],
      "Resource": [
        "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${CONTACTS_TABLE}",
        "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${TASKS_TABLE}",
        "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${TASKS_TABLE}/index/*",
        "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${WORKSPACES_TABLE}",
        "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${WORKSPACE_MEMBERSHIPS_TABLE}",
        "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${MEETING_PARTICIPANTS_TABLE}"
      ] },
    { "Sid": "CrmSyncQueueConsume", "Effect": "Allow",
      "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes",
                 "sqs:ChangeMessageVisibility"],
      "Resource": "${CRM_SYNC_QUEUE_ARN}" }
  ]
}
EOF
aws iam put-role-policy \
  --role-name "$CRM_SYNC_WORKER_ROLE_NAME" \
  --policy-name "crmSyncWorker-least-privilege" \
  --policy-document "file://$(winpath "$POLICY_FILE")"
echo ">> crmSyncWorker-least-privilege inline policy set."

# -------------------------------------------------------------
# 2. Package: crm-sync-worker/lambda_function.py + a COPY of userapi's
#    lambda_function.py (renamed userapi_worker.py) + shared/*.py, all flat.
# -------------------------------------------------------------
ZIP_PATH="$PROJECT_ROOT/functions/crm-sync-worker/function.zip"
rm -f "$ZIP_PATH"
( cd "$PROJECT_ROOT" && python - <<'PY'
import zipfile
from pathlib import Path

root = Path.cwd()
out = root / "functions/crm-sync-worker/function.zip"
shared = root / "shared"

with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    z.write(root / "functions/crm-sync-worker/lambda_function.py", "lambda_function.py")
    # Renamed on the way in — see lambda_function.py's own docstring for why
    # this is a packaged COPY, not a live import of the deployed userApi.
    z.write(root / "functions/userapi/lambda_function.py", "userapi_worker.py")
    for mod in sorted(shared.glob("*.py")):
        z.write(mod, mod.name)
        print(f"   + {mod.name}")
print(f">> packaged {out.relative_to(root)}")
PY
)

# -------------------------------------------------------------
# 3. Create or update the Lambda function.
# -------------------------------------------------------------
# Environment: the SAME table/queue names userApi uses, MERGED into the live
# environment when the function exists (scripts/74, 78 and 79 add their own
# variables), minus the retired Salesforce OAuth variables.
CURRENT_ENV="$(aws lambda get-function-configuration \
    --function-name "$CRM_SYNC_WORKER_LAMBDA_NAME" \
    --query 'Environment.Variables' --output json 2>/dev/null || echo null)"
ENV_JSON="$(python - "$CURRENT_ENV" "$CRM_SYNC_JOBS_TABLE" "$RECORDINGS_TABLE" \
    "$CONTACTS_TABLE" "$TASKS_TABLE" "$WORKSPACES_TABLE" \
    "$WORKSPACE_MEMBERSHIPS_TABLE" "$CRM_SYNC_QUEUE_URL" <<'PY'
import json, sys
current_raw, jobs, recordings, contacts, tasks, workspaces, memberships, queue_url = sys.argv[1:9]
try:
    env = json.loads(current_raw) or {}
except ValueError:
    env = {}
RETIRED = ("ORG_CRM_CONNECTIONS_TABLE", "ORG_SALESFORCE_USER_LINKS_TABLE",
           "CRM_CONNECTIONS_TABLE", "SALESFORCE_KMS_KEY_ID",
           "SALESFORCE_CLIENT_SECRET_ARN", "SALESFORCE_CLIENT_ID",
           "SALESFORCE_LOGIN_URL", "SALESFORCE_API_VERSION",
           "SF_MEETING_OBJECT", "SF_MEETING_EXTERNAL_ID", "SF_PACKAGE_INSTALL_URL")
for key in RETIRED:
    env.pop(key, None)
env.update({
    "CRM_SYNC_JOBS_TABLE": jobs,
    "RECORDINGS_TABLE": recordings,
    "CONTACTS_TABLE": contacts,
    "TASKS_TABLE": tasks,
    "WORKSPACES_TABLE": workspaces,
    "WORKSPACE_MEMBERSHIPS_TABLE": memberships,
    "CRM_SYNC_QUEUE_URL": queue_url,
})
print(json.dumps({"Variables": env}))
PY
)"

if aws lambda get-function --function-name "$CRM_SYNC_WORKER_LAMBDA_NAME" >/dev/null 2>&1; then
  echo ">> Function exists — updating code and configuration."
  aws lambda update-function-code \
    --function-name "$CRM_SYNC_WORKER_LAMBDA_NAME" \
    --zip-file "fileb://$(winpath "$ZIP_PATH")" \
    --publish \
    --query "[FunctionName,CodeSize,LastModified]" --output text
  aws lambda wait function-updated-v2 --function-name "$CRM_SYNC_WORKER_LAMBDA_NAME"
  aws lambda update-function-configuration \
    --function-name "$CRM_SYNC_WORKER_LAMBDA_NAME" \
    --timeout "$CRM_SYNC_WORKER_TIMEOUT" \
    --memory-size "$CRM_SYNC_WORKER_MEMORY" \
    --environment "$ENV_JSON" \
    --query "[FunctionName,Timeout,MemorySize]" --output text
  aws lambda wait function-updated-v2 --function-name "$CRM_SYNC_WORKER_LAMBDA_NAME"
else
  echo ">> Creating function: $CRM_SYNC_WORKER_LAMBDA_NAME"
  aws lambda create-function \
    --function-name "$CRM_SYNC_WORKER_LAMBDA_NAME" \
    --runtime python3.12 \
    --role "$ROLE_ARN" \
    --handler lambda_function.lambda_handler \
    --timeout "$CRM_SYNC_WORKER_TIMEOUT" \
    --memory-size "$CRM_SYNC_WORKER_MEMORY" \
    --zip-file "fileb://$(winpath "$ZIP_PATH")" \
    --environment "$ENV_JSON" \
    --query "[FunctionName,FunctionArn]" --output text
  aws lambda wait function-active-v2 --function-name "$CRM_SYNC_WORKER_LAMBDA_NAME"
fi

WORKER_ARN="$(aws lambda get-function --function-name "$CRM_SYNC_WORKER_LAMBDA_NAME" \
                --query 'Configuration.FunctionArn' --output text)"

# -------------------------------------------------------------
# 4. SQS event source mapping, with partial-batch-failure reporting.
# -------------------------------------------------------------
EXISTING_MAPPING_ID="$(aws lambda list-event-source-mappings \
    --function-name "$CRM_SYNC_WORKER_LAMBDA_NAME" \
    --event-source-arn "$CRM_SYNC_QUEUE_ARN" \
    --query 'EventSourceMappings[0].UUID' --output text 2>/dev/null || true)"
if [[ -n "$EXISTING_MAPPING_ID" && "$EXISTING_MAPPING_ID" != "None" ]]; then
  echo ">> Event source mapping already exists: $EXISTING_MAPPING_ID"
else
  aws lambda create-event-source-mapping \
    --function-name "$CRM_SYNC_WORKER_LAMBDA_NAME" \
    --event-source-arn "$CRM_SYNC_QUEUE_ARN" \
    --batch-size "$CRM_SYNC_BATCH_SIZE" \
    --function-response-types ReportBatchItemFailures \
    >/dev/null
  echo ">> Event source mapping created: $CRM_SYNC_QUEUE_ARN -> $CRM_SYNC_WORKER_LAMBDA_NAME"
fi

echo
echo ">> Done."
echo ">> Worker: $WORKER_ARN"
echo ">> Add to .env: CRM_SYNC_WORKER_LAMBDA_NAME=${CRM_SYNC_WORKER_LAMBDA_NAME}"
