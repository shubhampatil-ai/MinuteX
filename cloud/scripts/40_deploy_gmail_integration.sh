#!/usr/bin/env bash
# =============================================================
# 40_deploy_gmail_integration.sh — the Integrations layer + Gmail (Phase 1).
#
# Provisions everything userApi needs for the generic integration connect flow
# and the Gmail send capability, then deploys the routes:
#
#   1. A KMS key (alias alias/minutex-integrations) to envelope-encrypt each
#      user's refresh token before it is written to DynamoDB. Created only if
#      INTEGRATIONS_KMS_KEY_ID is not already set in .env.
#
#      SEPARATE from alias/crm-salesforce on purpose: a mail credential and a
#      CRM credential should not share a key, so rotating or disabling one
#      cannot silently break the other. The Lambda falls back to the Salesforce
#      key when this one is unset, which keeps an un-provisioned stack
#      encrypted rather than plaintext — but that is a safety net, not the
#      intended configuration.
#
#   2. A Secrets Manager secret holding the Google OAuth client secret
#      (GOOGLE_CLIENT_SECRET from .env — never a plaintext Lambda env var).
#
#   3. userApi role: extends the inline policy with Integrations table
#      read/write/delete, KMS Encrypt/Decrypt on the new key, and
#      GetSecretValue on the new secret.
#
#   4. Deploys functions/userapi/lambda_function.py (+ shared/) and wires:
#        GET    /integrations
#        GET    /integrations/{provider}
#        POST   /integrations/{provider}/connect
#        GET    /integrations/{provider}/callback   (no JWT — see below)
#        DELETE /integrations/{provider}
#        POST   /integrations/gmail/send
#        GET    /integrations/gmail/recipients/{key+}
#        POST   /integrations/gmail/send/meeting/{key+}
#        POST   /integrations/gmail/send/task/{task_id}
#
# /integrations/{provider}/callback needs no special gateway treatment: like
# every route in this API it has AuthorizationType NONE at the gateway level —
# this repo never uses an API Gateway authorizer, JWT auth is enforced inside
# each handler via _require_auth(). The callback is simply the route that skips
# that check and verifies the signed `state` instead, because Google's redirect
# is an unauthenticated browser GET.
#
# GOOGLE CLOUD PREREQUISITES (do these first — the deploy cannot):
#   1. Google Cloud Console -> APIs & Services -> Enable "Gmail API".
#   2. OAuth consent screen: External, add the two scopes below, and add your
#      test accounts while the app is unverified. NOTE: with a send scope the
#      app needs Google verification before it can serve users outside the
#      test list — start that early, it is not instant.
#   3. Credentials -> Create OAuth client ID -> Web application.
#      Authorized redirect URI MUST EXACTLY equal GOOGLE_REDIRECT_URI below.
#      A "Web application" client (not "Android"/"iOS") is correct here
#      BECAUSE the exchange happens on the server with a client secret — the
#      mobile app never sees a Google credential.
#   Scopes requested (the minimum for Phase 1 — see GMAIL_SCOPES in the
#   Lambda for why nothing more is asked for):
#      https://www.googleapis.com/auth/gmail.send
#      https://www.googleapis.com/auth/userinfo.email
#
# Prerequisites:
#   - scripts/39_create_integrations_table.sh (Integrations table)
#   - .env: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, INTEGRATION_RETURN_URL
#     (deep link back into the app, e.g. recorderapp://integrations-connected)
#
# Idempotent: create-key/create-secret are skipped if already provisioned;
# update-function-code and put-role-policy overwrite; ensure_route skips
# existing routes.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"
INTEGRATIONS_TABLE="${INTEGRATIONS_TABLE:-Integrations}"
: "${GOOGLE_CLIENT_ID:?GOOGLE_CLIENT_ID not set (put it in .env)}"
: "${GOOGLE_CLIENT_SECRET:?GOOGLE_CLIENT_SECRET not set (put it in .env)}"
: "${INTEGRATION_RETURN_URL:?INTEGRATION_RETURN_URL not set (deep link back into the app)}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

API_ID="$(aws apigatewayv2 get-apis \
            --query "Items[?Name=='${API_NAME}'].ApiId | [0]" \
            --output text)"
if [[ "$API_ID" == "None" || -z "$API_ID" ]]; then
  echo "ERROR: HTTP API '$API_NAME' not found — run 03_create_apigateway.sh first." >&2
  exit 1
fi
echo ">> API: $API_NAME ($API_ID)"

# One redirect URI for every Google-family provider — the {provider} segment
# distinguishes them, so Calendar/Tasks later need no new Console entry beyond
# their own path.
GOOGLE_REDIRECT_URI="${GOOGLE_REDIRECT_URI:-${API_URL%/}/integrations/gmail/callback}"
echo ">> Redirect URI (must match the Google OAuth client EXACTLY):"
echo "     $GOOGLE_REDIRECT_URI"

