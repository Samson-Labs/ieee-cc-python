"""Lambda entry point for the bulk worker (SQS-triggered, per-item)."""

from __future__ import annotations

from src.common.error_handler import build_error_response
from src.common.logging import get_json_logger
from src.bulk.bulk_worker import BulkWorker

logger = get_json_logger(__name__)

# Module-level singleton — reuses boto3 clients across warm invocations.
worker = BulkWorker()


def handler(event: dict, context) -> dict:
    """Process an SQS batch of bulk items.

    Supports partial batch failure via ``batchItemFailures``.

    Args:
        event: SQS event with ``Records`` list.
        context: Lambda context (unused).

    Returns:
        Dict with ``batchItemFailures`` for any records that could not
        be processed, plus a ``results`` summary.
    """
    records = event.get("Records", [])

    results = []
    batch_item_failures = []

    for record in records:
        message_id = record.get("messageId", "unknown")
        try:
            result = worker.process_item(record)
            results.append({"messageId": message_id, **result})
            logger.info("Processed bulk item %s: %s", message_id, result["action"])
        except Exception as exc:
            logger.error("Failed to process bulk item %s: %s", message_id, exc)
            batch_item_failures.append({"itemIdentifier": message_id})
            results.append({
                "messageId": message_id,
                **build_error_response(exc),
            })

    return {
        "batchItemFailures": batch_item_failures,
        "results": results,
    }
