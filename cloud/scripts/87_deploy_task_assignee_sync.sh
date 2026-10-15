#!/usr/bin/env bash
# =============================================================
# 87_deploy_task_assignee_sync.sh — task detail shows the real assignee.
#
#   GET /tasks/{task_id}   (unchanged route, fixed hydration)
#
# THE BUG: get_task hydrated `contact` with an inline
# `owner_user_id == user_id` test. In an organisation workspace the assignee
# is usually a SHARED contact a colleague created, or a materialized member
# contact — readable by the caller but not owned by them — so `contact` was
# withheld and the task detail said "Nobody is assigned" while the Task
# dashboard (which renders the row's assignee_name) showed the person.
#
# THE FIX: hydrate through _readable_contact (the one contact read rule),
# falling back to the task row's own display fields when the contact is
# unreadable or deleted.
#
# WHAT IT DOES
#   1. Preflight: the fix is in source; the task/access suites pass.
#   2. Code only (flat packaging, shared/*.py vendored).
#   3. Probe: GET /tasks/{id} without a JWT reaches the Lambda (401).
#
# NO env, IAM, table or route changes — every variable (GROQ_MODEL included)
# is left exactly as it is on the function.
#
# NOTE: this packages the WHOLE current functions/userapi + shared/ tree, so
# any other uncommitted backend edits in your working copy ship with it.
#
# ROLLBACK: re-deploy the previous commit's userApi code.
#
# Run:  bash scripts/87_deploy_task_assignee_sync.sh      SKIP_TESTS=1 to skip pytest
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
grep -q "^def get_task(event)" "$HANDLER" \
  || { echo "FAIL: get_task() missing from lambda_function.py" >&2; exit 1; }
# The fix itself: get_task must hydrate through _readable_contact.
python - "$HANDLER" <<'PY' || { echo "FAIL: get_task does not use _readable_contact — fix not in source." >&2; exit 1; }
import io, sys
src = io.open(sys.argv[1], encoding="utf-8").read()
start = src.index("\ndef get_task(event):")
end = src.index("\ndef ", start + 1)
sys.exit(0 if "_readable_contact(user_id, cid)" in src[start:end] else 1)
PY
echo ">> get_task hydrates the assignee via _readable_contact."

if [[ "${SKIP_TESTS:-0}" != "1" ]]; then
  ( cd "$PROJECT_ROOT" && python -m pytest -q \
      tests/test_assignee_meeting_access.py \
      tests/test_task_permissions.py \
      tests/test_notifications.py ) \
    || { echo "FAIL: task/access tests — not deploying." >&2; exit 1; }
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
echo ">> $USERAPI_FN code updated."

# -------------------------------------------------------------
# 3. Probe — an unauthenticated read must reach OUR auth check.
# -------------------------------------------------------------
if [[ -z "${API_ID:-}" ]]; then
  for name in ${API_NAME:-} meeting-recorder-api iot-audio-api; do
    API_ID="$(aws apigatewayv2 get-apis --query "Items[?Name=='${name}'].ApiId | [0]" \
      --output text | tr -d '\r')"
    [[ -n "$API_ID" && "$API_ID" != "None" ]] && break
  done
fi
[[ -n "$API_ID" && "$API_ID" != "None" ]] || { echo "FAIL: no API id" >&2; exit 1; }
API_ENDPOINT="$(aws apigatewayv2 get-api --api-id "$API_ID" \
    --query 'ApiEndpoint' --output text | tr -d '\r')"
status="$(curl -s -o /dev/null -w '%{http_code}' \
    "${API_ENDPOINT}/tasks/probe-task-id" || echo 000)"
case "$status" in
  401) echo ">> Verified GET /tasks/{id} -> 401 from the Lambda." ;;
  *)   echo "ERROR: GET /tasks/{id} returned $status — check the deploy/logs." >&2; exit 1 ;;
esac

echo
echo "============================================================="
echo " Task assignee sync deployed: GET /tasks/{id} now shows shared"
echo " organisation / member contacts as the assignee."
echo "============================================================="