# -------------------------------------------------------------
# 0. Offline tests before anything is provisioned.
#    Same gate 38_deploy_meeting_share.sh applies: these tests need no AWS, so
#    there is no reason to discover a broken build after the deploy.
# -------------------------------------------------------------
echo ">> Running the integration test suite (offline) ..."
( cd "$PROJECT_ROOT" && python -m pytest tests/test_gmail_integration.py -q )
echo ">> Tests passed."

# -------------------------------------------------------------
# 1. KMS key for refresh-token envelope encryption.
# -------------------------------------------------------------
if [[ -n "${INTEGRATIONS_KMS_KEY_ID:-}" ]]; then
  echo ">> Using existing KMS key from .env: $INTEGRATIONS_KMS_KEY_ID"
else
  EXISTING_KEY="$(aws kms list-aliases \
      --query "Aliases[?AliasName=='alias/minutex-integrations'].TargetKeyId | [0]" \
      --output text 2>/dev/null || true)"
  if [[ -n "$EXISTING_KEY" && "$EXISTING_KEY" != "None" ]]; then
    INTEGRATIONS_KMS_KEY_ID="$EXISTING_KEY"
    echo ">> Found existing KMS key via alias/minutex-integrations: $INTEGRATIONS_KMS_KEY_ID"
  else
    INTEGRATIONS_KMS_KEY_ID="$(aws kms create-key \
        --description "Envelope-encrypts per-user integration OAuth refresh tokens (MinuteX)" \
        --query 'KeyMetadata.KeyId' --output text)"
    aws kms create-alias --alias-name "alias/minutex-integrations" \
        --target-key-id "$INTEGRATIONS_KMS_KEY_ID"
    echo ">> Created KMS key: $INTEGRATIONS_KMS_KEY_ID (alias/minutex-integrations)"
  fi
  echo ">> Add this to .env so future runs reuse it: INTEGRATIONS_KMS_KEY_ID=$INTEGRATIONS_KMS_KEY_ID"
fi
KMS_KEY_ARN="arn:aws:kms:${AWS_REGION}:${ACCOUNT_ID}:key/${INTEGRATIONS_KMS_KEY_ID}"

# -------------------------------------------------------------
# 2. Secrets Manager: the Google OAuth client secret.
# -------------------------------------------------------------
SECRET_NAME="userApi/googleClientSecret"
if aws secretsmanager describe-secret --secret-id "$SECRET_NAME" >/dev/null 2>&1; then
  aws secretsmanager put-secret-value --secret-id "$SECRET_NAME" \
      --secret-string "$GOOGLE_CLIENT_SECRET" >/dev/null
  echo ">> Updated existing secret: $SECRET_NAME"
else
  aws secretsmanager create-secret --name "$SECRET_NAME" \
      --description "Google OAuth client secret (MinuteX Gmail integration)" \
      --secret-string "$GOOGLE_CLIENT_SECRET" >/dev/null
  echo ">> Created secret: $SECRET_NAME"
fi
GOOGLE_CLIENT_SECRET_ARN="$(aws secretsmanager describe-secret \
    --secret-id "$SECRET_NAME" --query 'ARN' --output text)"

# -------------------------------------------------------------
# 3. userApi role: extend the inline policy.
#    Additive under its OWN policy name, exactly like script 23 — the other
#    scripts' policies stay in force because put-role-policy is per-name.
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
    { "Sid": "IntegrationsTable", "Effect": "Allow",
      "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
                 "dynamodb:DeleteItem", "dynamodb:Query"],
      "Resource": "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${INTEGRATIONS_TABLE}" },
    { "Sid": "IntegrationTokenKms", "Effect": "Allow",
      "Action": ["kms:Encrypt", "kms:Decrypt"],
      "Resource": "${KMS_KEY_ARN}" },
    { "Sid": "GoogleClientSecret", "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "${GOOGLE_CLIENT_SECRET_ARN}" }
  ]
}
EOF
aws iam put-role-policy \
  --role-name "$USERAPI_ROLE" \
  --policy-name "userApi-integrations-gmail" \
  --policy-document "file://$(winpath "$POLICY_FILE")"
echo ">> userApi-integrations-gmail inline policy set (Integrations, KMS, secret)."

# -------------------------------------------------------------
# 4. Deploy userApi with the new env vars merged in.
# -------------------------------------------------------------
CURRENT_ENV="$(aws lambda get-function-configuration \
                 --function-name "$USERAPI_LAMBDA_NAME" \
                 --query 'Environment.Variables' --output json)"
MERGED_ENV="$(python - "$CURRENT_ENV" "$INTEGRATIONS_TABLE" "$GOOGLE_CLIENT_ID" \
    "$GOOGLE_REDIRECT_URI" "$GOOGLE_CLIENT_SECRET_ARN" "$INTEGRATIONS_KMS_KEY_ID" \
    "$INTEGRATION_RETURN_URL" <<'PY'
import json, sys
(env_json, table, client_id, redirect_uri, secret_arn, kms_key_id,
 return_url) = sys.argv[1:8]
