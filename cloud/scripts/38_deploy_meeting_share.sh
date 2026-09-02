#!/usr/bin/env bash
# =============================================================
# 38_deploy_meeting_share.sh — Meeting Share: five routes + the Shares table
# wiring + the IAM the public page needs.
#
#   POST   /recordings/share/{key+}    (JWT)  create a link
#   GET    /recordings/shares/{key+}   (JWT)  list this meeting's links
#   PATCH  /shares/{share_id}          (JWT)  retoggle / re-expire
#   DELETE /shares/{share_id}          (JWT)  revoke
#   GET    /share/{token}              NO JWT  the public HTML page
#   GET    /share/{token}/audio        NO JWT  302 -> a fresh audio presign
#
# THE UNAUTHENTICATED ROUTE. GET /share/{token} carries no bearer token by
# design — the recipient has no MinuteX account. Like /crm/salesforce/callback
# and /webhooks/elevenlabs/stt before it, it has AuthorizationType NONE at the
# gateway, which is not a weakening: this API has NEVER used an API Gateway
# authorizer. JWT enforcement lives in the Lambda (_require_auth), so "no
# authorizer" is the status quo for every route here, and the four owner-side
# routes above still call _require_auth exactly like every other route.
# Authorization for the public route is possession of a 256-bit token whose
# sha256 is the only stored form. See cloud/shared/share_schema.py.
#
# ACTION FIRST, KEY LAST. /recordings/share/{key+} rather than the more
# natural /recordings/{key}/share, for the same hard API Gateway constraint
# every recording-scoped route in this API obeys: a recording key contains
# slashes so it must be greedy, and "Greedy variables may only be in last
# position". The literal "share"/"shares" segment cannot collide with a real
# key, which always begins "recordings/{user_id}/..." or a legacy device id.
#
# WHY {token} IS NOT GREEDY. A share token is base64url — no slashes — so a
# plain path variable is right, and a non-greedy variable refuses a token
# containing a "/" instead of silently accepting a mangled one.
#
# Idempotent throughout: existing routes are detected and skipped, the env
# merge preserves unrelated variables, and the IAM policy is a PUT.
#
# Run:  bash scripts/38_deploy_meeting_share.sh
#       SKIP_TESTS=1 bash scripts/38_deploy_meeting_share.sh
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"
SHARES_TABLE="${SHARES_TABLE:-Shares}"

API_ID="$(aws apigatewayv2 get-apis \
            --query "Items[?Name=='${API_NAME}'].ApiId | [0]" \
            --output text)"
if [[ "$API_ID" == "None" || -z "$API_ID" ]]; then
  echo "ERROR: HTTP API '$API_NAME' not found — run 03_create_apigateway.sh first." >&2
  exit 1
fi
echo ">> API: $API_NAME ($API_ID)"
echo ">> Lambda: $USERAPI_LAMBDA_NAME"

# -------------------------------------------------------------
# 1. Preflight — fail BEFORE touching AWS if the source is incomplete.
#
# Each of these would otherwise surface as a broken deployment rather than a
# failed script: a missing share_schema.py bricks EVERY route on cold start
# (userApi imports it at module scope), and handlers without the routes
# registered answer 404 while looking perfectly healthy.
# -------------------------------------------------------------
SHARE_SHARED="$PROJECT_ROOT/shared/share_schema.py"
HANDLER="$PROJECT_ROOT/functions/userapi/lambda_function.py"

if [[ ! -f "$SHARE_SHARED" ]]; then
  echo "ERROR: shared/share_schema.py is missing." >&2
  exit 1
fi

for handler_fn in create_share list_shares update_share revoke_share \
                  public_share public_share_audio; do
  if ! grep -q "^def ${handler_fn}(event)" "$HANDLER"; then
    echo "ERROR: handler ${handler_fn}() not found in lambda_function.py." >&2
    exit 1
  fi
done

for route_key in '"/recordings/share/{key+}"' '"/recordings/shares/{key+}"' \
                 '"/shares/{share_id}"' '"/share/{token}"' \
                 '"/share/{token}/audio"'; do
  if ! grep -q "$route_key" "$HANDLER"; then
    echo "ERROR: route ${route_key} is not registered in the _ROUTES table." >&2
    echo "       The API Gateway route below would answer 404." >&2
    exit 1
  fi
done
echo ">> Preflight: share_schema.py present, 6 handlers + routes registered."

