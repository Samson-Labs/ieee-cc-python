"""Tests for the WizardTransfer module (CC3-898)."""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock

import pytest
import requests
from botocore.exceptions import ClientError

from src.transfer.wizard_transfer import (
    ERR_DEST_WRITE_FAILED,
    ERR_DRIVE_TOKEN_EXPIRED,
    ERR_INTERNAL,
    ERR_TLS_ERROR,
    ERR_URL_NOT_FOUND,
    ERR_URL_TIMEOUT,
    WizardTransfer,
    _CountingStream,
    _TerminalTransferError,
)


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------

VALID_URL_TRIGGER = {
    "version": 1,
    "source_type": "url",
    "source_ref": "https://example.com/file.mp4",
    "dest_bucket": "dest-bkt",
    "dest_key": "ou/pending/file.mp4",
    "item_id": "item-1",
    "request_id": "req-1",
    "operation": "transfer_media",
    "callback_url": "https://drupal.example/api/iplr/webhook/media-transfer",
    "callback_secret_ref": "iplr/webhook-secret",
}

VALID_DRIVE_TRIGGER = {
    **VALID_URL_TRIGGER,
    "source_type": "google_drive",
    "source_ref": "1AbC_DRIVE_FILE_ID",
    "drive_oauth_token_ref": "iplr/drive-tokens/req-1-item-1",
}


def _make_s3_with_trigger(trigger: dict) -> MagicMock:
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": BytesIO(json.dumps(trigger).encode())}
    s3.head_object.return_value = {"ETag": '"abc123-1"'}

    # Mimic boto3's upload_fileobj draining the stream so the counting
    # wrapper sees actual bytes flow through. Real boto3 reads in chunks
    # via .read(amt) until EOF — we do the same.
    def _drain(Fileobj, **_):
        while True:
            chunk = Fileobj.read(64 * 1024)
            if not chunk:
                break

    s3.upload_fileobj.side_effect = _drain
    return s3


def _make_response(
    status: int = 200,
    body: bytes = b"hello world",
    raw_class=BytesIO,
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.text = body.decode(errors="replace")
    resp.raw = raw_class(body)
    # decode_content must be settable
    resp.raw.decode_content = False
    return resp


def _make_http_session(response: MagicMock) -> MagicMock:
    session = MagicMock()
    session.get.return_value = response
    return session


def _make_secrets(token_value: str = "ya29.fresh-token") -> MagicMock:
    secrets = MagicMock()
    secrets.get_secret_value.return_value = {"SecretString": token_value}
    return secrets


def _make_webhook_sender(returns: bool = True) -> MagicMock:
    ws = MagicMock()
    ws.send.return_value = returns
    return ws


def _build_transfer(
    s3=None,
    secrets=None,
    webhook=None,
    http=None,
    trigger: dict | None = None,
) -> WizardTransfer:
    return WizardTransfer(
        s3_client=s3 or _make_s3_with_trigger(trigger or VALID_URL_TRIGGER),
        secrets_client=secrets or _make_secrets(),
        webhook_sender=webhook or _make_webhook_sender(),
        requests_session=http or _make_http_session(_make_response()),
    )


# ---------------------------------------------------------------------------
# Trigger validation
# ---------------------------------------------------------------------------


class TestTriggerValidation:
    def test_missing_fields_raise_value_error(self):
        bad = {k: v for k, v in VALID_URL_TRIGGER.items() if k != "callback_url"}
        s3 = MagicMock()
        s3.get_object.return_value = {"Body": BytesIO(json.dumps(bad).encode())}
        wt = _build_transfer(s3=s3)

        with pytest.raises(ValueError, match="missing required fields"):
            wt.process_trigger("b", "transfer-actions/x.json")

    def test_unknown_source_type_raises(self):
        bad = {**VALID_URL_TRIGGER, "source_type": "ftp"}
        wt = _build_transfer(trigger=bad)
        with pytest.raises(ValueError, match="Invalid source_type"):
            wt.process_trigger("b", "transfer-actions/x.json")

    def test_drive_without_token_ref_raises(self):
        bad = {**VALID_URL_TRIGGER, "source_type": "google_drive"}
        # No drive_oauth_token_ref
        wt = _build_transfer(trigger=bad)
        with pytest.raises(ValueError, match="drive_oauth_token_ref"):
            wt.process_trigger("b", "transfer-actions/x.json")

    def test_unsupported_version_raises(self):
        bad = {**VALID_URL_TRIGGER, "version": 2}
        wt = _build_transfer(trigger=bad)
        with pytest.raises(ValueError, match="Unsupported trigger version"):
            wt.process_trigger("b", "transfer-actions/x.json")

    def test_invalid_operation_raises(self):
        bad = {**VALID_URL_TRIGGER, "operation": "delete_everything"}
        wt = _build_transfer(trigger=bad)
        with pytest.raises(ValueError, match="Invalid operation"):
            wt.process_trigger("b", "transfer-actions/x.json")

    def test_malformed_json_trigger_raises(self):
        s3 = MagicMock()
        s3.get_object.return_value = {"Body": BytesIO(b"not-json{")}
        wt = _build_transfer(s3=s3)
        with pytest.raises(ValueError, match="not valid JSON"):
            wt.process_trigger("b", "transfer-actions/x.json")

    def test_s3_get_failure_raises(self):
        s3 = MagicMock()
        s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey", "Message": "Not found"}},
            "GetObject",
        )
        wt = _build_transfer(s3=s3)
        with pytest.raises(ValueError, match="Failed to read trigger"):
            wt.process_trigger("b", "transfer-actions/x.json")


