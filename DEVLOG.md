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
