"""AI Orchestrator for IEEE Content Conversion pipeline.

Central router that reads .meta.json for uploaded files, determines whether
AI enrichment is enabled, and dispatches accordingly:
  - AI disabled: moves file from /pending/ to /processed/
  - AI enabled:  dispatches to transcription (video) or text extraction (PDF),
                 invokes Bedrock for metadata generation, sends webhook to Drupal.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import TypedDict

import boto3
from botocore.exceptions import ClientError

from src.webhook.sender import WebhookSender

logger = logging.getLogger(__name__)

# Media type routing
PDF_MEDIA_TYPES = {"application/pdf"}
VIDEO_MEDIA_TYPES = {"video/mp4", "video/quicktime", "video/webm"}

# Lambda function names for dispatch (configurable via env vars)
PDF_EXTRACTOR_FUNCTION = os.environ.get("PDF_EXTRACTOR_FUNCTION", "ieee-cc-pdf-extractor")
VIDEO_TRANSCRIBER_FUNCTION = os.environ.get("VIDEO_TRANSCRIBER_FUNCTION", "ieee-cc-video-transcriber")
BEDROCK_FUNCTION = os.environ.get("BEDROCK_FUNCTION", "ieee-cc-bedrock-inference")

# Webhook secret
DRUPAL_WEBHOOK_SECRET = os.environ.get("DRUPAL_WEBHOOK_SECRET", "")

# Retry settings for S3 reads
S3_READ_MAX_RETRIES = 3
S3_READ_BACKOFF_BASE = 1  # seconds

# Required fields in .meta.json
REQUIRED_META_FIELDS = {
    "item_id",
    "ou",
    "product_part_number",
    "ai_enrichment_enabled",
    "content",
}
REQUIRED_CONTENT_FIELDS = {"media_type", "filename"}


class OrchestratorResult(TypedDict):
    """Result of an orchestration run."""

    item_id: str
    ou: str
    action: str  # "skipped" | "enriched" | "moved"
    ai_enrichment_enabled: bool
    source_key: str
    destination_key: str
    processing_time_ms: int
    details: dict


class AIOrchestrator:
    """Routes uploaded files based on .meta.json configuration."""

    def __init__(
        self,
        s3_client=None,
        lambda_client=None,
        sns_client=None,
    ):
        self._s3 = s3_client or boto3.client("s3")
        self._lambda = lambda_client or boto3.client("lambda")
        self._webhook_sender = WebhookSender(sns_client=sns_client)

    def process(
        self,
        bucket: str,
        key: str,
        request_id: str = "",
    ) -> OrchestratorResult:
        """Process an uploaded file based on its .meta.json.

        Args:
            bucket: S3 bucket name.
            key: S3 key of the uploaded file (e.g. PES/pending/STD-12345.pdf).
            request_id: Lambda request ID for correlation logging.

        Returns:
            OrchestratorResult with routing outcome.
        """
        start = time.time()

        ou, item_id, ext = self._parse_key(key)
        correlation = f"[{request_id}:{item_id}]" if request_id else f"[{item_id}]"

        logger.info("%s Processing s3://%s/%s", correlation, bucket, key)

        # Step 1: Read and validate .meta.json
        meta_key = f"{ou}/metadata/{item_id}.meta.json"
        meta = self._read_meta_json(bucket, meta_key, correlation)
        self._validate_meta(meta)

        logger.info(
            "%s ai_enrichment_enabled=%s, media_type=%s",
            correlation,
            meta["ai_enrichment_enabled"],
            meta["content"]["media_type"],
        )

        product_part_number = meta["product_part_number"]
        destination_key = f"{ou}/processed/{item_id}.{ext}"

        # Step 2: Route based on ai_enrichment_enabled
        if not meta["ai_enrichment_enabled"]:
            # Move file from /pending/ to /processed/
            self._move_file(bucket, key, destination_key, correlation)
            elapsed = int((time.time() - start) * 1000)
            logger.info("%s Moved to /processed/ (AI disabled) in %dms", correlation, elapsed)

            return OrchestratorResult(
                item_id=item_id,
                ou=ou,
                action="moved",
                ai_enrichment_enabled=False,
                source_key=key,
                destination_key=destination_key,
                processing_time_ms=elapsed,
                details={"reason": "ai_enrichment_disabled"},
            )

        # Step 3: Dispatch to extraction/transcription
        media_type = meta["content"]["media_type"]
        extraction_result = self._dispatch_extraction(
            bucket, key, ou, product_part_number, media_type, correlation
        )

        # Step 4: Invoke Bedrock for metadata generation
        extracted_text = extraction_result.get("text") or extraction_result.get("transcript", "")
        bedrock_result = {}

        if extracted_text.strip():
            bedrock_result = self._invoke_bedrock(
                bucket, ou, product_part_number, extracted_text, correlation
            )
        else:
            logger.warning("%s No text extracted — skipping Bedrock", correlation)

        # Step 5: Send webhook to Drupal
        callback_url = meta.get("callback_url") or meta.get("webhook_url")
        webhook_sent = False
        if callback_url:
            signal = (
                "transcription_ready"
                if media_type in VIDEO_MEDIA_TYPES
                else "extraction_ready"
            )
            payload = {
                "signal": signal,
                "product_part_number": product_part_number,
                "item_id": item_id,
                "ou": ou,
                "status": "completed",
                "completed_at": datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "extraction": extraction_result,
                "metadata": bedrock_result,
            }
            webhook_sent = self._webhook_sender.send(
                callback_url, DRUPAL_WEBHOOK_SECRET, payload, correlation,
            )

        # Step 6: Move file from /pending/ to /processed/
        self._move_file(bucket, key, destination_key, correlation)

        elapsed = int((time.time() - start) * 1000)
        logger.info("%s Enrichment complete in %dms", correlation, elapsed)

        return OrchestratorResult(
            item_id=item_id,
            ou=ou,
            action="enriched",
            ai_enrichment_enabled=True,
            source_key=key,
            destination_key=destination_key,
            processing_time_ms=elapsed,
            details={
                "media_type": media_type,
                "extraction": extraction_result,
                "bedrock": bedrock_result,
                "webhook_sent": webhook_sent,
            },
        )

    # ------------------------------------------------------------------
    # Key parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_key(key: str) -> tuple[str, str, str]:
        """Parse S3 key into (ou, item_id, extension).

        Expected pattern: {ou}/pending/{item_id}.{ext}
        """
        parts = key.split("/")
        if len(parts) < 3 or parts[1] != "pending":
            raise ValueError(
                f"Key does not match '{{ou}}/pending/{{filename}}.{{ext}}': {key}"
            )
        ou = parts[0]
        filename = "/".join(parts[2:])  # handle nested paths
        if "." not in filename:
            raise ValueError(f"Filename has no extension: {filename}")
        name, ext = filename.rsplit(".", 1)
        return ou, name, ext.lower()

    # ------------------------------------------------------------------
    # .meta.json reading with retry
    # ------------------------------------------------------------------

    def _read_meta_json(
        self, bucket: str, key: str, correlation: str
    ) -> dict:
        """Read and parse .meta.json from S3 with retry."""
        last_error = None
        for attempt in range(1, S3_READ_MAX_RETRIES + 1):
            try:
                resp = self._s3.get_object(Bucket=bucket, Key=key)
                body = resp["Body"].read()
                return json.loads(body)
            except ClientError as exc:
                code = exc.response["Error"]["Code"]
                if code == "NoSuchKey":
                    raise ValueError(
                        f"Meta file not found: s3://{bucket}/{key}"
                    ) from exc
                last_error = exc
                wait = S3_READ_BACKOFF_BASE * (2 ** (attempt - 1))
                logger.warning(
                    "%s S3 read attempt %d/%d failed (%s), retrying in %ds",
                    correlation, attempt, S3_READ_MAX_RETRIES, code, wait,
                )
                time.sleep(wait)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid JSON in meta file s3://{bucket}/{key}: {exc}"
                ) from exc

        raise RuntimeError(
            f"Failed to read s3://{bucket}/{key} after {S3_READ_MAX_RETRIES} retries"
        ) from last_error

    @staticmethod
    def _validate_meta(meta: dict) -> None:
        """Validate .meta.json has all required fields."""
        missing = REQUIRED_META_FIELDS - set(meta.keys())
        if missing:
            raise ValueError(f"Missing required .meta.json fields: {sorted(missing)}")

        content = meta.get("content", {})
        if not isinstance(content, dict):
            raise ValueError(".meta.json 'content' must be an object")

        missing_content = REQUIRED_CONTENT_FIELDS - set(content.keys())
        if missing_content:
            raise ValueError(
                f"Missing required content fields: {sorted(missing_content)}"
            )

    # ------------------------------------------------------------------
    # File operations
    # ------------------------------------------------------------------

    def _move_file(
        self, bucket: str, source_key: str, dest_key: str, correlation: str
    ) -> None:
        """Move a file within S3 (copy + delete)."""
        logger.info("%s Moving %s -> %s", correlation, source_key, dest_key)
        self._s3.copy_object(
            Bucket=bucket,
            CopySource={"Bucket": bucket, "Key": source_key},
            Key=dest_key,
        )
        self._s3.delete_object(Bucket=bucket, Key=source_key)

    # ------------------------------------------------------------------
    # Lambda dispatch
    # ------------------------------------------------------------------

    def _dispatch_extraction(
        self,
        bucket: str,
        key: str,
        ou: str,
        product_part_number: str,
        media_type: str,
        correlation: str,
    ) -> dict:
        """Dispatch to the appropriate extraction Lambda."""
        if media_type in PDF_MEDIA_TYPES:
            function_name = PDF_EXTRACTOR_FUNCTION
            logger.info("%s Dispatching to PDF extractor", correlation)
        elif media_type in VIDEO_MEDIA_TYPES:
            function_name = VIDEO_TRANSCRIBER_FUNCTION
            logger.info("%s Dispatching to video transcriber", correlation)
        else:
            raise ValueError(f"Unsupported media type: {media_type}")

        payload = {
            "bucket": bucket,
            "key": key,
            "ou": ou,
            "product_part_number": product_part_number,
        }

        response = self._lambda.invoke(
            FunctionName=function_name,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode(),
        )

        result = json.loads(response["Payload"].read())

        if "FunctionError" in response:
            raise RuntimeError(
                f"{function_name} failed: {result.get('errorMessage', 'Unknown error')}"
            )

        body = result.get("body", {})
        status = result.get("statusCode", 0)
        if status != 200:
            raise RuntimeError(
                f"{function_name} returned {status}: {body.get('error', 'Unknown')}"
            )

        logger.info("%s Extraction complete: %s", correlation, function_name)
        return body

    def _invoke_bedrock(
        self,
        bucket: str,
        ou: str,
        product_part_number: str,
        text: str,
        correlation: str,
    ) -> dict:
        """Invoke Bedrock metadata generation Lambda."""
        logger.info("%s Invoking Bedrock for metadata generation", correlation)

        payload = {
            "text": text,
        }

        response = self._lambda.invoke(
            FunctionName=BEDROCK_FUNCTION,
            InvocationType="RequestResponse",
            Payload=json.dumps(payload).encode(),
        )

        result = json.loads(response["Payload"].read())

        if "FunctionError" in response:
            raise RuntimeError(
                f"Bedrock inference failed: {result.get('errorMessage', 'Unknown error')}"
            )

        body = result.get("body", {})
        status = result.get("statusCode", 0)
        if status != 200:
            raise RuntimeError(
                f"Bedrock inference returned {status}: {body.get('error', 'Unknown')}"
            )

        logger.info("%s Bedrock metadata generated", correlation)
        return body

