#!/usr/bin/env bash
# =============================================================
# 35_deploy_avatars.sh — profile photos and contact photos on userApi.
#
# Ships ONE new route:
#
#   POST /avatars/upload-request  {format, scope?, contact_id?, size?}
#                                -> {upload_url, key, expires_in, content_type}
#
# and changes the behaviour of four that already exist:
#
#   GET  /me                  -> user now carries avatar_view_url
#   PATCH /me                 -> avatar_url is validated + the old object deleted
#   GET/POST/PATCH /contacts  -> contacts carry avatar_view_url + avatar_source
#
# No new route keys for those four, so no coordinated app release is needed:
# every field the old API returned still means the same thing, and the new
# fields are additive.
#
# WHAT THIS IS FOR
# ----------------
# A person had no face anywhere in the product. Every avatar was coloured
# initials, which is fine as a fallback and poor as the only option — a list of
# twenty contacts reads as twenty coloured discs. Three sources of a real photo
# now exist:
#
#   1. The user picks one for themselves (profile) or for a contact.
#   2. A phone-contact import carries the device address-book photo.
#   3. A contact who is ALSO a MinuteX user shows their own profile photo,
#      which they maintain, so it updates itself.
#
# THE STORAGE DECISION, because it drives everything below: an avatar is stored
# as an S3 KEY, never a URL. Objects stay private and the API hands out a
# short-lived presigned GET on every read (avatar_view_url). A stored URL would
# expire in the database; re-signing needs the key anyway.
#
# NO NEW TABLE and NO NEW BUCKET. The key lives on the existing Users /
# Contacts rows (attribute `avatar_url`, already present on Users since
# signup), and the object lives in the existing recordings bucket under a new
# `avatars/` prefix.
#
# THE TWO THINGS THAT CAN GO SILENTLY WRONG, both checked below:
#
#   IAM. The userApi role holds s3:PutObject/GetObject scoped to recordings/*
#   and transcripts/* (scripts 18, 19, 28, 30). NOTHING grants avatars/*. A
#   presigned URL is a local signing operation — it is produced happily and
#   then REJECTED by S3 at upload time, so the symptom is "choosing a photo
#   does nothing" with a 403 that never reaches a log the app can show. Step 2
#   attaches the prefix and step 5 proves it with a policy simulation.
#
#   THE S3 TRIGGER. ObjectCreated on this bucket starts transcription. It is
#   filtered by SUFFIX — .wav plus the other audio extensions (script 18) — so
#   an image cannot match it, and AVATAR_FORMATS deliberately excludes wav from
#   the other direction. Step 4 re-asserts that, because a rule widened to a
#   bare prefix later would feed every uploaded selfie into the STT pipeline
#   and bill for it.
#
# Idempotent: update-function-code / put-role-policy / ensure_route all
# overwrite or skip.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"
# Ceiling and view-URL lifetime. Defaults match the Lambda's own, so leaving
# them unset is the same as not setting them at all.
MAX_AVATAR_BYTES="${MAX_AVATAR_BYTES:-8388608}"      # 8 MiB
AVATAR_URL_EXPIRY="${AVATAR_URL_EXPIRY:-21600}"      # 6h

: "${BUCKET_NAME:?BUCKET_NAME not set (put it in .env)}"

API_ID="$(aws apigatewayv2 get-apis \
            --query "Items[?Name=='${API_NAME}'].ApiId | [0]" \
            --output text)"
if [[ "$API_ID" == "None" || -z "$API_ID" ]]; then
  echo "ERROR: HTTP API '$API_NAME' not found — run 03_create_apigateway.sh first." >&2
  exit 1
fi
echo ">> API: $API_NAME ($API_ID)"
echo ">> Bucket: $BUCKET_NAME"

USERAPI_ROLE_ARN="$(aws lambda get-function --function-name "$USERAPI_LAMBDA_NAME" \
                      --query 'Configuration.Role' --output text)"
USERAPI_ROLE_NAME="${USERAPI_ROLE_ARN##*/}"
echo ">> userApi role: $USERAPI_ROLE_NAME"

