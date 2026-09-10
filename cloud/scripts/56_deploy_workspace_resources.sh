#!/usr/bin/env bash
# =============================================================
# 56_deploy_workspace_resources.sh — ship Phase 2B and wire its routes.
#
#   POST   /workspaces                                    create organisation
#   GET    /workspaces/{workspace_id}/members             list members
#   POST   /workspaces/{workspace_id}/members/invite      invite
#   PATCH  /workspaces/{workspace_id}/members/{user_id}   change role
#   DELETE /workspaces/{workspace_id}/members/{user_id}   remove member
#   POST   /workspace-invitations/{token}/accept          accept
#
# WHAT ELSE CHANGES ON DEPLOY (no new route, but real behaviour)
#   * request_upload stamps workspace_id + created_by on the recording row,
#     from a membership verified BEFORE anything is presigned.
#   * get_recording / list_recordings admit organisation readers per
#     _can_read_meeting. _owned_recording is deliberately NOT widened, so the
#     ~30 mutating AI routes stay creator-only.
#   * Tasks and Contacts are stamped with workspace_id + created_by.
#
# BACKWARD COMPATIBILITY — the property to check after deploying.
#   A client that sends no x-minutex-workspace header behaves EXACTLY as
#   before: _creation_workspace falls back to the caller's personal
#   workspace, list_recordings takes its original two-index path, and rows
#   with no workspace_id resolve to their owner's personal workspace. The
#   existing app needs no change to keep working.
#
# ORDERING. Run AFTER 54 (the workspace-index GSIs must be ACTIVE — querying
# a building index is a ValidationException). 55 (the backfill) is optional
# and may run before or after this; reads are correct either way.
#
# Prerequisites: 50, 53, 54.
# Idempotent: routes are create-or-skip; the code deploy is a full replace.
#
# ROLLBACK
#   Redeploy the previous userApi zip and delete the six routes below. Rows
#   already stamped with workspace_id are inert to the old code — it never
#   reads the attribute — so no data needs undoing. The GSIs may stay.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"

API_ID="$(aws apigatewayv2 get-apis \
            --query "Items[?Name=='${API_NAME}'].ApiId | [0]" \
            --output text)"
if [[ "$API_ID" == "None" || -z "$API_ID" ]]; then
  echo "ERROR: HTTP API '$API_NAME' not found � run 03_create_apigateway.sh first." >&2
  exit 1
fi
echo ">> API: $API_NAME ($API_ID)"

# -------------------------------------------------------------
# 1. Preflight. A missing shared module bricks EVERY route on the next cold
#    start, not just the new ones (the failure 38 documents).
# -------------------------------------------------------------
[[ -f "$PROJECT_ROOT/shared/workspace_schema.py" ]] || {
  echo "ERROR: shared/workspace_schema.py is missing." >&2; exit 1; }

python - "$PROJECT_ROOT" <<'PY'
import sys
from pathlib import Path

root = Path(sys.argv[1])
src = (root / "functions/userapi/lambda_function.py").read_text(encoding="utf-8")

required = [
    "def create_workspace(", "def list_members(", "def invite_member(",
    "def accept_invitation(", "def update_member(", "def remove_member(",
    "def _can_read_meeting(", "def _creation_workspace(",
    "def _row_workspace_id(", "def _has_organisation_membership(",
    '("POST", "/workspaces")',
    '("POST", "/workspace-invitations/{token}/accept")',
]
missing = [r for r in required if r not in src]
if missing:
    sys.exit("ERROR: userapi is missing: " + ", ".join(missing))

# INVARIANT 1 — read access must not silently become write access.
# _owned_recording gates ~30 mutating routes and must use the WRITE
# predicate, never the read one.
#
# Comment lines are stripped first: these functions' docstrings discuss the
# other predicate by name, and matching that prose would make the guard cry
# wolf on the very code it protects.
def code_of(fn, span=3000):
    start = src.index(f"def {fn}(")
    body = src[start:start + span]
    return "\n".join(line.split("#", 1)[0] for line in body.splitlines())

owned = code_of("_owned_recording")
if "_can_read_meeting" in owned:
    sys.exit("ERROR: _owned_recording now calls _can_read_meeting — that "
             "would grant organisation readers WRITE access to ~30 routes. "
             "Read and write authority are separate; see the comment there.")
if "_can_write_meeting" not in owned:
    sys.exit("ERROR: _owned_recording no longer delegates to "
             "_can_write_meeting — the organisation membership gate would be "
             "bypassed and a REMOVED member could mutate meetings they "
             "created. This is the P0 regression; see _can_write_meeting.")

