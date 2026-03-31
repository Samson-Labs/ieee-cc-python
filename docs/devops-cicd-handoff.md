# DevOps CI/CD Handoff — IEEE Content Conversion Pipeline

## Architecture Overview

```
                                S3 Bucket
                    dev-ieee-conference-cloud-bulk-uploads
                                   |
                    {ou}/pending/{item_id}.{ext}  (upload trigger)
                    {ou}/metadata/{item_id}.meta.json  (routing config)
                                   |
                                   v
                    +------------------------------+
                    |   AI Orchestrator Lambda      |
                    |   (ieee-rc-ai-orchestrator)   |
                    |   - Reads .meta.json          |
                    |   - Routes by media_type      |
                    +------------------------------+
                           /        |         \
                          v         v          v
            +-----------+  +-----------+  +-------------+
            |  PDF      |  |  Video    |  |  Bedrock    |
            |  Extractor|  |Transcriber|  |  Inference  |
            +-----------+  +-----------+  +-------------+
            ieee-cc-pdf-   ieee-cc-video-  ieee-cc-bedrock-
            extractor      transcriber     inference
                          \        |         /
                           v       v        v
                    +------------------------------+
                    |   Webhook to Drupal           |
                    |   HMAC-SHA256 signed POST     |
                    |   X-Webhook-Signature header  |
                    +------------------------------+
                                   |
                    File moved: /pending/ -> /processed/
                                   |
                    On Failure:    v
                    +------------------------------+
                    |   SQS Dead Letter Queue       |
                    |   ieee-rc-processing-dlq      |
                    +------------------------------+
                                   |
                                   v
                    +------------------------------+
                    |   DLQ Processor Lambda        |
                    |   ieee-rc-dlq-processor       |
                    |   - Retry if is_retriable     |
                    |   - Archive to S3 failed/     |
                    +------------------------------+

    Standalone:
    +------------------------------+     +------------------------------+
    |   Image Overlay Generator    |     |   Bulk Processor + Worker    |
    |   ieee-rc-image-generator    |     |   (not yet deployed)         |
    |   S3 trigger: actions/*.json |     |   Manifest -> SQS -> Worker  |
    +------------------------------+     +------------------------------+
```

## AWS Account & Region

| Setting | Value |
|---------|-------|
| Account ID | `141770997341` |
| Region | `us-east-1` |
| AWS Profile | `ieee-cc` |

## Lambda Functions

### Build & Deploy Matrix

| Lambda | ECR Repo | Dockerfile | Base Image | Memory | Timeout | Handler |
|--------|----------|------------|------------|--------|---------|---------|
| `ieee-cc-pdf-extractor` | `ieee-cc-pdf-extractor` | `src/extractors/Dockerfile` | `python:3.13` | 3008 MB | 300s | `src.handlers.pdf_handler.handler` |
| `ieee-cc-video-transcriber` | `ieee-cc-video-transcriber` | `src/extractors/Dockerfile` | `python:3.13` | 512 MB | 900s | `src.handlers.video_transcriber_handler.handler` |
| `ieee-cc-bedrock-inference` | `ieee-cc-bedrock-inference` | `src/ai/Dockerfile` | `python:3.13` | 512 MB | 120s | `src.handlers.bedrock_handler.handler` |
| `ieee-rc-ai-orchestrator` | `ieee-rc-ai-orchestrator` | `src/orchestrator/AIOrchestratorDockerfile` | `python:3.12` | 512 MB | 300s | `src.handlers.ai_orchestrator_handler.handler` |
| `ieee-rc-image-generator` | `ieee-rc-image-generator` | `src/generators/Dockerfile` | `python:3.12` | 1024 MB | 60s | `src.handlers.image_overlay_handler.handler` |
| `ieee-rc-dlq-processor` | `ieee-rc-dlq-processor` | `src/dlq/Dockerfile` | `python:3.13` | 256 MB | 60s | `src.handlers.dlq_handler.handler` |
| `ieee-rc-bulk-processor` | `ieee-rc-bulk-processor` | `src/bulk/Dockerfile.processor` | `python:3.13` | 256 MB | 60s | `src.handlers.bulk_processor_handler.handler` |
| `ieee-rc-bulk-worker` | `ieee-rc-bulk-worker` | `src/bulk/Dockerfile.worker` | `python:3.13` | 512 MB | 300s | `src.handlers.bulk_worker_handler.handler` |

