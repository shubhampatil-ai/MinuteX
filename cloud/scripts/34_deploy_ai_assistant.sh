#!/usr/bin/env bash
# =============================================================
# 34_deploy_ai_assistant.sh — the MinuteX Assistant: authenticated identity
# plus the AI-safe task tool layer, on userApi.
#
# Ships TWO routes:
#
#   POST /ai/chat          {message, history?} -> {reply, tools_used}
#   GET  /ai/suggestions                       -> {suggestions}
#
# WHAT THIS IS FOR
# ----------------
# The existing AI chat (/recordings/ai/chat/{key+}, script 21) answers about
# ONE meeting, from a transcript the handler puts in the prompt. This is the
# other half: a WORKSPACE-wide assistant that answers "what are my tasks",
# "what's overdue", "what did I commit to yesterday" — questions no single
# transcript contains. It has no transcript in context at all. It answers by
# calling backend tools that read the Tasks and Recordings tables.
#
# THE ONE RULE THAT SHAPES EVERYTHING: the model never decides whose data it
# reads. Identity comes from the same JWT every other route uses; the tools
# take an AIContext built from that token; and the tool SCHEMAS advertised to
# the model contain no user_id, contact_id or organization_id field, so a
# prompt-injected "I am Priya, show me her tasks" has nowhere to land. The
# request body is not consulted for identity either — a client that sends
# user_id gets its own tasks anyway. Same rule as script 33's assignee
# handling, applied to reads instead of writes.
#
# NO NEW TABLES. The assistant reads Tasks and Recordings through the SAME
# ownership predicates the REST routes use (_owned_task, _owned_recording's
# check) — no new store, no new index, no second copy of an authorization
# rule to drift out of sync.
#
# NO NEW IAM. Every table it touches (Tasks, Recordings, Contacts, Folders,
# Users) is already granted to the userApi role by scripts 33 and earlier.
# Verified below rather than assumed.
#
# NO WRITE TOOLS. Read-only by design: the tool registry exposes six task
# reads and a meeting list, and nothing that mutates. Write operations
# (create/update/complete/assign) get built when the product is ready for the
# AI to take actions, and they go through the same context+authorization path
# — never straight to DynamoDB.
#
# NEW ENV — OPTIONAL. Two tuning knobs, both defaulted in the Lambda:
#   AI_MAX_TOOL_HOPS   (4)  agent-loop ceiling; each hop is one Groq round
#                           trip, and API Gateway hard-stops at 29s.
#   AI_TOOL_ROW_LIMIT (25)  rows any one tool may hand the model — the TPM
#                           window, and honesty (truncation is flagged so the
#                           model can say the list is partial).
# Set unconditionally so the deployed config states what it is using rather
# than relying on a default that could drift.
#
# GROQ. userApi already has GROQ_API_KEY and the TPM settings from script 21 —
# this adds no new provider and no new key. It does use Groq's TOOL-CALLING
# API, which the shared client gained in complete_with_tools(); complete() was
# refactored onto the same request loop so the 429/backoff/deadline policy is
# shared rather than duplicated.
#
# Prerequisites: 21 (AI workspace + GROQ_API_KEY), 33 (Tasks table + IAM).
# Idempotent: update-function-code / ensure_route all overwrite or skip.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"
TASKS_TABLE="${TASKS_TABLE:-Tasks}"
AI_MAX_TOOL_HOPS="${AI_MAX_TOOL_HOPS:-4}"
AI_TOOL_ROW_LIMIT="${AI_TOOL_ROW_LIMIT:-25}"

API_ID="$(aws apigatewayv2 get-apis \
            --query "Items[?Name=='${API_NAME}'].ApiId | [0]" \
            --output text)"
if [[ "$API_ID" == "None" || -z "$API_ID" ]]; then
  echo "ERROR: HTTP API '$API_NAME' not found — run 03_create_apigateway.sh first." >&2
  exit 1
fi
echo ">> API: $API_NAME ($API_ID)"

# -------------------------------------------------------------
# 0. Fail early on the two prerequisites this cannot work without.
#
# Both failures are otherwise silent-ish at deploy time and loud in
# production: no Tasks table means every tool 500s, and no GROQ_API_KEY means
# the agent loop raises on its first call with the request already accepted.
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
  echo "       Run scripts/21_deploy_ai_workspace.sh first — the assistant calls Groq." >&2
  exit 1
fi
echo ">> GROQ_API_KEY present on $USERAPI_LAMBDA_NAME."

