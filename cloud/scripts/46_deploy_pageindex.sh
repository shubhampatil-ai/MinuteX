#!/usr/bin/env bash
# =============================================================
# 46_deploy_pageindex.sh — grounded Meeting AI: per-meeting PageIndex
# retrieval, on userApi + transcribeRecording.
#
# WHAT THIS IS FOR
# ----------------
# Meeting AI (/recordings/ai/chat/{key+}) built its context by head-truncating
# the transcript: prompts.build_context cut it to the first N characters and
# told the model its record was partial. On the free tier N is ~15,000 chars,
# so a 45-minute meeting was ALREADY being cut, and "what did we decide near
# the end?" was unanswerable on anything longer. The words were in S3; nothing
# fetched them.
#
# This ships the retrieval layer that fixes it: each meeting's transcript is
# indexed into its own PageIndex tree (shared/pageindex.py), stored in S3, and
# searched per question so relevant content is found ANYWHERE in the meeting.
# Answers come back with segment ids that deep-link into the transcript UI that
# already exists.
#
# NO NEW TABLE, NO NEW BUCKET, NO VECTOR DB. The tree is one gzipped S3 object
# (0.4-1.6 KB measured, 15min-4h) beside the transcript it indexes, plus a
# ~300-byte pointer attribute on the recording row. The row itself must not
# grow with meeting length — that is the 400 KB ceiling transcript_store.py
# was written to get out from under, and this respects the same rule.
#
# WHAT CHANGES AT RUNTIME
#   * transcribeRecording builds the index AFTER its terminal write, inside a
#     try/except that swallows. It cannot strand a recording or cost a re-run
#     of the paid analysis stages.
#   * userApi retrieves against it, and builds one lazily for any meeting that
#     has none (the whole back catalogue) on the first question.
#   * A long-meeting question now makes TWO Groq calls (navigate, then answer)
#     instead of one. Short meetings still make one — if the transcript fits
#     the budget it is sent whole, because retrieval could only subtract there.
#
# IAM — THE PART THAT IS NOT OPTIONAL
# -----------------------------------
# Both roles could read the bucket but neither could WRITE the new prefix:
#   * transcribeRecording had s3:PutObject on transcripts/* only.
#   * userApi had s3:GetObject and NO s3:PutObject at all.
# Without the grants below the feature still ANSWERS (every failure path falls
# back), but no index is ever stored, so every long-meeting question rebuilds
# from scratch and throws the result away. Silent, and exactly the kind of
# "works in staging, expensive in production" failure worth naming here.
#
# userApi-permanent-delete is also widened to pageindex/*, or an index object
# outlives the recording it describes: invisible in the app, still billed.
#
# GRANTS ARE PREFIX-SCOPED, never bucket-wide. userApi gets PutObject on
# pageindex/* and nothing else — it has no business writing audio or
# transcripts, and a wildcard here would quietly hand it that power.
#
# NEW ENV — ALL OPTIONAL, all defaulted in code. Set explicitly so the
# deployed config states what it is using rather than relying on a default
# that could drift:
#   PAGEINDEX_PREFIX             (pageindex)  where trees live in S3
#   PAGEINDEX_LOCK_TTL           (300)        single-flight claim expiry
#   PAGEINDEX_RETRIEVAL_DEADLINE (7)          seconds for the navigation call
#   PAGEINDEX_LLM_SUMMARY        (0)          see below — leave it OFF
#
# PAGEINDEX_LLM_SUMMARY DELIBERATELY STAYS OFF. The official PageIndex builds
# its tree by LLM-summarising every node. On this account's Groq quota
# (GROQ_TPM_LIMIT=12000) that is ~40 paced calls to index ONE 3-hour meeting —
# minutes of Lambda wall-clock, real 429 exposure, and Groq spend on every
# meeting whether or not anyone ever asks it a question. The tree is built
# structurally instead (deterministic, ~25ms for a 2-hour meeting, zero Groq
# calls). Turning the flag on changes only the node DESCRIPTIONS, not the tree
# shape, so it neither invalidates stored indexes nor requires a rebuild.
#
# BACKFILL IS NOT RUN HERE, on purpose. Lazy generation means the back
# catalogue already works without it; a bulk backfill is a separate, explicit
# decision. See scripts/45_backfill_pageindex.py --dry-run.
#
# Prerequisites:
#   - scripts/21_deploy_ai_workspace.sh  (AI routes + GROQ_API_KEY on userApi)
#   - scripts/28_deploy_async_stt.sh     (transcript_store / S3 transcripts)
#
# Idempotent: put-role-policy, update-function-code and
# update-function-configuration all overwrite. Safe to re-run.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"
TRANSCRIBE_LAMBDA_NAME="${TRANSCRIBE_LAMBDA_NAME:-transcribeRecording}"
BUCKET="${BUCKET_NAME:-}"

if [[ -z "$BUCKET" ]]; then
  echo "ERROR: BUCKET_NAME is not set (.env at the repo root)." >&2
  exit 1
