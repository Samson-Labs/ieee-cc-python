"""Image Overlay Generation module for IEEE Content Conversion pipeline.

Generates product overlay images from JSON trigger files. Loads a background
image from S3, applies text overlays (title, authors, logo) using Pillow,
and writes output to the destination bucket.
"""

from __future__ import annotations

import json
import logging
import os
import textwrap
from io import BytesIO
from typing import Literal, TypedDict

import boto3
from botocore.exceptions import ClientError
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from src.webhook.sender import WebhookSender

logger = logging.getLogger(__name__)


class RenderError(Exception):
    """Raised when Pillow image decode/encode fails during overlay generation.

    Distinguished from generic Exception so the Drupal callback can emit
    the documented `render_failed` error_code per
    docs/contracts/image-overlay-callback-v1.json.
    """


# Callback error codes — mirrors docs/contracts/image-overlay-callback-v1.json
ERR_SOURCE_NOT_FOUND = "source_not_found"
ERR_DEST_WRITE_FAILED = "dest_write_failed"
ERR_VALIDATION = "validation_error"
ERR_RENDER_FAILED = "render_failed"
ERR_INTERNAL = "internal"

# Layout constants (proportional to image dimensions)
TITLE_FONT_RATIO = 0.042  # title font size as fraction of image height
AUTHOR_FONT_RATIO = 0.026  # author font size as fraction of image height
TITLE_MAX_LINES = 4
AUTHOR_MAX_LINES = 2
LOGO_BAR_RATIO = 0.15  # bottom 15% reserved for logo bar
TITLE_Y_RATIO = 0.12  # title starts at 12% from top
AUTHOR_GAP_RATIO = 0.04  # gap between title and author as fraction of height
LINE_SPACING_RATIO = 0.012  # line spacing as fraction of height
SHADOW_OFFSET = 2  # pixels offset for text shadow
SHADOW_COLOR = (0, 0, 0, 160)  # semi-transparent black

# Legacy parity defaults — match Node.js image-generator's getTextRows/getTextElements.
LEGACY_PADDING_FACTOR_DEFAULT = 0.04  # widthPadFactor default; max width = w - (w * 0.04)
LEGACY_ROW_HEIGHT_PAD_DEFAULT = 2  # rowHeightPad default in Node.js getTextElements.js

# Thumbnail dimensions
THUMBNAIL_SIZE = (400, 300)

# Supported output formats
SUPPORTED_FORMATS = {"jpg", "png"}
DEFAULT_FORMAT = "jpg"
DEFAULT_QUALITY = 85

REQUIRED_FIELDS = {
    "product_part_number",
    "title",
    "authors",
    "config",
    "background_source",
}
REQUIRED_CONFIG_FIELDS = {"source_bucket", "dest_bucket", "public_path"}


class TriggerConfig(TypedDict):
    source_bucket: str
    dest_bucket: str
    public_path: str


class TriggerPayload(TypedDict, total=False):
    product_part_number: str
    title: str
    authors: str
    config: TriggerConfig
    background_source: str
    output_format: str
    output_quality: int
    is_thumbnail: bool


# Legacy schema fields (existing Drupal ImageGenerationService format)
LEGACY_FIELDS = {"sourceBucket", "sourceName", "destBucket", "destName", "overlay"}


class GenerationResult(TypedDict, total=False):
    output_key: str
    thumbnail_key: str
    width: int
    height: int
    format: str
    bytes: int
    s3_etag: str
    dest_bucket: str
    callback_sent: bool


