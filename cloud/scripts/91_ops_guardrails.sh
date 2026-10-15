#!/usr/bin/env bash
# =============================================================
# 91_ops_guardrails.sh — the AWS half of the P0 request-flood fix.
#
# WHY. On 8 Oct 2026 a client loop (~200 POST /me/push-tokens/s) filled the
# account's 10 Lambda slots; 38,010 requests were throttled in 45 minutes,
# throttling also happened on 25 Sep, 28 Sep–1 Oct, 5–6 Oct, and nobody was
# told. Nothing showed WHICH route it was. See docs/LOCAL_FIRST_AUDIT.md.
#
# WHAT (each step is a section; run all, or pick with --only):
#   quota      Request Lambda concurrent executions 10 -> $QUOTA_TARGET.
#              Free (you pay per run, not per slot). AWS approves it
#              asynchronously; this script only files the request.
#   reserve    Reserve $USERAPI_RESERVED slots for userApi, so an app bug can
#              never starve transcribeRecording again. ALSO caps userApi at
#              that many. Skipped until the quota is high enough (AWS keeps
#              at least 100 slots unreserved).
#   throttle   $default stage limit: $THROTTLE_RATE req/s, burst $THROTTLE_BURST.
#              Excess gets a 429 at the gateway: no Lambda run, no cost.
#              NOTE: this is a limit for the WHOLE stage (all users), not per
#              user — HTTP APIs have no per-client usage plans. It protects
#              the backend and the bill; per-user fairness is a later step.
#   accesslog  One JSON line per request: route TEMPLATE, status, latency.
#              No IP, no path (paths carry recording keys), no headers.
#              Log group $ACCESS_LOG_GROUP, $ACCESS_LOG_DAYS-day retention.
#   retention  Lambda log groups that keep logs FOREVER get $LAMBDA_LOG_DAYS
#              days. IRREVERSIBLE for logs older than that: they are deleted.
#   alarms     SNS topic $TOPIC_NAME (+ email subscription to $ALERT_EMAIL —
#              the address must click the confirmation link AWS sends) and:
#                - userApi Throttles > 0 in 5 min
#                - transcribeRecording Throttles > 0 in 5 min
#                - userApi Invocations > $INVOCATIONS_ALARM in 5 min
#                - API 5xx > 2% in 5 min (only once >= 50 requests)
#   budget     Monthly cost budget of $BUDGET_USD USD, emails at 80% actual and
#              100% forecast.
#
# SAFE BY DEFAULT. Without --apply nothing is changed: every section prints
# what it sees and what it WOULD do. With --apply it asks for "yes" once
# (skip with --yes). Every step is idempotent; re-running is harmless.
#
# Usage:
#   bash cloud/scripts/91_ops_guardrails.sh                      # plan only
#   ALERT_EMAIL=ops@example.com bash cloud/scripts/91_ops_guardrails.sh --apply
#   bash cloud/scripts/91_ops_guardrails.sh --apply --only quota
#   bash cloud/scripts/91_ops_guardrails.sh --apply --only reserve   # after approval
#
# Cost: alarms ~$0.10 each/month (5 → ~$0.50); access logs ~$0.20 per million
# requests ingested; SNS email free at this volume; budgets free (2 per account).
# =============================================================
set -euo pipefail
export MSYS_NO_PATHCONV=1   # Git Bash must not rewrite /aws/... log group names

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

