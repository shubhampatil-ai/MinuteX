#!/usr/bin/env bash
# =============================================================
# 78_deploy_salesforce_site_delivery.sh — ship Salesforce Site delivery
# (delivery_type = salesforce_site) to userApi and crmSyncWorker.
#
# WHAT THIS CHANGES
#   1. IAM (new, separately named inline policies — revocable on their own;
#      no existing policy is edited):
#        userApi        "userApi-site-delivery"
#          SiteDeliveryEndpoints  Get/Put/Update/Delete
#          People                 Get/Put/Update/Query
#        crmSyncWorker  "crmSyncWorker-site-delivery"
#          SiteDeliveryEndpoints  Get/Update   (health/people-sync summaries)
#          People                 Get/Put/Update/Query
#          Users                  GetItem      (a tagged member's name/email)
#          SQS SendMessage on the CRM sync queue (people-sync continuation)
#          KMS Decrypt on the key that encrypts endpoint credentials
#      Audio size comes from S3 HeadObject, which the s3:GetObject grant in
#      scripts/74 already covers.
#   2. Environment on both Lambdas: SITE_DELIVERY_ENDPOINTS_TABLE,
#      PEOPLE_TABLE, SITE_AUDIO_BASE_URL (+ optional overrides below).
#   3. Code: repackages both Lambdas (the worker bundles userapi as
#      userapi_worker.py, exactly as scripts/65/73 do).
#   4. API Gateway: adds the new routes. No existing route is touched.
#
# WHAT IT DOES NOT CHANGE: any Salesforce OAuth resource, connection row,
# secret, route or setting. Existing workspaces keep their current Salesforce
# behaviour; Site delivery is off for every workspace until an Owner/Manager
# configures an endpoint (PUT /workspaces/{id}/site-delivery).
#
# Optional overrides (env):
#   SITE_AUDIO_BASE_URL                  public API origin for audio links
#                                        (default: userApi's SHARE_BASE_URL)
#   SITE_AUDIO_URL_TTL_SECONDS           audio link lifetime (default 30 days)
#   SITE_DELIVERY_ALLOWED_HOST_SUFFIXES  comma list of receiver host suffixes
#
# Prerequisites: 63/64/65 (jobs table, queue, worker), 74 (worker S3 read),
# 77 (the two new tables).
# =============================================================
set -euo pipefail
cd "$(dirname "$0")"
. ./aws.sh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

USERAPI_FN="${USERAPI_FN:-userApi}"
WORKER_FN="${WORKER_FN:-crmSyncWorker}"
SITE_DELIVERY_ENDPOINTS_TABLE="${SITE_DELIVERY_ENDPOINTS_TABLE:-SiteDeliveryEndpoints}"
PEOPLE_TABLE="${PEOPLE_TABLE:-People}"
USERS_TABLE="${USERS_TABLE:-Users}"
: "${CRM_SYNC_QUEUE_ARN:?CRM_SYNC_QUEUE_ARN not set — see scripts/64}"

for t in "$SITE_DELIVERY_ENDPOINTS_TABLE" "$PEOPLE_TABLE"; do
  aws dynamodb describe-table --table-name "$t" >/dev/null 2>&1 || {
    echo "FAIL: table $t not found — run 77_create_site_delivery_tables.sh first" >&2
    exit 1; }
done
for f in site_contract.py site_delivery_client.py people_store.py; do
  test -f "$PROJECT_ROOT/shared/$f" || { echo "FAIL: shared/$f missing" >&2; exit 1; }
done

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ddb_arn() { echo "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/$1"; }
ENDPOINTS_ARN="$(ddb_arn "$SITE_DELIVERY_ENDPOINTS_TABLE")"
PEOPLE_ARN="$(ddb_arn "$PEOPLE_TABLE")"
USERS_ARN="$(ddb_arn "$USERS_TABLE")"

