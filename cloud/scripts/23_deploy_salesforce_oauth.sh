#!/usr/bin/env bash
# =============================================================
# 23_deploy_salesforce_oauth.sh — Phase 1: Salesforce OAuth connect.
#
# Provisions everything userApi needs for the web-server OAuth flow and
# deploys the four new routes (see the CRM section in functions/userapi):
#
#   1. A KMS key (alias alias/crm-salesforce) to envelope-encrypt each
#      user's refresh token before it is written to DynamoDB. Created only
#      if SALESFORCE_KMS_KEY_ID is not already set in .env.
#   2. A Secrets Manager secret holding the Connected App's consumer
#      secret (SALESFORCE_CLIENT_SECRET from .env — never becomes a
#      plaintext Lambda env var, unlike ELEVENLABS_API_KEY/GROQ_API_KEY,
#      which the migration notes already flag as something to fix).
#   3. userApi role: extends the inline policy with CrmConnections
#      read/write/delete, KMS Encrypt/Decrypt (scoped to that one key),
#      and GetSecretValue on the new secret.
#   4. Deploys functions/userapi/lambda_function.py and wires the four routes:
#        GET    /crm/salesforce/connect
#        GET    /crm/salesforce/callback   (no authorizer — see note below)
#        GET    /crm/salesforce/status
#        DELETE /crm/salesforce
#
# /crm/salesforce/callback needs no special gateway treatment: like every
# other route in this API, it has AuthorizationType NONE at the gateway
# level — this repo never uses an API Gateway authorizer, JWT auth is
# enforced inside each handler via _require_auth(). /callback is simply
# the one route that skips that check and verifies `state` instead, since
# Salesforce's redirect is an unauthenticated browser GET, not a call the
# app can attach a bearer token to.
#
# Prerequisites:
#   - scripts/22_create_crm_connections_table.sh (CrmConnections table)
#   - A Salesforce Connected App already created in your org, with:
#       Callback URL = ${API_URL}/crm/salesforce/callback
#       OAuth scopes: at least "api" and "refresh_token, offline_access"
#     (Phase 2 will need "id"/"openid" too, already requested by whoami().)
#   - .env: SALESFORCE_CLIENT_ID, SALESFORCE_CLIENT_SECRET,
#     SALESFORCE_LOGIN_URL (default https://login.salesforce.com — use
#     https://test.salesforce.com for a sandbox), SALESFORCE_RETURN_URL
#     (deep link back into the app, e.g. minutex://crm/salesforce/connected).
#
# Idempotent: create-key/create-secret are skipped if already provisioned
# (by SALESFORCE_KMS_KEY_ID / a describe-secret probe); update-function-code
# and put-role-policy simply overwrite; ensure_route skips existing routes.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"
CRM_CONNECTIONS_TABLE="${CRM_CONNECTIONS_TABLE:-CrmConnections}"
SALESFORCE_LOGIN_URL="${SALESFORCE_LOGIN_URL:-https://login.salesforce.com}"
: "${SALESFORCE_CLIENT_ID:?SALESFORCE_CLIENT_ID not set (put it in .env)}"
: "${SALESFORCE_CLIENT_SECRET:?SALESFORCE_CLIENT_SECRET not set (put it in .env)}"
: "${SALESFORCE_RETURN_URL:?SALESFORCE_RETURN_URL not set (deep link back into the app)}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

API_ID="$(aws apigatewayv2 get-apis \
            --query "Items[?Name=='${API_NAME}'].ApiId | [0]" \
            --output text)"
if [[ "$API_ID" == "None" || -z "$API_ID" ]]; then
  echo "ERROR: HTTP API '$API_NAME' not found — run 03_create_apigateway.sh first." >&2
  exit 1
fi
echo ">> API: $API_NAME ($API_ID)"

SALESFORCE_REDIRECT_URI="${SALESFORCE_REDIRECT_URI:-${API_URL%/}/crm/salesforce/callback}"
echo ">> Redirect URI (must match the Connected App's Callback URL exactly):"
echo "     $SALESFORCE_REDIRECT_URI"

# -------------------------------------------------------------
# 1. KMS key for refresh-token envelope encryption.
# -------------------------------------------------------------
if [[ -n "${SALESFORCE_KMS_KEY_ID:-}" ]]; then
  echo ">> Using existing KMS key from .env: $SALESFORCE_KMS_KEY_ID"
