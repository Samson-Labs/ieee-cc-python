"""Tests for the DLQ handler Lambda entry point."""

from unittest.mock import patch

from src.handlers.dlq_handler import handler
from tests.conftest import make_dlq_message, make_sqs_record


class TestDLQHandler:
    def test_processes_multiple_records(self):
        msg1 = make_dlq_message(error_type="ValidationError", is_retriable=False)
        msg2 = make_dlq_message(error_type="BedrockError", is_retriable=True)
        event = {
            "Records": [
                make_sqs_record(msg1, "msg-001"),
                make_sqs_record(msg2, "msg-002"),
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
        msg = make_dlq_message()
        event = {
            "Records": [
                make_sqs_record(msg, "msg-001"),
                make_sqs_record(msg, "msg-002"),
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
