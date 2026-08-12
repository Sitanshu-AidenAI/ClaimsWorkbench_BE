"""HTTP middleware: request ids and access logging."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import Settings, settings
from app.core.context import set_request_id
from app.core.logging import get_logger

logger = get_logger("app.access")

RequestResponseCall = Callable[[Request], Awaitable[Response]]


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign/propagate a request id and bind it to the logging context."""

    def __init__(self, app: object, config: Settings | None = None) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._header = (config or settings).observability.request_id_header

    async def dispatch(self, request: Request, call_next: RequestResponseCall) -> Response:
        request_id = request.headers.get(self._header) or str(uuid.uuid4())

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        set_request_id(request_id)
        request.state.request_id = request_id

        try:
            response = await call_next(request)
        finally:
            set_request_id(None)

        response.headers[self._header] = request_id
        return response


class AccessLogMiddleware(BaseHTTPMiddleware):
    """One structured line per request, with duration."""

    async def dispatch(self, request: Request, call_next: RequestResponseCall) -> Response:
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.exception(
                "request_failed",
                method=request.method,
                path=request.url.path,
                duration_ms=duration_ms,
            )
            raise

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info(
            "request_completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        response.headers["X-Response-Time-ms"] = str(duration_ms)
        return response
