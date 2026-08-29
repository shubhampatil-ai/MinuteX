#!/usr/bin/env bash
# =============================================================
# 36_deploy_mom.sh — the structured, editable Minutes of Meeting on userApi.
#
# Ships FOUR routes:
#
#   GET    /recordings/ai/mom/{key+}            -> {mom, document, exists}
#   POST   /recordings/ai/mom/{key+}  {}        -> {mom, document, regenerated}
#   PUT    /recordings/ai/mom/{key+}  {mom}     -> {mom, document}
#   DELETE /recordings/ai/mom/{key+}            -> {deleted}
#
# and changes the behaviour of NO existing route. The mirrored
# documents.minutes_of_meeting is written through the same _save_document the
# other document types already use, and every field it returns still means
# what it meant — so no coordinated app release is needed. An app build that
# predates this deploy keeps opening the MoM as a Markdown document and keeps
# working.
#
# WHAT THIS IS FOR
#
# Minutes of Meeting used to be one opaque Markdown blob: fine to read,
# impossible to edit structurally. "Delete the Highlights section", "move
# Action Items above Decisions", "add a Deadline column" and above all "keep MY
# wording when the AI regenerates the rest" are all unanswerable against a
# blob. The MoM now has a structured representation in a new `mom` attribute
# on the recording row, and the Markdown document becomes a derived MIRROR of
# it, rewritten on every structured write.
#
# WHY THIS SCRIPT IS SHORTER THAN 33 OR 35
#
# It genuinely needs less. Deliberately absent, each for a reason:
#
#   * NO new DynamoDB table. The structure is one attribute on the EXISTING
#     recordings row — always read with the recording, never queried
#     independently, exactly like the `documents` and `tasks` maps beside it.
#   * NO IAM change. The routes touch `recordings` (already granted) and read
#     the Tasks table through the same helper list_meeting_tasks uses (granted
#     by 33). Verified: the MoM section of lambda_function.py references no
#     other table.
#   * NO env change. The MoM code reads no environment variable at all —
#     verified by grep, not assumed. Nothing to merge, so _merge_env.py is not
#     needed and JWT_SECRET_ARN et al. are never at risk here.
#   * NO Groq/model change. Generating a MoM makes ZERO model calls: it
#     arranges analysis the pipeline already produced (participants,
#     meeting_highlights, tasks, summary, highlights). No quota to raise, and
#     the route cannot fail on a rate limit.
#
# THE ONE PACKAGING HAZARD, AND WHY IT IS ALREADY HANDLED
#
# lambda_function.py does `import mom_schema` at MODULE scope. A zip missing
# shared/mom_schema.py therefore fails on cold start and takes down EVERY
# userApi route, not just the four below. The packaging step vendors
# shared/*.py flat by glob (same as 23/25/27/30/33), so the new module is
# picked up automatically — but step 3 asserts it landed in the archive rather
# than trusting the glob, because that failure is total and silent until the
# next cold start.
#
# Run:  bash scripts/36_deploy_mom.sh
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"

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
# Both of these would otherwise surface as a broken deployment rather than as
# a failed script: a missing mom_schema.py bricks every route on cold start,
# and a handler without the routes registered answers 404 on all four while
# looking perfectly healthy.
# -------------------------------------------------------------
MOM_SHARED="$PROJECT_ROOT/shared/mom_schema.py"
HANDLER="$PROJECT_ROOT/functions/userapi/lambda_function.py"

if [[ ! -f "$MOM_SHARED" ]]; then
  echo "ERROR: shared/mom_schema.py is missing." >&2
  echo "       userApi imports it at module scope, so deploying without it" >&2
  echo "       would fail EVERY route on the next cold start." >&2
  exit 1
fi
if ! grep -q '^import mom_schema' "$HANDLER"; then
  echo "ERROR: lambda_function.py does not import mom_schema." >&2
  exit 1
fi
for handler_fn in get_mom generate_mom save_mom delete_mom; do
  if ! grep -q "^def ${handler_fn}(event)" "$HANDLER"; then
    echo "ERROR: handler ${handler_fn}() not found in lambda_function.py." >&2
    exit 1
  fi
done
if ! grep -q '"/recordings/ai/mom/{key+}"' "$HANDLER"; then
  echo "ERROR: the MoM routes are not registered in the _ROUTES table." >&2
  echo "       The API Gateway routes below would all answer 404." >&2
  exit 1
fi
echo ">> Preflight: mom_schema.py present, 4 handlers + routes registered."

# The offline suites are fast (<20s) and cover the merge rule that protects
# user edits from a regeneration. Running them here is cheaper than finding
# out from a user that their wording was overwritten.
if [[ "${SKIP_TESTS:-0}" != "1" ]]; then
  echo ">> Running the MoM test suites..."
  ( cd "$PROJECT_ROOT" && python -m pytest tests/test_mom_schema.py tests/test_mom_api.py -q ) \
    || { echo "ERROR: MoM tests failed — not deploying." >&2; exit 1; }