# ---------------------------------------------------------------------------
# URL source path
# ---------------------------------------------------------------------------


class TestUrlSource:
    def test_success_streams_to_s3_and_sends_webhook(self):
        s3 = _make_s3_with_trigger(VALID_URL_TRIGGER)
        webhook = _make_webhook_sender(returns=True)
        body = b"x" * (5 * 1024 * 1024)
        http = _make_http_session(_make_response(status=200, body=body))

        wt = _build_transfer(s3=s3, webhook=webhook, http=http)
        result = wt.process_trigger("trigger-bkt", "transfer-actions/x.json")

        assert result["status"] == "complete"
        assert result["bytes_transferred"] == len(body)
        assert result["s3_etag"] == '"abc123-1"'
        assert result["webhook_delivered"] is True

        # upload_fileobj called with our counting stream + correct dest
        s3.upload_fileobj.assert_called_once()
        kwargs = s3.upload_fileobj.call_args.kwargs
        assert kwargs["Bucket"] == "dest-bkt"
        assert kwargs["Key"] == "ou/pending/file.mp4"

        # head_object called for the etag
        s3.head_object.assert_called_once_with(
            Bucket="dest-bkt", Key="ou/pending/file.mp4"
        )

        # webhook called with status=complete and bytes/etag
        assert webhook.send.call_count == 1
        send_kwargs = webhook.send.call_args.kwargs
        assert send_kwargs["url"] == VALID_URL_TRIGGER["callback_url"]
        payload = send_kwargs["payload"]
        assert payload["status"] == "complete"
        assert payload["item_id"] == "item-1"
        assert payload["request_id"] == "req-1"
        assert payload["bytes_transferred"] == len(body)
        assert payload["s3_etag"] == '"abc123-1"'
        assert "error_code" not in payload

        # Trigger deleted on success
        s3.delete_object.assert_called_once_with(
            Bucket="trigger-bkt", Key="transfer-actions/x.json"
        )

    def test_404_returns_url_not_found_and_does_not_upload(self):
        s3 = _make_s3_with_trigger(VALID_URL_TRIGGER)
        webhook = _make_webhook_sender()
        http = _make_http_session(_make_response(status=404, body=b"nope"))

        wt = _build_transfer(s3=s3, webhook=webhook, http=http)
        result = wt.process_trigger("b", "transfer-actions/x.json")

        assert result["status"] == "error"
        assert result["error_code"] == ERR_URL_NOT_FOUND
        s3.upload_fileobj.assert_not_called()

        payload = webhook.send.call_args.kwargs["payload"]
        assert payload["status"] == "error"
        assert payload["error_code"] == ERR_URL_NOT_FOUND
        assert "error_message" in payload

        # Failed transfer => trigger NOT deleted
        s3.delete_object.assert_not_called()

    def test_connect_timeout_returns_url_timeout(self):
        s3 = _make_s3_with_trigger(VALID_URL_TRIGGER)
        webhook = _make_webhook_sender()
        http = MagicMock()
        http.get.side_effect = requests.exceptions.Timeout("connect timeout")

        wt = _build_transfer(s3=s3, webhook=webhook, http=http)
        result = wt.process_trigger("b", "transfer-actions/x.json")

        assert result["error_code"] == ERR_URL_TIMEOUT
        s3.upload_fileobj.assert_not_called()
        s3.delete_object.assert_not_called()

    def test_ssl_error_returns_tls_error(self):
        s3 = _make_s3_with_trigger(VALID_URL_TRIGGER)
        webhook = _make_webhook_sender()
        http = MagicMock()
        http.get.side_effect = requests.exceptions.SSLError("bad cert")

        wt = _build_transfer(s3=s3, webhook=webhook, http=http)
        result = wt.process_trigger("b", "transfer-actions/x.json")

        assert result["error_code"] == ERR_TLS_ERROR
        payload = webhook.send.call_args.kwargs["payload"]
        assert payload["error_code"] == ERR_TLS_ERROR

    def test_500_from_source_returns_internal(self):
        http = _make_http_session(_make_response(status=500, body=b"server boom"))
        wt = _build_transfer(http=http)
        result = wt.process_trigger("b", "transfer-actions/x.json")
        assert result["error_code"] == ERR_INTERNAL

    def test_unexpected_request_exception_returns_internal(self):
        http = MagicMock()
        http.get.side_effect = requests.exceptions.RequestException("weird")
        wt = _build_transfer(http=http)
        result = wt.process_trigger("b", "transfer-actions/x.json")
        assert result["error_code"] == ERR_INTERNAL

    def test_s3_upload_failure_returns_dest_write_failed(self):
        s3 = _make_s3_with_trigger(VALID_URL_TRIGGER)
        s3.upload_fileobj.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "no write"}},
            "UploadPart",
        )
        webhook = _make_webhook_sender()
        wt = _build_transfer(s3=s3, webhook=webhook)
        result = wt.process_trigger("b", "transfer-actions/x.json")

        assert result["error_code"] == ERR_DEST_WRITE_FAILED
        assert webhook.send.call_args.kwargs["payload"]["error_code"] == ERR_DEST_WRITE_FAILED
        s3.delete_object.assert_not_called()

    def test_head_object_failure_after_upload_returns_dest_write_failed(self):
        s3 = _make_s3_with_trigger(VALID_URL_TRIGGER)
        s3.head_object.side_effect = ClientError(
            {"Error": {"Code": "NotFound", "Message": "?"}},
            "HeadObject",
        )
        wt = _build_transfer(s3=s3)
        result = wt.process_trigger("b", "transfer-actions/x.json")
        assert result["error_code"] == ERR_DEST_WRITE_FAILED


