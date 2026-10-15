#!/usr/bin/env bash
# =============================================================
# 77_create_site_delivery_tables.sh — tables for Salesforce Site delivery
# (delivery_type = salesforce_site).
#
# Two NEW tables. Nothing existing is altered: CrmSyncJobs, the CRM sync
# queue/DLQ and the worker are REUSED as they are (the new jobs are ordinary
# CrmSyncJobs rows with their own `operation` values).
#
#   SiteDeliveryEndpoints   PK workspace_id
#       One row per workspace that has configured a Salesforce Site receiver:
#       url, auth_mode, enabled, KMS-encrypted credentials (HMAC secret +
#       audio-link signing key), last health check, people-sync summary.
#       Separate from CrmConnections/OrgCrmConnections on purpose: those are
#       the OAuth architecture's rows and must not change shape or meaning.
#
#   People                  PK workspace_id, SK person_key
#       The generic People Store ("{provider}#{type}#{external_id}"), filled
#       by people_sync and read by participant tagging. No GSI: every read is
#       a single-partition query within one workspace.
#
# Idempotent: creates what is missing, leaves existing tables alone.
# Next: 78_deploy_salesforce_site_delivery.sh
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

SITE_DELIVERY_ENDPOINTS_TABLE="${SITE_DELIVERY_ENDPOINTS_TABLE:-SiteDeliveryEndpoints}"
PEOPLE_TABLE="${PEOPLE_TABLE:-People}"

echo ">> Region: $AWS_REGION"

create_table() {
  local name="$1"; shift
  if aws dynamodb describe-table --table-name "$name" >/dev/null 2>&1; then
    echo ">> Table '$name' already exists. Skipping."
  else
    echo ">> Creating table '$name' ..."
    aws dynamodb create-table --table-name "$name" "$@" \
      --billing-mode PAY_PER_REQUEST >/dev/null
    aws dynamodb wait table-exists --table-name "$name"
    echo "   '$name' is ACTIVE."
  fi
  # Point-in-time recovery, as scripts/51 set on every other table: the
  # endpoint row holds the only copy of the audio-link signing key.
  aws dynamodb update-continuous-backups --table-name "$name" \
    --point-in-time-recovery-specification PointInTimeRecoveryEnabled=true \
    >/dev/null 2>&1 && echo "   PITR enabled on '$name'." \
    || echo "   PITR: could not enable on '$name' (check permissions)."
}

create_table "$SITE_DELIVERY_ENDPOINTS_TABLE" \
  --attribute-definitions AttributeName=workspace_id,AttributeType=S \
  --key-schema AttributeName=workspace_id,KeyType=HASH

create_table "$PEOPLE_TABLE" \
  --attribute-definitions \
      AttributeName=workspace_id,AttributeType=S \
      AttributeName=person_key,AttributeType=S \
  --key-schema \
      AttributeName=workspace_id,KeyType=HASH \
      AttributeName=person_key,KeyType=RANGE

echo ">> Done. Next: 78_deploy_salesforce_site_delivery.sh"
