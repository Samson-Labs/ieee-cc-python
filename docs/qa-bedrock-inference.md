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
| Model ID | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` (configurable via `BEDROCK_MODEL_ID`) |

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

### Test 5: IEEE Thesaurus Tool Use — Engineering Content
- **Date:** 2026-04-06
- **Result:** PASSED
- **Response:**
  ```
  statusCode: 200
  keywords: Deep learning, Load forecasting, Deep reinforcement learning, Transfer learning,
            Fault detection, Energy storage, Renewable energy sources, Photovoltaic systems,
            Power systems, Smart grid, Microgrids, Model predictive control
  thesaurus_coverage: 10/12
  processing_time_ms: 13,136
  input_tokens: 5,830
  ```
- **Tool calls:** LLM searched thesaurus for smart grid, deep learning, power systems topics
- **Verification:** 10 of 12 keywords are IEEE Thesaurus preferred terms

### Test 6: IEEE Thesaurus Tool Use — Non-IEEE Content (Economics)
- **Date:** 2026-04-06
- **Result:** PASSED (expected low coverage)
- **Response:**
  ```
  statusCode: 200
  keywords: monetary policy, interest rates, inflation, Consumer Price Index,
            Federal Open Market Committee, interest rate policy, price stability,
            household consumption, central banking, macroeconomics
  thesaurus_coverage: 1/10
  processing_time_ms: 21,017
  input_tokens: 8,176
  ```
- **Tool calls:** LLM searched for monetary policy, consumer price index, macroeconomics topics
- **Verification:** Only "macroeconomics" matched — expected, as IEEE Thesaurus does not cover monetary economics. LLM correctly fell back to domain-appropriate non-thesaurus terms.

### Results Summary

| # | Test Case | Status | Processing Time | Thesaurus Coverage |
|---|-----------|--------|----------------|-------------------|
| 1 | Direct text — technical report | PASSED | 6,774ms | N/A (pre-thesaurus) |
| 2 | Direct text with thesaurus terms | PASSED | 16,097ms | N/A (pre-thesaurus) |
| 3 | Empty text rejection | PASSED | N/A | N/A |
| 4 | S3 reference without extractedText | PASSED | N/A | N/A |
| 5 | Thesaurus tool use — engineering | PASSED | 13,136ms | 10/12 (83%) |
| 6 | Thesaurus tool use — economics | PASSED | 21,017ms | 1/10 (10%) |

---

## Unit Tests

| Test File | Tests | Description |
|-----------|-------|-------------|
| `tests/ai/test_bedrock_inference.py` | 30 | Bedrock calls, retries, JSON parsing, validation, thesaurus, tool use |
| `tests/ai/test_thesaurus.py` | 20 | Thesaurus loading, search, coverage, integration with real data |
| `tests/handlers/test_bedrock_handler.py` | 9 | Direct invocation, S3 invocation, error handling |
| **Total** | **59** | All passing |

### Test Classes (bedrock_inference)

| Class | Tests | Coverage |
|-------|-------|----------|
| `TestGenerateMetadata` | 6 | Successful generation, text truncation, thesaurus append, env model ID, processing time |
| `TestThrottleRetry` | 4 | Throttle retry with backoff, max retries exceeded, exponential backoff, non-retryable error |
| `TestInvalidJsonRetry` | 3 | Invalid JSON retry, retry failure, markdown fence stripping |
| `TestValidation` | 12 | Abstract paragraphs, word count, keywords count, learning level, audience, category, all valid values |
| `TestCloudWatchMetrics` | 3 | Token metrics, accumulation on retry, no metrics without client |
| `TestToolUse` | 7 | Tool in request, tool-use loop, multiple calls, explicit thesaurus skip, coverage in result, zero coverage, max iterations safety |

### Test Classes (thesaurus)

| Class | Tests | Coverage |
|-------|-------|----------|
| `TestLoading` | 3 | Load terms, missing file, empty terms |
| `TestSearch` | 11 | Exact match, case-insensitive, synonym, acronym, multi-word, substring, word-overlap, no match, empty query, limit, scope notes, broader terms |
| `TestIsPreferredTerm` | 4 | Exact match, case-insensitive, synonym not preferred, unknown term |
| `TestCoverage` | 4 | All matched, none matched, partial match, case-insensitive |
| `TestRealThesaurus` | 4 | Loads thousands of terms, finds terms, economics terms, non-IEEE terms not preferred |

### Run Tests

```bash
# All Bedrock + thesaurus tests
python -m pytest tests/ai/ tests/handlers/test_bedrock_handler.py -v

# Tool use tests only
python -m pytest tests/ai/test_bedrock_inference.py::TestToolUse -v

# Thesaurus tests only
python -m pytest tests/ai/test_thesaurus.py -v
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