# ---- settings (override by env) --------------------------------------------
API_NAME_GW="${API_NAME_GW:-iot-audio-api}"
STAGE="${STAGE:-\$default}"
USERAPI_FN="${USERAPI_FN:-userApi}"
TRANSCRIBE_FN="${TRANSCRIBE_FN:-transcribeRecording}"
QUOTA_CODE="L-B99A9384"                       # Lambda: Concurrent executions
QUOTA_TARGET="${QUOTA_TARGET:-1000}"
USERAPI_RESERVED="${USERAPI_RESERVED:-100}"
THROTTLE_RATE="${THROTTLE_RATE:-50}"
THROTTLE_BURST="${THROTTLE_BURST:-100}"
ACCESS_LOG_GROUP="${ACCESS_LOG_GROUP:-/aws/apigateway/${API_NAME_GW}-access}"
ACCESS_LOG_DAYS="${ACCESS_LOG_DAYS:-14}"
LAMBDA_LOG_DAYS="${LAMBDA_LOG_DAYS:-30}"
TOPIC_NAME="${TOPIC_NAME:-minutex-ops-alerts}"
ALERT_EMAIL="${ALERT_EMAIL:-}"
INVOCATIONS_ALARM="${INVOCATIONS_ALARM:-2000}"
BUDGET_NAME="${BUDGET_NAME:-minutex-monthly}"
BUDGET_USD="${BUDGET_USD:-20}"
ALL_SECTIONS="quota reserve throttle accesslog retention alarms budget"

# ---- args -------------------------------------------------------------------
APPLY="no"; ASSUME_YES="no"; ONLY=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY="yes" ;;
    --yes|-y) ASSUME_YES="yes" ;;
    --only) ONLY="${2:-}"; shift ;;
    -h|--help) sed -n '2,52p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done
SECTIONS="${ONLY//,/ }"; SECTIONS="${SECTIONS:-$ALL_SECTIONS}"
for s in $SECTIONS; do
  [[ " $ALL_SECTIONS " == *" $s "* ]] || { echo "Unknown section: $s (have: $ALL_SECTIONS)" >&2; exit 2; }
done
want() { [[ " $SECTIONS " == *" $1 "* ]]; }

# Every change goes through `run`: printed always, executed only with --apply.
run() {
  echo "    \$ aws $*"
  if [[ "$APPLY" == "yes" ]]; then aws "$@"; fi
}
note() { echo "    - $*"; }
# Reads whose output is captured: aws.exe on Windows ends lines with CR, and
# "None<CR>" != "None" would silently skip a step.
q() { aws "$@" | tr -d '\r'; }
hdr()  { echo ""; echo "== $* =="; }

# ---- preflight ----------------------------------------------------------------
ACCOUNT_ID="$(q sts get-caller-identity --query Account --output text)"
API_ID="$(q apigatewayv2 get-apis --query "Items[?Name=='${API_NAME_GW}'].ApiId | [0]" --output text)"
[[ -n "$API_ID" && "$API_ID" != "None" ]] || { echo "ERROR: API '$API_NAME_GW' not found in $AWS_REGION" >&2; exit 1; }
LIMIT="$(q lambda get-account-settings --query 'AccountLimit.ConcurrentExecutions' --output text)"

echo "Account $ACCOUNT_ID  region $AWS_REGION  API $API_NAME_GW ($API_ID)  stage $STAGE"
echo "Lambda concurrency limit now: $LIMIT"
echo "Mode: $([[ $APPLY == yes ]] && echo APPLY || echo 'PLAN (no changes; add --apply)')"
echo "Sections: $SECTIONS"

if want alarms || want budget; then
  if [[ -z "$ALERT_EMAIL" && "$APPLY" == "yes" ]]; then
    echo "ERROR: set ALERT_EMAIL=you@example.com for the alarms/budget sections (or --only the others)." >&2
    exit 2
  fi
  ALERT_EMAIL="${ALERT_EMAIL:-<ALERT_EMAIL>}"
fi

if [[ "$APPLY" == "yes" && "$ASSUME_YES" != "yes" ]]; then
  want retention && echo "WARNING: 'retention' permanently deletes Lambda logs older than $LAMBDA_LOG_DAYS days."
  read -r -p "Apply the sections above to account $ACCOUNT_ID? Type 'yes': " ans
  [[ "$ans" == "yes" ]] || { echo "Aborted."; exit 1; }
fi

