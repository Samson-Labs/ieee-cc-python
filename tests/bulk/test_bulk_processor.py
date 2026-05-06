"""Tests for the bulk processor (manifest dispatcher)."""

import json
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from src.bulk.bulk_processor import BulkProcessor
from src.common.exceptions import BulkProcessingError, ValidationError
from src.handlers.bulk_processor_handler import handler


def _valid_manifest(item_count: int = 2) -> dict:
    items = [
        {
            "item_id": 100 + i,
            "request_id": i,
            "s3_key": f"PES/archive/paper-{i}.pdf",
            "media_type": "PDF",
            "resource_center": "PES",
            "title": f"Paper {i}",
        }
        for i in range(item_count)
    ]
    return {
        "batch_id": "bulk-test-001",
        "callback_url": "https://example.com/webhook",
        "items": items,
        "config": {"max_concurrent": 5, "delay_between_ms": 100},
    }


def _s3_manifest_response(manifest: dict) -> dict:
    return {"Body": BytesIO(json.dumps(manifest).encode())}


@pytest.fixture
def processor():
    s3_mock = MagicMock()
    sqs_mock = MagicMock()
    sns_mock = MagicMock()
    proc = BulkProcessor(s3_client=s3_mock, sqs_client=sqs_mock, sns_client=sns_mock)
    return proc, s3_mock, sqs_mock, sns_mock


# --- Manifest validation ---


class TestManifestValidation:
    def test_missing_required_fields(self, processor):
        proc, s3_mock, _, _ = processor
        manifest = {"batch_id": "test"}  # missing callback_url, items, config
        s3_mock.get_object.return_value = _s3_manifest_response(manifest)

        with pytest.raises(ValidationError, match="missing required fields"):
            proc.process_manifest("bucket", "test")

    def test_empty_items_list(self, processor):
        proc, s3_mock, _, _ = processor
        manifest = _valid_manifest()
        manifest["items"] = []
        s3_mock.get_object.return_value = _s3_manifest_response(manifest)

        with pytest.raises(ValidationError, match="non-empty list"):
            proc.process_manifest("bucket", "test")

    def test_item_missing_fields(self, processor):
        proc, s3_mock, _, _ = processor
        manifest = _valid_manifest()
        del manifest["items"][0]["media_type"]
        s3_mock.get_object.return_value = _s3_manifest_response(manifest)

        with pytest.raises(ValidationError, match="missing 'media_type'"):
            proc.process_manifest("bucket", "test")

    def test_invalid_media_type(self, processor):
        proc, s3_mock, _, _ = processor
        manifest = _valid_manifest()
        manifest["items"][0]["media_type"] = "WAV"
        s3_mock.get_object.return_value = _s3_manifest_response(manifest)

        with pytest.raises(ValidationError, match="invalid media_type 'WAV'"):
            proc.process_manifest("bucket", "test")

    def test_manifest_not_found(self, processor):
        proc, s3_mock, _, _ = processor
        s3_mock.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})
        s3_mock.get_object.side_effect = s3_mock.exceptions.NoSuchKey("not found")

        with pytest.raises(ValidationError, match="Manifest not found"):
            proc.process_manifest("bucket", "missing-batch")

    def test_invalid_json(self, processor):
        proc, s3_mock, _, _ = processor
        s3_mock.get_object.return_value = {"Body": BytesIO(b"not json!!!")}
        s3_mock.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})

        with pytest.raises(ValidationError, match="Invalid manifest JSON"):
            proc.process_manifest("bucket", "bad-json")


# --- CC3-860: Backfill validation ---


def _text_only_manifest():
    return {
        "batch_id": "backfill-001",
        "callback_url": "https://example.com/webhook",
        "items": [
            {
                "item_id": 200,
                "request_id": 1,
                "resource_center": "PES",
                "input_text": "User-provided abstract text.",
                "input_text_mode": "as_abstract",
                "requested_fields": ["keywords", "category"],
            }
        ],
    }


