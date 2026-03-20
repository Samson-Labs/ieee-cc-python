"""Tests for AIOrchestrator."""

import json
import time
from io import BytesIO
from unittest.mock import MagicMock, patch, call

import pytest

from src.orchestrator.ai_orchestrator import AIOrchestrator


# ---------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------

def _make_meta(
    ai_enabled=True,
    media_type="application/pdf",
    webhook_url=None,
):
    meta = {
        "item_id": "STD-12345",
        "ou": "PES",
        "product_part_number": "STD-12345",
        "ai_enrichment_enabled": ai_enabled,
        "content": {
            "media_type": media_type,
            "filename": "STD-12345.pdf",
        },
    }
    if webhook_url:
        meta["webhook_url"] = webhook_url
    return meta


def _s3_get_object_response(body_dict):
    return {"Body": BytesIO(json.dumps(body_dict).encode())}


def _lambda_invoke_response(status_code=200, body=None):
    payload = {"statusCode": status_code, "body": body or {}}
    mock_payload = MagicMock()
    mock_payload.read.return_value = json.dumps(payload).encode()
    return {"Payload": mock_payload, "StatusCode": 200}


@pytest.fixture
def orchestrator():
    s3 = MagicMock()
    lam = MagicMock()
    sns = MagicMock()
    orch = AIOrchestrator(s3_client=s3, lambda_client=lam, sns_client=sns)
    return orch, s3, lam


# ---------------------------------------------------------------
# Key Parsing
# ---------------------------------------------------------------

class TestParseKey:
    def test_valid_pdf_key(self):
        ou, item_id, ext = AIOrchestrator._parse_key("PES/pending/STD-12345.pdf")
        assert ou == "PES"
        assert item_id == "STD-12345"
        assert ext == "pdf"

    def test_valid_video_key(self):
        ou, item_id, ext = AIOrchestrator._parse_key("AESS/pending/lecture.mp4")
        assert ou == "AESS"
        assert item_id == "lecture"
        assert ext == "mp4"

    def test_uppercase_extension_lowered(self):
        _, _, ext = AIOrchestrator._parse_key("PES/pending/doc.PDF")
        assert ext == "pdf"

    def test_missing_pending_raises(self):
        with pytest.raises(ValueError, match="does not match"):
            AIOrchestrator._parse_key("PES/uploads/file.pdf")

    def test_no_extension_raises(self):
        with pytest.raises(ValueError, match="no extension"):
            AIOrchestrator._parse_key("PES/pending/noext")

    def test_short_key_raises(self):
        with pytest.raises(ValueError, match="does not match"):
            AIOrchestrator._parse_key("file.pdf")


# ---------------------------------------------------------------
# Meta JSON Validation
# ---------------------------------------------------------------

class TestValidateMeta:
    def test_valid_meta_passes(self):
        AIOrchestrator._validate_meta(_make_meta())

    def test_missing_top_level_field(self):
        meta = _make_meta()
        del meta["item_id"]
        with pytest.raises(ValueError, match="Missing required .meta.json"):
            AIOrchestrator._validate_meta(meta)

    def test_missing_content_field(self):
        meta = _make_meta()
        del meta["content"]["media_type"]
        with pytest.raises(ValueError, match="Missing required content"):
            AIOrchestrator._validate_meta(meta)

    def test_content_not_dict(self):
        meta = _make_meta()
        meta["content"] = "not a dict"
        with pytest.raises(ValueError, match="must be an object"):
            AIOrchestrator._validate_meta(meta)

    def test_all_required_fields_present(self):
        meta = _make_meta()
        # Should not raise
        AIOrchestrator._validate_meta(meta)


# ---------------------------------------------------------------
# Meta JSON Reading
# ---------------------------------------------------------------

