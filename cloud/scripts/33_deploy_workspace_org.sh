#!/usr/bin/env bash
# =============================================================
# 33_deploy_workspace_org.sh — Contacts, Folders, Participants and first-class
# Tasks on userApi.
#
# Ships TWENTY-ONE routes:
#
#   Folders
#     POST   /folders                                     -> 201 {folder}
#     GET    /folders                                     -> {folders, general_count}
#     GET    /folders/{folder_id}                         -> {folder, contacts}
#     PATCH  /folders/{folder_id}                         -> {folder}
#     DELETE /folders/{folder_id}                         -> {deleted, counts...}
#     GET    /folders/{folder_id}/contacts                -> {contacts}
#     POST   /folders/{folder_id}/contacts/{contact_id}   -> {linked}
#     DELETE /folders/{folder_id}/contacts/{contact_id}   -> {unlinked}
#   Contacts
#     POST   /contacts        -> 201 {contact} | 200 {existing} | 409 {candidates}
#     GET    /contacts        ?search&limit&cursor        -> {contacts, next_cursor}
#     GET    /contacts/{contact_id}                       -> {contact, folders}
#     PATCH  /contacts/{contact_id}                       -> {contact}
#     DELETE /contacts/{contact_id}                       -> {deleted, counts...}
#   Meeting <-> folder / participants  (action FIRST, {key+} LAST — see below)
#     PATCH  /recordings/folder/{key+}                    -> {recording, folder_id}
#     GET    /recordings/participants/{key+}              -> {participants, speakers, ...}
#     PUT    /recordings/participants/{key+}              -> {participant, tasks_resolved}
#   Tasks (cross-meeting)
#     GET    /tasks           ?status&folder_id&...       -> {tasks, next_cursor}
#     GET    /tasks/{task_id}                             -> {task, contact, folder, recording}
#     PATCH  /tasks/{task_id}                             -> {task}
#     POST   /tasks/{task_id}/resolve                     -> {task}
#     GET    /tasks/{task_id}/assignee-candidates         -> {status, candidates}
#
# The four EXISTING task routes (/recordings/ai/tasks/{key+}) keep their route
# keys and change behaviour only: they are now served from the Tasks table
# instead of the recording row's embedded map. No route to create, and no
# coordinated app release needed — the response shape was kept backward
# compatible (every field the old API returned still means the same thing).
#
# WHAT THIS IS FOR
# ----------------
# Meetings had no organization and tasks had no identity. A task lived inside
# its recording row, so "what do I owe this week" could not be answered without
# opening meetings one at a time, and an assignee was a bare NAME string that
# nothing could notify. This adds the three entities that fix that — Folder,
# Contact, Task — plus the speaker->contact mapping that connects a voice in a
# transcript to a real person.
#
# THE ONE RULE THAT SHAPES EVERYTHING: identity is never guessed. The AI hears
# "Rahul, send the proposal" and records the NAME and WHICH SPEAKER said it. It
# does not decide which Rahul that is, and neither does this backend — not even
# when exactly one contact has that name. Those tasks are stored
# resolution_status = UNRESOLVED with the name preserved, and a human resolves
# them (POST /tasks/{id}/resolve, or by mapping the speaker). Silently assigning
# work to the wrong person is the failure this design exists to prevent.
#
# NEW TABLES — REQUIRED FIRST. Run scripts/31_create_workspace_tables.sh before
# this script. Five tables: Contacts, Folders, FolderContacts,
# MeetingParticipants, Tasks. Their key schemas and the reasoning behind each
# GSI are documented there.
#
# NEW IAM — REQUIRED. The userApi role can read/write Recordings, Users,
# Devices, DeviceKeys, UserDevices and CrmConnections. It has NO access to the
# five new tables, so without this grant every route above fails with
# AccessDeniedException at the first query. Granted below, scoped to exactly
# those five tables plus their indexes (Query needs the index ARN explicitly —
# a table-only ARN does not cover its GSIs, which is the single easiest thing to
# get wrong here and shows up as "user is not authorized to perform:
# dynamodb:Query on resource: .../index/owner-index").
#
# NEW ENV — REQUIRED. Five table names. Defaults are baked into the Lambda
# (Contacts/Folders/FolderContacts/MeetingParticipants/Tasks), so this only
# needs to set them when .env overrides them — but they are set unconditionally
# so the deployed config states what it is using rather than relying on a
# default that could drift from the table that actually exists.
#
# MIGRATION. Existing embedded tasks are migrated LAZILY, the first time each
# meeting's task list is read — idempotent, and it never modifies the embedded
# map. For the whole account at once (so the Task Tracker is complete on day
# one) run scripts/32_backfill_tasks.py after this. Nothing is destroyed by
# either path: the old `tasks` map stays exactly where it is as the rollback
# route, and userApi keeps writing to it (see _mirror_task_to_recording) until a
# later change retires it.
#
# Prerequisites: 31 (tables), 21 (AI workspace routes), 30 (trash).
# Idempotent: update-function-code / put-role-policy / ensure_route all
# overwrite or skip.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"
CONTACTS_TABLE="${CONTACTS_TABLE:-Contacts}"
FOLDERS_TABLE="${FOLDERS_TABLE:-Folders}"
FOLDER_CONTACTS_TABLE="${FOLDER_CONTACTS_TABLE:-FolderContacts}"
MEETING_PARTICIPANTS_TABLE="${MEETING_PARTICIPANTS_TABLE:-MeetingParticipants}"
TASKS_TABLE="${TASKS_TABLE:-Tasks}"

