#!/usr/bin/env bash
# =============================================================
# 57_shared_contact_indexes.sh — the two GSIs shared organisation contacts
# need (Phase 2C).
#
#   Contacts  + GSI workspace-index        workspace_id + created_at
#             + GSI workspace-email-index  workspace_id + email_lc
#
# NO KEY SCHEMA IS CHANGED AND NO EXISTING INDEX IS TOUCHED. The three
# existing Contacts indexes (owner-index, owner-email-index,
# owner-phone-index) keep serving personal contacts exactly as they do today.
#
# WHY workspace-email-index IS NOT OPTIONAL
# -----------------------------------------
# This is the index that makes shared dedupe CORRECT, and its absence was the
# reason Phase 2B declined to ship shared contacts at all.
#
# owner-email-index is partitioned on owner_user_id, so it can only answer
# "does THIS PERSON already have this email". In a shared address book the
# question is "does THIS ORGANISATION already have them" — and two colleagues
# adding the same client would each pass their own owner-scoped check, each
# create a row, and create_contact would report 201 "created" rather than 200
# "existing". That is a data-correctness bug, not a cosmetic one, and no
# amount of post-filtering fixes it: the HASH key forces you to know whose
# namespace to look in before you read.
#
# THERE IS DELIBERATELY NO workspace-PHONE index. Phone is the weaker of the
# two strong identifiers and the rarer input, so the organisation phone path
# filters the workspace page in memory instead (_find_contact_by_phone) rather
# than adding a fifth GSI to the hot contact-create path. Bounded by
# CONTACTS_PAGE_MAX, exactly as the name-matching path already is.
#
# BOTH INDEXES ARE SPARSE. Only rows carrying workspace_id enter them, so
# every pre-Phase-2C contact is absent — which is correct, because those rows
# are personal and owner-index serves them. Nothing needs backfilling for
# personal contacts to keep working.
#
# WHAT DOES NEED A BACKFILL, AND WHAT DOES NOT
# --------------------------------------------
# Contacts created from Phase 2B onward already carry workspace_id (Phase 2B
# stamped them). Contacts created BEFORE that carry none and resolve to their
# owner's personal workspace on read — correct, and they are personal, so they
# never needed to appear in a shared list. So: no backfill is REQUIRED for
# correctness. 55_backfill_resource_workspaces.py already covers Contacts if
# you want the attribute materialized.
#
# ONE INDEX AT A TIME. DynamoDB permits a single GSI creation per table at a
# time, so these two are created and awaited sequentially. Expect this script
# to take a few minutes on a table with real data.
#
# Prerequisites: 54 (the Phase 2B resource indexes), 56 (Phase 2B code).
# Idempotent: each create is guarded by a describe.
#
# ROLLBACK
#   aws dynamodb update-table --table-name Contacts \
#     --global-secondary-index-updates \
#       '[{"Delete":{"IndexName":"workspace-email-index"}}]'
#   (and again for workspace-index). Deleting a GSI does not touch table data.
#   Shared contact listing stops working; personal contacts are unaffected.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"
CONTACTS_TABLE="${CONTACTS_TABLE:-Contacts}"
WS_INDEX="${CONTACTS_WORKSPACE_INDEX:-workspace-index}"
WS_EMAIL_INDEX="${CONTACTS_WORKSPACE_EMAIL_INDEX:-workspace-email-index}"

# -------------------------------------------------------------
# 0. Offline tests first — the gate every schema script here applies.
# -------------------------------------------------------------
echo ">> Running the workspace suites (offline) ..."
python -m pytest "$SCRIPT_DIR/../tests/test_workspace_foundation.py" \
                 "$SCRIPT_DIR/../tests/test_workspace_resources.py" \
                 "$SCRIPT_DIR/../tests/test_workspace_phase2c.py" -q

