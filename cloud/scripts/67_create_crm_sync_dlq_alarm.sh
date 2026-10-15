#!/usr/bin/env bash
# =============================================================
# 67_create_crm_sync_dlq_alarm.sh — CloudWatch alarm on CrmSyncDLQ
# (Phase 2D.4 deployment hardening).
#
# THIS IS THE FIRST CLOUDWATCH ALARM IN THIS CODEBASE. Verified before
# writing this script: no script anywhere calls `aws cloudwatch
# put-metric-alarm`, and no SNS topic exists in .env or .env.example. There
# is no existing alarm-naming or notification convention to match — this
# script establishes one, following the same aws.sh/idempotency style every
# other resource-creation script in this repo uses (see scripts/63-66).
#
# WHAT THIS ALARMS ON, AND WHY.
#   A message only reaches CrmSyncDLQ (scripts/64) after
#   CRM_SYNC_MAX_RECEIVES (default 5) delivery attempts without a
#   successful deletion — see docs/WORKSPACE_PHASE2D.md's Phase 2D.4
#   section. That is already well past the job-level retry policy
#   (CRM_JOB_MAX_ATTEMPTS=3, backoff 60s/300s/900s) giving up on its own —
#   a DLQ arrival means either (a) a bug the worker's own error
#   classification didn't handle cleanly (an exception escaping before
#   _finish_crm_sync_job could even persist a terminal status), or (b) the
#   worker itself is failing to run at all (crashing, throttled, or its
#   IAM/config broken) so messages exhaust delivery attempts without the
#   job row ever being updated. EITHER WAY THIS IS ALWAYS AN OPERATOR
#   SIGNAL, never an expected steady-state condition — a normal permanent
#   failure (permission/validation/reauth) resolves to a terminal job
#   status (FAILED/RECONNECT_REQUIRED) WITHOUT ever touching the DLQ, so a
#   nonzero DLQ depth is never "business as usual, some pushes fail."
#
#   Two alarms, not one metric watched two ways — kept deliberately simple
#   (no composite alarm, no anomaly detection, nothing this codebase has no
#   existing pattern for):
#     1. CrmSyncDLQ-HasMessages — ApproximateNumberOfMessagesVisible >= 1
#        for one 5-minute period. The PRIMARY signal: "at least one message
#        needs a human." Fast to trip (single period) because there is no
#        legitimate reason for this metric to ever be nonzero even briefly
#        for long — unlike a queue backlog metric, a nonzero DLQ count is
#        itself already the alarm condition, not a threshold on a normal
#        fluctuating value.
#     2. CrmSyncDLQ-OldMessage — ApproximateAgeOfOldestMessage > 3600s
#        (1 hour) for one period. A SECONDARY signal in case alarm #1's
#        notification was missed/muted: catches a DLQ message that has sat
#        unaddressed for a while, independent of whether the count changed.
#
# WHAT THE ALARM MEANS / EXPECTED OPERATOR ACTION (also in
# docs/WORKSPACE_PHASE2D.md's Phase 2D.4 section — see "Smoke test" and
# "DLQ monitoring"):
#   1. Read the DLQ message(s) (do not delete them) to get each one's
#      job_id: `aws sqs receive-message --queue-url <DLQ_URL>
#      --max-number-of-messages 10 --visibility-timeout 0` (0-second
#      visibility timeout so peeking does not hide the message from a
#      later real receive).
#   2. Look up each job_id in CrmSyncJobs (GetItem) for the actual failure
#      history: status, attempt_count, last_error_category/code/message.
#   3. If the job row shows a normal terminal state (FAILED/
#      RECONNECT_REQUIRED) already reached before the DLQ arrival, the DLQ
#      copy is redundant noise from an SQS-level redrive race — safe to
#      purge that one message. If the job row is stuck (still SYNCING/
#      RETRYING, or attempt_count doesn't match CRM_JOB_MAX_ATTEMPTS),
#      investigate the worker Lambda's own CloudWatch Logs (filter on
#      `[crm-sync-worker]` / `[crm-sync]` around that job_id) for what
#      actually happened — a real bug, an IAM/config break, or an
#      exhausted-Lambda-timeout pattern.
#   4. Once the underlying cause is fixed, redrive the message back onto
#      CrmSyncQueue (console "Start DLQ redrive", or
#      `aws sqs start-message-move-task`) — safe at any time, since
#      execute_crm_sync_job re-reads current job/meeting/workspace state
#      and re-checks idempotency rather than trusting the message body.
#   There is no automatic DLQ replay — a human always decides when to
#   redrive, per the original Phase 2D.4 spec.
#
# NOTIFICATION: optional, via CRM_SYNC_ALARM_SNS_TOPIC_ARN. No SNS topic is
# created by this script — none exists anywhere in this codebase yet, and
# creating one (plus subscription management, email/Slack wiring) is a
# genuinely separate decision for whoever owns on-call for this account,
# not something to guess at here. If CRM_SYNC_ALARM_SNS_TOPIC_ARN is unset,
# the alarms are still created and visible/searchable in the CloudWatch
# console and API (so "does an alarm exist" is never blocked on that
# decision) — they simply have no configured action until a topic ARN is
# supplied and this script is re-run (it is idempotent: put-metric-alarm
# with the same name updates the existing alarm's actions in place).
#
# LEAST PRIVILEGE: this script itself only needs CloudWatch
# PutMetricAlarm/DescribeAlarms (typically covered by a broad deploy-time
# credential, not a running Lambda's own role) — no new Lambda IAM
# permissions are needed, since alarms watch a queue's own CloudWatch
# metrics (published by SQS itself) rather than requiring the worker to
# publish anything.
#
# NO SECRETS: alarm names/descriptions/dimensions carry only queue names
# and thresholds — never a token, credential, or Salesforce data.
#
# Idempotent: put-metric-alarm with an existing alarm name updates it
# in place rather than creating a duplicate.
#
# IMPORTANT: this script is NOT run as part of this change. It creates no
# AWS resource until explicitly executed by an operator after 64_create_
# crm_sync_queue.sh (the DLQ must exist first).
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

