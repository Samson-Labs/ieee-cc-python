"""Lambda handler for Image Overlay Generation.

Triggers on s3:ObjectCreated:* events for the actions/*.json prefix.
Reads the trigger JSON, generates an overlay image, and writes output
to the destination bucket.
"""

from __future__ import annotations

import logging
import os

import boto3
from botocore.exceptions import ClientError

from src.generators.image_overlay_generator import ImageOverlayGenerator

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_s3_client = boto3.client("s3")
_generator = ImageOverlayGenerator(s3_client=_s3_client)


def handler(event: dict, context) -> dict:
    """Lambda entry point."""
    try:
        bucket, key = _parse_event(event)
    except (KeyError, ValueError) as exc:
        logger.error("Bad request: %s", exc)
        return {"statusCode": 400, "body": {"error": str(exc)}}

    try:
        result = _generator.process_trigger(bucket=bucket, key=key)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        msg = exc.response["Error"]["Message"]
        logger.error("S3 error (%s): %s", code, msg)
        return {"statusCode": 500, "body": {"error": f"S3 {code}: {msg}"}}
    except ValueError as exc:
        logger.error("Validation error: %s", exc)
        return {"statusCode": 400, "body": {"error": str(exc)}}
    except Exception as exc:
        logger.exception("Unexpected error during image generation")
        return {
            "statusCode": 500,
            "body": {"error": f"Internal error: {type(exc).__name__}"},
        }

    return {
        "statusCode": 200,
        "body": {
            "output_key": result["output_key"],
            "thumbnail_key": result["thumbnail_key"],
            "width": result["width"],
            "height": result["height"],
            "format": result["format"],
        },
    }


def _parse_event(event: dict) -> tuple[str, str]:
    """Extract bucket and key from the event.

    Supports both S3 event notifications and direct invocations.
    Returns (bucket, key).
    """
    if "Records" in event:
        record = event["Records"][0]
        bucket = record["s3"]["bucket"]["name"]
        key = record["s3"]["object"]["key"]
    elif "bucket" in event and "key" in event:
        bucket = event["bucket"]
        key = event["key"]
    else:
        raise KeyError("Event must contain 'Records' (S3 trigger) or 'bucket'/'key' (direct)")

    if not key.startswith("actions/") or not key.endswith(".json"):
        raise ValueError(
            f"Key does not match expected pattern 'actions/*.json': {key}"
        )

    return bucket, key
