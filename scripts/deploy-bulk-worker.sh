#!/usr/bin/env bash
#
# Deploy the Bulk Worker Lambda using AWS CLI + Docker.
#
# Prerequisites:
#   - AWS CLI configured with profile "ieee-cc"
#   - Docker running locally
#   - SQS queue already created by deploy-bulk-processor.sh
#     (`ieee-rc-bulk-processing-queue` on dev, `ieee-rc-bulk-processing-queue-${ENV}` elsewhere)
#
# Usage:
#   ./scripts/deploy-bulk-worker.sh <env>            # first-time setup + deploy
#   ./scripts/deploy-bulk-worker.sh <env> update     # rebuild + update Lambda code only
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
export AWS_PROFILE AWS_REGION

AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

ECR_REPO_NAME="ieee-rc-bulk-worker"
LAMBDA_FUNCTION_NAME="ieee-rc-bulk-worker-${ENV}"
S3_BUCKET_NAME="${S3_BUCKET_NAME:-${ENV}-ieee-conference-cloud-bulk-uploads}"
LAMBDA_ROLE_NAME="ieee-rc-bulk-worker-${ENV}-role"
IMAGE_TAG="latest"

# Orchestrator target — strict `-${ENV}` suffix; the orchestrator's deploy
# script creates the env-suffixed Lambda in every env (CC3-886 Phase 1).
ORCHESTRATOR_FUNCTION_NAME="ieee-rc-ai-orchestrator-${ENV}"

# Shared SNS topic + SQS queue (provisioned by deploy-bulk-processor.sh).
# Strict `-${ENV}` suffix — must stay in lockstep with bulk-processor.
SNS_TOPIC_NAME="ieee-rc-bulk-completion-${ENV}"
SQS_QUEUE_NAME="ieee-rc-bulk-processing-queue-${ENV}"

# Publish buckets the worker reads source media from for Strategy A items
# (the Drupal classifier emits s3_key pointing at the publish-bucket
# convention, e.g. video/private/{PPN}/{PPN}.{ext}). Comma-separated.
# Env-aware default; can still be overridden via the SOURCE_PUBLISH_BUCKETS
# env var for one-off deploys.
SOURCE_PUBLISH_BUCKETS="${SOURCE_PUBLISH_BUCKETS:-${ENV}-ieee-conference-cloud-content,${ENV}-ieee-conference-cloud}"

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

    # Build the SOURCE_PUBLISH_BUCKETS list into JSON ARN arrays for both
    # object-level (s3:GetObject, requires "/*") and bucket-level
    # (s3:ListBucket, requires bare bucket ARN) statements. The worker's
    # CopyObject path consults the source bucket before copying, so both
    # are required — having only s3:GetObject reproduces the AccessDenied
    # this whole IAM thread has been chasing.
    #
    # Hard-fail on empty input: an IAM policy with Resource: [] is rejected
    # by AWS (MalformedPolicyDocument), and silently installing a placeholder
    # ARN would mask a configuration error and re-trigger the AccessDenied
    # at runtime.
    _render_arns() {
        # $1 = suffix ("/*" for objects, "" for bucket-level)
        python3 -c '
import json, sys
raw = sys.argv[1]
suffix = sys.argv[2]
names = [b.strip() for b in raw.split(",") if b.strip()]
if not names:
    sys.stderr.write(
        f"ERROR: SOURCE_PUBLISH_BUCKETS resolved to no buckets (raw={raw!r}).\n"
        f"Refusing to write an IAM policy with no source buckets — Strategy A "
        f"items would fail with AccessDenied at runtime.\n"
    )
    sys.exit(1)
print(json.dumps([f"arn:aws:s3:::{n}{suffix}" for n in names]))
' "${SOURCE_PUBLISH_BUCKETS}" "$1"
    }
    SOURCE_BUCKET_OBJECT_ARNS=$(_render_arns "/*")
    SOURCE_BUCKET_LIST_ARNS=$(_render_arns "")

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
            "Resource": ${SOURCE_BUCKET_OBJECT_ARNS}
        },
        {
            "Sid": "ListSourcePublishBuckets",
            "Effect": "Allow",
            "Action": ["s3:ListBucket"],
            "Resource": ${SOURCE_BUCKET_LIST_ARNS}
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
build_env_vars() {
    # Single source of truth for the Lambda's env map, so create + update
    # paths can't drift. Lambda's --environment replaces wholesale.
    printf '%s' \
        "LOG_LEVEL=${LOG_LEVEL:-INFO}" \
        ",STAGE=${ENV}" \
        ",ORCHESTRATOR_FUNCTION_NAME=${ORCHESTRATOR_FUNCTION_NAME}" \
        ",S3_BUCKET=${S3_BUCKET_NAME}" \
        ",COMPLETION_SNS_TOPIC_ARN=arn:aws:sns:${AWS_REGION}:${AWS_ACCOUNT_ID}:${SNS_TOPIC_NAME}"
}

create_lambda() {
    log "Creating Lambda function: ${LAMBDA_FUNCTION_NAME}"
    ROLE_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:role/${LAMBDA_ROLE_NAME}"

    if aws lambda get-function --function-name "${LAMBDA_FUNCTION_NAME}" \
        --region "${AWS_REGION}" >/dev/null 2>&1; then
        log "Lambda already exists — updating code and configuration..."
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
            --environment "Variables={$(build_env_vars)}"

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

    # Re-emit env vars on every deploy so config changes (STAGE swap,
    # orchestrator name change post-carve-out-revert, new vars) actually
    # land on existing Lambdas — not just on first create.
    aws lambda update-function-configuration \
        --function-name "${LAMBDA_FUNCTION_NAME}" \
        --region "${AWS_REGION}" \
        --environment "Variables={$(build_env_vars)}"

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
if [[ "${2:-}" == "update" ]]; then
    log "Update mode (${ENV}) — refreshing IAM, rebuilding image, and updating Lambda code."
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

log "Full deployment starting (env=${ENV})..."
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
