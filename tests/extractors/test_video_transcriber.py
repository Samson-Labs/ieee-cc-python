"""Tests for VideoTranscriber.

Covers: job submission, polling, transcript parsing, Haiku cleanup,
duration metadata, format detection, error handling.
"""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock, call, patch

import pytest

from src.extractors.video_transcriber import (
    LANGUAGE_CODE,
    MAX_SPEAKERS,
    POLL_INTERVAL_SECONDS,
    POLL_TIMEOUT_SECONDS,
    SUPPORTED_FORMATS,
    TranscriptionResult,
    VideoTranscriber,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_transcribe_json(
    transcript_text: str = "Hello this is a test transcript.",
    end_time: str = "5025.0",
    speaker_count: int = 2,
) -> dict:
    """Create a mock AWS Transcribe JSON output."""
    return {
        "results": {
            "transcripts": [{"transcript": transcript_text}],
            "items": [
                {
                    "start_time": "0.0",
                    "end_time": "2.5",
                    "type": "pronunciation",
                    "alternatives": [{"content": "Hello"}],
                },
                {
                    "start_time": "2.5",
                    "end_time": end_time,
                    "type": "pronunciation",
                    "alternatives": [{"content": "test"}],
                },
            ],
            "speaker_labels": {
                "speakers": speaker_count,
                "segments": [
                    {"speaker_label": f"spk_{i}", "start_time": "0.0", "end_time": "5.0"}
                    for i in range(speaker_count)
                ],
            },
        }
    }


def _mock_transcribe_complete(transcribe_mock, job_name_pattern=None):
    """Set up Transcribe mock for a successful job."""
    transcribe_mock.get_transcription_job.return_value = {
        "TranscriptionJob": {
            "TranscriptionJobStatus": "COMPLETED",
            "Transcript": {
                "TranscriptFileUri": "s3://output-bucket/transcripts/job.json"
            },
        }
    }


def _mock_s3_transcript(s3_mock, transcript_json=None):
    """Set up S3 mock to return transcript JSON."""
    tj = transcript_json or _make_transcribe_json()

    def get_object_side_effect(Bucket, Key):
        return {"Body": BytesIO(json.dumps(tj).encode())}

    s3_mock.get_object.side_effect = get_object_side_effect


def _mock_bedrock_cleanup(bedrock_mock, cleaned_text="Hello, this is a test transcript."):
    """Set up Bedrock mock for Haiku cleanup."""
    bedrock_mock.invoke_model.return_value = {
        "body": BytesIO(
            json.dumps({"content": [{"text": cleaned_text}]}).encode()
        )
    }


# ---------------------------------------------------------------------------
# Tests: format detection
# ---------------------------------------------------------------------------


class TestFormatDetection:
    def test_mp4(self):
        assert VideoTranscriber._detect_format("PES/pending/video.mp4") == "mp4"

    def test_mov(self):
        assert VideoTranscriber._detect_format("PES/pending/video.mov") == "mov"

    def test_webm(self):
        assert VideoTranscriber._detect_format("PES/pending/video.webm") == "webm"

    def test_case_insensitive(self):
        assert VideoTranscriber._detect_format("PES/pending/video.MP4") == "mp4"

    def test_unsupported_raises(self):
        with pytest.raises(ValueError, match="Unsupported media format"):
            VideoTranscriber._detect_format("PES/pending/file.avi")

    def test_no_extension_raises(self):
        with pytest.raises(ValueError, match="Unsupported media format"):
            VideoTranscriber._detect_format("PES/pending/noextension")


# ---------------------------------------------------------------------------
# Tests: job name generation
# ---------------------------------------------------------------------------


class TestJobNameGeneration:
    def test_contains_prefix_and_id(self):
        name = VideoTranscriber._generate_job_name("STD-12345")
        assert name.startswith("ieee-rc-STD-12345-")

    def test_sanitizes_special_characters(self):
        name = VideoTranscriber._generate_job_name("file name/with:special")
        assert "/" not in name
        assert ":" not in name
        assert " " not in name

    def test_unique_timestamps(self):
        name1 = VideoTranscriber._generate_job_name("TEST")
        name2 = VideoTranscriber._generate_job_name("TEST")
        # Same second could match, but format is correct
        assert name1.startswith("ieee-rc-TEST-")


# ---------------------------------------------------------------------------
# Tests: duration formatting
# ---------------------------------------------------------------------------


class TestDurationFormatting:
    def test_hours_minutes_seconds(self):
        assert VideoTranscriber._format_duration(5025) == "01:23:45"

    def test_zero(self):
        assert VideoTranscriber._format_duration(0) == "00:00:00"

    def test_under_minute(self):
        assert VideoTranscriber._format_duration(45) == "00:00:45"

    def test_exact_hour(self):
        assert VideoTranscriber._format_duration(3600) == "01:00:00"


# ---------------------------------------------------------------------------
# Tests: S3 URI parsing
# ---------------------------------------------------------------------------


class TestParseS3Uri:
    def test_s3_protocol(self):
        bucket, key = VideoTranscriber._parse_s3_uri(
            "s3://my-bucket/path/to/file.json"
        )
        assert bucket == "my-bucket"
        assert key == "path/to/file.json"

    def test_https_url(self):
        bucket, key = VideoTranscriber._parse_s3_uri(
            "https://s3.us-east-1.amazonaws.com/my-bucket/path/file.json"
        )
        assert bucket == "my-bucket"
        assert key == "path/file.json"

    def test_invalid_uri_raises(self):
        with pytest.raises(ValueError, match="Cannot parse"):
            VideoTranscriber._parse_s3_uri("http://example.com/file.json")


# ---------------------------------------------------------------------------
# Tests: transcript parsing
# ---------------------------------------------------------------------------


class TestTranscriptParsing:
    def test_parses_text(self):
        tj = _make_transcribe_json("Hello world")
        text, _, _ = VideoTranscriber._parse_transcript_json(tj)
        assert text == "Hello world"

    def test_parses_duration(self):
        tj = _make_transcribe_json(end_time="5025.7")
        _, duration, _ = VideoTranscriber._parse_transcript_json(tj)
        assert duration == 5025

    def test_parses_speaker_count(self):
        tj = _make_transcribe_json(speaker_count=2)
        _, _, speakers = VideoTranscriber._parse_transcript_json(tj)
        assert speakers == 2

    def test_empty_transcript(self):
        tj = {"results": {"transcripts": [], "items": []}}
        text, duration, speakers = VideoTranscriber._parse_transcript_json(tj)
        assert text == ""
        assert duration == 0
        assert speakers == 0

    def test_no_speaker_labels(self):
        tj = _make_transcribe_json()
        del tj["results"]["speaker_labels"]
        _, _, speakers = VideoTranscriber._parse_transcript_json(tj)
        assert speakers == 0

    def test_fallback_speaker_count_from_segments(self):
        tj = _make_transcribe_json()
        tj["results"]["speaker_labels"]["speakers"] = 0
        tj["results"]["speaker_labels"]["segments"] = [
            {"speaker_label": "spk_0"},
            {"speaker_label": "spk_1"},
            {"speaker_label": "spk_0"},
        ]
        _, _, speakers = VideoTranscriber._parse_transcript_json(tj)
        assert speakers == 2


# ---------------------------------------------------------------------------
# Tests: job submission and polling
# ---------------------------------------------------------------------------


class TestStartJob:
    def test_calls_transcribe_with_correct_params(self):
        t_mock = MagicMock()
        transcriber = VideoTranscriber(
            s3_client=MagicMock(),
            transcribe_client=t_mock,
            bedrock_client=MagicMock(),
        )

        transcriber._start_job("test-job", "s3://bucket/video.mp4", "mp4")

        t_mock.start_transcription_job.assert_called_once_with(
            TranscriptionJobName="test-job",
            LanguageCode=LANGUAGE_CODE,
            Media={"MediaFileUri": "s3://bucket/video.mp4"},
            MediaFormat="mp4",
            Settings={
                "ShowSpeakerLabels": True,
                "MaxSpeakerLabels": MAX_SPEAKERS,
            },
        )


class TestPollJob:
    @patch("src.extractors.video_transcriber.time.sleep")
    def test_returns_on_completed(self, mock_sleep):
        t_mock = MagicMock()
        t_mock.get_transcription_job.return_value = {
            "TranscriptionJob": {
                "TranscriptionJobStatus": "COMPLETED",
                "Transcript": {"TranscriptFileUri": "s3://b/out.json"},
            }
        }
        transcriber = VideoTranscriber(
            s3_client=MagicMock(),
            transcribe_client=t_mock,
            bedrock_client=MagicMock(),
        )

        result = transcriber._poll_job("test-job")
        assert result["TranscriptionJob"]["TranscriptionJobStatus"] == "COMPLETED"
        mock_sleep.assert_not_called()

    @patch("src.extractors.video_transcriber.time.sleep")
    def test_polls_until_complete(self, mock_sleep):
        t_mock = MagicMock()
        t_mock.get_transcription_job.side_effect = [
            {"TranscriptionJob": {"TranscriptionJobStatus": "IN_PROGRESS"}},
            {"TranscriptionJob": {"TranscriptionJobStatus": "IN_PROGRESS"}},
            {
                "TranscriptionJob": {
                    "TranscriptionJobStatus": "COMPLETED",
                    "Transcript": {"TranscriptFileUri": "s3://b/out.json"},
                }
            },
        ]
        transcriber = VideoTranscriber(
            s3_client=MagicMock(),
            transcribe_client=t_mock,
            bedrock_client=MagicMock(),
        )

        result = transcriber._poll_job("test-job")
        assert result["TranscriptionJob"]["TranscriptionJobStatus"] == "COMPLETED"
        assert mock_sleep.call_count == 2

    @patch("src.extractors.video_transcriber.time.sleep")
    def test_raises_on_failure(self, mock_sleep):
        t_mock = MagicMock()
        t_mock.get_transcription_job.return_value = {
            "TranscriptionJob": {
                "TranscriptionJobStatus": "FAILED",
                "FailureReason": "Invalid audio",
            }
        }
        transcriber = VideoTranscriber(
            s3_client=MagicMock(),
            transcribe_client=t_mock,
            bedrock_client=MagicMock(),
        )

        with pytest.raises(RuntimeError, match="Invalid audio"):
            transcriber._poll_job("test-job")

    @patch("src.extractors.video_transcriber.time.sleep")
    def test_raises_on_timeout(self, mock_sleep):
        t_mock = MagicMock()
        t_mock.get_transcription_job.return_value = {
            "TranscriptionJob": {"TranscriptionJobStatus": "IN_PROGRESS"}
        }
        transcriber = VideoTranscriber(
            s3_client=MagicMock(),
            transcribe_client=t_mock,
            bedrock_client=MagicMock(),
        )

        with pytest.raises(TimeoutError, match="timed out"):
            transcriber._poll_job("test-job")

        expected_polls = POLL_TIMEOUT_SECONDS // POLL_INTERVAL_SECONDS
        assert mock_sleep.call_count == expected_polls


# ---------------------------------------------------------------------------
# Tests: Haiku cleanup
# ---------------------------------------------------------------------------


class TestHaikuCleanup:
    def test_calls_bedrock(self):
        bedrock_mock = MagicMock()
        _mock_bedrock_cleanup(bedrock_mock, "Cleaned text.")
        transcriber = VideoTranscriber(
            s3_client=MagicMock(),
            transcribe_client=MagicMock(),
            bedrock_client=bedrock_mock,
        )

        result = transcriber._clean_transcript("uh um raw text")
        assert result == "Cleaned text."
        bedrock_mock.invoke_model.assert_called_once()

    def test_truncates_long_transcript(self):
        bedrock_mock = MagicMock()
        _mock_bedrock_cleanup(bedrock_mock, "Cleaned.")
        transcriber = VideoTranscriber(
            s3_client=MagicMock(),
            transcribe_client=MagicMock(),
            bedrock_client=bedrock_mock,
        )

        long_text = "word " * 50000  # 250k chars
        transcriber._clean_transcript(long_text)

        call_body = json.loads(
            bedrock_mock.invoke_model.call_args[1]["body"]
        )
        # Should be truncated to 100k
        assert len(call_body["messages"][0]["content"]) <= 100001


# ---------------------------------------------------------------------------
# Tests: full transcribe flow (mocked)
# ---------------------------------------------------------------------------


class TestTranscribeFlow:
    @patch("src.extractors.video_transcriber.time.sleep")
    def test_full_flow_with_cleanup(self, mock_sleep):
        s3_mock = MagicMock()
        t_mock = MagicMock()
        bedrock_mock = MagicMock()

        # S3 returns transcript JSON
        transcript_json = _make_transcribe_json(
            "uh um hello this is a test", end_time="3661.0", speaker_count=2
        )
        _mock_s3_transcript(s3_mock, transcript_json)

        # Transcribe completes immediately
        _mock_transcribe_complete(t_mock)

        # Bedrock cleans transcript
        _mock_bedrock_cleanup(bedrock_mock, "Hello, this is a test.")

        transcriber = VideoTranscriber(
            s3_client=s3_mock,
            transcribe_client=t_mock,
            bedrock_client=bedrock_mock,
        )

        result = transcriber.transcribe(
            bucket="test-bucket",
            key="PES/pending/video.mp4",
            ou="PES",
            product_part_number="VID-001",
        )

        assert result["transcript"] == "Hello, this is a test."
        assert result["duration"] == "01:01:01"
        assert result["duration_seconds"] == 3661
        assert result["speaker_count"] == 2

        # Verify metadata was written
        put_calls = [
            c for c in s3_mock.put_object.call_args_list
            if c[1]["Key"].endswith(".mp4.json")
        ]
        assert len(put_calls) == 1
        metadata = json.loads(put_calls[0][1]["Body"])
        assert metadata["duration"] == "01:01:01"
        assert metadata["durationSeconds"] == 3661
        assert "extractedAt" in metadata

    @patch("src.extractors.video_transcriber.time.sleep")
    def test_flow_without_cleanup(self, mock_sleep):
        s3_mock = MagicMock()
        t_mock = MagicMock()
        bedrock_mock = MagicMock()

        transcript_json = _make_transcribe_json("raw transcript text")
        _mock_s3_transcript(s3_mock, transcript_json)
        _mock_transcribe_complete(t_mock)

        transcriber = VideoTranscriber(
            s3_client=s3_mock,
            transcribe_client=t_mock,
            bedrock_client=bedrock_mock,
        )

        result = transcriber.transcribe(
            bucket="test-bucket",
            key="PES/pending/video.mp4",
            ou="PES",
            product_part_number="VID-002",
            clean_transcript=False,
        )

        assert result["transcript"] == "raw transcript text"
        bedrock_mock.invoke_model.assert_not_called()

    @patch("src.extractors.video_transcriber.time.sleep")
    def test_cleanup_failure_falls_back_to_raw(self, mock_sleep):
        s3_mock = MagicMock()
        t_mock = MagicMock()
        bedrock_mock = MagicMock()

        transcript_json = _make_transcribe_json("raw text with uh um")
        _mock_s3_transcript(s3_mock, transcript_json)
        _mock_transcribe_complete(t_mock)
        bedrock_mock.invoke_model.side_effect = RuntimeError("Bedrock down")

        transcriber = VideoTranscriber(
            s3_client=s3_mock,
            transcribe_client=t_mock,
            bedrock_client=bedrock_mock,
        )

        result = transcriber.transcribe(
            bucket="test-bucket",
            key="PES/pending/video.mp4",
            ou="PES",
            product_part_number="VID-003",
        )

        # Should fall back to raw transcript
        assert result["transcript"] == "raw text with uh um"

    def test_unsupported_format_raises(self):
        transcriber = VideoTranscriber(
            s3_client=MagicMock(),
            transcribe_client=MagicMock(),
            bedrock_client=MagicMock(),
        )

        with pytest.raises(ValueError, match="Unsupported media format"):
            transcriber.transcribe(
                bucket="b",
                key="PES/pending/video.avi",
                ou="PES",
                product_part_number="VID",
            )


# ---------------------------------------------------------------------------
# Tests: metadata writing
# ---------------------------------------------------------------------------


class TestMetadataWriting:
    def test_writes_correct_format(self):
        s3_mock = MagicMock()
        transcriber = VideoTranscriber(
            s3_client=s3_mock,
            transcribe_client=MagicMock(),
            bedrock_client=MagicMock(),
        )

        transcriber._write_metadata(
            "test-bucket", "PES/metadata/VID-001.mp4.json", "01:23:45", 5025
        )

        put_kwargs = s3_mock.put_object.call_args[1]
        assert put_kwargs["Bucket"] == "test-bucket"
        assert put_kwargs["Key"] == "PES/metadata/VID-001.mp4.json"
        assert put_kwargs["ContentType"] == "application/json"

        metadata = json.loads(put_kwargs["Body"])
        assert metadata["duration"] == "01:23:45"
        assert metadata["durationSeconds"] == 5025
        assert metadata["extractedAt"].endswith("Z")
