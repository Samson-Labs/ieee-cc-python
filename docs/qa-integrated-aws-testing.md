# QA Integrated AWS Testing Guide

End-to-end testing guide for the IEEE Content Conversion pipeline in AWS.

## Prerequisites

```bash
# 1. AWS CLI configured with ieee-cc profile
aws sts get-caller-identity --profile ieee-cc

# 2. Set profile (or use direnv)
export AWS_PROFILE=ieee-cc
export AWS_REGION=us-east-1

# 3. Verify all Lambdas are deployed
aws lambda list-functions --query 'Functions[?starts_with(FunctionName, `ieee-`)].FunctionName' --output table
```

**Expected Lambdas:**

| Lambda | Purpose |
|--------|---------|
| `ieee-cc-pdf-extractor` | PDF text extraction |
| `ieee-cc-video-transcriber` | Video transcription via AWS Transcribe |
| `ieee-cc-bedrock-inference` | AI metadata generation via Claude Sonnet |
| `ieee-rc-ai-orchestrator` | Central router — orchestrates the full pipeline |
| `ieee-rc-image-generator` | Branded image overlay generation |
| `ieee-rc-dlq-processor` | Dead letter queue — retries or archives failures |

**S3 Bucket:** `dev-ieee-conference-cloud-bulk-uploads`

---

## Important: File Lifecycle

Files move through the pipeline as follows:

```
{ou}/pending/{filename}     <- Input (upload here)
{ou}/metadata/{item_id}.meta.json  <- Config (create this)
{ou}/metadata/{ppn}.pdf.json       <- Output metadata (auto-generated)
{ou}/processed/{filename}  <- Final location (auto-moved)
```

If a file has already been processed, it will be in `/processed/`. To re-test, **copy it back to `/pending/`**:

```bash
aws s3 cp \
    s3://dev-ieee-conference-cloud-bulk-uploads/{ou}/processed/{filename} \
    s3://dev-ieee-conference-cloud-bulk-uploads/{ou}/pending/{filename}
```

---

## Test 1: PDF Extraction (Standalone)

Tests the PDF extractor Lambda directly, without the orchestrator.

### Step 1: Ensure the PDF is in `/pending/`

```bash
# Check if file exists
aws s3 ls s3://dev-ieee-conference-cloud-bulk-uploads/PES/pending/PES_TR_TR139_ITSLC_012826.pdf

# If not found, copy from /processed/ (if it was already processed before)
aws s3 cp \
    s3://dev-ieee-conference-cloud-bulk-uploads/PES/processed/PES_TR_TR139_ITSLC_012826.pdf \
    s3://dev-ieee-conference-cloud-bulk-uploads/PES/pending/PES_TR_TR139_ITSLC_012826.pdf

# Or upload a new PDF from your local machine
aws s3 cp /path/to/your/file.pdf \
    s3://dev-ieee-conference-cloud-bulk-uploads/PES/pending/PES_TR_TR139_ITSLC_012826.pdf
```

### Step 2: Invoke the PDF Extractor

```bash
./scripts/invoke.sh dev-ieee-conference-cloud-bulk-uploads \
    PES/pending/PES_TR_TR139_ITSLC_012826.pdf PES PES_TR_TR139_ITSLC_012826
```

### Expected Result

```json
{
  "statusCode": 200,
  "body": {
    "text": "... extracted text content ...",
    "page_count": 42,
    "extraction_method": "text"
  }
}
```

### Verify Output

```bash
# Metadata JSON should be written
aws s3 ls s3://dev-ieee-conference-cloud-bulk-uploads/PES/metadata/PES_TR_TR139_ITSLC_012826.pdf.json
```

### Common Errors

| Error | Cause | Fix |
|-------|-------|-----|
| `NoSuchKey: The specified key does not exist` | File not in `/pending/` | Copy file to `/pending/` first (see Step 1) |
| `AccessDenied` | Missing IAM permissions | Run deploy script to update role |

---

## Test 2: Full Pipeline via Orchestrator (PDF)

Tests the complete flow: PDF extraction -> Bedrock metadata -> Webhook -> Move to processed.

### Step 1: Ensure the PDF is in `/pending/`

