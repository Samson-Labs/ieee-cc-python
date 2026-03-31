# AI Orchestrator Module

Central routing Lambda for the IEEE Content Conversion pipeline. Reads `.meta.json` configuration for uploaded files and routes them through extraction, AI metadata generation, and webhook notification.

## Architecture

```
S3 ObjectCreated: {ou}/pending/{file}.{ext}
        |
        v
+-----------------------------+
|  ieee-rc-ai-orchestrator   |  512 MB, 5 min timeout
|                             |
|  1. Read .meta.json         |  {ou}/metadata/{item_id}.meta.json
|  2. Check ai_enrichment     |
|     |                       |
|     +-- disabled ---------> |  Move to /processed/ (done)
|     |                       |
|     +-- enabled:            |
|        3. Dispatch extract  |  PDF extractor or video transcriber
|        4. Invoke Bedrock    |  metadata generation
|        5. Send webhook      |  POST to Drupal
|        6. Move to /processed|
+-----------------------------+
```

## .meta.json Schema

The orchestrator reads `{ou}/metadata/{item_id}.meta.json` to determine routing.

```json
{
  "item_id": "STD-12345",
  "ou": "PES",
  "product_part_number": "STD-12345",
  "ai_enrichment_enabled": true,
  "content": {
    "media_type": "application/pdf",
    "filename": "STD-12345.pdf"
  },
  "callback_url": "https://drupal.example.com/hook"
}
```

### Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `item_id` | string | Unique item identifier |
| `ou` | string | Organizational unit (e.g. PES, COMPSOC) |
| `product_part_number` | string | Product part number for metadata output |
| `ai_enrichment_enabled` | boolean | Whether to run AI extraction + Bedrock |
| `content.media_type` | string | MIME type for routing |
| `content.filename` | string | Original filename |

### Optional Fields

| Field | Type | Description |
|-------|------|-------------|
| `callback_url` | string | URL for Drupal notification on completion |

## Media Type Routing

| Media Type | Lambda Target |
|------------|---------------|
| `application/pdf` | `ieee-cc-pdf-extractor` |
| `video/mp4` | `ieee-cc-video-transcriber` |
| `video/quicktime` | `ieee-cc-video-transcriber` |
| `video/webm` | `ieee-cc-video-transcriber` |

## Event Formats

### Direct Invocation

```json
{
  "bucket": "dev-ieee-conference-cloud-bulk-uploads",
  "key": "PES/pending/STD-12345.pdf"
}
```

### S3 Event Trigger

Standard S3 ObjectCreated event format with `Records[0].s3.bucket.name` and `Records[0].s3.object.key`.

## Response Format

```json
{
  "statusCode": 200,
  "body": {
    "item_id": "STD-12345",
    "ou": "PES",
    "action": "enriched",
    "ai_enrichment_enabled": true,
    "source_key": "PES/pending/STD-12345.pdf",
    "destination_key": "PES/processed/STD-12345.pdf",
    "processing_time_ms": 45230
  }
}
```

### Action Values

| Action | Description |
|--------|-------------|
| `moved` | AI disabled — file moved to /processed/ without enrichment |
| `enriched` | AI enabled — extraction + Bedrock + webhook + move completed |

## Error Handling

| Status | Condition |
|--------|-----------|
| 400 | Invalid event, missing/invalid `.meta.json`, unsupported media type |
| 500 | AWS service errors, Lambda dispatch failures, Bedrock errors |

On error, the source file remains in `/pending/` (no partial moves).

## S3 Paths

| Path | Purpose |
|------|---------|
| `{ou}/pending/{item_id}.{ext}` | Input file (trigger) |
| `{ou}/metadata/{item_id}.meta.json` | Routing configuration |
| `{ou}/processed/{item_id}.{ext}` | Output (after processing) |

## Webhook Payload

When `callback_url` is provided and AI enrichment completes:

```json
{
  "item_id": "STD-12345",
  "ou": "PES",
  "product_part_number": "STD-12345",
  "status": "completed",
  "completed_at": "2026-03-18T17:40:00Z",
  "extraction": { "...extraction result..." },
  "metadata": { "...bedrock result..." }
}
```

## AWS Resources

| Resource | Name |
|----------|------|
| ECR | `ieee-rc-ai-orchestrator` |
| Lambda | `ieee-rc-ai-orchestrator` (512 MB, 5 min) |
| IAM Role | `ieee-rc-ai-orchestrator-role` |
| Permissions | S3 read/write/delete, Lambda invoke (pdf-extractor, video-transcriber, bedrock-inference) |

## Deployment

```bash
# First-time deploy (ECR + IAM + Lambda)
./scripts/deploy-ai-orchestrator.sh

# Update code only
./scripts/deploy-ai-orchestrator.sh update

# Invoke
./scripts/invoke-ai-orchestrator.sh <bucket> <key>

# Teardown
./scripts/teardown-ai-orchestrator.sh
```
