#!/usr/bin/env bash
# =============================================================
# 43_deploy_eager_task_seeding.sh — seed AI tasks when processing finishes.
#
# THE CHANGE. AI task seeding used to happen only when somebody opened a task
# list. A meeting could finish, extract real action items, and have none of
# them exist anywhere the product could see — absent from the Task Tracker, no
# TASK_ASSIGNED for the assignee, no AI_ACTION_REQUIRED for the owner — until a
# human happened to browse to it. This makes the pipeline seed them itself.
#
# WHAT IT DEPLOYS.
#   transcribeRecording  calls userApi with a typed internal event
#                        ({"type":"tasks.seed"}) right after the terminal
#                        status write and just before the completion
#                        notification.
#   userApi              handles that event by running the EXISTING
#                        _seed_ai_tasks / _migrate_embedded_tasks it already
#                        owns. No task-creation logic was copied or rewritten.
#
# NO NEW TABLES, NO SCHEMA CHANGE. Both Lambdas already read and write every
# table involved (Tasks, Contacts, MeetingParticipants, Recordings,
# Notifications). This is a code + IAM change only.
#
# THE ONE NEW PERMISSION. transcribeRecording could not previously invoke
# anything, so it gets lambda:InvokeFunction scoped to EXACTLY the userApi
# function ARN — not a wildcard. That is the whole new privilege: it may ask
# userApi to seed a meeting, and nothing else. userApi's own permissions are
# unchanged; it was already allowed to write every table the seeder touches
# (scripts 33 and 42), which is precisely why the logic could stay where it is.
#
# IDEMPOTENCY IS NOT NEW. The seeder was ALREADY safe to call repeatedly — the
# lazy path ran it on every task-list read. Eager seeding just adds one more
# caller to a function built for exactly that. The lazy call is deliberately
# KEPT as the safety net for a failed invoke, a legacy row, or a recording
# processed before this deploy.
#
# NON-BLOCKING BY DESIGN. The invoke is async ("Event") and every failure is
# swallowed and logged. The transcript and analysis are already persisted and
# already paid for by the time it runs; raising would fail the invocation, and
# S3/Lambda would then retry the WHOLE pipeline, re-paying ElevenLabs and Groq
# for a recording that already succeeded. A failed seed costs a delay, not a
# task — the lazy path still produces the same rows on first read.
#
# Prerequisites:
#   - scripts/33_deploy_workspace_org.sh   (Tasks table + userApi task routes)
#   - scripts/42_deploy_notifications.sh   (the notifications the seeder raises)
#
# Idempotent: put-role-policy and update-function-code overwrite.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"
TRANSCRIBE_LAMBDA_NAME="${TRANSCRIBE_LAMBDA_NAME:-transcribeRecording}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

echo ">> userApi:    $USERAPI_LAMBDA_NAME"
echo ">> transcribe: $TRANSCRIBE_LAMBDA_NAME"

# -------------------------------------------------------------
# 0. Offline tests before anything is provisioned.
#    Same gate scripts 38/40/42 apply — these need no AWS, so there is no
#    reason to discover a broken build after the deploy.
# -------------------------------------------------------------
echo ">> Running the eager-seeding test suite (offline) ..."
( cd "$PROJECT_ROOT" && python -m pytest tests/test_eager_task_seeding.py -q )
echo ">> Running the task suites it must not regress ..."
( cd "$PROJECT_ROOT" && python -m pytest \
    tests/test_task_extraction.py tests/test_workspace_org.py \
    tests/test_notifications.py -q )
echo ">> Tests passed."

# -------------------------------------------------------------
# 1. IAM — transcribeRecording may invoke userApi, and nothing else.
#
# Resolved to the real function ARN rather than written as a wildcard: the
# pipeline needs exactly one call, so the grant should describe exactly one
# call. A "lambda:InvokeFunction on *" here would let a future bug in the
# transcription path reach any function in the account.
# -------------------------------------------------------------
USERAPI_ARN="$(aws lambda get-function --function-name "$USERAPI_LAMBDA_NAME" \
                 --query 'Configuration.FunctionArn' --output text)"
if [[ -z "$USERAPI_ARN" || "$USERAPI_ARN" == "None" ]]; then
  echo "ERROR: could not resolve $USERAPI_LAMBDA_NAME - run 33 first." >&2
  exit 1
fi
echo ">> userApi ARN: $USERAPI_ARN"

TR_ROLE="$(aws lambda get-function --function-name "$TRANSCRIBE_LAMBDA_NAME" \
             --query 'Configuration.Role' --output text)"
