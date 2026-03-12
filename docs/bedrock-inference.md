# Bedrock Metadata Generation Module

## Overview

The `BedrockInference` module calls AWS Bedrock (Claude Sonnet) to generate structured metadata from extracted document text. It uses the IEEE Technical Metadata Specialist system prompt (v1.2) to produce a JSON response with abstract, keywords, learning level, intended audience, and category.

## Module Path

- **Inference class:** `src/ai/bedrock_inference.py`
- **Lambda handler:** `src/handlers/bedrock_handler.py`
- **System prompt:** `docs/plans/external/1.2 AI System Prompt.md`

## API Contract

### Request to Bedrock

```json
{
  "anthropic_version": "bedrock-2023-05-31",
  "modelId": "anthropic.claude-sonnet-4-5-20250929-v1:0",
  "messages": [
    {"role": "user", "content": "<extracted text truncated to 180k chars>"}
  ],
  "system": "<system prompt v1.2 + optional thesaurus context>",
  "max_tokens": 2048,
  "temperature": 0.3
}
```

### Response (InferenceResult)

```json
{
  "abstract": "First paragraph (50-150 words).\n\nSecond paragraph (50-150 words).",
  "keywords": ["term1", "term2", "...", "term8-12"],
  "learning_level": "Foundational | Professional | Expert",
  "intended_audience": "Non-Engineer | Engineering Adjacent Professional | New Engineer | Seasoned Engineering Professional",
  "category": "Research Papers and Publications | Professional Development | Society Outreach | Technical Tutorial",
  "processing_time_ms": 1234
}
```

## Lambda Invocation

### Direct invocation (text in event)

```json
{
  "text": "Extracted document text...",
  "thesaurus_terms": ["smart grid", "power systems"]
}
```

### S3 metadata reference

```json
{
  "bucket": "dev-ieee-conference-cloud-bulk-uploads",
  "key": "PES/metadata/doc.pdf.json"
}
```

The S3 JSON file must contain an `extractedText` field.

## Validation Rules

| Field | Rule |
|-------|------|
| `abstract` | Two paragraphs separated by `\n\n`, each 50–150 words |
| `keywords` | Array of 8–12 non-empty strings |
| `learning_level` | One of: Foundational, Professional, Expert |
| `intended_audience` | One of: Non-Engineer, Engineering Adjacent Professional, New Engineer, Seasoned Engineering Professional |
| `category` | One of: Research Papers and Publications, Professional Development, Society Outreach, Technical Tutorial |

## Retry Logic

1. **Bedrock throttling (429):** Exponential backoff — 1s, 2s, 4s (max 3 attempts)
2. **Invalid JSON response:** Retry once with explicit JSON instruction appended to system prompt

## Configuration

| Setting | Default | Override |
|---------|---------|----------|
| Model ID | `anthropic.claude-sonnet-4-5-20250929-v1:0` | `BEDROCK_MODEL_ID` env var |
| Log level | `INFO` | `LOG_LEVEL` env var |
| Text truncation | 180,000 chars | `TEXT_TRUNCATION_LIMIT` constant |

## AWS Resources

| Resource | Name |
|----------|------|
| Lambda | `ieee-cc-bedrock-inference` (512 MB, 120s timeout) |
| ECR | `ieee-cc-bedrock-inference` |
| IAM Role | `ieee-cc-bedrock-inference-role` (S3 read + Bedrock invoke) |
