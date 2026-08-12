"""Shared test fixtures.

Unit tests run without Postgres or Redis: the lifespan hooks that would open
those connections are patched out, and the auth dependency is overridable per
test. Tests that genuinely need the real services are marked `integration`.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator

import pytest

# Set before any application module is imported, so settings pick these up.
os.environ.setdefault("CWB_ENVIRONMENT", "test")
os.environ.setdefault("CWB_OBS_LOG_LEVEL", "WARNING")
os.environ.setdefault("CWB_CELERY_TASK_ALWAYS_EAGER", "true")

from httpx import ASGITransport, AsyncClient

from app.api.deps.auth import get_current_principal, get_verifier
from app.core.config import Settings, get_settings
from app.core.security import Principal


@pytest.fixture(scope="session")
def settings() -> Settings:
    return get_settings()


@pytest.fixture
def principal() -> Principal:
    return Principal(
        subject="00000000-0000-0000-0000-000000000001",
        username="test-user",
        email="test-user@example.com",
        full_name="Test User",
        realm_roles=frozenset({"claims-adjuster"}),
        client_roles=frozenset({"claims-admin"}),
        claims={"sub": "00000000-0000-0000-0000-000000000001"},
    )


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    """The application with external connections stubbed out.

    Patched at the point `app.main` imports them, so the lifespan runs end to
    end — middleware, handlers and metrics are all exercised for real.
    """
    import app.main as app_main

    async def _noop(*_args: object, **_kwargs: object) -> None:
        return None

    for target in (
        "init_engine",
        "dispose_engine",
        "init_pool",
        "close_pool",
        "init_redis",
        "close_redis",
    ):
        monkeypatch.setattr(app_main, target, _noop)

    # A per-test registry, so building an app per test does not collide with
    # collectors registered by the previous one.
    from prometheus_client import CollectorRegistry

    yield app_main.create_app(metrics_registry=CollectorRegistry())


@pytest.fixture
async def client(app: object) -> AsyncIterator[AsyncClient]:
    """Unauthenticated client, with the lifespan actually run."""
    from fastapi import FastAPI

    assert isinstance(app, FastAPI)
    async with (
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as http_client,
        app.router.lifespan_context(app),
    ):
        yield http_client


@pytest.fixture
async def auth_client(app: object, principal: Principal) -> AsyncIterator[AsyncClient]:
    """Client whose requests resolve to `principal` without contacting Keycloak."""
    from fastapi import FastAPI

    assert isinstance(app, FastAPI)
    app.dependency_overrides[get_current_principal] = lambda: principal
    app.dependency_overrides[get_verifier] = lambda: None

    async with (
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers={"Authorization": "Bearer test-token"},
        ) as http_client,
        app.router.lifespan_context(app),
    ):
        yield http_client

    app.dependency_overrides.clear()
