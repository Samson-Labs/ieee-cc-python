"""Domain-specific exception hierarchy for the IEEE CC pipeline."""

from __future__ import annotations


class PipelineError(Exception):
    """Base exception for all pipeline errors."""

    error_type: str = "PipelineError"
    is_retriable: bool = False

    def __init__(self, message: str, *, is_retriable: bool | None = None, details: dict | None = None):
        super().__init__(message)
        if is_retriable is not None:
            self.is_retriable = is_retriable
        self.details = details or {}


class TranscribeError(PipelineError):
    """AWS Transcribe failures (job timeout, service error)."""

    error_type: str = "TranscribeError"
    is_retriable: bool = True


class BedrockError(PipelineError):
    """AWS Bedrock / Claude inference failures (throttling, bad response)."""

    error_type: str = "BedrockError"
    is_retriable: bool = True


class WebhookError(PipelineError):
    """Webhook delivery failures (HTTP 5xx, connection error)."""

    error_type: str = "WebhookError"
    is_retriable: bool = True


class S3Error(PipelineError):
    """S3 read/write failures."""

    error_type: str = "S3Error"
    is_retriable: bool = True


class ValidationError(PipelineError):
    """Input validation failures (bad payload, missing fields)."""

    error_type: str = "ValidationError"
    is_retriable: bool = False
