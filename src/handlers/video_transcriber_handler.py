"""Lambda handler for Video Transcription.

Accepts direct invocation with bucket, key, ou, and product_part_number,
or an S3 event trigger on {ou}/pending/*.{mp4|mov|webm}.
"""

from __future__ import annotations

import logging
import os
import re

import boto3
from botocore.exceptions import ClientError

from src.extractors.video_transcriber import VideoTranscriber

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_s3_client = boto3.client("s3")
_transcribe_client = boto3.client("transcribe")
_bedrock_client = boto3.client("bedrock-runtime", region_name="us-east-1")
_cloudwatch_client = boto3.client("cloudwatch")
_transcriber = VideoTranscriber(
    s3_client=_s3_client,
    transcribe_client=_transcribe_client,
    bedrock_client=_bedrock_client,
    cloudwatch_client=_cloudwatch_client,
)

# Pattern: {ou}/pending/{filename}.{mp4|mov|webm}
_S3_KEY_PATTERN = re.compile(r"^([^/]+)/pending/([^/]+)\.(mp4|mov|webm)$", re.IGNORECASE)


def handler(event: dict, context) -> dict:
    """Lambda entry point."""
    try:
        bucket, key, ou, product_part_number = _parse_event(event)
    except (KeyError, ValueError) as exc:
        logger.error("Bad request: %s", exc)
        return {"statusCode": 400, "body": {"error": str(exc)}}

    clean_transcript = event.get("clean_transcript", True)

    try:
        result = _transcriber.transcribe(
            bucket=bucket,
            key=key,
            ou=ou,
            product_part_number=product_part_number,
            clean_transcript=clean_transcript,
        )
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        msg = exc.response["Error"]["Message"]
        logger.error("AWS error (%s): %s", code, msg)
        return {"statusCode": 500, "body": {"error": f"{code}: {msg}"}}
    except ValueError as exc:
        logger.error("Validation error: %s", exc)
        return {"statusCode": 400, "body": {"error": str(exc)}}
    except (TimeoutError, RuntimeError) as exc:
        logger.error("Transcription error: %s", exc)
        return {"statusCode": 500, "body": {"error": str(exc)}}
    except Exception as exc:
        logger.exception("Unexpected error during transcription")
        return {
            "statusCode": 500,
            "body": {"error": f"Internal error: {type(exc).__name__}"},
        }

    return {
        "statusCode": 200,
        "body": {
            "transcript": result["transcript"],
            "duration": result["duration"],
            "duration_seconds": result["duration_seconds"],
            "speaker_count": result["speaker_count"],
        },
    }


def _parse_event(event: dict) -> tuple[str, str, str, str]:
    """Extract bucket, key, ou, product_part_number from the event.

    Supports:
        1. Direct: {bucket, key, ou, product_part_number}
        2. S3 event: {Records[0].s3...} — derives ou and part number from key
    """
    if "Records" in event:
        record = event["Records"][0]
        bucket = record["s3"]["bucket"]["name"]
        key = record["s3"]["object"]["key"]
        match = _S3_KEY_PATTERN.match(key)
        if not match:
            raise ValueError(
                f"Key does not match pattern "
                f"'{{ou}}/pending/{{file}}.{{mp4|mov|webm}}': {key}"
            )
        ou = match.group(1)
        product_part_number = match.group(2)
    elif "bucket" in event and "key" in event:
        bucket = event["bucket"]
        key = event["key"]
        ou = event.get("ou")
        product_part_number = event.get("product_part_number")
        if not ou or not product_part_number:
            match = _S3_KEY_PATTERN.match(key)
            if match:
                ou = ou or match.group(1)
                product_part_number = product_part_number or match.group(2)
            else:
                raise ValueError(
                    "Must provide 'ou' and 'product_part_number' or use key "
                    "pattern '{ou}/pending/{file}.{mp4|mov|webm}'"
                )
    else:
        raise KeyError(
            "Event must contain 'Records' (S3 trigger) or 'bucket'/'key' (direct)"
        )

    return bucket, key, ou, product_part_number
