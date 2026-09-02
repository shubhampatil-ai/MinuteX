#!/usr/bin/env bash
# =============================================================
# 47_deploy_assignee_meeting_access.sh — the read-only meeting a task
# assignee can open from their task.
#
#   GET /recordings/shared-with-me/{key+}   (JWT)  notes-only meeting view
#
# WHAT THIS ADDS. Before this, every meeting route gated on OWNERSHIP, so the
# person a task was assigned to could see a title and a deadline and nothing
# about the meeting the work came from. This route lets them read that
# meeting's NOTES — and only the notes.
#
# NO NEW TABLE, NO NEW IAM. That is the point of the design and the reason
# this script is short. Access is DERIVED on each request from the Tasks
# table (assignee-user-index, created by 44) rather than stored as a grant,
# so there is nothing to provision, nothing to migrate, and nothing to sweep
# when a task is reassigned — reassignment revokes access by construction.
# The Lambda already holds Query on the Tasks indexes and GetItem on
# Recordings; this route reads nothing else.
#
# WHY {key+} IS GREEDY AND THE ACTION COMES FIRST. Same hard API Gateway
# constraint every recording-scoped route in this API obeys: a recording key
# contains slashes, so it must be greedy, and "Greedy variables may only be in
# last position". Hence /recordings/shared-with-me/{key+} and never
# /recordings/{key}/shared-with-me. The literal "shared-with-me" segment
# cannot collide with a real key, which always begins "recordings/{user_id}/"
# or a legacy device id.
#
# SECURITY SURFACE. This is the first route in the API that serves one user
# content from ANOTHER user's recording, so the preflight below refuses to
# deploy unless the offline suite that pins the boundary passes — no
# transcript, no audio, no presign, 404 for a non-assignee, and instant
# revocation on reassignment.
#
# Idempotent: the route is detected and skipped if it already exists.
#
# Run:  bash scripts/47_deploy_assignee_meeting_access.sh
#       SKIP_TESTS=1 bash scripts/47_deploy_assignee_meeting_access.sh
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"

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
SHARE_SHARED="$PROJECT_ROOT/shared/share_schema.py"

# The route reuses the share payload assembler rather than building its own —
# that is what guarantees a new attribute on the recording row cannot leak to
# an assignee by being forgotten. Without this module the handler cannot even
# import.
if [[ ! -f "$SHARE_SHARED" ]]; then
  echo "ERROR: shared/share_schema.py is missing — this route depends on it." >&2
  exit 1
fi

for handler_fn in get_assignee_meeting; do
  if ! grep -q "^def ${handler_fn}(event)" "$HANDLER"; then
    echo "ERROR: handler ${handler_fn}() not found in lambda_function.py." >&2
    exit 1
  fi
done

for helper in _assignee_share_config _assignee_tasks_in_recording \
              _assignee_readable_recording; do
  if ! grep -q "^def ${helper}(" "$HANDLER"; then
    echo "ERROR: helper ${helper}() not found in lambda_function.py." >&2
    exit 1
  fi
done

if ! grep -q '"/recordings/shared-with-me/{key+}"' "$HANDLER"; then
  echo "ERROR: route /recordings/shared-with-me/{key+} is not registered in" >&2
  echo "       the _ROUTES table. The API Gateway route below would 404." >&2
  exit 1
fi
echo ">> Preflight: handler, 3 helpers and the route are all present."

# The index the derived grant is queried on. Created by 44; without it the
# route 500s on every assignee request while working fine for the owner —
# exactly the kind of half-broken deploy worth catching here.
ASSIGNEE_INDEX="${TASKS_ASSIGNEE_USER_INDEX:-assignee-user-index}"
TASKS_TABLE="${TASKS_TABLE:-Tasks}"
INDEX_STATUS="$(aws dynamodb describe-table --table-name "$TASKS_TABLE" \
    --query "Table.GlobalSecondaryIndexes[?IndexName=='${ASSIGNEE_INDEX}'].IndexStatus | [0]" \
    --output text 2>/dev/null | tr -d '\r' || echo "None")"
if [[ "$INDEX_STATUS" != "ACTIVE" ]]; then
  echo "ERROR: ${TASKS_TABLE} index '${ASSIGNEE_INDEX}' is '${INDEX_STATUS}', not ACTIVE." >&2
  echo "       Run scripts/44_add_assignee_user_index.sh and wait for it." >&2
  exit 1
fi
echo ">> Index verified: ${TASKS_TABLE}/${ASSIGNEE_INDEX} is ACTIVE."