fi

echo ">> userApi:    $USERAPI_LAMBDA_NAME"
echo ">> transcribe: $TRANSCRIBE_LAMBDA_NAME"
echo ">> bucket:     $BUCKET"

# -------------------------------------------------------------
# 0. Offline tests before anything is provisioned.
#    Same gate scripts 38/40/42/43 apply — these need no AWS, so there is no
#    reason to discover a broken build after the deploy.
# -------------------------------------------------------------
echo ">> Running the PageIndex suite (offline) ..."
( cd "$PROJECT_ROOT" && python -m pytest tests/test_pageindex.py -q )
echo ">> Running the suites it must not regress ..."
( cd "$PROJECT_ROOT" && python -m pytest \
    tests/test_ai_workspace.py tests/test_ai_assistant.py \
    tests/test_evidence_segments.py tests/test_transcript_store.py \
    tests/test_mom_api.py -q )
echo ">> Tests passed."

# -------------------------------------------------------------
# 1. IAM — writing and reading the index objects.
#
# Scoped to the pageindex/ prefix on both roles. See the header: neither role
# could write it before, and without these the index is never stored.
# -------------------------------------------------------------
UA_ROLE="$(aws lambda get-function --function-name "$USERAPI_LAMBDA_NAME" \
             --query 'Configuration.Role' --output text)"
UA_ROLE="${UA_ROLE##*/}"
TR_ROLE="$(aws lambda get-function --function-name "$TRANSCRIBE_LAMBDA_NAME" \
             --query 'Configuration.Role' --output text)"
TR_ROLE="${TR_ROLE##*/}"

UA_POLICY="$(mktemp)"
TR_POLICY="$(mktemp)"
DEL_POLICY="$(mktemp)"
trap 'rm -f "$UA_POLICY" "$TR_POLICY" "$DEL_POLICY"' EXIT

# userApi: lazy generation writes the tree; retrieval reads it back.
cat > "$UA_POLICY" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "PageIndexReadWrite", "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": "arn:aws:s3:::${BUCKET}/pageindex/*" }
  ]
}
EOF
aws iam put-role-policy --role-name "$UA_ROLE" \
  --policy-name "userApi-pageindex" \
  --policy-document "file://$(winpath "$UA_POLICY")"
echo ">> $USERAPI_LAMBDA_NAME ($UA_ROLE): may read/write pageindex/*"

# transcribeRecording: builds the index as each meeting finishes.
cat > "$TR_POLICY" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "PageIndexWrite", "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject"],
      "Resource": "arn:aws:s3:::${BUCKET}/pageindex/*" }
  ]
}
EOF
aws iam put-role-policy --role-name "$TR_ROLE" \
  --policy-name "transcribe-pageindex" \
  --policy-document "file://$(winpath "$TR_POLICY")"
echo ">> $TRANSCRIBE_LAMBDA_NAME ($TR_ROLE): may write pageindex/*"

# Permanent delete must take the index with the recording. REWRITTEN in full
# (put-role-policy replaces, it does not merge) so the two original prefixes
# are restated here — dropping either would break permanent delete outright.
cat > "$DEL_POLICY" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "PermanentDeleteObjects", "Effect": "Allow",
      "Action": "s3:DeleteObject",
      "Resource": [
        "arn:aws:s3:::${BUCKET}/recordings/*",
        "arn:aws:s3:::${BUCKET}/transcripts/*",
        "arn:aws:s3:::${BUCKET}/pageindex/*"
      ] },
    { "Sid": "PermanentDeleteRow", "Effect": "Allow",
      "Action": "dynamodb:DeleteItem",
      "Resource": "arn:aws:dynamodb:${AWS_REGION}:$(aws sts get-caller-identity --query Account --output text):table/Recordings" }
  ]
}
EOF
aws iam put-role-policy --role-name "$UA_ROLE" \
  --policy-name "userApi-permanent-delete" \
  --policy-document "file://$(winpath "$DEL_POLICY")"
echo ">> $USERAPI_LAMBDA_NAME ($UA_ROLE): permanent delete now covers pageindex/*"

# IAM is eventually consistent. A deploy that raced it would look like a
# permissions bug in the first few invocations and then fix itself, which is
# the most confusing possible symptom.
echo ">> Waiting for IAM to propagate ..."
sleep 10

# -------------------------------------------------------------
# 2. Environment — stated explicitly rather than left to code defaults.
# -------------------------------------------------------------
set_pageindex_env() {
  local fn="$1"
  local current merged
  current="$(aws lambda get-function-configuration --function-name "$fn" \
               --query 'Environment.Variables' --output json)"
  merged="$(python - "$current" <<'PY'
import json, sys
env = json.loads(sys.argv[1]) if sys.argv[1] not in ("null", "") else {}
env.update({
    "PAGEINDEX_PREFIX": "pageindex",
    "PAGEINDEX_LOCK_TTL": "300",
    "PAGEINDEX_RETRIEVAL_DEADLINE": "7",
    # OFF. See the header — on a 12k TPM quota this would make ~40 Groq calls
    # to index one 3-hour meeting, for node descriptions the extractive path
    # already provides.
    "PAGEINDEX_LLM_SUMMARY": "0",
})
print(json.dumps({"Variables": env}))
PY
)"
  aws lambda update-function-configuration \
    --function-name "$fn" --environment "$merged" \
    --query "[FunctionName,Environment.Variables.PAGEINDEX_PREFIX]" --output text
  aws lambda wait function-updated-v2 --function-name "$fn"
}