class TestReadMetaJson:
    def test_reads_valid_meta(self, orchestrator):
        orch, s3, _ = orchestrator
        meta = _make_meta()
        s3.get_object.return_value = _s3_get_object_response(meta)

        result = orch._read_meta_json("bucket", "key", "[test]")
        assert result["item_id"] == "STD-12345"

    def test_no_such_key_raises_value_error(self, orchestrator):
        orch, s3, _ = orchestrator
        s3.get_object.side_effect = _client_error("NoSuchKey")

        with pytest.raises(ValueError, match="Meta file not found"):
            orch._read_meta_json("bucket", "key", "[test]")

    @patch("src.orchestrator.ai_orchestrator.time.sleep")
    def test_retries_on_transient_error(self, mock_sleep, orchestrator):
        orch, s3, _ = orchestrator
        meta = _make_meta()
        s3.get_object.side_effect = [
            _client_error("InternalError"),
            _s3_get_object_response(meta),
        ]

        result = orch._read_meta_json("bucket", "key", "[test]")
        assert result["item_id"] == "STD-12345"
        assert s3.get_object.call_count == 2

    @patch("src.orchestrator.ai_orchestrator.time.sleep")
    def test_exhausts_retries(self, mock_sleep, orchestrator):
        orch, s3, _ = orchestrator
        s3.get_object.side_effect = _client_error("InternalError")

        with pytest.raises(RuntimeError, match="after 3 retries"):
            orch._read_meta_json("bucket", "key", "[test]")
        assert s3.get_object.call_count == 3

    def test_invalid_json_raises(self, orchestrator):
        orch, s3, _ = orchestrator
        s3.get_object.return_value = {"Body": BytesIO(b"not json")}

        with pytest.raises(ValueError, match="Invalid JSON"):
            orch._read_meta_json("bucket", "key", "[test]")


# ---------------------------------------------------------------
# File Move
# ---------------------------------------------------------------

class TestMoveFile:
    def test_copies_and_deletes(self, orchestrator):
        orch, s3, _ = orchestrator

        orch._move_file("bucket", "PES/pending/f.pdf", "PES/processed/f.pdf", "[t]")

        s3.copy_object.assert_called_once_with(
            Bucket="bucket",
            CopySource={"Bucket": "bucket", "Key": "PES/pending/f.pdf"},
            Key="PES/processed/f.pdf",
        )
        s3.delete_object.assert_called_once_with(
            Bucket="bucket", Key="PES/pending/f.pdf"
        )


# ---------------------------------------------------------------
# AI Disabled Flow
# ---------------------------------------------------------------

class TestAIDisabledFlow:
    def test_moves_file_when_ai_disabled(self, orchestrator):
        orch, s3, _ = orchestrator
        meta = _make_meta(ai_enabled=False)
        s3.get_object.return_value = _s3_get_object_response(meta)

        result = orch.process("bucket", "PES/pending/STD-12345.pdf")

        assert result["action"] == "moved"
        assert result["ai_enrichment_enabled"] is False
        assert result["destination_key"] == "PES/processed/STD-12345.pdf"
        s3.copy_object.assert_called_once()
        s3.delete_object.assert_called_once()

    def test_does_not_invoke_lambda_when_ai_disabled(self, orchestrator):
        orch, s3, lam = orchestrator
        meta = _make_meta(ai_enabled=False)
        s3.get_object.return_value = _s3_get_object_response(meta)

        orch.process("bucket", "PES/pending/STD-12345.pdf")

        lam.invoke.assert_not_called()


# ---------------------------------------------------------------
# AI Enabled — PDF Flow
# ---------------------------------------------------------------

