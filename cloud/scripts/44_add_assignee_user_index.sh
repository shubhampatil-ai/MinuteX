#!/usr/bin/env bash
# =============================================================
# 44_add_assignee_user_index.sh — the GSI that lets an ASSIGNEE see their work.
#
# THE PROBLEM. MinuteX's Tasks table is partitioned by CREATOR
# (`owner_user_id`), and every task query ran through the owner-index. That is
# correct for "tasks I created" and silently wrong for "tasks I must do": a
# task User A creates and assigns to User B lives in A's partition, so B's
# dashboard could never return it. B was the one person who actually had to do
# the work, and it was invisible to them.
#
# WHY A NEW INDEX IS GENUINELY NEEDED. The existing `assignee-index` is keyed
# on `assignee_contact_id` — a row in somebody's ADDRESS BOOK, not a MinuteX
# account. Two different users each have their own contact record for the same
# person, so a contact id cannot answer "tasks assigned to my account", and it
# is not an identity that can authenticate. The only field that means "this
# authenticated user must do this task" is `assignee_user_id`, and no index
# was keyed on it. Post-filtering is not an option either: B's tasks are in
# A's partition, so finding them without an index means a full table scan.
#
#   GSI assignee-user-index   assignee_user_id HASH, created_at RANGE
#                             Projection ALL (the list returns whole tasks)
#
# SPARSE BY CONSTRUCTION. `assignee_user_id` is already in _TASK_SPARSE_KEYS,
# so it is written only when a task is assigned to a contact LINKED to a real
# account, and omitted otherwise. Unassigned tasks and tasks assigned to
# non-MinuteX contacts never enter this index — it stays small and costs
# nothing for them.
#
# NO SCHEMA CHANGE, NO BACKFILL. This adds an access path over an attribute
# the table already writes. Existing rows carrying assignee_user_id are
# indexed by DynamoDB automatically as it backfills; rows without it are
# correctly absent.
#
# ORDERING MATTERS. Run this BEFORE deploying the userApi code that queries
# the index. Querying a GSI that does not exist is a ValidationException. The
# Lambda degrades gracefully (it logs and falls back to creator-only tasks
# rather than 500-ing the dashboard), but that is a safety net, not a plan.
#
# Prerequisites:
#   - scripts/31_create_workspace_tables.sh   (the Tasks table itself)
#
# Idempotent: an existing index is detected and left alone.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

TASKS_TABLE="${TASKS_TABLE:-Tasks}"
INDEX_NAME="${TASKS_ASSIGNEE_USER_INDEX:-assignee-user-index}"

echo ">> Table: $TASKS_TABLE"
echo ">> Index: $INDEX_NAME"

# -------------------------------------------------------------
# 0. Offline tests first — the same gate scripts 42/43 apply.
# -------------------------------------------------------------
echo ">> Running the task permission suite (offline) ..."
( cd "$PROJECT_ROOT" && python -m pytest tests/test_task_permissions.py -q )
echo ">> Running the task suites it must not regress ..."
( cd "$PROJECT_ROOT" && python -m pytest tests/test_task_extraction.py \
    tests/test_eager_task_seeding.py -q )

# -------------------------------------------------------------
# 1. Is it already there? Adding an existing index is an error, so this
#    script has to be able to run twice.
# -------------------------------------------------------------
EXISTING="$(aws dynamodb describe-table \
  --table-name "$TASKS_TABLE" \
  --query "Table.GlobalSecondaryIndexes[?IndexName=='$INDEX_NAME'].IndexStatus" \
  --output text 2>/dev/null || true)"

if [ -n "$EXISTING" ] && [ "$EXISTING" != "None" ]; then
  echo ">> $INDEX_NAME already exists (status: $EXISTING) — nothing to do."
else
  echo ">> Creating $INDEX_NAME ..."
  # DynamoDB allows ONE index creation at a time per table; if another is
  # still backfilling this call fails loudly rather than half-applying.
  aws dynamodb update-table \
    --table-name "$TASKS_TABLE" \
    --attribute-definitions \
        AttributeName=assignee_user_id,AttributeType=S \
        AttributeName=created_at,AttributeType=S \
    --global-secondary-index-updates "$(cat <<JSON
[{"Create":{"IndexName":"$INDEX_NAME",
            "KeySchema":[{"AttributeName":"assignee_user_id","KeyType":"HASH"},
                         {"AttributeName":"created_at","KeyType":"RANGE"}],
            "Projection":{"ProjectionType":"ALL"}}}]
JSON
)" >/dev/null
  echo ">> Create issued."
fi

# -------------------------------------------------------------
# 2. Wait for the backfill. The index is NOT queryable until it is ACTIVE,
#    so deploying the code before this finishes would break the dashboard.
# -------------------------------------------------------------
echo ">> Waiting for $INDEX_NAME to become ACTIVE (this can take minutes) ..."
for _ in $(seq 1 60); do
  STATUS="$(aws dynamodb describe-table \
    --table-name "$TASKS_TABLE" \
    --query "Table.GlobalSecondaryIndexes[?IndexName=='$INDEX_NAME'].IndexStatus" \
    --output text 2>/dev/null || true)"
  echo "   status: ${STATUS:-<none>}"
  [ "$STATUS" = "ACTIVE" ] && break
  sleep 10
done

if [ "$STATUS" != "ACTIVE" ]; then
  echo "WARNING: $INDEX_NAME is still $STATUS. Do NOT deploy the userApi code" >&2
  echo "         that queries it until this reports ACTIVE." >&2
  exit 1
fi

echo ""
echo ">> Done. $INDEX_NAME is ACTIVE."
echo "   Assignees can now see tasks created for them by other users."
echo "   Next: deploy userApi (scripts/33_deploy_workspace_org.sh) so the"
echo "   permission model and the dashboard query go live together."
