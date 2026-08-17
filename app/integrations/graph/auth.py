"""Client-credentials tokens for Microsoft Graph.

One token per process, refreshed a minute before it expires and re-fetched when
Graph says it is no longer good enough. There is no user in this flow and no
refresh token: an access token that has expired is simply asked for again, which
is why nothing above this module ever has to think about token lifetime.

The secret is read from settings, sent to the token endpoint, and never logged —
neither is the token itself. What is logged is that a token was obtained and how
long it lasts, which is what an operator needs when intake stops working.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from app.core.config import GraphSettings, settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger
from app.integrations.graph.errors import GraphAuthError
from app.integrations.http import HttpClient

logger = get_logger(__name__)

#: Refresh this long before expiry, so a token never expires mid-request.
EXPIRY_MARGIN_SECONDS = 60.0


class GraphTokenProvider:
    """Fetches and caches an application token for the Graph API."""

    def __init__(self, config: GraphSettings | None = None) -> None:
        self._config = config or settings.graph
        if not self._config.configured:
            raise ValueError(
                "GraphTokenProvider requires a tenant id, client id, client secret and mailbox."
            )
        self._http = HttpClient(
            self._config.authority,
            timeout=httpx.Timeout(self._config.timeout_seconds, connect=10.0),
            max_attempts=self._config.http_max_attempts,
        )
        self._token: str | None = None
        self._expires_at: float = 0.0
        # Concurrent callers must not each mint a token: the first one through
        # fetches, the rest wait and find it cached.
        self._lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    def invalidate(self) -> None:
        """Drop the cached token. Called when Graph answers 401 with it."""
        self._token = None
        self._expires_at = 0.0

    async def token(self) -> str:
        """A valid access token, minting one if the cached token is spent."""
        if self._token is not None and time.monotonic() < self._expires_at:
            return self._token

        async with self._lock:
            # Re-checked inside the lock: whoever held it may have just refreshed.
            if self._token is not None and time.monotonic() < self._expires_at:
                return self._token
            return await self._fetch()

    async def _fetch(self) -> str:
        path = f"/{self._config.tenant_id}/oauth2/v2.0/token"
        try:
            response = await self._http.post(
                path,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self._config.client_id,
                    "client_secret": self._config.client_secret,
                    "scope": self._config.scope,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except ExternalServiceError as exc:
            logger.error("graph_token_unreachable", tenant=self._config.tenant_id)
            raise GraphAuthError("The Microsoft identity platform is unreachable.") from exc

        if response.status_code >= 400:
            # The body names the reason (AADSTS…), which is the one thing worth
            # having in the log; it contains no secret.
            detail = _error_description(response)
            logger.error(
                "graph_token_rejected",
                status_code=response.status_code,
                tenant=self._config.tenant_id,
                detail=detail,
            )
            raise GraphAuthError(
                f"The token request was refused: {detail}",
                status_code=response.status_code,
            )

        payload = response.json()
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise GraphAuthError("The token response carried no access token.")

        expires_in = float(payload.get("expires_in") or 3600)
        self._token = token
        self._expires_at = time.monotonic() + max(expires_in - EXPIRY_MARGIN_SECONDS, 0.0)
        logger.info("graph_token_acquired", expires_in_seconds=int(expires_in))
        return token


def _error_description(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    description = body.get("error_description") or body.get("error")
    if not isinstance(description, str):
        return f"HTTP {response.status_code}"
    # The description is a paragraph with a correlation id and a timestamp in it;
    # the first line is the sentence that says what is wrong.
    return description.splitlines()[0][:300]
