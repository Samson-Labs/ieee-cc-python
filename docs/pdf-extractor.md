# PDF Text Extraction Module

## Overview

Reusable Python module that extracts raw text from PDF files stored in S3. Designed to be called by the orchestrator Lambda in the IEEE Content Conversion pipeline. Returns cleaned text suitable for passing to AWS Bedrock (Claude Sonnet).

**Module path:** `src/extractors/pdf_extractor.py`

## Usage

```python
from src.extractors import PDFExtractor

extractor = PDFExtractor(s3_client=boto3.client("s3"))
result = extractor.extract(
    bucket="my-bucket",
    key="ieee/pending/document.pdf",
    ou="ieee",
    product_part_number="STD-12345",
)

print(result["text"])                # extracted text (up to 180k chars)
print(result["page_count"])          # number of pages in the PDF
print(result["extraction_method"])   # "extract_text" | "ocr" | "failed"
```

For testing or non-S3 usage:

```python
result = extractor.extract_from_bytes(pdf_bytes)
```

## S3 Paths

| Direction | Path Pattern |
|-----------|-------------|
| Input PDF | `{ou}/pending/{filename}.pdf` |
| Metadata output | `{ou}/metadata/{product_part_number}.pdf.json` |

## Metadata Output

Written to S3 after each extraction:

```json
{
  "pageCount": 42,
  "extractionMethod": "extract_text",
  "extractedAt": "2026-03-09T14:30:00Z"
}
```

This is consumed by the MetadataExtractor on the Drupal side.

## Extraction Methods

Vocabulary matches Drupal's `WebhookController` contract (CC3-952): `{transcribe, extract_text, ocr, failed}`.

| Method | Meaning |
|--------|---------|
| `extract_text` | Text was successfully extracted from the PDF — either from the native text layer, or (when OCR is enabled) recovered from a scanned PDF via Textract. |
| `ocr` | PDF appears scanned (no text layer) **and** no text was recovered — OCR disabled, or OCR ran but found nothing. Empty text returned with a warning; Drupal routes the item to manual entry. |
| `failed` | PDF could not be processed (encrypted, corrupted, or unreadable) |

### Scanned-PDF OCR fallback (CC3-1049, opt-in)

When a PDF lacks a substantial native text layer, an optional AWS Textract pass
can recover the text so it still flows through Bedrock enrichment instead of
requiring manual entry. Recovered text is returned as `extraction_method:
"extract_text"` (it is, semantically, extracted text), so the orchestrator and
Drupal contract are unchanged.

"Scanned" is detected as: **no page** carries ≥ `NATIVE_TEXT_SUFFICIENT` (100)
native characters **and** the pages are either textless or **image-dominated**
(a raster image covering ≥ 50% of the page). This catches both pure image-only
PDFs *and* scans with a tiny digital overlay — e.g. a `SAMPLE LETTER` heading
stamped over an otherwise-scanned letter, which a naive "any text → not scanned"
check would extract almost nothing from. A genuinely sparse text PDF with no
images (e.g. a title slide) keeps its native text and is **not** OCR'd.

| Env var | Default | Purpose |
|---------|---------|---------|
| `ENABLE_SCANNED_PDF_OCR` | _(off)_ | Set to `1`/`true` to enable the Textract fallback. |
| `MAX_OCR_PAGES` | `20` | Page cap — only the first N pages are OCR'd, bounding cost/latency on large scans. |

Cost ≈ **$1.50 / 1,000 pages** (Textract `DetectDocumentText`). The Lambda role
needs `textract:DetectDocumentText` (added in `scripts/deploy.sh`). If Textract
errors or finds nothing, the extractor degrades to the empty/`ocr` path — it
never raises.

## Text Processing Pipeline

1. **Header/footer stripping** — Each page is clipped to the inner 84% vertically (top/bottom 8% excluded). This removes running headers, footers, and page numbers from the extraction area.
2. **Page joining** — Pages are joined with double newlines.
3. **Page number removal** — Regex strips standalone numbers (e.g., `42`) and prefixed variants (e.g., `Page 12`, `Pg. 7`) that appear on their own line.
4. **Whitespace normalization** — Runs of 3+ newlines are collapsed to double newlines.
5. **Truncation** — Final text is capped at 180,000 characters to fit within Claude Sonnet's context window alongside the system prompt.

## Error Handling

| Scenario | Behavior |
|----------|----------|
| Corrupted/unreadable PDF | Returns `extraction_method: "failed"`, empty text, `page_count: 0` |
| Encrypted PDF | Returns `extraction_method: "failed"`, empty text, page count preserved |
| Scanned PDF (no text layer) | OCR off (default): `extraction_method: "ocr"`, empty text, warning logged. OCR on (`ENABLE_SCANNED_PDF_OCR`): Textract recovers text → `extraction_method: "extract_text"`; falls back to `ocr` if Textract finds nothing or errors |
| Successful text extraction | Returns `extraction_method: "extract_text"`, full text, `page_count: N` |
| PDF exceeding 180k chars | Text truncated, info logged |

## Dependencies

- **PyMuPDF (fitz)** — PDF parsing and text extraction
- **boto3** — S3 download and metadata upload

## Tests

```bash
python -m pytest tests/extractors/test_pdf_extractor.py -v
```

Tests cover: normal PDF, scanned PDF, scanned-PDF Textract OCR fallback (enabled/disabled, page cap, empty-result and Textract-error fallbacks), encrypted PDF, corrupted PDF, large PDF truncation, multi-column PDF, Unicode text, blank PDF, S3 errors (404, 403, 500, timeout), S3 integration with metadata write, and text cleaning utilities.
