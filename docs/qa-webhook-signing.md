# QA Testing Guide — HMAC-SHA256 Webhook Signing (CC3-777)

## Overview

The `WebhookSender` module (`src/webhook/sender.py`) replaces the previous unsigned webhook POST in the AI Orchestrator with HMAC-SHA256 signed delivery. It provides:
- **HMAC-SHA256 signing** via `X-Webhook-Signature` header
- **Retry with exponential backoff** (3 attempts: 2s, 4s, 8s) on 5xx and connection errors
- **No retry on 4xx** (permanent client errors)
- **SNS dead-letter alerting** after exhausting retries

## Prerequisites

- AWS CLI configured with `ieee-cc` profile
- Access to `dev-ieee-conference-cloud-bulk-uploads` S3 bucket
- AI Orchestrator Lambda deployed with updated code
- Environment variables set on the orchestrator Lambda:
  - `DRUPAL_WEBHOOK_SECRET` — shared HMAC secret (must match Drupal)
  - `WEBHOOK_FAILURES_SNS_TOPIC_ARN` — ARN of the `ieee-rc-webhook-failures` SNS topic
- SNS topic `ieee-rc-webhook-failures` created with appropriate subscriptions (email/Slack)

## Test Cases

### TC-1: Signed Webhook — Successful Delivery

**Purpose:** Verify webhook is sent with `X-Webhook-Signature` header and correct payload.

**Setup:**
```bash
cat <<'EOF' | aws s3 cp - s3://dev-ieee-conference-cloud-bulk-uploads/PES/metadata/TEST_WEBHOOK.meta.json
{
  "item_id": "TEST_WEBHOOK",
  "ou": "PES",
  "product_part_number": "TEST_WEBHOOK",
  "ai_enrichment_enabled": true,
  "content": { "media_type": "application/pdf", "filename": "TEST_WEBHOOK.pdf" },
  "webhook_url": "https://httpbin.org/post"
}
EOF

# Upload a test PDF to /pending/
aws s3 cp some-test.pdf s3://dev-ieee-conference-cloud-bulk-uploads/PES/pending/TEST_WEBHOOK.pdf
```

**Invoke:**
```bash
./scripts/invoke-ai-orchestrator.sh dev-ieee-conference-cloud-bulk-uploads PES/pending/TEST_WEBHOOK.pdf
```

**Expected:**
- `action: "enriched"`, `webhook_sent: true`
- httpbin.org response shows `X-Webhook-Signature` header in the echoed request headers
- Header value is a 64-character lowercase hex string (SHA-256 digest)
- `Content-Type: application/json` header present

**Verify signature manually:**
```bash
# After inspecting the httpbin response, verify the signature:
echo -n '<raw JSON body from httpbin response>' | \
  openssl dgst -sha256 -hmac "$DRUPAL_WEBHOOK_SECRET"

# Output should match the X-Webhook-Signature value
```

---

### TC-2: Signature Verification with PHP (Drupal Compatibility)

**Purpose:** Confirm the Python HMAC output matches what PHP `hash_hmac('sha256', ...)` produces.

**Python side:**
```python
import hmac, hashlib, json

secret = "test-shared-secret"
body = json.dumps({"item_id": "TEST", "status": "completed"}).encode()
sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
print(sig)
```

**PHP side (Drupal engineer to verify):**
```php
$secret = 'test-shared-secret';
$body = '{"item_id": "TEST", "status": "completed"}';
$sig = hash_hmac('sha256', $body, $secret);
echo $sig;
// Must match the Python output exactly
```

**Expected:** Both produce the same 64-character lowercase hex digest.

---

### TC-3: Retry on 5xx — Server Error Recovery

**Purpose:** Verify retry logic triggers on 5xx responses.

**Setup:** Use a webhook URL that returns 5xx (e.g. httpbin.org/status/500, or a custom endpoint).

```bash
cat <<'EOF' | aws s3 cp - s3://dev-ieee-conference-cloud-bulk-uploads/PES/metadata/TEST_RETRY.meta.json
{
  "item_id": "TEST_RETRY",
  "ou": "PES",
  "product_part_number": "TEST_RETRY",
  "ai_enrichment_enabled": true,
  "content": { "media_type": "application/pdf", "filename": "TEST_RETRY.pdf" },
  "webhook_url": "https://httpbin.org/status/500"
}
EOF
```

