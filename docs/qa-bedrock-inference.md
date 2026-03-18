# QA Testing Guide — Bedrock Metadata Generator (CC3-776)

## Overview

The Bedrock Metadata Generator (`ieee-cc-bedrock-inference`) takes extracted document text, sends it to AWS Bedrock (Claude Sonnet) with the IEEE Technical Metadata Specialist system prompt (v1.2), and returns structured metadata: abstract, keywords, learning_level, intended_audience, and category.

## Prerequisites

- AWS CLI configured with `ieee-cc` profile
- Access to `dev-ieee-conference-cloud-bulk-uploads` S3 bucket
- Bedrock model access enabled for Anthropic Claude models
- Lambda deployed: `ieee-cc-bedrock-inference`

## Lambda Details

| Property | Value |
|----------|-------|
| Function Name | `ieee-cc-bedrock-inference` |
| Runtime | Python 3.13 (Docker image) |
| Memory | 512 MB |
| Timeout | 120s (2 minutes) |
| ECR Repo | `ieee-cc-bedrock-inference` |
| IAM Role | `ieee-cc-bedrock-inference-role` |
| Model ID | `global.anthropic.claude-sonnet-4-6` (configurable via `BEDROCK_MODEL_ID`) |

## Acceptance Criteria Checklist

- [x] Calls Bedrock API with configurable model ID via `BEDROCK_MODEL_ID` env var
- [x] Constructs messages array with system prompt (v1.2) and user message containing extracted text
- [x] Appends thesaurus context to system prompt when `thesaurus_terms` provided
- [x] Parses response as JSON, validates all required fields
- [x] Abstract: two paragraphs separated by `\n\n`, each 50-150 words
- [x] Keywords: array of 8-12 strings
- [x] Learning level: one of `["Foundational", "Professional", "Expert"]`
- [x] Intended audience: one of `["Non-Engineer", "Engineering Adjacent Professional", "New Engineer", "Seasoned Engineering Professional"]`
- [x] Category: one of `["Research Papers and Publications", "Professional Development", "Society Outreach", "Technical Tutorial"]`
- [x] Retries on Bedrock throttling (429) with exponential backoff: 1s, 2s, 4s (max 3 attempts)
- [x] Handles invalid JSON response: retry once with explicit JSON instruction appended
- [x] Returns structured result with all fields plus `processing_time_ms`
- [x] Unit tests with mocked Bedrock responses (success, throttle, invalid JSON, timeout)

## Event Formats

### Direct Text Invocation

```json
{
  "text": "Extracted document text...",
  "thesaurus_terms": ["smart grid", "machine learning"]
}
```

### S3 Metadata Reference

```json
{
  "bucket": "dev-ieee-conference-cloud-bulk-uploads",
  "key": "PES/metadata/doc.pdf.json"
}
```

Reads `extractedText` field from the referenced S3 JSON file.

## Response Format

```json
{
  "statusCode": 200,
  "body": {
    "abstract": "First paragraph...\n\nSecond paragraph...",
    "keywords": ["term1", "term2", "..."],
    "learning_level": "Expert",
    "intended_audience": "Seasoned Engineering Professional",
    "category": "Research Papers and Publications",
    "processing_time_ms": 6774
  }
}
```

---

## Test Cases

### TC-1: Direct Text — Technical Report

**Purpose:** Verify metadata generation from directly provided technical text.

**Invoke:**
```bash
./scripts/invoke-bedrock.sh --text "This IEEE technical report presents findings on grid-interactive efficient buildings and their role in enhancing electric service resilience. The task force examines how buildings can serve as flexible resources for the power grid through demand response, energy storage, and distributed generation."
```

**Expected:**
- `statusCode: 200`
- `abstract`: Two paragraphs, each 50-150 words
- `keywords`: 8-12 relevant terms
- `learning_level`: One of the valid values
- `intended_audience`: One of the valid values
- `category`: One of the valid values
- `processing_time_ms`: Populated

---

### TC-2: Direct Text with Thesaurus Terms

**Purpose:** Verify thesaurus terms are prioritized in keyword selection.

**Invoke:**
```bash
aws lambda invoke \
    --function-name ieee-cc-bedrock-inference \
    --region us-east-1 \
    --payload '{"text":"This paper discusses machine learning for predictive maintenance in smart grid infrastructure.","thesaurus_terms":["smart grid","machine learning","predictive maintenance","power transformers"]}' \
    --cli-read-timeout 120 \
    --profile ieee-cc \
    /tmp/bedrock-thesaurus.json && python3 -m json.tool /tmp/bedrock-thesaurus.json
```

**Expected:**
- `statusCode: 200`
- `keywords` array should include provided thesaurus terms where relevant
- Other fields valid

---

### TC-3: S3 Metadata Reference

**Purpose:** Verify reading extracted text from an S3 JSON file.

**Invoke:**
```bash
./scripts/invoke-bedrock.sh dev-ieee-conference-cloud-bulk-uploads PES/metadata/doc.pdf.json
```

**Expected:**
- `statusCode: 200` if the JSON contains `extractedText`
- `statusCode: 400` with "No extractedText" error if the field is missing

---

### TC-4: Empty Text

**Purpose:** Verify rejection of empty text input.

