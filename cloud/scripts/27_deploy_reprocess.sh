#!/usr/bin/env bash
# =============================================================
# 27_deploy_reprocess.sh — user-facing retry for the whole pipeline.
#
# Ships ONE new route on userApi:
#
#   POST /recordings/ai/reprocess/{key+}   -> 202 {status, started_at}
#
# WHAT IT IS FOR
# --------------
# Until now a recording that failed transcription, or stalled mid-pipeline, was
# a DEAD END in the app: the screen explained what went wrong and offered
# nothing to do about it. The only recovery was an operator running
# scripts/26_reprocess_stuck.py from a laptop. This route gives that same
# recovery to the user who is actually looking at the broken recording.
#
# It reuses script 26's approach exactly: re-invoke transcribeRecording with the
# SAME synthetic S3 event the real trigger sends, so a replay goes down an
# identical code path — no "reprocess" branch inside the pipeline that could
# drift from production behaviour. The audio is never deleted on failure, so a
# replay is always possible.
#
# WHY THE INVOKE IS ASYNC
# -----------------------
# The pipeline runs for minutes (ElevenLabs STT + up to 3 Groq calls), far past
# API Gateway's ~30s integration ceiling. A synchronous invoke would guarantee a
# gateway timeout on a run that is actually succeeding. So the route fires an
# InvocationType=Event and returns 202; the app polls `status` exactly as it
# already does after a first-time upload.
#
# COSTS REAL MONEY, so two guards ship with it:
#   * the app confirms before calling ("Process this recording again?");
#   * the backend refuses a repeat inside REPROCESS_COOLDOWN_SECONDS with a
#     429, stamped on the row itself (reprocess_started_at) so the limit holds
#     across cold starts and every container.
# The stamp is written BEFORE the invoke: if the response is lost in flight the
# cooldown is still recorded, so a client retry cannot double-charge.
#
# NEW IAM: userApi gains lambda:InvokeFunction on transcribeRecording ONLY.
# NEW env (optional):
#   REPROCESS_COOLDOWN_SECONDS=300   how long before a repeat is allowed
#   TRANSCRIBE_LAMBDA_NAME=transcribeRecording
#
# Prerequisites: script 21 (AI workspace routes on userApi).
# Idempotent: update-function-code / put-role-policy / ensure_route overwrite
# or skip.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"
TRANSCRIBE_LAMBDA_NAME="${TRANSCRIBE_LAMBDA_NAME:-transcribeRecording}"
REPROCESS_COOLDOWN_SECONDS="${REPROCESS_COOLDOWN_SECONDS:-300}"
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
# 1. IAM: let userApi invoke transcribeRecording — and nothing else.
#    Scoped to the ONE function ARN rather than "*": this is the only
#    cross-Lambda call userApi makes, and a wildcard here would let a future
#    bug in userApi invoke anything in the account.
# -------------------------------------------------------------
USERAPI_ROLE="$(aws lambda get-function --function-name "$USERAPI_LAMBDA_NAME" \
                  --query 'Configuration.Role' --output text)"
USERAPI_ROLE="${USERAPI_ROLE##*/}"
echo ">> userApi role: $USERAPI_ROLE"

TR_ARN="$(aws lambda get-function --function-name "$TRANSCRIBE_LAMBDA_NAME" \
            --query 'Configuration.FunctionArn' --output text)"
if [[ "$TR_ARN" == "None" || -z "$TR_ARN" ]]; then
  echo "ERROR: $TRANSCRIBE_LAMBDA_NAME not found." >&2
  exit 1
fi
echo ">> transcribe lambda: $TR_ARN"

POLICY_FILE="$(mktemp)"
trap 'rm -f "$POLICY_FILE"' EXIT
cat > "$POLICY_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "InvokeTranscribeForReprocess", "Effect": "Allow",
      "Action": "lambda:InvokeFunction",
      "Resource": ["${TR_ARN}", "${TR_ARN}:*"] }
  ]
}
EOF
aws iam put-role-policy \
  --role-name "$USERAPI_ROLE" \
  --policy-name "userApi-invoke-transcribe" \
  --policy-document "file://$(winpath "$POLICY_FILE")"
echo ">> userApi-invoke-transcribe policy attached."

# -------------------------------------------------------------
# 2. Env: the cooldown and the target function name.
# -------------------------------------------------------------
CURRENT_ENV="$(aws lambda get-function-configuration \
                 --function-name "$USERAPI_LAMBDA_NAME" \
                 --query 'Environment.Variables' --output json)"
MERGED_ENV="$(python - "$CURRENT_ENV" "$REPROCESS_COOLDOWN_SECONDS" "$TRANSCRIBE_LAMBDA_NAME" <<'PY'
import json, sys
env_json, cooldown, tr_name = sys.argv[1:4]
env = json.loads(env_json) if env_json not in ("null", "") else {}
env["REPROCESS_COOLDOWN_SECONDS"] = cooldown
env["TRANSCRIBE_LAMBDA_NAME"] = tr_name
print(json.dumps({"Variables": env}))
PY
)"
aws lambda update-function-configuration \
  --function-name "$USERAPI_LAMBDA_NAME" \
  --environment "$MERGED_ENV" \
  --query "[FunctionName,Environment.Variables.REPROCESS_COOLDOWN_SECONDS]" --output text
aws lambda wait function-updated-v2 --function-name "$USERAPI_LAMBDA_NAME"

# -------------------------------------------------------------
# 3. Deploy the code. Shared modules vendored FLAT — userApi imports
#    ai_schema/groq_client/prompts at module scope, so a zip without them
#    fails on cold start (same packaging as scripts 23/25).
# -------------------------------------------------------------
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
# 4. Wire the route.
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

# The recording key is greedy and therefore LAST — API Gateway allows {key+}
# only in the final position, which is why the action is a literal prefix.
ensure_route "$USERAPI_INT" "POST /recordings/ai/reprocess/{key+}"

echo
echo ">> Done. Verify with:"
echo "     python -m pytest tests/test_ai_workspace.py -k Reprocess"
echo ">> Then in the app: open a failed or stalled meeting and tap Try again."
echo "   A repeat within ${REPROCESS_COOLDOWN_SECONDS}s is refused with 429 by design."
