# Deployment Guide

## Prerequisites

- AWS CLI configured with profile `ieee-cc`
- Docker running locally
- Account: `141770997341`, Region: `us-east-1`

---

## PDF Text Extractor

### First-Time Deploy

```bash
./scripts/deploy.sh
```

Creates all resources in order:
1. **ECR repository** (`ieee-cc-pdf-extractor`) — container image registry
2. **S3 bucket** (`dev-ieee-conference-cloud-bulk-uploads`) — versioned, public access blocked
3. **IAM role** (`ieee-cc-pdf-extractor-role`) — Lambda execution + S3 read/write
4. **Docker build + push** — builds from `src/extractors/Dockerfile`, pushes to ECR
5. **Lambda function** (`ieee-cc-pdf-extractor`) — 3 GB memory, 5 min timeout, x86_64

### Update Code Only

```bash
./scripts/deploy.sh update
```

### Invoke

```bash
./scripts/invoke.sh <bucket> <key> <ou> <product_part_number>

# Example:
./scripts/invoke.sh dev-ieee-conference-cloud-bulk-uploads \
    PES/pending/STD-12345.pdf PES STD-12345
```

### Tear Down

```bash
./scripts/teardown.sh
```

### Lambda Configuration

| Setting | Value | Rationale |
|---------|-------|-----------|
| Memory | 3008 MB | PyMuPDF loads full pages into memory; 100+ page PDFs need headroom |
| Timeout | 300s | Large PDF download + extraction can be slow |
| Architecture | x86_64 | PyMuPDF wheel compatibility |
| Base image | `public.ecr.aws/lambda/python:3.13` | AWS-provided Lambda runtime with Python 3.13 |

### IAM Permissions

- `AWSLambdaBasicExecutionRole` (managed policy) — CloudWatch Logs
- Inline `S3ReadWriteAccess` — `s3:GetObject` and `s3:PutObject` on the pipeline bucket

### Invocation Patterns

**1. Direct (orchestrator):**
```json
{
  "bucket": "dev-ieee-conference-cloud-bulk-uploads",
  "key": "PES/pending/STD-12345.pdf",
  "ou": "PES",
  "product_part_number": "STD-12345"
}
```

**2. S3 event trigger:**
```json
{
  "Records": [{
    "s3": {
      "bucket": {"name": "dev-ieee-conference-cloud-bulk-uploads"},
      "object": {"key": "PES/pending/STD-12345.pdf"}
    }
  }]
}
```

The S3 event format derives `ou` and `product_part_number` from the key pattern `{ou}/pending/{filename}.pdf`.

---

## Image Overlay Generator

### First-Time Deploy

```bash
./scripts/deploy-image-overlay.sh
```

Creates all resources in order:
1. **ECR repository** (`ieee-rc-image-generator`)
2. **IAM role** (`ieee-rc-image-generator-role`) — S3 read/write/delete + CloudWatch
3. **Docker build + push** — builds from `src/generators/Dockerfile`
4. **Lambda function** (`ieee-rc-image-generator`) — 1024 MB, 60s timeout
5. **S3 event notification** — `actions/*.json` triggers the Lambda

### Update Code Only

```bash
./scripts/deploy-image-overlay.sh update
```

### Invoke

```bash
./scripts/invoke-image-overlay.sh <bucket> <key>

# Example:
./scripts/invoke-image-overlay.sh dev-ieee-conference-cloud-bulk-uploads actions/job-001.json
```

### Tear Down

```bash
./scripts/teardown-image-overlay.sh
```

### Lambda Configuration

| Setting | Value | Rationale |
|---------|-------|-----------|
| Memory | 1024 MB | Pillow image processing with overlay rendering |
| Timeout | 60s | Background download + image generation + upload |
| Architecture | x86_64 | Pillow wheel compatibility |
| Base image | `public.ecr.aws/lambda/python:3.12` | AWS-provided Lambda runtime with Python 3.12 |

### IAM Permissions

- `AWSLambdaBasicExecutionRole` (managed policy) — CloudWatch Logs
- Inline `S3Access` — `s3:GetObject` (any bucket), `s3:PutObject` (any bucket), `s3:DeleteObject` (trigger bucket `actions/*`)

### Invocation Patterns

**1. Direct (orchestrator):**
```json
{
  "bucket": "dev-ieee-conference-cloud-bulk-uploads",
  "key": "actions/job-001.json"
}
```

**2. S3 event trigger (automatic):**

Triggered by any `s3:ObjectCreated:*` event matching `actions/*.json` in the configured bucket.

---

## Bedrock Metadata Generator

### First-Time Deploy

