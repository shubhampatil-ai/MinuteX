#!/usr/bin/env bash
# =============================================================
# 25_deploy_crm_config.sh — user-configured Salesforce record mapping.
#
# Ships the schema-discovery, configuration and lookup routes on userApi:
#
#   GET  /crm/salesforce/objects              the org's objects + a suggestion
#   GET  /crm/salesforce/fields/{object_name} that object's fields, bucketed
#   GET  /crm/salesforce/config               {enabled, mappings:[...]}
#   PUT  /crm/salesforce/config               validate against Describe, save
#   POST /crm/salesforce/lookup               identifier -> record Id
#   POST /crm/salesforce/sync/{key+}          push the configured fields
#
# ...plus the generic CRM identifier extraction in transcribeRecording.
#
# The flow the routes implement, for ANY configured object:
#   identifier -> lookup (found | not_found | ambiguous) -> user confirms
#   -> push to the stored record Id -> synced
# Confirmation is mandatory: the sync route refuses a record the user has not
# confirmed, because notes on the wrong record are worse than no notes. After a
# successful resolution the record Id is reused directly — no repeated SOQL.
#
# WHY this exists rather than constants: every Salesforce org names its objects
# and fields differently, and a customer maps whichever objects they actually
# use — a site visit, a lead, an opportunity, a custom object, several at once.
# So the configuration is a LIST of mappings the user builds from their org's
# live Describe metadata, and the app renders one meeting input per mapping.
# Consequences worth knowing when operating this:
#   * No object or field API name appears anywhere in the code.
#   * A customer adding a new object type needs no deploy.
#   * Zero mappings is a valid state: meetings then show no CRM fields at all.
#   * A configuration written by the earlier single-object version keeps working
#     — it is read through the same generic mapping mechanism.
#
# Also adds the OAuth token-refresh path that Phase 1 deliberately deferred:
# only the refresh token is stored (KMS-encrypted), so every request that
# talks to Salesforce mints a short-lived access token on demand and retries
# once on a 401. Access tokens are never persisted — that keeps exactly one
# durable secret per user instead of two.
#
# The mapping is stored under `config` on the SAME CrmConnections row the
# connection already uses (PK user_id, SK provider), so a disconnect removes
# the mapping with it and there is no orphaned config to clean up.
#
# No new IAM, no new table: CrmConnections already has GetItem/PutItem, and
# this adds UpdateItem on the same table.
#
# NEW env var (optional):
#   SALESFORCE_API_VERSION=v62.0   REST/Describe version. Pinned rather than
#                                  floating so an org's Salesforce upgrade
#                                  can't silently change describe output.
#
# Prerequisites: scripts 22-23 (CrmConnections table + OAuth).
# Idempotent: update-function-code / put-role-policy / ensure_route overwrite
# or skip.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"
CRM_CONNECTIONS_TABLE="${CRM_CONNECTIONS_TABLE:-CrmConnections}"
SALESFORCE_API_VERSION="${SALESFORCE_API_VERSION:-v62.0}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

API_ID="$(aws apigatewayv2 get-apis \
            --query "Items[?Name=='${API_NAME}'].ApiId | [0]" \
            --output text)"
if [[ "$API_ID" == "None" || -z "$API_ID" ]]; then
  echo "ERROR: HTTP API '$API_NAME' not found — run 03_create_apigateway.sh first." >&2
  exit 1
fi
echo ">> API: $API_NAME ($API_ID)"

# -------------------------------------------------------------
# 1. userApi role: add UpdateItem on CrmConnections (the config lives on the
#    existing connection row). Rewrites the SAME policy name script 23 set,
#    so the KMS + secret statements must be repeated here or they'd be lost.
# -------------------------------------------------------------
USERAPI_ROLE="$(aws lambda get-function --function-name "$USERAPI_LAMBDA_NAME" \
                  --query 'Configuration.Role' --output text)"
USERAPI_ROLE="${USERAPI_ROLE##*/}"

KMS_KEY_ID="$(aws lambda get-function-configuration \
                --function-name "$USERAPI_LAMBDA_NAME" \
                --query 'Environment.Variables.SALESFORCE_KMS_KEY_ID' --output text)"
SECRET_ARN="$(aws lambda get-function-configuration \
                --function-name "$USERAPI_LAMBDA_NAME" \
                --query 'Environment.Variables.SALESFORCE_CLIENT_SECRET_ARN' --output text)"
if [[ "$KMS_KEY_ID" == "None" || -z "$KMS_KEY_ID" ]]; then
  echo "ERROR: SALESFORCE_KMS_KEY_ID not set on $USERAPI_LAMBDA_NAME —" >&2
  echo "       run scripts/23_deploy_salesforce_oauth.sh first." >&2
  exit 1
fi
echo ">> userApi role: $USERAPI_ROLE (kms key: $KMS_KEY_ID)"

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
      "Resource": "arn:aws:kms:${AWS_REGION}:${ACCOUNT_ID}:key/${KMS_KEY_ID}" },
    { "Sid": "SalesforceClientSecret", "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "${SECRET_ARN}" }
  ]
}
EOF
aws iam put-role-policy \
  --role-name "$USERAPI_ROLE" \
  --policy-name "userApi-crm-salesforce" \
  --policy-document "file://$(winpath "$POLICY_FILE")"