class ImageOverlayGenerator:
    """Generates product overlay images from trigger JSON files in S3.

    When the trigger JSON carries CC3-906 webhook fields (`callback_url`,
    `callback_secret_ref`, `request_id`, `item_id`), this class also POSTs
    an HMAC-signed `image-overlay-callback-v1` payload to Drupal once the
    job reaches a terminal state (success or failure). Triggers without
    those fields work unchanged for backward compatibility.
    """

    def __init__(
        self,
        s3_client=None,
        secrets_client=None,
        sns_client=None,
        webhook_sender=None,
    ):
        self._s3 = s3_client or boto3.client("s3")
        self._secrets = secrets_client or boto3.client("secretsmanager")
        self._webhook_sender = webhook_sender or WebhookSender(sns_client=sns_client)

    def process_trigger(self, bucket: str, key: str) -> GenerationResult:
        """Process an S3 trigger JSON file end-to-end.

        Supports both the new schema (product_part_number/title/authors/config)
        and the legacy Drupal schema (sourceBucket/sourceName/destBucket/destName/overlay).

        1. Read the trigger JSON
        2. Detect schema and route accordingly
        3. Generate overlay, write output, delete trigger
        4. POST callback to Drupal if the trigger carries webhook fields (CC3-906)

        On failure, post an error callback (best-effort) and re-raise so the
        Lambda runtime sees the original exception for retry/DLQ.

        Args:
            bucket: S3 bucket containing the trigger JSON.
            key: S3 object key of the trigger JSON (e.g. actions/xyz.json).

        Returns:
            GenerationResult with output key, dimensions, and format.
        """
        logger.info("Processing trigger s3://%s/%s", bucket, key)

        # Read the trigger up front so we have the callback fields available
        # even if generation fails. The downstream _process_* methods do not
        # re-fetch — they receive the already-parsed payload.
        try:
            payload = self._read_trigger(bucket, key)
        except (ClientError, json.JSONDecodeError) as exc:
            # No trigger payload → no callback fields → can't notify Drupal.
            # Re-raise so the Lambda invocation lands in DLQ.
            logger.error("Failed to read trigger s3://%s/%s: %s", bucket, key, exc)
            raise

        correlation = self._correlation_for(payload)

        try:
            if self._is_legacy_payload(payload):
                result = self._process_legacy(payload, bucket, key)
            else:
                result = self._process_standard(payload, bucket, key)
        except ValueError as exc:
            self._post_callback(
                payload, correlation,
                status="error", error_code=ERR_VALIDATION, error_message=str(exc),
            )
            raise
        except RenderError as exc:
            self._post_callback(
                payload, correlation,
                status="error", error_code=ERR_RENDER_FAILED, error_message=str(exc),
            )
            raise
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("NoSuchKey", "NoSuchBucket"):
                error_code = ERR_SOURCE_NOT_FOUND
            else:
                error_code = ERR_DEST_WRITE_FAILED
            self._post_callback(
                payload, correlation,
                status="error", error_code=error_code, error_message=str(exc),
            )
            raise
        except Exception as exc:
            self._post_callback(
                payload, correlation,
                status="error", error_code=ERR_INTERNAL, error_message=str(exc),
            )
            raise

        callback_sent = self._post_callback(
            payload, correlation,
            status="complete",
            dest_bucket=result.get("dest_bucket", ""),
            dest_key=result.get("output_key", ""),
            bytes_=result.get("bytes", 0),
            s3_etag=result.get("s3_etag", ""),
            width=result.get("width", 0),
            height=result.get("height", 0),
        )
        result["callback_sent"] = callback_sent
        return result

    def _process_standard(
        self, payload: dict, trigger_bucket: str, trigger_key: str
    ) -> GenerationResult:
        """Process a trigger using the standard schema."""
        self._validate_payload(payload)

        config = payload["config"]
        output_format = payload.get("output_format", DEFAULT_FORMAT).lower()
        if output_format not in SUPPORTED_FORMATS:
            output_format = DEFAULT_FORMAT
        output_quality = payload.get("output_quality", DEFAULT_QUALITY)
        is_thumbnail = payload.get("is_thumbnail", False)

        bg_key = f"backgrounds/{payload['background_source']}.jpg"
        bg_bytes = self._download(config["source_bucket"], bg_key)
        try:
            background = Image.open(BytesIO(bg_bytes)).convert("RGBA")
        except (UnidentifiedImageError, OSError) as exc:
            raise RenderError(f"Failed to open background image: {exc}") from exc

        overlay = self.generate_overlay(
            background=background,
            title=payload["title"],
            authors=payload["authors"],
        )

        # Write full-size image
        output_key = (
            f"{config['public_path']}/{payload['product_part_number']}.{output_format}"
        )
        image_bytes = self._encode_image(overlay, output_format, output_quality)
        s3_etag = self._upload(
            config["dest_bucket"], output_key, image_bytes, output_format
        )
        logger.info(
            "Wrote overlay to s3://%s/%s", config["dest_bucket"], output_key
        )

        # Write thumbnail if requested
        thumbnail_key = ""
        if is_thumbnail:
            thumb = overlay.copy()
            thumb.thumbnail(THUMBNAIL_SIZE, Image.Resampling.LANCZOS)
            thumb_key_name = (
                f"{config['public_path']}/{payload['product_part_number']}"
                f"_thumb.{output_format}"
            )
            thumb_bytes = self._encode_image(thumb, output_format, output_quality)
            self._upload(
                config["dest_bucket"], thumb_key_name, thumb_bytes, output_format
            )
            thumbnail_key = thumb_key_name
            logger.info(
                "Wrote thumbnail to s3://%s/%s", config["dest_bucket"], thumb_key_name
            )

        # Delete trigger JSON on success
        self._s3.delete_object(Bucket=trigger_bucket, Key=trigger_key)
        logger.info("Deleted trigger s3://%s/%s", trigger_bucket, trigger_key)

        return GenerationResult(
            output_key=output_key,
            thumbnail_key=thumbnail_key,
            width=overlay.width,
            height=overlay.height,
            format=output_format,
            bytes=len(image_bytes),
            s3_etag=s3_etag,
            dest_bucket=config["dest_bucket"],
        )

    def _process_legacy(
        self, payload: dict, trigger_bucket: str, trigger_key: str
    ) -> GenerationResult:
        """Process a trigger using the legacy Drupal schema.

        Legacy format:
        {
            "sourceBucket": "ieee-conference-cloud-uploads",
            "sourceName": "video-image-backgrounds/conferences/bg.jpg",
            "destBucket": "ieee-conference-cloud-bulk-uploads",
            "destName": "SPS/output.jpg",
            "overlay": [
                {
                    "text": "Title text",
                    "attributes": [{"attr": "font-size", "value": "64px"}, ...],
                    "rowHeightPad": "30"
                }
            ]
        }
        """
        self._validate_legacy_payload(payload)
        logger.info("Detected legacy Drupal trigger schema")

        # Stage prefix: dev/staging buckets are isolated from prod by a
        # `dev-` / `staging-` prefix on the bucket name; prod has no prefix.
        # `_apply_stage_prefix` is idempotent — Drupal's IPLR module also
        # prefixes via `S3UtilityTrait::prefixBucket()`, so trigger payloads
        # already arrive `dev-`/`staging-` qualified; applying the prefix
        # blindly produced `dev-dev-...` NoSuchBucket errors (see CC3-870
        # follow-up). Mirrors Node.js handler.js:26-36 with idempotency.
        prefix = _stage_prefix()
        source_bucket = _apply_stage_prefix(payload["sourceBucket"], prefix)
        source_name = payload["sourceName"]
        dest_bucket = _apply_stage_prefix(payload["destBucket"], prefix)
        dest_name = payload["destName"]
        overlay_specs = payload["overlay"]

        # Load background image
        bg_bytes = self._download(source_bucket, source_name)
        try:
            background = Image.open(BytesIO(bg_bytes)).convert("RGBA")
        except (UnidentifiedImageError, OSError) as exc:
            raise RenderError(f"Failed to open background image: {exc}") from exc

        # Extract text and styling from overlay specs
        overlay = self.generate_legacy_overlay(
            background=background,
            overlay_specs=overlay_specs,
        )

        # Determine output format from dest key extension
        output_format = dest_name.rsplit(".", 1)[-1].lower() if "." in dest_name else DEFAULT_FORMAT
        if output_format not in SUPPORTED_FORMATS:
            output_format = DEFAULT_FORMAT
        output_quality = DEFAULT_QUALITY

        # Write output image
        image_bytes = self._encode_image(overlay, output_format, output_quality)
        s3_etag = self._upload(dest_bucket, dest_name, image_bytes, output_format)
        logger.info("Wrote overlay to s3://%s/%s", dest_bucket, dest_name)

        # Delete trigger JSON on success
        self._s3.delete_object(Bucket=trigger_bucket, Key=trigger_key)
        logger.info("Deleted trigger s3://%s/%s", trigger_bucket, trigger_key)

        return GenerationResult(
            output_key=dest_name,
            thumbnail_key="",
            width=overlay.width,
            height=overlay.height,
            format=output_format,
            bytes=len(image_bytes),
            s3_etag=s3_etag,
            dest_bucket=dest_bucket,
        )

    def generate_overlay(
        self,
        background: Image.Image,
        title: str,
        authors: str,
    ) -> Image.Image:
        """Apply text overlays to a background image.

        Text is horizontally centered with a drop shadow for contrast.
        Font sizes and positioning scale proportionally to image dimensions.

        Args:
            background: PIL Image to draw on (will be copied).
            title: Product title text.
            authors: Author names text.

        Returns:
            New PIL Image with overlays applied.
        """
        img = background.copy()
        draw = ImageDraw.Draw(img)

        w, h = img.size

        title_font_size = max(20, int(h * TITLE_FONT_RATIO))
        author_font_size = max(14, int(h * AUTHOR_FONT_RATIO))
        line_spacing = int(h * LINE_SPACING_RATIO)
        author_gap = int(h * AUTHOR_GAP_RATIO)

        title_font = _load_font(title_font_size, bold=True)
        author_font = _load_font(author_font_size, bold=False)

        # Calculate wrap width based on image width and font size
        # Estimate chars per line: usable width (~80% of image) / avg char width
        avg_char_width = title_font_size * 0.55
        title_wrap_width = max(15, int((w * 0.80) / avg_char_width))
        avg_author_char_width = author_font_size * 0.55
        author_wrap_width = max(20, int((w * 0.80) / avg_author_char_width))

        # Draw title (word-wrapped, centered)
        title_lines = _wrap_and_truncate(title, title_wrap_width, TITLE_MAX_LINES)
        y = int(h * TITLE_Y_RATIO)
        for line in title_lines:
            bbox = draw.textbbox((0, 0), line, font=title_font)
            text_w = bbox[2] - bbox[0]
            x = (w - text_w) // 2
            # Shadow
            draw.text(
                (x + SHADOW_OFFSET, y + SHADOW_OFFSET),
                line, fill=SHADOW_COLOR, font=title_font,
            )
            # Main text
            draw.text((x, y), line, fill="white", font=title_font)
            y += title_font_size + line_spacing

        # Draw authors (word-wrapped, centered)
        author_lines = _wrap_and_truncate(authors, author_wrap_width, AUTHOR_MAX_LINES)
        y += author_gap
        for line in author_lines:
            bbox = draw.textbbox((0, 0), line, font=author_font)
            text_w = bbox[2] - bbox[0]
            x = (w - text_w) // 2
            draw.text(
                (x + SHADOW_OFFSET, y + SHADOW_OFFSET),
                line, fill=SHADOW_COLOR, font=author_font,
            )
            draw.text((x, y), line, fill="white", font=author_font)
            y += author_font_size + line_spacing

        return img

    def generate_legacy_overlay(
        self,
        background: Image.Image,
        overlay_specs: list[dict],
    ) -> Image.Image:
        """Apply text overlays using the legacy Drupal overlay spec format.

        Each spec contains:
            text: The text to render
            attributes: List of {attr, value} CSS-style attribute dicts
            rowHeightPad: Padding between wrapped lines (default: 2)
            widthPadFactor: Horizontal padding as fraction of width (default: 0.04)

        Mirrors the Node.js implementation in `image-generator/`:
        - Pixel-based word wrapping (getTextRows.js)
        - Vertical anchoring branched on (y < / == / >) image center (getTextElements.js)
        - No drop shadow, no line cap

        Args:
            background: PIL Image to draw on (will be copied).
            overlay_specs: List of overlay specification dicts.

        Returns:
            New PIL Image with overlays applied.
        """
        img = background.copy()
        draw = ImageDraw.Draw(img)
        w, h = img.size

        for spec in overlay_specs:
            text = spec.get("text", "")
            if not text:
                continue

            attrs = _parse_legacy_attributes(spec.get("attributes", []))
            row_height_pad = _safe_int(
                spec.get("rowHeightPad"), LEGACY_ROW_HEIGHT_PAD_DEFAULT, "rowHeightPad"
            )
            pad_factor = _safe_float(
                spec.get("widthPadFactor"), LEGACY_PADDING_FACTOR_DEFAULT, "widthPadFactor"
            )

            font_size = attrs.get("font_size", 40)
            is_bold = attrs.get("font_weight", "").lower() in ("bold", "700", "800", "900")
            font_family = attrs.get("font_family")
            font = _load_font(font_size, bold=is_bold, family=font_family)
            fill_color = attrs.get("fill", "white")

            # Pixel-based wrapping mirrors getTextRows.js: max usable width is
            # `w - (w * padFactor)`. No max-line cap — Node.js wraps unlimited.
            max_pixel_width = int(w - (w * pad_factor))
            lines = _wrap_pixels(text, font, max_pixel_width)
            if not lines:
                continue

            # Anchoring (getTextElements.js:36-52). Faithfully ports the
            # Node.js three-branch math, including the ~half-row discontinuity
            # crossing center from below — that is Node's behavior.
            x_pct = attrs.get("x_pct")
            y_pct = attrs.get("y_pct")
            text_anchor = attrs.get("text_anchor", "middle")
            if text_anchor not in ("middle", "start", "end"):
                logger.warning(
                    "Unknown text-anchor %r — falling back to 'middle'", text_anchor,
                )
                text_anchor = "middle"

            y_anchor = int(h * y_pct / 100) if y_pct is not None else int(h * 0.2)
            padded_row_height = font_size + row_height_pad
            row_count = len(lines)
            top_y = _compute_top_y(y_anchor, h, padded_row_height, row_count)

            for i, line in enumerate(lines):
                bbox = draw.textbbox((0, 0), line, font=font)
                text_w = bbox[2] - bbox[0]

                if text_anchor == "middle":
                    x_pos = (w - text_w) // 2
                elif text_anchor == "start":
                    x_pos = int(w * x_pct / 100) if x_pct is not None else 0
                else:  # "end" — right-anchored at x_pct (or right edge)
                    if x_pct is not None:
                        x_pos = int(w * x_pct / 100) - text_w
                    else:
                        x_pos = w - text_w

                y_pos = top_y + i * padded_row_height
                draw.text((x_pos, y_pos), line, fill=fill_color, font=font)

        return img

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_legacy_payload(payload: dict) -> bool:
        """Check if the payload uses the legacy Drupal schema."""
        return "overlay" in payload and "sourceBucket" in payload

    @staticmethod
    def _validate_legacy_payload(payload: dict) -> None:
        """Validate a legacy Drupal trigger payload."""
        missing = LEGACY_FIELDS - set(payload.keys())
        if missing:
            raise ValueError(f"Missing required fields: {', '.join(sorted(missing))}")
        if not isinstance(payload.get("overlay"), list):
            raise ValueError("'overlay' must be a list")

    def _read_trigger(self, bucket: str, key: str) -> TriggerPayload:
        body = self._download(bucket, key)
        return json.loads(body)

    def _validate_payload(self, payload: dict) -> None:
        missing = REQUIRED_FIELDS - set(payload.keys())
        if missing:
            raise ValueError(f"Missing required fields: {', '.join(sorted(missing))}")

        config = payload.get("config", {})
        missing_cfg = REQUIRED_CONFIG_FIELDS - set(config.keys())
        if missing_cfg:
            raise ValueError(
                f"Missing required config fields: {', '.join(sorted(missing_cfg))}"
            )

    def _download(self, bucket: str, key: str) -> bytes:
        resp = self._s3.get_object(Bucket=bucket, Key=key)
        return resp["Body"].read()

    def _upload(
        self, bucket: str, key: str, data: bytes, output_format: str
    ) -> str:
        """Upload bytes to S3 and return the dest object's ETag.

        ETag is needed for the CC3-906 completion callback payload so Drupal
        can store it in media_metadata for audit.
        """
        content_type = "image/png" if output_format == "png" else "image/jpeg"
        resp = self._s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        return resp.get("ETag", "")

    @staticmethod
    def _encode_image(img: Image.Image, fmt: str, quality: int) -> bytes:
        buf = BytesIO()
        save_format = "PNG" if fmt == "png" else "JPEG"
        # Convert RGBA to RGB for JPEG (no alpha channel support)
        if save_format == "JPEG" and img.mode == "RGBA":
            img = img.convert("RGB")
        save_kwargs = {"format": save_format}
        if save_format == "JPEG":
            save_kwargs["quality"] = quality
        try:
            img.save(buf, **save_kwargs)
        except (OSError, ValueError) as exc:
            raise RenderError(f"Failed to encode {save_format}: {exc}") from exc
        return buf.getvalue()

    # ------------------------------------------------------------------
    # CC3-906 — Drupal callback
    # ------------------------------------------------------------------

    @staticmethod
    def _correlation_for(payload: dict) -> str:
        """Build a logging correlation tag from the trigger payload."""
        request_id = payload.get("request_id", "")
        item_id = payload.get("item_id", "")
        if request_id and item_id:
            return f"[{request_id}:{item_id}]"
        if item_id:
            return f"[{item_id}]"
        return ""

    def _post_callback(
        self,
        trigger: dict,
        correlation: str,
        *,
        status: Literal["complete", "error"],
        error_code: str | None = None,
        error_message: str | None = None,
        dest_bucket: str = "",
        dest_key: str = "",
        bytes_: int = 0,
        s3_etag: str = "",
        width: int = 0,
        height: int = 0,
    ) -> bool:
        """POST the image-overlay-callback-v1 payload to the trigger's callback_url.

        Returns True if the webhook was delivered (2xx); False on permanent
        failure or when the trigger doesn't carry callback fields (no-op).
        Webhook failure is logged but never re-raised — image generation
        succeeded and shouldn't be reverted because Drupal is unreachable.
        """
        callback_url = trigger.get("callback_url")
        secret_ref = trigger.get("callback_secret_ref")
        request_id = trigger.get("request_id")
        item_id = trigger.get("item_id")

        if not (callback_url and secret_ref and request_id and item_id):
            # Backward-compat: pre-CC3-906 triggers without webhook fields.
            logger.info(
                "%s No callback fields in trigger — skipping Drupal webhook",
                correlation or "[no-correlation]",
            )
            return False

        payload: dict = {
            "item_id": item_id,
            "request_id": request_id,
            "operation": "generate_image",
            "status": status,
        }
        if "product_part_number" in trigger:
            payload["product_part_number"] = trigger["product_part_number"]

        if status == "error":
            payload["error_code"] = error_code or ERR_INTERNAL
            payload["error_message"] = error_message or ""
        else:
            payload["dest_bucket"] = dest_bucket
            payload["dest_key"] = dest_key
            payload["bytes_transferred"] = bytes_
            payload["s3_etag"] = s3_etag
            payload["width"] = width
            payload["height"] = height

        # Best-effort: any unexpected failure here (Secrets Manager outage,
        # malformed callback_url, transport-layer crash) must NOT propagate.
        # Image generation has already succeeded; an unreachable Drupal must
        # not put the invocation into DLQ retry.
        try:
            secret = self._resolve_callback_secret(secret_ref, correlation)
            return self._webhook_sender.send(
                url=callback_url,
                secret=secret,
                payload=payload,
                correlation=correlation,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "%s Webhook delivery failed unexpectedly: %s",
                correlation or "[no-correlation]",
                exc,
            )
            return False

    def _resolve_callback_secret(self, secret_ref: str, correlation: str) -> str:
        """Resolve the HMAC signing secret.

        Tries Secrets Manager first using the trigger-supplied secret_ref;
        falls back to the DRUPAL_WEBHOOK_SECRET env var. Mirrors the
        resolution policy in src/transfer/wizard_transfer.py and
        src/orchestrator/ai_orchestrator.py for consistency across all
        Drupal-bound webhooks.
        """
        try:
            resp = self._secrets.get_secret_value(SecretId=secret_ref)
            secret = resp.get("SecretString", "")
            if secret:
                return secret
        except ClientError as exc:
            logger.info(
                "%s callback_secret_ref %s not in Secrets Manager (%s); "
                "falling back to env",
                correlation,
                secret_ref,
                exc.response.get("Error", {}).get("Code", "Unknown"),
            )

        env_secret = os.environ.get("DRUPAL_WEBHOOK_SECRET", "")
        if not env_secret:
            logger.error(
                "%s No webhook secret available "
                "(Secrets Manager fetch failed and DRUPAL_WEBHOOK_SECRET unset)",
                correlation,
            )
        return env_secret


