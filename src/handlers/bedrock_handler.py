"""Lambda handler for Bedrock Claude metadata generation.

Supports two invocation patterns:
1. Direct / orchestrator: event contains text (and optional thesaurus_terms)
2. S3 metadata event: event contains bucket and key pointing to a metadata JSON
   file that has an "extractedText" field
"""

from __future__ import annotations

import json
import logging
import os

import boto3
from botocore.exceptions import ClientError

from src.ai.bedrock_inference import ALL_FIELDS, BedrockInference

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_bedrock_client = boto3.client("bedrock-runtime", region_name="us-east-1")
_s3_client = boto3.client("s3")
_cloudwatch_client = boto3.client("cloudwatch")
_inference = BedrockInference(
    bedrock_client=_bedrock_client,
    cloudwatch_client=_cloudwatch_client,
)


def handler(event: dict, context) -> dict:
    """Lambda entry point."""
    try:
        text, thesaurus_terms, requested_fields = _parse_event(event)
    except (KeyError, ValueError) as exc:
        logger.error("Bad request: %s", exc)
        return {"statusCode": 400, "body": {"error": str(exc)}}

    try:
        result = _inference.generate_metadata(
            text=text,
            thesaurus_terms=thesaurus_terms,
            requested_fields=requested_fields,
        )
    except ValueError as exc:
        logger.error("Validation error: %s", exc)
        return {"statusCode": 422, "body": {"error": str(exc)}}
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        msg = exc.response["Error"]["Message"]
        logger.error("Bedrock error (%s): %s", code, msg)
        return {"statusCode": 500, "body": {"error": f"Bedrock {code}: {msg}"}}
    except Exception as exc:
        logger.exception("Unexpected error during inference")
        return {
            "statusCode": 500,
            "body": {"error": f"Internal error: {type(exc).__name__}"},
        }

    return {"statusCode": 200, "body": dict(result)}  # includes input_tokens, output_tokens


def _parse_event(event: dict) -> tuple[str, list[str] | None, frozenset[str] | None]:
    """Extract text, optional thesaurus_terms, and optional requested_fields.

    Returns:
        (text, thesaurus_terms, requested_fields) tuple.

    Raises:
        KeyError/ValueError if required fields are missing or invalid.
    """
    requested_fields = _parse_requested_fields(event)

    # Direct invocation — text provided in event
    if "text" in event:
        text = event["text"]
        if not text or not text.strip():
            raise ValueError("text must be a non-empty string")
        thesaurus_terms = event.get("thesaurus_terms")
        return text, thesaurus_terms, requested_fields

    # S3 metadata reference — read text from S3 JSON
    if "bucket" in event and "key" in event:
        resp = _s3_client.get_object(Bucket=event["bucket"], Key=event["key"])
        metadata = json.loads(resp["Body"].read())
        text = metadata.get("extractedText", "")
        if not text or not text.strip():
            raise ValueError(
                f"No extractedText in s3://{event['bucket']}/{event['key']}"
            )
        thesaurus_terms = event.get("thesaurus_terms")
        return text, thesaurus_terms, requested_fields

    raise KeyError("Event must contain 'text' or 'bucket'+'key'")


def _parse_requested_fields(event: dict) -> frozenset[str] | None:
    """Parse and validate requested_fields from event."""
    raw = event.get("requested_fields")
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        raise ValueError("requested_fields must be a non-empty array")
    if any(not isinstance(field, str) for field in raw):
        raise ValueError("requested_fields must contain only strings")
    invalid = set(raw) - ALL_FIELDS
    if invalid:
        raise ValueError(f"Invalid requested_fields: {sorted(invalid)}")
    return frozenset(raw)
