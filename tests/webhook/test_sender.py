"""Tests for WebhookSender."""

import hashlib
import hmac
import json
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch, call

import pytest

from src.webhook.sender import (
    WebhookSender,
    MAX_RETRIES,
    BACKOFF_DELAYS,
    MAX_RESPONSE_BYTES,
    SNS_TOPIC_ENV,
)


@pytest.fixture
def sender():
    sns = MagicMock()
    return WebhookSender(sns_client=sns), sns


URL = "https://drupal.example.com/hook"
SECRET = "test-secret-key"
PAYLOAD = {"item_id": "STD-12345", "status": "completed"}
CORRELATION = "[req:STD-12345]"


def _mock_success_response(body: bytes | None = None):
    resp = MagicMock()
    resp.status = 200
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    resp.read = MagicMock(return_value=body if body is not None else b"")
    return resp


class TestSign:
    def test_sign_produces_correct_hmac(self):
        body = json.dumps(PAYLOAD).encode()
        expected = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        assert WebhookSender._sign(SECRET, body) == expected

    def test_hmac_matches_drupal_hash_equals(self):
        """Verify hex digest format is compatible with PHP hash_equals(hash_hmac('sha256', $body, $secret))."""
        body = b'{"test":"data"}'
        result = WebhookSender._sign("my-secret", body)
        # PHP hash_hmac('sha256', ...) returns lowercase hex — verify same format
        assert all(c in "0123456789abcdef" for c in result)
        assert len(result) == 64  # SHA-256 hex digest is 64 chars


class TestHeaders:
    @patch("src.webhook.sender.urllib.request.urlopen")
    def test_sets_signature_and_content_type_headers(self, mock_urlopen, sender):
        ws, _ = sender
        mock_urlopen.return_value = _mock_success_response()

        ws.send(URL, SECRET, PAYLOAD, CORRELATION)

        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Content-type") == "application/json"
        assert req.get_header("X-webhook-signature") is not None

        # Verify the signature matches the body
        body_bytes = json.dumps(PAYLOAD).encode()
        expected_sig = WebhookSender._sign(SECRET, body_bytes)
        assert req.get_header("X-webhook-signature") == expected_sig


class TestSuccessPath:
    @patch("src.webhook.sender.urllib.request.urlopen")
    def test_success_on_first_attempt(self, mock_urlopen, sender):
        ws, _ = sender
        mock_urlopen.return_value = _mock_success_response()

        result = ws.send(URL, SECRET, PAYLOAD, CORRELATION)

        assert result is True
        assert mock_urlopen.call_count == 1


class TestRetry:
    @patch("src.webhook.sender.time.sleep")
    @patch("src.webhook.sender.urllib.request.urlopen")
    def test_retries_on_5xx(self, mock_urlopen, mock_sleep, sender):
        ws, _ = sender
        error_500 = urllib.error.HTTPError(
            URL, 500, "Internal Server Error", {}, BytesIO(b"server error")
        )
        mock_urlopen.side_effect = [error_500, _mock_success_response()]

        result = ws.send(URL, SECRET, PAYLOAD, CORRELATION)

        assert result is True
        assert mock_urlopen.call_count == 2
        mock_sleep.assert_called_once_with(BACKOFF_DELAYS[0])

    @patch("src.webhook.sender.time.sleep")
    @patch("src.webhook.sender.urllib.request.urlopen")
    def test_retries_on_connection_error(self, mock_urlopen, mock_sleep, sender):
        ws, _ = sender
        mock_urlopen.side_effect = [
            urllib.error.URLError("connection refused"),
            _mock_success_response(),
        ]

        result = ws.send(URL, SECRET, PAYLOAD, CORRELATION)

        assert result is True
        assert mock_urlopen.call_count == 2

    @patch("src.webhook.sender.urllib.request.urlopen")
    def test_no_retry_on_400(self, mock_urlopen, sender):
        ws, _ = sender
        error_400 = urllib.error.HTTPError(
            URL, 400, "Bad Request", {}, BytesIO(b"bad request")
        )
        mock_urlopen.side_effect = error_400

        result = ws.send(URL, SECRET, PAYLOAD, CORRELATION)

        assert result is False
        assert mock_urlopen.call_count == 1

    @patch("src.webhook.sender.urllib.request.urlopen")
    def test_no_retry_on_401(self, mock_urlopen, sender):
        ws, _ = sender
        error_401 = urllib.error.HTTPError(
            URL, 401, "Unauthorized", {}, BytesIO(b"unauthorized")
        )
        mock_urlopen.side_effect = error_401

        result = ws.send(URL, SECRET, PAYLOAD, CORRELATION)

        assert result is False
        assert mock_urlopen.call_count == 1


