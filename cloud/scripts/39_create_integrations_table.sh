#!/usr/bin/env bash
# =============================================================
# 39_create_integrations_table.sh — per-user external application connections.
#
# Table: Integrations  (override with INTEGRATIONS_TABLE in .env)
#   Partition key: user_id  (String)
#   Sort key:      provider (String)   -- "gmail" today; the SK is what makes
#                  this generic, so whatsapp/google_calendar/google_tasks/
#                  slack land here with no schema change and no second table.
#   Billing:       PAY_PER_REQUEST
#
# WHY NOT REUSE CrmConnections (script 22). Same key shape on purpose — that
# design was right and this copies it. What it does not copy is the SCOPE:
# CrmConnections rows carry Salesforce-specific attributes (instance_url,
# org_id, sf_username) plus the CRM field-mapping config, and every consumer
# reads them. Putting a Gmail credential in there would mean either Salesforce
# columns sitting empty on mail rows, or one item that two unrelated features
# both write. Salesforce therefore stays where it is; the Integrations screen
# still lists it (flagged managed_elsewhere) so the user sees one complete
# picture. Migrating it onto this table later is a data move, not a redesign.
#
#   Item shape (written by functions/userapi's integration_callback, read by
#   list_integrations / get_integration / _integration_call):
#     user_id             PK
#     provider            SK ("gmail")
#     status              CONNECTED | REAUTH_REQUIRED | ERROR
#                         NOT_CONNECTED is never stored — it is what the
#                         ABSENCE of a row means. See shared/integrations.py.
#     status_message      user-facing reason, only when not CONNECTED
#     refresh_token_enc   KMS ciphertext (base64url), never the raw token
#     account_identifier  the connected address, e.g. user@gmail.com
#     account_name        display name, when the provider gives one
#     scopes              the scopes actually GRANTED (not the ones requested)
#     connected_at        ISO timestamp — survives a reconnect
#     updated_at          ISO timestamp
#
#   Access tokens are deliberately NOT stored: they expire in an hour, and
#   persisting them would mean a second secret to encrypt, rotate and leak.
#   Every request mints one from the refresh token (see _integration_call).
#
# No GSI: every access is a GetItem/Query by user_id — there is no
# "find all users connected to Gmail" query in this phase.
#
# Idempotent: no-op if the table already exists.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

INTEGRATIONS_TABLE="${INTEGRATIONS_TABLE:-Integrations}"

echo ">> Region: $AWS_REGION"
echo ">> Table:  $INTEGRATIONS_TABLE"

if aws dynamodb describe-table --table-name "$INTEGRATIONS_TABLE" >/dev/null 2>&1; then
  status="$(aws dynamodb describe-table --table-name "$INTEGRATIONS_TABLE" \
              --query 'Table.TableStatus' --output text)"
  echo ">> Table '$INTEGRATIONS_TABLE' already exists (status: $status). Nothing to do."
  exit 0
fi

echo ">> Creating table '$INTEGRATIONS_TABLE' ..."
aws dynamodb create-table \
  --table-name "$INTEGRATIONS_TABLE" \
  --attribute-definitions \
      AttributeName=user_id,AttributeType=S \
      AttributeName=provider,AttributeType=S \
  --key-schema \
      AttributeName=user_id,KeyType=HASH \
      AttributeName=provider,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST >/dev/null

echo ">> Waiting for table to become ACTIVE ..."
aws dynamodb wait table-exists --table-name "$INTEGRATIONS_TABLE"
echo ">> Table '$INTEGRATIONS_TABLE' is ACTIVE."
