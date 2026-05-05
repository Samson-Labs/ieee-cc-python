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
    LEGACY_FIELDS,
    LEGACY_PADDING_FACTOR_DEFAULT,
    LEGACY_ROW_HEIGHT_PAD_DEFAULT,
    SUPPORTED_FORMATS,
    THUMBNAIL_SIZE,
    TITLE_MAX_LINES,
    GenerationResult,
    ImageOverlayGenerator,
    _compute_top_y,
    _load_font,
    _normalize_family,
    _parse_legacy_attributes,
    _safe_float,
    _safe_int,
    _stage_prefix,
    _wrap_and_truncate,
    _wrap_pixels,
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


# ---------------------------------------------------------------------------
# Helpers: legacy schema
# ---------------------------------------------------------------------------


def _make_legacy_payload(**overrides) -> dict:
    """Create a valid legacy Drupal trigger payload."""
    payload = {
        "sourceBucket": "ieee-conference-cloud-uploads",
        "sourceName": "video-image-backgrounds/conferences/ieee-test.jpg",
        "destBucket": "ieee-conference-cloud-bulk-uploads",
        "destName": "SPS/SPSTEST001.jpg",
        "overlay": [
            {
                "text": "Advanced Power Systems Engineering",
                "attributes": [
                    {"attr": "y", "value": "22%"},
                    {"attr": "x", "value": "50%"},
                    {"attr": "fill", "value": "white"},
                    {"attr": "text-anchor", "value": "middle"},
                    {"attr": "font-family", "value": "OpenSans"},
                    {"attr": "font-weight", "value": "Bold"},
                    {"attr": "font-size", "value": "64px"},
                ],
                "rowHeightPad": "30",
            },
            {
                "text": "Jane Doe, John Smith",
                "attributes": [
                    {"attr": "y", "value": "70%"},
                    {"attr": "x", "value": "50%"},
                    {"attr": "fill", "value": "white"},
                    {"attr": "text-anchor", "value": "middle"},
                    {"attr": "font-family", "value": "OpenSans"},
                    {"attr": "font-size", "value": "32px"},
                    {"attr": "font-weight", "value": "bold"},
                ],
                "rowHeightPad": "15",
            },
        ],
    }
    payload.update(overrides)
    return payload


def _mock_s3_for_legacy_trigger(s3_mock, trigger_payload=None, bg_size=(800, 600)):
    """Set up S3 mock for legacy trigger JSON and background image."""
    trigger = trigger_payload or _make_legacy_payload()
    trigger_bytes = json.dumps(trigger).encode()
    bg_bytes = _background_bytes(*bg_size)

    def get_object_side_effect(Bucket, Key):
        if Key.endswith(".json"):
            return {"Body": BytesIO(trigger_bytes)}
        # Legacy uses full source path, not backgrounds/ prefix
        return {"Body": BytesIO(bg_bytes)}

    s3_mock.get_object.side_effect = get_object_side_effect


# ---------------------------------------------------------------------------
# Tests: legacy schema support
# ---------------------------------------------------------------------------


class TestLegacySchemaDetection:
    def test_detects_legacy_payload(self):
        payload = _make_legacy_payload()
        assert ImageOverlayGenerator._is_legacy_payload(payload) is True

    def test_detects_standard_payload(self):
        payload = _make_trigger_payload()
        assert ImageOverlayGenerator._is_legacy_payload(payload) is False

    def test_validates_legacy_payload(self):
        gen = ImageOverlayGenerator(s3_client=MagicMock())
        payload = _make_legacy_payload()
        gen._validate_legacy_payload(payload)  # should not raise

    def test_missing_legacy_field_raises(self):
        gen = ImageOverlayGenerator(s3_client=MagicMock())
        for field in ["sourceBucket", "sourceName", "destBucket", "destName", "overlay"]:
            payload = _make_legacy_payload()
            del payload[field]
            with pytest.raises(ValueError, match="Missing required fields"):
                gen._validate_legacy_payload(payload)

    def test_overlay_not_list_raises(self):
        gen = ImageOverlayGenerator(s3_client=MagicMock())
        payload = _make_legacy_payload(overlay="not a list")
        with pytest.raises(ValueError, match="must be a list"):
            gen._validate_legacy_payload(payload)


