#!/usr/bin/env bash
# =============================================================
# 21_deploy_ai_workspace.sh — the AI Meeting Workspace.
#
# Ships the shared AI core plus both halves of the pipeline that use it:
#
#   shared/                THE shared AI core, vendored into BOTH zips.
#     groq_client.py                one Groq client, one retry policy, one TPM
#                                   budget (the quota is per-ACCOUNT, so both
#                                   callers must pace identically to be correct)
#     prompts.py                    every prompt template in the backend
#     ai_schema.py                  strict coercion + the cache fingerprint
#
#   1. transcribeRecording <- functions/transcribe/ + shared/
#        Now runs a SECOND Groq stage after the summary, writing
#        `meeting_highlights` (decisions / action items / deadlines / numbers /
#        open questions / risks). Degrades independently: if that call is
#        rate-limited the brief still ships and the workspace regenerates the
#        highlights on demand.
#        Also stamps `transcript_fingerprint`, which is what the document cache
#        keys on — so reprocessing a recording invalidates documents generated
#        from the OLD transcript, and does NOT invalidate them when the
#        transcript comes back byte-identical.
#
#   2. userApi <- functions/userapi/ + shared/
#        Adds the on-demand AI routes (documents, Quick AI, chat, highlights
#        regeneration). Needs GROQ_API_KEY in its env — it never had it before,
#        because it never called Groq.
#
# ROUTE SHAPE — the action comes BEFORE the recording key:
#     /recordings/ai/{action}/{key+}      not   /recordings/{key+}/{action}
#   API Gateway rejects a greedy path variable in any but the final position
#   ("Greedy variables may only be in last position of route key"), and a
#   recording key contains slashes so it MUST be greedy and MUST be last.
#   Verified against live AWS. Do not "tidy" these paths.
#
# TIMEOUT: userApi is raised to 29s. API Gateway's integration timeout caps at
# 29s, so a longer Lambda timeout cannot help — the gateway cuts first. The
# generation code respects ONDEMAND_DEADLINE_SECONDS (22s) and returns a
# partial/stitched result rather than being killed mid-flight.
#
# Prerequisites: scripts 12-20 (this only adds to what they built).
# Idempotent: update-function-code / update-function-configuration / ensure_route
# all simply overwrite or skip.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"
TRANSCRIBE_LAMBDA_NAME="${TRANSCRIBE_LAMBDA_NAME:-transcribeRecording}"

# On-demand generation budget. Must stay under API Gateway's 29s ceiling.
ONDEMAND_DEADLINE_SECONDS="${ONDEMAND_DEADLINE_SECONDS:-22}"
USERAPI_TIMEOUT="${USERAPI_TIMEOUT:-29}"

: "${GROQ_API_KEY:?GROQ_API_KEY not set (put it in .env) — userApi now calls Groq}"

API_ID="$(aws apigatewayv2 get-apis \
            --query "Items[?Name=='${API_NAME}'].ApiId | [0]" \
            --output text)"
if [[ "$API_ID" == "None" || -z "$API_ID" ]]; then
  echo "ERROR: HTTP API '$API_NAME' not found — run 03_create_apigateway.sh first." >&2
  exit 1
fi
echo ">> API: $API_NAME ($API_ID)"

# -------------------------------------------------------------
# Package a Python Lambda with the shared AI core vendored in.
#
# The shared modules are zipped FLAT (ai_schema.py, groq_client.py, prompts.py
# at the archive root), because that is how `import ai_schema` resolves inside
# the Lambda runtime — the handler's directory is on sys.path, subdirectories
# are not. Your editor will flag those imports as unresolved locally; that is
# expected and does not affect the deployed function.
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
        # Flat at the archive root — see the note above.
        z.write(mod, mod.name)
        print(f"   + {mod.name}")
print(f">> packaged {out.relative_to(root)}")
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

