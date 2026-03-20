# QA Testing Guide — Error Handling, Retry Logic, and DLQ Processing (CC3-781)

## Overview

This ticket introduces shared error handling infrastructure (`src/common/`) and a new DLQ Processor Lambda (`ieee-rc-dlq-processor`) for the IEEE CC pipeline. The shared module provides:

- **Custom exception hierarchy** — `PipelineError` base with 5 domain errors (`TranscribeError`, `BedrockError`, `WebhookError`, `S3Error`, `ValidationError`), each with a class-level `is_retriable` default
- **`@with_retry` decorator** — Exponential or fixed backoff, configurable exceptions, `on_retry` callback
- **Structured error responses** — `build_error_response()` with correlation IDs, timestamps, stack traces, and auto-detected HTTP status codes
- **JSON structured logger** — `get_json_logger()` outputs one JSON object per line, compatible with CloudWatch Logs Insights
- **DLQ message builder** — `build_dlq_message()` formats failed events for SQS dead-letter queues
- **DLQ Processor Lambda** — Reads from SQS DLQ, re-invokes the orchestrator for retriable errors, archives permanently failed messages to S3, and sends SNS alerts

**Scope boundary:** The shared modules are created but existing modules (bedrock, orchestrator, webhook) are NOT refactored to use them. Adoption is gradual.

## Prerequisites

- AWS CLI configured with `ieee-cc` profile
- Access to `dev-ieee-conference-cloud-bulk-uploads` S3 bucket
- AI Orchestrator Lambda deployed (`ieee-rc-ai-orchestrator`)
- SQS queue `ieee-rc-processing-dlq` created
- SNS topic `ieee-rc-processing-failures` created with appropriate subscriptions (email/Slack)

## Lambda Details

| Property | Value |
|----------|-------|
| Function Name | `ieee-rc-dlq-processor` |
| Runtime | Python 3.13 (Docker image) |
| Memory | 256 MB |
| Timeout | 60s |
| ECR Repo | `ieee-rc-dlq-processor` |
| IAM Role | `ieee-rc-dlq-processor-role` |
| SQS Trigger | `ieee-rc-processing-dlq` (batch size 1) |

## Acceptance Criteria Checklist

- [x] AC #1 — `@with_retry` decorator with exponential backoff (`base_delay * 2^attempt`), fixed delays, and `on_retry` callback
- [x] AC #2 — `PipelineError` hierarchy with `is_retriable` class defaults: `TranscribeError` (True), `BedrockError` (True), `WebhookError` (True), `S3Error` (True), `ValidationError` (False)
- [x] AC #3 — `build_error_response()` returns structured dict with `statusCode`, `error_type`, `error_message`, `correlation_id`, `timestamp`, `stack_trace`
- [x] AC #4 — `build_dlq_message()` formats `original_event`, `error`, and `retry_count` for SQS
- [x] AC #5 — `DLQProcessor` re-invokes orchestrator for retriable errors (max 2 reprocess attempts), archives + notifies for permanent failures
- [x] AC #6 — `get_json_logger()` outputs JSON lines with `timestamp`, `level`, `logger`, `message`, optional `correlation_id`, `error_type`, and extras
- [x] AC #7 — Retry decorator tests: success on first attempt, exponential delays (1s, 2s, 4s), fixed delays, max attempts exhaustion, unconfigured exception passthrough, `on_retry` callback, `functools.wraps` preservation

## New Modules

### `src/common/exceptions.py` — Exception Hierarchy

| Exception | `error_type` | `is_retriable` | Use Case |
|-----------|-------------|----------------|----------|
| `PipelineError` | `PipelineError` | `False` | Base class |
| `TranscribeError` | `TranscribeError` | `True` | AWS Transcribe failures |
| `BedrockError` | `BedrockError` | `True` | Bedrock/Claude failures |
| `WebhookError` | `WebhookError` | `True` | Webhook delivery failures |
| `S3Error` | `S3Error` | `True` | S3 read/write failures |
| `ValidationError` | `ValidationError` | `False` | Input validation failures |

All exceptions accept `is_retriable` override per instance: `BedrockError("invalid JSON", is_retriable=False)`.

