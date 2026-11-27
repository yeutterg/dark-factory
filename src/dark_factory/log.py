"""Structured logs: one JSON object per line, with the ids an operator needs to follow work."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

LOGGER = "dark_factory"
ID_KEYS = ("root", "work_item_id", "attempt_id", "attempt", "job_id", "job", "release")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
        }
        entry.update(getattr(record, "fields", {}))
        return json.dumps(entry, sort_keys=True, default=str)


def configure(stream=None, level: int = logging.INFO) -> None:
    """Route the factory's logs to a stream (stderr by default) as JSON lines."""
    logger = logging.getLogger(LOGGER)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False


def event(name: str, actor: str, payload: dict[str, Any]) -> None:
    """Emit an audit record as a log line, lifting its ids to the top level."""
    logger = logging.getLogger(LOGGER)
    if not logger.handlers:
        return
    fields: dict[str, Any] = {"actor": actor}
    fields.update({k: payload[k] for k in ID_KEYS if k in payload})
    level = logging.WARNING if "error" in name or "fail" in name else logging.INFO
    if "error" in payload:
        fields["error"] = str(payload["error"])[:300]
    logger.log(level, name, extra={"fields": fields})
