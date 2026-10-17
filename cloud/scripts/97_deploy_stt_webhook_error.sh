#!/usr/bin/env bash
# =============================================================
# 97_deploy_stt_webhook_error.sh — a failed transcription stops hanging.
#
#   userApi only (code, ONE function). No env, IAM, table or route changes.
#
# THE PROBLEM: when ElevenLabs cannot transcribe the audio it sends a
# `webhook_error` event instead of a transcript. The STT webhook dropped it as
# an unknown event type, so the recording sat at "transcribing" forever — no
# error, no Retry. Seen 2026-10-17: one undecodable .aac uploaded 6 times.
#
# THE FIX: webhook_error goes through _stt_webhook_failure (row -> "failed",
# with ElevenLabs' reason) when it carries our webhook_metadata + request_id,
# and is logged as a safe summary either way (the event is undocumented, so
# the first delivery is how its shape is learned). _stt_webhook_failure no
# longer creates a ghost row, requires the row's stt_request_id to match, and
# never fails a job whose transcript already landed. No app update needed.
#
# WHAT IT SHIPS — the live package with ONLY lambda_function.py replaced by the
# working copy, and only after proving every line that differs from live sits
# inside the STT webhook section. Anything else differing (an undeployed
# change, or live drifted from the repo) refuses the deploy rather than
# rolling it out or rolling it back.
#
# WHAT IT DOES
#   1. Preflight: the async STT tests pass (SKIP_TESTS=1 to skip).
#   2. Download the live package (kept as the rollback copy).
#   3. Build + check the diff is confined to the STT webhook section.
#   4. Ask for "yes", deploy code only, wait, verify the live code has it.
#
# ROLLBACK: printed at the end (re-uploads the copy from step 2).
#
# Run:  bash cloud/scripts/97_deploy_stt_webhook_error.sh
# =============================================================
set -euo pipefail
# NOT MSYS_NO_PATHCONV: curl.exe and python.exe here need Git Bash to turn
# /c/... paths into C:\... (see 92).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

FN="${USERAPI_FN:-userApi}"
SRC="$PROJECT_ROOT/functions/userapi/lambda_function.py"
WORK="$(mktemp -d)"
STAMP="$(date +%Y%m%d-%H%M%S)"
# Outside the repo: the rollback copy is the full live code and must never be
# committed by accident (cloud/scripts/ is not git-ignored).
BACKUP_DIR="${BACKUP_DIR:-$HOME/minutex-lambda-backups}"
BACKUP="$BACKUP_DIR/userApi-before-97-$STAMP.zip"
mkdir -p "$BACKUP_DIR"

echo "== 1. preflight: async STT tests"
if [[ "${SKIP_TESTS:-0}" != "1" ]]; then
  (cd "$PROJECT_ROOT" && python -m pytest tests/test_async_stt.py -q)
else
  echo "   skipped (SKIP_TESTS=1)"
fi

echo "== 2. download live $FN (rollback copy: $BACKUP)"
URL="$(aws lambda get-function --function-name "$FN" --query Code.Location --output text | tr -d '\r')"
curl -sSf -o "$BACKUP" "$URL"
LIVE_SHA="$(aws lambda get-function --function-name "$FN" --query Configuration.CodeSha256 --output text | tr -d '\r')"
echo "   live CodeSha256: $LIVE_SHA"

echo "== 3. build: live package + the working copy's lambda_function.py"
python - "$BACKUP" "$SRC" "$WORK/package.zip" <<'PY'
import sys, zipfile, difflib

backup, src, out = sys.argv[1:]
MARK = 'STT_ERROR_EVENT = "webhook_error"'
# The section the change lives in, in both versions: from the STT event
# constants down to the claim-release helper that follows the failure path.
START, END = 'STT_WEBHOOK_EVENT = "speech_to_text_transcription"', "def _release_stt_claim(key):"

def norm(s): return s.replace("\r\n", "\n")

def section(lines):
    s = next(i for i, l in enumerate(lines) if l.startswith(START))
    e = next(i for i, l in enumerate(lines) if l.startswith(END))
    return s, e

zin = zipfile.ZipFile(backup)
live = norm(zin.read("lambda_function.py").decode("utf-8"))
work = norm(open(src, encoding="utf-8").read())
if MARK in live:
    sys.exit("live already handles webhook_error: nothing to deploy")
if MARK not in work:
    sys.exit("REFUSED: the working copy does not have the webhook_error fix")

a, b = live.splitlines(), work.splitlines()
(ls, le), (ws, we) = section(a), section(b)
changed = 0
for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
    if tag == "equal":
        continue
    if not (ls <= i1 and i2 <= le and ws <= j1 and j2 <= we):
        sys.exit(f"REFUSED: live and the working copy also differ OUTSIDE the "
                 f"STT webhook section (live lines {i1 + 1}-{i2}, working "
                 f"{j1 + 1}-{j2}). Deploying would ship or roll back something "
                 f"else — resolve that first.")
    changed += (i2 - i1) + (j2 - j1)
print(f"   {changed} lines changed, all inside the STT webhook section")
compile(work, "lambda_function.py", "exec")   # syntax check before upload

with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
    for info in zin.infolist():
        data = zin.read(info.filename)
        if info.filename == "lambda_function.py":
            data = work.encode("utf-8")
        zout.writestr(info, data)
PY

echo ""
read -r -p "Deploy this to $FN in $AWS_REGION? Type 'yes': " ans
[[ "$ans" == "yes" ]] || { echo "Aborted. Nothing changed."; exit 1; }

echo "== 4. deploy (code only)"
aws lambda update-function-code --function-name "$FN" \
  --zip-file "fileb://$(winpath "$WORK/package.zip")" --query CodeSha256 --output text
aws lambda wait function-updated --function-name "$FN"

echo "== verify"
URL2="$(aws lambda get-function --function-name "$FN" --query Code.Location --output text | tr -d '\r')"
curl -sSf -o "$WORK/deployed.zip" "$URL2"
python - "$WORK/deployed.zip" <<'PY'
import sys, zipfile
src = zipfile.ZipFile(sys.argv[1]).read("lambda_function.py").decode("utf-8")
ok = 'STT_ERROR_EVENT = "webhook_error"' in src
print("   live handles webhook_error:", ok)
sys.exit(0 if ok else 1)
PY

echo ""
echo "AFTER THE NEXT FAILED TRANSCRIPTION, the event's real shape is in:"
echo "   aws logs filter-log-events --log-group-name /aws/lambda/$FN \\"
echo "     --filter-pattern '\"webhook_error\"'"
echo ""
echo "ROLLBACK (re-uploads the code that was live before this script):"
echo "   aws lambda update-function-code --region $AWS_REGION --function-name $FN \\"
echo "     --zip-file fileb://$(winpath "$BACKUP")"
rm -rf "$WORK"
