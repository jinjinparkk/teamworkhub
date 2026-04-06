"""Unit tests for logging_cfg — JSON formatter and configure_logging().

No file I/O; captures log output in memory.
"""
from __future__ import annotations

import json
import logging
import sys
from io import StringIO
from unittest.mock import patch

import pytest

from src.logging_cfg import _JsonFormatter, configure_logging


# ── _JsonFormatter ───────────────────────────────────────────────────── #

def _make_record(
    msg: str = "hello",
    level: int = logging.INFO,
    name: str = "test.logger",
    extra: dict | None = None,
) -> logging.LogRecord:
    """Build a LogRecord directly, optionally merging extra fields."""
    record = logging.LogRecord(
        name=name,
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )
    for k, v in (extra or {}).items():
        setattr(record, k, v)
    return record


class TestJsonFormatter:
    def _fmt(self, record: logging.LogRecord) -> dict:
        raw = _JsonFormatter().format(record)
        return json.loads(raw)

    def test_output_is_valid_json(self):
        raw = _JsonFormatter().format(_make_record())
        json.loads(raw)  # must not raise

    def test_has_timestamp_key(self):
        assert "timestamp" in self._fmt(_make_record())

    def test_has_severity_key(self):
        assert "severity" in self._fmt(_make_record())

    def test_has_logger_key(self):
        assert "logger" in self._fmt(_make_record())

    def test_has_message_key(self):
        assert "message" in self._fmt(_make_record())

    def test_severity_matches_level(self):
        data = self._fmt(_make_record(level=logging.WARNING))
        assert data["severity"] == "WARNING"

    def test_message_matches_msg(self):
        data = self._fmt(_make_record(msg="important event"))
        assert data["message"] == "important event"

    def test_logger_name_correct(self):
        data = self._fmt(_make_record(name="myapp.sync"))
        assert data["logger"] == "myapp.sync"

    def test_extra_field_merged(self):
        data = self._fmt(_make_record(extra={"run_id": "abc123"}))
        assert data["run_id"] == "abc123"

    def test_multiple_extra_fields(self):
        data = self._fmt(_make_record(extra={"run_id": "x", "processed": 5}))
        assert data["run_id"] == "x"
        assert data["processed"] == 5

    def test_stdlib_field_not_leaked(self):
        # 'lineno', 'funcName', 'process' are stdlib internals — must not appear
        data = self._fmt(_make_record())
        assert "lineno" not in data
        assert "funcName" not in data
        assert "process" not in data

    def test_exception_info_included(self):
        try:
            raise ValueError("test error")
        except ValueError:
            record = logging.LogRecord(
                name="test", level=logging.ERROR, pathname=__file__,
                lineno=1, msg="oops", args=(), exc_info=sys.exc_info(),
            )
        data = self._fmt(record)
        assert "exception" in data
        assert "ValueError" in data["exception"]

    def test_non_serialisable_value_does_not_raise(self):
        """default=str should handle non-JSON-serialisable extra values."""
        data = self._fmt(_make_record(extra={"obj": object()}))
        assert "obj" in data  # stringified, but present

    def test_timestamp_is_iso_format(self):
        data = self._fmt(_make_record())
        ts = data["timestamp"]
        # ISO-8601 with timezone offset or 'Z'
        assert "T" in ts and ("+" in ts or "Z" in ts or ts.endswith("+00:00"))


# ── configure_logging() ──────────────────────────────────────────────── #

class TestConfigureLogging:
    def test_root_logger_has_one_handler(self, monkeypatch):
        monkeypatch.setenv("LOG_FORMAT", "json")
        monkeypatch.setenv("LOG_LEVEL", "INFO")
        configure_logging()
        root = logging.getLogger()
        assert len(root.handlers) == 1

    def test_json_format_uses_json_formatter(self, monkeypatch):
        monkeypatch.setenv("LOG_FORMAT", "json")
        configure_logging()
        handler = logging.getLogger().handlers[0]
        assert isinstance(handler.formatter, _JsonFormatter)

    def test_pretty_format_uses_basic_formatter(self, monkeypatch):
        monkeypatch.setenv("LOG_FORMAT", "pretty")
        configure_logging()
        handler = logging.getLogger().handlers[0]
        assert not isinstance(handler.formatter, _JsonFormatter)

    def test_log_level_debug_applied(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        configure_logging()
        assert logging.getLogger().level == logging.DEBUG

    def test_log_level_warning_applied(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "WARNING")
        configure_logging()
        assert logging.getLogger().level == logging.WARNING

    def test_handler_writes_to_stdout(self, monkeypatch):
        monkeypatch.setenv("LOG_FORMAT", "json")
        configure_logging()
        handler = logging.getLogger().handlers[0]
        assert handler.stream is sys.stdout

    def test_idempotent_multiple_calls(self, monkeypatch):
        """Calling configure_logging() twice must not stack handlers."""
        monkeypatch.setenv("LOG_FORMAT", "json")
        configure_logging()
        configure_logging()
        assert len(logging.getLogger().handlers) == 1
