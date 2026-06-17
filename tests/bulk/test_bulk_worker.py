"""Tests for the bulk worker (SQS-triggered, per-item processor)."""

import json
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from src.bulk.bulk_worker import BulkWorker
from src.common.exceptions import BulkProcessingError, ValidationError
from src.handlers.bulk_worker_handler import handler


def _make_item(
    item_id: int = 100,
    media_type: str = "PDF",
    s3_key: str = "PES/archive/paper.pdf",
    resource_center: str = "PES",
    product_part_number: str = "PESPAPER001",
    input_text: str | None = None,
    input_text_mode: str | None = None,
    requested_fields: list[str] | None = None,
    source_bucket: str | None = None,
) -> dict:
    item = {
        "item_id": item_id,
        "resource_center": resource_center,
        "product_part_number": product_part_number,
        "title": "Test Paper",
    }
    # Only include media_type for file-backed items (mirrors real payloads)
    if s3_key is not None:
        item["media_type"] = media_type
        item["s3_key"] = s3_key
    if input_text is not None:
        item["input_text"] = input_text
    if input_text_mode is not None:
        item["input_text_mode"] = input_text_mode
    if requested_fields is not None:
        item["requested_fields"] = requested_fields
    if source_bucket is not None:
        item["source_bucket"] = source_bucket
    return item


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

    # Default S3 mocks for the readback verification step in _copy_to_pending.
    # Tests exercising readback-failure paths override these explicitly.
    s3_mock.head_object.return_value = {"ContentLength": 1024}
    default_body = MagicMock()
    default_body.read.return_value = b""
    s3_mock.get_object.return_value = {"Body": default_body}

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


# --- Readback verification (Option B) ---


class TestReadbackVerification:
    """Verify a freshly-copied object is end-to-end readable before
    invoking the orchestrator. Catches the S3-read-after-copy race
    that surfaces as MediaConvert demuxer 1401 / orchestrator
    head_object 404 in the strategic-backfill flow.
    """

    def test_probes_head_only_for_small_files(self, worker):
        w, _, s3_mock, _ = worker
        s3_mock.head_object.return_value = {"ContentLength": 4096}
        item = _make_item(item_id=1, media_type="PDF", s3_key="PES/archive/p.pdf")

        w._copy_to_pending("bucket", item)

        # Single range read for files <= READBACK_PROBE_BYTES (64 KB).
        range_calls = [
            c for c in s3_mock.get_object.call_args_list if "Range" in c.kwargs
        ]
        assert len(range_calls) == 1
        assert range_calls[0].kwargs["Range"] == "bytes=0-4095"

    def test_probes_head_and_tail_for_large_files(self, worker):
        w, _, s3_mock, _ = worker
        size = 2_576_270_428  # the 2.4 GB MTTIMSWEB0000.mp4 size from CC3-915
        s3_mock.head_object.return_value = {"ContentLength": size}
        item = _make_item(item_id=1, media_type="MP4", s3_key="PES/archive/v.mp4")

        w._copy_to_pending("bucket", item)

        range_calls = [
            c.kwargs["Range"]
            for c in s3_mock.get_object.call_args_list
            if "Range" in c.kwargs
        ]
        # Head probe: first 64 KB. Tail probe: last 64 KB (where the moov
        # atom typically lives in MP4s).
        assert range_calls == [
            "bytes=0-65535",
            f"bytes={size - 65536}-{size - 1}",
        ]

    def test_skips_range_reads_for_zero_byte_object(self, worker):
        w, _, s3_mock, _ = worker
        s3_mock.head_object.return_value = {"ContentLength": 0}
        item = _make_item(item_id=1, media_type="PDF", s3_key="PES/archive/p.pdf")

        w._copy_to_pending("bucket", item)

        # No Range get_object calls — head_object alone is enough.
        range_calls = [
            c for c in s3_mock.get_object.call_args_list if "Range" in c.kwargs
        ]
        assert range_calls == []

    @patch("src.bulk.bulk_worker.time.sleep")
    def test_retries_on_transient_read_failure_then_succeeds(self, mock_sleep, worker):
        w, _, s3_mock, _ = worker
        not_found = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "Not found"}}, "HeadObject"
        )
        s3_mock.head_object.side_effect = [
            not_found,
            not_found,
            {"ContentLength": 1024},
        ]
        body = MagicMock()
        body.read.return_value = b""
        s3_mock.get_object.return_value = {"Body": body}
        item = _make_item(item_id=1, media_type="PDF", s3_key="PES/archive/p.pdf")

        w._copy_to_pending("bucket", item)

        assert s3_mock.head_object.call_count == 3
        # Linear backoff between failed attempts.
        assert mock_sleep.call_count == 2

    @patch("src.bulk.bulk_worker.time.sleep")
    def test_raises_after_exhausted_retries(self, mock_sleep, worker):
        w, _, s3_mock, _ = worker
        s3_mock.head_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "Not found"}}, "HeadObject"
        )
        item = _make_item(item_id=1, media_type="PDF", s3_key="PES/archive/p.pdf")

        with pytest.raises(BulkProcessingError, match="not readable after"):
            w._copy_to_pending("bucket", item)

        # 5 attempts (READBACK_MAX_ATTEMPTS), 4 sleep calls between them.
        assert s3_mock.head_object.call_count == 5
        assert mock_sleep.call_count == 4


