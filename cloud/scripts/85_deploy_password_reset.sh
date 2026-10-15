#!/usr/bin/env bash
# =============================================================
# 85_deploy_password_reset.sh — self-serve "Forgot password" by emailed code.
#
#   POST /password/forgot  {email}                      (NO JWT)
#   POST /password/reset   {email, code, new_password}  (NO JWT)
#
# Email goes out through Amazon SES v2 from PASSWORD_RESET_FROM_EMAIL. No new
# table: the transient code lives on the Users row (reset_code_hash etc.).
#
# PREREQUISITES (one-time, done in the SES console or CLI — NOT by this script,
# because both involve DNS records or an AWS support request):
#   1. Verify the sender identity in SES, in SES_REGION. Prefer verifying the
#      DOMAIN (exceller.tech) with DKIM over a single address — mail from an
#      unauthenticated domain lands in spam.
#   2. Request SES production access. In the sandbox SES delivers ONLY to
#      verified recipient addresses, so real users would never get the code.
#      This script warns (does not fail) while the account is in the sandbox,
#      so the feature can be tested with verified addresses first.
#
# WHAT IT DOES
#   1. Preflight: handlers + routes in source; SES identity verified; the
#      offline suite passes.
#   2. Inline IAM policy "userApi-password-reset" — ses:SendEmail, restricted
#      to the one From address via the ses:FromAddress condition key.
#   3. Code (flat packaging, shared/*.py vendored).
#   4. Routes.
#   5. Env, MERGED (every other variable — GROQ_MODEL included — untouched).
#   6. Probe: an empty reset reaches the Lambda (400 from our validation).
#
# TEST MODE (no email yet):
#   PASSWORD_RESET_DEV_EMAILS="*@minutex-test.com" bash scripts/85_deploy_password_reset.sh
#   For those addresses the code is RETURNED by /password/forgot and shown in
#   the app. Turn off: PASSWORD_RESET_DEV_EMAILS= bash scripts/85_...
#
# ROLLBACK: PASSWORD_RESET_FROM_EMAIL= bash 85_deploy_password_reset.sh
#   removes the variable; both routes then answer 503
#   password_reset_unavailable and the app says reset is unavailable.
#
# Run:  PASSWORD_RESET_FROM_EMAIL=no-reply@exceller.tech bash scripts/85_deploy_password_reset.sh
#       (or put PASSWORD_RESET_FROM_EMAIL in .env)   SKIP_TESTS=1 to skip pytest
# =============================================================
set -euo pipefail
cd "$(dirname "$0")"
. ./aws.sh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

USERAPI_FN="${USERAPI_FN:-userApi}"
SES_REGION="${SES_REGION:-$AWS_REGION}"
FROM="${PASSWORD_RESET_FROM_EMAIL-__unset__}"
# TEST MODE without email: PASSWORD_RESET_DEV_EMAILS="*@yourtest.domain,me@x.com"
# makes /password/forgot RETURN the code for those addresses (no SES needed).
# Set it EMPTY to switch test mode off. List test accounts only.
DEV_EMAILS="${PASSWORD_RESET_DEV_EMAILS-__unset__}"
if [[ "$FROM" == "__unset__" && "$DEV_EMAILS" == "__unset__" ]]; then
  echo "ERROR: set PASSWORD_RESET_FROM_EMAIL (e.g. no-reply@exceller.tech)," >&2
  echo "       and/or PASSWORD_RESET_DEV_EMAILS for test mode." >&2
  echo "       Set a variable EMPTY to remove it (rollback)." >&2
  exit 1
fi
[[ "$FROM" == "__unset__" ]] && FROM="__keep__"
if [[ "$DEV_EMAILS" != "__unset__" && -n "$DEV_EMAILS" ]]; then
  if grep -Eq '(^|,)[[:space:]]*\*?[[:space:]]*(,|$)|@\*' <<<"$DEV_EMAILS"; then
    echo "ERROR: PASSWORD_RESET_DEV_EMAILS must list addresses or *@domain only." >&2
    exit 1
  fi
  echo "WARN: TEST MODE for: $DEV_EMAILS"
  echo "      Anyone can reset these accounts via the API. Unset before launch."
