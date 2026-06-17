#!/usr/bin/env bash
#
# Invoke the Wizard Async Transfer Lambda directly with a test payload
# (bypasses the S3 trigger). Use for end-to-end smoke testing in dev.
#
# Usage:
#   ./scripts/invoke-wizard-transfer.sh <bucket> <key>
#
# Where <key> points at an existing transfer-actions/*.json trigger in
# <bucket>. Example:
#   ./scripts/invoke-wizard-transfer.sh \
#     dev-ieee-conference-cloud-bulk-uploads \
#     transfer-actions/req-smoke1-item1-transfer_media.json
#
set -euo pipefail

AWS_PROFILE="${AWS_PROFILE:-ieee-cc}"
AWS_REGION="${AWS_REGION:-us-east-1}"
LAMBDA_FUNCTION_NAME="ieee-rc-wizard-transfer"

BUCKET="${1:?Usage: invoke-wizard-transfer.sh <bucket> <key>}"
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
    --cli-binary-format raw-in-base64-out \
    "${RESPONSE_FILE}"

echo ""
echo "==> Response:"
cat "${RESPONSE_FILE}" | python3 -m json.tool
