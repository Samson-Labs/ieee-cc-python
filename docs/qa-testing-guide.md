# QA Testing Guide — IEEE Content Conversion Pipeline

**Date:** 2026-03-16
**Environment:** AWS Account `141770997341`, Region `us-east-1`
**Profile:** `ieee-cc`

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

## 1. Deployed Lambdas

| Lambda | Memory | Timeout | Status |
|--------|--------|---------|--------|
| `ieee-cc-pdf-extractor` | 3 GB | 5 min | Deployed |
| `ieee-rc-image-generator` | 1024 MB | 60s | Deployed |
| `ieee-cc-bedrock-inference` | 512 MB | 120s | Deployed |

Verify all three are active:
```bash
aws lambda list-functions --query "Functions[?starts_with(FunctionName,'ieee-')].{Name:FunctionName,State:State,Memory:MemorySize,Timeout:Timeout}" --output table
```

---

## 2. PDF Text Extraction (`ieee-cc-pdf-extractor`)

### 2.1 Test Data in S3

| PDF File | Path | Size | Pages | Method |
|----------|------|------|-------|--------|
| PES_TP_Mag_PE_v23_N6_SP.pdf | `PES/pending/` | 11 MB | 202 | text |
| PES_TR_138_SBLCS_011726.pdf | `PES/pending/` | 3.1 MB | 89 | text |
| PES_TR_TR139_ITSLC_012826.pdf | `PES/pending/` | 1.6 MB | 43 | text |
| pes_wp_peswfi_wfi_022025.pdf | `PES/pending/` | 1.5 MB | 34 | text |
| PES_MAG_ELE_13-4.pdf | `PES/pending/` | 73 MB | 92 | text |
| PES_PUB_TP_TP101_101995.pdf | `PES/pending/` | 98 MB | 187 | ocr (scanned) |

### 2.2 Invoke PDF Extractor

```bash
# Test with a text-based PDF
aws lambda invoke \
  --function-name ieee-cc-pdf-extractor \
  --payload '{"bucket":"dev-ieee-conference-cloud-bulk-uploads","key":"PES/pending/PES_TR_TR139_ITSLC_012826.pdf","ou":"PES","product_part_number":"PES_TR_TR139_ITSLC_012826"}' \
  /tmp/pdf-response.json && cat /tmp/pdf-response.json | python3 -m json.tool
```

### 2.3 Expected Results

**Successful text extraction:**
```json
{
  "statusCode": 200,
  "body": {
    "text": "<extracted text content>",
    "page_count": 43,
    "extraction_method": "text"
  }
}
```

**Scanned PDF (no extractable text):**
```json
{
  "statusCode": 200,
  "body": {
    "text": "",
    "page_count": 187,
    "extraction_method": "ocr"
  }
}
```

### 2.4 Validation Checklist

- [ ] `statusCode` is `200`
- [ ] `extraction_method` is `"text"` for regular PDFs, `"ocr"` for scanned PDFs
- [ ] `page_count` matches the actual page count of the PDF
- [ ] `text` is non-empty for text-based PDFs
- [ ] `text` is empty for scanned PDFs (PES_PUB_TP_TP101_101995.pdf)
- [ ] Metadata JSON written to S3: `PES/metadata/<product_part_number>.pdf.json`

### 2.5 Verify Metadata in S3

```bash
aws s3 cp s3://dev-ieee-conference-cloud-bulk-uploads/PES/metadata/PES_TR_TR139_ITSLC_012826.pdf.json - | python3 -m json.tool
```

**Existing metadata results (from 2026-03-11 test run):**

| File | Pages | Method | Extracted At |
|------|-------|--------|-------------|
| PES_TP_Mag_PE_v23_N6_SP.pdf.json | 202 | text | 2026-03-11T11:25:06Z |
| PES_TR_138_SBLCS_011726.pdf.json | 89 | text | 2026-03-11T11:25:10Z |
| PES_TR_TR139_ITSLC_012826.pdf.json | 43 | text | 2026-03-11T11:25:13Z |
| pes_wp_peswfi_wfi_022025.pdf.json | 34 | text | 2026-03-11T11:25:17Z |
| PES_MAG_ELE_13-4.pdf.json | 92 | text | 2026-03-11T11:25:24Z |
| PES_PUB_TP_TP101_101995.pdf.json | 187 | ocr | 2026-03-11T11:25:21Z |