# ---- quota --------------------------------------------------------------------
if want quota; then
  hdr "quota: Lambda concurrent executions -> $QUOTA_TARGET"
  PENDING="$(q service-quotas list-requested-service-quota-change-history-by-quota \
    --service-code lambda --quota-code "$QUOTA_CODE" \
    --query "RequestedQuotas[?Status=='PENDING' || Status=='CASE_OPENED'].DesiredValue | [0]" --output text)"
  if [[ "${LIMIT%.*}" -ge "$QUOTA_TARGET" ]]; then
    note "already $LIMIT — nothing to do"
  elif [[ -n "$PENDING" && "$PENDING" != "None" ]]; then
    note "a request for $PENDING is already open — not filing another"
  else
    run service-quotas request-service-quota-increase --service-code lambda \
      --quota-code "$QUOTA_CODE" --desired-value "$QUOTA_TARGET"
    note "AWS approves this asynchronously (often minutes, can be days). Then re-run with --only reserve."
  fi
fi

# ---- reserve ------------------------------------------------------------------
if want reserve; then
  hdr "reserve: $USERAPI_RESERVED slots for $USERAPI_FN"
  CUR="$(q lambda get-function-concurrency --function-name "$USERAPI_FN" \
    --query ReservedConcurrentExecutions --output text 2>/dev/null || echo None)"
  # AWS requires >= 100 unreserved slots to remain.
  if [[ "${LIMIT%.*}" -lt $(( USERAPI_RESERVED + 100 )) ]]; then
    note "SKIPPED: limit is $LIMIT; reserving $USERAPI_RESERVED needs >= $(( USERAPI_RESERVED + 100 )). Run 'quota' first, wait for approval."
  elif [[ "$CUR" == "$USERAPI_RESERVED" ]]; then
    note "already reserved: $CUR"
  else
    note "current reserved: $CUR"
    run lambda put-function-concurrency --function-name "$USERAPI_FN" \
      --reserved-concurrent-executions "$USERAPI_RESERVED"
  fi
fi

# ---- throttle -----------------------------------------------------------------
if want throttle; then
  hdr "throttle: stage $STAGE -> ${THROTTLE_RATE} rps, burst ${THROTTLE_BURST}"
  note "current: $(q apigatewayv2 get-stage --api-id "$API_ID" --stage-name "$STAGE" \
    --query 'DefaultRouteSettings' --output json | tr -d '\n ')"
  run apigatewayv2 update-stage --api-id "$API_ID" --stage-name "$STAGE" \
    --default-route-settings "DetailedMetricsEnabled=false,ThrottlingBurstLimit=${THROTTLE_BURST},ThrottlingRateLimit=${THROTTLE_RATE}"
fi

# ---- accesslog ----------------------------------------------------------------
if want accesslog; then
  hdr "accesslog: $ACCESS_LOG_GROUP (${ACCESS_LOG_DAYS} days)"
  EXISTS="$(q logs describe-log-groups --log-group-name-prefix "$ACCESS_LOG_GROUP" \
    --query "logGroups[?logGroupName=='${ACCESS_LOG_GROUP}'].logGroupName | [0]" --output text)"
  if [[ "$EXISTS" == "None" || -z "$EXISTS" ]]; then
    run logs create-log-group --log-group-name "$ACCESS_LOG_GROUP"
  else
    note "log group exists"
  fi
  run logs put-retention-policy --log-group-name "$ACCESS_LOG_GROUP" --retention-in-days "$ACCESS_LOG_DAYS"
  # Route TEMPLATE only ("GET /recordings/{key+}"): never $context.path (it
  # carries recording keys) and never the caller's IP.
  FORMAT='{"requestId":"$context.requestId","time":"$context.requestTime","route":"$context.routeKey","status":"$context.status","ms":"$context.responseLatency","integrationMs":"$context.integrationLatency","bytes":"$context.responseLength","integrationError":"$context.integrationErrorMessage"}'
  LOG_ARN="arn:aws:logs:${AWS_REGION}:${ACCOUNT_ID}:log-group:${ACCESS_LOG_GROUP}"
  SETTINGS_FILE="$(mktemp)"
  python - "$LOG_ARN" "$FORMAT" > "$SETTINGS_FILE" <<'PY'
