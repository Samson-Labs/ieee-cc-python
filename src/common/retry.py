"""Reusable retry decorator with exponential backoff."""

from __future__ import annotations

import functools
import time
from typing import Callable, Sequence


def with_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    exceptions: Sequence[type[Exception]] = (Exception,),
    fixed_delays: Sequence[float] | None = None,
    on_retry: Callable[[int, Exception, float], None] | None = None,
):
    """Decorator that retries a function on specified exceptions.

    Args:
        max_attempts: Total attempts (including the first call). Must be >= 1.
        base_delay: Base delay in seconds for exponential backoff.
        exceptions: Tuple of exception types to catch and retry.
        fixed_delays: If provided, use these exact delays instead of exponential backoff.
            Length must be >= max_attempts - 1.
        on_retry: Optional callback invoked before each retry sleep as
            ``on_retry(attempt, exception, delay)``.

    Raises:
        ValueError: If ``max_attempts < 1`` or ``fixed_delays`` is too short.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")
    if fixed_delays is not None and len(fixed_delays) < max_attempts - 1:
        raise ValueError(
            f"fixed_delays length ({len(fixed_delays)}) must be >= "
            f"max_attempts - 1 ({max_attempts - 1})"
        )

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc: Exception | None = None
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except tuple(exceptions) as exc:
                    last_exc = exc
                    if attempt < max_attempts - 1:
                        if fixed_delays is not None:
                            delay = fixed_delays[attempt]
                        else:
                            delay = base_delay * (2 ** attempt)
                        if on_retry is not None:
                            on_retry(attempt, exc, delay)
                        time.sleep(delay)
            raise last_exc  # type: ignore[misc]

        return wrapper

    return decorator
