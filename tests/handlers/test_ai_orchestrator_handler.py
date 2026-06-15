"""Tests for AI Orchestrator Lambda handler."""

import json
from io import BytesIO
from unittest.mock import patch, MagicMock

import pytest

from src.handlers.ai_orchestrator_handler import handler, _parse_event


# ---------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------

@pytest.fixture
def mock_orchestrator():
    with patch("src.handlers.ai_orchestrator_handler._orchestrator") as mock:
        mock.process.return_value = {
            "item_id": "STD-12345",
            "ou": "PES",
            "action": "enriched",
            "ai_enrichment_enabled": True,
            "source_key": "PES/pending/STD-12345.pdf",
            "destination_key": "PES/processed/STD-12345.pdf",
            "processing_time_ms": 5000,
            "details": {},
        }
        yield mock


def _s3_event(bucket="bucket", key="PES/pending/STD-12345.pdf"):
    return {
        "Records": [{
            "s3": {
                "bucket": {"name": bucket},
                "object": {"key": key},
            }
        }]
    }


def _direct_event(bucket="bucket", key="PES/pending/STD-12345.pdf"):
    return {"bucket": bucket, "key": key}


def _eventbridge_event(bucket="bucket", key="PES/pending/STD-12345.pdf"):
    return {
        "version": "0",
        "source": "aws.s3",
        "detail-type": "Object Created",
        "detail": {
            "bucket": {"name": bucket},
            "object": {"key": key},
        },
    }


# ---------------------------------------------------------------
# Event Parsing
# ---------------------------------------------------------------

class TestParseEvent:
    def test_s3_event(self):
        bucket, key = _parse_event(_s3_event())
        assert bucket == "bucket"
        assert key == "PES/pending/STD-12345.pdf"

    def test_direct_event(self):
        bucket, key = _parse_event(_direct_event())
        assert bucket == "bucket"
        assert key == "PES/pending/STD-12345.pdf"

    def test_eventbridge_event(self):
        bucket, key = _parse_event(_eventbridge_event())
        assert bucket == "bucket"
        assert key == "PES/pending/STD-12345.pdf"

    def test_missing_records_and_bucket_raises(self):
        with pytest.raises(KeyError, match="Records"):
            _parse_event({})

    def test_invalid_key_pattern_raises(self):
        with pytest.raises(ValueError, match="does not match"):
            _parse_event({"bucket": "b", "key": "wrong/path.pdf"})

    def test_key_without_pending_raises(self):
        with pytest.raises(ValueError, match="does not match"):
            _parse_event({"bucket": "b", "key": "PES/uploads/file.pdf"})

    def test_eventbridge_invalid_key_pattern_raises(self):
        with pytest.raises(ValueError, match="does not match"):
            _parse_event(_eventbridge_event(key="iplr_uploads/file.pdf"))


# ---------------------------------------------------------------
# Direct Invocation
# ---------------------------------------------------------------

class TestDirectInvocation:
    def test_success(self, mock_orchestrator):
        result = handler(_direct_event(), None)

        assert result["statusCode"] == 200
        assert result["body"]["item_id"] == "STD-12345"
        assert result["body"]["action"] == "enriched"
        mock_orchestrator.process.assert_called_once()

    def test_passes_bucket_and_key(self, mock_orchestrator):
        handler(_direct_event("my-bucket", "PES/pending/doc.pdf"), None)

        call_kwargs = mock_orchestrator.process.call_args[1]
        assert call_kwargs["bucket"] == "my-bucket"
        assert call_kwargs["key"] == "PES/pending/doc.pdf"


# ---------------------------------------------------------------
# S3 Event Invocation
# ---------------------------------------------------------------

class TestS3EventInvocation:
    def test_success(self, mock_orchestrator):
        result = handler(_s3_event(), None)

        assert result["statusCode"] == 200
        mock_orchestrator.process.assert_called_once()

    def test_extracts_bucket_and_key(self, mock_orchestrator):
        handler(_s3_event("prod-bucket", "AESS/pending/lecture.mp4"), None)

        call_kwargs = mock_orchestrator.process.call_args[1]
        assert call_kwargs["bucket"] == "prod-bucket"
        assert call_kwargs["key"] == "AESS/pending/lecture.mp4"


