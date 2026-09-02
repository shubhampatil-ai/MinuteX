#!/usr/bin/env bash
# =============================================================
# 37_create_shares_table.sh — the Shares table behind Meeting Share.
#
# One table, one entity, same shape as every other table in this stack
# (Users, Devices, Recordings, Contacts, Folders, Tasks are all separate
# tables), so its GSIs can be reasoned about on their own.
#
#   Shares  (override SHARES_TABLE)
#     PK share_id (S)          "shr_<uuid4hex>"
#
#     GSI token-index    token_hash HASH
#                        -> THE public route's only lookup. GET /share/{token}
#                           arrives holding a token and nothing else, so this
#                           index is what makes that a single indexed query
#                           instead of a table scan. It is keyed on the
#                           sha256 HASH, never on a raw token — the raw token
#                           is never stored anywhere (see below).
#
#     GSI recording-index  recording_key HASH, created_at RANGE
#                        -> "every share link for this meeting", newest first,
#                           for the owner's manage-links screen and for the
#                           active-link ceiling enforced at create time.
#
# WHY token_hash AND NOT token. Possession of the token IS the authorization
# for the public page — there is no account behind it. Storing the raw value
# would mean a table dump hands the reader working links to every shared
# meeting in the product. Only sha256(token) is written, so a dump yields
# nothing usable, and the raw token exists exactly once: in the create
# response. That is also why there is no "resend link" route — the server
# genuinely cannot reconstruct one. See cloud/shared/share_schema.py.
#
# A HASH-only GSI (no range key) is correct for token-index: sha256 is
# effectively unique, so the query returns 0 or 1 item and a sort key would
# add nothing to order.
#
# NO TTL ATTRIBUTE. Expiry is enforced in code (share_schema.is_expired) on
# every public read, NOT by DynamoDB TTL. TTL deletion is best-effort and can
# lag by up to 48 hours, which for an access-control mechanism means a link
# the owner expired staying live for two more days. The rows are tiny and the
# audit trail of an expired-but-present share is worth keeping.
#
# Billing: PAY_PER_REQUEST, matching every other table here.
#
# IAM: the userApi role needs GetItem/PutItem/UpdateItem/Query on this table
# AND on its two indexes — see scripts/iam/. A policy that grants the table
# but forgets index/* makes the public route fail on every request while the
# owner-side routes keep working.
#
# Idempotent: skipped if the table already exists, safe to re-run.
#
# Run:  bash scripts/37_create_shares_table.sh
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

SHARES_TABLE="${SHARES_TABLE:-Shares}"

echo ">> Region: $AWS_REGION"

if aws dynamodb describe-table --table-name "$SHARES_TABLE" >/dev/null 2>&1; then
  STATUS="$(aws dynamodb describe-table --table-name "$SHARES_TABLE" \
              --query 'Table.TableStatus' --output text)"
  echo ">> Table '$SHARES_TABLE' already exists (status: $STATUS). Skipping."
  exit 0
fi

echo ">> Creating table '$SHARES_TABLE' ..."
aws dynamodb create-table \
  --table-name "$SHARES_TABLE" \
  --attribute-definitions \
      AttributeName=share_id,AttributeType=S \
      AttributeName=token_hash,AttributeType=S \
      AttributeName=recording_key,AttributeType=S \
      AttributeName=created_at,AttributeType=S \
  --key-schema AttributeName=share_id,KeyType=HASH \
  --global-secondary-indexes '[
    {"IndexName":"token-index",
     "KeySchema":[{"AttributeName":"token_hash","KeyType":"HASH"}],
     "Projection":{"ProjectionType":"ALL"}},
    {"IndexName":"recording-index",
     "KeySchema":[{"AttributeName":"recording_key","KeyType":"HASH"},
                  {"AttributeName":"created_at","KeyType":"RANGE"}],
     "Projection":{"ProjectionType":"ALL"}}
  ]' \
  --billing-mode PAY_PER_REQUEST >/dev/null

echo "   waiting for '$SHARES_TABLE' to become ACTIVE ..."
aws dynamodb wait table-exists --table-name "$SHARES_TABLE"
echo "   '$SHARES_TABLE' is ACTIVE."

echo
echo ">> Done. Next: bash scripts/38_deploy_meeting_share.sh"
