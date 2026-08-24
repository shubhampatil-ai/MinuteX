#!/usr/bin/env bash
# =============================================================
# teardown.sh — remove everything this project created.
#
# Removes: API Gateway, Lambda function, IAM role + inline policy,
#          DynamoDB table.
#
# NEVER touches the S3 bucket or its contents. The bucket is yours
# and pre-existing; this script only deletes resources the deploy
# scripts created.
#
# Idempotent: missing resources are skipped with a note.
# Prompts once for confirmation (skip with:  teardown.sh --yes).
# =============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./aws.sh
source "$SCRIPT_DIR/aws.sh"

ASSUME_YES="no"
[[ "${1:-}" == "--yes" || "${1:-}" == "-y" ]] && ASSUME_YES="yes"

echo "This will DELETE (region $AWS_REGION):"
echo "   - API Gateway:  $API_NAME"
echo "   - Lambda:       $LAMBDA_NAME"
echo "   - IAM role:     $ROLE_NAME (+ inline policy)"
echo "   - DynamoDB:     $TABLE_NAME"
echo "It will NOT touch the S3 bucket '$BUCKET_NAME' or its contents."
echo ""

if [[ "$ASSUME_YES" != "yes" ]]; then
  read -r -p "Type 'yes' to proceed: " ans
  [[ "$ans" == "yes" ]] || { echo "Aborted."; exit 1; }
fi

# -------------------------------------------------------------
# 1. API Gateway
# -------------------------------------------------------------
API_ID="$(aws apigatewayv2 get-apis \
            --query "Items[?Name=='${API_NAME}'].ApiId | [0]" \
            --output text 2>/dev/null || echo "None")"
if [[ "$API_ID" != "None" && -n "$API_ID" ]]; then
  echo ">> Deleting API '$API_NAME' ($API_ID) ..."
  aws apigatewayv2 delete-api --api-id "$API_ID" || true
else
  echo ">> API '$API_NAME' not found — skipping."
fi

# -------------------------------------------------------------
# 2. Lambda
# -------------------------------------------------------------
if aws lambda get-function --function-name "$LAMBDA_NAME" >/dev/null 2>&1; then
  echo ">> Deleting Lambda '$LAMBDA_NAME' ..."
  aws lambda delete-function --function-name "$LAMBDA_NAME" || true
else
  echo ">> Lambda '$LAMBDA_NAME' not found — skipping."
fi

# -------------------------------------------------------------
# 3. IAM role + inline policy
# -------------------------------------------------------------
if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  echo ">> Removing inline policies from role '$ROLE_NAME' ..."
  for pol in $(aws iam list-role-policies --role-name "$ROLE_NAME" \
                 --query 'PolicyNames[]' --output text 2>/dev/null); do
    aws iam delete-role-policy --role-name "$ROLE_NAME" --policy-name "$pol" || true
  done
  # Detach any managed policies too (defensive; deploy uses inline only).
  for arn in $(aws iam list-attached-role-policies --role-name "$ROLE_NAME" \
                 --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null); do
    aws iam detach-role-policy --role-name "$ROLE_NAME" --policy-arn "$arn" || true
  done
  echo ">> Deleting role '$ROLE_NAME' ..."
  aws iam delete-role --role-name "$ROLE_NAME" || true
else
  echo ">> Role '$ROLE_NAME' not found — skipping."
fi

# -------------------------------------------------------------
# 4. DynamoDB table
# -------------------------------------------------------------
if aws dynamodb describe-table --table-name "$TABLE_NAME" >/dev/null 2>&1; then
  echo ">> Deleting DynamoDB table '$TABLE_NAME' ..."
  aws dynamodb delete-table --table-name "$TABLE_NAME" >/dev/null || true
  echo ">> Waiting for table deletion ..."
  aws dynamodb wait table-not-exists --table-name "$TABLE_NAME" || true
else
  echo ">> Table '$TABLE_NAME' not found — skipping."
fi

echo ""
echo ">> Teardown complete. S3 bucket '$BUCKET_NAME' left untouched."