class TestEventBridgeInvocation:
    def test_success(self, mock_orchestrator):
        result = handler(_eventbridge_event(), None)

        assert result["statusCode"] == 200
        mock_orchestrator.process.assert_called_once()

    def test_extracts_bucket_and_key(self, mock_orchestrator):
        handler(_eventbridge_event("dev-bucket", "PES/pending/93.pdf"), None)

        call_kwargs = mock_orchestrator.process.call_args[1]
        assert call_kwargs["bucket"] == "dev-bucket"
        assert call_kwargs["key"] == "PES/pending/93.pdf"


# ---------------------------------------------------------------
# pending/metadata/ side-effect skip (CC3-995)
# ---------------------------------------------------------------

class TestPendingMetadataSkip:
    """get-video-info Lambda writes side-effect JSONs under {ou}/pending/metadata/
    that the */pending/* EventBridge rule can't easily exclude. The handler
    short-circuits with a clean 200 instead of running the orchestrator."""

    def test_eventbridge_metadata_path_skipped(self, mock_orchestrator):
        result = handler(
            _eventbridge_event(
                "dev-ieee-conference-cloud-bulk-uploads",
                "APS/pending/metadata/100.mp4.json",
            ),
            None,
        )

        assert result["statusCode"] == 200
        assert result["body"]["action"] == "skipped"
        assert result["body"]["key"] == "APS/pending/metadata/100.mp4.json"
        mock_orchestrator.process.assert_not_called()

    def test_s3_metadata_path_skipped(self, mock_orchestrator):
        result = handler(
            _s3_event(
                "dev-ieee-conference-cloud-bulk-uploads",
                "PES/pending/metadata/200.mp4.json",
            ),
            None,
        )

        assert result["statusCode"] == 200
        assert result["body"]["action"] == "skipped"
        mock_orchestrator.process.assert_not_called()

    def test_legit_pending_media_still_processes(self, mock_orchestrator):
        # Regression guard: don't accidentally over-skip. Files directly in
        # {ou}/pending/ should still be dispatched.
        result = handler(
            _eventbridge_event("dev-bucket", "APS/pending/100.mp4"),
            None,
        )

        assert result["statusCode"] == 200
        mock_orchestrator.process.assert_called_once()


# ---------------------------------------------------------------
# Error Handling
# ---------------------------------------------------------------

class TestErrorHandling:
    def test_bad_event_returns_400(self, mock_orchestrator):
        result = handler({}, None)
        assert result["statusCode"] == 400

    def test_invalid_key_returns_400(self, mock_orchestrator):
        result = handler({"bucket": "b", "key": "bad/path"}, None)
        assert result["statusCode"] == 400

    def test_validation_error_returns_400(self, mock_orchestrator):
        mock_orchestrator.process.side_effect = ValueError("Missing field")
        result = handler(_direct_event(), None)
        assert result["statusCode"] == 400
        assert "Missing field" in result["body"]["error"]

    def test_client_error_returns_500(self, mock_orchestrator):
        from botocore.exceptions import ClientError
        mock_orchestrator.process.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "Forbidden"}},
            "GetObject",
        )
        result = handler(_direct_event(), None)
        assert result["statusCode"] == 500
        assert "AccessDenied" in result["body"]["error"]

    def test_runtime_error_returns_500(self, mock_orchestrator):
        mock_orchestrator.process.side_effect = RuntimeError("Lambda failed")
        result = handler(_direct_event(), None)
        assert result["statusCode"] == 500
        assert "Lambda failed" in result["body"]["error"]

    def test_unexpected_error_returns_500(self, mock_orchestrator):
        mock_orchestrator.process.side_effect = TypeError("bad type")
        result = handler(_direct_event(), None)
        assert result["statusCode"] == 500
        assert "TypeError" in result["body"]["error"]


