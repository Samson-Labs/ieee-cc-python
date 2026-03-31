# IEEE Content Conversion — Python Pipeline

Python Lambda modules for the IEEE Content Conversion pipeline. Handles PDF text extraction, video transcription, image overlay generation, AI-powered metadata generation, and central orchestration via Docker-based Lambdas deployed to AWS.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt -r requirements-dev.txt

# Run all tests (353 total)
python -m pytest tests/ -v

# Deploy PDF Extractor
./scripts/deploy.sh

# Deploy Image Overlay Generator
./scripts/deploy-image-overlay.sh

# Deploy Bedrock Metadata Generator
./scripts/deploy-bedrock.sh

# Deploy Video Transcriber
./scripts/deploy-video-transcriber.sh

# Deploy AI Orchestrator
./scripts/deploy-ai-orchestrator.sh

# Deploy DLQ Processor
./scripts/deploy-dlq-processor.sh

# Deploy Bulk Processor (manifest dispatcher)
./scripts/deploy-bulk-processor.sh

# Deploy Bulk Worker (SQS-triggered, per-item)
./scripts/deploy-bulk-worker.sh
```

## Lambdas

### PDF Text Extractor

Extracts text from PDFs in S3 using PyMuPDF. Strips headers/footers, removes page numbers, truncates to 180k chars for Claude Sonnet's context window.

```
S3: {ou}/pending/{file}.pdf
        |
        v
+------------------------+
|  ieee-cc-pdf-extractor |  (Python 3.13, PyMuPDF, 3GB, 5min)
|  +- Header/footer strip|  (top/bottom 8%)
|  +- Page number removal|  (regex)
|  +- Truncate 180k      |  (Claude Sonnet limit)
+----------+-------------+
           |
           +-> Response: {text, page_count, extraction_method}
           |
           +-> S3: {ou}/metadata/{part_number}.pdf.json
```

### Image Overlay Generator

Generates product overlay images from JSON trigger files. Loads a background image, applies title/author text overlays using Pillow, writes output to a destination bucket.

```
S3: actions/{job_id}.json  (trigger from Drupal)
        |
        v
+---------------------------+
|  ieee-rc-image-generator  |  (Python 3.12, Pillow, 1024MB, 60s)
|  +- Load background       |  backgrounds/{ou}.jpg
|  +- Title overlay          |  (40px, max 3 lines, word-wrap)
|  +- Author overlay         |  (24px, max 2 lines)
|  +- Optional thumbnail     |  (400x300 max)
+----------+----------------+
           |
           +-> S3: {public_path}/{part_number}.{jpg|png}
           |
           +-> Delete trigger JSON on success
```

### Video Transcriber

Transcribes video files (MP4, MOV, WEBM) using AWS Transcribe with speaker diarization. Optionally cleans transcripts with Claude 3.5 Haiku to remove filler words and fix formatting.

```
S3: {ou}/pending/{file}.{mp4|mov|webm}
        |
        v
+-----------------------------+
|  ieee-cc-video-transcriber  |  (Python 3.13, boto3, 512MB, 15min)
|  +- AWS Transcribe          |  (en-US, max 2 speakers)
|  +- Speaker diarization     |  (poll every 30s, 600s timeout)
|  +- Claude Haiku cleanup    |  (optional filler word removal)
+----------+------------------+
           |
           +-> Response: {transcript, duration, duration_seconds, speaker_count}
           |
           +-> S3: {ou}/metadata/{part_number}.mp4.json
```

### Bedrock Metadata Generator

Takes extracted document text, sends it to AWS Bedrock (Claude Sonnet) with the IEEE Technical Metadata Specialist system prompt (v1.2), and returns structured metadata.

```
Extracted text (direct or from S3 JSON)
        |
        v
+------------------------------+
|  ieee-cc-bedrock-inference   |  (Python 3.13, Bedrock, 512MB, 120s)
|  +- System prompt v1.2       |  Technical Metadata Specialist
|  +- Thesaurus context        |  (optional IEEE terms)
|  +- Retry: throttle (3x)     |  exponential backoff 1s/2s/4s
|  +- Retry: invalid JSON (1x) |  explicit JSON instruction
+----------+-------------------+
           |
           +-> Response: {abstract, keywords, learning_level,
                          intended_audience, category, processing_time_ms}
