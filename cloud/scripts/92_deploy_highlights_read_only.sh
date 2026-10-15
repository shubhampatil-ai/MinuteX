#!/usr/bin/env bash
# =============================================================
# 92_deploy_highlights_read_only.sh — the highlights route stops calling Groq.
#
#   userApi only (code, ONE function). No env, IAM, table or route changes.
#
# THE PROBLEM: POST /recordings/ai/highlights/{key} ran Groq whenever a meeting
# had no stored highlights, and an empty result is never stored — so every
# Calendar visit re-ran Groq for each recent meeting with nothing to find
# (13 analysed meetings on 2026-10-12, plus 31 older ones), each call holding
# one of the account's Lambda slots for up to ~29 s.
#
# THE FIX: the route is READ-ONLY unless the body says {"regenerate": true}.
# It returns the stored highlights, or empty ones, with no Groq call and no S3
# transcript fetch. The pipeline already extracts highlights (same Groq call as
# the Overview). Older meetings are deliberately not backfilled (decided
# 2026-10-12). Response shape unchanged: no app update needed.
#
# WHAT IT SHIPS — and why not the working tree or HEAD:
#   live userApi (2026-10-12) == working copy EXCEPT this change and ~84
#   UNDEPLOYED lines in _as_duration/request_upload; it is ~4,150 lines away
#   from HEAD. So this ships EXACTLY THE LIVE CODE with only the function
#   regenerate_highlights replaced by the working copy's version. Nothing
#   else changes; HEAD-based packaging would roll back live features.
#
# WHAT IT DOES
#   1. Preflight: the highlights tests pass (SKIP_TESTS=1 to skip).
#   2. Download the live package (kept as the rollback copy).
#   3. Swap in the one function; refuse unless live has the OLD version and
#      the working copy has the NEW one, and nothing else differs.
#   4. Ask for "yes", deploy code only, wait, verify the live code has it.
#
# ROLLBACK: printed at the end (re-uploads the copy from step 2).
#
# Run:  bash cloud/scripts/92_deploy_highlights_read_only.sh
# =============================================================
set -euo pipefail
# NOT MSYS_NO_PATHCONV: curl.exe and python.exe here need Git Bash to turn
# /c/... paths into C:\... (exporting it made the download fail, exit 23).
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
BACKUP="$BACKUP_DIR/userApi-before-92-$STAMP.zip"
mkdir -p "$BACKUP_DIR"

echo "== 1. preflight: highlights tests"
if [[ "${SKIP_TESTS:-0}" != "1" ]]; then
  (cd "$PROJECT_ROOT" && python -m pytest tests/test_ai_workspace.py -q -k "TestHighlights")
else
  echo "   skipped (SKIP_TESTS=1)"
fi

echo "== 2. download live $FN (rollback copy: $BACKUP)"
URL="$(aws lambda get-function --function-name "$FN" --query Code.Location --output text | tr -d '\r')"
curl -sSf -o "$BACKUP" "$URL"
LIVE_SHA="$(aws lambda get-function --function-name "$FN" --query Configuration.CodeSha256 --output text | tr -d '\r')"
echo "   live CodeSha256: $LIVE_SHA"

echo "== 3. build: live code + the new regenerate_highlights only"
python - "$BACKUP" "$SRC" "$WORK/package.zip" <<'PY'
import sys, zipfile, difflib

backup, src, out = sys.argv[1:]
FN_START = "def regenerate_highlights(event):\n"
OLD_MARK = "if not regenerate and isinstance(stored, dict) and \\\n"
NEW_MARK = "READ-ONLY unless `regenerate` is true."

def norm(s): return s.replace("\r\n", "\n")

def function(text):
    i = text.index(FN_START)
    j = text.find("\ndef ", i + len(FN_START))
    return i, (j + 1 if j >= 0 else len(text))

zin = zipfile.ZipFile(backup)
live = norm(zin.read("lambda_function.py").decode("utf-8"))
work = norm(open(src, encoding="utf-8").read())

li, lj = function(live)
wi, wj = function(work)
old_fn, new_fn = live[li:lj], work[wi:wj]
if NEW_MARK in old_fn:
    sys.exit("live already has the read-only highlights route: nothing to deploy")
if OLD_MARK not in old_fn:
    sys.exit("REFUSED: live regenerate_highlights is not the expected old version")
if NEW_MARK not in new_fn:
    sys.exit("REFUSED: working copy regenerate_highlights is not the new version")

patched = live[:li] + new_fn + live[lj:]
changed = [l for l in difflib.unified_diff(live.splitlines(), patched.splitlines(), lineterm="", n=0)
           if l[:1] in "+-" and not l.startswith(("+++", "---"))]
print(f"   {len(changed)} lines changed, all inside regenerate_highlights")
compile(patched, "lambda_function.py", "exec")   # syntax check before upload

with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
    for info in zin.infolist():
        data = zin.read(info.filename)
        if info.filename == "lambda_function.py":
            data = patched.encode("utf-8")
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
ok = "READ-ONLY unless `regenerate` is true." in src
print("   live has the read-only highlights route:", ok)
sys.exit(0 if ok else 1)
PY

echo ""
echo "ROLLBACK (re-uploads the code that was live before this script):"
echo "   aws lambda update-function-code --region $AWS_REGION --function-name $FN \\"
echo "     --zip-file fileb://$(winpath "$BACKUP")"
rm -rf "$WORK"
