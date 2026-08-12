"""Health and metadata endpoints."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


async def test_liveness_reports_ok(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["environment"] == "test"


async def test_liveness_echoes_request_id(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health", headers={"X-Request-ID": "abc-123"})

    assert response.headers["X-Request-ID"] == "abc-123"


async def test_liveness_generates_request_id_when_absent(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")

    assert response.headers.get("X-Request-ID")


async def test_readiness_degrades_when_dependencies_are_down(client: AsyncClient) -> None:
    # Postgres and Redis are not initialised in unit tests, so readiness must
    # report degraded rather than raise.
    response = await client.get("/api/v1/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["dependencies"] == {"postgres": False, "redis": False}


async def test_metrics_endpoint_is_exposed(client: AsyncClient) -> None:
    response = await client.get("/metrics")

    assert response.status_code == 200
    assert "http_request_duration_seconds" in response.text


async def test_client_config_is_public(client: AsyncClient) -> None:
    response = await client.get("/api/v1/meta/config")

    assert response.status_code == 200
    body = response.json()
    assert body["auth"]["realm"] == "claims-workbench"
    assert body["auth"]["authority"].endswith("/realms/claims-workbench")


@pytest.mark.parametrize("path", ["/api/v1/meta/whoami"])
async def test_protected_routes_reject_anonymous_callers(client: AsyncClient, path: str) -> None:
    response = await client.get(path)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


async def test_whoami_returns_the_principal(auth_client: AsyncClient) -> None:
    response = await auth_client.get("/api/v1/meta/whoami")

    assert response.status_code == 200
    body = response.json()
    assert body["username"] == "test-user"
    assert body["roles"] == ["claims-adjuster", "claims-admin"]
