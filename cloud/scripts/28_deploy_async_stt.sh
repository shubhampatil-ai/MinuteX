#!/usr/bin/env bash
# =============================================================
# 28_deploy_async_stt.sh — asynchronous ElevenLabs STT + the unified Groq call.
#
# WHAT THIS CHANGES
# -----------------
# 1. transcribeRecording gains a second entry point and stops waiting for
#    transcripts. It now QUEUES an ElevenLabs job (webhook=true) and exits in a
#    couple of seconds, whatever the recording's length.
# 2. userApi gains POST /webhooks/elevenlabs/stt — the (unauthenticated,
#    HMAC-verified) endpoint ElevenLabs calls when a transcription finishes —
#    plus POST /recordings/ai/stt-reconcile/{key+} to recover a job whose
#    webhook never arrived.
# 3. The analysis becomes ONE Groq call instead of three.
# 4. Upload ceilings rise to the provider's own limits: 3 GB / 10 hours.
#
# WHY: the old flow held a 290s socket open inside a 300s Lambda for the whole
# transcription. A long recording died on that timeout — and a Lambda timeout is
# not a Python exception, so no handler ran and the row was stranded at
# status="transcribing" forever, after ElevenLabs had already been paid. Async
# delivery removes the socket from the critical path entirely.
#
# THE WEBHOOK SECRET
# ------------------
# ElevenLabs shows a webhook's signing secret ONCE, at creation. This script
# creates the webhook via POST /v1/workspace/webhooks, captures that secret, and
# puts it straight into AWS Secrets Manager — it is never written to a file,
# never a Lambda env var, never logged. Only the ARN reaches the function.
#
# Re-running is safe: an existing webhook pointing at the same URL is reused
# (its secret cannot be re-read, so the stored one stays authoritative).
#
# ORDER MATTERS. The webhook is registered LAST, after the route exists and the
# code that serves it is live. Registering first would point ElevenLabs at a
# route that 404s, and those deliveries are lost — not retried forever.
#
# Prerequisites: scripts 12-27.
# Idempotent throughout.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"
TRANSCRIBE_LAMBDA_NAME="${TRANSCRIBE_LAMBDA_NAME:-transcribeRecording}"
USERAPI_ROLE="${USERAPI_ROLE:-userApi-aps1}"
WEBHOOK_SECRET_NAME="${WEBHOOK_SECRET_NAME:-userApi/elevenlabsWebhookSecret}"
WEBHOOK_ROUTE="POST /webhooks/elevenlabs/stt"
RECONCILE_ROUTE="POST /recordings/ai/stt-reconcile/{key+}"

: "${BUCKET_NAME:?BUCKET_NAME not set (put it in .env)}"
: "${ELEVENLABS_API_KEY:?ELEVENLABS_API_KEY not set — needed to register the webhook}"

# GROQ_TPM_LIMIT is required for the same reason script 21 requires it: silently
# defaulting to the free-tier number would downgrade the live value of an
# upgraded plan on the next deploy.
#
# NOTE: no apostrophes inside the ${VAR:?message} expansions below. bash parses
# quotes there, so a word like "account's" reads as an unterminated single quote
# and the error surfaces ~130 lines later at an unrelated parenthesis.
: "${GROQ_TPM_LIMIT:?GROQ_TPM_LIMIT not set. Export the current tokens-per-minute limit for the account before deploying.}"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
API_ID="$(aws apigatewayv2 get-apis \
            --query "Items[?Name=='${API_NAME}'].ApiId | [0]" --output text)"
if [[ "$API_ID" == "None" || -z "$API_ID" ]]; then
  echo "ERROR: HTTP API '$API_NAME' not found - run 03_create_apigateway.sh first." >&2
  exit 1
fi
API_ENDPOINT="$(aws apigatewayv2 get-api --api-id "$API_ID" \
                  --query ApiEndpoint --output text)"
WEBHOOK_URL="${API_ENDPOINT}/webhooks/elevenlabs/stt"

echo ">> API:      $API_NAME ($API_ID)"
echo ">> Webhook:  $WEBHOOK_URL"
echo

# -------------------------------------------------------------
# 0. Tests BEFORE anything is shipped. Both suites, because the unified
#    analysis touches the shared modules test_ai_workspace.py covers.
# -------------------------------------------------------------
echo ">> Verifying the shared AI core imports (flat, as in the zip)..."
cd "$PROJECT_ROOT/shared"
python "$SCRIPT_DIR/_preflight_check.py"
cd "$PROJECT_ROOT"

echo ">> Running the offline unit tests..."
python tests/test_ai_workspace.py 2>&1 | tail -2
python tests/test_async_stt.py 2>&1 | tail -2

