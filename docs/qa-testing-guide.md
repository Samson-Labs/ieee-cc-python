# QA Testing Guide — PDF Text Extraction (CC3-774)

**Date:** 2026-03-16
**Environment:** AWS Account `141770997341`, Region `us-east-1`
**Profile:** `ieee-cc`

## What Was Implemented

Reusable Python module (`src/extractors/pdf_extractor.py`) that extracts raw text from PDF files stored in S3 using PyMuPDF. Returns cleaned text suitable for passing to AWS Bedrock (Claude Sonnet). Deployed as a Docker-based Lambda (`ieee-cc-pdf-extractor`).

### Acceptance Criteria

- [x] Extracts text from single-column and multi-column PDFs
- [x] Handles scanned PDFs gracefully (returns empty text with warning, no crash)
- [x] Strips headers, footers, and page numbers where possible
- [x] Returns structured result: `{"text": "...", "page_count": N, "extraction_method": "text|ocr|failed"}`
- [x] Truncates text to 180,000 characters (fits within Claude Sonnet context window with system prompt)
- [x] Returns `page_count` for the MetadataExtractor on Drupal side
- [x] Writes page count JSON to `{ou}/metadata/{product_part_number}.pdf.json`
- [x] Unit tests cover: normal PDF, scanned PDF, encrypted PDF, corrupted PDF, very large PDF
- [x] S3 Input Path: `{ou}/pending/{filename}.pdf`
- [x] S3 Metadata Output: `{ou}/metadata/{product_part_number}.pdf.json`

---

## Prerequisites

- AWS CLI installed and configured with profile `ieee-cc`
- Permissions to invoke Lambda functions and read/write S3
- Set profile for all commands:
  ```bash
  export AWS_PROFILE=ieee-cc
  export AWS_REGION=us-east-1
  ```

---

## Lambda Details

| Setting | Value |
|---------|-------|
| Function Name | `ieee-cc-pdf-extractor` |
| Memory | 3 GB |
| Timeout | 5 min |
| Runtime | Python 3.13 (Docker) |
| S3 Bucket | `dev-ieee-conference-cloud-bulk-uploads` |

Verify the Lambda is active:
```bash
aws lambda get-function --function-name ieee-cc-pdf-extractor --query "Configuration.{State:State,Memory:MemorySize,Timeout:Timeout}" --output table
```

---

## Test Data in S3

| PDF File | S3 Key | Size | Pages | Expected Method |
|----------|--------|------|-------|-----------------|
| PES_TP_Mag_PE_v23_N6_SP.pdf | `PES/pending/PES_TP_Mag_PE_v23_N6_SP.pdf` | 11 MB | 202 | text |
| PES_TR_138_SBLCS_011726.pdf | `PES/pending/PES_TR_138_SBLCS_011726.pdf` | 3.1 MB | 89 | text |
| PES_TR_TR139_ITSLC_012826.pdf | `PES/pending/PES_TR_TR139_ITSLC_012826.pdf` | 1.6 MB | 43 | text |
| pes_wp_peswfi_wfi_022025.pdf | `PES/pending/pes_wp_peswfi_wfi_022025.pdf` | 1.5 MB | 34 | text |
| PES_MAG_ELE_13-4.pdf | `PES/pending/PES_MAG_ELE_13-4.pdf` | 73 MB | 92 | text |
| PES_PUB_TP_TP101_101995.pdf | `PES/pending/PES_PUB_TP_TP101_101995.pdf` | 98 MB | 187 | ocr (scanned) |

Verify test files exist:
```bash
aws s3 ls s3://dev-ieee-conference-cloud-bulk-uploads/PES/pending/
```

---

## Test 1: Text-Based PDF Extraction

Invoke the Lambda with a text-based PDF:

```bash
aws lambda invoke \
  --function-name ieee-cc-pdf-extractor \
  --payload '{"bucket":"dev-ieee-conference-cloud-bulk-uploads","key":"PES/pending/PES_TR_TR139_ITSLC_012826.pdf","ou":"PES","product_part_number":"PES_TR_TR139_ITSLC_012826"}' \
  /tmp/pdf-response.json && cat /tmp/pdf-response.json | python3 -m json.tool
```

