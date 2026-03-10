"""Tests for the Image Overlay Generation Lambda handler."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from src.handlers.image_overlay_handler import _parse_event, handler


@pytest.fixture
def mock_generator():
    with patch("src.handlers.image_overlay_handler._generator") as mock:
        mock.process_trigger.return_value = {
            "output_key": "images/products/STD-123.jpg",
            "thumbnail_key": "",
            "width": 800,
            "height": 600,
            "format": "jpg",
        }
        yield mock


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

    def test_success(self, mock_generator):
        event = self._s3_event("trigger-bucket", "actions/job-001.json")
        result = handler(event, None)

        assert result["statusCode"] == 200
        assert result["body"]["output_key"] == "images/products/STD-123.jpg"
        mock_generator.process_trigger.assert_called_once_with(
            bucket="trigger-bucket", key="actions/job-001.json"
        )

    def test_invalid_key_prefix_returns_400(self, mock_generator):
        event = self._s3_event("bucket", "wrong-prefix/file.json")
        result = handler(event, None)

        assert result["statusCode"] == 400
        assert "error" in result["body"]
        mock_generator.process_trigger.assert_not_called()

    def test_non_json_key_returns_400(self, mock_generator):
        event = self._s3_event("bucket", "actions/file.txt")
        result = handler(event, None)

        assert result["statusCode"] == 400
        mock_generator.process_trigger.assert_not_called()


# ---------------------------------------------------------------------------
# Direct invocation
# ---------------------------------------------------------------------------


class TestDirectInvocation:
    def test_success(self, mock_generator):
        event = {"bucket": "trigger-bucket", "key": "actions/job.json"}
        result = handler(event, None)

        assert result["statusCode"] == 200
        mock_generator.process_trigger.assert_called_once_with(
            bucket="trigger-bucket", key="actions/job.json"
        )

    def test_missing_fields_returns_400(self, mock_generator):
        event = {"something": "else"}
        result = handler(event, None)

        assert result["statusCode"] == 400
        mock_generator.process_trigger.assert_not_called()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_s3_client_error_returns_500(self, mock_generator):
        mock_generator.process_trigger.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
            "GetObject",
        )
        event = {"bucket": "b", "key": "actions/job.json"}
        result = handler(event, None)

        assert result["statusCode"] == 500
        assert "NoSuchKey" in result["body"]["error"]

    def test_validation_error_returns_400(self, mock_generator):
        mock_generator.process_trigger.side_effect = ValueError("Missing required fields: title")
        event = {"bucket": "b", "key": "actions/job.json"}
        result = handler(event, None)

        assert result["statusCode"] == 400
        assert "title" in result["body"]["error"]

    def test_unexpected_error_returns_500(self, mock_generator):
        mock_generator.process_trigger.side_effect = RuntimeError("boom")
        event = {"bucket": "b", "key": "actions/job.json"}
        result = handler(event, None)

        assert result["statusCode"] == 500
        assert "RuntimeError" in result["body"]["error"]


# ---------------------------------------------------------------------------
# Event parsing
# ---------------------------------------------------------------------------


class TestParseEvent:
    def test_s3_event(self):
        bucket, key = _parse_event({
            "Records": [{
                "s3": {
                    "bucket": {"name": "b"},
                    "object": {"key": "actions/job.json"},
                }
            }]
        })
        assert bucket == "b"
        assert key == "actions/job.json"

    def test_direct_event(self):
        bucket, key = _parse_event({"bucket": "b", "key": "actions/test.json"})
        assert bucket == "b"
        assert key == "actions/test.json"

    def test_missing_keys_raises(self):
        with pytest.raises(KeyError):
            _parse_event({"foo": "bar"})

    def test_wrong_prefix_raises(self):
        with pytest.raises(ValueError, match="does not match"):
            _parse_event({"bucket": "b", "key": "other/file.json"})
