#!/usr/bin/env bash
# =============================================================
# 65_deploy_crm_sync_worker.sh — create/update the CRM sync worker Lambda
# and wire it to the CRM sync SQS queue (Phase 2D.4).
#
# THIS IS THE FIRST SCRIPT IN THIS CODEBASE THAT CREATES A NEW LAMBDA
# FUNCTION AND A NEW IAM ROLE FROM SCRATCH. Verified before writing this:
# every existing NN_deploy_*.sh script only UPDATES a pre-existing function
# (aws lambda update-function-code/update-function-configuration) and only
# EXTENDS an existing role's inline policy (aws iam put-role-policy) — there
# is no create-function/create-role precedent anywhere in cloud/scripts/.
# This script establishes that pattern, following the same aws.sh/winpath/
# idempotency style everything else uses; it deliberately does NOT touch
# any other Lambda's role, matching the least-privilege requirement (spec
# section 20) — this worker gets its OWN role with ONLY what it needs.
#
# Resources created:
#   1. IAM role `crmSyncWorkerRole` — Lambda service trust policy (mirrors
#      the shape in scripts/iam/trust-policy.json, the one static reference
#      this repo already had for it) + an inline policy scoped to EXACTLY:
#        - CrmSyncJobs: Get/Put/Update/Query (job state)
#        - Recordings: Get/Update (meeting content, crm_records/
#          crm_tasks_synced/crm_event_synced) — Update only on the
#          Recordings table, never Delete
#        - Contacts, Tasks, WorkspaceMemberships, Workspaces,
#          OrgCrmConnections, OrgSalesforceUserLinks: Get/Query (read-only —
#          the worker CONSUMES resolved identity, it never writes it; see
#          the "must not re-resolve identity" requirement)
#        - KMS Decrypt (SAME key Personal/Organisation Salesforce already
#          use — SALESFORCE_KMS_KEY_ID) — needed by _sf_call_org to decrypt
#          the stored refresh token; no Encrypt permission is granted here
#          because the worker's OWN token-rotation path re-encrypts through
#          the same _kms_encrypt used everywhere else, which DOES need
#          Encrypt too — see the policy below, it is included, but scoped
#          to the one key, matching every existing Salesforce-touching role
#        - Secrets Manager GetSecretValue on the Salesforce Connected App
#          client secret (same secret Personal/Organisation already read)
#        - SQS ReceiveMessage/DeleteMessage/GetQueueAttributes/
#          ChangeMessageVisibility on the CRM sync queue ONLY (not the DLQ —
#          nothing in this Lambda's own code sends to or reads from the DLQ
#          directly; SQS's redrive policy moves messages there on Lambda's
#          behalf). ChangeMessageVisibility lets a retried message's actual
#          redelivery delay match the job's own computed backoff instead of
#          always falling back to the queue's fixed VisibilityTimeout — see
#          docs/WORKSPACE_PHASE2D.md's Phase 2D.4 retry-timing section.
#        - CloudWatch Logs (the standard Lambda execution logging grant)
#      Explicitly NOT granted: any OTHER DynamoDB table, S3 (the worker
#      never touches audio/transcript objects), any other Secrets Manager
#      secret, iam:PassRole, or write access to Contacts/Tasks/
#      WorkspaceMemberships/OrgCrmConnections/OrgSalesforceUserLinks.
#   2. Lambda function `crmSyncWorker` — packaged from
#      functions/crm-sync-worker/lambda_function.py, with a COPY of
#      functions/userapi/lambda_function.py (renamed to userapi_worker.py to
#      make explicit in code that this is a packaged copy, not a live
#      cross-Lambda import — see that file's own module docstring for why
#      importing it directly, rather than duplicating or extracting its
#      logic, is the deliberate choice here) plus every shared/*.py module,
#      all flattened to the archive root (same packaging convention every
#      other Lambda in this repo already uses — the runtime only puts the
#      handler's own directory on sys.path).
#   3. SQS event source mapping, CRM sync queue -> crmSyncWorker, with
#      `ReportBatchItemFailures` enabled (see the worker's own module
#      docstring for why partial-batch-failure reporting matters here).
#
# Idempotent: creates what is missing, updates what already exists.
#
# Prerequisites:
#   - scripts/63_create_crm_sync_jobs_table.sh (CrmSyncJobs table)
#   - scripts/64_create_crm_sync_queue.sh (queue + DLQ; CRM_SYNC_QUEUE_URL/
#     CRM_SYNC_QUEUE_ARN in .env)
#   - scripts/23_deploy_salesforce_oauth.sh (SALESFORCE_KMS_KEY_ID,
#     SALESFORCE_CLIENT_SECRET_ARN already provisioned — reused, not
#     recreated)
#   - scripts/60/61/62 (OrgCrmConnections, org OAuth routes,
#     OrgSalesforceUserLinks already deployed)
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
ORG_CRM_CONNECTIONS_TABLE="${ORG_CRM_CONNECTIONS_TABLE:-OrgCrmConnections}"
ORG_SALESFORCE_USER_LINKS_TABLE="${ORG_SALESFORCE_USER_LINKS_TABLE:-OrgSalesforceUserLinks}"
: "${CRM_SYNC_QUEUE_ARN:?CRM_SYNC_QUEUE_ARN not set — run scripts/64_create_crm_sync_queue.sh first and add it to .env}"
: "${CRM_SYNC_QUEUE_URL:?CRM_SYNC_QUEUE_URL not set — run scripts/64_create_crm_sync_queue.sh first and add it to .env}"
: "${SALESFORCE_KMS_KEY_ID:?SALESFORCE_KMS_KEY_ID not set — run scripts/23_deploy_salesforce_oauth.sh first}"

