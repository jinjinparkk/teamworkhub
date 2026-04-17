"""Structured JSON logging — Cloud Logging compatible.

Output format (LOG_FORMAT=json, default):
    {"timestamp": "...", "severity": "INFO", "logger": "...", "message": "...", ...extra}

Cloud Logging picks up `severity` and `message` fields automatically.
Set LOG_FORMAT=pretty for human-readable local dev output.
"""
import json
import logging
import os
import sys
from datetime import datetime, timezone


class _JsonFormatter(logging.Formatter):
    """Emit one JSON object per log line."""

    # Fields added by LogRecord that we don't want to re-emit as extras.
    _STDLIB_FIELDS = frozenset({
        "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
        "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
        "created", "msecs", "relativeCreated", "thread", "threadName",
        "processName", "process", "message", "taskName",
    })

    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        payload: dict = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.message,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # Merge caller-supplied `extra={...}` fields.
        for key, val in record.__dict__.items():
            if key not in self._STDLIB_FIELDS:
                payload[key] = val
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging() -> None:
    """Configure root logger.  Call once at startup."""
    fmt = os.environ.get("LOG_FORMAT", "json").lower()
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    # On Windows, sys.stdout/stderr may default to a narrow encoding (e.g.
    # cp949).  Reconfigure both to UTF-8 so log messages with non-ASCII
    # characters (em-dashes, Korean text) don't raise UnicodeEncodeError.
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    handler = logging.StreamHandler(sys.stdout)
    if fmt == "pretty":
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)-8s] %(name)s - %(message)s")
        )
    else:
        handler.setFormatter(_JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # Reduce noise from internal libraries.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").propagate = False
