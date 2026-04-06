# Bedrock Metadata Generation Module

## Overview

The `BedrockInference` module calls AWS Bedrock (Claude Sonnet) to generate structured metadata from extracted document text. It uses the IEEE Technical Metadata Specialist system prompt (v1.2) to produce a JSON response with abstract, keywords, learning level, intended audience, and category.

## Module Path

- **Inference class:** `src/ai/bedrock_inference.py`
- **Lambda handler:** `src/handlers/bedrock_handler.py`
- **Thesaurus search:** `src/ai/thesaurus.py`
- **Thesaurus data:** `src/ai/data/ieee_thesaurus_v104.json` (7,639 preferred terms)
- **System prompt:** `docs/plans/external/1.2 AI System Prompt.md`

## API Contract

### Request to Bedrock

The model ID is passed as the `modelId` API parameter to `invoke_model`, not in the JSON body.

**With thesaurus tool use (default when thesaurus data is loaded):**

```json
{
  "anthropic_version": "bedrock-2023-05-31",
  "messages": [
    {"role": "user", "content": "<extracted text truncated to 180k chars>"}
  ],
  "system": "<system prompt v1.2>",
  "max_tokens": 2048,
  "temperature": 0.3,
  "tools": [
    {
      "name": "search_ieee_thesaurus",
      "description": "Search the IEEE Thesaurus for official standardized terms...",
      "input_schema": {
        "type": "object",
        "properties": {
          "query": {"type": "string", "description": "Topic or concept to search for"}
        },
        "required": ["query"]
      }
    }
  ]
}
```

The LLM makes 2-3 tool calls to search the thesaurus, receives matching IEEE terms, then produces the final JSON response. This multi-turn conversation is handled automatically via a tool-use loop (max 5 iterations).

**Legacy path (when explicit `thesaurus_terms` are provided or thesaurus data is unavailable):**

```json
{
  "anthropic_version": "bedrock-2023-05-31",
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
  "processing_time_ms": 1234,
  "input_tokens": 5000,
  "output_tokens": 500,
  "thesaurus_coverage": 10
}
```

The `thesaurus_coverage` field indicates how many of the returned keywords are IEEE Thesaurus preferred terms.

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

## IEEE Thesaurus Grounding

Keywords are grounded in the IEEE Thesaurus v1.04 (7,639 preferred terms) using Bedrock tool use. During metadata generation, the LLM can call `search_ieee_thesaurus` to look up standardized IEEE terms before selecting keywords.

**How it works:**
1. The `ThesaurusSearch` module (`src/ai/thesaurus.py`) loads the thesaurus on init and provides search against preferred terms and USE FOR synonyms
2. The LLM makes 2-3 searches covering different topic areas from the content
3. The tool returns matching IEEE terms with scope notes and broader terms
4. The LLM selects keywords primarily from thesaurus results, with non-thesaurus terms allowed for topics outside IEEE's coverage

**Observed results:**
- Engineering content (smart grid, deep learning): ~83% thesaurus coverage (10/12)
- Non-IEEE content (economics, policy): ~10% coverage — expected, as IEEE Thesaurus focuses on engineering/technology

**Token overhead:** ~1-2K additional tokens per request (~1% increase).

**Fallback:** When the thesaurus data file is absent or explicit `thesaurus_terms` are provided, the system falls back to the legacy single-request path without tool use.

## Retry Logic

1. **Bedrock throttling (429):** Exponential backoff — 1s, 2s, 4s (max 3 attempts)
2. **Invalid JSON response:** Retry once with explicit JSON instruction appended to system prompt (uses no-tool prompt to prevent hallucinated tool calls)

## Configuration

| Setting | Default | Override |
|---------|---------|----------|
| Model ID | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` | `BEDROCK_MODEL_ID` env var |
| Log level | `INFO` | `LOG_LEVEL` env var |
| Text truncation | 180,000 chars | `TEXT_TRUNCATION_LIMIT` constant |

## AWS Resources

| Resource | Name |
|----------|------|
| Lambda | `ieee-cc-bedrock-inference` (512 MB, 120s timeout) |
| ECR | `ieee-cc-bedrock-inference` |
| IAM Role | `ieee-cc-bedrock-inference-role` (S3 read + Bedrock invoke) |