# ---------------------------------------------------------------
# Context Handling
# ---------------------------------------------------------------

class TestContextHandling:
    def test_passes_request_id_from_context(self, mock_orchestrator):
        ctx = MagicMock()
        ctx.aws_request_id = "req-abc-123"

        handler(_direct_event(), ctx)

        call_kwargs = mock_orchestrator.process.call_args[1]
        assert call_kwargs["request_id"] == "req-abc-123"

    def test_handles_none_context(self, mock_orchestrator):
        handler(_direct_event(), None)

        call_kwargs = mock_orchestrator.process.call_args[1]
        assert call_kwargs["request_id"] == ""


# ---------------------------------------------------------------
# CC3-858: Direct Meta Invocation
# ---------------------------------------------------------------

def _make_direct_meta_event(
    input_text="User abstract text",
    input_text_mode="as_source",
    requested_fields=None,
):
    meta = {
        "item_id": "12345",
        "ai_enrichment_enabled": True,
        "input_text": input_text,
        "input_text_mode": input_text_mode,
        "content": {"media_type": "text", "resource_center": "PES"},
        "ou": "PES",
        "callback_url": "https://drupal.example.com/hook",
    }
    if requested_fields:
        meta["requested_fields"] = requested_fields
    return {"bucket": "test-bucket", "meta": meta}


class TestDirectMetaInvocation:
    def test_happy_path_returns_200(self, mock_orchestrator):
        event = _make_direct_meta_event()
        result = handler(event, None)

        assert result["statusCode"] == 200
        call_kwargs = mock_orchestrator.process.call_args[1]
        assert call_kwargs["key"] is None
        assert call_kwargs["meta"]["input_text"] == "User abstract text"
        assert call_kwargs["bucket"] == "test-bucket"

    def test_missing_input_text_returns_400(self, mock_orchestrator):
        event = _make_direct_meta_event()
        del event["meta"]["input_text"]
        result = handler(event, None)

        assert result["statusCode"] == 400
        assert "input_text" in result["body"]["error"]
        mock_orchestrator.process.assert_not_called()

    def test_invalid_input_text_mode_returns_400(self, mock_orchestrator):
        event = _make_direct_meta_event(input_text_mode="bad_mode")
        result = handler(event, None)

        assert result["statusCode"] == 400
        assert "input_text_mode" in result["body"]["error"]
        mock_orchestrator.process.assert_not_called()

    def test_missing_content_returns_400(self, mock_orchestrator):
        event = _make_direct_meta_event()
        del event["meta"]["content"]
        result = handler(event, None)

        assert result["statusCode"] == 400
        assert "content" in result["body"]["error"]
        mock_orchestrator.process.assert_not_called()

    def test_s3_event_still_routes_correctly(self, mock_orchestrator):
        """Backward compat: S3 events bypass the meta branch."""
        event = {
            "Records": [{
                "s3": {
                    "bucket": {"name": "bucket"},
                    "object": {"key": "PES/pending/STD-12345.pdf"},
                }
            }]
        }
        result = handler(event, None)

        assert result["statusCode"] == 200
        call_kwargs = mock_orchestrator.process.call_args[1]
        assert call_kwargs["key"] == "PES/pending/STD-12345.pdf"
        assert "meta" not in call_kwargs or call_kwargs.get("meta") is None

    @patch("src.handlers.ai_orchestrator_handler._sqs_client")
    @patch("src.handlers.ai_orchestrator_handler.DLQ_QUEUE_URL", "https://sqs/dlq")
    def test_processing_error_publishes_to_dlq(self, mock_sqs, mock_orchestrator):
        """DLQ receives the original direct invocation event on failure."""
        mock_orchestrator.process.side_effect = RuntimeError("Bedrock down")
        event = _make_direct_meta_event()

        result = handler(event, None)

        assert result["statusCode"] == 500
        mock_sqs.send_message.assert_called_once()


# ---------------------------------------------------------------
# CC3-1049: no duplicate webhook when an error occurs AFTER the
# success webhook (Step 6 move / Step 7 metrics)
# ---------------------------------------------------------------