echo ">> userApi-crm-salesforce policy updated (+ dynamodb:UpdateItem)."

# -------------------------------------------------------------
# 2. Env: pin the API version, then deploy.
# -------------------------------------------------------------
CURRENT_ENV="$(aws lambda get-function-configuration \
                 --function-name "$USERAPI_LAMBDA_NAME" \
                 --query 'Environment.Variables' --output json)"
MERGED_ENV="$(python - "$CURRENT_ENV" "$SALESFORCE_API_VERSION" <<'PY'
import json, sys
env_json, api_version = sys.argv[1:3]
env = json.loads(env_json) if env_json not in ("null", "") else {}
env["SALESFORCE_API_VERSION"] = api_version
print(json.dumps({"Variables": env}))
PY
)"
aws lambda update-function-configuration \
  --function-name "$USERAPI_LAMBDA_NAME" \
  --environment "$MERGED_ENV" \
  --query "[FunctionName,Environment.Variables.SALESFORCE_API_VERSION]" --output text
aws lambda wait function-updated-v2 --function-name "$USERAPI_LAMBDA_NAME"

# Shared modules vendored FLAT — userApi imports ai_schema/groq_client/prompts
# at module scope, so a zip without them fails on cold start (see script 23).
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
# 3. Wire the four routes.
# -------------------------------------------------------------
USERAPI_INT="$(aws apigatewayv2 get-integrations --api-id "$API_ID" \
    --query "Items[?contains(IntegrationUri, '${USERAPI_LAMBDA_NAME}')].IntegrationId | [0]" \
    --output text)"
if [[ "$USERAPI_INT" == "None" || -z "$USERAPI_INT" ]]; then
  echo "ERROR: no API integration for $USERAPI_LAMBDA_NAME — run 17 first." >&2
  exit 1
fi

# See script 20 for why the head -1 | tr -d '\r' normalization is load-bearing.
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

ensure_route "$USERAPI_INT" "GET /crm/salesforce/objects"
ensure_route "$USERAPI_INT" "GET /crm/salesforce/fields/{object_name}"
ensure_route "$USERAPI_INT" "GET /crm/salesforce/config"
ensure_route "$USERAPI_INT" "PUT /crm/salesforce/config"
ensure_route "$USERAPI_INT" "POST /crm/salesforce/lookup"
# The recording key comes LAST: API Gateway only allows a greedy {key+} in the
# final position (same constraint as the AI Workspace routes).
ensure_route "$USERAPI_INT" "POST /crm/salesforce/sync/{key+}"

# -------------------------------------------------------------
# 4. transcribeRecording: read-only access to the CRM config, so the AI
#    extraction knows WHAT to look for (a site visit number, a lead email,
#    ...). Without this the pipeline simply extracts nothing and the app's
#    manual-entry path is the only route — a degradation, not a break.
# -------------------------------------------------------------
TR_LAMBDA_NAME="${TRANSCRIBE_LAMBDA_NAME:-transcribeRecording}"
TR_ROLE="$(aws lambda get-function --function-name "$TR_LAMBDA_NAME" \
             --query 'Configuration.Role' --output text)"
TR_ROLE="${TR_ROLE##*/}"
TR_POLICY_FILE="$(mktemp)"
cat > "$TR_POLICY_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "CrmConnectionsRead", "Effect": "Allow",
      "Action": "dynamodb:GetItem",
      "Resource": "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${CRM_CONNECTIONS_TABLE}" }
  ]
}
EOF
aws iam put-role-policy \
  --role-name "$TR_ROLE" \
  --policy-name "transcribe-crm-config-read" \
  --policy-document "file://$(winpath "$TR_POLICY_FILE")"
rm -f "$TR_POLICY_FILE"
echo ">> $TR_ROLE granted read-only GetItem on $CRM_CONNECTIONS_TABLE."

TR_ENV="$(aws lambda get-function-configuration --function-name "$TR_LAMBDA_NAME" \
            --query 'Environment.Variables' --output json)"
TR_MERGED="$(python - "$TR_ENV" "$CRM_CONNECTIONS_TABLE" <<'PY'
import json, sys
env_json, crm_table = sys.argv[1:3]
env = json.loads(env_json) if env_json not in ("null", "") else {}
env["CRM_CONNECTIONS_TABLE"] = crm_table
print(json.dumps({"Variables": env}))
PY
)"
aws lambda update-function-configuration \
  --function-name "$TR_LAMBDA_NAME" \
  --environment "$TR_MERGED" \
  --query "[FunctionName,Environment.Variables.CRM_CONNECTIONS_TABLE]" --output text
aws lambda wait function-updated-v2 --function-name "$TR_LAMBDA_NAME"
deploy_py_with_shared "$TR_LAMBDA_NAME" "functions/transcribe"

echo
echo ">> Done. Verify with:"
echo "     python tests/test_salesforce_oauth.py"
echo ">> Then, with a REAL connected org (needs a browser login):"
echo "     GET \$API_URL/crm/salesforce/objects   -> your org's objects"
echo "   The app's Settings -> Salesforce -> Salesforce mapping screen drives"
echo "   the whole flow; nothing here hardcodes an object or field name."
