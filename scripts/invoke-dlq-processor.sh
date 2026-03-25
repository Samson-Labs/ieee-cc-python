#!/usr/bin/env bash
#
# Invoke the DLQ Processor Lambda with a sample SQS event.
#
# Usage:
#   ./scripts/invoke-dlq-processor.sh
#
set -euo pipefail

AWS_PROFILE="${AWS_PROFILE:-ieee-cc}"
AWS_REGION="${AWS_REGION:-us-east-1}"
LAMBDA_FUNCTION_NAME="ieee-rc-dlq-processor"

export AWS_PROFILE AWS_REGION

# Sample SQS event with a DLQ message
PAYLOAD='{
  "Records": [
    {
      "messageId": "test-msg-001",
      "body": "{\"original_event\":{\"bucket\":\"dev-ieee-conference-cloud-bulk-uploads\",\"key\":\"PES/pending/STD-12345.pdf\"},\"error\":{\"error_type\":\"BedrockError\",\"error_message\":\"ThrottlingException\",\"is_retriable\":true,\"correlation_id\":\"req-test-001\",\"timestamp\":\"2026-03-20T00:00:00+00:00\",\"stack_trace\":\"Traceback ...\"},\"retry_count\":0}"
    }
  ]
}'

OUTPUT_FILE=$(mktemp)

echo "==> Invoking ${LAMBDA_FUNCTION_NAME} with sample DLQ event"
echo ""

aws lambda invoke \
    --function-name "${LAMBDA_FUNCTION_NAME}" \
    --region "${AWS_REGION}" \
    --payload "${PAYLOAD}" \
    --cli-read-timeout 60 \
    "${OUTPUT_FILE}"

echo ""
echo "==> Response:"
python3 -m json.tool "${OUTPUT_FILE}"
rm -f "${OUTPUT_FILE}"
