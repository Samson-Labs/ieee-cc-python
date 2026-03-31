"""Shared pytest fixtures for the ieee-cc-python test suite."""

import json
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError


@pytest.fixture
def mock_s3_client():
    return MagicMock()


@pytest.fixture
def mock_lambda_client():
    return MagicMock()


@pytest.fixture
def mock_sns_client():
    return MagicMock()


@pytest.fixture
def make_client_error():
    """Factory fixture that returns a function for creating ClientError instances."""

    def _make(code: str = "NoSuchKey", message: str = "Not found") -> ClientError:
        return ClientError(
            {"Error": {"Code": code, "Message": message}},
            "TestOperation",
        )

    return _make


# --- DLQ test data factories (shared by test_dlq_processor and test_dlq_handler) ---


def make_sqs_record(message: dict, message_id: str = "msg-001") -> dict:
    """Build a minimal SQS record dict for testing."""
    return {"messageId": message_id, "body": json.dumps(message)}


def make_dlq_message(
    error_type: str = "BedrockError",
    error_message: str = "throttled",
    is_retriable: bool = True,
    correlation_id: str = "req-123",
    retry_count: int = 0,
) -> dict:
    """Build a DLQ message payload for testing."""
    return {
        "original_event": {"bucket": "test-bucket", "key": "PES/pending/STD-123.pdf"},
        "error": {
            "error_type": error_type,
            "error_message": error_message,
            "is_retriable": is_retriable,
            "correlation_id": correlation_id,
            "timestamp": "2026-03-20T00:00:00+00:00",
            "stack_trace": "Traceback ...",
        },
        "retry_count": retry_count,
    }
