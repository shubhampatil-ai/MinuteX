#!/usr/bin/env bash
# =============================================================
# 62_create_org_salesforce_user_links_table.sh — internal-speaker ->
# Salesforce User links (Phase 2D.3).
#
# Table: OrgSalesforceUserLinks (override with ORG_SALESFORCE_USER_LINKS_TABLE)
#   Partition key: workspace_id  (String)
#   Sort key:      user_id       (String)   -- the MinuteX org member's
#                  user_id, NOT a Salesforce id.
#   Billing:       PAY_PER_REQUEST
#
# WHY A SEPARATE TABLE. An "internal" speaker (Rahul, the SM) resolves to a
# MinuteX organisation MEMBER, and that identity needs its own confirmed
# Salesforce User Id — reused across every future meeting rather than
# re-resolved each time (mirrors the point of storing a Contact's
# crm_external_id, see cloud/functions/userapi's Contacts section). This is
# NOT the same relationship WorkspaceMemberships models (role/status — the
# authorization boundary) and NOT the same relationship Contacts models
# (people OUTSIDE the organisation) — a THIRD, narrower relationship,
# so it gets its own table rather than overloading either.
#
#   Item shape (written by resolve_speaker_crm_identity, read by the CRM
#   review/push routes):
#     workspace_id     PK
#     user_id          SK  (the MinuteX member's user_id)
#     sf_user_id       Salesforce User Id (confirmed, never guessed —
#                      see the resolution route's matching-priority rule)
#     sf_username      Salesforce username/email (informational, shown in
#                      CRM Review)
#     resolved_by      user_id who confirmed the match
#     resolved_at      ISO timestamp
#     updated_at       ISO timestamp
#
# No GSI: every access is a direct GetItem/PutItem/DeleteItem by
# (workspace_id, user_id) — there is no "find every workspace a Salesforce
# User belongs to" query needed.
#
# Idempotent: no-op if the table already exists.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

ORG_SALESFORCE_USER_LINKS_TABLE="${ORG_SALESFORCE_USER_LINKS_TABLE:-OrgSalesforceUserLinks}"

echo ">> Region: $AWS_REGION"
echo ">> Table:  $ORG_SALESFORCE_USER_LINKS_TABLE"

if aws dynamodb describe-table --table-name "$ORG_SALESFORCE_USER_LINKS_TABLE" >/dev/null 2>&1; then
  status="$(aws dynamodb describe-table --table-name "$ORG_SALESFORCE_USER_LINKS_TABLE" \
              --query 'Table.TableStatus' --output text)"
  echo ">> Table '$ORG_SALESFORCE_USER_LINKS_TABLE' already exists (status: $status). Nothing to do."
  exit 0
fi

echo ">> Creating table '$ORG_SALESFORCE_USER_LINKS_TABLE' ..."
aws dynamodb create-table \
  --table-name "$ORG_SALESFORCE_USER_LINKS_TABLE" \
  --attribute-definitions \
      AttributeName=workspace_id,AttributeType=S \
      AttributeName=user_id,AttributeType=S \
  --key-schema \
      AttributeName=workspace_id,KeyType=HASH \
      AttributeName=user_id,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST >/dev/null

echo ">> Waiting for table to become ACTIVE ..."
aws dynamodb wait table-exists --table-name "$ORG_SALESFORCE_USER_LINKS_TABLE"
echo ">> Table '$ORG_SALESFORCE_USER_LINKS_TABLE' is ACTIVE."
