#!/usr/bin/env bash
# =============================================================
# 17_wire_pairing_routes.sh — API Gateway routes for the pairing
# and device endpoints (user-owned architecture).
#
# Adds to the existing HTTP API ($API_NAME):
#   userApi Lambda ($USERAPI_LAMBDA_NAME, default userApi):
#     POST   /devices/pair-request
#     POST   /devices/pair
#     GET    /devices/{device_id}
#     DELETE /devices/{device_id}
#   getUploadUrl Lambda ($LAMBDA_NAME):
#     POST   /device/heartbeat
#     POST   /device/upload-complete
#
# Reuses each Lambda's existing AWS_PROXY integration when one is
# already attached to the API (the userApi routes like GET /devices
# were wired outside this repo); creates one otherwise. Re-adds the
# lambda:InvokeFunction permission if missing.
#
# Idempotent: existing routes/integrations are reused, not duplicated.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

API_ID="$(aws apigatewayv2 get-apis \
            --query "Items[?Name=='${API_NAME}'].ApiId | [0]" \
            --output text)"
if [[ "$API_ID" == "None" || -z "$API_ID" ]]; then
  echo "ERROR: HTTP API '$API_NAME' not found — run 03_create_apigateway.sh first." >&2
  exit 1
fi
echo ">> API: $API_NAME ($API_ID)"

# find_or_create_integration <lambda-name> -> integration id on stdout
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
  # Invoke permission (idempotent: swallow "already exists").
  aws lambda add-permission \
    --function-name "$fn" \
    --statement-id "apigw-${API_ID}-invoke" \
    --action lambda:InvokeFunction \
    --principal apigateway.amazonaws.com \
    --source-arn "arn:aws:execute-api:${AWS_REGION}:${ACCOUNT_ID}:${API_ID}/*" \
    >/dev/null 2>&1 || true
  printf '%s' "$integration_id"
}

# ensure_route <integration-id> <route-key>
#
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
ensure_route "$USERAPI_INT" "POST /devices/pair-request"
ensure_route "$USERAPI_INT" "POST /devices/pair"
ensure_route "$USERAPI_INT" "GET /devices/{device_id}"
ensure_route "$USERAPI_INT" "DELETE /devices/{device_id}"

UPLOAD_INT="$(find_or_create_integration "$LAMBDA_NAME")"
ensure_route "$UPLOAD_INT" "POST /device/heartbeat"
ensure_route "$UPLOAD_INT" "POST /device/upload-complete"

echo ">> Done. Remember the Lambda env vars:"
echo "   userApi:      DEVICES_TABLE, PAIRED_USER_INDEX, USER_INDEX, PAIRING_CODE_TTL"
echo "   getUploadUrl: DEVICES_TABLE, RECORDINGS_TABLE"
