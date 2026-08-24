#!/usr/bin/env bash
# =============================================================
# 14_create_devices_table.sh — device lifecycle/ownership table.
#
# Table: Devices  (override with DEVICES_TABLE in .env)
#   Partition key: device_id (String)
#   Billing:       PAY_PER_REQUEST
#   GSI:           paired-user-index (paired_user_id HASH)
#                  -> "all devices owned by user X" without a scan.
#                  Sparse: UNPAIRED rows have no paired_user_id and
#                  never appear in the index.
#
#   Item shape (created by scripts/provision_device.py, mutated by
#   the userApi pairing endpoints and the getUploadUrl last_seen
#   touch):
#     device_id           PK
#     status              UNPAIRED | PAIRING | PAIRED
#     paired_user_id      (only while PAIRED)
#     paired_at           ISO timestamp (only while PAIRED)
#     last_seen           ISO timestamp (any authenticated device call)
#     firmware_version    string
#     serial_number       string
#     pairing_code_hash / pairing_expires_at / pairing_user_id
#                         (transient, only while PAIRING)
#     created_at          ISO timestamp
#
# This table is OWNERSHIP only. Authentication stays in DeviceKeys
# (apiKey -> deviceId) — the fixed firmware contract is untouched.
#
# After creating it, backfill rows for existing devices with:
#   python scripts/16_backfill_devices.py [--adopt-claims]
#
# IAM note: the userApi role needs dynamodb Get/Put/Update/Query on
# this table + its index; the getUploadUrl role needs Get/Update.
#
# Idempotent: no-op if the table already exists.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

DEVICES_TABLE="${DEVICES_TABLE:-Devices}"

echo ">> Region: $AWS_REGION"
echo ">> Table:  $DEVICES_TABLE"

if aws dynamodb describe-table --table-name "$DEVICES_TABLE" >/dev/null 2>&1; then
  status="$(aws dynamodb describe-table --table-name "$DEVICES_TABLE" \
              --query 'Table.TableStatus' --output text)"
  echo ">> Table '$DEVICES_TABLE' already exists (status: $status). Nothing to do."
  exit 0
fi

echo ">> Creating table '$DEVICES_TABLE' ..."
aws dynamodb create-table \
  --table-name "$DEVICES_TABLE" \
  --attribute-definitions \
      AttributeName=device_id,AttributeType=S \
      AttributeName=paired_user_id,AttributeType=S \
  --key-schema AttributeName=device_id,KeyType=HASH \
  --global-secondary-indexes '[{
      "IndexName": "paired-user-index",
      "KeySchema": [{"AttributeName": "paired_user_id", "KeyType": "HASH"}],
      "Projection": {"ProjectionType": "ALL"}
  }]' \
  --billing-mode PAY_PER_REQUEST >/dev/null

echo ">> Waiting for table to become ACTIVE ..."
aws dynamodb wait table-exists --table-name "$DEVICES_TABLE"
echo ">> Table '$DEVICES_TABLE' is ACTIVE."
echo ">> Backfill existing devices with:  python scripts/16_backfill_devices.py"