class TestPDFFlow:
    def test_dispatches_to_pdf_extractor_and_bedrock(self, orchestrator):
        orch, s3, lam = orchestrator
        meta = _make_meta(ai_enabled=True, media_type="application/pdf")
        s3.get_object.return_value = _s3_get_object_response(meta)

        extraction_body = {"text": "extracted text", "page_count": 10, "extraction_method": "text"}
        bedrock_body = {"abstract": "summary", "keywords": ["ai"], "learning_level": "Expert", "intended_audience": "Seasoned", "category": "Research"}

        lam.invoke.side_effect = [
            _lambda_invoke_response(200, extraction_body),
            _lambda_invoke_response(200, bedrock_body),
        ]

        result = orch.process("bucket", "PES/pending/STD-12345.pdf")

        assert result["action"] == "enriched"
        assert result["ai_enrichment_enabled"] is True
        assert lam.invoke.call_count == 2

        # First call: PDF extractor
        first_call = lam.invoke.call_args_list[0]
        assert first_call[1]["FunctionName"] == "ieee-cc-pdf-extractor"

        # Second call: Bedrock
        second_call = lam.invoke.call_args_list[1]
        assert second_call[1]["FunctionName"] == "ieee-cc-bedrock-inference"

    def test_skips_bedrock_when_no_text(self, orchestrator):
        orch, s3, lam = orchestrator
        meta = _make_meta(ai_enabled=True)
        s3.get_object.return_value = _s3_get_object_response(meta)

        extraction_body = {"text": "", "page_count": 10, "extraction_method": "ocr"}
        lam.invoke.return_value = _lambda_invoke_response(200, extraction_body)

        result = orch.process("bucket", "PES/pending/STD-12345.pdf")

        assert result["action"] == "enriched"
        assert lam.invoke.call_count == 1  # Only PDF extractor, no Bedrock


# ---------------------------------------------------------------
# AI Enabled — Video Flow
# ---------------------------------------------------------------

class TestVideoFlow:
    def test_dispatches_to_video_transcriber(self, orchestrator):
        orch, s3, lam = orchestrator
        meta = _make_meta(ai_enabled=True, media_type="video/mp4")
        meta["content"]["filename"] = "lecture.mp4"
        s3.get_object.return_value = _s3_get_object_response(meta)

        transcription_body = {"transcript": "hello world", "duration": "00:10:00", "duration_seconds": 600, "speaker_count": 1}
        bedrock_body = {"abstract": "talk", "keywords": ["video"], "learning_level": "Foundational", "intended_audience": "New", "category": "Tutorial"}

        lam.invoke.side_effect = [
            _lambda_invoke_response(200, transcription_body),
            _lambda_invoke_response(200, bedrock_body),
        ]

        result = orch.process("bucket", "AESS/pending/lecture.mp4")

        assert result["action"] == "enriched"
        first_call = lam.invoke.call_args_list[0]
        assert first_call[1]["FunctionName"] == "ieee-cc-video-transcriber"

    def test_supports_quicktime_media_type(self, orchestrator):
        orch, s3, lam = orchestrator
        meta = _make_meta(ai_enabled=True, media_type="video/quicktime")
        meta["content"]["filename"] = "lecture.mov"
        s3.get_object.return_value = _s3_get_object_response(meta)

        lam.invoke.side_effect = [
            _lambda_invoke_response(200, {"transcript": "text", "duration": "00:01:00", "duration_seconds": 60, "speaker_count": 1}),
            _lambda_invoke_response(200, {"abstract": "a", "keywords": [], "learning_level": "Expert", "intended_audience": "Seasoned", "category": "Research"}),
        ]

        result = orch.process("bucket", "PES/pending/lecture.mov")
        assert result["action"] == "enriched"

    def test_unsupported_media_type_raises(self, orchestrator):
        orch, s3, _ = orchestrator
        meta = _make_meta(ai_enabled=True, media_type="image/png")
        s3.get_object.return_value = _s3_get_object_response(meta)

        with pytest.raises(ValueError, match="Unsupported media type"):
            orch.process("bucket", "PES/pending/image.png")


# ---------------------------------------------------------------
# Lambda Dispatch Errors
# ---------------------------------------------------------------

