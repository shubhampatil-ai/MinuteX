#!/usr/bin/env bash
# =============================================================
# 20_deploy_device_mgmt.sh — device management + pairing-gated presign.
#
# Completes the user-owned device story:
#
#   1. userApi  <- functions/userapi/lambda_function.py
#        adds PATCH  /devices/{device_id}                (rename)
#        adds POST   /devices/{device_id}/factory-reset  (mocked firmware ack)
#      and wires both routes on the HTTP API.
#
#   2. getUploadUrl <- functions/device-presign/lambda_function.py
#        THE IMPORTANT ONE. The live build is still the original
#        device-centric presign: it issues a URL to ANY device with a valid
#        API key and writes legacy keys ({deviceId}/{meetingId}_{ts}.wav).
#        The new build requires Devices.paired_user_id (403 "Device not
#        paired" otherwise) and writes the user-owned key
#        recordings/{user_id}/{device_id}/{recording_id}.wav.
#      Needs DEVICES_TABLE in the env and dynamodb read/update on Devices.
#
# ORDERING: the live transcribeRecording already parses the user-owned
# 4-part key (verified against functions/transcribe/parse_key), so the
# new presign can go live without touching it.
#
# HEADS-UP (see MEMORY): esp32-001 is currently UNPAIRED. After this
# deploys, that device gets 403 until a user pairs it — that is the
# intended behaviour, not a regression. Pair it via the app (or
# POST /devices/pair-request + /devices/pair) to restore uploads.
#
# Prerequisites: scripts 14-19.
# Idempotent: update-function-code / put-role-policy / ensure_route all
# simply overwrite or skip.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"
PRESIGN_LAMBDA_NAME="${LAMBDA_NAME:-getUploadUrl}"
DEVICES_TABLE="${DEVICES_TABLE:-Devices}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

API_ID="$(aws apigatewayv2 get-apis \
            --query "Items[?Name=='${API_NAME}'].ApiId | [0]" \
            --output text)"
if [[ "$API_ID" == "None" || -z "$API_ID" ]]; then
  echo "ERROR: HTTP API '$API_NAME' not found — run 03_create_apigateway.sh first." >&2
  exit 1
fi
echo ">> API: $API_NAME ($API_ID)"

# Package a Python Lambda, vendoring shared/*.py ONLY when the handler
# actually imports it.
#
# THE BUG THIS FIXES: this packager used to write lambda_function.py alone.
# That was correct when both handlers were stdlib-only, but userApi now
# imports 15 modules from shared/ at TOP LEVEL, so a single-file zip does
# not fail on some route - it fails in Runtime.ImportModuleError during
# init and EVERY request 500s. Running this script took production down
# exactly that way; script 19 had the identical defect.
#
# NOT a blanket vendor, though: device-presign (getUploadUrl) is genuinely
# stdlib-only, and shipping 15 unused modules into it would inflate a 4.6KB
# function to ~180KB for nothing. The zip is decided by what the handler
# IMPORTS, so each function gets exactly what it needs and this stays
# correct if either handler's imports change.
#
# Zipped FLAT (ai_schema.py at the archive root): the handler's directory is
# on sys.path, subdirectories are not.
deploy_py() {
  local fn="$1" src_dir="$2"
  local zip="$PROJECT_ROOT/$src_dir/function.zip"
  rm -f "$zip"
  ( cd "$PROJECT_ROOT" && python - "$src_dir" <<'PY'
import re, sys, zipfile
from pathlib import Path

src_dir = sys.argv[1]
root = Path.cwd()
out = root / src_dir / "function.zip"
handler = root / src_dir / "lambda_function.py"
shared = root / "shared"

# Which shared modules does this handler need? Resolved as a TRANSITIVE
# CLOSURE, not just the handler's own import lines: ai_schema imports
# ai_sanitize, so scanning one level deep ships a zip that still dies with
# ModuleNotFoundError - the very failure this packager exists to prevent.
available = {m.stem: m for m in shared.glob("*.py")}


def imports_of(path):
    src = path.read_text(encoding="utf-8")
    return set(re.findall(r"^(?:import|from)[ 	]+([A-Za-z_][A-Za-z0-9_]*)",
                          src, re.MULTILINE))


needed, queue = {}, list(imports_of(handler))
while queue:
    name = queue.pop()
    if name in needed or name not in available:
        continue
    needed[name] = available[name]
    queue.extend(imports_of(available[name]))
needed = [needed[k] for k in sorted(needed)]

with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    z.write(handler, "lambda_function.py")
    for mod in needed:
        z.write(mod, mod.name)
        print(f"   + {mod.name}")
label = f"{len(needed)} shared module(s)" if needed else "stdlib only"
print(f">> packaged {out.relative_to(root)} ({label})")
PY
  )
  aws lambda update-function-code \
    --function-name "$fn" \
    --zip-file "fileb://$(winpath "$zip")" \
    --publish \
    --query "[FunctionName,CodeSize,LastModified]" --output text
  aws lambda wait function-updated-v2 --function-name "$fn"
  echo ">> $fn deployed from $src_dir/"
}

