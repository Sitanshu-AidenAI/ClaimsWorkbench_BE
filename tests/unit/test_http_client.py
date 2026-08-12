"""Outbound HTTP retry policy, driven through respx."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.core.errors import ExternalServiceError
from app.integrations.http import HttpClient

BASE_URL = "http://upstream.test"


@pytest.fixture
async def client() -> HttpClient:
    return HttpClient(BASE_URL, max_attempts=3)


@respx.mock
async def test_successful_request_is_not_retried(client: HttpClient) -> None:
    route = respx.get(f"{BASE_URL}/resource").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )

    response = await client.get("/resource")

    assert response.status_code == 200
    assert route.call_count == 1
    await client.aclose()


@respx.mock
async def test_retryable_status_is_retried_then_succeeds(client: HttpClient) -> None:
    route = respx.get(f"{BASE_URL}/resource").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(200, json={"ok": True}),
        ]
    )

    response = await client.get("/resource")

    assert response.status_code == 200
    assert route.call_count == 2
    await client.aclose()


@respx.mock
async def test_exhausted_retries_raise_external_service_error(client: HttpClient) -> None:
    route = respx.get(f"{BASE_URL}/resource").mock(return_value=httpx.Response(500))

    with pytest.raises(ExternalServiceError):
        await client.get("/resource")

    assert route.call_count == 3
    await client.aclose()


@respx.mock
async def test_client_errors_are_returned_not_retried(client: HttpClient) -> None:
    # A 404 is the upstream's answer, not a transient failure — the caller
    # decides what it means.
    route = respx.get(f"{BASE_URL}/missing").mock(return_value=httpx.Response(404))

    response = await client.get("/missing")

    assert response.status_code == 404
    assert route.call_count == 1
    await client.aclose()


@respx.mock
async def test_transport_errors_surface_as_external_service_error(client: HttpClient) -> None:
    respx.get(f"{BASE_URL}/resource").mock(side_effect=httpx.ConnectError("connection refused"))

    with pytest.raises(ExternalServiceError):
        await client.get("/resource")

    await client.aclose()
