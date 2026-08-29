#!/usr/bin/env bash
# =============================================================
# 30_deploy_trash.sh — Trash: soft delete, restore, permanent delete.
#
# Ships FOUR routes on userApi:
#
#   DELETE /recordings/{key+}             -> 200 {trashed, key, deleted_at}
#   GET    /trash                         -> 200 {recordings, count}
#   POST   /recordings/restore/{key+}     -> 200 {restored, key, status}
#   DELETE /recordings/permanent/{key+}   -> 200 {deleted, key}
#
# WHAT IT IS FOR
# --------------
# Until now nothing in MinuteX could be deleted at all, and the first cut of
# this feature deleted permanently and immediately. Both are wrong ends of the
# same stick: a Desk you cannot tidy, or a Desk where one mis-tap destroys a
# meeting. Delete is now TWO STEPS — Desk -> Trash -> (Restore | gone).
#
# SOFT DELETE IS A FLAG ON THE EXISTING ROW, never a copy and never a second
# table: `recording_status = "trashed"` plus a `deleted_at` stamp, written with
# update_item onto the row that is already there. Everything the recording owns
# (transcript, documents, tasks, chat, highlights, speaker names, crm_records)
# is an attribute ON that row, so trashing touches none of it and restoring
# brings all of it back. Copying into a "trash table" would duplicate every AI
# artifact and give us two rows that could drift apart.
#
# WHY NOT status = "trashed": `status` is the PIPELINE's field and the
# transcription Lambda writes it without asking anyone. Overloading it would
# let a webhook landing after a trash quietly un-trash the recording, and would
# destroy the information Restore needs to put the row back as it was.
# `recording_status` is a separate lifecycle axis owned only by userApi.
#
# BACKWARD COMPATIBLE BY CONSTRUCTION: every row written before this has no
# `recording_status` at all, and MISSING MEANS ACTIVE. No backfill, no
# migration, and no existing recording changes behaviour. MinuteX filter tests
# one exact string ("trashed"), so complete/failed/uploading/transcribing/
# generating_ai all keep listing exactly as before.
#
# PERMANENT DELETE is the only route that destroys anything, and keeps the
# original ordering: S3 audio, S3 transcript, then the DynamoDB row LAST. If it
# dies halfway the row survives and a retry converges; the reverse order would
# strand the S3 objects with nothing pointing at them — invisible in the app,
# still billed. The S3 deletes stay BEST-EFFORT (S3 DELETE is idempotent, so
# the only failure is a real permission/service problem, and blocking the row
# delete on it would leave the user unable to remove what they asked to remove).
#
# IN-FLIGHT PROTECTION is on PERMANENT delete only: "uploading"/"uploaded"
# answer 409, because transcribeRecording writes with update_item and would
# resurrect a row deleted under it as a ghost fragment. TRASHING during those
# states is deliberately allowed — setting a lifecycle flag races nothing, and
# a user who just uploaded the wrong file shouldn't wait out a transcription.
#
# RETENTION: `deleted_at` is stamped so a "Trash for 30 days" sweep can be
# added later without another migration. Nothing expires automatically today —
# a retention job that deletes user data is not something to ship as a side
# effect of adding a Trash screen.
#
# NEW IAM — REQUIRED, and verified against the live role during the ap-south-1
# deploy on 2026-08-18. An earlier draft of this script asserted "no new IAM";
# that was WRONG. The userApi role held s3:GetObject + s3:PutObject and
# dynamodb:GetItem/Query/UpdateItem, but NOT the two delete actions:
#
#   s3:DeleteObject      on recordings/* AND transcripts/*   (was implicitDeny)
#   dynamodb:DeleteItem  on table/Recordings                 (was implicitDeny)
#
# Soft delete and restore need NEITHER — they are update_item on a table the
# role already writes. Only PERMANENT delete does. Without the grant it fails
# in the worst way available: the S3 deletes are best-effort so they log and
# continue, then delete_item throws and the caller gets a 500 — a recording
# stuck in Trash forever with its audio still billed.
#
# The transcripts/* prefix is easy to miss: transcript objects live under
# "transcripts/", NOT under "recordings/", so a policy scoped to the upload
# prefix alone would delete the audio and silently leak every transcript.
#
# Applied by step 0 below from scripts/iam/userApi-permanent-delete.json.
# NO NEW ENV.
#
# Prerequisites: script 21 (AI workspace routes on userApi).
# Idempotent: update-function-code / ensure_route overwrite or skip.
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

# -------------------------------------------------------------
# 0. IAM: the two delete actions permanent delete needs.
#
# Scoped narrowly on purpose — s3:DeleteObject is limited to the two prefixes
# that actually hold recording data, and dynamodb:DeleteItem to the Recordings
# table (no index ARN: DeleteItem does not apply to a GSI). A wildcard here
# would let a future bug in userApi delete anything in the bucket.
# -------------------------------------------------------------
USERAPI_ROLE_NAME="$(aws lambda get-function --function-name "$USERAPI_LAMBDA_NAME"                        --query 'Configuration.Role' --output text)"
USERAPI_ROLE_NAME="${USERAPI_ROLE_NAME##*/}"
echo ">> userApi role: $USERAPI_ROLE_NAME"