```bash
aws s3 cp \
    s3://dev-ieee-conference-cloud-bulk-uploads/PES/processed/PES_TR_138_SBLCS_011726.pdf \
    s3://dev-ieee-conference-cloud-bulk-uploads/PES/pending/PES_TR_138_SBLCS_011726.pdf
```

### Step 2: Create `.meta.json`

```bash
cat <<'EOF' | aws s3 cp - s3://dev-ieee-conference-cloud-bulk-uploads/PES/metadata/PES_TR_138_SBLCS_011726.meta.json
{
  "item_id": "PES_TR_138_SBLCS_011726",
  "ou": "PES",
  "product_part_number": "CAS2023NEWCAS0000",
  "ai_enrichment_enabled": true,
  "callback_url": "https://pr-646-cc-ieee.pantheonsite.io/api/v1/ai/webhook",
  "content": {
    "media_type": "application/pdf",
    "filename": "PES_TR_138_SBLCS_011726.pdf"
  }
}
EOF
```

**Required fields in `.meta.json`:**

| Field | Type | Description |
|-------|------|-------------|
| `item_id` | string | Must match the filename (without extension) |
| `ou` | string | Organizational unit (e.g. PES, CAS, AESS) |
| `product_part_number` | string | Drupal product identifier |
| `ai_enrichment_enabled` | boolean | `true` for full pipeline, `false` to just move |
| `callback_url` | string | Drupal webhook URL (optional) |
| `content.media_type` | string | `application/pdf`, `video/mp4`, `video/quicktime`, `video/webm` |
| `content.filename` | string | Original filename |

### Step 3: Invoke the Orchestrator

```bash
./scripts/invoke-ai-orchestrator.sh dev-ieee-conference-cloud-bulk-uploads \
    PES/pending/PES_TR_138_SBLCS_011726.pdf
```

### Expected Result

```json
{
  "statusCode": 200,
  "body": {
    "item_id": "PES_TR_138_SBLCS_011726",
    "ou": "PES",
    "action": "enriched",
    "ai_enrichment_enabled": true,
    "source_key": "PES/pending/PES_TR_138_SBLCS_011726.pdf",
    "destination_key": "PES/processed/PES_TR_138_SBLCS_011726.pdf",
    "processing_time_ms": 17682
  }
}
```

### Verify in CloudWatch

```bash
aws logs tail /aws/lambda/ieee-rc-ai-orchestrator --since 5m --format short
```

**Expected log sequence:**
1. `Processing s3://...`
2. `ai_enrichment_enabled=True, media_type=application/pdf`
3. `Dispatching to PDF extractor`
4. `Extraction complete`
5. `Invoking Bedrock for metadata generation`
6. `Bedrock metadata generated`
7. `Webhook sent to ... (status 200)`
8. `Moving ... -> .../processed/...`
9. `Enrichment complete in XXXXms`

### Verify Output

```bash
# File moved to /processed/
aws s3 ls s3://dev-ieee-conference-cloud-bulk-uploads/PES/processed/PES_TR_138_SBLCS_011726.pdf

# File removed from /pending/
aws s3 ls s3://dev-ieee-conference-cloud-bulk-uploads/PES/pending/PES_TR_138_SBLCS_011726.pdf
# (should return nothing)
```

---

## Test 3: Full Pipeline via Orchestrator (Video)

### Step 1: Ensure video is in `/pending/`

```bash
aws s3 ls s3://dev-ieee-conference-cloud-bulk-uploads/AESS/pending/

# If needed, copy a video file to pending
aws s3 cp \
    s3://dev-ieee-conference-cloud-bulk-uploads/AESS/processed/AESSBMR0000.mp4 \
    s3://dev-ieee-conference-cloud-bulk-uploads/AESS/pending/AESSBMR0000.mp4
```

### Step 2: Create `.meta.json`

```bash
cat <<'EOF' | aws s3 cp - s3://dev-ieee-conference-cloud-bulk-uploads/AESS/metadata/AESSBMR0000.meta.json
{
  "item_id": "AESSBMR0000",
  "ou": "AESS",
  "product_part_number": "AESSBMR0000",
  "ai_enrichment_enabled": true,
  "callback_url": "https://httpbin.org/post",
  "content": {
    "media_type": "video/mp4",
    "filename": "AESSBMR0000.mp4"
  }
}
EOF
```

