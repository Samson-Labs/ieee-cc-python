"""Tests for the Video Transcriber Lambda handler."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from src.handlers.video_transcriber_handler import _parse_event, handler


MOCK_RESULT = {
    "transcript": "Hello, this is a test.",
    "duration": "01:23:45",
    "duration_seconds": 5025,
    "speaker_count": 2,
    "vtt_s3_key": "transcribe-output/ieee-rc-VID-001-123.vtt",
}


@pytest.fixture
def mock_transcriber():
    with patch("src.handlers.video_transcriber_handler._transcriber") as mock:
        mock.transcribe.return_value = MOCK_RESULT.copy()
        yield mock


# ---------------------------------------------------------------------------
# Direct invocation
# ---------------------------------------------------------------------------


class TestDirectInvocation:
    def test_success(self, mock_transcriber):
        event = {
            "bucket": "test-bucket",
            "key": "PES/pending/video.mp4",
            "ou": "PES",
            "product_part_number": "VID-001",
        }
        result = handler(event, None)

        assert result["statusCode"] == 200
        assert result["body"]["transcript"] == "Hello, this is a test."
        assert result["body"]["duration"] == "01:23:45"
        assert result["body"]["duration_seconds"] == 5025
        assert result["body"]["speaker_count"] == 2
        assert result["body"]["vtt_s3_key"] == "transcribe-output/ieee-rc-VID-001-123.vtt"

    def test_derives_ou_from_key(self, mock_transcriber):
        event = {
            "bucket": "test-bucket",
            "key": "SPS/pending/lecture.mp4",
        }
        result = handler(event, None)

        assert result["statusCode"] == 200
        mock_transcriber.transcribe.assert_called_once_with(
            bucket="test-bucket",
            key="SPS/pending/lecture.mp4",
            ou="SPS",
            product_part_number="lecture",
            clean_transcript=True,
        )

    def test_missing_fields_returns_400(self, mock_transcriber):
        event = {"something": "else"}
        result = handler(event, None)

        assert result["statusCode"] == 400
        mock_transcriber.transcribe.assert_not_called()

    def test_passes_clean_transcript_false(self, mock_transcriber):
        event = {
            "bucket": "b",
            "key": "PES/pending/v.mp4",
            "ou": "PES",
            "product_part_number": "V1",
            "clean_transcript": False,
        }
        handler(event, None)

        mock_transcriber.transcribe.assert_called_once_with(
            bucket="b",
            key="PES/pending/v.mp4",
            ou="PES",
            product_part_number="V1",
            clean_transcript=False,
        )


# ---------------------------------------------------------------------------
# S3 event invocation
# ---------------------------------------------------------------------------


class TestS3EventInvocation:
    def _s3_event(self, bucket: str, key: str) -> dict:
        return {
            "Records": [{
                "s3": {
                    "bucket": {"name": bucket},
                    "object": {"key": key},
                }
            }]
        }

    def test_success_mp4(self, mock_transcriber):
        event = self._s3_event("test-bucket", "PES/pending/video.mp4")
        result = handler(event, None)

        assert result["statusCode"] == 200
        mock_transcriber.transcribe.assert_called_once()

    def test_success_mov(self, mock_transcriber):
        event = self._s3_event("test-bucket", "PES/pending/video.mov")
        result = handler(event, None)

        assert result["statusCode"] == 200

    def test_success_webm(self, mock_transcriber):
        event = self._s3_event("test-bucket", "PES/pending/video.webm")
        result = handler(event, None)

        assert result["statusCode"] == 200

    def test_invalid_key_pattern_returns_400(self, mock_transcriber):
        event = self._s3_event("b", "wrong/path/file.mp4")
        result = handler(event, None)

        assert result["statusCode"] == 400
        mock_transcriber.transcribe.assert_not_called()

    def test_unsupported_format_returns_400(self, mock_transcriber):
        event = self._s3_event("b", "PES/pending/file.avi")
        result = handler(event, None)

        assert result["statusCode"] == 400


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_s3_client_error_returns_500(self, mock_transcriber):
        mock_transcriber.transcribe.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
            "GetObject",
        )
        event = {
            "bucket": "b",
            "key": "PES/pending/v.mp4",
            "ou": "PES",
            "product_part_number": "V1",
        }
        result = handler(event, None)

        assert result["statusCode"] == 500
        assert "NoSuchKey" in result["body"]["error"]

    def test_timeout_returns_500(self, mock_transcriber):
        mock_transcriber.transcribe.side_effect = TimeoutError("Job timed out")
        event = {
            "bucket": "b",
            "key": "PES/pending/v.mp4",
            "ou": "PES",
            "product_part_number": "V1",
        }
        result = handler(event, None)

        assert result["statusCode"] == 500
        assert "timed out" in result["body"]["error"]

    def test_validation_error_returns_400(self, mock_transcriber):
        mock_transcriber.transcribe.side_effect = ValueError("Unsupported format")
        event = {
            "bucket": "b",
            "key": "PES/pending/v.mp4",
            "ou": "PES",
            "product_part_number": "V1",
        }
        result = handler(event, None)

        assert result["statusCode"] == 400

    def test_runtime_error_returns_500(self, mock_transcriber):
        mock_transcriber.transcribe.side_effect = RuntimeError("Transcribe failed")
        event = {
            "bucket": "b",
            "key": "PES/pending/v.mp4",
            "ou": "PES",
            "product_part_number": "V1",
        }
        result = handler(event, None)

        assert result["statusCode"] == 500

    def test_unexpected_error_returns_500(self, mock_transcriber):
        mock_transcriber.transcribe.side_effect = TypeError("boom")
        event = {
            "bucket": "b",
            "key": "PES/pending/v.mp4",
            "ou": "PES",
            "product_part_number": "V1",
        }
        result = handler(event, None)

        assert result["statusCode"] == 500
        assert "TypeError" in result["body"]["error"]


# ---------------------------------------------------------------------------
# Event parsing
# ---------------------------------------------------------------------------


class TestParseEvent:
    def test_direct_event(self):
        bucket, key, ou, ppn = _parse_event({
            "bucket": "b",
            "key": "PES/pending/v.mp4",
            "ou": "PES",
            "product_part_number": "V1",
        })
        assert bucket == "b"
        assert ou == "PES"
        assert ppn == "V1"

    def test_s3_event(self):
        bucket, key, ou, ppn = _parse_event({
            "Records": [{
                "s3": {
                    "bucket": {"name": "b"},
                    "object": {"key": "SPS/pending/lecture.mp4"},
                }
            }]
        })
        assert bucket == "b"
        assert ou == "SPS"
        assert ppn == "lecture"

    def test_derives_missing_ou_from_key(self):
        bucket, key, ou, ppn = _parse_event({
            "bucket": "b",
            "key": "PES/pending/video.webm",
        })
        assert ou == "PES"
        assert ppn == "video"

    def test_missing_key_and_records_raises(self):
        with pytest.raises(KeyError):
            _parse_event({"foo": "bar"})

    def test_bad_key_without_ou_raises(self):
        with pytest.raises(ValueError, match="Must provide"):
            _parse_event({"bucket": "b", "key": "some/random/path.mp4"})