# ------------------------------------------------------------------
# Text utilities
# ------------------------------------------------------------------


def _wrap_and_truncate(text: str, width: int, max_lines: int) -> list[str]:
    """Word-wrap text and truncate to max_lines, adding ellipsis if needed."""
    lines = textwrap.wrap(text, width=width)
    if not lines:
        return []
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        # Add ellipsis to last line
        last = lines[-1]
        if len(last) + 3 > width:
            last = last[: width - 3]
        lines[-1] = last + "..."
    return lines


def _wrap_pixels(
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
) -> list[str]:
    """Word-wrap text by measured pixel width — mirrors Node.js getTextRows.

    Adds words to a row until the row's rendered width meets or exceeds
    `max_width`, at which point the offending word is pushed to the next row.
    No line cap; long text wraps unlimited.
    """
    if not text:
        return []
    rows: list[str] = []
    current_row = ""
    for word in text.split(" "):
        candidate = f"{current_row} {word}" if current_row else word
        bbox = font.getbbox(candidate)
        if current_row and (bbox[2] - bbox[0]) >= max_width:
            rows.append(current_row)
            current_row = word
        else:
            current_row = candidate
    if current_row:
        rows.append(current_row)
    return rows


def _compute_top_y(
    y_anchor: int, image_height: int, padded_row_height: int, row_count: int
) -> int:
    """Port of getTextElements.js:36-52 — branches on whether the anchor
    is above, at, or below the image center.

    Returns the y-coordinate of the FIRST row's top.

    The math is preserved as-is from Node.js, including a ~half-row
    discontinuity when the anchor crosses center from below — that is
    Node's behavior, not a port artifact. See the regression test cases
    in TestComputeTopY for the exact Node-equivalent expected values.
    """
    center = image_height / 2
    if y_anchor < center:
        return y_anchor + padded_row_height // 2
    if y_anchor == center:
        return padded_row_height + int(
            y_anchor - (padded_row_height * row_count) / 2
        )
    return y_anchor - padded_row_height * (row_count - 1)