class TestSNSAlert:
    @patch("src.webhook.sender.time.sleep")
    @patch("src.webhook.sender.urllib.request.urlopen")
    def test_sns_alert_after_exhausted_retries(self, mock_urlopen, mock_sleep, sender):
        ws, sns = sender
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")

        with patch.dict("os.environ", {SNS_TOPIC_ENV: "arn:aws:sns:us-east-1:123:webhook-failures"}):
            result = ws.send(URL, SECRET, PAYLOAD, CORRELATION)

        assert result is False
        assert mock_urlopen.call_count == MAX_RETRIES
        sns.publish.assert_called_once()

        publish_call = sns.publish.call_args
        assert publish_call[1]["TopicArn"] == "arn:aws:sns:us-east-1:123:webhook-failures"
        assert publish_call[1]["Subject"] == "Webhook delivery failure"

        message = json.loads(publish_call[1]["Message"])
        assert message["url"] == URL
        assert message["correlation"] == CORRELATION

    @patch("src.webhook.sender.urllib.request.urlopen")
    def test_sns_alert_on_4xx(self, mock_urlopen, sender):
        ws, sns = sender
        error_400 = urllib.error.HTTPError(
            URL, 400, "Bad Request", {}, BytesIO(b"bad")
        )
        mock_urlopen.side_effect = error_400

        with patch.dict("os.environ", {SNS_TOPIC_ENV: "arn:aws:sns:us-east-1:123:topic"}):
            ws.send(URL, SECRET, PAYLOAD, CORRELATION)

        sns.publish.assert_called_once()


class TestResponseValidator:
    """CC3-931: optional validator runs against the parsed 2xx body and can
    fail an otherwise-successful HTTP call when the contract is violated.
    """

    @patch("src.webhook.sender.urllib.request.urlopen")
    def test_validator_pass_returns_true(self, mock_urlopen, sender):
        ws, _ = sender
        mock_urlopen.return_value = _mock_success_response(
            b'{"success":true,"message":"Webhook processed successfully."}'
        )

        result = ws.send(
            URL, SECRET, PAYLOAD, CORRELATION,
            response_validator=lambda body: (True, ""),
        )

        assert result is True

    @patch("src.webhook.sender.urllib.request.urlopen")
    def test_validator_fail_returns_false_and_alerts(self, mock_urlopen, sender):
        ws, sns = sender
        mock_urlopen.return_value = _mock_success_response(
            b'{"success":true,"ignored":true,"message":"Webhook ignored: item is no longer awaiting AI processing."}'
        )

        with patch.dict("os.environ", {SNS_TOPIC_ENV: "arn:aws:sns:us-east-1:123:topic"}):
            result = ws.send(
                URL, SECRET, PAYLOAD, CORRELATION,
                response_validator=lambda body: (False, "webhook ignored: stale callback"),
            )

        assert result is False
        sns.publish.assert_called_once()
        message = json.loads(sns.publish.call_args[1]["Message"])
        assert "contract validation failed" in message["error"]
        assert "ignored" in message["error"]

    @patch("src.webhook.sender.urllib.request.urlopen")
    def test_validator_receives_parsed_body(self, mock_urlopen, sender):
        ws, _ = sender
        mock_urlopen.return_value = _mock_success_response(
            b'{"success":true,"message":"Webhook processed successfully."}'
        )

        seen = {}

        def _capture(body):
            seen["body"] = body
            return True, ""

        ws.send(URL, SECRET, PAYLOAD, CORRELATION, response_validator=_capture)

        assert seen["body"] == {
            "success": True,
            "message": "Webhook processed successfully.",
        }

    @patch("src.webhook.sender.urllib.request.urlopen")
    def test_validator_receives_none_for_unparseable_body(self, mock_urlopen, sender):
        ws, _ = sender
        mock_urlopen.return_value = _mock_success_response(b"<html>not json</html>")

        seen = {}

        def _capture(body):
            seen["body"] = body
            return True, ""

        ws.send(URL, SECRET, PAYLOAD, CORRELATION, response_validator=_capture)

        assert seen["body"] is None

    @patch("src.webhook.sender.urllib.request.urlopen")
    def test_response_read_is_capped_at_max_bytes(self, mock_urlopen, sender):
        """Defense-in-depth: a runaway peer response must not exhaust Lambda memory."""
        ws, _ = sender
        resp = _mock_success_response()
        resp.read = MagicMock(return_value=b'{"success":true,"message":"ok"}')
        mock_urlopen.return_value = resp

        ws.send(
            URL, SECRET, PAYLOAD, CORRELATION,
            response_validator=lambda body: (True, ""),
        )

        resp.read.assert_called_once_with(MAX_RESPONSE_BYTES)

    @patch("src.webhook.sender.urllib.request.urlopen")
    def test_no_validator_does_not_read_body(self, mock_urlopen, sender):
        """Existing callers (no validator) should not pay the read cost or
        be affected by response-parse failures.
        """
        ws, _ = sender
        resp = _mock_success_response()
        # Sentinel: if anything calls .read() this will break the test.
        resp.read = MagicMock(side_effect=AssertionError("read() should not be called without a validator"))
        mock_urlopen.return_value = resp

        result = ws.send(URL, SECRET, PAYLOAD, CORRELATION)

        assert result is True


class TestLogging:
    @patch("src.webhook.sender.time.sleep")
    @patch("src.webhook.sender.urllib.request.urlopen")
    def test_logs_non_200_response(self, mock_urlopen, mock_sleep, sender):
        ws, _ = sender
        error_503 = urllib.error.HTTPError(
            URL, 503, "Service Unavailable", {}, BytesIO(b"unavailable")
        )
        mock_urlopen.side_effect = error_503

        with patch("src.webhook.sender.logger") as mock_logger:
            with patch.dict("os.environ", {SNS_TOPIC_ENV: ""}):
                ws.send(URL, SECRET, PAYLOAD, CORRELATION)

            error_calls = [c for c in mock_logger.error.call_args_list
                           if "503" in str(c)]
            assert len(error_calls) > 0