class TestBackfillValidation:
    def test_text_only_item_accepted(self, processor):
        proc, s3_mock, sqs_mock, _ = processor
        manifest = _text_only_manifest()
        s3_mock.get_object.return_value = _s3_manifest_response(manifest)
        s3_mock.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})

        with patch.dict("os.environ", {"BULK_QUEUE_URL": "https://sqs/q"}):
            result = proc.process_manifest("bucket", "backfill-001")

        assert result["total_items"] == 1
        assert result["published_count"] == 1

    def test_hybrid_item_accepted(self, processor):
        proc, s3_mock, sqs_mock, _ = processor
        manifest = {
            "batch_id": "hybrid-001",
            "callback_url": "https://example.com/webhook",
            "items": [{
                "item_id": 300,
                "request_id": 2,
                "s3_key": "PES/archive/paper.pdf",
                "media_type": "PDF",
                "resource_center": "PES",
                "input_text": "Existing abstract.",
                "input_text_mode": "as_abstract",
            }],
        }
        s3_mock.get_object.return_value = _s3_manifest_response(manifest)
        s3_mock.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})

        with patch.dict("os.environ", {"BULK_QUEUE_URL": "https://sqs/q"}):
            result = proc.process_manifest("bucket", "hybrid-001")

        assert result["published_count"] == 1

    def test_neither_text_nor_key_rejected(self, processor):
        proc, s3_mock, _, _ = processor
        manifest = _valid_manifest()
        del manifest["items"][0]["s3_key"]
        s3_mock.get_object.return_value = _s3_manifest_response(manifest)

        with pytest.raises(ValidationError, match="at least one of"):
            proc.process_manifest("bucket", "test")

    def test_file_without_media_type_rejected(self, processor):
        proc, s3_mock, _, _ = processor
        manifest = _valid_manifest()
        del manifest["items"][0]["media_type"]
        s3_mock.get_object.return_value = _s3_manifest_response(manifest)

        with pytest.raises(ValidationError, match="missing 'media_type'"):
            proc.process_manifest("bucket", "test")

    def test_invalid_input_text_mode_rejected(self, processor):
        proc, s3_mock, _, _ = processor
        manifest = _text_only_manifest()
        manifest["items"][0]["input_text_mode"] = "bad_mode"
        s3_mock.get_object.return_value = _s3_manifest_response(manifest)

        with pytest.raises(ValidationError, match="input_text_mode"):
            proc.process_manifest("bucket", "test")

    def test_empty_requested_fields_rejected(self, processor):
        proc, s3_mock, _, _ = processor
        manifest = _text_only_manifest()
        manifest["items"][0]["requested_fields"] = []
        s3_mock.get_object.return_value = _s3_manifest_response(manifest)

        with pytest.raises(ValidationError, match="non-empty"):
            proc.process_manifest("bucket", "test")

    def test_backward_compat_existing_manifest(self, processor):
        """Standard manifests with s3_key + media_type still validate."""
        proc, s3_mock, sqs_mock, _ = processor
        manifest = _valid_manifest(3)
        s3_mock.get_object.return_value = _s3_manifest_response(manifest)
        s3_mock.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})

        with patch.dict("os.environ", {"BULK_QUEUE_URL": "https://sqs/q"}):
            with patch("src.bulk.bulk_processor.time.sleep"):
                result = proc.process_manifest("bucket", "bulk-test-001")

        assert result["published_count"] == 3


def _strategy_a_manifest():
    """Strategy A item: file-bearing, input_text empty by design."""
    return {
        "batch_id": "strategy-a-001",
        "callback_url": "https://example.com/webhook",
        "items": [{
            "item_id": 400,
            "request_id": 0,
            "resource_center": "MTT",
            "s3_key": "video/private/MTTIMSWEB0010/MTTIMSWEB0010.mp4",
            "media_type": "MP4",
            "source_bucket": "ieee-conference-cloud-content",
            "input_text": "",
            "input_text_mode": "",
        }],
    }


