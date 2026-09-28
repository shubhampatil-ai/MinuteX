#!/usr/bin/env bash
# =============================================================
# 74_grant_crmsyncworker_transcript_s3.sh — let the CRM sync worker READ
# offloaded transcripts from S3.
#
# WHY THIS IS A SEPARATE SCRIPT AND WHY IT IS REQUIRED.
#
# transcript_store moved every transcript out of DynamoDB into one S3 object
# per meeting (a 45-minute meeting measured 96% of the hard 400 KB item
# limit; see that module's docstring). The row keeps only a POINTER —
# `transcript_s3_key` — and `transcript` is simply absent.
#
# crmSyncWorker was built before this mattered to it and was granted NO S3
# permission at all (see 65_deploy_crm_sync_worker.sh's policy: DynamoDB,
# KMS, Secrets Manager, SQS — no s3:GetObject, and no BUCKET_NAME in its
# environment). It read the recording row with a bare GetItem and pushed
# `item["transcript"]`, which on every non-legacy meeting is "".
#
# That produced a SILENT failure: _crm_push_payload skips empty content by
# design (so a push never blanks a field a customer filled in themselves),
# so a customer who had mapped a Transcript field received nothing at all,
# with no error, no failed job and no log line. The CRM review screen made
# it worse by hydrating on ITS read path — so the UI displayed a transcript
# that the push then did not deliver.
#
# The worker's meeting delivery calls transcript_store.hydrate. Without the grant
# below that call fails on AccessDenied, is swallowed by its deliberate
# best-effort try/except, and the transcript stays empty — the exact bug,
# with a log line. The Site meeting delivery (scripts/78) needs this grant.
#
# SCOPE: read-only, and only under the transcripts/ prefix. The worker has
# no reason to read audio, avatars or PageIndex objects, and no reason to
# write anything to S3 at all.
#
# Idempotent: re-running replaces the same inline policy with the same
# content. It does NOT touch crmSyncWorker-least-privilege (65's policy), so
# the two can be reasoned about — and revoked — independently.
# =============================================================
set -euo pipefail
cd "$(dirname "$0")"
. ./aws.sh

ROLE_NAME="${CRM_SYNC_WORKER_ROLE_NAME:-crmSyncWorkerRole}"
POLICY_NAME="crmSyncWorker-transcript-read"
WORKER_FN="${CRM_SYNC_WORKER_LAMBDA_NAME:-crmSyncWorker}"
BUCKET="${BUCKET_NAME:?BUCKET_NAME must be set (see .env)}"

echo "==> Granting $ROLE_NAME read access to s3://$BUCKET/transcripts/*"

# The recordings/* grant is what lets the OPTIONAL audio delivery read the
# meeting's own audio object. It is deliberately part of the same policy —
# a role that may read transcripts but not the recording would fail audio
# delivery at upload time, per meeting, for a permission the operator
# thought they had granted. Both are read-only, and the worker never writes
# to S3 at all.
POLICY_JSON=$(cat <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadOffloadedTranscripts",
      "Effect": "Allow",
      "Action": ["s3:GetObject"],
      "Resource": "arn:aws:s3:::${BUCKET}/transcripts/*"
    },
    {
      "Sid": "ReadMeetingAudioForOptionalDelivery",
      "Effect": "Allow",
      "Action": ["s3:GetObject"],
      "Resource": "arn:aws:s3:::${BUCKET}/recordings/*"
    }
  ]
}
JSON
)

aws iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name "$POLICY_NAME" \
  --policy-document "$POLICY_JSON"

echo "==> Setting BUCKET_NAME on $WORKER_FN"
# transcript_store.hydrate is a no-op when the bucket is empty, so the
# variable matters as much as the IAM grant — without it the worker would
# still silently deliver no transcript.
# _merge_env.py takes the CURRENT env as JSON on argv[1] (not a function
# name) and prints the merged {"Variables": {...}} — the same two-step shape
# every other deploy script here uses. Merging rather than replacing is what
# stops this call blanking the worker's existing variables.
CURRENT_ENV="$(aws lambda get-function-configuration \
                 --function-name "$WORKER_FN" \
                 --query 'Environment.Variables' --output json)"
ENV_JSON="$(python "$(dirname "$0")/_merge_env.py" "$CURRENT_ENV" \
              "BUCKET_NAME=$BUCKET")"
aws lambda update-function-configuration \
  --function-name "$WORKER_FN" \
  --environment "$ENV_JSON" \
  --query "[FunctionName]" --output text
aws lambda wait function-updated-v2 --function-name "$WORKER_FN"

cat <<EOF

==> Done.

crmSyncWorker can now read offloaded transcripts, so both the mapping path
and generic delivery send the REAL transcript instead of an empty string.

Verify on the next push:
  aws logs tail /aws/lambda/$WORKER_FN --follow
and confirm no "transcript hydrate failed" line appears.
EOF
