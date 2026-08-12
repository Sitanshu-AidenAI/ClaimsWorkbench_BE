"""The uniform error envelope."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    register_exception_handlers,
)


@pytest.fixture
async def error_client() -> AsyncClient:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/not-found")
    async def _not_found() -> None:
        raise NotFoundError("Claim CL-1 does not exist.", details={"claim_id": "CL-1"})

    @app.get("/conflict")
    async def _conflict() -> None:
        raise ConflictError()

    @app.get("/forbidden")
    async def _forbidden() -> None:
        raise PermissionDeniedError()

    @app.get("/boom")
    async def _boom() -> None:
        raise RuntimeError("something unexpected")

    @app.get("/validated")
    async def _validated(count: int) -> dict[str, int]:
        return {"count": count}

    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    )


async def test_app_errors_map_to_their_status_and_code(error_client: AsyncClient) -> None:
    response = await error_client.get("/not-found")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["message"] == "Claim CL-1 does not exist."
    assert body["error"]["details"] == {"claim_id": "CL-1"}
    assert "request_id" in body


@pytest.mark.parametrize(
    ("path", "status_code", "code"),
    [
        ("/conflict", 409, "conflict"),
        ("/forbidden", 403, "permission_denied"),
    ],
)
async def test_error_types_use_their_defaults(
    error_client: AsyncClient, path: str, status_code: int, code: str
) -> None:
    response = await error_client.get(path)

    assert response.status_code == status_code
    assert response.json()["error"]["code"] == code


async def test_unexpected_exceptions_do_not_leak_details(error_client: AsyncClient) -> None:
    response = await error_client.get("/boom")

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "internal_error"
    assert "something unexpected" not in body["error"]["message"]


async def test_request_validation_uses_the_same_envelope(error_client: AsyncClient) -> None:
    response = await error_client.get("/validated", params={"count": "not-a-number"})

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["details"]["errors"]


async def test_unknown_routes_use_the_same_envelope(error_client: AsyncClient) -> None:
    response = await error_client.get("/no-such-route")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