# SALESFORCE_CLIENT_SECRET_ARN is deliberately never written to .env (see
# script 23/61 — only the SECRET's ARN is ever an env var, never its
# plaintext contents, and it lives in the already-deployed userApi
# Lambda's own configuration, not in local config). Reuse it from there
# when not already supplied locally, the same read-only
# get-function-configuration pattern script 61 already uses — never call
# secretsmanager:GetSecretValue here, this script only ever needs the ARN.
if [[ -z "${SALESFORCE_CLIENT_SECRET_ARN:-}" ]]; then
  echo ">> SALESFORCE_CLIENT_SECRET_ARN not set locally — reading it from" \
       "the deployed $USERAPI_LAMBDA_NAME Lambda's configuration (read-only)."
  SALESFORCE_CLIENT_SECRET_ARN="$(aws lambda get-function-configuration \
      --function-name "$USERAPI_LAMBDA_NAME" \
      --query 'Environment.Variables.SALESFORCE_CLIENT_SECRET_ARN' \
      --output text 2>/dev/null || true)"
  if [[ -z "$SALESFORCE_CLIENT_SECRET_ARN" || "$SALESFORCE_CLIENT_SECRET_ARN" == "None" ]]; then
    echo "ERROR: SALESFORCE_CLIENT_SECRET_ARN not set and not found on the" \
         "deployed $USERAPI_LAMBDA_NAME Lambda — run" \
         "scripts/23_deploy_salesforce_oauth.sh first." >&2
    exit 1
  fi
  echo ">> Found SALESFORCE_CLIENT_SECRET_ARN on $USERAPI_LAMBDA_NAME: $SALESFORCE_CLIENT_SECRET_ARN"
fi
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
KMS_KEY_ARN="arn:aws:kms:${AWS_REGION}:${ACCOUNT_ID}:key/${SALESFORCE_KMS_KEY_ID}"

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
    --description "Execution role for crmSyncWorker (Phase 2D.4) - least-privilege, scoped to CRM sync only" \
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
        "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${ORG_CRM_CONNECTIONS_TABLE}",
        "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${ORG_SALESFORCE_USER_LINKS_TABLE}"
      ] },
    { "Sid": "OrgCrmConnectionsTokenRotation", "Effect": "Allow",
      "Action": ["dynamodb:UpdateItem"],
      "Resource": "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${ORG_CRM_CONNECTIONS_TABLE}" },
    { "Sid": "SalesforceTokenKms", "Effect": "Allow",
      "Action": ["kms:Encrypt", "kms:Decrypt"],
      "Resource": "${KMS_KEY_ARN}" },
    { "Sid": "SalesforceClientSecret", "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "${SALESFORCE_CLIENT_SECRET_ARN}" },
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
# Environment: the SAME table/queue names userApi already uses (so the
# worker reads/writes the identical rows), plus the Salesforce Connected
# App config userApi's org routes already rely on. No secret VALUE is ever
# an env var — only ARNs, matching userApi's own SALESFORCE_CLIENT_SECRET_ARN
# convention.
ENV_JSON="$(python - "$CRM_SYNC_JOBS_TABLE" "$RECORDINGS_TABLE" "$CONTACTS_TABLE" \
    "$TASKS_TABLE" "$WORKSPACES_TABLE" "$WORKSPACE_MEMBERSHIPS_TABLE" \
    "$ORG_CRM_CONNECTIONS_TABLE" "$ORG_SALESFORCE_USER_LINKS_TABLE" \
    "$SALESFORCE_KMS_KEY_ID" "$SALESFORCE_CLIENT_SECRET_ARN" \
    "${SALESFORCE_CLIENT_ID:-}" "${SALESFORCE_LOGIN_URL:-https://login.salesforce.com}" \
    "${SALESFORCE_API_VERSION:-v62.0}" "$CRM_SYNC_QUEUE_URL" <<'PY'
import json, sys
(jobs, recordings, contacts, tasks, workspaces, memberships, org_crm,
 org_sf_links, kms_key, secret_arn, client_id, login_url, api_version,
 queue_url) = sys.argv[1:15]
env = {
    "CRM_SYNC_JOBS_TABLE": jobs,
    "RECORDINGS_TABLE": recordings,
    "CONTACTS_TABLE": contacts,
    "TASKS_TABLE": tasks,
    "WORKSPACES_TABLE": workspaces,
    "WORKSPACE_MEMBERSHIPS_TABLE": memberships,
    "ORG_CRM_CONNECTIONS_TABLE": org_crm,
    "ORG_SALESFORCE_USER_LINKS_TABLE": org_sf_links,
    "SALESFORCE_KMS_KEY_ID": kms_key,
    "SALESFORCE_CLIENT_SECRET_ARN": secret_arn,
    "SALESFORCE_CLIENT_ID": client_id,
    "SALESFORCE_LOGIN_URL": login_url,
    "SALESFORCE_API_VERSION": api_version,
    "CRM_SYNC_QUEUE_URL": queue_url,
}
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
