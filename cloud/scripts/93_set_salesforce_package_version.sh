#!/usr/bin/env bash
# =============================================================
# 93_set_salesforce_package_version.sh — point the app's "Install MinuteX in
# Salesforce" button at a MinuteX package version. Changes two environment
# settings on userApi and nothing else: no code, no routes, no policies.
#
#   bash 93_set_salesforce_package_version.sh                    # from .env
#   bash 93_set_salesforce_package_version.sh 04tXXXXXXXXXXXXXXX  # this version (beta)
#   bash 93_set_salesforce_package_version.sh 04tXXXXXXXXXXXXXXX released
#
# Without arguments it uses SF_PACKAGE_VERSION_ID / SF_PACKAGE_RELEASED from
# .env. A version id is "04t" + 12 or 15 letters/digits (sf package version
# list). "released" = a promoted version (installs in production too);
# otherwise a beta (sandboxes and Developer Edition orgs only).
#
# The app reads the link each time the Salesforce screen loads: no app
# release, no restart. Re-running is safe.
#
# ROLLBACK: run it again with the previous version id (kept as a comment in
# .env).
# =============================================================
set -euo pipefail
cd "$(dirname "$0")"
. ./aws.sh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
USERAPI_FN="${USERAPI_FN:-userApi}"

VERSION_ID="${1:-${SF_PACKAGE_VERSION_ID:-}}"
RELEASED_ARG="${2:-}"
if [[ "$RELEASED_ARG" == "released" ]]; then
  RELEASED=1
elif [[ -n "$RELEASED_ARG" ]]; then
  echo "FAIL: the second argument may only be \"released\"." >&2; exit 1
elif [[ $# -ge 1 ]]; then
  RELEASED=0
else
  RELEASED="${SF_PACKAGE_RELEASED:-0}"
fi

if [[ ! "$VERSION_ID" =~ ^04t[0-9A-Za-z]{12}([0-9A-Za-z]{3})?$ ]]; then
  echo "FAIL: \"$VERSION_ID\" is not a package version id (04t…)." >&2; exit 1
fi

echo "==> $USERAPI_FN: SF_PACKAGE_VERSION_ID=$VERSION_ID SF_PACKAGE_RELEASED=$RELEASED"
CURRENT_ENV="$(aws lambda get-function-configuration --function-name "$USERAPI_FN" \
               --query 'Environment.Variables' --output json)"
ENV_JSON="$(python "$SCRIPT_DIR/_merge_env.py" "$CURRENT_ENV" \
            "SF_PACKAGE_VERSION_ID=$VERSION_ID" "SF_PACKAGE_RELEASED=$RELEASED")"
aws lambda update-function-configuration --function-name "$USERAPI_FN" \
  --environment "$ENV_JSON" --query FunctionName --output text
aws lambda wait function-updated-v2 --function-name "$USERAPI_FN"

echo
if [[ "$RELEASED" == "1" ]]; then
  echo "Done. The Install button now opens $VERSION_ID (released: production and sandboxes)."
else
  echo "Done. The Install button now opens $VERSION_ID (beta: sandboxes and Developer Edition orgs)."
fi
echo "  https://login.salesforce.com/packaging/installPackage.apexp?p0=$VERSION_ID"
