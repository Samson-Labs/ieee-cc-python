#!/usr/bin/env bash
#
# Deploy the Video Transcriber Lambda using AWS CLI + Docker.
#
# Prerequisites:
#   - AWS CLI configured with profile "ieee-cc"
#   - Docker running locally
#
# Usage:
#   ./scripts/deploy-video-transcriber.sh <env>            # first-time setup + deploy
#   ./scripts/deploy-video-transcriber.sh <env> update     # rebuild + update Lambda code only
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

ECR_REPO_NAME="ieee-cc-video-transcriber"
LAMBDA_FUNCTION_NAME="ieee-cc-video-transcriber-${ENV}"
S3_BUCKET_NAME="${ENV}-ieee-conference-cloud-bulk-uploads"
LAMBDA_ROLE_NAME="ieee-cc-video-transcriber-${ENV}-role"
MEDIACONVERT_ROLE_NAME="ieee-cc-mediaconvert-${ENV}-role"
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
        -f "${PROJECT_ROOT}/src/extractors/VideoTranscriberDockerfile" \
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

    # S3 read/write/delete + Transcribe + Bedrock + MediaConvert + iam:PassRole
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
                \"Action\": [
                    \"transcribe:StartTranscriptionJob\",
                    \"transcribe:GetTranscriptionJob\"
                ],
                \"Resource\": \"*\"
            },
            {
                \"Effect\": \"Allow\",
                \"Action\": [
                    \"mediaconvert:CreateJob\",
                    \"mediaconvert:GetJob\",
                    \"mediaconvert:DescribeEndpoints\"
                ],
                \"Resource\": \"*\"
            },
            {
                \"Effect\": \"Allow\",
                \"Action\": [\"iam:PassRole\"],
                \"Resource\": \"arn:aws:iam::${AWS_ACCOUNT_ID}:role/${MEDIACONVERT_ROLE_NAME}\",
                \"Condition\": {
                    \"StringEquals\": {
                        \"iam:PassedToService\": \"mediaconvert.amazonaws.com\"
                    }
                }
            },
            {
                \"Effect\": \"Allow\",
                \"Action\": [\"bedrock:InvokeModel\"],
                \"Resource\": [
                    \"arn:aws:bedrock:*::foundation-model/*\",
                    \"arn:aws:bedrock:*:*:inference-profile/*\"
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
        --policy-name "S3TranscribeBedrockAccess" \
        --policy-document "${INLINE_POLICY}"

    ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${LAMBDA_ROLE_NAME}"
}

# ---------------------------------------------------------------
# 2b. Create MediaConvert service role (idempotent)
# ---------------------------------------------------------------
create_mediaconvert_role() {
    log "Creating IAM role: ${MEDIACONVERT_ROLE_NAME}"

    MC_TRUST_POLICY='{
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "mediaconvert.amazonaws.com"},
            "Action": "sts:AssumeRole"
        }]
    }'

    MC_INLINE_POLICY="{
        \"Version\": \"2012-10-17\",
        \"Statement\": [
            {
                \"Effect\": \"Allow\",
                \"Action\": [\"s3:GetObject\"],
                \"Resource\": \"arn:aws:s3:::${S3_BUCKET_NAME}/*\"
            },
            {
                \"Effect\": \"Allow\",
                \"Action\": [\"s3:PutObject\"],
                \"Resource\": \"arn:aws:s3:::${S3_BUCKET_NAME}/transcribe-input/*\"
            }
        ]
    }"

    aws iam get-role --role-name "${MEDIACONVERT_ROLE_NAME}" >/dev/null 2>&1 \
    || aws iam create-role \
        --role-name "${MEDIACONVERT_ROLE_NAME}" \
        --assume-role-policy-document "${MC_TRUST_POLICY}"

    aws iam put-role-policy \
        --role-name "${MEDIACONVERT_ROLE_NAME}" \
        --policy-name "S3TranscribeInputAccess" \
        --policy-document "${MC_INLINE_POLICY}"
}

# ---------------------------------------------------------------
# 3. Create or update Lambda function
# ---------------------------------------------------------------
create_lambda() {
    log "Creating Lambda function: ${LAMBDA_FUNCTION_NAME}"
    ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${LAMBDA_ROLE_NAME}"
    MEDIACONVERT_ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${MEDIACONVERT_ROLE_NAME}"
    MEDIACONVERT_ENDPOINT="$(aws mediaconvert describe-endpoints \
        --region "${AWS_REGION}" --query 'Endpoints[0].Url' --output text)"

    LAMBDA_ENV_VARS="Variables={LOG_LEVEL=INFO,STAGE=${ENV},CLEANUP_MODEL_ID=us.anthropic.claude-3-5-haiku-20241022-v1:0,ENABLE_AUDIO_EXTRACTION=true,MEDIACONVERT_ENDPOINT=${MEDIACONVERT_ENDPOINT},MEDIACONVERT_ROLE_ARN=${MEDIACONVERT_ROLE_ARN}}"

    if aws lambda get-function --function-name "${LAMBDA_FUNCTION_NAME}" \
        --region "${AWS_REGION}" >/dev/null 2>&1; then
        log "Lambda already exists — updating code + environment..."
        update_lambda_code
        aws lambda update-function-configuration \
            --function-name "${LAMBDA_FUNCTION_NAME}" \
            --region "${AWS_REGION}" \
            --environment "${LAMBDA_ENV_VARS}"
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
            --environment "${LAMBDA_ENV_VARS}"

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
    log "Update mode (${ENV}) — rebuilding image and updating Lambda code only."
    build_and_push
    update_lambda_code
    log "Done."
    exit 0
fi

log "Full deployment starting (env=${ENV})..."
create_ecr_repo
create_mediaconvert_role
create_lambda_role
build_and_push
create_lambda
log "Deployment complete."
log ""
log "  ECR:     ${ECR_URI}:${IMAGE_TAG}"
log "  Lambda:  ${LAMBDA_FUNCTION_NAME} (512 MB, 15 min timeout)"
log ""
log "  Invoke:"
log "    ./scripts/invoke-video-transcriber.sh <bucket> <key> <ou> <product_part_number>"