**Invoke:**
```bash
aws lambda invoke \
    --function-name ieee-cc-bedrock-inference \
    --region us-east-1 \
    --payload '{"text":""}' \
    --profile ieee-cc \
    /tmp/bedrock-empty.json && python3 -m json.tool /tmp/bedrock-empty.json
```

**Expected:**
- `statusCode: 400`
- Error: "text must be a non-empty string"

---

### TC-5: Missing Text Field

**Purpose:** Verify rejection of events with no text field.

**Invoke:**
```bash
aws lambda invoke \
    --function-name ieee-cc-bedrock-inference \
    --region us-east-1 \
    --payload '{}' \
    --profile ieee-cc \
    /tmp/bedrock-missing.json && python3 -m json.tool /tmp/bedrock-missing.json
```

**Expected:**
- `statusCode: 400`
- Error about missing text/bucket+key

---

### TC-6: Long Text (Truncation)

**Purpose:** Verify text exceeding 180,000 characters is truncated before sending to Bedrock.

**Notes:** Text is truncated to `TEXT_TRUNCATION_LIMIT = 180,000` characters to fit within Claude's context window. This is handled internally — the caller does not need to pre-truncate.

---

## AWS Live Test Results

### Test 1: Direct Text — Technical Report
- **Date:** 2026-03-18
- **Result:** PASSED
- **Response:**
  ```
  statusCode: 200
  abstract: 2 paragraphs (valid length)
  keywords: 12 terms (grid-interactive efficient buildings, demand response, building energy management systems, ...)
  learning_level: Expert
  intended_audience: Seasoned Engineering Professional
  category: Research Papers and Publications
  processing_time_ms: 6,774
  ```

### Test 2: Direct Text with Thesaurus Terms
- **Date:** 2026-03-18
- **Result:** PASSED
- **Response:**
  ```
  statusCode: 200
  keywords: 12 terms — thesaurus terms prioritized (smart grid, machine learning, predictive maintenance, deep learning, anomaly detection, power transformers, ...)
  learning_level: Expert
  category: Research Papers and Publications
  processing_time_ms: 16,097
  ```
- **Verification:** All 6 provided thesaurus terms appeared in the keywords where relevant

### Test 3: Empty Text
- **Date:** 2026-03-18
- **Result:** PASSED
- **Response:** `statusCode: 400`, `error: "text must be a non-empty string"`

### Test 4: S3 Reference without extractedText
- **Date:** 2026-03-18
- **Result:** PASSED (expected error)
- **Response:** `statusCode: 400`, `error: "No extractedText in s3://..."`

### Results Summary

| # | Test Case | Status | Processing Time |
|---|-----------|--------|----------------|
| 1 | Direct text — technical report | PASSED | 6,774ms |
| 2 | Direct text with thesaurus terms | PASSED | 16,097ms |
| 3 | Empty text rejection | PASSED | N/A |
| 4 | S3 reference without extractedText | PASSED | N/A |

---

## Unit Tests

| Test File | Tests | Description |
|-----------|-------|-------------|
| `tests/ai/test_bedrock_inference.py` | 25 | Bedrock calls, retries, JSON parsing, validation, thesaurus |
| `tests/handlers/test_bedrock_handler.py` | 9 | Direct invocation, S3 invocation, error handling |
| **Total** | **34** | All passing |

### Test Classes

| Class | Tests | Coverage |
|-------|-------|----------|
| `TestGenerateMetadata` | 3 | Successful generation, text truncation, thesaurus append |
| `TestRetries` | 4 | Throttle retry with backoff, max retries exceeded, invalid JSON retry, non-retryable error |
| `TestBuildMessages` | 3 | Message construction, system prompt, thesaurus context |
| `TestValidation` | 9 | Abstract paragraphs, word count, keywords count, learning level, audience, category |
| `TestProcessingTime` | 1 | processing_time_ms populated |
| `TestDirectInvocation` | 3 | Success, thesaurus terms, empty text |
| `TestS3Invocation` | 2 | S3 text read, empty extracted text |
| `TestErrorHandling` | 3 | Validation 422, Bedrock 500, unexpected 500 |

### Run Tests

```bash
# All Bedrock tests
python -m pytest tests/ai/test_bedrock_inference.py tests/handlers/test_bedrock_handler.py -v

# Single test class
python -m pytest tests/ai/test_bedrock_inference.py::TestRetries -v
```

## Error Handling

| Status | Condition |
|--------|-----------|
| 400 | Empty/missing text, missing S3 fields, no extractedText in S3 JSON |
| 422 | Bedrock returned invalid metadata (wrong field values, bad counts) |
| 500 | Bedrock API errors, throttle exhaustion, unexpected errors |

## Retry Logic

| Scenario | Behavior |
|----------|----------|
| Bedrock throttling (429) | Exponential backoff: 1s, 2s, 4s — max 3 attempts |
| Invalid JSON response | Retry once with explicit JSON instruction appended to prompt |
| Non-retryable errors | Fail immediately |

## Configuration

| Env Var | Default | Description |
|---------|---------|-------------|
| `BEDROCK_MODEL_ID` | `global.anthropic.claude-sonnet-4-6` | Bedrock model inference profile |
| `LOG_LEVEL` | `INFO` | Logging level |
