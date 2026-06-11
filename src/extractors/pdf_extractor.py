"""PDF Text Extraction module for IEEE Content Conversion pipeline.

Extracts raw text from PDF files stored in S3 using PyMuPDF (fitz).
Returns cleaned text suitable for passing to Bedrock (Claude Sonnet).
Handles large PDFs by truncating to the model's context window.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import TypedDict

import boto3
import fitz  # PyMuPDF

logger = logging.getLogger(__name__)

MAX_TEXT_LENGTH = 180_000
HEADER_FOOTER_MARGIN_RATIO = 0.08  # top/bottom 8% of page treated as header/footer zone

# CC3-1049: optional OCR fallback for scanned/image-only PDFs (no text layer).
# When enabled, the first MAX_OCR_PAGES pages are rasterized and sent to AWS
# Textract; the recovered text is returned as a normal "extract_text" result so
# the orchestrator runs Bedrock exactly as it would for a born-digital PDF.
# Default OFF — turn on per environment after validating OCR quality + cost on
# real scans. Cost ≈ $1.50 / 1,000 pages (Textract DetectDocumentText); the page
# cap bounds the worst case (e.g. a 500-page scanned book).
OCR_RENDER_DPI = 200  # good legibility for OCR without oversized images
# A page with at least this many native characters is treated as a real text
# page (never OCR'd). Below this, a page is OCR-eligible only when it is image-
# dominated or textless — see _extract_from_document.
NATIVE_TEXT_SUFFICIENT = 100
# An image covering at least this fraction of the page area marks the page as a
# scan (its real content is locked in the image, not the text layer).
IMAGE_COVERAGE_RATIO = 0.5


def _ocr_enabled() -> bool:
    """Whether the scanned-PDF OCR fallback is enabled (env flag, default off)."""
    return os.environ.get("ENABLE_SCANNED_PDF_OCR", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _max_ocr_pages() -> int:
    """Page cap for OCR — bounds cost/latency on very large scans."""
    try:
        return max(1, int(os.environ.get("MAX_OCR_PAGES", "20")))
    except (TypeError, ValueError):
        return 20


class ExtractionResult(TypedDict):
    text: str
    page_count: int
    extraction_method: str  # "extract_text" | "ocr" | "failed"


class PDFExtractor:
    """Extracts text from PDFs stored in S3.

    Usage (called by the orchestrator):
        extractor = PDFExtractor(s3_client=boto3.client("s3"))
        result = extractor.extract(bucket, key, ou, product_part_number)
    """

    def __init__(self, s3_client=None, textract_client=None):
        self._s3 = s3_client or boto3.client("s3")
        # Created lazily on first OCR use (see _get_textract) so non-scanned
        # extractions — and tests — never need a Textract client.
        self._textract = textract_client

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
        logger.info("Extracting text from s3://%s/%s", bucket, key)
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
        for page in doc:
            pages_text.append(self._extract_page_text(page))

        # CC3-1049: detect "scanned" beyond pure image-only PDFs. A scan with a
        # tiny digital overlay (e.g. a 'SAMPLE LETTER' heading stamped over an
        # otherwise-scanned letter) leaves a sparse text layer that a naive
        # any-text check treats as a complete text PDF — extracting almost
        # nothing. Treat the PDF as scanned when no page carries a substantial
        # native text layer AND the pages are either textless or image-dominated
        # (a sparse-but-real text PDF with no images is left as extract_text).
        has_substantial_text = any(
            len(t.strip()) >= NATIVE_TEXT_SUFFICIENT for t in pages_text
        )
        all_textless = all(not t.strip() for t in pages_text)
        image_dominated = any(self._has_dominant_image(page) for page in doc)

        if not has_substantial_text and (all_textless or image_dominated):
            # When OCR is enabled, recover text via Textract and return it as a
            # normal "extract_text" result so the orchestrator runs Bedrock
            # unchanged.
            if _ocr_enabled():
                ocr_text = self._ocr_scanned(doc)
                if ocr_text.strip():
                    cleaned = _clean_text(ocr_text)
                    if len(cleaned) > MAX_TEXT_LENGTH:
                        cleaned = cleaned[:MAX_TEXT_LENGTH]
                    logger.info(
                        "Textract OCR recovered %d chars from scanned PDF (%d pages).",
                        len(cleaned),
                        page_count,
                    )
                    return ExtractionResult(
                        text=cleaned,
                        page_count=page_count,
                        extraction_method="extract_text",
                    )
                logger.warning(
                    "Scanned PDF (%d pages): Textract OCR returned no usable text; "
                    "falling back to manual entry.",
                    page_count,
                )
            else:
                logger.warning(
                    "PDF appears to be scanned (no substantial text layer on %d pages). "
                    "OCR disabled (ENABLE_SCANNED_PDF_OCR off); returning empty text.",
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
            text=full_text, page_count=page_count, extraction_method="extract_text"
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

    def _has_dominant_image(self, page: fitz.Page) -> bool:
        """Whether a raster image covers a large fraction of the page.

        The signal that a sparse-text page is actually a scan (real content
        locked in a full-page image), versus a genuinely sparse text page (e.g.
        a title slide) that has no image and should be kept as extracted text.
        """
        page_area = abs(page.rect.width * page.rect.height)
        if page_area <= 0:
            return False
        for img in page.get_images(full=True):
            try:
                rects = page.get_image_rects(img[0])
            except Exception:
                continue
            for rect in rects:
                if abs(rect.width * rect.height) >= IMAGE_COVERAGE_RATIO * page_area:
                    return True
        return False

    def _get_textract(self):
        """Lazily create the Textract client (only when OCR actually runs)."""
        if self._textract is None:
            self._textract = boto3.client("textract", region_name="us-east-1")
        return self._textract

    def _ocr_scanned(self, doc: fitz.Document) -> str:
        """OCR the first MAX_OCR_PAGES pages of a scanned PDF via AWS Textract.

        Rasterizes each page and calls the synchronous DetectDocumentText API
        (no S3 round-trip; one call per page, so the page cap directly bounds
        cost). Returns the recovered text, or "" if Textract finds nothing or
        errors — callers then fall back to the empty/manual path. Never raises.
        """
        total = len(doc)
        limit = min(total, _max_ocr_pages())
        if total > limit:
            logger.info(
                "Scanned PDF has %d pages; OCR-ing the first %d (MAX_OCR_PAGES).",
                total,
                limit,
            )
        client = self._get_textract()
        zoom = OCR_RENDER_DPI / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        pages_text: list[str] = []
        for i in range(limit):
            try:
                pix = doc[i].get_pixmap(matrix=matrix)
                png = pix.tobytes("png")
                resp = client.detect_document_text(Document={"Bytes": png})
                lines = [
                    block["Text"]
                    for block in resp.get("Blocks", [])
                    if block.get("BlockType") == "LINE" and block.get("Text")
                ]
                if lines:
                    pages_text.append("\n".join(lines))
            except Exception:
                logger.exception("Textract OCR failed on page %d; skipping.", i + 1)
        return "\n\n".join(pages_text)

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
