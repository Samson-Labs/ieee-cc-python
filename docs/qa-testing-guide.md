# QA Testing Guide — Bedrock Claude Integration for Metadata Generation (CC3-776)

**Date:** 2026-03-16
**Environment:** AWS Account `141770997341`, Region `us-east-1`
**Profile:** `ieee-cc`

## What Was Implemented

Reusable Python module (`src/ai/bedrock_inference.py`) that takes extracted text (from PDF or transcript), combines it with the IEEE system prompt (v1.2), and calls AWS Bedrock (Claude Sonnet) to generate structured metadata. Returns JSON with abstract, keywords, learning_level, intended_audience, and category. Deployed as a Docker-based Lambda (`ieee-cc-bedrock-inference`).

### Acceptance Criteria

- [x] Calls Bedrock API with model ID `us.anthropic.claude-sonnet-4-5-20250929-v1:0` (configurable via env var `BEDROCK_MODEL_ID`)
- [x] Constructs messages array with system prompt (v1.2) and user message containing extracted text
- [x] Appends thesaurus context to system prompt when provided
- [x] Parses response as JSON, validates all required fields:
  - `abstract`: two paragraphs separated by `\n\n`, each 50–150 words
  - `keywords`: array of 8–12 strings
  - `learning_level`: one of `Foundational`, `Professional`, `Expert`
  - `intended_audience`: one of `Non-Engineer`, `Engineering Adjacent Professional`, `New Engineer`, `Seasoned Engineering Professional`
  - `category`: one of `Research Papers and Publications`, `Professional Development`, `Society Outreach`, `Technical Tutorial`
- [x] Retries on Bedrock throttling (429) with exponential backoff: 1s, 2s, 4s (max 3 attempts)
- [x] Handles invalid JSON response: retry once with explicit JSON instruction appended
- [x] Returns structured result with all fields plus `processing_time_ms`
- [x] Unit tests with mocked Bedrock responses (success, throttle, invalid JSON, timeout)

---

## Prerequisites

- AWS CLI installed and configured with profile `ieee-cc`
- Permissions to invoke Lambda functions and read/write S3
- Set profile for all commands:
  ```bash
  export AWS_PROFILE=ieee-cc
  export AWS_REGION=us-east-1
  ```

---

## Lambda Details

| Setting | Value |
|---------|-------|
| Function Name | `ieee-cc-bedrock-inference` |
| Memory | 512 MB |
| Timeout | 120s |
| Runtime | Python 3.13 (Docker) |
| Model ID | `us.anthropic.claude-sonnet-4-5-20250929-v1:0` |
| S3 Bucket | `dev-ieee-conference-cloud-bulk-uploads` |

Verify the Lambda is active:
```bash
aws lambda get-function --function-name ieee-cc-bedrock-inference --query "Configuration.{State:State,Memory:MemorySize,Timeout:Timeout,Env:Environment.Variables}" --output table
```

---

## Test 1: Direct Text Invocation (Research Paper)

Create a test payload file:
```bash
cat > /tmp/bedrock-test.json << 'EOF'
{
  "text": "Power systems are critical infrastructure for modern society, providing the backbone for economic activity, public safety, and quality of life. As the global energy landscape shifts toward decarbonization, the integration of renewable energy sources such as solar photovoltaics and wind turbines into existing power grids presents significant technical challenges. This paper presents a comprehensive study on advanced load forecasting methodologies for smart grid environments, combining deep learning architectures with traditional statistical approaches to achieve superior prediction accuracy. The research focuses on distribution-level forecasting, where variability from distributed energy resources creates unique modeling challenges not present in bulk power system operations. We propose a hybrid framework that leverages long short-term memory networks alongside gradient boosting methods, trained on three years of smart meter data from a metropolitan utility serving approximately 500,000 customers. The dataset includes granular consumption records at 15-minute intervals, weather observations, calendar features, and economic indicators. Our preprocessing pipeline addresses data quality issues common in advanced metering infrastructure deployments, including missing readings, communication failures, and meter tampering detection. Feature engineering incorporates domain knowledge from power systems engineering, including temperature-load correlations, holiday effects, and industrial production cycles. The experimental evaluation compares our hybrid approach against seven baseline methods including persistence models, ARIMA variants, support vector regression, random forests, and standalone neural network architectures. Results demonstrate that the proposed hybrid framework achieves a mean absolute percentage error of 2.3 percent on day-ahead forecasts, representing a 15 percent improvement over the best individual model. The framework shows particular strength during extreme weather events and demand response periods, where traditional methods typically exhibit degraded performance. Cross-validation across different seasons and customer segments confirms the robustness of the approach. We also present a novel uncertainty quantification method based on conformal prediction that provides calibrated prediction intervals without distributional assumptions. The operational deployment of our system at the partner utility has reduced procurement costs by an estimated 3.2 million dollars annually through more accurate reserve margin calculations. Additionally, improved forecast accuracy has enabled the utility to increase its renewable energy portfolio from 18 to 27 percent while maintaining grid reliability standards. The paper concludes with a discussion of transfer learning approaches that allow the framework to be adapted to new service territories with limited local training data, addressing a key barrier to widespread adoption of machine learning in power system operations."
}
EOF
```

