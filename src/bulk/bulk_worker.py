"""Bulk worker — processes a single item from SQS via the orchestrator."""

from __future__ import annotations

import json
import os
import time
from typing import TypedDict

import boto3
from botocore.exceptions import ClientError

from src.common.exceptions import BulkProcessingError, ValidationError
from src.common.logging import get_json_logger

logger = get_json_logger(__name__)

DEFAULT_BUCKET = os.environ.get("S3_BUCKET", "dev-ieee-conference-cloud-bulk-uploads")
ORCHESTRATOR_FUNCTION = os.environ.get(
    "ORCHESTRATOR_FUNCTION_NAME", "ieee-rc-ai-orchestrator"
)

# Map manifest media_type strings to MIME types for .meta.json.
MEDIA_TYPE_MAP = {
    "PDF": "application/pdf",
    "MP4": "video/mp4",
    "MOV": "video/quicktime",
    "WEBM": "video/webm",
}

# Extension map for building the pending key.
EXTENSION_MAP = {
    "PDF": "pdf",
    "MP4": "mp4",
    "MOV": "mov",
    "WEBM": "webm",
}


class BulkWorkerResult(TypedDict):
    """Result of processing a single bulk item."""

    batch_id: str
    item_id: int
    action: str  # "processed" | "failed"
    processing_time_ms: int


