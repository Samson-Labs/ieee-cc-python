"""Tests for the DLQ handler Lambda entry point."""

import json
from unittest.mock import patch

from src.handlers.dlq_handler import handler


def _make_sqs_record(message: dict, message_id: str = "msg-001") -> dict:
    return {"messageId": message_id, "body": json.dumps(message)}


def _make_dlq_message(
    error_type: str = "BedrockError",
    is_retriable: bool = True,
    retry_count: int = 0,
) -> dict:
    return {
        "original_event": {"bucket": "test-bucket", "key": "PES/pending/STD-123.pdf"},
        "error": {
            "error_type": error_type,
            "error_message": "test error",
            "is_retriable": is_retriable,
            "correlation_id": "req-123",
            "timestamp": "2026-03-20T00:00:00+00:00",
            "stack_trace": "Traceback ...",
        },
        "retry_count": retry_count,
    }


class TestDLQHandler:
    def test_processes_multiple_records(self):
        msg1 = _make_dlq_message(error_type="ValidationError", is_retriable=False)
        msg2 = _make_dlq_message(error_type="BedrockError", is_retriable=True)
        event = {
            "Records": [
                _make_sqs_record(msg1, "msg-001"),
                _make_sqs_record(msg2, "msg-002"),
            ]
        }

        with patch("src.handlers.dlq_handler.processor") as mock_proc:
            mock_proc.process_message.side_effect = [
                {"action": "archived"},
                {"action": "reprocessed"},
            ]
            result = handler(event, None)

        assert len(result["results"]) == 2
        assert result["batchItemFailures"] == []

    def test_partial_batch_failure(self):
        msg = _make_dlq_message()
        event = {
            "Records": [
                _make_sqs_record(msg, "msg-001"),
                _make_sqs_record(msg, "msg-002"),
            ]
        }

        with patch("src.handlers.dlq_handler.processor") as mock_proc:
            mock_proc.process_message.side_effect = [
                {"action": "archived"},
                RuntimeError("unexpected"),
            ]
            result = handler(event, None)

        assert len(result["batchItemFailures"]) == 1
        assert result["batchItemFailures"][0]["itemIdentifier"] == "msg-002"
        assert len(result["results"]) == 2

    def test_empty_records(self):
        result = handler({"Records": []}, None)

        assert result["batchItemFailures"] == []
        assert result["results"] == []

    def test_missing_records_key(self):
        result = handler({}, None)

        assert result["batchItemFailures"] == []
        assert result["results"] == []
