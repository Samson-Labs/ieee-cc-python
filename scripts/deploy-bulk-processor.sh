#!/usr/bin/env bash
#
# Deploy the Bulk Processor Lambda using AWS CLI + Docker.
#
# Prerequisites:
#   - AWS CLI configured with profile "ieee-cc"
#   - Docker running locally
#
# Usage:
#   ./scripts/deploy-bulk-processor.sh <env>            # first-time setup + deploy
#   ./scripts/deploy-bulk-processor.sh <env> update     # rebuild + update Lambda code only
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

ECR_REPO_NAME="ieee-rc-bulk-processor"
LAMBDA_FUNCTION_NAME="ieee-rc-bulk-processor-${ENV}"
S3_BUCKET_NAME="${S3_BUCKET_NAME:-${ENV}-ieee-conference-cloud-bulk-uploads}"
LAMBDA_ROLE_NAME="ieee-rc-bulk-processor-${ENV}-role"
IMAGE_TAG="latest"

# Shared SNS topic + SQS queue — strict `-${ENV}` suffix in every env.
# Matches the pattern in deploy-dlq-processor.sh (CC3-886 Phase 1). The
# legacy unsuffixed resources on dev stay live until Phase 6.4
# decommissions them.
SNS_TOPIC_NAME="ieee-rc-bulk-completion-${ENV}"
SQS_QUEUE_NAME="ieee-rc-bulk-processing-queue-${ENV}"

TRIGGER_PREFIX="bulk/manifests/"
TRIGGER_SUFFIX=".json"
# Stable NOTIFICATION_ID across envs so deploying the env-suffixed Lambda
# atomically retargets the existing trigger entry (the merge logic replaces
# by Id). Avoids double-firing the legacy + env-suffixed Lambdas on the
# same s3:ObjectCreated event during the cutover window.
NOTIFICATION_ID="bulk-processor-trigger"

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
            "Action": ["s3:ListBucket"],
            "Resource": "arn:aws:s3:::${S3_BUCKET_NAME}"
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
# 3. Create SNS topic for batch-completion notifications (idempotent)
# ---------------------------------------------------------------
create_sns_topic() {
    log "Creating SNS topic: ${SNS_TOPIC_NAME}"
    aws sns create-topic \
        --name "${SNS_TOPIC_NAME}" \
        --region "${AWS_REGION}" \
        --query TopicArn --output text >/dev/null
}

# ---------------------------------------------------------------
# 4. Create SQS queue (idempotent)
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
build_env_vars() {
    # Single source of truth for the Lambda's env map, so create + update
    # paths can't drift. Lambda's --environment replaces wholesale; new
    # vars MUST be added here, not just to the create-function call.
    local queue_url
    queue_url=$(aws sqs get-queue-url --queue-name "${SQS_QUEUE_NAME}" \
        --region "${AWS_REGION}" --query QueueUrl --output text 2>/dev/null || echo "")

    printf '%s' \
        "LOG_LEVEL=${LOG_LEVEL:-INFO}" \
        ",STAGE=${ENV}" \
        ",S3_BUCKET=${S3_BUCKET_NAME}" \
        ",BULK_QUEUE_URL=${queue_url}" \
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

    # Re-emit env vars on every deploy so config changes (STAGE swap, SQS
    # URL refreshes, new vars) land on existing Lambdas — not just on first
    # create. Without this, --environment on the create-function path
    # silently no-ops when the Lambda already exists.
    aws lambda update-function-configuration \
        --function-name "${LAMBDA_FUNCTION_NAME}" \
        --region "${AWS_REGION}" \
        --environment "Variables={$(build_env_vars)}"

    aws lambda wait function-updated-v2 \
        --function-name "${LAMBDA_FUNCTION_NAME}" \
        --region "${AWS_REGION}"
}

# ---------------------------------------------------------------
# 5. Configure S3 event notification (MERGE — must not overwrite the
#    existing actions/*.json -> image-generator or transfer-actions/*.json
#    -> wizard-transfer triggers on the same bucket)
# ---------------------------------------------------------------
configure_s3_trigger() {
    log "Merging S3 event notification on ${S3_BUCKET_NAME} -> ${TRIGGER_PREFIX}*${TRIGGER_SUFFIX}"

    LAMBDA_ARN="arn:aws:lambda:${AWS_REGION}:${AWS_ACCOUNT_ID}:function:${LAMBDA_FUNCTION_NAME}"

    # Grant S3 permission to invoke the Lambda (idempotent).
    aws lambda add-permission \
        --function-name "${LAMBDA_FUNCTION_NAME}" \
        --statement-id "s3-trigger-${NOTIFICATION_ID}" \
        --action "lambda:InvokeFunction" \
        --principal "s3.amazonaws.com" \
        --source-arn "arn:aws:s3:::${S3_BUCKET_NAME}" \
        --source-account "${AWS_ACCOUNT_ID}" \
        --region "${AWS_REGION}" 2>/dev/null \
    || log "Permission already exists — skipping."

    # Read-modify-write the bucket notification config so we don't clobber
    # other Lambdas already wired to this bucket.
    log "Reading existing bucket notification configuration..."
    local existing
    existing=$(aws s3api get-bucket-notification-configuration \
        --bucket "${S3_BUCKET_NAME}" 2>/dev/null || echo '{}')

    local merged
    merged=$(LAMBDA_ARN="${LAMBDA_ARN}" \
             NOTIFICATION_ID="${NOTIFICATION_ID}" \
             TRIGGER_PREFIX="${TRIGGER_PREFIX}" \
             TRIGGER_SUFFIX="${TRIGGER_SUFFIX}" \
             EXISTING="${existing}" \
             python3 <<'PY'
import json, os

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
configs = [c for c in configs if c.get("Id") != notification_id]
configs.append(new_entry)
existing["LambdaFunctionConfigurations"] = configs

for k in ("QueueConfigurations", "TopicConfigurations", "EventBridgeConfiguration"):
    if k in existing and not existing[k]:
        del existing[k]

print(json.dumps(existing))
PY
)

    log "Writing merged bucket notification configuration..."
    aws s3api put-bucket-notification-configuration \
        --bucket "${S3_BUCKET_NAME}" \
        --notification-configuration "${merged}"

    log "S3 trigger configured: s3://${S3_BUCKET_NAME}/${TRIGGER_PREFIX}*${TRIGGER_SUFFIX} -> ${LAMBDA_FUNCTION_NAME}"
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
create_sns_topic
create_sqs_queue
build_and_push
create_lambda
configure_s3_trigger
log "Deployment complete."
log ""
log "  ECR:     ${ECR_URI}:${IMAGE_TAG}"
log "  Lambda:  ${LAMBDA_FUNCTION_NAME} (512 MB, 5 min timeout)"
log "  SQS:     ${SQS_QUEUE_NAME}"
log "  Trigger: s3://${S3_BUCKET_NAME}/${TRIGGER_PREFIX}*${TRIGGER_SUFFIX}"
log ""
log "  Invoke (direct, for replay):"
log "    ./scripts/invoke-bulk-processor.sh <batch_id>"
