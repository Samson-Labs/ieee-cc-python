# IEEE Content Conversion — Python Pipeline

PDF text extraction Lambda for the IEEE Content Conversion pipeline. Extracts text from PDFs in S3 and returns cleaned output suitable for AWS Bedrock (Claude Sonnet).

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt -r requirements-dev.txt

# Run tests
python -m pytest tests/ -v

# Deploy to AWS (first time)
./scripts/deploy.sh

# Deploy code changes only
./scripts/deploy.sh update
```

## Architecture

```
S3: {ou}/pending/{file}.pdf
        │
        ▼
┌──────────────────────┐
│  Lambda: pdf_handler  │  (Docker, ECR, 3GB, 5min timeout)
│  ├─ PDFExtractor      │  (PyMuPDF text extraction)
│  ├─ Header/footer     │  (strip top/bottom 8%)
│  ├─ Page numbers      │  (regex removal)
│  └─ Truncate 180k     │  (Claude Sonnet context limit)
└──────────┬───────────┘
           │
           ├─► Response: {text, page_count, extraction_method}
           │
           └─► S3: {ou}/metadata/{part_number}.pdf.json
                   {pageCount, extractionMethod, extractedAt}
```

## AWS Resources

| Resource | Name | Details |
|----------|------|---------|
| S3 Bucket | `ieee-cc-python` | Versioned, public access blocked |
| ECR | `ieee-cc-pdf-extractor` | us-east-1 |
| Lambda | `ieee-cc-pdf-extractor` | 3 GB, 5 min timeout |
| IAM Role | `ieee-cc-pdf-extractor-role` | S3 read/write + CloudWatch |

## Invoking the Lambda

**Direct (orchestrator):**
```json
{
  "bucket": "ieee-cc-python",
  "key": "ieee/pending/STD-12345.pdf",
  "ou": "ieee",
  "product_part_number": "STD-12345"
}
```

**Via script:**
```bash
./scripts/invoke.sh ieee-cc-python ieee/pending/STD-12345.pdf ieee STD-12345
```

**Response:**
```json
{
  "statusCode": 200,
  "body": {
    "text": "Extracted content...",
    "page_count": 42,
    "extraction_method": "text"
  }
}
```

## Project Structure

```
src/
  extractors/pdf_extractor.py   # PDF text extraction (PyMuPDF)
  handlers/pdf_handler.py       # Lambda entry point
tests/
  extractors/test_pdf_extractor.py  # 21 extractor tests
  handlers/test_pdf_handler.py      # 8 handler tests
scripts/
  deploy.sh     # Full deploy (ECR, S3, IAM, Lambda)
  invoke.sh     # Manual Lambda invocation
  teardown.sh   # Cleanup (preserves S3)
docs/
  pdf-extractor.md   # Module documentation
  deployment.md      # Deployment guide
```

## Documentation

- [PDF Extractor Module](docs/pdf-extractor.md) — extraction pipeline, error handling, API
- [Deployment Guide](docs/deployment.md) — AWS CLI deploy, teardown, configuration
- [Dev Log](DEVLOG.md) — chronological implementation progress