import json, sys
print(json.dumps({"DestinationArn": sys.argv[1], "Format": sys.argv[2]}))
PY
  run apigatewayv2 update-stage --api-id "$API_ID" --stage-name "$STAGE" \
    --access-log-settings "file://$(winpath "$SETTINGS_FILE")"
  note "query later: fields route, status | filter status >= 429 | stats count(*) by route, status"
fi

# ---- retention ----------------------------------------------------------------
if want retention; then
  hdr "retention: Lambda log groups with no expiry -> ${LAMBDA_LOG_DAYS} days"
  FOUND=0
  while IFS=$'\t' read -r name days; do
    [[ -z "$name" ]] && continue
    if [[ "$days" == "None" ]]; then
      FOUND=1
      run logs put-retention-policy --log-group-name "$name" --retention-in-days "$LAMBDA_LOG_DAYS"
    else
      note "$name keeps $days days — unchanged"
    fi
  done < <(q logs describe-log-groups --log-group-name-prefix /aws/lambda/ \
             --query 'logGroups[].[logGroupName,retentionInDays]' --output text)
  [[ "$FOUND" == 1 ]] || note "every Lambda log group already has a retention"
fi

# ---- alarms -------------------------------------------------------------------
if want alarms; then
  hdr "alarms -> SNS $TOPIC_NAME -> $ALERT_EMAIL"
  TOPIC_ARN="arn:aws:sns:${AWS_REGION}:${ACCOUNT_ID}:${TOPIC_NAME}"
  run sns create-topic --name "$TOPIC_NAME"
  SUBSCRIBED="$(q sns list-subscriptions-by-topic --topic-arn "$TOPIC_ARN" \
    --query "Subscriptions[?Endpoint=='${ALERT_EMAIL}'].SubscriptionArn | [0]" --output text 2>/dev/null || echo None)"
  if [[ "$SUBSCRIBED" == "None" || -z "$SUBSCRIBED" ]]; then
    run sns subscribe --topic-arn "$TOPIC_ARN" --protocol email --notification-endpoint "$ALERT_EMAIL"
    note "AWS emails $ALERT_EMAIL a confirmation link — nothing is delivered until it is clicked."
  else
    note "$ALERT_EMAIL already subscribed ($SUBSCRIBED)"
  fi

  lambda_alarm() {  # name fn metric threshold description
    run cloudwatch put-metric-alarm --alarm-name "$1" --alarm-description "$5" \
      --namespace AWS/Lambda --metric-name "$3" --dimensions "Name=FunctionName,Value=$2" \
      --statistic Sum --period 300 --evaluation-periods 1 --threshold "$4" \
      --comparison-operator GreaterThanThreshold --treat-missing-data notBreaching \
      --alarm-actions "$TOPIC_ARN" --ok-actions "$TOPIC_ARN"
  }
  lambda_alarm "minutex-userApi-throttles" "$USERAPI_FN" Throttles 0 \
    "userApi requests are being refused (Service Unavailable in the app). See docs/LOCAL_FIRST_AUDIT.md."
  lambda_alarm "minutex-transcribe-throttles" "$TRANSCRIBE_FN" Throttles 0 \
    "Meeting processing is being refused: recordings will not transcribe."
  lambda_alarm "minutex-userApi-invocation-spike" "$USERAPI_FN" Invocations "$INVOCATIONS_ALARM" \
    "Unusual userApi traffic: possible client request loop. Check the API access log by route."

  METRICS_FILE="$(mktemp)"
  python - "$API_ID" > "$METRICS_FILE" <<'PY'
import json, sys
api = sys.argv[1]
def m(i, name):
    return {"Id": i, "ReturnData": False, "MetricStat": {"Metric": {
        "Namespace": "AWS/ApiGateway", "MetricName": name,
        "Dimensions": [{"Name": "ApiId", "Value": api}]}, "Period": 300, "Stat": "Sum"}}
