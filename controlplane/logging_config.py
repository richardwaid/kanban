"""Structured logging configuration for the kanban agent system.

Supports two output formats controlled by KANBAN_LOG_FORMAT env var:
  - "json" (default): JSON lines with structured fields for log aggregation
  - "text": Human-readable text for development
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Include structured context fields if present
        for field in ("task_id", "feature_id", "agent", "duration", "cost_usd"):
            val = getattr(record, field, None)
            if val is not None:
                entry[field] = val
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, separators=(",", ":"))


def setup_logging() -> None:
    """Configure the root logger based on KANBAN_LOG_FORMAT env var."""
    fmt = os.environ.get("KANBAN_LOG_FORMAT", "text")
    level = os.environ.get("KANBAN_LOG_LEVEL", "INFO").upper()

    root = logging.getLogger()
    root.setLevel(getattr(logging, level, logging.INFO))

    # Remove any existing handlers (e.g. from basicConfig)
    root.handlers.clear()

    handler = logging.StreamHandler()
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
        ))
    root.addHandler(handler)
