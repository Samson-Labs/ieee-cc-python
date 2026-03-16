#!/usr/bin/env bash
#
# Invoke the Bedrock Metadata Generation Lambda manually.
#
# Usage:
#   ./scripts/invoke-bedrock.sh <bucket> <key>        # S3 metadata reference
#   ./scripts/invoke-bedrock.sh --text "extracted text" # Direct text invocation
#
set -euo pipefail

AWS_PROFILE="${AWS_PROFILE:-ieee-cc}"
AWS_REGION="${AWS_REGION:-us-east-1}"
LAMBDA_FUNCTION_NAME="ieee-cc-bedrock-inference"

export AWS_PROFILE AWS_REGION

if [[ "${1:-}" == "--text" ]]; then
    TEXT="${2:?Usage: $0 --text \"extracted text\"}"
    PAYLOAD="{\"text\": $(printf '%s' "${TEXT}" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')}"
else
    BUCKET="${1:?Usage: $0 <bucket> <key>}"
    KEY="${2:?Usage: $0 <bucket> <key>}"
    PAYLOAD="{\"bucket\": \"${BUCKET}\", \"key\": \"${KEY}\"}"
fi

echo "==> Invoking ${LAMBDA_FUNCTION_NAME}..."
echo "    Payload: ${PAYLOAD}"

aws lambda invoke \
    --function-name "${LAMBDA_FUNCTION_NAME}" \
    --region "${AWS_REGION}" \
    --payload "${PAYLOAD}" \
    /dev/stdout 2>/dev/null | python3 -m json.tool
