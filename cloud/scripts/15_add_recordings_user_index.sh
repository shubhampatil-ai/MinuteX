#!/usr/bin/env bash
# =============================================================
# 15_add_recordings_user_index.sh — user-ownership GSI on Recordings.
#
# Adds GSI user-index (user_id HASH, created_at RANGE) to the
# Recordings table so the userApi can answer "all recordings owned
# by user X, newest first" without a scan.
#
# Sparse by design: legacy rows uploaded before the user-owned
# refactor have no user_id and simply don't appear — they stay
# reachable through the existing device-index until an unpair
# stamps them (see functions/userapi, _stamp_user_on_legacy_recordings).
#
# Idempotent: no-op if the index already exists.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

REC_TABLE="${RECORDINGS_TABLE:-recordings}"
INDEX_NAME="${USER_INDEX:-user-index}"

echo ">> Region: $AWS_REGION"
echo ">> Table:  $REC_TABLE"
echo ">> Index:  $INDEX_NAME"

existing="$(aws dynamodb describe-table --table-name "$REC_TABLE" \
              --query "Table.GlobalSecondaryIndexes[?IndexName=='${INDEX_NAME}'] | [0].IndexName" \
              --output text)"
if [[ "$existing" == "$INDEX_NAME" ]]; then
  echo ">> Index '$INDEX_NAME' already exists. Nothing to do."
  exit 0
fi

echo ">> Adding GSI '$INDEX_NAME' ..."
aws dynamodb update-table \
  --table-name "$REC_TABLE" \
  --attribute-definitions \
      AttributeName=user_id,AttributeType=S \
      AttributeName=created_at,AttributeType=S \
  --global-secondary-index-updates '[{
      "Create": {
        "IndexName": "'"$INDEX_NAME"'",
        "KeySchema": [
          {"AttributeName": "user_id",    "KeyType": "HASH"},
          {"AttributeName": "created_at", "KeyType": "RANGE"}
        ],
        "Projection": {"ProjectionType": "ALL"}
      }
  }]' >/dev/null

echo ">> Index creation started (backfills in the background)."
echo ">> Check status with:"
echo "   aws dynamodb describe-table --table-name $REC_TABLE \\"
echo "     --query \"Table.GlobalSecondaryIndexes[?IndexName=='$INDEX_NAME'].IndexStatus\""
