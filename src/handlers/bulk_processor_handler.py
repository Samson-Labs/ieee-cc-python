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

    2. S3 event notification on ``bulk/manifests/*.json``. May contain
       multiple records if S3 batches notifications::

           {"Records": [{"s3": {"bucket": {"name": "..."},
                                "object": {"key": "bulk/manifests/<batch_id>.json"}}},
                        ...]}

    Returns:
        Dict with ``statusCode`` and ``body``. For direct invocation, body
        contains the dispatch result. For S3 events, body contains a
        ``results`` list (one entry per record).
    """
    if "Records" in event:
        return _handle_s3_event(event)
    return _handle_direct(event)


def _handle_direct(event: dict) -> dict:
    """Direct-invocation path. Single manifest, detailed response."""
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


def _handle_s3_event(event: dict) -> dict:
    """S3-event path. Iterate all records; never raise.

    Lambda retries the *entire* event on raise, which would re-dispatch
    already-succeeded manifests (process_manifest publishes to SQS — not
    idempotent). Per-record failures are logged and returned in the
    response; operators replay via invoke-bulk-processor.sh if needed.
    """
    results = []
    for record in event["Records"]:
        try:
            bucket, batch_id, key = _parse_s3_record(record)
        except (KeyError, ValueError) as exc:
            logger.error("Skipping malformed S3 record: %s", exc)
            results.append({"status": "skipped", "error": str(exc)})
            continue

        try:
            result = processor.process_manifest(bucket=bucket, batch_id=batch_id)
            results.append({
                "key": key,
                "batch_id": result["batch_id"],
                "total_items": result["total_items"],
                "published_count": result["published_count"],
                "status": result["status"],
            })
            logger.info(
                "Dispatched batch %s (%d items)",
                result["batch_id"],
                result["published_count"],
            )
        except Exception as exc:
            logger.error("Bulk processing failed for %s: %s", key, exc)
            results.append({
                "key": key,
                "status": "failed",
                **build_error_response(exc).get("body", {"error": str(exc)}),
            })

    return {"statusCode": 200, "body": {"results": results}}


def _parse_s3_record(record: dict) -> tuple[str, str, str]:
    """Extract (bucket, batch_id, key) from a single S3 event record.

    Raises:
        KeyError: if the record shape is malformed.
        ValueError: if the key does not match the expected pattern.
    """
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

    return bucket, batch_id, key