```

### AI Orchestrator

Central routing Lambda triggered by S3 ObjectCreated events on `{ou}/pending/`. Reads `.meta.json` to determine routing: if AI enrichment is disabled, moves the file to `/processed/`; if enabled, dispatches to the appropriate extraction Lambda, invokes Bedrock for metadata, sends a webhook to Drupal, then moves the file.

```
S3: {ou}/pending/{file}.{pdf|mp4|mov|webm}
        |
        v
+-----------------------------+
|  ieee-rc-ai-orchestrator   |  (Python 3.12, boto3, 512MB, 15min)
|  +- Read .meta.json        |  {ou}/metadata/{item_id}.meta.json
|  +- Route by media_type    |  PDF -> extractor, Video -> transcriber
|  +- Invoke Bedrock         |  metadata generation
|  +- Send webhook           |  HMAC-SHA256 signed POST to Drupal
|  +- Move to /processed/    |  copy + delete
|  +- DLQ on failure         |  publish to SQS for retry
+-----------------------------+
```

**.meta.json schema:**
```json
{
  "item_id": "STD-12345",
  "ou": "PES",
  "product_part_number": "STD-12345",
  "ai_enrichment_enabled": true,
  "content": { "media_type": "application/pdf", "filename": "STD-12345.pdf" },
  "callback_url": "https://drupal.example.com/hook"
}
```

### DLQ Processor

Reads failed pipeline events from SQS dead-letter queue. Re-invokes the orchestrator for retriable errors (up to 2 attempts), archives permanently failed messages to S3, and sends SNS alerts.

```
SQS: ieee-rc-processing-dlq
        |
        v
+---------------------------+
|  ieee-rc-dlq-processor   |  (Python 3.13, boto3, 256MB, 60s)
|  +- Parse DLQ message     |  error_type, is_retriable, retry_count
|  +- Retriable: reprocess  |  re-invoke orchestrator (max 2x)
|  +- Permanent: archive    |  S3: failed/{correlation_id}/{ts}.json
|  +- SNS alert             |  publish failure summary
+---------------------------+
```

### Bulk Processor (Manifest Dispatcher)

Reads a batch manifest from S3, validates items, estimates cost, and publishes each item to an SQS queue for processing. Used for re-tagging existing catalog items through the AI pipeline.

```
Direct invoke: {"batch_id": "bulk-2026-03-17"}
        |
        v
+----------------------------+
|  ieee-rc-bulk-processor   |  (Python 3.13, boto3, 512MB, 5min)
|  +- Read manifest          |  bulk/manifests/{batch_id}.json
|  +- Validate items         |  media_type, s3_key, resource_center
|  +- Estimate cost          |  PDF ~$0.01, video ~$0.03 per item
|  +- Publish to SQS         |  with configurable delay_between_ms
|  +- Write progress         |  bulk/progress/{batch_id}_progress.json
+----------------------------+
```

### Bulk Worker (Per-Item Processor)

SQS-triggered Lambda that processes individual catalog items. Copies files to `/pending/`, creates `.meta.json`, invokes the orchestrator, and tracks batch progress.

```
SQS: ieee-rc-bulk-processing-queue (MaxConcurrency=10)
        |
        v
