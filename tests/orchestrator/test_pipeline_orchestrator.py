"""Tests for PipelineOrchestrator."""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from src.orchestrator.pipeline_orchestrator import PipelineOrchestrator, PipelineResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EXTRACTION_RESULT = {
    "text": "Extracted document text for testing purposes. " * 20,
    "page_count": 5,
    "extraction_method": "text",
}

INFERENCE_RESULT = {
    "abstract": "First paragraph of the abstract with sufficient words. " * 3
    + "\n\n"
    + "Second paragraph of the abstract with sufficient words. " * 3,
    "keywords": ["smart grid", "power systems", "renewable energy", "IEEE",
                 "transformer", "voltage", "current", "frequency"],
    "learning_level": "intermediate",
    "intended_audience": "engineers",
    "category": "standards",
    "processing_time_ms": 1500,
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestPipelineRun:
    @patch("src.orchestrator.pipeline_orchestrator.BedrockInference")
    @patch("src.orchestrator.pipeline_orchestrator.PDFExtractor")
    def test_full_pipeline_success(self, MockExtractor, MockInference):
        s3_mock = MagicMock()
        extractor_instance = MockExtractor.return_value
        extractor_instance.extract.return_value = EXTRACTION_RESULT.copy()

        inference_instance = MockInference.return_value
        inference_instance.generate_metadata.return_value = INFERENCE_RESULT.copy()

        orch = PipelineOrchestrator(s3_client=s3_mock)

        result = orch.run(
            bucket="test-bucket",
            key="PES/pending/STD-12345.pdf",
            ou="PES",
            product_part_number="STD-12345",
        )

        # Verify extraction was called
        extractor_instance.extract.assert_called_once_with(
            bucket="test-bucket",
            key="PES/pending/STD-12345.pdf",
            ou="PES",
            product_part_number="STD-12345",
        )

        # Verify inference was called with extracted text
        inference_instance.generate_metadata.assert_called_once_with(
            text=EXTRACTION_RESULT["text"],
            thesaurus_terms=None,
        )

        # Verify enriched metadata was written to S3
        s3_mock.put_object.assert_called_once()
        put_kwargs = s3_mock.put_object.call_args[1]
        assert put_kwargs["Bucket"] == "test-bucket"
        assert put_kwargs["Key"] == "PES/metadata/STD-12345.enriched.json"
        assert put_kwargs["ContentType"] == "application/json"

        # Verify result structure
        assert result["text_length"] == len(EXTRACTION_RESULT["text"])
        assert result["page_count"] == 5
        assert result["extraction_method"] == "text"
        assert result["abstract"] == INFERENCE_RESULT["abstract"]
        assert result["keywords"] == INFERENCE_RESULT["keywords"]
        assert result["learning_level"] == "intermediate"
        assert result["enriched_metadata_key"] == "PES/metadata/STD-12345.enriched.json"
        assert result["pipeline_time_ms"] >= 0

    @patch("src.orchestrator.pipeline_orchestrator.BedrockInference")
    @patch("src.orchestrator.pipeline_orchestrator.PDFExtractor")
    def test_passes_thesaurus_terms(self, MockExtractor, MockInference):
        s3_mock = MagicMock()
        MockExtractor.return_value.extract.return_value = EXTRACTION_RESULT.copy()
        MockInference.return_value.generate_metadata.return_value = INFERENCE_RESULT.copy()

        orch = PipelineOrchestrator(s3_client=s3_mock)
        orch.run(
            bucket="b",
            key="PES/pending/STD-1.pdf",
            ou="PES",
            product_part_number="STD-1",
            thesaurus_terms=["smart grid", "power systems"],
        )

        MockInference.return_value.generate_metadata.assert_called_once_with(
            text=EXTRACTION_RESULT["text"],
            thesaurus_terms=["smart grid", "power systems"],
        )

    @patch("src.orchestrator.pipeline_orchestrator.BedrockInference")
    @patch("src.orchestrator.pipeline_orchestrator.PDFExtractor")
    def test_empty_text_skips_bedrock(self, MockExtractor, MockInference):
        s3_mock = MagicMock()
        MockExtractor.return_value.extract.return_value = {
            "text": "",
            "page_count": 1,
            "extraction_method": "ocr",
        }

        orch = PipelineOrchestrator(s3_client=s3_mock)
        result = orch.run(
            bucket="b",
            key="PES/pending/STD-1.pdf",
            ou="PES",
            product_part_number="STD-1",
        )

        # Bedrock should NOT be called
        MockInference.return_value.generate_metadata.assert_not_called()

        # S3 should NOT be written to
        s3_mock.put_object.assert_not_called()

        assert result["text_length"] == 0
        assert result["abstract"] == ""
        assert result["keywords"] == []
        assert result["enriched_metadata_key"] == ""

    @patch("src.orchestrator.pipeline_orchestrator.BedrockInference")
    @patch("src.orchestrator.pipeline_orchestrator.PDFExtractor")
    def test_extraction_error_propagates(self, MockExtractor, MockInference):
        s3_mock = MagicMock()
        MockExtractor.return_value.extract.side_effect = RuntimeError("PDF corrupt")

        orch = PipelineOrchestrator(s3_client=s3_mock)

        with pytest.raises(RuntimeError, match="PDF corrupt"):
            orch.run(
                bucket="b",
                key="PES/pending/bad.pdf",
                ou="PES",
                product_part_number="BAD",
            )

        MockInference.return_value.generate_metadata.assert_not_called()

    @patch("src.orchestrator.pipeline_orchestrator.BedrockInference")
    @patch("src.orchestrator.pipeline_orchestrator.PDFExtractor")
    def test_bedrock_error_propagates(self, MockExtractor, MockInference):
        s3_mock = MagicMock()
        MockExtractor.return_value.extract.return_value = EXTRACTION_RESULT.copy()
        MockInference.return_value.generate_metadata.side_effect = RuntimeError(
            "Bedrock throttled"
        )

        orch = PipelineOrchestrator(s3_client=s3_mock)

        with pytest.raises(RuntimeError, match="Bedrock throttled"):
            orch.run(
                bucket="b",
                key="PES/pending/STD-1.pdf",
                ou="PES",
                product_part_number="STD-1",
            )

        # Enriched metadata should NOT be written
        s3_mock.put_object.assert_not_called()

    @patch("src.orchestrator.pipeline_orchestrator.BedrockInference")
    @patch("src.orchestrator.pipeline_orchestrator.PDFExtractor")
    def test_enriched_json_structure(self, MockExtractor, MockInference):
        s3_mock = MagicMock()
        MockExtractor.return_value.extract.return_value = EXTRACTION_RESULT.copy()
        MockInference.return_value.generate_metadata.return_value = INFERENCE_RESULT.copy()

        orch = PipelineOrchestrator(s3_client=s3_mock)
        orch.run(
            bucket="b",
            key="PES/pending/STD-1.pdf",
            ou="PES",
            product_part_number="STD-1",
        )

        written_body = s3_mock.put_object.call_args[1]["Body"]
        enriched = json.loads(written_body)

        assert enriched["product_part_number"] == "STD-1"
        assert enriched["ou"] == "PES"
        assert enriched["source_key"] == "PES/pending/STD-1.pdf"
        assert enriched["extraction"]["page_count"] == 5
        assert enriched["extraction"]["extraction_method"] == "text"
        assert enriched["metadata"]["abstract"] == INFERENCE_RESULT["abstract"]
        assert enriched["metadata"]["keywords"] == INFERENCE_RESULT["keywords"]
        assert enriched["metadata"]["learning_level"] == "intermediate"
