#!/usr/bin/env bash
# =============================================================
# 51_enable_pitr_protection.sh — turn on PITR and deletion protection for the
# existing tables.
#
# WHY THIS IS ITS OWN SCRIPT
#   The Phase 1 audit found point-in-time recovery and deletion protection
#   DISABLED on all 16 existing tables. That is the single highest-value
#   mitigation available before any migration writes to production data, and
#   it is also completely independent of the workspace feature — so it ships
#   as its own reviewable change rather than buried inside a schema script.
#
# THIS CHANGES NO DATA AND NO SCHEMA.
#   PITR is a backup setting; deletion protection is a guard rail. Neither
#   alters an item, a key, an index or a capacity mode, and neither is visible
#   to the application. There is no code change and no deploy.
#
# COST. PITR is billed on the size of the table it protects. These tables are
# small (the largest is Recordings at ~121 items), so the cost here is
# negligible — but it is not zero, which is why the operator runs this
# knowingly rather than having it happen as a side effect of another script.
#
# ORDERING. Run this BEFORE 52_migrate_personal_workspaces.py. The migration
# only ever inserts into the two new tables, so it is already low-risk, but
# "restore to five minutes ago" is the thing you want to already have when a
# migration surprises you — not the thing you wish you had enabled.
#
# Idempotent: both settings are checked before being set, and AWS treats a
# no-op update as success anyway.
#
# ROLLBACK
#   aws dynamodb update-table --table-name <T> --no-deletion-protection-enabled
#   aws dynamodb update-continuous-backups --table-name <T> \
#     --point-in-time-recovery-specification PointInTimeRecoveryEnabled=false
#   Disabling PITR discards the continuous backup window, so treat rollback as
#   a deliberate act rather than a routine one.
#
# USAGE
#   bash scripts/51_enable_pitr_protection.sh            # all 16 tables
#   bash scripts/51_enable_pitr_protection.sh --dry-run  # report only
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

DRY_RUN="false"
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN="true"

# The 16 tables the audit enumerated, plus the three workspace tables (which
# 50 already hardens — listed here so a single run leaves everything covered).
TABLES=(
  "${USERS_TABLE:-Users}"
  "${RECORDINGS_TABLE:-Recordings}"
  "${TASKS_TABLE:-Tasks}"
  "${CONTACTS_TABLE:-Contacts}"
  "${FOLDERS_TABLE:-Folders}"
  "${FOLDER_CONTACTS_TABLE:-FolderContacts}"
  "${MEETING_PARTICIPANTS_TABLE:-MeetingParticipants}"
  "${DEVICES_TABLE:-Devices}"
  "${DEVICE_KEYS_TABLE:-DeviceKeys}"
  "${USER_DEVICES_TABLE:-UserDevices}"
  "${CHAT_SESSIONS_TABLE:-ChatSessions}"
  "${SHARES_TABLE:-Shares}"
  "${INTEGRATIONS_TABLE:-Integrations}"
  "${CRM_CONNECTIONS_TABLE:-CrmConnections}"
  "${NOTIFICATIONS_TABLE:-Notifications}"
  "${NOTIFICATION_DEDUPE_TABLE:-NotificationDedupe}"
  "${WORKSPACES_TABLE:-Workspaces}"
  "${WORKSPACE_MEMBERSHIPS_TABLE:-WorkspaceMemberships}"
  "${WORKSPACE_INVITATIONS_TABLE:-WorkspaceInvitations}"
)

changed=0
skipped=0
missing=0

for t in "${TABLES[@]}"; do
  if ! aws dynamodb describe-table --table-name "$t" >/dev/null 2>&1; then
    echo "-- $t: does not exist, skipping"
    missing=$((missing + 1))
    continue
  fi

  pitr="$(aws dynamodb describe-continuous-backups --table-name "$t" \
      --query 'ContinuousBackupsDescription.PointInTimeRecoveryDescription.PointInTimeRecoveryStatus' \
      --output text 2>/dev/null || echo "UNKNOWN")"
  prot="$(aws dynamodb describe-table --table-name "$t" \
      --query 'Table.DeletionProtectionEnabled' --output text 2>/dev/null || echo "False")"

  if [[ "$pitr" == "ENABLED" && "$prot" == "True" ]]; then
    echo "== $t: already protected (PITR=$pitr, deletion-protection=$prot)"
    skipped=$((skipped + 1))
    continue
  fi

  if [[ "$DRY_RUN" == "true" ]]; then
    echo ">> $t: WOULD enable (PITR=$pitr -> ENABLED, protection=$prot -> True)"
    changed=$((changed + 1))
    continue
  fi

  if [[ "$pitr" != "ENABLED" ]]; then
    echo ">> $t: enabling PITR ..."
    aws dynamodb update-continuous-backups --table-name "$t" \
      --point-in-time-recovery-specification PointInTimeRecoveryEnabled=true \
      >/dev/null
  fi
  if [[ "$prot" != "True" ]]; then
    echo ">> $t: enabling deletion protection ..."
    aws dynamodb update-table --table-name "$t" \
      --deletion-protection-enabled >/dev/null
  fi
  changed=$((changed + 1))
done

echo
echo "=============================================================="
echo " tables changed : $changed"
echo " already set    : $skipped"
echo " not found      : $missing"
if [[ "$DRY_RUN" == "true" ]]; then
  echo
  echo " DRY RUN — nothing was changed."
fi
echo "=============================================================="
