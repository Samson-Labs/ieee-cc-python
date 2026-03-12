"""Image Overlay Generation module for IEEE Content Conversion pipeline.

Generates product overlay images from JSON trigger files. Loads a background
image from S3, applies text overlays (title, authors, logo) using Pillow,
and writes output to the destination bucket.
"""

from __future__ import annotations

import json
import logging
import textwrap
from io import BytesIO
from typing import TypedDict

import boto3
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

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


class GenerationResult(TypedDict):
    output_key: str
    thumbnail_key: str
    width: int
    height: int
    format: str


class ImageOverlayGenerator:
    """Generates product overlay images from trigger JSON files in S3."""

    def __init__(self, s3_client=None):
        self._s3 = s3_client or boto3.client("s3")

    def process_trigger(self, bucket: str, key: str) -> GenerationResult:
        """Process an S3 trigger JSON file end-to-end.

        1. Read and validate the trigger JSON
        2. Load the background image
        3. Generate the overlay image (and thumbnail if requested)
        4. Write output(s) to the destination bucket
        5. Delete the trigger JSON on success

        Args:
            bucket: S3 bucket containing the trigger JSON.
            key: S3 object key of the trigger JSON (e.g. actions/xyz.json).

        Returns:
            GenerationResult with output key, dimensions, and format.
        """
        logger.info("Processing trigger s3://%s/%s", bucket, key)

        payload = self._read_trigger(bucket, key)
        self._validate_payload(payload)

        config = payload["config"]
        output_format = payload.get("output_format", DEFAULT_FORMAT).lower()
        if output_format not in SUPPORTED_FORMATS:
            output_format = DEFAULT_FORMAT
        output_quality = payload.get("output_quality", DEFAULT_QUALITY)
        is_thumbnail = payload.get("is_thumbnail", False)

        bg_key = f"backgrounds/{payload['background_source']}.jpg"
        bg_bytes = self._download(config["source_bucket"], bg_key)
        background = Image.open(BytesIO(bg_bytes)).convert("RGBA")

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
        self._upload(config["dest_bucket"], output_key, image_bytes, output_format)
        logger.info(
            "Wrote overlay to s3://%s/%s", config["dest_bucket"], output_key
        )

        # Write thumbnail if requested
        thumbnail_key = ""
        if is_thumbnail:
            thumb = overlay.copy()
            thumb.thumbnail(THUMBNAIL_SIZE, Image.LANCZOS)
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
        self._s3.delete_object(Bucket=bucket, Key=key)
        logger.info("Deleted trigger s3://%s/%s", bucket, key)

        return GenerationResult(
            output_key=output_key,
            thumbnail_key=thumbnail_key,
            width=overlay.width,
            height=overlay.height,
            format=output_format,
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
        content_h = h * (1 - LOGO_BAR_RATIO)  # area above logo bar

        title_font_size = max(20, int(h * TITLE_FONT_RATIO))
        author_font_size = max(14, int(h * AUTHOR_FONT_RATIO))
        line_spacing = int(h * LINE_SPACING_RATIO)
        author_gap = int(h * AUTHOR_GAP_RATIO)

        title_font = _load_font(title_font_size)
        author_font = _load_font(author_font_size)

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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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
    ) -> None:
        content_type = "image/png" if output_format == "png" else "image/jpeg"
        self._s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=data,
            ContentType=content_type,
        )

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
        img.save(buf, **save_kwargs)
        return buf.getvalue()


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


def _load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load a TrueType font, falling back to Pillow's default."""
    # Try common system font paths
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Linux/Lambda
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",  # macOS
        "/System/Library/Fonts/SFNSDisplay.ttf",  # macOS fallback
    ]
    for path in font_paths:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    logger.warning("No TrueType font found, using Pillow default bitmap font")
    return ImageFont.load_default()
