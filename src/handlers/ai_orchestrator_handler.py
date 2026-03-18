"""Lambda handler for AI Orchestrator.

Triggered by S3 ObjectCreated events on {ou}/pending/ prefix.
Reads .meta.json, routes to extraction/transcription + Bedrock, sends webhook.
"""

from __future__ import annotations

import logging
import os

import boto3
from botocore.exceptions import ClientError

from src.orchestrator.ai_orchestrator import AIOrchestrator

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_s3_client = boto3.client("s3")
_lambda_client = boto3.client("lambda")
_orchestrator = AIOrchestrator(
    s3_client=_s3_client,
    lambda_client=_lambda_client,
)


def handler(event: dict, context) -> dict:
    """Lambda entry point."""
    request_id = ""
    if context and hasattr(context, "aws_request_id"):
        request_id = context.aws_request_id

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
        return {"statusCode": 500, "body": {"error": f"{code}: {msg}"}}
    except RuntimeError as exc:
        logger.error("Processing error: %s", exc)
        return {"statusCode": 500, "body": {"error": str(exc)}}
    except Exception as exc:
        logger.exception("Unexpected error")
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
