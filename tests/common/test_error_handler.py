"""Tests for the structured error response builder."""

from unittest.mock import patch
from datetime import datetime, timezone

from src.common.error_handler import build_error_response
from src.common.exceptions import (
    PipelineError,
    ValidationError,
    BedrockError,
    S3Error,
)


class TestStatusCodes:
    def test_validation_error_returns_400(self):
        exc = ValidationError("missing field")
        resp = build_error_response(exc)
        assert resp["statusCode"] == 400

    def test_value_error_returns_400(self):
        resp = build_error_response(ValueError("bad value"))
        assert resp["statusCode"] == 400

    def test_key_error_returns_400(self):
        resp = build_error_response(KeyError("missing_key"))
        assert resp["statusCode"] == 400

    def test_retriable_pipeline_error_returns_502(self):
        exc = BedrockError("throttled")
        resp = build_error_response(exc)
        assert resp["statusCode"] == 502

    def test_generic_exception_returns_500(self):
        resp = build_error_response(RuntimeError("unexpected"))
        assert resp["statusCode"] == 500

    def test_non_retriable_pipeline_error_returns_500(self):
        exc = PipelineError("unknown")
        resp = build_error_response(exc)
        assert resp["statusCode"] == 500

    def test_status_code_override(self):
        exc = ValidationError("bad")
        resp = build_error_response(exc, status_code=503)
        assert resp["statusCode"] == 503


class TestBody:
    def test_contains_required_fields(self):
        exc = BedrockError("timeout")
        resp = build_error_response(exc, correlation_id="req-123")
        body = resp["body"]
        assert body["error_type"] == "BedrockError"
        assert body["error_message"] == "timeout"
        assert body["correlation_id"] == "req-123"
        assert "timestamp" in body
        assert "stack_trace" in body

    def test_generic_exception_uses_class_name(self):
        resp = build_error_response(RuntimeError("oops"))
        assert resp["body"]["error_type"] == "RuntimeError"

    def test_timestamp_is_iso_format(self):
        resp = build_error_response(RuntimeError("x"))
        ts = resp["body"]["timestamp"]
        # Should parse without error
        datetime.fromisoformat(ts)

    def test_empty_correlation_id_by_default(self):
        resp = build_error_response(RuntimeError("x"))
        assert resp["body"]["correlation_id"] == ""


class TestStackTrace:
    def test_stack_trace_present(self):
        try:
            raise S3Error("bucket not found")
        except S3Error as exc:
            resp = build_error_response(exc)

        assert "S3Error" in resp["body"]["stack_trace"]
        assert "bucket not found" in resp["body"]["stack_trace"]

    def test_stack_trace_truncated_at_2000_chars(self):
        try:
            raise RuntimeError("x" * 3000)
        except RuntimeError as exc:
            resp = build_error_response(exc)

        assert len(resp["body"]["stack_trace"]) <= 2000
