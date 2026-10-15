#!/usr/bin/env bash
# =============================================================
# 89_deploy_task_call.sh — meeting tasks come from their own Groq call.
#
#   transcribeRecording only (code). No env, IAM, table or route changes.
#
# THE PROBLEM: tasks were one field of the single analysis call, extracted in
# the same breath as the title, overview, participants and highlights. On an
# 84-minute Marathi meeting (2026-10-08) that call returned 0 tasks in
# production and 0-4 vague ones on re-runs, while the meeting chat — the same
# model, but reading the analysis's ENGLISH overview first — listed them well.
#
# THE FIX: the analysis prompt no longer asks for tasks. A second call,
# extract_tasks(), runs after it with tasks as its only job and the analysis
# placed ahead of the transcript (prompts.TASKS_SYSTEM). Measured on the same
# meeting through this code path: 9 / 12 / 5 specific tasks across three runs.
# A task-call failure stores no tasks and never fails the meeting.
#
# WHAT IT SHIPS. Not the working tree: a clean export of HEAD with ONLY this
# change's files laid over it (FILES below). Unrelated uncommitted backend
# edits in your working copy therefore do NOT ship. Once this change is
# committed the overlay is a no-op and the package is exactly HEAD.
#
# WHAT IT DOES
#   1. Preflight: the change is in source; the task suites pass.
#   2. Build the clean package and run _preflight_check against it.
#   3. Back up the code that is live now (for rollback).
#   4. Deploy code only, then verify the deployed hash and that GROQ_MODEL
#      is untouched.
#
# ROLLBACK: the script prints the exact command, using the backup from step 3.
#
# Run:  bash scripts/89_deploy_task_call.sh        SKIP_TESTS=1 to skip pytest
# =============================================================
set -euo pipefail
cd "$(dirname "$0")"
. ./aws.sh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

FN="${TRANSCRIBE_FN:-transcribeRecording}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/minutex-deploy-backups}"

# The files this change touches, relative to cloud/. Everything else in the
# package comes from HEAD.
FILES=(
  shared/prompts.py
  shared/ai_schema.py
  functions/transcribe/lambda_function.py
  scripts/_preflight_check.py
)

# -------------------------------------------------------------
# 1. Preflight
# -------------------------------------------------------------
echo "==> Preflight"
HANDLER="$PROJECT_ROOT/functions/transcribe/lambda_function.py"
grep -q "^def extract_tasks(" "$HANDLER" \
  || { echo "FAIL: extract_tasks() missing from transcribe — change not in source." >&2; exit 1; }
grep -q "^TASKS_SYSTEM = " "$PROJECT_ROOT/shared/prompts.py" \
  || { echo "FAIL: TASKS_SYSTEM missing from prompts.py." >&2; exit 1; }
echo ">> extract_tasks() and TASKS_SYSTEM are in source."

if [[ "${SKIP_TESTS:-0}" != "1" ]]; then
  # test_task_extraction FIRST: its TestSeedingChain is order-sensitive (a
  # pre-existing fake-boto3 interaction), not affected by this change.
  ( cd "$PROJECT_ROOT" && python -m pytest -q -p no:cacheprovider \
      tests/test_task_extraction.py \
      tests/test_task_call.py \
      tests/test_eager_seeding_e2e.py \
      tests/test_speaker_confidence.py \
      tests/test_async_stt.py ) \
    || { echo "FAIL: task tests — not deploying." >&2; exit 1; }
fi

# -------------------------------------------------------------
# 2. Clean build: HEAD + this change's files only
# -------------------------------------------------------------
echo "==> Building a clean package (HEAD + ${#FILES[@]} changed files)"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
git -C "$REPO_ROOT" archive HEAD cloud/shared cloud/functions/transcribe cloud/scripts \
  | tar -x -C "$BUILD"
for f in "${FILES[@]}"; do
  cp "$PROJECT_ROOT/$f" "$BUILD/cloud/$f"
done
( cd "$BUILD/cloud/shared" && python ../scripts/_preflight_check.py ) \
  || { echo "FAIL: preflight on the built package — not deploying." >&2; exit 1; }
python "$SCRIPT_DIR/_package_lambda.py" functions/transcribe "$BUILD/cloud"
ZIP="$BUILD/cloud/functions/transcribe/function.zip"

# The zip must import, with the new call in it, before anything is uploaded.
mkdir "$BUILD/unzipped"
( cd "$BUILD/unzipped" && unzip -q "$ZIP" \
  && AWS_REGION="$AWS_REGION" python -c "import lambda_function as l; assert callable(l.extract_tasks)" ) \
  || { echo "FAIL: built package does not import — not deploying." >&2; exit 1; }
echo ">> Package imports cleanly and contains extract_tasks()."

# -------------------------------------------------------------
# 3. Back up what is live now
# -------------------------------------------------------------
echo "==> Backing up the live $FN code"
mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$BACKUP_DIR/$FN-$STAMP.zip"
curl -sf -o "$BACKUP" "$(aws lambda get-function --function-name "$FN" \
  --query Code.Location --output text | tr -d '\r')" \
  || { echo "FAIL: could not back up the live code — not deploying." >&2; exit 1; }
MODEL_BEFORE="$(aws lambda get-function-configuration --function-name "$FN" \
  --query Environment.Variables.GROQ_MODEL --output text | tr -d '\r')"
echo ">> Backup: $BACKUP"

# -------------------------------------------------------------
# 4. Deploy (code only) and verify
# -------------------------------------------------------------
echo "==> Deploying $FN (code only)"
aws lambda update-function-code --function-name "$FN" \
  --zip-file "fileb://$(winpath "$ZIP")" --publish \
  --query "[FunctionName,CodeSize,LastModified]" --output text
aws lambda wait function-updated-v2 --function-name "$FN"

LOCAL_SHA="$(python -c "import base64,hashlib,sys; print(base64.b64encode(hashlib.sha256(open(sys.argv[1],'rb').read()).digest()).decode())" "$ZIP")"
LIVE_SHA="$(aws lambda get-function-configuration --function-name "$FN" \
  --query CodeSha256 --output text | tr -d '\r')"
MODEL_AFTER="$(aws lambda get-function-configuration --function-name "$FN" \
  --query Environment.Variables.GROQ_MODEL --output text | tr -d '\r')"
[[ "$LOCAL_SHA" == "$LIVE_SHA" ]] \
  || { echo "ERROR: deployed code hash differs from the built package." >&2; exit 1; }
[[ "$MODEL_BEFORE" == "$MODEL_AFTER" ]] \
  || { echo "ERROR: GROQ_MODEL changed ($MODEL_BEFORE -> $MODEL_AFTER)." >&2; exit 1; }
echo ">> Deployed code matches the package; GROQ_MODEL still $MODEL_AFTER."

echo
echo "============================================================="
echo " Task call deployed to $FN."
echo
echo " TEST: reprocess a meeting in the app, then:"
echo "   MSYS_NO_PATHCONV=1 aws logs tail /aws/lambda/$FN --region $AWS_REGION --since 15m \\"
echo "     | grep -E 'tasks ok|task call FAILED|analyze ok'"
echo
echo " ROLLBACK:"
echo "   aws lambda update-function-code --region $AWS_REGION --function-name $FN \\"
echo "     --zip-file \"fileb://\$(cygpath -w '$BACKUP')\" --publish"
echo "============================================================="
