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
print(result["extraction_method"])   # "text" | "ocr" | "failed"
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
  "extractionMethod": "text",
  "extractedAt": "2026-03-09T14:30:00Z"
}
```

This is consumed by the MetadataExtractor on the Drupal side.

## Extraction Methods

| Method | Meaning |
|--------|---------|
| `text` | Text was successfully extracted from the PDF |
| `ocr` | PDF appears scanned (no text layer). Empty text returned with a warning. OCR is not performed. |
| `failed` | PDF could not be processed (encrypted, corrupted, or unreadable) |

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
| Scanned PDF (no text layer) | Returns `extraction_method: "ocr"`, empty text, page count preserved, warning logged |
| PDF exceeding 180k chars | Text truncated, info logged |

## Dependencies

- **PyMuPDF (fitz)** — PDF parsing and text extraction
- **boto3** — S3 download and metadata upload

## Tests

```bash
python -m pytest tests/extractors/test_pdf_extractor.py -v
```

21 tests covering: normal PDF, scanned PDF, encrypted PDF, corrupted PDF, large PDF truncation, multi-column PDF, Unicode text, blank PDF, S3 errors (404, 403, 500, timeout), S3 integration with metadata write, and text cleaning utilities.
