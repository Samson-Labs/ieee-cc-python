# Dev Log

Chronological log of implementation progress, decisions, and modules completed.

---

## 2026-03-09 — PDF Text Extraction Module

**Module:** `src/extractors/pdf_extractor.py`

**What was built:**
- `PDFExtractor` class that downloads PDFs from S3, extracts text using PyMuPDF, and writes page-count metadata back to S3.
- Handles normal, scanned, encrypted, and corrupted PDFs gracefully.
- Strips headers/footers by clipping to the inner 84% of each page.
- Removes standalone page numbers via regex.
- Truncates output to 180,000 chars to fit Claude Sonnet's context window.
- Writes metadata JSON (`pageCount`, `extractionMethod`, `extractedAt`) to `{ou}/metadata/{product_part_number}.pdf.json`.

**Decisions:**
- Chose PyMuPDF (`fitz`) over `pdfplumber` — faster, lower memory, and handles edge cases better for large PDFs.
- Scanned PDFs return `extraction_method: "ocr"` with empty text and a warning rather than attempting OCR (no Tesseract dependency in Lambda).
- Encrypted PDFs return `extraction_method: "failed"` — no attempt to decrypt.
- Header/footer margin set at 8% of page height — reasonable default that catches most running headers without clipping body text.

**Tests:** 12 tests in `tests/extractors/test_pdf_extractor.py` — all passing. Covers normal, scanned, encrypted, corrupted, large PDF, S3 integration, and text cleaning.