### `src/common/retry.py` — `@with_retry` Decorator

```python
@with_retry(max_attempts=3, base_delay=1.0, exceptions=[ClientError])
def call_bedrock():
    ...
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `max_attempts` | `int` | `3` | Total attempts including first call |
| `base_delay` | `float` | `1.0` | Base delay for exponential backoff |
| `exceptions` | `Sequence[type]` | `(Exception,)` | Exception types to catch |
| `fixed_delays` | `Sequence[float]` | `None` | Use exact delays instead of exponential |
| `on_retry` | `Callable` | `None` | Callback: `fn(attempt, exception, delay)` |

### `src/common/error_handler.py` — Status Code Logic

| Exception Type | Status Code |
|----------------|-------------|
| `ValidationError`, `ValueError`, `KeyError` | 400 |
| Retriable `PipelineError` | 502 |
| All other exceptions | 500 |
| Override via `status_code` param | Any |

### `src/common/logging.py` — JSON Output Format

```json
{"timestamp": "2026-03-20T12:00:00+00:00", "level": "INFO", "logger": "src.dlq.dlq_processor", "message": "Reprocessing message (attempt 1): req-123", "correlation_id": "req-123"}
```

### `src/common/dlq.py` — DLQ Message Format

```json
{
  "original_event": {"bucket": "...", "key": "..."},
  "error": {
    "error_type": "BedrockError",
    "error_message": "ThrottlingException",
    "correlation_id": "req-123",
    "timestamp": "2026-03-20T12:00:00+00:00",
    "stack_trace": "Traceback ..."
  },
  "retry_count": 0
}
```

---

## DLQ Processor Decision Logic

```
SQS Message → Parse body as DLQ message
  ├── error.is_retriable == true AND retry_count < 2:
  │     → Re-invoke ieee-rc-ai-orchestrator (async) with is_retry=True, retry_count=N+1
  │     → Return {"action": "reprocessed"}
  │
  ├── error.is_retriable == false OR retries exhausted:
  │     → Archive to s3://{bucket}/failed/{correlation_id}/{timestamp}.json
  │     → Publish SNS alert to FAILURES_SNS_TOPIC_ARN
  │     → Return {"action": "archived"}
  │
  └── Invalid message (bad JSON, missing body):
        → Treat as permanent → archive + notify
```

The DLQ processor uses the `is_retriable` boolean flag embedded in each DLQ message (set by `build_dlq_message()` from the exception's `is_retriable` attribute). This decouples the processor from specific error type names.

**Retriable (is_retriable=True):** `TranscribeError`, `BedrockError`, `WebhookError`, `S3Error`
**Permanent (is_retriable=False):** `ValidationError`, unknown/generic exceptions, or any error with `retry_count >= 2`

### Environment Variables

| Env Var | Default | Description |
|---------|---------|-------------|
| `ORCHESTRATOR_FUNCTION_NAME` | `ieee-rc-ai-orchestrator` | Lambda to re-invoke |
| `ARCHIVE_BUCKET` | `dev-ieee-conference-cloud-bulk-uploads` | S3 bucket for failed message archive |
| `FAILURES_SNS_TOPIC_ARN` | _(none)_ | SNS topic for permanent failure alerts |
| `LOG_LEVEL` | `INFO` | Logging level |

---

## Test Cases

### TC-1: Retriable Error — First Retry

**Purpose:** Verify DLQ processor re-invokes the orchestrator for a retriable error with retry_count < 2.

**Invoke:**
```bash
aws lambda invoke \
    --function-name ieee-rc-dlq-processor \
    --region us-east-1 \
    --payload '{
      "Records": [{
        "messageId": "test-msg-001",
        "body": "{\"original_event\":{\"bucket\":\"dev-ieee-conference-cloud-bulk-uploads\",\"key\":\"PES/pending/STD-12345.pdf\"},\"error\":{\"error_type\":\"BedrockError\",\"error_message\":\"ThrottlingException\",\"correlation_id\":\"req-test-001\",\"timestamp\":\"2026-03-20T00:00:00+00:00\",\"stack_trace\":\"Traceback ...\"},\"retry_count\":0}"
      }]
    }' \
    --profile ieee-cc \
    /tmp/dlq-retry.json && python3 -m json.tool /tmp/dlq-retry.json
