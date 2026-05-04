#!/usr/bin/env bash
#
# Tear down all AWS resources created by deploy-wizard-transfer.sh.
#
# Removes our notification entry from the bucket notification configuration
# (preserving any other triggers — e.g. the image-generator's actions/*.json
# trigger). Does NOT delete the shared trigger bucket or the SQS DLQ.
#
# Usage:
#   ./scripts/teardown-wizard-transfer.sh
#
set -euo pipefail

AWS_PROFILE="${AWS_PROFILE:-ieee-cc}"
AWS_REGION="${AWS_REGION:-us-east-1}"
AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

ECR_REPO_NAME="ieee-rc-wizard-transfer"
LAMBDA_FUNCTION_NAME="ieee-rc-wizard-transfer"
LAMBDA_ROLE_NAME="ieee-rc-wizard-transfer-role"
TRIGGER_BUCKET_NAME="${TRIGGER_BUCKET_NAME:-dev-ieee-conference-cloud-bulk-uploads}"
NOTIFICATION_ID="wizard-transfer-trigger"

export AWS_PROFILE AWS_REGION

log() { echo "==> $*"; }

# 1. Strip our entry from the bucket notification configuration
log "Removing notification entry '${NOTIFICATION_ID}' from ${TRIGGER_BUCKET_NAME}"
existing=$(aws s3api get-bucket-notification-configuration \
    --bucket "${TRIGGER_BUCKET_NAME}" 2>/dev/null || echo '{}')

merged=$(NOTIFICATION_ID="${NOTIFICATION_ID}" EXISTING="${existing}" python3 <<'PY'
import json, os
existing = json.loads(os.environ["EXISTING"] or "{}")
notification_id = os.environ["NOTIFICATION_ID"]
configs = existing.get("LambdaFunctionConfigurations", [])
existing["LambdaFunctionConfigurations"] = [
    c for c in configs if c.get("Id") != notification_id
]
# AWS rejects empty arrays — strip them
for k in ("LambdaFunctionConfigurations", "QueueConfigurations",
          "TopicConfigurations", "EventBridgeConfiguration"):
    if k in existing and not existing[k]:
        del existing[k]
print(json.dumps(existing))
PY
)

aws s3api put-bucket-notification-configuration \
    --bucket "${TRIGGER_BUCKET_NAME}" \
    --notification-configuration "${merged}" \
    || log "Failed to update bucket notification — continuing teardown."

# 2. Delete Lambda
log "Deleting Lambda function: ${LAMBDA_FUNCTION_NAME}"
aws lambda delete-function \
    --function-name "${LAMBDA_FUNCTION_NAME}" \
    --region "${AWS_REGION}" 2>/dev/null || log "Lambda not found — skipping."

# 3. IAM cleanup
log "Cleaning up IAM role: ${LAMBDA_ROLE_NAME}"
aws iam delete-role-policy \
    --role-name "${LAMBDA_ROLE_NAME}" \
    --policy-name "WizardTransferAccess" 2>/dev/null || true
aws iam detach-role-policy \
    --role-name "${LAMBDA_ROLE_NAME}" \
    --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole" 2>/dev/null || true
aws iam delete-role --role-name "${LAMBDA_ROLE_NAME}" 2>/dev/null || log "Role not found — skipping."

# 4. ECR (force deletes images)
log "Deleting ECR repository: ${ECR_REPO_NAME}"
aws ecr delete-repository \
    --repository-name "${ECR_REPO_NAME}" \
    --region "${AWS_REGION}" \
    --force 2>/dev/null || log "ECR repo not found — skipping."

log "Teardown complete."
log "Note: SQS DLQ '${DLQ_QUEUE_NAME:-ieee-rc-processing-dlq}' is shared and was not removed."
