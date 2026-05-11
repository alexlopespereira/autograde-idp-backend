from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

_RESERVED = frozenset(
    {
        "args", "msg", "exc_info", "exc_text", "stack_info", "created", "msecs",
        "relativeCreated", "levelname", "levelno", "pathname", "filename", "module",
        "lineno", "funcName", "name", "thread", "threadName", "processName", "process",
        "asctime", "message", "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = value
        return json.dumps(payload, default=str)


LOG_CONFIG: dict[str, object] = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "app.logging_config.JsonFormatter"}},
    "handlers": {
        "default": {"class": "logging.StreamHandler", "formatter": "json"},
    },
    "root": {"level": "INFO", "handlers": ["default"]},
}