API_ID="$(aws apigatewayv2 get-apis \
            --query "Items[?Name=='${API_NAME}'].ApiId | [0]" \
            --output text)"
if [[ "$API_ID" == "None" || -z "$API_ID" ]]; then
  echo "ERROR: HTTP API '$API_NAME' not found — run 03_create_apigateway.sh first." >&2
  exit 1
fi
echo ">> API: $API_NAME ($API_ID)"

# -------------------------------------------------------------
# 0. Fail early if the tables do not exist.
#
# Deploying code that queries a missing table produces a 500 on every new route
# with a ResourceNotFoundException buried in CloudWatch. Checking here turns
# that into one clear line at deploy time.
# -------------------------------------------------------------
for t in "$CONTACTS_TABLE" "$FOLDERS_TABLE" "$FOLDER_CONTACTS_TABLE" \
         "$MEETING_PARTICIPANTS_TABLE" "$TASKS_TABLE"; do
  if ! aws dynamodb describe-table --table-name "$t" >/dev/null 2>&1; then
    echo "ERROR: table '$t' does not exist." >&2
    echo "       Run scripts/31_create_workspace_tables.sh first." >&2
    exit 1
  fi
done
echo ">> All five workspace tables present."

# -------------------------------------------------------------
# 1. IAM: read/write on the five new tables AND their indexes.
# -------------------------------------------------------------
USERAPI_ROLE_ARN="$(aws lambda get-function --function-name "$USERAPI_LAMBDA_NAME" \
                      --query 'Configuration.Role' --output text)"
USERAPI_ROLE_NAME="${USERAPI_ROLE_ARN##*/}"
echo ">> userApi role: $USERAPI_ROLE_NAME"

ORG_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ORG_POLICY_FILE="$(mktemp)"
trap 'rm -f "$ORG_POLICY_FILE"' EXIT

ddb_arn() { echo "arn:aws:dynamodb:${AWS_REGION}:${ORG_ACCOUNT_ID}:table/$1"; }

