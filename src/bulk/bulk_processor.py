"""Bulk processor — reads a manifest from S3 and fans out items to SQS."""

from __future__ import annotations

import json
import os
import time
from typing import TypedDict

import boto3

from src.common.exceptions import BulkProcessingError, ValidationError
from src.common.logging import get_json_logger

logger = get_json_logger(__name__)

DEFAULT_BUCKET = os.environ.get("S3_BUCKET", "dev-ieee-conference-cloud-bulk-uploads")

REQUIRED_MANIFEST_FIELDS = {"batch_id", "callback_url", "items"}
ALWAYS_REQUIRED_ITEM_FIELDS = {"item_id", "request_id", "resource_center"}
VALID_MEDIA_TYPES = {"PDF", "MP4", "MOV", "WEBM"}
VALID_INPUT_TEXT_MODES = frozenset({"as_source", "as_abstract"})

# Estimated per-item costs (USD) for logging purposes.
COST_ESTIMATES = {
    "PDF": 0.01,     # PDF extraction + Bedrock inference
    "MP4": 0.03,     # Transcribe + Bedrock inference
    "MOV": 0.03,
    "WEBM": 0.03,
    "text": 0.005,   # Bedrock inference only, no extraction
}


class BulkProcessorResult(TypedDict):
    """Result of a manifest dispatch."""

    batch_id: str
    total_items: int
    published_count: int
    estimated_cost: dict
    status: str  # "dispatched" | "failed"