# ---------------------------------------------------------------------------
# Google Drive source path
# ---------------------------------------------------------------------------


class TestDriveSource:
    def test_drive_success_uses_bearer_token_from_secrets_manager(self):
        s3 = _make_s3_with_trigger(VALID_DRIVE_TRIGGER)
        secrets = _make_secrets("ya29.special-token")
        body = b"drive-bytes" * 100
        http = _make_http_session(_make_response(status=200, body=body))

        wt = _build_transfer(s3=s3, secrets=secrets, http=http)
        result = wt.process_trigger("b", "transfer-actions/x.json")

        assert result["status"] == "complete"
        # Drive token fetched from Secrets Manager
        secrets.get_secret_value.assert_any_call(
            SecretId="iplr/drive-tokens/req-1-item-1"
        )
        # Drive media URL hit with Bearer header
        call = http.get.call_args
        assert "/drive/v3/files/1AbC_DRIVE_FILE_ID" in call.args[0]
        assert call.kwargs["headers"]["Authorization"] == "Bearer ya29.special-token"
        assert call.kwargs["stream"] is True

    def test_drive_token_secret_supports_json_blob(self):
        s3 = _make_s3_with_trigger(VALID_DRIVE_TRIGGER)
        secrets = MagicMock()
        secrets.get_secret_value.return_value = {
            "SecretString": json.dumps({"access_token": "ya29.from-json", "expires_in": 3600}),
        }
        http = _make_http_session(_make_response())

        wt = _build_transfer(s3=s3, secrets=secrets, http=http)
        wt.process_trigger("b", "transfer-actions/x.json")

        assert http.get.call_args.kwargs["headers"]["Authorization"] == "Bearer ya29.from-json"

    def test_drive_401_returns_drive_token_expired(self):
        s3 = _make_s3_with_trigger(VALID_DRIVE_TRIGGER)
        webhook = _make_webhook_sender()
        http = _make_http_session(_make_response(status=401, body=b"unauthorized"))

        wt = _build_transfer(s3=s3, webhook=webhook, http=http)
        result = wt.process_trigger("b", "transfer-actions/x.json")

        assert result["error_code"] == ERR_DRIVE_TOKEN_EXPIRED
        payload = webhook.send.call_args.kwargs["payload"]
        assert payload["error_code"] == ERR_DRIVE_TOKEN_EXPIRED
        s3.upload_fileobj.assert_not_called()
        s3.delete_object.assert_not_called()

    def test_drive_404_returns_url_not_found(self):
        s3 = _make_s3_with_trigger(VALID_DRIVE_TRIGGER)
        http = _make_http_session(_make_response(status=404, body=b"missing"))
        wt = _build_transfer(s3=s3, http=http)
        result = wt.process_trigger("b", "transfer-actions/x.json")
        assert result["error_code"] == ERR_URL_NOT_FOUND

    def test_secrets_manager_failure_returns_drive_token_expired(self):
        s3 = _make_s3_with_trigger(VALID_DRIVE_TRIGGER)
        secrets = MagicMock()
        secrets.get_secret_value.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}},
            "GetSecretValue",
        )
        wt = _build_transfer(s3=s3, secrets=secrets)
        result = wt.process_trigger("b", "transfer-actions/x.json")
        assert result["error_code"] == ERR_DRIVE_TOKEN_EXPIRED


