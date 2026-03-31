# QA Testing Guide — AI Orchestrator (CC3-773)

## Overview

The AI Orchestrator (`ieee-rc-ai-orchestrator`) is the central routing Lambda for the IEEE Content Conversion pipeline. It reads `.meta.json` files from S3, determines routing based on the `ai_enrichment_enabled` flag, and dispatches to downstream Lambdas.

## Prerequisites

- AWS CLI configured with `ieee-cc` profile
- Access to `dev-ieee-conference-cloud-bulk-uploads` S3 bucket
- Downstream Lambdas deployed: `ieee-cc-pdf-extractor`, `ieee-cc-video-transcriber`, `ieee-cc-bedrock-inference`

## Test Cases

### TC-1: AI Disabled — Move to /processed/

**Purpose:** Verify file is moved without AI processing when `ai_enrichment_enabled: false`.

**Setup:**
```bash
# Upload .meta.json with AI disabled
cat <<'EOF' | aws s3 cp - s3://dev-ieee-conference-cloud-bulk-uploads/PES/metadata/TEST_ITEM.meta.json
{
  "item_id": "TEST_ITEM",
  "ou": "PES",
  "product_part_number": "TEST_ITEM",
  "ai_enrichment_enabled": false,
  "content": { "media_type": "application/pdf", "filename": "TEST_ITEM.pdf" }
}
EOF

# Ensure test file exists in /pending/
aws s3 cp some-test.pdf s3://dev-ieee-conference-cloud-bulk-uploads/PES/pending/TEST_ITEM.pdf
```

**Invoke:**
```bash
./scripts/invoke-ai-orchestrator.sh dev-ieee-conference-cloud-bulk-uploads PES/pending/TEST_ITEM.pdf
```

**Expected:**
- `action: "moved"`, `ai_enrichment_enabled: false`
- File moved from `PES/pending/TEST_ITEM.pdf` to `PES/processed/TEST_ITEM.pdf`
- No downstream Lambda invoked

**Verify:**
```bash
# Should exist
aws s3 ls s3://dev-ieee-conference-cloud-bulk-uploads/PES/processed/TEST_ITEM.pdf

# Should NOT exist
aws s3 ls s3://dev-ieee-conference-cloud-bulk-uploads/PES/pending/TEST_ITEM.pdf
```

---

### TC-2: AI Enabled — PDF Extraction + Bedrock

**Purpose:** Verify full PDF pipeline: extract text, invoke Bedrock, send webhook, move file.

**Setup:**
```bash
cat <<'EOF' | aws s3 cp - s3://dev-ieee-conference-cloud-bulk-uploads/PES/metadata/TEST_PDF.meta.json
{
  "item_id": "TEST_PDF",
  "ou": "PES",
  "product_part_number": "TEST_PDF",
  "ai_enrichment_enabled": true,
  "content": { "media_type": "application/pdf", "filename": "TEST_PDF.pdf" },
  "callback_url": "https://httpbin.org/post"
}
EOF
```

**Invoke:**
```bash
./scripts/invoke-ai-orchestrator.sh dev-ieee-conference-cloud-bulk-uploads PES/pending/TEST_PDF.pdf
```

**Expected:**
- `action: "enriched"`, `ai_enrichment_enabled: true`
- PDF extractor Lambda invoked successfully
- Bedrock metadata generated
- Webhook sent to httpbin.org (status 200)
- File moved to `PES/processed/TEST_PDF.pdf`

---

### TC-3: AI Enabled — Video Transcription + Bedrock

**Purpose:** Verify full video pipeline: transcribe, invoke Bedrock, send webhook, move file.

**Setup:**
```bash
cat <<'EOF' | aws s3 cp - s3://dev-ieee-conference-cloud-bulk-uploads/PES/metadata/TEST_VIDEO.meta.json
{
  "item_id": "TEST_VIDEO",
  "ou": "PES",
  "product_part_number": "TEST_VIDEO",
  "ai_enrichment_enabled": true,
  "content": { "media_type": "video/mp4", "filename": "TEST_VIDEO.mp4" },
  "callback_url": "https://httpbin.org/post"
}
EOF
```

**Expected:**
- Video transcriber Lambda invoked
- Bedrock metadata generated from transcript
- File moved to `PES/processed/TEST_VIDEO.mp4`

---

### TC-4: Missing .meta.json

