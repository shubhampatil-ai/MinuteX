#!/usr/bin/env bash
# =============================================================
# 49_create_chat_sessions_table.sh — persistent workspace AI conversations.
#
# Creates ONE table and wires the three session routes plus the extended
# /ai/chat onto userApi:
#
#   POST   /ai/chat                          {message, session_id?, history?}
#   GET    /ai/chat/sessions                 -> {sessions, count, next_cursor}
#   GET    /ai/chat/sessions/{session_id}    -> {session}
#   DELETE /ai/chat/sessions/{session_id}    -> {deleted, session_id}
#
# WHY ONE TABLE AND NOT TWO
# -------------------------
# The textbook shape for a chat log is ChatSessions + ChatMessages with a
# composite key. Two findings from the codebase ruled it out:
#
#   1. MinuteX HAS NO COMPOSITE PK+SK TABLE. All sixteen existing tables are
#      a single HASH key plus GSIs for listing. Notifications is the closest
#      analogue and the model followed here: PK notification_id, GSI
#      user_id+created_at. A PK/SK table would be the first of its kind, for
#      a feature whose per-session volume is a few dozen short turns.
#   2. THE MEETING CHAT ALREADY SOLVES THIS IN PRODUCTION, by storing a
#      bounded `chat_history` list on the recording row and trimming it to
#      MAX_CHAT_TURNS. Same problem, same scale, already proven here.
#
# So a session is ONE item holding a bounded turn list.
#
# THE 400 KB ITEM LIMIT IS RESPECTED BY CONSTRUCTION, not by hoping:
#
#      MAX_SESSION_TURNS       40 turns (= 20 exchanges)
#    x MAX_AI_MESSAGE_CHARS  2 000 per user turn
#    + MAX_STORED_REPLY_CHARS 4 000 per assistant turn
#    -------------------------------------------------
#      worst case ~123 KB, measured — comfortably inside 400 KB
#
# _append_turns is the ONLY writer of `turns` and slices before every write,
# so no code path can append without trimming. Verified by test
# `test_the_item_cannot_grow_without_bound`, which sends 60 max-size
# exchanges and asserts the stored item stays bounded.
#
# NO TTL, DELIBERATELY. Spec section 13 allows one but asks for nothing
# aggressive. A conversation is user content — the kind of thing someone
# expects to still be there next month — and silently deleting it after N
# days would be a surprise, not a policy. Growth is already bounded per
# session, and DELETE gives the user an explicit way to remove one. If a
# retention policy is wanted later, enable TTL on an `expires_at_epoch`
# attribute (see 41_create_notifications_table.sh for the pattern) and
# document the period in the product, not just in code.
#
# NO NEW IAM BEYOND THIS TABLE. userApi's role needs Query/GetItem/PutItem/
# UpdateItem/DeleteItem on the table and Query on its index; the policy is
# attached below and verified by simulation.
#
# Prerequisites: 34 (POST /ai/chat exists), 48 (task-dashboard AI).
# Idempotent: the table, the routes and the policy are all create-or-skip.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"
CHAT_SESSIONS_TABLE="${CHAT_SESSIONS_TABLE:-ChatSessions}"
CHAT_SESSIONS_USER_INDEX="${CHAT_SESSIONS_USER_INDEX:-user-index}"
MAX_SESSION_TURNS="${MAX_SESSION_TURNS:-40}"

# -------------------------------------------------------------
# 1. The table.
#
# KEY SCHEMA
#   PK  session_id   (S)  — the item is the whole conversation
#   GSI user-index        — user_id (HASH) + updated_at (RANGE)
#
# WHY updated_at IS THE RANGE KEY. The session list is "my conversations,
# most recently updated first", which is then the INDEX's own order — no
# in-memory sort, and Limit actually bounds the work rather than bounding
# what survives a sort. created_at would have needed a sort on every read.
#
# PROJECTION IS KEYS_ONLY... no: ALL. The list route returns title,
# message_count and last_message_preview, so a KEYS_ONLY projection would
# force a GetItem per row — N+1 reads for a list. ALL costs storage on
# attributes that are already small (turns is the only large one, and a list
# never renders it, but excluding a single attribute is not expressible in
# a GSI projection alongside the rest, so ALL is the honest trade).
# -------------------------------------------------------------
if aws dynamodb describe-table --table-name "$CHAT_SESSIONS_TABLE" >/dev/null 2>&1; then
  echo ">> Table '$CHAT_SESSIONS_TABLE' already exists."
