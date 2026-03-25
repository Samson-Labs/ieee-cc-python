#!/usr/bin/env bash
#
# Invoke the Bulk Worker Lambda with a sample SQS event.
#
# Usage:
#   ./scripts/invoke-bulk-worker.sh
#
set -euo pipefail

AWS_PROFILE="${AWS_PROFILE:-ieee-cc}"
AWS_REGION="${AWS_REGION:-us-east-1}"
LAMBDA_FUNCTION_NAME="ieee-rc-bulk-worker"

export AWS_PROFILE AWS_REGION

# Sample SQS event with one bulk item
PAYLOAD='{
  "Records": [
    {
      "messageId": "test-msg-001",
      "body": "{\"batch_id\":\"bulk-test-001\",\"callback_url\":\"https://resourcecenter.ieee.org/api/iplr/webhook/ai-enrichment\",\"item\":{\"item_id\":12345,\"request_id\":0,\"s3_key\":\"PES/archive/paper.pdf\",\"media_type\":\"PDF\",\"resource_center\":\"PES\",\"title\":\"Test Paper\"},\"total_items\":1}"
    }
  ]
}'

OUTPUT_FILE=$(mktemp)

echo "==> Invoking ${LAMBDA_FUNCTION_NAME} with sample bulk item"
echo ""

aws lambda invoke \
    --function-name "${LAMBDA_FUNCTION_NAME}" \
    --region "${AWS_REGION}" \
    --payload "${PAYLOAD}" \
    --cli-read-timeout 300 \
    "${OUTPUT_FILE}"

echo ""
echo "==> Response:"
python3 -m json.tool "${OUTPUT_FILE}"
rm -f "${OUTPUT_FILE}"