# This route serves one user content from ANOTHER user's meeting, so the suite
# pinning that boundary is not optional. It asserts the payload carries no
# transcript, no audio and no presign, that a non-assignee gets 404, and that
# reassignment revokes on the next request.
if [[ "${SKIP_TESTS:-0}" != "1" ]]; then
  echo ">> Running the assignee-access and task-permission suites..."
  ( cd "$PROJECT_ROOT" && python -m pytest \
      tests/test_assignee_meeting_access.py \
      tests/test_task_permissions.py \
      tests/test_meeting_share.py -q ) \
    || { echo "ERROR: tests failed — not deploying." >&2; exit 1; }
else
  echo ">> SKIP_TESTS=1 — skipping the offline suite."
fi

# -------------------------------------------------------------
# 2. Deploy the code. Shared modules vendored FLAT — userApi imports
#    ai_schema/groq_client/mom_schema/share_schema/prompts/stt_result/
#    transcript_store at module scope, so a zip without them fails on cold
#    start (same packaging as 23/25/27/30/33/36/38/46).
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

  ( cd "$PROJECT_ROOT" && python - "$src_dir" <<'PY'
import sys, zipfile
from pathlib import Path

names = zipfile.ZipFile(Path.cwd() / sys.argv[1] / "function.zip").namelist()
missing = [m for m in ("lambda_function.py", "share_schema.py", "mom_schema.py",
                       "ai_schema.py", "groq_client.py", "prompts.py")
           if m not in names]
if missing:
    sys.exit(f"ERROR: archive is missing {', '.join(missing)} — refusing to deploy.")
print(f">> archive verified: {len(names)} entries, share_schema.py present")
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
# 3. Wire the route.
# -------------------------------------------------------------
USERAPI_INT="$(aws apigatewayv2 get-integrations --api-id "$API_ID" \
    --query "Items[?contains(IntegrationUri, '${USERAPI_LAMBDA_NAME}')].IntegrationId | [0]" \
    --output text)"
if [[ "$USERAPI_INT" == "None" || -z "$USERAPI_INT" ]]; then
  echo "ERROR: no API integration for $USERAPI_LAMBDA_NAME — run 17 first." >&2
  exit 1
fi

# Filter by CONTENT, not by line position — see 33's note. `aws --output text`
# emits the RouteId plus a stray "None" line in EITHER order, so a `head -1`
# that grabs the "None" makes an existing route look absent and the re-run
# dies on a ConflictException.
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

ensure_route "$USERAPI_INT" "GET /recordings/shared-with-me/{key+}"

# -------------------------------------------------------------
# 4. Verify the route reaches the Lambda.
#
# Probed WITHOUT a bearer token, which must produce the Lambda's own 401 from
# _require_auth. That is the useful signal: a 401 proves the request reached
# the handler (this API has no gateway authorizer), whereas a 403 or 000 means
# the route was never created on this stage and every assignee would see the
# meeting row fail to open.
# -------------------------------------------------------------
API_ENDPOINT="$(aws apigatewayv2 get-api --api-id "$API_ID" \
    --query 'ApiEndpoint' --output text | tr -d '\r')"
PROBE_URL="${API_ENDPOINT}/recordings/shared-with-me/deploy-probe/not-a-real-key.m4a"
echo ">> Probing $PROBE_URL"

PROBE_STATUS="$(curl -s -o /dev/null -w '%{http_code}' "$PROBE_URL" || echo "000")"

case "$PROBE_STATUS" in
  401)
    echo ">> Route verified: unauthenticated -> 401 from the Lambda."
    ;;
  200)
    echo "ERROR: an unauthenticated request returned 200 — auth is not running." >&2
    exit 1
    ;;
  403|000)
    echo "ERROR: the route did not reach the Lambda (HTTP $PROBE_STATUS)." >&2
    echo "       403 usually means the route was not created on this stage." >&2
    exit 1
    ;;
  *)
    echo ">> Unexpected probe status $PROBE_STATUS (expected 401)." >&2
    echo "   The route exists; check the Lambda logs before relying on it." >&2
    ;;
esac

echo
echo "============================================================="
echo " Assignee meeting access deployed."
echo
echo "   GET /recordings/shared-with-me/{key+}   (JWT)"
echo
echo " A task assignee can now open the MEETING NOTES their work came"
echo " from. No transcript, no audio, no presigned URL, and no write"
echo " route — access is derived from the current task assignment, so"
echo " reassigning or deleting the task revokes it on the next request."
echo "============================================================="
