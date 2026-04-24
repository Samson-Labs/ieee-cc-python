"""Tests for DLQ processor."""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.dlq.dlq_processor import DLQProcessor
from tests.conftest import make_dlq_message as _make_dlq_message
from tests.conftest import make_sqs_record as _make_sqs_record


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

        # Invalid-message synthetic payload has no original_event, so
        # the env var fallback is required to resolve an archive bucket.
        with patch.dict("os.environ", {"ARCHIVE_BUCKET": "fallback-bucket"}):
            result = proc.process_message(record)

        assert result == {"action": "archived"}
        lambda_mock.invoke.assert_not_called()
        s3_mock.put_object.assert_called_once()

    def test_archives_missing_body(self, processor):
        proc, lambda_mock, s3_mock, _ = processor
        record = {"messageId": "msg-nobody"}

        with patch.dict("os.environ", {"ARCHIVE_BUCKET": "fallback-bucket"}):
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


class TestExtractSourceBucket:
    def test_direct_invoke_shape(self):
        event = {"bucket": "my-bucket", "key": "ou/pending/f.pdf"}
        assert DLQProcessor._extract_source_bucket(event) == "my-bucket"

    def test_s3_records_shape(self):
        event = {
            "Records": [
                {"s3": {"bucket": {"name": "my-records-bucket"}, "object": {"key": "x"}}}
            ]
        }
        assert DLQProcessor._extract_source_bucket(event) == "my-records-bucket"

    def test_records_shape_preferred_when_both_present(self):
        event = {
            "bucket": "direct-bucket",
            "Records": [{"s3": {"bucket": {"name": "records-bucket"}}}],
        }
        assert DLQProcessor._extract_source_bucket(event) == "records-bucket"

    def test_returns_none_for_empty_dict(self):
        assert DLQProcessor._extract_source_bucket({}) is None

    def test_returns_none_for_non_dict(self):
        assert DLQProcessor._extract_source_bucket(None) is None
        assert DLQProcessor._extract_source_bucket("string") is None

    def test_returns_none_when_records_malformed(self):
        assert DLQProcessor._extract_source_bucket({"Records": []}) is None
        assert DLQProcessor._extract_source_bucket({"Records": [{}]}) is None
        assert DLQProcessor._extract_source_bucket({"Records": [{"s3": {}}]}) is None

    def test_returns_none_when_bucket_is_empty_string(self):
        assert DLQProcessor._extract_source_bucket({"bucket": ""}) is None


class TestEnvAwareArchiveBucket:
    """Regression tests for CC3-880: archive bucket must track source event env."""

    def test_dev_event_archives_to_dev_bucket(self, processor):
        proc, _, s3_mock, _ = processor
        message = _make_dlq_message(
            is_retriable=False,
            original_event={
                "bucket": "dev-ieee-conference-cloud-bulk-uploads",
                "key": "PES/pending/x.pdf",
            },
        )
        record = _make_sqs_record(message)

        proc.process_message(record)

        assert s3_mock.put_object.call_args[1]["Bucket"] == "dev-ieee-conference-cloud-bulk-uploads"

    def test_staging_event_archives_to_staging_bucket(self, processor):
        proc, _, s3_mock, _ = processor
        message = _make_dlq_message(
            is_retriable=False,
            original_event={
                "bucket": "staging-ieee-conference-cloud-bulk-uploads",
                "key": "PES/pending/x.pdf",
            },
        )
        record = _make_sqs_record(message)

        # Env var points to dev bucket; event must override it.
        with patch.dict("os.environ", {"ARCHIVE_BUCKET": "dev-ieee-conference-cloud-bulk-uploads"}):
            proc.process_message(record)

        assert s3_mock.put_object.call_args[1]["Bucket"] == "staging-ieee-conference-cloud-bulk-uploads"

    def test_s3_records_event_archives_to_records_bucket(self, processor):
        proc, _, s3_mock, _ = processor
        message = _make_dlq_message(
            is_retriable=False,
            original_event={
                "Records": [
                    {
                        "s3": {
                            "bucket": {"name": "staging-ieee-conference-cloud-bulk-uploads"},
                            "object": {"key": "PES/pending/x.pdf"},
                        }
                    }
                ]
            },
        )
        record = _make_sqs_record(message)

        proc.process_message(record)

        assert s3_mock.put_object.call_args[1]["Bucket"] == "staging-ieee-conference-cloud-bulk-uploads"

    def test_falls_back_to_env_var_when_event_missing_bucket(self, processor):
        proc, _, s3_mock, _ = processor
        message = _make_dlq_message(is_retriable=False, original_event={})
        record = _make_sqs_record(message)

        with patch.dict("os.environ", {"ARCHIVE_BUCKET": "fallback-bucket"}):
            proc.process_message(record)

        assert s3_mock.put_object.call_args[1]["Bucket"] == "fallback-bucket"

    def test_raises_when_no_bucket_anywhere(self, processor):
        """Prefer failing loudly over silently archiving to a wrong bucket.

        The message stays on the DLQ for manual triage instead of being
        misrouted to a hardcoded default.
        """
        proc, _, s3_mock, _ = processor
        message = _make_dlq_message(is_retriable=False, original_event={})
        record = _make_sqs_record(message)

        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(RuntimeError, match="archive bucket"):
                proc.process_message(record)

        s3_mock.put_object.assert_not_called()