userapi_env() {
  aws lambda get-function-configuration --function-name "$USERAPI_FN" \
    --query "Environment.Variables.$1" --output text 2>/dev/null | tr -d '\r'
}
# Endpoint credentials are encrypted with the integrations key (see
# _integration_kms_encrypt; provisioned by scripts/40_deploy_gmail_integration.sh).
KMS_KEY_ID="$(userapi_env INTEGRATIONS_KMS_KEY_ID)"
[[ -z "$KMS_KEY_ID" || "$KMS_KEY_ID" == "None" ]] && {
  echo "FAIL: userApi has no INTEGRATIONS_KMS_KEY_ID" >&2; exit 1; }
case "$KMS_KEY_ID" in
  arn:*) KMS_KEY_ARN="$KMS_KEY_ID" ;;
  *)     KMS_KEY_ARN="arn:aws:kms:${AWS_REGION}:${ACCOUNT_ID}:key/${KMS_KEY_ID}" ;;
esac

if [[ -z "${SITE_AUDIO_BASE_URL:-}" ]]; then
  SITE_AUDIO_BASE_URL="$(userapi_env SHARE_BASE_URL)"
  [[ "$SITE_AUDIO_BASE_URL" == "None" ]] && SITE_AUDIO_BASE_URL=""
fi
if [[ -z "$SITE_AUDIO_BASE_URL" ]]; then
  echo "WARNING: no SITE_AUDIO_BASE_URL and no SHARE_BASE_URL on $USERAPI_FN —"
  echo "         meetings will be delivered WITHOUT an audio link until one is set."
fi

POLICY_FILE="$(mktemp)"
trap 'rm -f "$POLICY_FILE"' EXIT

put_policy() {
  local fn="$1" name="$2" role
  role="$(aws lambda get-function --function-name "$fn" \
            --query 'Configuration.Role' --output text)"
  role="${role##*/}"
  aws iam put-role-policy --role-name "$role" --policy-name "$name" \
    --policy-document "file://$(winpath "$POLICY_FILE")"
  echo ">> $fn ($role): $name policy set."
}

cat > "$POLICY_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "SiteDeliveryEndpoints", "Effect": "Allow",
      "Action": ["dynamodb:GetItem","dynamodb:PutItem","dynamodb:UpdateItem","dynamodb:DeleteItem"],
      "Resource": ["${ENDPOINTS_ARN}"] },
    { "Sid": "PeopleStore", "Effect": "Allow",
      "Action": ["dynamodb:GetItem","dynamodb:PutItem","dynamodb:UpdateItem","dynamodb:Query"],
      "Resource": ["${PEOPLE_ARN}"] }
  ]
}
EOF
put_policy "$USERAPI_FN" "userApi-site-delivery"

cat > "$POLICY_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "SiteDeliveryEndpoints", "Effect": "Allow",
      "Action": ["dynamodb:GetItem","dynamodb:UpdateItem"],
      "Resource": ["${ENDPOINTS_ARN}"] },
    { "Sid": "PeopleStore", "Effect": "Allow",
      "Action": ["dynamodb:GetItem","dynamodb:PutItem","dynamodb:UpdateItem","dynamodb:Query"],
      "Resource": ["${PEOPLE_ARN}"] },
    { "Sid": "TaggedMemberProfile", "Effect": "Allow",
      "Action": ["dynamodb:GetItem"],
      "Resource": ["${USERS_ARN}"] },
    { "Sid": "PeopleSyncContinuation", "Effect": "Allow",
      "Action": ["sqs:SendMessage"],
      "Resource": ["${CRM_SYNC_QUEUE_ARN}"] },
    { "Sid": "EndpointCredentials", "Effect": "Allow",
      "Action": ["kms:Decrypt"],
      "Resource": ["${KMS_KEY_ARN}"] }
  ]
}
EOF
put_policy "$WORKER_FN" "crmSyncWorker-site-delivery"

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
  --zip-file "fileb://$(winpath "$WORKER_ZIP")" \
  --query "[FunctionName,CodeSize]" --output text
aws lambda wait function-updated-v2 --function-name "$WORKER_FN"

ENV_ARGS=("SITE_DELIVERY_ENDPOINTS_TABLE=$SITE_DELIVERY_ENDPOINTS_TABLE"
          "PEOPLE_TABLE=$PEOPLE_TABLE"
          "SITE_AUDIO_BASE_URL=$SITE_AUDIO_BASE_URL")