# The checked-in JSON documents the shape; the ARNs are rendered here from the
# environment so this is not pinned to one account or bucket.
: "${BUCKET_NAME:?BUCKET_NAME not set (put it in .env)}"
PD_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
PD_POLICY_FILE="$(mktemp)"
trap 'rm -f "$PD_POLICY_FILE"' EXIT
cat > "$PD_POLICY_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "PermanentDeleteAudioAndTranscript", "Effect": "Allow",
      "Action": "s3:DeleteObject",
      "Resource": ["arn:aws:s3:::${BUCKET_NAME}/recordings/*",
                   "arn:aws:s3:::${BUCKET_NAME}/transcripts/*"] },
    { "Sid": "PermanentDeleteRecordingRow", "Effect": "Allow",
      "Action": "dynamodb:DeleteItem",
      "Resource": "arn:aws:dynamodb:${AWS_REGION}:${PD_ACCOUNT_ID}:table/${RECORDINGS_TABLE:-Recordings}" }
  ]
}
EOF
aws iam put-role-policy   --role-name "$USERAPI_ROLE_NAME"   --policy-name "userApi-permanent-delete"   --policy-document "file://$(winpath "$PD_POLICY_FILE")"
echo ">> userApi-permanent-delete policy attached."

# -------------------------------------------------------------
# 1. Deploy the code. Shared modules vendored FLAT — userApi imports
#    ai_schema/groq_client/prompts/transcript_store at module scope, so a zip
#    without them fails on cold start (same packaging as scripts 23/25/27).
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
# 2. Wire the route.
#
# The recording key is greedy and therefore LAST — API Gateway allows {key+}
# only in the final position. This route is the DELETE sibling of the existing
# GET/PATCH /recordings/{key+} and needs no new integration.
# -------------------------------------------------------------
USERAPI_INT="$(aws apigatewayv2 get-integrations --api-id "$API_ID" \
    --query "Items[?contains(IntegrationUri, '${USERAPI_LAMBDA_NAME}')].IntegrationId | [0]" \
    --output text)"
if [[ "$USERAPI_INT" == "None" || -z "$USERAPI_INT" ]]; then
  echo "ERROR: no API integration for $USERAPI_LAMBDA_NAME — run 17 first." >&2
  exit 1
fi

# See script 20 for why the head -1 | tr -d '\r' normalization is load-bearing.
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

# DELETE /recordings/{key+} already exists if an earlier revision of this
# script ran — ensure_route skips it. Its BEHAVIOUR changes with the code
# deployed above (hard delete -> soft delete), not with the route itself.
#
# restore/permanent put the ACTION FIRST and the key LAST, exactly like the AI
# routes: API Gateway rejects a greedy variable in any but the final position,
# and a recording key contains slashes, so "/recordings/{key+}/restore" cannot
# be created at all. The literal prefixes cannot collide with a real key —
# keys always begin "recordings/{user_id}/..." or a legacy device id.
ensure_route "$USERAPI_INT" "DELETE /recordings/{key+}"
ensure_route "$USERAPI_INT" "GET /trash"
ensure_route "$USERAPI_INT" "POST /recordings/restore/{key+}"
ensure_route "$USERAPI_INT" "DELETE /recordings/permanent/{key+}"

# -------------------------------------------------------------
# 3. Verify userApi can actually delete from the bucket.
#
# PERMANENT delete is useless-but-silent without s3:DeleteObject: the row would
# vanish while the audio stayed behind, and the failure would only ever appear
# as a "[delete] audio ... not removed" line in CloudWatch. Checked here so a
# missing permission surfaces at deploy time instead of in a log nobody reads.
# (Soft delete and restore need no S3 access at all — they are update_item.)
# -------------------------------------------------------------
USERAPI_ROLE="$(aws lambda get-function --function-name "$USERAPI_LAMBDA_NAME" \
                  --query 'Configuration.Role' --output text)"
USERAPI_ROLE_ARN="$USERAPI_ROLE"
USERAPI_ROLE="${USERAPI_ROLE##*/}"
echo ">> userApi role: $USERAPI_ROLE"

if [[ -n "${BUCKET_NAME:-}" ]]; then
  DECISION="$(aws iam simulate-principal-policy \
      --policy-source-arn "$USERAPI_ROLE_ARN" \
      --action-names "s3:DeleteObject" \
      --resource-arns "arn:aws:s3:::${BUCKET_NAME}/recordings/probe.wav" \
      --query 'EvaluationResults[0].EvalDecision' --output text 2>/dev/null || echo "unknown")"
  case "$DECISION" in
    allowed)
      echo ">> s3:DeleteObject on ${BUCKET_NAME}: allowed." ;;
    unknown)
      echo ">> WARNING: couldn't simulate the policy (needs iam:SimulatePrincipalPolicy)."
      echo "   Verify by hand that $USERAPI_ROLE has s3:DeleteObject on ${BUCKET_NAME}." ;;
    *)
      echo "ERROR: $USERAPI_ROLE is DENIED s3:DeleteObject on ${BUCKET_NAME}." >&2
      echo "       The row would be deleted while the audio stayed behind." >&2
      echo "       Grant it, then re-run this script." >&2
      exit 1 ;;
  esac
else
  echo ">> BUCKET_NAME unset — skipping the s3:DeleteObject check."
fi

echo
echo ">> Done. Verify with:"
echo "     python -m pytest tests/test_ai_workspace.py -k \"Trash or SoftDelete or Restore or Permanent\""
echo ">> Then in the app:"
echo "     * open a meeting -> ⋯ -> Move to Trash (nothing is destroyed)"
echo "     * or long-press any brief on MinuteX"
echo "     * a failed brief also shows Move to Trash inline, on the card"
echo "     * Settings -> Privacy & data -> Trash: Restore / Delete permanently"
echo "   Only 'Delete permanently' removes data. A recording still uploading"
echo "   is refused there with 409 by design; trashing it is allowed."
