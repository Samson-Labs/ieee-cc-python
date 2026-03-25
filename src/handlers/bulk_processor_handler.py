"""Lambda entry point for the bulk processor (manifest dispatcher)."""

from __future__ import annotations

from src.common.error_handler import build_error_response
from src.common.logging import get_json_logger
from src.bulk.bulk_processor import BulkProcessor, DEFAULT_BUCKET

logger = get_json_logger(__name__)

# Module-level singleton — reuses boto3 clients across warm invocations.
processor = BulkProcessor()


def handler(event: dict, context) -> dict:
    """Process a batch manifest and publish items to SQS.

    Expects direct invocation with::

        {"batch_id": "bulk-2026-03-17"}
        {"batch_id": "bulk-2026-03-17", "bucket": "custom-bucket"}

    Returns:
        Dict with ``statusCode`` and ``body`` containing dispatch results.
    """
    batch_id = event.get("batch_id")
    if not batch_id:
        return {"statusCode": 400, "body": {"error": "Missing required field: batch_id"}}

    bucket = event.get("bucket", DEFAULT_BUCKET)

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