class BulkProcessor:
    """Reads a batch manifest from S3 and publishes items to SQS."""

    def __init__(
        self,
        s3_client=None,
        sqs_client=None,
        sns_client=None,
    ):
        self._s3 = s3_client or boto3.client("s3")
        self._sqs = sqs_client or boto3.client("sqs")
        self._sns = sns_client or boto3.client("sns")

    def process_manifest(self, bucket: str, batch_id: str) -> BulkProcessorResult:
        """Read manifest, validate, estimate cost, publish items to SQS.

        Args:
            bucket: S3 bucket containing the manifest.
            batch_id: Manifest identifier (maps to ``bulk/manifests/{batch_id}.json``).

        Returns:
            Dispatch result with item counts and cost estimate.
        """
        manifest_key = f"bulk/manifests/{batch_id}.json"
        logger.info("Reading manifest: s3://%s/%s", bucket, manifest_key)

        try:
            response = self._s3.get_object(Bucket=bucket, Key=manifest_key)
            manifest = json.loads(response["Body"].read().decode())
        except self._s3.exceptions.NoSuchKey:
            raise ValidationError(f"Manifest not found: {manifest_key}")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValidationError(f"Invalid manifest JSON: {exc}")

        self._validate_manifest(manifest)

        items = manifest["items"]
        callback_url = manifest["callback_url"]
        config = manifest.get("config", {})

        cost_estimate = self._estimate_cost(items)
        logger.info(
            "Cost estimate for batch %s: %s (total: $%.2f)",
            batch_id,
            cost_estimate,
            cost_estimate["total_usd"],
        )

        try:
            published = self._publish_items(items, batch_id, callback_url, config)
        except Exception as exc:
            raise BulkProcessingError(f"Failed to publish items: {exc}") from exc

        self._write_progress(bucket, batch_id, len(items), published, "dispatched")

        return BulkProcessorResult(
            batch_id=batch_id,
            total_items=len(items),
            published_count=published,
            estimated_cost=cost_estimate,
            status="dispatched",
        )

    def _validate_manifest(self, manifest: dict) -> None:
        """Validate manifest structure and item fields."""
        missing = REQUIRED_MANIFEST_FIELDS - set(manifest.keys())
        if missing:
            raise ValidationError(f"Manifest missing required fields: {sorted(missing)}")

        items = manifest["items"]
        if not isinstance(items, list) or len(items) == 0:
            raise ValidationError("Manifest 'items' must be a non-empty list")

        # Item-validation contract:
        #   ALWAYS REQUIRED:       {item_id, request_id, resource_center}
        #   WHEN s3_key PRESENT:   media_type ∈ VALID_MEDIA_TYPES;
        #                          source_bucket non-empty if provided
        #   WHEN s3_key EMPTY:     input_text non-empty
        # Empty/absent values for context-dependent fields are tolerated
        # (Strategy A items emit input_text="" by design; text-only items
        # emit source_bucket="" because there's no source to fetch).
        for i, item in enumerate(items):
            # Always-required fields
            item_missing = ALWAYS_REQUIRED_ITEM_FIELDS - set(item.keys())
            if item_missing:
                raise ValidationError(
                    f"Item {i} missing required fields: {sorted(item_missing)}"
                )

            # Use consistent truthiness checks (matches BulkWorker routing)
            has_file = bool(item.get("s3_key"))
            input_text = item.get("input_text")
            has_text = isinstance(input_text, str) and bool(input_text.strip())

            # Must have at least one of s3_key or input_text — the only
            # real "no content" failure case. Strict-empty checks for
            # input_text/input_text_mode/source_bucket are gated on
            # has_file below so file-bearing items (Strategy A) aren't
            # rejected for sending the empty-string sentinels that the
            # Drupal builder emits for inapplicable fields.
            if not has_file and not has_text:
                raise ValidationError(
                    f"Item {i} must have at least one of 's3_key' or 'input_text'"
                )

            # File items: media_type must be valid; source_bucket if
            # present must be non-empty (the worker would otherwise issue
            # an S3 CopyObject with an empty source bucket name).
            if has_file:
                if "media_type" not in item:
                    raise ValidationError(
                        f"Item {i} has 's3_key' but missing 'media_type'"
                    )
                if item["media_type"] not in VALID_MEDIA_TYPES:
                    raise ValidationError(
                        f"Item {i} has invalid media_type '{item['media_type']}'; "
                        f"expected one of {sorted(VALID_MEDIA_TYPES)}"
                    )

                source_bucket = item.get("source_bucket")
                if source_bucket is not None:
                    if not isinstance(source_bucket, str):
                        raise ValidationError(
                            f"Item {i} 'source_bucket' must be a string"
                        )
                    if not source_bucket.strip():
                        raise ValidationError(
                            f"Item {i} 'source_bucket' must be non-empty when "
                            f"'s3_key' is set"
                        )

            # input_text_mode is only meaningful when input_text is used;
            # empty/None treated as absent (Strategy A items send "").
            input_text_mode = item.get("input_text_mode")
            if input_text_mode:
                if not has_text:
                    raise ValidationError(
                        f"Item {i} has 'input_text_mode' without 'input_text'"
                    )
                if input_text_mode not in VALID_INPUT_TEXT_MODES:
                    raise ValidationError(
                        f"Item {i} has invalid input_text_mode "
                        f"'{input_text_mode}'; "
                        f"expected one of {sorted(VALID_INPUT_TEXT_MODES)}"
                    )

            requested_fields = item.get("requested_fields")
            if requested_fields is not None:
                if not isinstance(requested_fields, list) or not requested_fields:
                    raise ValidationError(
                        f"Item {i} 'requested_fields' must be a non-empty array"
                    )
                if any(
                    not isinstance(field, str) or not field.strip()
                    for field in requested_fields
                ):
                    raise ValidationError(
                        f"Item {i} 'requested_fields' must contain only non-empty strings"
                    )

    @staticmethod
    def _estimate_cost(items: list[dict]) -> dict:
        """Estimate processing cost by media type."""
        breakdown: dict[str, int] = {}
        total = 0.0
        for item in items:
            media = item.get("media_type", "text")
            breakdown[media] = breakdown.get(media, 0) + 1
            total += COST_ESTIMATES.get(media, 0.01)

        return {
            "breakdown": breakdown,
            "total_usd": round(total, 2),
        }

    def _publish_items(
        self,
        items: list[dict],
        batch_id: str,
        callback_url: str,
        config: dict,
    ) -> int:
        """Publish each item as an SQS message."""
        queue_url = os.environ.get("BULK_QUEUE_URL", "")
        if not queue_url:
            raise BulkProcessingError("BULK_QUEUE_URL environment variable not set")

        delay_ms = config.get("delay_between_ms", 0)
        delay_s = delay_ms / 1000.0
        total = len(items)
        published = 0

        for item in items:
            message_body = json.dumps({
                "batch_id": batch_id,
                "callback_url": callback_url,
                "item": item,
                "total_items": total,
            })

            self._sqs.send_message(
                QueueUrl=queue_url,
                MessageBody=message_body,
            )
            published += 1

            if delay_s > 0 and published < total:
                time.sleep(delay_s)

        logger.info("Published %d/%d items for batch %s", published, total, batch_id)
        return published

    def _write_progress(
        self,
        bucket: str,
        batch_id: str,
        total: int,
        published: int,
        status: str,
    ) -> None:
        """Write initial progress file to S3."""
        progress_key = f"bulk/progress/{batch_id}_progress.json"
        progress = {
            "batch_id": batch_id,
            "total_items": total,
            "published": published,
            "completed": 0,
            "failed": 0,
            "status": status,
        }
        self._s3.put_object(
            Bucket=bucket,
            Key=progress_key,
            Body=json.dumps(progress).encode(),
            ContentType="application/json",
        )
        logger.info("Progress written to s3://%s/%s", bucket, progress_key)