**Purpose:** Verify error handling when `.meta.json` does not exist.

**Invoke:**
```bash
aws lambda invoke \
    --function-name ieee-rc-ai-orchestrator \
    --payload '{"bucket":"dev-ieee-conference-cloud-bulk-uploads","key":"PES/pending/NONEXISTENT.pdf"}' \
    /tmp/test-output.json && cat /tmp/test-output.json
```

**Expected:**
- `statusCode: 400`
- Error: "Meta file not found"
- File NOT moved

---

### TC-5: Invalid .meta.json (Missing Fields)

**Purpose:** Verify validation catches incomplete `.meta.json`.

**Setup:**
```bash
echo '{"item_id": "BAD"}' | aws s3 cp - s3://dev-ieee-conference-cloud-bulk-uploads/PES/metadata/BAD_META.meta.json
```

**Expected:**
- `statusCode: 400`
- Error: "Missing required .meta.json fields"

---

### TC-6: Unsupported Media Type

**Purpose:** Verify rejection of unsupported media types.

**Setup:**
```bash
cat <<'EOF' | aws s3 cp - s3://dev-ieee-conference-cloud-bulk-uploads/PES/metadata/BAD_TYPE.meta.json
{
  "item_id": "BAD_TYPE",
  "ou": "PES",
  "product_part_number": "BAD_TYPE",
  "ai_enrichment_enabled": true,
  "content": { "media_type": "image/png", "filename": "BAD_TYPE.png" }
}
EOF
```

**Expected:**
- `statusCode: 400`
- Error: "Unsupported media type: image/png"

---

### TC-7: Invalid Key Format

**Purpose:** Verify rejection of keys that don't match `{ou}/pending/{filename}`.

**Invoke:**
```bash
aws lambda invoke \
    --function-name ieee-rc-ai-orchestrator \
    --payload '{"bucket":"dev-ieee-conference-cloud-bulk-uploads","key":"invalid/path/file.pdf"}' \
    /tmp/test-output.json && cat /tmp/test-output.json
```

**Expected:**
- `statusCode: 400`
- Error about key not matching expected pattern

---

## AWS Live Test Results

### Test 1: AI Disabled Flow
- **Date:** 2026-03-18
- **File:** `PES/pending/PES_TR_TR139_ITSLC_012826.pdf` (1.6 MB)
- **Result:** PASSED
- **Response:**
  ```json
  {
    "statusCode": 200,
    "body": {
      "item_id": "PES_TR_TR139_ITSLC_012826",
      "ou": "PES",
      "action": "moved",
      "ai_enrichment_enabled": false,
      "source_key": "PES/pending/PES_TR_TR139_ITSLC_012826.pdf",
      "destination_key": "PES/processed/PES_TR_TR139_ITSLC_012826.pdf",
      "processing_time_ms": 322
    }
  }
  ```
- **Verification:** File confirmed moved from `/pending/` to `/processed/`, source deleted

### Test 2: AI Enabled + PDF Flow
- **Date:** 2026-03-18
- **File:** `PES/pending/PES_TR_TR139_ITSLC_012826.pdf` (1.6 MB)
- **Result:** PARTIAL — PDF extraction succeeded, Bedrock blocked by account model access issue
- **Error:** `Bedrock ResourceNotFoundException: Model use case details have not been submitted`
- **Notes:**
  - PDF extractor Lambda dispatched successfully (confirmed by new `.pdf.json` metadata)
  - File correctly remained in `/pending/` (error stops pipeline, no partial move)
  - Bedrock issue is account-level, not a code defect — was working on 2026-03-17

### Unit Tests
- **Module tests:** 29 tests (key parsing, validation, meta reading, move, AI flows, dispatch errors, webhook)
- **Handler tests:** 17 tests (event parsing, direct/S3 invocation, error handling, context)
- **Total:** 46 tests, all passing

## Performance

| Flow | Processing Time |
|------|----------------|
| AI Disabled (move only) | ~320ms |
| AI Enabled + PDF | Depends on PDF extractor + Bedrock (~30-60s typical) |
| AI Enabled + Video | Depends on video length + Transcribe + Bedrock (~2-10 min) |

## Known Issues

1. **Bedrock model access:** Account model access may need periodic renewal. Error: `ResourceNotFoundException: Model use case details have not been submitted`. Resolution: Submit/renew the Anthropic use case details form in AWS console.
