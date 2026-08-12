"""Server-side authorisation on the FNOL and claims routes.

The client hides what a person cannot do; this is what makes it true. Every case
here calls the real route with a real principal and asserts the refusal happens
before any work — which is why the session dependency is a stub that would fail
loudly if a handler ever reached it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.deps.auth import get_current_principal, get_verifier
from app.api.deps.db import get_session
from app.api.deps.services import get_ai_provider_dependency
from app.core.security import Principal


class UnusableSession:
    """A session that fails if a handler gets past the authorisation gate.

    The point of the test is that authorisation refuses *first*. If a route ever
    starts doing work before checking, these tests turn into loud errors rather
    than quietly passing against a database that happens to be up.
    """

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(
            f"The request reached the database (`session.{name}`) despite being unauthorised."
        )


def principal_with(*roles: str) -> Principal:
    return Principal(
        subject="00000000-0000-0000-0000-000000000042",
        username="test-user",
        email="test-user@example.com",
        full_name="Test User",
        realm_roles=frozenset(roles),
    )


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    from prometheus_client import CollectorRegistry

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

    application = app_main.create_app(metrics_registry=CollectorRegistry())

    async def _session() -> AsyncIterator[UnusableSession]:
        yield UnusableSession()

    application.dependency_overrides[get_session] = _session
    application.dependency_overrides[get_verifier] = lambda: None
    application.dependency_overrides[get_ai_provider_dependency] = lambda: None
    yield application
    application.dependency_overrides.clear()


async def client_for(application: FastAPI, principal: Principal | None) -> AsyncClient:
    if principal is None:
        application.dependency_overrides.pop(get_current_principal, None)
    else:
        application.dependency_overrides[get_current_principal] = lambda: principal
    return AsyncClient(transport=ASGITransport(app=application), base_url="http://test")


#: Every mutating FNOL route, with a body the schema accepts. Listed exhaustively
#: on purpose: a new write endpoint that is not in this table is a new endpoint
#: nobody has checked the permissions on.
WRITE_ROUTES: tuple[tuple[str, str, dict[str, Any] | None], ...] = (
    ("POST", "/api/v1/fnol", {"channel": "manual", "body": "A loss occurred."}),
    (
        "POST",
        "/api/v1/fnol/email",
        {
            "sender": "broker@example.com",
            "recipient": "fnol@carrier.com",
            "subject": "FNOL",
            "body": "A loss occurred.",
            "message_id": "<a@b>",
        },
    ),
    ("POST", "/api/v1/fnol/FNOL-2026-000001/process", {}),
    ("PATCH", "/api/v1/fnol/FNOL-2026-000001", {"updates": {"loss.loss_country": "UK"}}),
    (
        "POST",
        "/api/v1/fnol/FNOL-2026-000001/severity",
        {"severity": "high", "reason": "Because."},
    ),
    ("POST", "/api/v1/fnol/FNOL-2026-000001/cat-match", {"confirmed": False}),
    ("POST", "/api/v1/fnol/FNOL-2026-000001/notes", {"body": "A note."}),
    ("POST", "/api/v1/fnol/FNOL-2026-000001/create-claim", {}),
)

READ_ROUTES: tuple[str, ...] = (
    "/api/v1/fnol",
    "/api/v1/fnol/board",
    "/api/v1/fnol/FNOL-2026-000001",
    "/api/v1/claims",
    "/api/v1/claims/CLM-2026-000001",
)


class TestAuthentication:
    async def test_every_fnol_route_requires_a_token(self, api: FastAPI) -> None:
        async with await client_for(api, None) as http:
            for path in READ_ROUTES:
                assert (await http.get(path)).status_code == 401, path
            for method, path, body in WRITE_ROUTES:
                response = await http.request(method, path, json=body)
                assert response.status_code == 401, path


class TestFNOLAuthorisation:
    async def test_a_handler_cannot_work_the_intake_queue(self, api: FastAPI) -> None:
        async with await client_for(api, principal_with("claims-handler")) as http:
            for method, path, body in WRITE_ROUTES:
                response = await http.request(method, path, json=body)
                assert response.status_code == 403, f"{method} {path}"
                assert response.json()["error"]["code"] == "permission_denied"

    async def test_a_role_outside_claims_cannot_read_intake(self, api: FastAPI) -> None:
        async with await client_for(api, principal_with("marketing")) as http:
            for path in READ_ROUTES:
                assert (await http.get(path)).status_code == 403, path

    async def test_an_officer_is_admitted_past_the_gate(self, api: FastAPI) -> None:
        # Admitted, then stopped by the stub session — which is exactly the proof
        # that the gate let the request through rather than refusing it.
        async with await client_for(api, principal_with("fnol-officer")) as http:
            with pytest.raises(AssertionError, match="reached the database"):
                await http.get("/api/v1/fnol/board")


class TestClaimAuthorisation:
    async def test_assignment_is_a_managers_decision(self, api: FastAPI) -> None:
        async with await client_for(api, principal_with("fnol-officer")) as http:
            response = await http.post(
                "/api/v1/claims/CLM-2026-000001/assignment", json={"handler_id": None}
            )
            assert response.status_code == 403

    async def test_triage_override_is_a_managers_decision(self, api: FastAPI) -> None:
        async with await client_for(api, principal_with("claims-handler")) as http:
            response = await http.post(
                "/api/v1/claims/CLM-2026-000001/triage",
                json={
                    "route_key": "major_loss",
                    "route_label": "Major Loss Team",
                    "priority": "urgent",
                    "reason": "Exposure is larger than first reported.",
                },
            )
            assert response.status_code == 403

    async def test_a_manager_is_admitted_past_the_gate(self, api: FastAPI) -> None:
        async with await client_for(api, principal_with("claims-manager")) as http:
            with pytest.raises(AssertionError, match="reached the database"):
                await http.post(
                    "/api/v1/claims/CLM-2026-000001/assignment", json={"handler_id": None}
                )
