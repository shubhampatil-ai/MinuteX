#!/usr/bin/env bash
# =============================================================
# 29_rotate_elevenlabs_key.sh — swap the ElevenLabs API key safely.
#
# THE ONE THING THAT BREAKS THIS
# ------------------------------
# An ElevenLabs webhook belongs to a WORKSPACE, not to a key. ElevenLabs only
# delivers a transcription webhook for jobs submitted with a key from the SAME
# workspace that owns the webhook.
#
# So a key rotation has two very different outcomes:
#
#   SAME workspace      -> the existing webhook + signing secret stay valid.
#                          Swapping the key is all that is needed.
#   DIFFERENT workspace -> the new key cannot see our webhook. Async STT stops
#                          working and silently falls back to the synchronous
#                          path (which reintroduces the 290s timeout risk on
#                          long recordings). A new webhook must be registered in
#                          the new workspace and its signing secret stored.
#
# This script detects which case you are in BEFORE changing anything, by asking
# the new key to list the workspace's webhooks and checking whether our endpoint
# is among them. That is the only reliable test available: a key with just
# speech-to-text + webhooks_read cannot read a workspace id directly.
#
# USAGE
#   bash scripts/29_rotate_elevenlabs_key.sh --check          # verify only
#   bash scripts/29_rotate_elevenlabs_key.sh --apply          # verify + swap
#   bash scripts/29_rotate_elevenlabs_key.sh --rollback       # restore previous
#
# The key is read from the ELEVENLABS_NEW_KEY environment variable, never from
# the command line, so it does not land in shell history:
#   read -s -p "new key: " ELEVENLABS_NEW_KEY; export ELEVENLABS_NEW_KEY
#
# Never prints a key or a secret. Only lengths, fingerprints and HTTP statuses.
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

TRANSCRIBE_LAMBDA_NAME="${TRANSCRIBE_LAMBDA_NAME:-transcribeRecording}"
USERAPI_LAMBDA_NAME="${USERAPI_LAMBDA_NAME:-userApi}"
BACKUP_DIR="${BACKUP_DIR:-$PROJECT_ROOT/.keybackup}"
MERGE_ENV_PY="$SCRIPT_DIR/_merge_env.py"

MODE="${1:---check}"

API_ID="$(aws apigatewayv2 get-apis \
            --query "Items[?Name=='${API_NAME}'].ApiId | [0]" --output text)"
API_ENDPOINT="$(aws apigatewayv2 get-api --api-id "$API_ID" \
                  --query ApiEndpoint --output text)"
WEBHOOK_URL="${API_ENDPOINT}/webhooks/elevenlabs/stt"

fingerprint() {
  # A short, non-reversible identifier so two keys can be compared in a log
  # without either one being printed.
  printf '%s' "$1" | sha256sum | cut -c1-12
}

# -------------------------------------------------------------
# Rollback: restore the key saved by the last --apply.
# -------------------------------------------------------------
if [[ "$MODE" == "--rollback" ]]; then
  BK="$BACKUP_DIR/elevenlabs_key.prev"
  if [[ ! -f "$BK" ]]; then
    echo "ERROR: no backup at $BK - nothing to roll back to." >&2
    exit 1
  fi
  PREV="$(cat "$BK")"
  echo ">> restoring key fingerprint $(fingerprint "$PREV")"
  for fn in "$TRANSCRIBE_LAMBDA_NAME" "$USERAPI_LAMBDA_NAME"; do
    CUR="$(aws lambda get-function-configuration --function-name "$fn" \
             --query 'Environment.Variables' --output json)"
    MERGED="$(python "$MERGE_ENV_PY" "$CUR" "ELEVENLABS_API_KEY=$PREV")"
    aws lambda update-function-configuration --function-name "$fn" \
      --environment "$MERGED" --query FunctionName --output text >/dev/null
    aws lambda wait function-updated-v2 --function-name "$fn"
    echo ">> $fn restored"
  done
  echo ">> done. NOTE: if the webhook/secret were also changed, restore those too."
  exit 0
fi

: "${ELEVENLABS_NEW_KEY:?ELEVENLABS_NEW_KEY not set. Use: read -s -p 'key: ' ELEVENLABS_NEW_KEY; export ELEVENLABS_NEW_KEY}"
NEW_KEY="$ELEVENLABS_NEW_KEY"

