"""HTTP middleware: request ids, access logging and security headers."""

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


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Response headers that reduce what a compromised page can do.

    This exists because of how the browser session is designed rather than as
    boilerplate. The access token is held in JavaScript memory, so script
    injection is the one attack that can reach it — a CSP is the control that
    makes injection unlikely in the first place, and it belongs beside the session
    it is protecting.

    Applied to API responses, which is narrower than it sounds: the SPA is served
    by Vite in development and by a static host in deployment, and those need
    their own headers. What these cover is the API's own JSON, its `/docs`, and
    anything a browser might be induced to render from this origin.
    """

    def __init__(self, app: object, config: Settings | None = None) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self._config = config or settings

    async def dispatch(self, request: Request, call_next: RequestResponseCall) -> Response:
        response = await call_next(request)

        # `default-src 'none'` because an API serves no page that needs anything:
        # every directive below is an explicit exception for the docs UI.
        policy = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        if not self._config.is_production and request.url.path.startswith(
            ("/docs", "/redoc", "/openapi")
        ):
            # Swagger UI loads its bundle and inlines a style block. Relaxed only
            # for the docs paths, and only where the docs exist at all.
            policy = (
                "default-src 'self'; img-src 'self' data:; "
                "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
                "frame-ancestors 'none'; base-uri 'none'"
            )

        headers = {
            "Content-Security-Policy": policy,
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            # An API has no use for any of these, and saying so is what stops a
            # future embedded document from quietly acquiring them.
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        }
        if self._config.is_production:
            # Only in production: sent over plain HTTP it is ignored, and sent
            # from a local dev server it can pin a developer's browser to HTTPS
            # on localhost for a year.
            headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        for name, value in headers.items():
            response.headers.setdefault(name, value)
        return response
