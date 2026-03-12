"""Tests for the PDF extraction Lambda handler."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from src.handlers.pdf_handler import _parse_event, handler


@pytest.fixture
def mock_extractor():
    with patch("src.handlers.pdf_handler._extractor") as mock:
        mock.extract.return_value = {
            "text": "Extracted content.",
            "page_count": 5,
            "extraction_method": "text",
        }
        yield mock


# ---------------------------------------------------------------------------
# Direct / orchestrator invocation
# ---------------------------------------------------------------------------

class TestDirectInvocation:
    def test_success(self, mock_extractor):
        event = {
            "bucket": "my-bucket",
            "key": "ieee/pending/STD-123.pdf",
            "ou": "ieee",
            "product_part_number": "STD-123",
        }
        result = handler(event, None)

        assert result["statusCode"] == 200
        assert result["body"]["page_count"] == 5
        assert result["body"]["extraction_method"] == "text"
        mock_extractor.extract.assert_called_once_with(
            bucket="my-bucket",
            key="ieee/pending/STD-123.pdf",
            ou="ieee",
            product_part_number="STD-123",
        )

    def test_missing_fields_returns_400(self, mock_extractor):
        event = {"bucket": "my-bucket"}  # missing key, ou, product_part_number
        result = handler(event, None)

        assert result["statusCode"] == 400
        assert "error" in result["body"]
        mock_extractor.extract.assert_not_called()


# ---------------------------------------------------------------------------
# S3 event trigger invocation
# ---------------------------------------------------------------------------

class TestS3EventInvocation:
    def _s3_event(self, bucket: str, key: str) -> dict:
        return {
            "Records": [
                {
                    "s3": {
                        "bucket": {"name": bucket},
                        "object": {"key": key},
                    }
                }
            ]
        }

    def test_parses_s3_event(self, mock_extractor):
        event = self._s3_event("my-bucket", "ieee/pending/STD-456.pdf")
        result = handler(event, None)

        assert result["statusCode"] == 200
        mock_extractor.extract.assert_called_once_with(
            bucket="my-bucket",
            key="ieee/pending/STD-456.pdf",
            ou="ieee",
            product_part_number="STD-456",
        )

    def test_invalid_key_pattern_returns_400(self, mock_extractor):
        event = self._s3_event("my-bucket", "random/path/file.pdf")
        result = handler(event, None)

        assert result["statusCode"] == 400
        assert "error" in result["body"]


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------

class TestErrorHandling:
    def test_s3_client_error_returns_500(self, mock_extractor):
        mock_extractor.extract.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
            "GetObject",
        )
        event = {
            "bucket": "b",
            "key": "ieee/pending/x.pdf",
            "ou": "ieee",
            "product_part_number": "x",
        }
        result = handler(event, None)

        assert result["statusCode"] == 500
        assert "NoSuchKey" in result["body"]["error"]

    def test_unexpected_error_returns_500(self, mock_extractor):
        mock_extractor.extract.side_effect = RuntimeError("PyMuPDF segfault")
        event = {
            "bucket": "b",
            "key": "ieee/pending/x.pdf",
            "ou": "ieee",
            "product_part_number": "x",
        }
        result = handler(event, None)

        assert result["statusCode"] == 500
        assert "RuntimeError" in result["body"]["error"]


# ---------------------------------------------------------------------------
# Event parsing
# ---------------------------------------------------------------------------

class TestParseEvent:
    def test_direct_event(self):
        params = _parse_event({
            "bucket": "b",
            "key": "k",
            "ou": "o",
            "product_part_number": "p",
        })
        assert params == {"bucket": "b", "key": "k", "ou": "o", "product_part_number": "p"}

    def test_s3_event_derives_ou_and_part_number(self):
        params = _parse_event({
            "Records": [{
                "s3": {
                    "bucket": {"name": "b"},
                    "object": {"key": "myou/pending/PART-789.pdf"},
                }
            }]
        })
        assert params["ou"] == "myou"
        assert params["product_part_number"] == "PART-789"
        assert params["bucket"] == "b"
        assert params["key"] == "myou/pending/PART-789.pdf"

    def test_s3_event_bad_path_raises(self):
        with pytest.raises(ValueError, match="does not match"):
            _parse_event({
                "Records": [{
                    "s3": {
                        "bucket": {"name": "b"},
                        "object": {"key": "no-pending-dir/file.pdf"},
                    }
                }]
            })
