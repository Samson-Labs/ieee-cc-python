"""Tests for the bulk worker (SQS-triggered, per-item processor)."""

import json
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from src.bulk.bulk_worker import BulkWorker
from src.common.exceptions import BulkProcessingError, ValidationError
from src.handlers.bulk_worker_handler import handler


def _make_item(
    item_id: int = 100,
    media_type: str = "PDF",
    s3_key: str = "PES/archive/paper.pdf",
    resource_center: str = "PES",
    request_id: int = 0,
) -> dict:
    return {
        "item_id": item_id,
        "request_id": request_id,
        "s3_key": s3_key,
        "media_type": media_type,
        "resource_center": resource_center,
        "title": "Test Paper",
    }


def _make_sqs_record(
    item: dict | None = None,
    batch_id: str = "bulk-test-001",
    callback_url: str = "https://example.com/webhook",
    total_items: int = 10,
    message_id: str = "msg-001",
) -> dict:
    body = {
        "batch_id": batch_id,
        "callback_url": callback_url,
        "item": item or _make_item(),
        "total_items": total_items,
    }
    return {"messageId": message_id, "body": json.dumps(body)}


def _orchestrator_success_response() -> dict:
    payload = json.dumps({
        "statusCode": 200,
        "body": {"item_id": "100", "action": "enriched"},
    })
    mock_payload = MagicMock()
    mock_payload.read.return_value = payload
    return {"StatusCode": 200, "Payload": mock_payload}


def _progress_response(completed: int = 0, failed: int = 0, total: int = 10) -> dict:
    progress = {
        "batch_id": "bulk-test-001",
        "total_items": total,
        "published": total,
        "completed": completed,
        "failed": failed,
        "status": "processing",
    }
    return {"Body": BytesIO(json.dumps(progress).encode())}


@pytest.fixture
def worker():
    lambda_mock = MagicMock()
    s3_mock = MagicMock()
    sns_mock = MagicMock()
    w = BulkWorker(lambda_client=lambda_mock, s3_client=s3_mock, sns_client=sns_mock)
    return w, lambda_mock, s3_mock, sns_mock


# --- Copy to pending ---


class TestCopyToPending:
    def test_copies_to_correct_pending_key(self, worker):
        w, _, s3_mock, _ = worker
        item = _make_item(item_id=42, media_type="PDF", s3_key="PES/archive/paper.pdf")

        key = w._copy_to_pending("bucket", item)

        assert key == "PES/pending/42.pdf"
        s3_mock.copy_object.assert_called_once_with(
            Bucket="bucket",
            CopySource={"Bucket": "bucket", "Key": "PES/archive/paper.pdf"},
            Key="PES/pending/42.pdf",
        )

    def test_video_extension(self, worker):
        w, _, s3_mock, _ = worker
        item = _make_item(item_id=99, media_type="MP4", s3_key="PES/archive/video.mp4")

        key = w._copy_to_pending("bucket", item)

        assert key == "PES/pending/99.mp4"


# --- Create meta JSON ---


class TestCreateMetaJson:
    def test_writes_correct_meta_key(self, worker):
        w, _, s3_mock, _ = worker
        item = _make_item(item_id=42, request_id=7)

        key = w._create_meta_json("bucket", item, "https://example.com/webhook")

        assert key == "PES/metadata/42.meta.json"
        call_kwargs = s3_mock.put_object.call_args[1]
        meta = json.loads(call_kwargs["Body"])
        assert meta["item_id"] == "42"
        assert meta["ou"] == "PES"
        assert meta["product_part_number"] == "7"
        assert meta["ai_enrichment_enabled"] is True
        assert meta["callback_url"] == "https://example.com/webhook"
        assert meta["content"]["media_type"] == "application/pdf"
        assert meta["content"]["filename"] == "42.pdf"

    def test_video_mime_type(self, worker):
        w, _, s3_mock, _ = worker
        item = _make_item(media_type="MOV")

        w._create_meta_json("bucket", item, "https://example.com")

        meta = json.loads(s3_mock.put_object.call_args[1]["Body"])
        assert meta["content"]["media_type"] == "video/quicktime"


# --- Invoke orchestrator ---


class TestInvokeOrchestrator:
    def test_success(self, worker):
        w, lambda_mock, _, _ = worker
        lambda_mock.invoke.return_value = _orchestrator_success_response()

        result = w._invoke_orchestrator("bucket", "PES/pending/42.pdf")

        assert result["action"] == "enriched"
        lambda_mock.invoke.assert_called_once()
        call_kwargs = lambda_mock.invoke.call_args[1]
        assert call_kwargs["InvocationType"] == "RequestResponse"
        payload = json.loads(call_kwargs["Payload"])
        assert payload["bucket"] == "bucket"
        assert payload["key"] == "PES/pending/42.pdf"

    def test_function_error_raises(self, worker):
        w, lambda_mock, _, _ = worker
        error_payload = MagicMock()
        error_payload.read.return_value = '{"errorMessage": "boom"}'
        lambda_mock.invoke.return_value = {
            "StatusCode": 200,
            "FunctionError": "Unhandled",
            "Payload": error_payload,
        }

        with pytest.raises(BulkProcessingError, match="FunctionError"):
            w._invoke_orchestrator("bucket", "PES/pending/42.pdf")

    def test_non_200_raises(self, worker):
        w, lambda_mock, _, _ = worker
        payload = json.dumps({"statusCode": 500, "body": {"error": "oops"}})
        mock_payload = MagicMock()
        mock_payload.read.return_value = payload
        lambda_mock.invoke.return_value = {"StatusCode": 200, "Payload": mock_payload}

        with pytest.raises(BulkProcessingError, match="status 500"):
            w._invoke_orchestrator("bucket", "PES/pending/42.pdf")


