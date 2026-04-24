"""Core DLQ processing logic — retry or archive failed pipeline events."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import boto3

from src.common.logging import get_json_logger

logger = get_json_logger(__name__)


class DLQProcessor:
    """Processes messages from the pipeline dead-letter queue.

    For retriable errors below the reprocess limit, re-invokes the
    orchestrator Lambda.  Otherwise archives the message to S3 and
    publishes an SNS alert.
    """

    MAX_REPROCESS_ATTEMPTS = 2

    def __init__(self, lambda_client=None, s3_client=None, sns_client=None):
        self._lambda = lambda_client or boto3.client("lambda")
        self._s3 = s3_client or boto3.client("s3")
        self._sns = sns_client or boto3.client("sns")

    def process_message(self, sqs_record: dict) -> dict:
        """Process a single SQS record from the DLQ.

        Args:
            sqs_record: An SQS event record with a JSON ``body``.

        Returns:
            Dict with ``action`` key (``"reprocessed"`` or ``"archived"``).
        """
        try:
            message = json.loads(sqs_record["body"])
        except (json.JSONDecodeError, KeyError) as exc:
            logger.error("Invalid DLQ message format: %s", exc)
            message = {
                "original_event": {},
                "error": {
                    "error_type": "InvalidMessage",
                    "error_message": f"Failed to parse DLQ message: {exc}",
                    "is_retriable": False,
                    "correlation_id": "",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "stack_trace": "",
                },
                "retry_count": self.MAX_REPROCESS_ATTEMPTS,
            }

        error = message.get("error", {})
        correlation_id = error.get("correlation_id", "")

        try:
            retry_count = int(message.get("retry_count", 0))
        except (TypeError, ValueError):
            retry_count = self.MAX_REPROCESS_ATTEMPTS

        if self._is_retriable(error) and retry_count < self.MAX_REPROCESS_ATTEMPTS:
            logger.info(
                "Reprocessing message (attempt %d): %s",
                retry_count + 1,
                correlation_id,
            )
            return self._reprocess(message, retry_count)

        logger.info(
            "Archiving permanently failed message: %s",
            correlation_id,
        )
        return self._archive_and_notify(message, retry_count)

    @staticmethod
    def _is_retriable(error: dict) -> bool:
        """Check whether the error is retriable using the message flag."""
        return error.get("is_retriable", False) is True

    @staticmethod
    def _extract_source_bucket(original_event: dict) -> str | None:
        """Extract the source S3 bucket from the original pipeline event.

        Supports both orchestrator entry shapes:
        - S3 trigger: ``{"Records": [{"s3": {"bucket": {"name": ...}}}]}``
        - Direct invoke: ``{"bucket": "...", ...}``

        Returns the bucket name, or ``None`` if neither shape is present.
        """
        if not isinstance(original_event, dict):
            return None

        records = original_event.get("Records")
        if isinstance(records, list) and records:
            try:
                name = records[0]["s3"]["bucket"]["name"]
                if isinstance(name, str) and name:
                    return name
            except (KeyError, TypeError):
                pass

        bucket = original_event.get("bucket")
        if isinstance(bucket, str) and bucket:
            return bucket

        return None

    def _reprocess(self, message: dict, retry_count: int) -> dict:
        """Re-invoke the orchestrator Lambda with the original event."""
        function_name = os.environ.get(
            "ORCHESTRATOR_FUNCTION_NAME", "ieee-rc-ai-orchestrator"
        )
        original_event = message.get("original_event", {})

        payload = {
            **original_event,
            "is_retry": True,
            "retry_count": retry_count + 1,
        }

        self._lambda.invoke(
            FunctionName=function_name,
            InvocationType="Event",
            Payload=json.dumps(payload).encode(),
        )

        logger.info(
            "Re-invoked %s with retry_count=%d",
            function_name,
            retry_count + 1,
        )
        return {"action": "reprocessed"}

    def _archive_and_notify(self, message: dict, retry_count: int) -> dict:
        """Archive the failed message to S3 and publish an SNS alert.

        The archive bucket is derived from the original pipeline event so
        staging failures archive under ``staging-...`` and dev failures
        archive under ``dev-...``. ``ARCHIVE_BUCKET`` env var is a fallback
        only — used when the event shape lacks a bucket.
        """
        error = message.get("error", {})
        correlation_id = error.get("correlation_id", "") or "unknown"
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        bucket = self._extract_source_bucket(message.get("original_event", {}))
        if not bucket:
            bucket = os.environ.get("ARCHIVE_BUCKET")
        if not bucket:
            raise KeyError(
                "Unable to resolve archive bucket: original_event has no "
                "bucket and ARCHIVE_BUCKET env var is not set"
            )

        key = f"failed/{correlation_id}/{timestamp}.json"

        self._s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(message, default=str).encode(),
            ContentType="application/json",
        )
        logger.info("Archived failed message to s3://%s/%s", bucket, key)

        topic_arn = os.environ.get("FAILURES_SNS_TOPIC_ARN")
        if topic_arn:
            summary = {
                "correlation_id": correlation_id,
                "error_type": error.get("error_type", ""),
                "error_message": error.get("error_message", ""),
                "retry_count": retry_count,
                "archive_key": key,
            }
            self._sns.publish(
                TopicArn=topic_arn,
                Subject="Pipeline processing failure",
                Message=json.dumps(summary, default=str),
            )
            logger.info("Published failure notification to SNS")
        else:
            logger.warning("FAILURES_SNS_TOPIC_ARN not set — skipping SNS alert")

        return {"action": "archived"}
