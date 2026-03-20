"""Tests for JSON structured logger."""

import json
import logging

from src.common.logging import get_json_logger, JsonFormatter


class TestJsonFormatter:
    def test_output_is_valid_json(self):
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="hello", args=(), exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["message"] == "hello"
        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "test"
        assert "timestamp" in parsed

    def test_includes_correlation_id(self):
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="msg", args=(), exc_info=None,
        )
        record.correlation_id = "req-abc"
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["correlation_id"] == "req-abc"

    def test_includes_error_type(self):
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="test", level=logging.ERROR, pathname="", lineno=0,
            msg="fail", args=(), exc_info=None,
        )
        record.error_type = "BedrockError"
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["error_type"] == "BedrockError"

    def test_includes_extras(self):
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="msg", args=(), exc_info=None,
        )
        record.extras = {"key": "value", "count": 42}
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["key"] == "value"
        assert parsed["count"] == 42

    def test_includes_exception_info(self):
        formatter = JsonFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            import sys
            record = logging.LogRecord(
                name="test", level=logging.ERROR, pathname="", lineno=0,
                msg="caught", args=(), exc_info=sys.exc_info(),
            )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert "ValueError" in parsed["exception"]
        assert "test error" in parsed["exception"]


class TestGetJsonLogger:
    def test_returns_logger_with_json_handler(self):
        logger = get_json_logger("test.json.logger")
        assert len(logger.handlers) >= 1
        assert isinstance(logger.handlers[-1].formatter, JsonFormatter)

    def test_sets_level(self):
        logger = get_json_logger("test.json.level", level="DEBUG")
        assert logger.level == logging.DEBUG

    def test_does_not_duplicate_handlers(self):
        name = "test.json.nodup"
        logger1 = get_json_logger(name)
        handler_count = len(logger1.handlers)
        logger2 = get_json_logger(name)
        assert len(logger2.handlers) == handler_count
