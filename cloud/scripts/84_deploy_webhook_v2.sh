#!/usr/bin/env bash
# =============================================================
# 84_deploy_webhook_v2.sh — Webhook Contract v2 (delivery_type=webhook,
# provider=salesforce): signed meeting events to a customer-owned endpoint,
# customer meeting types, record search and People Sync over v2.
# Spec: docs/webhook-contract-v2.md. A SEPARATE path from the MinuteX
# Salesforce package (77-83); a workspace uses one or the other.
#
#   bash 84_deploy_webhook_v2.sh            # deploy, feature ON
#   WEBHOOK_ENABLED=false bash 84_...sh     # deploy dark / roll back
#
# Run AFTER 78 (it relies on 78's grants). Idempotent.
#
# NO new table, queue or IAM policy:
#   * the endpoint is a SiteDeliveryEndpoints row (delivery_type=webhook);
#   * the per-meeting selection is Recordings.integration_context;
#   * the delivery state is a CrmSyncJobs row (operation=webhook_event) on
#     the existing CrmSyncQueue / crmSyncWorker.
# The preflight below proves 78's grants are present instead of assuming.
#
# 1. Preflight: shared modules present; 78's inline policies on both roles.
# 2. Code on userApi and crmSyncWorker (the worker packages userapi).
# 3. Routes (before the env, so an env failure never leaves them missing).
# 4. Env on BOTH functions (the worker imports userapi, so it reads the same
#    flags): WEBHOOK_ENABLED (+ optional overrides).
#
# ROLLBACK: WEBHOOK_ENABLED=false bash 84_deploy_webhook_v2.sh — every v2
# route answers 404 webhook_disabled, every dirty hook is a no-op, queued v2
# jobs are skipped. v1 (the package) is untouched either way.
#
# EGRESS: v2 endpoints are arbitrary public HTTPS hosts. Both functions run
# outside a VPC today (the v1 path already calls Salesforce Sites); if they
# are ever moved into one, it needs a NAT route or deliveries fail transient.
# =============================================================
set -euo pipefail
cd "$(dirname "$0")"
. ./aws.sh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

USERAPI_FN="${USERAPI_FN:-userApi}"
WORKER_FN="${WORKER_FN:-crmSyncWorker}"
WEBHOOK_ENABLED="${WEBHOOK_ENABLED:-true}"

echo "==> Preflight"
for f in webhook_contract.py webhook_client.py site_contract.py \
         site_delivery_client.py people_store.py; do
  test -f "$PROJECT_ROOT/shared/$f" || { echo "FAIL: shared/$f missing" >&2; exit 1; }
done
role_of() {
  local r
  r="$(aws lambda get-function --function-name "$1" --query 'Configuration.Role' --output text)"
  echo "${r##*/}"
}
require_policy() {
  local role="$1" policy="$2"
  if ! aws iam list-role-policies --role-name "$role" --query 'PolicyNames' \
      --output text | tr '\t' '\n' | tr -d '\r' | grep -qx "$policy"; then
    echo "FAIL: role $role has no inline policy $policy — run 78 first." >&2
    exit 1
  fi
  echo ">> $role has $policy"
}
require_policy "$(role_of "$USERAPI_FN")" "userApi-site-delivery"
require_policy "$(role_of "$WORKER_FN")" "crmSyncWorker-site-delivery"

echo "==> Meeting files (MoM PDF, transcript) in S3"
# The app uploads the MoM PDF to userApi, which keeps it under
# meeting-files/ beside the recordings; the worker reads it to send to
# Salesforce. Additive policy under its own name on both roles.
: "${BUCKET_NAME:?BUCKET_NAME not set (from .env)}"
FILES_POLICY="$(mktemp)"
cat > "$FILES_POLICY" <<JSON
{ "Version": "2012-10-17",
  "Statement": [ { "Sid": "MeetingFiles", "Effect": "Allow",
    "Action": ["s3:PutObject", "s3:GetObject"],
    "Resource": "arn:aws:s3:::${BUCKET_NAME}/meeting-files/*" } ] }
JSON
for fn in "$USERAPI_FN" "$WORKER_FN"; do
  aws iam put-role-policy --role-name "$(role_of "$fn")" --policy-name "${fn}-meeting-files" \
    --policy-document "file://$(winpath "$FILES_POLICY")"
  echo ">> ${fn}-meeting-files set"