else
  EXISTING_KEY="$(aws kms list-aliases \
      --query "Aliases[?AliasName=='alias/crm-salesforce'].TargetKeyId | [0]" \
      --output text 2>/dev/null || true)"
  if [[ -n "$EXISTING_KEY" && "$EXISTING_KEY" != "None" ]]; then
    SALESFORCE_KMS_KEY_ID="$EXISTING_KEY"
    echo ">> Found existing KMS key via alias/crm-salesforce: $SALESFORCE_KMS_KEY_ID"
  else
    SALESFORCE_KMS_KEY_ID="$(aws kms create-key \
        --description "Envelope-encrypts per-user CRM OAuth refresh tokens (MinuteX)" \
        --query 'KeyMetadata.KeyId' --output text)"
    aws kms create-alias --alias-name "alias/crm-salesforce" --target-key-id "$SALESFORCE_KMS_KEY_ID"
    echo ">> Created KMS key: $SALESFORCE_KMS_KEY_ID (alias/crm-salesforce)"
  fi
  echo ">> Add this to .env so future runs reuse it: SALESFORCE_KMS_KEY_ID=$SALESFORCE_KMS_KEY_ID"
fi
KMS_KEY_ARN="arn:aws:kms:${AWS_REGION}:${ACCOUNT_ID}:key/${SALESFORCE_KMS_KEY_ID}"

# -------------------------------------------------------------
# 2. Secrets Manager: Connected App consumer secret.
# -------------------------------------------------------------
SECRET_NAME="userApi/salesforceClientSecret"
if aws secretsmanager describe-secret --secret-id "$SECRET_NAME" >/dev/null 2>&1; then
  aws secretsmanager put-secret-value --secret-id "$SECRET_NAME" \
      --secret-string "$SALESFORCE_CLIENT_SECRET" >/dev/null
  echo ">> Updated existing secret: $SECRET_NAME"
else
  aws secretsmanager create-secret --name "$SECRET_NAME" \
      --description "Salesforce Connected App consumer secret (MinuteX CRM OAuth)" \
      --secret-string "$SALESFORCE_CLIENT_SECRET" >/dev/null
  echo ">> Created secret: $SECRET_NAME"
fi
SALESFORCE_CLIENT_SECRET_ARN="$(aws secretsmanager describe-secret \
    --secret-id "$SECRET_NAME" --query 'ARN' --output text)"

# -------------------------------------------------------------
# 3. userApi role: extend inline policy (CrmConnections, KMS, new secret).
#    Additive — read the existing policy document names so this script
#    doesn't need to know every Sid the previous scripts already added;
#    put-role-policy on a DIFFERENT policy name keeps them all in force.
# -------------------------------------------------------------
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
    { "Sid": "CrmConnections", "Effect": "Allow",
      "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
                 "dynamodb:DeleteItem"],
      "Resource": "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${CRM_CONNECTIONS_TABLE}" },
    { "Sid": "SalesforceTokenKms", "Effect": "Allow",
      "Action": ["kms:Encrypt", "kms:Decrypt"],
      "Resource": "${KMS_KEY_ARN}" },
    { "Sid": "SalesforceClientSecret", "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "${SALESFORCE_CLIENT_SECRET_ARN}" }
  ]
}
EOF
aws iam put-role-policy \
  --role-name "$USERAPI_ROLE" \
  --policy-name "userApi-crm-salesforce" \
  --policy-document "file://$(winpath "$POLICY_FILE")"
echo ">> userApi-crm-salesforce inline policy set (CrmConnections, KMS, secret)."

# -------------------------------------------------------------
# 4. Deploy userApi with the new env vars merged in (existing vars survive).
# -------------------------------------------------------------
CURRENT_ENV="$(aws lambda get-function-configuration \
                 --function-name "$USERAPI_LAMBDA_NAME" \
                 --query 'Environment.Variables' --output json)"
MERGED_ENV="$(python - "$CURRENT_ENV" "$CRM_CONNECTIONS_TABLE" "$SALESFORCE_LOGIN_URL" \
    "$SALESFORCE_CLIENT_ID" "$SALESFORCE_REDIRECT_URI" "$SALESFORCE_CLIENT_SECRET_ARN" \
    "$SALESFORCE_KMS_KEY_ID" "$SALESFORCE_RETURN_URL" <<'PY'
import json, sys
(env_json, crm_table, login_url, client_id, redirect_uri,
 secret_arn, kms_key_id, return_url) = sys.argv[1:9]
