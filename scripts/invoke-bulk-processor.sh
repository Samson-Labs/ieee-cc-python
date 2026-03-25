#!/usr/bin/env bash
#
# Invoke the Bulk Processor Lambda with a batch_id.
#
# Usage:
#   ./scripts/invoke-bulk-processor.sh <batch_id>
#   ./scripts/invoke-bulk-processor.sh bulk-test-001
#
set -euo pipefail

AWS_PROFILE="${AWS_PROFILE:-ieee-cc}"
AWS_REGION="${AWS_REGION:-us-east-1}"
LAMBDA_FUNCTION_NAME="ieee-rc-bulk-processor"

export AWS_PROFILE AWS_REGION

BATCH_ID="${1:-bulk-test-001}"

PAYLOAD=$(cat <<EOF
{"batch_id": "${BATCH_ID}"}
EOF
)

OUTPUT_FILE=$(mktemp)

echo "==> Invoking ${LAMBDA_FUNCTION_NAME} with batch_id=${BATCH_ID}"
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