done
rm -f "$FILES_POLICY"

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

echo "==> Routes"
# API_ID, else API_NAME, else the names this API has had (the live one in
# ap-south-1 is "iot-audio-api").
if [[ -z "${API_ID:-}" ]]; then
  for name in ${API_NAME:-} meeting-recorder-api iot-audio-api; do
    API_ID="$(aws apigatewayv2 get-apis --query "Items[?Name=='${name}'].ApiId | [0]" \
      --output text | tr -d '\r')"
    [[ -n "$API_ID" && "$API_ID" != "None" ]] && break
  done
fi
[[ -n "$API_ID" && "$API_ID" != "None" ]] || { echo "FAIL: no API id" >&2; exit 1; }
INTEGRATION_ID="$(aws apigatewayv2 get-integrations --api-id "$API_ID" \
  --query "Items[?contains(IntegrationUri, ':${USERAPI_FN}')].IntegrationId | [0]" \
  --output text | tr -d '\r')"
[[ -n "$INTEGRATION_ID" && "$INTEGRATION_ID" != "None" ]] || { echo "FAIL: no userApi integration" >&2; exit 1; }
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
ensure_route "GET /workspaces/{workspace_id}/webhook"
ensure_route "PUT /workspaces/{workspace_id}/webhook"
ensure_route "DELETE /workspaces/{workspace_id}/webhook"
ensure_route "POST /workspaces/{workspace_id}/webhook/secret"
ensure_route "POST /workspaces/{workspace_id}/webhook/test"
ensure_route "GET /workspaces/{workspace_id}/webhook/configuration"
ensure_route "POST /workspaces/{workspace_id}/webhook/configuration/refresh"
ensure_route "GET /workspaces/{workspace_id}/webhook/records"
ensure_route "GET /workspaces/{workspace_id}/webhook/people"
ensure_route "GET /recordings/webhook-people/{key+}"
ensure_route "GET /workspaces/{workspace_id}/webhook/link-config"
ensure_route "GET /workspaces/{workspace_id}/webhook/link-records"
ensure_route "GET /recordings/webhook-links/{key+}"
ensure_route "PUT /recordings/webhook-links/{key+}"
ensure_route "PUT /recordings/webhook-mom-pdf/{key+}"
ensure_route "PUT /recordings/webhook-doc-pdf/{key+}"
ensure_route "GET /recordings/webhook-context/{key+}"
ensure_route "PUT /recordings/webhook-context/{key+}"
ensure_route "POST /recordings/webhook/{key+}"
ensure_route "GET /recordings/webhook/{key+}"
ensure_route "POST /recordings/webhook-retry/{key+}"

echo "==> Environment (WEBHOOK_ENABLED=$WEBHOOK_ENABLED)"
ENV_ARGS=("WEBHOOK_ENABLED=$WEBHOOK_ENABLED")
[[ -n "${WEBHOOK_DEBOUNCE_SECONDS:-}" ]] && ENV_ARGS+=("WEBHOOK_DEBOUNCE_SECONDS=$WEBHOOK_DEBOUNCE_SECONDS")
[[ -n "${WEBHOOK_MAX_ATTEMPTS:-}" ]] && ENV_ARGS+=("WEBHOOK_MAX_ATTEMPTS=$WEBHOOK_MAX_ATTEMPTS")
for fn in "$USERAPI_FN" "$WORKER_FN"; do
  CURRENT_ENV="$(aws lambda get-function-configuration --function-name "$fn" \
                 --query 'Environment.Variables' --output json)"
  ENV_JSON="$(python "$SCRIPT_DIR/_merge_env.py" "$CURRENT_ENV" "${ENV_ARGS[@]}")"
  aws lambda update-function-configuration --function-name "$fn" --environment "$ENV_JSON" >/dev/null
  aws lambda wait function-updated-v2 --function-name "$fn"
  echo ">> $fn environment updated."
done

echo ">> Done. Build the app with EXPO_PUBLIC_WEBHOOK_V2=1 to show the v2 screens,"
echo "   and install the reference receiver (salesforce/minutex-webhook) in the"
echo "   customer org — see its README."
