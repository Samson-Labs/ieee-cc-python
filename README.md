# IEEE Content Conversion — Python Pipeline

Python Lambda modules for the IEEE Content Conversion pipeline. Handles PDF text extraction, video transcription, image overlay generation, AI-powered metadata generation, and central orchestration via Docker-based Lambdas deployed to AWS.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt -r requirements-dev.txt

# Run all tests (218 total)
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
|  ieee-rc-ai-orchestrator   |  (Python 3.12, boto3, 512MB, 5min)
|  +- Read .meta.json        |  {ou}/metadata/{item_id}.meta.json
|  +- Route by media_type    |  PDF -> extractor, Video -> transcriber
|  +- Invoke Bedrock         |  metadata generation
|  +- Send webhook           |  POST to Drupal
|  +- Move to /processed/    |  copy + delete
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
  "webhook_url": "https://drupal.example.com/hook"
}
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
| Lambda | `ieee-cc-pdf-extractor` | 3 GB, 5 min timeout |
| Lambda | `ieee-rc-image-generator` | 1024 MB, 60s timeout |
| Lambda | `ieee-cc-bedrock-inference` | 512 MB, 120s timeout |
| Lambda | `ieee-cc-video-transcriber` | 512 MB, 15 min timeout |
| Lambda | `ieee-rc-ai-orchestrator` | 512 MB, 5 min timeout |
| S3 Trigger | `actions/*.json` | -> image generator |

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
  handlers/
    pdf_handler.py                # PDF extractor Lambda entry point
    image_overlay_handler.py      # Image overlay Lambda entry point
    bedrock_handler.py            # Bedrock inference Lambda entry point
    video_transcriber_handler.py  # Video transcriber Lambda entry point
    ai_orchestrator_handler.py    # AI orchestrator Lambda entry point
tests/
  extractors/test_pdf_extractor.py           # 21 tests
  extractors/test_video_transcriber.py       # 35 tests
  generators/test_image_overlay_generator.py # 43 tests
  ai/test_bedrock_inference.py               # 25 tests
  orchestrator/test_ai_orchestrator.py       # 29 tests
  handlers/
    test_pdf_handler.py                      # 9 tests
    test_image_overlay_handler.py            # 12 tests
    test_bedrock_handler.py                  # 9 tests
    test_video_transcriber_handler.py        # 18 tests
    test_ai_orchestrator_handler.py          # 17 tests
scripts/
  deploy.sh / invoke.sh / teardown.sh                        # PDF extractor
  deploy-image-overlay.sh / invoke-image-overlay.sh / teardown-image-overlay.sh  # Image overlay
  deploy-bedrock.sh / invoke-bedrock.sh / teardown-bedrock.sh                    # Bedrock metadata
  deploy-video-transcriber.sh / invoke-video-transcriber.sh / teardown-video-transcriber.sh  # Video transcriber
  deploy-ai-orchestrator.sh / invoke-ai-orchestrator.sh / teardown-ai-orchestrator.sh        # AI orchestrator
```

## Documentation

- [PDF Extractor Module](docs/pdf-extractor.md) — extraction pipeline, error handling, API
- [Image Overlay Generator](docs/image-overlay-generator.md) — trigger schema, text layout, output formats
- [Bedrock Metadata Generator](docs/bedrock-inference.md) — system prompt, validation, retry logic
- [Video Transcriber Module](docs/video-transcriber.md) — AWS Transcribe integration, speaker diarization, Haiku cleanup
- [Deployment Guide](docs/deployment.md) — AWS CLI deploy, teardown, configuration
- [AI Orchestrator Module](docs/ai-orchestrator.md) — routing logic, .meta.json schema, Lambda dispatch
- **QA Testing Guides:**
  - [Image Overlay QA](docs/qa-image-overlay.md) — Image overlay test cases and results
  - [Video Transcriber QA](docs/qa-video-transcriber.md) — Video transcription test cases and AWS live results
  - [AI Orchestrator QA](docs/qa-ai-orchestrator.md) — Orchestrator test cases and AWS live results
