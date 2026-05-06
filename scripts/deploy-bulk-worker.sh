#!/usr/bin/env bash
#
# Deploy the Bulk Worker Lambda using AWS CLI + Docker.
#
# Prerequisites:
#   - AWS CLI configured with profile "ieee-cc"
#   - Docker running locally
#   - SQS queue "ieee-rc-bulk-processing-queue" already created by deploy-bulk-processor.sh
#
# Usage:
#   ./scripts/deploy-bulk-worker.sh                # first-time setup + deploy
#   ./scripts/deploy-bulk-worker.sh update         # rebuild image + update Lambda code only
#
set -euo pipefail

AWS_PROFILE="${AWS_PROFILE:-ieee-cc}"
AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_PROFILE AWS_REGION

AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

ECR_REPO_NAME="ieee-rc-bulk-worker"
LAMBDA_FUNCTION_NAME="ieee-rc-bulk-worker"
S3_BUCKET_NAME="${S3_BUCKET_NAME:-dev-ieee-conference-cloud-bulk-uploads}"
LAMBDA_ROLE_NAME="ieee-rc-bulk-worker-role"
ORCHESTRATOR_FUNCTION_NAME="ieee-rc-ai-orchestrator"
SNS_TOPIC_NAME="ieee-rc-bulk-completion"
SQS_QUEUE_NAME="ieee-rc-bulk-processing-queue"
IMAGE_TAG="latest"

# Publish buckets the worker reads source media from for Strategy A items
# (the Drupal classifier emits s3_key pointing at the publish-bucket
# convention, e.g. video/private/{PPN}/{PPN}.{ext}). Comma-separated.
# Override for prod via environment: SOURCE_PUBLISH_BUCKETS=ieee-conference-cloud-content,ieee-conference-cloud
SOURCE_PUBLISH_BUCKETS="${SOURCE_PUBLISH_BUCKETS:-dev-ieee-conference-cloud-content,dev-ieee-conference-cloud}"

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
        -f "${PROJECT_ROOT}/src/bulk/Dockerfile.worker" \
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

    # Build the SOURCE_PUBLISH_BUCKETS list into a JSON array of S3 ARNs.
    # Hard-fail on empty input: an IAM policy with Resource: [] is rejected
    # by AWS (MalformedPolicyDocument), and silently installing a placeholder
    # ARN would mask a configuration error and re-trigger the AccessDenied
    # bug this PR is fixing.
    SOURCE_BUCKET_ARNS=$(python3 -c '
import json, sys
raw = sys.argv[1]
names = [b.strip() for b in raw.split(",") if b.strip()]
if not names:
    sys.stderr.write(
        f"ERROR: SOURCE_PUBLISH_BUCKETS resolved to no buckets (raw={raw!r}).\n"
        f"Refusing to write an IAM policy with no source buckets — Strategy A "
        f"items would fail with AccessDenied at runtime.\n"
    )
    sys.exit(1)
print(json.dumps([f"arn:aws:s3:::{n}/*" for n in names]))
' "${SOURCE_PUBLISH_BUCKETS}")

    INLINE_POLICY=$(cat <<EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": ["lambda:InvokeFunction"],
            "Resource": "arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:${ORCHESTRATOR_FUNCTION_NAME}"
        },
        {
            "Effect": "Allow",
            "Action": ["s3:GetObject", "s3:PutObject"],
            "Resource": [
                "arn:aws:s3:::${S3_BUCKET_NAME}/bulk/progress/*",
                "arn:aws:s3:::${S3_BUCKET_NAME}/*/metadata/*",
                "arn:aws:s3:::${S3_BUCKET_NAME}/*/pending/*"
            ]
        },
        {
            "Effect": "Allow",
            "Action": ["s3:GetObject"],
            "Resource": "arn:aws:s3:::${S3_BUCKET_NAME}/*/archive/*"
        },
        {
            "Sid": "ReadSourcePublishBuckets",
            "Effect": "Allow",
            "Action": ["s3:GetObject"],
            "Resource": ${SOURCE_BUCKET_ARNS}
        },
        {
            "Effect": "Allow",
            "Action": ["s3:ListBucket"],
            "Resource": "arn:aws:s3:::${S3_BUCKET_NAME}"
        },
        {
            "Effect": "Allow",
            "Action": ["sns:Publish"],
            "Resource": "arn:aws:sns:${AWS_REGION}:${AWS_ACCOUNT_ID}:${SNS_TOPIC_NAME}"
        },
        {
            "Effect": "Allow",
            "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"],
            "Resource": "${SQS_ARN}"
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
        --policy-name "BulkWorkerAccess" \
        --policy-document "${INLINE_POLICY}"

    ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${LAMBDA_ROLE_NAME}"
}