echo ">> MinuteX webhook endpoint:"
echo "   $WEBHOOK_URL"
echo ">> new key fingerprint: $(fingerprint "$NEW_KEY") (${#NEW_KEY} chars)"
echo

# -------------------------------------------------------------
# 1. Does the new key work at all, and can it do speech-to-text?
#
#    STT is probed for real, because that is the permission the pipeline
#    actually depends on and the only way to be sure is to use it. A tiny
#    public sample keeps the cost negligible.
# -------------------------------------------------------------
echo ">> === permission checks ==="
WH_LIST="$(curl -s -w '\n%{http_code}' -H "xi-api-key: $NEW_KEY" \
             https://api.elevenlabs.io/v1/workspace/webhooks)"
WH_CODE="$(tail -1 <<<"$WH_LIST")"
WH_BODY="$(sed '$d' <<<"$WH_LIST")"
echo "   webhooks_read : HTTP $WH_CODE"

WR_CODE="$(curl -s -o /dev/null -w '%{http_code}' -X POST \
             https://api.elevenlabs.io/v1/workspace/webhooks \
             -H "xi-api-key: $NEW_KEY" -H 'Content-Type: application/json' \
             -d '{}')"
# 422 means the permission passed and only the (deliberately empty) body was
# rejected. 401 means the permission is missing.
if [[ "$WR_CODE" == "401" ]]; then
  echo "   webhooks_write: MISSING (fine if webhooks are registered by hand)"
else
  echo "   webhooks_write: present (HTTP $WR_CODE on an empty body)"
fi

STT_BODY="$(curl -s -X POST https://api.elevenlabs.io/v1/speech-to-text \
    -H "xi-api-key: $NEW_KEY" \
    -F "model_id=${ELEVENLABS_MODEL:-scribe_v2}" \
    -F "source_url=https://github.com/mozilla/DeepSpeech/raw/master/data/smoke_test/LDC93S1.wav" \
    -F "diarize=true")"
if grep -q '"detail"' <<<"$STT_BODY"; then
  echo "   speech_to_text: FAILED"
  python - "$STT_BODY" <<'PY'
import json, sys
try:
    d = json.loads(sys.argv[1])
    det = d.get("detail")
    det = det if isinstance(det, dict) else {"message": str(det)}
    print(f"      code   : {det.get('code') or det.get('status')}")
    print(f"      message: {str(det.get('message'))[:160]}")
except Exception:
    print(f"      raw: {sys.argv[1][:160]}")
PY
  echo
  echo "ERROR: the new key cannot run speech-to-text. Nothing was changed." >&2
  echo "       Grant the key the Speech to Text permission and re-run." >&2
  exit 1
fi
echo "   speech_to_text: OK"
echo

# -------------------------------------------------------------
# 2. THE DECIDING CHECK — is our webhook in this key's workspace?
# -------------------------------------------------------------
echo ">> === workspace check (the one that matters) ==="
SAME_WORKSPACE="$(python - "$WH_BODY" "$WEBHOOK_URL" <<'PY'
import json, sys
try:
    hooks = (json.loads(sys.argv[1]) or {}).get("webhooks") or []
except Exception:
    hooks = []
target = sys.argv[2]
match = [h for h in hooks if (h.get("webhook_url") or "") == target]
for h in hooks:
    mark = "  <-- MinuteX" if (h.get("webhook_url") or "") == target else ""
    print(f"   found: {h.get('webhook_url')} "
          f"[{h.get('auth_type')}, disabled={h.get('is_disabled')}]{mark}",
          file=sys.stderr)
if not hooks:
    print("   (this workspace has NO webhooks at all)", file=sys.stderr)
if match and not match[0].get("is_disabled"):
    print("yes")
elif match:
    print("disabled")
else:
    print("no")
PY
)"

