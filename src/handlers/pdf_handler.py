"""Lambda handler for PDF text extraction.

Supports two invocation patterns:
1. Direct / orchestrator: event contains bucket, key, ou, product_part_number
2. S3 event trigger: event contains Records[].s3 — derives ou and part number from key
"""

from __future__ import annotations

import logging
import os
from pathlib import PurePosixPath

import boto3
from botocore.exceptions import ClientError

from src.extractors.pdf_extractor import PDFExtractor

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_s3_client = boto3.client("s3")
_extractor = PDFExtractor(s3_client=_s3_client)


def handler(event: dict, context) -> dict:
    """Lambda entry point."""
    try:
        params = _parse_event(event)
    except (KeyError, ValueError) as exc:
        logger.error("Bad request: %s", exc)
        return {"statusCode": 400, "body": {"error": str(exc)}}

    try:
        result = _extractor.extract(**params)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        msg = exc.response["Error"]["Message"]
        logger.error("S3 error (%s): %s", code, msg)
        return {"statusCode": 500, "body": {"error": f"S3 {code}: {msg}"}}
    except Exception as exc:
        logger.exception("Unexpected error during extraction")
        return {"statusCode": 500, "body": {"error": f"Internal error: {type(exc).__name__}"}}

    return {
        "statusCode": 200,
        "body": {
            "text": result["text"],
            "page_count": result["page_count"],
            "extraction_method": result["extraction_method"],
        },
    }


def _parse_event(event: dict) -> dict:
    """Extract bucket/key/ou/product_part_number from the event.

    Raises KeyError or ValueError if required fields are missing.
    """
    if "Records" in event:
        return _parse_s3_event(event)
    return _parse_direct_event(event)


def _parse_direct_event(event: dict) -> dict:
    """Parse orchestrator invocation: all fields provided explicitly."""
    missing = [
        f for f in ("bucket", "key", "ou", "product_part_number") if f not in event
    ]
    if missing:
        raise KeyError(f"Missing required fields: {', '.join(missing)}")
    return {
        "bucket": event["bucket"],
        "key": event["key"],
        "ou": event["ou"],
        "product_part_number": event["product_part_number"],
    }


def _parse_s3_event(event: dict) -> dict:
    """Parse S3 event notification.

    Derives ou and product_part_number from the key pattern:
        {ou}/pending/{product_part_number}.pdf
    """
    record = event["Records"][0]
    bucket = record["s3"]["bucket"]["name"]
    key = record["s3"]["object"]["key"]

    parts = PurePosixPath(key)
    # Expected: {ou}/pending/{filename}.pdf
    path_parts = parts.parts
    if len(path_parts) < 3 or path_parts[-2] != "pending":
        raise ValueError(
            f"S3 key does not match expected pattern '{{ou}}/pending/{{filename}}.pdf': {key}"
        )

    ou = path_parts[0]
    product_part_number = parts.stem  # filename without .pdf

    return {
        "bucket": bucket,
        "key": key,
        "ou": ou,
        "product_part_number": product_part_number,
    }
