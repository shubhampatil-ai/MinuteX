#!/usr/bin/env bash
# =============================================================
# 60_create_org_crm_connections_table.sh — Organisation-scoped CRM OAuth
# connections (Phase 2D.1).
#
# Table: OrgCrmConnections  (override with ORG_CRM_CONNECTIONS_TABLE in .env)
#   Partition key: workspace_id  (String)
#   Sort key:      provider     (String)   -- "salesforce" today, same
#                  open-ended SK as CrmConnections (scripts/22).
#   Billing:       PAY_PER_REQUEST
#
# DELIBERATELY A SEPARATE TABLE FROM CrmConnections (scripts/22), not a
# repurposed row in it: Personal Salesforce is keyed by user_id and must
# keep working byte-for-byte unchanged. Mixing "user_id" and "workspace_id"
# into the same partition key would make every reader guess which kind of
# id it has; two tables make the boundary a schema fact instead of a
# runtime check.
#
#   Item shape (written by functions/userapi's org_salesforce_callback, read
#   by org_salesforce_status/org_salesforce_disconnect/_sf_call variants):
#     workspace_id        PK  (organisation workspace id, "org_...")
#     provider            SK  ("salesforce")
#     connected_by_user_id  who completed the OAuth flow (audit only — the
#                            connection is used by the WORKSPACE, not by them)
#     instance_url         e.g. https://yourorg.my.salesforce.com
#     refresh_token_enc    KMS ciphertext (base64url), never the raw token
#     org_id                Salesforce org id (informational)
#     sf_user_id             Salesforce user id of the connecting identity
#     sf_username             Salesforce username/email (shown in the app)
#     connected_at          ISO timestamp
#     updated_at            ISO timestamp
#     config                 {mappings:[...], updated_at} — same shape as
#                             Personal's CrmConnections.config
#
# No GSI: every access is a direct GetItem/PutItem/DeleteItem by
# (workspace_id, provider) — same rationale as CrmConnections.
#
# Reuses the EXISTING Salesforce Connected App (SALESFORCE_CLIENT_ID /
# SALESFORCE_CLIENT_SECRET_ARN) and the EXISTING KMS key
# (SALESFORCE_KMS_KEY_ID) — see 61_deploy_org_salesforce_oauth.sh, which
# grants userApi's role access to THIS table only; no new secret, no new key.
#
# Idempotent: no-op if the table already exists.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

ORG_CRM_CONNECTIONS_TABLE="${ORG_CRM_CONNECTIONS_TABLE:-OrgCrmConnections}"

echo ">> Region: $AWS_REGION"
echo ">> Table:  $ORG_CRM_CONNECTIONS_TABLE"

if aws dynamodb describe-table --table-name "$ORG_CRM_CONNECTIONS_TABLE" >/dev/null 2>&1; then
  status="$(aws dynamodb describe-table --table-name "$ORG_CRM_CONNECTIONS_TABLE" \
              --query 'Table.TableStatus' --output text)"
  echo ">> Table '$ORG_CRM_CONNECTIONS_TABLE' already exists (status: $status). Nothing to do."
  exit 0
fi

echo ">> Creating table '$ORG_CRM_CONNECTIONS_TABLE' ..."
aws dynamodb create-table \
  --table-name "$ORG_CRM_CONNECTIONS_TABLE" \
  --attribute-definitions \
      AttributeName=workspace_id,AttributeType=S \
      AttributeName=provider,AttributeType=S \
  --key-schema \
      AttributeName=workspace_id,KeyType=HASH \
      AttributeName=provider,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST >/dev/null

echo ">> Waiting for table to become ACTIVE ..."
aws dynamodb wait table-exists --table-name "$ORG_CRM_CONNECTIONS_TABLE"
echo ">> Table '$ORG_CRM_CONNECTIONS_TABLE' is ACTIVE."
