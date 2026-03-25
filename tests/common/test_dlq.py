"""Tests for the DLQ message format builder."""

from datetime import datetime, timezone

import pytest

from src.common.dlq import build_dlq_message
from src.common.exceptions import BedrockError, ValidationError


SAMPLE_EVENT = {"bucket": "test-bucket", "key": "PES/pending/STD-123.pdf"}


def _raise_and_capture(exc_class, message):
    """Raise an exception so it has a real traceback, then return it."""
    try:
        raise exc_class(message)
    except Exception as exc:
        return exc


class TestBuildDLQMessage:
    def test_pipeline_error_uses_error_type_and_retriable(self):
        exc = BedrockError("throttled")
        result = build_dlq_message(SAMPLE_EVENT, exc, correlation_id="req-1")

        assert result["error"]["error_type"] == "BedrockError"
        assert result["error"]["is_retriable"] is True

    def test_non_retriable_pipeline_error(self):
        exc = ValidationError("bad input")
        result = build_dlq_message(SAMPLE_EVENT, exc)

        assert result["error"]["error_type"] == "ValidationError"
        assert result["error"]["is_retriable"] is False

    def test_generic_exception_uses_class_name(self):
        exc = RuntimeError("something broke")
        result = build_dlq_message(SAMPLE_EVENT, exc)

        assert result["error"]["error_type"] == "RuntimeError"
        assert result["error"]["is_retriable"] is False

    def test_preserves_original_event(self):
        exc = RuntimeError("fail")
        result = build_dlq_message(SAMPLE_EVENT, exc)

        assert result["original_event"] is SAMPLE_EVENT

    def test_preserves_retry_count(self):
        exc = RuntimeError("fail")
        result = build_dlq_message(SAMPLE_EVENT, exc, retry_count=3)

        assert result["retry_count"] == 3

    def test_correlation_id_included(self):
        exc = RuntimeError("fail")
        result = build_dlq_message(SAMPLE_EVENT, exc, correlation_id="abc-123")

        assert result["error"]["correlation_id"] == "abc-123"

    def test_timestamp_is_iso_utc(self):
        exc = RuntimeError("fail")
        result = build_dlq_message(SAMPLE_EVENT, exc)

        ts = datetime.fromisoformat(result["error"]["timestamp"])
        assert ts.tzinfo is not None
        assert ts.tzinfo == timezone.utc

    def test_stack_trace_included(self):
        exc = _raise_and_capture(RuntimeError, "fail with trace")
        result = build_dlq_message(SAMPLE_EVENT, exc)

        assert "RuntimeError" in result["error"]["stack_trace"]
        assert "fail with trace" in result["error"]["stack_trace"]

    def test_default_correlation_id_is_empty(self):
        exc = RuntimeError("fail")
        result = build_dlq_message(SAMPLE_EVENT, exc)

        assert result["error"]["correlation_id"] == ""

    def test_default_retry_count_is_zero(self):
        exc = RuntimeError("fail")
        result = build_dlq_message(SAMPLE_EVENT, exc)

        assert result["retry_count"] == 0