# --- Update progress ---


class TestUpdateProgress:
    def test_increments_completed(self, worker):
        w, _, s3_mock, _ = worker
        s3_mock.get_object.return_value = _progress_response(completed=5, total=10)

        progress = w._update_progress("bucket", "bulk-test-001", 42, True, 10)

        assert progress["completed"] == 6
        s3_mock.put_object.assert_called_once()

    def test_increments_failed(self, worker):
        w, _, s3_mock, _ = worker
        s3_mock.get_object.return_value = _progress_response(failed=2, total=10)

        progress = w._update_progress("bucket", "bulk-test-001", 42, False, 10)

        assert progress["failed"] == 3

    def test_sets_completed_status_when_done(self, worker):
        w, _, s3_mock, _ = worker
        s3_mock.get_object.return_value = _progress_response(completed=9, total=10)

        progress = w._update_progress("bucket", "bulk-test-001", 42, True, 10)

        assert progress["status"] == "completed"

    def test_creates_progress_if_missing(self, worker):
        w, _, s3_mock, _ = worker
        s3_mock.get_object.side_effect = Exception("NoSuchKey")

        progress = w._update_progress("bucket", "bulk-test-001", 42, True, 10)

        assert progress["completed"] == 1
        assert progress["total_items"] == 10


# --- Completion notification ---


class TestCompletionNotification:
    def test_sends_sns_on_completion(self, worker):
        w, lambda_mock, s3_mock, sns_mock = worker
        lambda_mock.invoke.return_value = _orchestrator_success_response()
        s3_mock.get_object.return_value = _progress_response(completed=9, total=10)
        record = _make_sqs_record(total_items=10)

        with patch.dict("os.environ", {"COMPLETION_SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:123:topic"}):
            w.process_item(record)

        sns_mock.publish.assert_called_once()
        call_kwargs = sns_mock.publish.call_args[1]
        assert "bulk-test-001" in call_kwargs["Subject"]

    def test_skips_sns_when_not_last_item(self, worker):
        w, lambda_mock, s3_mock, sns_mock = worker
        lambda_mock.invoke.return_value = _orchestrator_success_response()
        s3_mock.get_object.return_value = _progress_response(completed=3, total=10)
        record = _make_sqs_record(total_items=10)

        w.process_item(record)

        sns_mock.publish.assert_not_called()

    def test_skips_sns_when_topic_not_set(self, worker):
        w, _, s3_mock, sns_mock = worker
        progress = {
            "batch_id": "test",
            "total_items": 1,
            "completed": 1,
            "failed": 0,
            "status": "completed",
        }

        with patch.dict("os.environ", {}, clear=True):
            w._send_completion_notification("test", progress)

        sns_mock.publish.assert_not_called()


# --- Process item end-to-end ---


class TestProcessItem:
    def test_success_flow(self, worker):
        w, lambda_mock, s3_mock, _ = worker
        lambda_mock.invoke.return_value = _orchestrator_success_response()
        s3_mock.get_object.return_value = _progress_response(completed=0, total=10)
        record = _make_sqs_record()

        result = w.process_item(record)

        assert result["action"] == "processed"
        assert result["batch_id"] == "bulk-test-001"
        assert result["item_id"] == 100
        assert result["processing_time_ms"] >= 0

        # Verify the flow: copy, meta, invoke, progress
        s3_mock.copy_object.assert_called_once()
        assert s3_mock.put_object.call_count >= 1  # meta + progress
        lambda_mock.invoke.assert_called_once()

    def test_orchestrator_failure_marks_failed(self, worker):
        w, lambda_mock, s3_mock, _ = worker
        lambda_mock.invoke.side_effect = RuntimeError("boom")
        s3_mock.get_object.return_value = _progress_response(completed=0, total=10)
        record = _make_sqs_record()

        result = w.process_item(record)

        assert result["action"] == "failed"

    def test_invalid_message_body(self, worker):
        w, _, _, _ = worker
        record = {"messageId": "msg-bad", "body": "not json!!!"}

        with pytest.raises(ValidationError, match="Invalid SQS message body"):
            w.process_item(record)


# --- Handler ---


class TestBulkWorkerHandler:
    def test_processes_multiple_records(self):
        record1 = _make_sqs_record(message_id="msg-001")
        record2 = _make_sqs_record(message_id="msg-002")
        event = {"Records": [record1, record2]}

        with patch("src.handlers.bulk_worker_handler.worker") as mock_worker:
            mock_worker.process_item.side_effect = [
                {"batch_id": "test", "item_id": 1, "action": "processed", "processing_time_ms": 100},
                {"batch_id": "test", "item_id": 2, "action": "processed", "processing_time_ms": 200},
            ]
            result = handler(event, None)

        assert len(result["results"]) == 2
        assert result["batchItemFailures"] == []

    def test_partial_batch_failure(self):
        record1 = _make_sqs_record(message_id="msg-001")
        record2 = _make_sqs_record(message_id="msg-002")
        event = {"Records": [record1, record2]}

        with patch("src.handlers.bulk_worker_handler.worker") as mock_worker:
            mock_worker.process_item.side_effect = [
                {"batch_id": "test", "item_id": 1, "action": "processed", "processing_time_ms": 100},
                RuntimeError("unexpected"),
            ]
            result = handler(event, None)

        assert len(result["batchItemFailures"]) == 1
        assert result["batchItemFailures"][0]["itemIdentifier"] == "msg-002"

    def test_empty_records(self):
        result = handler({"Records": []}, None)
        assert result["batchItemFailures"] == []
        assert result["results"] == []
