# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

IEEE Content Conversion (CC) pipeline — Python Lambda modules for PDF text extraction, video transcription, image overlay generation, and AI-powered metadata generation via AWS Bedrock. Modules are designed as reusable classes called by an orchestrator Lambda.

## Commands

```bash
# Run all tests
python -m pytest tests/ -v

# Run a single test file
python -m pytest tests/extractors/test_pdf_extractor.py -v
python -m pytest tests/generators/test_image_overlay_generator.py -v
python -m pytest tests/ai/test_bedrock_inference.py -v
python -m pytest tests/extractors/test_video_transcriber.py -v
python -m pytest tests/webhook/test_sender.py -v
python -m pytest tests/common/ -v
python -m pytest tests/dlq/test_dlq_processor.py -v
python -m pytest tests/bulk/ -v

# Run a single test class or method
python -m pytest tests/extractors/test_pdf_extractor.py::TestNormalPDF::test_extracts_text -v

# Install dependencies
pip install -r requirements.txt -r requirements-dev.txt

# PDF Extractor Lambda
./scripts/deploy.sh                    # first-time full deploy
./scripts/deploy.sh update             # rebuild + update code only
./scripts/invoke.sh <bucket> <key> <ou> <product_part_number>
./scripts/teardown.sh

# Image Overlay Generator Lambda
./scripts/deploy-image-overlay.sh          # first-time full deploy
./scripts/deploy-image-overlay.sh update   # rebuild + update code only
./scripts/invoke-image-overlay.sh <bucket> <key>
./scripts/teardown-image-overlay.sh

# Bedrock Metadata Generation Lambda
./scripts/deploy-bedrock.sh                # first-time full deploy
./scripts/deploy-bedrock.sh update         # rebuild + update code only
./scripts/invoke-bedrock.sh <bucket> <key> # S3 metadata reference
./scripts/invoke-bedrock.sh --text "text"  # direct text invocation
./scripts/teardown-bedrock.sh

# Video Transcriber Lambda
./scripts/deploy-video-transcriber.sh          # first-time full deploy
./scripts/deploy-video-transcriber.sh update   # rebuild + update code only
./scripts/invoke-video-transcriber.sh <bucket> <key> <ou> <product_part_number>
./scripts/teardown-video-transcriber.sh

# AI Orchestrator Lambda
./scripts/deploy-ai-orchestrator.sh            # first-time full deploy
./scripts/deploy-ai-orchestrator.sh update     # rebuild + update code only
./scripts/invoke-ai-orchestrator.sh <bucket> <key>
./scripts/teardown-ai-orchestrator.sh

# DLQ Processor Lambda
./scripts/deploy-dlq-processor.sh              # first-time full deploy
./scripts/deploy-dlq-processor.sh update       # rebuild + update code only
./scripts/invoke-dlq-processor.sh              # test with sample DLQ event
./scripts/teardown-dlq-processor.sh

# Bulk Processor Lambda (manifest dispatcher)
./scripts/deploy-bulk-processor.sh             # first-time full deploy
./scripts/deploy-bulk-processor.sh update      # rebuild + update code only
./scripts/invoke-bulk-processor.sh <batch_id>  # invoke with batch ID
./scripts/teardown-bulk-processor.sh

# Bulk Worker Lambda (SQS-triggered, per-item)
./scripts/deploy-bulk-worker.sh                # first-time full deploy
./scripts/deploy-bulk-worker.sh update         # rebuild + update code only
./scripts/invoke-bulk-worker.sh                # test with sample bulk item
./scripts/teardown-bulk-worker.sh
```

## Architecture