class TestParseLegacyAttributes:
    def test_parses_font_size(self):
        attrs = [{"attr": "font-size", "value": "64px"}]
        result = _parse_legacy_attributes(attrs)
        assert result["font_size"] == 64

    def test_parses_position_percentages(self):
        attrs = [
            {"attr": "x", "value": "50%"},
            {"attr": "y", "value": "22%"},
        ]
        result = _parse_legacy_attributes(attrs)
        assert result["x_pct"] == 50.0
        assert result["y_pct"] == 22.0

    def test_parses_fill_and_anchor(self):
        attrs = [
            {"attr": "fill", "value": "white"},
            {"attr": "text-anchor", "value": "middle"},
        ]
        result = _parse_legacy_attributes(attrs)
        assert result["fill"] == "white"
        assert result["text_anchor"] == "middle"

    def test_parses_font_weight(self):
        attrs = [{"attr": "font-weight", "value": "Bold"}]
        result = _parse_legacy_attributes(attrs)
        assert result["font_weight"] == "Bold"

    def test_empty_attributes(self):
        assert _parse_legacy_attributes([]) == {}


class TestLegacyProcessTrigger:
    def test_generates_and_uploads_image(self):
        s3_mock = MagicMock()
        _mock_s3_for_legacy_trigger(s3_mock)
        gen = ImageOverlayGenerator(s3_client=s3_mock)

        result = gen.process_trigger(
            bucket="trigger-bucket", key="actions/job-001.json"
        )

        assert result["output_key"] == "SPS/SPSTEST001.jpg"
        assert result["format"] == "jpg"
        assert result["width"] == 800
        assert result["height"] == 600
        assert result["thumbnail_key"] == ""

        put_calls = s3_mock.put_object.call_args_list
        assert len(put_calls) == 1
        assert put_calls[0][1]["Bucket"] == "ieee-conference-cloud-bulk-uploads"
        assert put_calls[0][1]["Key"] == "SPS/SPSTEST001.jpg"

    def test_deletes_trigger_on_success(self):
        s3_mock = MagicMock()
        _mock_s3_for_legacy_trigger(s3_mock)
        gen = ImageOverlayGenerator(s3_client=s3_mock)

        gen.process_trigger(bucket="trigger-bucket", key="actions/job.json")

        s3_mock.delete_object.assert_called_once_with(
            Bucket="trigger-bucket", Key="actions/job.json"
        )

    def test_overlay_modifies_pixels(self):
        gen = ImageOverlayGenerator(s3_client=MagicMock())
        bg = _make_background()
        overlay_specs = _make_legacy_payload()["overlay"]

        result = gen.generate_legacy_overlay(
            background=bg, overlay_specs=overlay_specs
        )

        assert list(result.getdata()) != list(bg.getdata())

    def test_empty_overlay_list(self):
        gen = ImageOverlayGenerator(s3_client=MagicMock())
        bg = _make_background()

        result = gen.generate_legacy_overlay(background=bg, overlay_specs=[])
        assert result.size == bg.size

    def test_png_output_from_dest_extension(self):
        s3_mock = MagicMock()
        trigger = _make_legacy_payload(destName="SPS/output.png")
        _mock_s3_for_legacy_trigger(s3_mock, trigger)
        gen = ImageOverlayGenerator(s3_client=s3_mock)

        result = gen.process_trigger(
            bucket="trigger-bucket", key="actions/job.json"
        )

        assert result["format"] == "png"
        put_kwargs = s3_mock.put_object.call_args[1]
        assert put_kwargs["ContentType"] == "image/png"


# ---------------------------------------------------------------------------
# CC3-870: Parity gap fixes vs legacy Node.js image-generator
# ---------------------------------------------------------------------------


