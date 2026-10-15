#!/usr/bin/env bash
# =============================================================
# 81_deploy_salesforce_site_phase2.sh — Salesforce Site, phase 2:
# People in the app, filtered objects, the meeting's Salesforce relationship,
# Task inheritance (Who + What) and Task sync bookkeeping.
#
# Run AFTER 77 + 78 + 79. Idempotent. No table is created or altered:
#   * the meeting relationship is one attribute on the Recordings row
#     (salesforce_relation), written by userApi, which already has access;
#   * Task rows gain who_/what_/sync fields on the existing
#     SiteSalesforceTasks table.
#
# 1. IAM (a separately named inline policy, revocable on its own):
#      crmSyncWorker  "crmSyncWorker-site-task-links"  Tasks UpdateItem —
#      after Salesforce confirms a Task, the worker records it on the MinuteX
#      task it came from (Tasks.salesforce_sync). Best-effort in code: without
#      this grant the Task still syncs and the link is only logged as missing.
# 2. Code on userApi and crmSyncWorker (the worker packages userapi).
# 3. API routes:
#      GET/PUT /workspaces/{workspace_id}/site-salesforce/meeting-relation
#      POST    /workspaces/{workspace_id}/people/contact
#      PUT     /workspaces/{workspace_id}/site-salesforce/object-preferences
#      GET     /workspaces/{workspace_id}/site-salesforce/destinations
#      GET     /workspaces/{workspace_id}/site-salesforce/destinations/{destination_id}/records
#      PUT     /workspaces/{workspace_id}/site-salesforce/meeting-destination
#
# THEN, in each connected Salesforce org, deploy the receiver (1.3.0: the
# relationship fields, Who+What Tasks) — see salesforce/minutex-site/README.md:
#   cd salesforce/minutex-site
#   sf project deploy start --source-dir force-app --target-org <alias> \
#     --test-level RunSpecifiedTests --tests MinuteXSiteReceiverTest \
#     --tests MinuteXSiteSalesforceTest
# The order does not matter for safety: a 1.2 receiver ignores the new
# payload keys (meetings stay unrelated, Tasks keep their primary record).
#
# Finally re-run "Discover Salesforce Objects" once per workspace. It now
# builds the full object CATALOG (every queryable business/custom object;
# receiver 1.4.0 adds mru_enabled to drop platform machinery) — the old run
# stored only ~14 objects. Workspace object preferences (PREF#objects) survive
# every rediscovery.
# =============================================================
set -euo pipefail
cd "$(dirname "$0")"
. ./aws.sh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

USERAPI_FN="${USERAPI_FN:-userApi}"
WORKER_FN="${WORKER_FN:-crmSyncWorker}"
TASKS_TABLE="${TASKS_TABLE:-Tasks}"

for f in site_salesforce_schema.py site_contract.py people_store.py; do
  test -f "$PROJECT_ROOT/shared/$f" || { echo "FAIL: shared/$f missing" >&2; exit 1; }
done

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
POLICY_FILE="$(mktemp)"
trap 'rm -f "$POLICY_FILE"' EXIT
cat > "$POLICY_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "LinkSalesforceTaskToMinutexTask", "Effect": "Allow",
      "Action": ["dynamodb:UpdateItem"],
      "Resource": ["arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${TASKS_TABLE}"] }
  ]
}
EOF
ROLE="$(aws lambda get-function --function-name "$WORKER_FN" --query 'Configuration.Role' --output text)"
ROLE="${ROLE##*/}"
aws iam put-role-policy --role-name "$ROLE" --policy-name "crmSyncWorker-site-task-links" \
  --policy-document "file://$(winpath "$POLICY_FILE")"
echo ">> $WORKER_FN ($ROLE): crmSyncWorker-site-task-links policy set."

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
ensure_route "GET /workspaces/{workspace_id}/site-salesforce/meeting-relation"
ensure_route "PUT /workspaces/{workspace_id}/site-salesforce/meeting-relation"
ensure_route "POST /workspaces/{workspace_id}/people/contact"
ensure_route "PUT /workspaces/{workspace_id}/site-salesforce/object-preferences"
ensure_route "GET /workspaces/{workspace_id}/site-salesforce/destinations"
ensure_route "GET /workspaces/{workspace_id}/site-salesforce/destinations/{destination_id}/records"
ensure_route "PUT /workspaces/{workspace_id}/site-salesforce/meeting-destination"

echo ">> Done. Deploy the Salesforce receiver (1.3.0) to each connected org, then"
echo "   re-run Discover Salesforce Objects once per workspace."