else
  echo ">> Creating '$CHAT_SESSIONS_TABLE' ..."
  aws dynamodb create-table \
    --table-name "$CHAT_SESSIONS_TABLE" \
    --attribute-definitions \
        AttributeName=session_id,AttributeType=S \
        AttributeName=user_id,AttributeType=S \
        AttributeName=updated_at,AttributeType=S \
    --key-schema \
        AttributeName=session_id,KeyType=HASH \
    --global-secondary-indexes "[
      {\"IndexName\":\"${CHAT_SESSIONS_USER_INDEX}\",
       \"KeySchema\":[{\"AttributeName\":\"user_id\",\"KeyType\":\"HASH\"},
                    {\"AttributeName\":\"updated_at\",\"KeyType\":\"RANGE\"}],
       \"Projection\":{\"ProjectionType\":\"ALL\"}}
    ]" \
    --billing-mode PAY_PER_REQUEST >/dev/null

  echo "   waiting for '$CHAT_SESSIONS_TABLE' to become ACTIVE ..."
  aws dynamodb wait table-exists --table-name "$CHAT_SESSIONS_TABLE"
  echo "   '$CHAT_SESSIONS_TABLE' is ACTIVE."
fi

# -------------------------------------------------------------
# 2. IAM. Table AND index ARNs — the distinction matters: a policy granting
#    only the table passes every GetItem and fails every list, which would
#    surface as "you have no conversations" rather than as an error.
# -------------------------------------------------------------
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
TABLE_ARN="arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${CHAT_SESSIONS_TABLE}"
USERAPI_ROLE_ARN="$(aws lambda get-function --function-name "$USERAPI_LAMBDA_NAME" \
                      --query 'Configuration.Role' --output text)"
USERAPI_ROLE_NAME="${USERAPI_ROLE_ARN##*/}"
POLICY_NAME="MinuteXChatSessionsAccess"

echo ">> Attaching $POLICY_NAME to $USERAPI_ROLE_NAME ..."
aws iam put-role-policy \
  --role-name "$USERAPI_ROLE_NAME" \
  --policy-name "$POLICY_NAME" \
  --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [{
      \"Effect\": \"Allow\",
      \"Action\": [
        \"dynamodb:GetItem\",
        \"dynamodb:PutItem\",
        \"dynamodb:UpdateItem\",
        \"dynamodb:DeleteItem\",
        \"dynamodb:Query\"
      ],
      \"Resource\": [
        \"${TABLE_ARN}\",
        \"${TABLE_ARN}/index/*\"
      ]
    }]
  }" >/dev/null
echo ">> Policy attached."

# -------------------------------------------------------------
# 3. Env.
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
  "CHAT_SESSIONS_TABLE=$CHAT_SESSIONS_TABLE" \
  "CHAT_SESSIONS_USER_INDEX=$CHAT_SESSIONS_USER_INDEX" \
  "MAX_SESSION_TURNS=$MAX_SESSION_TURNS"

# -------------------------------------------------------------
# 4. Code.
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
# 5. Routes.
#
# All three are static paths with a NON-greedy {session_id}, so none of the
# greedy-{key+}-must-be-last constraints that shape /recordings/ai/* apply.
# "/ai/chat/sessions" cannot shadow "/ai/chat": API Gateway matches the full
# path, not a prefix.
# -------------------------------------------------------------
API_ID="$(aws apigatewayv2 get-apis \
            --query "Items[?Name=='${API_NAME}'].ApiId | [0]" --output text)"
if [[ "$API_ID" == "None" || -z "$API_ID" ]]; then
  echo "ERROR: HTTP API '$API_NAME' not found — run 03_create_apigateway.sh first." >&2
  exit 1
fi

USERAPI_INT="$(aws apigatewayv2 get-integrations --api-id "$API_ID" \
    --query "Items[?contains(IntegrationUri, '${USERAPI_LAMBDA_NAME}')].IntegrationId | [0]" \
    --output text)"
if [[ "$USERAPI_INT" == "None" || -z "$USERAPI_INT" ]]; then
  echo "ERROR: no API integration for $USERAPI_LAMBDA_NAME — run 17 first." >&2
  exit 1
