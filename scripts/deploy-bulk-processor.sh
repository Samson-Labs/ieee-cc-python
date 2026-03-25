#!/usr/bin/env bash
#
# Deploy the Bulk Processor Lambda using AWS CLI + Docker.
#
# Prerequisites:
#   - AWS CLI configured with profile "ieee-cc"
#   - Docker running locally
#
# Usage:
#   ./scripts/deploy-bulk-processor.sh                # first-time setup + deploy
#   ./scripts/deploy-bulk-processor.sh update         # rebuild image + update Lambda code only
#
set -euo pipefail

AWS_PROFILE="${AWS_PROFILE:-ieee-cc}"
AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_PROFILE AWS_REGION

AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

ECR_REPO_NAME="ieee-rc-bulk-processor"
LAMBDA_FUNCTION_NAME="ieee-rc-bulk-processor"
S3_BUCKET_NAME="dev-ieee-conference-cloud-bulk-uploads"
LAMBDA_ROLE_NAME="ieee-rc-bulk-processor-role"
SNS_TOPIC_NAME="ieee-rc-bulk-completion"
SQS_QUEUE_NAME="ieee-rc-bulk-processing-queue"
IMAGE_TAG="latest"

ECR_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO_NAME}"
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

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
        -f "${PROJECT_ROOT}/src/bulk/Dockerfile.processor" \
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

    SQS_ARN="arn:aws:sqs:${AWS_REGION}:${AWS_ACCOUNT_ID}:${SQS_QUEUE_NAME}"

    INLINE_POLICY=$(cat <<EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": ["s3:GetObject"],
            "Resource": "arn:aws:s3:::${S3_BUCKET_NAME}/bulk/manifests/*"
        },
        {
            "Effect": "Allow",
            "Action": ["s3:PutObject"],
            "Resource": "arn:aws:s3:::${S3_BUCKET_NAME}/bulk/progress/*"
        },
        {
            "Effect": "Allow",
            "Action": ["sqs:SendMessage"],
            "Resource": "${SQS_ARN}"
        },
        {
            "Effect": "Allow",
            "Action": ["sns:Publish"],
            "Resource": "arn:aws:sns:${AWS_REGION}:${AWS_ACCOUNT_ID}:${SNS_TOPIC_NAME}"
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
        --policy-name "BulkProcessorAccess" \
        --policy-document "${INLINE_POLICY}"

    ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${LAMBDA_ROLE_NAME}"
}

# ---------------------------------------------------------------
# 3. Create SQS queue (idempotent)
# ---------------------------------------------------------------
create_sqs_queue() {
    log "Creating SQS queue: ${SQS_QUEUE_NAME}"

    QUEUE_URL=$(aws sqs get-queue-url --queue-name "${SQS_QUEUE_NAME}" \
        --region "${AWS_REGION}" --query QueueUrl --output text 2>/dev/null || echo "")

    if [[ -z "${QUEUE_URL}" ]]; then
        QUEUE_URL=$(aws sqs create-queue \
            --queue-name "${SQS_QUEUE_NAME}" \
            --region "${AWS_REGION}" \
            --attributes "VisibilityTimeout=600,MessageRetentionPeriod=1209600" \
            --query QueueUrl --output text)
        log "Queue created: ${QUEUE_URL}"
    else
        log "Queue already exists: ${QUEUE_URL}"
    fi
}

# ---------------------------------------------------------------
# 4. Create or update Lambda function
# ---------------------------------------------------------------
create_lambda() {
    log "Creating Lambda function: ${LAMBDA_FUNCTION_NAME}"
    ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${LAMBDA_ROLE_NAME}"

    QUEUE_URL=$(aws sqs get-queue-url --queue-name "${SQS_QUEUE_NAME}" \
        --region "${AWS_REGION}" --query QueueUrl --output text 2>/dev/null || echo "")

    if aws lambda get-function --function-name "${LAMBDA_FUNCTION_NAME}" \
        --region "${AWS_REGION}" >/dev/null 2>&1; then
        log "Lambda already exists — updating code..."
        update_lambda_code
    else
        log "Waiting for IAM role propagation..."
        aws iam wait role-exists --role-name "${LAMBDA_ROLE_NAME}"

        aws lambda create-function \
            --function-name "${LAMBDA_FUNCTION_NAME}" \
            --region "${AWS_REGION}" \
            --package-type Image \
            --code "ImageUri=${ECR_URI}:${IMAGE_TAG}" \
            --role "${ROLE_ARN}" \
            --memory-size 512 \
            --timeout 300 \
            --architectures x86_64 \
            --environment "Variables={LOG_LEVEL=INFO,S3_BUCKET=${S3_BUCKET_NAME},BULK_QUEUE_URL=${QUEUE_URL},COMPLETION_SNS_TOPIC_ARN=arn:aws:sns:${AWS_REGION}:${AWS_ACCOUNT_ID}:${SNS_TOPIC_NAME}}"

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
create_sqs_queue
build_and_push
create_lambda
log "Deployment complete."
log ""
log "  ECR:     ${ECR_URI}:${IMAGE_TAG}"
log "  Lambda:  ${LAMBDA_FUNCTION_NAME} (512 MB, 5 min timeout)"
log "  SQS:     ${SQS_QUEUE_NAME}"
log ""
log "  Invoke:"
log "    ./scripts/invoke-bulk-processor.sh <batch_id>"