env = json.loads(env_json) if env_json not in ("null", "") else {}
env.update({
    "CRM_CONNECTIONS_TABLE": crm_table,
    "SALESFORCE_LOGIN_URL": login_url,
    "SALESFORCE_CLIENT_ID": client_id,
    "SALESFORCE_REDIRECT_URI": redirect_uri,
    "SALESFORCE_CLIENT_SECRET_ARN": secret_arn,
    "SALESFORCE_KMS_KEY_ID": kms_key_id,
    "SALESFORCE_RETURN_URL": return_url,
})
print(json.dumps({"Variables": env}))
PY
)"
aws lambda update-function-configuration \
  --function-name "$USERAPI_LAMBDA_NAME" \
  --environment "$MERGED_ENV" \
  --query "[FunctionName,Environment.Variables.CRM_CONNECTIONS_TABLE]" --output text
aws lambda wait function-updated-v2 --function-name "$USERAPI_LAMBDA_NAME"

# Package with the shared AI core vendored in FLAT (ai_schema.py,
# groq_client.py, prompts.py at the archive root — that is how
# `import ai_schema` resolves in the Lambda runtime). Since script 21,
# userApi imports those modules at module scope, so a zip containing only
# lambda_function.py would fail on cold start with ModuleNotFoundError and
# take down EVERY route, not just the CRM ones. Same helper as script 21.
deploy_py_with_shared() {
  local fn="$1" src_dir="$2"
  local zip="$PROJECT_ROOT/$src_dir/function.zip"
  rm -f "$zip"
  ( cd "$PROJECT_ROOT" && python - "$src_dir" <<'PY'
import sys, zipfile
from pathlib import Path

src_dir = sys.argv[1]
root = Path.cwd()
out = root / src_dir / "function.zip"
shared = root / "shared"

with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    z.write(root / src_dir / "lambda_function.py", "lambda_function.py")
    for mod in sorted(shared.glob("*.py")):
        z.write(mod, mod.name)
        print(f"   + {mod.name}")
print(f">> packaged {out.relative_to(root)}")
PY
  )
  aws lambda update-function-code \
    --function-name "$fn" \
    --zip-file "fileb://$(winpath "$zip")" \
    --publish \
    --query "[FunctionName,CodeSize,LastModified]" --output text
  aws lambda wait function-updated-v2 --function-name "$fn"
  echo ">> $fn deployed from $src_dir/ + shared/"
}
deploy_py_with_shared "$USERAPI_LAMBDA_NAME" "functions/userapi"

# -------------------------------------------------------------
# 5. Wire the four routes.
# -------------------------------------------------------------
USERAPI_INT="$(aws apigatewayv2 get-integrations --api-id "$API_ID" \
    --query "Items[?contains(IntegrationUri, '${USERAPI_LAMBDA_NAME}')].IntegrationId | [0]" \
    --output text)"
if [[ "$USERAPI_INT" == "None" || -z "$USERAPI_INT" ]]; then
  echo "ERROR: no API integration for $USERAPI_LAMBDA_NAME — run 17 first." >&2
  exit 1
fi

# ensure_route <integration-id> <route-key>
# (see script 20 for why the head -1 | tr -d '\r' normalization is load-bearing)
ensure_route() {
  local integration_id="$1" route_key="$2"
  local existing
  existing="$(aws apigatewayv2 get-routes --api-id "$API_ID" \
      --query "Items[?RouteKey=='${route_key}'].RouteId | [0]" --output text \
      | head -1 | tr -d '\r')"
  if [[ -n "$existing" && "$existing" != "None" ]]; then
    echo ">> Route exists: $route_key ($existing)"
    return 0
  fi
  aws apigatewayv2 create-route \
    --api-id "$API_ID" \
    --route-key "$route_key" \
    --target "integrations/$integration_id" >/dev/null
  echo ">> Route created: $route_key"
}

ensure_route "$USERAPI_INT" "GET /crm/salesforce/connect"
ensure_route "$USERAPI_INT" "GET /crm/salesforce/callback"
ensure_route "$USERAPI_INT" "GET /crm/salesforce/status"
ensure_route "$USERAPI_INT" "DELETE /crm/salesforce"

echo
echo ">> Done. Verify with:"
echo "     python tests/test_salesforce_oauth.py"
echo ">> Reminder: set the Connected App's Callback URL to exactly:"
echo "     $SALESFORCE_REDIRECT_URI"
