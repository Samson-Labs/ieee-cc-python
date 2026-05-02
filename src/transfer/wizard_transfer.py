"""Wizard async transfer module (CC3-898 / CC3-896 companion).

Reads a per-item trigger JSON from s3://{bucket}/transfer-actions/*.json,
streams the source bytes (Google Drive or URL) into S3 via multipart upload,
and POSTs an HMAC-SHA256 signed callback to Drupal.

Contracts:
    docs/contracts/transfer-trigger-v1.json   (input)
    docs/contracts/transfer-webhook-v1.json   (output payload)
"""

from __future__ import annotations

import json
import os
from typing import IO, Literal, NotRequired, TypedDict

import boto3
import requests
from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ClientError

from src.common.logging import get_json_logger
from src.webhook.sender import WebhookSender

logger = get_json_logger(__name__)

DRIVE_API_URL = "https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"

# Multipart upload tuning. boto3 streams response.raw through s3 in 64 MB
# parts with up to 10 parallel uploads — plenty of throughput for 10 GB
# inside the 15-minute Lambda cap, while staying well under Lambda's
# memory budget (each part is held in memory briefly).
MULTIPART_CHUNKSIZE = 64 * 1024 * 1024
MULTIPART_THRESHOLD = 64 * 1024 * 1024
MAX_CONCURRENCY = 10

# Connect / read timeouts for the source HTTP fetch. The read timeout is
# per-chunk, not for the whole transfer.
HTTP_CONNECT_TIMEOUT = 10
HTTP_READ_TIMEOUT = 60

REQUIRED_FIELDS = (
    "version",
    "source_type",
    "source_ref",
    "dest_bucket",
    "dest_key",
    "item_id",
    "request_id",
    "operation",
    "callback_url",
    "callback_secret_ref",
)
VALID_SOURCE_TYPES = frozenset({"google_drive", "url"})
VALID_OPERATIONS = frozenset({"transfer_media", "transfer_image"})

# error_code values per docs/contracts/transfer-webhook-v1.json
ERR_DRIVE_TOKEN_EXPIRED = "drive_token_expired"
ERR_URL_NOT_FOUND = "url_not_found"
ERR_URL_TIMEOUT = "url_timeout"
ERR_TLS_ERROR = "tls_error"
ERR_DEST_WRITE_FAILED = "dest_write_failed"
ERR_INTERNAL = "internal"


class TransferTrigger(TypedDict):
    version: int
    source_type: Literal["google_drive", "url"]
    source_ref: str
    drive_oauth_token_ref: NotRequired[str]
    dest_bucket: str
    dest_key: str
    item_id: str
    request_id: str
    operation: Literal["transfer_media", "transfer_image"]
    callback_url: str
    callback_secret_ref: str


class TransferResult(TypedDict):
    status: Literal["complete", "error"]
    error_code: NotRequired[str]
    bytes_transferred: int
    s3_etag: NotRequired[str]
    webhook_delivered: bool


class _CountingStream:
    """Wraps a file-like to count bytes read.

    boto3.s3.upload_fileobj reads chunks via .read(amt); we intercept to
    track the total for the webhook callback's bytes_transferred field.
    """

    def __init__(self, wrapped: IO[bytes]):
        self._wrapped = wrapped
        self.bytes_read = 0

    def read(self, amt: int | None = None) -> bytes:
        chunk = self._wrapped.read(amt) if amt is not None else self._wrapped.read()
        self.bytes_read += len(chunk)
        return chunk


