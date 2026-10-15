#!/usr/bin/env bash
# =============================================================
# 88_deploy_salesforce_oauth_connection.sh — connect a workspace to its
# Salesforce org through an INTEGRATION USER (OAuth web-server flow + PKCE).
# Approved 2026-10-05. Once a workspace connects, EVERY call to its receiver
# (events, files, searches, people, configuration) goes to the org's API
# address as the integration user (Bearer token, still HMAC-signed) instead
# of the Site address: a Site guest user can't own what it creates, so
# participants, tasks and files on a new meeting fail there. Workspaces that
# never connect keep the Site address (84) unchanged.
#
# The integration user needs (permission set): Apex class access to
# MinuteXReceiver; Read/Create/Edit on MinuteX Meeting, Participant, Delivery
# Log and Task; Read on every linkable object (Lead, Contact, Account, ...).
#
#   SF_OAUTH_CLIENT_ID=... bash 88_deploy_salesforce_oauth_connection.sh
#   SALESFORCE_OAUTH_ENABLED=0 bash 88_...sh     # deploy dark / roll back
#
# Needs (in .env or the environment):
#   SF_OAUTH_CLIENT_ID       the External Client App's consumer key
#   SF_OAUTH_CLIENT_SECRET   optional; only if the app requires the secret
#                            (stored in Secrets Manager, never as an env var)
#   INTEGRATION_RETURN_URL   the app's return link (already set by script 40)
#
# Run AFTER 40 (the KMS key alias/minutex-integrations and INTEGRATIONS_KMS_KEY_ID)
# and 84. Idempotent. (85-87 are other features.)
#
# 1. Offline tests.
# 2. Table SalesforceOAuthConnections (PK workspace_id, on-demand).
# 3. Optional secret userApi/salesforceClientSecret.
# 4. Managed policy minutex-salesforce-oauth on the userApi and worker roles
#    (userApi's inline policies are at IAM's 10 KB limit): the table, KMS on
#    the integrations key, GetSecretValue on the secret.
# 5. Code (userApi + crmSyncWorker, which imports userapi).
# 6. Routes.
# 7. Env on both functions.
#
# THE CALLBACK URL to register on the External Client App:
#   https://<api id>.execute-api.<region>.amazonaws.com/salesforce/oauth/callback
#
# ROLLBACK: SALESFORCE_OAUTH_ENABLED=0 bash 88_...sh — the routes answer 404
# salesforce_oauth_disabled; stored connections stay until disconnected.
# =============================================================
set -euo pipefail
cd "$(dirname "$0")"
. ./aws.sh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

USERAPI_FN="${USERAPI_FN:-userApi}"
WORKER_FN="${WORKER_FN:-crmSyncWorker}"
TABLE="${SF_OAUTH_CONNECTIONS_TABLE:-SalesforceOAuthConnections}"
SALESFORCE_OAUTH_ENABLED="${SALESFORCE_OAUTH_ENABLED:-1}"
: "${SF_OAUTH_CLIENT_ID:?SF_OAUTH_CLIENT_ID not set (the External Client App consumer key)}"
ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text | tr -d '\r')}"

echo "==> Offline tests"
( cd "$PROJECT_ROOT" && python -m pytest tests/test_salesforce_oauth_connection.py -q )

echo "==> KMS key (from script 40)"
if [[ -z "${INTEGRATIONS_KMS_KEY_ID:-}" ]]; then
  INTEGRATIONS_KMS_KEY_ID="$(aws kms list-aliases \
    --query "Aliases[?AliasName=='alias/minutex-integrations'].TargetKeyId | [0]" \
    --output text | tr -d '\r')"
fi
[[ -n "$INTEGRATIONS_KMS_KEY_ID" && "$INTEGRATIONS_KMS_KEY_ID" != "None" ]] || {
  echo "FAIL: no integrations KMS key — run 40 first." >&2; exit 1; }
KMS_KEY_ARN="arn:aws:kms:${AWS_REGION}:${ACCOUNT_ID}:key/${INTEGRATIONS_KMS_KEY_ID}"