# -------------------------------------------------------------
# Packaging + env helpers (same approach as script 21: shared modules go in
# FLAT at the archive root, because that is how `import ai_schema` resolves
# inside the Lambda runtime).
# -------------------------------------------------------------
PACKAGE_PY="$SCRIPT_DIR/_package_lambda.py"

deploy_py_with_shared() {
  local fn="$1" src_dir="$2"
  local zip="$PROJECT_ROOT/$src_dir/function.zip"
  rm -f "$zip"
  python "$PACKAGE_PY" "$src_dir" "$PROJECT_ROOT"
  aws lambda update-function-code \
    --function-name "$fn" \
    --zip-file "fileb://$(winpath "$zip")" \
    --publish \
    --query "[FunctionName,CodeSize,LastModified]" --output text
  aws lambda wait function-updated-v2 --function-name "$fn"
  echo ">> $fn deployed from $src_dir/ + shared/"
}

# The env-merge helper is a separate .py file rather than an inline heredoc.
# Script 21 inlines it via command substitution wrapping a heredoc, which the
# machine mis-parses — the unterminated-heredoc warning surfaces as a syntax
# error tens of lines later, at whatever the next parenthesis happens to be.
MERGE_ENV_PY="$SCRIPT_DIR/_merge_env.py"

merge_env() {
  local fn="$1"; shift
  local current merged
  current="$(aws lambda get-function-configuration \
               --function-name "$fn" \
               --query 'Environment.Variables' --output json)"
  merged="$(python "$MERGE_ENV_PY" "$current" "$@")"
  aws lambda update-function-configuration \
    --function-name "$fn" --environment "$merged" \
    --query "FunctionName" --output text >/dev/null
  aws lambda wait function-updated-v2 --function-name "$fn"
  echo ">> $fn env merged: $*" | sed -E 's/(SECRET|KEY)=[^ ]*/\1=***/g'
}

# ensure_route — same CRLF/"None" normalization as script 21. `aws --output
# text` here returns the value plus a stray "None" line, in either order, so
# filter by CONTENT rather than trusting line position (a naive check silently
# skipped creating three routes on script 21's first run).
ensure_route() {
  local integration_id="$1" route_key="$2" existing
  existing="$(aws apigatewayv2 get-routes --api-id "$API_ID" \
      --query "Items[?RouteKey=='${route_key}'].RouteId | [0]" --output text \
      | tr -d '\r' | grep -v '^None$' | grep -v '^$' | head -1 || true)"
  if [[ -n "$existing" ]]; then
    echo ">> Route exists: $route_key ($existing)"
    return 0
  fi
  aws apigatewayv2 create-route --api-id "$API_ID" \
    --route-key "$route_key" --target "integrations/$integration_id" >/dev/null
  echo ">> Route created: $route_key"
}

# -------------------------------------------------------------
# 1. IAM. Two genuinely new permissions for userApi:
#      s3:PutObject on transcripts/*  — the webhook stores the transcript there.
#        The existing upload-presign policy only covers recordings/*, so without
#        this every delivery would fail to persist (verified against the live
#        policy before writing this).
#      secretsmanager:GetSecretValue on the webhook secret.
#    lambda:InvokeFunction on transcribeRecording already exists (the reprocess
#    route), and the webhook's async hand-off reuses exactly that.
# -------------------------------------------------------------
echo
echo ">> === IAM: transcript writes + webhook secret ==="
SECRET_ARN="$(aws secretsmanager describe-secret --secret-id "$WEBHOOK_SECRET_NAME" \
                --query ARN --output text 2>/dev/null || true)"
if [[ -z "$SECRET_ARN" || "$SECRET_ARN" == "None" ]]; then
  # Placeholder now; the real secret is written in step 5 once ElevenLabs mints
  # it. Created up front so the IAM policy can reference a stable ARN.
  SECRET_ARN="$(aws secretsmanager create-secret \
                  --name "$WEBHOOK_SECRET_NAME" \
                  --description "ElevenLabs STT webhook HMAC signing secret" \
                  --secret-string "PENDING" \
                  --query ARN --output text)"
  echo ">> Secret created: $WEBHOOK_SECRET_NAME"
else
  echo ">> Secret exists:  $WEBHOOK_SECRET_NAME"
fi

# Built by a helper script and written to a temp file. Every inline
# inline python-heredoc in this file was replaced the same way: the bash build
# on this machine mis-parses a heredoc nested inside command substitution and
# reports the failure at an unrelated line much further down.
POLICY_FILE="$(mktemp)"
trap 'rm -f "$POLICY_FILE"' EXIT
python "$SCRIPT_DIR/_stt_webhook_policy.py" "$BUCKET_NAME" "$SECRET_ARN" \
  > "$POLICY_FILE"
aws iam put-role-policy --role-name "$USERAPI_ROLE" \
  --policy-name userApi-stt-webhook \
  --policy-document "file://$(winpath "$POLICY_FILE")"