### Step 3: Invoke

```bash
./scripts/invoke-ai-orchestrator.sh dev-ieee-conference-cloud-bulk-uploads \
    AESS/pending/AESSBMR0000.mp4
```

**Note:** Video transcription takes 2-10 minutes depending on file length.

### Expected CloudWatch Log Sequence
1. `Dispatching to video transcriber`
2. `Extraction complete: ieee-cc-video-transcriber`
3. `Invoking Bedrock for metadata generation`
4. `Bedrock metadata generated`
5. `Webhook sent to ... (status 200)`
6. `Enrichment complete`

---

## Test 4: AI Disabled (Move Only)

### Setup

```bash
cat <<'EOF' | aws s3 cp - s3://dev-ieee-conference-cloud-bulk-uploads/PES/metadata/TEST_SKIP.meta.json
{
  "item_id": "TEST_SKIP",
  "ou": "PES",
  "product_part_number": "TEST_SKIP",
  "ai_enrichment_enabled": false,
  "content": { "media_type": "application/pdf", "filename": "TEST_SKIP.pdf" }
}
EOF

# Create a small test file
echo "test" | aws s3 cp - s3://dev-ieee-conference-cloud-bulk-uploads/PES/pending/TEST_SKIP.pdf
```

### Invoke

```bash
./scripts/invoke-ai-orchestrator.sh dev-ieee-conference-cloud-bulk-uploads PES/pending/TEST_SKIP.pdf
```

### Expected

- `action: "moved"`, `ai_enrichment_enabled: false`
- No Lambda dispatch, no Bedrock, no webhook
- File moved to `PES/processed/TEST_SKIP.pdf`
- Processing time: ~200-400ms

---

## Test 5: Webhook HMAC Signing

### Verify webhook reaches Drupal with valid signature

```bash
python3 -c "
import hmac, hashlib, json, urllib.request, urllib.error

secret = 'webhook-test-secret'
url = 'https://pr-646-cc-ieee.pantheonsite.io/api/v1/ai/webhook'

payload = {
    'signal': 'extraction_ready',
    'product_part_number': 'CAS2023NEWCAS0000',
    'metadata': {
        'abstract': 'First paragraph summary of the document content and methodology.\n\nSecond paragraph with key findings and implications.',
        'keywords': ['power systems', 'renewable energy', 'smart grid', 'load control', 'energy storage', 'demand response', 'grid resilience', 'distributed generation'],
        'learning_level': 'Professional',
        'intended_audience': 'Seasoned Engineering Professional',
        'category': 'Technical Tutorial'
    }
}

body_bytes = json.dumps(payload).encode()
signature = hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()

req = urllib.request.Request(url, data=body_bytes, headers={
    'Content-Type': 'application/json',
    'X-Webhook-Signature': signature,
}, method='POST')

try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        print(f'Status: {resp.status}')
        print(f'Response: {json.dumps(json.loads(resp.read().decode()), indent=2)}')
except urllib.error.HTTPError as exc:
    print(f'Status: {exc.code}')
    print(f'Response: {exc.read().decode()}')
"
```

### Expected

```json
{
  "status": "ok",
  "product_number": "CAS2023NEWCAS0000",
  "signal": "extraction_ready",
  "updated_fields": ["field_signal", "body", "field_description", ...]
}
```

---

## Test 6: DLQ Error Handling

### Trigger a failure to test DLQ flow

```bash
# Invoke with a non-existent OU (no .meta.json will be found)
aws lambda invoke \
    --function-name ieee-rc-ai-orchestrator \
    --payload '{"bucket":"dev-ieee-conference-cloud-bulk-uploads","key":"FAKE/pending/NO_META.pdf"}' \
    --cli-read-timeout 60 \
    /tmp/dlq-test.json && python3 -m json.tool /tmp/dlq-test.json
```

### Expected Response

```json
{
  "statusCode": 500,
  "body": {
    "error": "Failed to read s3://dev-ieee-conference-cloud-bulk-uploads/FAKE/metadata/NO_META.meta.json after 3 retries"
  }
}
```

### Verify DLQ Processing

