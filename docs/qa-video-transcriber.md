# QA Testing Guide — Video Transcription (CC3-775)

**Date:** 2026-03-18
**Environment:** AWS Account `141770997341`, Region `us-east-1`
**Profile:** `ieee-cc`

## What Was Implemented

Reusable Python module (`src/extractors/video_transcriber.py`) that transcribes video files (MP4, MOV, WEBM) stored in S3 using AWS Transcribe with speaker diarization. Optionally cleans transcripts via Claude 3.5 Haiku to remove filler words and format speaker transitions. Writes duration metadata to S3 and returns structured results. Deployed as a Docker-based Lambda (`ieee-cc-video-transcriber`).

### Acceptance Criteria

- [x] Submits video files to AWS Transcribe with speaker diarization (max 2 speakers)
- [x] Polls Transcribe job every 30s with 600s timeout
- [x] Fetches and parses transcript JSON (text, duration, speaker count)
- [x] Optional Claude 3.5 Haiku cleanup removes filler words and formats speaker transitions
- [x] Graceful fallback: if Haiku cleanup fails, raw transcript is returned
- [x] Writes duration metadata to `{ou}/metadata/{product_part_number}.mp4.json`
- [x] Returns structured result: `{transcript, duration, duration_seconds, speaker_count}`
- [x] Supports MP4, MOV, WEBM formats
- [x] Lambda handler supports direct invocation and S3 event triggers
- [x] S3 Input Path: `{ou}/pending/{filename}.{mp4|mov|webm}`
- [x] S3 Metadata Output: `{ou}/metadata/{product_part_number}.mp4.json`
- [x] Unit tests cover: format detection, job management, polling, transcript parsing, Haiku cleanup, metadata writing, error handling

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
| Function Name | `ieee-cc-video-transcriber` |
| Memory | 512 MB |
| Timeout | 15 min (900s) |
| Runtime | Python 3.13 (Docker) |
| S3 Bucket | `dev-ieee-conference-cloud-bulk-uploads` |
| Haiku Model | `us.anthropic.claude-3-5-haiku-20241022-v1:0` |

Verify the Lambda is active:
```bash
aws lambda get-function --function-name ieee-cc-video-transcriber --query "Configuration.{State:State,Memory:MemorySize,Timeout:Timeout}" --output table
```

---

## Test Data in S3

| Video File | S3 Key | Size | Expected Duration | Expected Speakers |
|-----------|--------|------|-------------------|-------------------|
| ABC123454.mp4 | `ABC/pending/ABC123454.mp4` | 2 MB | ~0s (no speech) | 0 |
| AESSBMR0000.mp4 | `AESS/pending/AESSBMR0000.mp4` | 55 MB | ~30 min | 1 |
| SGWEB0009.mp4 | `SmartGrid/pending/SGWEB0009.mp4` | 10 MB | ~42 min | 2 |
| AESSBMR0040.mp4 | `AESS/pending/AESSBMR0040.mp4` | 40 MB | ~26 min | 1 |
| ICME20VID037.mp4 | `SPS/pending/ICME20VID037.mp4` | 10 MB | ~6 min | 1 |

To copy test videos from existing locations to `pending/` paths:
```bash
aws s3 cp s3://dev-ieee-conference-cloud-bulk-uploads/ABC/ABC123454.mp4 s3://dev-ieee-conference-cloud-bulk-uploads/ABC/pending/ABC123454.mp4
aws s3 cp s3://dev-ieee-conference-cloud-bulk-uploads/IEEETV/ns/ieeetvdl/Products/AESS/AESSBMR0000.mp4 s3://dev-ieee-conference-cloud-bulk-uploads/AESS/pending/AESSBMR0000.mp4
aws s3 cp s3://dev-ieee-conference-cloud-bulk-uploads/IEEETV/ns/ieeetvdl/Products/SmartGrid/SGWEB0009.mp4 s3://dev-ieee-conference-cloud-bulk-uploads/SmartGrid/pending/SGWEB0009.mp4
aws s3 cp s3://dev-ieee-conference-cloud-bulk-uploads/IEEETV/ns/ieeetvdl/Products/AESS/AESSBMR0040.mp4 s3://dev-ieee-conference-cloud-bulk-uploads/AESS/pending/AESSBMR0040.mp4
aws s3 cp s3://dev-ieee-conference-cloud-bulk-uploads/IEEETV/ns/ieeetvdl/Products/SPS/ICME2020/ICME20VID037.mp4 s3://dev-ieee-conference-cloud-bulk-uploads/SPS/pending/ICME20VID037.mp4
```