class WizardTransfer:
    """Streams Drive / URL bytes into S3 and POSTs a signed webhook callback."""

    def __init__(
        self,
        s3_client=None,
        secrets_client=None,
        webhook_sender: WebhookSender | None = None,
        requests_session: requests.Session | None = None,
    ):
        self._s3 = s3_client or boto3.client("s3")
        self._secrets = secrets_client or boto3.client("secretsmanager")
        self._webhook = webhook_sender or WebhookSender()
        self._http = requests_session or requests.Session()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def process_trigger(self, bucket: str, key: str) -> TransferResult:
        """Process a transfer-actions/*.json trigger end-to-end.

        Always sends a webhook callback to Drupal on terminal completion
        (success or terminal failure). Returns a TransferResult summarising
        the outcome.
        """
        trigger = self._read_trigger(bucket, key)
        correlation = f"[{trigger['request_id']}:{trigger['item_id']}]"
        logger.info(
            "%s Transfer start: source_type=%s dest=s3://%s/%s",
            correlation,
            trigger["source_type"],
            trigger["dest_bucket"],
            trigger["dest_key"],
        )

        try:
            bytes_transferred, etag = self._stream_to_s3(trigger, correlation)
        except _TerminalTransferError as exc:
            self._send_callback(
                trigger,
                correlation,
                status="error",
                error_code=exc.error_code,
                error_message=exc.message,
            )
            return {
                "status": "error",
                "error_code": exc.error_code,
                "bytes_transferred": exc.bytes_transferred,
                "webhook_delivered": True,
            }

        webhook_delivered = self._send_callback(
            trigger,
            correlation,
            status="complete",
            bytes_transferred=bytes_transferred,
            s3_etag=etag,
        )

        # Only delete the trigger on successful transfer. Failed triggers
        # remain in S3 for operator inspection / re-submission.
        self._delete_trigger(bucket, key, correlation)

        return {
            "status": "complete",
            "bytes_transferred": bytes_transferred,
            "s3_etag": etag,
            "webhook_delivered": webhook_delivered,
        }

    # ------------------------------------------------------------------
    # Trigger I/O
    # ------------------------------------------------------------------

    def _read_trigger(self, bucket: str, key: str) -> TransferTrigger:
        try:
            obj = self._s3.get_object(Bucket=bucket, Key=key)
            raw = obj["Body"].read()
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            raise ValueError(f"Failed to read trigger s3://{bucket}/{key}: {code}") from exc

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Trigger {key} is not valid JSON: {exc}") from exc

        self._validate_trigger(payload)
        return payload  # type: ignore[return-value]

    @staticmethod
    def _validate_trigger(payload: dict) -> None:
        if not isinstance(payload, dict):
            raise ValueError("Trigger payload must be a JSON object")

        missing = [f for f in REQUIRED_FIELDS if f not in payload]
        if missing:
            raise ValueError(f"Trigger missing required fields: {sorted(missing)}")

        if payload.get("version") != 1:
            raise ValueError(f"Unsupported trigger version: {payload.get('version')}")

        if payload["source_type"] not in VALID_SOURCE_TYPES:
            raise ValueError(f"Invalid source_type: {payload['source_type']}")

        if payload["operation"] not in VALID_OPERATIONS:
            raise ValueError(f"Invalid operation: {payload['operation']}")

        if payload["source_type"] == "google_drive" and not payload.get("drive_oauth_token_ref"):
            raise ValueError("drive_oauth_token_ref is required when source_type=google_drive")

    def _delete_trigger(self, bucket: str, key: str, correlation: str) -> None:
        try:
            self._s3.delete_object(Bucket=bucket, Key=key)
            logger.info("%s Deleted trigger s3://%s/%s", correlation, bucket, key)
        except ClientError as exc:
            # Non-fatal: the transfer succeeded and the webhook fired. Worst
            # case the trigger is reprocessed (idempotent — same dest key,
            # same secret, etc.).
            logger.warning(
                "%s Failed to delete trigger s3://%s/%s: %s",
                correlation, bucket, key, exc,
            )

    # ------------------------------------------------------------------
    # Source -> S3 streaming
    # ------------------------------------------------------------------

    def _stream_to_s3(
        self, trigger: TransferTrigger, correlation: str
    ) -> tuple[int, str]:
        """Stream the source into S3. Returns (bytes_transferred, etag).

        Raises _TerminalTransferError with a contract error_code on any
        fatal failure. Closes the source Response on every exit path so
        the underlying urllib3 connection is returned to the pool — Lambda
        warm-invocation reuse depends on this.
        """
        response = self._open_source(trigger, correlation)
        try:
            counter = _CountingStream(response.raw)

            config = TransferConfig(
                multipart_threshold=MULTIPART_THRESHOLD,
                multipart_chunksize=MULTIPART_CHUNKSIZE,
                max_concurrency=MAX_CONCURRENCY,
                use_threads=True,
            )

            try:
                self._s3.upload_fileobj(
                    Fileobj=counter,
                    Bucket=trigger["dest_bucket"],
                    Key=trigger["dest_key"],
                    Config=config,
                )
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "Unknown")
                raise _TerminalTransferError(
                    ERR_DEST_WRITE_FAILED,
                    f"S3 upload failed ({code}): {exc}",
                    bytes_transferred=counter.bytes_read,
                ) from exc
            except (requests.exceptions.Timeout, requests.exceptions.ReadTimeout) as exc:
                raise _TerminalTransferError(
                    ERR_URL_TIMEOUT,
                    f"Source read timed out mid-transfer: {exc}",
                    bytes_transferred=counter.bytes_read,
                ) from exc
            except requests.exceptions.SSLError as exc:
                raise _TerminalTransferError(
                    ERR_TLS_ERROR,
                    f"TLS error mid-transfer: {exc}",
                    bytes_transferred=counter.bytes_read,
                ) from exc
            except Exception as exc:  # noqa: BLE001 — last-resort mapper to error_code
                raise _TerminalTransferError(
                    ERR_INTERNAL,
                    f"Unexpected error during upload: {type(exc).__name__}: {exc}",
                    bytes_transferred=counter.bytes_read,
                ) from exc

            # Read back the ETag. head_object is cheap and gives us the
            # multipart ETag (which is NOT a raw MD5 — Drupal treats as opaque).
            try:
                head = self._s3.head_object(
                    Bucket=trigger["dest_bucket"], Key=trigger["dest_key"]
                )
                etag = head.get("ETag", "")
            except ClientError as exc:
                raise _TerminalTransferError(
                    ERR_DEST_WRITE_FAILED,
                    f"head_object after upload failed: {exc}",
                    bytes_transferred=counter.bytes_read,
                ) from exc

            logger.info(
                "%s Transfer complete: bytes=%d etag=%s",
                correlation, counter.bytes_read, etag,
            )
            return counter.bytes_read, etag
        finally:
            response.close()

    def _open_source(
        self, trigger: TransferTrigger, correlation: str
    ) -> requests.Response:
        """Open a streaming GET against the configured source.

        Returns the live ``requests.Response``; the caller is responsible
        for closing it (so the underlying connection is returned to the
        pool — important for Lambda warm-invocation reuse). For Drive,
        fetches the OAuth token from Secrets Manager and uses a Bearer
        header. Non-2xx responses are closed here and re-raised as
        _TerminalTransferError with the appropriate contract error_code.
        """
        source_type = trigger["source_type"]

        if source_type == "google_drive":
            token = self._fetch_drive_token(
                trigger["drive_oauth_token_ref"], correlation,
            )
            url = DRIVE_API_URL.format(file_id=trigger["source_ref"])
            headers = {"Authorization": f"Bearer {token}"}
        else:
            url = trigger["source_ref"]
            headers = {}

        try:
            response = self._http.get(
                url,
                headers=headers,
                stream=True,
                timeout=(HTTP_CONNECT_TIMEOUT, HTTP_READ_TIMEOUT),
                allow_redirects=True,
            )
        except requests.exceptions.SSLError as exc:
            raise _TerminalTransferError(
                ERR_TLS_ERROR, f"TLS handshake failed: {exc}",
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise _TerminalTransferError(
                ERR_URL_TIMEOUT, f"Connect/read timeout: {exc}",
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise _TerminalTransferError(
                ERR_INTERNAL, f"HTTP client error: {type(exc).__name__}: {exc}",
            ) from exc

        if response.status_code == 401 and source_type == "google_drive":
            response.close()
            raise _TerminalTransferError(
                ERR_DRIVE_TOKEN_EXPIRED,
                "Drive returned 401 Unauthorized; access token has expired or been revoked.",
            )
        if response.status_code == 404:
            response.close()
            raise _TerminalTransferError(
                ERR_URL_NOT_FOUND,
                f"Source returned 404 Not Found: {url}",
            )
        if response.status_code >= 400:
            # Read directly from the raw socket so a misconfigured source
            # returning a multi-MB error page can't OOM the Lambda. 200
            # bytes is plenty for the error_message snippet.
            try:
                snippet_bytes = response.raw.read(200)
            except Exception:  # noqa: BLE001 — best-effort error context
                snippet_bytes = b""
            body_snippet = snippet_bytes.decode(errors="replace")
            response.close()
            raise _TerminalTransferError(
                ERR_INTERNAL,
                f"Source returned HTTP {response.status_code}: {body_snippet}",
            )

        # Ensure transparent decompression so bytes_transferred reflects
        # decoded content (matches what S3 actually stores).
        response.raw.decode_content = True
        return response

    def _fetch_drive_token(self, secret_id: str, correlation: str) -> str:
        try:
            resp = self._secrets.get_secret_value(SecretId=secret_id)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "Unknown")
            # ResourceNotFoundException / AccessDeniedException both
            # indicate the token can't be retrieved — Drupal must mint
            # a fresh one.
            raise _TerminalTransferError(
                ERR_DRIVE_TOKEN_EXPIRED,
                f"Failed to fetch Drive token from {secret_id}: {code}",
            ) from exc

        # Secret may be stored as plain string or as a JSON blob with an
        # access_token key. Support both — Drupal currently writes plain.
        secret_string = resp.get("SecretString", "")
        if not secret_string:
            raise _TerminalTransferError(
                ERR_DRIVE_TOKEN_EXPIRED,
                f"Drive token secret {secret_id} has no SecretString",
            )

        try:
            parsed = json.loads(secret_string)
            if isinstance(parsed, dict) and "access_token" in parsed:
                return str(parsed["access_token"])
        except json.JSONDecodeError:
            pass
        return secret_string

    # ------------------------------------------------------------------
    # Webhook callback
    # ------------------------------------------------------------------

    def _send_callback(
        self,
        trigger: TransferTrigger,
        correlation: str,
        *,
        status: Literal["complete", "error"],
        error_code: str | None = None,
        error_message: str | None = None,
        bytes_transferred: int | None = None,
        s3_etag: str | None = None,
    ) -> bool:
        """POST the transfer-webhook-v1 payload to Drupal's callback_url.

        Returns True if the webhook was delivered (2xx), False otherwise.
        """
        payload: dict = {
            "item_id": trigger["item_id"],
            "request_id": trigger["request_id"],
            "status": status,
        }
        if status == "error":
            payload["error_code"] = error_code or ERR_INTERNAL
            payload["error_message"] = error_message or ""
        else:
            payload["bytes_transferred"] = bytes_transferred or 0
            payload["s3_etag"] = s3_etag or ""

        secret = self._resolve_callback_secret(trigger["callback_secret_ref"], correlation)
        return self._webhook.send(
            url=trigger["callback_url"],
            secret=secret,
            payload=payload,
            correlation=correlation,
        )

    def _resolve_callback_secret(self, secret_ref: str, correlation: str) -> str:
        """Resolve the HMAC signing secret.

        Today the existing AI-webhook pipeline uses the DRUPAL_WEBHOOK_SECRET
        env var; the trigger contract carries a Secrets Manager ref to allow
        future per-tenant rotation without redeploying. We try Secrets
        Manager first, fall back to the env var to stay drop-in compatible
        with the current deployment.
        """
        try:
            resp = self._secrets.get_secret_value(SecretId=secret_ref)
            secret = resp.get("SecretString", "")
            if secret:
                return secret
        except ClientError as exc:
            logger.info(
                "%s callback_secret_ref %s not in Secrets Manager (%s); falling back to env",
                correlation,
                secret_ref,
                exc.response.get("Error", {}).get("Code", "Unknown"),
            )

        env_secret = os.environ.get("DRUPAL_WEBHOOK_SECRET", "")
        if not env_secret:
            # Webhook will still attempt; HMAC against empty secret will
            # mismatch and Drupal returns 401, which WebhookSender treats
            # as a permanent failure (correct outcome).
            logger.error(
                "%s No webhook secret available (Secrets Manager fetch failed and DRUPAL_WEBHOOK_SECRET unset)",
                correlation,
            )
        return env_secret


class _TerminalTransferError(Exception):
    """Internal sentinel mapping a failure to a contract error_code."""

    def __init__(
        self,
        error_code: str,
        message: str,
        bytes_transferred: int = 0,
    ):
        super().__init__(message)
        self.error_code = error_code
        self.message = message
        self.bytes_transferred = bytes_transferred