### Docker Build Command (all Lambdas)

All Dockerfiles use the **repo root** as the build context:

```bash
docker buildx build --platform linux/amd64 --provenance=false \
    --output type=docker \
    -f <DOCKERFILE_PATH> \
    -t <ECR_REPO>:latest \
    <REPO_ROOT>
```

`--platform linux/amd64` is required for Lambda compatibility (builds run on Apple Silicon Macs).
`--provenance=false` is required to avoid multi-platform manifest issues with ECR.

### ECR Push

```bash
# Login
aws ecr get-login-password --region us-east-1 | \
    docker login --username AWS --password-stdin 141770997341.dkr.ecr.us-east-1.amazonaws.com

# Tag & Push
docker tag <ECR_REPO>:latest 141770997341.dkr.ecr.us-east-1.amazonaws.com/<ECR_REPO>:latest
docker push 141770997341.dkr.ecr.us-east-1.amazonaws.com/<ECR_REPO>:latest

# Update Lambda to use new image
aws lambda update-function-code \
    --function-name <LAMBDA_NAME> \
    --image-uri 141770997341.dkr.ecr.us-east-1.amazonaws.com/<ECR_REPO>:latest

aws lambda wait function-updated-v2 --function-name <LAMBDA_NAME>
```

## Environment Variables

### ieee-rc-ai-orchestrator

| Variable | Value | Description |
|----------|-------|-------------|
| `PDF_EXTRACTOR_FUNCTION` | `ieee-cc-pdf-extractor` | PDF extraction Lambda |
| `VIDEO_TRANSCRIBER_FUNCTION` | `ieee-cc-video-transcriber` | Video transcription Lambda |
| `BEDROCK_FUNCTION` | `ieee-cc-bedrock-inference` | Bedrock metadata Lambda |
| `DRUPAL_WEBHOOK_SECRET` | `webhook-test-secret` | HMAC-SHA256 signing secret (change per env) |
| `DLQ_QUEUE_URL` | `https://queue.amazonaws.com/141770997341/ieee-rc-processing-dlq` | SQS DLQ URL |
| `LOG_LEVEL` | `INFO` | Log verbosity |

### ieee-cc-video-transcriber

| Variable | Value | Description |
|----------|-------|-------------|
| `CLEANUP_MODEL_ID` | `us.anthropic.claude-3-5-haiku-20241022-v1:0` | Haiku model for transcript cleanup |
| `LOG_LEVEL` | `INFO` | |

### ieee-cc-bedrock-inference

| Variable | Value | Description |
|----------|-------|-------------|
| `BEDROCK_MODEL_ID` | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | Claude Sonnet model for metadata |
| `LOG_LEVEL` | `INFO` | |

### ieee-rc-dlq-processor

| Variable | Value | Description |
|----------|-------|-------------|
| `ORCHESTRATOR_FUNCTION_NAME` | `ieee-rc-ai-orchestrator` | For retry invocations |
| `ARCHIVE_BUCKET` | `dev-ieee-conference-cloud-bulk-uploads` | S3 bucket for failed/ archives |
| `FAILURES_SNS_TOPIC_ARN` | *(not set yet)* | SNS topic for failure alerts |
| `LOG_LEVEL` | `INFO` | |

### ieee-cc-pdf-extractor

| Variable | Value | Description |
|----------|-------|-------------|
| `LOG_LEVEL` | `INFO` | |

### ieee-rc-image-generator

| Variable | Value | Description |
|----------|-------|-------------|
| `LOG_LEVEL` | `INFO` | |

## IAM Roles

Each Lambda has a dedicated IAM role: `<lambda-name>-role`

### Permission Summary

