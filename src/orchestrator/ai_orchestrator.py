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

from src.ai.bedrock_inference import ALL_FIELDS
from src.common.metrics import publish_metrics
from src.webhook.sender import WebhookSender

logger = logging.getLogger(__name__)

# Pricing constants for cost estimation (USD) — configurable via env vars
# to support different Bedrock models (defaults are for Claude Sonnet 4.5).
BEDROCK_INPUT_COST_PER_TOKEN = float(
    os.environ.get("BEDROCK_INPUT_COST_PER_MILLION", "3.00")
) / 1_000_000
BEDROCK_OUTPUT_COST_PER_TOKEN = float(
    os.environ.get("BEDROCK_OUTPUT_COST_PER_MILLION", "15.00")
) / 1_000_000
TRANSCRIBE_COST_PER_MINUTE = float(
    os.environ.get("TRANSCRIBE_COST_PER_MINUTE", "0.024")
)

# Media type routing
PDF_MEDIA_TYPES = {"application/pdf"}
VIDEO_MEDIA_TYPES = {"video/mp4", "video/quicktime", "video/webm"}
PPTX_MEDIA_TYPES = {
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.ms-powerpoint",
    "pptx",
}

# Normalize Drupal-style media types to MIME types.
MEDIA_TYPE_MAP = {
    "PDF": "application/pdf",
    "Video": "video/mp4",
    "PowerPoint": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "Presentation": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

# Lambda function names for dispatch (configurable via env vars).
# Fallbacks suffix the deploy stage so a misconfigured orchestrator never
# silently dispatches into a different env's Lambda.
_STAGE = os.environ.get("STAGE", "dev")
PDF_EXTRACTOR_FUNCTION = os.environ.get("PDF_EXTRACTOR_FUNCTION", f"ieee-cc-pdf-extractor-{_STAGE}")
VIDEO_TRANSCRIBER_FUNCTION = os.environ.get("VIDEO_TRANSCRIBER_FUNCTION", f"ieee-cc-video-transcriber-{_STAGE}")
PPTX_EXTRACTOR_FUNCTION = os.environ.get("PPTX_EXTRACTOR_FUNCTION", f"ieee-rc-pptx-extractor-{_STAGE}")
BEDROCK_FUNCTION = os.environ.get("BEDROCK_FUNCTION", f"ieee-cc-bedrock-inference-{_STAGE}")

# Webhook secret
DRUPAL_WEBHOOK_SECRET = os.environ.get("DRUPAL_WEBHOOK_SECRET", "")

# Retry settings for S3 reads
S3_READ_MAX_RETRIES = 3
S3_READ_BACKOFF_BASE = 1  # seconds

# Required fields in .meta.json (relaxed to accept Drupal's actual schema).
# Fields like 'ou' and 'product_part_number' are derived from the S3 key
# or meta content if not present at the top level.
REQUIRED_META_FIELDS = {
    "item_id",
    "ai_enrichment_enabled",
    "content",
}
REQUIRED_CONTENT_FIELDS = {"media_type"}

VALID_INPUT_TEXT_MODES = frozenset({"as_source", "as_abstract"})


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
        cloudwatch_client=None,
    ):
        self._s3 = s3_client or boto3.client("s3")
        self._lambda = lambda_client or boto3.client("lambda")
        self._webhook_sender = WebhookSender(sns_client=sns_client)
        self._cloudwatch = cloudwatch_client

    def process(
        self,
        bucket: str,
        key: str | None,
        request_id: str = "",
        meta: dict | None = None,
    ) -> OrchestratorResult:
        """Process an uploaded file or direct text invocation.

        Args:
            bucket: S3 bucket name.
            key: S3 key (e.g. PES/pending/STD-12345.pdf). None for text-only.
            request_id: Lambda request ID for correlation logging.
            meta: Inline meta dict for direct invocation. When provided,
                  skips S3 meta read and key parsing.

        Returns:
            OrchestratorResult with routing outcome.
        """
        start = time.time()
        has_file = key is not None

        # Step 1: Obtain and validate meta
        if meta is not None:
            # Direct invocation — meta provided inline
            item_id = str(meta["item_id"])
            ou = meta.get("ou", "")
            ext = ""
            destination_key = ""
            correlation = f"[{request_id}:{item_id}]" if request_id else f"[{item_id}]"
            logger.info("%s Direct invocation (text-only)", correlation)
        else:
            # Standard S3-key flow
            ou, item_id, ext = self._parse_key(key)
            correlation = f"[{request_id}:{item_id}]" if request_id else f"[{item_id}]"
            logger.info("%s Processing s3://%s/%s", correlation, bucket, key)

            meta_key = f"{ou}/metadata/{item_id}.meta.json"
            meta = self._read_meta_json(bucket, meta_key, correlation)
            destination_key = f"{ou}/processed/{item_id}.{ext}"

        self._validate_meta(meta)

        # Normalize media type from Drupal format ('Video', 'PDF') to MIME.
        raw_media_type = meta["content"]["media_type"]
        meta["content"]["media_type"] = MEDIA_TYPE_MAP.get(
            raw_media_type, raw_media_type
        )

        logger.info(
            "%s ai_enrichment_enabled=%s, media_type=%s",
            correlation,
            meta["ai_enrichment_enabled"],
            meta["content"]["media_type"],
        )

        # Derive fields that may not be in .meta.json top level.
        meta_item_id = str(meta["item_id"])
        meta_ou = meta.get("ou", meta["content"].get("resource_center", ou))
        product_part_number = meta.get(
            "product_part_number",
            meta["content"].get("resource_center", meta_ou) + "_" + str(meta_item_id),
        )

        # Step 2: Route based on ai_enrichment_enabled
        if not meta["ai_enrichment_enabled"]:
            if has_file:
                self._move_file(bucket, key, destination_key, correlation)
            elapsed = int((time.time() - start) * 1000)
            logger.info("%s Moved to /processed/ (AI disabled) in %dms", correlation, elapsed)

            publish_metrics(self._cloudwatch, [
                {
                    "MetricName": "submission-processed",
                    "Value": 1,
                    "Unit": "Count",
                    "Dimensions": [
                        {"Name": "AiToggleEnabled", "Value": "false"},
                        {"Name": "ResourceCenter", "Value": meta_ou},
                    ],
                },
            ])

            return OrchestratorResult(
                item_id=item_id,
                ou=ou,
                action="moved",
                ai_enrichment_enabled=False,
                source_key=key or "",
                destination_key=destination_key,
                processing_time_ms=elapsed,
                details={"reason": "ai_enrichment_disabled"},
            )

        # Extract new CC3-858 fields from meta
        input_text = meta.get("input_text")
        input_text_mode = meta.get("input_text_mode", "as_source")
        requested_fields_raw = meta.get("requested_fields")
        self._validate_new_meta_fields(meta, key)

        # Compute effective fields for Bedrock
        requested_fields = frozenset(requested_fields_raw) if requested_fields_raw else None
        effective_fields = requested_fields or ALL_FIELDS
        if input_text_mode == "as_abstract":
            effective_fields = effective_fields - {"abstract"}

        # Step 3: Get text for Bedrock (user-provided or extracted)
        media_type = meta["content"]["media_type"]
        extraction_result = {}

        if input_text:
            # User-provided text — skip extraction entirely
            extracted_text = input_text
            extraction_result = {"source": "user_provided", "text": input_text}
            logger.info("%s Using user-provided input_text (%s mode)", correlation, input_text_mode)
        elif has_file:
            # Standard extraction from file
            extraction_result = self._dispatch_extraction(
                bucket, key, ou, product_part_number, media_type, correlation
            )
            extracted_text = extraction_result.get("text") or extraction_result.get("transcript", "")
        else:
            raise ValueError("Direct invocation requires 'input_text' in meta")

        # Step 4: Invoke Bedrock for metadata generation
        bedrock_result = {}

        if extracted_text.strip():
            bedrock_text = extracted_text
            if input_text_mode == "as_abstract":
                bedrock_text = (
                    "The following is a finalized abstract for an IEEE publication. "
                    "Generate metadata based on this abstract:\n\n" + extracted_text
                )
            # Forward requested_fields to Bedrock when the caller specified a
            # subset OR when as_abstract mode removed "abstract" from the set —
            # so Bedrock only generates the fields we actually need.
            bedrock_rf = effective_fields if requested_fields_raw or input_text_mode == "as_abstract" else None
            bedrock_result = self._invoke_bedrock(
                bedrock_text, correlation, requested_fields=bedrock_rf
            )
        else:
            logger.warning("%s No text available — skipping Bedrock", correlation)

        # Post-Bedrock merge for as_abstract mode
        if input_text_mode == "as_abstract" and input_text:
            bedrock_result["abstract"] = input_text

        # Step 5a: Copy VTT subtitle file if present (video, no input_text)
        vtt_key = ""
        if media_type in VIDEO_MEDIA_TYPES and not input_text:
            source_vtt_key = extraction_result.get("vtt_s3_key", "")
            if source_vtt_key:
                destination_vtt_key = f"{meta_ou}/subtitles/{product_part_number}.vtt"
                try:
                    self._s3.copy_object(
                        Bucket=bucket,
                        CopySource={"Bucket": bucket, "Key": source_vtt_key},
                        Key=destination_vtt_key,
                    )
                    vtt_key = destination_vtt_key
                    logger.info(
                        "%s Copied VTT subtitle to s3://%s/%s",
                        correlation, bucket, vtt_key,
                    )
                except Exception:
                    logger.warning(
                        "%s Failed to copy VTT subtitle from %s",
                        correlation, source_vtt_key,
                        exc_info=True,
                    )

        # Step 5: Send webhook to Drupal
        callback_url = meta.get("callback_url") or meta.get("webhook_url")
        webhook_sent = False
        if callback_url:
            # Determine signal based on text source
            if input_text:
                signal = "metadata_ready"
            elif media_type in VIDEO_MEDIA_TYPES:
                signal = "transcription_ready"
            else:
                signal = "extraction_ready"

            # Derive generated_fields from actual Bedrock output, not intent —
            # avoids claiming fields were generated when Bedrock was skipped.
            actual_fields = set(bedrock_result.keys()) & ALL_FIELDS
            if input_text_mode == "as_abstract" and input_text:
                actual_fields.discard("abstract")
            generated_fields = sorted(actual_fields)

            payload = {
                "request_id": meta.get("request_id") or request_id,
                "item_id": meta_item_id,
                "status": "success",
                "signal": signal,
                "product_part_number": product_part_number,
                "ou": meta_ou,
                "completed_at": datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "extraction": extraction_result,
                "data": bedrock_result,
                "generated_fields": generated_fields,
                "vtt_s3_key": vtt_key if vtt_key else None,
            }
            webhook_sent = self._webhook_sender.send(
                callback_url, DRUPAL_WEBHOOK_SECRET, payload, correlation,
            )

        # Step 6: Move file from /pending/ to /processed/
        if has_file:
            self._move_file(bucket, key, destination_key, correlation)

        elapsed = int((time.time() - start) * 1000)
        logger.info("%s Enrichment complete in %dms", correlation, elapsed)

        # Step 7: Publish cost estimate and submission metric
        self._publish_enrichment_metrics(
            extraction_result, bedrock_result, media_type, meta_ou
        )

        return OrchestratorResult(
            item_id=item_id,
            ou=meta_ou,
            action="enriched",
            ai_enrichment_enabled=True,
            source_key=key or "",
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

    @staticmethod
    def _validate_new_meta_fields(meta: dict, key: str | None) -> None:
        """Validate CC3-858 fields: input_text_mode, requested_fields."""
        input_text = meta.get("input_text")
        has_text = isinstance(input_text, str) and bool(input_text.strip())

        # input_text_mode only valid when input_text is present
        if "input_text_mode" in meta:
            if not has_text:
                raise ValueError("'input_text_mode' requires 'input_text' to be present")
            if meta["input_text_mode"] not in VALID_INPUT_TEXT_MODES:
                raise ValueError(
                    f"Invalid input_text_mode: {meta['input_text_mode']!r}. "
                    f"Must be one of {sorted(VALID_INPUT_TEXT_MODES)}"
                )

        requested_fields = meta.get("requested_fields")
        if requested_fields is not None:
            if not isinstance(requested_fields, list) or not requested_fields:
                raise ValueError("requested_fields must be a non-empty array")
            if any(not isinstance(field, str) for field in requested_fields):
                raise ValueError("requested_fields must contain only strings")
            invalid = set(requested_fields) - ALL_FIELDS
            if invalid:
                raise ValueError(f"Invalid requested_fields: {sorted(invalid)}")

        if key is None and not has_text:
            raise ValueError("Direct invocation requires 'input_text' in meta")

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
    # Metrics
    # ------------------------------------------------------------------

    def _publish_enrichment_metrics(
        self,
        extraction_result: dict,
        bedrock_result: dict,
        media_type: str,
        ou: str,
    ) -> None:
        """Compute and publish cost estimate and submission-processed metrics."""
        input_tokens = bedrock_result.get("input_tokens", 0)
        output_tokens = bedrock_result.get("output_tokens", 0)

        cost = (
            input_tokens * BEDROCK_INPUT_COST_PER_TOKEN
            + output_tokens * BEDROCK_OUTPUT_COST_PER_TOKEN
        )

        if media_type in VIDEO_MEDIA_TYPES:
            duration_seconds = extraction_result.get("duration_seconds", 0)
            cost += (duration_seconds / 60) * TRANSCRIBE_COST_PER_MINUTE

        publish_metrics(self._cloudwatch, [
            {
                "MetricName": "processing-cost-estimate",
                "Value": round(cost, 6),
                "Unit": "None",
                "Dimensions": [
                    {"Name": "ResourceCenter", "Value": ou},
                ],
            },
            {
                "MetricName": "submission-processed",
                "Value": 1,
                "Unit": "Count",
                "Dimensions": [
                    {"Name": "AiToggleEnabled", "Value": "true"},
                    {"Name": "ResourceCenter", "Value": ou},
                ],
            },
        ])

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
        elif media_type in PPTX_MEDIA_TYPES:
            function_name = PPTX_EXTRACTOR_FUNCTION
            logger.info("%s Dispatching to PPTX extractor", correlation)
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
        text: str,
        correlation: str,
        requested_fields: frozenset[str] | None = None,
    ) -> dict:
        """Invoke Bedrock metadata generation Lambda."""
        logger.info("%s Invoking Bedrock for metadata generation", correlation)

        payload = {
            "text": text,
        }
        if requested_fields:
            payload["requested_fields"] = sorted(requested_fields)

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
