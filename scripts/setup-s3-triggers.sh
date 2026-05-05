#!/usr/bin/env bash
# setup-s3-triggers.sh
#
# Idempotent setup of S3 EventBridge triggers + supporting infrastructure
# for the ieee-cc-python AI pipeline.
#
# Provisions per environment:
#   1. EventBridge notifications enabled on the S3 bucket
#   2. EventBridge rules + Lambda targets + permissions
#        - ieee-rc-s3-pending-trigger-{env}   -> ieee-rc-ai-orchestrator-{env}
#        - ieee-rc-image-generator-trigger-{env} -> ieee-rc-image-generator-{env}
#   3. SQS DLQ:  ieee-rc-processing-dlq-{env}
#   4. SNS topics: ieee-rc-webhook-failures-{env}
#                  ieee-rc-processing-alerts-{env}
#   5. Lambda async invoke config (2 retries, DLQ on failure)
#   6. CloudWatch alarms (Lambda errors >= 5, DLQ depth > 0, Bedrock throttles >= 10)
#   7. Resource tags: Project=ieee-rc, Environment={env}
#
# Naming convention (matches account pattern confCloudAuth / confCloudAuth-staging):
#   dev     -> ieee-rc-ai-orchestrator-dev     bucket: dev-ieee-conference-cloud-bulk-uploads
#   staging -> ieee-rc-ai-orchestrator-staging bucket: staging-ieee-conference-cloud-bulk-uploads
#   prod    -> ieee-rc-ai-orchestrator         bucket: ieee-conference-cloud-bulk-uploads
#
# NOTE: Prod bucket has a live legacy S3->Lambda notification
# (bulk-uploads-transfer-prod-transfer). This script does NOT remove it.
# EventBridge coexists with S3 direct notifications safely.
#
# Usage:
#   ./scripts/setup-s3-triggers.sh dev
#   ./scripts/setup-s3-triggers.sh staging
#   ./scripts/setup-s3-triggers.sh prod
#
# Prerequisites (must exist before running):
#   - ieee-rc-ai-orchestrator-{env} Lambda
#   - ieee-rc-image-generator-{env} Lambda
#   (These are created by deploy-ai-orchestrator.sh and deploy-image-overlay.sh)

set -euo pipefail

# ---------------------------------------------------------------
# Arg validation
# ---------------------------------------------------------------
ENV="${1:-}"
case "${ENV}" in
    dev|staging|prod) ;;
    *)
        echo "Usage: $0 <env>   # env = dev | staging | prod" >&2
        exit 1
        ;;
esac

AWS_PROFILE="${AWS_PROFILE:-ieee-cc}"
AWS_REGION="${AWS_REGION:-us-east-1}"
ACCOUNT="$(aws sts get-caller-identity --profile "${AWS_PROFILE}" --query Account --output text)"

# ---------------------------------------------------------------
# Naming — prod has no suffix (account convention)
# ---------------------------------------------------------------
if [[ "${ENV}" == "prod" ]]; then
    SUFFIX=""
    BUCKET="ieee-conference-cloud-bulk-uploads"
else
    SUFFIX="-${ENV}"
    BUCKET="${ENV}-ieee-conference-cloud-bulk-uploads"
fi

ORCHESTRATOR_FN="ieee-rc-ai-orchestrator${SUFFIX}"
IMAGE_GEN_FN="ieee-rc-image-generator${SUFFIX}"
PENDING_RULE="ieee-rc-s3-pending-trigger${SUFFIX}"
IMAGE_RULE="ieee-rc-image-generator-trigger${SUFFIX}"
DLQ_NAME="ieee-rc-processing-dlq${SUFFIX}"
SNS_WEBHOOK="ieee-rc-webhook-failures${SUFFIX}"
SNS_ALERTS="ieee-rc-processing-alerts${SUFFIX}"

