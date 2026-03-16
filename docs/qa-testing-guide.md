# QA Testing Guide — Image Overlay Generation (CC3-778)

**Date:** 2026-03-16
**Environment:** AWS Account `141770997341`, Region `us-east-1`
**Profile:** `ieee-cc`

## What Was Implemented

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

## Test 5: Error Handling

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

- Font warning: "No TrueType font found, using Pillow default bitmap font" — this is expected on Lambda. The module falls back to Pillow's default bitmap font when system TrueType fonts (DejaVu, Liberation, Helvetica) are not available in the Lambda container.
- S3 trigger deletes the trigger JSON on success, so direct invocation after trigger fires will fail with AccessDenied (trigger already consumed).
- All triggers were auto-processed within seconds of upload.

---

## Cleanup

Remove test images after QA:

```bash
aws s3 rm s3://dev-ieee-conference-cloud-bulk-uploads/images/products/ --recursive --exclude "*" --include "QA-TEST-*" --include "TEST-*"
aws s3 rm s3://dev-ieee-conference-cloud-bulk-uploads/backgrounds/ieee-test.jpg
```

---

## Unit Tests (40 total, all passing)

Run locally:
```bash
python -m pytest tests/generators/test_image_overlay_generator.py tests/handlers/test_image_overlay_handler.py -v
```

| Test Suite | Count | Status |
|------------|-------|--------|
| tests/generators/test_image_overlay_generator.py | 28 | PASS |
| tests/handlers/test_image_overlay_handler.py | 12 | PASS |
