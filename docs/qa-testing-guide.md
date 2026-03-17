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
Reusable Python module (`src/generators/image_overlay_generator.py`) that generates product overlay images from JSON trigger files written by Drupal's ImageGenerationService. Reads JSON triggers from S3 (`actions/*.json`), loads background images, applies title/author text overlays using Pillow, and writes output to a destination bucket. Deployed as a Docker-based Lambda (`ieee-rc-image-generator`) with an S3 event trigger on `actions/*.json`.

### Acceptance Criteria

- [x] Reads JSON trigger from S3 (`actions/*.json`)
- [x] Validates trigger JSON (required fields: product_part_number, title, authors, config, background_source)
- [x] Loads background image from S3 (`backgrounds/{source}.jpg`)
- [x] Applies title overlay (centered, proportional font, word-wrapped, max 4 lines, drop shadow)
- [x] Applies author overlay (centered, proportional font, word-wrapped, max 2 lines, drop shadow)
- [x] Supports JPG and PNG output formats (defaults to JPG)
- [x] Supports configurable output quality
- [x] Optional thumbnail generation (`is_thumbnail: true`, max 400x300)
- [x] Writes output to `{config.public_path}/{product_part_number}.{format}`
- [x] Deletes trigger JSON on success (not on failure)
- [x] S3 event trigger configured for `actions/*.json` prefix
- [x] Unit tests cover: overlay rendering, text wrapping, thumbnails, output formats, validation, S3 errors, image encoding

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
| Function Name | `ieee-rc-image-generator` |
| Memory | 1024 MB |
| Timeout | 60s |
| Runtime | Python 3.12 (Docker, Pillow) |
| S3 Bucket | `dev-ieee-conference-cloud-bulk-uploads` |
| S3 Trigger | `actions/*.json` → auto-invokes Lambda |

Verify the Lambda is active:
```bash
aws lambda get-function --function-name ieee-rc-image-generator --query "Configuration.{State:State,Memory:MemorySize,Timeout:Timeout}" --output table
```

---

## Setup: Upload a Background Image

A background image must exist in S3 before triggers can be processed. Upload one:

```bash
# Use any JPG image as a background (800x600 or larger recommended)
aws s3 cp <your-background>.jpg s3://dev-ieee-conference-cloud-bulk-uploads/backgrounds/ieee-test.jpg
```

Or create a test background:
```bash
python3 -c "
from PIL import Image
img = Image.new('RGB', (800, 600), color=(0, 40, 85))
img.save('/tmp/test-background.jpg', 'JPEG', quality=90)
" && aws s3 cp /tmp/test-background.jpg s3://dev-ieee-conference-cloud-bulk-uploads/backgrounds/ieee-test.jpg
```

---

## Test 1: Basic Image Generation (S3 Trigger)

Upload a trigger JSON — the S3 event trigger will automatically invoke the Lambda:

```bash
cat > /tmp/overlay-test1.json << 'EOF'
{
  "product_part_number": "QA-TEST-001",
  "title": "Advanced Power Systems Engineering: A Comprehensive Guide to Modern Grid Infrastructure",
  "authors": "Jane Doe, John Smith, Robert Johnson",
  "config": {
    "source_bucket": "dev-ieee-conference-cloud-bulk-uploads",
    "dest_bucket": "dev-ieee-conference-cloud-bulk-uploads",
    "public_path": "images/products"
  },
  "background_source": "ieee-test",
  "output_format": "jpg",
  "output_quality": 85
}
EOF

aws s3 cp /tmp/overlay-test1.json s3://dev-ieee-conference-cloud-bulk-uploads/actions/qa-test-001.json
```

Wait a few seconds, then verify:

```bash
# Check output image was created
aws s3 ls s3://dev-ieee-conference-cloud-bulk-uploads/images/products/QA-TEST-001.jpg

# Check trigger was deleted
aws s3 ls s3://dev-ieee-conference-cloud-bulk-uploads/actions/qa-test-001.json
```