echo ">> IAM policy userApi-stt-webhook applied to $USERAPI_ROLE"
echo "   (IAM is eventually consistent - a first invocation right now may 403)"

# -------------------------------------------------------------
# 2. transcribeRecording. The timeout can come DOWN: the queue call is a few
#    seconds and the analysis half is bounded by GROQ_DEADLINE_SECONDS. It is
#    left at its current value rather than lowered here, because the analysis
#    entry point still needs room for a map-reduce over a very long transcript.
# -------------------------------------------------------------
echo
echo ">> === $TRANSCRIBE_LAMBDA_NAME (async STT start + unified analysis) ==="
merge_env "$TRANSCRIBE_LAMBDA_NAME" \
  "GROQ_MODEL=${GROQ_MODEL:-openai/gpt-oss-120b}" \
  "GROQ_TPM_LIMIT=$GROQ_TPM_LIMIT" \
  "GROQ_CONTEXT_TOKENS=${GROQ_CONTEXT_TOKENS:-131072}" \
  "STT_URL_EXPIRY=${STT_URL_EXPIRY:-21600}" \
  "STT_START_TIMEOUT=${STT_START_TIMEOUT:-30}"
deploy_py_with_shared "$TRANSCRIBE_LAMBDA_NAME" "functions/transcribe"

# -------------------------------------------------------------
# 3. userApi: env + code, THEN the routes (a route pointing at a build without
#    the handler would 404 for as long as the gap lasts).
# -------------------------------------------------------------
echo
echo ">> === $USERAPI_LAMBDA_NAME (STT webhook + reconcile + new limits) ==="
merge_env "$USERAPI_LAMBDA_NAME" \
  "ELEVENLABS_WEBHOOK_SECRET_ARN=$SECRET_ARN" \
  "ELEVENLABS_API_KEY=$ELEVENLABS_API_KEY" \
  "MAX_UPLOAD_BYTES=${MAX_UPLOAD_BYTES:-3000000000}" \
  "MAX_DURATION_SECONDS=${MAX_DURATION_SECONDS:-36000}" \
  "GROQ_TPM_LIMIT=$GROQ_TPM_LIMIT" \
  "GROQ_CONTEXT_TOKENS=${GROQ_CONTEXT_TOKENS:-131072}"
deploy_py_with_shared "$USERAPI_LAMBDA_NAME" "functions/userapi"

USERAPI_INT="$(aws apigatewayv2 get-integrations --api-id "$API_ID" \
    --query "Items[?contains(IntegrationUri, '${USERAPI_LAMBDA_NAME}')].IntegrationId | [0]" \
    --output text)"
if [[ "$USERAPI_INT" == "None" || -z "$USERAPI_INT" ]]; then
  echo "ERROR: no API integration for $USERAPI_LAMBDA_NAME - run 17 first." >&2
  exit 1
fi

echo
echo ">> Wiring routes..."
ensure_route "$USERAPI_INT" "$WEBHOOK_ROUTE"
ensure_route "$USERAPI_INT" "$RECONCILE_ROUTE"

# Fail loudly if a route we just wired isn't actually there — a silently
# missing route is the one failure that looks like success in the log.
ACTUAL="$(aws apigatewayv2 get-routes --api-id "$API_ID" \
    --query 'Items[].RouteKey' --output text | tr '\t' '\n' | tr -d '\r')"
for rk in "$WEBHOOK_ROUTE" "$RECONCILE_ROUTE"; do
  grep -Fxq "$rk" <<<"$ACTUAL" || { echo "ERROR: route missing: $rk" >&2; exit 1; }
done
echo ">> Verified: both routes are present on the API."

# API Gateway needs explicit permission to invoke the Lambda for a new route.
# The existing statement is usually a wildcard on the API, but adding an
# explicit one is idempotent-safe and cheap insurance.
aws lambda add-permission \
  --function-name "$USERAPI_LAMBDA_NAME" \
  --statement-id apigw-stt-webhook \
  --action lambda:InvokeFunction \
  --principal apigateway.amazonaws.com \
  --source-arn "arn:aws:execute-api:${AWS_REGION}:${ACCOUNT_ID}:${API_ID}/*/*" \
  >/dev/null 2>&1 && echo ">> Lambda invoke permission added for API Gateway" \
  || echo ">> Lambda invoke permission already present"

# -------------------------------------------------------------
# 4. Smoke-test the live endpoint BEFORE telling ElevenLabs about it.
#    An unsigned POST must be rejected — that single check proves the route is
#    wired, the code is live, and the signature gate is closed. If this passed
#    with a 200 we would be registering an OPEN endpoint.
# -------------------------------------------------------------
echo
echo ">> === Pre-registration check: unsigned POST must be rejected ==="
CODE="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$WEBHOOK_URL" \
          -H 'Content-Type: application/json' -d '{"type":"x"}')"