ORCHESTRATOR_ARN="arn:aws:lambda:${AWS_REGION}:${ACCOUNT}:function:${ORCHESTRATOR_FN}"
IMAGE_GEN_ARN="arn:aws:lambda:${AWS_REGION}:${ACCOUNT}:function:${IMAGE_GEN_FN}"
PENDING_RULE_ARN="arn:aws:events:${AWS_REGION}:${ACCOUNT}:rule/${PENDING_RULE}"
IMAGE_RULE_ARN="arn:aws:events:${AWS_REGION}:${ACCOUNT}:rule/${IMAGE_RULE}"

log() { echo "==> $*"; }

# ---------------------------------------------------------------
# 0. Verify target Lambdas exist
# ---------------------------------------------------------------
log "[0/7] Verifying target Lambdas exist..."

for FN in "${ORCHESTRATOR_FN}" "${IMAGE_GEN_FN}"; do
    if ! aws lambda get-function --function-name "${FN}" \
        --region "${AWS_REGION}" --profile "${AWS_PROFILE}" &>/dev/null; then
        echo "ERROR: Lambda '${FN}' does not exist. Deploy it first." >&2
        echo "  ./scripts/deploy-ai-orchestrator.sh ${ENV}" >&2
        exit 1
    fi
    echo "  found: ${FN}"
done

# ---------------------------------------------------------------
# 1. Enable EventBridge notifications on bucket
# ---------------------------------------------------------------
log "[1/7] Enabling EventBridge notifications on s3://${BUCKET}..."

EB_STATUS=$(aws s3api get-bucket-notification-configuration \
    --bucket "${BUCKET}" \
    --region "${AWS_REGION}" \
    --profile "${AWS_PROFILE}" \
    --query 'EventBridgeConfiguration' \
    --output text 2>/dev/null || echo "None")

