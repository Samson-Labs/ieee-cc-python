# QA Testing Guide — PDF Text Extractor (CC3-774)

## Overview

The PDF Text Extractor (`ieee-cc-pdf-extractor`) is a reusable Lambda module that extracts raw text from PDF files stored in S3. Uses PyMuPDF (fitz) for text extraction, strips headers/footers, removes page numbers, and truncates to 180,000 characters for Claude Sonnet's context window.

## Prerequisites

- AWS CLI configured with `ieee-cc` profile
- Access to `dev-ieee-conference-cloud-bulk-uploads` S3 bucket
- Lambda deployed: `ieee-cc-pdf-extractor`

## Lambda Details

| Property | Value |
|----------|-------|
| Function Name | `ieee-cc-pdf-extractor` |
| Runtime | Python 3.13 (Docker image) |
| Memory | 3,008 MB |
| Timeout | 300s (5 minutes) |
| ECR Repo | `ieee-cc-pdf-extractor` |
| IAM Role | `ieee-cc-pdf-extractor-role` |

## Acceptance Criteria Checklist

- [x] Extracts text from single-column and multi-column PDFs
- [x] Handles scanned PDFs gracefully (returns empty text with warning, no crash)
- [x] Strips headers, footers, and page numbers where possible
- [x] Returns structured result: `{"text": "...", "page_count": N, "extraction_method": "extract_text|ocr|failed"}`
- [x] Truncates text to 180,000 characters (fits within Claude Sonnet context window)
- [x] Returns page_count for the MetadataExtractor on Drupal side
- [x] Writes page count JSON to `{ou}/metadata/{product_part_number}.pdf.json`
- [x] Unit tests cover: normal PDF, scanned PDF, encrypted PDF, corrupted PDF, very large PDF

## S3 Paths

| Path | Purpose |
|------|---------|
| `{ou}/pending/{filename}.pdf` | Input PDF file |
| `{ou}/metadata/{product_part_number}.pdf.json` | Output metadata JSON |

## Metadata Output Format

```json
{
  "pageCount": 42,
  "extractionMethod": "extract_text",
  "extractedAt": "2026-03-15T14:30:00.123456Z"
}
```

---

## Test Cases

### TC-1: Normal Single-Column PDF

**Purpose:** Verify text extraction from a standard single-column technical report.

**Invoke:**
```bash
./scripts/invoke.sh dev-ieee-conference-cloud-bulk-uploads \
    PES/pending/PES_TR_TR139_ITSLC_012826.pdf PES PES_TR_TR139_ITSLC_012826
```

**Expected:**
- `statusCode: 200`
- `extraction_method: "extract_text"`
- `page_count` matches actual PDF page count
- `text` contains meaningful extracted content
- Metadata JSON written to `PES/metadata/PES_TR_TR139_ITSLC_012826.pdf.json`

**Verify metadata:**
```bash
aws s3 cp s3://dev-ieee-conference-cloud-bulk-uploads/PES/metadata/PES_TR_TR139_ITSLC_012826.pdf.json - --profile ieee-cc
```

---

### TC-2: Large PDF with Truncation

**Purpose:** Verify large PDFs are truncated to 180,000 characters.

**Invoke:**
```bash
./scripts/invoke.sh dev-ieee-conference-cloud-bulk-uploads \
    PES/pending/PES_TR_138_SBLCS_011726.pdf PES PES_TR_138_SBLCS_011726
```

**Expected:**
- `statusCode: 200`
- `extraction_method: "extract_text"`
- `text` length is exactly 180,000 characters (truncated)
- No errors or crashes

---

### TC-3: Multi-Column Magazine PDF

**Purpose:** Verify extraction from multi-column layout PDFs (magazines, journals).

**Invoke:**
```bash
./scripts/invoke.sh dev-ieee-conference-cloud-bulk-uploads \
    PES/pending/PES_MAG_ELE_13-4.pdf PES PES_MAG_ELE_13-4
```

**Expected:**
- `statusCode: 200`
- `extraction_method: "extract_text"`
- Text from both columns extracted (not garbled/merged)
- `page_count` matches actual page count

---

### TC-4: Scanned PDF (Image-Only)

**Purpose:** Verify scanned PDFs return gracefully without crashing.

**Invoke:**
```bash
./scripts/invoke.sh dev-ieee-conference-cloud-bulk-uploads \
    PES/pending/PES_PUB_TP_TP101_101995.pdf PES PES_PUB_TP_TP101_101995
```

**Expected:**
- `statusCode: 200`
- `extraction_method: "ocr"`
- `text: ""` (empty — no OCR engine, graceful degradation)
- `page_count` still returned correctly
- No crash or error

---

### TC-5: Small Whitepaper PDF

**Purpose:** Verify extraction from a smaller PDF that doesn't require truncation.

**Invoke:**
```bash
./scripts/invoke.sh dev-ieee-conference-cloud-bulk-uploads \
    PES/pending/pes_wp_peswfi_wfi_022025.pdf PES pes_wp_peswfi_wfi_022025
```

**Expected:**
- `statusCode: 200`
- `extraction_method: "extract_text"`
- Text length less than 180,000 (no truncation needed)
- Content is readable and meaningful

---

### TC-6: Missing PDF File

**Purpose:** Verify error handling when the PDF doesn't exist in S3.

**Invoke:**
```bash
aws lambda invoke \
    --function-name ieee-cc-pdf-extractor \
    --region us-east-1 \
    --payload '{"bucket":"dev-ieee-conference-cloud-bulk-uploads","key":"PES/pending/NONEXISTENT.pdf","ou":"PES","product_part_number":"NONEXISTENT"}' \
    --profile ieee-cc \
    /tmp/pdf-error.json && python3 -m json.tool /tmp/pdf-error.json
```

