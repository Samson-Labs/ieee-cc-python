"""Tests for WebhookSender."""

import json
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from src.webhook.sender import WebhookSender, MAX_RETRIES, BACKOFF_DELAYS, SNS_TOPIC_ENV


@pytest.fixture
def sender():
    sns = MagicMock()
    return WebhookSender(sns_client=sns), sns


URL = "https://drupal.example.com/hook"
SECRET = "test-secret-key"
PAYLOAD = {"item_id": "STD-12345", "status": "success"}
CORRELATION = "[req:STD-12345]"


def _mock_success_response():
    resp = MagicMock()
    resp.status = 200
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestHeaders:
    @patch("src.webhook.sender.urllib.request.urlopen")
    def test_sets_bearer_auth_and_content_type(self, mock_urlopen, sender):
        ws, _ = sender
        mock_urlopen.return_value = _mock_success_response()

        ws.send(URL, SECRET, PAYLOAD, CORRELATION)

        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Content-type") == "application/json"
        assert req.get_header("Authorization") == f"Bearer {SECRET}"

    @patch("src.webhook.sender.urllib.request.urlopen")
    def test_no_hmac_signature_header(self, mock_urlopen, sender):
        ws, _ = sender
        mock_urlopen.return_value = _mock_success_response()

        ws.send(URL, SECRET, PAYLOAD, CORRELATION)

        req = mock_urlopen.call_args[0][0]
        assert req.get_header("X-webhook-signature") is None


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
