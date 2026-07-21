"""Structured logging with request correlation

A per-request id (from an inbound ``X-Request-ID`` header or generated) is stored
in a context variable, injected into every log record by ``RequestIDFilter``, and
emitted as JSON by ``JsonFormatter`` so logs are queryable and correlatable.
"""

from __future__ import annotations

import contextvars
import json
import logging

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class RequestIDFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class SlowQueryFilter(logging.Filter):
    """Pass only SQL log records slower than a threshold (in seconds)."""

    def __init__(self, threshold_ms: float = 0):
        super().__init__()
        self.threshold = threshold_ms / 1000.0

    def filter(self, record: logging.LogRecord) -> bool:
        duration = getattr(record, "duration", None)
        return self.threshold > 0 and duration is not None and duration >= self.threshold


class JsonFormatter(logging.Formatter):
    """One JSON object per log line, including the correlating request id."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)
