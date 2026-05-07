"""Tests for AudioExtractor (MediaConvert audio-extraction module)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.common.exceptions import MediaConvertError
from src.extractors.audio_extractor import (
    POLL_TIMEOUT_SECONDS,
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
            extractor.extract_audio("s3://b/in.mp4", "b", "transcribe-input/job")

        # POLL_TIMEOUT_SECONDS / POLL_INTERVAL_SECONDS = 600 / 30 = 20 polls
        assert mc_mock.get_job.call_count == POLL_TIMEOUT_SECONDS // 30



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