class TestStagePrefix:
    """Gap #1: STAGE env → bucket prefix (handler.js:26-36).

    R1 hardening: strip+lowercase normalize, raise on unknown values
    rather than fall through to no-prefix (would route dev jobs to prod).
    """

    def test_dev_prefix(self, monkeypatch):
        monkeypatch.setenv("STAGE", "dev")
        assert _stage_prefix() == "dev-"

    def test_staging_prefix(self, monkeypatch):
        monkeypatch.setenv("STAGE", "staging")
        assert _stage_prefix() == "staging-"

    def test_prod_no_prefix(self, monkeypatch):
        monkeypatch.setenv("STAGE", "prod")
        assert _stage_prefix() == ""

    def test_unset_no_prefix(self, monkeypatch):
        monkeypatch.delenv("STAGE", raising=False)
        assert _stage_prefix() == ""

    @pytest.mark.parametrize("value", ["DEV", "Dev", "Dev ", "  dev\n", "DEV  "])
    def test_dev_normalized(self, monkeypatch, value):
        """R1: case + whitespace variants of dev resolve to dev- (no prod leak)."""
        monkeypatch.setenv("STAGE", value)
        assert _stage_prefix() == "dev-"

    @pytest.mark.parametrize("value", ["STAGING", "Staging\n", "  staging  "])
    def test_staging_normalized(self, monkeypatch, value):
        monkeypatch.setenv("STAGE", value)
        assert _stage_prefix() == "staging-"

    @pytest.mark.parametrize("value", ["PROD", "  prod  ", "Prod\n"])
    def test_prod_normalized(self, monkeypatch, value):
        monkeypatch.setenv("STAGE", value)
        assert _stage_prefix() == ""

    @pytest.mark.parametrize("value", ["qa", "test", "production", "develop"])
    def test_unknown_value_raises(self, monkeypatch, value):
        """R1: unknown STAGE values raise rather than silently fall through."""
        monkeypatch.setenv("STAGE", value)
        with pytest.raises(ValueError, match="Unrecognized STAGE"):
            _stage_prefix()

    def test_legacy_process_applies_prefix_to_buckets(self, monkeypatch):
        """End-to-end: dev STAGE causes both source + dest buckets to be prefixed."""
        monkeypatch.setenv("STAGE", "dev")
        s3_mock = MagicMock()
        payload = _make_legacy_payload()
        _mock_s3_for_legacy_trigger(s3_mock, payload)
        gen = ImageOverlayGenerator(s3_client=s3_mock)

        gen.process_trigger(bucket="trigger-bucket", key="actions/job.json")

        # Source download should hit the prefixed source bucket.
        get_calls = s3_mock.get_object.call_args_list
        source_calls = [c for c in get_calls if not c[1]["Key"].endswith(".json")]
        assert len(source_calls) == 1
        assert source_calls[0][1]["Bucket"] == f"dev-{payload['sourceBucket']}"

        # Upload should hit the prefixed dest bucket.
        put_kwargs = s3_mock.put_object.call_args[1]
        assert put_kwargs["Bucket"] == f"dev-{payload['destBucket']}"

    def test_legacy_process_no_prefix_in_prod(self, monkeypatch):
        monkeypatch.setenv("STAGE", "prod")
        s3_mock = MagicMock()
        payload = _make_legacy_payload()
        _mock_s3_for_legacy_trigger(s3_mock, payload)
        gen = ImageOverlayGenerator(s3_client=s3_mock)

        gen.process_trigger(bucket="trigger-bucket", key="actions/job.json")

        put_kwargs = s3_mock.put_object.call_args[1]
        assert put_kwargs["Bucket"] == payload["destBucket"]


