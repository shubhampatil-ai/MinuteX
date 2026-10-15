#!/usr/bin/env bash
# =============================================================
# 76_deploy_push_notifications.sh — "Brief ready" OS push via Expo.
#
# Wires the PUSH channel into the existing notification engine:
#   * userApi             POST /me/push-tokens, DELETE /me/push-tokens/{id},
#                         and the opportunistic Expo receipt check
#   * transcribeRecording sends ONE push when a meeting reaches `complete`
#                         (alongside the in-app AI_OUTPUT_READY row), claimed
#                         once in NotificationDedupe so retries stay silent
#
# Prerequisites:
#   41 (Notifications + NotificationDedupe), 42 (the in-app engine), 75 (this
#   feature's PushTokens table).
#
# Optional secret, from .env (never committed):
#   EXPO_ACCESS_TOKEN  only if the Expo project has "Enhanced Security for
#                      Push Notifications" turned on. Leave unset otherwise —
#                      Expo rejects an access token it does not expect.
#
# The Lambdas need no FCM or APNs credentials: those live in EAS (see
# docs/PUSH_NOTIFICATIONS.md), and Expo Push Service uses them on our behalf.
#
# Idempotent: re-running re-applies the policy, env and code, and skips
# routes that already exist.
# =============================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"
TRANSCRIBE_LAMBDA_NAME="${TRANSCRIBE_LAMBDA_NAME:-transcribeRecording}"
PUSH_TOKENS_TABLE="${PUSH_TOKENS_TABLE:-PushTokens}"
EXPO_ACCESS_TOKEN="${EXPO_ACCESS_TOKEN:-}"
: "${API_NAME:?API_NAME not set - put the HTTP API name in .env}"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
API_ID="$(aws apigatewayv2 get-apis \
            --query "Items[?Name=='${API_NAME}'].ApiId | [0]" --output text)"
if [[ "$API_ID" == "None" || -z "$API_ID" ]]; then
  echo "ERROR: HTTP API '$API_NAME' not found." >&2
  exit 1
fi
echo ">> API:   $API_NAME ($API_ID)"
echo ">> Table: $PUSH_TOKENS_TABLE"
echo ">> Expo access token: $([[ -n "$EXPO_ACCESS_TOKEN" ]] && echo set || echo 'not set (fine unless enhanced security is on)')"

echo ">> Running the push + notification tests (offline) ..."
( cd "$PROJECT_ROOT" && python -m pytest tests/test_push_notifications.py tests/test_notifications.py -q )
echo ">> Tests passed."

if ! aws dynamodb describe-table --table-name "$PUSH_TOKENS_TABLE" >/dev/null 2>&1; then
  echo "ERROR: table '$PUSH_TOKENS_TABLE' not found — run 75_create_push_tokens_table.sh first." >&2
  exit 1
fi

PUSH_ARN="arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${PUSH_TOKENS_TABLE}"
POLICY_FILE="$(mktemp)"
trap 'rm -f "$POLICY_FILE"' EXIT

grant() {
  local fn="$1" actions="$2" role
  cat > "$POLICY_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "PushTokens", "Effect": "Allow",
      "Action": [${actions}],
      "Resource": ["${PUSH_ARN}", "${PUSH_ARN}/index/*"] }
  ]
}
EOF
  role="$(aws lambda get-function --function-name "$fn" \
            --query 'Configuration.Role' --output text)"
  role="${role##*/}"
  aws iam put-role-policy --role-name "$role" --policy-name "lambda-push-tokens" \
    --policy-document "file://$(winpath "$POLICY_FILE")"
  echo ">> $fn ($role): lambda-push-tokens policy set."
}
# userApi registers, prunes rotated tokens, deletes on sign-out, and marks
# receipt failures. transcribeRecording only reads a user's tokens and marks
# dead ones — least privilege: no Put/Delete.
grant "$USERAPI_LAMBDA_NAME" '"dynamodb:GetItem","dynamodb:PutItem","dynamodb:DeleteItem","dynamodb:UpdateItem","dynamodb:Query"'
grant "$TRANSCRIBE_LAMBDA_NAME" '"dynamodb:Query","dynamodb:UpdateItem"'
# (The push CLAIM goes in NotificationDedupe, which script 42 already grants
# PutItem on for transcribeRecording.)

