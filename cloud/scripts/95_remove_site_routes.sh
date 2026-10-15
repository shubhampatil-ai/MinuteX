#!/usr/bin/env bash
# =============================================================
# 95_remove_site_routes.sh — delete the API Gateway routes of the retired
# Salesforce Site path, after its code is gone.
#
# WHY. Go-live uses ONLY the MinuteX managed package (webhook contract v2 +
# integration-user OAuth). The Site path's 37 routes were removed from
# userApi's _ROUTES; this removes them from the gateway too, so they are not
# even routed to the Lambda.
#
# REMOVED: every route under
#   /workspaces/{workspace_id}/site-delivery*      (endpoint, health, people sync)
#   /workspaces/{workspace_id}/site-salesforce*    (connect, install, discovery,
#                                                   objects, mappings, Tasks, ...)
#   /recordings/site-delivery*, /recordings/site-participants/{key+}
#   POST /salesforce/pair
#
# KEPT (the package uses them): /workspaces/{id}/people, .../people/contact,
#   /delivery/audio/{token}, every /webhook* and /salesforce/* OAuth route.
#
# NOT TOUCHED: DynamoDB tables (SiteDeliveryEndpoints holds the package's own
# webhook row and stays; the Site-only tables are a separate, explicit step),
# IAM, Lambda environment variables.
#
# ORDER. Run AFTER deploying the userApi that no longer serves these routes
# (scripts/94_redeploy_from_tag.sh). The script refuses to run while the
# deployed userApi still contains them.
#
# Usage:
#   bash cloud/scripts/95_remove_site_routes.sh            # dry run: lists them
#   bash cloud/scripts/95_remove_site_routes.sh --apply    # deletes them
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
SITE_ROUTES='/site-delivery|/site-salesforce|/site-participants|/salesforce/pair'

# --- 1. The deployed userApi must no longer serve them -------------------
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
URL="$(aws lambda get-function --function-name "$USERAPI_LAMBDA_NAME" \
         --query Code.Location --output text | tr -d '\r')"
curl -sSf -o "$WORK/userapi.zip" "$URL"
if python -c "import sys, zipfile; s = zipfile.ZipFile(sys.argv[1]).read('lambda_function.py').decode('utf-8'); sys.exit(0 if 'site_sf_connect' in s else 1)" \
     "$(winpath "$WORK/userapi.zip")"; then
  echo "REFUSED: the deployed $USERAPI_LAMBDA_NAME still serves the Site routes." >&2
  echo "         Deploy the current code first (scripts/94_redeploy_from_tag.sh)." >&2
  exit 1
fi
echo ">> Deployed $USERAPI_LAMBDA_NAME no longer serves the Site routes."

# --- 2. Find the routes ----------------------------------------------------
API_ID="${API_ID:-$(aws apigatewayv2 get-apis \
  --query "Items[?Name=='${API_NAME:-meeting-recorder-api}'].ApiId | [0]" \
  --output text | tr -d '\r')}"
[[ -n "$API_ID" && "$API_ID" != "None" ]] || { echo "FAIL: no API id" >&2; exit 1; }

ROUTES="$(aws apigatewayv2 get-routes --api-id "$API_ID" \
            --query "Items[].[RouteId,RouteKey]" --output text | tr -d '\r' \
          | grep -E "$SITE_ROUTES" || true)"
if [[ -z "$ROUTES" ]]; then
  echo ">> No Site routes on $API_ID. Nothing to do."
  exit 0
fi
echo ">> Site routes on $API_ID ($(printf '%s\n' "$ROUTES" | wc -l)):"
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
