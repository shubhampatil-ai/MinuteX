#!/usr/bin/env bash
# =============================================================
# 58_grant_users_batchget.sh — the ONE permission that made every
# member name render as a raw uuid.
#
# THE BUG. _users_by_ids (members list) and _linked_avatar_map (contact
# photos for linked MinuteX users) both read the Users table with
# BatchGetItem — one call per page instead of a read per row. The
# userApi-inline policy granted GetItem/PutItem/UpdateItem/Query on
# Users but NOT BatchGetItem, so both calls failed AccessDeniedException.
#
# WHY IT WAS INVISIBLE. Each call site catches ClientError and degrades
# rather than failing the whole route — correct behaviour on its own, but
# it turned a permission error into silently missing data:
#
#   * the members list got {} for every profile, so `name` AND `email`
#     were both absent and the client fell through to the user_id;
#   * every linked contact fell back to initials instead of their photo.
#
# The only visible trace was a CloudWatch line, which is exactly why this
# looked like a frontend bug for so long.
#
# WHY A SEPARATE SCRIPT. Script 19 owns userApi-inline and has been
# corrected too, so a fresh environment gets this from the start. But
# re-running 19 also REDEPLOYS Lambda code for two functions, which is a
# far heavier action than adding one IAM verb to a running system. This
# script changes permissions only and touches no code.
#
# Idempotent: put-role-policy overwrites the whole policy document, and
# the document below is script 19's with BatchGetItem added — so running
# this twice, or running it after 19, converges on the same state.
#
# Verify afterwards (both should be non-empty rather than a uuid):
#   GET /workspaces/{id}/members  -> every row has a human `name`
#   GET /contacts                 -> linked contacts carry avatar_view_url
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

# This script REWRITES the whole userApi-inline document, so every value it
# interpolates must be present. BUCKET_NAME is the dangerous one: aws.sh does
# not guard it, and an EMPTY value would render the GetAudio statement as
# "arn:aws:s3:::/*" — silently revoking the Lambda's audio reads while
# appearing to succeed. Fail loudly instead.
: "${BUCKET_NAME:?BUCKET_NAME not set (put it in .env) — needed for the GetAudio statement}"

USERAPI_ROLE="$(aws lambda get-function --function-name "$USERAPI_LAMBDA_NAME" \
                  --query 'Configuration.Role' --output text)"
USERAPI_ROLE="${USERAPI_ROLE##*/}"
SECRET_ARN="$(aws lambda get-function-configuration \
                --function-name "$USERAPI_LAMBDA_NAME" \
                --query 'Environment.Variables.JWT_SECRET_ARN' --output text)"
echo ">> userApi role: $USERAPI_ROLE"

# Read the CURRENT policy first, so a drifted document is reported rather
# than silently overwritten by this script's idea of it. A superset that
# was added after script 19 (by a later script) would be lost otherwise.
echo ">> current Users statement:"
aws iam get-role-policy \
  --role-name "$USERAPI_ROLE" \
  --policy-name "userApi-inline" \
  --query 'PolicyDocument.Statement[?Sid==`Users`]' \
  --output json

if aws iam get-role-policy \
     --role-name "$USERAPI_ROLE" \
     --policy-name "userApi-inline" \
     --query 'PolicyDocument.Statement[?Sid==`Users`].Action' \
     --output text 2>/dev/null | grep -q "BatchGetItem"; then
  echo ">> BatchGetItem already granted on Users — nothing to do."
  exit 0
fi

POLICY_FILE="$(mktemp)"
trap 'rm -f "$POLICY_FILE"' EXIT
cat > "$POLICY_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "Logs", "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "arn:aws:logs:${AWS_REGION}:${ACCOUNT_ID}:*" },
    { "Sid": "Users", "Effect": "Allow",
      "Action": ["dynamodb:GetItem", "dynamodb:BatchGetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"],
      "Resource": [
        "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/Users",
        "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/Users/index/*" ] },
    { "Sid": "UserDevices", "Effect": "Allow",
      "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query", "dynamodb:DeleteItem"],
      "Resource": "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/UserDevices" },
    { "Sid": "DeviceKeysRead", "Effect": "Allow",
      "Action": "dynamodb:GetItem",
      "Resource": "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/DeviceKeys" },
    { "Sid": "Devices", "Effect": "Allow",
      "Action": ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:Query"],
      "Resource": [
        "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/Devices",
        "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/Devices/index/*" ] },
    { "Sid": "Recordings", "Effect": "Allow",
      "Action": ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:UpdateItem"],
      "Resource": [
        "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/Recordings",
        "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/Recordings/index/*" ] },
    { "Sid": "JwtSecret", "Effect": "Allow",
      "Action": "secretsmanager:GetSecretValue",
      "Resource": "${SECRET_ARN}" },
    { "Sid": "GetAudio", "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::${BUCKET_NAME}/*" }
  ]
}
EOF

aws iam put-role-policy \
  --role-name "$USERAPI_ROLE" \
  --policy-name "userApi-inline" \
  --policy-document "file://$(winpath "$POLICY_FILE")"

echo ">> Users now allows BatchGetItem."
echo ">> IAM is eventually consistent — allow ~10s before testing."
