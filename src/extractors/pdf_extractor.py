"""PDF Text Extraction module for IEEE Content Conversion pipeline.

Extracts raw text from PDF files stored in S3 using PyMuPDF (fitz).
Returns cleaned text suitable for passing to Bedrock (Claude Sonnet).
Handles large PDFs by truncating to the model's context window.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from io import BytesIO
from typing import TypedDict

import boto3
import fitz  # PyMuPDF

logger = logging.getLogger(__name__)

MAX_TEXT_LENGTH = 180_000
HEADER_FOOTER_MARGIN_RATIO = 0.08  # top/bottom 8% of page treated as header/footer zone


class ExtractionResult(TypedDict):
    text: str
    page_count: int
    extraction_method: str  # "text" | "ocr" | "failed"


class PDFExtractor:
    """Extracts text from PDFs stored in S3.

    Usage (called by the orchestrator):
        extractor = PDFExtractor(s3_client=boto3.client("s3"))
        result = extractor.extract(bucket, key, ou, product_part_number)
    """

    def __init__(self, s3_client=None):
        self._s3 = s3_client or boto3.client("s3")

    def extract(
        self,
        bucket: str,
        key: str,
        ou: str,
        product_part_number: str,
    ) -> ExtractionResult:
        """Download a PDF from S3, extract text, and write page-count metadata.

        Args:
            bucket: S3 bucket name.
            key: S3 object key, e.g. ``{ou}/pending/{filename}.pdf``.
            ou: Organizational unit prefix used for output paths.
            product_part_number: Identifier used in the metadata filename.

        Returns:
            ExtractionResult with text, page_count, and extraction_method.
        """
        pdf_bytes = self._download(bucket, key)
        result = self.extract_from_bytes(pdf_bytes)

        self._write_metadata(
            bucket=bucket,
            ou=ou,
            product_part_number=product_part_number,
            page_count=result["page_count"],
            extraction_method=result["extraction_method"],
        )

        return result

    def extract_from_bytes(self, pdf_bytes: bytes) -> ExtractionResult:
        """Extract text from raw PDF bytes (no S3 interaction).

        Useful for unit testing and non-S3 callers.
        """
        try:
            doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        except Exception:
            logger.exception("Failed to open PDF")
            return ExtractionResult(text="", page_count=0, extraction_method="failed")

        try:
            return self._extract_from_document(doc)
        finally:
            doc.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _download(self, bucket: str, key: str) -> bytes:
        resp = self._s3.get_object(Bucket=bucket, Key=key)
        return resp["Body"].read()

    def _extract_from_document(self, doc: fitz.Document) -> ExtractionResult:
        page_count = len(doc)

        if doc.is_encrypted:
            logger.warning("PDF is encrypted – cannot extract text")
            return ExtractionResult(
                text="", page_count=page_count, extraction_method="failed"
            )

        pages_text: list[str] = []
        total_chars = 0
        has_text = False

        for page in doc:
            page_text = self._extract_page_text(page)
            if page_text.strip():
                has_text = True
            pages_text.append(page_text)
            total_chars += len(page_text)

        if not has_text:
            logger.warning(
                "PDF appears to be scanned (no extractable text on %d pages). "
                "OCR is not performed; returning empty text.",
                page_count,
            )
            return ExtractionResult(
                text="", page_count=page_count, extraction_method="ocr"
            )

        full_text = "\n\n".join(pages_text)
        full_text = _clean_text(full_text)

        if len(full_text) > MAX_TEXT_LENGTH:
            logger.info(
                "Truncating extracted text from %d to %d characters",
                len(full_text),
                MAX_TEXT_LENGTH,
            )
            full_text = full_text[:MAX_TEXT_LENGTH]

        return ExtractionResult(
            text=full_text, page_count=page_count, extraction_method="text"
        )

    def _extract_page_text(self, page: fitz.Page) -> str:
        """Extract text from a single page, stripping header/footer zones."""
        rect = page.rect
        margin = rect.height * HEADER_FOOTER_MARGIN_RATIO

        # Clip to content area (exclude top/bottom margins)
        content_rect = fitz.Rect(
            rect.x0,
            rect.y0 + margin,
            rect.x1,
            rect.y1 - margin,
        )

        text = page.get_text("text", clip=content_rect)
        return text

    def _write_metadata(
        self,
        bucket: str,
        ou: str,
        product_part_number: str,
        page_count: int,
        extraction_method: str,
    ) -> None:
        metadata_key = f"{ou}/metadata/{product_part_number}.pdf.json"
        body = json.dumps(
            {
                "pageCount": page_count,
                "extractionMethod": extraction_method,
                "extractedAt": datetime.now(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
            }
        )
        self._s3.put_object(
            Bucket=bucket,
            Key=metadata_key,
            Body=body.encode(),
            ContentType="application/json",
        )
        logger.info("Wrote metadata to s3://%s/%s", bucket, metadata_key)


# ------------------------------------------------------------------
# Text cleaning utilities
# ------------------------------------------------------------------

_PAGE_NUMBER_RE = re.compile(
    r"(?m)"
    r"(?:^[ \t]*\d{1,4}[ \t]*$)"  # standalone page numbers
    r"|"
    r"(?:^[ \t]*(?:page|pg\.?)[ \t]*\d{1,4}[ \t]*$)",  # "Page 12" variants
    re.IGNORECASE,
)

_EXCESSIVE_NEWLINES_RE = re.compile(r"\n{3,}")


def _clean_text(text: str) -> str:
    """Remove residual page numbers and normalise whitespace."""
    text = _PAGE_NUMBER_RE.sub("", text)
    text = _EXCESSIVE_NEWLINES_RE.sub("\n\n", text)
    return text.strip()