- **`src/extractors/`** — Reusable extraction modules (one per file type). Each extractor class takes an S3 client, downloads the file, extracts content, writes metadata JSON back to S3, and returns a structured result dict. Contains its own `Dockerfile`. Includes `VideoTranscriber` which uses AWS Transcribe for video-to-text with speaker diarization and optional Claude Haiku transcript cleanup.
- **`src/generators/`** — Reusable generation modules. Each generator class takes an S3 client, reads trigger JSON, processes assets, writes output to S3, and returns a structured result dict. Contains its own `Dockerfile` and `requirements.txt`.
- **`src/ai/`** — AI inference modules. `BedrockInference` calls AWS Bedrock (Claude Sonnet) with the IEEE system prompt to generate structured metadata from document text. Includes retry logic for throttling and invalid JSON. Contains its own `Dockerfile` and `requirements.txt`.
- **`src/common/`** — Shared infrastructure modules. `exceptions.py` defines a `PipelineError` hierarchy with domain-specific errors (`TranscribeError`, `BedrockError`, `WebhookError`, `S3Error`, `ValidationError`, `BulkProcessingError`). `retry.py` provides a `@with_retry` decorator with exponential/fixed backoff. `error_handler.py` builds structured error responses with correlation IDs and stack traces. `logging.py` provides a JSON structured logger for CloudWatch. `dlq.py` builds DLQ message payloads.
- **`src/webhook/`** — Webhook delivery module. `WebhookSender` signs payloads with HMAC-SHA256, retries with exponential backoff on 5xx/connection errors, and publishes to an SNS dead-letter topic after exhausting retries.
- **`src/dlq/`** — DLQ processor module. `DLQProcessor` reads failed events from SQS, re-invokes the orchestrator for retriable errors (up to 2 reprocess attempts), and archives permanently failed messages to S3 with SNS alerting. Contains its own `Dockerfile` and `requirements.txt`.
- **`src/bulk/`** — Bulk processing modules for existing catalog re-tagging. `BulkProcessor` reads a batch manifest from S3 and fans out items to an SQS queue with configurable delay. `BulkWorker` processes individual items from SQS by copying files to `/pending/`, creating `.meta.json`, and invoking the orchestrator Lambda. Tracks progress in S3 and sends SNS on batch completion. Contains its own Dockerfiles and `requirements.txt`.
- **`src/orchestrator/`** — AI Orchestrator module. `AIOrchestrator` reads `.meta.json` for uploaded files, routes based on `ai_enrichment_enabled` flag: dispatches to PDF extractor or video transcriber, invokes Bedrock for metadata, sends webhook to Drupal, and moves files from `/pending/` to `/processed/`. Contains its own `Dockerfile` and `requirements.txt`.
- **`src/handlers/`** — Lambda entry points. Each handler wraps an extractor, generator, inference, or orchestrator module, parses the event, and returns a structured response.
- **`scripts/`** — AWS CLI deployment scripts (per-Lambda: `deploy-*.sh`, `invoke-*.sh`, `teardown-*.sh`).
- **`tests/`** — Mirrors `src/` structure. Tests use in-memory assets and mock S3 via `unittest.mock`.

### Deployment

Docker-based Lambdas deployed via AWS CLI (no CDK/SAM). Each Lambda has its own Dockerfile inside its `src/` module, a deploy script, and an ECR repo. Images built with `--platform linux/amd64 --provenance=false` for Lambda compatibility on Apple Silicon.

### AWS Resources (account `141770997341`, us-east-1)