**Expected:**
- CloudWatch logs show 3 retry attempts with backoff (2s, 4s, 8s delays)
- `webhook_sent: false` in response
- SNS alert published to `ieee-rc-webhook-failures` topic (if `WEBHOOK_FAILURES_SNS_TOPIC_ARN` is set)
- Orchestrator **still completes** — file is moved to `/processed/` despite webhook failure

**Verify in CloudWatch:**
```
Webhook HTTP 500 from https://httpbin.org/status/500: ...
Retrying webhook in 2s (attempt 2/3)
Webhook HTTP 500 from https://httpbin.org/status/500: ...
Retrying webhook in 4s (attempt 3/3)
Webhook HTTP 500 from https://httpbin.org/status/500: ...
Webhook to https://httpbin.org/status/500 failed after 3 attempts
Published webhook failure to SNS
```

---

### TC-4: No Retry on 4xx — Permanent Failure

**Purpose:** Verify 4xx responses are treated as permanent and not retried.

**Setup:**
```bash
cat <<'EOF' | aws s3 cp - s3://dev-ieee-conference-cloud-bulk-uploads/PES/metadata/TEST_4XX.meta.json
{
  "item_id": "TEST_4XX",
  "ou": "PES",
  "product_part_number": "TEST_4XX",
  "ai_enrichment_enabled": true,
  "content": { "media_type": "application/pdf", "filename": "TEST_4XX.pdf" },
  "webhook_url": "https://httpbin.org/status/401"
}
EOF
```

**Expected:**
- CloudWatch logs show only **1 attempt** (no retries)
- `webhook_sent: false`
- SNS alert published (signature mismatch or auth failure is worth alerting on)
- File still moves to `/processed/`

---

### TC-5: SNS Dead-Letter Alert

**Purpose:** Verify SNS notification is published after exhausted retries.

**Prerequisites:**
- `ieee-rc-webhook-failures` SNS topic created
- Email/Slack subscription confirmed on the topic
- `WEBHOOK_FAILURES_SNS_TOPIC_ARN` env var set on the orchestrator Lambda

**Invoke:** Use TC-3 (5xx endpoint) or TC-4 (4xx endpoint).

**Expected SNS message:**
```json
{
  "url": "https://httpbin.org/status/500",
  "correlation": "[<request_id>:TEST_RETRY]",
  "error": "HTTP 500: ...",
  "payload": {
    "item_id": "TEST_RETRY",
    "ou": "PES",
    "product_part_number": "TEST_RETRY",
    "status": "completed",
    "completed_at": "2026-03-20T...",
    "extraction": { ... },
    "metadata": { ... }
  }
}
```

---

### TC-6: Missing Webhook Secret

**Purpose:** Verify behavior when `DRUPAL_WEBHOOK_SECRET` is not set.

**Expected:**
- Webhook is still sent (with signature computed using empty string as secret)
- Drupal should reject with 401 (signature mismatch)
- No retry on 401
- SNS alert published

**Note:** This is a misconfiguration scenario. The orchestrator logs should make it easy to diagnose.

---

### TC-7: No Webhook URL — Skip Silently

**Purpose:** Verify no webhook is sent when `webhook_url` is absent from `.meta.json`.

**Setup:**
```bash
cat <<'EOF' | aws s3 cp - s3://dev-ieee-conference-cloud-bulk-uploads/PES/metadata/TEST_NOWH.meta.json
{
  "item_id": "TEST_NOWH",
  "ou": "PES",
  "product_part_number": "TEST_NOWH",
  "ai_enrichment_enabled": true,
  "content": { "media_type": "application/pdf", "filename": "TEST_NOWH.pdf" }
}
EOF
```

**Expected:**
- `webhook_sent: false`
- No webhook HTTP calls in CloudWatch logs
- No SNS alert

---

## Unit Test Results

