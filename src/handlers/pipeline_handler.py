"""Lambda handler for the Pipeline Orchestrator.

Accepts a direct invocation with bucket, key, ou, and product_part_number,
or an S3 event trigger on {ou}/pending/*.pdf.
"""

from __future__ import annotations

import logging
import os
import re

import boto3

from src.orchestrator.pipeline_orchestrator import PipelineOrchestrator

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_s3_client = boto3.client("s3")
_bedrock_client = boto3.client("bedrock-runtime", region_name="us-east-1")
_orchestrator = PipelineOrchestrator(
    s3_client=_s3_client,
    bedrock_client=_bedrock_client,
)

# Pattern: {ou}/pending/{filename}.pdf
_S3_KEY_PATTERN = re.compile(r"^([^/]+)/pending/([^/]+)\.pdf$")


def handler(event: dict, context) -> dict:
    """Lambda entry point."""
    try:
        bucket, key, ou, product_part_number = _parse_event(event)
    except (KeyError, ValueError) as exc:
        logger.error("Bad request: %s", exc)
        return {"statusCode": 400, "body": {"error": str(exc)}}

    thesaurus_terms = event.get("thesaurus_terms")

    try:
        result = _orchestrator.run(
            bucket=bucket,
            key=key,
            ou=ou,
            product_part_number=product_part_number,
            thesaurus_terms=thesaurus_terms,
        )
    except Exception as exc:
        logger.exception("Pipeline failed")
        return {
            "statusCode": 500,
            "body": {"error": f"{type(exc).__name__}: {exc}"},
        }

    return {
        "statusCode": 200,
        "body": {
            "text_length": result["text_length"],
            "page_count": result["page_count"],
            "extraction_method": result["extraction_method"],
            "abstract": result["abstract"],
            "keywords": result["keywords"],
            "learning_level": result["learning_level"],
            "intended_audience": result["intended_audience"],
            "category": result["category"],
            "enriched_metadata_key": result["enriched_metadata_key"],
            "pipeline_time_ms": result["pipeline_time_ms"],
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
                f"Key does not match pattern '{{ou}}/pending/{{file}}.pdf': {key}"
            )
        ou = match.group(1)
        product_part_number = match.group(2)
    elif "bucket" in event and "key" in event:
        bucket = event["bucket"]
        key = event["key"]
        ou = event.get("ou")
        product_part_number = event.get("product_part_number")
        if not ou or not product_part_number:
            # Try to derive from key
            match = _S3_KEY_PATTERN.match(key)
            if match:
                ou = ou or match.group(1)
                product_part_number = product_part_number or match.group(2)
            else:
                raise ValueError(
                    "Must provide 'ou' and 'product_part_number' or use key pattern "
                    "'{ou}/pending/{file}.pdf'"
                )
    else:
        raise KeyError(
            "Event must contain 'Records' (S3 trigger) or 'bucket'/'key' (direct)"
        )

    return bucket, key, ou, product_part_number
