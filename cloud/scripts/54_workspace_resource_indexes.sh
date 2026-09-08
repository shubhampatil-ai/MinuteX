#!/usr/bin/env bash
# =============================================================
# 54_workspace_resource_indexes.sh — the Phase 2B resource infrastructure.
#
# Creates ONE new table and adds TWO sparse GSIs to existing tables:
#
#   MeetingAccess          PK meeting_id  SK user_id      (new table)
#                          GSI user-index  user_id + meeting_id
#
#   Recordings             + GSI workspace-index  workspace_id + created_at
#   Tasks                  + GSI workspace-index  workspace_id + created_at
#
# NO KEY SCHEMA IS CHANGED AND NO EXISTING INDEX IS TOUCHED.
# Adding a GSI does not alter a table's primary key, does not rewrite items,
# and does not affect any existing query. The three Contacts indexes, the two
# Recordings indexes and the six Tasks indexes all keep working exactly as
# they do today.
#
# WHY THE NEW INDEXES ARE SPARSE, AND WHY THAT IS THE POINT
# ---------------------------------------------------------
# `workspace_id` is written only on rows created from Phase 2B onward. Every
# older row has no such attribute and is therefore ABSENT from the new index.
# That is correct rather than a gap:
#
#   * an unstamped row is PERSONAL to its owner (workspace_schema.
#     resolve_workspace_id), and personal reads still run off the untouched
#     user-index / owner-index — the paths they have always used;
#   * the workspace-index is only ever queried for an ORGANISATION, and no
#     pre-Phase-2B row can belong to one, because organisations did not exist.
#
# So there is no window in which a user sees an empty list, and the backfill
# (55) is an optimization rather than a correctness gate — the same property
# Phase 2A's personal-workspace migration has.
#
# WHY MeetingAccess IS A TABLE AND NOT AN ATTRIBUTE
# -------------------------------------------------
# "May this user reach this meeting" must be ONE point read on an
# authorization path, and the grant list is unbounded (a meeting can be shared
# with many colleagues), so it cannot live in the 400 KB recording row beside
# the transcript, documents and chat history. PK+SK gives the point read;
# user-index answers "which meetings am I granted" for a future screen.
#
# GSI BACKFILL IS ONLINE BUT NOT INSTANT. DynamoDB backfills a new index in
# the background and the table stays fully available throughout. Querying the
# index before it is ACTIVE returns a ValidationException, so this script
# WAITS for both to become ACTIVE before exiting — deploy the code (56) only
# after this finishes. The Lambda also degrades safely if it races (see
# _list_workspace_recordings, which is only reached for an organisation).
#
# ONE INDEX AT A TIME. DynamoDB allows only one GSI creation per table at a
# time, and Recordings/Tasks are different tables, so these two do not
# conflict — but a re-run while one is still building will report
# LimitExceededException, which this script treats as "already in progress".
#
# Prerequisites: 50 (workspace tables), 53 (Phase 2A code).
# Idempotent: every create is guarded by a describe.
#
# ROLLBACK
#   aws dynamodb update-table --table-name Recordings \
#     --global-secondary-index-updates '[{"Delete":{"IndexName":"workspace-index"}}]'
#   (same for Tasks), then delete the MeetingAccess table. Deleting a GSI does
#   not touch table data. Nothing here is destructive.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"
RECORDINGS_TABLE="${RECORDINGS_TABLE:-Recordings}"
TASKS_TABLE="${TASKS_TABLE:-Tasks}"
MEETING_ACCESS_TABLE="${MEETING_ACCESS_TABLE:-MeetingAccess}"
WORKSPACE_INDEX="${RECORDINGS_WORKSPACE_INDEX:-workspace-index}"
MEETING_ACCESS_USER_INDEX="${MEETING_ACCESS_USER_INDEX:-user-index}"

# -------------------------------------------------------------
# 0. Offline tests first — the gate every schema script here applies.
# -------------------------------------------------------------
echo ">> Running the workspace suites (offline) ..."
python -m pytest "$SCRIPT_DIR/../tests/test_workspace_foundation.py" \
                 "$SCRIPT_DIR/../tests/test_workspace_resources.py" -q

# -------------------------------------------------------------
# 1. MeetingAccess.
# -------------------------------------------------------------
if aws dynamodb describe-table --table-name "$MEETING_ACCESS_TABLE" >/dev/null 2>&1; then
  echo ">> Table '$MEETING_ACCESS_TABLE' already exists."