# ---------------------------------------------------------------------------
# Webhook secret resolution
# ---------------------------------------------------------------------------


class TestWebhookSecretResolution:
    def test_uses_secrets_manager_callback_secret_when_present(self):
        s3 = _make_s3_with_trigger(VALID_URL_TRIGGER)
        # Trigger reads the trigger JSON, then resolves the callback secret —
        # both via secrets_client.get_secret_value. Distinguish via SecretId.
        secrets = MagicMock()

        def fake_get(SecretId, **_):
            if SecretId == "iplr/webhook-secret":
                return {"SecretString": "secret-from-sm"}
            return {"SecretString": "x"}

        secrets.get_secret_value.side_effect = fake_get
        webhook = _make_webhook_sender()
        http = _make_http_session(_make_response())

        wt = _build_transfer(s3=s3, secrets=secrets, webhook=webhook, http=http)
        wt.process_trigger("b", "transfer-actions/x.json")

        assert webhook.send.call_args.kwargs["secret"] == "secret-from-sm"

    def test_falls_back_to_env_var_when_secrets_manager_fails(self, monkeypatch):
        monkeypatch.setenv("DRUPAL_WEBHOOK_SECRET", "env-fallback")
        s3 = _make_s3_with_trigger(VALID_URL_TRIGGER)
        secrets = MagicMock()
        secrets.get_secret_value.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "?"}},
            "GetSecretValue",
        )
        webhook = _make_webhook_sender()
        wt = _build_transfer(s3=s3, secrets=secrets, webhook=webhook)

        wt.process_trigger("b", "transfer-actions/x.json")

        assert webhook.send.call_args.kwargs["secret"] == "env-fallback"


# ---------------------------------------------------------------------------
# Counting stream
# ---------------------------------------------------------------------------


class TestCountingStream:
    def test_tracks_bytes_across_chunked_reads(self):
        src = BytesIO(b"a" * 100 + b"b" * 50)
        cs = _CountingStream(src)
        assert cs.read(40) == b"a" * 40
        assert cs.bytes_read == 40
        assert cs.read(60) == b"a" * 60
        assert cs.bytes_read == 100
        rest = cs.read()
        assert rest == b"b" * 50
        assert cs.bytes_read == 150

    def test_read_with_no_amt_passes_through(self):
        src = BytesIO(b"hello")
        cs = _CountingStream(src)
        assert cs.read() == b"hello"
        assert cs.bytes_read == 5


# ---------------------------------------------------------------------------
# Webhook delivery failure does NOT break Lambda
# ---------------------------------------------------------------------------


class TestWebhookDeliveryFailure:
    def test_webhook_returning_false_still_completes_transfer(self):
        s3 = _make_s3_with_trigger(VALID_URL_TRIGGER)
        webhook = _make_webhook_sender(returns=False)
        http = _make_http_session(_make_response(body=b"ok"))

        wt = _build_transfer(s3=s3, webhook=webhook, http=http)
        result = wt.process_trigger("b", "transfer-actions/x.json")

        assert result["status"] == "complete"
        assert result["webhook_delivered"] is False
        # Trigger still deleted — the transfer itself succeeded; webhook
        # failure is tracked separately via SNS in WebhookSender.
        s3.delete_object.assert_called_once()