# The offline suite covers the security rules that matter most here — expiry,
# revocation, toggle enforcement and the private-field exclusion. Running it
# before deploying is much cheaper than discovering from a user that a
# transcript they never shared is on the public internet.
if [[ "${SKIP_TESTS:-0}" != "1" ]]; then
  echo ">> Running the Meeting Share test suite..."
  ( cd "$PROJECT_ROOT" && python -m pytest tests/test_meeting_share.py -q ) \
    || { echo "ERROR: share tests failed — not deploying." >&2; exit 1; }
else
  echo ">> SKIP_TESTS=1 — skipping the offline suite."
fi

# -------------------------------------------------------------
# 2. IAM — the Shares table and BOTH its indexes.
#
# index/* is the part that is easy to forget and quiet when wrong: without it
# the owner-side routes keep working (they GetItem/PutItem by share_id) while
# the PUBLIC route fails on every request, because the token lookup is a Query
# against token-index.
# -------------------------------------------------------------
ROLE_NAME="$(aws lambda get-function-configuration \
               --function-name "$USERAPI_LAMBDA_NAME" \
               --query 'Role' --output text | awk -F/ '{print $NF}')"
echo ">> Lambda role: $ROLE_NAME"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
TABLE_ARN="arn:aws:dynamodb:${AWS_REGION}:${ACCOUNT_ID}:table/${SHARES_TABLE}"

aws iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name "userApi-shares" \
  --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [
      {
        \"Effect\": \"Allow\",
        \"Action\": [
          \"dynamodb:GetItem\",
          \"dynamodb:PutItem\",
          \"dynamodb:UpdateItem\",
          \"dynamodb:Query\"
        ],
        \"Resource\": [
          \"${TABLE_ARN}\",
          \"${TABLE_ARN}/index/*\"
        ]
      }
    ]
  }"
echo ">> IAM: userApi-shares attached (table + index/*)."

# -------------------------------------------------------------
# 3. Environment — the table, its indexes, and the public link origin.
#
# SHARE_BASE_URL is intentionally optional. Left unset, the Lambda builds
# links from the Host header of the owner's own create request, so links work
# the moment this deploy finishes. Set it in .env to move to a custom domain
# later WITHOUT a code change; the route path stays /share/{token} either way.
# -------------------------------------------------------------
# MERGES rather than replaces (see _merge_env.py): BUCKET_NAME, JWT_SECRET_ARN
# and every other live variable must survive a deploy that only adds four.
MERGE_ENV_PY="$SCRIPT_DIR/_merge_env.py"

merge_env() {
  local fn="$1"; shift
  local current merged
  current="$(aws lambda get-function-configuration \
               --function-name "$fn" \
               --query 'Environment.Variables' --output json)"
  merged="$(python "$MERGE_ENV_PY" "$current" "$@")"
  aws lambda update-function-configuration \
    --function-name "$fn" --environment "$merged" >/dev/null
  aws lambda wait function-updated-v2 --function-name "$fn"
}

merge_env "$USERAPI_LAMBDA_NAME" \
  "SHARES_TABLE=$SHARES_TABLE" \
  "SHARES_TOKEN_INDEX=token-index" \
  "SHARES_RECORDING_INDEX=recording-index" \
  "SHARE_BASE_URL=${SHARE_BASE_URL:-}"
echo ">> Env merged (SHARES_TABLE, indexes, SHARE_BASE_URL)."

