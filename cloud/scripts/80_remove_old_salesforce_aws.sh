#!/usr/bin/env bash
# =============================================================
# 80_remove_old_salesforce_aws.sh — remove the AWS resources that existed
# ONLY for the old Salesforce OAuth/2GP integration, after its code is gone.
#
# ALREADY DONE (2026-09-24, not repeated here):
#   - the 33 old API Gateway routes (/crm/salesforce/*, crm-review, crm-push,
#     crm-sync-jobs*, PUT /contacts/{id}/crm) were deleted;
#   - Secrets Manager userApi/salesforceClientSecret was scheduled for
#     deletion (30-day recovery window; `aws secretsmanager restore-secret`).
#
# THIS SCRIPT (run AFTER deploying the current userApi, crmSyncWorker and
# transcribeRecording code — the previously deployed userApi still reads
# CrmConnections on GET /recordings/{key}, so removing it first would break
# the meeting screen). It refuses to run while a deployed function still
# contains the old code.
#   1. Environment: removes the old Salesforce variables from userApi,
#      crmSyncWorker and transcribeRecording (merge, not replace).
#   2. IAM: deletes userApi-crm-salesforce, userApi-org-crm-salesforce,
#      userApi-org-salesforce-user-links and transcribe-crm-config-read, and
#      rewrites crmSyncWorker-least-privilege without the old grants (run
#      scripts/65_deploy_crm_sync_worker.sh for that; it is idempotent).
#      userApi keeps kms:Decrypt on alias/crm-salesforce in a small
#      replacement policy (userApi-legacy-kms-decrypt): Gmail refresh tokens
#      stored before INTEGRATIONS_KMS_KEY_ID existed may be encrypted with it,
#      and that has not been verified either way.
#   3. DynamoDB: takes an on-demand backup of CrmConnections, OrgCrmConnections
#      and OrgSalesforceUserLinks, then deletes the tables.
#
# NOT removed: the KMS key alias/crm-salesforce (see step 2) — retire it
# only once no stored ciphertext depends on it. CrmSyncJobs, CrmSyncQueue,
# CrmSyncDLQ, the DLQ alarm and crmSyncWorker are SHARED with the
# Salesforce Site flow and stay.
#
# Idempotent: every step skips what is already gone.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

USERAPI_FN="${USERAPI_LAMBDA_NAME:-userApi}"
WORKER_FN="${CRM_SYNC_WORKER_LAMBDA_NAME:-crmSyncWorker}"
TRANSCRIBE_FN="${TRANSCRIBE_LAMBDA_NAME:-transcribeRecording}"
OLD_TABLES=(CrmConnections OrgCrmConnections OrgSalesforceUserLinks)
OLD_KMS_KEY_ID="b5a78676-4c51-4ba4-8f2a-bb0a77bfcec4"   # alias/crm-salesforce
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

role_of() {
  aws lambda get-function-configuration --function-name "$1" \
    --query Role --output text | tr -d '\r' | sed 's|.*/||'
}

# -------------------------------------------------------------
# 0. Guard: the deployed code must no longer contain the old integration.
# -------------------------------------------------------------
echo "==> Checking deployed code"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
for FN in "$USERAPI_FN" "$WORKER_FN" "$TRANSCRIBE_FN"; do
  URL="$(aws lambda get-function --function-name "$FN" --query Code.Location --output text | tr -d '\r')"
  curl -sSf -o "$TMP/$FN.zip" "$URL"
  if python - "$TMP/$FN.zip" <<'PY'
import sys, zipfile
z = zipfile.ZipFile(sys.argv[1])
markers = (b"class SalesforceClient", b"_crm_connections = ", b"CRM_CONNECTIONS_TABLE",
           b"def execute_crm_sync_job")
hit = [m.decode() for n in z.namelist() if n.endswith(".py")
       for m in markers if m in z.read(n)]
sys.exit(0 if hit else 1)
PY
  then
    echo "FAIL: $FN still runs the old Salesforce code. Deploy the current" \
         "code first (userApi, crmSyncWorker via scripts/65, transcribeRecording)." >&2
    exit 1
  fi
  echo "   $FN: clean"
done

