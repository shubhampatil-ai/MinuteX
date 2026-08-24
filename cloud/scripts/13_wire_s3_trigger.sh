#!/usr/bin/env bash
# =============================================================
# 13_wire_s3_trigger.sh — S3 ObjectCreated(.wav) -> transcribe Lambda.
#
# RUN THIS LAST. It turns on automatic processing: any .wav landing in
# the bucket invokes transcribeAndSync.
#
# Steps:
#   1. Grant S3 permission to invoke the Lambda (idempotent).
#   2. Put a bucket notification: ObjectCreated:* filtered to suffix .wav.
#
# NO trigger loop: the Lambda only READS audio and writes to DynamoDB /
# Salesforce — it never writes .wav back to the bucket.
#
# WARNING: put-bucket-notification-configuration REPLACES the whole
# notification config. This script writes ONLY our .wav->Lambda rule.
# If you later add other notifications, merge them into the JSON here.
# (Checked at build time: the bucket had no existing notifications.)
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

TR_LAMBDA_NAME="${TRANSCRIBE_LAMBDA_NAME:-transcribeAndSync}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
LAMBDA_ARN="$(aws lambda get-function --function-name "$TR_LAMBDA_NAME" \
                --query 'Configuration.FunctionArn' --output text)"

echo ">> Bucket: $BUCKET_NAME"
echo ">> Lambda: $LAMBDA_ARN"

# -------------------------------------------------------------
# 1. Permission for S3 to invoke the Lambda (idempotent).
# -------------------------------------------------------------
STMT_ID="s3-invoke-transcribe"
echo ">> Ensuring S3 invoke permission ..."
if aws lambda add-permission \
     --function-name "$TR_LAMBDA_NAME" \
     --statement-id "$STMT_ID" \
     --action lambda:InvokeFunction \
     --principal s3.amazonaws.com \
     --source-arn "arn:aws:s3:::${BUCKET_NAME}" \
     --source-account "$ACCOUNT_ID" >/dev/null 2>&1; then
  echo ">> Permission added."
else
  echo ">> Permission already present (ok)."
fi

# -------------------------------------------------------------
# 2. Bucket notification: ObjectCreated + suffix .wav -> Lambda.
# -------------------------------------------------------------
NOTIF_FILE="$(mktemp)"
trap 'rm -f "$NOTIF_FILE"' EXIT
cat > "$NOTIF_FILE" <<EOF
{
  "LambdaFunctionConfigurations": [
    {
      "Id": "transcribe-on-wav",
      "LambdaFunctionArn": "${LAMBDA_ARN}",
      "Events": ["s3:ObjectCreated:*"],
      "Filter": {
        "Key": {
          "FilterRules": [
            { "Name": "suffix", "Value": ".wav" }
          ]
        }
      }
    }
  ]
}
EOF

echo ">> Putting bucket notification (ObjectCreated, suffix .wav) ..."
aws s3api put-bucket-notification-configuration \
  --bucket "$BUCKET_NAME" \
  --notification-configuration "file://$(winpath "$NOTIF_FILE")"

echo ">> Trigger wired. New .wav uploads now auto-invoke $TR_LAMBDA_NAME."
echo ">> Verify:  aws s3api get-bucket-notification-configuration --bucket $BUCKET_NAME --region $AWS_REGION"
