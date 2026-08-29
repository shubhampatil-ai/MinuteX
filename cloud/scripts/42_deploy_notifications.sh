#!/usr/bin/env bash
# =============================================================
# 42_deploy_notifications.sh — the in-app notification engine (Phase 1).
#
# Deploys BOTH Lambdas, because both raise notifications:
#
#   userApi              the API (list / unread-count / read / read-all) AND
#                        the task, AI-review, document and sharing events.
#   transcribeRecording  the meeting-processing events — completion and
#                        failure are only KNOWN inside the pipeline, which is
#                        why the write path is shared rather than duplicated
#                        (see shared/notification_schema.py).
#
# Both zips already vendor shared/*.py flat, so notification_schema.py travels
# to each of them with no packaging change — that is precisely why the model
# lives there instead of inside one function.
#
# What this wires:
#   GET    /notifications                          (JWT)
#   GET    /notifications/unread-count             (JWT)
#   POST   /notifications/{notification_id}/read   (JWT)
#   POST   /notifications/read-all                 (JWT)
#
# Every one of them is JWT-only and scoped to the caller inside the handler
# (_require_auth + a user_id re-check on the row). Like every route in this
# API they carry AuthorizationType NONE at the gateway — this repo has never
# used a gateway authorizer; auth is enforced in the Lambda.
#
# IAM. Both roles need read/write on Notifications and PutItem on
# NotificationDedupe. The dedupe grant is deliberately PutItem-only: the claim
# table is written once per fact and never read back by the product (the
# conditional write IS the read), so nothing needs GetItem or Query on it, and
# a narrower grant is one less thing an unexpected code path can do.
#
# GMAIL IS NOT TOUCHED. No Gmail scope, secret, KMS key or route appears here,
# and the notification engine imports nothing from the Gmail section. Gmail
# stays exactly what script 40 deployed: a user-triggered communication
# integration. The day EMAIL becomes a delivery channel it will be a CONSUMER
# of the rows this deploys, reading the `channels` seam.
#
# Prerequisites:
#   - scripts/41_create_notifications_table.sh  (both tables)
#
# Idempotent: put-role-policy and update-function-code overwrite;
# ensure_route skips routes that already exist.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"
TRANSCRIBE_LAMBDA_NAME="${TRANSCRIBE_LAMBDA_NAME:-transcribeRecording}"
NOTIFICATIONS_TABLE="${NOTIFICATIONS_TABLE:-Notifications}"
NOTIFICATION_DEDUPE_TABLE="${NOTIFICATION_DEDUPE_TABLE:-NotificationDedupe}"
# Set in .env, like every other deploy script in this directory. Named
# explicitly here so a missing value fails with a sentence rather than with an
# empty JMESPath filter that matches nothing and reports "API '' not found".
: "${API_NAME:?API_NAME not set - put the HTTP API name in .env}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

API_ID="$(aws apigatewayv2 get-apis \
            --query "Items[?Name=='${API_NAME}'].ApiId | [0]" \
            --output text)"
if [[ "$API_ID" == "None" || -z "$API_ID" ]]; then
  echo "ERROR: HTTP API '$API_NAME' not found — run 03_create_apigateway.sh first." >&2
  exit 1
fi
echo ">> API:    $API_NAME ($API_ID)"
echo ">> Tables: $NOTIFICATIONS_TABLE, $NOTIFICATION_DEDUPE_TABLE"

# -------------------------------------------------------------
# 0. Offline tests before anything is provisioned.
#    Same gate scripts 38/40 apply: these need no AWS, so there is no reason
#    to discover a broken build after the deploy.
# -------------------------------------------------------------
echo ">> Running the notification test suite (offline) ..."
( cd "$PROJECT_ROOT" && python -m pytest tests/test_notifications.py -q )
echo ">> Tests passed."

# The tables must exist BEFORE the code that writes to them goes live —
# otherwise every notification silently fails open into the log.
for t in "$NOTIFICATIONS_TABLE" "$NOTIFICATION_DEDUPE_TABLE"; do
  if ! aws dynamodb describe-table --table-name "$t" >/dev/null 2>&1; then
    echo "ERROR: table '$t' not found — run 41_create_notifications_table.sh first." >&2
    exit 1
  fi
done
echo ">> Both tables present."

# -------------------------------------------------------------
# 1. IAM — the same grant for both roles.
# -------------------------------------------------------------
NOTIF_ARN="arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${NOTIFICATIONS_TABLE}"
DEDUPE_ARN="arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${NOTIFICATION_DEDUPE_TABLE}"

POLICY_FILE="$(mktemp)"
trap 'rm -f "$POLICY_FILE"' EXIT
cat > "$POLICY_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "NotificationsTable", "Effect": "Allow",
      "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
                 "dynamodb:Query"],
      "Resource": ["${NOTIF_ARN}", "${NOTIF_ARN}/index/*"] },
    { "Sid": "NotificationDedupeClaims", "Effect": "Allow",
      "Action": ["dynamodb:PutItem"],
      "Resource": "${DEDUPE_ARN}" }
  ]
}
EOF

