"""Tests for ImageOverlayGenerator.

Covers: overlay rendering, text wrapping/truncation, thumbnail generation,
trigger JSON validation, S3 integration, output formats, error handling.
"""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock, call

import pytest
from botocore.exceptions import ClientError
from PIL import Image

from src.generators.image_overlay_generator import (
    AUTHOR_MAX_LINES,
    DEFAULT_FORMAT,
    DEFAULT_QUALITY,
    SUPPORTED_FORMATS,
    THUMBNAIL_SIZE,
    TITLE_MAX_LINES,
    TITLE_WRAP_WIDTH,
    GenerationResult,
    ImageOverlayGenerator,
    _wrap_and_truncate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_background(width: int = 800, height: int = 600) -> Image.Image:
    """Create a solid-color background image for testing."""
    return Image.new("RGBA", (width, height), color=(0, 0, 128, 255))


def _background_bytes(width: int = 800, height: int = 600) -> bytes:
    """Create background image as JPEG bytes."""
    img = Image.new("RGB", (width, height), color=(0, 0, 128))
    buf = BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def _make_trigger_payload(**overrides) -> dict:
    """Create a valid trigger payload dict."""
    payload = {
        "product_part_number": "STD-12345",
        "title": "A Sample Product Title",
        "authors": "Jane Doe, John Smith",
        "config": {
            "source_bucket": "ieee-rc-assets",
            "dest_bucket": "ieee-rc-public",
            "public_path": "images/products",
        },
        "background_source": "ieee",
        "output_format": "jpg",
        "output_quality": 85,
    }
    payload.update(overrides)
    return payload


def _make_trigger_bytes(**overrides) -> bytes:
    return json.dumps(_make_trigger_payload(**overrides)).encode()


def _mock_s3_for_trigger(s3_mock, trigger_payload: dict | None = None, bg_size=(800, 600)):
    """Set up S3 mock to return trigger JSON and background image."""
    trigger = trigger_payload or _make_trigger_payload()
    trigger_bytes = json.dumps(trigger).encode()
    bg_bytes = _background_bytes(*bg_size)

    def get_object_side_effect(Bucket, Key):
        if Key.endswith(".json"):
            return {"Body": BytesIO(trigger_bytes)}
        if Key.startswith("backgrounds/"):
            return {"Body": BytesIO(bg_bytes)}
        raise ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "Not found"}}, "GetObject"
        )

    s3_mock.get_object.side_effect = get_object_side_effect


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def generator():
    return ImageOverlayGenerator(s3_client=MagicMock())


@pytest.fixture
def background():
    return _make_background()


# ---------------------------------------------------------------------------
# Tests: generate_overlay (no S3)
# ---------------------------------------------------------------------------


class TestGenerateOverlay:
    def test_returns_image_with_same_dimensions(self, generator, background):
        result = generator.generate_overlay(
            background=background,
            title="Test Title",
            authors="Author One",
        )
        assert isinstance(result, Image.Image)
        assert result.size == background.size

    def test_does_not_mutate_original(self, generator, background):
        original_data = list(background.getdata())
        generator.generate_overlay(
            background=background,
            title="Test",
            authors="Author",
        )
        assert list(background.getdata()) == original_data

    def test_overlay_modifies_pixels(self, generator, background):
        result = generator.generate_overlay(
            background=background,
            title="Visible Text",
            authors="Author Name",
        )
        # At least some pixels should differ from the solid background
        assert list(result.getdata()) != list(background.getdata())

    def test_empty_title_and_authors(self, generator, background):
        """Empty strings should not crash."""
        result = generator.generate_overlay(
            background=background,
            title="",
            authors="",
        )
        assert isinstance(result, Image.Image)


# ---------------------------------------------------------------------------
# Tests: _wrap_and_truncate
# ---------------------------------------------------------------------------


class TestWrapAndTruncate:
    def test_short_text_single_line(self):
        lines = _wrap_and_truncate("Short title", width=30, max_lines=3)
        assert lines == ["Short title"]

    def test_wraps_long_text(self):
        long_title = "This is a very long title that should be wrapped across multiple lines"
        lines = _wrap_and_truncate(long_title, width=30, max_lines=3)
        assert len(lines) <= 3
        for line in lines:
            assert len(line) <= 33  # width + ellipsis allowance

    def test_truncates_with_ellipsis(self):
        very_long = " ".join(["word"] * 50)
        lines = _wrap_and_truncate(very_long, width=20, max_lines=2)
        assert len(lines) == 2
        assert lines[-1].endswith("...")

    def test_empty_string(self):
        assert _wrap_and_truncate("", width=30, max_lines=3) == []

    def test_exact_fit_no_ellipsis(self):
        text = "Line one here"
        lines = _wrap_and_truncate(text, width=30, max_lines=3)
        assert not lines[-1].endswith("...")

    def test_respects_max_lines(self):
        text = "A " * 100
        for max_lines in [1, 2, 3, 5]:
            lines = _wrap_and_truncate(text, width=10, max_lines=max_lines)
            assert len(lines) <= max_lines


