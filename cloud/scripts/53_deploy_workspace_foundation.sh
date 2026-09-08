#!/usr/bin/env bash
# =============================================================
# 53_deploy_workspace_foundation.sh — wire the two workspace READ routes.
#
# NOTE (Phase 2C): this script's original job was "ship Phase 2A", but the
# code it packages is simply whatever is on disk now. Its remaining unique
# responsibility is the two GET routes below — script 56 wires the six write
# routes and nothing else wires these. If 56 is run without 53, the app can
# create an organisation but cannot LIST one, which reads as "Not Found" on
# the Workspaces screen. Run both.
#
#   GET /workspaces                  -> {workspaces, count, current_workspace_id}
#   GET /workspaces/{workspace_id}   -> {workspace}
#
# WHAT THIS DEPLOYS
#   * shared/workspace_schema.py — the vocabulary (roles, ids, tokens, expiry)
#   * the workspace authorization helpers in userApi
#   * the atomic email-uniqueness fix in signup()
#   * the two read routes above
#
# WHAT IT DELIBERATELY DOES NOT WIRE
#   No POST/PATCH/DELETE on /workspaces, and no invitation route. Creating an
#   organisation and accepting an invitation both depend on the unresolved
#   identity decision documented at the end of shared/workspace_schema.py:
#   whether one email may belong to both a Personal and an Organisation
#   context. Shipping half of that flow would bake in an answer nobody has
#   given yet. test_workspace_foundation.py has a tripwire test
#   (test_no_write_route_exists_yet) that fails if a write route appears
#   before that decision is made.
#
# THE EMAIL CHANGE IS THE ONE TO WATCH ON DEPLOY.
#   signup() now claims a deterministic "email#{address}" row in the Users
#   table before writing the account, so two concurrent signups for one
#   address can no longer both succeed. It is backward compatible in both
#   directions: existing accounts have no claim row and are unaffected, and a
#   rollback to the previous code simply stops writing claims (the orphaned
#   claim rows are inert — they carry no `email` attribute, so they are not in
#   the sparse email-index and login cannot see them).
#
# Prerequisites: 50 (tables). Run 51 (PITR) and 52 (backfill) around it as the
# ordering note in each of those explains — 52 is optional, see its header.
#
# Idempotent: routes are create-or-skip; the code deploy is a full replace.
#
# ROLLBACK
#   Redeploy the previous userApi zip. The two routes can stay — they resolve
#   through helpers that no longer exist only if the code is rolled back, so
#   delete them too if you roll back:
#     aws apigatewayv2 delete-route --api-id <id> --route-id <id>
#   No data written by this script needs undoing; it writes none.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"

# The HTTP API this wires routes onto. Resolved by NAME, like every other
# script here does, because the id is account-specific and must not be
# hardcoded. Missing from the first version of this script, which meant it
# reached the route-wiring step with an empty $API_ID and failed there — after
# it had already deployed the code.
API_ID="$(aws apigatewayv2 get-apis \
            --query "Items[?Name=='${API_NAME}'].ApiId | [0]" \
            --output text)"
if [[ "$API_ID" == "None" || -z "$API_ID" ]]; then
  echo "ERROR: HTTP API '$API_NAME' not found - run 03_create_apigateway.sh first." >&2
  exit 1
fi
echo ">> API: $API_NAME ($API_ID)"

# -------------------------------------------------------------
# 1. Preflight. A missing shared module bricks EVERY route on the next cold
#    start, not just the new ones — the failure 38 documents. Check before
#    anything is uploaded.
# -------------------------------------------------------------
WS_SHARED="$PROJECT_ROOT/shared/workspace_schema.py"
if [[ ! -f "$WS_SHARED" ]]; then
  echo "ERROR: shared/workspace_schema.py is missing." >&2
  exit 1
fi

python - "$PROJECT_ROOT" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
src = (root / "functions/userapi/lambda_function.py").read_text(encoding="utf-8")