print(json.dumps([m("errors", "5xx"), m("total", "Count"),
    {"Id": "rate", "ReturnData": True, "Label": "5xx % (when >= 50 requests)",
     "Expression": "IF(total >= 50, 100 * errors / total, 0)"}]))
PY
  run cloudwatch put-metric-alarm --alarm-name "minutex-api-5xx-rate" \
    --alarm-description "More than 2% of API requests failed with 5xx in 5 minutes." \
    --metrics "file://$(winpath "$METRICS_FILE")" --evaluation-periods 1 --threshold 2 \
    --comparison-operator GreaterThanThreshold --treat-missing-data notBreaching \
    --alarm-actions "$TOPIC_ARN" --ok-actions "$TOPIC_ARN"
fi

# ---- budget -------------------------------------------------------------------
if want budget; then
  hdr "budget: $BUDGET_NAME, $BUDGET_USD USD/month -> $ALERT_EMAIL"
  # Budgets is a global API served from us-east-1.
  budgets() { "$AWS_BIN" --region us-east-1 budgets "$@"; }
  if budgets describe-budget --account-id "$ACCOUNT_ID" --budget-name "$BUDGET_NAME" >/dev/null 2>&1; then
    note "budget exists — leaving it as is (edit it in the console to change the amount)"
  else
    BUDGET_FILE="$(mktemp)"; NOTIF_FILE="$(mktemp)"
    python - "$BUDGET_NAME" "$BUDGET_USD" "$ALERT_EMAIL" "$BUDGET_FILE" "$NOTIF_FILE" <<'PY'
import json, sys
name, usd, email, bf, nf = sys.argv[1:]
json.dump({"BudgetName": name, "BudgetType": "COST", "TimeUnit": "MONTHLY",
           "BudgetLimit": {"Amount": usd, "Unit": "USD"}}, open(bf, "w"))
sub = [{"SubscriptionType": "EMAIL", "Address": email}]
json.dump([
  {"Notification": {"NotificationType": "ACTUAL", "ComparisonOperator": "GREATER_THAN",
                    "Threshold": 80, "ThresholdType": "PERCENTAGE"}, "Subscribers": sub},
  {"Notification": {"NotificationType": "FORECASTED", "ComparisonOperator": "GREATER_THAN",
                    "Threshold": 100, "ThresholdType": "PERCENTAGE"}, "Subscribers": sub},
], open(nf, "w"))
PY
    echo "    \$ aws --region us-east-1 budgets create-budget --account-id $ACCOUNT_ID --budget file://... --notifications-with-subscribers file://..."
    if [[ "$APPLY" == "yes" ]]; then
      budgets create-budget --account-id "$ACCOUNT_ID" \
        --budget "file://$(winpath "$BUDGET_FILE")" \
        --notifications-with-subscribers "file://$(winpath "$NOTIF_FILE")"
    fi
  fi
fi

# ---- verify -------------------------------------------------------------------
hdr "state now"
echo "    concurrency limit: $(q lambda get-account-settings --query 'AccountLimit.ConcurrentExecutions' --output text)"
echo "    userApi reserved:  $(q lambda get-function-concurrency --function-name "$USERAPI_FN" --query ReservedConcurrentExecutions --output text 2>/dev/null || echo None)"
echo "    stage:             $(q apigatewayv2 get-stage --api-id "$API_ID" --stage-name "$STAGE" \
  --query '{throttle:DefaultRouteSettings,log:AccessLogSettings.DestinationArn}' --output json | tr -d '\n ')"
echo "    alarms:            $(q cloudwatch describe-alarms --alarm-name-prefix minutex- --query 'MetricAlarms[].[AlarmName,StateValue]' --output text | tr '\n\t' '; ')"
if [[ "$APPLY" != "yes" ]]; then
  echo ""
  echo "PLAN ONLY — nothing was changed. Re-run with --apply after review."
fi