---

## Test 1: Single Speaker Lecture (with Haiku Cleanup)

```bash
./scripts/invoke-video-transcriber.sh \
  dev-ieee-conference-cloud-bulk-uploads \
  AESS/pending/AESSBMR0000.mp4 AESS AESSBMR0000
```

### Validation

- [ ] `statusCode` is `200`
- [ ] `transcript` is non-empty and contains readable technical content
- [ ] `transcript` has clean sentences (no "uh", "um", "like" filler words)
- [ ] `duration` is in `HH:MM:SS` format (expected: ~`00:30:51`)
- [ ] `duration_seconds` is a positive integer (expected: ~1851)
- [ ] `speaker_count` is `1`
- [ ] Lambda completes within 15 min timeout

---

## Test 2: Multi-Speaker Webinar (Speaker Diarization)

```bash
./scripts/invoke-video-transcriber.sh \
  dev-ieee-conference-cloud-bulk-uploads \
  SmartGrid/pending/SGWEB0009.mp4 SmartGrid SGWEB0009
```

### Validation

- [ ] `statusCode` is `200`
- [ ] `transcript` is non-empty
- [ ] `speaker_count` is `2` (host + presenter)
- [ ] `duration` is in `HH:MM:SS` format (expected: ~`00:42:16`)
- [ ] `duration_seconds` is a positive integer (expected: ~2536)
- [ ] Transcript contains content from both speakers

---

## Test 3: Short Conference Talk (Non-English Accent)

```bash
./scripts/invoke-video-transcriber.sh \
  dev-ieee-conference-cloud-bulk-uploads \
  SPS/pending/ICME20VID037.mp4 SPS ICME20VID037
```

### Validation

- [ ] `statusCode` is `200`
- [ ] `transcript` is non-empty
- [ ] `duration` is in `HH:MM:SS` format (expected: ~`00:06:12`)
- [ ] `speaker_count` is `1`
- [ ] Transcript captures accented English speech reasonably well

---

## Test 4: Raw Transcript (No Haiku Cleanup)

Invoke directly with `clean_transcript: false`:

```bash
aws lambda invoke \
  --function-name ieee-cc-video-transcriber \
  --region us-east-1 \
  --cli-read-timeout 900 \
  --payload '{"bucket":"dev-ieee-conference-cloud-bulk-uploads","key":"SPS/pending/ICME20VID037.mp4","ou":"SPS","product_part_number":"ICME20VID037-RAW","clean_transcript":false}' \
  /tmp/vt-raw.json && python3 -m json.tool /tmp/vt-raw.json
```

### Validation

- [ ] `statusCode` is `200`
- [ ] `transcript` contains filler words ("uh", "um") — not cleaned
- [ ] Compare with Test 3 output — Test 3 should be cleaner

---

## Test 5: No Speech Content

```bash
./scripts/invoke-video-transcriber.sh \
  dev-ieee-conference-cloud-bulk-uploads \
  ABC/pending/ABC123454.mp4 ABC ABC123454
```

### Validation

- [ ] `statusCode` is `200`
- [ ] `transcript` is empty `""`
- [ ] `duration` is `"00:00:00"`
- [ ] `duration_seconds` is `0`
- [ ] `speaker_count` is `0`

---

## Test 6: Verify Metadata Written to S3

After transcription, duration metadata JSON is written to `{ou}/metadata/`. Verify:

```bash
# List metadata files
aws s3 ls s3://dev-ieee-conference-cloud-bulk-uploads/AESS/metadata/ | grep ".mp4.json"
aws s3 ls s3://dev-ieee-conference-cloud-bulk-uploads/SmartGrid/metadata/ | grep ".mp4.json"
aws s3 ls s3://dev-ieee-conference-cloud-bulk-uploads/SPS/metadata/ | grep ".mp4.json"

# Read a specific metadata file
aws s3 cp s3://dev-ieee-conference-cloud-bulk-uploads/AESS/metadata/AESSBMR0000.mp4.json - | python3 -m json.tool
```

### Expected Metadata Format

```json
{
  "duration": "00:30:51",
  "durationSeconds": 1851,
  "extractedAt": "2026-03-18T11:03:21.278421Z"
}
```