# -------------------------------------------------------------
# 1. Env: the two tuning knobs.
#
# Merged into the EXISTING environment — update-function-configuration
# --environment REPLACES the whole map, so writing only these two would wipe
# GROQ_API_KEY, JWT_SECRET_ARN, the table names and everything else.
# scripts/_merge_env.py exists for exactly this.
# -------------------------------------------------------------
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
  "MAX_AVATAR_BYTES=$MAX_AVATAR_BYTES" \
  "AVATAR_URL_EXPIRY=$AVATAR_URL_EXPIRY"

# -------------------------------------------------------------
# 2. IAM — the avatars/ prefix.
#
# THE LOAD-BEARING STEP of this script. Three actions, and each is needed by a
# specific code path:
#
#   s3:PutObject     the presigned PUT the app uploads through
#                    (request_avatar_upload)
#   s3:GetObject     the presigned GET every read hands back
#                    (_avatar_view_url) — note that presigning GET requires the
#                    ROLE to hold GetObject, since a presigned URL carries the
#                    signer's own authority and nothing more
#   s3:DeleteObject  replacing or clearing a photo, and deleting a contact
#                    (_delete_avatar_object) — without it a user who changes
#                    their picture ten times pays to store ten
#
# Scoped to avatars/* alone. A broader grant would let a bug in the avatar path
# touch recordings, which is the one thing in this bucket that cannot be
# regenerated.
#
# A SEPARATE inline policy rather than an edit to userApi-upload-presign:
# put-role-policy replaces a policy document wholesale, so folding avatars into
# that one means script 18 and this script each clobber the other's grants
# depending on run order.
# -------------------------------------------------------------
AV_POLICY_FILE="$(mktemp)"
trap 'rm -f "$AV_POLICY_FILE"' EXIT
cat > "$AV_POLICY_FILE" <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AvatarObjects",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::${BUCKET_NAME}/avatars/*"
    }
  ]
}
EOF
aws iam put-role-policy \
  --role-name "$USERAPI_ROLE_NAME" \
  --policy-name "userApi-avatars" \
  --policy-document "file://$(winpath "$AV_POLICY_FILE")"
echo ">> Attached inline policy userApi-avatars (Put/Get/Delete on avatars/*)."

# -------------------------------------------------------------
# 3. Deploy the code. Shared modules vendored FLAT — userApi imports
#    ai_schema/groq_client/prompts/spoken_dates/stt_result/transcript_store at
#    module scope, so a zip without them fails on cold start (same packaging as
#    scripts 21/33/34).
#
#    Only lambda_function.py changed in this release, but the shared modules
#    ship anyway: the zip REPLACES the function's whole code, so omitting them
#    would cold-start into ImportError.
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
# 4. Wire the route. An ordinary static path — no {key+}, so none of the
#    greedy-variable ordering constraints that shape /recordings/ai/* apply.
# -------------------------------------------------------------
USERAPI_INT="$(aws apigatewayv2 get-integrations --api-id "$API_ID" \
    --query "Items[?contains(IntegrationUri, '${USERAPI_LAMBDA_NAME}')].IntegrationId | [0]" \
    --output text)"
if [[ "$USERAPI_INT" == "None" || -z "$USERAPI_INT" ]]; then
  echo "ERROR: no API integration for $USERAPI_LAMBDA_NAME — run 17 first." >&2
  exit 1
fi

# Filter by CONTENT, not by line position — see script 33 for why: this command
# emits the RouteId plus a stray "None" line in EITHER order, and a head -1
# that grabs the "None" makes an existing route look absent.
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

ensure_route "$USERAPI_INT" "POST /avatars/upload-request"