| Resource | Name | Config |
|----------|------|--------|
| S3 Bucket | `dev-ieee-conference-cloud-bulk-uploads` | Shared across Lambdas, versioned |
| ECR | `ieee-cc-pdf-extractor` | PDF extractor |
| ECR | `ieee-rc-image-generator` | Image overlay |
| ECR | `ieee-cc-bedrock-inference` | Bedrock metadata |
| ECR | `ieee-cc-video-transcriber` | Video transcriber |
| ECR | `ieee-rc-ai-orchestrator` | AI orchestrator |
| ECR | `ieee-rc-dlq-processor` | DLQ processor |
| ECR | `ieee-rc-bulk-processor` | Bulk manifest dispatcher |
| ECR | `ieee-rc-bulk-worker` | Bulk per-item worker |
| Lambda | `ieee-cc-pdf-extractor` | 3 GB, 5 min timeout, Python 3.13 |
| Lambda | `ieee-rc-image-generator` | 1024 MB, 60s timeout, Python 3.12 |
| Lambda | `ieee-cc-bedrock-inference` | 512 MB, 120s timeout, Python 3.13 |
| Lambda | `ieee-cc-video-transcriber` | 512 MB, 15 min timeout, Python 3.13 |
| Lambda | `ieee-rc-ai-orchestrator` | 512 MB, 15 min timeout, Python 3.12 |
| Lambda | `ieee-rc-dlq-processor` | 256 MB, 60s timeout, Python 3.13 |
| Lambda | `ieee-rc-bulk-processor` | 512 MB, 5 min timeout, Python 3.13 |
| Lambda | `ieee-rc-bulk-worker` | 512 MB, 5 min timeout, Python 3.13 |
| IAM Role | `ieee-cc-pdf-extractor-role` | S3 read/write + CloudWatch |
| IAM Role | `ieee-rc-image-generator-role` | S3 read/write/delete + CloudWatch |
| IAM Role | `ieee-cc-bedrock-inference-role` | S3 read + Bedrock invoke + CloudWatch |
| IAM Role | `ieee-cc-video-transcriber-role` | S3 read/write + Transcribe + Bedrock + CloudWatch |
| IAM Role | `ieee-rc-ai-orchestrator-role` | S3 read/write/delete + Lambda invoke + CloudWatch |
| IAM Role | `ieee-rc-dlq-processor-role` | Lambda invoke + S3 write (failed/) + SNS publish + SQS receive + CloudWatch |
| IAM Role | `ieee-rc-bulk-processor-role` | S3 read (manifests) + S3 write (progress) + SQS send + SNS publish + CloudWatch |
| IAM Role | `ieee-rc-bulk-worker-role` | Lambda invoke + S3 read/write (pending, metadata, progress) + SNS publish + SQS receive + CloudWatch |
| SQS Queue | `ieee-rc-processing-dlq` | DLQ for failed pipeline events |
| SQS Queue | `ieee-rc-bulk-processing-queue` | Bulk re-tagging work queue (MaxConcurrency 10) |
| SQS Trigger | `ieee-rc-processing-dlq` | -> `ieee-rc-dlq-processor` (batch size 1) |
| SQS Trigger | `ieee-rc-bulk-processing-queue` | -> `ieee-rc-bulk-worker` (batch size 1, MaxConcurrency 10) |
| S3 Trigger | `actions/*.json` | -> `ieee-rc-image-generator` |

### S3 Path Conventions

- PDF Input: `{ou}/pending/{filename}.pdf`
- PDF Metadata output: `{ou}/metadata/{product_part_number}.pdf.json`
- Image trigger: `actions/{job_id}.json`
- Image background: `backgrounds/{ou_short_name}.jpg`
- Image output: `{config.public_path}/{product_part_number}.{format}`
- Video Input: `{ou}/pending/{filename}.{mp4|mov|webm}`
- Video Metadata output: `{ou}/metadata/{product_part_number}.mp4.json`
- Meta config: `{ou}/metadata/{item_id}.meta.json`
- Processed output: `{ou}/processed/{item_id}.{ext}`
- DLQ archive: `failed/{correlation_id}/{timestamp}.json`
- WebVTT subtitle (transcribe output): `transcribe-output/{job_name}.vtt`
- WebVTT subtitle (orchestrator copy): `{ou}/subtitles/{product_part_number}.vtt`
- Bulk manifest: `bulk/manifests/{batch_id}.json`
- Bulk progress: `bulk/progress/{batch_id}_progress.json`

### Key Conventions

- All modules accept an optional `s3_client` param for dependency injection (testability).
- Each module exposes a method for unit testing without S3/Bedrock (e.g. `extract_from_bytes()`, `generate_overlay()`, `generate_metadata()`).
- Results use `TypedDict` for type safety.
- AWS profile: `ieee-cc` (set via `.envrc` / direnv).