+----------------------------+
|  ieee-rc-bulk-worker      |  (Python 3.13, boto3, 512MB, 5min)
|  +- Copy to /pending/      |  archive -> pending
|  +- Create .meta.json      |  ai_enrichment_enabled: true
|  +- Invoke orchestrator    |  synchronous (RequestResponse)
|  +- Update progress        |  bulk/progress/{batch_id}_progress.json
|  +- SNS on completion      |  when all items processed
+----------------------------+
```

## AWS Resources

| Resource | Name | Config |
|----------|------|--------|
| S3 Bucket | `dev-ieee-conference-cloud-bulk-uploads` | Shared, versioned |
| ECR | `ieee-cc-pdf-extractor` | PDF extractor image |
| ECR | `ieee-rc-image-generator` | Image overlay image |
| ECR | `ieee-cc-bedrock-inference` | Bedrock metadata image |
| ECR | `ieee-cc-video-transcriber` | Video transcriber image |
| ECR | `ieee-rc-ai-orchestrator` | AI orchestrator image |
| ECR | `ieee-rc-dlq-processor` | DLQ processor image |
| ECR | `ieee-rc-bulk-processor` | Bulk manifest dispatcher image |
| ECR | `ieee-rc-bulk-worker` | Bulk per-item worker image |
| Lambda | `ieee-cc-pdf-extractor` | 3 GB, 5 min timeout |
| Lambda | `ieee-rc-image-generator` | 1024 MB, 60s timeout |
| Lambda | `ieee-cc-bedrock-inference` | 512 MB, 120s timeout |
| Lambda | `ieee-cc-video-transcriber` | 512 MB, 15 min timeout |
| Lambda | `ieee-rc-ai-orchestrator` | 512 MB, 15 min timeout |
| Lambda | `ieee-rc-dlq-processor` | 256 MB, 60s timeout |
| Lambda | `ieee-rc-bulk-processor` | 512 MB, 5 min timeout |
| Lambda | `ieee-rc-bulk-worker` | 512 MB, 5 min timeout |
| SQS Queue | `ieee-rc-processing-dlq` | DLQ for failed pipeline events |
| SQS Queue | `ieee-rc-bulk-processing-queue` | Bulk re-tagging work queue (MaxConcurrency 10) |
| SQS Trigger | `ieee-rc-processing-dlq` | -> `ieee-rc-dlq-processor` (batch size 1) |
| SQS Trigger | `ieee-rc-bulk-processing-queue` | -> `ieee-rc-bulk-worker` (batch size 1, MaxConcurrency 10) |
| S3 Trigger | `actions/*.json` | -> `ieee-rc-image-generator` |

## Invoking

**PDF Extractor:**
```bash
./scripts/invoke.sh dev-ieee-conference-cloud-bulk-uploads ieee/pending/STD-12345.pdf ieee STD-12345
```

**Image Overlay Generator:**
```bash
./scripts/invoke-image-overlay.sh dev-ieee-conference-cloud-bulk-uploads actions/job-001.json
```

**Bedrock Metadata Generator:**
```bash
# From S3 metadata JSON
./scripts/invoke-bedrock.sh dev-ieee-conference-cloud-bulk-uploads PES/metadata/doc.pdf.json

# Direct text
./scripts/invoke-bedrock.sh --text "Extracted document text..."
```

**Video Transcriber:**
```bash
./scripts/invoke-video-transcriber.sh dev-ieee-conference-cloud-bulk-uploads PES/pending/lecture.mp4 PES LECTURE-001
```

**AI Orchestrator:**
```bash
./scripts/invoke-ai-orchestrator.sh dev-ieee-conference-cloud-bulk-uploads PES/pending/STD-12345.pdf
```

**DLQ Processor:**
```bash
./scripts/invoke-dlq-processor.sh
```

**Bulk Processor:**
```bash
./scripts/invoke-bulk-processor.sh bulk-2026-03-17
```

**Bulk Worker:**
```bash
./scripts/invoke-bulk-worker.sh
```

## Project Structure

```
src/
  extractors/
    Dockerfile                    # Python 3.13 + PyMuPDF (PDF extractor)
    VideoTranscriberDockerfile    # Python 3.13 + boto3 (Video transcriber)
    pdf_extractor.py              # PDF text extraction
    video_transcriber.py          # Video transcription (AWS Transcribe + Haiku)
  generators/
    Dockerfile                    # Python 3.12 + Pillow
    requirements.txt              # Pillow + boto3
    image_overlay_generator.py    # Image overlay generation
  ai/
    Dockerfile                    # Python 3.13 + boto3
    requirements.txt              # boto3
    bedrock_inference.py          # Bedrock Claude metadata generation
  orchestrator/
    AIOrchestratorDockerfile       # Python 3.12 + boto3
    ai_orchestrator_requirements.txt
    ai_orchestrator.py             # Central routing orchestrator
  common/
    exceptions.py                 # PipelineError hierarchy
    retry.py                      # @with_retry decorator
    error_handler.py              # Structured error responses
    logging.py                    # JSON structured logger
    dlq.py                        # DLQ message builder
  webhook/
    sender.py                     # HMAC-SHA256 webhook sender with retry
  dlq/
    Dockerfile                    # Python 3.13 + boto3
    dlq_processor.py              # DLQ message processor
  bulk/
    Dockerfile.processor          # Python 3.13 + boto3 (manifest dispatcher)
    Dockerfile.worker             # Python 3.13 + boto3 (per-item worker)
    bulk_processor.py             # Manifest reader + SQS publisher
    bulk_worker.py                # Per-item orchestrator invoker
  handlers/
    pdf_handler.py                # PDF extractor Lambda entry point
    image_overlay_handler.py      # Image overlay Lambda entry point
    bedrock_handler.py            # Bedrock inference Lambda entry point
    video_transcriber_handler.py  # Video transcriber Lambda entry point
    ai_orchestrator_handler.py    # AI orchestrator Lambda entry point
    dlq_handler.py                # DLQ processor Lambda entry point
    bulk_processor_handler.py     # Bulk processor Lambda entry point
    bulk_worker_handler.py        # Bulk worker Lambda entry point
tests/
  conftest.py                                    # Shared fixtures
  extractors/test_pdf_extractor.py               # 21 tests
  extractors/test_video_transcriber.py           # 34 tests
  generators/test_image_overlay_generator.py     # 43 tests
  ai/test_bedrock_inference.py                   # 25 tests
  orchestrator/test_ai_orchestrator.py           # 30 tests
  common/test_exceptions.py                      # 21 tests
  common/test_error_handler.py                   # 13 tests
  common/test_retry.py                           # 12 tests
  common/test_logging.py                         # 10 tests
  common/test_dlq.py                             #  9 tests
  webhook/test_sender.py                         # 11 tests
  dlq/test_dlq_processor.py                      # 17 tests
  bulk/test_bulk_processor.py                    # 17 tests
  bulk/test_bulk_worker.py                       # 20 tests
  handlers/
    test_pdf_handler.py                          #  9 tests
    test_image_overlay_handler.py                # 12 tests
    test_bedrock_handler.py                      #  9 tests
    test_video_transcriber_handler.py            # 19 tests
    test_ai_orchestrator_handler.py              # 17 tests
    test_dlq_handler.py                          #  4 tests
scripts/
  deploy.sh / invoke.sh / teardown.sh                        # PDF extractor
  deploy-image-overlay.sh / invoke-image-overlay.sh / ...    # Image overlay
  deploy-bedrock.sh / invoke-bedrock.sh / ...                # Bedrock metadata
  deploy-video-transcriber.sh / invoke-video-transcriber.sh / ...  # Video transcriber
  deploy-ai-orchestrator.sh / invoke-ai-orchestrator.sh / ...     # AI orchestrator
  deploy-dlq-processor.sh / invoke-dlq-processor.sh / ...         # DLQ processor
  deploy-bulk-processor.sh / invoke-bulk-processor.sh / ...       # Bulk processor
  deploy-bulk-worker.sh / invoke-bulk-worker.sh / ...             # Bulk worker
```

## Documentation

- [PDF Extractor Module](docs/pdf-extractor.md) — extraction pipeline, error handling, API
- [Image Overlay Generator](docs/image-overlay-generator.md) — trigger schema, text layout, output formats
- [Bedrock Metadata Generator](docs/bedrock-inference.md) — system prompt, validation, retry logic
- [Video Transcriber Module](docs/video-transcriber.md) — AWS Transcribe integration, speaker diarization, Haiku cleanup
- [AI Orchestrator Module](docs/ai-orchestrator.md) — routing logic, .meta.json schema, Lambda dispatch
- [Deployment Guide](docs/deployment.md) — AWS CLI deploy, teardown, configuration
- [DevOps CI/CD Handoff](docs/devops-cicd-handoff.md) — architecture, build matrix, environment config
- **QA Testing Guides:**
  - [Integrated AWS Testing](docs/qa-integrated-aws-testing.md) — end-to-end pipeline test scenarios
  - [PDF Extractor QA](docs/qa-pdf-extractor.md) — PDF extraction test cases
  - [Image Overlay QA](docs/qa-image-overlay.md) — Image overlay test cases and results
  - [Video Transcriber QA](docs/qa-video-transcriber.md) — Video transcription test cases and AWS live results
  - [AI Orchestrator QA](docs/qa-ai-orchestrator.md) — Orchestrator test cases and AWS live results
  - [Bedrock Inference QA](docs/qa-bedrock-inference.md) — Bedrock metadata generation test cases
  - [Webhook Signing QA](docs/qa-webhook-signing.md) — HMAC-SHA256 webhook signing test cases
  - [Error Handling & DLQ QA](docs/qa-error-handling-dlq.md) — Error handling, retry, and DLQ test cases
  - [Bulk Processor QA](docs/qa-bulk-processor.md) — Bulk re-tagging test cases
  - [Testing Guide](docs/qa-testing-guide.md) — General testing guide