| Role | Permissions |
|------|------------|
| `ieee-cc-pdf-extractor-role` | S3 GetObject/PutObject/ListBucket on pipeline bucket + CloudWatch |
| `ieee-cc-video-transcriber-role` | S3 GetObject/PutObject/ListBucket + Transcribe full access + Bedrock InvokeModel + CloudWatch |
| `ieee-cc-bedrock-inference-role` | S3 GetObject/ListBucket on pipeline bucket + Bedrock InvokeModel (regional) + CloudWatch |
| `ieee-rc-ai-orchestrator-role` | S3 GetObject/PutObject/DeleteObject/ListBucket + Lambda InvokeFunction (3 downstream) + SQS SendMessage (DLQ) + CloudWatch |
| `ieee-rc-image-generator-role` | S3 GetObject/PutObject/DeleteObject/ListBucket + CloudWatch |
| `ieee-rc-dlq-processor-role` | Lambda InvokeFunction (orchestrator) + S3 PutObject (failed/) + S3 ListBucket + SNS Publish + SQS Receive/Delete/GetQueueAttributes + CloudWatch |

All roles also have `AWSLambdaBasicExecutionRole` managed policy attached.

## S3 Bucket

**Bucket:** `dev-ieee-conference-cloud-bulk-uploads` (versioned, public access blocked)

### Path Conventions

```
{ou}/pending/{item_id}.{ext}              # Input files
{ou}/metadata/{item_id}.meta.json          # Orchestrator routing config
{ou}/metadata/{ppn}.pdf.json               # PDF extraction metadata output
{ou}/metadata/{ppn}.{format}.json          # Video duration metadata output
{ou}/processed/{item_id}.{ext}             # Processed files (moved from pending)
actions/{job_id}.json                      # Image overlay trigger
backgrounds/{ou_short_name}.jpg            # Image overlay backgrounds
failed/{correlation_id}/{timestamp}.json   # DLQ archived failures
bulk/manifests/{batch_id}.json             # Bulk processing manifests
bulk/progress/{batch_id}_progress.json     # Bulk processing progress
```

## SQS Queues

| Queue | Config | Trigger |
|-------|--------|---------|
| `ieee-rc-processing-dlq` | 14-day retention, 120s visibility | -> `ieee-rc-dlq-processor` (batch size 1) |

## S3 Event Triggers

| Prefix Filter | Lambda Target |
|---------------|---------------|
| `actions/*.json` | `ieee-rc-image-generator` |

## CI/CD Pipeline Steps

### Recommended Pipeline

```
1. Code Push / PR Merge
        |
2. Run Tests
        |  python -m pytest tests/ -v
        |  (353 tests, ~2.5s)
        |
3. Build Docker Images (parallel)
        |  One image per changed Lambda
        |  --platform linux/amd64 --provenance=false
        |
4. Push to ECR
        |  Tag: latest + git SHA for rollback
        |
5. Update Lambda Function Code
        |  aws lambda update-function-code --image-uri ...
        |  aws lambda wait function-updated-v2 ...
        |
6. Smoke Test (optional)
        |  Invoke orchestrator with test payload
        |
7. Done
```

### Change Detection (which Lambdas to rebuild)

| If files changed in... | Rebuild |
|------------------------|---------|
| `src/extractors/` or `src/handlers/pdf_handler.py` | `ieee-cc-pdf-extractor` |
| `src/extractors/` or `src/handlers/video_transcriber_handler.py` | `ieee-cc-video-transcriber` |
| `src/ai/` or `src/handlers/bedrock_handler.py` | `ieee-cc-bedrock-inference` |
| `src/orchestrator/` or `src/handlers/ai_orchestrator_handler.py` or `src/webhook/` or `src/common/` | `ieee-rc-ai-orchestrator` |
| `src/generators/` or `src/handlers/image_overlay_handler.py` | `ieee-rc-image-generator` |
| `src/dlq/` or `src/handlers/dlq_handler.py` or `src/common/` | `ieee-rc-dlq-processor` |
| `src/bulk/` or `src/handlers/bulk_processor_handler.py` | `ieee-rc-bulk-processor` |
| `src/bulk/` or `src/handlers/bulk_worker_handler.py` | `ieee-rc-bulk-worker` |
| `src/common/` | All Lambdas that include `src/common/` in their Dockerfile |

**Note:** `src/common/` is a shared dependency included in orchestrator, DLQ processor, and bulk Lambdas. Changes to `src/common/` require rebuilding those images.

### Existing Deploy Scripts (reference)