if [[ "$CODE" == "401" ]]; then
  echo ">> OK: unsigned request rejected with 401"
elif [[ "$CODE" == "404" ]]; then
  echo "ERROR: route 404s - not registering a webhook at a dead URL." >&2
  exit 1
else
  echo "ERROR: unsigned request returned $CODE (expected 401). Refusing to" >&2
  echo "       register the webhook: an open endpoint would accept forged" >&2
  echo "       transcripts." >&2
  exit 1
fi

# -------------------------------------------------------------
# 5. Register the webhook with ElevenLabs and store its secret.
#
# The secret is returned ONLY here, at creation. It goes straight into Secrets
# Manager — never to disk, never to a Lambda env var, never to the log.
# -------------------------------------------------------------
echo
echo ">> === ElevenLabs webhook registration ==="
python - "$WEBHOOK_URL" "$WEBHOOK_SECRET_NAME" "$AWS_REGION" <<'PY'
import json, os, subprocess, sys, urllib.error, urllib.request

webhook_url, secret_name, region = sys.argv[1], sys.argv[2], sys.argv[3]
api_key = os.environ["ELEVENLABS_API_KEY"]
BASE = "https://api.elevenlabs.io/v1/workspace/webhooks"


def call(url, method="GET", payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"xi-api-key": api_key, "Content-Type": "application/json",
                 "User-Agent": "minutex-deploy/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


status, existing = call(BASE)
if status != 200 or not isinstance(existing, dict):
    sys.exit(f"ERROR: could not list ElevenLabs webhooks ({status}): "
             f"{str(existing)[:300]}")

for hook in existing.get("webhooks") or []:
    if (hook.get("webhook_url") or "") == webhook_url:
        # Reuse. The secret cannot be re-read, so whatever is already in
        # Secrets Manager stays authoritative — overwriting it with a new
        # webhook's secret would invalidate the live one.
        print(f"   webhook already registered: {hook.get('webhook_id')}")
        print("   NOTE: its signing secret cannot be re-read from the API.")
        print("   If verification fails, delete it in the ElevenLabs dashboard")
        print("   and re-run this script to mint a fresh pair.")
        sys.exit(0)

status, created = call(BASE, "POST", {
    "settings": {"auth_type": "hmac",
                 "name": "MinuteX STT completion",
                 "webhook_url": webhook_url}})
if status != 200 or not isinstance(created, dict):
    sys.exit(f"ERROR: webhook creation failed ({status}): {str(created)[:300]}")

webhook_id = created.get("webhook_id")
secret = created.get("webhook_secret")
print(f"   created webhook {webhook_id}")
if not secret:
    sys.exit("ERROR: ElevenLabs returned no webhook_secret. Signature "
             "verification cannot work — set it manually from the dashboard "
             f"into the secret '{secret_name}'.")

# Straight into Secrets Manager. Not echoed, not written to a file.
subprocess.run(
    ["aws", "secretsmanager", "put-secret-value",
     "--secret-id", secret_name, "--secret-string", secret,
     "--region", region, "--query", "Name", "--output", "text"],
    check=True, stdout=subprocess.DEVNULL)
print(f"   signing secret stored in Secrets Manager ({secret_name})")
print(f"   webhook_id={webhook_id}  (set ELEVENLABS_WEBHOOK_ID to pin it)")
PY

# The Lambda caches the secret per container, so a container started before the
# secret was written would hold "PENDING". Force fresh containers.
echo
echo ">> Rolling userApi containers so the new secret is picked up..."
merge_env "$USERAPI_LAMBDA_NAME" "STT_WEBHOOK_DEPLOYED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# -------------------------------------------------------------
# 6. Report.
# -------------------------------------------------------------
echo
echo ">> Done."
echo
echo "   Old STT flow:  S3 -> transcribeRecording -> [290s socket] -> Groq x3 -> DDB"
echo "   New STT flow:  S3 -> transcribeRecording -> queue job -> exit (~2s)"
echo "                  ElevenLabs -> POST $WEBHOOK_ROUTE -> verify -> S3"
echo "                             -> async invoke -> ONE Groq call -> DDB"
echo
echo "   Limits:        2 GB / 4 h  ->  3 GB / 10 h (the provider's own)"
echo "   Groq calls:    3 per meeting -> 1"
echo
echo "   Verify (offline):  python tests/test_async_stt.py"
echo "   Recover a stuck recording:"
echo "     curl -X POST -H \"Authorization: Bearer \$JWT\" \\"
echo "       \"\$API_URL/recordings/ai/stt-reconcile/\$KEY\""
echo
echo "   FRONTEND: app/lib/uploads.tsx now advertises 3 GB / 10 h."
echo "   That ships in the next EAS build - deploy it separately."
