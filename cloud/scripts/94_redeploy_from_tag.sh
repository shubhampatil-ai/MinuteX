#!/usr/bin/env bash
# =============================================================
# 94_redeploy_from_tag.sh — deploy all four Lambdas from ONE git tag.
#
# WHY. Until now every deploy script zipped the local WORKING TREE, so what
# ran in production was whatever was on one laptop at that moment. On
# 15 Oct 2026 userApi matched an uncommitted tree byte for byte, several
# shared modules it ran were not in git at all, and transcribeRecording
# (deployed 8 Oct) ran OLDER copies of notification_schema/workspace_schema
# than userApi — two functions writing the same tables with different schema
# code. This script makes "what is live" answerable with a tag name.
#
# WHAT.
#   1. Exports cloud/functions + cloud/shared from the TAG (git archive) —
#      uncommitted edits in the working tree can never reach production.
#   2. Builds each zip exactly as that function's own deploy script does
#      (see _package_release.py).
#   3. Downloads the LIVE code of each function into
#      .deploy-backups/<UTC time>/ — the rollback copy — and prints which
#      files would change.
#   4. --dry-run stops here. Otherwise asks once, then per function:
#      update-function-code --publish, wait, verify CodeSha256 matches the
#      zip it sent, and set the description to "release <tag> (<commit>)".
#
# CODE ONLY. Environment variables, IAM, memory, timeout and API routes are
# never touched — those stay owned by their numbered scripts.
#
# Usage (Git Bash, from anywhere):
#   bash cloud/scripts/94_redeploy_from_tag.sh v1.0.0-rc1 --dry-run
#   bash cloud/scripts/94_redeploy_from_tag.sh v1.0.0-rc1
#   bash cloud/scripts/94_redeploy_from_tag.sh v1.0.0-rc1 --yes   # no prompt
#
# Rollback (printed again at the end, with the real paths):
#   aws lambda update-function-code --function-name <fn> \
#     --zip-file fileb://.deploy-backups/<time>/<fn>.zip
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

TAG="${1:-}"
[[ -n "$TAG" && "$TAG" != --* ]] || {
  echo "usage: $0 <tag> [--dry-run] [--yes]" >&2; exit 1; }
shift
DRY_RUN="no"; ASSUME_YES="no"
for a in "$@"; do
  case "$a" in
    --dry-run) DRY_RUN="yes" ;;
    --yes|-y)  ASSUME_YES="yes" ;;
    *) echo "unknown option: $a" >&2; exit 1 ;;
  esac
done

USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"
TRANSCRIBE_LAMBDA_NAME="${TRANSCRIBE_LAMBDA_NAME:-transcribeRecording}"
CRM_SYNC_WORKER_LAMBDA_NAME="${CRM_SYNC_WORKER_LAMBDA_NAME:-crmSyncWorker}"
PRESIGN_LAMBDA_NAME="${PRESIGN_LAMBDA_NAME:-getUploadUrl}"

# Deploy order: the device path first (independent), then userApi (serves
# the app), its worker copy, and transcribeRecording last.
#   "<function dir>:<lambda name>"
TARGETS=(
  "device-presign:$PRESIGN_LAMBDA_NAME"
  "userapi:$USERAPI_LAMBDA_NAME"
  "crm-sync-worker:$CRM_SYNC_WORKER_LAMBDA_NAME"
  "transcribe:$TRANSCRIBE_LAMBDA_NAME"
)

# --- 1. The tag, and nothing but the tag ---------------------------------
COMMIT="$(git -C "$REPO_ROOT" rev-parse --verify --quiet "$TAG^{commit}")" || {
  echo "ERROR: '$TAG' is not a tag or commit in this repo." >&2; exit 1; }
SHORT="${COMMIT:0:7}"
echo ">> Release $TAG ($SHORT)"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
git -C "$REPO_ROOT" archive --format=tar "$COMMIT" cloud/functions cloud/shared \
  | tar -x -C "$WORK"

# --- 2. Build ---------------------------------------------------------------
BUILD="$WORK/zips"
declare -A BUILT_SHA
while read -r fn sha; do
  # Python on Windows ends lines with CRLF and `read` keeps the CR.
  BUILT_SHA["$fn"]="${sha%$'\r'}"
done < <(python "$(winpath "$SCRIPT_DIR/_package_release.py")" build \
           "$(winpath "$WORK/cloud")" "$(winpath "$BUILD")")

# --- 3. Back up live code and show what changes ---------------------------
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP="$REPO_ROOT/.deploy-backups/$STAMP"
mkdir -p "$BACKUP"
echo ">> Live code backed up to .deploy-backups/$STAMP/"
for t in "${TARGETS[@]}"; do
  fn="${t%%:*}"; name="${t#*:}"
  url="$(aws lambda get-function --function-name "$name" \
           --query Code.Location --output text)"
  curl -sSf -o "$BACKUP/$name.zip" "$url"
  echo ""
  echo "== $name  (live $(aws lambda get-function-configuration \
          --function-name "$name" --query LastModified --output text))"
  python "$(winpath "$SCRIPT_DIR/_package_release.py")" diff \
    "$(winpath "$BACKUP/$name.zip")" "$(winpath "$BUILD/$fn.zip")" \
    | sed 's/^/     /'
done
echo ""

if [[ "$DRY_RUN" == "yes" ]]; then
  echo ">> Dry run: nothing deployed."
  exit 0
fi

if [[ "$ASSUME_YES" != "yes" ]]; then
  read -r -p "Deploy $TAG ($SHORT) to all four functions in $AWS_REGION? Type 'yes': " ans
  [[ "$ans" == "yes" ]] || { echo "Aborted. Nothing deployed."; exit 1; }
fi

# --- 4. Deploy, verify, label ---------------------------------------------
DONE=()
for t in "${TARGETS[@]}"; do
  fn="${t%%:*}"; name="${t#*:}"
  echo ">> $name"
  aws lambda update-function-code \
    --function-name "$name" \
    --zip-file "fileb://$(winpath "$BUILD/$fn.zip")" \
    --publish \
    --query "[Version,CodeSize]" --output text
  aws lambda wait function-updated-v2 --function-name "$name"

  live="$(aws lambda get-function-configuration --function-name "$name" \
            --query CodeSha256 --output text | tr -d '\r')"
  if [[ "$live" != "${BUILT_SHA[$fn]}" ]]; then
    echo "ERROR: $name reports CodeSha256 $live, expected ${BUILT_SHA[$fn]}." >&2
    echo "       Deployed so far: ${DONE[*]:-none}. Roll back with the commands below." >&2
    break
  fi
  aws lambda update-function-configuration \
    --function-name "$name" \
    --description "release $TAG ($SHORT)" \
    --query "Description" --output text
  aws lambda wait function-updated-v2 --function-name "$name"
  DONE+=("$name")
done

echo ""
echo ">> Deployed: ${DONE[*]:-none}"
echo ">> Rollback, if needed:"
for t in "${TARGETS[@]}"; do
  name="${t#*:}"
  echo "   aws lambda update-function-code --region $AWS_REGION --function-name $name \\"
  echo "     --zip-file \"fileb://$(winpath "$BACKUP/$name.zip")\""
done
[[ ${#DONE[@]} -eq ${#TARGETS[@]} ]] || exit 1