fi

# -------------------------------------------------------------
# 1. Preflight
# -------------------------------------------------------------
echo "==> Preflight"
HANDLER="$PROJECT_ROOT/functions/userapi/lambda_function.py"
for fn in forgot_password reset_password; do
  grep -q "^def ${fn}(event)" "$HANDLER" \
    || { echo "FAIL: ${fn}() missing from lambda_function.py" >&2; exit 1; }
done
for route in '"/password/forgot"' '"/password/reset"'; do
  grep -q "$route" "$HANDLER" \
    || { echo "FAIL: route $route not registered in _ROUTES" >&2; exit 1; }
done

if [[ -n "$FROM" && "$FROM" != "__keep__" ]]; then
  DOMAIN="${FROM##*@}"
  # A verified DOMAIN covers every address on it; otherwise the exact address.
  verified=""
  for identity in "$DOMAIN" "$FROM"; do
    status="$("$AWS_BIN" --region "$SES_REGION" sesv2 get-email-identity \
        --email-identity "$identity" --query 'VerifiedForSendingStatus' \
        --output text 2>/dev/null | tr -d '\r' || true)"
    if [[ "$status" == "True" ]]; then verified="$identity"; break; fi
  done
  if [[ -z "$verified" ]]; then
    echo "FAIL: neither $DOMAIN nor $FROM is a verified SES identity in $SES_REGION." >&2
    echo "      aws sesv2 create-email-identity --email-identity $DOMAIN --region $SES_REGION" >&2
    echo "      then add the DKIM CNAME records it prints to DNS." >&2
    exit 1
  fi
  echo ">> SES identity verified: $verified ($SES_REGION)"

  prod="$("$AWS_BIN" --region "$SES_REGION" sesv2 get-account \
      --query 'ProductionAccessEnabled' --output text 2>/dev/null | tr -d '\r' || true)"
  if [[ "$prod" != "True" ]]; then
    echo "WARN: SES account in $SES_REGION is in the SANDBOX — codes reach only"
    echo "      VERIFIED recipient addresses. Request production access before launch."
  else
    echo ">> SES production access enabled."
  fi
fi

if [[ "${SKIP_TESTS:-0}" != "1" ]]; then
  ( cd "$PROJECT_ROOT" && python -m pytest tests/test_password_reset.py -q ) \
    || { echo "FAIL: tests/test_password_reset.py — not deploying." >&2; exit 1; }
fi

# -------------------------------------------------------------
# 2. IAM — send only, only as the configured sender.
# -------------------------------------------------------------
ROLE="$(aws lambda get-function --function-name "$USERAPI_FN" \
          --query 'Configuration.Role' --output text | tr -d '\r')"
ROLE="${ROLE##*/}"
if [[ -n "$FROM" && "$FROM" != "__keep__" ]]; then
  POLICY_FILE="$(mktemp)"
  trap 'rm -f "$POLICY_FILE"' EXIT
  cat > "$POLICY_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "PasswordResetEmail", "Effect": "Allow",
      "Action": ["ses:SendEmail"],
      "Resource": "*",
      "Condition": { "StringEquals": { "ses:FromAddress": "${FROM}" } } }
  ]
}
EOF
  aws iam put-role-policy --role-name "$ROLE" --policy-name "userApi-password-reset" \
    --policy-document "file://$(winpath "$POLICY_FILE")"
  echo ">> $ROLE: userApi-password-reset policy set."
fi

