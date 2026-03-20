"""Structured error response builder for Lambda handlers."""

from __future__ import annotations

from datetime import datetime, timezone

from src.common import format_stack_trace
from src.common.exceptions import PipelineError, ValidationError


def build_error_response(
    exc: Exception,
    correlation_id: str = "",
    status_code: int | None = None,
) -> dict:
    """Build a structured error response dict.

    Args:
        exc: The exception to convert.
        correlation_id: Optional correlation ID for tracing.
        status_code: Override the auto-detected HTTP status code.

    Returns:
        Dict with ``statusCode`` and ``body`` suitable for Lambda responses.
    """
    if status_code is not None:
        code = status_code
    elif isinstance(exc, (ValidationError, ValueError, KeyError)):
        code = 400
    elif isinstance(exc, PipelineError) and exc.is_retriable:
        code = 502
    else:
        code = 500

    if isinstance(exc, PipelineError):
        error_type = exc.error_type
    else:
        error_type = type(exc).__name__

    stack_trace = format_stack_trace(exc)

    return {
        "statusCode": code,
        "body": {
            "error_type": error_type,
            "error_message": str(exc),
            "correlation_id": correlation_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stack_trace": stack_trace,
        },
    }
