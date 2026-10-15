#!/usr/bin/env bash
# =============================================================
# 96_remove_folder_routes.sh — delete the API Gateway routes left behind by
# the Folders feature.
#
# WHY. Folders were removed on 2026-09-07 (code and live data), but their 9
# gateway routes stayed. They point at userApi, which has had no handler for
# them since, so every call already answers 404; this removes the routes so
# the gateway matches the code. Nothing calls them: not the app, the device
# firmware or the Salesforce package.
#
# REMOVED: every route under /folders, and PATCH /recordings/folder/{key+}.
# NOT TOUCHED: anything else (no table, IAM or Lambda change).
#
# The script refuses to run if the deployed userApi still serves a folder
# route.
#
# Usage:
#   bash cloud/scripts/96_remove_folder_routes.sh            # dry run: lists them
#   bash cloud/scripts/96_remove_folder_routes.sh --apply    # deletes them
#
# Idempotent: a second run finds nothing to delete.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

APPLY="no"
[[ "${1:-}" == "--apply" ]] && APPLY="yes"
USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"
FOLDER_ROUTES=' /folders|/recordings/folder/'

# --- 1. The deployed userApi must not serve them -------------------------
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
URL="$(aws lambda get-function --function-name "$USERAPI_LAMBDA_NAME" \
         --query Code.Location --output text | tr -d '\r')"
curl -sSf -o "$WORK/userapi.zip" "$URL"
if python -c "import sys, zipfile; s = zipfile.ZipFile(sys.argv[1]).read('lambda_function.py').decode('utf-8'); sys.exit(0 if ('\"/folders' in s or '/recordings/folder/' in s) else 1)" \
     "$(winpath "$WORK/userapi.zip")"; then
  echo "REFUSED: the deployed $USERAPI_LAMBDA_NAME still serves a folder route." >&2
  exit 1
fi
echo ">> Deployed $USERAPI_LAMBDA_NAME serves no folder route."

# --- 2. Find the routes ----------------------------------------------------
API_ID="${API_ID:-$(aws apigatewayv2 get-apis \
  --query "Items[?Name=='${API_NAME:-meeting-recorder-api}'].ApiId | [0]" \
  --output text | tr -d '\r')}"
[[ -n "$API_ID" && "$API_ID" != "None" ]] || { echo "FAIL: no API id" >&2; exit 1; }

ROUTES="$(aws apigatewayv2 get-routes --api-id "$API_ID" \
            --query "Items[].[RouteId,RouteKey]" --output text | tr -d '\r' \
          | grep -E "$FOLDER_ROUTES" || true)"
if [[ -z "$ROUTES" ]]; then
  echo ">> No folder routes on $API_ID. Nothing to do."
  exit 0
fi
echo ">> Folder routes on $API_ID ($(printf '%s\n' "$ROUTES" | wc -l)):"
printf '%s\n' "$ROUTES" | awk -F'\t' '{print "     " $2}'

if [[ "$APPLY" != "yes" ]]; then
  echo ">> Dry run: nothing deleted. Re-run with --apply to delete them."
  exit 0
fi

# --- 3. Delete -------------------------------------------------------------
while IFS=$'\t' read -r route_id route_key; do
  aws apigatewayv2 delete-route --api-id "$API_ID" --route-id "$route_id"
  echo "   deleted: $route_key"
done <<< "$ROUTES"
echo ">> Done. The \$default stage auto-deploys; the routes are gone now."
