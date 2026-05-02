"""Lambda handler for the Wizard Async Transfer (CC3-898).

Triggers on s3:ObjectCreated:* events for the transfer-actions/*.json prefix
on the metadata-json bucket. Reads the trigger JSON, streams the source
(Drive or URL) into S3 via multipart upload, and POSTs an HMAC-signed
webhook callback to Drupal.

The handler ALWAYS returns 200 if Lambda completed (whether the transfer
succeeded or hit a terminal source/dest error) — terminal errors are
surfaced via the webhook callback's error_code field. 500 is reserved for
genuinely unexpected failures, which Lambda routes to the SQS DLQ via
async dead-letter config.
"""

from __future__ import annotations

from botocore.exceptions import ClientError

from src.common.logging import get_json_logger
from src.transfer.wizard_transfer import WizardTransfer

logger = get_json_logger(__name__)

_transfer = WizardTransfer()


def handler(event: dict, context) -> dict:
    """Lambda entry point."""
    try:
        bucket, key = _parse_event(event)
    except (KeyError, ValueError) as exc:
        logger.error("Bad request: %s", exc)
        return {"statusCode": 400, "body": {"error": str(exc)}}

    try:
        result = _transfer.process_trigger(bucket=bucket, key=key)
    except ValueError as exc:
        # Trigger validation failure (bad/missing fields, malformed JSON).
        # Drupal's webhook never fires here — there's no valid callback_url
        # to POST to. Log loudly; the trigger remains in S3 for inspection.
        logger.error("Trigger validation failed: %s", exc)
        return {"statusCode": 400, "body": {"error": str(exc)}}
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        msg = exc.response["Error"]["Message"]
        logger.error("S3 error (%s): %s", code, msg)
        return {"statusCode": 500, "body": {"error": f"S3 {code}: {msg}"}}
    except Exception as exc:
        logger.exception("Unexpected error during transfer")
        return {
            "statusCode": 500,
            "body": {"error": f"Internal error: {type(exc).__name__}"},
        }

    return {
        "statusCode": 200,
        "body": {
            "status": result["status"],
            "bytes_transferred": result["bytes_transferred"],
            "error_code": result.get("error_code"),
            "s3_etag": result.get("s3_etag"),
            "webhook_delivered": result["webhook_delivered"],
        },
    }


def _parse_event(event: dict) -> tuple[str, str]:
    """Extract bucket and key from the event.

    Supports both S3 event notifications and direct invocations
    ({"bucket": ..., "key": ...}). Returns (bucket, key).
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

    if not key.startswith("transfer-actions/") or not key.endswith(".json"):
        raise ValueError(
            f"Key does not match expected pattern 'transfer-actions/*.json': {key}"
        )

    return bucket, key