echo "==> Table $TABLE"
if aws dynamodb describe-table --table-name "$TABLE" >/dev/null 2>&1; then
  echo ">> Table exists"
else
  aws dynamodb create-table --table-name "$TABLE" \
    --attribute-definitions AttributeName=workspace_id,AttributeType=S \
    --key-schema AttributeName=workspace_id,KeyType=HASH \
    --billing-mode PAY_PER_REQUEST >/dev/null
  aws dynamodb wait table-exists --table-name "$TABLE"
  echo ">> Table created"
fi

SECRET_ARN=""
if [[ -n "${SF_OAUTH_CLIENT_SECRET:-}" ]]; then
  echo "==> Secret userApi/salesforceClientSecret"
  SECRET_NAME="userApi/salesforceClientSecret"
  if aws secretsmanager describe-secret --secret-id "$SECRET_NAME" >/dev/null 2>&1; then
    aws secretsmanager put-secret-value --secret-id "$SECRET_NAME" \
      --secret-string "$SF_OAUTH_CLIENT_SECRET" >/dev/null
  else
    aws secretsmanager create-secret --name "$SECRET_NAME" \
      --description "Salesforce External Client App secret (MinuteX org connection)" \
      --secret-string "$SF_OAUTH_CLIENT_SECRET" >/dev/null
  fi
  SECRET_ARN="$(aws secretsmanager describe-secret --secret-id "$SECRET_NAME" \
    --query ARN --output text | tr -d '\r')"
fi

# A MANAGED policy, attached to both roles: userApi's role is at the 10 KB
# limit for inline policies (all inline policies on a role count together).
echo "==> Policy minutex-salesforce-oauth"
POLICY_NAME="minutex-salesforce-oauth"
POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/${POLICY_NAME}"
POLICY_FILE="$(mktemp)"
trap 'rm -f "$POLICY_FILE"' EXIT
SECRET_STMT=""
if [[ -n "$SECRET_ARN" ]]; then
  SECRET_STMT=",{ \"Sid\": \"SalesforceClientSecret\", \"Effect\": \"Allow\", \"Action\": \"secretsmanager:GetSecretValue\", \"Resource\": \"${SECRET_ARN}\" }"