# ---------------------------------------------------------------
# 3. Create SNS topic for batch-completion notifications (idempotent)
#    Shared with the bulk-processor; safe to (re)create from either script.
# ---------------------------------------------------------------
create_sns_topic() {
    log "Creating SNS topic: ${SNS_TOPIC_NAME}"
    aws sns create-topic \
        --name "${SNS_TOPIC_NAME}" \
        --region "${AWS_REGION}" \
        --query TopicArn --output text >/dev/null
}

# ---------------------------------------------------------------
# 4. Create or update Lambda function
# ---------------------------------------------------------------
create_lambda() {
    log "Creating Lambda function: ${LAMBDA_FUNCTION_NAME}"
    ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${LAMBDA_ROLE_NAME}"

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
            --environment "Variables={LOG_LEVEL=${LOG_LEVEL:-INFO},ORCHESTRATOR_FUNCTION_NAME=${ORCHESTRATOR_FUNCTION_NAME},S3_BUCKET=${S3_BUCKET_NAME},COMPLETION_SNS_TOPIC_ARN=arn:aws:sns:${AWS_REGION}:${AWS_ACCOUNT_ID}:${SNS_TOPIC_NAME}}"

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
# 4. Create SQS event source mapping (idempotent)
# ---------------------------------------------------------------
create_event_source_mapping() {
    log "Creating SQS event source mapping..."
    SQS_ARN="arn:aws:sqs:${AWS_REGION}:${AWS_ACCOUNT_ID}:${SQS_QUEUE_NAME}"

    EXISTING=$(aws lambda list-event-source-mappings \
        --function-name "${LAMBDA_FUNCTION_NAME}" \
        --event-source-arn "${SQS_ARN}" \
        --region "${AWS_REGION}" \
        --query "EventSourceMappings[0].UUID" \
        --output text 2>/dev/null || echo "None")

    if [[ "${EXISTING}" == "None" || "${EXISTING}" == "" ]]; then
        aws lambda create-event-source-mapping \
            --function-name "${LAMBDA_FUNCTION_NAME}" \
            --event-source-arn "${SQS_ARN}" \
            --batch-size 1 \
            --scaling-config '{"MaximumConcurrency": 10}' \
            --function-response-types "ReportBatchItemFailures" \
            --region "${AWS_REGION}"
        log "Event source mapping created (MaxConcurrency=10)."
    else
        log "Event source mapping already exists: ${EXISTING}"
    fi
}

# ---------------------------------------------------------------
# Main
# ---------------------------------------------------------------
if [[ "${1:-}" == "update" ]]; then
    log "Update mode — refreshing IAM, rebuilding image, and updating Lambda code."
    # IAM is refreshed every run because create_lambda_role is idempotent
    # (put-role-policy replaces) and the inline policy may have grown
    # between deploys. Without this, code that needs new permissions
    # (e.g. CC3-892's s3:GetObject on publish source buckets) deploys
    # fine but logs AccessDenied on every invocation.
    create_lambda_role
    build_and_push
    update_lambda_code
    log "Done."
    exit 0
fi

log "Full deployment starting..."
create_ecr_repo
create_lambda_role
create_sns_topic
build_and_push
create_lambda
create_event_source_mapping
log "Deployment complete."
log ""
log "  ECR:     ${ECR_URI}:${IMAGE_TAG}"
log "  Lambda:  ${LAMBDA_FUNCTION_NAME} (512 MB, 5 min timeout)"
log "  SQS:     ${SQS_QUEUE_NAME} → ${LAMBDA_FUNCTION_NAME} (batch 1, MaxConcurrency 10)"
log ""
log "  Invoke:"
log "    ./scripts/invoke-bulk-worker.sh"