# ensure_route <integration-id> <route-key>
#
# The `head -1 | tr -d '\r'` is load-bearing, not defensive noise: on this
# setup `aws --output text` returns the value followed by a SECOND line
# containing "None", with CRLF endings — e.g. an EXISTING route yields
# "abc123\r\nNone" and a MISSING one yields "None\r\nNone". Without the
# normalization, `[[ "$existing" != "None" ]]` reads "None\r\nNone" as truthy
# and silently skips creating the route (found while wiring the AI Workspace
# routes in script 21 — three routes were reported "exists" but were absent).
ensure_route() {
  local integration_id="$1" route_key="$2"
  local existing
  existing="$(aws apigatewayv2 get-routes --api-id "$API_ID" \
      --query "Items[?RouteKey=='${route_key}'].RouteId | [0]" --output text \
      | head -1 | tr -d '\r')"
  if [[ -n "$existing" && "$existing" != "None" ]]; then
    echo ">> Route exists: $route_key ($existing)"
    return 0
  fi
  aws apigatewayv2 create-route \
    --api-id "$API_ID" \
    --route-key "$route_key" \
    --target "integrations/$integration_id" >/dev/null
  echo ">> Route created: $route_key"
}

# -------------------------------------------------------------
# 1. userApi: deploy + wire the two new device-management routes.
#    The role already has Devices GetItem/UpdateItem and UserDevices
#    DeleteItem from script 19 — rename/factory-reset need nothing more.
# -------------------------------------------------------------
deploy_py "$USERAPI_LAMBDA_NAME" "functions/userapi"

USERAPI_INT="$(aws apigatewayv2 get-integrations --api-id "$API_ID" \
    --query "Items[?contains(IntegrationUri, '${USERAPI_LAMBDA_NAME}')].IntegrationId | [0]" \
    --output text)"
if [[ "$USERAPI_INT" == "None" || -z "$USERAPI_INT" ]]; then
  echo "ERROR: no API integration for $USERAPI_LAMBDA_NAME — run 17 first." >&2
  exit 1
fi
ensure_route "$USERAPI_INT" "PATCH /devices/{device_id}"
ensure_route "$USERAPI_INT" "POST /devices/{device_id}/factory-reset"

# -------------------------------------------------------------
# 2. getUploadUrl: env var + IAM for the Devices lookup, then deploy.
#    Env is merged (not replaced) so BUCKET_NAME/TABLE_NAME/URL_EXPIRY
#    on the live function survive.
# -------------------------------------------------------------
CURRENT_ENV="$(aws lambda get-function-configuration \
                 --function-name "$PRESIGN_LAMBDA_NAME" \
                 --query 'Environment.Variables' --output json)"
MERGED_ENV="$(python - "$CURRENT_ENV" "$DEVICES_TABLE" <<'PY'
import json, sys
env = json.loads(sys.argv[1]) if sys.argv[1] not in ("null", "") else {}
env["DEVICES_TABLE"] = sys.argv[2]
print(json.dumps({"Variables": env}))
PY
)"
aws lambda update-function-configuration \
  --function-name "$PRESIGN_LAMBDA_NAME" \
  --environment "$MERGED_ENV" \
  --query "[FunctionName,Environment.Variables.DEVICES_TABLE]" --output text
aws lambda wait function-updated-v2 --function-name "$PRESIGN_LAMBDA_NAME"
echo ">> $PRESIGN_LAMBDA_NAME env: DEVICES_TABLE=$DEVICES_TABLE"

PRESIGN_ROLE="$(aws lambda get-function --function-name "$PRESIGN_LAMBDA_NAME" \
                  --query 'Configuration.Role' --output text)"
PRESIGN_ROLE="${PRESIGN_ROLE##*/}"
POLICY_FILE="$(mktemp)"
trap 'rm -f "$POLICY_FILE"' EXIT
cat > "$POLICY_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "Logs", "Effect": "Allow",
      "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
      "Resource": "arn:aws:logs:${AWS_REGION}:${ACCOUNT_ID}:*" },
    { "Sid": "DeviceKeysRead", "Effect": "Allow",
      "Action": "dynamodb:GetItem",
      "Resource": "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${TABLE_NAME}" },
    { "Sid": "DevicesReadTouch", "Effect": "Allow",
      "Action": ["dynamodb:GetItem", "dynamodb:UpdateItem"],
      "Resource": "arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${DEVICES_TABLE}" },
    { "Sid": "PutRecordings", "Effect": "Allow",
      "Action": "s3:PutObject",
      "Resource": "arn:aws:s3:::${BUCKET_NAME}/*" }
  ]
}
EOF
aws iam put-role-policy \
  --role-name "$PRESIGN_ROLE" \
  --policy-name "device-presign-inline" \
  --policy-document "file://$(winpath "$POLICY_FILE")"
echo ">> $PRESIGN_ROLE policy set (DeviceKeys read, Devices read+touch, S3 put)."

deploy_py "$PRESIGN_LAMBDA_NAME" "functions/device-presign"

echo
echo ">> Done. Verify with:"
echo "     python tests/test_device_mgmt.py"
echo "     python tests/test_mock_device.py"