set_pageindex_env "$USERAPI_LAMBDA_NAME"
set_pageindex_env "$TRANSCRIBE_LAMBDA_NAME"

# -------------------------------------------------------------
# 3. Deploy both functions, shared modules vendored FLAT.
#    A zip with only lambda_function.py fails on cold start and takes down
#    every route — same helper as scripts 21/23/38/40/42/43. The new
#    pageindex.py / pageindex_store.py are picked up automatically because
#    _package_lambda.py globs shared/*.py.
# -------------------------------------------------------------
PACKAGE_PY="$SCRIPT_DIR/_package_lambda.py"

deploy_py_with_shared() {
  local fn="$1" src_dir="$2"
  local zip="$PROJECT_ROOT/$src_dir/function.zip"
  rm -f "$zip"
  ( cd "$PROJECT_ROOT" && python "$PACKAGE_PY" "$src_dir" "$PROJECT_ROOT" )
  aws lambda update-function-code \
    --function-name "$fn" \
    --zip-file "fileb://$(winpath "$zip")" \
    --publish \
    --query "[FunctionName,CodeSize,LastModified]" --output text
  aws lambda wait function-updated-v2 --function-name "$fn"
  echo ">> $fn deployed from $src_dir/ + shared/"
}

# userApi FIRST. It is the READER of the index; deploying the writer first
# would open a window where transcribeRecording stamps a `pageindex` pointer
# that the deployed userApi does not know to use. Harmless (it would be
# ignored), but pointless to arrange deliberately.
deploy_py_with_shared "$USERAPI_LAMBDA_NAME" "functions/userapi"
deploy_py_with_shared "$TRANSCRIBE_LAMBDA_NAME" "functions/transcribe"

# -------------------------------------------------------------
# 4. Verify the new modules actually loaded.
#
# A cold-start import failure is the one way this deploy could take down every
# AI route, and it would not show up until a user hit one. The probe is an
# UNAUTHENTICATED chat request: it must come back 401 from the auth layer,
# which proves the module imported and the router ran. A FunctionError or a
# 502 would mean the import died.
# -------------------------------------------------------------
PROBE_OUT="$(mktemp)"
trap 'rm -f "$UA_POLICY" "$TR_POLICY" "$DEL_POLICY" "$PROBE_OUT"' EXIT
PROBE_PAYLOAD='{"routeKey":"POST /recordings/ai/chat/{key+}","requestContext":{"http":{"method":"POST","path":"/"}},"pathParameters":{"key":"recordings/__deploy_probe__/none.m4a"},"headers":{},"body":"{\"message\":\"probe\"}"}'

ERR="$(aws lambda invoke \
        --function-name "$USERAPI_LAMBDA_NAME" \
        --payload "$(printf '%s' "$PROBE_PAYLOAD" | base64 | tr -d '\n')" \
        --query 'FunctionError' --output text "$PROBE_OUT" 2>/dev/null || true)"
BODY="$(cat "$PROBE_OUT")"

if [[ "$ERR" != "None" && -n "$ERR" ]]; then
  echo "ERROR: the chat route raised on invoke — the new modules may not have" >&2
  echo "       imported. AI routes are likely DOWN. Body:" >&2
  echo "$BODY" >&2
  exit 1
fi

case "$BODY" in
  *401*)
    echo ">> Probe OK: chat route live, auth enforced (401 as expected)." ;;
  *ImportError*|*ModuleNotFoundError*|*pageindex*)
    echo "ERROR: import failure in the deployed package:" >&2
    echo "$BODY" >&2
    exit 1 ;;
  *)
    echo ">> Probe returned: $BODY"
    echo "   (expected a 401 — check the above if it looks wrong)" ;;
esac

echo ""
echo ">> Done. Meeting AI now retrieves instead of head-truncating."
echo ""
echo "   New meetings are indexed as they finish processing."
echo "   Existing meetings are indexed LAZILY on their first question, so the"
echo "   back catalogue works with no backfill."
echo ""
echo "   Optional, to pre-warm the back catalogue instead of paying on first"
echo "   question — START WITH --dry-run:"
echo "     python scripts/45_backfill_pageindex.py --dry-run"
echo ""
echo "   Watch it work:"
echo "     aws logs filter-log-pattern --log-group-name /aws/lambda/$USERAPI_LAMBDA_NAME \\"
echo "       --filter-pattern '[ai_metrics]'"
