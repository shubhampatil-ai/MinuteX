#!/usr/bin/env bash
# =============================================================
# 22_create_crm_connections_table.sh — per-user CRM OAuth connections.
#
# Table: CrmConnections  (override with CRM_CONNECTIONS_TABLE in .env)
#   Partition key: user_id  (String)
#   Sort key:      provider (String)   -- "salesforce" today; SK leaves
#                  room for hubspot/zoho later without a schema change,
#                  even though only salesforce is wired up (see plan:
#                  generic CRM support is explicitly NOT MVP scope).
#   Billing:       PAY_PER_REQUEST
#
#   Item shape (written by functions/userapi's salesforce_callback, read by
#   salesforce_status/salesforce_disconnect):
#     user_id             PK
#     provider            SK ("salesforce")
#     instance_url        e.g. https://yourorg.my.salesforce.com
#     refresh_token_enc   KMS ciphertext (base64url), never the raw token
#     org_id              Salesforce org id (informational)
#     sf_user_id          Salesforce user id (informational)
#     sf_username         Salesforce username/email (shown in the app)
#     connected_at        ISO timestamp
#     updated_at          ISO timestamp
#
# No GSI: every access is a direct GetItem/PutItem/DeleteItem by
# (user_id, provider) — there is no "find all users connected to org X"
# query in the MVP.
#
# Idempotent: no-op if the table already exists.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

CRM_CONNECTIONS_TABLE="${CRM_CONNECTIONS_TABLE:-CrmConnections}"

echo ">> Region: $AWS_REGION"
echo ">> Table:  $CRM_CONNECTIONS_TABLE"

if aws dynamodb describe-table --table-name "$CRM_CONNECTIONS_TABLE" >/dev/null 2>&1; then
  status="$(aws dynamodb describe-table --table-name "$CRM_CONNECTIONS_TABLE" \
              --query 'Table.TableStatus' --output text)"
  echo ">> Table '$CRM_CONNECTIONS_TABLE' already exists (status: $status). Nothing to do."
  exit 0
fi

echo ">> Creating table '$CRM_CONNECTIONS_TABLE' ..."
aws dynamodb create-table \
  --table-name "$CRM_CONNECTIONS_TABLE" \
  --attribute-definitions \
      AttributeName=user_id,AttributeType=S \
      AttributeName=provider,AttributeType=S \
  --key-schema \
      AttributeName=user_id,KeyType=HASH \
      AttributeName=provider,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST >/dev/null

echo ">> Waiting for table to become ACTIVE ..."
aws dynamodb wait table-exists --table-name "$CRM_CONNECTIONS_TABLE"
echo ">> Table '$CRM_CONNECTIONS_TABLE' is ACTIVE."
