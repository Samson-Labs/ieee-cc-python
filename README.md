# IEEE Content Conversion — Python Pipeline

Python Lambda modules for the IEEE Content Conversion pipeline. Handles PDF text extraction and image overlay generation via Docker-based Lambdas deployed to AWS.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt -r requirements-dev.txt

# Run all tests (70 total)
python -m pytest tests/ -v

# Deploy PDF Extractor
./scripts/deploy.sh

# Deploy Image Overlay Generator
./scripts/deploy-image-overlay.sh
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

## AWS Resources

| Resource | Name | Config |
|----------|------|--------|
| S3 Bucket | `dev-ieee-conference-cloud-bulk-uploads` | Shared, versioned |
| ECR | `ieee-cc-pdf-extractor` | PDF extractor image |
| ECR | `ieee-rc-image-generator` | Image overlay image |
| Lambda | `ieee-cc-pdf-extractor` | 3 GB, 5 min timeout |
| Lambda | `ieee-rc-image-generator` | 1024 MB, 60s timeout |
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

## Project Structure

```
src/
  extractors/
    Dockerfile                    # Python 3.13 + PyMuPDF
    pdf_extractor.py              # PDF text extraction
  generators/
    Dockerfile                    # Python 3.12 + Pillow
    requirements.txt              # Pillow + boto3
    image_overlay_generator.py    # Image overlay generation
  handlers/
    pdf_handler.py                # PDF extractor Lambda entry point
    image_overlay_handler.py      # Image overlay Lambda entry point
tests/
  extractors/test_pdf_extractor.py           # 21 tests
  generators/test_image_overlay_generator.py # 28 tests
  handlers/
    test_pdf_handler.py                      # 9 tests
    test_image_overlay_handler.py            # 12 tests
scripts/
  deploy.sh / invoke.sh / teardown.sh                        # PDF extractor
  deploy-image-overlay.sh / invoke-image-overlay.sh / teardown-image-overlay.sh  # Image overlay
```

## Documentation

- [PDF Extractor Module](docs/pdf-extractor.md) — extraction pipeline, error handling, API
- [Image Overlay Generator](docs/image-overlay-generator.md) — trigger schema, text layout, output formats
- [Deployment Guide](docs/deployment.md) — AWS CLI deploy, teardown, configuration