else
  echo ">> SKIP_TESTS=1 — skipping the offline suites."
fi

# -------------------------------------------------------------
# 2. Deploy the code. Shared modules vendored FLAT — userApi imports
#    ai_schema/groq_client/mom_schema/prompts/stt_result/transcript_store at
#    module scope, so a zip without them fails on cold start (same packaging
#    as 23/25/27/30/33).
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
missing = [m for m in ("lambda_function.py", "mom_schema.py", "ai_schema.py",
                       "groq_client.py", "prompts.py") if m not in names]
if missing:
    sys.exit(f"ERROR: archive is missing {', '.join(missing)} — refusing to deploy.")
print(f">> archive verified: {len(names)} entries, mom_schema.py present")
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
# 3. Wire the routes.
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

# ACTION FIRST, key LAST — the same hard constraint every recording-scoped
# route in this API obeys: a recording key contains slashes so it must be
# greedy ({key+}), and API Gateway rejects a greedy variable in any but the
# FINAL position ("Greedy variables may only be in last position"). The "ai/"
# segment keeps the action namespace from colliding with a real key: keys
# always begin "recordings/{user_id}/..." or a legacy device id, never "ai/".
ensure_route "$USERAPI_INT" "GET /recordings/ai/mom/{key+}"
ensure_route "$USERAPI_INT" "POST /recordings/ai/mom/{key+}"
ensure_route "$USERAPI_INT" "PUT /recordings/ai/mom/{key+}"
ensure_route "$USERAPI_INT" "DELETE /recordings/ai/mom/{key+}"

# -------------------------------------------------------------
# 4. Verify PUT actually reaches the Lambda.
#
# PUT is the one method here the API may not have carried before (33 added
# "PUT /recordings/participants/{key+}", but a stage or CORS config predating
# it can still exclude the verb). It is worth probing because the failure is
# quiet in the wrong direction: GET/POST/DELETE work, the editor loads and
# generates fine, and only SAVING fails — which reads to a user as "my edits
# vanished" rather than as a deploy problem.
# -------------------------------------------------------------
PUT_ROUTE="$(aws apigatewayv2 get-routes --api-id "$API_ID" \
    --query "Items[?RouteKey=='PUT /recordings/ai/mom/{key+}'].Target | [0]" \
    --output text | tr -d '\r')"
if [[ "$PUT_ROUTE" == "integrations/$USERAPI_INT" ]]; then
  echo ">> PUT route target verified: $PUT_ROUTE"
else
  echo "ERROR: the PUT route is not wired to the userApi integration." >&2
  echo "       Saving edits would fail while everything else appeared to work." >&2
  exit 1
fi

# CORS, if the API declares an explicit allow-list. An API with no CORS block
# (or a wildcard) needs nothing here, so a missing PUT is only reported when
# there IS a list to be missing from.
CORS_METHODS="$(aws apigatewayv2 get-api --api-id "$API_ID" \
    --query 'CorsConfiguration.AllowMethods' --output text 2>/dev/null | tr -d '\r' || true)"
if [[ -n "$CORS_METHODS" && "$CORS_METHODS" != "None" ]]; then
  if [[ "$CORS_METHODS" == *"PUT"* || "$CORS_METHODS" == *"*"* ]]; then
    echo ">> CORS allows PUT."
  else
    echo ">> WARNING: CORS AllowMethods is '$CORS_METHODS' and excludes PUT."
    echo "   The native app is unaffected (CORS is a browser rule), but any"
    echo "   web client would fail to save MoM edits."
  fi
fi

echo
echo ">> Done. Four MoM routes live on $API_NAME."
echo
echo ">> Verify offline:"
echo "     python tests/test_mom_schema.py    # 46 unit tests — model + merge"
echo "     python tests/test_mom_api.py       # 33 route tests — mirror + access"
echo
echo ">> Verify live, against a recording you own (needs a JWT):"
echo "     curl -H \"Authorization: Bearer \$TOKEN\" \\"
echo "       \"\$API_BASE/recordings/ai/mom/\$(python -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=\"\"))' \"\$KEY\")\""
echo "   -> {\"mom\": {...}, \"document\": null, \"exists\": false} on a recording"
echo "      with no MoM yet. POST the same URL to build one."
echo
echo ">> NOTHING IS DESTRUCTIVE HERE. No table was created or altered, no IAM"
echo "   policy changed, no environment variable touched. The new \`mom\`"
echo "   attribute is written only when a user opens the MoM editor, and"
echo "   DELETE /recordings/ai/mom/{key+} removes the structure while LEAVING"
echo "   the mirrored document in the user's Documents list."
echo
echo ">> Rollback: redeploy the previous userApi zip. The four routes can stay"
echo "   (they 404 harmlessly once the handler no longer registers them), and"
echo "   any \`mom\` attribute already written is simply ignored by the older"
echo "   code — the mirrored minutes_of_meeting document keeps working."
