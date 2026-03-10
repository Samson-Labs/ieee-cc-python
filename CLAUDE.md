# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

IEEE Content Conversion (CC) pipeline — Python Lambda modules for PDF text extraction and image overlay generation. Modules are designed as reusable classes called by an orchestrator Lambda.

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

# Deploy (first time — creates ECR, S3, IAM role, Lambda)
./scripts/deploy.sh

# Deploy (update code only — rebuild image + update Lambda)
./scripts/deploy.sh update

# Invoke Lambda manually
./scripts/invoke.sh <bucket> <key> <ou> <product_part_number>

# Tear down AWS resources (preserves S3 bucket)
./scripts/teardown.sh
```

## Architecture

- **`src/extractors/`** — Reusable extraction modules (one per file type). Each extractor class takes an S3 client, downloads the file, extracts content, writes metadata JSON back to S3, and returns a structured result dict.
- **`src/generators/`** — Reusable generation modules. Each generator class takes an S3 client, reads trigger JSON, processes assets, writes output to S3, and returns a structured result dict.
- **`src/handlers/`** — Lambda entry points. Each handler wraps an extractor or generator, parses the event, and returns a structured response.
- **`scripts/`** — AWS CLI deployment scripts (per-Lambda: `deploy-*.sh`, `invoke-*.sh`, `teardown-*.sh`).
- **`tests/`** — Mirrors `src/` structure. Tests use in-memory assets and mock S3 via `unittest.mock`.

### Deployment

Docker-based Lambda deployed via AWS CLI (no CDK/SAM). The `Dockerfile` at the project root uses `public.ecr.aws/lambda/python:3.13` as the base image. `scripts/deploy.sh` handles ECR repo creation, Docker build+push, S3 bucket, IAM role, and Lambda function creation. Image is built with `--platform linux/amd64 --provenance=false` for Lambda compatibility on Apple Silicon.

### AWS Resources (account `141770997341`, us-east-1)

| Resource | Name |
|----------|------|
| S3 Bucket | `ieee-cc-python` |
| ECR Repository | `ieee-cc-pdf-extractor` |
| Lambda Function | `ieee-cc-pdf-extractor` |
| IAM Role | `ieee-cc-pdf-extractor-role` |

### S3 Path Conventions

- PDF Input: `{ou}/pending/{filename}.pdf`
- PDF Metadata output: `{ou}/metadata/{product_part_number}.pdf.json`
- Image trigger: `actions/{job_id}.json`
- Image background: `backgrounds/{ou_short_name}.jpg`
- Image output: `{config.public_path}/{product_part_number}.{format}`

### Key Conventions

- All modules accept an optional `s3_client` param for dependency injection (testability).
- Each module exposes a method for unit testing without S3 (e.g. `extract_from_bytes()`, `generate_overlay()`).
- Results use `TypedDict` for type safety.
- AWS profile: `ieee-cc` (set via `.envrc` / direnv).

### Deployment

Docker-based Lambdas deployed via AWS CLI (no CDK/SAM). Each Lambda has its own Dockerfile, deploy script, and ECR repo. Images built with `--platform linux/amd64 --provenance=false` for Lambda compatibility on Apple Silicon.

### AWS Resources (account `141770997341`, us-east-1)

| Resource | Name | Config |
|----------|------|--------|
| S3 Bucket | `ieee-cc-python` | Shared across Lambdas |
| ECR | `ieee-rc-image-generator` | Image overlay |
| Lambda | `ieee-rc-image-generator` | 1024 MB, 60s timeout, Python 3.12 |
| IAM Role | `ieee-rc-image-generator-role` | S3 read/write/delete + CloudWatch |
| S3 Trigger | `actions/*.json` | -> `ieee-rc-image-generator` |

### Deploy Commands

```bash
# Image Overlay Generator
./scripts/deploy-image-overlay.sh          # first-time full deploy
./scripts/deploy-image-overlay.sh update   # rebuild + update code only
./scripts/invoke-image-overlay.sh <bucket> <key>
./scripts/teardown-image-overlay.sh
```
