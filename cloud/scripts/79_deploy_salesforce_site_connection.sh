#!/usr/bin/env bash
# =============================================================
# 79_deploy_salesforce_site_connection.sh — the Site architecture's org
# connection: org id, object/field discovery, manual Task mappings, record
# selection and Task create/update. Every call goes through the workspace's
# Salesforce Site endpoint (scripts/78) and its pluggable auth mode; there is
# no separate Salesforce credential.
#
# Run AFTER 77 + 78 (Site delivery). Idempotent.
#
# 1. Tables (new; nothing existing is altered):
#      SiteSalesforceConnections   PK workspace_id
#          org id (tenant identity), integration user, Task permissions,
#          discovery summary. No credential of any kind.
#      SiteSalesforceSchema        PK workspace_id, SK item_key
#          OBJ#<object> / FLD#<object>: discovered metadata (as the
#          integration user sees it). Rebuilt by every discovery run.
#      SiteSalesforceTaskMappings  PK workspace_id, SK mapping_id
#      SiteSalesforceTasks         PK workspace_id, SK task_ref
# 2. IAM, as separately named inline policies (revocable on their own):
#      userApi        "userApi-site-salesforce"        all four tables
#      crmSyncWorker  "crmSyncWorker-site-salesforce"  connections Get/Update,
#                     schema Get/Put/Update/Delete/Query, tasks Get/Update
# 3. Environment + code on both Lambdas, then the new API routes.
#
# Does NOT touch any existing Salesforce OAuth resource, route or table.
# =============================================================
set -euo pipefail
cd "$(dirname "$0")"
. ./aws.sh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

USERAPI_FN="${USERAPI_FN:-userApi}"
WORKER_FN="${WORKER_FN:-crmSyncWorker}"
CONN_TABLE="${SITE_SF_CONNECTIONS_TABLE:-SiteSalesforceConnections}"
SCHEMA_TABLE="${SITE_SF_SCHEMA_TABLE:-SiteSalesforceSchema}"
MAPPINGS_TABLE="${SITE_SF_MAPPINGS_TABLE:-SiteSalesforceTaskMappings}"
TASKS_TABLE="${SITE_SF_TASKS_TABLE:-SiteSalesforceTasks}"

for f in site_salesforce_schema.py site_contract.py site_delivery_client.py; do
  test -f "$PROJECT_ROOT/shared/$f" || { echo "FAIL: shared/$f missing" >&2; exit 1; }
done

create_table() {
  local name="$1" sk="$2"
  if aws dynamodb describe-table --table-name "$name" >/dev/null 2>&1; then
    echo ">> Table '$name' already exists."
  else
    echo ">> Creating '$name' ..."
    if [[ -n "$sk" ]]; then
      aws dynamodb create-table --table-name "$name" --billing-mode PAY_PER_REQUEST \
        --attribute-definitions AttributeName=workspace_id,AttributeType=S \
                                AttributeName="$sk",AttributeType=S \
        --key-schema AttributeName=workspace_id,KeyType=HASH \
                     AttributeName="$sk",KeyType=RANGE >/dev/null
    else
      aws dynamodb create-table --table-name "$name" --billing-mode PAY_PER_REQUEST \
        --attribute-definitions AttributeName=workspace_id,AttributeType=S \
        --key-schema AttributeName=workspace_id,KeyType=HASH >/dev/null
    fi
    aws dynamodb wait table-exists --table-name "$name"
  fi
  aws dynamodb update-continuous-backups --table-name "$name" \
    --point-in-time-recovery-specification PointInTimeRecoveryEnabled=true \
    >/dev/null 2>&1 && echo "   PITR enabled on '$name'." \
    || echo "   PITR: could not enable on '$name'."
}
create_table "$CONN_TABLE" ""
create_table "$SCHEMA_TABLE" "item_key"
create_table "$MAPPINGS_TABLE" "mapping_id"
create_table "$TASKS_TABLE" "task_ref"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
arn() { echo "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/$1"; }
POLICY_FILE="$(mktemp)"
trap 'rm -f "$POLICY_FILE"' EXIT
put_policy() {
  local fn="$1" name="$2" role
  role="$(aws lambda get-function --function-name "$fn" --query 'Configuration.Role' --output text)"
  role="${role##*/}"
  aws iam put-role-policy --role-name "$role" --policy-name "$name" \
    --policy-document "file://$(winpath "$POLICY_FILE")"
  echo ">> $fn ($role): $name policy set."
}

cat > "$POLICY_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "SiteSalesforceTables", "Effect": "Allow",
      "Action": ["dynamodb:GetItem","dynamodb:PutItem","dynamodb:UpdateItem",
                 "dynamodb:DeleteItem","dynamodb:Query"],
      "Resource": ["$(arn "$CONN_TABLE")", "$(arn "$SCHEMA_TABLE")",
                   "$(arn "$MAPPINGS_TABLE")", "$(arn "$TASKS_TABLE")"] }
  ]
}
EOF
put_policy "$USERAPI_FN" "userApi-site-salesforce"