```

**Expected:**
- `batchItemFailures: []`
- `results[0].action: "reprocessed"`
- CloudWatch logs for `ieee-rc-ai-orchestrator` show a new invocation with `is_retry: true`, `retry_count: 1`

---

### TC-2: Retriable Error — Retries Exhausted

**Purpose:** Verify DLQ processor archives when retry_count reaches the max (2).

**Invoke:**
```bash
aws lambda invoke \
    --function-name ieee-rc-dlq-processor \
    --region us-east-1 \
    --payload '{
      "Records": [{
        "messageId": "test-msg-002",
        "body": "{\"original_event\":{\"bucket\":\"dev-ieee-conference-cloud-bulk-uploads\",\"key\":\"PES/pending/STD-12345.pdf\"},\"error\":{\"error_type\":\"BedrockError\",\"error_message\":\"ThrottlingException\",\"correlation_id\":\"req-test-002\",\"timestamp\":\"2026-03-20T00:00:00+00:00\",\"stack_trace\":\"Traceback ...\"},\"retry_count\":2}"
      }]
    }' \
    --profile ieee-cc \
    /tmp/dlq-exhausted.json && python3 -m json.tool /tmp/dlq-exhausted.json
```

**Expected:**
- `results[0].action: "archived"`
- S3 object created at `s3://dev-ieee-conference-cloud-bulk-uploads/failed/req-test-002/<timestamp>.json`
- SNS notification published to `FAILURES_SNS_TOPIC_ARN` (if set)

**Verify S3 archive:**
```bash
aws s3 ls s3://dev-ieee-conference-cloud-bulk-uploads/failed/req-test-002/ --profile ieee-cc
```

---

### TC-3: Permanent Error — No Retry

**Purpose:** Verify DLQ processor immediately archives non-retriable errors.

**Invoke:**
```bash
aws lambda invoke \
    --function-name ieee-rc-dlq-processor \
    --region us-east-1 \
    --payload '{
      "Records": [{
        "messageId": "test-msg-003",
        "body": "{\"original_event\":{\"bucket\":\"dev-ieee-conference-cloud-bulk-uploads\",\"key\":\"PES/pending/BAD.pdf\"},\"error\":{\"error_type\":\"ValidationError\",\"error_message\":\"Missing required field: product_part_number\",\"correlation_id\":\"req-test-003\",\"timestamp\":\"2026-03-20T00:00:00+00:00\",\"stack_trace\":\"Traceback ...\"},\"retry_count\":0}"
      }]
    }' \
    --profile ieee-cc \
    /tmp/dlq-permanent.json && python3 -m json.tool /tmp/dlq-permanent.json
```

**Expected:**
- `results[0].action: "archived"` (not "reprocessed")
- No invocation of `ieee-rc-ai-orchestrator`
- S3 archive created under `failed/req-test-003/`
- SNS notification sent

---

### TC-4: Invalid Message Format

**Purpose:** Verify DLQ processor handles malformed SQS messages gracefully.

**Invoke:**
```bash
aws lambda invoke \
    --function-name ieee-rc-dlq-processor \
    --region us-east-1 \
    --payload '{
      "Records": [{
        "messageId": "test-msg-004",
        "body": "this is not json"
      }]
    }' \
    --profile ieee-cc \
    /tmp/dlq-invalid.json && python3 -m json.tool /tmp/dlq-invalid.json
```

**Expected:**
- `results[0].action: "archived"` (treated as permanent failure)
- No crash, no `batchItemFailures`
- S3 archive created under `failed/unknown/`

---

### TC-5: Batch Processing — Multiple Records

**Purpose:** Verify DLQ processor handles multiple SQS records in a single batch.