env = json.loads(env_json) if env_json not in ("null", "") else {}
env.update({
    "INTEGRATIONS_TABLE": table,
    "GOOGLE_CLIENT_ID": client_id,
    "GOOGLE_REDIRECT_URI": redirect_uri,
    "GOOGLE_CLIENT_SECRET_ARN": secret_arn,
    "INTEGRATIONS_KMS_KEY_ID": kms_key_id,
    "INTEGRATION_RETURN_URL": return_url,
})
print(json.dumps({"Variables": env}))
PY
)"
aws lambda update-function-configuration \
  --function-name "$USERAPI_LAMBDA_NAME" \
  --environment "$MERGED_ENV" \
  --query "[FunctionName,Environment.Variables.INTEGRATIONS_TABLE]" --output text
aws lambda wait function-updated-v2 --function-name "$USERAPI_LAMBDA_NAME"

# Package with the shared modules vendored in FLAT (that is how
# `import integrations` / `import email_message` resolve in the Lambda
# runtime). Same helper as scripts 21/23/38 — a zip with only
# lambda_function.py fails on cold start and takes down EVERY route.
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
# 5. Wire the routes.
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
  # Tolerate the route already existing. The lookup above is a best-effort
  # probe, not a guarantee: get-routes paginates, and its JMESPath filter has
  # to match route keys containing a greedy "{key+}" — a literal the query
  # language treats specially. A miss there is a FALSE negative, and on a
  # re-run the create then fails with ConflictException and, under `set -e`,
  # aborts the whole deploy AFTER the Lambda has already been updated. The
  # service's own answer is the authoritative one, so let it decide.
  local out
  if out="$(aws apigatewayv2 create-route \
              --api-id "$API_ID" \
              --route-key "$route_key" \
              --target "integrations/$integration_id" 2>&1)"; then
    echo ">> Route created: $route_key"
  elif grep -q "already exists" <<<"$out"; then
    echo ">> Route exists: $route_key"
  else
    echo "ERROR: could not create route $route_key" >&2
    echo "$out" >&2
    return 1
  fi
}

ensure_route "$USERAPI_INT" "GET /integrations"
ensure_route "$USERAPI_INT" "GET /integrations/{provider}"
ensure_route "$USERAPI_INT" "POST /integrations/{provider}/connect"
ensure_route "$USERAPI_INT" "GET /integrations/{provider}/callback"
ensure_route "$USERAPI_INT" "DELETE /integrations/{provider}"
# The Gmail routes are declared with a LITERAL "gmail" segment where the
# generic ones use {provider}. API Gateway prefers the literal, so
# "POST /integrations/gmail/send" never falls through to
# "POST /integrations/{provider}/connect" — different final segments anyway,
# but the precedence rule is what makes this safe in general.
ensure_route "$USERAPI_INT" "POST /integrations/gmail/send"
ensure_route "$USERAPI_INT" "GET /integrations/gmail/recipients/{key+}"
ensure_route "$USERAPI_INT" "POST /integrations/gmail/send/meeting/{key+}"
ensure_route "$USERAPI_INT" "POST /integrations/gmail/send/task/{task_id}"

# -------------------------------------------------------------
# 6. Verify the catalog route answers.
#
# Worth probing: an unauthenticated GET /integrations must be 401 (the handler
# ran and rejected it), NOT 403/404 (the route never reached the Lambda). That
# single check distinguishes "deployed" from "wired but dead", which is the
# failure this script exists to avoid announcing as success.
# -------------------------------------------------------------
API_ENDPOINT="$(aws apigatewayv2 get-api --api-id "$API_ID" \
    --query 'ApiEndpoint' --output text | tr -d '\r')"
PROBE_STATUS="$(curl -s -o /dev/null -w '%{http_code}' \
    "${API_ENDPOINT}/integrations" || echo "000")"
case "$PROBE_STATUS" in
  401) echo ">> Route verified: unauthenticated GET /integrations -> 401" ;;
  403|404|000)
    echo "ERROR: GET /integrations did not reach the Lambda (HTTP $PROBE_STATUS)." >&2
    exit 1 ;;
  200)
    echo "ERROR: GET /integrations returned 200 WITHOUT a token — auth is not running." >&2
    exit 1 ;;
  *)  echo "WARN: unexpected probe status $PROBE_STATUS — check CloudWatch." >&2 ;;
esac

echo
echo ">> Gmail integration deployed."
echo "   Google OAuth client redirect URI must be EXACTLY:"
echo "     $GOOGLE_REDIRECT_URI"
echo "   App deep link on completion: $INTEGRATION_RETURN_URL"
echo
echo ">> Manual verification (needs a real Google account — the code exchange"
echo "   cannot be automated):"
echo "     1. Settings -> Integrations -> Gmail -> Connect Gmail"
echo "     2. Approve; the card should read Connected with your address"
echo "     3. Open a meeting -> Share -> Email via Gmail -> pick recipients -> Send"
echo "     4. Settings -> Integrations -> Gmail -> Manage -> Disconnect"
echo "     5. The meeting's Gmail action must disappear again"
