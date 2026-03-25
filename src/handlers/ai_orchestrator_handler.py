"""Lambda handler for AI Orchestrator.

Triggered by S3 ObjectCreated events on {ou}/pending/ prefix.
Reads .meta.json, routes to extraction/transcription + Bedrock, sends webhook.
On retriable failures, publishes to the DLQ for automatic reprocessing.
"""

from __future__ import annotations

import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

from src.common.dlq import build_dlq_message
from src.orchestrator.ai_orchestrator import AIOrchestrator

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_s3_client = boto3.client("s3")
_lambda_client = boto3.client("lambda")
_sqs_client = boto3.client("sqs")
_orchestrator = AIOrchestrator(
    s3_client=_s3_client,
    lambda_client=_lambda_client,
)

DLQ_QUEUE_URL = os.environ.get("DLQ_QUEUE_URL", "")


def handler(event: dict, context) -> dict:
    """Lambda entry point."""
    request_id = ""
    if context and hasattr(context, "aws_request_id"):
        request_id = context.aws_request_id

    retry_count = event.get("retry_count", 0)

    try:
        bucket, key = _parse_event(event)
    except (KeyError, ValueError) as exc:
        logger.error("Bad request: %s", exc)
        return {"statusCode": 400, "body": {"error": str(exc)}}

    try:
        result = _orchestrator.process(
            bucket=bucket,
            key=key,
            request_id=request_id,
        )
    except ValueError as exc:
        logger.error("Validation error: %s", exc)
        return {"statusCode": 400, "body": {"error": str(exc)}}
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        msg = exc.response["Error"]["Message"]
        logger.error("AWS error (%s): %s", code, msg)
        _publish_to_dlq(event, exc, request_id, retry_count)
        return {"statusCode": 500, "body": {"error": f"{code}: {msg}"}}
    except RuntimeError as exc:
        logger.error("Processing error: %s", exc)
        _publish_to_dlq(event, exc, request_id, retry_count)
        return {"statusCode": 500, "body": {"error": str(exc)}}
    except Exception as exc:
        logger.exception("Unexpected error")
        _publish_to_dlq(event, exc, request_id, retry_count)
        return {
            "statusCode": 500,
            "body": {"error": f"Internal error: {type(exc).__name__}"},
        }

    return {
        "statusCode": 200,
        "body": {
            "item_id": result["item_id"],
            "ou": result["ou"],
            "action": result["action"],
            "ai_enrichment_enabled": result["ai_enrichment_enabled"],
            "source_key": result["source_key"],
            "destination_key": result["destination_key"],
            "processing_time_ms": result["processing_time_ms"],
        },
    }


def _publish_to_dlq(
    event: dict, exc: Exception, correlation_id: str, retry_count: int
) -> None:
    """Publish a failed event to the DLQ for reprocessing."""
    if not DLQ_QUEUE_URL:
        logger.warning("DLQ_QUEUE_URL not set — skipping DLQ publish")
        return

    try:
        message = build_dlq_message(event, exc, correlation_id, retry_count)
        _sqs_client.send_message(
            QueueUrl=DLQ_QUEUE_URL,
            MessageBody=json.dumps(message, default=str),
        )
        logger.info("Published failed event to DLQ (retry_count=%d)", retry_count)
    except Exception as dlq_exc:
        logger.error("Failed to publish to DLQ: %s", dlq_exc)


def _parse_event(event: dict) -> tuple[str, str]:
    """Extract bucket and key from event.

    Supports:
        1. S3 event: {Records[0].s3...}
        2. Direct: {bucket, key}
    """
    if "Records" in event:
        record = event["Records"][0]
        bucket = record["s3"]["bucket"]["name"]
        key = record["s3"]["object"]["key"]
    elif "bucket" in event and "key" in event:
        bucket = event["bucket"]
        key = event["key"]
    else:
        raise KeyError(
            "Event must contain 'Records' (S3 trigger) or 'bucket'/'key' (direct)"
        )

    # Validate key pattern
    parts = key.split("/")
    if len(parts) < 3 or parts[1] != "pending":
        raise ValueError(
            f"Key does not match '{{ou}}/pending/{{filename}}': {key}"
        )

    return bucket, key