Invoke:
```bash
aws lambda invoke \
  --function-name ieee-cc-bedrock-inference \
  --payload fileb:///tmp/bedrock-test.json \
  /tmp/bedrock-response.json && cat /tmp/bedrock-response.json | python3 -m json.tool
```

### Validation

- [ ] `statusCode` is `200`
- [ ] `abstract` contains exactly two paragraphs separated by `\n\n`
- [ ] Each paragraph is 50–150 words (count manually or use script below)
- [ ] `keywords` is an array of 8–12 strings
- [ ] Keywords are relevant to the input text (power systems, smart grid, etc.)
- [ ] `learning_level` is one of: `Foundational`, `Professional`, `Expert`
- [ ] `intended_audience` is one of: `Non-Engineer`, `Engineering Adjacent Professional`, `New Engineer`, `Seasoned Engineering Professional`
- [ ] `category` is one of: `Research Papers and Publications`, `Professional Development`, `Society Outreach`, `Technical Tutorial`
- [ ] `processing_time_ms` is a positive integer
- [ ] Response returns within 120 seconds (Lambda timeout)

Word count helper:
```bash
python3 -c "
import json
with open('/tmp/bedrock-response.json') as f:
    data = json.load(f)
abstract = data['body']['abstract']
paragraphs = [p.strip() for p in abstract.split('\n\n') if p.strip()]
print(f'Paragraph count: {len(paragraphs)}')
for i, p in enumerate(paragraphs):
    wc = len(p.split())
    status = 'OK' if 50 <= wc <= 150 else 'FAIL'
    print(f'  Paragraph {i+1}: {wc} words [{status}]')
print(f'Keywords count: {len(data[\"body\"][\"keywords\"])}')
print(f'Keywords: {data[\"body\"][\"keywords\"]}')
print(f'Learning level: {data[\"body\"][\"learning_level\"]}')
print(f'Audience: {data[\"body\"][\"intended_audience\"]}')
print(f'Category: {data[\"body\"][\"category\"]}')
print(f'Processing time: {data[\"body\"][\"processing_time_ms\"]}ms')
"
```

---

## Test 2: Direct Text Invocation (Tutorial Content)

Tests that the model correctly classifies non-research content:

```bash
cat > /tmp/bedrock-tutorial.json << 'EOF'
{
  "text": "This tutorial introduces engineers to the fundamentals of electric vehicle charging infrastructure design. Topics covered include AC Level 1 and Level 2 charging, DC fast charging standards including CCS and CHAdeMO, grid interconnection requirements, demand management strategies, and site planning considerations. The guide walks through a complete design example for a commercial charging station, including electrical panel sizing, transformer selection, and load management system configuration. We begin with an overview of the different charging levels and their power requirements. Level 1 charging uses a standard 120V household outlet and provides about 4 to 5 miles of range per hour of charging. Level 2 charging operates at 240V and delivers 12 to 80 miles of range per hour depending on the amperage. DC fast charging can provide 60 to 200 miles of range in just 20 to 30 minutes. Each level has specific electrical requirements and installation considerations that engineers must understand. The tutorial then covers site assessment and planning, including parking lot layout considerations, cable management approaches, and accessibility requirements under the Americans with Disabilities Act. Electrical infrastructure topics include service panel capacity evaluation, transformer sizing calculations, and conduit routing best practices. The demand management section explains how smart charging software can reduce peak demand charges by scheduling charging sessions during off-peak hours and implementing load sharing across multiple chargers. Finally, we present a worked example showing the complete design process for a 10-stall commercial charging station with a mix of Level 2 and DC fast chargers."
}
EOF

aws lambda invoke \
  --function-name ieee-cc-bedrock-inference \
  --payload fileb:///tmp/bedrock-tutorial.json \
  /tmp/bedrock-tutorial-response.json && python3 -c "
import json
with open('/tmp/bedrock-tutorial-response.json') as f:
    data = json.load(f)
b = data['body']
print(f'Status: {data[\"statusCode\"]}')
print(f'Learning level: {b[\"learning_level\"]}')
print(f'Audience: {b[\"intended_audience\"]}')
print(f'Category: {b[\"category\"]}')
print(f'Keywords: {b[\"keywords\"]}')
"
```

