#!/usr/bin/env bash
#
# Deploy the Wizard Async Transfer Lambda (CC3-898) using AWS CLI + Docker.
#
# Lambda code lives at src/transfer/. Triggers on s3:ObjectCreated:* for the
# transfer-actions/*.json prefix on the metadata-json bucket. Streams Drive
# or URL bytes into S3 via multipart upload and POSTs an HMAC-signed
# webhook callback to Drupal.
#
# Prerequisites:
#   - AWS CLI configured with profile "ieee-cc"
#   - Docker running locally
#   - Existing webhook signing secret available at iplr/webhook-secret in
#     Secrets Manager (shared with the AI orchestrator's webhook)
#   - Existing SNS topic for webhook failures (WEBHOOK_FAILURES_SNS_TOPIC_ARN)
#   - Existing SQS DLQ "ieee-rc-processing-dlq" for async invocation failures
#
# Drupal-side prerequisite (A4 — NOT done by this script):
#   The Drupal s3fs IAM role needs secretsmanager:CreateSecret/PutSecretValue/
#   DeleteSecret/DescribeSecret on arn:aws:secretsmanager:*:*:secret:iplr/
#   drive-tokens/* so DriveTokenVault::mintForDispatch() can write per-request
#   OAuth tokens. Without this, end-to-end smoke tests will fail.
#
# Usage:
#   ./scripts/deploy-wizard-transfer.sh                # first-time setup + deploy
#   ./scripts/deploy-wizard-transfer.sh update         # rebuild + update code only
#
set -euo pipefail

AWS_PROFILE="${AWS_PROFILE:-ieee-cc}"
AWS_REGION="${AWS_REGION:-us-east-1}"
AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

ECR_REPO_NAME="ieee-rc-wizard-transfer"
LAMBDA_FUNCTION_NAME="ieee-rc-wizard-transfer"
LAMBDA_ROLE_NAME="ieee-rc-wizard-transfer-role"
TRIGGER_BUCKET_NAME="${TRIGGER_BUCKET_NAME:-dev-ieee-conference-cloud-bulk-uploads}"
TRIGGER_PREFIX="transfer-actions/"
TRIGGER_SUFFIX=".json"
NOTIFICATION_ID="wizard-transfer-trigger"

DLQ_QUEUE_NAME="${DLQ_QUEUE_NAME:-ieee-rc-processing-dlq}"
DLQ_QUEUE_ARN="arn:aws:sqs:${AWS_REGION}:${AWS_ACCOUNT_ID}:${DLQ_QUEUE_NAME}"

# Optional — passed through to the Lambda environment if set.
WEBHOOK_FAILURES_SNS_TOPIC_ARN="${WEBHOOK_FAILURES_SNS_TOPIC_ARN:-}"
DRUPAL_WEBHOOK_SECRET="${DRUPAL_WEBHOOK_SECRET:-}"

IMAGE_TAG="latest"
ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO_NAME}"
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

export AWS_PROFILE AWS_REGION

# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------
log() { echo "==> $*"; }

ecr_login() {
    log "Logging in to ECR..."
    aws ecr get-login-password --region "${AWS_REGION}" \
        | docker login --username AWS --password-stdin \
          "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
}

build_and_push() {
    log "Building Docker image..."
    docker buildx build --platform linux/amd64 --provenance=false \
        --output type=docker \
        -f "${PROJECT_ROOT}/src/transfer/Dockerfile" \
        -t "${ECR_REPO_NAME}:${IMAGE_TAG}" "${PROJECT_ROOT}"

    log "Tagging image..."
    docker tag "${ECR_REPO_NAME}:${IMAGE_TAG}" "${ECR_URI}:${IMAGE_TAG}"

    ecr_login

    log "Pushing image to ECR..."
    docker push "${ECR_URI}:${IMAGE_TAG}"
}

# ---------------------------------------------------------------
# 1. Create ECR repository (idempotent)
# ---------------------------------------------------------------
create_ecr_repo() {
    log "Creating ECR repository: ${ECR_REPO_NAME}"
    aws ecr describe-repositories --repository-names "${ECR_REPO_NAME}" \
        --region "${AWS_REGION}" >/dev/null 2>&1 \
    || aws ecr create-repository \
        --repository-name "${ECR_REPO_NAME}" \
        --region "${AWS_REGION}" \
        --image-scanning-configuration scanOnPush=true \
        --encryption-configuration encryptionType=AES256
}

