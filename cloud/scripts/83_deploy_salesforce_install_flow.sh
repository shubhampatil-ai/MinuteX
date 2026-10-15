#!/usr/bin/env bash
# =============================================================
# 83_deploy_salesforce_install_flow.sh — "Install MinuteX in Salesforce"
# from the app (Settings -> Integrations -> Salesforce).
#
#   bash 83_deploy_salesforce_install_flow.sh [PACKAGE_VERSION_ID]
#
# PACKAGE_VERSION_ID is the MinuteX package version (04t..., from
# salesforce/minutex-site/scripts/release_package.ps1 or Setup > Package
# Manager > Upload). Without it the flow answers "isn't available right now"
# until you re-run this script with it. Run AFTER 81. Idempotent.
#
# 1. Table SiteSalesforcePairings (PK pairing_key, TTL expires_epoch): the
#    one-time setup codes. Nothing else is altered.
# 2. IAM: userApi "userApi-site-salesforce-install" on that table only.
# 3. userApi code.
# 4. Routes (before the env, so an env failure never leaves them missing):
#      POST /workspaces/{workspace_id}/site-salesforce/install
#      GET  /workspaces/{workspace_id}/site-salesforce/install
#      POST /workspaces/{workspace_id}/site-salesforce/install/connect
#      POST /salesforce/pair        (no JWT: called by the org's MinuteX Setup
#                                    tab; the one-time setup code is the
#                                    credential, and MinuteX verifies the Site)
# 5. userApi env: SITE_SF_PACKAGE_VERSION_ID; the dead OAuth Salesforce
#    variables are removed to stay under Lambda's 4 KB limit.
# =============================================================
set -euo pipefail
cd "$(dirname "$0")"
. ./aws.sh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

USERAPI_FN="${USERAPI_FN:-userApi}"
PAIRINGS_TABLE="${SITE_SF_PAIRINGS_TABLE:-SiteSalesforcePairings}"
PACKAGE_VERSION_ID="${1:-${SITE_SF_PACKAGE_VERSION_ID:-}}"
if [[ -n "$PACKAGE_VERSION_ID" && "$PACKAGE_VERSION_ID" != 04t* ]]; then
  echo "FAIL: the package version id starts with 04t" >&2; exit 1
fi

if aws dynamodb describe-table --table-name "$PAIRINGS_TABLE" >/dev/null 2>&1; then
  echo ">> Table '$PAIRINGS_TABLE' already exists."
else
  echo ">> Creating '$PAIRINGS_TABLE' ..."
  aws dynamodb create-table --table-name "$PAIRINGS_TABLE" --billing-mode PAY_PER_REQUEST \
    --attribute-definitions AttributeName=pairing_key,AttributeType=S \
    --key-schema AttributeName=pairing_key,KeyType=HASH >/dev/null
  aws dynamodb wait table-exists --table-name "$PAIRINGS_TABLE"
fi
aws dynamodb update-time-to-live --table-name "$PAIRINGS_TABLE" \
  --time-to-live-specification "Enabled=true,AttributeName=expires_epoch" >/dev/null 2>&1 \
  && echo "   TTL on expires_epoch." || echo "   TTL already configured."

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
POLICY_FILE="$(mktemp)"
trap 'rm -f "$POLICY_FILE"' EXIT
cat > "$POLICY_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "SalesforceInstallPairings", "Effect": "Allow",
      "Action": ["dynamodb:GetItem","dynamodb:PutItem","dynamodb:UpdateItem","dynamodb:DeleteItem"],
      "Resource": ["arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${PAIRINGS_TABLE}"] }
  ]
}
EOF
ROLE="$(aws lambda get-function --function-name "$USERAPI_FN" --query 'Configuration.Role' --output text)"
ROLE="${ROLE##*/}"
aws iam put-role-policy --role-name "$ROLE" --policy-name "userApi-site-salesforce-install" \
  --policy-document "file://$(winpath "$POLICY_FILE")"
echo ">> $USERAPI_FN ($ROLE): userApi-site-salesforce-install policy set."

echo "==> Packaging userApi"
python "$SCRIPT_DIR/_package_lambda.py" functions/userapi "$PROJECT_ROOT"
aws lambda update-function-code --function-name "$USERAPI_FN" \
  --zip-file "fileb://$(winpath "$PROJECT_ROOT/functions/userapi/function.zip")" \
  --query "[FunctionName,CodeSize]" --output text
aws lambda wait function-updated-v2 --function-name "$USERAPI_FN"

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
ensure_route "POST /workspaces/{workspace_id}/site-salesforce/install"
ensure_route "GET /workspaces/{workspace_id}/site-salesforce/install"
ensure_route "POST /workspaces/{workspace_id}/site-salesforce/install/connect"
ensure_route "POST /salesforce/pair"
# userApi's variables are at Lambda's 4 KB cap, so make room by removing the
# old OAuth Salesforce variables. The code deployed above reads none of them;
# they are the same userApi list script 80 drops. SITE_SF_PAIRINGS_TABLE is set
# only when it differs from the code's default.
ENV_ARGS=(-CRM_CONNECTIONS_TABLE -ORG_CRM_CONNECTIONS_TABLE -ORG_SALESFORCE_REDIRECT_URI
          -SALESFORCE_API_VERSION -SALESFORCE_CLIENT_ID -SALESFORCE_CLIENT_SECRET_ARN
          -SALESFORCE_KMS_KEY_ID -SALESFORCE_LOGIN_URL -SALESFORCE_REDIRECT_URI
          -SALESFORCE_RETURN_URL -SF_MEETING_EXTERNAL_ID -SF_MEETING_OBJECT
          -SF_PACKAGE_INSTALL_URL)
[[ "$PAIRINGS_TABLE" != "SiteSalesforcePairings" ]] && ENV_ARGS+=("SITE_SF_PAIRINGS_TABLE=$PAIRINGS_TABLE")
[[ -n "$PACKAGE_VERSION_ID" ]] && ENV_ARGS+=("SITE_SF_PACKAGE_VERSION_ID=$PACKAGE_VERSION_ID")
CURRENT_ENV="$(aws lambda get-function-configuration --function-name "$USERAPI_FN"                  --query 'Environment.Variables' --output json)"
ENV_JSON="$(python "$SCRIPT_DIR/_merge_env.py" "$CURRENT_ENV" "${ENV_ARGS[@]}")"
aws lambda update-function-configuration --function-name "$USERAPI_FN" --environment "$ENV_JSON" >/dev/null
aws lambda wait function-updated-v2 --function-name "$USERAPI_FN"
echo ">> $USERAPI_FN environment updated${PACKAGE_VERSION_ID:+ (package $PACKAGE_VERSION_ID)}."

[[ -n "$PACKAGE_VERSION_ID" ]] || echo ">> NOTE: no package version id yet — re-run with it once uploaded."
