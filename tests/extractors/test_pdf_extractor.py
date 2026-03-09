"""Tests for PDFExtractor.

Covers: normal PDF, scanned PDF, encrypted PDF, corrupted PDF, very large PDF.
"""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock, patch

import fitz  # PyMuPDF
import pytest

from src.extractors.pdf_extractor import (
    MAX_TEXT_LENGTH,
    ExtractionResult,
    PDFExtractor,
    _clean_text,
)


# ---------------------------------------------------------------------------
# Helpers to build in-memory PDFs
# ---------------------------------------------------------------------------

def _make_pdf(pages: list[str], *, encrypt: bool = False) -> bytes:
    """Create a minimal PDF in memory with the given page texts."""
    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        # Insert text in the middle of the page (avoids header/footer strip)
        tw = fitz.TextWriter(page.rect)
        tw.append(fitz.Point(72, page.rect.height / 2), text)
        tw.write_text(page)
    if encrypt:
        # PyMuPDF save with encryption
        buf = doc.tobytes(
            encryption=fitz.PDF_ENCRYPT_AES_256,
            owner_pw="owner",
            user_pw="user",
            permissions=0,
        )
    else:
        buf = doc.tobytes()
    doc.close()
    return buf


def _make_scanned_pdf(num_pages: int = 3) -> bytes:
    """Create a PDF with pages that have no extractable text (simulates scanned)."""
    doc = fitz.open()
    for _ in range(num_pages):
        # Insert a blank page with a small image instead of text
        page = doc.new_page()
        # Draw a filled rect to simulate a scanned image — no text layer
        shape = page.new_shape()
        shape.draw_rect(fitz.Rect(100, 100, 200, 200))
        shape.finish(color=(0, 0, 0), fill=(0.5, 0.5, 0.5))
        shape.commit()
    buf = doc.tobytes()
    doc.close()
    return buf


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def extractor():
    return PDFExtractor(s3_client=MagicMock())


@pytest.fixture
def normal_pdf():
    return _make_pdf(["Hello World from page one.", "Content on page two."])


@pytest.fixture
def scanned_pdf():
    return _make_scanned_pdf(3)


@pytest.fixture
def encrypted_pdf():
    return _make_pdf(["Secret content."], encrypt=True)


@pytest.fixture
def corrupted_pdf():
    return b"%PDF-1.4 this is not a valid pdf"


@pytest.fixture
def large_pdf():
    """PDF whose extracted text exceeds MAX_TEXT_LENGTH."""
    # Each page has ~1000 chars; need enough pages to exceed 180k
    chunk = "A" * 1000 + " "
    num_pages = (MAX_TEXT_LENGTH // 1000) + 50  # comfortably over the limit
    return _make_pdf([chunk] * num_pages)


# ---------------------------------------------------------------------------
# Tests: extract_from_bytes
# ---------------------------------------------------------------------------

class TestNormalPDF:
    def test_extracts_text(self, extractor: PDFExtractor, normal_pdf: bytes):
        result = extractor.extract_from_bytes(normal_pdf)

        assert result["extraction_method"] == "text"
        assert result["page_count"] == 2
        assert "Hello World" in result["text"]
        assert "page two" in result["text"]

    def test_text_is_cleaned(self, extractor: PDFExtractor):
        pdf = _make_pdf(["Real content.\n\n\n\n\nMore content."])
        result = extractor.extract_from_bytes(pdf)

        # Excessive newlines should be collapsed
        assert "\n\n\n" not in result["text"]


class TestScannedPDF:
    def test_returns_empty_text_with_ocr_method(
        self, extractor: PDFExtractor, scanned_pdf: bytes
    ):
        result = extractor.extract_from_bytes(scanned_pdf)

        assert result["extraction_method"] == "ocr"
        assert result["text"] == ""
        assert result["page_count"] == 3

    def test_logs_warning(self, extractor: PDFExtractor, scanned_pdf: bytes):
        with patch("src.extractors.pdf_extractor.logger") as mock_logger:
            extractor.extract_from_bytes(scanned_pdf)
            mock_logger.warning.assert_called_once()
            assert "scanned" in mock_logger.warning.call_args[0][0].lower()


class TestEncryptedPDF:
    def test_returns_failed(self, extractor: PDFExtractor, encrypted_pdf: bytes):
        result = extractor.extract_from_bytes(encrypted_pdf)

        assert result["extraction_method"] == "failed"
        assert result["text"] == ""
        # page_count may still be available from the encrypted doc
        assert result["page_count"] >= 0


class TestCorruptedPDF:
    def test_returns_failed(self, extractor: PDFExtractor, corrupted_pdf: bytes):
        result = extractor.extract_from_bytes(corrupted_pdf)

        assert result["extraction_method"] == "failed"
        assert result["text"] == ""
        assert result["page_count"] == 0


class TestLargePDF:
    def test_truncates_to_max_length(self, extractor: PDFExtractor, large_pdf: bytes):
        result = extractor.extract_from_bytes(large_pdf)

        assert result["extraction_method"] == "text"
        assert len(result["text"]) <= MAX_TEXT_LENGTH
        assert result["page_count"] > 0


# ---------------------------------------------------------------------------
# Tests: S3 integration (extract with metadata write)
# ---------------------------------------------------------------------------

class TestExtractWithS3:
    def test_downloads_and_writes_metadata(self, normal_pdf: bytes):
        s3_mock = MagicMock()
        s3_mock.get_object.return_value = {"Body": BytesIO(normal_pdf)}

        extractor = PDFExtractor(s3_client=s3_mock)
        result = extractor.extract(
            bucket="my-bucket",
            key="ieee/pending/doc.pdf",
            ou="ieee",
            product_part_number="STD-12345",
        )

        assert result["extraction_method"] == "text"
        assert result["page_count"] == 2

        # Verify S3 download
        s3_mock.get_object.assert_called_once_with(
            Bucket="my-bucket", Key="ieee/pending/doc.pdf"
        )

        # Verify metadata write
        s3_mock.put_object.assert_called_once()
        put_kwargs = s3_mock.put_object.call_args[1]
        assert put_kwargs["Bucket"] == "my-bucket"
        assert put_kwargs["Key"] == "ieee/metadata/STD-12345.pdf.json"
        assert put_kwargs["ContentType"] == "application/json"

        metadata = json.loads(put_kwargs["Body"].decode())
        assert metadata["pageCount"] == 2
        assert metadata["extractionMethod"] == "text"
        assert "extractedAt" in metadata
        assert metadata["extractedAt"].endswith("Z")


# ---------------------------------------------------------------------------
# Tests: _clean_text utility
# ---------------------------------------------------------------------------

class TestCleanText:
    def test_removes_standalone_page_numbers(self):
        text = "Some content.\n  42  \nMore content."
        assert "42" not in _clean_text(text)

    def test_removes_page_prefix_numbers(self):
        text = "Content.\nPage 7\nMore."
        cleaned = _clean_text(text)
        assert "Page 7" not in cleaned

    def test_preserves_inline_numbers(self):
        text = "The standard defines 42 requirements."
        assert "42" in _clean_text(text)

    def test_collapses_excessive_newlines(self):
        text = "A\n\n\n\n\nB"
        assert _clean_text(text) == "A\n\nB"
