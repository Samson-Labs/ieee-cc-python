# QA Testing Guide — Bulk Processing Lambda (CC3-786)

## Overview

Two-Lambda system for batch re-tagging of existing catalog items:

- **BulkProcessor** (`ieee-rc-bulk-processor`) — Reads a manifest JSON from S3, validates items, estimates cost, and fans out to SQS.
- **BulkWorker** (`ieee-rc-bulk-worker`) — Consumes SQS messages, copies files to `/pending/`, creates `.meta.json`, invokes orchestrator, tracks progress, sends SNS on batch completion.

## Acceptance Criteria Checklist

- [x] AC #1 — BulkProcessor reads manifest from `bulk/manifests/{batch_id}.json`
- [x] AC #2 — Validates manifest structure (`batch_id`, `callback_url`, `items[]`) and item fields (`item_id`, `request_id`, `s3_key`, `media_type`, `resource_center`)
- [x] AC #3 — Validates `media_type` against allowed set (`PDF`, `MP4`, `MOV`, `WEBM`)
- [x] AC #4 — Estimates per-item cost by media type (PDF: $0.01, video: $0.03)
- [x] AC #5 — Publishes each item to SQS with optional configurable delay (`delay_between_ms`)
- [x] AC #6 — Writes initial progress file to `bulk/progress/{batch_id}_progress.json`
- [x] AC #7 — BulkWorker copies source file from archive to `{ou}/pending/{item_id}.{ext}`
- [x] AC #8 — BulkWorker creates `.meta.json` with correct MIME type mapping and `callback_url`
- [x] AC #9 — BulkWorker invokes orchestrator Lambda synchronously
- [x] AC #10 — BulkWorker updates progress file (completed/failed counters)
- [x] AC #11 — BulkWorker sends SNS completion notification when all items processed
- [x] AC #12 — Graceful failure handling — orchestrator errors don't crash the worker

## Architecture

```
Manifest (S3)
    │
    ▼
BulkProcessor Lambda
    │  validates, estimates cost
    │  publishes N items to SQS
    ▼
SQS Bulk Queue
    │
    ▼
BulkWorker Lambda (per item)
    │  copies file → /pending/
    │  creates .meta.json
    │  invokes Orchestrator
    │  updates progress
    ▼
SNS notification on batch completion
```

## S3 Path Conventions

| Path | Purpose |
|------|---------|
| `bulk/manifests/{batch_id}.json` | Input manifest |
| `bulk/progress/{batch_id}_progress.json` | Progress tracking |
| `{ou}/pending/{item_id}.{ext}` | File staged for orchestrator |
| `{ou}/metadata/{item_id}.meta.json` | Meta config for orchestrator |

## Manifest Schema

```json
{
  "batch_id": "retag-2026-03",
  "callback_url": "https://drupal.example.com/api/v1/ai/webhook",
  "config": {
    "delay_between_ms": 500
  },
  "items": [
    {
      "item_id": 100,
      "request_id": 1,
      "s3_key": "PES/archive/paper.pdf",
      "media_type": "PDF",
      "resource_center": "PES"
    }
  ]
}
```

## Environment Variables

| Variable | Lambda | Default |
|----------|--------|---------|
| `S3_BUCKET` | Both | `dev-ieee-conference-cloud-bulk-uploads` |
| `BULK_QUEUE_URL` | Processor | (required) |
| `ORCHESTRATOR_FUNCTION_NAME` | Worker | `ieee-rc-ai-orchestrator` |
| `COMPLETION_SNS_TOPIC_ARN` | Worker | (optional) |

## Test Coverage

**37 unit tests** across processor and worker:

```
tests/bulk/test_bulk_processor.py — 17 tests
tests/bulk/test_bulk_worker.py   — 20 tests
```

Run:
```bash
python -m pytest tests/bulk/ -v
```

## Manual QA Test

**TC-1: Dispatch Manifest**
```bash
cat <<'EOF' | aws s3 cp - s3://dev-ieee-conference-cloud-bulk-uploads/bulk/manifests/test-batch.json
{
  "batch_id": "test-batch",
  "callback_url": "https://httpbin.org/post",
  "items": [
    {"item_id": 100, "request_id": 1, "s3_key": "PES/archive/paper.pdf", "media_type": "PDF", "resource_center": "PES"}
  ]
}
EOF

./scripts/invoke-bulk-processor.sh dev-ieee-conference-cloud-bulk-uploads test-batch
```

**Expected:** Status `dispatched`, `published_count: 1`, cost estimate returned, progress file written.