# --- Create meta JSON ---


class TestCreateMetaJson:
    def test_writes_correct_meta_key(self, worker):
        w, _, s3_mock, _ = worker
        item = _make_item(item_id=42, product_part_number="PESVID042")

        key = w._create_meta_json("bucket", item, "https://example.com/webhook")

        assert key == "PES/metadata/42.meta.json"
        call_kwargs = s3_mock.put_object.call_args[1]
        meta = json.loads(call_kwargs["Body"])
        assert meta["item_id"] == "42"
        assert meta["ou"] == "PES"
        assert meta["product_part_number"] == "PESVID042"
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

    def test_ppn_not_synthesized_from_request_id(self, worker):
        """CC3-1001 regression: meta.json must carry the real PPN from
        the manifest, not a str(request_id) synthesis. When request_id
        is present (Drupal's transitional additive payload), the worker
        must still read product_part_number directly.
        """
        w, _, s3_mock, _ = worker
        item = _make_item(item_id=42, product_part_number="SPSEDU20VID002")
        item["request_id"] = 0  # transitional Drupal payload

        w._create_meta_json("bucket", item, "https://example.com/webhook")

        meta = json.loads(s3_mock.put_object.call_args[1]["Body"])
        assert meta["product_part_number"] == "SPSEDU20VID002"
        assert meta["product_part_number"] != "0"


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
        s3_mock.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "Not found"}}, "GetObject"
        )

        progress = w._update_progress("bucket", "bulk-test-001", 42, True, 10)

        assert progress["completed"] == 1
        assert progress["total_items"] == 10

    def test_uses_if_match_when_etag_known(self, worker):
        w, _, s3_mock, _ = worker
        response = _progress_response(completed=5, total=10)
        response["ETag"] = '"abc123"'
        s3_mock.get_object.return_value = response

        w._update_progress("bucket", "bulk-test-001", 42, True, 10)

        put_kwargs = s3_mock.put_object.call_args[1]
        assert put_kwargs["IfMatch"] == '"abc123"'
        assert "IfNoneMatch" not in put_kwargs

    def test_uses_if_none_match_when_progress_missing(self, worker):
        w, _, s3_mock, _ = worker
        s3_mock.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "Not found"}}, "GetObject"
        )

        w._update_progress("bucket", "bulk-test-001", 42, True, 10)

        put_kwargs = s3_mock.put_object.call_args[1]
        assert put_kwargs["IfNoneMatch"] == "*"
        assert "IfMatch" not in put_kwargs

    def test_retries_on_concurrent_write_conflict(self, worker):
        """Two workers race on the same progress file. The first PUT
        raises PreconditionFailed (someone else won); the worker re-reads
        and retries with the new ETag, picking up the other's increment.
        """
        w, _, s3_mock, _ = worker

        # Read sequence: first attempt sees stale state, retry sees the
        # other worker's update with a fresh ETag.
        first_read = _progress_response(completed=2, total=10)
        first_read["ETag"] = '"stale"'
        second_read = _progress_response(completed=3, total=10)
        second_read["ETag"] = '"fresh"'
        s3_mock.get_object.side_effect = [first_read, second_read]

        # First put raises 412; second succeeds.
        s3_mock.put_object.side_effect = [
            ClientError(
                {"Error": {"Code": "PreconditionFailed", "Message": "ETag mismatch"}},
                "PutObject",
            ),
            None,
        ]

        with patch("src.bulk.bulk_worker.time.sleep"):
            progress = w._update_progress("bucket", "bulk-test-001", 42, True, 10)

        assert s3_mock.get_object.call_count == 2
        assert s3_mock.put_object.call_count == 2
        # Counter built on top of the OTHER worker's update (3 -> 4),
        # not the stale value (2 -> 3) — proving the retry re-read.
        assert progress["completed"] == 4

    def test_raises_bulk_processing_error_after_max_concurrent_retries(self, worker):
        w, _, s3_mock, _ = worker
        response = _progress_response(completed=0, total=10)
        response["ETag"] = '"e"'
        s3_mock.get_object.return_value = response
        s3_mock.put_object.side_effect = ClientError(
            {"Error": {"Code": "PreconditionFailed", "Message": "ETag mismatch"}},
            "PutObject",
        )

        with patch("src.bulk.bulk_worker.time.sleep"):
            with pytest.raises(BulkProcessingError, match="concurrent-write retries"):
                w._update_progress("bucket", "bulk-test-001", 42, True, 10)

    def test_non_412_put_error_propagates_immediately(self, worker):
        """AccessDenied / network / throttling on PutObject should NOT be
        retried as if it were a concurrency conflict.
        """
        w, _, s3_mock, _ = worker
        response = _progress_response(completed=0, total=10)
        response["ETag"] = '"e"'
        s3_mock.get_object.return_value = response
        s3_mock.put_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "PutObject"
        )

        with pytest.raises(ClientError, match="AccessDenied"):
            w._update_progress("bucket", "bulk-test-001", 42, True, 10)
        # First attempt should fail immediately, no retry.
        assert s3_mock.put_object.call_count == 1

    def test_non_nosuchkey_get_error_propagates(self, worker):
        """AccessDenied on the GET must not be silently masked into a
        fresh-default retry loop that would overwrite real state.
        """
        w, _, s3_mock, _ = worker
        s3_mock.get_object.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "GetObject"
        )

        with pytest.raises(ClientError, match="AccessDenied"):
            w._update_progress("bucket", "bulk-test-001", 42, True, 10)
        # No PUT should have been attempted.
        s3_mock.put_object.assert_not_called()


