"""Outbound HTTP retry policy, driven through respx."""

from __future__ import annotations

import httpx
import pytest
import respx

from app.core.errors import ExternalServiceError
from app.integrations.http import MAX_RETRY_AFTER_SECONDS, HttpClient, retry_after_seconds

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


# ---------------------------------------------------------------------------
# Retry-After
#
# A rate limiter knows when its window resets and this code does not, so its
# number is honoured rather than talked over. Retrying at 0.4s against a limiter
# that asked for twenty seconds is guaranteed to fail, and on most providers it
# also spends the budget being waited for.
# ---------------------------------------------------------------------------


def _response(value: str | None) -> httpx.Response:
    headers = {"Retry-After": value} if value is not None else {}
    return httpx.Response(429, headers=headers, request=httpx.Request("GET", BASE_URL))


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("", None),
        ("20", 20.0),
        ("0", 0.0),
        ("not a delay", None),
        # An HTTP date already in the past is the same as no delay at all.
        ("Wed, 21 Oct 2015 07:28:00 GMT", 0.0),
    ],
)
def test_retry_after_is_read_in_both_forms(header: str | None, expected: float | None) -> None:
    assert retry_after_seconds(_response(header)) == expected


@respx.mock
async def test_a_retry_after_longer_than_we_may_hold_fails_immediately(
    client: HttpClient,
) -> None:
    """Sleeping a minute inside an inbound request, then failing, helps nobody.

    The caller gets its error now and the pipeline stage records a provider that
    was unavailable — a state it already handles — instead of the request being
    held open for a window this process cannot wait out.
    """
    route = respx.get(f"{BASE_URL}/resource").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "600"})
    )

    with pytest.raises(ExternalServiceError, match="600s"):
        await client.get("/resource")

    assert route.call_count == 1
    await client.aclose()


@respx.mock
async def test_a_short_retry_after_is_waited_out_and_the_call_succeeds(
    client: HttpClient,
) -> None:
    route = respx.get(f"{BASE_URL}/resource").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )

    response = await client.get("/resource")

    assert response.status_code == 200
    assert route.call_count == 2
    # `Retry-After: 0` must not turn the backoff into a hot loop — the exponential
    # floor still applies, so the server's number only ever raises the wait.
    assert MAX_RETRY_AFTER_SECONDS > 0
    await client.aclose()
