#!/usr/bin/env bash
#
# Tear down all AWS resources created by deploy-pptx-extractor.sh.
# S3 bucket is NOT deleted (shared + data retention).
#
# Usage:
#   ./scripts/teardown-pptx-extractor.sh
#
set -euo pipefail

AWS_PROFILE="${AWS_PROFILE:-ieee-cc}"
AWS_REGION="${AWS_REGION:-us-east-1}"
AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-141770997341}"

ECR_REPO_NAME="ieee-rc-pptx-extractor"
LAMBDA_FUNCTION_NAME="ieee-rc-pptx-extractor"
LAMBDA_ROLE_NAME="ieee-rc-pptx-extractor-role"

export AWS_PROFILE AWS_REGION

log() { echo "==> $*"; }

log "Deleting Lambda function: ${LAMBDA_FUNCTION_NAME}"
aws lambda delete-function \
    --function-name "${LAMBDA_FUNCTION_NAME}" \
    --region "${AWS_REGION}" 2>/dev/null || log "Lambda not found — skipping."

log "Cleaning up IAM role: ${LAMBDA_ROLE_NAME}"
aws iam delete-role-policy \
    --role-name "${LAMBDA_ROLE_NAME}" \
    --policy-name "S3CloudWatchAccess" 2>/dev/null || true
aws iam detach-role-policy \
    --role-name "${LAMBDA_ROLE_NAME}" \
    --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole" 2>/dev/null || true
aws iam delete-role --role-name "${LAMBDA_ROLE_NAME}" 2>/dev/null || log "Role not found — skipping."

log "Deleting ECR repository: ${ECR_REPO_NAME}"
aws ecr delete-repository \
    --repository-name "${ECR_REPO_NAME}" \
    --region "${AWS_REGION}" \
    --force 2>/dev/null || log "ECR repo not found — skipping."

log "Teardown complete. S3 bucket was intentionally preserved."
