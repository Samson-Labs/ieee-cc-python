"""Audio extraction module for IEEE Content Conversion pipeline.

Submits an AWS Elemental MediaConvert job that strips video and emits a
mono 128 kbps MP3 to S3. Used to bring oversized MP4s under the 2 GB
AWS Transcribe input limit.
"""

from __future__ import annotations

import logging
import os
import time

import boto3

from src.common.exceptions import MediaConvertError

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 30
POLL_TIMEOUT_SECONDS = 600

MP3_BITRATE = 128_000
MP3_SAMPLE_RATE = 44_100
MP3_CHANNELS = 1
NAME_MODIFIER = "-audio"


class AudioExtractor:
    """Extracts audio from a video in S3 via AWS MediaConvert."""

    def __init__(
        self,
        mediaconvert_client=None,
        role_arn: str | None = None,
        endpoint_url: str | None = None,
    ):
        self._role_arn = role_arn or os.environ.get("MEDIACONVERT_ROLE_ARN")
        if not self._role_arn:
            raise MediaConvertError(
                "MEDIACONVERT_ROLE_ARN env var or role_arn arg is required"
            )

        self._endpoint = endpoint_url or os.environ.get("MEDIACONVERT_ENDPOINT")
        if mediaconvert_client is not None:
            self._mc = mediaconvert_client
        else:
            if not self._endpoint:
                self._endpoint = self._discover_endpoint()
            self._mc = boto3.client("mediaconvert", endpoint_url=self._endpoint)

    def extract_audio(
        self,
        source_uri: str,
        output_bucket: str,
        output_key_prefix: str,
    ) -> str:
        """Submit a MediaConvert job, poll until done, return MP3 S3 URI.

        Args:
            source_uri: ``s3://bucket/key`` of the source video.
            output_bucket: S3 bucket for the extracted audio.
            output_key_prefix: Key prefix (no trailing slash) under which
                MediaConvert writes ``{basename}{NAME_MODIFIER}.mp3``.

        Returns:
            ``s3://bucket/key`` of the resulting MP3.

        Raises:
            MediaConvertError: If the job fails, is canceled, or polling
                exceeds POLL_TIMEOUT_SECONDS.
        """
        destination = f"s3://{output_bucket}/{output_key_prefix}/"
        job_settings = self._build_job_settings(source_uri, destination)

        logger.info(
            "Submitting MediaConvert job: source=%s destination=%s",
            source_uri,
            destination,
        )
        response = self._mc.create_job(
            Role=self._role_arn,
            Settings=job_settings,
            StatusUpdateInterval="SECONDS_30",
        )
        job_id = response["Job"]["Id"]
        logger.info("MediaConvert job %s submitted", job_id)

        self._poll_job(job_id)
        # MediaConvert's GetJob response does not return the output URI for
        # FILE_GROUP_SETTINGS outputs, so we compute it deterministically:
        #   {destination}{source_basename_no_ext}{NameModifier}.mp3
        return self._compute_output_uri(source_uri, destination)

    @staticmethod
    def _build_job_settings(source_uri: str, destination: str) -> dict:
        """Build the Settings payload for a mono 128 kbps MP3 extraction."""
        return {
            "Inputs": [
                {
                    "FileInput": source_uri,
                    "AudioSelectors": {
                        "Audio Selector 1": {"DefaultSelection": "DEFAULT"},
                    },
                    "TimecodeSource": "ZEROBASED",
                }
            ],
            "OutputGroups": [
                {
                    "OutputGroupSettings": {
                        "Type": "FILE_GROUP_SETTINGS",
                        "FileGroupSettings": {"Destination": destination},
                    },
                    "Outputs": [
                        {
                            "ContainerSettings": {"Container": "RAW"},
                            "AudioDescriptions": [
                                {
                                    "AudioSourceName": "Audio Selector 1",
                                    "CodecSettings": {
                                        "Codec": "MP3",
                                        "Mp3Settings": {
                                            "Bitrate": MP3_BITRATE,
                                            "Channels": MP3_CHANNELS,
                                            "RateControlMode": "CBR",
                                            "SampleRate": MP3_SAMPLE_RATE,
                                        },
                                    },
                                }
                            ],
                            "NameModifier": NAME_MODIFIER,
                        }
                    ],
                }
            ],
        }

    def _poll_job(self, job_id: str) -> dict:
        """Poll a MediaConvert job until terminal state or timeout."""
        elapsed = 0
        while elapsed < POLL_TIMEOUT_SECONDS:
            response = self._mc.get_job(Id=job_id)
            job = response["Job"]
            status = job["Status"]

            if status == "COMPLETE":
                logger.info("MediaConvert job %s complete", job_id)
                return job
            if status in ("ERROR", "CANCELED"):
                error_code = job.get("ErrorCode", "Unknown")
                error_message = job.get("ErrorMessage", "Unknown failure")
                raise MediaConvertError(
                    f"MediaConvert job {job_id} {status.lower()}: "
                    f"[{error_code}] {error_message}"
                )

            logger.info(
                "MediaConvert job %s status: %s (elapsed %ds)",
                job_id,
                status,
                elapsed,
            )
            time.sleep(POLL_INTERVAL_SECONDS)
            elapsed += POLL_INTERVAL_SECONDS

        raise MediaConvertError(
            f"MediaConvert job {job_id} timed out after {POLL_TIMEOUT_SECONDS}s"
        )

    @staticmethod
    def _compute_output_uri(source_uri: str, destination: str) -> str:
        """Derive the resulting MP3 S3 URI from the job inputs.

        MediaConvert's FILE_GROUP_SETTINGS output writes to
        ``{destination}{source_basename_without_ext}{NameModifier}.{codec_ext}``.
        For MP3 codec in a RAW container, the extension is ``.mp3``.
        """
        basename = source_uri.rsplit("/", 1)[-1]
        stem = basename.rsplit(".", 1)[0] if "." in basename else basename
        return f"{destination}{stem}{NAME_MODIFIER}.mp3"

    @staticmethod
    def _discover_endpoint() -> str:
        """Look up the account-scoped MediaConvert endpoint URL."""
        client = boto3.client("mediaconvert")
        endpoints = client.describe_endpoints()["Endpoints"]
        if not endpoints:
            raise MediaConvertError(
                "describe_endpoints returned no endpoints for this account"
            )
        return endpoints[0]["Url"]