class TestEmptySentinelTolerance:
    """The Drupal builder emits empty-string sentinels for inapplicable
    fields (Strategy A items have ``input_text=""`` by design, text-only
    items have ``source_bucket=""`` because there's no source to fetch).
    The validator must tolerate these without rejecting the item.
    """

    def test_strategy_a_item_with_empty_input_text_accepted(self, processor):
        proc, s3_mock, sqs_mock, _ = processor
        manifest = _strategy_a_manifest()
        s3_mock.get_object.return_value = _s3_manifest_response(manifest)
        s3_mock.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})

        with patch.dict("os.environ", {"BULK_QUEUE_URL": "https://sqs/q"}):
            result = proc.process_manifest("bucket", "strategy-a-001")

        assert result["published_count"] == 1

    def test_text_only_item_with_empty_source_bucket_accepted(self, processor):
        """No s3_key → source_bucket is irrelevant, empty value tolerated."""
        proc, s3_mock, sqs_mock, _ = processor
        manifest = _text_only_manifest()
        manifest["items"][0]["source_bucket"] = ""
        s3_mock.get_object.return_value = _s3_manifest_response(manifest)
        s3_mock.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})

        with patch.dict("os.environ", {"BULK_QUEUE_URL": "https://sqs/q"}):
            result = proc.process_manifest("bucket", "backfill-001")

        assert result["published_count"] == 1

    def test_file_item_with_empty_source_bucket_rejected(self, processor):
        """Has s3_key → source_bucket must be non-empty (worker uses it)."""
        proc, s3_mock, _, _ = processor
        manifest = _strategy_a_manifest()
        manifest["items"][0]["source_bucket"] = ""
        s3_mock.get_object.return_value = _s3_manifest_response(manifest)
        s3_mock.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})

        with pytest.raises(ValidationError, match="source_bucket.*non-empty"):
            proc.process_manifest("bucket", "test")

    def test_empty_input_text_mode_treated_as_absent(self, processor):
        """input_text_mode="" must not trigger 'invalid input_text_mode'."""
        proc, s3_mock, sqs_mock, _ = processor
        manifest = _strategy_a_manifest()  # input_text_mode="" already
        s3_mock.get_object.return_value = _s3_manifest_response(manifest)
        s3_mock.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})

        with patch.dict("os.environ", {"BULK_QUEUE_URL": "https://sqs/q"}):
            result = proc.process_manifest("bucket", "strategy-a-001")

        assert result["published_count"] == 1

    def test_input_text_mode_still_rejected_when_set_invalid(self, processor):
        """Non-empty invalid input_text_mode is still rejected."""
        proc, s3_mock, _, _ = processor
        manifest = _text_only_manifest()
        manifest["items"][0]["input_text_mode"] = "garbage"
        s3_mock.get_object.return_value = _s3_manifest_response(manifest)
        s3_mock.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})

        with pytest.raises(ValidationError, match="input_text_mode"):
            proc.process_manifest("bucket", "test")


# --- Cost estimation ---


class TestCostEstimation:
    def test_pdf_only_batch(self):
        manifest = _valid_manifest(3)
        estimate = BulkProcessor._estimate_cost(manifest["items"])

        assert estimate["breakdown"] == {"PDF": 3}
        assert estimate["total_usd"] == 0.03

    def test_mixed_batch(self):
        items = [
            {"item_id": 1, "media_type": "PDF", "s3_key": "a", "request_id": 0, "resource_center": "PES"},
            {"item_id": 2, "media_type": "MP4", "s3_key": "b", "request_id": 0, "resource_center": "PES"},
        ]
        estimate = BulkProcessor._estimate_cost(items)

        assert estimate["breakdown"] == {"PDF": 1, "MP4": 1}
        assert estimate["total_usd"] == 0.04  # 0.01 + 0.03

    def test_text_only_cost(self):
        items = [
            {"item_id": 1, "request_id": 0, "resource_center": "PES", "input_text": "text"},
            {"item_id": 2, "request_id": 0, "resource_center": "PES", "input_text": "text"},
        ]
        estimate = BulkProcessor._estimate_cost(items)

        assert estimate["breakdown"] == {"text": 2}
        assert estimate["total_usd"] == 0.01  # 2 * 0.005


# --- Publish items ---


