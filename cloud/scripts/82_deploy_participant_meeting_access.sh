#!/usr/bin/env bash
# =============================================================
# 82_deploy_participant_meeting_access.sh — tagged MinuteX users can open the
# meeting, READ-ONLY, at the level the recorder chooses.
#
#   GET /recordings/shared-with-me          (JWT)  the Desk's "Shared with you"
#   PUT /recordings/access/{key+}           (JWT)  recorder-only: none|notes|full
#   GET /recordings/shared-with-me/{key+}   (existing, script 47) now serves
#                                           participant grants too
#
# WHAT CHANGES. Tagging a person who has a MinuteX account gives them NOTES
# access by default; the recorder can switch each person to FULL (notes +
# transcript + audio) or NONE. Every level is read-only. The recorder keeps
# every existing owner read, write, and meeting-edit permission; these
# permissions are unchanged by participant grants.
#
# SECURITY INVARIANT. MeetingAccess is never general meeting authorization —
# it is consumed only by the shared read-only route. The preflight below
# refuses to deploy unless the suite that pins that (every owner route 404s
# for a grantee, every write route refuses a FULL grantee) passes.
#
# NO NEW TABLE, NO NEW IAM, NO ENV CHANGE. MeetingAccess (and its user-index)
# plus the MinuteXMeetingAccess policy — GetItem/PutItem/UpdateItem/
# DeleteItem/Query on the table and index/* — come from script 54, verified
# below. This script deploys CODE ONLY and never touches the Lambda's
# environment, so GROQ_MODEL and every other variable are left as they are.
#
# ROUTE COLLISION. "GET /recordings/shared-with-me" is a literal path and API
# Gateway prefers it over the greedy "GET /recordings/{key+}", exactly as the
# existing "/recordings/shared-with-me/{key+}" already is. A real recording key
# always begins "recordings/{user_id}/" or a device id, so it cannot equal it.
#
# Idempotent: existing routes are detected and skipped.
#
# Run:  bash scripts/82_deploy_participant_meeting_access.sh
#       SKIP_TESTS=1 bash scripts/82_deploy_participant_meeting_access.sh
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"
MEETING_ACCESS_TABLE="${MEETING_ACCESS_TABLE:-MeetingAccess}"
MEETING_ACCESS_USER_INDEX="${MEETING_ACCESS_USER_INDEX:-user-index}"

API_ID="$(aws apigatewayv2 get-apis \
            --query "Items[?Name=='${API_NAME}'].ApiId | [0]" \
            --output text)"
if [[ "$API_ID" == "None" || -z "$API_ID" ]]; then
  echo "ERROR: HTTP API '$API_NAME' not found — run 03_create_apigateway.sh first." >&2
  exit 1
fi
echo ">> API: $API_NAME ($API_ID)"
echo ">> Lambda: $USERAPI_LAMBDA_NAME"

# -------------------------------------------------------------
# 1. Preflight — fail BEFORE touching AWS if the source is incomplete.
# -------------------------------------------------------------
HANDLER="$PROJECT_ROOT/functions/userapi/lambda_function.py"

for handler_fn in list_shared_with_me set_meeting_access get_assignee_meeting; do
  if ! grep -q "^def ${handler_fn}(event)" "$HANDLER"; then
    echo "ERROR: handler ${handler_fn}() not found in lambda_function.py." >&2
    exit 1
  fi
done
for route in '"/recordings/shared-with-me"' '"/recordings/access/{key+}"'; do
  if ! grep -q "$route" "$HANDLER"; then
    echo "ERROR: route $route is not registered in _ROUTES." >&2
    exit 1
  fi
done
if ! grep -q "MEETING_ACCESS_GRANTED" "$PROJECT_ROOT/shared/notification_schema.py"; then
  echo "ERROR: MEETING_ACCESS_GRANTED is missing from notification_schema.py." >&2
  exit 1
fi
echo ">> Preflight: handlers, routes and notification type present."

# The table and its user-index (script 54). Without the index the Desk's
# "Shared with you" 500s for everyone while the owner screens keep working.
INDEX_STATUS="$(aws dynamodb describe-table --table-name "$MEETING_ACCESS_TABLE" \
    --query "Table.GlobalSecondaryIndexes[?IndexName=='${MEETING_ACCESS_USER_INDEX}'].IndexStatus | [0]" \
    --output text 2>/dev/null | tr -d '\r' || echo "None")"
if [[ "$INDEX_STATUS" != "ACTIVE" ]]; then
  echo "ERROR: ${MEETING_ACCESS_TABLE}/${MEETING_ACCESS_USER_INDEX} is '${INDEX_STATUS}', not ACTIVE." >&2
  echo "       Run scripts/54_workspace_resource_indexes.sh first." >&2
  exit 1
fi
echo ">> Index verified: ${MEETING_ACCESS_TABLE}/${MEETING_ACCESS_USER_INDEX} is ACTIVE."

USERAPI_ROLE_ARN="$(aws lambda get-function --function-name "$USERAPI_LAMBDA_NAME" \
                      --query 'Configuration.Role' --output text)"
USERAPI_ROLE_NAME="${USERAPI_ROLE_ARN##*/}"
POLICY_DOC="$(aws iam get-role-policy --role-name "$USERAPI_ROLE_NAME" \
    --policy-name "MinuteXMeetingAccess" --query 'PolicyDocument' --output json \
    2>/dev/null || echo "")"
for action in dynamodb:Query dynamodb:DeleteItem dynamodb:PutItem dynamodb:GetItem; do
  if ! grep -q "\"$action\"" <<<"$POLICY_DOC"; then
    echo "ERROR: $USERAPI_ROLE_NAME policy MinuteXMeetingAccess lacks $action." >&2
    echo "       Run scripts/54_workspace_resource_indexes.sh to (re)attach it." >&2
    exit 1
  fi