fi
cat > "$POLICY_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "SalesforceOAuthTable", "Effect": "Allow",
      "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"],
      "Resource": "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${TABLE}" },
    { "Sid": "SalesforceTokenKms", "Effect": "Allow",
      "Action": ["kms:Encrypt", "kms:Decrypt"], "Resource": "${KMS_KEY_ARN}" }${SECRET_STMT}
  ]
}
EOF
if aws iam get-policy --policy-arn "$POLICY_ARN" >/dev/null 2>&1; then
  # A managed policy keeps at most 5 versions: drop the oldest spare one.
  OLD="$(aws iam list-policy-versions --policy-arn "$POLICY_ARN" \
    --query "Versions[?IsDefaultVersion==\`false\`] | sort_by(@, &CreateDate)[0].VersionId" \
    --output text | tr -d '\r')"
  if [[ -n "$OLD" && "$OLD" != "None" ]]; then
    aws iam delete-policy-version --policy-arn "$POLICY_ARN" --version-id "$OLD"
  fi
  aws iam create-policy-version --policy-arn "$POLICY_ARN" --set-as-default \
    --policy-document "file://$(winpath "$POLICY_FILE")" >/dev/null
  echo ">> $POLICY_NAME updated"
else
  aws iam create-policy --policy-name "$POLICY_NAME" \
    --description "MinuteX: Salesforce integration-user connection (script 88)" \
    --policy-document "file://$(winpath "$POLICY_FILE")" >/dev/null
  echo ">> $POLICY_NAME created"
fi
for fn in "$USERAPI_FN" "$WORKER_FN"; do
  r="$(aws lambda get-function --function-name "$fn" --query 'Configuration.Role' --output text)"
  aws iam attach-role-policy --role-name "${r##*/}" --policy-arn "$POLICY_ARN"
  echo ">> attached to ${r##*/}"
done

echo "==> Code"
python "$SCRIPT_DIR/_package_lambda.py" functions/userapi "$PROJECT_ROOT"
aws lambda update-function-code --function-name "$USERAPI_FN" \
  --zip-file "fileb://$(winpath "$PROJECT_ROOT/functions/userapi/function.zip")" \
  --query "[FunctionName,CodeSize]" --output text
aws lambda wait function-updated-v2 --function-name "$USERAPI_FN"
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
print(f">> packaged {out.relative_to(root)}")
PY
)
aws lambda update-function-code --function-name "$WORKER_FN" \
  --zip-file "fileb://$(winpath "$WORKER_ZIP")" --query "[FunctionName,CodeSize]" --output text
aws lambda wait function-updated-v2 --function-name "$WORKER_FN"

echo "==> Routes"
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
ensure_route() {
  local route_key="$1" existing
  existing="$(aws apigatewayv2 get-routes --api-id "$API_ID" \
      --query "Items[?RouteKey=='${route_key}'].RouteId | [0]" --output json | tr -d '"\r')"
  if [[ -n "$existing" && "$existing" != "null" ]]; then echo ">> Route exists: $route_key"; return 0; fi
  aws apigatewayv2 create-route --api-id "$API_ID" --route-key "$route_key" \
    --target "integrations/$INTEGRATION_ID" >/dev/null
  echo ">> Route created: $route_key"
}
ensure_route "POST /workspaces/{workspace_id}/salesforce/connect"
ensure_route "GET /salesforce/oauth/callback"
ensure_route "GET /workspaces/{workspace_id}/salesforce/connection"
ensure_route "POST /workspaces/{workspace_id}/salesforce/setup"
ensure_route "DELETE /workspaces/{workspace_id}/salesforce/connection"

API_URL="https://${API_ID}.execute-api.${AWS_REGION}.amazonaws.com"
REDIRECT_URI="${SF_OAUTH_REDIRECT_URI:-$API_URL/salesforce/oauth/callback}"

echo "==> Environment (SALESFORCE_OAUTH_ENABLED=$SALESFORCE_OAUTH_ENABLED)"
ENV_ARGS=("SALESFORCE_OAUTH_ENABLED=$SALESFORCE_OAUTH_ENABLED"
          "SF_OAUTH_CLIENT_ID=$SF_OAUTH_CLIENT_ID"
          "SF_OAUTH_REDIRECT_URI=$REDIRECT_URI"
          "SF_OAUTH_CONNECTIONS_TABLE=$TABLE"
          "SF_OAUTH_CLIENT_SECRET_ARN=$SECRET_ARN"
          # BOTH functions store the rotated refresh token (the worker refreshes
          # during delivery), so both need the key to encrypt it.
          "INTEGRATIONS_KMS_KEY_ID=$INTEGRATIONS_KMS_KEY_ID"
          # The MinuteX package version the app's Install button opens
          # (04t…; empty hides the button) and whether it is released.
          "SF_PACKAGE_VERSION_ID=${SF_PACKAGE_VERSION_ID:-}"
          "SF_PACKAGE_RELEASED=${SF_PACKAGE_RELEASED:-0}"
          # Where the package's receiver lives in a customer org.
          "SF_PACKAGE_NAMESPACE=${SF_PACKAGE_NAMESPACE:-DevMinuteX}")
for fn in "$USERAPI_FN" "$WORKER_FN"; do
  CURRENT_ENV="$(aws lambda get-function-configuration --function-name "$fn" \
                 --query 'Environment.Variables' --output json)"
  ENV_JSON="$(python "$SCRIPT_DIR/_merge_env.py" "$CURRENT_ENV" "${ENV_ARGS[@]}")"
  aws lambda update-function-configuration --function-name "$fn" \
    --environment "$ENV_JSON" --query FunctionName --output text
  aws lambda wait function-updated-v2 --function-name "$fn"
done

echo
echo "Done. Register this callback URL on the External Client App:"
echo "  $REDIRECT_URI"
