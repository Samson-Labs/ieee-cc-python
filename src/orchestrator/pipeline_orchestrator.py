"""Pipeline Orchestrator for IEEE Content Conversion.

Chains the PDF text extraction and Bedrock metadata generation steps into
a single end-to-end pipeline. Designed for integration testing — in production,
an external orchestrator (Step Functions, etc.) would coordinate these steps.

Flow:
    1. PDF Extractor: download PDF from S3, extract text, write metadata JSON
    2. Bedrock Inference: send extracted text to Claude, get structured metadata
    3. Write final enriched metadata back to S3
"""

from __future__ import annotations

import json
import logging
import time
from typing import TypedDict

import boto3

from src.ai.bedrock_inference import BedrockInference
from src.extractors.pdf_extractor import PDFExtractor

logger = logging.getLogger(__name__)


class PipelineResult(TypedDict):
    """Result of a full pipeline run."""

    # PDF extraction
    text_length: int
    page_count: int
    extraction_method: str

    # Bedrock metadata
    abstract: str
    keywords: list[str]
    learning_level: str
    intended_audience: str
    category: str

    # Pipeline metadata
    enriched_metadata_key: str
    pipeline_time_ms: int


class PipelineOrchestrator:
    """Orchestrates PDF extraction → Bedrock metadata generation."""

    def __init__(
        self,
        s3_client=None,
        bedrock_client=None,
        model_id: str | None = None,
    ):
        self._s3 = s3_client or boto3.client("s3")
        self._extractor = PDFExtractor(s3_client=self._s3)
        self._inference = BedrockInference(
            bedrock_client=bedrock_client, model_id=model_id
        )

    def run(
        self,
        bucket: str,
        key: str,
        ou: str,
        product_part_number: str,
        thesaurus_terms: list[str] | None = None,
    ) -> PipelineResult:
        """Run the full pipeline: extract → infer → write enriched metadata.

        Args:
            bucket: S3 bucket containing the PDF.
            key: S3 key of the PDF (e.g. PES/pending/STD-12345.pdf).
            ou: Organizational unit (e.g. PES).
            product_part_number: Product identifier (e.g. STD-12345).
            thesaurus_terms: Optional IEEE thesaurus terms for Bedrock context.

        Returns:
            PipelineResult with extraction + inference results.
        """
        start = time.time()

        # Step 1: Extract text from PDF
        logger.info(
            "Step 1/3: Extracting text from s3://%s/%s", bucket, key
        )
        extraction = self._extractor.extract(
            bucket=bucket,
            key=key,
            ou=ou,
            product_part_number=product_part_number,
        )

        extracted_text = extraction["text"]
        if not extracted_text.strip():
            logger.warning("PDF extraction returned empty text — skipping Bedrock")
            return PipelineResult(
                text_length=0,
                page_count=extraction["page_count"],
                extraction_method=extraction["extraction_method"],
                abstract="",
                keywords=[],
                learning_level="",
                intended_audience="",
                category="",
                enriched_metadata_key="",
                pipeline_time_ms=int((time.time() - start) * 1000),
            )

        logger.info(
            "Extracted %d chars, %d pages (method: %s)",
            len(extracted_text),
            extraction["page_count"],
            extraction["extraction_method"],
        )

        # Step 2: Generate metadata via Bedrock
        logger.info("Step 2/3: Generating metadata via Bedrock")
        inference = self._inference.generate_metadata(
            text=extracted_text,
            thesaurus_terms=thesaurus_terms,
        )
        logger.info(
            "Bedrock returned: %d keywords, level=%s, audience=%s",
            len(inference["keywords"]),
            inference["learning_level"],
            inference["intended_audience"],
        )

        # Step 3: Write enriched metadata to S3
        enriched_key = f"{ou}/metadata/{product_part_number}.enriched.json"
        logger.info("Step 3/3: Writing enriched metadata to s3://%s/%s", bucket, enriched_key)

        enriched = {
            "product_part_number": product_part_number,
            "ou": ou,
            "source_key": key,
            "extraction": {
                "page_count": extraction["page_count"],
                "extraction_method": extraction["extraction_method"],
                "text_length": len(extracted_text),
            },
            "metadata": {
                "abstract": inference["abstract"],
                "keywords": inference["keywords"],
                "learning_level": inference["learning_level"],
                "intended_audience": inference["intended_audience"],
                "category": inference["category"],
            },
            "processing_time_ms": inference["processing_time_ms"],
        }

        self._s3.put_object(
            Bucket=bucket,
            Key=enriched_key,
            Body=json.dumps(enriched, indent=2).encode(),
            ContentType="application/json",
        )

        pipeline_ms = int((time.time() - start) * 1000)
        logger.info("Pipeline complete in %dms", pipeline_ms)

        return PipelineResult(
            text_length=len(extracted_text),
            page_count=extraction["page_count"],
            extraction_method=extraction["extraction_method"],
            abstract=inference["abstract"],
            keywords=inference["keywords"],
            learning_level=inference["learning_level"],
            intended_audience=inference["intended_audience"],
            category=inference["category"],
            enriched_metadata_key=enriched_key,
            pipeline_time_ms=pipeline_ms,
        )