# -------------------------------------------------------------
# 3. Code
# -------------------------------------------------------------
echo "==> Packaging userApi"
python "$SCRIPT_DIR/_package_lambda.py" functions/userapi "$PROJECT_ROOT"
aws lambda update-function-code --function-name "$USERAPI_FN" \
  --zip-file "fileb://$(winpath "$PROJECT_ROOT/functions/userapi/function.zip")" \
  --query "[FunctionName,CodeSize]" --output text
aws lambda wait function-updated-v2 --function-name "$USERAPI_FN"

# -------------------------------------------------------------
# 4. Routes
# -------------------------------------------------------------
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
[[ -n "$INTEGRATION_ID" && "$INTEGRATION_ID" != "None" ]] || { echo "FAIL: no userApi integration" >&2; exit 1; }
ensure_route() {
  local route_key="$1" existing
  existing="$(aws apigatewayv2 get-routes --api-id "$API_ID" \
      --query "Items[?RouteKey=='${route_key}'].RouteId | [0]" --output json | tr -d '"\r')"
  if [[ -n "$existing" && "$existing" != "null" ]]; then
    echo ">> Route exists: $route_key"; return 0
  fi
  aws apigatewayv2 create-route --api-id "$API_ID" --route-key "$route_key" \
    --target "integrations/$INTEGRATION_ID" >/dev/null
  echo ">> Route created: $route_key"
}
ensure_route "POST /password/forgot"
ensure_route "POST /password/reset"

# -------------------------------------------------------------
# 5. Env (merged)
# -------------------------------------------------------------
echo "==> Environment"
ENV_ARGS=()
if [[ "$FROM" == "__keep__" ]]; then
  :  # sender left as it is on the function
elif [[ -n "$FROM" ]]; then
  ENV_ARGS+=("PASSWORD_RESET_FROM_EMAIL=$FROM")
  [[ "$SES_REGION" != "$AWS_REGION" ]] && ENV_ARGS+=("SES_REGION=$SES_REGION")
else
  ENV_ARGS+=("-PASSWORD_RESET_FROM_EMAIL")
fi
if [[ "$DEV_EMAILS" != "__unset__" ]]; then
  if [[ -n "$DEV_EMAILS" ]]; then
    ENV_ARGS+=("PASSWORD_RESET_DEV_EMAILS=$DEV_EMAILS")
  else
    ENV_ARGS+=("-PASSWORD_RESET_DEV_EMAILS")
  fi
fi
CURRENT_ENV="$(aws lambda get-function-configuration --function-name "$USERAPI_FN" \
               --query 'Environment.Variables' --output json)"
ENV_JSON="$(python "$SCRIPT_DIR/_merge_env.py" "$CURRENT_ENV" "${ENV_ARGS[@]}")"
aws lambda update-function-configuration --function-name "$USERAPI_FN" \
  --environment "$ENV_JSON" >/dev/null
aws lambda wait function-updated-v2 --function-name "$USERAPI_FN"
echo ">> $USERAPI_FN environment updated."

# -------------------------------------------------------------
# 6. Probe — an empty reset must reach OUR validation.
# -------------------------------------------------------------
API_ENDPOINT="$(aws apigatewayv2 get-api --api-id "$API_ID" \
    --query 'ApiEndpoint' --output text | tr -d '\r')"
status="$(curl -s -o /dev/null -w '%{http_code}' -X POST \
    -H 'Content-Type: application/json' -d '{}' \
    "${API_ENDPOINT}/password/reset" || echo 000)"
case "$status" in
  400) echo ">> Verified POST /password/reset -> 400 from the Lambda." ;;
  503) echo ">> POST /password/reset -> 503 (feature disabled, as configured)." ;;
  *)   echo "ERROR: POST /password/reset returned $status — check routes/logs." >&2; exit 1 ;;
esac

echo
echo "============================================================="
echo " Password reset deployed (sender: ${FROM:-<disabled>}; test mode: ${DEV_EMAILS/__unset__/unchanged})."
echo "   POST /password/forgot   POST /password/reset"
echo "============================================================="