Download and inspect:
```bash
aws s3 cp s3://dev-ieee-conference-cloud-bulk-uploads/images/products/QA-TEST-001.jpg /tmp/qa-test-001.jpg
python3 -c "
from PIL import Image; import os
img = Image.open('/tmp/qa-test-001.jpg')
print(f'Size: {img.size[0]}x{img.size[1]}')
print(f'Mode: {img.mode}')
print(f'Format: {img.format}')
print(f'File size: {os.path.getsize(\"/tmp/qa-test-001.jpg\")} bytes')
"
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
- [ ] Output image exists at `images/products/QA-TEST-001.jpg`
- [ ] Trigger JSON is deleted from `actions/`
- [ ] Image dimensions match background (800x600)
- [ ] Image format is JPEG
- [ ] Image contains visible title and author text overlays
- [ ] File size is reasonable (>5 KB)

---

## Test 2: Thumbnail Generation

```bash
cat > /tmp/overlay-test2.json << 'EOF'
{
  "product_part_number": "QA-TEST-THUMB",
  "title": "Smart Grid Optimization Techniques",
  "authors": "Alice Brown",
  "config": {
    "source_bucket": "dev-ieee-conference-cloud-bulk-uploads",
    "dest_bucket": "dev-ieee-conference-cloud-bulk-uploads",
    "public_path": "images/products"
  },
  "background_source": "ieee-test",
  "output_format": "jpg",
  "output_quality": 85,
  "is_thumbnail": true
}
EOF

aws s3 cp /tmp/overlay-test2.json s3://dev-ieee-conference-cloud-bulk-uploads/actions/qa-test-thumb.json
```

Wait a few seconds, then verify:

```bash
# Should have both full-size and thumbnail
aws s3 ls s3://dev-ieee-conference-cloud-bulk-uploads/images/products/QA-TEST-THUMB

# Download and check thumbnail dimensions
aws s3 cp s3://dev-ieee-conference-cloud-bulk-uploads/images/products/QA-TEST-THUMB_thumb.jpg /tmp/qa-thumb.jpg
python3 -c "
from PIL import Image
img = Image.open('/tmp/qa-thumb.jpg')
print(f'Thumbnail size: {img.size[0]}x{img.size[1]}')
assert img.size[0] <= 400 and img.size[1] <= 300, 'Thumbnail too large!'
print('Thumbnail size OK (within 400x300)')
"
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
- [ ] Full-size image exists: `images/products/QA-TEST-THUMB.jpg`
- [ ] Thumbnail exists: `images/products/QA-TEST-THUMB_thumb.jpg`
- [ ] Thumbnail dimensions are within 400x300
- [ ] Thumbnail file is smaller than full-size image
- [ ] Trigger JSON is deleted

---

## Test 3: PNG Output Format

```bash
cat > /tmp/overlay-test3.json << 'EOF'
{
  "product_part_number": "QA-TEST-PNG",
  "title": "Renewable Energy Integration Standards",
  "authors": "Bob Wilson, Carol Zhang, David Lee",
  "config": {
    "source_bucket": "dev-ieee-conference-cloud-bulk-uploads",
    "dest_bucket": "dev-ieee-conference-cloud-bulk-uploads",
    "public_path": "images/products"
  },
  "background_source": "ieee-test",
  "output_format": "png"
}
EOF

aws s3 cp /tmp/overlay-test3.json s3://dev-ieee-conference-cloud-bulk-uploads/actions/qa-test-png.json
```

Wait, then verify:

```bash
aws s3 ls s3://dev-ieee-conference-cloud-bulk-uploads/images/products/QA-TEST-PNG.png

aws s3 cp s3://dev-ieee-conference-cloud-bulk-uploads/images/products/QA-TEST-PNG.png /tmp/qa-png.png
python3 -c "
from PIL import Image
img = Image.open('/tmp/qa-png.png')
print(f'Size: {img.size[0]}x{img.size[1]}, Format: {img.format}, Mode: {img.mode}')
"
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
    /tmp/pdf-${pdf}.json
  python3 -c "
import json, sys
try:
    with open(f'/tmp/pdf-{pdf}.json') as f:
        data = json.load(f)
    body = data.get('body', {})
    status = data.get('statusCode')
    print(f'  Status: {status}')
    if status == 200:
        print(f'  Pages: {body.get(\"page_count\", \"N/A\")}')
        print(f'  Method: {body.get(\"extraction_method\", \"N/A\")}')
        print(f'  Text length: {len(body.get(\"text\", \"\"))} chars')
    else:
        print(f'  Error: {body.get(\"error\", \"Unknown error\")}')
except Exception as e:
    print(f'  Failed to process response: {type(e).__name__}', file=sys.stderr)
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
- [ ] Output file is `.png` (not `.jpg`)
- [ ] Image format is PNG
- [ ] Image mode is RGBA (PNG supports alpha)
- [ ] Trigger JSON is deleted

---

## Test 4: Long Title and Authors (Word Wrapping)

```bash
cat > /tmp/overlay-test4.json << 'EOF'
{
  "product_part_number": "QA-TEST-WRAP",
  "title": "A Very Long Title That Should Be Automatically Wrapped Across Multiple Lines by the Overlay Generator Because It Exceeds the Maximum Line Width",
  "authors": "Dr. Alexander Hamilton III, Professor Elizabeth Blackwell, Chief Engineer Nikola Tesla, Dr. Marie Curie, Alan Turing",
  "config": {
    "source_bucket": "dev-ieee-conference-cloud-bulk-uploads",
    "dest_bucket": "dev-ieee-conference-cloud-bulk-uploads",
    "public_path": "images/products"
  },
  "background_source": "ieee-test",
  "output_format": "jpg",
  "output_quality": 85
}
EOF