# -------------------------------------------------------------
# 4. Deploy the code. Shared modules vendored FLAT — userApi imports
#    ai_schema/groq_client/mom_schema/share_schema/prompts/stt_result/
#    transcript_store at module scope, so a zip without them fails on cold
#    start (same packaging as 23/25/27/30/33/36).
# -------------------------------------------------------------
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

  # Assert the new module actually landed. The glob above should include it,
  # but "should" is not good enough when the failure mode is every route
  # 500ing on the next cold start.
  ( cd "$PROJECT_ROOT" && python - "$src_dir" <<'PY'
import sys, zipfile
from pathlib import Path

names = zipfile.ZipFile(Path.cwd() / sys.argv[1] / "function.zip").namelist()
missing = [m for m in ("lambda_function.py", "share_schema.py", "mom_schema.py",
                       "ai_schema.py", "groq_client.py", "prompts.py")
           if m not in names]
if missing:
    sys.exit(f"ERROR: archive is missing {', '.join(missing)} — refusing to deploy.")
print(f">> archive verified: {len(names)} entries, share_schema.py present")
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
deploy_py_with_shared "$USERAPI_LAMBDA_NAME" "functions/userapi"

# -------------------------------------------------------------
# 5. Wire the routes.
# -------------------------------------------------------------
USERAPI_INT="$(aws apigatewayv2 get-integrations --api-id "$API_ID" \
    --query "Items[?contains(IntegrationUri, '${USERAPI_LAMBDA_NAME}')].IntegrationId | [0]" \
    --output text)"
if [[ "$USERAPI_INT" == "None" || -z "$USERAPI_INT" ]]; then
  echo "ERROR: no API integration for $USERAPI_LAMBDA_NAME — run 17 first." >&2
  exit 1
fi

# Filter by CONTENT, not by line position — see 33's note. `aws --output text`
# emits the RouteId plus a stray "None" line in EITHER order, so a `head -1`
# that grabs the "None" makes an existing route look absent and the re-run
# dies on a ConflictException.
ensure_route() {
  local integration_id="$1" route_key="$2" existing
  existing="$(aws apigatewayv2 get-routes --api-id "$API_ID" \
      --query "Items[?RouteKey=='${route_key}'].RouteId | [0]" --output text \
      | tr -d '\r' | grep -v '^None$' | grep -v '^$' | head -1 || true)"
  if [[ -n "$existing" ]]; then
    echo ">> Route exists: $route_key ($existing)"
    return 0
  fi
  aws apigatewayv2 create-route \
    --api-id "$API_ID" \
    --route-key "$route_key" \
    --target "integrations/$integration_id" >/dev/null
  echo ">> Route created: $route_key"
}

ensure_route "$USERAPI_INT" "POST /recordings/share/{key+}"
ensure_route "$USERAPI_INT" "GET /recordings/shares/{key+}"
ensure_route "$USERAPI_INT" "PATCH /shares/{share_id}"
ensure_route "$USERAPI_INT" "DELETE /shares/{share_id}"
# The public page. No authorizer — same as every other route in this API.
ensure_route "$USERAPI_INT" "GET /share/{token}"
# The audio gateway. Declared AFTER the page route; API Gateway matches the
# more specific literal segment regardless of declaration order, so
# /share/{token}/audio never falls through to /share/{token}.
ensure_route "$USERAPI_INT" "GET /share/{token}/audio"

# -------------------------------------------------------------
# 6. Verify the public route is reachable and answers HTML.
#
# Worth probing because the failure is quiet in the wrong direction: the four
# authenticated routes can be perfectly healthy while the public page — the
# entire point of the feature — 404s at the gateway. A junk token must render
# the "link isn't available" page with status 404, which proves BOTH that the
# route reaches the Lambda AND that an unknown token is rejected.
# -------------------------------------------------------------
API_ENDPOINT="$(aws apigatewayv2 get-api --api-id "$API_ID" \
    --query 'ApiEndpoint' --output text | tr -d '\r')"
PROBE_URL="${API_ENDPOINT}/share/deploy-probe-not-a-real-token-000000"
echo ">> Probing $PROBE_URL"

PROBE_STATUS="$(curl -s -o /dev/null -w '%{http_code}' "$PROBE_URL" || echo "000")"
PROBE_TYPE="$(curl -s -o /dev/null -w '%{content_type}' "$PROBE_URL" || echo "")"

case "$PROBE_STATUS" in
  404)
    echo ">> Public route verified: unknown token -> 404 ($PROBE_TYPE)"
    ;;
  403|000)
    echo "ERROR: the public route did not reach the Lambda (HTTP $PROBE_STATUS)." >&2
    echo "       403 usually means the route was not created on this stage." >&2
    exit 1
    ;;
  200)
    echo "ERROR: a junk token returned 200 — the token check is not running." >&2
    exit 1
    ;;
  *)
    echo "WARN: unexpected probe status $PROBE_STATUS ($PROBE_TYPE)." >&2
    echo "      Check CloudWatch for $USERAPI_LAMBDA_NAME before announcing this." >&2
    ;;
esac

echo
echo ">> Meeting Share deployed."
echo "   Public page:  ${SHARE_BASE_URL:-$API_ENDPOINT}/share/<token>"
echo "   Set SHARE_BASE_URL in .env to serve links from a custom domain."
