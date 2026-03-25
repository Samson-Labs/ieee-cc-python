"""Tests for DLQ processor and handler."""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.dlq.dlq_processor import DLQProcessor
from src.handlers.dlq_handler import handler


def _make_sqs_record(message: dict, message_id: str = "msg-001") -> dict:
    return {"messageId": message_id, "body": json.dumps(message)}


def _make_dlq_message(
    error_type: str = "BedrockError",
    error_message: str = "throttled",
    is_retriable: bool = True,
    correlation_id: str = "req-123",
    retry_count: int = 0,
) -> dict:
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


@pytest.fixture
def processor():
    lambda_mock = MagicMock()
    s3_mock = MagicMock()
    sns_mock = MagicMock()
    proc = DLQProcessor(
        lambda_client=lambda_mock,
        s3_client=s3_mock,
        sns_client=sns_mock,
    )
    return proc, lambda_mock, s3_mock, sns_mock


class TestRetriableReprocess:
    def test_reinvokes_orchestrator_when_retriable(self, processor):
        proc, lambda_mock, _, _ = processor
        message = _make_dlq_message(error_type="BedrockError", is_retriable=True, retry_count=0)
        record = _make_sqs_record(message)

        result = proc.process_message(record)

        assert result == {"action": "reprocessed"}
        lambda_mock.invoke.assert_called_once()
        call_kwargs = lambda_mock.invoke.call_args[1]
        assert call_kwargs["InvocationType"] == "Event"
        payload = json.loads(call_kwargs["Payload"])
        assert payload["is_retry"] is True
        assert payload["retry_count"] == 1
        assert payload["bucket"] == "test-bucket"

    def test_reinvokes_with_custom_function_name(self, processor):
        proc, lambda_mock, _, _ = processor
        message = _make_dlq_message(error_type="S3Error", is_retriable=True, retry_count=1)
        record = _make_sqs_record(message)

        with patch.dict("os.environ", {"ORCHESTRATOR_FUNCTION_NAME": "my-orchestrator"}):
            proc.process_message(record)

        call_kwargs = lambda_mock.invoke.call_args[1]
        assert call_kwargs["FunctionName"] == "my-orchestrator"


class TestRetriableExhausted:
    def test_archives_when_retries_exhausted(self, processor):
        proc, lambda_mock, s3_mock, sns_mock = processor
        message = _make_dlq_message(error_type="BedrockError", is_retriable=True, retry_count=2)
        record = _make_sqs_record(message)

        with patch.dict("os.environ", {"FAILURES_SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:123:failures"}):
            result = proc.process_message(record)

        assert result == {"action": "archived"}
        lambda_mock.invoke.assert_not_called()
        s3_mock.put_object.assert_called_once()
        sns_mock.publish.assert_called_once()


class TestPermanentError:
    def test_archives_immediately_for_non_retriable(self, processor):
        proc, lambda_mock, s3_mock, sns_mock = processor
        message = _make_dlq_message(error_type="ValidationError", is_retriable=False, retry_count=0)
        record = _make_sqs_record(message)

        with patch.dict("os.environ", {"FAILURES_SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:123:failures"}):
            result = proc.process_message(record)

        assert result == {"action": "archived"}
        lambda_mock.invoke.assert_not_called()
        s3_mock.put_object.assert_called_once()

    def test_archives_when_is_retriable_missing(self, processor):
        """Messages without is_retriable flag default to permanent."""
        proc, lambda_mock, s3_mock, _ = processor
        message = _make_dlq_message(retry_count=0)
        del message["error"]["is_retriable"]
        record = _make_sqs_record(message)

        result = proc.process_message(record)

        assert result == {"action": "archived"}
        lambda_mock.invoke.assert_not_called()