aws s3 cp /tmp/overlay-test4.json s3://dev-ieee-conference-cloud-bulk-uploads/actions/qa-test-wrap.json
```

### Validation

- [ ] Image is generated successfully
- [ ] Title text is wrapped (not cut off or overflowing)
- [ ] Author text is wrapped or truncated with ellipsis
- [ ] Text does not overflow the image boundaries

---

## Test 5: Legacy Drupal Schema (Backward Compatibility)

Tests that the Lambda accepts the existing Drupal `ImageGenerationService` JSON format:

```bash
cat > /tmp/overlay-test5.json << 'EOF'
{
    "sourceBucket": "dev-ieee-conference-cloud-bulk-uploads",
    "sourceName": "backgrounds/ieee-test.jpg",
    "destBucket": "dev-ieee-conference-cloud-bulk-uploads",
    "destName": "images/products/QA-LEGACY-001.jpg",
    "overlay": [
        {
            "text": "Advanced Power Systems Engineering",
            "attributes": [
                { "attr": "y", "value": "22%" },
                { "attr": "x", "value": "50%" },
                { "attr": "fill", "value": "white" },
                { "attr": "text-anchor", "value": "middle" },
                { "attr": "font-family", "value": "OpenSans" },
                { "attr": "font-weight", "value": "Bold" },
                { "attr": "font-size", "value": "40px" }
            ],
            "rowHeightPad": "20"
        },
        {
            "text": "Jane Doe, John Smith",
            "attributes": [
                { "attr": "y", "value": "65%" },
                { "attr": "x", "value": "50%" },
                { "attr": "fill", "value": "white" },
                { "attr": "text-anchor", "value": "middle" },
                { "attr": "font-size", "value": "24px" },
                { "attr": "font-weight", "value": "bold" }
            ],
            "rowHeightPad": "10"
        }
    ]
}
EOF

aws s3 cp /tmp/overlay-test5.json s3://dev-ieee-conference-cloud-bulk-uploads/actions/qa-legacy-001.json
```

Wait a few seconds, then verify:

```bash
aws s3 ls s3://dev-ieee-conference-cloud-bulk-uploads/images/products/QA-LEGACY-001.jpg

# Download and inspect
aws s3 cp s3://dev-ieee-conference-cloud-bulk-uploads/images/products/QA-LEGACY-001.jpg /tmp/qa-legacy-001.jpg
python3 -c "
from PIL import Image; import os
img = Image.open('/tmp/qa-legacy-001.jpg')
print(f'Size: {img.size[0]}x{img.size[1]}')
print(f'Format: {img.format}')
print(f'File size: {os.path.getsize(\"/tmp/qa-legacy-001.jpg\")} bytes')
"
```

### Validation

- [ ] Metadata JSON exists at `PES/metadata/<product_part_number>.pdf.json`
- [ ] `pageCount` matches the PDF page count
- [ ] `extractionMethod` is `"text"` or `"ocr"`
- [ ] `extractedAt` is a valid ISO 8601 timestamp
- [ ] Output image exists at `images/products/QA-LEGACY-001.jpg`
- [ ] Trigger JSON is deleted from `actions/`
- [ ] Image contains visible title and author text overlays
- [ ] CloudWatch log shows "Detected legacy Drupal trigger schema"
- [ ] File size is reasonable (>5 KB)

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
### Missing required fields (should return 400)

```bash
aws lambda invoke \
  --function-name ieee-rc-image-generator \
  --payload '{"bucket":"dev-ieee-conference-cloud-bulk-uploads"}' \
  /tmp/overlay-err1.json && cat /tmp/overlay-err1.json | python3 -m json.tool