done
if ! grep -q "index/\*" <<<"$POLICY_DOC"; then
  echo "ERROR: MinuteXMeetingAccess does not cover ${MEETING_ACCESS_TABLE}/index/*." >&2
  exit 1
fi
echo ">> IAM verified: MinuteXMeetingAccess allows Get/Put/Delete/Query incl. index/*."

# Each suite in its OWN process: some suites share the patched `api` module
# and are order-sensitive when collected together.
if [[ "${SKIP_TESTS:-0}" != "1" ]]; then
  echo ">> Running the access-boundary suites..."
  for suite in test_meeting_access test_assignee_meeting_access \
               test_workspace_resources test_participant_self_tag \
               test_notifications; do
    ( cd "$PROJECT_ROOT" && python -m pytest "tests/${suite}.py" -q ) \
      || { echo "ERROR: tests/${suite}.py failed — not deploying." >&2; exit 1; }
  done
else
  echo ">> SKIP_TESTS=1 — skipping the offline suites."
fi

# -------------------------------------------------------------
# 2. Deploy the code — same flat packaging as 47 (shared/*.py vendored).
# -------------------------------------------------------------
ZIP="$PROJECT_ROOT/functions/userapi/function.zip"
rm -f "$ZIP"
( cd "$PROJECT_ROOT" && python - <<'PY'
import zipfile
from pathlib import Path

root = Path.cwd()
out = root / "functions/userapi/function.zip"
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    z.write(root / "functions/userapi/lambda_function.py", "lambda_function.py")
    for mod in sorted((root / "shared").glob("*.py")):
        z.write(mod, mod.name)
names = zipfile.ZipFile(out).namelist()
missing = [m for m in ("lambda_function.py", "share_schema.py",
                       "workspace_schema.py", "notification_schema.py",
                       "mom_schema.py", "ai_schema.py") if m not in names]
if missing:
    raise SystemExit(f"ERROR: archive is missing {', '.join(missing)}")
print(f">> packaged {out.relative_to(root)} ({len(names)} entries)")
PY
)

aws lambda update-function-code \
  --function-name "$USERAPI_LAMBDA_NAME" \
  --zip-file "fileb://$(winpath "$ZIP")" \
  --publish \
  --query "[FunctionName,CodeSize,LastModified]" --output text
aws lambda wait function-updated-v2 --function-name "$USERAPI_LAMBDA_NAME"
echo ">> $USERAPI_LAMBDA_NAME deployed (code only; environment untouched)."

# -------------------------------------------------------------
# 3. Wire the routes.
# -------------------------------------------------------------
USERAPI_INT="$(aws apigatewayv2 get-integrations --api-id "$API_ID" \
    --query "Items[?contains(IntegrationUri, '${USERAPI_LAMBDA_NAME}')].IntegrationId | [0]" \
    --output text)"
if [[ "$USERAPI_INT" == "None" || -z "$USERAPI_INT" ]]; then
  echo "ERROR: no API integration for $USERAPI_LAMBDA_NAME — run 17 first." >&2
  exit 1
fi

# Filter by CONTENT, not line position — see 33's/47's note on the stray
# "None" line `--output text` can emit in either order.
ensure_route() {
  local integration_id="$1" route_key="$2" existing
  existing="$(aws apigatewayv2 get-routes --api-id "$API_ID" \
      --query "Items[?RouteKey=='${route_key}'].RouteId | [0]" --output text \
      | tr -d '\r' | grep -v '^None$' | grep -v '^$' | head -1 || true)"
  if [[ -n "$existing" ]]; then
    echo ">> Route exists: $route_key ($existing)"
    return 0
  fi
  aws apigatewayv2 create-route \
    --api-id "$API_ID" \
    --route-key "$route_key" \
    --target "integrations/$integration_id" >/dev/null
  echo ">> Route created: $route_key"
}

ensure_route "$USERAPI_INT" "GET /recordings/shared-with-me"
ensure_route "$USERAPI_INT" "PUT /recordings/access/{key+}"

# -------------------------------------------------------------
# 4. Verify both routes reach the Lambda (unauthenticated -> its own 401).
# -------------------------------------------------------------
API_ENDPOINT="$(aws apigatewayv2 get-api --api-id "$API_ID" \
    --query 'ApiEndpoint' --output text | tr -d '\r')"

probe() {
  local method="$1" url="$2" status
  status="$(curl -s -o /dev/null -w '%{http_code}' -X "$method" "$url" || echo "000")"
  case "$status" in
    401) echo ">> Verified $method $url -> 401 from the Lambda." ;;
    2*)  echo "ERROR: unauthenticated $method returned $status — auth is not running." >&2; exit 1 ;;
    403|000)
         echo "ERROR: $method $url did not reach the Lambda (HTTP $status)." >&2; exit 1 ;;
    *)   echo ">> Unexpected status $status for $method $url (expected 401) — check the logs." >&2 ;;
  esac
}
probe GET "${API_ENDPOINT}/recordings/shared-with-me"
probe PUT "${API_ENDPOINT}/recordings/access/deploy-probe/not-a-real-key.m4a"

echo
echo "============================================================="
echo " Participant meeting access deployed."
echo
echo "   GET /recordings/shared-with-me          (JWT)"
echo "   PUT /recordings/access/{key+}           (JWT, recorder only)"
echo
echo " Tagged MinuteX users get read-only Notes by default; the"
echo " recorder can set each person to No access / Notes / Full."
echo " The recorder's own permissions are unchanged."
echo "============================================================="
