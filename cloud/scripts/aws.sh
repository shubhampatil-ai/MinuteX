#!/usr/bin/env bash
# =============================================================
# aws.sh — shared helper sourced by all Stage-0/1 shell scripts.
#
# Purpose:
#   1. Load .env (non-secret config: region, bucket, resource names).
#   2. Provide a `winpath` helper that converts a Git-Bash / Cygwin
#      POSIX path (/c/foo) to a Windows path (C:\foo) using cygpath,
#      because the native Windows aws.exe expects Windows-style paths
#      for file arguments like  --zip-file fileb://C:\path\function.zip
#   3. Pin the region on every call so a mismatched `aws configure`
#      default (e.g. ap-south-1) can never send resources to the
#      wrong region.
#
# Usage in a script:
#   SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
#   source "$SCRIPT_DIR/aws.sh"
#   aws s3 ls                      # region auto-applied
#   ZIP="$(winpath /c/.../function.zip)"
# =============================================================

set -euo pipefail

# --- Resolve the directory this helper lives in (cloud/scripts/) ---
# Layout: <repo>/cloud/{functions,shared,scripts,tests}
#   PROJECT_ROOT = <repo>/cloud   -- where functions/ and shared/ live, so the
#                                    deploy helpers resolve source dirs here.
#   REPO_ROOT    = <repo>         -- where .env and the app/ + device/ trees are.
AWS_SH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$AWS_SH_DIR/.." && pwd)"
REPO_ROOT="$(cd "$PROJECT_ROOT/.." && pwd)"

# --- Load .env (KEY=VALUE lines; ignore comments/blanks) ---
# .env stays at the REPO root (shared with the app/device trees), not in cloud/.
ENV_FILE="$REPO_ROOT/.env"
if [[ -f "$ENV_FILE" ]]; then
  # Export every non-comment KEY=VALUE pair.
  set -a
  # shellcheck disable=SC1090
  source <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_FILE")
  set +a
else
  echo "WARN: $ENV_FILE not found — relying on already-exported env vars." >&2
fi

# --- Region: required, and pinned on every aws call ---
: "${AWS_REGION:?AWS_REGION not set (put it in .env)}"
export AWS_DEFAULT_REGION="$AWS_REGION"

# --- Locate the AWS CLI binary (prefer the native aws.exe on Windows) ---
if command -v aws.exe >/dev/null 2>&1; then
  AWS_BIN="$(command -v aws.exe)"
elif command -v aws >/dev/null 2>&1; then
  AWS_BIN="$(command -v aws)"
else
  echo "ERROR: aws CLI not found on PATH." >&2
  exit 1
fi

# --- winpath: POSIX path -> Windows path for aws.exe file args ---
# Only converts when cygpath exists (Git Bash / Cygwin). On a native
# Linux runner cygpath is absent and the path is returned unchanged.
winpath() {
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -w "$1"
  else
    printf '%s' "$1"
  fi
}

# --- aws wrapper: always inject --region so nothing leaks to the
#     wrong region regardless of the local `aws configure` default. ---
aws() {
  "$AWS_BIN" --region "$AWS_REGION" "$@"
}

# Export so subshells in the sourcing script see them.
export -f winpath
export -f aws
export AWS_BIN
export PROJECT_ROOT REPO_ROOT