**Invoke:**
```bash
aws lambda invoke \
    --function-name ieee-rc-dlq-processor \
    --region us-east-1 \
    --payload '{
      "Records": [
        {
          "messageId": "test-msg-005a",
          "body": "{\"original_event\":{\"bucket\":\"test\",\"key\":\"a.pdf\"},\"error\":{\"error_type\":\"BedrockError\",\"error_message\":\"throttled\",\"correlation_id\":\"req-a\",\"timestamp\":\"2026-03-20T00:00:00+00:00\",\"stack_trace\":\"\"},\"retry_count\":0}"
        },
        {
          "messageId": "test-msg-005b",
          "body": "{\"original_event\":{\"bucket\":\"test\",\"key\":\"b.pdf\"},\"error\":{\"error_type\":\"ValidationError\",\"error_message\":\"bad input\",\"correlation_id\":\"req-b\",\"timestamp\":\"2026-03-20T00:00:00+00:00\",\"stack_trace\":\"\"},\"retry_count\":0}"
        }
      ]
    }' \
    --profile ieee-cc \
    /tmp/dlq-batch.json && python3 -m json.tool /tmp/dlq-batch.json
```

**Expected:**
- `results` has 2 entries
- First: `action: "reprocessed"` (retriable BedrockError)
- Second: `action: "archived"` (permanent ValidationError)
- `batchItemFailures: []`

---

### TC-6: SNS Alert Content

**Purpose:** Verify SNS notification contains actionable error summary.

**Prerequisites:**
- `FAILURES_SNS_TOPIC_ARN` set on the Lambda
- Email/Slack subscription confirmed on the topic

**Invoke:** Use TC-2 or TC-3.

**Expected SNS message:**
```json
{
  "correlation_id": "req-test-002",
  "error_type": "BedrockError",
  "error_message": "ThrottlingException",
  "retry_count": 2,
  "archive_key": "failed/req-test-002/<timestamp>.json"
}
```

---

### TC-7: Sample Invocation Script

**Purpose:** Verify the invoke script works with the sample DLQ event.

**Invoke:**
```bash
./scripts/invoke-dlq-processor.sh
```

**Expected:**
- Lambda invoked successfully
- Response includes `batchItemFailures` and `results`
- Sample BedrockError with `retry_count: 0` → `action: "reprocessed"`

---

### TC-8: Missing SNS Topic — Graceful Degradation

**Purpose:** Verify DLQ processor archives to S3 even when SNS topic is not configured.

**Setup:** Ensure `FAILURES_SNS_TOPIC_ARN` is NOT set on the Lambda.

**Invoke:** Use TC-2 (retries exhausted).

**Expected:**
- `action: "archived"`
- S3 archive created successfully
- CloudWatch log: `"FAILURES_SNS_TOPIC_ARN not set — skipping SNS alert"`
- No crash

---

## Unit Tests

| Test File | Tests | Description |
|-----------|-------|-------------|
| `tests/common/test_exceptions.py` | 21 | Exception hierarchy, `is_retriable` defaults, overrides, inheritance |
| `tests/common/test_retry.py` | 8 | Decorator: success, retries, backoff, exhaustion, callback, `functools.wraps` |
| `tests/common/test_error_handler.py` | 13 | Status codes (400/500/502), body fields, stack trace truncation |
| `tests/common/test_logging.py` | 8 | JSON output, correlation_id, error_type, extras, exception info, handler dedup |
| `tests/dlq/test_dlq_processor.py` | 14 | DLQ processor + handler: reprocess, archive, SNS, invalid messages, batch failures |
| **Total** | **64** | All passing |

### Test Output

