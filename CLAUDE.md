# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

IEEE Content Conversion (CC) pipeline — Python Lambda modules that extract text from PDFs in S3 and pass it to AWS Bedrock (Claude Sonnet) for processing. Modules are designed as reusable classes called by an orchestrator Lambda.

## Commands

```bash
# Run all tests
python -m pytest tests/ -v

# Run a single test file
python -m pytest tests/extractors/test_pdf_extractor.py -v

# Run a single test class or method
python -m pytest tests/extractors/test_pdf_extractor.py::TestNormalPDF::test_extracts_text -v

# Install dependencies
pip install -r requirements.txt -r requirements-dev.txt
```

## Architecture

- **`src/extractors/`** — Reusable extraction modules (one per file type). Each extractor class takes an S3 client, downloads the file, extracts content, writes metadata JSON back to S3, and returns a structured result dict.
- **`tests/`** — Mirrors `src/` structure. Tests use in-memory PDFs built with PyMuPDF and mock S3 via `unittest.mock`.

### S3 Path Conventions

- Input: `{ou}/pending/{filename}.pdf`
- Metadata output: `{ou}/metadata/{product_part_number}.pdf.json`

### Key Conventions

- Extractors accept an optional `s3_client` param for dependency injection (testability).
- Each extractor exposes a `extract_from_bytes()` method for unit testing without S3.
- Extraction results use `TypedDict` with fields: `text`, `page_count`, `extraction_method` (`"text"` | `"ocr"` | `"failed"`).
- Text is truncated to 180,000 characters to fit Claude Sonnet's context window.
- AWS profile: `ieee-cc` (set via `.envrc` / direnv).
