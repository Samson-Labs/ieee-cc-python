"""JSON structured logger for CloudWatch Logs Insights."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        log_obj: dict = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        if hasattr(record, "correlation_id"):
            log_obj["correlation_id"] = record.correlation_id

        if hasattr(record, "error_type"):
            log_obj["error_type"] = record.error_type

        if hasattr(record, "extras") and isinstance(record.extras, dict):
            log_obj.update(record.extras)

        if record.exc_info and record.exc_info[1] is not None:
            log_obj["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_obj, default=str)


def get_json_logger(name: str, level: str = "INFO") -> logging.Logger:
    """Return a logger configured with JSON output.

    Args:
        name: Logger name (typically ``__name__``).
        level: Log level string (e.g. ``"INFO"``, ``"DEBUG"``).

    Returns:
        A ``logging.Logger`` with a ``JsonFormatter`` stream handler.
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)

    return logger