```
tests/common/test_error_handler.py::TestStatusCodes::test_validation_error_returns_400 PASSED
tests/common/test_error_handler.py::TestStatusCodes::test_value_error_returns_400 PASSED
tests/common/test_error_handler.py::TestStatusCodes::test_key_error_returns_400 PASSED
tests/common/test_error_handler.py::TestStatusCodes::test_retriable_pipeline_error_returns_502 PASSED
tests/common/test_error_handler.py::TestStatusCodes::test_generic_exception_returns_500 PASSED
tests/common/test_error_handler.py::TestStatusCodes::test_non_retriable_pipeline_error_returns_500 PASSED
tests/common/test_error_handler.py::TestStatusCodes::test_status_code_override PASSED
tests/common/test_error_handler.py::TestBody::test_contains_required_fields PASSED
tests/common/test_error_handler.py::TestBody::test_generic_exception_uses_class_name PASSED
tests/common/test_error_handler.py::TestBody::test_timestamp_is_iso_format PASSED
tests/common/test_error_handler.py::TestBody::test_empty_correlation_id_by_default PASSED
tests/common/test_error_handler.py::TestStackTrace::test_stack_trace_present PASSED
tests/common/test_error_handler.py::TestStackTrace::test_stack_trace_truncated_at_2000_chars PASSED
tests/common/test_exceptions.py::TestPipelineError::test_message PASSED
tests/common/test_exceptions.py::TestPipelineError::test_default_not_retriable PASSED
tests/common/test_exceptions.py::TestPipelineError::test_override_retriable PASSED
tests/common/test_exceptions.py::TestPipelineError::test_default_error_type PASSED
tests/common/test_exceptions.py::TestPipelineError::test_details_default_empty PASSED
tests/common/test_exceptions.py::TestPipelineError::test_details_passed PASSED
tests/common/test_exceptions.py::TestPipelineError::test_is_exception PASSED
tests/common/test_exceptions.py::TestTranscribeError::test_retriable_by_default PASSED
tests/common/test_exceptions.py::TestTranscribeError::test_error_type PASSED
tests/common/test_exceptions.py::TestTranscribeError::test_override_not_retriable PASSED
tests/common/test_exceptions.py::TestTranscribeError::test_is_pipeline_error PASSED
tests/common/test_exceptions.py::TestBedrockError::test_retriable_by_default PASSED
tests/common/test_exceptions.py::TestBedrockError::test_error_type PASSED
tests/common/test_exceptions.py::TestBedrockError::test_override_not_retriable PASSED
tests/common/test_exceptions.py::TestWebhookError::test_retriable_by_default PASSED
tests/common/test_exceptions.py::TestWebhookError::test_error_type PASSED
tests/common/test_exceptions.py::TestS3Error::test_retriable_by_default PASSED
tests/common/test_exceptions.py::TestS3Error::test_error_type PASSED
tests/common/test_exceptions.py::TestValidationError::test_not_retriable_by_default PASSED
tests/common/test_exceptions.py::TestValidationError::test_error_type PASSED
tests/common/test_exceptions.py::TestValidationError::test_is_pipeline_error PASSED
tests/common/test_logging.py::TestJsonFormatter::test_output_is_valid_json PASSED
tests/common/test_logging.py::TestJsonFormatter::test_includes_correlation_id PASSED
tests/common/test_logging.py::TestJsonFormatter::test_includes_error_type PASSED
tests/common/test_logging.py::TestJsonFormatter::test_includes_extras PASSED
tests/common/test_logging.py::TestJsonFormatter::test_includes_exception_info PASSED
tests/common/test_logging.py::TestGetJsonLogger::test_returns_logger_with_json_handler PASSED
tests/common/test_logging.py::TestGetJsonLogger::test_sets_level PASSED
tests/common/test_logging.py::TestGetJsonLogger::test_does_not_duplicate_handlers PASSED
tests/common/test_retry.py::TestSuccessOnFirstAttempt::test_no_sleep_when_succeeds_immediately PASSED
tests/common/test_retry.py::TestRetryAndRecover::test_retries_on_configured_exception_and_succeeds PASSED
tests/common/test_retry.py::TestExponentialBackoff::test_delays_are_exponential PASSED
tests/common/test_retry.py::TestFixedDelays::test_uses_fixed_delays PASSED
tests/common/test_retry.py::TestExhausted::test_raises_after_max_attempts PASSED
tests/common/test_retry.py::TestUnconfiguredException::test_does_not_retry_unconfigured_exception PASSED
tests/common/test_retry.py::TestOnRetryCallback::test_callback_invoked_with_attempt_exc_delay PASSED
tests/common/test_retry.py::TestFunctools::test_wraps_preserves_name_and_docstring PASSED
tests/dlq/test_dlq_processor.py::TestRetriableReprocess::test_reinvokes_orchestrator_when_retriable PASSED
tests/dlq/test_dlq_processor.py::TestRetriableReprocess::test_reinvokes_with_custom_function_name PASSED
tests/dlq/test_dlq_processor.py::TestRetriableExhausted::test_archives_when_retries_exhausted PASSED
tests/dlq/test_dlq_processor.py::TestPermanentError::test_archives_immediately_for_validation_error PASSED
tests/dlq/test_dlq_processor.py::TestPermanentError::test_archives_immediately_for_webhook_error PASSED
tests/dlq/test_dlq_processor.py::TestArchiveDetails::test_s3_key_contains_correlation_id PASSED
tests/dlq/test_dlq_processor.py::TestArchiveDetails::test_sns_notification_contains_error_summary PASSED
tests/dlq/test_dlq_processor.py::TestArchiveDetails::test_skips_sns_when_topic_not_set PASSED
tests/dlq/test_dlq_processor.py::TestInvalidMessage::test_archives_invalid_json PASSED
tests/dlq/test_dlq_processor.py::TestInvalidMessage::test_archives_missing_body PASSED
tests/dlq/test_dlq_processor.py::TestRetriableErrorTypes::test_retriable_types PASSED
tests/dlq/test_dlq_processor.py::TestRetriableErrorTypes::test_non_retriable_types PASSED
tests/dlq/test_dlq_processor.py::TestDLQHandler::test_processes_multiple_records PASSED
tests/dlq/test_dlq_processor.py::TestDLQHandler::test_partial_batch_failure PASSED

64 passed in 0.24s
```

