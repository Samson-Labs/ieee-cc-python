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

**Tests:** 21 tests in `tests/extractors/test_pdf_extractor.py` — all passing. Covers normal, scanned, encrypted, corrupted, large PDF, multi-column, Unicode, S3 errors, and text cleaning.

---

## 2026-03-09 — AWS Deployment (Docker Lambda + CLI)

**Files added:**
- `Dockerfile` — Lambda container image based on `public.ecr.aws/lambda/python:3.13`
- `src/handlers/pdf_handler.py` — Lambda handler wrapping `PDFExtractor`
- `scripts/deploy.sh` — Full deployment: ECR repo, S3 bucket, IAM role, Docker build+push, Lambda creation
- `scripts/invoke.sh` — Manual Lambda invocation helper
- `scripts/teardown.sh` — Cleanup (deletes Lambda, IAM role, ECR; preserves S3 bucket)

**Handler design:**
- Supports two invocation patterns: direct (orchestrator passes `bucket/key/ou/product_part_number`) and S3 event trigger (derives `ou` and `product_part_number` from key pattern `{ou}/pending/{filename}.pdf`)
- Returns `statusCode` 200/400/500 with structured body
- Extractor instance created at module level for connection reuse across warm invocations

**Lambda config:**
- 3 GB memory (large PDFs with PyMuPDF need headroom)
- 5-minute timeout
- x86_64 architecture (PyMuPDF wheel compatibility)
- Environment vars: `BUCKET_NAME`, `LOG_LEVEL`

**AWS resources (all `ieee-cc-` prefixed):**
- ECR: `ieee-cc-pdf-extractor`
- S3: `ieee-cc-python` (versioned, all public access blocked)
- IAM: `ieee-cc-pdf-extractor-role` (Lambda basic execution + S3 GetObject/PutObject)
- Lambda: `ieee-cc-pdf-extractor`

**Deployment note:** Docker image must be built with `--platform linux/amd64 --provenance=false` on Apple Silicon to produce a single-arch manifest compatible with Lambda.

**Tests:** 8 handler tests in `tests/handlers/test_pdf_handler.py` — direct invocation, S3 event parsing, missing fields, error handling.

---

## 2026-03-09 — Verified End-to-End on AWS

**Test performed:**
1. Uploaded 2-page test PDF to `s3://ieee-cc-python/ieee/pending/STD-TEST-001.pdf`
2. Invoked Lambda — returned `statusCode: 200`, extracted text from both pages
3. Verified metadata written to `s3://ieee-cc-python/ieee/metadata/STD-TEST-001.pdf.json`

**Result:** Full pipeline working — PDF download, text extraction, metadata write, structured response all confirmed on account `141770997341` (us-east-1).

---

## 2026-03-10 — Image Overlay Generation Module

**Module:** `src/generators/image_overlay_generator.py`
**Handler:** `src/handlers/image_overlay_handler.py`

**What was built:**
- `ImageOverlayGenerator` class that reads JSON trigger files from S3, loads a background image, applies text overlays (title, authors) using Pillow, and writes output to a destination bucket.
- Supports both S3 event triggers (`actions/*.json`) and direct Lambda invocations.
- Title overlay: word-wrapped, max 3 lines, 40px font, truncated with ellipsis.
- Author overlay: word-wrapped, max 2 lines, 24px font.
- Thumbnail generation when `is_thumbnail: true` (resized to 400x300 max).
- Output formats: JPEG (default, with configurable quality) and PNG.
- RGBA-to-RGB conversion for JPEG output (no alpha channel support).
- Trigger JSON deleted on success, preserved on failure for retry.
- Font loading with system font fallback chain (Linux/Lambda, macOS).

**Trigger JSON schema:**
```json
{
  "product_part_number": "STD-12345",
  "title": "Product Title",
  "authors": "Author One, Author Two",
  "config": {
    "source_bucket": "ieee-rc-assets",
    "dest_bucket": "ieee-rc-public",
    "public_path": "images/products"
  },
  "background_source": "ieee",
  "output_format": "jpg",
  "output_quality": 85,
  "is_thumbnail": false
}
```

**S3 paths:**
- Trigger input: `actions/{job_id}.json`
- Background: `backgrounds/{background_source}.jpg`
- Output: `{config.public_path}/{product_part_number}.{format}`
- Thumbnail: `{config.public_path}/{product_part_number}_thumb.{format}`

**Lambda config:** `ieee-rc-image-generator`, Python 3.12 + Pillow, 1024 MB, 60s timeout.

**Decisions:**
- Used `src/generators/` (not `src/extractors/`) since this module generates output rather than extracting content.
- Trigger JSON acts as a job queue — Drupal writes JSON, Lambda processes and deletes it.
- Font fallback chain avoids hard dependency on specific system fonts.
- Validation errors return 400 (not 500) so the orchestrator can distinguish bad input from infra failures.

**Tests:** 28 generator tests + 12 handler tests = 40 total in `tests/generators/test_image_overlay_generator.py` and `tests/handlers/test_image_overlay_handler.py`. Covers overlay rendering, text wrapping/truncation, thumbnail generation, output formats (JPEG/PNG), trigger validation, S3 errors, image encoding, event parsing, and error handling.