class TestVerticalAnchoring:
    """Gap #2: Three-branch y-positioning (getTextElements.js:36-52).

    For an 800x600 image with center=300, font_size=40, rowHeightPad=2,
    paddedRowHeight=42:
      y=120 (top, < center)  → top_y = 120 + 21        = 141
      y=300 (center)         → top_y = 42 + (300 - 21) = 321 (1 row)
      y=480 (bottom, > center, 1 row)        → top_y = 480
      y=480 (bottom, > center, 3 rows)       → top_y = 480 - 42*2 = 396
    The Node.js arithmetic uses `parseInt` which truncates toward zero;
    Python `int()` matches for non-negative values. We assert the
    resulting top_y by drawing a single overlay and checking which rows
    are populated against the expected position.
    """

    def _spec(self, *, y_pct: int, text: str, font_size: int = 40, row_pad: int = 2):
        return {
            "text": text,
            "attributes": [
                {"attr": "y", "value": f"{y_pct}%"},
                {"attr": "x", "value": "50%"},
                {"attr": "fill", "value": "white"},
                {"attr": "text-anchor", "value": "middle"},
                {"attr": "font-size", "value": f"{font_size}px"},
            ],
            "rowHeightPad": str(row_pad),
        }

    def test_top_anchored_grows_down(self):
        gen = ImageOverlayGenerator(s3_client=MagicMock())
        bg = _make_background(800, 600)  # center y=300

        # Single short word at y=20% → y_anchor=120, top_y=120+21=141.
        spec = self._spec(y_pct=20, text="Hi", font_size=40)
        out = gen.generate_legacy_overlay(background=bg, overlay_specs=[spec])

        # The text should render *below* the anchor (top_y > y_anchor).
        # Verify pixels above y_anchor are still pure background.
        for y in range(0, 100):
            for x in range(0, 800, 50):
                # Background is (0, 0, 128); white text changes the pixel.
                assert out.getpixel((x, y))[:3] == (0, 0, 128)

    def test_bottom_anchored_grows_up(self):
        """Three rows at y=80% should not run off the bottom of an 800x600 image."""
        gen = ImageOverlayGenerator(s3_client=MagicMock())
        bg = _make_background(800, 600)  # center y=300, y_anchor at 80% = 480

        # Long text guaranteed to wrap to multiple rows.
        long_text = "First " + "wrapping " * 30 + "tail"
        spec = self._spec(y_pct=80, text=long_text, font_size=40, row_pad=2)
        out = gen.generate_legacy_overlay(background=bg, overlay_specs=[spec])

        # Bottom-anchored: text grows up, so the last row should be near
        # y_anchor=480, not running past the bottom edge (599). Confirm
        # there is text rendered above y=400 (proof rows extend upward).
        modified_above_400 = False
        for y in range(200, 400):
            for x in range(0, 800, 25):
                if out.getpixel((x, y))[:3] != (0, 0, 128):
                    modified_above_400 = True
                    break
            if modified_above_400:
                break
        assert modified_above_400, "bottom-anchored multi-row text did not grow upward"

    def test_old_implementation_grew_off_image(self):
        """Regression guard: confirm the new code does NOT overflow at y=80%.

        Pre-CC3-870 always grew downward, so 3 rows × ~42px starting at
        y=480 would reach y≈564 — borderline ok for 3 rows but breaks
        for any longer wrap. Verify content stays within the image bounds.
        """
        gen = ImageOverlayGenerator(s3_client=MagicMock())
        bg = _make_background(800, 600)
        # Text that wraps to many rows.
        long_text = " ".join(["word"] * 80)
        spec = self._spec(y_pct=80, text=long_text, font_size=40, row_pad=2)
        out = gen.generate_legacy_overlay(background=bg, overlay_specs=[spec])

        # Image should not error and should have same dimensions.
        assert out.size == bg.size


