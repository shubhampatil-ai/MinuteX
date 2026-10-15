#!/usr/bin/env bash
# =============================================================
# 86_deploy_google_sign_in.sh — "Continue with Google" (login + signup).
#
#   POST /auth/google  {id_token}  (NO JWT)
#
# The app gets a Google ID token natively and posts it here; userApi verifies
# it with Google's tokeninfo endpoint and answers with a MinuteX JWT.
#
# NO NEW TABLE, NO NEW IAM. Identity claims are "google#{sub}" rows in Users
# (same pattern as signup's email claims); verification is outbound HTTPS,
# which userApi already makes (Gmail).
#
# CLIENT IDS. The token's `aud` must be one of GOOGLE_SIGNIN_CLIENT_IDS. When
# that is unset the Lambda falls back to GOOGLE_CLIENT_ID — the web client
# script 40 already configured for Gmail, which is also the webClientId the
# app should use. Set GOOGLE_SIGNIN_CLIENT_IDS only to accept a different or
# additional client (comma-separated).
#
# GOOGLE CLOUD CONSOLE (one-time, same project as GOOGLE_CLIENT_ID):
#   * Android OAuth client: package com.hp.airecorder + the SHA-1 of EVERY
#     signing key you ship (EAS: `eas credentials`, and Play App Signing's key
#     if the app goes through Play). A missing SHA-1 = DEVELOPER_ERROR.
#   * iOS OAuth client: bundle id com.hp.airecorder.
#   * OAuth consent screen published (or test users added) — scopes are only
#     openid/email/profile, which need no Google verification.
#
# Idempotent. Run:  bash scripts/86_deploy_google_sign_in.sh
#                   SKIP_TESTS=1 bash scripts/86_deploy_google_sign_in.sh
# =============================================================
set -euo pipefail
cd "$(dirname "$0")"
. ./aws.sh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

USERAPI_FN="${USERAPI_FN:-userApi}"

# -------------------------------------------------------------
# 1. Preflight
# -------------------------------------------------------------
echo "==> Preflight"
HANDLER="$PROJECT_ROOT/functions/userapi/lambda_function.py"
grep -q "^def google_sign_in(event)" "$HANDLER" \
  || { echo "FAIL: google_sign_in() missing from lambda_function.py" >&2; exit 1; }
grep -q '"/auth/google"' "$HANDLER" \
  || { echo "FAIL: /auth/google not registered in _ROUTES" >&2; exit 1; }

CURRENT_ENV="$(aws lambda get-function-configuration --function-name "$USERAPI_FN" \
               --query 'Environment.Variables' --output json)"
LIVE_IDS="$(python -c 'import json,sys; e=json.loads(sys.argv[1] or "{}") or {}; print(e.get("GOOGLE_SIGNIN_CLIENT_IDS") or e.get("GOOGLE_CLIENT_ID") or "")' "$CURRENT_ENV")"
if [[ -z "${GOOGLE_SIGNIN_CLIENT_IDS:-}" && -z "$LIVE_IDS" ]]; then
  echo "FAIL: $USERAPI_FN has neither GOOGLE_SIGNIN_CLIENT_IDS nor GOOGLE_CLIENT_ID." >&2
  echo "      Set GOOGLE_SIGNIN_CLIENT_IDS=<web client id> and re-run." >&2
  exit 1
fi
echo ">> Accepted audience: ${GOOGLE_SIGNIN_CLIENT_IDS:-$LIVE_IDS}"

if [[ "${SKIP_TESTS:-0}" != "1" ]]; then
  for suite in test_google_sign_in test_password_reset test_workspace_foundation; do
    ( cd "$PROJECT_ROOT" && python -m pytest "tests/${suite}.py" -q ) \
      || { echo "FAIL: tests/${suite}.py — not deploying." >&2; exit 1; }
  done
fi

# -------------------------------------------------------------
# 2. Code
# -------------------------------------------------------------
echo "==> Packaging userApi"
python "$SCRIPT_DIR/_package_lambda.py" functions/userapi "$PROJECT_ROOT"
aws lambda update-function-code --function-name "$USERAPI_FN" \
  --zip-file "fileb://$(winpath "$PROJECT_ROOT/functions/userapi/function.zip")" \
  --query "[FunctionName,CodeSize]" --output text
aws lambda wait function-updated-v2 --function-name "$USERAPI_FN"

# -------------------------------------------------------------
# 3. Route
# -------------------------------------------------------------
echo "==> Route"
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
[[ -n "$INTEGRATION_ID" && "$INTEGRATION_ID" != "None" ]] || { echo "FAIL: no userApi integration" >&2; exit 1; }
existing="$(aws apigatewayv2 get-routes --api-id "$API_ID" \
    --query "Items[?RouteKey=='POST /auth/google'].RouteId | [0]" --output json | tr -d '"\r')"
if [[ -n "$existing" && "$existing" != "null" ]]; then
  echo ">> Route exists: POST /auth/google"
else
  aws apigatewayv2 create-route --api-id "$API_ID" --route-key "POST /auth/google" \
    --target "integrations/$INTEGRATION_ID" >/dev/null
  echo ">> Route created: POST /auth/google"
fi

# -------------------------------------------------------------
# 4. Env — only when an explicit audience list was given (merged).
# -------------------------------------------------------------
if [[ -n "${GOOGLE_SIGNIN_CLIENT_IDS:-}" ]]; then
  ENV_JSON="$(python "$SCRIPT_DIR/_merge_env.py" "$CURRENT_ENV" \
              "GOOGLE_SIGNIN_CLIENT_IDS=$GOOGLE_SIGNIN_CLIENT_IDS")"
  aws lambda update-function-configuration --function-name "$USERAPI_FN" \
    --environment "$ENV_JSON" >/dev/null
  aws lambda wait function-updated-v2 --function-name "$USERAPI_FN"
  echo ">> GOOGLE_SIGNIN_CLIENT_IDS set."
else
  echo ">> Environment untouched (falls back to GOOGLE_CLIENT_ID)."
fi

# -------------------------------------------------------------
# 5. Probe — an empty body must reach OUR validation (400).
# -------------------------------------------------------------
API_ENDPOINT="$(aws apigatewayv2 get-api --api-id "$API_ID" \
    --query 'ApiEndpoint' --output text | tr -d '\r')"
status="$(curl -s -o /dev/null -w '%{http_code}' -X POST \
    -H 'Content-Type: application/json' -d '{}' \
    "${API_ENDPOINT}/auth/google" || echo 000)"
if [[ "$status" == "400" ]]; then
  echo ">> Verified POST /auth/google -> 400 from the Lambda."
else
  echo "ERROR: POST /auth/google returned $status — check the route/logs." >&2
  exit 1
fi

echo
echo "============================================================="
echo " Continue with Google deployed:  POST /auth/google"
echo " App build needs EXPO_PUBLIC_GOOGLE_WEB_CLIENT_ID (+ _IOS_ for iOS),"
echo " then a NATIVE rebuild (eas build) — the module is native code."
echo "============================================================="
