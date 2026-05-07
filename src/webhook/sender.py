"""HMAC-SHA256 signed webhook sender with retry and SNS dead-letter alerting."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.request

from typing import Callable

import boto3

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
BACKOFF_DELAYS = [2, 4, 8]  # seconds
SNS_TOPIC_ENV = "WEBHOOK_FAILURES_SNS_TOPIC_ARN"

# Cap on response-body bytes when a validator is supplied. Drupal acks
# are O(100 bytes); 1 MiB is well above any legitimate response and keeps
# a malfunctioning or malicious peer from filling the Lambda's memory.
MAX_RESPONSE_BYTES = 1 * 1024 * 1024

ResponseValidator = Callable[[dict | None], "tuple[bool, str]"]


class WebhookSender:
    """Sends HMAC-SHA256 signed webhook POSTs with retry and SNS alerting."""

    def __init__(self, sns_client=None):
        self._sns = sns_client or boto3.client("sns")

    @staticmethod
    def _sign(secret: str, body_bytes: bytes) -> str:
        """Compute HMAC-SHA256 hex digest."""
        return hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()

    @staticmethod
    def _parse_response_body(resp) -> dict | None:
        """Best-effort JSON parse of a response body. Returns None on failure.

        Reads at most ``MAX_RESPONSE_BYTES`` to bound memory use against a
        runaway peer; a body that doesn't fit is treated as unparseable.
        """
        try:
            raw = resp.read(MAX_RESPONSE_BYTES)
        except Exception:
            return None
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        return parsed if isinstance(parsed, dict) else None

    def send(
        self,
        url: str,
        secret: str,
        payload: dict,
        correlation: str = "",
        response_validator: ResponseValidator | None = None,
    ) -> bool:
        """Send a signed webhook POST.

        Args:
            url: Destination URL.
            secret: HMAC-SHA256 shared secret.
            payload: JSON-serialisable dict to POST.
            correlation: Logging correlation tag.
            response_validator: Optional callback invoked with the parsed
                JSON response body on a 2xx response. Returns
                ``(is_valid, reason)``; when ``is_valid`` is False the
                delivery is treated as a permanent failure (logged + SNS
                alerted) even though HTTP succeeded. Used to catch silent
                contract drift like Drupal acknowledging a webhook that
                applied zero target fields.

        Returns:
            True on success (2xx and validator-accepted), False on
            permanent failure.
        """
        body_bytes = json.dumps(payload).encode()
        signature = self._sign(secret, body_bytes)

        req = urllib.request.Request(
            url,
            data=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Webhook-Signature": signature,
            },
            method="POST",
        )

        last_error: Exception | None = None

        for attempt in range(MAX_RETRIES):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    if response_validator is not None:
                        body = self._parse_response_body(resp)
                        is_valid, reason = response_validator(body)
                        if not is_valid:
                            logger.error(
                                "%s Webhook to %s returned HTTP %d but failed contract validation: %s",
                                correlation, url, resp.status, reason,
                            )
                            self._publish_failure(
                                url, payload, correlation,
                                f"contract validation failed: {reason}",
                            )
                            return False
                    logger.info(
                        "%s Webhook sent to %s (status %d)",
                        correlation, url, resp.status,
                    )
                    return True

            except urllib.error.HTTPError as exc:
                status = exc.code
                body = exc.read().decode(errors="replace")
                logger.error(
                    "%s Webhook HTTP %d from %s: %s",
                    correlation, status, url, body,
                )

                if 400 <= status < 500:
                    # Client errors are permanent — do not retry
                    self._publish_failure(url, payload, correlation, f"HTTP {status}: {body}")
                    return False

                # 5xx — retry
                last_error = exc

            except (urllib.error.URLError, OSError) as exc:
                logger.error("%s Webhook connection error: %s", correlation, exc)
                last_error = exc

            # Backoff before next attempt (skip delay after last attempt)
            if attempt < MAX_RETRIES - 1:
                delay = BACKOFF_DELAYS[attempt]
                logger.info(
                    "%s Retrying webhook in %ds (attempt %d/%d)",
                    correlation, delay, attempt + 2, MAX_RETRIES,
                )
                time.sleep(delay)

        # Exhausted retries
        logger.error(
            "%s Webhook to %s failed after %d attempts: %s",
            correlation, url, MAX_RETRIES, last_error,
        )
        self._publish_failure(url, payload, correlation, str(last_error))
        return False

    def _publish_failure(
        self,
        url: str,
        payload: dict,
        correlation: str,
        error: str,
    ) -> None:
        """Publish failure alert to SNS dead-letter topic."""
        topic_arn = os.environ.get(SNS_TOPIC_ENV)
        if not topic_arn:
            logger.warning(
                "%s %s not set — skipping SNS failure alert",
                correlation, SNS_TOPIC_ENV,
            )
            return

        try:
            message = json.dumps({
                "url": url,
                "correlation": correlation,
                "error": error,
                "payload": payload,
            })
            self._sns.publish(
                TopicArn=topic_arn,
                Subject="Webhook delivery failure",
                Message=message,
            )
            logger.info("%s Published webhook failure to SNS", correlation)
        except Exception as exc:
            logger.error("%s Failed to publish to SNS: %s", correlation, exc)
