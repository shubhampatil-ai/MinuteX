#!/usr/bin/env bash
# =============================================================
# 48_deploy_ai_task_dashboard.sh — the AI Task Dashboard on userApi.
#
# Ships ONE new route plus the code behind three changed ones:
#
#   POST /ai/task-intelligence   {} -> {summary, rows, analyzed, partial}
#
# and redeploys the Lambda so these keep working:
#
#   POST /ai/chat        now also returns {sources, proposals}   (additive)
#   GET  /ai/suggestions now accepts ?task_id=                   (additive)
#
# WHY THIS SCRIPT EXISTS AT ALL — read this before assuming it is boilerplate.
#
# THE BUG IT FIXES. /ai/task-intelligence was registered in the Lambda's
# _ROUTES table and never wired into API Gateway. A route in _ROUTES that has
# no API Gateway route is not a 500 and not a log line: API Gateway answers
# 404 "Not Found" before the Lambda is ever invoked, so nothing in CloudWatch
# records the attempt. In the app that surfaced as the dashboard's
# "Could not analyze your tasks" — the frontend's catch-all, faithfully
# reporting a request that never reached the backend.
#
# The lesson generalises: adding a handler to _ROUTES is HALF a route. Every
# static path needs an ensure_route here too, and the verification step below
# now asserts the route exists rather than trusting that creating it worked.
#
# WHAT THE ENDPOINT IS. Structured judgements over the caller's open tasks for
# the dashboard's AI sections: (task_id, kind, reason, recommendation) rows the
# app merges onto tasks it already loaded from GET /tasks. It is deliberately
# NOT a source of task state — the response schema has no status/assignee/
# deadline field at all (see ai_schema.coerce_task_intelligence), so a
# recommendation cannot contradict the Tasks table.
#
# WHY IT IS POST AND NOT GET. It runs a Groq generation. A GET invites a proxy
# or a client to cache or retry it for free, and neither is true.
#
# NO NEW TABLES, NO NEW IAM, NO NEW PROVIDER. It reads Tasks and Recordings
# through the SAME predicates the REST routes use, and Groq is already
# configured on userApi by script 21. Both verified below rather than assumed.
#
# NEW ENV — OPTIONAL, all defaulted in the Lambda:
#   AI_INTEL_MAX_TASKS          (60) tasks sent for analysis. A COUNT cap; the
#                                    handler ALSO trims to the token budget,
#                                    because task views vary in size and 60
#                                    fat rows measured ~15.6k tokens — over
#                                    the 12k TPM a default account has.
#   AI_CONTEXT_DEADLINE_SECONDS  (9) budget for one PageIndex retrieval inside
#                                    an agent hop, which still has to spend a
#                                    Groq call turning it into an answer.
#   AI_CONTEXT_MAX_CHARS      (6000) retrieved transcript per tool result.
# Set unconditionally so the deployed config states what it uses rather than
# relying on a default that could drift.
#
# Prerequisites: 21 (GROQ_API_KEY), 33 (Tasks table + IAM), 34 (/ai/chat),
#                46 (PageIndex — get_meeting_context reuses its retrieval).
# Idempotent: update-function-code / ensure_route all overwrite or skip.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"
TASKS_TABLE="${TASKS_TABLE:-Tasks}"
AI_INTEL_MAX_TASKS="${AI_INTEL_MAX_TASKS:-60}"
AI_CONTEXT_DEADLINE_SECONDS="${AI_CONTEXT_DEADLINE_SECONDS:-9}"
AI_CONTEXT_MAX_CHARS="${AI_CONTEXT_MAX_CHARS:-6000}"

API_ID="$(aws apigatewayv2 get-apis \
            --query "Items[?Name=='${API_NAME}'].ApiId | [0]" \
            --output text)"
if [[ "$API_ID" == "None" || -z "$API_ID" ]]; then
  echo "ERROR: HTTP API '$API_NAME' not found — run 03_create_apigateway.sh first." >&2
  exit 1
fi
echo ">> API: $API_NAME ($API_ID)"

# -------------------------------------------------------------
# 0. Prerequisites. Both are silent-ish at deploy time and loud in production.
# -------------------------------------------------------------
if ! aws dynamodb describe-table --table-name "$TASKS_TABLE" >/dev/null 2>&1; then
  echo "ERROR: table '$TASKS_TABLE' does not exist." >&2
  echo "       Run scripts/31_create_workspace_tables.sh and 33_deploy_workspace_org.sh first." >&2
  exit 1
