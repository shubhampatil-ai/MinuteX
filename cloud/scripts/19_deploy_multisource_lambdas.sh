#!/usr/bin/env bash
# =============================================================
# 19_deploy_multisource_lambdas.sh — deploy the multi-source code.
#
# Ships the two Lambdas the recording-sources feature changes:
#
#   userApi             <- functions/userapi/lambda_function.py
#       (upload-request/upload-complete + pairing/user-owned routes)
#   transcribeRecording <- functions/transcribe/lambda_function.py
#       (the LIVE ElevenLabs+Groq Python build, pulled from ap-south-1
#        and patched: all audio formats, mobile/uploads key parsing,
#        status lifecycle, put_item -> upsert so presign-stamped
#        user_id/source/title/duration survive reprocessing)
#
# Also extends the userApi role's inline DynamoDB policy: the new code
# reads/writes the Devices table (pairing), updates Recordings (upload
# stubs + legacy stamping) and deletes UserDevices rows (unpair).
# (s3:PutObject for presigned uploads is attached by script 18.)
#
# Prerequisites: scripts 14-17 (Devices table, user-index GSI, backfill,
# pairing routes). Follow with script 18 (upload routes + S3 trigger).
#
# Idempotent: update-function-code/put-role-policy simply overwrite.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"
TR_LAMBDA_NAME="${TRANSCRIBE_LAMBDA_NAME:-transcribeRecording}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

# -------------------------------------------------------------
# 1. userApi role: extended inline policy (superset of the current
#    userApi-inline — everything it had, plus Devices / Recordings
#    writes / UserDevices delete).
# -------------------------------------------------------------
USERAPI_ROLE="$(aws lambda get-function --function-name "$USERAPI_LAMBDA_NAME" \
                  --query 'Configuration.Role' --output text)"
USERAPI_ROLE="${USERAPI_ROLE##*/}"
SECRET_ARN="$(aws lambda get-function-configuration --function-name "$USERAPI_LAMBDA_NAME" \
                --query 'Environment.Variables.JWT_SECRET_ARN' --output text)"
echo ">> userApi role: $USERAPI_ROLE (secret: $SECRET_ARN)"

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
      "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"],
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
echo ">> userApi-inline policy extended (Devices, Recordings writes, UserDevices delete)."

# -------------------------------------------------------------
# 2. Deploy userApi (single-file zip, stdlib only).
# -------------------------------------------------------------
deploy_py() {
  local fn="$1" src_dir="$2"
  local zip="$PROJECT_ROOT/$src_dir/function.zip"
  rm -f "$zip"
  (cd "$PROJECT_ROOT/$src_dir" && "$AWS_BIN" --version >/dev/null && \
     python -c "import zipfile; z = zipfile.ZipFile('function.zip', 'w', zipfile.ZIP_DEFLATED); z.write('lambda_function.py'); z.close()")
  aws lambda update-function-code \
    --function-name "$fn" \
    --zip-file "fileb://$(winpath "$zip")" \
    --publish \
    --query "[FunctionName,CodeSize,LastModified]" --output text
  aws lambda wait function-updated-v2 --function-name "$fn"
  echo ">> $fn deployed from $src_dir/lambda_function.py"
}

deploy_py "$USERAPI_LAMBDA_NAME" "functions/userapi"

# -------------------------------------------------------------
# 3. transcribeRecording role: the patched build upserts (UpdateItem)
#    and reads (GetItem) the row instead of blind put_item — the
#    original policy only allowed PutItem/GetItem/Query.
#
#    Also needs s3:PutObject on transcripts/* : transcript + timestamps
#    are written to S3 instead of into the DynamoDB item, which used to
#    push a long meeting past the hard 400 KB item limit (see
#    shared/transcript_store.py). Scoped to that ONE prefix, not
#    the bucket — this role must never be able to overwrite a user's
#    audio, only the derived transcript beside it.
# -------------------------------------------------------------
TR_ROLE="$(aws lambda get-function --function-name "$TR_LAMBDA_NAME" \
             --query 'Configuration.Role' --output text)"
TR_ROLE="${TR_ROLE##*/}"
TR_POLICY_FILE="$(mktemp)"
cat > "$TR_POLICY_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "PresignAndReadAudioObjects", "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::${BUCKET_NAME}/*" },
    { "Sid": "WriteOffloadedTranscripts", "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::${BUCKET_NAME}/transcripts/*" },
    { "Sid": "UpsertAndReadRecordings", "Effect": "Allow",
      "Action": ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:Query"],
      "Resource": [
        "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/Recordings",
        "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/Recordings/index/*" ] }
  ]
}
EOF
aws iam put-role-policy \
  --role-name "$TR_ROLE" \
  --policy-name "transcribe-recording-access" \
  --policy-document "file://$(winpath "$TR_POLICY_FILE")"
rm -f "$TR_POLICY_FILE"
echo ">> $TR_ROLE policy extended (dynamodb:UpdateItem for status upserts,"
echo "   s3:PutObject on transcripts/* for the offloaded transcript)."

# -------------------------------------------------------------
# 4. Deploy transcribeRecording (the patched LIVE Python build).
# -------------------------------------------------------------
if [[ ! -f "$PROJECT_ROOT/functions/transcribe/lambda_function.py" ]]; then
  echo "ERROR: functions/transcribe/lambda_function.py missing — pull the live" >&2
  echo "       artifact first (aws lambda get-function) and re-apply the patch." >&2
  exit 1
fi
deploy_py "$TR_LAMBDA_NAME" "functions/transcribe"

echo ">> Done. Now run: bash scripts/18_wire_upload_routes.sh"
