"""Outbound HTTP.

A shared `httpx.AsyncClient` per base URL with connection pooling, plus a
tenacity retry policy that retries only what is safe to retry: transport errors
and 429/5xx responses.
"""

from __future__ import annotations

from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from app.core.errors import ExternalServiceError
from app.core.logging import get_logger

logger = get_logger(__name__)

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
DEFAULT_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20)


class RetryableStatusError(Exception):
    """Raised internally to feed a retryable response back into tenacity."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        super().__init__(f"Retryable status {response.status_code} from {response.request.url}")


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
            wait=wait_exponential_jitter(initial=0.2, max=5.0),
            retry=retry_if_exception_type((httpx.TransportError, RetryableStatusError)),
            reraise=True,
        )

        try:
            async for attempt in retrying:
                with attempt:
                    response = await self._client.request(method, url, **kwargs)
                    if response.status_code in RETRYABLE_STATUS:
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
