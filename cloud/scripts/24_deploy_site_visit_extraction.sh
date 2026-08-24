#!/usr/bin/env bash
# =============================================================
# 24_deploy_site_visit_extraction.sh — Phase 3: Site Visit Number extraction.
#
# Ships the AI extraction step that feeds CRM linking. Both Lambdas change,
# because both vendor the same shared modules:
#
#   transcribeRecording <- functions/transcribe/lambda_function.py
#       + extract_site_visit(): a NEW small Groq call (Stage 2b) after
#         summary/highlights, writing the `site_visit` attribute. Degrades
#         independently — a failure keeps the brief and leaves the app's
#         manual-entry path as the only route.
#   userApi             <- functions/userapi/lambda_function.py
#       + PATCH /recordings/{key+} {site_visit_number} — manual entry and
#         correction (null/"" clears it). No new route to wire: the PATCH
#         route already exists, this only adds a field to it.
#
#   shared/prompts.py    + SITE_VISIT_SYSTEM
#   shared/ai_schema.py  + coerce_site_visit / site_visit_found
#
# No new IAM, no new table, no new routes — the extraction writes an
# attribute on the EXISTING Recordings row, and both roles already have what
# they need (Groq is called over the internet with a key from env; DynamoDB
# UpdateItem on Recordings is already granted).
#
# NEW env vars on transcribeRecording (all optional, defaults shown):
#   SITE_VISIT_DEADLINE=20     wall-clock ceiling for the extra Groq call.
#                              Deliberately a SMALL slice ON TOP of
#                              GROQ_DEADLINE_SECONDS' existing 60/40
#                              summary/highlights split, not carved out of
#                              it — taking from either would trade a
#                              guaranteed output for an optional one.
#   SITE_VISIT_HEAD_CHARS=6000 only the transcript's head and tail are
#   SITE_VISIT_TAIL_CHARS=2000 searched; a visit number is stated when the
#                              visit is identified, never buried mid-meeting.
#
# HEADS-UP on the Lambda timeout: this adds a third Groq call to the
# S3-triggered pipeline, so worst-case wall clock grows by up to
# SITE_VISIT_DEADLINE. The function's timeout must exceed
# GROQ_DEADLINE_SECONDS + SITE_VISIT_DEADLINE + transcription time; this
# script CHECKS that and refuses to deploy if the margin is too thin rather
# than letting the pipeline start dying at the very end of a long meeting.
#
# Prerequisites: scripts 12-21.
# Idempotent: update-function-code / update-function-configuration overwrite.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"
TRANSCRIBE_LAMBDA_NAME="${TRANSCRIBE_LAMBDA_NAME:-transcribeRecording}"
SITE_VISIT_DEADLINE="${SITE_VISIT_DEADLINE:-20}"
SITE_VISIT_HEAD_CHARS="${SITE_VISIT_HEAD_CHARS:-6000}"
SITE_VISIT_TAIL_CHARS="${SITE_VISIT_TAIL_CHARS:-2000}"

# Same packaging helper as script 21 — shared modules FLAT at the archive root,
# because that is how `import ai_schema` resolves in the Lambda runtime.
deploy_py_with_shared() {
  local fn="$1" src_dir="$2"
  local zip="$PROJECT_ROOT/$src_dir/function.zip"
  rm -f "$zip"
  ( cd "$PROJECT_ROOT" && python - "$src_dir" <<'PY'
import sys, zipfile
from pathlib import Path

src_dir = sys.argv[1]
root = Path.cwd()
out = root / src_dir / "function.zip"
shared = root / "shared"

with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    z.write(root / src_dir / "lambda_function.py", "lambda_function.py")
    for mod in sorted(shared.glob("*.py")):
        z.write(mod, mod.name)
        print(f"   + {mod.name}")
print(f">> packaged {out.relative_to(root)}")
PY
  )
  aws lambda update-function-code \
    --function-name "$fn" \
    --zip-file "fileb://$(winpath "$zip")" \
    --publish \
    --query "[FunctionName,CodeSize,LastModified]" --output text
  aws lambda wait function-updated-v2 --function-name "$fn"
  echo ">> $fn deployed from $src_dir/ + shared/"
}