def _lambda_invoke_response(status_code=200, body=None):
    """Build a mocked lambda.invoke() return value (mirrors the orchestrator
    unit-test helper)."""
    payload = {"statusCode": status_code, "body": body or {}}
    mock_payload = MagicMock()
    mock_payload.read.return_value = json.dumps(payload).encode()
    return {"Payload": mock_payload, "StatusCode": 200}


def _enriched_pdf_meta():
    return {
        "request_id": 42,
        "item_id": "STD-12345",
        "ou": "PES",
        "product_part_number": "STD-12345",
        "ai_enrichment_enabled": True,
        "content": {
            "media_type": "application/pdf",
            "filename": "STD-12345.pdf",
            "resource_center": "PES",
        },
        "callback_url": "https://drupal.example.com/hook",
    }


class TestNoDuplicateWebhookAfterSuccess:
    """CC3-1049 regression: an error in Step 6 (S3 move) or Step 7 (metrics) —
    which runs *after* the Step 5 success webhook — must NOT make the handler's
    except block deliver a second 'failure' webhook for the same item, flipping
    a correctly-enriched item into the failure path.

    Drives the *real* orchestrator through ``handler()`` so the
    success→failure interaction is exercised end-to-end. The unit test
    (``test_failure_webhook_sent_on_processing_error``) calls
    ``send_failure_webhook()`` manually, so it can't catch this.
    """

    def _real_orchestrator(self, copy_object_side_effect):
        from src.orchestrator.ai_orchestrator import AIOrchestrator

        s3 = MagicMock()
        lam = MagicMock()
        secrets = MagicMock()
        secrets.get_secret_value.return_value = {"SecretString": "test-secret"}

        s3.get_object.return_value = {
            "Body": BytesIO(json.dumps(_enriched_pdf_meta()).encode())
        }
        s3.copy_object.side_effect = copy_object_side_effect

        lam.invoke.side_effect = [
            _lambda_invoke_response(
                200,
                {"text": "text", "page_count": 5, "extraction_method": "extract_text"},
            ),
            _lambda_invoke_response(
                200,
                {"abstract": "a", "keywords": ["k"], "learning_level": "Expert"},
            ),
        ]

        return AIOrchestrator(
            s3_client=s3, lambda_client=lam, secrets_client=secrets
        ), s3

    @patch("src.handlers.ai_orchestrator_handler.DLQ_QUEUE_URL", "")
    @patch("src.orchestrator.ai_orchestrator.WebhookSender.send", return_value=True)
    def test_step6_move_clienterror_sends_only_success(self, mock_send):
        from botocore.exceptions import ClientError

        # _move_file's copy_object raises AccessDenied AFTER the success webhook.
        real_orch, _s3 = self._real_orchestrator(
            ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "Forbidden"}},
                "CopyObject",
            )
        )

        with patch("src.handlers.ai_orchestrator_handler._orchestrator", real_orch):
            result = handler(_s3_event(), None)

        assert result["statusCode"] == 500
        assert "AccessDenied" in result["body"]["error"]

        # Exactly one webhook — the success. No failure webhook on top of it.
        assert mock_send.call_count == 1
        assert mock_send.call_args[0][2]["status"] == "success"

    @patch("src.handlers.ai_orchestrator_handler.DLQ_QUEUE_URL", "")
    @patch("src.orchestrator.ai_orchestrator.WebhookSender.send", return_value=True)
    def test_step6_move_generic_error_sends_only_success(self, mock_send):
        # The except Exception path (e.g. delete_object failing) must behave
        # the same way — single success webhook, no failure follow-up.
        real_orch, s3 = self._real_orchestrator(None)
        s3.delete_object.side_effect = RuntimeError("transient S3 delete failure")

        with patch("src.handlers.ai_orchestrator_handler._orchestrator", real_orch):
            result = handler(_s3_event(), None)

        assert result["statusCode"] == 500
        assert mock_send.call_count == 1
        assert mock_send.call_args[0][2]["status"] == "success"