# Recognized STAGE values. Anything outside these sets — including casing
# variants of dev/staging that didn't survive normalization — raises so a
# misconfigured Lambda fails loud rather than silently routing to prod.
_STAGE_PREFIXED = ("dev", "staging")
_STAGE_NO_PREFIX = ("", "prod")


def _stage_prefix() -> str:
    """Return the bucket-name prefix for the current stage.

    `dev` → `dev-`, `staging` → `staging-`, `prod` or unset → `""`. The
    STAGE value is normalized with `.strip().lower()` so accidental casing
    or whitespace (`"DEV"`, `"staging\\n"`) is tolerated. Any other value
    raises ValueError to prevent silent routing to prod buckets.
    Mirrors Node.js handler.js:26-36 with hardened normalization.
    """
    stage = os.environ.get("STAGE", "").strip().lower()
    if stage in _STAGE_PREFIXED:
        return f"{stage}-"
    if stage in _STAGE_NO_PREFIX:
        return ""
    raise ValueError(
        f"Unrecognized STAGE={stage!r}; expected one of "
        f"{_STAGE_PREFIXED + _STAGE_NO_PREFIX}"
    )


# Known stage prefixes — used by `_apply_stage_prefix` to detect when a
# bucket name is already environment-qualified and skip a second prefix
# application. Derived from `_STAGE_PREFIXED` so new stages (e.g. "qa")
# only need to be added in one place. Mirrors `Samson-Labs/ieee-cc`'s
# `S3UtilityTrait::prefixBucket()`, which is the upstream source of bucket
# names in the legacy Drupal trigger payloads.
_KNOWN_STAGE_PREFIXES = tuple(f"{s}-" for s in _STAGE_PREFIXED)


