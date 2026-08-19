"""The OpenID Connect handshake with Keycloak.

This module speaks the protocol and makes no decisions with it: it builds an
authorize URL, exchanges a code for tokens, refreshes them, and hands back what
came off the wire. Whether a session may be created from that, how long it lives
and where it is stored belong to `app.services.auth`; whether a token is
acceptable belongs to `app.core.security` — which stays the single implementation
of verification, so nothing here validates a signature.

The split is the one `app.integrations.graph` draws against `app.services.mail`,
and for the same reason: a second identity provider later is a second module here
and no change anywhere above it.

Nothing in this file logs a token, a refresh token, a client secret or a PKCE
verifier. What it logs is that an exchange happened and how long the result is
good for, which is what an operator needs when sign-in stops working.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from app.core.config import Settings, settings
from app.core.errors import AuthenticationError, ExternalServiceError
from app.core.logging import get_logger
from app.integrations.http import HttpClient

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PkcePair:
    """A PKCE verifier and the challenge derived from it.

    The verifier stays with us and the challenge goes to Keycloak, so an attacker
    holding an intercepted authorization code cannot redeem it without the half
    they never saw. S256 only — `plain` is in the spec and is worth nothing.
    """

    verifier: str
    challenge: str

    @classmethod
    def generate(cls) -> PkcePair:
        verifier = secrets.token_urlsafe(64)
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return cls(verifier=verifier, challenge=challenge)


@dataclass(frozen=True, slots=True)
class TokenResponse:
    """What the token endpoint returned.

    `expires_in` is kept as the provider stated it rather than converted to an
    absolute instant here: the caller knows whether it is about to store this or
    hand it straight to a browser, and those two want different clocks.
    """

    access_token: str
    refresh_token: str | None
    id_token: str | None
    expires_in: int
    scope: str | None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> TokenResponse:
        access = payload.get("access_token")
        if not isinstance(access, str) or not access:
            raise AuthenticationError("The token response carried no access token.")

        return cls(
            access_token=access,
            refresh_token=_optional_str(payload.get("refresh_token")),
            id_token=_optional_str(payload.get("id_token")),
            # Defaulted rather than required: the spec makes expires_in optional,
            # and a provider that omits it is saying "ask me again soon", not
            # "this never expires".
            expires_in=_positive_int(payload.get("expires_in"), default=300),
            scope=_optional_str(payload.get("scope")),
        )


class OIDCClient:
    """Authorize-URL construction and token-endpoint calls against one realm."""

    def __init__(self, config: Settings | None = None) -> None:
        self._settings = config or settings
        self._keycloak = self._settings.keycloak
        self._auth = self._settings.auth
        self._http = HttpClient(
            self._keycloak.realm_url,
            timeout=httpx.Timeout(self._auth.timeout_seconds, connect=5.0),
            max_attempts=self._auth.max_attempts,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    @property
    def configured(self) -> bool:
        """Whether a code exchange is possible at all.

        Verification needs only the JWKS, which is public — but the
        authorization-code grant runs here as a *confidential* client, so a
        missing client secret means sign-in cannot work. The routes check this and
        say so plainly rather than failing later at the token endpoint.
        """
        return bool(self._keycloak.client_secret)

    def authorization_url(self, *, state: str, nonce: str, challenge: str) -> str:
        """Where to send the browser to authenticate."""
        query = urlencode(
            {
                "client_id": self._keycloak.client_id,
                "response_type": "code",
                "redirect_uri": self._auth.redirect_url,
                "scope": self._auth.scopes,
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self._keycloak.authorization_url}?{query}"

    def end_session_url(self, *, id_token: str | None = None) -> str:
        """Where to send the browser to end the realm's own SSO session.

        Keycloak, then straight back to the sign-in screen. Not the marketing root:
        somebody who has just signed out is at a keyboard and means either to sign in
        again or to hand the machine over, and landing them on the front page makes
        them go looking for the way back in.
        """
        params: dict[str, str] = {
            "post_logout_redirect_uri": self._settings.sign_in_url,
            "client_id": self._keycloak.client_id,
        }
        # With the id token Keycloak can end the specific session rather than
        # asking the person to confirm which one they meant.
        if id_token:
            params["id_token_hint"] = id_token
        return f"{self._keycloak.end_session_url}?{urlencode(params)}"

    async def exchange_code(self, *, code: str, verifier: str) -> TokenResponse:
        """Redeem an authorization code. One shot — codes are single-use."""
        return await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self._auth.redirect_url,
                "code_verifier": verifier,
            },
            operation="code_exchange",
        )

    async def password_grant(self, *, username: str, password: str) -> TokenResponse:
        """Exchange a username and password directly for tokens.

        OAuth calls this the resource-owner password credentials grant; Keycloak
        calls it the direct access grant. It exists so the sign-in form can live in
        our own application rather than on the realm's login page.

        Two things it cannot do, and neither is a limitation of this method: it
        cannot issue an MFA challenge, and it cannot complete a required action such
        as a forced password change. The grant has no way to ask the person a second
        question. Both remain reachable through the redirect flow above, which is
        why that path is kept rather than deleted.

        The password reaches Keycloak over the same client-authenticated request
        every other grant uses, and is never logged here or by the caller.
        """
        return await self._token_request(
            {
                "grant_type": "password",
                "username": username,
                "password": password,
                "scope": self._auth.scopes,
            },
            operation="password_grant",
        )

    async def refresh(self, refresh_token: str) -> TokenResponse:
        """Trade a refresh token for a fresh pair."""
        return await self._token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token},
            operation="refresh",
        )

    async def revoke(self, refresh_token: str) -> None:
        """Ask the realm to invalidate a refresh token.

        Best effort, deliberately. This runs from sign-out, where our own grant is
        already destroyed — the person is signed out of this application whatever
        the realm answers, and failing their logout because a revocation endpoint
        was briefly unreachable would be the wrong trade.
        """
        try:
            await self._http.post(
                "/protocol/openid-connect/revoke",
                data={
                    "client_id": self._keycloak.client_id,
                    "client_secret": self._keycloak.client_secret or "",
                    "token": refresh_token,
                    "token_type_hint": "refresh_token",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except (ExternalServiceError, httpx.HTTPError) as exc:
            logger.warning("oidc_revoke_failed", error=type(exc).__name__)

    async def _token_request(self, form: dict[str, str], *, operation: str) -> TokenResponse:
        payload = {
            **form,
            "client_id": self._keycloak.client_id,
            "client_secret": self._keycloak.client_secret or "",
        }
        try:
            response = await self._http.post(
                "/protocol/openid-connect/token",
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except ExternalServiceError as exc:
            logger.error("oidc_provider_unreachable", operation=operation)
            raise ExternalServiceError("The identity provider is unreachable.") from exc

        if response.status_code >= 400:
            detail = _error_description(response)
            logger.warning(
                "oidc_token_request_refused",
                operation=operation,
                status_code=response.status_code,
                detail=detail,
            )
            # A refused grant is not a 502. An expired refresh token and an
            # unreachable realm are different states, and the routes above answer
            # them differently — one asks the person to sign in again, the other
            # says the provider is down.
            raise AuthenticationError(f"The identity provider refused the request: {detail}")

        try:
            body = response.json()
        except ValueError as exc:
            raise ExternalServiceError("The identity provider returned a non-JSON body.") from exc

        tokens = TokenResponse.from_payload(body)
        logger.info("oidc_tokens_issued", operation=operation, expires_in=tokens.expires_in)
        return tokens


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _positive_int(value: object, *, default: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _error_description(response: httpx.Response) -> str:
    """The one sentence in the body worth having. Never carries a credential."""
    try:
        body = response.json()
    except ValueError:
        return f"HTTP {response.status_code}"
    detail = body.get("error_description") or body.get("error")
    if not isinstance(detail, str):
        return f"HTTP {response.status_code}"
    return detail.splitlines()[0][:300]


#: Process-wide, so the connection pool and its keep-alives are shared — the same
#: reasoning as `get_mail_client` and `get_document_store`.
_client: OIDCClient | None = None


def get_oidc_client(config: Settings | None = None) -> OIDCClient:
    global _client
    if _client is None:
        _client = OIDCClient(config)
    return _client


def set_oidc_client(client: OIDCClient | None) -> None:
    """Swap the process client. For tests."""
    global _client
    _client = client


async def close_oidc_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