```

- [ ] `statusCode` is `400`

### Wrong key prefix (should return 400)

```bash
aws lambda invoke \
  --function-name ieee-rc-image-generator \
  --payload '{"bucket":"b","key":"wrong/path.json"}' \
  /tmp/overlay-err2.json && cat /tmp/overlay-err2.json | python3 -m json.tool
```

- [ ] `statusCode` is `400`
- [ ] Error mentions "does not match expected pattern"

### Non-JSON key (should return 400)

```bash
aws lambda invoke \
  --function-name ieee-rc-image-generator \
  --payload '{"bucket":"b","key":"actions/file.txt"}' \
  /tmp/overlay-err3.json && cat /tmp/overlay-err3.json | python3 -m json.tool
```

- [ ] `statusCode` is `400`

### Error Summary

| Test Case | Expected Status | Expected Error |
|-----------|----------------|----------------|
| Missing fields | 400 | "Event must contain 'Records' or 'bucket'/'key'" |
| Wrong prefix | 400 | "does not match expected pattern 'actions/*.json'" |
| Non-JSON key | 400 | "does not match expected pattern 'actions/*.json'" |

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
aws logs filter-log-events \
  --log-group-name /aws/lambda/ieee-rc-image-generator \
  --start-time $(python3 -c "import time; print(int((time.time()-3600)*1000))") \
  --query "events[*].message" --output text
```

---

## Existing Test Results (2026-03-16)

### AWS Live Tests (via S3 Event Trigger)

| Test | Trigger | Output | Dimensions | Format | Size | Duration | Result |
|------|---------|--------|------------|--------|------|----------|--------|
| Basic JPG | test-001.json | TEST-001.jpg | 800x600 | JPEG | 14.9 KB | 279ms | PASS |
| Thumbnail | test-thumb.json | TEST-THUMB.jpg + TEST-THUMB_thumb.jpg | 800x600 / 400x300 | JPEG | 10.8 KB / 3.3 KB | 258ms | PASS |
| PNG format | test-png.json | TEST-PNG.png | 800x600 | PNG | 11.1 KB | 179ms | PASS |
| Direct invoke | test-direct.json | TEST-DIRECT.jpg | 800x600 | JPEG | 11.4 KB | ~200ms | PASS |
| Legacy schema | legacy-test-001.json | LEGACY-TEST-001.jpg | 800x600 | JPEG | 33.8 KB | ~374ms | PASS |
| Standard schema | standard-test-001.json | STANDARD-TEST-001.jpg | 800x600 | JPEG | 18.7 KB | ~270ms | PASS |

### Error Handling Tests

| Test | Expected | Actual | Result |
|------|----------|--------|--------|
| Missing fields | 400 | 400 — "Event must contain 'Records' or 'bucket'/'key'" | PASS |
| Wrong prefix | 400 | 400 — "does not match expected pattern" | PASS |
| Non-JSON key | 400 | 400 — "does not match expected pattern" | PASS |

### Lambda Performance

| Metric | Value |
|--------|-------|
| Cold start | ~1.9s (init) + 279ms (processing) |
| Warm invocation | ~180–260ms |
| Memory used | ~105–109 MB (of 1024 MB) |

### Notes

- OpenSans fonts (Bold, SemiBold) are bundled in the Docker image. Title text uses OpenSans Bold, author text uses OpenSans SemiBold.
- S3 trigger deletes the trigger JSON on success, so direct invocation after trigger fires will fail with AccessDenied (trigger already consumed).
- All triggers were auto-processed within seconds of upload.

---

## Cleanup

Remove test images after QA:

```bash
aws s3 rm s3://dev-ieee-conference-cloud-bulk-uploads/images/products/ --recursive --exclude "*" --include "QA-TEST-*" --include "QA-LEGACY-*" --include "TEST-*"
aws s3 rm s3://dev-ieee-conference-cloud-bulk-uploads/backgrounds/ieee-test.jpg
```

---

## Unit Tests (55 total, all passing)

Run locally:
```bash
python -m pytest tests/generators/test_image_overlay_generator.py tests/handlers/test_image_overlay_handler.py -v
```

| Test Suite | Count | Status |
|------------|-------|--------|
| tests/extractors/test_pdf_extractor.py | 21 | PASS |
| tests/handlers/test_pdf_handler.py | 9 | PASS |
| tests/generators/test_image_overlay_generator.py | 43 | PASS |
| tests/handlers/test_image_overlay_handler.py | 12 | PASS |