### 2.6 Error Cases

```bash
# Non-existent file — should return 500 with S3 NoSuchKey
aws lambda invoke \
  --function-name ieee-cc-pdf-extractor \
  --payload '{"bucket":"dev-ieee-conference-cloud-bulk-uploads","key":"PES/pending/DOES_NOT_EXIST.pdf","ou":"PES","product_part_number":"DOES_NOT_EXIST"}' \
  /tmp/pdf-error.json && cat /tmp/pdf-error.json | python3 -m json.tool

# Missing required fields — should return 400
aws lambda invoke \
  --function-name ieee-cc-pdf-extractor \
  --payload '{"bucket":"dev-ieee-conference-cloud-bulk-uploads"}' \
  /tmp/pdf-bad-request.json && cat /tmp/pdf-bad-request.json | python3 -m json.tool
```

---

## 3. Bedrock Metadata Generation (`ieee-cc-bedrock-inference`)

### 3.1 Test 1: Direct Text Invocation

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

### 3.2 Expected Response (Reference)

This is the actual response from the 2026-03-16 test run:
```json
{
  "statusCode": 200,
  "body": {
    "abstract": "This paper presents a comprehensive study on advanced load forecasting methodologies for smart grid environments, addressing the technical challenges posed by integrating renewable energy sources into existing power grids. The research proposes a hybrid framework combining long short-term memory networks with gradient boosting methods, trained on three years of smart meter data from a metropolitan utility serving approximately 500,000 customers. The study focuses on distribution-level forecasting where distributed energy resources create unique modeling challenges, incorporating granular 15-minute interval consumption records, weather observations, calendar features, and economic indicators. The preprocessing pipeline addresses data quality issues common in advanced metering infrastructure, while feature engineering incorporates domain knowledge including temperature-load correlations, holiday effects, and industrial production cycles.\n\nExperimental evaluation demonstrates that the proposed hybrid framework achieves a mean absolute percentage error of 2.3 percent on day-ahead forecasts, representing a 15 percent improvement over the best individual baseline model among seven compared methods. The framework exhibits particular strength during extreme weather events and demand response periods, with a novel uncertainty quantification method based on conformal prediction providing calibrated prediction intervals. Operational deployment at the partner utility has reduced procurement costs by an estimated 3.2 million dollars annually and enabled increasing the renewable energy portfolio from 18 to 27 percent while maintaining grid reliability standards. The paper also presents transfer learning approaches for adapting the framework to new service territories with limited local training data, addressing a key barrier to widespread adoption of machine learning in power system operations.",
    "keywords": [
      "load forecasting",
      "smart grid",
      "deep learning",
      "long short-term memory",
      "gradient boosting",
      "renewable energy integration",
      "advanced metering infrastructure",
      "distributed energy resources",
      "conformal prediction",
      "demand response",
      "transfer learning",
      "power system operations"
    ],
    "learning_level": "Expert",
    "intended_audience": "Seasoned Engineering Professional",
    "category": "Research Papers and Publications",
    "processing_time_ms": 6159
  }
}
```

### 3.3 Validation Checklist

- [ ] `statusCode` is `200`
- [ ] `abstract` contains exactly two paragraphs separated by `\n\n`
- [ ] Each paragraph is 50–150 words
- [ ] `keywords` is an array of 8–12 strings
- [ ] `learning_level` is one of: `Foundational`, `Professional`, `Expert`
- [ ] `intended_audience` is one of: `Non-Engineer`, `Engineering Adjacent Professional`, `New Engineer`, `Seasoned Engineering Professional`
- [ ] `category` is one of: `Research Papers and Publications`, `Professional Development`, `Society Outreach`, `Technical Tutorial`
- [ ] `processing_time_ms` is a positive integer
- [ ] Response returns within 120 seconds (Lambda timeout)