def _apply_stage_prefix(bucket: str, prefix: str) -> str:
    """Apply the current stage prefix to a bucket name, idempotently.

    Returns `bucket` unchanged when it already starts with `prefix`
    (which covers prod / unset STAGE where `prefix == ""`, since
    `str.startswith("")` is always True) or with any other known stage
    prefix (so a `staging-...` bucket reaching a `dev-` Lambda doesn't
    get mangled to `dev-staging-...`). Otherwise prepends `prefix`.

    The Drupal IPLR module (`Samson-Labs/ieee-cc`,
    `web/modules/custom/ieee_product_load_request/src/Service/S3UtilityTrait.php`)
    applies `dev-`/`staging-` prefixes via its own `prefixBucket()` helper,
    so trigger JSON written by Drupal arrives with bucket names that are
    already qualified. The blind-prefix variant produced `dev-dev-...` and
    failed with NoSuchBucket — see the CC3-870 thread for the 2026-05-18 trace.
    """
    if bucket.startswith(prefix) or bucket.startswith(_KNOWN_STAGE_PREFIXES):
        return bucket
    return f"{prefix}{bucket}"


def _safe_int(value, default: int, field: str) -> int:
    """Parse `value` to int or return `default`, logging on failure.

    Drupal-supplied values can be empty/None/CSS-suffixed (e.g. ``"4%"``)
    and would crash the entire trigger if passed straight to ``int()``.
    """
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning(
            "Could not parse %s=%r as int; falling back to default %r",
            field, value, default,
        )
        return default


