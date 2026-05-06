"""Lambda entry point for the bulk processor (manifest dispatcher)."""

from __future__ import annotations

from src.common.error_handler import build_error_response
from src.common.logging import get_json_logger
from src.bulk.bulk_processor import BulkProcessor, DEFAULT_BUCKET

logger = get_json_logger(__name__)

MANIFEST_PREFIX = "bulk/manifests/"
MANIFEST_SUFFIX = ".json"

# Module-level singleton — reuses boto3 clients across warm invocations.
processor = BulkProcessor()


def handler(event: dict, context) -> dict:
    """Process a batch manifest and publish items to SQS.

    Supports two invocation shapes:

    1. Direct invocation (operator replay, ``invoke-bulk-processor.sh``)::

           {"batch_id": "bulk-2026-03-17"}
           {"batch_id": "bulk-2026-03-17", "bucket": "custom-bucket"}

    2. S3 event notification on ``bulk/manifests/*.json``::

           {"Records": [{"s3": {"bucket": {"name": "..."},
                                "object": {"key": "bulk/manifests/<batch_id>.json"}}}]}

    Returns:
        Dict with ``statusCode`` and ``body`` containing dispatch results.
    """
    try:
        bucket, batch_id = _parse_event(event)
    except (KeyError, ValueError) as exc:
        logger.error("Bad request: %s", exc)
        return {"statusCode": 400, "body": {"error": str(exc)}}

    try:
        result = processor.process_manifest(bucket=bucket, batch_id=batch_id)
    except Exception as exc:
        logger.error("Bulk processing failed for batch %s: %s", batch_id, exc)
        return build_error_response(exc)

    return {
        "statusCode": 200,
        "body": {
            "batch_id": result["batch_id"],
            "total_items": result["total_items"],
            "published_count": result["published_count"],
            "estimated_cost": result["estimated_cost"],
            "status": result["status"],
        },
    }


def _parse_event(event: dict) -> tuple[str, str]:
    """Extract (bucket, batch_id) from either an S3 event or direct payload."""
    if "Records" in event:
        record = event["Records"][0]
        bucket = record["s3"]["bucket"]["name"]
        key = record["s3"]["object"]["key"]

        if not key.startswith(MANIFEST_PREFIX) or not key.endswith(MANIFEST_SUFFIX):
            raise ValueError(
                f"Key does not match expected pattern "
                f"'{MANIFEST_PREFIX}*{MANIFEST_SUFFIX}': {key}"
            )

        batch_id = key[len(MANIFEST_PREFIX):-len(MANIFEST_SUFFIX)]
        if not batch_id:
            raise ValueError(f"Cannot derive batch_id from key: {key}")

        return bucket, batch_id

    batch_id = event.get("batch_id")
    if not batch_id:
        raise KeyError("Missing required field: batch_id")

    bucket = event.get("bucket", DEFAULT_BUCKET)
    return bucket, batch_id
