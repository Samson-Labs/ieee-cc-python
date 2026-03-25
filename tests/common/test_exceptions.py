"""Tests for pipeline exception hierarchy."""

from src.common.exceptions import (
    PipelineError,
    TranscribeError,
    BedrockError,
    WebhookError,
    S3Error,
    ValidationError,
)


class TestPipelineError:
    def test_message(self):
        exc = PipelineError("something broke")
        assert str(exc) == "something broke"

    def test_default_not_retriable(self):
        exc = PipelineError("fail")
        assert exc.is_retriable is False

    def test_override_retriable(self):
        exc = PipelineError("fail", is_retriable=True)
        assert exc.is_retriable is True

    def test_default_error_type(self):
        exc = PipelineError("fail")
        assert exc.error_type == "PipelineError"

    def test_details_default_empty(self):
        exc = PipelineError("fail")
        assert exc.details == {}

    def test_details_passed(self):
        exc = PipelineError("fail", details={"key": "val"})
        assert exc.details == {"key": "val"}

    def test_is_exception(self):
        assert issubclass(PipelineError, Exception)


class TestTranscribeError:
    def test_retriable_by_default(self):
        exc = TranscribeError("timeout")
        assert exc.is_retriable is True

    def test_error_type(self):
        assert TranscribeError("x").error_type == "TranscribeError"

    def test_override_not_retriable(self):
        exc = TranscribeError("permanent", is_retriable=False)
        assert exc.is_retriable is False

    def test_is_pipeline_error(self):
        assert issubclass(TranscribeError, PipelineError)


class TestBedrockError:
    def test_retriable_by_default(self):
        assert BedrockError("throttled").is_retriable is True

    def test_error_type(self):
        assert BedrockError("x").error_type == "BedrockError"

    def test_override_not_retriable(self):
        exc = BedrockError("invalid JSON", is_retriable=False)
        assert exc.is_retriable is False


class TestWebhookError:
    def test_retriable_by_default(self):
        assert WebhookError("5xx").is_retriable is True

    def test_error_type(self):
        assert WebhookError("x").error_type == "WebhookError"


class TestS3Error:
    def test_retriable_by_default(self):
        assert S3Error("timeout").is_retriable is True

    def test_error_type(self):
        assert S3Error("x").error_type == "S3Error"


class TestValidationError:
    def test_not_retriable_by_default(self):
        assert ValidationError("bad input").is_retriable is False

    def test_error_type(self):
        assert ValidationError("x").error_type == "ValidationError"

    def test_is_pipeline_error(self):
        assert issubclass(ValidationError, PipelineError)
