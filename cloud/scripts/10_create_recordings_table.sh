#!/usr/bin/env bash
# =============================================================
# 10_create_recordings_table.sh — DynamoDB backend copy of transcripts.
#
# NOTE ON DESIGN DEVIATION (see README):
#   The original spec called for Aurora Serverless v2 + RDS Data API.
#   AWS has no free-tier Aurora Serverless v2, and the RDS free-tier
#   instance (db.t3.micro) does NOT support the Data API — so "free +
#   Data API" is impossible. We use DynamoDB instead: same fields,
#   UPSERT on audio_s3_key, no VPC/NAT, effectively free at this volume.
#   Every functional goal of the "durable backend copy" is preserved.
#
# Table: recordings
#   Partition key: audio_s3_key (String)   <- UPSERT key (unique per object)
#   Billing:       PAY_PER_REQUEST
#
#   Item attributes written by the Lambda:
#     audio_s3_key, device_id, meeting_id, recorded_at,
#     transcript, summary, detected_language,
#     salesforce_object, salesforce_record_id,
#     sync_status ('success'|'failed'|'unmapped'),
#     sync_error, transcribed_at
#
# Idempotent: no-op if the table already exists.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

REC_TABLE="${RECORDINGS_TABLE:-recordings}"

echo ">> Region: $AWS_REGION"
echo ">> Table:  $REC_TABLE"

if aws dynamodb describe-table --table-name "$REC_TABLE" >/dev/null 2>&1; then
  status="$(aws dynamodb describe-table --table-name "$REC_TABLE" \
              --query 'Table.TableStatus' --output text)"
  echo ">> Table '$REC_TABLE' already exists (status: $status). Nothing to do."
  exit 0
fi

echo ">> Creating table '$REC_TABLE' ..."
aws dynamodb create-table \
  --table-name "$REC_TABLE" \
  --attribute-definitions AttributeName=audio_s3_key,AttributeType=S \
  --key-schema AttributeName=audio_s3_key,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST >/dev/null

echo ">> Waiting for table to become ACTIVE ..."
aws dynamodb wait table-exists --table-name "$REC_TABLE"
echo ">> Table '$REC_TABLE' is ACTIVE."