[[ -n "${SITE_AUDIO_URL_TTL_SECONDS:-}" ]] && \
  ENV_ARGS+=("SITE_AUDIO_URL_TTL_SECONDS=$SITE_AUDIO_URL_TTL_SECONDS")
[[ -n "${SITE_DELIVERY_ALLOWED_HOST_SUFFIXES:-}" ]] && \
  ENV_ARGS+=("SITE_DELIVERY_ALLOWED_HOST_SUFFIXES=$SITE_DELIVERY_ALLOWED_HOST_SUFFIXES")
for FN in "$USERAPI_FN" "$WORKER_FN"; do
  echo "==> Setting Site delivery configuration on $FN"
  CURRENT_ENV="$(aws lambda get-function-configuration --function-name "$FN" \
                   --query 'Environment.Variables' --output json)"
  ENV_JSON="$(python "$SCRIPT_DIR/_merge_env.py" "$CURRENT_ENV" "${ENV_ARGS[@]}")"
  aws lambda update-function-configuration --function-name "$FN" \
    --environment "$ENV_JSON" >/dev/null
  aws lambda wait function-updated-v2 --function-name "$FN"
done

API_ID="${API_ID:-$(aws apigatewayv2 get-apis \
  --query "Items[?Name=='${API_NAME:-meeting-recorder-api}'].ApiId | [0]" \
  --output text | tr -d '\r')}"
[[ -n "$API_ID" && "$API_ID" != "None" ]] || { echo "FAIL: no API id" >&2; exit 1; }
INTEGRATION_ID="$(aws apigatewayv2 get-integrations --api-id "$API_ID" \
  --query "Items[?contains(IntegrationUri, ':${USERAPI_FN}')].IntegrationId | [0]" \
  --output text | tr -d '\r')"
[[ -n "$INTEGRATION_ID" && "$INTEGRATION_ID" != "None" ]] || {
  echo "FAIL: no $USERAPI_FN integration on $API_ID" >&2; exit 1; }

ensure_route() {
  local route_key="$1" existing
  # JSON output, not text — see scripts/69 for the CRLF bug text mode hits.
  existing="$(aws apigatewayv2 get-routes --api-id "$API_ID" \
      --query "Items[?RouteKey=='${route_key}'].RouteId | [0]" --output json \
      | tr -d '"\r')"
  if [[ -n "$existing" && "$existing" != "null" ]]; then
    echo ">> Route exists: $route_key"
    return 0
  fi
  aws apigatewayv2 create-route --api-id "$API_ID" --route-key "$route_key" \
    --target "integrations/$INTEGRATION_ID" >/dev/null
  echo ">> Route created: $route_key"
}

ensure_route "GET /workspaces/{workspace_id}/site-delivery"
ensure_route "PUT /workspaces/{workspace_id}/site-delivery"
ensure_route "DELETE /workspaces/{workspace_id}/site-delivery"
ensure_route "POST /workspaces/{workspace_id}/site-delivery/health"
ensure_route "POST /workspaces/{workspace_id}/site-delivery/people-sync"
ensure_route "GET /workspaces/{workspace_id}/site-delivery/people-sync"
ensure_route "GET /workspaces/{workspace_id}/people"
ensure_route "POST /recordings/site-delivery/{key+}"
ensure_route "GET /recordings/site-delivery/{key+}"
ensure_route "POST /recordings/site-delivery-retry/{key+}"
ensure_route "PUT /recordings/site-participants/{key+}"
# Unauthenticated at the gateway by design (the signed token is the
# credential) — same as the existing /share/{token}/audio route.
ensure_route "GET /delivery/audio/{token}"

cat <<EOF

==> Done. Salesforce Site delivery is deployed and OFF for every workspace.

Enable it for one workspace (Owner/Manager JWT):
  PUT  /workspaces/<workspace_id>/site-delivery
       {"url": "https://<site>/services/apexrest/minutex/v1/",
        "auth_mode": "hmac_sha256", "shared_secret": "<32+ chars>"}
  POST /workspaces/<workspace_id>/site-delivery/health

Watch the worker:
  aws logs tail /aws/lambda/$WORKER_FN --follow --filter-pattern site-delivery
EOF