class BulkWorker:
    """Processes a single catalog item by invoking the orchestrator."""

    def __init__(
        self,
        lambda_client=None,
        s3_client=None,
        sns_client=None,
    ):
        self._lambda = lambda_client or boto3.client("lambda")
        self._s3 = s3_client or boto3.client("s3")
        self._sns = sns_client or boto3.client("sns")

    def process_item(self, sqs_record: dict) -> BulkWorkerResult:
        """Process a single SQS record containing one bulk item.

        Steps:
            1. Parse message body.
            2. Copy source file to ``{ou}/pending/``.
            3. Create ``.meta.json`` in ``{ou}/metadata/``.
            4. Invoke orchestrator Lambda synchronously.
            5. Update batch progress.
            6. Send SNS if batch is complete.

        Returns:
            Result dict with item_id, action, and timing.
        """
        start = time.time()

        body = sqs_record.get("body", "{}")
        try:
            message = json.loads(body)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValidationError(f"Invalid SQS message body: {exc}")

        item = message.get("item", {})
        batch_id = message.get("batch_id", "unknown")
        callback_url = message.get("callback_url", "")
        total_items = message.get("total_items", 0)
        item_id = item.get("item_id", 0)
        bucket = os.environ.get("S3_BUCKET", DEFAULT_BUCKET)

        logger.info("[%s] Processing item %s", batch_id, item_id)

        has_file = bool(item.get("s3_key"))
        input_text = item.get("input_text")
        has_text = isinstance(input_text, str) and bool(input_text.strip())

        # Step 1-3: Route based on item type.
        try:
            if has_text and not has_file:
                # Text-only: skip file copy + meta, use direct invocation
                self._invoke_orchestrator_direct(bucket, item, callback_url)
            else:
                # File path (with or without input_text)
                pending_key = self._copy_to_pending(bucket, item)
                self._create_meta_json(bucket, item, callback_url)
                self._invoke_orchestrator(bucket, pending_key)
            action = "processed"
        except Exception as exc:
            logger.error("[%s] Orchestrator failed for item %s: %s", batch_id, item_id, exc)
            action = "failed"

        # Step 4: Update progress.
        success = action == "processed"
        progress = self._update_progress(bucket, batch_id, item_id, success, total_items)

        # Step 5: Completion notification.
        if progress and progress.get("completed", 0) + progress.get("failed", 0) >= total_items > 0:
            self._send_completion_notification(batch_id, progress)

        elapsed_ms = int((time.time() - start) * 1000)
        return BulkWorkerResult(
            batch_id=batch_id,
            item_id=item_id,
            action=action,
            processing_time_ms=elapsed_ms,
        )

    def _copy_to_pending(self, bucket: str, item: dict) -> str:
        """Copy the source file from its archive location to ``{ou}/pending/``."""
        s3_key = item["s3_key"]
        ou = item["resource_center"]
        item_id = item["item_id"]
        ext = EXTENSION_MAP.get(item["media_type"], "pdf")
        pending_key = f"{ou}/pending/{item_id}.{ext}"

        source_bucket = item.get("source_bucket", bucket)
        self._s3.copy_object(
            Bucket=bucket,
            CopySource={"Bucket": source_bucket, "Key": s3_key},
            Key=pending_key,
        )
        logger.info("Copied %s -> %s", s3_key, pending_key)
        return pending_key

    def _create_meta_json(self, bucket: str, item: dict, callback_url: str) -> str:
        """Write the ``.meta.json`` the orchestrator expects."""
        ou = item["resource_center"]
        item_id = item["item_id"]
        ext = EXTENSION_MAP.get(item["media_type"], "pdf")
        filename = f"{item_id}.{ext}"
        mime_type = MEDIA_TYPE_MAP.get(item["media_type"], "application/pdf")

        meta = {
            "item_id": str(item_id),
            "ou": ou,
            "product_part_number": str(item.get("request_id", item_id)),
            "ai_enrichment_enabled": True,
            "callback_url": callback_url,
            "content": {
                "media_type": mime_type,
                "filename": filename,
            },
        }

        # Forward CC3-858 fields for hybrid items (input_text + file)
        if item.get("input_text"):
            meta["input_text"] = item["input_text"]
            meta["input_text_mode"] = item.get("input_text_mode", "as_source")
        if item.get("requested_fields"):
            meta["requested_fields"] = item["requested_fields"]

        meta_key = f"{ou}/metadata/{item_id}.meta.json"
        self._s3.put_object(
            Bucket=bucket,
            Key=meta_key,
            Body=json.dumps(meta).encode(),
            ContentType="application/json",
        )
        logger.info("Created meta: s3://%s/%s", bucket, meta_key)
        return meta_key

    def _invoke_orchestrator(self, bucket: str, key: str) -> dict:
        """Invoke the orchestrator Lambda with standard S3-key payload."""
        payload = json.dumps({"bucket": bucket, "key": key}).encode()
        return self._invoke_lambda(payload)

    def _invoke_orchestrator_direct(
        self, bucket: str, item: dict, callback_url: str
    ) -> dict:
        """Invoke orchestrator with direct invocation (text-only, no S3 file)."""
        meta = {
            "item_id": str(item["item_id"]),
            "ou": item["resource_center"],
            "product_part_number": str(item.get("request_id", item["item_id"])),
            "ai_enrichment_enabled": True,
            "callback_url": callback_url,
            "input_text": item["input_text"],
            "input_text_mode": item.get("input_text_mode", "as_source"),
            "content": {"media_type": "text/plain"},
        }
        if item.get("requested_fields"):
            meta["requested_fields"] = item["requested_fields"]

        payload = json.dumps({"bucket": bucket, "meta": meta}).encode()
        return self._invoke_lambda(payload)

    def _invoke_lambda(self, payload: bytes) -> dict:
        """Invoke orchestrator Lambda and validate the response."""
        response = self._lambda.invoke(
            FunctionName=ORCHESTRATOR_FUNCTION,
            InvocationType="RequestResponse",
            Payload=payload,
        )

        # Read payload once to avoid consuming the stream twice
        raw_payload = response["Payload"].read()

        if response.get("FunctionError"):
            error_payload = raw_payload.decode() if isinstance(raw_payload, bytes) else raw_payload
            raise BulkProcessingError(
                f"Orchestrator returned FunctionError: {error_payload}"
            )

        result = json.loads(raw_payload)
        status = result.get("statusCode", 0)
        if status != 200:
            raise BulkProcessingError(
                f"Orchestrator returned status {status}: {result.get('body', {})}"
            )

        return result.get("body", {})

    def _update_progress(
        self,
        bucket: str,
        batch_id: str,
        item_id: int,
        success: bool,
        total_items: int,
    ) -> dict:
        """Update the batch progress file in S3."""
        progress_key = f"bulk/progress/{batch_id}_progress.json"

        try:
            response = self._s3.get_object(Bucket=bucket, Key=progress_key)
            progress = json.loads(response["Body"].read().decode())
        except (ClientError, json.JSONDecodeError, KeyError):
            progress = {
                "batch_id": batch_id,
                "total_items": total_items,
                "published": total_items,
                "completed": 0,
                "failed": 0,
                "status": "processing",
            }

        if success:
            progress["completed"] = progress.get("completed", 0) + 1
        else:
            progress["failed"] = progress.get("failed", 0) + 1

        done = progress["completed"] + progress["failed"]
        if done >= total_items > 0:
            progress["status"] = "completed"

        self._s3.put_object(
            Bucket=bucket,
            Key=progress_key,
            Body=json.dumps(progress).encode(),
            ContentType="application/json",
        )

        if done % 100 == 0 or done >= total_items:
            logger.info(
                "[%s] Progress: %d/%d completed, %d failed",
                batch_id,
                progress["completed"],
                total_items,
                progress["failed"],
            )

        return progress

    def _send_completion_notification(self, batch_id: str, progress: dict) -> None:
        """Publish batch completion to SNS."""
        topic_arn = os.environ.get("COMPLETION_SNS_TOPIC_ARN", "")
        if not topic_arn:
            logger.warning("COMPLETION_SNS_TOPIC_ARN not set; skipping notification")
            return

        self._sns.publish(
            TopicArn=topic_arn,
            Subject=f"Bulk batch completed: {batch_id}",
            Message=json.dumps({
                "batch_id": batch_id,
                "total_items": progress.get("total_items", 0),
                "completed": progress.get("completed", 0),
                "failed": progress.get("failed", 0),
                "status": progress.get("status", "completed"),
            }),
        )
        logger.info("[%s] Completion notification sent", batch_id)
