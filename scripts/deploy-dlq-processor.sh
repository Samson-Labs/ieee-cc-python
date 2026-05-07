#!/usr/bin/env bash
#
# Deploy the DLQ Processor Lambda using AWS CLI + Docker.
#
# Prerequisites:
#   - AWS CLI configured with profile "ieee-cc"
#   - Docker running locally
#
# Usage:
#   ./scripts/deploy-dlq-processor.sh <env>            # first-time setup + deploy
#   ./scripts/deploy-dlq-processor.sh <env> update     # rebuild + update Lambda code only
#
#   <env> = dev | staging | prod
#
set -euo pipefail

ENV="${1:-}"
case "${ENV}" in
    dev|staging|prod) ;;
    *)
        echo "Usage: $0 <env> [update]   # env = dev | staging | prod" >&2
        exit 1
        ;;
esac

AWS_PROFILE="${AWS_PROFILE:-ieee-cc}"
AWS_REGION="${AWS_REGION:-us-east-1}"
export AWS_PROFILE AWS_REGION

AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

# Prod uses no suffix (account naming convention); dev/staging use -${ENV}
SUFFIX=$([[ "${ENV}" == "prod" ]] && echo "" || echo "-${ENV}")

ECR_REPO_NAME="ieee-rc-dlq-processor"
LAMBDA_FUNCTION_NAME="ieee-rc-dlq-processor${SUFFIX}"
S3_BUCKET_NAME=$([[ "${ENV}" == "prod" ]] && echo "ieee-conference-cloud-bulk-uploads" || echo "${ENV}-ieee-conference-cloud-bulk-uploads")
LAMBDA_ROLE_NAME="ieee-rc-dlq-processor${SUFFIX}-role"
SQS_QUEUE_NAME="ieee-rc-processing-dlq${SUFFIX}"
ORCHESTRATOR_FUNCTION_NAME="ieee-rc-ai-orchestrator${SUFFIX}"
# Must match the topic provisioned by setup-s3-triggers.sh
SNS_TOPIC_NAME="ieee-rc-processing-alerts${SUFFIX}"
SNS_TOPIC_ARN="arn:aws:sns:${AWS_REGION}:${AWS_ACCOUNT_ID}:${SNS_TOPIC_NAME}"
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
        -f "${PROJECT_ROOT}/src/dlq/Dockerfile" \
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
            "Effect": "Allow",
            "Action": ["lambda:InvokeFunction"],
            "Resource": "arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:${ORCHESTRATOR_FUNCTION_NAME}"
        },
        {
            "Effect": "Allow",
            "Action": ["s3:PutObject"],
            "Resource": "arn:aws:s3:::${S3_BUCKET_NAME}/failed/*"
        },
        {
            "Effect": "Allow",
            "Action": ["sns:Publish"],
            "Resource": "arn:aws:sns:${AWS_REGION}:${AWS_ACCOUNT_ID}:${SNS_TOPIC_NAME}"
        },
        {
            "Effect": "Allow",
            "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"],
            "Resource": "arn:aws:sqs:${AWS_REGION}:${AWS_ACCOUNT_ID}:${SQS_QUEUE_NAME}"
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
        --policy-name "DLQProcessorAccess" \
        --policy-document "${INLINE_POLICY}"

    ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${LAMBDA_ROLE_NAME}"
}

# ---------------------------------------------------------------
# 3. Create or update Lambda function
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
            --memory-size 256 \
            --timeout 60 \
            --architectures x86_64 \
            --environment "Variables={LOG_LEVEL=INFO,STAGE=${ENV},ORCHESTRATOR_FUNCTION_NAME=${ORCHESTRATOR_FUNCTION_NAME},ARCHIVE_BUCKET=${S3_BUCKET_NAME},FAILURES_SNS_TOPIC_ARN=${SNS_TOPIC_ARN}}"

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

    # Merge owned env vars into existing configuration so out-of-band vars are preserved.
    # Existing vars are written to a temp file to avoid shell quoting / newline issues.
    ENV_TMPFILE=$(mktemp)
    aws lambda get-function-configuration \
        --function-name "${LAMBDA_FUNCTION_NAME}" \
        --region "${AWS_REGION}" \
        --query 'Environment.Variables' --output json 2>/dev/null > "${ENV_TMPFILE}" || echo "{}" > "${ENV_TMPFILE}"

    ENV_JSON_FILE=$(mktemp)
    python3 - "${ENV_TMPFILE}" > "${ENV_JSON_FILE}" <<PYEOF
import json, sys
with open(sys.argv[1]) as f:
    existing = json.load(f) or {}
existing.update({
    "LOG_LEVEL": "INFO",
    "STAGE": "${ENV}",
    "ORCHESTRATOR_FUNCTION_NAME": "${ORCHESTRATOR_FUNCTION_NAME}",
    "ARCHIVE_BUCKET": "${S3_BUCKET_NAME}",
    "FAILURES_SNS_TOPIC_ARN": "${SNS_TOPIC_ARN}",
})
print(json.dumps({"Variables": existing}))
PYEOF
    aws lambda update-function-configuration \
        --function-name "${LAMBDA_FUNCTION_NAME}" \
        --region "${AWS_REGION}" \
        --environment "file://${ENV_JSON_FILE}"
    rm -f "${ENV_TMPFILE}" "${ENV_JSON_FILE}"

    aws lambda wait function-updated-v2 \
        --function-name "${LAMBDA_FUNCTION_NAME}" \
        --region "${AWS_REGION}"
}

# ---------------------------------------------------------------
# 4. Create SQS event source mapping (idempotent)
# ---------------------------------------------------------------
verify_sqs_queue_exists() {
    if ! aws sqs get-queue-url \
        --queue-name "${SQS_QUEUE_NAME}" \
        --region "${AWS_REGION}" >/dev/null 2>&1; then
        echo "ERROR: SQS queue '${SQS_QUEUE_NAME}' does not exist." >&2
        echo "  Run: ./scripts/setup-s3-triggers.sh ${ENV}" >&2
        exit 1
    fi
    log "SQS queue verified: ${SQS_QUEUE_NAME}"
}

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
            --function-response-types "ReportBatchItemFailures" \
            --region "${AWS_REGION}"
        log "Event source mapping created."
    else
        log "Event source mapping already exists: ${EXISTING}"
    fi
}

# ---------------------------------------------------------------
# Main
# ---------------------------------------------------------------
if [[ "${2:-}" == "update" ]]; then
    log "Update mode (${ENV}) — rebuilding image and updating Lambda code only."
    build_and_push
    update_lambda_code
    log "Done."
    exit 0
fi

log "Full deployment starting (env=${ENV})..."
create_ecr_repo
create_lambda_role
build_and_push
create_lambda
verify_sqs_queue_exists
create_event_source_mapping
log "Deployment complete."
log ""
log "  ECR:     ${ECR_URI}:${IMAGE_TAG}"
log "  Lambda:  ${LAMBDA_FUNCTION_NAME} (256 MB, 60s timeout)"
log "  SQS:     ${SQS_QUEUE_NAME} → ${LAMBDA_FUNCTION_NAME} (batch size 1)"
log ""
log "  Invoke:"
log "    ./scripts/invoke-dlq-processor.sh"