if [[ "${EB_STATUS}" == "None" || -z "${EB_STATUS}" ]]; then
    # Get current notification config and merge EventBridge in
    CURRENT_NOTIF=$(aws s3api get-bucket-notification-configuration \
        --bucket "${BUCKET}" \
        --region "${AWS_REGION}" \
        --profile "${AWS_PROFILE}" \
        --output json 2>/dev/null || echo "{}")

    # Add EventBridgeConfiguration to existing config
    MERGED=$(echo "${CURRENT_NOTIF}" | python3 -c "
import sys, json
cfg = json.load(sys.stdin)
cfg['EventBridgeConfiguration'] = {}
print(json.dumps(cfg))
")

    aws s3api put-bucket-notification-configuration \
        --bucket "${BUCKET}" \
        --notification-configuration "${MERGED}" \
        --region "${AWS_REGION}" \
        --profile "${AWS_PROFILE}"
    echo "  enabled EventBridge on ${BUCKET}"
else
    echo "  already enabled: EventBridgeConfiguration present"
fi

# ---------------------------------------------------------------
# 2. Create/update EventBridge rules
# ---------------------------------------------------------------
log "[2/7] Creating/updating EventBridge rules..."

# Rule 1: pending trigger (pdf/mp4/pptx in {ou}/pending/)
PENDING_PATTERN=$(cat <<EOF
{
  "source": ["aws.s3"],
  "detail-type": ["Object Created"],
  "detail": {
    "bucket": {"name": ["${BUCKET}"]},
    "object": {"key": [{"wildcard": "*/pending/*.pdf"}, {"wildcard": "*/pending/*.mp4"}, {"wildcard": "*/pending/*.pptx"}]}
  }
}
EOF
)

aws events put-rule \
    --name "${PENDING_RULE}" \
    --event-pattern "${PENDING_PATTERN}" \
    --state ENABLED \
    --description "Trigger ${ORCHESTRATOR_FN} on S3 ObjectCreated in {ou}/pending/ (${ENV})" \
    --region "${AWS_REGION}" \
    --profile "${AWS_PROFILE}" \
    --output text > /dev/null
echo "  upserted: ${PENDING_RULE}"

# Rule 2: image generator trigger (actions/*.json)
IMAGE_PATTERN=$(cat <<EOF
{
  "source": ["aws.s3"],
  "detail-type": ["Object Created"],
  "detail": {
    "bucket": {"name": ["${BUCKET}"]},
    "object": {"key": [{"wildcard": "actions/*.json"}]}
  }
}
EOF
)

aws events put-rule \
    --name "${IMAGE_RULE}" \
    --event-pattern "${IMAGE_PATTERN}" \
    --state ENABLED \
    --description "Trigger ${IMAGE_GEN_FN} on actions/*.json uploads (${ENV})" \
    --region "${AWS_REGION}" \
    --profile "${AWS_PROFILE}" \
    --output text > /dev/null
echo "  upserted: ${IMAGE_RULE}"

# ---------------------------------------------------------------
# 3. Grant EventBridge invoke permissions on Lambdas
# ---------------------------------------------------------------
log "[3/7] Granting EventBridge Lambda invoke permissions..."

add_permission_idempotent() {
    local FN="$1" SID="$2" SOURCE_ARN="$3"
    local ERR
    if ERR=$(aws lambda add-permission \
        --function-name "${FN}" \
        --statement-id "${SID}" \
        --action lambda:InvokeFunction \
        --principal events.amazonaws.com \
        --source-arn "${SOURCE_ARN}" \
        --source-account "${ACCOUNT}" \
        --region "${AWS_REGION}" \
        --profile "${AWS_PROFILE}" \
        --output text 2>&1); then
        echo "  added: ${SID} on ${FN}"
    elif echo "${ERR}" | grep -q "ResourceConflictException\|already exists"; then
        echo "  exists: ${SID} on ${FN} (skipped)"
    else
        echo "ERROR: Failed to add permission ${SID} on ${FN}: ${ERR}" >&2
        exit 1
    fi
}

add_permission_idempotent "${ORCHESTRATOR_FN}" \
    "events${SUFFIX}-pending-trigger" \
    "${PENDING_RULE_ARN}"

add_permission_idempotent "${IMAGE_GEN_FN}" \
    "events${SUFFIX}-image-generator-trigger" \
    "${IMAGE_RULE_ARN}"

# ---------------------------------------------------------------
# 4. Set EventBridge rule targets
# ---------------------------------------------------------------
log "[4/7] Setting EventBridge rule targets..."

aws events put-targets \
    --rule "${PENDING_RULE}" \
    --targets "Id=${ORCHESTRATOR_FN},Arn=${ORCHESTRATOR_ARN}" \
    --region "${AWS_REGION}" \
    --profile "${AWS_PROFILE}" \
    --output text > /dev/null
echo "  target set: ${PENDING_RULE} -> ${ORCHESTRATOR_FN}"

aws events put-targets \
    --rule "${IMAGE_RULE}" \
    --targets "Id=${IMAGE_GEN_FN},Arn=${IMAGE_GEN_ARN}" \
    --region "${AWS_REGION}" \
    --profile "${AWS_PROFILE}" \
    --output text > /dev/null
echo "  target set: ${IMAGE_RULE} -> ${IMAGE_GEN_FN}"

# ---------------------------------------------------------------
# 5. SQS DLQ + SNS topics
# ---------------------------------------------------------------
log "[5/7] Creating SQS DLQ and SNS topics..."

# SQS DLQ
DLQ_URL=$(aws sqs get-queue-url \
    --queue-name "${DLQ_NAME}" \
    --region "${AWS_REGION}" \
    --profile "${AWS_PROFILE}" \
    --query 'QueueUrl' --output text 2>/dev/null || true)

if [[ -z "${DLQ_URL}" ]]; then
    DLQ_URL=$(aws sqs create-queue \
        --queue-name "${DLQ_NAME}" \
        --attributes VisibilityTimeout=120,MessageRetentionPeriod=1209600 \
        --region "${AWS_REGION}" \
        --profile "${AWS_PROFILE}" \
        --query 'QueueUrl' --output text)
    echo "  created: ${DLQ_NAME}"
else
    echo "  exists: ${DLQ_NAME}"
fi

DLQ_ARN=$(aws sqs get-queue-attributes \
    --queue-url "${DLQ_URL}" \
    --attribute-names QueueArn \
    --region "${AWS_REGION}" \
    --profile "${AWS_PROFILE}" \
    --query 'Attributes.QueueArn' --output text)

# SNS topics
for TOPIC_NAME in "${SNS_WEBHOOK}" "${SNS_ALERTS}"; do
    TOPIC_ARN=$(aws sns create-topic \
        --name "${TOPIC_NAME}" \
        --region "${AWS_REGION}" \
        --profile "${AWS_PROFILE}" \
        --query 'TopicArn' --output text)
    echo "  upserted: ${TOPIC_NAME}"
done

# ---------------------------------------------------------------
# 6. Lambda async invoke config
# ---------------------------------------------------------------
log "[6/7] Configuring Lambda async invoke (2 retries + DLQ)..."

for FN in "${ORCHESTRATOR_FN}" "${IMAGE_GEN_FN}"; do
    aws lambda put-function-event-invoke-config \
        --function-name "${FN}" \
        --maximum-retry-attempts 2 \
        --destination-config "OnFailure={Destination=${DLQ_ARN}}" \
        --region "${AWS_REGION}" \
        --profile "${AWS_PROFILE}" \
        --output text > /dev/null
    echo "  configured: ${FN} (retries=2, dlq=${DLQ_NAME})"
done

# ---------------------------------------------------------------
# 7. CloudWatch alarms
# ---------------------------------------------------------------
log "[7/7] Creating/updating CloudWatch alarms..."

ALERTS_ARN="arn:aws:sns:${AWS_REGION}:${ACCOUNT}:${SNS_ALERTS}"

# Lambda errors >= 5 in 5 min
aws cloudwatch put-metric-alarm \
    --alarm-name "ieee-rc-lambda-error-rate${SUFFIX}" \
    --alarm-description "Lambda errors >= 5 in 5 min (${ENV})" \
    --metric-name Errors \
    --namespace AWS/Lambda \
    --statistic Sum \
    --period 300 \
    --evaluation-periods 1 \
    --threshold 5 \
    --comparison-operator GreaterThanOrEqualToThreshold \
    --dimensions "Name=FunctionName,Value=${ORCHESTRATOR_FN}" \
    --alarm-actions "${ALERTS_ARN}" \
    --region "${AWS_REGION}" \
    --profile "${AWS_PROFILE}"
echo "  upserted: ieee-rc-lambda-error-rate${SUFFIX}"

# DLQ message count > 0
aws cloudwatch put-metric-alarm \
    --alarm-name "ieee-rc-dlq-messages${SUFFIX}" \
    --alarm-description "DLQ has messages (${ENV})" \
    --metric-name ApproximateNumberOfMessagesVisible \
    --namespace AWS/SQS \
    --statistic Sum \
    --period 60 \
    --evaluation-periods 1 \
    --threshold 0 \
    --comparison-operator GreaterThanThreshold \
    --dimensions "Name=QueueName,Value=${DLQ_NAME}" \
    --alarm-actions "${ALERTS_ARN}" \
    --region "${AWS_REGION}" \
    --profile "${AWS_PROFILE}"
echo "  upserted: ieee-rc-dlq-messages${SUFFIX}"

# Bedrock throttles >= 10 in 10 min
aws cloudwatch put-metric-alarm \
    --alarm-name "ieee-rc-bedrock-throttling${SUFFIX}" \
    --alarm-description "Bedrock throttles >= 10 in 10 min (${ENV})" \
    --metric-name InvocationThrottles \
    --namespace AWS/Bedrock \
    --statistic Sum \
    --period 600 \
    --evaluation-periods 1 \
    --threshold 10 \
    --comparison-operator GreaterThanOrEqualToThreshold \
    --dimensions "Name=ModelId,Value=us.anthropic.claude-sonnet-4-5-20250929-v1:0" \
    --alarm-actions "${ALERTS_ARN}" \
    --region "${AWS_REGION}" \
    --profile "${AWS_PROFILE}"
echo "  upserted: ieee-rc-bedrock-throttling${SUFFIX}"

# ---------------------------------------------------------------
# Tag all created resources
# ---------------------------------------------------------------
log "Tagging resources..."

aws sqs tag-queue \
    --queue-url "${DLQ_URL}" \
    --tags "Project=ieee-rc,Environment=${ENV}" \
    --region "${AWS_REGION}" \
    --profile "${AWS_PROFILE}"

for TOPIC_NAME in "${SNS_WEBHOOK}" "${SNS_ALERTS}"; do
    T_ARN="arn:aws:sns:${AWS_REGION}:${ACCOUNT}:${TOPIC_NAME}"
    aws sns tag-resource \
        --resource-arn "${T_ARN}" \
        --tags "Key=Project,Value=ieee-rc" "Key=Environment,Value=${ENV}" \
        --region "${AWS_REGION}" \
        --profile "${AWS_PROFILE}"
done

aws events tag-resource \
    --resource-arn "${PENDING_RULE_ARN}" \
    --tags "Key=Project,Value=ieee-rc" "Key=Environment,Value=${ENV}" \
    --region "${AWS_REGION}" \
    --profile "${AWS_PROFILE}"

aws events tag-resource \
    --resource-arn "${IMAGE_RULE_ARN}" \
    --tags "Key=Project,Value=ieee-rc" "Key=Environment,Value=${ENV}" \
    --region "${AWS_REGION}" \
    --profile "${AWS_PROFILE}"

# Tag CloudWatch alarms
for ALARM in \
    "ieee-rc-lambda-error-rate${SUFFIX}" \
    "ieee-rc-dlq-messages${SUFFIX}" \
    "ieee-rc-bedrock-throttling${SUFFIX}"; do
    aws cloudwatch tag-resource \
        --resource-arn "arn:aws:cloudwatch:${AWS_REGION}:${ACCOUNT}:alarm:${ALARM}" \
        --tags "Key=Project,Value=ieee-rc" "Key=Environment,Value=${ENV}" \
        --region "${AWS_REGION}" \
        --profile "${AWS_PROFILE}"
done

echo "  tagged all resources"

# ---------------------------------------------------------------
# Verification
# ---------------------------------------------------------------
echo ""
log "Verifying setup..."

echo "  EB rules:"
aws events list-rules \
    --name-prefix "ieee-rc" \
    --region "${AWS_REGION}" \
    --profile "${AWS_PROFILE}" \
    --query "Rules[?Name=='${PENDING_RULE}' || Name=='${IMAGE_RULE}'].{Name:Name,State:State}" \
    --output table 2>/dev/null

echo "  SQS DLQ:"
aws sqs get-queue-attributes \
    --queue-url "${DLQ_URL}" \
    --attribute-names ApproximateNumberOfMessages \
    --region "${AWS_REGION}" \
    --profile "${AWS_PROFILE}" \
    --output text

echo "  Lambda async invoke config:"
for FN in "${ORCHESTRATOR_FN}" "${IMAGE_GEN_FN}"; do
    aws lambda get-function-event-invoke-config \
        --function-name "${FN}" \
        --region "${AWS_REGION}" \
        --profile "${AWS_PROFILE}" \
        --query '{Function:FunctionArn,Retries:MaximumRetryAttempts,DLQ:DestinationConfig.OnFailure.Destination}' \
        --output table 2>/dev/null
done

echo ""
log "Setup complete for env=${ENV}."
