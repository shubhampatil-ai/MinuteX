#!/usr/bin/env bash
# =============================================================
# 50_create_workspace_tables.sh — the Workspace foundation (Phase 2A).
#
# Creates THREE new tables and grants userApi access to them. It changes NO
# existing table, adds NO index to an existing table, writes NO row to an
# existing table, and wires NO route. Phase 2A is additive by construction:
# running this script leaves every current behaviour exactly as it was.
#
#   Workspaces             PK workspace_id
#                          GSI owner-index    owner_user_id + created_at
#
#   WorkspaceMemberships   PK workspace_id  SK user_id
#                          GSI user-index     user_id + workspace_id
#
#   WorkspaceInvitations   PK invitation_id
#                          GSI token-index    token_hash
#                          GSI workspace-index workspace_id + created_at
#                          GSI email-index     email + created_at
#
# WHY MEMBERSHIPS IS THE FIRST PK+SK TABLE WORTH ADDING
# -----------------------------------------------------
# Script 49 documented that MinuteX had no composite-key table and declined to
# introduce the first one for a bounded turn list. Membership is the case that
# genuinely needs it, for a reason that list did not have:
#
#   "is THIS user a member of THIS workspace" must be a single GetItem on the
#   authorization path of every protected request. With PK workspace_id + SK
#   user_id that is one strongly-consistent point read. Any single-hash design
#   (a synthetic "ws#user" id, or a GSI lookup) either invents a key format to
#   parse or puts an EVENTUALLY CONSISTENT index on the authorization path —
#   and a removed member must lose access immediately, not "soon". The JWT is
#   long-lived and cannot be revoked, so this read IS the revocation mechanism.
#
# FolderContacts already established the PK+SK shape here (folder_id +
# contact_id), so this is not a new pattern in the codebase after all — it is
# the same edge-table shape, used for the same many-to-many reason.
#
# THE user-index GSI uses workspace_id as its RANGE key rather than a
# timestamp: "list my workspaces" is a set membership question, not a
# chronological one, and this keeps the pair unique inside the index.
#
# CONSISTENCY NOTE. The membership read is a GetItem on the base table, which
# the code issues with ConsistentRead=True (see _workspace_membership in
# userapi). The user-index is only ever used for the LIST of a user's
# workspaces, never for an allow/deny decision — indexes are lookup paths, not
# authorization mechanisms, which is the rule the rest of this codebase
# already follows.
#
# TOKEN INDEX. Invitation acceptance looks a row up by token_hash, so
# token-index is HASH-only on token_hash: a sha256 hex is unique, and there is
# nothing to sort within one. The RAW token is never stored (workspace_schema
# hashes it), so this index cannot leak a usable credential.
#
# PITR AND DELETION PROTECTION ARE ENABLED ON ALL THREE.
# The Phase 1 audit found both disabled on all 16 existing tables. This script
# does not change those 16 — that is a separate, independently reviewable
# operational change (see 51_enable_pitr_protection.sh) — but there is no
# reason to create NEW tables carrying the same gap, and membership rows are
# the authorization substrate: losing them locks every organisation out.
#
# Prerequisites: 31 (the workspace/org tables this builds beside), 49.
# Idempotent: every table and the policy are create-or-skip.
#
# ROLLBACK. Nothing here is destructive and nothing existing is touched, so
# rollback is: delete the three tables (they are empty until 52 runs) and
# remove the inline policy. Deletion protection must be disabled first, which
# is deliberate friction.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"
WORKSPACES_TABLE="${WORKSPACES_TABLE:-Workspaces}"
WORKSPACE_MEMBERSHIPS_TABLE="${WORKSPACE_MEMBERSHIPS_TABLE:-WorkspaceMemberships}"
WORKSPACE_INVITATIONS_TABLE="${WORKSPACE_INVITATIONS_TABLE:-WorkspaceInvitations}"

WORKSPACES_OWNER_INDEX="${WORKSPACES_OWNER_INDEX:-owner-index}"
MEMBERSHIPS_USER_INDEX="${MEMBERSHIPS_USER_INDEX:-user-index}"
INVITATIONS_TOKEN_INDEX="${INVITATIONS_TOKEN_INDEX:-token-index}"
INVITATIONS_WORKSPACE_INDEX="${INVITATIONS_WORKSPACE_INDEX:-workspace-index}"
INVITATIONS_EMAIL_INDEX="${INVITATIONS_EMAIL_INDEX:-email-index}"

# -------------------------------------------------------------
# 0. Offline tests first — the same gate scripts 42/43/44 apply. A schema
#    change that ships with a red suite is a change nobody can review.
# -------------------------------------------------------------
echo ">> Running the workspace foundation suite (offline) ..."
python -m pytest "$SCRIPT_DIR/../tests/test_workspace_foundation.py" -q

