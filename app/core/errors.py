"""Application error types and the handlers that render them.

Every error leaves the API in one shape:

    {"error": {"code": "not_found", "message": "...", "details": {...}},
     "request_id": "..."}
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.context import get_request_id
from app.core.logging import get_logger

logger = get_logger(__name__)

# Spelled out rather than taken from `status`, whose constant for this code was
# renamed across Starlette versions.
HTTP_422_UNPROCESSABLE = 422


class AppError(Exception):
    """Base class for errors this application raises deliberately."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "internal_error"
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.status_code = status_code or self.status_code
        self.details = details or {}
        super().__init__(self.message)


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    message = "The requested resource was not found."


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"
    message = "The request conflicts with the current state of the resource."


class ValidationError(AppError):
    status_code = HTTP_422_UNPROCESSABLE
    code = "validation_error"
    message = "The request payload is invalid."


class AuthenticationError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthenticated"
    message = "Authentication credentials were missing or invalid."


class PermissionDeniedError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "permission_denied"
    message = "You do not have permission to perform this action."


class RateLimitedError(AppError):
    """Too many attempts. Carries the wait in `details.retry_after_seconds`.

    The `rate_limited` code was already in the `HTTPException` status map below,
    which is what a route raising a bare 429 would have produced. This gives the
    same shape a type to raise, so the sign-in throttle does not have to reach for
    `HTTPException` and lose the stable code the client switches on.
    """

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"


class ExternalServiceError(AppError):
    status_code = status.HTTP_502_BAD_GATEWAY
    code = "external_service_error"
    message = "A dependency returned an unexpected response."


def error_response(
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    """The one error envelope, as a response object.

    Public because a handler is not always the right place to build it. A route
    that must both fail *and* set a header — clearing a session cookie on the way
    out, say — cannot raise: the registered handler constructs its own response
    and every header the route set on the injected one is discarded with it. Such
    a route returns this instead, and the shape stays identical either way.
    """
    payload: dict[str, Any] = {
        "error": {"code": code, "message": message, "details": details or {}},
        "request_id": get_request_id(),
    }
    return JSONResponse(status_code=status_code, content=payload)


def _serialisable_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    """Pydantic's validation errors, with the parts that will not serialise removed.

    A schema whose validator raises `ValueError` — which is how Pydantic asks for
    a custom message — puts the *exception object* in `ctx["error"]`. Passing
    that to `json.dumps` raises inside the error handler, so a 422 becomes a 500
    and the caller is told nothing about what was wrong with their payload.

    `ctx` is dropped rather than coerced: its useful content is already in `msg`,
    and the remainder is Pydantic internals no API client should be reading.
    `url` goes with it — a link to pydantic.dev is not part of this API.

    **`input` is dropped too, and that one is a security property rather than
    tidiness.** Pydantic puts the value that failed validation in it, so echoing it
    back hands the caller their own payload — which is claim data on most routes and
    a *password* on `POST /auth/login`. A 422 on the login body would otherwise
    return the password in the response, and from there into any client-side error
    log. Nothing needs its own submission read back to it: `loc` says which field,
    `msg` says what was wrong with it.
    """
    cleaned: list[dict[str, Any]] = []
    for error in exc.errors():
        entry = {key: value for key, value in error.items() if key not in ("ctx", "url", "input")}
        # `loc` is a tuple and may carry non-string parts for a list index.
        entry["loc"] = [str(part) for part in error.get("loc", ())]
        cleaned.append(entry)
    return cleaned


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        if exc.status_code >= 500:
            logger.error("app_error", code=exc.code, message=exc.message, exc_info=exc)
        else:
            logger.info("app_error", code=exc.code, message=exc.message)
        return error_response(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_error(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {
            status.HTTP_401_UNAUTHORIZED: "unauthenticated",
            status.HTTP_403_FORBIDDEN: "permission_denied",
            status.HTTP_404_NOT_FOUND: "not_found",
            status.HTTP_409_CONFLICT: "conflict",
            status.HTTP_429_TOO_MANY_REQUESTS: "rate_limited",
        }.get(exc.status_code, "http_error")
        return error_response(exc.status_code, code, str(exc.detail))

    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return error_response(
            HTTP_422_UNPROCESSABLE,
            "validation_error",
            "The request payload is invalid.",
            {"errors": _serialisable_errors(exc)},
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled_exception", error=str(exc))
        return error_response(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "internal_error",
            "An unexpected error occurred.",
        )