fi
echo ">> Tasks table present: $TASKS_TABLE"

CURRENT_ENV="$(aws lambda get-function-configuration \
                 --function-name "$USERAPI_LAMBDA_NAME" \
                 --query 'Environment.Variables' --output json)"
if ! echo "$CURRENT_ENV" | grep -q '"GROQ_API_KEY"'; then
  echo "ERROR: $USERAPI_LAMBDA_NAME has no GROQ_API_KEY in its environment." >&2
  echo "       Run scripts/21_deploy_ai_workspace.sh first." >&2
  exit 1
fi
echo ">> GROQ_API_KEY present on $USERAPI_LAMBDA_NAME."

# The assistant route this builds on. Its absence means script 34 never ran,
# and the dashboard's chat entry point would 404 the same way task-intelligence
# did — worth catching here rather than in the app.
CHAT_ROUTE="$(aws apigatewayv2 get-routes --api-id "$API_ID" \
    --query "Items[?RouteKey=='POST /ai/chat'].RouteId | [0]" --output text \
    | tr -d '\r' | grep -v '^None$' | grep -v '^$' | head -1 || true)"
if [[ -z "$CHAT_ROUTE" ]]; then
  echo "ERROR: 'POST /ai/chat' is not wired — run scripts/34_deploy_ai_assistant.sh first." >&2
  exit 1
fi
echo ">> POST /ai/chat present ($CHAT_ROUTE)."

# -------------------------------------------------------------
# 1. Env: the tuning knobs, MERGED (never replaced — see script 34).
# -------------------------------------------------------------
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
  echo ">> $fn env merged: $*"
}

merge_env "$USERAPI_LAMBDA_NAME" \
  "AI_INTEL_MAX_TASKS=$AI_INTEL_MAX_TASKS" \
  "AI_CONTEXT_DEADLINE_SECONDS=$AI_CONTEXT_DEADLINE_SECONDS" \
  "AI_CONTEXT_MAX_CHARS=$AI_CONTEXT_MAX_CHARS"

# -------------------------------------------------------------
# 2. Deploy the code. Shared modules vendored FLAT — ai_schema.py and
#    prompts.py BOTH changed in this release (coerce_task_intelligence,
#    ASSISTANT_TASK_RULES + TASK_INTELLIGENCE_SYSTEM), so shipping only
#    lambda_function.py would cold-start into an AttributeError.
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
# 3. Wire the route.
#
# A static path — no {key+}, so none of the greedy-variable ordering
# constraints that shape the /recordings/ai/* routes apply.
# -------------------------------------------------------------
USERAPI_INT="$(aws apigatewayv2 get-integrations --api-id "$API_ID" \
    --query "Items[?contains(IntegrationUri, '${USERAPI_LAMBDA_NAME}')].IntegrationId | [0]" \
    --output text)"
if [[ "$USERAPI_INT" == "None" || -z "$USERAPI_INT" ]]; then
  echo "ERROR: no API integration for $USERAPI_LAMBDA_NAME — run 17 first." >&2
  exit 1
fi

# Filter by CONTENT, not by line position — this command emits the RouteId
# plus a stray "None" line in EITHER order, and a head -1 that grabs the
# "None" makes an existing route look absent (see script 33).
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

ensure_route "$USERAPI_INT" "POST /ai/task-intelligence"

# -------------------------------------------------------------
# 4. VERIFY THE ROUTE IS REALLY THERE.
#
# This step exists because of the bug this script fixes. create-route
# succeeding is not proof the route resolves — and the failure mode is a 404
# that never reaches CloudWatch, so nothing downstream would tell us. Assert
# it by reading the route back.
# -------------------------------------------------------------
for want in "POST /ai/task-intelligence" "POST /ai/chat" "GET /ai/suggestions"; do
  got="$(aws apigatewayv2 get-routes --api-id "$API_ID" \
      --query "Items[?RouteKey=='${want}'].RouteId | [0]" --output text \
      | tr -d '\r' | grep -v '^None$' | grep -v '^$' | head -1 || true)"
  if [[ -z "$got" ]]; then
    echo "ERROR: route '$want' is STILL missing after wiring." >&2
    echo "       The app would show 'Could not analyze your tasks' for a 404" >&2
    echo "       that never reaches the Lambda. Do not ship this state." >&2
    exit 1
  fi
  echo ">> Verified route: $want ($got)"
