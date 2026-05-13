"""Structured JSON logging (spec §9.2).

One JSON object per line on stdout. Every record carries: ``ts`` (ISO 8601
UTC), ``level``, ``event``, plus any record-specific fields.

A unit test asserts the schema by parsing each line as JSON and checking the
required keys.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    """Emits one compact JSON object per log record."""

    REQUIRED_KEYS = ("ts", "level", "event")

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
                timespec="milliseconds"
            ).replace("+00:00", "Z"),
            "level": record.levelname,
            "event": record.name + "." + (record.msg.split(" ", 1)[0] if isinstance(record.msg, str) else "log"),
            "message": record.getMessage(),
        }
        # Defensive: never log a record containing 'card_number'.
        if "card_number" in payload["message"]:
            payload["message"] = "[redacted: payload would have contained card_number]"
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str | None = None) -> None:
    """Install the JSON formatter on the root logger."""
    lvl = (level or os.environ.get("LOG_LEVEL", "INFO")).upper()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(lvl)
