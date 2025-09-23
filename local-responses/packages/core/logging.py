# packages/core/logging.py
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

from fastapi import Request, Response


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:  # type: ignore[override]
        base: Dict[str, Any] = {
            "level": record.levelname,
            "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "logger": record.name,
        }
        msg = record.msg
        if isinstance(msg, dict):
            payload = {**base, **msg}
        else:
            payload = {**base, "message": record.getMessage()}
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())

    # Clear existing handlers to avoid duplicates in reload
    root.handlers.clear()
    root.addHandler(handler)


async def request_logging_middleware(request: Request, call_next):
    start = time.perf_counter()
    response: Optional[Response] = None
    try:
        response = await call_next(request)
        return response
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        try:
            status = response.status_code if response else 500
        except Exception:
            status = 500
        logging.getLogger("app.request").info(
            {
                "method": request.method,
                "path": request.url.path,
                "status": status,
                "duration_ms": round(duration_ms, 2),
                "trace_id": request.headers.get("x-trace-id"),
            }
        )
