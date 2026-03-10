#!/usr/bin/env bash
#
# Invoke the PDF extractor Lambda with a test payload.
#
# Usage:
#   ./scripts/invoke.sh <bucket> <key> <ou> <product_part_number>
#
# Example:
#   ./scripts/invoke.sh dev-ieee-conference-cloud-bulk-uploads \
#       ieee/pending/STD-12345.pdf ieee STD-12345
#
set -euo pipefail

AWS_PROFILE="${AWS_PROFILE:-ieee-cc}"
AWS_REGION="${AWS_REGION:-us-east-1}"
LAMBDA_FUNCTION_NAME="ieee-cc-pdf-extractor"

BUCKET="${1:?Usage: invoke.sh <bucket> <key> <ou> <product_part_number>}"
KEY="${2:?}"
OU="${3:?}"
PART_NUMBER="${4:?}"

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
    --payload "${PAYLOAD}" \
    "${RESPONSE_FILE}"

echo ""
echo "==> Response:"
cat "${RESPONSE_FILE}" | python3 -m json.tool