# -------------------------------------------------------------
# 1. Timeout headroom check on transcribeRecording.
# -------------------------------------------------------------
TR_CFG="$(aws lambda get-function-configuration \
            --function-name "$TRANSCRIBE_LAMBDA_NAME" \
            --query '[Timeout, Environment.Variables.GROQ_DEADLINE_SECONDS]' \
            --output text)"
TR_TIMEOUT="$(echo "$TR_CFG" | awk '{print $1}')"
TR_GROQ_DEADLINE="$(echo "$TR_CFG" | awk '{print $2}')"
[[ "$TR_GROQ_DEADLINE" == "None" || -z "$TR_GROQ_DEADLINE" ]] && TR_GROQ_DEADLINE=180

NEEDED=$(( TR_GROQ_DEADLINE + SITE_VISIT_DEADLINE + 60 ))  # +60s for transcription
echo ">> $TRANSCRIBE_LAMBDA_NAME timeout=${TR_TIMEOUT}s, groq budget=${TR_GROQ_DEADLINE}s,"
echo "   + site visit ${SITE_VISIT_DEADLINE}s -> wants >= ${NEEDED}s"
if (( TR_TIMEOUT < NEEDED )); then
  echo "ERROR: $TRANSCRIBE_LAMBDA_NAME timeout (${TR_TIMEOUT}s) leaves too little" >&2
  echo "       headroom for a third Groq call. Raise it first, e.g.:" >&2
  echo "         aws lambda update-function-configuration \\" >&2
  echo "           --function-name $TRANSCRIBE_LAMBDA_NAME --timeout 300" >&2
  echo "       (or lower SITE_VISIT_DEADLINE / GROQ_DEADLINE_SECONDS)" >&2
  exit 1
fi
echo ">> Timeout headroom OK."

# -------------------------------------------------------------
# 2. transcribeRecording: the extraction tuning knobs, then deploy.
#    Env is MERGED so ELEVENLABS_API_KEY / GROQ_API_KEY / table names survive.
# -------------------------------------------------------------
CURRENT_ENV="$(aws lambda get-function-configuration \
                 --function-name "$TRANSCRIBE_LAMBDA_NAME" \
                 --query 'Environment.Variables' --output json)"
MERGED_ENV="$(python - "$CURRENT_ENV" "$SITE_VISIT_DEADLINE" \
    "$SITE_VISIT_HEAD_CHARS" "$SITE_VISIT_TAIL_CHARS" <<'PY'
import json, sys
env_json, deadline, head, tail = sys.argv[1:5]
env = json.loads(env_json) if env_json not in ("null", "") else {}
env.update({
    "SITE_VISIT_DEADLINE": deadline,
    "SITE_VISIT_HEAD_CHARS": head,
    "SITE_VISIT_TAIL_CHARS": tail,
})
print(json.dumps({"Variables": env}))
PY
)"
aws lambda update-function-configuration \
  --function-name "$TRANSCRIBE_LAMBDA_NAME" \
  --environment "$MERGED_ENV" \
  --query "[FunctionName,Environment.Variables.SITE_VISIT_DEADLINE]" --output text
aws lambda wait function-updated-v2 --function-name "$TRANSCRIBE_LAMBDA_NAME"
echo ">> $TRANSCRIBE_LAMBDA_NAME env: SITE_VISIT_* set."

deploy_py_with_shared "$TRANSCRIBE_LAMBDA_NAME" "functions/transcribe"

# -------------------------------------------------------------
# 3. userApi: the manual-entry field on the existing PATCH route.
#    No route wiring and no IAM change — PATCH /recordings/{key+} already
#    exists and already has UpdateItem on Recordings.
# -------------------------------------------------------------
deploy_py_with_shared "$USERAPI_LAMBDA_NAME" "functions/userapi"

echo
echo ">> Done. Verify with:"
echo "     python tests/test_site_visit_api.py"
echo ">> To see the extractor run end to end, upload a recording whose audio"
echo "   says a site visit number, then check the row:"
echo "     python tests/query_recording.py <key>   # look for 'site_visit'"
