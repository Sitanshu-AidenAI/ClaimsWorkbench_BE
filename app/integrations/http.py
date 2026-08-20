"""Outbound HTTP.

A shared `httpx.AsyncClient` per base URL with connection pooling, plus a
tenacity retry policy that retries only what is safe to retry: transport errors
and 429/5xx responses.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.core.errors import ExternalServiceError
from app.core.logging import get_logger

logger = get_logger(__name__)

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

#: The longest a `Retry-After` this code will honour by waiting.
#:
#: A rate limiter that says "come back in sixty seconds" is telling the truth, and
#: retrying at 0.4s is guaranteed to fail — on most providers it also counts
#: against the very budget being waited for. But an inbound request cannot be held
#: for a minute either, so past this point the honest move is to stop rather than
#: to sleep and then fail anyway: the caller gets its error now, and the pipeline
#: stage records a provider that was unavailable, which is a state it already
#: handles.
MAX_RETRY_AFTER_SECONDS = 10.0

DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
DEFAULT_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20)


class RetryableStatusError(Exception):
    """Raised internally to feed a retryable response back into tenacity."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"Retryable status {response.status_code} from {response.request.url}")


def retry_after_seconds(response: httpx.Response) -> float | None:
    """What the server asked us to wait, in seconds. `None` when it did not ask.

    Both forms RFC 9110 allows: a delta in seconds, and an HTTP date. The date
    form is compared against *our* clock, which is the only one available — a
    skewed clock makes the wait wrong, and a negative result means the moment has
    already passed, which is the same as no delay.
    """
    raw = (response.headers.get("retry-after") or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _wait(state: RetryCallState) -> float:
    """Back off exponentially, unless the server named a delay of its own.

    A rate limiter knows when its window resets and this code does not, so its
    number wins over the jitter — never downwards, though: a `Retry-After: 0` on
    an overloaded upstream should not turn a backoff into a hot loop.
    """
    base = wait_exponential_jitter(initial=0.2, max=5.0)(state)
    outcome = state.outcome
    if outcome is None or not outcome.failed:
        return base
    failure = outcome.exception()
    if not isinstance(failure, RetryableStatusError):
        return base
    asked = retry_after_seconds(failure.response)
    return base if asked is None else max(base, asked)


class HttpClient:
    """Base class for outbound integrations.

    Subclass per external service so timeouts, auth and retry counts are owned
    by the integration rather than scattered across call sites.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: httpx.Timeout | None = None,
        headers: dict[str, str] | None = None,
        max_attempts: int = 3,
    ) -> None:
        self._max_attempts = max_attempts
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout or DEFAULT_TIMEOUT,
            limits=DEFAULT_LIMITS,
            headers=headers or {},
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> HttpClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Issue a request, retrying transport errors and retryable statuses."""
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._max_attempts),
            wait=_wait,
            retry=retry_if_exception_type((httpx.TransportError, RetryableStatusError)),
            reraise=True,
        )

        try:
            async for attempt in retrying:
                with attempt:
                    response = await self._client.request(method, url, **kwargs)
                    if response.status_code in RETRYABLE_STATUS:
                        asked = retry_after_seconds(response)
                        if asked is not None and asked > MAX_RETRY_AFTER_SECONDS:
                            logger.error(
                                "http_retry_after_too_long",
                                method=method,
                                url=url,
                                status_code=response.status_code,
                                retry_after_seconds=asked,
                            )
                            raise ExternalServiceError(
                                f"Upstream returned {response.status_code} and asked for "
                                f"{asked:.0f}s before a retry."
                            )
                        raise RetryableStatusError(response)
                    return response
        except RetryableStatusError as exc:
            logger.error(
                "http_request_exhausted",
                method=method,
                url=url,
                status_code=exc.response.status_code,
            )
            raise ExternalServiceError(
                f"Upstream returned {exc.response.status_code} after {self._max_attempts} attempts."
            ) from exc
        except httpx.TransportError as exc:
            logger.error("http_transport_error", method=method, url=url, error=str(exc))
            raise ExternalServiceError("Upstream is unreachable.") from exc

        raise ExternalServiceError("Upstream request did not complete.")  # pragma: no cover

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("PUT", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> httpx.Response:
        return await self.request("DELETE", url, **kwargs)