cat > "$ORG_POLICY_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "WorkspaceOrgTables", "Effect": "Allow",
      "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
                 "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:BatchGetItem"],
      "Resource": [
        "$(ddb_arn "$CONTACTS_TABLE")",
        "$(ddb_arn "$FOLDERS_TABLE")",
        "$(ddb_arn "$FOLDER_CONTACTS_TABLE")",
        "$(ddb_arn "$MEETING_PARTICIPANTS_TABLE")",
        "$(ddb_arn "$TASKS_TABLE")"
      ] },
    { "Sid": "WorkspaceOrgIndexes", "Effect": "Allow",
      "Action": "dynamodb:Query",
      "Resource": [
        "$(ddb_arn "$CONTACTS_TABLE")/index/*",
        "$(ddb_arn "$FOLDERS_TABLE")/index/*",
        "$(ddb_arn "$FOLDER_CONTACTS_TABLE")/index/*",
        "$(ddb_arn "$MEETING_PARTICIPANTS_TABLE")/index/*",
        "$(ddb_arn "$TASKS_TABLE")/index/*"
      ] }
  ]
}
EOF

aws iam put-role-policy \
  --role-name "$USERAPI_ROLE_NAME" \
  --policy-name "userApi-workspace-org" \
  --policy-document "file://$(winpath "$ORG_POLICY_FILE")"
echo ">> userApi-workspace-org policy attached (5 tables + their indexes)."

# -------------------------------------------------------------
# 2. Env: the five table names.
#
# Merged into the EXISTING environment rather than replacing it —
# update-function-configuration --environment REPLACES the whole map, so
# writing only these five would wipe JWT_SECRET_ARN, BUCKET_NAME, the
# Salesforce config and every other variable the function needs.
# scripts/_merge_env.py exists for exactly this and is what the other deploy
# scripts use.
# -------------------------------------------------------------
# _merge_env.py reads the CURRENT env as JSON on argv[1] and PRINTS the merged
# map — it does not call AWS itself. Same wrapper as script 28.
MERGE_ENV_PY="$SCRIPT_DIR/_merge_env.py"

merge_env() {
  local fn="$1"; shift
  local current merged
  current="$(aws lambda get-function-configuration \
               --function-name "$fn" \
               --query 'Environment.Variables' --output json)"
  merged="$(python "$MERGE_ENV_PY" "$current" "$@")"
  aws lambda update-function-configuration \
    --function-name "$fn" --environment "$merged" \
    --query "FunctionName" --output text >/dev/null
  aws lambda wait function-updated-v2 --function-name "$fn"
  echo ">> $fn env merged: $*"
}

merge_env "$USERAPI_LAMBDA_NAME" \
  "CONTACTS_TABLE=$CONTACTS_TABLE" \
  "FOLDERS_TABLE=$FOLDERS_TABLE" \
  "FOLDER_CONTACTS_TABLE=$FOLDER_CONTACTS_TABLE" \
  "MEETING_PARTICIPANTS_TABLE=$MEETING_PARTICIPANTS_TABLE" \
  "TASKS_TABLE=$TASKS_TABLE"

# -------------------------------------------------------------
# 3. Deploy the code. Shared modules vendored FLAT — userApi imports
#    ai_schema/groq_client/prompts/stt_result/transcript_store at module scope,
#    so a zip without them fails on cold start (same packaging as 23/25/27/30).
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
# 4. Wire the routes.
# -------------------------------------------------------------
USERAPI_INT="$(aws apigatewayv2 get-integrations --api-id "$API_ID" \
    --query "Items[?contains(IntegrationUri, '${USERAPI_LAMBDA_NAME}')].IntegrationId | [0]" \
    --output text)"
if [[ "$USERAPI_INT" == "None" || -z "$USERAPI_INT" ]]; then
  echo "ERROR: no API integration for $USERAPI_LAMBDA_NAME — run 17 first." >&2
  exit 1
fi

# Filter by CONTENT, not by line position — the normalization script 28 uses,
# and it is load-bearing. `aws --output text` here emits the RouteId plus a
# stray "None" line in EITHER order, so a `head -1` that happens to grab the
# "None" makes an existing route look absent. On the first re-run of this script
# that produced a ConflictException on "GET /folders" immediately after
# correctly reporting "POST /folders" as existing, and aborted a deploy whose
# routes were all already in place.
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