### Expected Response

```json
{
  "statusCode": 200,
  "body": {
    "text": "<extracted text content — should be non-empty>",
    "page_count": 43,
    "extraction_method": "text"
  }
}
```

### Validation

- [ ] `statusCode` is `200`
- [ ] `extraction_method` is `"text"`
- [ ] `page_count` is `43`
- [ ] `text` is non-empty and contains readable content from the PDF

---

## Test 2: Scanned PDF (OCR Detection)

Invoke with the scanned PDF — this should detect no extractable text:

```bash
aws lambda invoke \
  --function-name ieee-cc-pdf-extractor \
  --payload '{"bucket":"dev-ieee-conference-cloud-bulk-uploads","key":"PES/pending/PES_PUB_TP_TP101_101995.pdf","ou":"PES","product_part_number":"PES_PUB_TP_TP101_101995"}' \
  /tmp/pdf-scanned.json && cat /tmp/pdf-scanned.json | python3 -m json.tool
```

### Expected Response

```json
{
  "statusCode": 200,
  "body": {
    "text": "",
    "page_count": 187,
    "extraction_method": "ocr"
  }
}
```

### Validation

- [ ] `statusCode` is `200`
- [ ] `extraction_method` is `"ocr"` (correctly identified as scanned)
- [ ] `page_count` is `187`
- [ ] `text` is empty (no text extractable from scanned images)

---

## Test 3: Large PDF (202 pages)

```bash
aws lambda invoke \
  --function-name ieee-cc-pdf-extractor \
  --payload '{"bucket":"dev-ieee-conference-cloud-bulk-uploads","key":"PES/pending/PES_TP_Mag_PE_v23_N6_SP.pdf","ou":"PES","product_part_number":"PES_TP_Mag_PE_v23_N6_SP"}' \
  /tmp/pdf-large.json && cat /tmp/pdf-large.json | python3 -m json.tool
```

### Validation

- [ ] `statusCode` is `200`
- [ ] `page_count` is `202`
- [ ] `extraction_method` is `"text"`
- [ ] `text` is non-empty
- [ ] Text length does not exceed 180,000 characters (truncation limit for Claude Sonnet)
- [ ] Lambda completes within 5 min timeout

---

## Test 4: Run All 6 PDFs

Run all test PDFs and compare results:

```bash
for pdf in PES_TP_Mag_PE_v23_N6_SP PES_TR_138_SBLCS_011726 PES_TR_TR139_ITSLC_012826 pes_wp_peswfi_wfi_022025 PES_MAG_ELE_13-4 PES_PUB_TP_TP101_101995; do
  echo "=== $pdf ==="
  aws lambda invoke \
    --function-name ieee-cc-pdf-extractor \
    --payload "{\"bucket\":\"dev-ieee-conference-cloud-bulk-uploads\",\"key\":\"PES/pending/${pdf}.pdf\",\"ou\":\"PES\",\"product_part_number\":\"${pdf}\"}" \
    /tmp/pdf-${pdf}.json 2>/dev/null
  python3 -c "
import json
with open('/tmp/pdf-${pdf}.json') as f:
    data = json.load(f)
b = data['body']
print(f\"  Status: {data['statusCode']}\")
print(f\"  Pages: {b['page_count']}\")
print(f\"  Method: {b['extraction_method']}\")
print(f\"  Text length: {len(b['text'])} chars\")
"
  echo ""
done
```

---

## Test 5: Verify Metadata Written to S3

After extraction, a metadata JSON is written to `PES/metadata/`. Verify:

```bash
# List all metadata files
aws s3 ls s3://dev-ieee-conference-cloud-bulk-uploads/PES/metadata/ | grep ".pdf.json"

# Read a specific metadata file
aws s3 cp s3://dev-ieee-conference-cloud-bulk-uploads/PES/metadata/PES_TR_TR139_ITSLC_012826.pdf.json - | python3 -m json.tool
```

