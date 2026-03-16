"""Tests for the Bedrock metadata generation Lambda handler."""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from src.handlers.bedrock_handler import handler


def _mock_inference_result() -> dict:
    """Return a mock InferenceResult dict."""
    para1 = " ".join(["word"] * 80)
    para2 = " ".join(["term"] * 80)
    return {
        "abstract": f"{para1}\n\n{para2}",
        "keywords": [
            "power systems", "smart grid", "energy storage", "renewable energy",
            "load forecasting", "demand response", "microgrid", "IEEE 1547",
            "distributed generation", "voltage regulation",
        ],
        "learning_level": "Professional",
        "intended_audience": "Seasoned Engineering Professional",
        "category": "Research Papers and Publications",
        "processing_time_ms": 1234,
    }


@pytest.fixture
def mock_inference():
    with patch("src.handlers.bedrock_handler._inference") as mock:
        mock.generate_metadata.return_value = _mock_inference_result()
        yield mock


@pytest.fixture
def mock_s3():
    with patch("src.handlers.bedrock_handler._s3_client") as mock:
        yield mock


# ---------------------------------------------------------------------------
# Direct invocation
# ---------------------------------------------------------------------------


class TestDirectInvocation:
    def test_success(self, mock_inference):
        event = {"text": "This is extracted document text."}
        result = handler(event, None)

        assert result["statusCode"] == 200
        assert result["body"]["learning_level"] == "Professional"
        assert result["body"]["processing_time_ms"] == 1234
        mock_inference.generate_metadata.assert_called_once_with(
            text="This is extracted document text.",
            thesaurus_terms=None,
        )

    def test_with_thesaurus_terms(self, mock_inference):
        event = {
            "text": "Document text.",
            "thesaurus_terms": ["smart grid", "power systems"],
        }
        result = handler(event, None)

        assert result["statusCode"] == 200
        mock_inference.generate_metadata.assert_called_once_with(
            text="Document text.",
            thesaurus_terms=["smart grid", "power systems"],
        )

    def test_empty_text_returns_400(self, mock_inference):
        event = {"text": "   "}
        result = handler(event, None)

        assert result["statusCode"] == 400
        assert "non-empty" in result["body"]["error"]
        mock_inference.generate_metadata.assert_not_called()

    def test_missing_text_returns_400(self, mock_inference):
        event = {"something": "else"}
        result = handler(event, None)

        assert result["statusCode"] == 400
        mock_inference.generate_metadata.assert_not_called()


# ---------------------------------------------------------------------------
# S3 metadata invocation
# ---------------------------------------------------------------------------


class TestS3Invocation:
    def test_reads_text_from_s3(self, mock_inference, mock_s3):
        mock_s3.get_object.return_value = {
            "Body": BytesIO(json.dumps({"extractedText": "S3 text"}).encode())
        }
        event = {"bucket": "my-bucket", "key": "metadata/doc.json"}
        result = handler(event, None)

        assert result["statusCode"] == 200
        mock_s3.get_object.assert_called_once_with(
            Bucket="my-bucket", Key="metadata/doc.json"
        )
        mock_inference.generate_metadata.assert_called_once_with(
            text="S3 text", thesaurus_terms=None,
        )

    def test_empty_extracted_text_returns_400(self, mock_inference, mock_s3):
        mock_s3.get_object.return_value = {
            "Body": BytesIO(json.dumps({"extractedText": ""}).encode())
        }
        event = {"bucket": "b", "key": "k"}
        result = handler(event, None)

        assert result["statusCode"] == 400
        assert "extractedText" in result["body"]["error"]


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_validation_error_returns_422(self, mock_inference):
        mock_inference.generate_metadata.side_effect = ValueError("Missing required fields: abstract")
        event = {"text": "text"}
        result = handler(event, None)

        assert result["statusCode"] == 422
        assert "abstract" in result["body"]["error"]

    def test_bedrock_error_returns_500(self, mock_inference):
        mock_inference.generate_metadata.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}},
            "InvokeModel",
        )
        event = {"text": "text"}
        result = handler(event, None)

        assert result["statusCode"] == 500
        assert "ThrottlingException" in result["body"]["error"]

    def test_unexpected_error_returns_500(self, mock_inference):
        mock_inference.generate_metadata.side_effect = RuntimeError("boom")
        event = {"text": "text"}
        result = handler(event, None)

        assert result["statusCode"] == 500
        assert "RuntimeError" in result["body"]["error"]