# -------------------------------------------------------------
# 1. Environment variables.
# -------------------------------------------------------------
drop_env() {
  local fn="$1"; shift
  local current
  current="$(aws lambda get-function-configuration --function-name "$fn" \
               --query 'Environment.Variables' --output json)"
  local new
  new="$(python - "$current" "$@" <<'PY'
import json, sys
env = json.loads(sys.argv[1]) or {}
gone = [k for k in sys.argv[2:] if k in env]
for k in gone:
    env.pop(k)
print(json.dumps({"Variables": env}) if gone else "")
PY
)"
  if [[ -z "$new" ]]; then echo "   $fn: nothing to remove"; return; fi
  aws lambda update-function-configuration --function-name "$fn" \
    --environment "$new" --query FunctionName --output text >/dev/null
  aws lambda wait function-updated-v2 --function-name "$fn"
  echo "   $fn: removed old variables"
}
echo "==> Environment"
drop_env "$USERAPI_FN" CRM_CONNECTIONS_TABLE ORG_CRM_CONNECTIONS_TABLE \
  ORG_SALESFORCE_REDIRECT_URI SALESFORCE_API_VERSION SALESFORCE_CLIENT_ID \
  SALESFORCE_CLIENT_SECRET_ARN SALESFORCE_KMS_KEY_ID SALESFORCE_LOGIN_URL \
  SALESFORCE_REDIRECT_URI SALESFORCE_RETURN_URL SF_MEETING_EXTERNAL_ID \
  SF_MEETING_OBJECT SF_PACKAGE_INSTALL_URL
drop_env "$WORKER_FN" ORG_CRM_CONNECTIONS_TABLE ORG_SALESFORCE_USER_LINKS_TABLE \
  SALESFORCE_API_VERSION SALESFORCE_CLIENT_ID SALESFORCE_CLIENT_SECRET_ARN \
  SALESFORCE_KMS_KEY_ID SALESFORCE_LOGIN_URL SF_MEETING_EXTERNAL_ID \
  SF_MEETING_OBJECT SF_PACKAGE_INSTALL_URL
drop_env "$TRANSCRIBE_FN" CRM_CONNECTIONS_TABLE

# -------------------------------------------------------------
# 2. IAM.
# -------------------------------------------------------------
echo "==> IAM"
USERAPI_ROLE="$(role_of "$USERAPI_FN")"
TRANSCRIBE_ROLE="$(role_of "$TRANSCRIBE_FN")"
KMS_POLICY="$(mktemp)"
cat > "$KMS_POLICY" <<EOF
{ "Version": "2012-10-17",
  "Statement": [
    { "Sid": "LegacyTokenDecrypt", "Effect": "Allow", "Action": "kms:Decrypt",
      "Resource": "arn:aws:kms:${AWS_REGION}:${ACCOUNT_ID}:key/${OLD_KMS_KEY_ID}" } ] }
EOF
aws iam put-role-policy --role-name "$USERAPI_ROLE" \
  --policy-name userApi-legacy-kms-decrypt \
  --policy-document "file://$(winpath "$KMS_POLICY")"
rm -f "$KMS_POLICY"
echo "   $USERAPI_ROLE: userApi-legacy-kms-decrypt set"
for pair in "$USERAPI_ROLE userApi-crm-salesforce" \
            "$USERAPI_ROLE userApi-org-crm-salesforce" \
            "$USERAPI_ROLE userApi-org-salesforce-user-links" \
            "$TRANSCRIBE_ROLE transcribe-crm-config-read"; do
  set -- $pair
  if aws iam get-role-policy --role-name "$1" --policy-name "$2" >/dev/null 2>&1; then
    aws iam delete-role-policy --role-name "$1" --policy-name "$2"
    echo "   $1: deleted $2"
  else
    echo "   $1: $2 already gone"
  fi
done
echo "   crmSyncWorker-least-privilege: rewritten by scripts/65 (run it if not yet)"

# -------------------------------------------------------------
# 3. DynamoDB: backup, then delete.
# -------------------------------------------------------------
echo "==> DynamoDB"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
for T in "${OLD_TABLES[@]}"; do
  if ! aws dynamodb describe-table --table-name "$T" >/dev/null 2>&1; then
    echo "   $T: already gone"; continue
  fi
  aws dynamodb create-backup --table-name "$T" \
    --backup-name "${T}-before-removal-${STAMP}" \
    --query BackupDetails.BackupArn --output text
  aws dynamodb update-table --table-name "$T" \
    --no-deletion-protection-enabled >/dev/null 2>&1 || true
  aws dynamodb wait table-exists --table-name "$T"
  aws dynamodb delete-table --table-name "$T" --query TableDescription.TableStatus --output text
  echo "   $T: backed up and deleted"
done

echo
echo ">> Done. Retained on purpose: KMS alias/crm-salesforce (+ userApi-legacy-kms-decrypt)."
