from __future__ import annotations

import json
import logging
import logging.config
import re
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from time import perf_counter
from typing import Any, cast
from uuid import uuid4

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)


def normalize_request_id(candidate: str | None) -> str:
    """Return a safe caller-supplied request ID or generate a new opaque ID."""
    if candidate is not None and REQUEST_ID_PATTERN.fullmatch(candidate):
        return candidate
    return uuid4().hex


class JsonFormatter(logging.Formatter):
    """Serialize application log records as compact JSON objects."""

    extra_fields = (
        "duration_ms",
        "environment",
        "event",
        "http_method",
        "http_path",
        "idempotency_key_hash",
        "new_status",
        "previous_status",
        "status_code",
        "task_id",
        "task_type",
    )

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        """Format one record without relying on ambient text formatting."""
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": self.service_name,
            "request_id": request_id_context.get(),
        }

        for field_name in self.extra_fields:
            if hasattr(record, field_name):
                payload[field_name] = getattr(record, field_name)

        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def configure_logging(level: str, service_name: str) -> None:
    """Configure application and Uvicorn logs to use one JSON stdout handler."""
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "json": {
                    "()": JsonFormatter,
                    "service_name": service_name,
                }
            },
            "handlers": {
                "stdout": {
                    "class": "logging.StreamHandler",
                    "formatter": "json",
                    "stream": "ext://sys.stdout",
                }
            },
            "root": {
                "handlers": ["stdout"],
                "level": level,
            },
            "loggers": {
                "uvicorn.access": {
                    "handlers": [],
                    "propagate": False,
                },
                "uvicorn.error": {
                    "handlers": ["stdout"],
                    "level": level,
                    "propagate": False,
                },
            },
        }
    )


async def sanitized_validation_error_handler(
    _: Request,
    error: Exception,
) -> JSONResponse:
    """Return validation details without echoing submitted values or non-finite input."""
    validation_error = cast(RequestValidationError, error)
    detail = [
        {
            "type": item["type"],
            "loc": list(item["loc"]),
            "msg": item["msg"],
        }
        for item in validation_error.errors()
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"detail": detail},
    )


class RequestContextMiddleware:
    """Add request correlation state, response headers, and structured access logs."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = normalize_request_id(Headers(scope=scope).get(REQUEST_ID_HEADER))
        token: Token[str | None] = request_id_context.set(request_id)
        started_at = perf_counter()
        status_code = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            logger.exception(
                "request_failed",
                extra={
                    "http_method": scope.get("method"),
                    "http_path": scope.get("path"),
                },
            )
            raise
        finally:
            logger.info(
                "request_completed",
                extra={
                    "duration_ms": round((perf_counter() - started_at) * 1000, 3),
                    "http_method": scope.get("method"),
                    "http_path": scope.get("path"),
                    "status_code": status_code,
                },
            )
            request_id_context.reset(token)