class TestDispatchErrors:
    def test_extraction_function_error(self, orchestrator):
        orch, s3, lam = orchestrator
        meta = _make_meta(ai_enabled=True)
        s3.get_object.return_value = _s3_get_object_response(meta)

        error_payload = MagicMock()
        error_payload.read.return_value = json.dumps({"errorMessage": "boom"}).encode()
        lam.invoke.return_value = {"Payload": error_payload, "StatusCode": 200, "FunctionError": "Unhandled"}

        with pytest.raises(RuntimeError, match="failed: boom"):
            orch.process("bucket", "PES/pending/STD-12345.pdf")

    def test_extraction_non_200_status(self, orchestrator):
        orch, s3, lam = orchestrator
        meta = _make_meta(ai_enabled=True)
        s3.get_object.return_value = _s3_get_object_response(meta)

        lam.invoke.return_value = _lambda_invoke_response(500, {"error": "S3 failure"})

        with pytest.raises(RuntimeError, match="returned 500"):
            orch.process("bucket", "PES/pending/STD-12345.pdf")


# ---------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------

class TestWebhook:
    @patch("src.orchestrator.ai_orchestrator.WebhookSender.send", return_value=True)
    def test_sends_webhook_on_success(self, mock_send, orchestrator):
        orch, s3, lam = orchestrator
        meta = _make_meta(ai_enabled=True, webhook_url="https://drupal.example.com/hook")
        s3.get_object.return_value = _s3_get_object_response(meta)

        extraction_body = {"text": "text", "page_count": 5, "extraction_method": "text"}
        bedrock_body = {"abstract": "a", "keywords": [], "learning_level": "Expert", "intended_audience": "Seasoned", "category": "Research"}
        lam.invoke.side_effect = [
            _lambda_invoke_response(200, extraction_body),
            _lambda_invoke_response(200, bedrock_body),
        ]

        result = orch.process("bucket", "PES/pending/STD-12345.pdf")

        assert result["details"]["webhook_sent"] is True
        mock_send.assert_called_once()

        # Verify correct URL and payload structure
        call_args = mock_send.call_args
        assert call_args[0][0] == "https://drupal.example.com/hook"
        payload = call_args[0][2]
        assert payload["item_id"] == "STD-12345"
        assert payload["ou"] == "PES"
        assert payload["product_part_number"] == "STD-12345"
        assert payload["status"] == "completed"

    def test_no_webhook_url_skips(self, orchestrator):
        orch, s3, lam = orchestrator
        meta = _make_meta(ai_enabled=True)  # No webhook_url
        s3.get_object.return_value = _s3_get_object_response(meta)

        extraction_body = {"text": "text", "page_count": 5, "extraction_method": "text"}
        bedrock_body = {"abstract": "a", "keywords": [], "learning_level": "Expert", "intended_audience": "Seasoned", "category": "Research"}
        lam.invoke.side_effect = [
            _lambda_invoke_response(200, extraction_body),
            _lambda_invoke_response(200, bedrock_body),
        ]

        result = orch.process("bucket", "PES/pending/STD-12345.pdf")

        assert result["details"]["webhook_sent"] is False

    @patch("src.orchestrator.ai_orchestrator.WebhookSender.send", return_value=False)
    def test_webhook_failure_does_not_block(self, mock_send, orchestrator):
        orch, s3, lam = orchestrator
        meta = _make_meta(ai_enabled=True, webhook_url="https://drupal.example.com/hook")
        s3.get_object.return_value = _s3_get_object_response(meta)

        extraction_body = {"text": "text", "page_count": 5, "extraction_method": "text"}
        bedrock_body = {"abstract": "a", "keywords": [], "learning_level": "Expert", "intended_audience": "Seasoned", "category": "Research"}
        lam.invoke.side_effect = [
            _lambda_invoke_response(200, extraction_body),
            _lambda_invoke_response(200, bedrock_body),
        ]

        result = orch.process("bucket", "PES/pending/STD-12345.pdf")

        # Should still succeed — webhook failure is non-fatal
        assert result["action"] == "enriched"
        assert result["details"]["webhook_sent"] is False


# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------

def _client_error(code, message="Error"):
    from botocore.exceptions import ClientError
    return ClientError(
        {"Error": {"Code": code, "Message": message}},
        "GetObject",
    )