# -------------------------------------------------------------
# 5. Verify the role can actually reach the avatars/ prefix.
#
# The failure this catches is quiet and expensive to diagnose: presigning is a
# local signing operation that succeeds regardless of permissions, so a missing
# grant produces a perfectly well-formed URL that S3 answers 403 to. From the
# app that looks like "the photo just doesn't save".
#
# GetObject is simulated too, not only PutObject, because they fail in
# DIFFERENT and equally confusing ways: without PutObject nothing uploads;
# without GetObject the upload works and every avatar renders as initials
# forever, which reads as a UI bug rather than a permissions one.
# -------------------------------------------------------------
AVATAR_ARN="arn:aws:s3:::${BUCKET_NAME}/avatars/probe.jpg"
for action in s3:PutObject s3:GetObject s3:DeleteObject; do
  DECISION="$(aws iam simulate-principal-policy \
      --policy-source-arn "$USERAPI_ROLE_ARN" \
      --action-names "$action" \
      --resource-arns "$AVATAR_ARN" \
      --query 'EvaluationResults[0].EvalDecision' --output text 2>/dev/null \
      || echo "unknown")"
  case "$DECISION" in
    allowed)
      echo ">> $action on avatars/*: allowed." ;;
    unknown)
      echo ">> WARNING: couldn't simulate $action (needs iam:SimulatePrincipalPolicy)."
      echo "   Verify by hand that $USERAPI_ROLE_NAME can $action on avatars/*." ;;
    *)
      echo "ERROR: $USERAPI_ROLE_NAME is DENIED $action on avatars/*." >&2
      echo "       Photos would fail silently — a presigned URL is signed" >&2
      echo "       locally and only rejected by S3 at transfer time." >&2
      exit 1 ;;
  esac
done

# -------------------------------------------------------------
# 6. Re-assert that the transcription trigger is SUFFIX-filtered.
#
# An avatar landing in this bucket must never start a transcription. Today it
# cannot: every ObjectCreated rule carries an audio suffix filter (script 18),
# and AVATAR_FORMATS excludes wav. This prints the rules so a future change to
# a bare prefix filter is caught here rather than on an STT invoice.
# -------------------------------------------------------------
echo ">> S3 ObjectCreated filters on $BUCKET_NAME:"
UNFILTERED="$(aws s3api get-bucket-notification-configuration \
    --bucket "$BUCKET_NAME" \
    --query 'LambdaFunctionConfigurations[?!not_null(Filter)] | length(@)' \
    --output text 2>/dev/null || echo "unknown")"
aws s3api get-bucket-notification-configuration --bucket "$BUCKET_NAME" \
    --query 'LambdaFunctionConfigurations[].Filter.Key.FilterRules[]' \
    --output text 2>/dev/null || echo "   (could not read notification config)"
if [[ "$UNFILTERED" != "0" && "$UNFILTERED" != "unknown" ]]; then
  echo ">> WARNING: $UNFILTERED ObjectCreated rule(s) have NO key filter."
  echo "   An uploaded image would be handed to the transcription pipeline."
  echo "   Re-run scripts/18_wire_upload_routes.sh to restore suffix filters."
fi

echo
echo ">> Done. Verify with:"
echo "     python -m pytest tests/test_avatars.py -q        # 58 offline unit tests"
echo "     python -m pytest tests/test_workspace_org.py -q  # contacts API unchanged"
echo "     python -m pytest tests/ -q                       # the whole suite"
echo
echo ">> Live smoke test (needs a token from POST /login):"
echo "     # 1. Presign"
echo "     curl -sS -X POST \"\$API_URL/avatars/upload-request\" \\"
echo "       -H \"Authorization: Bearer \$TOKEN\" -H 'Content-Type: application/json' \\"
echo "       -d '{\"format\":\"jpg\"}'"
echo "     # 2. PUT the bytes to .upload_url (no Content-Type header — it is"
echo "     #    deliberately unsigned), then store the key:"
echo "     curl -sS -X PATCH \"\$API_URL/me\" \\"
echo "       -H \"Authorization: Bearer \$TOKEN\" -H 'Content-Type: application/json' \\"
echo "       -d '{\"avatar_url\":\"<key from step 1>\"}'"
echo "     # 3. GET /me should now carry a non-empty avatar_view_url."
echo
echo ">> Ownership check — a key from another account must be REFUSED:"
echo "     PATCH /me with {\"avatar_url\":\"avatars/<someone-else>/user/x.jpg\"}"
echo "     answers 400. The server presigns reads of whatever it stores, so"
echo "     this is what stops one account reading another's image."
echo
echo ">> APP SIDE: expo-image-picker was added as a NEW NATIVE DEPENDENCY."
echo "   Choosing a photo cannot work over a JS reload — commit app.json and"
echo "   package.json, then rebuild the dev client:"
echo "     eas build --profile development --platform android"