### Validation

- [ ] Metadata JSON exists at `{ou}/metadata/{product_part_number}.mp4.json`
- [ ] `duration` matches the video duration in `HH:MM:SS` format
- [ ] `durationSeconds` is a positive integer matching the duration
- [ ] `extractedAt` is a valid ISO 8601 timestamp

---

## Test 7: Error Handling

### Unsupported file format (should return 400)

```bash
aws lambda invoke \
  --function-name ieee-cc-video-transcriber \
  --region us-east-1 \
  --payload '{"bucket":"dev-ieee-conference-cloud-bulk-uploads","key":"PES/pending/test.pdf","ou":"PES","product_part_number":"TEST"}' \
  /tmp/vt-err1.json && python3 -m json.tool /tmp/vt-err1.json
```

- [ ] `statusCode` is `400`
- [ ] `body.error` mentions "Unsupported media format"

### Non-existent file (should return 500)

```bash
aws lambda invoke \
  --function-name ieee-cc-video-transcriber \
  --region us-east-1 \
  --payload '{"bucket":"dev-ieee-conference-cloud-bulk-uploads","key":"PES/pending/DOES_NOT_EXIST.mp4","ou":"PES","product_part_number":"DOES_NOT_EXIST"}' \
  /tmp/vt-err2.json && python3 -m json.tool /tmp/vt-err2.json
```

- [ ] `statusCode` is `500`
- [ ] `body.error` contains AWS error message

### Missing required fields (should return 400)

```bash
aws lambda invoke \
  --function-name ieee-cc-video-transcriber \
  --region us-east-1 \
  --payload '{"bucket":"dev-ieee-conference-cloud-bulk-uploads"}' \
  /tmp/vt-err3.json && python3 -m json.tool /tmp/vt-err3.json
```

- [ ] `statusCode` is `400`
- [ ] `body.error` mentions missing fields

### Error Summary

| Test Case | Expected Status | Expected Error |
|-----------|----------------|----------------|
| Unsupported format (.pdf) | 400 | "Unsupported media format" |
| Non-existent file | 500 | AWS S3/Transcribe error |
| Missing required fields | 400 | "Event must contain..." |

---

## CloudWatch Logs

```bash
aws logs tail /aws/lambda/ieee-cc-video-transcriber --since 1h --format short
```

---

## Existing Test Results (2026-03-18)

### AWS Live Tests

| # | Video | Size | Duration | Speakers | Haiku Cleanup | Transcript Length | Result |
|---|-------|------|----------|----------|---------------|-------------------|--------|
| 1 | ABC123454.mp4 | 2 MB | 00:00:00 | 0 | Yes (skipped — no text) | 0 chars | PASS |
| 2 | AESSBMR0000.mp4 (Bistatic Radar Tutorial) | 55 MB | 00:30:51 | 1 | Yes | ~14,000 chars | PASS |
| 3 | SGWEB0009.mp4 (Smart Grid Webinar) | 10 MB | 00:42:16 | 2 | Yes | ~18,000 chars | PASS |
| 4 | AESSBMR0040.mp4 (Radar Signal Processing) | 40 MB | 00:26:26 | 1 | Yes | ~16,000 chars | PASS |
| 5 | ICME20VID037.mp4 (SPS Conference, Korean presenter) | 10 MB | 00:06:12 | 1 | No (raw) | ~3,000 chars | PASS |

### Key Observations

- **Speaker diarization** correctly detected 2 speakers in the Smart Grid webinar (host + presenter)
- **Haiku cleanup** successfully removed filler words ("uh", "um", "like") — compare Test 4 (cleaned) vs Test 5 (raw)
- **Accented speech** handled reasonably well (Test 5: Korean presenter on video compression standards)
- **Empty/silent video** handled gracefully — returns empty transcript with 0 duration (Test 1)
- **Duration metadata** written correctly to S3 for all tests

### Metadata Files in S3

| File | Duration | Seconds | Extracted At |
|------|----------|---------|-------------|
| ABC/metadata/ABC123454.mp4.json | 00:00:00 | 0 | 2026-03-18T10:57:05Z |
| AESS/metadata/AESSBMR0000.mp4.json | 00:30:51 | 1851 | 2026-03-18T11:03:21Z |
| SmartGrid/metadata/SGWEB0009.mp4.json | 00:42:16 | 2536 | 2026-03-18T11:10:45Z |
| AESS/metadata/AESSBMR0040.mp4.json | 00:26:26 | 1586 | 2026-03-18T11:18:12Z |
| SPS/metadata/ICME20VID037.mp4.json | 00:06:12 | 372 | 2026-03-18T11:22:30Z |

