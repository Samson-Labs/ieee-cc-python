"""Shared infrastructure: exceptions, retry, error handling, logging, DLQ."""

from __future__ import annotations

import traceback


def format_stack_trace(exc: Exception, max_length: int = 2000) -> str:
    """Format an exception's stack trace, truncated to *max_length* chars."""
    stack = traceback.format_exception(type(exc), exc, exc.__traceback__)
    stack_trace = "".join(stack)
    if len(stack_trace) > max_length:
        stack_trace = stack_trace[:max_length]
    return stack_trace