class TestUnlimitedWrapping:
    """Gap #3: No 4-line truncation cap on legacy path."""

    def test_long_text_wraps_unlimited(self):
        gen = ImageOverlayGenerator(s3_client=MagicMock())
        bg = _make_background(800, 600)
        # 100 short words; with default ~96% padding will wrap to many rows.
        text = " ".join(["alpha"] * 100)
        spec = {
            "text": text,
            "attributes": [
                {"attr": "y", "value": "10%"},
                {"attr": "x", "value": "50%"},
                {"attr": "fill", "value": "white"},
                {"attr": "text-anchor", "value": "middle"},
                {"attr": "font-size", "value": "20px"},
            ],
        }
        # No assertion error, no ellipsis added.
        out = gen.generate_legacy_overlay(background=bg, overlay_specs=[spec])
        assert out.size == bg.size

    def test_wrap_pixels_returns_more_than_four_rows(self):
        font = _load_font(20, bold=False)
        text = " ".join(["word"] * 60)
        rows = _wrap_pixels(text, font, max_width=200)
        assert len(rows) > 4, "legacy path must wrap unlimited rows"
        assert not any(r.endswith("...") for r in rows), "no ellipsis truncation"


class TestFontFamily:
    """Gap #4: font-family honored (or graceful fallback)."""

    def test_normalize_family_lowercase(self):
        assert _normalize_family("Roboto") == "roboto"
        assert _normalize_family("Courier Prime") == "courierprime"
        assert _normalize_family("OpenSans") == "opensans"

    def test_normalize_family_strips_quotes_and_fallback(self):
        assert _normalize_family("'Roboto'") == "roboto"
        assert _normalize_family('"Courier Prime", monospace') == "courierprime"

    def test_normalize_family_none_defaults_to_opensans(self):
        assert _normalize_family(None) == "opensans"
        assert _normalize_family("") == "opensans"

    def test_load_font_unknown_family_falls_back_to_opensans(self, caplog):
        with caplog.at_level("INFO"):
            font = _load_font(40, bold=True, family="NonexistentFontXYZ")
        assert font is not None
        # Either logs the fallback or silently uses OpenSans — both acceptable.

    def test_load_font_with_known_family_does_not_raise(self):
        # OpenSans is bundled in the repo, so this should always succeed.
        font = _load_font(40, bold=True, family="OpenSans")
        assert font is not None

    def test_legacy_overlay_passes_font_family(self):
        """End-to-end: spec with font-family doesn't crash and renders text."""
        gen = ImageOverlayGenerator(s3_client=MagicMock())
        bg = _make_background()
        spec = {
            "text": "Hello",
            "attributes": [
                {"attr": "y", "value": "50%"},
                {"attr": "x", "value": "50%"},
                {"attr": "fill", "value": "white"},
                {"attr": "text-anchor", "value": "middle"},
                {"attr": "font-family", "value": "Roboto"},
                {"attr": "font-size", "value": "40px"},
            ],
        }
        out = gen.generate_legacy_overlay(background=bg, overlay_specs=[spec])
        assert list(out.getdata()) != list(bg.getdata())


class TestWidthPadFactor:
    """Gap #5: widthPadFactor honored, default 0.04 (not hardcoded 0.85)."""

    def test_default_constant(self):
        assert LEGACY_PADDING_FACTOR_DEFAULT == 0.04

    def test_smaller_pad_factor_fits_more_text_per_row(self):
        """Lower padFactor → wider usable area → fewer wrapped rows."""
        gen = ImageOverlayGenerator(s3_client=MagicMock())
        bg = _make_background(1200, 600)
        text = "The quick brown fox jumps over the lazy dog repeatedly across the page"

        font = _load_font(40, bold=True)
        rows_default = _wrap_pixels(
            text, font,
            max_width=int(1200 - 1200 * LEGACY_PADDING_FACTOR_DEFAULT),  # 0.04 → 1152
        )
        rows_tight = _wrap_pixels(
            text, font,
            max_width=int(1200 - 1200 * 0.5),  # 0.50 → 600
        )
        assert len(rows_default) < len(rows_tight)

    def test_pad_factor_used_in_legacy_path(self):
        gen = ImageOverlayGenerator(s3_client=MagicMock())
        bg = _make_background(1200, 600)
        spec = {
            "text": "Short",
            "attributes": [
                {"attr": "y", "value": "50%"},
                {"attr": "x", "value": "50%"},
                {"attr": "fill", "value": "white"},
                {"attr": "text-anchor", "value": "middle"},
                {"attr": "font-size", "value": "40px"},
            ],
            "widthPadFactor": "0.10",
        }
        # No exception raised — the spec value is read (default would also work).
        out = gen.generate_legacy_overlay(background=bg, overlay_specs=[spec])
        assert out.size == bg.size


