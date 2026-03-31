"""Tests for AI Orchestrator Lambda handler."""

import json
from unittest.mock import patch, MagicMock

import pytest

from src.handlers.ai_orchestrator_handler import handler, _parse_event


# ---------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------

@pytest.fixture
def mock_orchestrator():
    with patch("src.handlers.ai_orchestrator_handler._orchestrator") as mock:
        mock.process.return_value = {
            "item_id": "STD-12345",
            "ou": "PES",
            "action": "enriched",
            "ai_enrichment_enabled": True,
            "source_key": "PES/pending/STD-12345.pdf",
            "destination_key": "PES/processed/STD-12345.pdf",
            "processing_time_ms": 5000,
            "details": {},
        }
        yield mock


def _s3_event(bucket="bucket", key="PES/pending/STD-12345.pdf"):
    return {
        "Records": [{
            "s3": {
                "bucket": {"name": bucket},
                "object": {"key": key},
            }
        }]
    }


def _direct_event(bucket="bucket", key="PES/pending/STD-12345.pdf"):
    return {"bucket": bucket, "key": key}


# ---------------------------------------------------------------
# Event Parsing
# ---------------------------------------------------------------

class TestParseEvent:
    def test_s3_event(self):
        bucket, key = _parse_event(_s3_event())
        assert bucket == "bucket"
        assert key == "PES/pending/STD-12345.pdf"

    def test_direct_event(self):
        bucket, key = _parse_event(_direct_event())
        assert bucket == "bucket"
        assert key == "PES/pending/STD-12345.pdf"

    def test_missing_records_and_bucket_raises(self):
        with pytest.raises(KeyError, match="Records"):
            _parse_event({})

    def test_invalid_key_pattern_raises(self):
        with pytest.raises(ValueError, match="does not match"):
            _parse_event({"bucket": "b", "key": "wrong/path.pdf"})

    def test_key_without_pending_raises(self):
        with pytest.raises(ValueError, match="does not match"):
            _parse_event({"bucket": "b", "key": "PES/uploads/file.pdf"})


# ---------------------------------------------------------------
# Direct Invocation
# ---------------------------------------------------------------

class TestDirectInvocation:
    def test_success(self, mock_orchestrator):
        result = handler(_direct_event(), None)

        assert result["statusCode"] == 200
        assert result["body"]["item_id"] == "STD-12345"
        assert result["body"]["action"] == "enriched"
        mock_orchestrator.process.assert_called_once()

    def test_passes_bucket_and_key(self, mock_orchestrator):
        handler(_direct_event("my-bucket", "PES/pending/doc.pdf"), None)

        call_kwargs = mock_orchestrator.process.call_args[1]
        assert call_kwargs["bucket"] == "my-bucket"
        assert call_kwargs["key"] == "PES/pending/doc.pdf"


# ---------------------------------------------------------------
# S3 Event Invocation
# ---------------------------------------------------------------

class TestS3EventInvocation:
    def test_success(self, mock_orchestrator):
        result = handler(_s3_event(), None)

        assert result["statusCode"] == 200
        mock_orchestrator.process.assert_called_once()

    def test_extracts_bucket_and_key(self, mock_orchestrator):
        handler(_s3_event("prod-bucket", "AESS/pending/lecture.mp4"), None)

        call_kwargs = mock_orchestrator.process.call_args[1]
        assert call_kwargs["bucket"] == "prod-bucket"
        assert call_kwargs["key"] == "AESS/pending/lecture.mp4"


# ---------------------------------------------------------------
# Error Handling
# ---------------------------------------------------------------

class TestErrorHandling:
    def test_bad_event_returns_400(self, mock_orchestrator):
        result = handler({}, None)
        assert result["statusCode"] == 400

    def test_invalid_key_returns_400(self, mock_orchestrator):
        result = handler({"bucket": "b", "key": "bad/path"}, None)
        assert result["statusCode"] == 400

    def test_validation_error_returns_400(self, mock_orchestrator):
        mock_orchestrator.process.side_effect = ValueError("Missing field")
        result = handler(_direct_event(), None)
        assert result["statusCode"] == 400
        assert "Missing field" in result["body"]["error"]

    def test_client_error_returns_500(self, mock_orchestrator):
        from botocore.exceptions import ClientError
        mock_orchestrator.process.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Forbidden"}},
            "GetObject",
        )
        result = handler(_direct_event(), None)
        assert result["statusCode"] == 500
        assert "AccessDenied" in result["body"]["error"]

    def test_runtime_error_returns_500(self, mock_orchestrator):
        mock_orchestrator.process.side_effect = RuntimeError("Lambda failed")
        result = handler(_direct_event(), None)
        assert result["statusCode"] == 500
        assert "Lambda failed" in result["body"]["error"]

    def test_unexpected_error_returns_500(self, mock_orchestrator):
        mock_orchestrator.process.side_effect = TypeError("bad type")
        result = handler(_direct_event(), None)
        assert result["statusCode"] == 500
        assert "TypeError" in result["body"]["error"]


# ---------------------------------------------------------------
# Context Handling
# ---------------------------------------------------------------

class TestContextHandling:
    def test_passes_request_id_from_context(self, mock_orchestrator):
        ctx = MagicMock()
        ctx.aws_request_id = "req-abc-123"

        handler(_direct_event(), ctx)

        call_kwargs = mock_orchestrator.process.call_args[1]
        assert call_kwargs["request_id"] == "req-abc-123"

    def test_handles_none_context(self, mock_orchestrator):
        handler(_direct_event(), None)

        call_kwargs = mock_orchestrator.process.call_args[1]
        assert call_kwargs["request_id"] == ""