else
  echo ">> Creating '$MEETING_ACCESS_TABLE' ..."
  aws dynamodb create-table \
    --table-name "$MEETING_ACCESS_TABLE" \
    --attribute-definitions \
        AttributeName=meeting_id,AttributeType=S \
        AttributeName=user_id,AttributeType=S \
    --key-schema \
        AttributeName=meeting_id,KeyType=HASH \
        AttributeName=user_id,KeyType=RANGE \
    --global-secondary-indexes "[
      {\"IndexName\":\"${MEETING_ACCESS_USER_INDEX}\",
       \"KeySchema\":[{\"AttributeName\":\"user_id\",\"KeyType\":\"HASH\"},
                    {\"AttributeName\":\"meeting_id\",\"KeyType\":\"RANGE\"}],
       \"Projection\":{\"ProjectionType\":\"ALL\"}}
    ]" \
    --billing-mode PAY_PER_REQUEST >/dev/null
  aws dynamodb wait table-exists --table-name "$MEETING_ACCESS_TABLE"
  echo "   '$MEETING_ACCESS_TABLE' is ACTIVE."

  aws dynamodb update-continuous-backups \
    --table-name "$MEETING_ACCESS_TABLE" \
    --point-in-time-recovery-specification PointInTimeRecoveryEnabled=true \
    >/dev/null 2>&1 || echo "   (PITR already enabled)"
  aws dynamodb update-table --table-name "$MEETING_ACCESS_TABLE" \
    --deletion-protection-enabled >/dev/null 2>&1 \
    || echo "   (deletion protection already enabled)"
fi

# -------------------------------------------------------------
# 2. The two sparse GSIs.
# -------------------------------------------------------------
add_workspace_index() {
  local table="$1"
  local existing
  existing="$(aws dynamodb describe-table --table-name "$table" \
      --query "Table.GlobalSecondaryIndexes[?IndexName=='${WORKSPACE_INDEX}'].IndexName | [0]" \
      --output text 2>/dev/null | tr -d '\r')"
  if [[ -n "$existing" && "$existing" != "None" ]]; then
    echo ">> $table: '$WORKSPACE_INDEX' already exists."
    return 0
  fi

  echo ">> $table: creating '$WORKSPACE_INDEX' ..."
  aws dynamodb update-table \
    --table-name "$table" \
    --attribute-definitions \
        AttributeName=workspace_id,AttributeType=S \
        AttributeName=created_at,AttributeType=S \
    --global-secondary-index-updates "[
      {\"Create\":{
        \"IndexName\":\"${WORKSPACE_INDEX}\",
        \"KeySchema\":[{\"AttributeName\":\"workspace_id\",\"KeyType\":\"HASH\"},
                     {\"AttributeName\":\"created_at\",\"KeyType\":\"RANGE\"}],
        \"Projection\":{\"ProjectionType\":\"ALL\"}}}
    ]" >/dev/null || {
      echo "   (index creation reported an error — it may already be building)"
      return 0
    }
}

wait_for_index() {
  local table="$1"
  echo ">> $table: waiting for '$WORKSPACE_INDEX' to become ACTIVE ..."
  for _ in $(seq 1 120); do   # up to ~20 minutes
    local st
    st="$(aws dynamodb describe-table --table-name "$table" \
        --query "Table.GlobalSecondaryIndexes[?IndexName=='${WORKSPACE_INDEX}'].IndexStatus | [0]" \
        --output text 2>/dev/null | tr -d '\r')"
    if [[ "$st" == "ACTIVE" ]]; then
      echo "   $table: '$WORKSPACE_INDEX' ACTIVE."
      return 0
    fi
    echo "   $table: $st ..."
    sleep 10
  done
  echo "ERROR: $table: '$WORKSPACE_INDEX' did not become ACTIVE in time." >&2
  exit 1
}

add_workspace_index "$RECORDINGS_TABLE"
add_workspace_index "$TASKS_TABLE"
wait_for_index "$RECORDINGS_TABLE"
wait_for_index "$TASKS_TABLE"

# -------------------------------------------------------------
# 3. IAM for the new table (the two GSIs are covered by the existing
#    table+index ARNs already granted on Recordings and Tasks).
# -------------------------------------------------------------
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
USERAPI_ROLE_ARN="$(aws lambda get-function --function-name "$USERAPI_LAMBDA_NAME" \
                      --query 'Configuration.Role' --output text)"
USERAPI_ROLE_NAME="${USERAPI_ROLE_ARN##*/}"
MA_ARN="arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${MEETING_ACCESS_TABLE}"

echo ">> Attaching MinuteXMeetingAccess to $USERAPI_ROLE_NAME ..."
aws iam put-role-policy \
  --role-name "$USERAPI_ROLE_NAME" \
  --policy-name "MinuteXMeetingAccess" \
  --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [{
      \"Effect\": \"Allow\",
      \"Action\": [
        \"dynamodb:GetItem\", \"dynamodb:PutItem\",
        \"dynamodb:UpdateItem\", \"dynamodb:DeleteItem\", \"dynamodb:Query\"
      ],
      \"Resource\": [\"${MA_ARN}\", \"${MA_ARN}/index/*\"]
    }]
  }" >/dev/null
echo ">> Policy attached."

# -------------------------------------------------------------
# 4. Env.
# -------------------------------------------------------------
MERGE_ENV_PY="$SCRIPT_DIR/_merge_env.py"
merge_env() {
  local fn="$1"; shift
  local current merged
  current="$(aws lambda get-function-configuration --function-name "$fn" \
               --query 'Environment.Variables' --output json)"
  merged="$(python "$MERGE_ENV_PY" "$current" "$@")"
  aws lambda update-function-configuration \
    --function-name "$fn" --environment "$merged" \
    --query "FunctionName" --output text >/dev/null
  aws lambda wait function-updated-v2 --function-name "$fn"
  echo ">> $fn env merged: $*"
}

merge_env "$USERAPI_LAMBDA_NAME" \
  "MEETING_ACCESS_TABLE=$MEETING_ACCESS_TABLE" \
  "MEETING_ACCESS_USER_INDEX=$MEETING_ACCESS_USER_INDEX" \
  "RECORDINGS_WORKSPACE_INDEX=$WORKSPACE_INDEX" \
  "TASKS_WORKSPACE_INDEX=$WORKSPACE_INDEX"

echo
echo "=============================================================="
echo " Phase 2B infrastructure ready. No key schema changed."
echo " Next: bash scripts/56_deploy_workspace_resources.sh"
echo "=============================================================="
