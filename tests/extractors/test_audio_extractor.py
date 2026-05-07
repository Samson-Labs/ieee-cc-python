"""Tests for AudioExtractor (MediaConvert audio-extraction module)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.common.exceptions import MediaConvertError
from src.extractors.audio_extractor import (
    POLL_TIMEOUT_SECONDS,
    RETRIABLE_ERROR_CODES,
    AudioExtractor,
)

ROLE_ARN = "arn:aws:iam::123456789012:role/ieee-cc-mediaconvert-dev-role"
ENDPOINT = "https://abcd1234.mediaconvert.us-east-1.amazonaws.com"


def _mock_complete_job(mc_mock):
    """Set up the MediaConvert mock for a successful job.

    The output URI is derived deterministically from the source/destination,
    not read from the job response.
    """
    mc_mock.create_job.return_value = {"Job": {"Id": "job-123"}}
    mc_mock.get_job.return_value = {
        "Job": {"Status": "COMPLETE", "OutputGroupDetails": [{"OutputDetails": [{}]}]}
    }


class TestExtractAudioJobSpec:
    def test_create_job_payload_shape(self):
        mc_mock = MagicMock()
        _mock_complete_job(mc_mock)

        extractor = AudioExtractor(
            mediaconvert_client=mc_mock, role_arn=ROLE_ARN, endpoint_url=ENDPOINT
        )
        extractor.extract_audio(
            source_uri="s3://bucket/PES/pending/in.mp4",
            output_bucket="bucket",
            output_key_prefix="transcribe-input/job",
        )

        mc_mock.create_job.assert_called_once()
        call_kwargs = mc_mock.create_job.call_args[1]
        assert call_kwargs["Role"] == ROLE_ARN
        assert call_kwargs["StatusUpdateInterval"] == "SECONDS_30"

        settings = call_kwargs["Settings"]
        assert settings["Inputs"][0]["FileInput"] == "s3://bucket/PES/pending/in.mp4"

        og = settings["OutputGroups"][0]
        assert og["OutputGroupSettings"]["Type"] == "FILE_GROUP_SETTINGS"
        assert (
            og["OutputGroupSettings"]["FileGroupSettings"]["Destination"]
            == "s3://bucket/transcribe-input/job/"
        )

        out = og["Outputs"][0]
        assert out["ContainerSettings"]["Container"] == "RAW"
        assert out["NameModifier"] == "-audio"

        codec = out["AudioDescriptions"][0]["CodecSettings"]
        assert codec["Codec"] == "MP3"
        assert codec["Mp3Settings"] == {
            "Bitrate": 128_000,
            "Channels": 1,
            "RateControlMode": "CBR",
            "SampleRate": 44_100,
        }

    def test_returns_output_uri_derived_from_source(self):
        mc_mock = MagicMock()
        _mock_complete_job(mc_mock)

        extractor = AudioExtractor(
            mediaconvert_client=mc_mock, role_arn=ROLE_ARN, endpoint_url=ENDPOINT
        )
        result = extractor.extract_audio(
            source_uri="s3://bucket/PES/pending/in.mp4",
            output_bucket="bucket",
            output_key_prefix="transcribe-input/job",
        )

        assert result == "s3://bucket/transcribe-input/job/in-audio.mp3"

    def test_returns_output_uri_for_basename_without_extension(self):
        mc_mock = MagicMock()
        _mock_complete_job(mc_mock)

        extractor = AudioExtractor(
            mediaconvert_client=mc_mock, role_arn=ROLE_ARN, endpoint_url=ENDPOINT
        )
        result = extractor.extract_audio(
            source_uri="s3://bucket/PES/pending/noext",
            output_bucket="bucket",
            output_key_prefix="transcribe-input/job",
        )

        assert result == "s3://bucket/transcribe-input/job/noext-audio.mp3"


class TestPolling:
    @patch("src.extractors.audio_extractor.time.sleep")
    def test_progressing_then_complete(self, mock_sleep):
        mc_mock = MagicMock()
        mc_mock.create_job.return_value = {"Job": {"Id": "job-1"}}
        mc_mock.get_job.side_effect = [
            {"Job": {"Status": "PROGRESSING"}},
            {"Job": {"Status": "COMPLETE"}},
        ]

        extractor = AudioExtractor(
            mediaconvert_client=mc_mock, role_arn=ROLE_ARN, endpoint_url=ENDPOINT
        )
        result = extractor.extract_audio("s3://b/in.mp4", "b", "transcribe-input/job")

        assert result == "s3://b/transcribe-input/job/in-audio.mp3"
        assert mc_mock.get_job.call_count == 2

    @patch("src.extractors.audio_extractor.time.sleep")
    def test_error_status_raises(self, mock_sleep):
        mc_mock = MagicMock()
        mc_mock.create_job.return_value = {"Job": {"Id": "job-2"}}
        mc_mock.get_job.return_value = {
            "Job": {
                "Status": "ERROR",
                "ErrorCode": "1404",
                "ErrorMessage": "Unable to read input",
            }
        }

        extractor = AudioExtractor(
            mediaconvert_client=mc_mock, role_arn=ROLE_ARN, endpoint_url=ENDPOINT
        )

        with pytest.raises(MediaConvertError, match="Unable to read input"):
            extractor.extract_audio("s3://b/in.mp4", "b", "transcribe-input/job")

    @patch("src.extractors.audio_extractor.time.sleep")
    def test_canceled_status_raises(self, mock_sleep):
        mc_mock = MagicMock()
        mc_mock.create_job.return_value = {"Job": {"Id": "job-3"}}
        mc_mock.get_job.return_value = {"Job": {"Status": "CANCELED"}}

        extractor = AudioExtractor(
            mediaconvert_client=mc_mock, role_arn=ROLE_ARN, endpoint_url=ENDPOINT
        )

        with pytest.raises(MediaConvertError, match="canceled"):
            extractor.extract_audio("s3://b/in.mp4", "b", "transcribe-input/job")

    @patch("src.extractors.audio_extractor.time.sleep")
    def test_timeout_raises(self, mock_sleep):
        mc_mock = MagicMock()
        mc_mock.create_job.return_value = {"Job": {"Id": "job-4"}}
        mc_mock.get_job.return_value = {"Job": {"Status": "PROGRESSING"}}

        extractor = AudioExtractor(
            mediaconvert_client=mc_mock, role_arn=ROLE_ARN, endpoint_url=ENDPOINT
        )

        with pytest.raises(MediaConvertError, match="timed out"):
            extractor.extract_audio(
                "s3://b/in.mp4", "b", "transcribe-input/job", max_attempts=1
            )

        # POLL_TIMEOUT_SECONDS / POLL_INTERVAL_SECONDS = 600 / 30 = 20 polls
        assert mc_mock.get_job.call_count == POLL_TIMEOUT_SECONDS // 30


class TestRetryOnTransientErrors:
    """Option A: in-Lambda retry on MediaConvert transient demuxer errors.

    1401 / 1402 (Audio input pipeline / Demuxer failures) are typically
    caused by S3 read flakiness on freshly-staged large MP4s; they
    resolve within a single retry once the source bytes are fully
    cache-coherent.
    """

    @patch("src.extractors.audio_extractor.time.sleep")
    def test_retries_on_1401_then_succeeds(self, mock_sleep):
        mc_mock = MagicMock()
        mc_mock.create_job.side_effect = [
            {"Job": {"Id": "attempt-1"}},
            {"Job": {"Id": "attempt-2"}},
        ]
        mc_mock.get_job.side_effect = [
            {
                "Job": {
                    "Status": "ERROR",
                    "ErrorCode": 1401,
                    "ErrorMessage": "Demuxer: Failed to read data",
                }
            },
            {"Job": {"Status": "COMPLETE"}},
        ]

        extractor = AudioExtractor(
            mediaconvert_client=mc_mock, role_arn=ROLE_ARN, endpoint_url=ENDPOINT
        )
        result = extractor.extract_audio("s3://b/in.mp4", "b", "transcribe-input/job")

        assert result == "s3://b/transcribe-input/job/in-audio.mp3"
        assert mc_mock.create_job.call_count == 2

    @patch("src.extractors.audio_extractor.time.sleep")
    def test_does_not_retry_on_non_retriable_code(self, mock_sleep):
        mc_mock = MagicMock()
        mc_mock.create_job.return_value = {"Job": {"Id": "single"}}
        mc_mock.get_job.return_value = {
            "Job": {
                "Status": "ERROR",
                "ErrorCode": 1404,
                "ErrorMessage": "Codec not supported",
            }
        }

        extractor = AudioExtractor(
            mediaconvert_client=mc_mock, role_arn=ROLE_ARN, endpoint_url=ENDPOINT
        )

        with pytest.raises(MediaConvertError, match="Codec not supported"):
            extractor.extract_audio("s3://b/in.mp4", "b", "transcribe-input/job")

        # 1404 is not retriable — exactly one job submitted.
        assert mc_mock.create_job.call_count == 1

    @patch("src.extractors.audio_extractor.time.sleep")
    def test_exhausts_retries_then_raises(self, mock_sleep):
        mc_mock = MagicMock()
        mc_mock.create_job.side_effect = [
            {"Job": {"Id": f"attempt-{i}"}} for i in range(1, 4)
        ]
        mc_mock.get_job.return_value = {
            "Job": {
                "Status": "ERROR",
                "ErrorCode": 1401,
                "ErrorMessage": "Demuxer: Failed to read data",
            }
        }

        extractor = AudioExtractor(
            mediaconvert_client=mc_mock, role_arn=ROLE_ARN, endpoint_url=ENDPOINT
        )

        with pytest.raises(MediaConvertError, match="Failed to read data"):
            extractor.extract_audio(
                "s3://b/in.mp4", "b", "transcribe-input/job", max_attempts=3
            )

        assert mc_mock.create_job.call_count == 3

    def test_retriable_codes_documented(self):
        # Sanity check that the documented codes are what's wired in.
        assert RETRIABLE_ERROR_CODES == frozenset({1401, 1402})


class TestEndpointDiscovery:
    @patch("src.extractors.audio_extractor.boto3.client")
    def test_env_var_skips_describe_endpoints(self, boto_client_mock, monkeypatch):
        monkeypatch.setenv("MEDIACONVERT_ROLE_ARN", ROLE_ARN)
        monkeypatch.setenv("MEDIACONVERT_ENDPOINT", ENDPOINT)

        AudioExtractor()

        # boto3.client called once for the mediaconvert client itself, with
        # endpoint_url passed through. No describe_endpoints discovery call.
        boto_client_mock.assert_called_once_with(
            "mediaconvert", endpoint_url=ENDPOINT
        )

    @patch("src.extractors.audio_extractor.boto3.client")
    def test_missing_endpoint_triggers_discovery(self, boto_client_mock, monkeypatch):
        monkeypatch.setenv("MEDIACONVERT_ROLE_ARN", ROLE_ARN)
        monkeypatch.delenv("MEDIACONVERT_ENDPOINT", raising=False)

        discovery_client = MagicMock()
        discovery_client.describe_endpoints.return_value = {
            "Endpoints": [{"Url": ENDPOINT}]
        }
        runtime_client = MagicMock()
        boto_client_mock.side_effect = [discovery_client, runtime_client]

        extractor = AudioExtractor()

        discovery_client.describe_endpoints.assert_called_once()
        # First call discovers, second creates the runtime client with the
        # discovered endpoint.
        assert boto_client_mock.call_args_list[0].args == ("mediaconvert",)
        assert boto_client_mock.call_args_list[1] == (
            ("mediaconvert",),
            {"endpoint_url": ENDPOINT},
        )
        assert extractor._mc is runtime_client

    def test_missing_role_arn_raises(self, monkeypatch):
        monkeypatch.delenv("MEDIACONVERT_ROLE_ARN", raising=False)

        with pytest.raises(MediaConvertError, match="MEDIACONVERT_ROLE_ARN"):
            AudioExtractor(mediaconvert_client=MagicMock(), endpoint_url=ENDPOINT)
