#!/usr/bin/env bash
#
# Tear down Bedrock Metadata Generation Lambda resources.
# Deletes: Lambda function, IAM role + policies, ECR repository.
#
# Usage:
#   ./scripts/teardown-bedrock.sh
#
set -euo pipefail

AWS_PROFILE="${AWS_PROFILE:-ieee-cc}"
AWS_REGION="${AWS_REGION:-us-east-1}"

ECR_REPO_NAME="ieee-cc-bedrock-inference"
LAMBDA_FUNCTION_NAME="ieee-cc-bedrock-inference"
LAMBDA_ROLE_NAME="ieee-cc-bedrock-inference-role"

export AWS_PROFILE AWS_REGION

log() { echo "==> $*"; }

# Delete Lambda function
log "Deleting Lambda: ${LAMBDA_FUNCTION_NAME}"
aws lambda delete-function \
    --function-name "${LAMBDA_FUNCTION_NAME}" \
    --region "${AWS_REGION}" 2>/dev/null \
|| log "Lambda not found — skipping."

# Detach managed policies and delete inline policies, then delete role
log "Deleting IAM role: ${LAMBDA_ROLE_NAME}"
aws iam detach-role-policy \
    --role-name "${LAMBDA_ROLE_NAME}" \
    --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole" 2>/dev/null || true
aws iam delete-role-policy \
    --role-name "${LAMBDA_ROLE_NAME}" \
    --policy-name "S3AndBedrockAccess" 2>/dev/null || true
aws iam delete-role \
    --role-name "${LAMBDA_ROLE_NAME}" 2>/dev/null \
|| log "IAM role not found — skipping."

# Delete ECR repository
log "Deleting ECR repository: ${ECR_REPO_NAME}"
aws ecr delete-repository \
    --repository-name "${ECR_REPO_NAME}" \
    --region "${AWS_REGION}" \
    --force 2>/dev/null \
|| log "ECR repo not found — skipping."

log "Teardown complete."