### Expected Metadata Format

```json
{
  "pageCount": 43,
  "extractionMethod": "text",
  "extractedAt": "2026-03-11T11:25:13.881445Z"
}
```

### Validation

- [ ] Metadata JSON exists at `PES/metadata/<product_part_number>.pdf.json`
- [ ] `pageCount` matches the PDF page count
- [ ] `extractionMethod` is `"text"` or `"ocr"`
- [ ] `extractedAt` is a valid ISO 8601 timestamp

---

## Test 6: Error Handling

### Non-existent file (should return 500)

```bash
aws lambda invoke \
  --function-name ieee-cc-pdf-extractor \
  --payload '{"bucket":"dev-ieee-conference-cloud-bulk-uploads","key":"PES/pending/DOES_NOT_EXIST.pdf","ou":"PES","product_part_number":"DOES_NOT_EXIST"}' \
  /tmp/pdf-error.json && cat /tmp/pdf-error.json | python3 -m json.tool
```

- [ ] `statusCode` is `500`
- [ ] `body.error` contains `"NoSuchKey"` or S3 error message

### Missing required fields (should return 400)

```bash
aws lambda invoke \
  --function-name ieee-cc-pdf-extractor \
  --payload '{"bucket":"dev-ieee-conference-cloud-bulk-uploads"}' \
  /tmp/pdf-bad-request.json && cat /tmp/pdf-bad-request.json | python3 -m json.tool
```

- [ ] `statusCode` is `400`
- [ ] `body.error` mentions missing fields

### Empty event (should return 400)

```bash
aws lambda invoke \
  --function-name ieee-cc-pdf-extractor \
  --payload '{}' \
  /tmp/pdf-empty.json && cat /tmp/pdf-empty.json | python3 -m json.tool
```

- [ ] `statusCode` is `400`

---

## CloudWatch Logs

Check Lambda execution logs:

```bash
aws logs tail /aws/lambda/ieee-cc-pdf-extractor --since 1h --format short
```

---

## Existing Test Results (2026-03-11)

These are the validated results from the initial test run on AWS:

| PDF | Pages | Method | Text Length | Result |
|-----|-------|--------|-------------|--------|
| PES_TP_Mag_PE_v23_N6_SP.pdf | 202 | text | Non-empty | PASS |
| PES_TR_138_SBLCS_011726.pdf | 89 | text | Non-empty | PASS |
| PES_TR_TR139_ITSLC_012826.pdf | 43 | text | Non-empty | PASS |
| pes_wp_peswfi_wfi_022025.pdf | 34 | text | Non-empty | PASS |
| PES_MAG_ELE_13-4.pdf | 92 | text | Non-empty | PASS |
| PES_PUB_TP_TP101_101995.pdf | 187 | ocr | Empty | PASS (correctly identified as scanned) |

### Metadata Files in S3

| File | Pages | Method | Extracted At |
|------|-------|--------|-------------|
| PES_TP_Mag_PE_v23_N6_SP.pdf.json | 202 | text | 2026-03-11T11:25:06Z |
| PES_TR_138_SBLCS_011726.pdf.json | 89 | text | 2026-03-11T11:25:10Z |
| PES_TR_TR139_ITSLC_012826.pdf.json | 43 | text | 2026-03-11T11:25:13Z |
| pes_wp_peswfi_wfi_022025.pdf.json | 34 | text | 2026-03-11T11:25:17Z |
| PES_MAG_ELE_13-4.pdf.json | 92 | text | 2026-03-11T11:25:24Z |
| PES_PUB_TP_TP101_101995.pdf.json | 187 | ocr | 2026-03-11T11:25:21Z |

---

## Unit Tests (30 total, all passing)

Run locally:
```bash
python -m pytest tests/extractors/test_pdf_extractor.py tests/handlers/test_pdf_handler.py -v
```

| Test Suite | Count | Status |
|------------|-------|--------|
| tests/extractors/test_pdf_extractor.py | 21 | PASS |
| tests/handlers/test_pdf_handler.py | 9 | PASS |