```
tests/webhook/test_sender.py::TestSign::test_sign_produces_correct_hmac PASSED
tests/webhook/test_sender.py::TestSign::test_hmac_matches_drupal_hash_equals PASSED
tests/webhook/test_sender.py::TestHeaders::test_sets_signature_and_content_type_headers PASSED
tests/webhook/test_sender.py::TestSuccessPath::test_success_on_first_attempt PASSED
tests/webhook/test_sender.py::TestRetry::test_retries_on_5xx PASSED
tests/webhook/test_sender.py::TestRetry::test_retries_on_connection_error PASSED
tests/webhook/test_sender.py::TestRetry::test_no_retry_on_400 PASSED
tests/webhook/test_sender.py::TestRetry::test_no_retry_on_401 PASSED
tests/webhook/test_sender.py::TestSNSAlert::test_sns_alert_after_exhausted_retries PASSED
tests/webhook/test_sender.py::TestSNSAlert::test_sns_alert_on_4xx PASSED
tests/webhook/test_sender.py::TestLogging::test_logs_non_200_response PASSED

11 passed in 0.12s
```

Full suite: **229 tests, all passing** (no regressions).

## AWS Resources Required for Deployment

| Resource | Name | Notes |
|----------|------|-------|
| SNS Topic | `ieee-rc-webhook-failures` | Dead-letter topic for failed webhooks |
| Lambda Env Var | `DRUPAL_WEBHOOK_SECRET` | Shared HMAC secret — must match Drupal |
| Lambda Env Var | `WEBHOOK_FAILURES_SNS_TOPIC_ARN` | ARN of the SNS topic above |
| IAM Policy | SNS `Publish` on `ieee-rc-webhook-failures` | Add to `ieee-rc-ai-orchestrator-role` |

## Information Needed from Drupal Engineer

Before end-to-end testing with the real Drupal endpoint, the following must be coordinated:

### 1. Shared HMAC Secret
- Agree on a shared secret value for `DRUPAL_WEBHOOK_SECRET`
- Secret should be generated securely (e.g. `openssl rand -hex 32`)
- Must be set in both the Lambda env var and Drupal's webhook receiver config

### 2. Drupal Webhook Receiver — Signature Verification
Confirm the Drupal endpoint verifies the signature using:
```php
$secret = getenv('WEBHOOK_SECRET'); // or however Drupal stores it
$payload = file_get_contents('php://input');
$expected = hash_hmac('sha256', $payload, $secret);
$received = $_SERVER['HTTP_X_WEBHOOK_SIGNATURE'];

if (!hash_equals($expected, $received)) {
    http_response_code(401);
    exit('Invalid signature');
}
```

### 3. Header Name Confirmation
- We send: `X-Webhook-Signature`
- Confirm Drupal reads this exact header (case-insensitive in HTTP, but verify the PHP `$_SERVER` key: `HTTP_X_WEBHOOK_SIGNATURE`)

### 4. Payload Shape Confirmation
Confirm Drupal expects this payload structure:
```json
{
  "item_id": "STD-12345",
  "ou": "PES",
  "product_part_number": "STD-12345",
  "status": "completed",
  "completed_at": "2026-03-20T12:00:00Z",
  "extraction": { "text": "...", "page_count": 10, "extraction_method": "text" },
  "metadata": { "abstract": "...", "keywords": [...], ... }
}
```

### 5. Error Response Codes
Confirm which HTTP status codes Drupal returns for:
- **Success:** 200 (or 201/204?)
- **Invalid signature:** 401
- **Malformed payload:** 400
- **Server error:** 500

This affects our retry logic (we only retry on 5xx, not 4xx).

### 6. Timeout / Rate Limits
- Does Drupal have a request timeout we should be aware of? (We use 30s)
- Any rate limiting on the webhook endpoint?

## Known Considerations

1. **Retry adds latency:** Worst case, a failing webhook adds ~14s (2+4+8) to the orchestrator processing time. This is within the Lambda 5-min timeout but worth noting.
2. **SNS topic must exist before deployment:** If `WEBHOOK_FAILURES_SNS_TOPIC_ARN` is not set, failures are logged but not alerted — the system degrades gracefully.
3. **Empty secret is valid but insecure:** If `DRUPAL_WEBHOOK_SECRET` is unset, signatures are computed with an empty string. Drupal should reject these as invalid.