# ---------------------------------------------------------------
# 2. Create IAM role for Lambda (idempotent)
# ---------------------------------------------------------------
create_lambda_role() {
    log "Creating IAM role: ${LAMBDA_ROLE_NAME}"

    TRUST_POLICY='{
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole"
        }]
    }'

    INLINE_POLICY=$(cat <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadAndDeleteTrigger",
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::${TRIGGER_BUCKET_NAME}/${TRIGGER_PREFIX}*"
    },
    {
      "Sid": "ListTriggerBucket",
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::${TRIGGER_BUCKET_NAME}"
    },
    {
      "Sid": "WriteDestinationMultipart",
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:PutObjectAcl",
        "s3:AbortMultipartUpload",
        "s3:ListMultipartUploadParts"
      ],
      "Resource": "arn:aws:s3:::${TRIGGER_BUCKET_NAME}/*"
    },
    {
      "Sid": "ReadDriveTokens",
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": "arn:aws:secretsmanager:${AWS_REGION}:${AWS_ACCOUNT_ID}:secret:iplr/drive-tokens/*"
    },
    {
      "Sid": "ReadWebhookSecret",
      "Effect": "Allow",
      "Action": ["secretsmanager:GetSecretValue"],
      "Resource": "arn:aws:secretsmanager:${AWS_REGION}:${AWS_ACCOUNT_ID}:secret:iplr/webhook-secret*"
    },
    {
      "Sid": "PublishWebhookFailures",
      "Effect": "Allow",
      "Action": ["sns:Publish"],
      "Resource": "arn:aws:sns:${AWS_REGION}:${AWS_ACCOUNT_ID}:*"
    },
    {
      "Sid": "DLQSendMessage",
      "Effect": "Allow",
      "Action": ["sqs:SendMessage"],
      "Resource": "${DLQ_QUEUE_ARN}"
    }
  ]
}
EOF
)

    aws iam get-role --role-name "${LAMBDA_ROLE_NAME}" >/dev/null 2>&1 \
    || aws iam create-role \
        --role-name "${LAMBDA_ROLE_NAME}" \
        --assume-role-policy-document "${TRUST_POLICY}"

    aws iam attach-role-policy \
        --role-name "${LAMBDA_ROLE_NAME}" \
        --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole" 2>/dev/null || true

    aws iam put-role-policy \
        --role-name "${LAMBDA_ROLE_NAME}" \
        --policy-name "WizardTransferAccess" \
        --policy-document "${INLINE_POLICY}"

    ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${LAMBDA_ROLE_NAME}"
}

# ---------------------------------------------------------------
# 3. Create or update Lambda function
# ---------------------------------------------------------------
create_lambda() {
    log "Creating Lambda function: ${LAMBDA_FUNCTION_NAME}"
    ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${LAMBDA_ROLE_NAME}"

    # Build environment-variables map JSON (only include set vars).
    local env_vars="LOG_LEVEL=INFO"
    if [[ -n "${DRUPAL_WEBHOOK_SECRET}" ]]; then
        env_vars="${env_vars},DRUPAL_WEBHOOK_SECRET=${DRUPAL_WEBHOOK_SECRET}"
    fi
    if [[ -n "${WEBHOOK_FAILURES_SNS_TOPIC_ARN}" ]]; then
        env_vars="${env_vars},WEBHOOK_FAILURES_SNS_TOPIC_ARN=${WEBHOOK_FAILURES_SNS_TOPIC_ARN}"
    fi

    if aws lambda get-function --function-name "${LAMBDA_FUNCTION_NAME}" \
        --region "${AWS_REGION}" >/dev/null 2>&1; then
        log "Lambda already exists — updating code..."
        update_lambda_code
    else
        log "Waiting for IAM role propagation..."
        aws iam wait role-exists --role-name "${LAMBDA_ROLE_NAME}"

        # 1024 MB / 900 s — needed for 10 GB worst case within Lambda's
        # 15-minute hard cap.
        aws lambda create-function \
            --function-name "${LAMBDA_FUNCTION_NAME}" \
            --region "${AWS_REGION}" \
            --package-type Image \
            --code "ImageUri=${ECR_URI}:${IMAGE_TAG}" \
            --role "${ROLE_ARN}" \
            --memory-size 1024 \
            --timeout 900 \
            --architectures x86_64 \
            --environment "Variables={${env_vars}}"

        aws lambda wait function-active-v2 \
            --function-name "${LAMBDA_FUNCTION_NAME}" \
            --region "${AWS_REGION}"
    fi

    log "Lambda deployed: ${LAMBDA_FUNCTION_NAME}"
}

update_lambda_code() {
    aws lambda update-function-code \
        --function-name "${LAMBDA_FUNCTION_NAME}" \
        --region "${AWS_REGION}" \
        --image-uri "${ECR_URI}:${IMAGE_TAG}"

    aws lambda wait function-updated-v2 \
        --function-name "${LAMBDA_FUNCTION_NAME}" \
        --region "${AWS_REGION}"
}

# ---------------------------------------------------------------
# 4. Wire async invocation failures to the existing SQS DLQ
# ---------------------------------------------------------------
configure_dlq() {
    log "Wiring async failures to SQS DLQ: ${DLQ_QUEUE_ARN}"
    aws lambda put-function-event-invoke-config \
        --function-name "${LAMBDA_FUNCTION_NAME}" \
        --region "${AWS_REGION}" \
        --maximum-retry-attempts 2 \
        --destination-config "{\"OnFailure\":{\"Destination\":\"${DLQ_QUEUE_ARN}\"}}"
}

# ---------------------------------------------------------------
# 5. Configure S3 event notification (MERGE — must not overwrite the
#    existing actions/*.json -> image-generator trigger)
# ---------------------------------------------------------------
configure_s3_trigger() {
    log "Merging S3 event notification on ${TRIGGER_BUCKET_NAME} -> ${TRIGGER_PREFIX}*${TRIGGER_SUFFIX}"

    LAMBDA_ARN="arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:${LAMBDA_FUNCTION_NAME}"

    # Grant S3 permission to invoke the Lambda
    aws lambda add-permission \
        --function-name "${LAMBDA_FUNCTION_NAME}" \
        --statement-id "s3-trigger-${NOTIFICATION_ID}" \
        --action "lambda:InvokeFunction" \
        --principal "s3.amazonaws.com" \
        --source-arn "arn:aws:s3:::${TRIGGER_BUCKET_NAME}" \
        --source-account "${AWS_ACCOUNT_ID}" \
        --region "${AWS_REGION}" 2>/dev/null \
    || log "Permission already exists — skipping."

    # MERGE-IN our notification config. Reading first preserves any other
    # Lambda triggers on the bucket (e.g. ieee-rc-image-generator on
    # actions/*.json). The python helper is idempotent on our Id.
    log "Reading existing bucket notification configuration..."
    local existing
    existing=$(aws s3api get-bucket-notification-configuration \
        --bucket "${TRIGGER_BUCKET_NAME}" 2>/dev/null || echo '{}')

    local merged
    merged=$(LAMBDA_ARN="${LAMBDA_ARN}" \
             NOTIFICATION_ID="${NOTIFICATION_ID}" \
             TRIGGER_PREFIX="${TRIGGER_PREFIX}" \
             TRIGGER_SUFFIX="${TRIGGER_SUFFIX}" \
             EXISTING="${existing}" \
             python3 <<'PY'
import json, os, sys

existing = json.loads(os.environ["EXISTING"] or "{}")
lambda_arn = os.environ["LAMBDA_ARN"]
notification_id = os.environ["NOTIFICATION_ID"]
prefix = os.environ["TRIGGER_PREFIX"]
suffix = os.environ["TRIGGER_SUFFIX"]

new_entry = {
    "Id": notification_id,
    "LambdaFunctionArn": lambda_arn,
    "Events": ["s3:ObjectCreated:*"],
    "Filter": {
        "Key": {
            "FilterRules": [
                {"Name": "prefix", "Value": prefix},
                {"Name": "suffix", "Value": suffix},
            ]
        }
    },
}

configs = existing.get("LambdaFunctionConfigurations", [])
# Replace any prior entry with the same Id (idempotent)
configs = [c for c in configs if c.get("Id") != notification_id]
configs.append(new_entry)
existing["LambdaFunctionConfigurations"] = configs

# AWS rejects the put if these are empty arrays; strip rather than send empty.
for k in ("QueueConfigurations", "TopicConfigurations", "EventBridgeConfiguration"):
    if k in existing and not existing[k]:
        del existing[k]

print(json.dumps(existing))
PY
)

    log "Writing merged bucket notification configuration..."
    aws s3api put-bucket-notification-configuration \
        --bucket "${TRIGGER_BUCKET_NAME}" \
        --notification-configuration "${merged}"

    log "S3 trigger configured: s3://${TRIGGER_BUCKET_NAME}/${TRIGGER_PREFIX}*${TRIGGER_SUFFIX} -> ${LAMBDA_FUNCTION_NAME}"
}

# ---------------------------------------------------------------
# Main
# ---------------------------------------------------------------
if [[ "${1:-}" == "update" ]]; then
    log "Update mode — rebuilding image and updating Lambda code only."
    build_and_push
    update_lambda_code
    log "Done."
    exit 0
fi

log "Full deployment starting..."
create_ecr_repo
create_lambda_role
build_and_push
create_lambda
configure_dlq
configure_s3_trigger
log "Deployment complete."
log ""
log "  ECR:       ${ECR_URI}:${IMAGE_TAG}"
log "  Lambda:    ${LAMBDA_FUNCTION_NAME} (1024 MB, 900s timeout)"
log "  Trigger:   s3://${TRIGGER_BUCKET_NAME}/${TRIGGER_PREFIX}*${TRIGGER_SUFFIX}"
log "  DLQ:       ${DLQ_QUEUE_ARN}"
log ""
log "Reminder: Drupal s3fs IAM role needs secretsmanager:CreateSecret/"
log "PutSecretValue/DeleteSecret on iplr/drive-tokens/* before end-to-end"
log "smoke tests can pass (CC3-898 acceptance criterion A4)."