class TestPublishItems:
    def test_publishes_all_items(self, processor):
        proc, s3_mock, sqs_mock, _ = processor
        manifest = _valid_manifest(3)
        s3_mock.get_object.return_value = _s3_manifest_response(manifest)
        s3_mock.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})

        with patch.dict("os.environ", {"BULK_QUEUE_URL": "https://sqs.example.com/queue"}):
            with patch("src.bulk.bulk_processor.time.sleep"):
                result = proc.process_manifest("bucket", "bulk-test-001")

        assert sqs_mock.send_message.call_count == 3
        assert result["published_count"] == 3

    def test_sqs_message_contains_item(self, processor):
        proc, s3_mock, sqs_mock, _ = processor
        manifest = _valid_manifest(1)
        s3_mock.get_object.return_value = _s3_manifest_response(manifest)
        s3_mock.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})

        with patch.dict("os.environ", {"BULK_QUEUE_URL": "https://sqs.example.com/queue"}):
            proc.process_manifest("bucket", "bulk-test-001")

        call_kwargs = sqs_mock.send_message.call_args[1]
        body = json.loads(call_kwargs["MessageBody"])
        assert body["batch_id"] == "bulk-test-001"
        assert body["item"]["item_id"] == 100
        assert body["total_items"] == 1

    def test_respects_delay_between_ms(self, processor):
        proc, s3_mock, sqs_mock, _ = processor
        manifest = _valid_manifest(2)
        s3_mock.get_object.return_value = _s3_manifest_response(manifest)
        s3_mock.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})

        with patch.dict("os.environ", {"BULK_QUEUE_URL": "https://sqs.example.com/queue"}):
            with patch("src.bulk.bulk_processor.time.sleep") as mock_sleep:
                proc.process_manifest("bucket", "bulk-test-001")

        # sleep called once between 2 items (not after the last)
        mock_sleep.assert_called_once_with(0.1)

    def test_missing_queue_url_raises(self, processor):
        proc, s3_mock, _, _ = processor
        manifest = _valid_manifest(1)
        s3_mock.get_object.return_value = _s3_manifest_response(manifest)
        s3_mock.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})

        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(BulkProcessingError, match="BULK_QUEUE_URL"):
                proc.process_manifest("bucket", "bulk-test-001")


# --- Progress writing ---


class TestProgressWriting:
    def test_writes_initial_progress(self, processor):
        proc, s3_mock, sqs_mock, _ = processor
        manifest = _valid_manifest(2)
        s3_mock.get_object.return_value = _s3_manifest_response(manifest)
        s3_mock.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})

        with patch.dict("os.environ", {"BULK_QUEUE_URL": "https://sqs.example.com/queue"}):
            with patch("src.bulk.bulk_processor.time.sleep"):
                proc.process_manifest("bucket", "bulk-test-001")

        put_calls = s3_mock.put_object.call_args_list
        assert len(put_calls) == 1
        call_kwargs = put_calls[0][1]
        assert call_kwargs["Key"] == "bulk/progress/bulk-test-001_progress.json"
        progress = json.loads(call_kwargs["Body"])
        assert progress["total_items"] == 2
        assert progress["status"] == "dispatched"


# --- End-to-end ---


class TestProcessManifest:
    def test_returns_dispatched_result(self, processor):
        proc, s3_mock, sqs_mock, _ = processor
        manifest = _valid_manifest(2)
        s3_mock.get_object.return_value = _s3_manifest_response(manifest)
        s3_mock.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})

        with patch.dict("os.environ", {"BULK_QUEUE_URL": "https://sqs.example.com/queue"}):
            with patch("src.bulk.bulk_processor.time.sleep"):
                result = proc.process_manifest("test-bucket", "bulk-test-001")

        assert result["batch_id"] == "bulk-test-001"
        assert result["total_items"] == 2
        assert result["published_count"] == 2
        assert result["status"] == "dispatched"
        assert "total_usd" in result["estimated_cost"]


# --- Handler ---


