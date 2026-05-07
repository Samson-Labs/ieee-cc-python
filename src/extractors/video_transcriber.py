"""Video Transcription module for IEEE Content Conversion pipeline.

Submits video files to AWS Transcribe, polls for completion, fetches the
transcript, and optionally cleans it via Claude Haiku. Writes duration
metadata to S3 and returns structured results.

Supported formats: MP4, MOV, WEBM.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import TypedDict

import boto3

from src.common.metrics import publish_metrics
from src.extractors.audio_extractor import AudioExtractor

logger = logging.getLogger(__name__)

# Transcribe settings
SUPPORTED_FORMATS = {"mp4", "mov", "webm", "mp3"}
LANGUAGE_CODE = "en-US"
MAX_SPEAKERS = 2
POLL_INTERVAL_SECONDS = 30
POLL_TIMEOUT_SECONDS = 800
JOB_NAME_PREFIX = "ieee-rc"

# AWS Transcribe rejects input files larger than 2 GB. Files above this
# threshold are routed through MediaConvert audio extraction first.
TRANSCRIBE_MAX_BYTES = 1_900_000_000

# Bedrock transcript cleanup
CLEANUP_MODEL_ID = os.environ.get(
    "CLEANUP_MODEL_ID", "us.anthropic.claude-3-5-haiku-20241022-v1:0"
)
TRANSCRIPT_TRUNCATION_LIMIT = 100_000  # chars before sending to Haiku

CLEANUP_SYSTEM_PROMPT = (
    "You are a transcript cleaner. Your task is to clean up a raw speech-to-text "
    "transcript. Remove filler words (uh, um, like, you know, so, basically, "
    "actually, right, I mean). Fix sentence boundaries and capitalization. "
    "Format speaker transitions as 'Speaker 1:' and 'Speaker 2:' on new lines. "
    "Preserve the original meaning exactly — do not add, remove, or rephrase "
    "content. Return only the cleaned transcript text, nothing else."
)


class TranscriptionResult(TypedDict):
    """Result of a video transcription."""

    transcript: str
    duration: str  # HH:MM:SS
    duration_seconds: int
    speaker_count: int
    vtt_s3_key: str  # S3 key of the generated WebVTT subtitle file


class VideoTranscriber:
    """Transcribes video files via AWS Transcribe with optional Haiku cleanup."""

    def __init__(
        self,
        s3_client=None,
        transcribe_client=None,
        bedrock_client=None,
        cloudwatch_client=None,
        audio_extractor: AudioExtractor | None = None,
    ):
        self._s3 = s3_client or boto3.client("s3")
        self._transcribe = transcribe_client or boto3.client("transcribe")
        self._bedrock = bedrock_client or boto3.client(
            "bedrock-runtime", region_name="us-east-1"
        )
        self._cloudwatch = cloudwatch_client
        self._audio_extractor = audio_extractor
        self._audio_extraction_enabled = (
            os.environ.get("ENABLE_AUDIO_EXTRACTION", "true").lower() == "true"
        )

    def transcribe(
        self,
        bucket: str,
        key: str,
        ou: str,
        product_part_number: str,
        clean_transcript: bool = True,
    ) -> TranscriptionResult:
        """Transcribe a video file from S3 end-to-end.

        1. Validate media format
        2. Start Transcribe job with speaker diarization
        3. Poll for completion
        4. Fetch and concatenate transcript
        5. Optionally clean via Haiku
        6. Write duration metadata to S3

        Args:
            bucket: S3 bucket containing the video.
            key: S3 key of the video file.
            ou: Organizational unit (e.g. PES).
            product_part_number: Product identifier.
            clean_transcript: Whether to clean via Claude Haiku.

        Returns:
            TranscriptionResult with transcript text, duration, and speaker count.
        """
        media_format = self._detect_format(key)
        media_uri = f"s3://{bucket}/{key}"
        job_name = self._generate_job_name(product_part_number)

        logger.info("Starting transcription job %s for %s", job_name, media_uri)

        # Step 1a: For oversized files, extract audio via MediaConvert so the
        # downstream Transcribe call stays under the 2 GB service limit. Skip
        # the size check entirely when the feature flag is off to avoid an
        # extra S3 head_object roundtrip on the fast path.
        extract_audio = False
        extracted_audio_key: str | None = None
        transcribe_uri = media_uri
        transcribe_format = media_format

        if self._audio_extraction_enabled:
            size_bytes = self._s3.head_object(
                Bucket=bucket, Key=key
            )["ContentLength"]
            if size_bytes > TRANSCRIBE_MAX_BYTES:
                logger.info(
                    "File size %d > %d, extracting audio via MediaConvert",
                    size_bytes,
                    TRANSCRIBE_MAX_BYTES,
                )
                if self._audio_extractor is None:
                    self._audio_extractor = AudioExtractor()
                audio_uri = self._audio_extractor.extract_audio(
                    source_uri=media_uri,
                    output_bucket=bucket,
                    output_key_prefix=f"transcribe-input/{job_name}",
                )
                _, extracted_audio_key = self._parse_s3_uri(audio_uri)
                transcribe_uri = audio_uri
                transcribe_format = "mp3"
                extract_audio = True

        try:
            # Step 1b: Start transcription job
            self._start_job(
                job_name, transcribe_uri, transcribe_format, output_bucket=bucket
            )

            # Step 2: Poll for completion
            job = self._poll_job(job_name)
        finally:
            if extracted_audio_key:
                try:
                    self._s3.delete_object(Bucket=bucket, Key=extracted_audio_key)
                    logger.info(
                        "Deleted extracted audio s3://%s/%s",
                        bucket,
                        extracted_audio_key,
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to delete extracted audio s3://%s/%s: %s",
                        bucket,
                        extracted_audio_key,
                        exc,
                    )

        # Step 3: Fetch transcript and VTT subtitle key
        transcript_uri = job["TranscriptionJob"]["Transcript"]["TranscriptFileUri"]
        raw_transcript, duration_seconds, speaker_count = self._fetch_transcript(
            transcript_uri
        )

        # Extract VTT S3 key from Subtitles output
        vtt_s3_key = ""
        subtitles_output = job["TranscriptionJob"].get("Subtitles", {}).get(
            "SubtitleFileUris", []
        )
        if subtitles_output:
            vtt_uri = subtitles_output[0]
            vtt_bucket, vtt_s3_key = self._parse_s3_uri(vtt_uri)
            logger.info("WebVTT subtitle file: s3://%s/%s", vtt_bucket, vtt_s3_key)

        logger.info(
            "Transcription complete: %d chars, %d speakers, %ds duration",
            len(raw_transcript),
            speaker_count,
            duration_seconds,
        )

        # Step 4: Optionally clean transcript
        transcript = raw_transcript
        if clean_transcript and raw_transcript.strip():
            try:
                transcript = self._clean_transcript(raw_transcript)
                logger.info("Transcript cleaned via Haiku")
            except Exception as exc:
                logger.warning("Haiku cleanup failed, using raw transcript: %s", exc)

        # Step 5: Write duration metadata to S3
        duration_str = self._format_duration(duration_seconds)
        metadata_key = f"{ou}/metadata/{product_part_number}.{media_format}.json"
        self._write_metadata(
            bucket, metadata_key, duration_str, duration_seconds
        )
        logger.info("Wrote metadata to s3://%s/%s", bucket, metadata_key)

        publish_metrics(self._cloudwatch, [
            {
                "MetricName": "transcribe-minutes",
                "Value": round(duration_seconds / 60, 2),
                "Unit": "None",
            },
        ])

        return TranscriptionResult(
            transcript=transcript,
            duration=duration_str,
            duration_seconds=duration_seconds,
            speaker_count=speaker_count,
            vtt_s3_key=vtt_s3_key,
        )

    def transcribe_from_uri(
        self, transcript_uri: str
    ) -> tuple[str, int, int]:
        """Fetch and parse a transcript from a Transcribe output URI.

        For testing without running a full Transcribe job.

        Returns:
            Tuple of (raw_transcript, duration_seconds, speaker_count).
        """
        return self._fetch_transcript(transcript_uri)

    # ------------------------------------------------------------------
    # Transcribe job management
    # ------------------------------------------------------------------

    def _start_job(
        self,
        job_name: str,
        media_uri: str,
        media_format: str,
        output_bucket: str | None = None,
    ) -> None:
        """Start an AWS Transcribe job with speaker diarization."""
        params = {
            "TranscriptionJobName": job_name,
            "LanguageCode": LANGUAGE_CODE,
            "Media": {"MediaFileUri": media_uri},
            "MediaFormat": media_format,
            "Settings": {
                "ShowSpeakerLabels": True,
                "MaxSpeakerLabels": MAX_SPEAKERS,
            },
            "Subtitles": {
                "Formats": ["vtt"],
                "OutputStartIndex": 1,
            },
        }
        if output_bucket:
            params["OutputBucketName"] = output_bucket
            params["OutputKey"] = f"transcribe-output/{job_name}.json"
        self._transcribe.start_transcription_job(**params)

    def _poll_job(self, job_name: str) -> dict:
        """Poll Transcribe job until completion or timeout."""
        elapsed = 0
        while elapsed < POLL_TIMEOUT_SECONDS:
            response = self._transcribe.get_transcription_job(
                TranscriptionJobName=job_name
            )
            status = response["TranscriptionJob"]["TranscriptionJobStatus"]

            if status == "COMPLETED":
                return response
            if status == "FAILED":
                reason = response["TranscriptionJob"].get(
                    "FailureReason", "Unknown failure"
                )
                raise RuntimeError(
                    f"Transcription job {job_name} failed: {reason}"
                )

            logger.info(
                "Job %s status: %s (elapsed %ds)", job_name, status, elapsed
            )
            time.sleep(POLL_INTERVAL_SECONDS)
            elapsed += POLL_INTERVAL_SECONDS

        raise TimeoutError(
            f"Transcription job {job_name} timed out after {POLL_TIMEOUT_SECONDS}s"
        )

    # ------------------------------------------------------------------
    # Transcript parsing
    # ------------------------------------------------------------------

    def _fetch_transcript(
        self, transcript_uri: str
    ) -> tuple[str, int, int]:
        """Fetch transcript JSON from Transcribe output and parse it.

        Returns:
            Tuple of (concatenated_text, duration_seconds, speaker_count).
        """
        # Transcribe writes output to an S3 URI
        # Parse bucket and key from the URI
        bucket, key = self._parse_s3_uri(transcript_uri)
        resp = self._s3.get_object(Bucket=bucket, Key=key)
        transcript_json = json.loads(resp["Body"].read())

        return self._parse_transcript_json(transcript_json)

    @staticmethod
    def _parse_transcript_json(
        transcript_json: dict,
    ) -> tuple[str, int, int]:
        """Parse AWS Transcribe JSON output.

        Returns:
            Tuple of (concatenated_text, duration_seconds, speaker_count).
        """
        results = transcript_json.get("results", {})

        # Get full transcript text
        transcripts = results.get("transcripts", [])
        full_text = transcripts[0]["transcript"] if transcripts else ""

        # Get duration from the last segment end time
        items = results.get("items", [])
        duration_seconds = 0
        if items:
            for item in reversed(items):
                if "end_time" in item:
                    duration_seconds = int(float(item["end_time"]))
                    break

        # Count distinct speakers from speaker labels
        speaker_count = 0
        speaker_labels = results.get("speaker_labels", {})
        if speaker_labels:
            speakers = speaker_labels.get("speakers", 0)
            speaker_count = int(speakers) if speakers else 0
            # Fallback: count distinct speaker labels in segments
            if not speaker_count:
                segments = speaker_labels.get("segments", [])
                distinct = {
                    seg["speaker_label"]
                    for seg in segments
                    if "speaker_label" in seg
                }
                speaker_count = len(distinct)

        return full_text, duration_seconds, speaker_count

    # ------------------------------------------------------------------
    # Transcript cleanup via Haiku
    # ------------------------------------------------------------------

    def _clean_transcript(self, raw_transcript: str) -> str:
        """Clean raw transcript via Claude Haiku to remove fillers."""
        text = raw_transcript[:TRANSCRIPT_TRUNCATION_LIMIT]

        body = json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 4096,
                "temperature": 0.1,
                "system": CLEANUP_SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": text}],
            }
        )

        response = self._bedrock.invoke_model(
            modelId=CLEANUP_MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=body,
        )

        result = json.loads(response["body"].read())
        return result["content"][0]["text"]

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    def _write_metadata(
        self,
        bucket: str,
        key: str,
        duration: str,
        duration_seconds: int,
    ) -> None:
        """Write duration metadata JSON to S3."""
        metadata = {
            "duration": duration,
            "durationSeconds": duration_seconds,
            "extractedAt": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        self._s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(metadata).encode(),
            ContentType="application/json",
        )

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_format(key: str) -> str:
        """Detect media format from the S3 key extension."""
        ext = key.rsplit(".", 1)[-1].lower() if "." in key else ""
        if ext not in SUPPORTED_FORMATS:
            raise ValueError(
                f"Unsupported media format: '{ext}'. "
                f"Supported: {', '.join(sorted(SUPPORTED_FORMATS))}"
            )
        return ext

    @staticmethod
    def _generate_job_name(item_id: str) -> str:
        """Generate a unique Transcribe job name."""
        timestamp = int(time.time())
        # Transcribe job names: alphanumeric, hyphens, periods, underscores
        safe_id = re.sub(r"[^a-zA-Z0-9._-]", "-", item_id)
        return f"{JOB_NAME_PREFIX}-{safe_id}-{timestamp}"

    @staticmethod
    def _format_duration(seconds: int) -> str:
        """Format seconds as HH:MM:SS."""
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    @staticmethod
    def _parse_s3_uri(uri: str) -> tuple[str, str]:
        """Parse an s3:// URI or https URL into (bucket, key)."""
        if uri.startswith("s3://"):
            parts = uri[5:].split("/", 1)
            return parts[0], parts[1]
        # Transcribe sometimes returns https URLs
        # https://s3.region.amazonaws.com/bucket/key
        match = re.match(
            r"https://s3[.\w-]*\.amazonaws\.com/([^/]+)/(.+)", uri
        )
        if match:
            return match.group(1), match.group(2)
        raise ValueError(f"Cannot parse transcript URI: {uri}")