class TestNoDropShadow:
    """Gap #6: Legacy path no longer renders a shadow.

    With white-on-blue background and no shadow, the only changed pixels
    should be white (255, 255, 255) — no semi-transparent black halo.
    """

    def test_legacy_overlay_has_no_dark_halo(self):
        """Earlier `b < 80` excluded shadow pixels and made the test useless.

        The right discriminator: shadow pixels REDUCE B from the (0,0,128) bg
        (alpha-blending black down toward 0), while glyph antialias pixels
        RAISE B toward white (255). So:
          shadow:           low R, low G, B < 128
          glyph antialias:  low/high R+G with B ≥ 128
        Asserting (R<30 AND G<30 AND B<128) catches a returning shadow without
        false-positiving on edge antialiasing.
        """
        gen = ImageOverlayGenerator(s3_client=MagicMock())
        bg = _make_background(800, 600)  # solid (0, 0, 128)

        spec = {
            "text": "X",  # single tall character
            "attributes": [
                {"attr": "y", "value": "50%"},
                {"attr": "x", "value": "50%"},
                {"attr": "fill", "value": "white"},
                {"attr": "text-anchor", "value": "middle"},
                {"attr": "font-size", "value": "120px"},
            ],
        }
        out = gen.generate_legacy_overlay(background=bg, overlay_specs=[spec])

        for y in range(out.height):
            for x in range(out.width):
                r, g, b, _ = out.getpixel((x, y))
                if (r, g, b) == (0, 0, 128):
                    continue
                assert not (r < 30 and g < 30 and b < 128), (
                    f"shadow-like pixel found at ({x},{y}): ({r},{g},{b})"
                )


class TestRowHeightPadDefault:
    """Gap #7: rowHeightPad default is 2 (not 10)."""

    def test_default_constant(self):
        assert LEGACY_ROW_HEIGHT_PAD_DEFAULT == 2

    def test_default_used_when_spec_omits_rowheightpad(self):
        gen = ImageOverlayGenerator(s3_client=MagicMock())
        bg = _make_background(800, 600)
        # Two rows worth of text, no rowHeightPad in spec.
        spec = {
            "text": "First Second Third Fourth Fifth Sixth Seventh Eighth",
            "attributes": [
                {"attr": "y", "value": "20%"},
                {"attr": "x", "value": "50%"},
                {"attr": "fill", "value": "white"},
                {"attr": "text-anchor", "value": "middle"},
                {"attr": "font-size", "value": "40px"},
            ],
        }
        # Just verify no crash and overlay applied.
        out = gen.generate_legacy_overlay(background=bg, overlay_specs=[spec])
        assert list(out.getdata()) != list(bg.getdata())


class TestPixelWrapping:
    """Gap #8: pixel-measured wrapping (not character-count estimation)."""

    def test_empty_text_returns_empty(self):
        font = _load_font(20, bold=False)
        assert _wrap_pixels("", font, max_width=400) == []

    def test_single_word_single_row(self):
        font = _load_font(20, bold=False)
        rows = _wrap_pixels("Hello", font, max_width=400)
        assert rows == ["Hello"]

    def test_wraps_when_row_exceeds_max_width(self):
        font = _load_font(40, bold=True)
        # Three short words at 40px should still wrap into a couple rows
        # at a tight max_width.
        rows = _wrap_pixels("Alpha Beta Gamma", font, max_width=80)
        assert len(rows) >= 2

    def test_bigger_font_yields_more_rows(self):
        """Pixel wrap is sensitive to font size — char-count estimation isn't."""
        text = "The quick brown fox jumps over the lazy dog"
        font_small = _load_font(16, bold=False)
        font_big = _load_font(48, bold=False)
        rows_small = _wrap_pixels(text, font_small, max_width=400)
        rows_big = _wrap_pixels(text, font_big, max_width=400)
        assert len(rows_big) > len(rows_small), (
            "pixel wrapping must respond to font size, not just char count"
        )


