"""Structured (JSON) logging with a per-request correlation id.

One line = one JSON object on stdout, always carrying the current `request_id`
so a single request can be traced across routers and services. Set the id once
per request (middleware) and every log line below it inherits it via a
contextvar — no need to thread it through function calls.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from contextvars import ContextVar

# The correlation id for the request currently being handled ("-" outside one).
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

# Standard LogRecord attributes we don't want to duplicate into the JSON body;
# anything passed via `extra=` that isn't in here is included as a field.
_STD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        # Merge structured fields passed via logger.info("...", extra={...}).
        for k, v in record.__dict__.items():
            if k not in _STD_ATTRS and not k.startswith("_"):
                payload[k] = v
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter as the single stdout handler on the root logger."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # uvicorn's own loggers should flow through our handler, not their default one.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
