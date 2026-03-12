#!/usr/bin/env bash
#
# Invoke the Image Overlay Generator Lambda with a test payload.
#
# Usage:
#   ./scripts/invoke-image-overlay.sh <bucket> <key>
#
# Example:
#   ./scripts/invoke-image-overlay.sh dev-ieee-conference-cloud-bulk-uploads actions/test-job.json
#
set -euo pipefail

AWS_PROFILE="${AWS_PROFILE:-ieee-cc}"
AWS_REGION="${AWS_REGION:-us-east-1}"
LAMBDA_FUNCTION_NAME="ieee-rc-image-generator"

BUCKET="${1:?Usage: invoke-image-overlay.sh <bucket> <key>}"
KEY="${2:?}"

PAYLOAD=$(cat <<EOF
{
  "bucket": "${BUCKET}",
  "key": "${KEY}"
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
