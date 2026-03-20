#!/usr/bin/env bash
#
# Tear down the DLQ Processor Lambda and associated resources.
# S3 bucket and SQS queue are preserved (shared with other Lambdas).
#
set -euo pipefail

AWS_PROFILE="${AWS_PROFILE:-ieee-cc}"
AWS_REGION="${AWS_REGION:-us-east-1}"

ECR_REPO_NAME="ieee-rc-dlq-processor"
LAMBDA_FUNCTION_NAME="ieee-rc-dlq-processor"
LAMBDA_ROLE_NAME="ieee-rc-dlq-processor-role"

export AWS_PROFILE AWS_REGION

log() { echo "==> $*"; }

log "Deleting Lambda function: ${LAMBDA_FUNCTION_NAME}"
aws lambda delete-function \
    --function-name "${LAMBDA_FUNCTION_NAME}" \
    --region "${AWS_REGION}" 2>/dev/null || log "Lambda not found — skipping."

log "Detaching managed policies from role: ${LAMBDA_ROLE_NAME}"
aws iam detach-role-policy \
    --role-name "${LAMBDA_ROLE_NAME}" \
    --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole" 2>/dev/null || true

log "Deleting inline policy from role"
aws iam delete-role-policy \
    --role-name "${LAMBDA_ROLE_NAME}" \
    --policy-name "DLQProcessorAccess" 2>/dev/null || true

log "Deleting IAM role: ${LAMBDA_ROLE_NAME}"
aws iam delete-role --role-name "${LAMBDA_ROLE_NAME}" 2>/dev/null || log "Role not found — skipping."

log "Deleting ECR repository: ${ECR_REPO_NAME}"
aws ecr delete-repository \
    --repository-name "${ECR_REPO_NAME}" \
    --region "${AWS_REGION}" \
    --force 2>/dev/null || log "ECR repo not found — skipping."

log "Teardown complete. S3 bucket and SQS queue preserved."
