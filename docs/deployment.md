# Deployment Guide

## Prerequisites

- AWS CLI configured with profile `ieee-cc`
- Docker running locally
- Account: `141770997341`, Region: `us-east-1`

## First-Time Deploy

```bash
./scripts/deploy.sh
```

This creates all resources in order:
1. **ECR repository** (`ieee-cc-pdf-extractor`) — container image registry
2. **S3 bucket** (`dev-ieee-conference-cloud-bulk-uploads`) — versioned, public access blocked
3. **IAM role** (`ieee-cc-pdf-extractor-role`) — Lambda execution + S3 read/write
4. **Docker build + push** — builds from `Dockerfile`, pushes to ECR
5. **Lambda function** (`ieee-cc-pdf-extractor`) — 3 GB memory, 5 min timeout, x86_64

All steps are idempotent — safe to re-run.

## Update Code Only

After making code changes, rebuild the image and update Lambda without recreating infrastructure:

```bash
./scripts/deploy.sh update
```

## Invoke Lambda

```bash
./scripts/invoke.sh <bucket> <key> <ou> <product_part_number>

# Example:
./scripts/invoke.sh dev-ieee-conference-cloud-bulk-uploads \
    ieee/pending/STD-12345.pdf ieee STD-12345
```

## Test PDFs

Sample PDFs provided by PM (production bucket):
- `s3://ieee-conference-cloud-bulk-uploads/PES/PES_PUB_TP_TP101_101995.pdf`
- `s3://ieee-conference-cloud-bulk-uploads/PES/PES_MAG_ELE_13-4.pdf`

Upload a test PDF to the dev bucket:
```bash
aws s3 cp test.pdf s3://dev-ieee-conference-cloud-bulk-uploads/PES/pending/STD-12345.pdf \
    --profile ieee-cc
```

## Tear Down

Deletes Lambda, IAM role, and ECR repository. **S3 bucket is preserved** (data retention).

```bash
./scripts/teardown.sh
```

## Apple Silicon Note

The deploy script uses `docker buildx build --platform linux/amd64 --provenance=false` to produce a single-arch image compatible with Lambda. Without `--provenance=false`, Docker Desktop on Apple Silicon creates a manifest list that Lambda rejects.

## Lambda Configuration

| Setting | Value | Rationale |
|---------|-------|-----------|
| Memory | 3008 MB | PyMuPDF loads full pages into memory; 100+ page PDFs need headroom |
| Timeout | 300s | Large PDF download + extraction can be slow |
| Architecture | x86_64 | PyMuPDF wheel compatibility |
| Base image | `public.ecr.aws/lambda/python:3.13` | AWS-provided Lambda runtime with Python 3.13 |

## IAM Permissions

The Lambda role has:
- `AWSLambdaBasicExecutionRole` (managed policy) — CloudWatch Logs
- Inline `S3ReadWriteAccess` — `s3:GetObject` and `s3:PutObject` on the pipeline bucket

## Invocation Patterns

The handler supports two event formats:

**1. Direct (orchestrator):**
```json
{
  "bucket": "dev-ieee-conference-cloud-bulk-uploads",
  "key": "ieee/pending/STD-12345.pdf",
  "ou": "ieee",
  "product_part_number": "STD-12345"
}
```

**2. S3 event trigger:**
```json
{
  "Records": [{
    "s3": {
      "bucket": {"name": "dev-ieee-conference-cloud-bulk-uploads"},
      "object": {"key": "ieee/pending/STD-12345.pdf"}
    }
  }]
}
```

The S3 event format derives `ou` and `product_part_number` from the key pattern `{ou}/pending/{filename}.pdf`.