grant_role() {
  local fn="$1" role
  role="$(aws lambda get-function --function-name "$fn" \
            --query 'Configuration.Role' --output text)"
  role="${role##*/}"
  aws iam put-role-policy \
    --role-name "$role" \
    --policy-name "lambda-notifications" \
    --policy-document "file://$(winpath "$POLICY_FILE")"
  echo ">> $fn ($role): lambda-notifications inline policy set."
}
grant_role "$USERAPI_LAMBDA_NAME"
grant_role "$TRANSCRIBE_LAMBDA_NAME"

# -------------------------------------------------------------
# 2. Environment — both functions name the same two tables.
# -------------------------------------------------------------
set_env() {
  local fn="$1" current merged
  current="$(aws lambda get-function-configuration --function-name "$fn" \
               --query 'Environment.Variables' --output json)"
  merged="$(python - "$current" "$NOTIFICATIONS_TABLE" \
              "$NOTIFICATION_DEDUPE_TABLE" <<'PY'
import json, sys
env_json, notif, dedupe = sys.argv[1:4]
env = json.loads(env_json) if env_json not in ("null", "") else {}
env.update({
    "NOTIFICATIONS_TABLE": notif,
    "NOTIFICATION_DEDUPE_TABLE": dedupe,
})
print(json.dumps({"Variables": env}))
PY
)"
  aws lambda update-function-configuration \
    --function-name "$fn" \
    --environment "$merged" \
    --query "[FunctionName,Environment.Variables.NOTIFICATIONS_TABLE]" --output text
  aws lambda wait function-updated-v2 --function-name "$fn"
}
set_env "$USERAPI_LAMBDA_NAME"
set_env "$TRANSCRIBE_LAMBDA_NAME"

# -------------------------------------------------------------
# 3. Deploy both functions, shared modules vendored FLAT.
#    A zip with only lambda_function.py fails on cold start and takes down
#    every route — same helper as scripts 21/23/38/40.
# -------------------------------------------------------------
PACKAGE_PY="$SCRIPT_DIR/_package_lambda.py"

deploy_py_with_shared() {
  local fn="$1" src_dir="$2"
  local zip="$PROJECT_ROOT/$src_dir/function.zip"
  rm -f "$zip"
  ( cd "$PROJECT_ROOT" && python "$PACKAGE_PY" "$src_dir" "$PROJECT_ROOT" )
  aws lambda update-function-code \
    --function-name "$fn" \
    --zip-file "fileb://$(winpath "$zip")" \
    --publish \
    --query "[FunctionName,CodeSize,LastModified]" --output text
  aws lambda wait function-updated-v2 --function-name "$fn"
  echo ">> $fn deployed from $src_dir/ + shared/"
}
deploy_py_with_shared "$USERAPI_LAMBDA_NAME" "functions/userapi"
deploy_py_with_shared "$TRANSCRIBE_LAMBDA_NAME" "functions/transcribe"

# -------------------------------------------------------------
# 4. Wire the routes.
# -------------------------------------------------------------
USERAPI_INT="$(aws apigatewayv2 get-integrations --api-id "$API_ID" \
    --query "Items[?contains(IntegrationUri, '${USERAPI_LAMBDA_NAME}')].IntegrationId | [0]" \
    --output text)"
if [[ "$USERAPI_INT" == "None" || -z "$USERAPI_INT" ]]; then
  echo "ERROR: no API integration for $USERAPI_LAMBDA_NAME — run 17 first." >&2
  exit 1
fi

# (see script 20/40 for why the head -1 | tr -d '\r' normalization and the
#  "already exists" tolerance are both load-bearing)
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

# The LITERAL "/notifications/unread-count" and "/notifications/read-all" sit
# alongside "/notifications/{notification_id}/read". No collision: API Gateway
# prefers a literal segment over a variable one, and the three have distinct
# shapes anyway (two segments vs three).
ensure_route "$USERAPI_INT" "GET /notifications"
ensure_route "$USERAPI_INT" "GET /notifications/unread-count"
ensure_route "$USERAPI_INT" "POST /notifications/{notification_id}/read"
ensure_route "$USERAPI_INT" "POST /notifications/read-all"

# -------------------------------------------------------------
# 5. Verify the route answers.
#
# An unauthenticated GET /notifications must be 401 — the handler ran and
# rejected it. A 403/404 means the route never reached the Lambda. That one
# check distinguishes "deployed" from "wired but dead", which is the failure
# this script exists to avoid announcing as success.
# -------------------------------------------------------------
API_ENDPOINT="$(aws apigatewayv2 get-api --api-id "$API_ID" \
    --query 'ApiEndpoint' --output text | tr -d '\r')"
PROBE_STATUS="$(curl -s -o /dev/null -w '%{http_code}' \
    "${API_ENDPOINT}/notifications" || echo "000")"
case "$PROBE_STATUS" in
  401) echo ">> Probe OK: GET /notifications -> 401 (route live, auth enforced)." ;;
  000) echo ">> Probe inconclusive (curl unavailable or no network)." ;;
  *)   echo "WARNING: GET /notifications answered $PROBE_STATUS, expected 401." >&2
       echo "         403/404 means the route is not reaching the Lambda." >&2 ;;
esac

echo ""
echo ">> Done. In-app notifications are live."
echo "   Delivery channel: IN_APP only. Gmail and WhatsApp are NOT wired to"
echo "   the notification engine — see the header of this script."
