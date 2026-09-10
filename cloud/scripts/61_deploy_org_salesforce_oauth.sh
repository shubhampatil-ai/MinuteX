#!/usr/bin/env bash
# =============================================================
# 61_deploy_org_salesforce_oauth.sh — Phase 2D.1: Organisation Salesforce
# OAuth connect.
#
# Provisions what userApi needs for the WORKSPACE-scoped Salesforce OAuth
# flow and deploys its routes (see the "Organisation CRM" section in
# functions/userapi/lambda_function.py):
#
#   1. userApi role: extends the inline policy with OrgCrmConnections
#      read/write/delete. Reuses the EXISTING KMS key and Secrets Manager
#      secret from 23_deploy_salesforce_oauth.sh — same Connected App, same
#      encryption key, so no new secret/key is created here.
#   2. Deploys functions/userapi/lambda_function.py and wires the routes:
#        GET    /workspaces/{workspace_id}/crm/salesforce/connect
#        GET    /crm/salesforce/org-callback   (no authorizer — see note)
#        GET    /workspaces/{workspace_id}/crm/salesforce/status
#        DELETE /workspaces/{workspace_id}/crm/salesforce
#
# WHY THE CALLBACK IS A FIXED, UNPARAMETERIZED PATH. Salesforce Connected
# Apps redirect to an EXACT, pre-registered Callback URL — there is no way
# to template a workspace id into it, and standard Connected Apps do not
# support wildcard callback URLs. So /crm/salesforce/org-callback is ONE
# static route (a second Callback URL entry on the SAME Connected App used
# by Personal Salesforce), and the workspace + initiating user instead ride
# inside the signed `state` param, exactly as user_id already does for
# Personal (see _sign_org_crm_state / _verify_org_crm_state). The callback
# has AuthorizationType NONE at the gateway, like every unauthenticated
# callback in this API — identity is proven by the HMAC-signed state, not a
# bearer token.
#
# Prerequisites:
#   - scripts/22_create_crm_connections_table.sh and
#     scripts/23_deploy_salesforce_oauth.sh already run (Personal Salesforce
#     working; SALESFORCE_KMS_KEY_ID / SALESFORCE_CLIENT_SECRET_ARN set)
#   - scripts/60_create_org_crm_connections_table.sh already run
#   - scripts/56_deploy_workspace_resources.sh already run (workspace/RBAC
#     tables + WORKSPACE_MEMBERSHIPS_TABLE exist)
#   - The Connected App used by Personal Salesforce has a SECOND Callback
#     URL added: ${API_URL}/crm/salesforce/org-callback
#
# Idempotent: put-role-policy overwrites; ensure_route skips existing routes.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"
ORG_CRM_CONNECTIONS_TABLE="${ORG_CRM_CONNECTIONS_TABLE:-OrgCrmConnections}"
: "${SALESFORCE_KMS_KEY_ID:?SALESFORCE_KMS_KEY_ID not set — run 23_deploy_salesforce_oauth.sh first}"
: "${SALESFORCE_CLIENT_ID:?SALESFORCE_CLIENT_ID not set (put it in .env)}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
KMS_KEY_ARN="arn:aws:kms:${AWS_REGION}:${ACCOUNT_ID}:key/${SALESFORCE_KMS_KEY_ID}"

API_ID="$(aws apigatewayv2 get-apis \
            --query "Items[?Name=='${API_NAME}'].ApiId | [0]" \
            --output text)"
if [[ "$API_ID" == "None" || -z "$API_ID" ]]; then
  echo "ERROR: HTTP API '$API_NAME' not found — run 03_create_apigateway.sh first." >&2
  exit 1
fi
echo ">> API: $API_NAME ($API_ID)"

ORG_SALESFORCE_REDIRECT_URI="${ORG_SALESFORCE_REDIRECT_URI:-${API_URL%/}/crm/salesforce/org-callback}"
echo ">> Org redirect URI (add as a SECOND Callback URL on the same Connected App):"
echo "     $ORG_SALESFORCE_REDIRECT_URI"

# -------------------------------------------------------------
# 1. userApi role: extend inline policy (OrgCrmConnections only — KMS key
#    and client secret policies already exist from script 23).
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
    { "Sid": "OrgCrmConnections", "Effect": "Allow",
      "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
                 "dynamodb:DeleteItem"],
      "Resource": "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${ORG_CRM_CONNECTIONS_TABLE}" }
  ]
}
EOF
aws iam put-role-policy \
  --role-name "$USERAPI_ROLE" \
  --policy-name "userApi-org-crm-salesforce" \
  --policy-document "file://$(winpath "$POLICY_FILE")"
echo ">> userApi-org-crm-salesforce inline policy set (OrgCrmConnections)."

# -------------------------------------------------------------
# 2. Deploy userApi with the new env vars merged in (existing vars survive).
#    Reuses SALESFORCE_CLIENT_ID / SALESFORCE_CLIENT_SECRET_ARN /
#    SALESFORCE_LOGIN_URL / SALESFORCE_KMS_KEY_ID from script 23 as-is.
# -------------------------------------------------------------
CURRENT_ENV="$(aws lambda get-function-configuration \
                 --function-name "$USERAPI_LAMBDA_NAME" \
                 --query 'Environment.Variables' --output json)"
MERGED_ENV="$(python - "$CURRENT_ENV" "$ORG_CRM_CONNECTIONS_TABLE" "$ORG_SALESFORCE_REDIRECT_URI" <<'PY'
import json, sys
env_json, org_crm_table, org_redirect_uri = sys.argv[1:4]
env = json.loads(env_json) if env_json not in ("null", "") else {}
env.update({
    "ORG_CRM_CONNECTIONS_TABLE": org_crm_table,
    "ORG_SALESFORCE_REDIRECT_URI": org_redirect_uri,
})
print(json.dumps({"Variables": env}))
PY
)"
aws lambda update-function-configuration \
  --function-name "$USERAPI_LAMBDA_NAME" \
  --environment "$MERGED_ENV" \
  --query "[FunctionName,Environment.Variables.ORG_CRM_CONNECTIONS_TABLE]" --output text
aws lambda wait function-updated-v2 --function-name "$USERAPI_LAMBDA_NAME"

# Same packaging helper as script 23 — shared/ modules vendored flat.
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
# 3. Wire the routes.
# -------------------------------------------------------------
USERAPI_INT="$(aws apigatewayv2 get-integrations --api-id "$API_ID" \
    --query "Items[?contains(IntegrationUri, '${USERAPI_LAMBDA_NAME}')].IntegrationId | [0]" \
    --output text)"
if [[ "$USERAPI_INT" == "None" || -z "$USERAPI_INT" ]]; then
  echo "ERROR: no API integration for $USERAPI_LAMBDA_NAME — run 17 first." >&2
  exit 1
fi

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

ensure_route "$USERAPI_INT" "GET /workspaces/{workspace_id}/crm/salesforce/connect"
ensure_route "$USERAPI_INT" "GET /crm/salesforce/org-callback"
ensure_route "$USERAPI_INT" "GET /workspaces/{workspace_id}/crm/salesforce/status"
ensure_route "$USERAPI_INT" "DELETE /workspaces/{workspace_id}/crm/salesforce"

echo
echo ">> Done. Verify with:"
echo "     python tests/test_org_salesforce_oauth.py"
echo ">> Reminder: add this as a SECOND Callback URL on the existing Connected App:"
echo "     $ORG_SALESFORCE_REDIRECT_URI"
