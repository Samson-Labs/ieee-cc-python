#!/usr/bin/env bash
#
# Deploy the AI Orchestrator Lambda using AWS CLI + Docker.
#
# Prerequisites:
#   - AWS CLI configured with profile "ieee-cc"
#   - Docker running locally
#
# Usage:
#   ./scripts/deploy-ai-orchestrator.sh <env>            # first-time setup + deploy
#   ./scripts/deploy-ai-orchestrator.sh <env> update     # rebuild + update Lambda code only
#
#   <env> = dev | staging   (prod naming handled separately under CC3-851)
#
set -euo pipefail

ENV="${1:-}"
case "${ENV}" in
    dev|staging) ;;
    *)
        echo "Usage: $0 <env> [update]   # env = dev | staging" >&2
        echo "       (prod naming is part of CC3-851; not accepted here)" >&2
        exit 1
        ;;
esac

AWS_PROFILE="${AWS_PROFILE:-ieee-cc}"
AWS_REGION="${AWS_REGION:-us-east-1}"
AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

ECR_REPO_NAME="ieee-rc-ai-orchestrator"
S3_BUCKET_NAME="${ENV}-ieee-conference-cloud-bulk-uploads"
IMAGE_TAG="latest"

# Lambda + role names — env-aware. CC3-886's env-suffix transition was
# never applied to the orchestrator on dev: the live function and role
# are still the legacy unsuffixed names, and the EventBridge rule
# `ieee-rc-s3-pending-trigger` targets them by that name. Deploying to
# `-dev` suffixes here would leave a parallel never-invoked twin while
# the wired-up Lambda stays stale. On staging the suffixed convention
# is used. CC3-975 / CC3-886.
LAMBDA_FUNCTION_NAME="ieee-rc-ai-orchestrator"
if [[ "${ENV}" != "dev" ]]; then
    LAMBDA_FUNCTION_NAME="${LAMBDA_FUNCTION_NAME}-${ENV}"
fi
LAMBDA_ROLE_NAME="${LAMBDA_FUNCTION_NAME}-role"

# NOTE: PDF_EXTRACTOR_FN points at the env-suffixed name, but the PDF
# extractor's deploy script (`scripts/deploy.sh`) still creates the
# unsuffixed legacy name `ieee-cc-pdf-extractor` (Node-14 migration is
# tracked separately). Until that catches up, override
# `PDF_EXTRACTOR_FUNCTION` on the orchestrator Lambda manually for the
# bridge period, or rename the existing PDF Lambda to `-${ENV}` first.
PDF_EXTRACTOR_FN="ieee-cc-pdf-extractor-${ENV}"
VIDEO_TRANSCRIBER_FN="ieee-cc-video-transcriber-${ENV}"
PPTX_EXTRACTOR_FN="ieee-rc-pptx-extractor-${ENV}"
BEDROCK_FN="ieee-cc-bedrock-inference-${ENV}"

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
        -f "${PROJECT_ROOT}/src/orchestrator/AIOrchestratorDockerfile" \
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

    # S3 read/write/delete + Bedrock + Transcribe + Lambda invoke
    INLINE_POLICY="{
        \"Version\": \"2012-10-17\",
        \"Statement\": [
            {
                \"Effect\": \"Allow\",
                \"Action\": [\"s3:GetObject\", \"s3:PutObject\", \"s3:DeleteObject\"],
                \"Resource\": \"arn:aws:s3:::${S3_BUCKET_NAME}/*\"
            },
            {
                \"Effect\": \"Allow\",
                \"Action\": [\"s3:ListBucket\"],
                \"Resource\": \"arn:aws:s3:::${S3_BUCKET_NAME}\"
            },
            {
                \"Effect\": \"Allow\",
                \"Action\": [\"lambda:InvokeFunction\"],
                \"Resource\": [
                    \"arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:${PDF_EXTRACTOR_FN}\",
                    \"arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:${VIDEO_TRANSCRIBER_FN}\",
                    \"arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:${PPTX_EXTRACTOR_FN}\",
                    \"arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:${BEDROCK_FN}\"
                ]
            },
            {
                \"Effect\": \"Allow\",
                \"Action\": [\"cloudwatch:PutMetricData\"],
                \"Resource\": \"*\",
                \"Condition\": {
                    \"StringEquals\": {
                        \"cloudwatch:namespace\": \"ieee-rc\"
                    }
                }
            },
            {
                \"Effect\": \"Allow\",
                \"Action\": [\"secretsmanager:GetSecretValue\"],
                \"Resource\": \"arn:aws:secretsmanager:${AWS_REGION}:${AWS_ACCOUNT_ID}:secret:iplr/webhook-secret*\"
            }
        ]
    }"

    aws iam get-role --role-name "${LAMBDA_ROLE_NAME}" >/dev/null 2>&1 \
    || aws iam create-role \
        --role-name "${LAMBDA_ROLE_NAME}" \
        --assume-role-policy-document "${TRUST_POLICY}"

    aws iam attach-role-policy \
        --role-name "${LAMBDA_ROLE_NAME}" \
        --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole" 2>/dev/null || true

    aws iam put-role-policy \
        --role-name "${LAMBDA_ROLE_NAME}" \
        --policy-name "OrchestratorAccess" \
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
            --memory-size 512 \
            --timeout 900 \
            --architectures x86_64 \
            --environment "Variables={LOG_LEVEL=INFO,STAGE=${ENV},PDF_EXTRACTOR_FUNCTION=${PDF_EXTRACTOR_FN},VIDEO_TRANSCRIBER_FUNCTION=${VIDEO_TRANSCRIBER_FN},PPTX_EXTRACTOR_FUNCTION=${PPTX_EXTRACTOR_FN},BEDROCK_FUNCTION=${BEDROCK_FN}}"

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
if [[ "${2:-}" == "update" ]]; then
    log "Update mode (${ENV}) — refreshing IAM, rebuilding image, and updating Lambda code."
    # IAM is refreshed every run because create_lambda_role is idempotent
    # (put-role-policy replaces) and the inline policy may have grown
    # between deploys. Without this, code that needs new permissions
    # (e.g. CC3-900's secretsmanager:GetSecretValue) deploys fine but
    # logs AccessDeniedException on every invocation in stage/live.
    create_lambda_role
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
log "Deployment complete."
log ""
log "  ECR:     ${ECR_URI}:${IMAGE_TAG}"
log "  Lambda:  ${LAMBDA_FUNCTION_NAME} (512 MB, 15 min timeout)"
log ""

# Probe the env-suffixed PDF extractor and warn if it doesn't exist yet.
# The legacy `scripts/deploy.sh` still creates an unsuffixed
# `ieee-cc-pdf-extractor`; orchestrator deploys here will reference
# `${PDF_EXTRACTOR_FN}` and fail with ResourceNotFoundException at first
# PDF dispatch unless the bridge is handled.
if ! aws lambda get-function --function-name "${PDF_EXTRACTOR_FN}" \
        --region "${AWS_REGION}" >/dev/null 2>&1; then
    log "WARNING: Lambda '${PDF_EXTRACTOR_FN}' does not exist yet."
    log "         Orchestrator will fail at PDF dispatch with ResourceNotFoundException."
    log "         Bridge until the PDF extractor migrates: either rename the legacy"
    log "         'ieee-cc-pdf-extractor' to '${PDF_EXTRACTOR_FN}', or override"
    log "         PDF_EXTRACTOR_FUNCTION on '${LAMBDA_FUNCTION_NAME}' to the legacy name."
fi

log "  Invoke:"
log "    ./scripts/invoke-ai-orchestrator.sh <bucket> <key>"
