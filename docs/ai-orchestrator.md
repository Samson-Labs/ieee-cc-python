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
|       5a. Copy VTT subtitle |  (video only, best-effort)
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
| `{ou}/subtitles/{product_part_number}.vtt` | WebVTT subtitle file (video only) |

## Webhook Payload

When `callback_url` is provided and AI enrichment completes:

```json
{
  "request_id": "abc-123",
  "item_id": "STD-12345",
  "status": "success",
  "signal": "extraction_ready",
  "product_part_number": "STD-12345",
  "ou": "PES",
  "completed_at": "2026-03-18T17:40:00Z",
  "extraction": { "...extraction result..." },
  "data": { "...bedrock result..." },
  "vtt_s3_key": null
}
```

For video transcriptions, `signal` is `"transcription_ready"` and `vtt_s3_key` contains the S3 key of the WebVTT subtitle file:

```json
{
  "request_id": "abc-123",
  "item_id": "VID-001",
  "status": "success",
  "signal": "transcription_ready",
  "product_part_number": "VID-001",
  "ou": "PES",
  "completed_at": "2026-03-18T17:40:00Z",
  "extraction": {
    "transcript": "Speaker 1: Welcome to the lecture...",
    "duration": "01:23:45",
    "duration_seconds": 5025,
    "speaker_count": 2,
    "vtt_s3_key": "transcribe-output/ieee-rc-VID-001-1712345678.vtt"
  },
  "data": { "...bedrock result..." },
  "vtt_s3_key": "PES/subtitles/VID-001.vtt"
}
```

### Webhook Field Reference

| Field | Type | Description |
|-------|------|-------------|
| `request_id` | string | Correlation ID from `.meta.json` or Lambda request |
| `item_id` | string | Entity ID from `.meta.json` |
| `status` | string | Always `"success"` |
| `signal` | string | `"extraction_ready"` (PDF) or `"transcription_ready"` (video) |
| `product_part_number` | string | Product identifier |
| `ou` | string | Organizational unit |
| `completed_at` | string | ISO 8601 UTC timestamp |
| `extraction` | object | Raw extraction/transcription result from the downstream Lambda |
| `data` | object | Bedrock AI-generated metadata (abstract, keywords, etc.) |
| `vtt_s3_key` | string \| null | S3 key of the WebVTT subtitle file, or `null` for PDFs / when unavailable |

### Drupal Integration: WebVTT Subtitles

When `signal` is `"transcription_ready"` and `vtt_s3_key` is non-null, the VTT file is available at:

```
s3://{bucket}/{vtt_s3_key}
```

For example: `s3://dev-ieee-conference-cloud-bulk-uploads/PES/subtitles/VID-001.vtt`

The VTT file follows the [WebVTT standard](https://www.w3.org/TR/webvtt1/) and can be used directly with the HTML5 `<track>` element:

```html
<video controls>
  <source src="video.mp4" type="video/mp4">
  <track kind="captions" src="{vtt_url}" srclang="en" label="English" default>
</video>
```

**Notes for Drupal integration:**
- `vtt_s3_key` is `null` for PDF submissions and when subtitle generation fails (best-effort)
- The VTT file is written to `{ou}/subtitles/{product_part_number}.vtt` — derive the S3 URL or CloudFront URL from this key
- Subtitle generation failure does not block the pipeline — the webhook is still sent with `vtt_s3_key: null`
- Cue indices start at 1 (not 0)

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