Each Lambda has manual deploy scripts that can serve as CI/CD reference:

```
scripts/deploy.sh                      # PDF extractor
scripts/deploy-video-transcriber.sh    # Video transcriber
scripts/deploy-bedrock.sh              # Bedrock inference
scripts/deploy-ai-orchestrator.sh      # AI orchestrator
scripts/deploy-image-overlay.sh        # Image overlay
scripts/deploy-dlq-processor.sh        # DLQ processor
scripts/deploy-bulk-processor.sh       # Bulk processor
scripts/deploy-bulk-worker.sh          # Bulk worker
```

Each script supports: `./scripts/deploy-*.sh` (full deploy) or `./scripts/deploy-*.sh update` (rebuild + code update only).

## Repository Structure

```
ieee-cc-python/
├── src/
│   ├── __init__.py
│   ├── common/              # Shared: exceptions, retry, error_handler, logging, dlq
│   ├── extractors/          # PDF extractor + Video transcriber + Dockerfile
│   ├── generators/          # Image overlay generator + Dockerfile
│   ├── ai/                  # Bedrock inference + Dockerfile
│   ├── orchestrator/        # AI orchestrator + Dockerfile
│   ├── webhook/             # HMAC-SHA256 webhook sender
│   ├── dlq/                 # DLQ processor + Dockerfile
│   ├── bulk/                # Bulk processor + worker + Dockerfiles
│   └── handlers/            # Lambda entry points (one per Lambda)
├── tests/                   # Mirrors src/ structure
├── scripts/                 # Deploy/invoke/teardown per Lambda
├── docs/                    # QA guides, architecture docs
├── requirements.txt         # Shared dev dependencies
├── requirements-dev.txt     # Test dependencies (pytest, etc.)
└── CLAUDE.md                # Project conventions
```

## Secrets & Sensitive Config

| Secret | Current Value | Where Used | Notes |
|--------|--------------|------------|-------|
| `DRUPAL_WEBHOOK_SECRET` | `webhook-test-secret` | Orchestrator env var | Must match Drupal-side config. Change per environment. |
| `BEDROCK_MODEL_ID` | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | Bedrock env var | Requires Bedrock model access in AWS account |
| `CLEANUP_MODEL_ID` | `us.anthropic.claude-3-5-haiku-20241022-v1:0` | Video transcriber env var | Requires Bedrock model access |

### Environment-Specific Config

For multi-environment (dev/staging/prod), these values need to change:

- `DRUPAL_WEBHOOK_SECRET` — different per Drupal environment
- `DLQ_QUEUE_URL` — different SQS queue per environment
- `ARCHIVE_BUCKET` — different S3 bucket per environment
- `FAILURES_SNS_TOPIC_ARN` — different SNS topic per environment
- Lambda function name references (orchestrator → downstream Lambdas)

## Testing

```bash
# Install dependencies
pip install -r requirements.txt -r requirements-dev.txt

# Run all tests (353 tests)
python -m pytest tests/ -v

# Run tests for a specific module
python -m pytest tests/extractors/ -v
python -m pytest tests/ai/ -v
python -m pytest tests/orchestrator/ -v
python -m pytest tests/webhook/ -v
python -m pytest tests/dlq/ -v
python -m pytest tests/bulk/ -v
python -m pytest tests/common/ -v
```

## Rollback

```bash
# List recent ECR image digests
aws ecr describe-images --repository-name <ECR_REPO> --query 'imageDetails | sort_by(@, &imagePushedAt) | [-5:].[imagePushedAt, imageDigest]' --output table

# Rollback to a specific image digest
aws lambda update-function-code \
    --function-name <LAMBDA_NAME> \
    --image-uri 141770997341.dkr.ecr.us-east-1.amazonaws.com/<ECR_REPO>@sha256:<DIGEST>
```

## Not Yet Deployed

| Lambda | ECR Repo | Status |
|--------|----------|--------|
| `ieee-rc-bulk-processor` | `ieee-rc-bulk-processor` | Code ready, not deployed to AWS |
| `ieee-rc-bulk-worker` | `ieee-rc-bulk-worker` | Code ready, not deployed to AWS |

These require a new SQS queue for the bulk fan-out and SNS topic for completion notifications.