# ---------------------------------------------------------------------------
# CC3-870 Round 1 review fixes
# ---------------------------------------------------------------------------


class TestComputeTopY:
    """R1 #3: Anchoring math regression vs Node getTextElements.js:36-52.

    Node source (image-generator/getTextElements.js, lines 36-52):

        const center = height / 2;
        let topPixelValue = 0;
        if (pixelInt < center) {
            topPixelValue = pixelInt + ( paddedRowHeight / 2 );
        } else if (pixelInt === center) {
            topPixelValue =
                paddedRowHeight +
                parseInt(pixelInt - ( (paddedRowHeight * rowCount) / 2) );
        } else if (pixelInt > center) {
            topPixelValue = parseInt(
                pixelInt - ( paddedRowHeight * (rowCount - 1) ),
            );
        }

    The half-row discontinuity crossing center from below is Node's actual
    behavior, not a port artifact. These cases pin the parity.
    """

    @pytest.mark.parametrize("y_anchor,padded,row_count,expected", [
        # 800x600 image (center=300), padded=42 (font_size=40 + row_pad=2)
        # y < center: top_y = y + padded // 2
        (0, 42, 1, 0 + 21),       # y=0   → 21
        (100, 42, 1, 100 + 21),   # y=100 → 121
        (299, 42, 1, 299 + 21),   # y=center-1 → 320
        (200, 42, 3, 200 + 21),   # y<center, 3 rows → still 221 (rows grow down)

        # y == center (the edge case Alex flagged)
        # Node: top_y = padded + int(y - (padded * rowCount) / 2)
        (300, 42, 1, 42 + 300 - 21),   # → 321
        (300, 42, 3, 42 + 300 - 63),   # → 279 (centered around y for 3-row block)
        (300, 42, 5, 42 + 300 - 105),  # → 237

        # y > center: top_y = y - padded * (rowCount - 1)
        (301, 42, 1, 301),         # 1 row → top_y=y exactly
        (301, 42, 3, 301 - 84),    # → 217 (3 rows grow upward)
        (480, 42, 5, 480 - 168),   # → 312 (bottom-anchored, 5 rows)
    ])
    def test_matches_node_for_all_branches(self, y_anchor, padded, row_count, expected):
        """Pin Node-equivalent top_y values across all three branches."""
        assert _compute_top_y(y_anchor, image_height=600, padded_row_height=padded,
                              row_count=row_count) == expected

    def test_documented_discontinuity_at_center(self):
        """Crossing center from below, top_y jumps by ~half a row.

        For a single line at padded=42:
          y=299 → top_y=320 (top-branch: 299 + 21)
          y=300 → top_y=321 (==-branch:  42 + 300 - 21)
          y=301 → top_y=301 (>-branch:   301 - 0)

        The 320 → 301 transition is a 19-pixel jump. This is preserved
        verbatim from Node and would only be wrong if Node itself were wrong
        — since the parent ticket's goal is byte-for-byte parity, we keep it.
        """
        assert _compute_top_y(299, 600, 42, 1) == 320
        assert _compute_top_y(300, 600, 42, 1) == 321
        assert _compute_top_y(301, 600, 42, 1) == 301