# Folders + contacts: ordinary {id} path variables, so they nest normally.
ensure_route "$USERAPI_INT" "POST /folders"
ensure_route "$USERAPI_INT" "GET /folders"
ensure_route "$USERAPI_INT" "GET /folders/{folder_id}"
ensure_route "$USERAPI_INT" "PATCH /folders/{folder_id}"
ensure_route "$USERAPI_INT" "DELETE /folders/{folder_id}"
ensure_route "$USERAPI_INT" "GET /folders/{folder_id}/contacts"
ensure_route "$USERAPI_INT" "POST /folders/{folder_id}/contacts/{contact_id}"
ensure_route "$USERAPI_INT" "DELETE /folders/{folder_id}/contacts/{contact_id}"

ensure_route "$USERAPI_INT" "POST /contacts"
ensure_route "$USERAPI_INT" "GET /contacts"
ensure_route "$USERAPI_INT" "GET /contacts/{contact_id}"
ensure_route "$USERAPI_INT" "PATCH /contacts/{contact_id}"
ensure_route "$USERAPI_INT" "DELETE /contacts/{contact_id}"

# Recording-scoped routes put the ACTION FIRST and the key LAST, exactly like
# the AI and trash routes and for the same hard reason: a recording key
# contains slashes so it must be greedy ({key+}), and API Gateway rejects a
# greedy variable in any but the FINAL position —
#   BadRequestException: Greedy variables may only be in last position
# — so "/recordings/{key+}/participants" cannot be created at all. The literal
# prefixes cannot collide with a real key: keys always begin
# "recordings/{user_id}/..." or a legacy device id, never "folder/" or
# "participants/".
ensure_route "$USERAPI_INT" "PATCH /recordings/folder/{key+}"
ensure_route "$USERAPI_INT" "GET /recordings/participants/{key+}"
ensure_route "$USERAPI_INT" "PUT /recordings/participants/{key+}"

ensure_route "$USERAPI_INT" "GET /tasks"
ensure_route "$USERAPI_INT" "GET /tasks/{task_id}"
ensure_route "$USERAPI_INT" "PATCH /tasks/{task_id}"
ensure_route "$USERAPI_INT" "POST /tasks/{task_id}/resolve"
ensure_route "$USERAPI_INT" "GET /tasks/{task_id}/assignee-candidates"

# -------------------------------------------------------------
# 5. Verify the role can actually reach a new table's INDEX.
#
# The table-vs-index ARN distinction is the failure mode worth probing: a
# policy granting Query on the table but not on table/index/* passes every
# GetItem and fails every list route, which looks like "folders are empty"
# rather than like a permissions problem.
# -------------------------------------------------------------
DECISION="$(aws iam simulate-principal-policy \
    --policy-source-arn "$USERAPI_ROLE_ARN" \
    --action-names "dynamodb:Query" \
    --resource-arns "$(ddb_arn "$FOLDERS_TABLE")/index/owner-index" \
    --query 'EvaluationResults[0].EvalDecision' --output text 2>/dev/null || echo "unknown")"
case "$DECISION" in
  allowed)
    echo ">> dynamodb:Query on ${FOLDERS_TABLE}/index/owner-index: allowed." ;;
  unknown)
    echo ">> WARNING: couldn't simulate the policy (needs iam:SimulatePrincipalPolicy)."
    echo "   Verify by hand that $USERAPI_ROLE_NAME can Query the new tables' indexes." ;;
  *)
    echo "ERROR: $USERAPI_ROLE_NAME is DENIED dynamodb:Query on the Folders index." >&2
    echo "       Every list route would return empty instead of failing loudly." >&2
    exit 1 ;;
esac

echo
echo ">> Done. Verify with:"
echo "     python tests/test_workspace_org.py          # 116 offline unit tests"
echo "     python tests/test_workspace_api.py --live   # live end-to-end"
echo
echo ">> Then migrate existing embedded tasks for the whole account:"
echo "     python scripts/32_backfill_tasks.py --dry-run"
echo "     python scripts/32_backfill_tasks.py"
echo "     python scripts/32_backfill_tasks.py --verify"
echo
echo ">> The embedded recording.tasks map is NOT removed by any of this. It"
echo "   stays as the rollback path and userApi keeps mirroring writes into it."