### Validation

- [ ] `statusCode` is `200`
- [ ] `learning_level` should be `Foundational` or `Professional` (tutorial content, not expert)
- [ ] `category` should be `Technical Tutorial` or `Professional Development`
- [ ] Keywords should include EV/charging related terms

---

## Test 3: With IEEE Thesaurus Terms

Tests that thesaurus terms are prioritized in keyword selection:

```bash
cat > /tmp/bedrock-thesaurus.json << 'EOF'
{
  "text": "This paper presents a novel approach to cybersecurity in industrial control systems, focusing on intrusion detection for SCADA networks used in power generation facilities. We develop a machine learning based anomaly detection system that monitors network traffic patterns and identifies potential cyber attacks in real time. The system uses a combination of deep packet inspection and behavioral analysis to detect both known attack signatures and zero-day exploits. Testing was conducted on a realistic testbed replicating a 500MW combined cycle power plant control network. Results show a detection rate of 98.7 percent with a false positive rate below 0.1 percent, significantly outperforming existing rule-based intrusion detection systems deployed in the energy sector.",
  "thesaurus_terms": ["SCADA systems", "cybersecurity", "intrusion detection", "power generation", "industrial control systems", "machine learning", "anomaly detection", "network security"]
}
EOF

aws lambda invoke \
  --function-name ieee-cc-bedrock-inference \
  --payload fileb:///tmp/bedrock-thesaurus.json \
  /tmp/bedrock-thesaurus-response.json && cat /tmp/bedrock-thesaurus-response.json | python3 -m json.tool
```

### Validation

- [ ] `statusCode` is `200`
- [ ] Keywords should prioritize terms from the provided thesaurus list (e.g., `SCADA systems`, `cybersecurity`, `intrusion detection`)
- [ ] At least 3–4 keywords should match or closely align with the thesaurus terms
- [ ] All standard field validations pass (abstract format, keyword count, etc.)

---

## Test 4: End-to-End Pipeline (PDF Extraction → Bedrock)

Extract text from a real PDF, then feed it to Bedrock:

```bash
# Step 1: Extract text from a PDF
aws lambda invoke \
  --function-name ieee-cc-pdf-extractor \
  --payload '{"bucket":"dev-ieee-conference-cloud-bulk-uploads","key":"PES/pending/pes_wp_peswfi_wfi_022025.pdf","ou":"PES","product_part_number":"pes_wp_peswfi_wfi_022025"}' \
  /tmp/pdf-output.json

# Step 2: Create Bedrock payload from extracted text
python3 -c "
import json
with open('/tmp/pdf-output.json') as f:
    data = json.load(f)
text = data['body']['text']
print(f'Extracted text: {len(text)} chars, {data[\"body\"][\"page_count\"]} pages')
payload = json.dumps({'text': text[:50000]})
with open('/tmp/bedrock-e2e.json', 'w') as f:
    f.write(payload)
print(f'Payload size: {len(payload)} chars (truncated to 50k for Lambda payload limit)')
"

# Step 3: Generate metadata via Bedrock
aws lambda invoke \
  --function-name ieee-cc-bedrock-inference \
  --payload fileb:///tmp/bedrock-e2e.json \
  /tmp/bedrock-e2e-response.json && python3 -c "
import json
with open('/tmp/bedrock-e2e-response.json') as f:
    data = json.load(f)
b = data['body']
print(f'Status: {data[\"statusCode\"]}')
print(f'Processing time: {b[\"processing_time_ms\"]}ms')
print(f'Learning level: {b[\"learning_level\"]}')
print(f'Audience: {b[\"intended_audience\"]}')
print(f'Category: {b[\"category\"]}')
print(f'Keywords ({len(b[\"keywords\"])}): {b[\"keywords\"]}')
paragraphs = [p.strip() for p in b['abstract'].split('\n\n') if p.strip()]
for i, p in enumerate(paragraphs):
    print(f'Abstract paragraph {i+1}: {len(p.split())} words')
"
```