done

# -------------------------------------------------------------
# 5. Verify the role can Query the indexes the tools read.
#
# The table-vs-index ARN distinction is the failure mode worth probing: a
# policy granting Query on the table but not on table/index/* passes every
# GetItem and fails every list — which surfaces as the assistant saying "you
# have no tasks" rather than as a permissions error. A confident wrong empty
# answer is indistinguishable from a true one, which is the worst outcome for
# this feature and precisely the class of bug this pass was opened for.
#
# BOTH indexes are checked now, not just owner-index: _ai_owner_tasks reads
# the assignee-user-index too (that is how a delegated task reaches the
# assistant at all), so a policy covering only owner-index would reintroduce
# the dashboard/AI disagreement in production while every test passed.
# -------------------------------------------------------------
USERAPI_ROLE_ARN="$(aws lambda get-function --function-name "$USERAPI_LAMBDA_NAME" \
                      --query 'Configuration.Role' --output text)"
USERAPI_ROLE_NAME="${USERAPI_ROLE_ARN##*/}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

for index in "owner-index" "assignee-user-index"; do
  arn="arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${TASKS_TABLE}/index/${index}"
  DECISION="$(aws iam simulate-principal-policy \
      --policy-source-arn "$USERAPI_ROLE_ARN" \
      --action-names "dynamodb:Query" \
      --resource-arns "$arn" \
      --query 'EvaluationResults[0].EvalDecision' --output text 2>/dev/null || echo "unknown")"
  case "$DECISION" in
    allowed)
      echo ">> dynamodb:Query on ${TASKS_TABLE}/index/${index}: allowed." ;;
    unknown)
      echo ">> WARNING: couldn't simulate the policy (needs iam:SimulatePrincipalPolicy)."
      echo "   Verify by hand that $USERAPI_ROLE_NAME can Query ${TASKS_TABLE}/index/${index}." ;;
    *)
      echo "ERROR: $USERAPI_ROLE_NAME is DENIED dynamodb:Query on ${index}." >&2
      if [[ "$index" == "assignee-user-index" ]]; then
        echo "       Delegated tasks would be invisible to the AI while the" >&2
        echo "       dashboard still lists them — the exact inconsistency this" >&2
        echo "       release fixes. Run scripts/44_add_assignee_user_index.sh." >&2
      else
        echo "       The assistant would answer 'you have no tasks' for every user." >&2
        echo "       Re-run scripts/33_deploy_workspace_org.sh." >&2
      fi
      exit 1 ;;
  esac
done

echo
echo ">> Done. Verify with:"
echo "     python -m pytest tests/test_ai_task_dashboard.py -q   # dashboard AI"
echo "     python -m pytest tests/test_ai_consistency.py -q      # dashboard == AI"
echo "     python -m pytest tests/ -q                            # full suite"
echo
echo ">> Live smoke test (needs a token from POST /login):"
echo "     curl -sS -X POST \"\$API_URL/ai/task-intelligence\" \\"
echo "       -H \"Authorization: Bearer \$TOKEN\" -H 'Content-Type: application/json' \\"
echo "       -d '{}'"
echo
echo ">> THE CONSISTENCY CHECK that matters, run as the same user:"
echo "     curl -sS \"\$API_URL/tasks?due_before=\$(date -d '+7 days' +%F)\" \\"
echo "       -H \"Authorization: Bearer \$TOKEN\" | python -c 'import json,sys; print(len(json.load(sys.stdin)[\"tasks\"]))'"
echo "     curl -sS -X POST \"\$API_URL/ai/chat\" \\"
echo "       -H \"Authorization: Bearer \$TOKEN\" -H 'Content-Type: application/json' \\"
echo "       -d '{\"message\":\"What are my upcoming tasks?\"}'"
echo "   The two must agree on the SAME tasks. They did not before this release:"
echo "   the AI compared the raw spoken due_date ('Wednesday') instead of the"
echo "   resolved due_date_normalized the dashboard uses."