```bash
./scripts/deploy-bedrock.sh
```

Creates all resources in order:
1. **ECR repository** (`ieee-cc-bedrock-inference`)
2. **IAM role** (`ieee-cc-bedrock-inference-role`) — S3 read + Bedrock invoke + CloudWatch
3. **Docker build + push** — builds from `src/ai/Dockerfile`
4. **Lambda function** (`ieee-cc-bedrock-inference`) — 512 MB, 120s timeout

### Update Code Only

```bash
./scripts/deploy-bedrock.sh update
```

### Invoke

```bash
# From S3 metadata JSON
./scripts/invoke-bedrock.sh <bucket> <key>
./scripts/invoke-bedrock.sh dev-ieee-conference-cloud-bulk-uploads PES/metadata/doc.pdf.json

# Direct text
./scripts/invoke-bedrock.sh --text "Extracted document text..."
```

### Tear Down

```bash
./scripts/teardown-bedrock.sh
```

### Lambda Configuration

| Setting | Value | Rationale |
|---------|-------|-----------|
| Memory | 512 MB | JSON processing only, no heavy libraries |
| Timeout | 120s | Bedrock inference can take 30–60s; retries need headroom |
| Architecture | x86_64 | Consistency with other Lambdas |
| Base image | `public.ecr.aws/lambda/python:3.13` | AWS-provided Lambda runtime with Python 3.13 |
| `BEDROCK_MODEL_ID` | `anthropic.claude-sonnet-4-5-20250929-v1:0` | Configurable via env var |

### IAM Permissions

- `AWSLambdaBasicExecutionRole` (managed policy) — CloudWatch Logs
- Inline `S3AndBedrockAccess`:
  - `s3:GetObject` — read metadata JSON from S3
  - `bedrock:InvokeModel` — call Bedrock foundation models

### Invocation Patterns

**1. Direct text (orchestrator):**
```json
{
  "text": "Extracted document text...",
  "thesaurus_terms": ["smart grid", "power systems"]
}
```

**2. S3 metadata reference:**
```json
{
  "bucket": "dev-ieee-conference-cloud-bulk-uploads",
  "key": "PES/metadata/doc.pdf.json"
}
```

The S3 JSON file must contain an `extractedText` field.

### Retry Logic

| Scenario | Strategy |
|----------|----------|
| Bedrock throttling (429) | Exponential backoff: 1s, 2s, 4s (max 3 attempts) |
| Invalid JSON response | Retry once with explicit JSON instruction appended |

---

## Common Notes

### Test PDFs

Sample PDFs provided by PM (production bucket):
- `s3://ieee-conference-cloud-bulk-uploads/PES/PES_PUB_TP_TP101_101995.pdf`
- `s3://ieee-conference-cloud-bulk-uploads/PES/PES_MAG_ELE_13-4.pdf`

Upload a test PDF to the dev bucket:
```bash
aws s3 cp test.pdf s3://dev-ieee-conference-cloud-bulk-uploads/PES/pending/STD-12345.pdf \
    --profile ieee-cc
```

### Apple Silicon Note

All deploy scripts use `docker buildx build --platform linux/amd64 --provenance=false` to produce single-arch images compatible with Lambda. Without `--provenance=false`, Docker Desktop on Apple Silicon creates a manifest list that Lambda rejects.

### Tear Down Behavior

All teardown scripts delete Lambda, IAM role (+ policies), and ECR repository. **S3 bucket is always preserved** (data retention).

### AWS Resources Summary

| Resource | Name | Config |
|----------|------|--------|
| S3 Bucket | `dev-ieee-conference-cloud-bulk-uploads` | Shared across Lambdas, versioned |
| ECR | `ieee-cc-pdf-extractor` | PDF extractor image |
| ECR | `ieee-rc-image-generator` | Image overlay image |
| ECR | `ieee-cc-bedrock-inference` | Bedrock metadata image |
| Lambda | `ieee-cc-pdf-extractor` | 3 GB, 5 min timeout, Python 3.13 |
| Lambda | `ieee-rc-image-generator` | 1024 MB, 60s timeout, Python 3.12 |
| Lambda | `ieee-cc-bedrock-inference` | 512 MB, 120s timeout, Python 3.13 |
| IAM Role | `ieee-cc-pdf-extractor-role` | S3 read/write + CloudWatch |
| IAM Role | `ieee-rc-image-generator-role` | S3 read/write/delete + CloudWatch |
| IAM Role | `ieee-cc-bedrock-inference-role` | S3 read + Bedrock invoke + CloudWatch |
| S3 Trigger | `actions/*.json` | -> `ieee-rc-image-generator` |
