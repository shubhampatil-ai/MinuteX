#!/usr/bin/env bash
# =============================================================
# 18_wire_upload_routes.sh — recording sources (MOBILE / UPLOAD).
#
# Wires everything the multi-source upload service needs, after the
# updated Lambda code is deployed (02/12 + userApi deploy):
#
#   1. API Gateway routes -> userApi Lambda:
#        POST /recordings/upload-request    (JWT presign + timeline stub)
#        POST /recordings/upload-complete   (status -> "uploaded")
#   2. IAM: userApi's role gains s3:PutObject on recordings/* — the
#      presigned PUT it hands the app is signed with the ROLE's identity,
#      so S3 honors the URL only if the role itself may PutObject.
#   3. S3 trigger: replaces the .wav-only ObjectCreated notification with
#      a single UNFILTERED rule so every supported audio format enters
#      the SAME transcription pipeline as device .wav's. (S3 has no
#      OR-of-suffixes filter and the accepted-format list is long; the
#      transcribe Lambda already skips non-audio keys by extension, and
#      it never writes to S3, so an unfiltered rule cannot loop.)
#   4. Reminder of the new (optional) userApi env vars.
#
# Idempotent: existing routes/integrations/permissions are reused.
#
# WARNING (same as 13): put-bucket-notification-configuration REPLACES
# the whole notification config. This script writes ONLY our rule;
# merge any future non-audio rules into the JSON.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"
TR_LAMBDA_NAME="${TRANSCRIBE_LAMBDA_NAME:-transcribeRecording}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

# -------------------------------------------------------------
# 1. API Gateway routes (same helpers as 17_wire_pairing_routes.sh)
# -------------------------------------------------------------
API_ID="$(aws apigatewayv2 get-apis \
            --query "Items[?Name=='${API_NAME}'].ApiId | [0]" \
            --output text)"
if [[ "$API_ID" == "None" || -z "$API_ID" ]]; then
  echo "ERROR: HTTP API '$API_NAME' not found — run 03_create_apigateway.sh first." >&2
  exit 1
fi
echo ">> API: $API_NAME ($API_ID)"

find_or_create_integration() {
  local fn="$1"
  local arn integration_id
  arn="$(aws lambda get-function --function-name "$fn" \
           --query 'Configuration.FunctionArn' --output text)"
  integration_id="$(aws apigatewayv2 get-integrations --api-id "$API_ID" \
      --query "Items[?contains(IntegrationUri, '${fn}')].IntegrationId | [0]" \
      --output text)"
  if [[ "$integration_id" == "None" || -z "$integration_id" ]]; then
    integration_id="$(aws apigatewayv2 create-integration \
        --api-id "$API_ID" \
        --integration-type AWS_PROXY \
        --integration-uri "$arn" \
        --payload-format-version 2.0 \
        --query IntegrationId --output text)"
    echo ">> Created integration $integration_id -> $fn" >&2
  else
    echo ">> Reusing integration $integration_id -> $fn" >&2
  fi
  aws lambda add-permission \
    --function-name "$fn" \
    --statement-id "apigw-${API_ID}-invoke" \
    --action lambda:InvokeFunction \
    --principal apigateway.amazonaws.com \
    --source-arn "arn:aws:execute-api:${AWS_REGION}:${ACCOUNT_ID}:${API_ID}/*" \
    >/dev/null 2>&1 || true
  printf '%s' "$integration_id"
}

# The `head -1 | tr -d '\r'` is load-bearing, not defensive noise: on this
# setup `aws --output text` returns the value followed by a SECOND line
# containing "None", with CRLF endings — e.g. an EXISTING route yields
# "abc123\r\nNone" and a MISSING one yields "None\r\nNone". Without the
# normalization, `[[ "$existing" != "None" ]]` reads "None\r\nNone" as truthy
# and silently skips creating the route (found while wiring the AI Workspace
# routes in script 21 — three routes were reported "exists" but were absent).
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

USERAPI_INT="$(find_or_create_integration "$USERAPI_LAMBDA_NAME")"
ensure_route "$USERAPI_INT" "POST /recordings/upload-request"
ensure_route "$USERAPI_INT" "POST /recordings/upload-complete"

# -------------------------------------------------------------
# 2. IAM — userApi presigns S3 PUTs now, so its role needs PutObject
#    scoped to the recordings/ prefix. Inline policy, idempotent put.
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
    {
      "Sid": "PresignedUserUploads",
      "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::${BUCKET_NAME}/recordings/*"
    }
  ]
}
EOF
aws iam put-role-policy \
  --role-name "$USERAPI_ROLE" \
  --policy-name "userApi-upload-presign" \
  --policy-document "file://$(winpath "$POLICY_FILE")"
echo ">> Attached inline policy userApi-upload-presign (s3:PutObject recordings/*)."

# -------------------------------------------------------------
# 3. S3 trigger — one ObjectCreated rule per supported audio suffix,
#    all invoking the transcribe Lambda (replaces the .wav-only rule).
# -------------------------------------------------------------
LAMBDA_ARN="$(aws lambda get-function --function-name "$TR_LAMBDA_NAME" \
                --query 'Configuration.FunctionArn' --output text)"

STMT_ID="s3-invoke-transcribe"
if aws lambda add-permission \
     --function-name "$TR_LAMBDA_NAME" \
     --statement-id "$STMT_ID" \
     --action lambda:InvokeFunction \
     --principal s3.amazonaws.com \
     --source-arn "arn:aws:s3:::${BUCKET_NAME}" \
     --source-account "$ACCOUNT_ID" >/dev/null 2>&1; then
  echo ">> S3 invoke permission added."
else
  echo ">> S3 invoke permission already present (ok)."
fi

NOTIF_FILE="$(mktemp)"
cat > "$NOTIF_FILE" <<EOF
{
  "LambdaFunctionConfigurations": [
    {
      "Id": "transcribe-on-audio",
      "LambdaFunctionArn": "${LAMBDA_ARN}",
      "Events": ["s3:ObjectCreated:*"]
    }
  ]
}
EOF

echo ">> Putting bucket notification (ObjectCreated, unfiltered — Lambda skips non-audio) ..."
aws s3api put-bucket-notification-configuration \
  --bucket "$BUCKET_NAME" \
  --notification-configuration "file://$(winpath "$NOTIF_FILE")"
rm -f "$NOTIF_FILE"

# -------------------------------------------------------------
# 4. Env-var reminder
# -------------------------------------------------------------
echo ">> Done. Optional userApi env vars (defaults in parentheses):"
echo "   MAX_UPLOAD_BYTES (2147483648 = 2 GB)"
echo "   MAX_DURATION_SECONDS (14400 = 4 h)"
echo "   UPLOAD_URL_EXPIRY (900 s)"
echo ">> Required (already set for pairing): BUCKET_NAME, RECORDINGS_TABLE, USER_INDEX."
