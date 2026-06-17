#!/usr/bin/env bash
#
# Invoke the PPTX extractor Lambda with a test payload.
#
# Usage:
#   ./scripts/invoke-pptx-extractor.sh <bucket> <key> <ou> <product_part_number>
#
# Example:
#   ./scripts/invoke-pptx-extractor.sh dev-ieee-conference-cloud-bulk-uploads \
#       ieee/pending/STD-12345.pptx ieee STD-12345
#
set -euo pipefail

AWS_PROFILE="${AWS_PROFILE:-ieee-cc}"
AWS_REGION="${AWS_REGION:-us-east-1}"
LAMBDA_FUNCTION_NAME="ieee-rc-pptx-extractor"

BUCKET="${1:?Usage: invoke-pptx-extractor.sh <bucket> <key> <ou> <product_part_number>}"
KEY="${2:?}"
OU="${3:?}"
PART_NUMBER="${4:?}"

export AWS_PROFILE AWS_REGION

PAYLOAD=$(cat <<EOF
{
  "bucket": "${BUCKET}",
  "key": "${KEY}",
  "ou": "${OU}",
  "product_part_number": "${PART_NUMBER}"
}
EOF
)

echo "==> Invoking ${LAMBDA_FUNCTION_NAME}..."
echo "    Payload: ${PAYLOAD}"

RESPONSE_FILE=$(mktemp)
trap 'rm -f "${RESPONSE_FILE}"' EXIT

aws lambda invoke \
    --function-name "${LAMBDA_FUNCTION_NAME}" \
    --region "${AWS_REGION}" \
    --cli-binary-format raw-in-base64-out \
    --payload "${PAYLOAD}" \
    "${RESPONSE_FILE}"

echo ""
echo "==> Response:"
cat "${RESPONSE_FILE}" | python3 -m json.tool
