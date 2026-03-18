#!/usr/bin/env bash
#
# Invoke the Video Transcriber Lambda.
#
# Usage:
#   ./scripts/invoke-video-transcriber.sh <bucket> <key> <ou> <product_part_number>
#
# Example:
#   ./scripts/invoke-video-transcriber.sh dev-ieee-conference-cloud-bulk-uploads \
#       PES/pending/lecture.mp4 PES LECTURE-001
#
set -euo pipefail

AWS_PROFILE="${AWS_PROFILE:-ieee-cc}"
AWS_REGION="${AWS_REGION:-us-east-1}"
LAMBDA_FUNCTION_NAME="ieee-cc-video-transcriber"

export AWS_PROFILE AWS_REGION

if [[ $# -lt 4 ]]; then
    echo "Usage: $0 <bucket> <key> <ou> <product_part_number>"
    echo "  Example: $0 dev-ieee-conference-cloud-bulk-uploads PES/pending/lecture.mp4 PES LECTURE-001"
    exit 1
fi

BUCKET="$1"
KEY="$2"
OU="$3"
PART_NUMBER="$4"

PAYLOAD="{\"bucket\":\"${BUCKET}\",\"key\":\"${KEY}\",\"ou\":\"${OU}\",\"product_part_number\":\"${PART_NUMBER}\"}"

OUTPUT_FILE=$(mktemp)

echo "==> Invoking ${LAMBDA_FUNCTION_NAME}"
echo "    Bucket: ${BUCKET}"
echo "    Key:    ${KEY}"
echo "    OU:     ${OU}"
echo "    Part#:  ${PART_NUMBER}"
echo ""

aws lambda invoke \
    --function-name "${LAMBDA_FUNCTION_NAME}" \
    --region "${AWS_REGION}" \
    --payload "${PAYLOAD}" \
    --cli-read-timeout 900 \
    "${OUTPUT_FILE}"

echo ""
echo "==> Response:"
python3 -m json.tool "${OUTPUT_FILE}"
rm -f "${OUTPUT_FILE}"