# merge_env <function-name> <KEY=VALUE>...
# Merges into the LIVE env rather than replacing it, so BUCKET_NAME /
# ELEVENLABS_API_KEY / JWT_SECRET etc. on the deployed function survive.
merge_env() {
  local fn="$1"; shift
  local current merged
  current="$(aws lambda get-function-configuration \
               --function-name "$fn" \
               --query 'Environment.Variables' --output json)"
  merged="$(python - "$current" "$@" <<'PY'
import json, sys
env = json.loads(sys.argv[1]) if sys.argv[1] not in ("null", "") else {}
for pair in sys.argv[2:]:
    k, _, v = pair.partition("=")
    env[k] = v
print(json.dumps({"Variables": env}))
PY
)"
  aws lambda update-function-configuration \
    --function-name "$fn" \
    --environment "$merged" \
    --query "FunctionName" --output text >/dev/null
  aws lambda wait function-updated-v2 --function-name "$fn"
  echo ">> $fn env merged: $*" | sed -E 's/(GROQ_API_KEY=)[^ ]*/\1***/'
}

# ensure_route <integration-id> <route-key>
#
# The output normalization is load-bearing, not defensive noise. On this setup
# `aws --output text` returns the value followed by a SECOND line containing
# "None", with CRLF line endings — e.g. a route that exists yields
# "tbda7qf\r\nNone" and one that doesn't yields "None\r\nNone". A naive
# [[ "$existing" != "None" ]] guard therefore reads "None\r\nNone" as a real
# RouteId and silently SKIPS creating the route — which is exactly what
# happened to the three chat routes on the first run of this script.
#
# So: strip CRs, drop every "None"/blank line, and take whatever's left as
# the real RouteId. Originally this kept only the FIRST line (on the
# documented assumption that a real value always precedes the stray "None"),
# which is not reliable — a live run returned "None\r\nuqdyi96\r\n" for a
# route that DOES exist, i.e. the real value SECOND, and head -1 silently
# read that as "route absent" and tried to create a duplicate (which then
# failed loudly with ConflictException — better than corrupting state, but
# still a false "missing" on an existing route). Filtering by CONTENT rather
# than trusting line position is correct regardless of which order the CLI
# happens to emit the value/placeholder lines in.
# (scripts 17/18/20 carry the same unfixed guard; they got away with it
# because their queries happened to return an empty string rather than
# "None".)
ensure_route() {
  local integration_id="$1" route_key="$2"
  local existing
  # grep exits 1 when NOTHING matches (the route genuinely doesn't exist
  # yet) — under `set -o pipefail` that would abort the whole script via
  # set -e on the assignment itself, so this pipeline is deliberately run
  # OUTSIDE pipefail's reach (the `|| true` makes the last stage always
  # succeed; an empty $existing is exactly the "absent" signal below).
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

# Fail loudly if a route we just wired isn't actually there. A silently missing
# route is the one failure mode that looks like success in the log — the first
# run of this script printed "Route exists" for three routes that did not
# exist, and only an explicit post-check catches that.
verify_routes() {
  local missing=0 rk actual
  actual="$(aws apigatewayv2 get-routes --api-id "$API_ID" \
      --query 'Items[].RouteKey' --output text | tr '\t' '\n' | tr -d '\r')"
  for rk in "$@"; do
    if ! grep -Fxq "$rk" <<<"$actual"; then
      echo "   MISSING: $rk" >&2
      missing=$((missing + 1))
    fi
  done
  if (( missing )); then
    echo "ERROR: $missing route(s) were not created." >&2
    return 1
  fi
  echo ">> Verified: all $# AI routes are present on the API."
}

# -------------------------------------------------------------
# 0. Sanity: the shared modules must import cleanly BEFORE we ship them.
#    Catches a syntax error or a bad cross-import here rather than as a
#    runtime ImportError on the first real invocation.
# -------------------------------------------------------------
echo
echo ">> Verifying the shared AI core imports (flat, as in the zip)..."
( cd "$PROJECT_ROOT/shared" && python -c "
import ai_schema, groq_client, prompts
assert len(prompts.DOCUMENT_KEYS) == 8, prompts.DOCUMENT_KEYS
assert prompts.QUICK_ACTIONS, 'no quick actions'
assert ai_schema.AI_VERSION, 'no AI_VERSION'
print('   ai_schema, groq_client, prompts OK '
      f'({len(prompts.DOCUMENT_KEYS)} documents, '
      f'{len(prompts.QUICK_ACTIONS)} quick actions)')
" )

echo ">> Running the offline unit tests..."
( cd "$PROJECT_ROOT" && python tests/test_ai_workspace.py 2>&1 | tail -3 )

# -------------------------------------------------------------
# GROQ_TPM_LIMIT / GROQ_CONTEXT_TOKENS — shared by BOTH Lambdas, since the
# TPM quota is per-ACCOUNT (both callers spend from the same bucket) and the
# context window is per-MODEL (both callers use the same GROQ_MODEL).
#
# NOT hardcoded here with a stale fallback: this script used to default
# GROQ_TPM_LIMIT to 12000 (the free tier) when the env var wasn't exported,
# which would have SILENTLY DOWNGRADED a manually-upgraded plan's live value
# back to the free-tier number on the next deploy — a real, easy-to-hit
# regression once the account moved to a paid plan out-of-band from this
# script. Requiring it explicitly means a stale local shell can never
# quietly undo a plan upgrade.
GROQ_TPM_LIMIT_ERR="GROQ_TPM_LIMIT not set. Export the account's current tokens-per-minute limit (check the x-ratelimit-limit-tokens response header on a live Groq call, or the Groq console) before deploying. This is deliberately not defaulted, since silently falling back to the free-tier number would downgrade an upgraded plan."
: "${GROQ_TPM_LIMIT:?$GROQ_TPM_LIMIT_ERR}"
GROQ_CONTEXT_TOKENS="${GROQ_CONTEXT_TOKENS:-131072}"  # openai/gpt-oss-120b

# -------------------------------------------------------------
# 1. transcribeRecording: the staged summary + highlights.
#    Needs the SAME Groq env as userApi (GROQ_TPM_LIMIT/GROQ_CONTEXT_TOKENS
#    decide the single-pass-vs-map_reduce threshold in BOTH callers) — this
#    Lambda always had GROQ_API_KEY/GROQ_MODEL, but never had a TPM/context
#    override until now, so it was silently running the single-pass budget
#    math against groq_client.py's free-tier CODE DEFAULT even after the
#    account was upgraded.
# -------------------------------------------------------------
echo
echo ">> === transcribeRecording (staged summary + highlights) ==="
merge_env "$TRANSCRIBE_LAMBDA_NAME" \
  "GROQ_MODEL=${GROQ_MODEL:-openai/gpt-oss-120b}" \
  "GROQ_TPM_LIMIT=$GROQ_TPM_LIMIT" \
  "GROQ_CONTEXT_TOKENS=$GROQ_CONTEXT_TOKENS"
deploy_py_with_shared "$TRANSCRIBE_LAMBDA_NAME" "functions/transcribe"

# -------------------------------------------------------------
# 2. userApi: Groq env, a workable timeout, the code, then the routes.
#
# ORDER MATTERS: env + code go first, so that by the time a route exists the
# function can actually serve it. A route pointing at a build without the
# handler would 404 (or 500) for as long as the gap lasts.
# -------------------------------------------------------------
echo
echo ">> === userApi (on-demand documents / Quick AI / chat) ==="
merge_env "$USERAPI_LAMBDA_NAME" \
  "GROQ_API_KEY=$GROQ_API_KEY" \
  "GROQ_MODEL=${GROQ_MODEL:-openai/gpt-oss-120b}" \
  "GROQ_TPM_LIMIT=$GROQ_TPM_LIMIT" \
  "GROQ_CONTEXT_TOKENS=$GROQ_CONTEXT_TOKENS" \
  "ONDEMAND_DEADLINE_SECONDS=$ONDEMAND_DEADLINE_SECONDS"

# The old timeout was sized for DynamoDB reads (a few hundred ms). A Groq
# generation needs far longer, and 29s is the most API Gateway will wait.
aws lambda update-function-configuration \
  --function-name "$USERAPI_LAMBDA_NAME" \
  --timeout "$USERAPI_TIMEOUT" \
  --query "[FunctionName,Timeout]" --output text
aws lambda wait function-updated-v2 --function-name "$USERAPI_LAMBDA_NAME"
echo ">> $USERAPI_LAMBDA_NAME timeout=${USERAPI_TIMEOUT}s"

deploy_py_with_shared "$USERAPI_LAMBDA_NAME" "functions/userapi"

USERAPI_INT="$(aws apigatewayv2 get-integrations --api-id "$API_ID" \
    --query "Items[?contains(IntegrationUri, '${USERAPI_LAMBDA_NAME}')].IntegrationId | [0]" \
    --output text)"
if [[ "$USERAPI_INT" == "None" || -z "$USERAPI_INT" ]]; then
  echo "ERROR: no API integration for $USERAPI_LAMBDA_NAME — run 17 first." >&2
  exit 1
fi

echo
echo ">> Wiring the AI Workspace routes (action first, greedy {key+} last)..."
AI_ROUTES=(
  "GET /recordings/ai/documents/{key+}"
  "POST /recordings/ai/documents/{key+}"
  "PATCH /recordings/ai/documents/{key+}"
  "DELETE /recordings/ai/documents/{key+}"
  "POST /recordings/ai/custom-document/{key+}"
  "POST /recordings/ai/quick/{key+}"
  "POST /recordings/ai/highlights/{key+}"
  "GET /recordings/ai/chat/{key+}"
  "POST /recordings/ai/chat/{key+}"
  "DELETE /recordings/ai/chat/{key+}"
  "GET /recordings/ai/tasks/{key+}"
  "POST /recordings/ai/tasks/{key+}"
  "PATCH /recordings/ai/tasks/{key+}"
  "DELETE /recordings/ai/tasks/{key+}"
)
for rk in "${AI_ROUTES[@]}"; do
  ensure_route "$USERAPI_INT" "$rk"
done

verify_routes "${AI_ROUTES[@]}"

# -------------------------------------------------------------
# 3. Report. No new IAM is required: both functions already have the
#    Recordings read/write they need (documents, chat_history and
#    meeting_highlights are attributes on a row the functions already
#    update), and Groq is reached over the public internet, not via IAM.
# -------------------------------------------------------------
echo
echo ">> Done."
echo
echo "   Deployed:"
echo "     $TRANSCRIBE_LAMBDA_NAME  staged summary + highlights (single-pass first,"
echo "                             map_reduce fallback only past the safe transcript size)"
echo "     $USERAPI_LAMBDA_NAME     ${#AI_ROUTES[@]} on-demand AI routes (timeout ${USERAPI_TIMEOUT}s)"
echo "   Both:                    GROQ_TPM_LIMIT=$GROQ_TPM_LIMIT, GROQ_CONTEXT_TOKENS=$GROQ_CONTEXT_TOKENS"
echo
echo "   Verify (offline):  python tests/test_ai_workspace.py"
echo "   Verify (live):     python tests/test_ai_api.py"
echo
echo "   NOTE: recordings processed before today have no meeting_highlights."
echo "   The app generates them on first open of the workspace — no backfill"
echo "   needed. To pre-warm one:"
echo "     curl -X POST -H \"Authorization: Bearer \$JWT\" \\"
echo "       \"\$API_URL/recordings/ai/highlights/\$(python -c \"import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=''))\" 'RECORDING_KEY')\""