### Validation

- [ ] PDF extraction succeeds (statusCode 200, non-empty text)
- [ ] Bedrock metadata generation succeeds (statusCode 200)
- [ ] Abstract is relevant to the actual PDF content
- [ ] Keywords match the document's technical domain
- [ ] All field validations pass

---

## Test 5: Error Handling

### Empty text (should return 400)

```bash
aws lambda invoke \
  --function-name ieee-cc-bedrock-inference \
  --payload '{"text": "   "}' \
  /tmp/bedrock-empty.json && cat /tmp/bedrock-empty.json | python3 -m json.tool
```

- [ ] `statusCode` is `400`
- [ ] `body.error` contains `"non-empty"`

### Missing text field (should return 400)

```bash
aws lambda invoke \
  --function-name ieee-cc-bedrock-inference \
  --payload '{"foo": "bar"}' \
  /tmp/bedrock-missing.json && cat /tmp/bedrock-missing.json | python3 -m json.tool
```

- [ ] `statusCode` is `400`
- [ ] `body.error` contains `"text"` or `"bucket"` reference

### Non-existent S3 reference (should return 500)

```bash
aws lambda invoke \
  --function-name ieee-cc-bedrock-inference \
  --payload '{"bucket":"dev-ieee-conference-cloud-bulk-uploads","key":"does/not/exist.json"}' \
  /tmp/bedrock-s3-error.json && cat /tmp/bedrock-s3-error.json | python3 -m json.tool
```

- [ ] `statusCode` is `500`
- [ ] `body.error` contains S3 error message

### Empty event (should return 400)

```bash
aws lambda invoke \
  --function-name ieee-cc-bedrock-inference \
  --payload '{}' \
  /tmp/bedrock-empty-event.json && cat /tmp/bedrock-empty-event.json | python3 -m json.tool
```

- [ ] `statusCode` is `400`

### Error Summary

| Test Case | Expected Status | Expected Error |
|-----------|----------------|----------------|
| Empty text | 400 | "text must be a non-empty string" |
| Missing text field | 400 | "Event must contain 'text' or 'bucket'+'key'" |
| Non-existent S3 key | 500 | S3 NoSuchKey error |
| Empty event | 400 | Missing required fields |

---

## CloudWatch Logs

Check Lambda execution logs:

```bash
aws logs tail /aws/lambda/ieee-cc-bedrock-inference --since 1h --format short
```

---

## Existing Test Results (2026-03-16)

### AWS Live Test — Direct Text (Power Systems Paper)

| Field | Value | Status |
|-------|-------|--------|
| statusCode | 200 | PASS |
| abstract paragraphs | 2 | PASS |
| abstract paragraph 1 words | ~130 | PASS (50–150 range) |
| abstract paragraph 2 words | ~130 | PASS (50–150 range) |
| keywords count | 12 | PASS (8–12 range) |
| learning_level | Expert | PASS |
| intended_audience | Seasoned Engineering Professional | PASS |
| category | Research Papers and Publications | PASS |
| processing_time_ms | 6159 | PASS |

### Keywords Returned

`load forecasting`, `smart grid`, `deep learning`, `long short-term memory`, `gradient boosting`, `renewable energy integration`, `advanced metering infrastructure`, `distributed energy resources`, `conformal prediction`, `demand response`, `transfer learning`, `power system operations`

---

## Unit Tests (34 total, all passing)

Run locally:
```bash
python -m pytest tests/ai/test_bedrock_inference.py tests/handlers/test_bedrock_handler.py -v
```

| Test Suite | Count | Status |
|------------|-------|--------|
| tests/ai/test_bedrock_inference.py | 25 | PASS |
| tests/handlers/test_bedrock_handler.py | 9 | PASS |

### Test Coverage

| Area | Tests |
|------|-------|
| Successful metadata generation | 6 (params, thesaurus, truncation, env model ID, timing) |
| Throttle retry (exponential backoff) | 4 (single retry, backoff timing, max retries, non-throttle error) |
| Invalid JSON retry | 3 (retry success, retry fail, markdown fence stripping) |
| Response validation | 12 (missing fields, abstract format, keyword count, all valid enum values) |
| Handler: direct invocation | 4 (success, thesaurus, empty text, missing text) |
| Handler: S3 invocation | 2 (success, empty extracted text) |
| Handler: error handling | 3 (validation error, Bedrock error, unexpected error) |