# ---------------------------------------------------------------------------
# Tests: process_trigger (S3 integration)
# ---------------------------------------------------------------------------


class TestProcessTrigger:
    def test_generates_and_uploads_image(self):
        s3_mock = MagicMock()
        _mock_s3_for_trigger(s3_mock)
        generator = ImageOverlayGenerator(s3_client=s3_mock)

        result = generator.process_trigger(
            bucket="trigger-bucket", key="actions/job-001.json"
        )

        assert result["output_key"] == "images/products/STD-12345.jpg"
        assert result["format"] == "jpg"
        assert result["width"] == 800
        assert result["height"] == 600
        assert result["thumbnail_key"] == ""

        # Verify image was uploaded
        put_calls = s3_mock.put_object.call_args_list
        assert len(put_calls) == 1
        assert put_calls[0][1]["Bucket"] == "ieee-rc-public"
        assert put_calls[0][1]["Key"] == "images/products/STD-12345.jpg"
        assert put_calls[0][1]["ContentType"] == "image/jpeg"

    def test_deletes_trigger_on_success(self):
        s3_mock = MagicMock()
        _mock_s3_for_trigger(s3_mock)
        generator = ImageOverlayGenerator(s3_client=s3_mock)

        generator.process_trigger(bucket="trigger-bucket", key="actions/job.json")

        s3_mock.delete_object.assert_called_once_with(
            Bucket="trigger-bucket", Key="actions/job.json"
        )

    def test_does_not_delete_trigger_on_failure(self):
        s3_mock = MagicMock()
        s3_mock.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "Not found"}}, "GetObject"
        )
        generator = ImageOverlayGenerator(s3_client=s3_mock)

        with pytest.raises(ClientError):
            generator.process_trigger(bucket="b", key="actions/bad.json")

        s3_mock.delete_object.assert_not_called()

    def test_generates_thumbnail_when_requested(self):
        s3_mock = MagicMock()
        trigger = _make_trigger_payload(is_thumbnail=True)
        _mock_s3_for_trigger(s3_mock, trigger)
        generator = ImageOverlayGenerator(s3_client=s3_mock)

        result = generator.process_trigger(
            bucket="trigger-bucket", key="actions/job.json"
        )

        assert result["thumbnail_key"] == "images/products/STD-12345_thumb.jpg"
        # Should have 2 put_object calls (full + thumbnail)
        assert s3_mock.put_object.call_count == 2

    def test_thumbnail_is_smaller(self):
        s3_mock = MagicMock()
        trigger = _make_trigger_payload(is_thumbnail=True)
        _mock_s3_for_trigger(s3_mock, trigger, bg_size=(1200, 900))
        generator = ImageOverlayGenerator(s3_client=s3_mock)

        result = generator.process_trigger(
            bucket="trigger-bucket", key="actions/job.json"
        )

        # Full image should be original size
        assert result["width"] == 1200
        assert result["height"] == 900

        # Thumbnail upload should have smaller image data
        thumb_call = s3_mock.put_object.call_args_list[1]
        thumb_bytes = thumb_call[1]["Body"]
        thumb_img = Image.open(BytesIO(thumb_bytes))
        assert thumb_img.width <= THUMBNAIL_SIZE[0]
        assert thumb_img.height <= THUMBNAIL_SIZE[1]


# ---------------------------------------------------------------------------
# Tests: output formats
# ---------------------------------------------------------------------------