CRM_SYNC_DLQ_NAME="${CRM_SYNC_DLQ_NAME:-CrmSyncDLQ}"
# How long a DLQ message may sit before the secondary "old message" alarm
# trips, independent of count changes. 1 hour default — long enough that a
# human has clearly had time to see and act on alarm #1's notification.
CRM_SYNC_DLQ_MAX_AGE_SECONDS="${CRM_SYNC_DLQ_MAX_AGE_SECONDS:-3600}"
# Optional. Leave unset to create the alarms with no notification action
# (still visible in the console/API) — see the header comment above.
CRM_SYNC_ALARM_SNS_TOPIC_ARN="${CRM_SYNC_ALARM_SNS_TOPIC_ARN:-}"

echo ">> Region: $AWS_REGION"
echo ">> DLQ:    $CRM_SYNC_DLQ_NAME"

DLQ_URL="$(aws sqs get-queue-url --queue-name "$CRM_SYNC_DLQ_NAME" \
             --query 'QueueUrl' --output text 2>/dev/null || true)"
if [[ -z "$DLQ_URL" || "$DLQ_URL" == "None" ]]; then
  echo "ERROR: queue '$CRM_SYNC_DLQ_NAME' does not exist — run" >&2
  echo "       64_create_crm_sync_queue.sh first." >&2
  exit 1
fi

ALARM_ACTIONS=()
if [[ -n "$CRM_SYNC_ALARM_SNS_TOPIC_ARN" ]]; then
  ALARM_ACTIONS=(--alarm-actions "$CRM_SYNC_ALARM_SNS_TOPIC_ARN")
  echo ">> Notifications: $CRM_SYNC_ALARM_SNS_TOPIC_ARN"
else
  echo ">> Notifications: none configured (set CRM_SYNC_ALARM_SNS_TOPIC_ARN"
  echo "   and re-run to attach one — alarms are still created either way)."
fi

# -------------------------------------------------------------
# 1. CrmSyncDLQ-HasMessages — the primary "a human needs to look" signal.
# -------------------------------------------------------------
echo ">> Creating/updating alarm: CrmSyncDLQ-HasMessages"
aws cloudwatch put-metric-alarm \
  --alarm-name "CrmSyncDLQ-HasMessages" \
  --alarm-description "One or more CRM sync jobs exhausted all delivery attempts and landed in CrmSyncDLQ. This is always an operator signal, never expected steady state — see docs/WORKSPACE_PHASE2D.md Phase 2D.4 'DLQ monitoring' for what to check and how to redrive." \
  --namespace "AWS/SQS" \
  --metric-name "ApproximateNumberOfMessagesVisible" \
  --dimensions "Name=QueueName,Value=${CRM_SYNC_DLQ_NAME}" \
  --statistic Maximum \
  --period 300 \
  --evaluation-periods 1 \
  --datapoints-to-alarm 1 \
  --threshold 1 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --treat-missing-data notBreaching \
  "${ALARM_ACTIONS[@]}" >/dev/null

# -------------------------------------------------------------
# 2. CrmSyncDLQ-OldMessage — secondary signal in case #1 was missed/muted.
# -------------------------------------------------------------
echo ">> Creating/updating alarm: CrmSyncDLQ-OldMessage"
aws cloudwatch put-metric-alarm \
  --alarm-name "CrmSyncDLQ-OldMessage" \
  --alarm-description "A message has sat in CrmSyncDLQ for over ${CRM_SYNC_DLQ_MAX_AGE_SECONDS}s. Secondary signal alongside CrmSyncDLQ-HasMessages, in case that notification was missed — see docs/WORKSPACE_PHASE2D.md Phase 2D.4 'DLQ monitoring'." \
  --namespace "AWS/SQS" \
  --metric-name "ApproximateAgeOfOldestMessage" \
  --dimensions "Name=QueueName,Value=${CRM_SYNC_DLQ_NAME}" \
  --statistic Maximum \
  --period 300 \
  --evaluation-periods 1 \
  --datapoints-to-alarm 1 \
  --threshold "$CRM_SYNC_DLQ_MAX_AGE_SECONDS" \
  --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching \
  "${ALARM_ACTIONS[@]}" >/dev/null

echo
echo ">> Done."
echo ">> Verify:"
echo "     aws cloudwatch describe-alarms --alarm-names CrmSyncDLQ-HasMessages CrmSyncDLQ-OldMessage"