TR_ROLE="${TR_ROLE##*/}"

POLICY_FILE="$(mktemp)"
trap 'rm -f "$POLICY_FILE"' EXIT
cat > "$POLICY_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "InvokeUserApiForTaskSeeding", "Effect": "Allow",
      "Action": "lambda:InvokeFunction",
      "Resource": ["${USERAPI_ARN}", "${USERAPI_ARN}:*"] }
  ]
}
EOF
aws iam put-role-policy \
  --role-name "$TR_ROLE" \
  --policy-name "transcribe-invoke-userapi" \
  --policy-document "file://$(winpath "$POLICY_FILE")"
echo ">> $TRANSCRIBE_LAMBDA_NAME ($TR_ROLE): may now invoke userApi."

# -------------------------------------------------------------
# 2. Environment — transcribeRecording needs to know what to call.
#    Defaulted in code to "userApi", set explicitly so a renamed stack works.
# -------------------------------------------------------------
CURRENT_ENV="$(aws lambda get-function-configuration \
                 --function-name "$TRANSCRIBE_LAMBDA_NAME" \
                 --query 'Environment.Variables' --output json)"
MERGED_ENV="$(python - "$CURRENT_ENV" "$USERAPI_LAMBDA_NAME" <<'PY'
import json, sys
env_json, userapi = sys.argv[1:3]
env = json.loads(env_json) if env_json not in ("null", "") else {}
env["USERAPI_LAMBDA_NAME"] = userapi
print(json.dumps({"Variables": env}))
PY
)"
aws lambda update-function-configuration \
  --function-name "$TRANSCRIBE_LAMBDA_NAME" \
  --environment "$MERGED_ENV" \
  --query "[FunctionName,Environment.Variables.USERAPI_LAMBDA_NAME]" --output text
aws lambda wait function-updated-v2 --function-name "$TRANSCRIBE_LAMBDA_NAME"

# -------------------------------------------------------------
# 3. Deploy both functions, shared modules vendored FLAT.
#    A zip with only lambda_function.py fails on cold start and takes down
#    every route — same helper as scripts 21/23/38/40/42.
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

# userApi FIRST. It is the one that grows the ability to HANDLE the seed event;
# deploying the caller first would open a window where transcribeRecording
# sends an event the deployed userApi does not recognise. The event would be
# 404'd and the tasks would fall back to lazy seeding — harmless, but pointless
# to arrange on purpose.
deploy_py_with_shared "$USERAPI_LAMBDA_NAME" "functions/userapi"
deploy_py_with_shared "$TRANSCRIBE_LAMBDA_NAME" "functions/transcribe"

# -------------------------------------------------------------
# 4. Verify the seed event is actually handled.
#
# A SYNCHRONOUS invoke with a deliberately nonexistent recording key. The
# handler must answer {"seeded": 0, "error": "recording not found"} — which
# proves the new branch is live and reached, without creating anything. A
# FunctionError or a 404-shaped HTTP body would mean the deployed userApi does
# not know this event, i.e. the wiring is dead.
# -------------------------------------------------------------
PROBE_OUT="$(mktemp)"
trap 'rm -f "$POLICY_FILE" "$PROBE_OUT"' EXIT
PROBE_PAYLOAD='{"type":"tasks.seed","audio_s3_key":"recordings/__deploy_probe__/none.m4a"}'
if aws lambda invoke \
      --function-name "$USERAPI_LAMBDA_NAME" \
      --payload "$(printf '%s' "$PROBE_PAYLOAD" | base64 | tr -d '\n')" \
      --query 'FunctionError' --output text "$PROBE_OUT" 2>/dev/null | grep -qv "None"; then
  echo "WARNING: the seed probe returned a FunctionError:" >&2
  cat "$PROBE_OUT" >&2
else
  BODY="$(cat "$PROBE_OUT")"
  case "$BODY" in
    *'"seeded": 0'*|*'"seeded":0'*)
      echo ">> Probe OK: userApi handled tasks.seed -> $BODY" ;;
    *statusCode*404*)
      echo "WARNING: userApi did not recognise tasks.seed (fell through to the" >&2
      echo "         HTTP router). Eager seeding will NOT run." >&2 ;;
    *)
      echo ">> Probe returned: $BODY" ;;
  esac
fi

echo ""
echo ">> Done. AI tasks are now seeded when processing completes."
echo "   The lazy path is RETAINED as the safety net — seeding is idempotent,"
echo "   so eager + lazy together still produce one task per fingerprint."