index_exists() {
  local table="$1" index="$2" found
  found="$(aws dynamodb describe-table --table-name "$table" \
      --query "Table.GlobalSecondaryIndexes[?IndexName=='${index}'].IndexName | [0]" \
      --output text 2>/dev/null | tr -d '\r')"
  [[ -n "$found" && "$found" != "None" ]]
}

wait_for_index() {
  local table="$1" index="$2" st
  echo ">> $table: waiting for '$index' to become ACTIVE ..."
  for _ in $(seq 1 120); do   # up to ~20 minutes
    st="$(aws dynamodb describe-table --table-name "$table" \
        --query "Table.GlobalSecondaryIndexes[?IndexName=='${index}'].IndexStatus | [0]" \
        --output text 2>/dev/null | tr -d '\r')"
    if [[ "$st" == "ACTIVE" ]]; then
      echo "   '$index' ACTIVE."
      return 0
    fi
    echo "   $index: $st ..."
    sleep 10
  done
  echo "ERROR: '$index' did not become ACTIVE in time." >&2
  exit 1
}

# -------------------------------------------------------------
# 1. workspace-index — the shared contact LIST.
# -------------------------------------------------------------
if index_exists "$CONTACTS_TABLE" "$WS_INDEX"; then
  echo ">> $CONTACTS_TABLE: '$WS_INDEX' already exists."
else
  echo ">> $CONTACTS_TABLE: creating '$WS_INDEX' ..."
  aws dynamodb update-table \
    --table-name "$CONTACTS_TABLE" \
    --attribute-definitions \
        AttributeName=workspace_id,AttributeType=S \
        AttributeName=created_at,AttributeType=S \
    --global-secondary-index-updates "[
      {\"Create\":{
        \"IndexName\":\"${WS_INDEX}\",
        \"KeySchema\":[{\"AttributeName\":\"workspace_id\",\"KeyType\":\"HASH\"},
                     {\"AttributeName\":\"created_at\",\"KeyType\":\"RANGE\"}],
        \"Projection\":{\"ProjectionType\":\"ALL\"}}}
    ]" >/dev/null
  wait_for_index "$CONTACTS_TABLE" "$WS_INDEX"
fi

# -------------------------------------------------------------
# 2. workspace-email-index — shared DEDUPE. See the header.
# -------------------------------------------------------------
if index_exists "$CONTACTS_TABLE" "$WS_EMAIL_INDEX"; then
  echo ">> $CONTACTS_TABLE: '$WS_EMAIL_INDEX' already exists."
else
  echo ">> $CONTACTS_TABLE: creating '$WS_EMAIL_INDEX' ..."
  aws dynamodb update-table \
    --table-name "$CONTACTS_TABLE" \
    --attribute-definitions \
        AttributeName=workspace_id,AttributeType=S \
        AttributeName=email_lc,AttributeType=S \
    --global-secondary-index-updates "[
      {\"Create\":{
        \"IndexName\":\"${WS_EMAIL_INDEX}\",
        \"KeySchema\":[{\"AttributeName\":\"workspace_id\",\"KeyType\":\"HASH\"},
                     {\"AttributeName\":\"email_lc\",\"KeyType\":\"RANGE\"}],
        \"Projection\":{\"ProjectionType\":\"ALL\"}}}
    ]" >/dev/null
  wait_for_index "$CONTACTS_TABLE" "$WS_EMAIL_INDEX"
fi

# -------------------------------------------------------------
# 3. Env. The Contacts table + index ARNs are already granted to userApi by
#    31_create_workspace_tables.sh (table/index/*), so no IAM change is
#    needed — a new index on an already-granted table is covered.
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
  "CONTACTS_WORKSPACE_INDEX=$WS_INDEX" \
  "CONTACTS_WORKSPACE_EMAIL_INDEX=$WS_EMAIL_INDEX"

echo
echo "=============================================================="
echo " Shared-contact indexes ready. No key schema changed, and the"
echo " three existing Contacts indexes are untouched."
echo
echo " Deploy the Phase 2C code next (re-run 56, which packages the"
echo " current lambda_function.py and shared modules)."
echo "=============================================================="