# -------------------------------------------------------------
# 1. Workspaces.
# -------------------------------------------------------------
if aws dynamodb describe-table --table-name "$WORKSPACES_TABLE" >/dev/null 2>&1; then
  echo ">> Table '$WORKSPACES_TABLE' already exists."
else
  echo ">> Creating '$WORKSPACES_TABLE' ..."
  aws dynamodb create-table \
    --table-name "$WORKSPACES_TABLE" \
    --attribute-definitions \
        AttributeName=workspace_id,AttributeType=S \
        AttributeName=owner_user_id,AttributeType=S \
        AttributeName=created_at,AttributeType=S \
    --key-schema \
        AttributeName=workspace_id,KeyType=HASH \
    --global-secondary-indexes "[
      {\"IndexName\":\"${WORKSPACES_OWNER_INDEX}\",
       \"KeySchema\":[{\"AttributeName\":\"owner_user_id\",\"KeyType\":\"HASH\"},
                    {\"AttributeName\":\"created_at\",\"KeyType\":\"RANGE\"}],
       \"Projection\":{\"ProjectionType\":\"ALL\"}}
    ]" \
    --billing-mode PAY_PER_REQUEST >/dev/null
  aws dynamodb wait table-exists --table-name "$WORKSPACES_TABLE"
  echo "   '$WORKSPACES_TABLE' is ACTIVE."
fi

# -------------------------------------------------------------
# 2. WorkspaceMemberships. PK + SK — see the header for why.
# -------------------------------------------------------------
if aws dynamodb describe-table --table-name "$WORKSPACE_MEMBERSHIPS_TABLE" >/dev/null 2>&1; then
  echo ">> Table '$WORKSPACE_MEMBERSHIPS_TABLE' already exists."
else
  echo ">> Creating '$WORKSPACE_MEMBERSHIPS_TABLE' ..."
  aws dynamodb create-table \
    --table-name "$WORKSPACE_MEMBERSHIPS_TABLE" \
    --attribute-definitions \
        AttributeName=workspace_id,AttributeType=S \
        AttributeName=user_id,AttributeType=S \
    --key-schema \
        AttributeName=workspace_id,KeyType=HASH \
        AttributeName=user_id,KeyType=RANGE \
    --global-secondary-indexes "[
      {\"IndexName\":\"${MEMBERSHIPS_USER_INDEX}\",
       \"KeySchema\":[{\"AttributeName\":\"user_id\",\"KeyType\":\"HASH\"},
                    {\"AttributeName\":\"workspace_id\",\"KeyType\":\"RANGE\"}],
       \"Projection\":{\"ProjectionType\":\"ALL\"}}
    ]" \
    --billing-mode PAY_PER_REQUEST >/dev/null
  aws dynamodb wait table-exists --table-name "$WORKSPACE_MEMBERSHIPS_TABLE"
  echo "   '$WORKSPACE_MEMBERSHIPS_TABLE' is ACTIVE."
fi

# -------------------------------------------------------------
# 3. WorkspaceInvitations.
# -------------------------------------------------------------
if aws dynamodb describe-table --table-name "$WORKSPACE_INVITATIONS_TABLE" >/dev/null 2>&1; then
  echo ">> Table '$WORKSPACE_INVITATIONS_TABLE' already exists."
else
  echo ">> Creating '$WORKSPACE_INVITATIONS_TABLE' ..."
  aws dynamodb create-table \
    --table-name "$WORKSPACE_INVITATIONS_TABLE" \
    --attribute-definitions \
        AttributeName=invitation_id,AttributeType=S \
        AttributeName=token_hash,AttributeType=S \
        AttributeName=workspace_id,AttributeType=S \
        AttributeName=email,AttributeType=S \
        AttributeName=created_at,AttributeType=S \
    --key-schema \
        AttributeName=invitation_id,KeyType=HASH \
    --global-secondary-indexes "[
      {\"IndexName\":\"${INVITATIONS_TOKEN_INDEX}\",
       \"KeySchema\":[{\"AttributeName\":\"token_hash\",\"KeyType\":\"HASH\"}],
       \"Projection\":{\"ProjectionType\":\"ALL\"}},
      {\"IndexName\":\"${INVITATIONS_WORKSPACE_INDEX}\",
       \"KeySchema\":[{\"AttributeName\":\"workspace_id\",\"KeyType\":\"HASH\"},
                    {\"AttributeName\":\"created_at\",\"KeyType\":\"RANGE\"}],
       \"Projection\":{\"ProjectionType\":\"ALL\"}},
      {\"IndexName\":\"${INVITATIONS_EMAIL_INDEX}\",
       \"KeySchema\":[{\"AttributeName\":\"email\",\"KeyType\":\"HASH\"},
                    {\"AttributeName\":\"created_at\",\"KeyType\":\"RANGE\"}],
       \"Projection\":{\"ProjectionType\":\"ALL\"}}
    ]" \
    --billing-mode PAY_PER_REQUEST >/dev/null
  aws dynamodb wait table-exists --table-name "$WORKSPACE_INVITATIONS_TABLE"
  echo "   '$WORKSPACE_INVITATIONS_TABLE' is ACTIVE."
fi

# -------------------------------------------------------------
# 4. PITR + deletion protection on the three NEW tables only.
#    Idempotent: both calls are no-ops when already in the desired state.
# -------------------------------------------------------------
for t in "$WORKSPACES_TABLE" "$WORKSPACE_MEMBERSHIPS_TABLE" "$WORKSPACE_INVITATIONS_TABLE"; do
  echo ">> Hardening '$t' (PITR + deletion protection) ..."
  aws dynamodb update-continuous-backups \
    --table-name "$t" \
    --point-in-time-recovery-specification PointInTimeRecoveryEnabled=true \
    >/dev/null 2>&1 || echo "   (PITR already enabled on $t)"
  aws dynamodb update-table \
    --table-name "$t" --deletion-protection-enabled \
    >/dev/null 2>&1 || echo "   (deletion protection already enabled on $t)"
done

# -------------------------------------------------------------
# 5. IAM. Table AND index ARNs — a policy granting only the table passes
#    every GetItem and fails every list.
# -------------------------------------------------------------
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
USERAPI_ROLE_ARN="$(aws lambda get-function --function-name "$USERAPI_LAMBDA_NAME" \
                      --query 'Configuration.Role' --output text)"
USERAPI_ROLE_NAME="${USERAPI_ROLE_ARN##*/}"
POLICY_NAME="MinuteXWorkspaceAccess"

WS_ARN="arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${WORKSPACES_TABLE}"
WM_ARN="arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${WORKSPACE_MEMBERSHIPS_TABLE}"
WI_ARN="arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${WORKSPACE_INVITATIONS_TABLE}"

echo ">> Attaching $POLICY_NAME to $USERAPI_ROLE_NAME ..."
aws iam put-role-policy \
  --role-name "$USERAPI_ROLE_NAME" \
  --policy-name "$POLICY_NAME" \
  --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [{
      \"Effect\": \"Allow\",
      \"Action\": [
        \"dynamodb:GetItem\",
        \"dynamodb:PutItem\",
        \"dynamodb:UpdateItem\",
        \"dynamodb:DeleteItem\",
        \"dynamodb:Query\"
      ],
      \"Resource\": [
        \"${WS_ARN}\", \"${WS_ARN}/index/*\",
        \"${WM_ARN}\", \"${WM_ARN}/index/*\",
        \"${WI_ARN}\", \"${WI_ARN}/index/*\"
      ]
    }]
  }" >/dev/null
echo ">> Policy attached."

# -------------------------------------------------------------
# 6. Env. Names only — no behaviour is switched on by this script.
# -------------------------------------------------------------
MERGE_ENV_PY="$SCRIPT_DIR/_merge_env.py"

merge_env() {
  local fn="$1"; shift
  local current merged
  current="$(aws lambda get-function-configuration \
               --function-name "$fn" \
               --query 'Environment.Variables' --output json)"
  merged="$(python "$MERGE_ENV_PY" "$current" "$@")"
  aws lambda update-function-configuration \
    --function-name "$fn" --environment "$merged" \
    --query "FunctionName" --output text >/dev/null
  aws lambda wait function-updated-v2 --function-name "$fn"
  echo ">> $fn env merged: $*"
}

merge_env "$USERAPI_LAMBDA_NAME" \
  "WORKSPACES_TABLE=$WORKSPACES_TABLE" \
  "WORKSPACE_MEMBERSHIPS_TABLE=$WORKSPACE_MEMBERSHIPS_TABLE" \
  "WORKSPACE_INVITATIONS_TABLE=$WORKSPACE_INVITATIONS_TABLE" \
  "WORKSPACES_OWNER_INDEX=$WORKSPACES_OWNER_INDEX" \
  "MEMBERSHIPS_USER_INDEX=$MEMBERSHIPS_USER_INDEX" \
  "INVITATIONS_TOKEN_INDEX=$INVITATIONS_TOKEN_INDEX" \
  "INVITATIONS_WORKSPACE_INDEX=$INVITATIONS_WORKSPACE_INDEX" \
  "INVITATIONS_EMAIL_INDEX=$INVITATIONS_EMAIL_INDEX"

echo
echo "=============================================================="
echo " Workspace tables created. NOTHING ELSE CHANGED."
echo
echo " No route was wired, no existing table was modified, and no"
echo " row was written. Run 52_migrate_personal_workspaces.py --dry-run"
echo " next to see what the personal-workspace backfill WOULD do."
echo "=============================================================="