cat > "$POLICY_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "Connection", "Effect": "Allow",
      "Action": ["dynamodb:GetItem","dynamodb:UpdateItem"],
      "Resource": ["$(arn "$CONN_TABLE")"] },
    { "Sid": "DiscoveredSchema", "Effect": "Allow",
      "Action": ["dynamodb:GetItem","dynamodb:PutItem","dynamodb:UpdateItem",
                 "dynamodb:DeleteItem","dynamodb:Query"],
      "Resource": ["$(arn "$SCHEMA_TABLE")"] },
    { "Sid": "Tasks", "Effect": "Allow",
      "Action": ["dynamodb:GetItem","dynamodb:UpdateItem"],
      "Resource": ["$(arn "$TASKS_TABLE")"] }
  ]
}
EOF
put_policy "$WORKER_FN" "crmSyncWorker-site-salesforce"

echo "==> Packaging userApi"
python "$SCRIPT_DIR/_package_lambda.py" functions/userapi "$PROJECT_ROOT"
aws lambda update-function-code --function-name "$USERAPI_FN" \
  --zip-file "fileb://$(winpath "$PROJECT_ROOT/functions/userapi/function.zip")" \
  --query "[FunctionName,CodeSize]" --output text
aws lambda wait function-updated-v2 --function-name "$USERAPI_FN"

echo "==> Packaging crmSyncWorker (with userapi_worker.py + shared/)"
WORKER_ZIP="$PROJECT_ROOT/functions/crm-sync-worker/function.zip"
rm -f "$WORKER_ZIP"
( cd "$PROJECT_ROOT" && python - <<'PY'
import zipfile
from pathlib import Path
root = Path.cwd()
out = root / "functions/crm-sync-worker/function.zip"
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    z.write(root / "functions/crm-sync-worker/lambda_function.py", "lambda_function.py")
    z.write(root / "functions/userapi/lambda_function.py", "userapi_worker.py")
    for mod in sorted((root / "shared").glob("*.py")):
        z.write(mod, mod.name)
print(f">> packaged {out.relative_to(root)} ({out.stat().st_size // 1024} KB)")
PY
)
aws lambda update-function-code --function-name "$WORKER_FN" \
  --zip-file "fileb://$(winpath "$WORKER_ZIP")" --query "[FunctionName,CodeSize]" --output text
aws lambda wait function-updated-v2 --function-name "$WORKER_FN"

ENV_ARGS=("SITE_SF_CONNECTIONS_TABLE=$CONN_TABLE" "SITE_SF_SCHEMA_TABLE=$SCHEMA_TABLE"
          "SITE_SF_MAPPINGS_TABLE=$MAPPINGS_TABLE" "SITE_SF_TASKS_TABLE=$TASKS_TABLE")
for FN in "$USERAPI_FN" "$WORKER_FN"; do
  CURRENT_ENV="$(aws lambda get-function-configuration --function-name "$FN" \
                   --query 'Environment.Variables' --output json)"
  ENV_JSON="$(python "$SCRIPT_DIR/_merge_env.py" "$CURRENT_ENV" "${ENV_ARGS[@]}")"
  aws lambda update-function-configuration --function-name "$FN" \
    --environment "$ENV_JSON" >/dev/null
  aws lambda wait function-updated-v2 --function-name "$FN"
  echo ">> $FN environment updated."
done

API_ID="${API_ID:-$(aws apigatewayv2 get-apis \
  --query "Items[?Name=='${API_NAME:-meeting-recorder-api}'].ApiId | [0]" \
  --output text | tr -d '\r')}"
[[ -n "$API_ID" && "$API_ID" != "None" ]] || { echo "FAIL: no API id" >&2; exit 1; }
INTEGRATION_ID="$(aws apigatewayv2 get-integrations --api-id "$API_ID" \
  --query "Items[?contains(IntegrationUri, ':${USERAPI_FN}')].IntegrationId | [0]" \
  --output text | tr -d '\r')"
ensure_route() {
  local route_key="$1" existing
  existing="$(aws apigatewayv2 get-routes --api-id "$API_ID" \
      --query "Items[?RouteKey=='${route_key}'].RouteId | [0]" --output json | tr -d '"\r')"
  if [[ -n "$existing" && "$existing" != "null" ]]; then
    echo ">> Route exists: $route_key"; return 0
  fi
  aws apigatewayv2 create-route --api-id "$API_ID" --route-key "$route_key" \
    --target "integrations/$INTEGRATION_ID" >/dev/null
  echo ">> Route created: $route_key"
}
B="/workspaces/{workspace_id}/site-salesforce"
ensure_route "PUT $B/connect"
ensure_route "GET $B"
ensure_route "DELETE $B"
ensure_route "POST $B/discover"
ensure_route "GET $B/discover"
ensure_route "GET $B/objects"
ensure_route "GET $B/objects/{object}/fields"
ensure_route "GET $B/objects/{object}/records"
ensure_route "GET $B/task-mappings"
ensure_route "POST $B/task-mappings"
ensure_route "PUT $B/task-mappings/{mapping_id}"
ensure_route "DELETE $B/task-mappings/{mapping_id}"
ensure_route "GET $B/tasks"
ensure_route "POST $B/tasks"
ensure_route "GET $B/tasks/{task_ref}"
ensure_route "PATCH $B/tasks/{task_ref}"
ensure_route "POST $B/tasks/{task_ref}/retry"

echo ">> Done. Nothing is connected until an Owner/Manager configures the Site"
echo "   endpoint (PUT .../site-delivery) and then connects (PUT $B/connect)."
