#!/usr/bin/env bash
#
# Invoke the AI Orchestrator Lambda.
#
# Usage:
#   ./scripts/invoke-ai-orchestrator.sh <bucket> <key>
#
# Example:
#   ./scripts/invoke-ai-orchestrator.sh dev-ieee-conference-cloud-bulk-uploads \
#       PES/pending/STD-12345.pdf
#
set -euo pipefail

AWS_PROFILE="${AWS_PROFILE:-ieee-cc}"
AWS_REGION="${AWS_REGION:-us-east-1}"
LAMBDA_FUNCTION_NAME="ieee-rc-ai-orchestrator"

export AWS_PROFILE AWS_REGION

if [[ $# -lt 2 ]]; then
    echo "Usage: $0 <bucket> <key>"
    echo "  Example: $0 dev-ieee-conference-cloud-bulk-uploads PES/pending/STD-12345.pdf"
    exit 1
fi

BUCKET="$1"
KEY="$2"

PAYLOAD="{\"bucket\":\"${BUCKET}\",\"key\":\"${KEY}\"}"

OUTPUT_FILE=$(mktemp)

echo "==> Invoking ${LAMBDA_FUNCTION_NAME}"
echo "    Bucket: ${BUCKET}"
echo "    Key:    ${KEY}"
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