```bash
# Check DLQ processor logs (should show "Archiving permanently failed message")
aws logs tail /aws/lambda/ieee-rc-dlq-processor --since 5m --format short

# Check archived failure in S3
aws s3 ls s3://dev-ieee-conference-cloud-bulk-uploads/failed/ --recursive
```

### DLQ Flow

```
Orchestrator error → SQS (ieee-rc-processing-dlq) → DLQ Processor Lambda
  ├── is_retriable=true + retry_count < 2 → Re-invoke orchestrator
  └── is_retriable=false OR retry_count >= 2 → Archive to S3 (failed/) + SNS alert
```

---

## Test 7: DLQ Processor (Direct Invoke)

```bash
./scripts/invoke-dlq-processor.sh
```

### Expected

- DLQ processor reads the sample message
- Archives to `failed/{correlation_id}/{timestamp}.json`
- Check with: `aws s3 ls s3://dev-ieee-conference-cloud-bulk-uploads/failed/ --recursive`

---

## Troubleshooting

### Common Errors

| Error | Cause | Fix |
|-------|-------|-----|
| `NoSuchKey` | File not in `/pending/` | Copy from `/processed/` or upload to `/pending/` |
| `AccessDenied: s3:ListBucket` | IAM role missing ListBucket | Re-run `./scripts/deploy-*.sh` to update role |
| `Meta file not found` | No `.meta.json` for the file | Create `.meta.json` in `{ou}/metadata/{item_id}.meta.json` |
| `Missing required .meta.json fields` | Incomplete `.meta.json` | Ensure all required fields present (see table above) |
| `Unsupported media type` | Invalid `content.media_type` | Use: `application/pdf`, `video/mp4`, `video/quicktime`, `video/webm` |
| `ResourceNotFoundException (Bedrock)` | Model access expired | Renew Anthropic use case in AWS Bedrock console |
| `Webhook HTTP 422` | Missing `signal` or `product_part_number` | Ensure orchestrator is up-to-date (`development` branch) |
| `Webhook HTTP 404` | Product not in Drupal | Use a valid `product_part_number` that exists in Drupal |

### Viewing CloudWatch Logs

```bash
# Orchestrator logs
aws logs tail /aws/lambda/ieee-rc-ai-orchestrator --since 10m --format short

# PDF extractor logs
aws logs tail /aws/lambda/ieee-cc-pdf-extractor --since 10m --format short

# Video transcriber logs
aws logs tail /aws/lambda/ieee-cc-video-transcriber --since 10m --format short

# Bedrock inference logs
aws logs tail /aws/lambda/ieee-cc-bedrock-inference --since 10m --format short

# DLQ processor logs
aws logs tail /aws/lambda/ieee-rc-dlq-processor --since 10m --format short
```

### Checking SQS Queue

```bash
# Messages waiting in DLQ
aws sqs get-queue-attributes \
    --queue-url https://queue.amazonaws.com/141770997341/ieee-rc-processing-dlq \
    --attribute-names ApproximateNumberOfMessages
```

---

## Test Data Available

| File | Location | Size | Type |
|------|----------|------|------|
| `PES_TR_138_SBLCS_011726.pdf` | `PES/processed/` | 3.1 MB | PDF |
| `PES_TR_TR139_ITSLC_012826.pdf` | `PES/processed/` | 1.6 MB | PDF |
| `AESSBMR0000.mp4` | `AESS/pending/` | 55 MB | Video |
| `AESSBMR0040.mp4` | `AESS/pending/` | 41 MB | Video |

**Drupal test product:** `CAS2023NEWCAS0000` on `pr-646-cc-ieee.pantheonsite.io`
**Webhook secret:** `webhook-test-secret`

---

## Verified Test Results (2026-03-25)

| Test | Result | Time |
|------|--------|------|
| PDF Extraction (standalone) | PASSED (200 OK) | ~2s |
| Full Pipeline PDF (orchestrator) | PASSED — extraction + Bedrock + webhook 200 + move | 17.7s |
| Webhook HMAC to Drupal | PASSED — 7 fields updated on CAS2023NEWCAS0000 | 1.5s |
| DLQ Error Flow | PASSED — error archived to S3 `failed/` | 0.2s |
| AI Disabled (move only) | PASSED | ~0.3s |