set_env() {
  local fn="$1" current merged
  current="$(aws lambda get-function-configuration --function-name "$fn" \
               --query 'Environment.Variables' --output json)"
  merged="$(python - "$current" "$PUSH_TOKENS_TABLE" "$EXPO_ACCESS_TOKEN" <<'PY'
import json, sys
env_json, table, expo = sys.argv[1:4]
env = json.loads(env_json) if env_json not in ("null", "") else {}
env["PUSH_TOKENS_TABLE"] = table
if expo:
    env["EXPO_ACCESS_TOKEN"] = expo
else:
    env.pop("EXPO_ACCESS_TOKEN", None)
print(json.dumps({"Variables": env}))
PY
)"
  aws lambda update-function-configuration --function-name "$fn" \
    --environment "$merged" \
    --query "[FunctionName,Environment.Variables.PUSH_TOKENS_TABLE]" --output text
  aws lambda wait function-updated-v2 --function-name "$fn"
}
set_env "$USERAPI_LAMBDA_NAME"
set_env "$TRANSCRIBE_LAMBDA_NAME"

PACKAGE_PY="$SCRIPT_DIR/_package_lambda.py"
deploy_py_with_shared() {
  local fn="$1" src_dir="$2" zip
  zip="$PROJECT_ROOT/$src_dir/function.zip"
  rm -f "$zip"
  ( cd "$PROJECT_ROOT" && python "$PACKAGE_PY" "$src_dir" "$PROJECT_ROOT" )
  aws lambda update-function-code --function-name "$fn" \
    --zip-file "fileb://$(winpath "$zip")" --publish \
    --query "[FunctionName,CodeSize,LastModified]" --output text
  aws lambda wait function-updated-v2 --function-name "$fn"
  echo ">> $fn deployed from $src_dir/ + shared/"
}
deploy_py_with_shared "$USERAPI_LAMBDA_NAME" "functions/userapi"
deploy_py_with_shared "$TRANSCRIBE_LAMBDA_NAME" "functions/transcribe"

USERAPI_INT="$(aws apigatewayv2 get-integrations --api-id "$API_ID" \
    --query "Items[?contains(IntegrationUri, '${USERAPI_LAMBDA_NAME}')].IntegrationId | [0]" \
    --output text)"
if [[ "$USERAPI_INT" == "None" || -z "$USERAPI_INT" ]]; then
  echo "ERROR: no API integration for $USERAPI_LAMBDA_NAME." >&2
  exit 1
fi

ensure_route() {
  local route_key="$1" existing out
  existing="$(aws apigatewayv2 get-routes --api-id "$API_ID" \
      --query "Items[?RouteKey=='${route_key}'].RouteId | [0]" --output text \
      | head -1 | tr -d '\r')"
  if [[ -n "$existing" && "$existing" != "None" ]]; then
    echo ">> Route exists: $route_key"
    return 0
  fi
  if out="$(aws apigatewayv2 create-route --api-id "$API_ID" \
              --route-key "$route_key" --target "integrations/$USERAPI_INT" 2>&1)"; then
    echo ">> Route created: $route_key"
  elif grep -q "already exists" <<<"$out"; then
    echo ">> Route exists: $route_key"
  else
    echo "ERROR: could not create route $route_key" >&2
    echo "$out" >&2
    return 1
  fi
}
ensure_route "POST /me/push-tokens"
ensure_route "DELETE /me/push-tokens/{installation_id}"

API_ENDPOINT="$(aws apigatewayv2 get-api --api-id "$API_ID" \
    --query 'ApiEndpoint' --output text | tr -d '\r')"
PROBE="$(curl -s -o /dev/null -w '%{http_code}' -X POST \
    "${API_ENDPOINT}/me/push-tokens" || echo "000")"
case "$PROBE" in
  401) echo ">> Probe OK: POST /me/push-tokens -> 401 (route live, auth enforced)." ;;
  000) echo ">> Probe inconclusive (curl unavailable or no network)." ;;
  *)   echo "WARNING: POST /me/push-tokens answered $PROBE, expected 401." >&2 ;;
esac

echo ""
echo ">> Done. The backend will send 'Brief ready' pushes to registered devices."
echo "   Devices register only from an app build that includes expo-notifications"
echo "   (a NEW EAS build) — see docs/PUSH_NOTIFICATIONS.md."