class TestBulkProcessorHandler:
    def test_success(self):
        with patch("src.handlers.bulk_processor_handler.processor") as mock_proc:
            mock_proc.process_manifest.return_value = {
                "batch_id": "test",
                "total_items": 5,
                "published_count": 5,
                "estimated_cost": {"breakdown": {"PDF": 5}, "total_usd": 0.05},
                "status": "dispatched",
            }
            result = handler({"batch_id": "test"}, None)

        assert result["statusCode"] == 200
        assert result["body"]["batch_id"] == "test"

    def test_missing_batch_id(self):
        result = handler({}, None)
        assert result["statusCode"] == 400

    def test_error_returns_structured_response(self):
        with patch("src.handlers.bulk_processor_handler.processor") as mock_proc:
            mock_proc.process_manifest.side_effect = ValidationError("bad manifest")
            result = handler({"batch_id": "test"}, None)

        assert result["statusCode"] == 400
        assert result["body"]["error_type"] == "ValidationError"

    def test_s3_event_invocation(self):
        """S3 PutObject on bulk/manifests/<batch_id>.json dispatches the batch."""
        s3_event = {
            "Records": [{
                "s3": {
                    "bucket": {"name": "dev-ieee-conference-cloud-bulk-uploads"},
                    "object": {"key": "bulk/manifests/strategic-test-001.json"},
                },
            }],
        }
        with patch("src.handlers.bulk_processor_handler.processor") as mock_proc:
            mock_proc.process_manifest.return_value = {
                "batch_id": "strategic-test-001",
                "total_items": 5,
                "published_count": 5,
                "estimated_cost": {"breakdown": {"PDF": 5}, "total_usd": 0.05},
                "status": "dispatched",
            }
            result = handler(s3_event, None)

        mock_proc.process_manifest.assert_called_once_with(
            bucket="dev-ieee-conference-cloud-bulk-uploads",
            batch_id="strategic-test-001",
        )
        assert result["statusCode"] == 200
        assert result["body"]["results"][0]["batch_id"] == "strategic-test-001"

    def test_s3_event_multiple_records(self):
        """S3 may batch multiple PutObject events; handler must process all."""
        s3_event = {
            "Records": [
                {"s3": {"bucket": {"name": "b"},
                        "object": {"key": "bulk/manifests/batch-a.json"}}},
                {"s3": {"bucket": {"name": "b"},
                        "object": {"key": "bulk/manifests/batch-b.json"}}},
                {"s3": {"bucket": {"name": "b"},
                        "object": {"key": "bulk/manifests/batch-c.json"}}},
            ],
        }
        with patch("src.handlers.bulk_processor_handler.processor") as mock_proc:
            mock_proc.process_manifest.side_effect = lambda bucket, batch_id: {
                "batch_id": batch_id,
                "total_items": 1,
                "published_count": 1,
                "estimated_cost": {"breakdown": {}, "total_usd": 0.0},
                "status": "dispatched",
            }
            result = handler(s3_event, None)

        assert mock_proc.process_manifest.call_count == 3
        dispatched = [r["batch_id"] for r in result["body"]["results"]]
        assert dispatched == ["batch-a", "batch-b", "batch-c"]
        assert result["statusCode"] == 200

    def test_s3_event_partial_failure_does_not_raise(self):
        """One record failing must not block others or trigger a Lambda retry."""
        s3_event = {
            "Records": [
                {"s3": {"bucket": {"name": "b"},
                        "object": {"key": "bulk/manifests/good.json"}}},
                {"s3": {"bucket": {"name": "b"},
                        "object": {"key": "bulk/manifests/bad.json"}}},
            ],
        }

        def fake_process(bucket, batch_id):
            if batch_id == "bad":
                raise ValidationError("boom")
            return {
                "batch_id": batch_id, "total_items": 1, "published_count": 1,
                "estimated_cost": {"breakdown": {}, "total_usd": 0.0},
                "status": "dispatched",
            }

        with patch("src.handlers.bulk_processor_handler.processor") as mock_proc:
            mock_proc.process_manifest.side_effect = fake_process
            result = handler(s3_event, None)

        assert result["statusCode"] == 200
        results = result["body"]["results"]
        assert results[0]["batch_id"] == "good"
        assert results[1]["status"] == "failed"

    def test_s3_event_wrong_prefix_skipped(self):
        s3_event = {
            "Records": [{
                "s3": {
                    "bucket": {"name": "bucket"},
                    "object": {"key": "actions/some-job.json"},
                },
            }],
        }
        result = handler(s3_event, None)
        assert result["statusCode"] == 200
        assert result["body"]["results"][0]["status"] == "skipped"
        assert "does not match" in result["body"]["results"][0]["error"]

    def test_s3_event_wrong_suffix_skipped(self):
        s3_event = {
            "Records": [{
                "s3": {
                    "bucket": {"name": "bucket"},
                    "object": {"key": "bulk/manifests/something.txt"},
                },
            }],
        }
        result = handler(s3_event, None)
        assert result["statusCode"] == 200
        assert result["body"]["results"][0]["status"] == "skipped"
        assert "does not match" in result["body"]["results"][0]["error"]