fi

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
    --api-id "$API_ID" --route-key "$route_key" \
    --target "integrations/$integration_id" >/dev/null
  echo ">> Route created: $route_key"
}

ensure_route "$USERAPI_INT" "GET /ai/chat/sessions"
ensure_route "$USERAPI_INT" "GET /ai/chat/sessions/{session_id}"
ensure_route "$USERAPI_INT" "DELETE /ai/chat/sessions/{session_id}"

# -------------------------------------------------------------
# 6. VERIFY. Adding a handler to _ROUTES is only half a route — a missing
#    API Gateway route 404s before the Lambda is invoked and leaves no
#    CloudWatch trace. That exact gap shipped once (see script 48), so the
#    routes are read back rather than assumed.
# -------------------------------------------------------------
for want in "POST /ai/chat" \
            "GET /ai/chat/sessions" \
            "GET /ai/chat/sessions/{session_id}" \
            "DELETE /ai/chat/sessions/{session_id}"; do
  got="$(aws apigatewayv2 get-routes --api-id "$API_ID" \
      --query "Items[?RouteKey=='${want}'].RouteId | [0]" --output text \
      | tr -d '\r' | grep -v '^None$' | grep -v '^$' | head -1 || true)"
  if [[ -z "$got" ]]; then
    echo "ERROR: route '$want' is missing after wiring. Do not ship this." >&2
    exit 1
  fi
  echo ">> Verified route: $want ($got)"
done

DECISION="$(aws iam simulate-principal-policy \
    --policy-source-arn "$USERAPI_ROLE_ARN" \
    --action-names "dynamodb:Query" \
    --resource-arns "${TABLE_ARN}/index/${CHAT_SESSIONS_USER_INDEX}" \
    --query 'EvaluationResults[0].EvalDecision' --output text 2>/dev/null || echo "unknown")"
case "$DECISION" in
  allowed)
    echo ">> dynamodb:Query on ${CHAT_SESSIONS_TABLE}/index/${CHAT_SESSIONS_USER_INDEX}: allowed." ;;
  unknown)
    echo ">> WARNING: couldn't simulate the policy (needs iam:SimulatePrincipalPolicy)."
    echo "   Verify by hand that $USERAPI_ROLE_NAME can Query the table's index." ;;
  *)
    echo "ERROR: $USERAPI_ROLE_NAME is DENIED Query on the session index." >&2
    echo "       The conversation list would always come back empty." >&2
    exit 1 ;;
esac

echo
echo ">> Done. Verify with:"
echo "     python -m pytest tests/test_ai_chat_sessions.py -q   # sessions"
echo "     python -m pytest tests/ -q                           # full suite"
echo
echo ">> Live smoke test (needs a token from POST /login):"
echo "     # 1. start a conversation and keep its id"
echo "     SID=\$(curl -sS -X POST \"\$API_URL/ai/chat\" \\"
echo "       -H \"Authorization: Bearer \$TOKEN\" -H 'Content-Type: application/json' \\"
echo "       -d '{\"message\":\"What should I work on this week?\"}' \\"
echo "       | python -c 'import json,sys; print(json.load(sys.stdin)[\"session_id\"])')"
echo "     # 2. continue it — the backend supplies the history"
echo "     curl -sS -X POST \"\$API_URL/ai/chat\" \\"
echo "       -H \"Authorization: Bearer \$TOKEN\" -H 'Content-Type: application/json' \\"
echo "       -d \"{\\\"message\\\":\\\"And which is most urgent?\\\",\\\"session_id\\\":\\\"\$SID\\\"}\""
echo "     # 3. list, read back, delete"
echo "     curl -sS \"\$API_URL/ai/chat/sessions\" -H \"Authorization: Bearer \$TOKEN\""
echo "     curl -sS \"\$API_URL/ai/chat/sessions/\$SID\" -H \"Authorization: Bearer \$TOKEN\""
echo "     curl -sS -X DELETE \"\$API_URL/ai/chat/sessions/\$SID\" -H \"Authorization: Bearer \$TOKEN\""
echo
echo ">> BACKWARD COMPATIBILITY: a client that sends no session_id still works"
echo "   exactly as before and simply receives a session_id it may ignore."