### Lambda Performance

| Metric | Value |
|--------|-------|
| Cold start | ~860ms (init) |
| Processing time | Depends on video length (~30s per Transcribe poll cycle + Haiku cleanup) |
| Memory used | ~93 MB (of 512 MB) |
| Typical invocation | 60–180s for 10–55 MB videos |

### Bugs Found and Fixed During Testing

| Bug | Root Cause | Fix |
|-----|-----------|-----|
| `Runtime.ImportModuleError: No module named 'fitz'` | `src/extractors/__init__.py` eagerly imported `PDFExtractor` (depends on PyMuPDF), which isn't in the video transcriber Docker image | Changed to lazy `__getattr__` imports |
| `KeyTooLongError: Your key is too long` | AWS Transcribe's default service-managed output bucket hit an internal key length limit | Added explicit `OutputBucketName` and `OutputKey` parameters to `start_transcription_job` |

---

## Cleanup

Remove test video copies from `pending/` paths after QA:

```bash
aws s3 rm s3://dev-ieee-conference-cloud-bulk-uploads/ABC/pending/ABC123454.mp4
aws s3 rm s3://dev-ieee-conference-cloud-bulk-uploads/AESS/pending/AESSBMR0000.mp4
aws s3 rm s3://dev-ieee-conference-cloud-bulk-uploads/AESS/pending/AESSBMR0040.mp4
aws s3 rm s3://dev-ieee-conference-cloud-bulk-uploads/SmartGrid/pending/SGWEB0009.mp4
aws s3 rm s3://dev-ieee-conference-cloud-bulk-uploads/SPS/pending/ICME20VID037.mp4
```

Remove Transcribe output files:
```bash
aws s3 rm s3://dev-ieee-conference-cloud-bulk-uploads/transcribe-output/ --recursive
```

Remove metadata files:
```bash
aws s3 rm s3://dev-ieee-conference-cloud-bulk-uploads/ABC/metadata/ABC123454.mp4.json
aws s3 rm s3://dev-ieee-conference-cloud-bulk-uploads/AESS/metadata/AESSBMR0000.mp4.json
aws s3 rm s3://dev-ieee-conference-cloud-bulk-uploads/AESS/metadata/AESSBMR0040.mp4.json
aws s3 rm s3://dev-ieee-conference-cloud-bulk-uploads/SmartGrid/metadata/SGWEB0009.mp4.json
aws s3 rm s3://dev-ieee-conference-cloud-bulk-uploads/SPS/metadata/ICME20VID037.mp4.json
```

---

## Unit Tests (53 total, all passing)

Run locally:
```bash
python -m pytest tests/extractors/test_video_transcriber.py tests/handlers/test_video_transcriber_handler.py -v
```

| Test Suite | Count | Status |
|------------|-------|--------|
| tests/extractors/test_video_transcriber.py | 35 | PASS |
| tests/handlers/test_video_transcriber_handler.py | 18 | PASS |
| **Total** | **53** | **ALL PASS** |

### Test Breakdown

| Test Class | Count | Description |
|-----------|-------|-------------|
| TestFormatDetection | 6 | Supported/unsupported format validation |
| TestJobNameGeneration | 3 | Job name format and special character handling |
| TestDurationFormatting | 4 | HH:MM:SS conversion edge cases |
| TestParseS3Uri | 3 | s3:// and https:// URI parsing |
| TestTranscriptParsing | 6 | Transcribe JSON output parsing |
| TestStartJob | 1 | Transcribe API call parameters |
| TestPollJob | 4 | Polling, timeout, and failure handling |
| TestHaikuCleanup | 2 | Bedrock cleanup call and fallback |
| TestTranscribeFlow | 4 | End-to-end integration (mocked) |
| TestMetadataWriting | 1 | S3 metadata JSON output |
| TestDirectInvocation | 4 | Handler direct event parsing |
| TestS3EventInvocation | 5 | Handler S3 event parsing |
| TestErrorHandling | 5 | Handler error status codes |
| TestParseEvent | 5 | Event format detection |