class TestOutputFormats:
    def test_png_output(self):
        s3_mock = MagicMock()
        trigger = _make_trigger_payload(output_format="png")
        _mock_s3_for_trigger(s3_mock, trigger)
        generator = ImageOverlayGenerator(s3_client=s3_mock)

        result = generator.process_trigger(
            bucket="trigger-bucket", key="actions/job.json"
        )

        assert result["format"] == "png"
        assert result["output_key"].endswith(".png")
        put_kwargs = s3_mock.put_object.call_args[1]
        assert put_kwargs["ContentType"] == "image/png"

    def test_unsupported_format_falls_back_to_jpg(self):
        s3_mock = MagicMock()
        trigger = _make_trigger_payload(output_format="bmp")
        _mock_s3_for_trigger(s3_mock, trigger)
        generator = ImageOverlayGenerator(s3_client=s3_mock)

        result = generator.process_trigger(
            bucket="trigger-bucket", key="actions/job.json"
        )

        assert result["format"] == "jpg"

    def test_default_format_is_jpg(self):
        s3_mock = MagicMock()
        trigger = _make_trigger_payload()
        del trigger["output_format"]
        _mock_s3_for_trigger(s3_mock, trigger)
        generator = ImageOverlayGenerator(s3_client=s3_mock)

        result = generator.process_trigger(
            bucket="trigger-bucket", key="actions/job.json"
        )

        assert result["format"] == "jpg"

    def test_custom_quality(self):
        s3_mock = MagicMock()
        trigger = _make_trigger_payload(output_quality=50)
        _mock_s3_for_trigger(s3_mock, trigger)
        generator = ImageOverlayGenerator(s3_client=s3_mock)

        result = generator.process_trigger(
            bucket="trigger-bucket", key="actions/job.json"
        )

        # Should succeed — quality affects JPEG compression
        assert result["format"] == "jpg"


# ---------------------------------------------------------------------------
# Tests: validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_missing_required_field_raises(self, generator):
        for field in ["product_part_number", "title", "authors", "config", "background_source"]:
            payload = _make_trigger_payload()
            del payload[field]
            with pytest.raises(ValueError, match="Missing required fields"):
                generator._validate_payload(payload)

    def test_missing_config_field_raises(self, generator):
        for field in ["source_bucket", "dest_bucket", "public_path"]:
            payload = _make_trigger_payload()
            del payload["config"][field]
            with pytest.raises(ValueError, match="Missing required config fields"):
                generator._validate_payload(payload)

    def test_valid_payload_does_not_raise(self, generator):
        payload = _make_trigger_payload()
        generator._validate_payload(payload)  # should not raise


# ---------------------------------------------------------------------------
# Tests: S3 error handling
# ---------------------------------------------------------------------------


class TestS3Errors:
    def test_background_not_found_raises(self):
        s3_mock = MagicMock()
        trigger_bytes = _make_trigger_bytes()

        def get_object_side_effect(Bucket, Key):
            if Key.endswith(".json"):
                return {"Body": BytesIO(trigger_bytes)}
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
                "GetObject",
            )

        s3_mock.get_object.side_effect = get_object_side_effect
        generator = ImageOverlayGenerator(s3_client=s3_mock)

        with pytest.raises(ClientError) as exc_info:
            generator.process_trigger(bucket="b", key="actions/job.json")
        assert exc_info.value.response["Error"]["Code"] == "NoSuchKey"
        s3_mock.delete_object.assert_not_called()

    def test_upload_failure_raises(self):
        s3_mock = MagicMock()
        _mock_s3_for_trigger(s3_mock)
        s3_mock.put_object.side_effect = ClientError(
            {"Error": {"Code": "InternalError", "Message": "S3 down"}},
            "PutObject",
        )
        generator = ImageOverlayGenerator(s3_client=s3_mock)

        with pytest.raises(ClientError):
            generator.process_trigger(bucket="b", key="actions/job.json")
        s3_mock.delete_object.assert_not_called()

    def test_trigger_not_found_raises(self):
        s3_mock = MagicMock()
        s3_mock.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
            "GetObject",
        )
        generator = ImageOverlayGenerator(s3_client=s3_mock)

        with pytest.raises(ClientError):
            generator.process_trigger(bucket="b", key="actions/missing.json")
        s3_mock.delete_object.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: image encoding
# ---------------------------------------------------------------------------


class TestImageEncoding:
    def test_jpeg_encoding(self):
        img = _make_background()
        data = ImageOverlayGenerator._encode_image(img, "jpg", 85)
        # JPEG magic bytes
        assert data[:2] == b"\xff\xd8"

    def test_png_encoding(self):
        img = _make_background()
        data = ImageOverlayGenerator._encode_image(img, "png", 85)
        # PNG magic bytes
        assert data[:4] == b"\x89PNG"

    def test_jpeg_converts_rgba_to_rgb(self):
        """JPEG does not support alpha; RGBA should be converted."""
        img = Image.new("RGBA", (100, 100), (255, 0, 0, 128))
        data = ImageOverlayGenerator._encode_image(img, "jpg", 85)
        # Should succeed and produce valid JPEG
        reloaded = Image.open(BytesIO(data))
        assert reloaded.mode == "RGB"