### 3.4 Test 2: With Thesaurus Terms

```bash
cat > /tmp/bedrock-thesaurus-test.json << 'EOF'
{
  "text": "This tutorial introduces engineers to the fundamentals of electric vehicle charging infrastructure design. Topics covered include AC Level 1 and Level 2 charging, DC fast charging standards (CCS, CHAdeMO), grid interconnection requirements, demand management strategies, and site planning considerations. The guide walks through a complete design example for a commercial charging station, including electrical panel sizing, transformer selection, and load management system configuration.",
  "thesaurus_terms": ["electric vehicles", "battery charging", "power distribution", "smart grid", "energy management", "power electronics"]
}
EOF

aws lambda invoke \
  --function-name ieee-cc-bedrock-inference \
  --payload fileb:///tmp/bedrock-thesaurus-test.json \
  /tmp/bedrock-thesaurus-response.json && cat /tmp/bedrock-thesaurus-response.json | python3 -m json.tool
```

**Validate:**
- [ ] Keywords should prioritize terms from the provided thesaurus list
- [ ] `learning_level` should be `Foundational` or `Professional` (tutorial content)
- [ ] `category` should be `Technical Tutorial` or `Professional Development`

### 3.5 Test 3: End-to-End (PDF Extraction → Bedrock)

Extract text from a PDF, then feed it to Bedrock:

```bash
# Step 1: Extract text from PDF
aws lambda invoke \
  --function-name ieee-cc-pdf-extractor \
  --payload '{"bucket":"dev-ieee-conference-cloud-bulk-uploads","key":"PES/pending/pes_wp_peswfi_wfi_022025.pdf","ou":"PES","product_part_number":"pes_wp_peswfi_wfi_022025"}' \
  /tmp/pdf-output.json

# Step 2: Extract the text field and create Bedrock payload
python3 -c "
import json
with open('/tmp/pdf-output.json') as f:
    data = json.load(f)
text = data['body']['text']
# Truncate for payload size
payload = json.dumps({'text': text[:50000]})
with open('/tmp/bedrock-e2e-payload.json', 'w') as f:
    f.write(payload)
print(f'Text length: {len(text)} chars, truncated to: {min(len(text), 50000)}')
"

# Step 3: Send to Bedrock for metadata generation
aws lambda invoke \
  --function-name ieee-cc-bedrock-inference \
  --payload fileb:///tmp/bedrock-e2e-payload.json \
  /tmp/bedrock-e2e-response.json && cat /tmp/bedrock-e2e-response.json | python3 -m json.tool
```

**Validate:**
- [ ] End-to-end pipeline produces valid metadata from a real IEEE PDF
- [ ] Abstract is relevant to the document content
- [ ] Keywords match the document's technical domain

### 3.6 Error Cases

```bash
# Empty text — should return 400
aws lambda invoke \
  --function-name ieee-cc-bedrock-inference \
  --payload '{"text": "   "}' \
  /tmp/bedrock-empty.json && cat /tmp/bedrock-empty.json | python3 -m json.tool

# Missing text — should return 400
aws lambda invoke \
  --function-name ieee-cc-bedrock-inference \
  --payload '{"foo": "bar"}' \
  /tmp/bedrock-missing.json && cat /tmp/bedrock-missing.json | python3 -m json.tool

# Non-existent S3 reference — should return 500
aws lambda invoke \
  --function-name ieee-cc-bedrock-inference \
  --payload '{"bucket":"dev-ieee-conference-cloud-bulk-uploads","key":"does/not/exist.json"}' \
  /tmp/bedrock-s3-error.json && cat /tmp/bedrock-s3-error.json | python3 -m json.tool
```

| Test Case | Expected Status | Expected Error |
|-----------|----------------|----------------|
| Empty text | 400 | "text must be a non-empty string" |
| Missing text field | 400 | "Event must contain 'text' or 'bucket'+'key'" |
| Non-existent S3 key | 500 | "Bedrock NoSuchKey" or S3 error |

---

## 4. Image Overlay Generator (`ieee-rc-image-generator`)

### 4.1 Invoke with Test Trigger