# --- Completion notification ---


class TestCompletionNotification:
    def test_sends_sns_on_completion(self, worker):
        w, lambda_mock, s3_mock, sns_mock = worker
        lambda_mock.invoke.return_value = _orchestrator_success_response()

        # Readback's range get_object and progress's get_object share the same
        # mock; dispatch on Range so the progress read returns fresh bytes
        # each call instead of seeing an exhausted BytesIO.
        def get_object_dispatch(**kwargs):
            if "Range" in kwargs:
                return {"Body": BytesIO(b"")}
            return _progress_response(completed=9, total=10)

        s3_mock.get_object.side_effect = get_object_dispatch
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


# --- CC3-860: Text-only path ---


class TestTextOnlyPath:
    def test_skips_copy_and_meta(self, worker):
        w, lambda_mock, s3_mock, _ = worker
        lambda_mock.invoke.return_value = _orchestrator_success_response()
        s3_mock.get_object.return_value = _progress_response(completed=0, total=1)
        item = _make_item(s3_key=None, input_text="User abstract.")
        record = _make_sqs_record(item=item, total_items=1)

        w.process_item(record)

        # No copy_object (no file to copy)
        s3_mock.copy_object.assert_not_called()
        # put_object only for progress, NOT for meta.json
        put_calls = s3_mock.put_object.call_args_list
        put_keys = [c[1]["Key"] for c in put_calls]
        assert not any("meta.json" in k for k in put_keys)

    def test_direct_invocation_format(self, worker):
        w, lambda_mock, s3_mock, _ = worker
        lambda_mock.invoke.return_value = _orchestrator_success_response()
        s3_mock.get_object.return_value = _progress_response(completed=0, total=1)
        item = _make_item(s3_key=None, input_text="User abstract.")
        record = _make_sqs_record(item=item, total_items=1)

        w.process_item(record)

        payload = json.loads(lambda_mock.invoke.call_args[1]["Payload"])
        assert "meta" in payload
        assert "key" not in payload
        assert payload["meta"]["input_text"] == "User abstract."

    def test_meta_fields_correct(self, worker):
        w, lambda_mock, s3_mock, _ = worker
        lambda_mock.invoke.return_value = _orchestrator_success_response()
        s3_mock.get_object.return_value = _progress_response(completed=0, total=1)
        item = _make_item(
            item_id=42, s3_key=None, input_text="Text.",
            product_part_number="PESTEXT042",
        )
        record = _make_sqs_record(item=item, total_items=1)

        w.process_item(record)

        meta = json.loads(lambda_mock.invoke.call_args[1]["Payload"])["meta"]
        assert meta["item_id"] == "42"
        assert meta["ou"] == "PES"
        assert meta["product_part_number"] == "PESTEXT042"
        assert meta["ai_enrichment_enabled"] is True
        assert meta["content"]["media_type"] == "text/plain"
        assert meta["callback_url"] == "https://example.com/webhook"

    def test_with_requested_fields(self, worker):
        w, lambda_mock, s3_mock, _ = worker
        lambda_mock.invoke.return_value = _orchestrator_success_response()
        s3_mock.get_object.return_value = _progress_response(completed=0, total=1)
        item = _make_item(
            s3_key=None, input_text="Text.",
            requested_fields=["keywords", "category"],
        )
        record = _make_sqs_record(item=item, total_items=1)

        w.process_item(record)

        meta = json.loads(lambda_mock.invoke.call_args[1]["Payload"])["meta"]
        assert meta["requested_fields"] == ["keywords", "category"]

    def test_with_as_abstract_mode(self, worker):
        w, lambda_mock, s3_mock, _ = worker
        lambda_mock.invoke.return_value = _orchestrator_success_response()
        s3_mock.get_object.return_value = _progress_response(completed=0, total=1)
        item = _make_item(
            s3_key=None, input_text="Abstract.",
            input_text_mode="as_abstract",
        )
        record = _make_sqs_record(item=item, total_items=1)

        w.process_item(record)

        meta = json.loads(lambda_mock.invoke.call_args[1]["Payload"])["meta"]
        assert meta["input_text_mode"] == "as_abstract"

    def test_still_updates_progress(self, worker):
        w, lambda_mock, s3_mock, _ = worker
        lambda_mock.invoke.return_value = _orchestrator_success_response()
        s3_mock.get_object.return_value = _progress_response(completed=0, total=1)
        item = _make_item(s3_key=None, input_text="Text.")
        record = _make_sqs_record(item=item, total_items=1)

        result = w.process_item(record)

        assert result["action"] == "processed"
        # Progress file written
        put_calls = s3_mock.put_object.call_args_list
        progress_puts = [c for c in put_calls if "progress" in c[1]["Key"]]
        assert len(progress_puts) == 1


