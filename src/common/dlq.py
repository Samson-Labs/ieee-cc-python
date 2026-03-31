"""DLQ message format builder."""

from __future__ import annotations

from datetime import datetime, timezone

from src.common import format_stack_trace
from src.common.exceptions import PipelineError


def build_dlq_message(
    original_event: dict,
    exc: Exception,
    correlation_id: str = "",
    retry_count: int = 0,
) -> dict:
    """Build a structured DLQ message.

    Args:
        original_event: The original Lambda event that failed.
        exc: The exception that caused the failure.
        correlation_id: Optional correlation ID for tracing.
        retry_count: Number of times this event has been retried.

    Returns:
        Dict suitable for publishing to an SQS dead-letter queue.
    """
    if isinstance(exc, PipelineError):
        error_type = exc.error_type
        is_retriable = exc.is_retriable
    else:
        error_type = type(exc).__name__
        is_retriable = False

    return {
        "original_event": original_event,
        "error": {
            "error_type": error_type,
            "error_message": str(exc),
            "is_retriable": is_retriable,
            "correlation_id": correlation_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stack_trace": format_stack_trace(exc),
        },
        "retry_count": retry_count,
    }