required = [
    "import workspace_schema",
    "def list_workspaces(",
    "def get_workspace(",
    "def _require_workspace_member(",
    "def _require_workspace_role(",
    "def _require_workspace_capability(",
    "def _active_membership(",
    "def _ensure_personal_workspace(",
    "def _user_email_claim(",
    '("GET", "/workspaces")',
    '("GET", "/workspaces/{workspace_id}")',
]
missing = [r for r in required if r not in src]
if missing:
    sys.exit("ERROR: userapi is missing: " + ", ".join(missing))

# NO WRITE-ROUTE TRIPWIRE ANY MORE — deliberately removed, not overlooked.
#
# This script used to refuse to run if ("POST", "/workspaces") existed,
# because Phase 2A promised read-only routes while the Personal-vs-
# Organisation identity model was still undecided. That guard was protecting a
# DECISION, not a security property.
#
# The decision was made in Phase 2B (one email = one identity; a personal-only
# identity is refused at invitation acceptance, never merged or converted), and
# Phase 2B/2C legitimately added POST /workspaces and the member routes. The
# tripwire then became a stale blocker: it stopped this script from wiring its
# own two GET routes on a codebase that had correctly moved on. The equivalent
# test tripwire was retired in Phase 2B; this was its twin, missed at the time.
#
# The security invariants that DO still matter are checked in
# 56_deploy_workspace_resources.sh (authorization order: workspace ->
# membership -> creator/owner, in the read, write, AI and contact predicates).
print(">> Preflight: workspace helpers + 2 read routes present.")
PY

# -------------------------------------------------------------
# 2. Offline tests. A change that ships with a red suite is unreviewable.
# -------------------------------------------------------------
echo ">> Running the workspace foundation suite ..."
python -m pytest "$PROJECT_ROOT/tests/test_workspace_foundation.py" -q
echo ">> Running the FULL backend suite (no regression allowed) ..."
python -m pytest "$PROJECT_ROOT/tests" -q

# -------------------------------------------------------------
# 3. Deploy. Shared modules vendored FLAT (same packaging as 23/25/27/30/
#    33/36/38) — the glob picks up workspace_schema.py automatically, and the
#    assertion below proves it landed rather than trusting that.
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

  ( cd "$PROJECT_ROOT" && python - "$src_dir" <<'PY'
import sys, zipfile
from pathlib import Path

names = zipfile.ZipFile(Path.cwd() / sys.argv[1] / "function.zip").namelist()
missing = [m for m in ("lambda_function.py", "workspace_schema.py",
                       "share_schema.py", "mom_schema.py", "ai_schema.py",
                       "groq_client.py", "prompts.py")
           if m not in names]
if missing:
    sys.exit(f"ERROR: archive is missing {', '.join(missing)} — refusing to deploy.")
print(f">> archive verified: {len(names)} entries, workspace_schema.py present")
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
# 4. Wire the two read routes.
# -------------------------------------------------------------
USERAPI_INT="$(aws apigatewayv2 get-integrations --api-id "$API_ID" \
    --query "Items[?contains(IntegrationUri, '${USERAPI_LAMBDA_NAME}')].IntegrationId | [0]" \
    --output text)"
if [[ "$USERAPI_INT" == "None" || -z "$USERAPI_INT" ]]; then
  echo "ERROR: no API integration for $USERAPI_LAMBDA_NAME — run 17 first." >&2
  exit 1
fi

# Filter by CONTENT, not line position — `aws --output text` emits the RouteId
# plus a stray "None" in either order (see 33/38).
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

ensure_route "$USERAPI_INT" "GET /workspaces"
ensure_route "$USERAPI_INT" "GET /workspaces/{workspace_id}"

echo
echo "=============================================================="
echo " Phase 2A deployed."
echo
echo " Existing behaviour is unchanged: no resource table was touched,"
echo " no route was modified, and every user's personal workspace"
echo " resolves whether or not 52 has run."
echo
echo " Next: python scripts/52_migrate_personal_workspaces.py --dry-run"
echo "=============================================================="
