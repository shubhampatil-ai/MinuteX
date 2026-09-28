#!/usr/bin/env bash
# =============================================================
# 75_create_push_tokens_table.sh — OS push tokens, one row per installation.
#
# Table: PushTokens  (override with PUSH_TOKENS_TABLE in .env)
#   Partition key: push_token (String)   e.g. "ExponentPushToken[...]"
#   GSI user-index: user_id HASH          (no range key)
#   Billing:        PAY_PER_REQUEST
#
# WHY THE TOKEN IS THE KEY, AND NOT (user_id, installation_id).
#
# A push token addresses ONE physical install. Keying on it makes "a token
# belongs to exactly one account" a property of the table rather than a rule
# every writer must remember: when a second account signs in on the same
# phone and registers, the row is REWRITTEN for that account, and the first
# account can no longer reach that lock screen. With (user_id, installation)
# as the key, the same token could sit under two users at once and both
# accounts' briefs would land on one device.
#
# user-index serves the only other read: "every device of this user", for the
# fan-out when a brief is ready and for sign-out. A user has a handful of rows,
# so no range key is needed.
#
# ITEM SHAPE (written by functions/userapi register_push_token; read and
# deactivated by functions/transcribe through shared/push_delivery.py):
#   push_token        PK. A delivery credential for one device — never logged
#                     whole (push_delivery.mask_token) and never returned by
#                     the API (push_delivery.public_token omits it).
#   user_id           the account this device currently belongs to (JWT sub)
#   installation_id   app-generated id for the install; a rotated token on the
#                     same installation replaces its predecessor
#   platform          ios | android
#   app_version       the app's version at last registration
#   enabled           this device's "Brief-ready notifications" switch — the
#                     sender reads it, so the Settings toggle is real
#   invalid_at        set when Expo reports DeviceNotRegistered; the token is
#                     then skipped until the app registers it again
#   invalid_reason    Expo's error code
#   pending_receipts  Expo ticket ids awaiting a receipt check
#   pending_since     epoch seconds of that send
#   created_at / updated_at / last_seen_at   ISO timestamps
#
# No TTL: a stale row is harmless (it is only ever sent to if still enabled
# and valid, and Expo will report it dead), and a TTL would silently stop
# notifications to someone who simply had not opened the app for a while.
#
# Idempotent: no-op when the table already exists.
# =============================================================
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

PUSH_TOKENS_TABLE="${PUSH_TOKENS_TABLE:-PushTokens}"
echo ">> Region: $AWS_REGION"
echo ">> Table:  $PUSH_TOKENS_TABLE"

if aws dynamodb describe-table --table-name "$PUSH_TOKENS_TABLE" >/dev/null 2>&1; then
  status="$(aws dynamodb describe-table --table-name "$PUSH_TOKENS_TABLE" \
              --query 'Table.TableStatus' --output text)"
  echo ">> Table '$PUSH_TOKENS_TABLE' already exists (status: $status). Skipping."
else
  echo ">> Creating table '$PUSH_TOKENS_TABLE' ..."
  aws dynamodb create-table \
    --table-name "$PUSH_TOKENS_TABLE" \
    --attribute-definitions \
        AttributeName=push_token,AttributeType=S \
        AttributeName=user_id,AttributeType=S \
    --key-schema AttributeName=push_token,KeyType=HASH \
    --global-secondary-indexes '[
      {"IndexName":"user-index",
       "KeySchema":[{"AttributeName":"user_id","KeyType":"HASH"}],
       "Projection":{"ProjectionType":"ALL"}}
    ]' \
    --billing-mode PAY_PER_REQUEST >/dev/null
  echo "   waiting for '$PUSH_TOKENS_TABLE' to become ACTIVE ..."
  aws dynamodb wait table-exists --table-name "$PUSH_TOKENS_TABLE"
  echo "   '$PUSH_TOKENS_TABLE' is ACTIVE."
fi

# Point-in-time recovery, like the other user-data tables (script 51).
aws dynamodb update-continuous-backups \
  --table-name "$PUSH_TOKENS_TABLE" \
  --point-in-time-recovery-specification PointInTimeRecoveryEnabled=true \
  >/dev/null 2>&1 && echo ">> PITR enabled." || echo ">> PITR: could not enable (check permissions)."

echo ">> Done. Next: 76_deploy_push_notifications.sh"
