"""Tests for the Wizard Async Transfer Lambda handler (CC3-898)."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError

from src.handlers.wizard_transfer_handler import _parse_event, handler


@pytest.fixture
def mock_transfer():
    with patch("src.handlers.wizard_transfer_handler._transfer") as mock:
        mock.process_trigger.return_value = {
            "status": "complete",
            "bytes_transferred": 1024,
            "s3_etag": '"abc-1"',
            "webhook_delivered": True,
        }
        yield mock


# ---------------------------------------------------------------------------
# S3 event invocation
# ---------------------------------------------------------------------------


class TestS3EventInvocation:
    def _s3_event(self, bucket: str, key: str) -> dict:
        return {
            "Records": [
                {
                    "s3": {
                        "bucket": {"name": bucket},
                        "object": {"key": key},
                    }
                }
            ]
        }

    def test_success(self, mock_transfer):
        event = self._s3_event("bkt", "transfer-actions/req-1-item-1-transfer_media.json")
        result = handler(event, None)

        assert result["statusCode"] == 200
        assert result["body"]["status"] == "complete"
        assert result["body"]["bytes_transferred"] == 1024
        assert result["body"]["s3_etag"] == '"abc-1"'
        assert result["body"]["webhook_delivered"] is True
        mock_transfer.process_trigger.assert_called_once_with(
            bucket="bkt", key="transfer-actions/req-1-item-1-transfer_media.json"
        )

    def test_terminal_transfer_error_still_returns_200(self, mock_transfer):
        # Terminal source/dest errors are surfaced via the webhook callback's
        # error_code field. The Lambda still completed successfully — webhook
        # was sent — so it should return 200.
        mock_transfer.process_trigger.return_value = {
            "status": "error",
            "error_code": "drive_token_expired",
            "bytes_transferred": 0,
            "webhook_delivered": True,
        }
        event = self._s3_event("bkt", "transfer-actions/x.json")
        result = handler(event, None)

        assert result["statusCode"] == 200
        assert result["body"]["status"] == "error"
        assert result["body"]["error_code"] == "drive_token_expired"

    def test_invalid_key_prefix_returns_400(self, mock_transfer):
        event = self._s3_event("bkt", "actions/oops.json")
        result = handler(event, None)
        assert result["statusCode"] == 400
        mock_transfer.process_trigger.assert_not_called()

    def test_non_json_key_returns_400(self, mock_transfer):
        event = self._s3_event("bkt", "transfer-actions/oops.txt")
        result = handler(event, None)
        assert result["statusCode"] == 400
        mock_transfer.process_trigger.assert_not_called()


# ---------------------------------------------------------------------------
# Direct invocation (for invoke scripts and tests)
# ---------------------------------------------------------------------------


class TestDirectInvocation:
    def test_success(self, mock_transfer):
        event = {"bucket": "bkt", "key": "transfer-actions/x.json"}
        result = handler(event, None)
        assert result["statusCode"] == 200
        mock_transfer.process_trigger.assert_called_once_with(
            bucket="bkt", key="transfer-actions/x.json"
        )

    def test_missing_fields_returns_400(self, mock_transfer):
        result = handler({"foo": "bar"}, None)
        assert result["statusCode"] == 400
        mock_transfer.process_trigger.assert_not_called()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_validation_error_returns_400(self, mock_transfer):
        # ValueError is raised by the module on bad trigger payloads.
        # No webhook gets sent (no valid callback_url).
        mock_transfer.process_trigger.side_effect = ValueError("Trigger missing required fields: ['callback_url']")
        event = {"bucket": "b", "key": "transfer-actions/x.json"}
        result = handler(event, None)
        assert result["statusCode"] == 400
        assert "callback_url" in result["body"]["error"]

    def test_s3_client_error_returns_500(self, mock_transfer):
        mock_transfer.process_trigger.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "no perms"}},
            "GetObject",
        )
        event = {"bucket": "b", "key": "transfer-actions/x.json"}
        result = handler(event, None)
        assert result["statusCode"] == 500
        assert "AccessDenied" in result["body"]["error"]

    def test_unexpected_error_returns_500_for_dlq(self, mock_transfer):
        # Unexpected errors return 500 so Lambda's async invocation
        # destination routes the event to the SQS DLQ.
        mock_transfer.process_trigger.side_effect = RuntimeError("memory blew up")
        event = {"bucket": "b", "key": "transfer-actions/x.json"}
        result = handler(event, None)
        assert result["statusCode"] == 500
        assert "RuntimeError" in result["body"]["error"]


# ---------------------------------------------------------------------------
# Event parsing
# ---------------------------------------------------------------------------


class TestParseEvent:
    def test_s3_event(self):
        bucket, key = _parse_event({
            "Records": [{
                "s3": {
                    "bucket": {"name": "b"},
                    "object": {"key": "transfer-actions/x.json"},
                }
            }]
        })
        assert bucket == "b"
        assert key == "transfer-actions/x.json"

    def test_direct_event(self):
        bucket, key = _parse_event({"bucket": "b", "key": "transfer-actions/x.json"})
        assert bucket == "b"
        assert key == "transfer-actions/x.json"

    def test_missing_keys_raises(self):
        with pytest.raises(KeyError):
            _parse_event({"foo": "bar"})

    def test_wrong_prefix_raises(self):
        with pytest.raises(ValueError, match="does not match"):
            _parse_event({"bucket": "b", "key": "actions/x.json"})

    def test_non_json_suffix_raises(self):
        with pytest.raises(ValueError, match="does not match"):
            _parse_event({"bucket": "b", "key": "transfer-actions/x.txt"})