case "$SAME_WORKSPACE" in
  yes)
    echo
    echo "   SAME WORKSPACE. The existing webhook and its stored signing secret"
    echo "   remain valid, so swapping the key is all that is required."
    ;;
  disabled)
    echo
    echo "ERROR: our endpoint exists in this workspace but is DISABLED." >&2
    echo "       Re-enable it in the ElevenLabs dashboard, then re-run." >&2
    exit 1
    ;;
  *)
    echo
    echo "   DIFFERENT WORKSPACE - our endpoint is not registered here."
    echo
    echo "   Swapping the key now would stop async STT: ElevenLabs would refuse"
    echo "   webhook=true and the pipeline would fall back to the synchronous"
    echo "   path, which reintroduces the ~290s timeout risk on long recordings."
    echo
    echo "   Before rotating, register the endpoint in THIS workspace:"
    echo "     Developers -> Webhooks -> Add endpoint"
    echo "       URL   : $WEBHOOK_URL"
    echo "       Event : Transcription completed"
    echo "       HMAC  : Enabled"
    echo "   Copy the signing secret it shows ONCE, then store it:"
    echo "     aws secretsmanager put-secret-value \\"
    echo "       --secret-id userApi/elevenlabsWebhookSecret \\"
    echo "       --region $AWS_REGION --secret-string 'THE_SIGNING_SECRET'"
    echo "   Then re-run this script."
    echo
    if [[ "$MODE" == "--apply" ]]; then
      echo "ERROR: refusing to swap the key into a workspace with no webhook." >&2
      echo "       Re-run with --check once the webhook is registered, or set" >&2
      echo "       ALLOW_NO_WEBHOOK=1 to swap anyway and accept the fallback." >&2
      [[ "${ALLOW_NO_WEBHOOK:-0}" == "1" ]] || exit 1
      echo "   ALLOW_NO_WEBHOOK=1 set - proceeding with the sync fallback."
    fi
    ;;
esac

if [[ "$MODE" != "--apply" ]]; then
  echo
  echo ">> --check only. Nothing was changed. Re-run with --apply to swap."
  exit 0
fi

# -------------------------------------------------------------
# 3. Swap, after backing up the current key for one-command rollback.
# -------------------------------------------------------------
echo
echo ">> === applying ==="
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR" 2>/dev/null || true
CURRENT_KEY="$(aws lambda get-function-configuration \
                 --function-name "$TRANSCRIBE_LAMBDA_NAME" \
                 --query 'Environment.Variables.ELEVENLABS_API_KEY' \
                 --output text)"
if [[ -n "$CURRENT_KEY" && "$CURRENT_KEY" != "None" ]]; then
  printf '%s' "$CURRENT_KEY" > "$BACKUP_DIR/elevenlabs_key.prev"
  chmod 600 "$BACKUP_DIR/elevenlabs_key.prev" 2>/dev/null || true
  echo "   previous key backed up (fingerprint $(fingerprint "$CURRENT_KEY"))"
fi

# BOTH functions: transcribeRecording submits the STT jobs, and userApi uses the
# key for /recordings/ai/stt-reconcile. Leaving one on the old key would make
# reconciliation query a workspace that knows nothing about our jobs.
for fn in "$TRANSCRIBE_LAMBDA_NAME" "$USERAPI_LAMBDA_NAME"; do
  CUR="$(aws lambda get-function-configuration --function-name "$fn" \
           --query 'Environment.Variables' --output json)"
  MERGED="$(python "$MERGE_ENV_PY" "$CUR" "ELEVENLABS_API_KEY=$NEW_KEY")"
  aws lambda update-function-configuration --function-name "$fn" \
    --environment "$MERGED" --query FunctionName --output text >/dev/null
  aws lambda wait function-updated-v2 --function-name "$fn"
  echo "   $fn updated"
done

echo
echo ">> verifying the deployed value matches the new key..."
for fn in "$TRANSCRIBE_LAMBDA_NAME" "$USERAPI_LAMBDA_NAME"; do
  LIVE="$(aws lambda get-function-configuration --function-name "$fn" \
            --query 'Environment.Variables.ELEVENLABS_API_KEY' --output text)"
  if [[ "$(fingerprint "$LIVE")" == "$(fingerprint "$NEW_KEY")" ]]; then
    echo "   $fn: MATCH"
  else
    echo "   $fn: MISMATCH - investigate before relying on this" >&2
  fi
done

echo
echo ">> Done. Rotation checklist:"
echo "   1. Revoke the OLD key in the ElevenLabs dashboard."
echo "   2. Upload a short recording and confirm the log shows"
echo "      'queued: request_id=...' rather than 'no webhook configured':"
echo "        aws logs tail /aws/lambda/$TRANSCRIBE_LAMBDA_NAME --region $AWS_REGION --since 5m"
echo "   3. Roll back if needed:  bash scripts/29_rotate_elevenlabs_key.sh --rollback"