**Expected:**
- `statusCode: 500`
- Error message referencing S3 NoSuchKey or similar

---

### TC-7: Invalid Event (Missing Fields)

**Purpose:** Verify handler rejects events missing required fields.

**Invoke:**
```bash
aws lambda invoke \
    --function-name ieee-cc-pdf-extractor \
    --region us-east-1 \
    --payload '{"bucket":"dev-ieee-conference-cloud-bulk-uploads"}' \
    --profile ieee-cc \
    /tmp/pdf-bad-event.json && python3 -m json.tool /tmp/pdf-bad-event.json
```

**Expected:**
- `statusCode: 400`
- Error about missing required fields

---

## AWS Live Test Results

### Test 1: Normal Single-Column PDF (43 pages, 1.6 MB)
- **File:** `PES/pending/PES_TR_TR139_ITSLC_012826.pdf`
- **Result:** PASSED
- **Response:** `statusCode: 200`, `page_count: 43`, `extraction_method: "extract_text"`, `text_length: 180,000` (truncated)
- **Metadata:** `{"pageCount": 43, "extractionMethod": "extract_text", "extractedAt": "2026-03-18T15:20:36.049466Z"}`

### Test 2: Large Technical Report (89 pages, 3.1 MB)
- **File:** `PES/pending/PES_TR_138_SBLCS_011726.pdf`
- **Result:** PASSED
- **Response:** `statusCode: 200`, `page_count: 89`, `extraction_method: "extract_text"`, `text_length: 180,000` (truncated)
- **Verification:** Text correctly truncated to 180k char limit

### Test 3: Multi-Column Magazine (92 pages, 70 MB)
- **File:** `PES/pending/PES_MAG_ELE_13-4.pdf`
- **Result:** PASSED
- **Response:** `statusCode: 200`, `page_count: 92`, `extraction_method: "extract_text"`, `text_length: 180,000` (truncated)
- **Metadata:** `{"pageCount": 92, "extractionMethod": "extract_text", "extractedAt": "2026-03-16T17:00:14.623358Z"}`

### Test 4: Scanned PDF / Image-Only (187 pages, 94 MB)
- **File:** `PES/pending/PES_PUB_TP_TP101_101995.pdf`
- **Result:** PASSED
- **Response:** `statusCode: 200`, `page_count: 187`, `extraction_method: "ocr"`, `text_length: 0`
- **Notes:** Graceful degradation — returned empty text with "ocr" method, no crash

### Test 5: Small Whitepaper (34 pages, 1.5 MB)
- **File:** `PES/pending/pes_wp_peswfi_wfi_022025.pdf`
- **Result:** PASSED
- **Response:** `statusCode: 200`, `page_count: 34`, `extraction_method: "extract_text"`, `text_length: 71,971`
- **Notes:** No truncation needed (under 180k limit)

### Results Summary

| # | Test Case | File Size | Pages | Method | Text Length | Status |
|---|-----------|-----------|-------|--------|-------------|--------|
| 1 | Single-column report | 1.6 MB | 43 | text | 180,000 (truncated) | PASSED |
| 2 | Large technical report | 3.1 MB | 89 | text | 180,000 (truncated) | PASSED |
| 3 | Multi-column magazine | 70 MB | 92 | text | 180,000 (truncated) | PASSED |
| 4 | Scanned/image-only PDF | 94 MB | 187 | ocr | 0 (graceful) | PASSED |
| 5 | Small whitepaper | 1.5 MB | 34 | text | 71,971 | PASSED |

---

## Unit Tests

| Test File | Tests | Description |
|-----------|-------|-------------|
| `tests/extractors/test_pdf_extractor.py` | 21 | Normal, scanned, encrypted, corrupted, large, multi-column, unicode, S3 errors |
| `tests/handlers/test_pdf_handler.py` | 9 | Direct invocation, S3 events, error handling, event parsing |
| **Total** | **30** | All passing |

### Test Classes

| Class | Tests | Coverage |
|-------|-------|----------|
| `TestNormalPDF` | 2 | Text extraction, text cleaning |
| `TestScannedPDF` | 2 | Empty text with "ocr" method, warning logging |
| `TestEncryptedPDF` | 1 | Returns "failed" extraction method |
| `TestCorruptedPDF` | 1 | Returns "failed" extraction method |
| `TestLargePDF` | 1 | Truncation to 180,000 chars |
| `TestExtractWithS3` | 1 | S3 download + metadata write |
| `TestCleanText` | 4 | Page number removal, inline number preservation, newline collapsing |
| `TestMultiColumnPDF` | 2 | Both columns extracted, no garbage merging |
| `TestUnicodePDF` | 3 | Accented chars, German chars, blank PDF |
| `TestS3Errors` | 4 | NoSuchKey, AccessDenied, metadata write failure, timeout |

### Run Tests

```bash
# All PDF extractor tests
python -m pytest tests/extractors/test_pdf_extractor.py tests/handlers/test_pdf_handler.py -v

# Single test class
python -m pytest tests/extractors/test_pdf_extractor.py::TestScannedPDF -v
```

## Text Processing Pipeline

1. **Download** PDF from S3 (`{ou}/pending/{filename}.pdf`)
2. **Extract** text page-by-page using PyMuPDF
3. **Strip headers/footers** — removes top/bottom 8% of each page
4. **Remove page numbers** — regex strips standalone numbers (e.g., "42", "Page 42")
5. **Collapse whitespace** — reduces excessive newlines to double newlines
6. **Truncate** to 180,000 characters if needed
7. **Write metadata** to `{ou}/metadata/{product_part_number}.pdf.json`
8. **Return** structured result with text, page_count, extraction_method