Upload a trigger JSON to S3 (this also tests the S3 event trigger if configured):

```bash
cat > /tmp/overlay-trigger.json << 'EOF'
{
  "product_part_number": "QA-TEST-001",
  "title": "QA Test: Advanced Power Systems Engineering",
  "authors": "Jane Doe, John Smith, Robert Johnson",
  "config": {
    "source_bucket": "dev-ieee-conference-cloud-bulk-uploads",
    "dest_bucket": "dev-ieee-conference-cloud-bulk-uploads",
    "public_path": "images/products"
  },
  "background_source": "ieee",
  "output_format": "jpg",
  "output_quality": 85
}
EOF

# Direct invocation
aws lambda invoke \
  --function-name ieee-rc-image-generator \
  --payload '{"bucket":"dev-ieee-conference-cloud-bulk-uploads","key":"actions/qa-test-001.json"}' \
  /tmp/overlay-response.json && cat /tmp/overlay-response.json | python3 -m json.tool
```

> **Note:** You must first upload a background image and the trigger JSON to S3 for this to work:
> ```bash
> aws s3 cp <background-image>.jpg s3://dev-ieee-conference-cloud-bulk-uploads/backgrounds/ieee.jpg
> aws s3 cp /tmp/overlay-trigger.json s3://dev-ieee-conference-cloud-bulk-uploads/actions/qa-test-001.json
> ```

### 4.2 Validation Checklist

- [ ] `statusCode` is `200`
- [ ] `output_key` matches `images/products/QA-TEST-001.jpg`
- [ ] `format` is `jpg`
- [ ] `width` and `height` match background image dimensions
- [ ] Output image exists in S3 at the `output_key` path
- [ ] Trigger JSON is deleted from S3 after success

---

## 5. CloudWatch Logs

Check logs for any Lambda invocation:

```bash
# PDF Extractor logs
aws logs tail /aws/lambda/ieee-cc-pdf-extractor --since 1h --format short

# Bedrock Inference logs
aws logs tail /aws/lambda/ieee-cc-bedrock-inference --since 1h --format short

# Image Overlay logs
aws logs tail /aws/lambda/ieee-rc-image-generator --since 1h --format short
```

---

## 6. Summary of Existing Test Results

### PDF Extractor (tested 2026-03-11)

| PDF | Pages | Method | Result |
|-----|-------|--------|--------|
| PES_TP_Mag_PE_v23_N6_SP.pdf | 202 | text | PASS — text extracted |
| PES_TR_138_SBLCS_011726.pdf | 89 | text | PASS — text extracted |
| PES_TR_TR139_ITSLC_012826.pdf | 43 | text | PASS — text extracted |
| pes_wp_peswfi_wfi_022025.pdf | 34 | text | PASS — text extracted |
| PES_MAG_ELE_13-4.pdf | 92 | text | PASS — text extracted |
| PES_PUB_TP_TP101_101995.pdf | 187 | ocr | PASS — correctly identified as scanned |

### Bedrock Metadata Generation (tested 2026-03-16)

| Test | Input | Status | Result |
|------|-------|--------|--------|
| Direct text (power systems paper) | ~2500 chars | 200 | PASS — valid metadata returned in 6.2s |
| Abstract validation | — | — | PASS — 2 paragraphs, each 50–150 words |
| Keywords validation | — | — | PASS — 12 relevant keywords |
| learning_level | — | — | PASS — "Expert" |
| intended_audience | — | — | PASS — "Seasoned Engineering Professional" |
| category | — | — | PASS — "Research Papers and Publications" |

### Unit Tests (104 total, all passing)

| Test Suite | Count | Status |
|------------|-------|--------|
| tests/extractors/test_pdf_extractor.py | 21 | PASS |
| tests/generators/test_image_overlay_generator.py | 28 | PASS |
| tests/ai/test_bedrock_inference.py | 25 | PASS |
| tests/handlers/test_pdf_handler.py | 9 | PASS |
| tests/handlers/test_image_overlay_handler.py | 12 | PASS |
| tests/handlers/test_bedrock_handler.py | 9 | PASS |
