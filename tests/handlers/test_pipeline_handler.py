"""Tests for the Pipeline Orchestrator Lambda handler."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.handlers.pipeline_handler import _parse_event, handler


MOCK_RESULT = {
    "text_length": 5000,
    "page_count": 10,
    "extraction_method": "text",
    "abstract": "Test abstract.",
    "keywords": ["test"],
    "learning_level": "intermediate",
    "intended_audience": "engineers",
    "category": "standards",
    "enriched_metadata_key": "PES/metadata/STD-1.enriched.json",
    "pipeline_time_ms": 3000,
}


@pytest.fixture
def mock_orchestrator():
    with patch("src.handlers.pipeline_handler._orchestrator") as mock:
        mock.run.return_value = MOCK_RESULT.copy()
        yield mock


# ---------------------------------------------------------------------------
# Direct invocation
# ---------------------------------------------------------------------------


class TestDirectInvocation:
    def test_success(self, mock_orchestrator):
        event = {
            "bucket": "test-bucket",
            "key": "PES/pending/STD-12345.pdf",
            "ou": "PES",
            "product_part_number": "STD-12345",
        }
        result = handler(event, None)

        assert result["statusCode"] == 200
        assert result["body"]["page_count"] == 10
        assert result["body"]["abstract"] == "Test abstract."
        mock_orchestrator.run.assert_called_once_with(
            bucket="test-bucket",
            key="PES/pending/STD-12345.pdf",
            ou="PES",
            product_part_number="STD-12345",
            thesaurus_terms=None,
        )

    def test_derives_ou_from_key(self, mock_orchestrator):
        event = {
            "bucket": "test-bucket",
            "key": "SPS/pending/DOC-999.pdf",
        }
        result = handler(event, None)

        assert result["statusCode"] == 200
        mock_orchestrator.run.assert_called_once_with(
            bucket="test-bucket",
            key="SPS/pending/DOC-999.pdf",
            ou="SPS",
            product_part_number="DOC-999",
            thesaurus_terms=None,
        )

    def test_passes_thesaurus_terms(self, mock_orchestrator):
        event = {
            "bucket": "b",
            "key": "PES/pending/STD-1.pdf",
            "ou": "PES",
            "product_part_number": "STD-1",
            "thesaurus_terms": ["smart grid"],
        }
        handler(event, None)

        mock_orchestrator.run.assert_called_once_with(
            bucket="b",
            key="PES/pending/STD-1.pdf",
            ou="PES",
            product_part_number="STD-1",
            thesaurus_terms=["smart grid"],
        )

    def test_missing_fields_returns_400(self, mock_orchestrator):
        event = {"something": "else"}
        result = handler(event, None)

        assert result["statusCode"] == 400
        mock_orchestrator.run.assert_not_called()


# ---------------------------------------------------------------------------
# S3 event invocation
# ---------------------------------------------------------------------------


class TestS3EventInvocation:
    def _s3_event(self, bucket: str, key: str) -> dict:
        return {
            "Records": [{
                "s3": {
                    "bucket": {"name": bucket},
                    "object": {"key": key},
                }
            }]
        }

    def test_success(self, mock_orchestrator):
        event = self._s3_event("test-bucket", "PES/pending/STD-12345.pdf")
        result = handler(event, None)

        assert result["statusCode"] == 200
        mock_orchestrator.run.assert_called_once_with(
            bucket="test-bucket",
            key="PES/pending/STD-12345.pdf",
            ou="PES",
            product_part_number="STD-12345",
            thesaurus_terms=None,
        )

    def test_invalid_key_pattern_returns_400(self, mock_orchestrator):
        event = self._s3_event("b", "wrong/path/file.pdf")
        result = handler(event, None)

        assert result["statusCode"] == 400
        mock_orchestrator.run.assert_not_called()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_pipeline_error_returns_500(self, mock_orchestrator):
        mock_orchestrator.run.side_effect = RuntimeError("Bedrock down")
        event = {
            "bucket": "b",
            "key": "PES/pending/STD-1.pdf",
            "ou": "PES",
            "product_part_number": "STD-1",
        }
        result = handler(event, None)

        assert result["statusCode"] == 500
        assert "RuntimeError" in result["body"]["error"]


# ---------------------------------------------------------------------------
# Event parsing
# ---------------------------------------------------------------------------


class TestParseEvent:
    def test_direct_event(self):
        bucket, key, ou, ppn = _parse_event({
            "bucket": "b",
            "key": "PES/pending/STD-1.pdf",
            "ou": "PES",
            "product_part_number": "STD-1",
        })
        assert bucket == "b"
        assert ou == "PES"
        assert ppn == "STD-1"

    def test_s3_event(self):
        bucket, key, ou, ppn = _parse_event({
            "Records": [{
                "s3": {
                    "bucket": {"name": "b"},
                    "object": {"key": "SPS/pending/DOC-999.pdf"},
                }
            }]
        })
        assert bucket == "b"
        assert ou == "SPS"
        assert ppn == "DOC-999"

    def test_derives_missing_ou_from_key(self):
        bucket, key, ou, ppn = _parse_event({
            "bucket": "b",
            "key": "PES/pending/STD-1.pdf",
        })
        assert ou == "PES"
        assert ppn == "STD-1"

    def test_missing_key_and_records_raises(self):
        with pytest.raises(KeyError):
            _parse_event({"foo": "bar"})

    def test_bad_key_without_ou_raises(self):
        with pytest.raises(ValueError, match="Must provide"):
            _parse_event({"bucket": "b", "key": "some/random/path.pdf"})