# --- CC3-860: Hybrid path ---


class TestHybridPath:
    def test_copies_file_and_adds_text_to_meta(self, worker):
        w, lambda_mock, s3_mock, _ = worker
        lambda_mock.invoke.return_value = _orchestrator_success_response()
        s3_mock.get_object.return_value = _progress_response(completed=0, total=1)
        item = _make_item(
            input_text="Existing abstract.",
            input_text_mode="as_abstract",
            requested_fields=["keywords", "category"],
        )
        record = _make_sqs_record(item=item, total_items=1)

        w.process_item(record)

        # File was copied
        s3_mock.copy_object.assert_called_once()
        # Meta.json includes input_text fields
        meta_put = [
            c for c in s3_mock.put_object.call_args_list
            if "meta.json" in c[1]["Key"]
        ]
        assert len(meta_put) == 1
        meta = json.loads(meta_put[0][1]["Body"])
        assert meta["input_text"] == "Existing abstract."
        assert meta["input_text_mode"] == "as_abstract"
        assert meta["requested_fields"] == ["keywords", "category"]

    def test_uses_standard_key_path(self, worker):
        w, lambda_mock, s3_mock, _ = worker
        lambda_mock.invoke.return_value = _orchestrator_success_response()
        s3_mock.get_object.return_value = _progress_response(completed=0, total=1)
        item = _make_item(input_text="Text.")
        record = _make_sqs_record(item=item, total_items=1)

        w.process_item(record)

        payload = json.loads(lambda_mock.invoke.call_args[1]["Payload"])
        assert "key" in payload
        assert "meta" not in payload


# --- CC3-860: Cross-bucket copy ---


class TestCrossBucketCopy:
    def test_uses_source_bucket(self, worker):
        w, _, s3_mock, _ = worker
        item = _make_item(source_bucket="other-bucket")

        w._copy_to_pending("pipeline-bucket", item)

        call_kwargs = s3_mock.copy_object.call_args[1]
        assert call_kwargs["CopySource"]["Bucket"] == "other-bucket"
        assert call_kwargs["Bucket"] == "pipeline-bucket"

    def test_no_source_bucket_uses_default(self, worker):
        w, _, s3_mock, _ = worker
        item = _make_item()

        w._copy_to_pending("pipeline-bucket", item)

        call_kwargs = s3_mock.copy_object.call_args[1]
        assert call_kwargs["CopySource"]["Bucket"] == "pipeline-bucket"


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