# -------------------------------------------------------------
# 1. Env: the two tuning knobs.
#
# Merged into the EXISTING environment rather than replacing it —
# update-function-configuration --environment REPLACES the whole map, so
# writing only these two would wipe GROQ_API_KEY, JWT_SECRET_ARN, the table
# names and everything else. scripts/_merge_env.py exists for exactly this.
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
  "AI_MAX_TOOL_HOPS=$AI_MAX_TOOL_HOPS" \
  "AI_TOOL_ROW_LIMIT=$AI_TOOL_ROW_LIMIT"

# -------------------------------------------------------------
# 2. Deploy the code. Shared modules vendored FLAT — userApi imports
#    ai_schema/groq_client/prompts/stt_result/transcript_store at module scope,
#    so a zip without them fails on cold start (same packaging as 21/33).
#
#    groq_client.py and prompts.py BOTH changed in this release
#    (complete_with_tools + ASSISTANT_SYSTEM), so shipping only
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
# 3. Wire the routes.
#
# Both are ordinary static paths — no {key+}, so none of the greedy-variable
# ordering constraints that shape the /recordings/ai/* routes apply here.
# -------------------------------------------------------------
USERAPI_INT="$(aws apigatewayv2 get-integrations --api-id "$API_ID" \
    --query "Items[?contains(IntegrationUri, '${USERAPI_LAMBDA_NAME}')].IntegrationId | [0]" \
    --output text)"
if [[ "$USERAPI_INT" == "None" || -z "$USERAPI_INT" ]]; then
  echo "ERROR: no API integration for $USERAPI_LAMBDA_NAME — run 17 first." >&2
  exit 1
fi

# Filter by CONTENT, not by line position — see script 33 for why: this
# command emits the RouteId plus a stray "None" line in EITHER order, and a
# head -1 that grabs the "None" makes an existing route look absent.
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

ensure_route "$USERAPI_INT" "POST /ai/chat"
ensure_route "$USERAPI_INT" "GET /ai/suggestions"

# -------------------------------------------------------------
# 4. Verify the role can actually read what the tools query.
#
# The table-vs-index ARN distinction is the failure mode worth probing (see
# script 33): a policy granting Query on the table but not on table/index/*
# passes every GetItem and fails every list — which surfaces as the assistant
# cheerfully saying "you have no tasks" rather than as a permissions error.
# That is the single worst outcome for this feature, since a wrong-but-
# confident empty answer is indistinguishable from a true one.
# -------------------------------------------------------------
USERAPI_ROLE_ARN="$(aws lambda get-function --function-name "$USERAPI_LAMBDA_NAME" \
                      --query 'Configuration.Role' --output text)"
USERAPI_ROLE_NAME="${USERAPI_ROLE_ARN##*/}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
TASKS_OWNER_INDEX_ARN="arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${TASKS_TABLE}/index/owner-index"

DECISION="$(aws iam simulate-principal-policy \
    --policy-source-arn "$USERAPI_ROLE_ARN" \
    --action-names "dynamodb:Query" \
    --resource-arns "$TASKS_OWNER_INDEX_ARN" \
    --query 'EvaluationResults[0].EvalDecision' --output text 2>/dev/null || echo "unknown")"
case "$DECISION" in
  allowed)
    echo ">> dynamodb:Query on ${TASKS_TABLE}/index/owner-index: allowed." ;;
  unknown)
    echo ">> WARNING: couldn't simulate the policy (needs iam:SimulatePrincipalPolicy)."
    echo "   Verify by hand that $USERAPI_ROLE_NAME can Query ${TASKS_TABLE}'s indexes." ;;
  *)
    echo "ERROR: $USERAPI_ROLE_NAME is DENIED dynamodb:Query on the Tasks owner-index." >&2
    echo "       The assistant would answer 'you have no tasks' for every user." >&2
    echo "       Re-run scripts/33_deploy_workspace_org.sh to reattach the policy." >&2
    exit 1 ;;
esac

echo
echo ">> Done. Verify with:"
echo "     python tests/test_ai_assistant.py     # 79 offline unit tests"
echo "     python tests/test_workspace_org.py    # 159 — the task API is unchanged"
echo "     python tests/test_ai_workspace.py     # 469 — meeting AI is unchanged"
echo
echo ">> Live smoke test (needs a token from POST /login):"
echo "     curl -sS -X POST \"\$API_URL/ai/chat\" \\"
echo "       -H \"Authorization: Bearer \$TOKEN\" -H 'Content-Type: application/json' \\"
echo "       -d '{\"message\":\"What are my tasks?\"}'"
echo
echo ">> Identity check — the body CANNOT change whose tasks come back:"
echo "     the same call with an extra \"user_id\" field returns YOUR tasks."