class TestRetryCountCoercion:
    def test_string_retry_count_coerced_to_int(self, processor):
        proc, lambda_mock, _, _ = processor
        message = _make_dlq_message(is_retriable=True, retry_count=0)
        message["retry_count"] = "1"
        record = _make_sqs_record(message)

        result = proc.process_message(record)

        assert result == {"action": "reprocessed"}
        payload = json.loads(lambda_mock.invoke.call_args[1]["Payload"])
        assert payload["retry_count"] == 2

    def test_invalid_retry_count_treated_as_exhausted(self, processor):
        proc, lambda_mock, s3_mock, _ = processor
        message = _make_dlq_message(is_retriable=True, retry_count=0)
        message["retry_count"] = "not-a-number"
        record = _make_sqs_record(message)

        result = proc.process_message(record)

        assert result == {"action": "archived"}
        lambda_mock.invoke.assert_not_called()

    def test_none_retry_count_treated_as_exhausted(self, processor):
        proc, lambda_mock, s3_mock, _ = processor
        message = _make_dlq_message(is_retriable=True, retry_count=0)
        message["retry_count"] = None
        record = _make_sqs_record(message)

        result = proc.process_message(record)

        assert result == {"action": "archived"}
        lambda_mock.invoke.assert_not_called()


class TestArchiveDetails:
    def test_s3_key_contains_correlation_id(self, processor):
        proc, _, s3_mock, _ = processor
        message = _make_dlq_message(correlation_id="req-abc", is_retriable=True, retry_count=2)
        record = _make_sqs_record(message)

        proc.process_message(record)

        call_kwargs = s3_mock.put_object.call_args[1]
        assert "failed/req-abc/" in call_kwargs["Key"]
        assert call_kwargs["Key"].endswith(".json")

    def test_sns_notification_contains_error_summary(self, processor):
        proc, _, _, sns_mock = processor
        message = _make_dlq_message(
            error_type="BedrockError",
            error_message="throttled",
            is_retriable=True,
            correlation_id="req-xyz",
            retry_count=2,
        )
        record = _make_sqs_record(message)

        with patch.dict("os.environ", {"FAILURES_SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:123:topic"}):
            proc.process_message(record)

        call_kwargs = sns_mock.publish.call_args[1]
        assert call_kwargs["Subject"] == "Pipeline processing failure"
        body = json.loads(call_kwargs["Message"])
        assert body["correlation_id"] == "req-xyz"
        assert body["error_type"] == "BedrockError"
        assert body["error_message"] == "throttled"
        assert body["retry_count"] == 2

    def test_skips_sns_when_topic_not_set(self, processor):
        proc, _, s3_mock, sns_mock = processor
        message = _make_dlq_message(is_retriable=True, retry_count=2)
        record = _make_sqs_record(message)

        with patch.dict("os.environ", {}, clear=True):
            proc.process_message(record)

        s3_mock.put_object.assert_called_once()
        sns_mock.publish.assert_not_called()


class TestInvalidMessage:
    def test_archives_invalid_json(self, processor):
        proc, lambda_mock, s3_mock, _ = processor
        record = {"messageId": "msg-bad", "body": "not json!!!"}

        result = proc.process_message(record)

        assert result == {"action": "archived"}
        lambda_mock.invoke.assert_not_called()
        s3_mock.put_object.assert_called_once()

    def test_archives_missing_body(self, processor):
        proc, lambda_mock, s3_mock, _ = processor
        record = {"messageId": "msg-nobody"}

        result = proc.process_message(record)

        assert result == {"action": "archived"}
        lambda_mock.invoke.assert_not_called()


class TestIsRetriable:
    def test_true_when_flag_is_true(self):
        assert DLQProcessor._is_retriable({"is_retriable": True}) is True

    def test_false_when_flag_is_false(self):
        assert DLQProcessor._is_retriable({"is_retriable": False}) is False

    def test_false_when_flag_missing(self):
        assert DLQProcessor._is_retriable({"error_type": "SomeError"}) is False

    def test_false_when_flag_is_string_true(self):
        """Only boolean True is accepted, not truthy strings."""
        assert DLQProcessor._is_retriable({"is_retriable": "true"}) is False


class TestDLQHandler:
    def test_processes_multiple_records(self):
        msg1 = _make_dlq_message(error_type="ValidationError", is_retriable=False, retry_count=0)
        msg2 = _make_dlq_message(error_type="BedrockError", is_retriable=True, retry_count=0)
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