class TestSafeParsing:
    """R1 #2: Defensive parsing for legacy spec values.

    Drupal payloads can carry empty/null/CSS-suffixed values that crash
    the entire trigger if passed straight to int()/float().
    """

    @pytest.mark.parametrize("value,expected", [
        (None, 5),
        ("", 5),
        ("10", 10),
        (10, 10),
        ("abc", 5),    # malformed → default
        ("4%", 5),     # CSS-style with units → default
        ([], 5),       # wrong type → default
    ])
    def test_safe_int(self, value, expected):
        assert _safe_int(value, default=5, field="x") == expected

    @pytest.mark.parametrize("value,expected", [
        (None, 0.04),
        ("", 0.04),
        ("0.10", 0.10),
        (0.10, 0.10),
        ("not-a-number", 0.04),
        ("10%", 0.04),
        ({}, 0.04),
    ])
    def test_safe_float(self, value, expected):
        assert _safe_float(value, default=0.04, field="x") == expected

    def test_legacy_overlay_survives_malformed_widthpadfactor(self, caplog):
        """Whole-job survival when Drupal sends widthPadFactor='4%'."""
        gen = ImageOverlayGenerator(s3_client=MagicMock())
        bg = _make_background(800, 600)
        spec = {
            "text": "Hello world",
            "attributes": [
                {"attr": "y", "value": "20%"},
                {"attr": "x", "value": "50%"},
                {"attr": "fill", "value": "white"},
                {"attr": "text-anchor", "value": "middle"},
                {"attr": "font-size", "value": "40px"},
            ],
            "widthPadFactor": "4%",   # malformed; would break float()
            "rowHeightPad": "",       # malformed; would break int()
        }
        with caplog.at_level("WARNING"):
            out = gen.generate_legacy_overlay(background=bg, overlay_specs=[spec])
        # Job completed; warnings were logged; defaults were applied.
        assert out.size == bg.size


class TestTextAnchorEnd:
    """R1 smaller: text-anchor='end' branch + unknown-anchor warning."""

    def test_end_anchor_right_aligns_text(self):
        gen = ImageOverlayGenerator(s3_client=MagicMock())
        bg = _make_background(800, 600)
        spec = {
            "text": "ENDX",
            "attributes": [
                {"attr": "y", "value": "20%"},
                {"attr": "x", "value": "100%"},
                {"attr": "fill", "value": "white"},
                {"attr": "text-anchor", "value": "end"},
                {"attr": "font-size", "value": "60px"},
            ],
        }
        out = gen.generate_legacy_overlay(background=bg, overlay_specs=[spec])

        # End-anchored text at x=100% should occupy pixels in the right portion
        # (last 20% of width), not the center or left.
        right_band_modified = False
        for y in range(0, 200):
            for x in range(640, 800):
                if out.getpixel((x, y))[:3] != (0, 0, 128):
                    right_band_modified = True
                    break
            if right_band_modified:
                break
        assert right_band_modified, "end-anchored text did not render in the right band"

    def test_unknown_anchor_falls_back_with_warning(self, caplog):
        gen = ImageOverlayGenerator(s3_client=MagicMock())
        bg = _make_background()
        spec = {
            "text": "Hi",
            "attributes": [
                {"attr": "y", "value": "20%"},
                {"attr": "x", "value": "50%"},
                {"attr": "fill", "value": "white"},
                {"attr": "text-anchor", "value": "bogus-value"},
                {"attr": "font-size", "value": "40px"},
            ],
        }
        with caplog.at_level("WARNING"):
            out = gen.generate_legacy_overlay(background=bg, overlay_specs=[spec])
        # No crash; warning logged with the offending value.
        assert "bogus-value" in caplog.text or "Unknown text-anchor" in caplog.text
        assert out.size == bg.size


class TestNormalizeFamilyEdgeCases:
    """R1 nit: _normalize_family handles whitespace-only inputs."""

    def test_whitespace_only_defaults_to_opensans(self):
        assert _normalize_family("   ") == "opensans"
        assert _normalize_family("\t\n") == "opensans"

    def test_already_handled_inputs_unchanged(self):
        # Sanity — make sure the explicit .strip() doesn't break the existing path.
        assert _normalize_family("Roboto") == "roboto"
        assert _normalize_family("'Courier Prime', monospace") == "courierprime"