def _safe_float(value, default: float, field: str) -> float:
    """Parse `value` to float or return `default`, logging on failure."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning(
            "Could not parse %s=%r as float; falling back to default %r",
            field, value, default,
        )
        return default


def _parse_legacy_attributes(attributes: list[dict]) -> dict:
    """Parse legacy CSS-style attributes into a normalized dict.

    Input format: [{"attr": "font-size", "value": "64px"}, ...]
    Output: {"font_size": 64, "fill": "white", "x_pct": 50, ...}
    """
    result: dict = {}
    for item in attributes:
        attr = item.get("attr", "")
        value = item.get("value", "")

        if attr == "font-size":
            result["font_size"] = int(value.replace("px", ""))
        elif attr == "font-weight":
            result["font_weight"] = value
        elif attr == "font-family":
            result["font_family"] = value
        elif attr == "fill":
            result["fill"] = value
        elif attr == "text-anchor":
            result["text_anchor"] = value
        elif attr == "x":
            if value.endswith("%"):
                result["x_pct"] = float(value.replace("%", ""))
        elif attr == "y":
            if value.endswith("%"):
                result["y_pct"] = float(value.replace("%", ""))

    return result


def _normalize_family(family: str | None) -> str:
    """Map a CSS-style font-family to a canonical key (lowercased, no spaces).

    Whitespace-only and empty inputs default to opensans. Quote stripping
    handles ``"'Roboto'"`` / ``'"Courier Prime", monospace'`` shapes seen
    in the legacy Drupal CSS attribute strings.
    """
    if not family or not family.strip():
        return "opensans"
    key = family.split(",")[0].strip().strip("'\"").lower().replace(" ", "")
    return key or "opensans"


# Family → (bold path candidates, regular path candidates).
# Each list is searched in order; first hit wins. Bundled fonts live under
# /usr/share/fonts/truetype/<name>/ inside the Lambda image (see Dockerfile).
# Cross-family fallbacks (e.g. Roboto's regular list ending in OpenSans-SemiBold)
# are belt-and-braces in case the family-specific .ttf is missing from the
# container — the bundled file at the head of the list is what runs in prod.
# If even those fall through, _load_font ultimately returns Pillow's default
# bitmap font.
_FONT_FAMILY_PATHS: dict[str, tuple[list[str], list[str]]] = {
    "opensans": (
        [
            "/usr/share/fonts/truetype/opensans/OpenSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
        ],
        [
            "/usr/share/fonts/truetype/opensans/OpenSans-SemiBold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/System/Library/Fonts/Helvetica.ttc",
        ],
    ),
    "roboto": (
        [
            "/usr/share/fonts/truetype/roboto/Roboto-Bold.ttf",
            "/usr/share/fonts/truetype/opensans/OpenSans-Bold.ttf",  # fallback
        ],
        [
            "/usr/share/fonts/truetype/roboto/Roboto-Regular.ttf",
            "/usr/share/fonts/truetype/opensans/OpenSans-SemiBold.ttf",  # fallback
        ],
    ),
    "courierprime": (
        [
            "/usr/share/fonts/truetype/courierprime/CourierPrime-Bold.ttf",
            "/usr/share/fonts/truetype/opensans/OpenSans-Bold.ttf",  # fallback
        ],
        [
            "/usr/share/fonts/truetype/courierprime/CourierPrime-Regular.ttf",
            "/usr/share/fonts/truetype/opensans/OpenSans-SemiBold.ttf",  # fallback
        ],
    ),
}


def _load_font(
    size: int, *, bold: bool = True, family: str | None = None
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load a TrueType font for the requested family, falling back gracefully.

    Family lookup is case-insensitive ("Roboto", "roboto", "ROBOTO" all work).
    Unknown families fall back to OpenSans. Within a family, candidates are
    tried in order; if none load (e.g. font missing from container), the
    next family fallback is used, then Pillow's default bitmap font.
    """
    key = _normalize_family(family)
    if key not in _FONT_FAMILY_PATHS:
        logger.info("Unknown font-family %r, falling back to OpenSans", family)
        key = "opensans"

    bold_paths, regular_paths = _FONT_FAMILY_PATHS[key]
    font_paths = bold_paths if bold else regular_paths
    for path in font_paths:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    logger.warning(
        "No TrueType font found for family=%r bold=%s, using Pillow default",
        family, bold,
    )
    return ImageFont.load_default()
