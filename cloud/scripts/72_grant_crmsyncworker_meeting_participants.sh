#!/usr/bin/env bash
# =============================================================
# 72_grant_crmsyncworker_meeting_participants.sh — let crmSyncWorker READ
# MeetingParticipants.
#
# THE BUG THIS FIXES. Every organisation CRM push failed in production with:
#
#   AccessDeniedException ... User: .../crmSyncWorkerRole/crmSyncWorker is not
#   authorized to perform: dynamodb:Query on resource: .../MeetingParticipants
#   because no identity-based policy allows the dynamodb:Query action
#
# WHY IT HAPPENED. scripts/65_deploy_crm_sync_worker.sh built the worker's
# least-privilege policy by listing the tables the push path reads, and
# MeetingParticipants was simply missed — even though it is on the MAIN path,
# not an edge case: _execute_org_meeting_crm_push -> _meeting_crm_identity_
# state -> _participant_rows(key) queries it to find the meeting's speakers,
# and the speakers ARE the CRM push (who the SM is, who the client is). So
# the failure was not intermittent: no organisation push could ever succeed.
#
# It surfaced only now because the push is ASYNC — the worker's
# AccessDeniedException is caught, classified PERMISSION (non-retryable) and
# written to the job row, so the user sees "CRM sync failed" on the meeting
# screen rather than an API error at push time.
#
# WHAT IS GRANTED: dynamodb:Query + GetItem on MeetingParticipants ONLY, and
# READ-ONLY. Verified against the code rather than assumed:
#   * _participant_rows does a base-table Query on audio_s3_key (the table's
#     partition key) — no index, so no /index/* is needed.
#   * the push path never WRITES participants; tagging happens in userApi
#     through set_participant, under the caller's own JWT.
#
# This is an ADDITIVE, separate inline policy. It deliberately does NOT
# rewrite crmSyncWorker-least-privilege: that policy is script 65's to own,
# and having two scripts write one policy document is how a later run of 65
# would silently revert this fix. scripts/65 has ALSO been corrected so a
# fresh deployment gets it right from the start; this script exists for the
# already-deployed role, which no re-run of 65 would otherwise reach without
# also redeploying the function.
#
# Idempotent: put-role-policy overwrites.
#
# Prerequisites: scripts/65_deploy_crm_sync_worker.sh already run.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

WORKER_LAMBDA_NAME="${WORKER_LAMBDA_NAME:-crmSyncWorker}"
MEETING_PARTICIPANTS_TABLE="${MEETING_PARTICIPANTS_TABLE:-MeetingParticipants}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

echo ">> Region: $AWS_REGION"
echo ">> Worker: $WORKER_LAMBDA_NAME"

# Read the role off the DEPLOYED function rather than hardcoding the name, so
# this cannot drift from whatever script 65 actually created.
WORKER_ROLE="$(aws lambda get-function --function-name "$WORKER_LAMBDA_NAME" \
                 --query 'Configuration.Role' --output text)"
WORKER_ROLE="${WORKER_ROLE##*/}"
echo ">> Worker role: $WORKER_ROLE"

POLICY_FILE="$(mktemp)"
trap 'rm -f "$POLICY_FILE"' EXIT
cat > "$POLICY_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "MeetingParticipantsReadOnly", "Effect": "Allow",
      "Action": ["dynamodb:GetItem", "dynamodb:Query"],
      "Resource": "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${MEETING_PARTICIPANTS_TABLE}" }
  ]
}
EOF
aws iam put-role-policy \
  --role-name "$WORKER_ROLE" \
  --policy-name "crmSyncWorker-meeting-participants" \
  --policy-document "file://$(winpath "$POLICY_FILE")"
echo ">> crmSyncWorker-meeting-participants inline policy set"
echo "   (${MEETING_PARTICIPANTS_TABLE}: GetItem + Query, read-only)."
echo
echo ">> Verify with a read-only listing:"
echo "     aws iam get-role-policy --role-name $WORKER_ROLE \\"
echo "         --policy-name crmSyncWorker-meeting-participants"
echo ">> No code, route, table or queue was modified."