Full suite: **293 tests, all passing** (no regressions).

### Run Tests

```bash
# All new tests
python -m pytest tests/common/ tests/dlq/ -v

# Shared common module only
python -m pytest tests/common/ -v

# DLQ processor only
python -m pytest tests/dlq/test_dlq_processor.py -v

# Retry decorator only
python -m pytest tests/common/test_retry.py -v

# Full regression suite
python -m pytest tests/ -v
```

## AWS Resources Required for Deployment

| Resource | Name | Notes |
|----------|------|-------|
| ECR Repo | `ieee-rc-dlq-processor` | DLQ processor Docker image |
| Lambda | `ieee-rc-dlq-processor` | 256 MB, 60s timeout, Python 3.13 |
| IAM Role | `ieee-rc-dlq-processor-role` | Lambda invoke + S3 write + SNS publish + SQS receive |
| SQS Queue | `ieee-rc-processing-dlq` | Dead-letter queue (must be created separately) |
| SNS Topic | `ieee-rc-processing-failures` | Permanent failure alerts |
| S3 Path | `failed/{correlation_id}/{timestamp}.json` | Archived failed messages |

### IAM Permissions (ieee-rc-dlq-processor-role)

| Action | Resource |
|--------|----------|
| `lambda:InvokeFunction` | `ieee-rc-ai-orchestrator` |
| `s3:PutObject` | `dev-ieee-conference-cloud-bulk-uploads/failed/*` |
| `sns:Publish` | `ieee-rc-processing-failures` topic |
| `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes` | `ieee-rc-processing-dlq` queue |

## Deploy Commands

```bash
# First-time full deploy
./scripts/deploy-dlq-processor.sh

# Rebuild + update code only
./scripts/deploy-dlq-processor.sh update

# Test invocation with sample DLQ event
./scripts/invoke-dlq-processor.sh

# Teardown
./scripts/teardown-dlq-processor.sh
```

## Known Considerations

1. **Scope boundary:** Existing modules (bedrock, orchestrator, webhook) continue using their inline retry logic. The shared `src/common/` module is available for gradual adoption — no existing behavior is changed.
2. **SQS queue must exist before deploy:** The deploy script creates an event source mapping from `ieee-rc-processing-dlq` to the Lambda. The queue itself must be created separately.
3. **SNS topic is optional:** If `FAILURES_SNS_TOPIC_ARN` is not set, the processor still archives to S3 and logs a warning — it degrades gracefully.
4. **Batch size 1:** The SQS event source mapping uses batch size 1. This simplifies debugging and ensures each message is processed independently. Can be increased later if throughput requires it.
5. **Async re-invocation:** The orchestrator is invoked with `InvocationType: "Event"` (async). This means the DLQ processor does not wait for the orchestrator to complete — it fires and returns.
6. **Max reprocess attempts = 2:** After 2 re-invocations via DLQ, the message is permanently archived. This prevents infinite retry loops.