# INVARIANT 2 — the membership gate must precede the creator branch in BOTH
# predicates. If a creator test runs first, a removed member keeps access to
# organisation data they created (the verified P0 bug).
for fn in ("_can_read_meeting", "_can_write_meeting"):
    body = code_of(fn, 2600)
    gate = body.find("is_organisation_workspace_id")
    creator = body.find('item.get("user_id") == user_id')
    if gate < 0 or creator < 0:
        sys.exit(f"ERROR: cannot verify authorization order in {fn}.")
    if creator < gate:
        sys.exit(f"ERROR: {fn} tests the CREATOR before the organisation "
                 "membership gate. A removed member would keep access to "
                 "organisation data they created. Restore the order: "
                 "workspace -> membership -> creator/role.")

# INVARIANT 3 — the AI path must delegate, not carry its own copy of the
# creator test. That copy is what made the AI the weaker door once the REST
# side was fixed.
ai = code_of("owns_recording", 1200)
if "_can_write_meeting" not in ai:
    sys.exit("ERROR: AIContext.owns_recording no longer delegates to "
             "_can_write_meeting — the AI path would drift from the REST "
             "path and could leak organisation transcripts to a removed "
             "member.")
# INVARIANT 4 (Phase 2C) — the CONTACT gate must also check membership
# before the owner test, or a member removed from an organisation would keep
# access to shared contacts they created. Same bug class as the Phase 2B P0.
# The span must cover the WHOLE function. Phase 2D inserted the projected-
# member branch (the `if not item:` block) AHEAD of both markers, and a
# window that stops short finds neither - which reads as "cannot verify"
# even though the order is correct. Widened, not relaxed: the gate<owner
# assertion below is unchanged and still fails loudly on a real inversion.
oc = code_of("_owned_contact", 4000)
gate = oc.find("is_organisation_workspace_id")
owner = oc.find('item.get("owner_user_id") != user_id')
if gate < 0 or owner < 0:
    sys.exit("ERROR: cannot verify authorization order in _owned_contact.")
if owner < gate:
    sys.exit("ERROR: _owned_contact tests the OWNER before the organisation "
             "membership gate. A removed member would keep access to shared "
             "contacts they created.")

print(">> Preflight: authorization order verified (membership before "
      "creator/owner) in read, write, AI and contact predicates.")
print(">> Preflight: Phase 2B handlers present; _owned_recording still "
      "creator-only.")
PY

# -------------------------------------------------------------
# 2. Offline tests. No regression is allowed against the Phase 2A baseline.
# -------------------------------------------------------------
echo ">> Running the workspace suites ..."
python -m pytest "$PROJECT_ROOT/tests/test_workspace_foundation.py" \
                 "$PROJECT_ROOT/tests/test_workspace_resources.py" -q
echo ">> Running the FULL backend suite ..."
python -m pytest "$PROJECT_ROOT/tests" -q

# -------------------------------------------------------------
# 3. Deploy. Shared modules vendored FLAT (same packaging as 23/25/…/53).
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
# 4. Routes.
# -------------------------------------------------------------
USERAPI_INT="$(aws apigatewayv2 get-integrations --api-id "$API_ID" \
    --query "Items[?contains(IntegrationUri, '${USERAPI_LAMBDA_NAME}')].IntegrationId | [0]" \
    --output text)"
if [[ "$USERAPI_INT" == "None" || -z "$USERAPI_INT" ]]; then
  echo "ERROR: no API integration for $USERAPI_LAMBDA_NAME — run 17 first." >&2
  exit 1
fi

ensure_route() {
  local integration_id="$1" route_key="$2" existing
  existing="$(aws apigatewayv2 get-routes --api-id "$API_ID" \
      --query "Items[?RouteKey=='${route_key}'].RouteId | [0]" --output text \
      | tr -d '\r' | grep -v '^None$' | grep -v '^$' | head -1 || true)"
  if [[ -n "$existing" ]]; then
    echo ">> Route exists: $route_key ($existing)"
    return 0
  fi
  aws apigatewayv2 create-route --api-id "$API_ID" \
    --route-key "$route_key" --target "integrations/$integration_id" >/dev/null
  echo ">> Route created: $route_key"
}

ensure_route "$USERAPI_INT" "POST /workspaces"
ensure_route "$USERAPI_INT" "GET /workspaces/{workspace_id}/members"
ensure_route "$USERAPI_INT" "POST /workspaces/{workspace_id}/members/invite"
ensure_route "$USERAPI_INT" "PATCH /workspaces/{workspace_id}/members/{user_id}"
ensure_route "$USERAPI_INT" "DELETE /workspaces/{workspace_id}/members/{user_id}"
ensure_route "$USERAPI_INT" "POST /workspace-invitations/{token}/accept"

echo
echo "=============================================================="
echo " Phase 2B deployed."
echo
echo " A client that sends no x-minutex-workspace header behaves"
echo " exactly as before. Verify that first, then optionally run:"
echo "   python scripts/55_backfill_resource_workspaces.py --dry-run"
echo "=============================================================="
