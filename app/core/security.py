"""Keycloak JWT verification.

Tokens are verified in-app against the realm's JWKS — the API never calls
Keycloak's introspection endpoint on the request path. The JWKS is fetched once
and cached for `jwks_cache_ttl_seconds`; an unknown `kid` forces a single
refresh, which is what makes realm key rotation a non-event.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import jwt
from jwt import PyJWKClient
from jwt.exceptions import InvalidTokenError

from app.core.config import KeycloakSettings, settings
from app.core.errors import AuthenticationError, PermissionDeniedError
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class Principal:
    """The authenticated caller, projected out of the access token."""

    subject: str
    username: str | None = None
    email: str | None = None
    full_name: str | None = None
    realm_roles: frozenset[str] = field(default_factory=frozenset)
    client_roles: frozenset[str] = field(default_factory=frozenset)
    claims: dict[str, Any] = field(default_factory=dict)

    @property
    def roles(self) -> frozenset[str]:
        return self.realm_roles | self.client_roles

    def has_role(self, *roles: str) -> bool:
        return bool(self.roles.intersection(roles))

    def require_role(self, *roles: str) -> None:
        if not self.has_role(*roles):
            raise PermissionDeniedError(
                f"This action requires one of the following roles: {', '.join(sorted(roles))}."
            )


class JWTVerifier:
    """Verifies Keycloak-issued RS256 access tokens."""

    def __init__(self, config: KeycloakSettings | None = None) -> None:
        self._config = config or settings.keycloak
        self._jwk_client: PyJWKClient | None = None
        self._jwk_client_created_at: float = 0.0

    def _client(self) -> PyJWKClient:
        age = time.monotonic() - self._jwk_client_created_at
        expired = age > self._config.jwks_cache_ttl_seconds
        if self._jwk_client is None or expired:
            self._jwk_client = PyJWKClient(
                self._config.jwks_url,
                cache_keys=True,
                lifespan=self._config.jwks_cache_ttl_seconds,
            )
            self._jwk_client_created_at = time.monotonic()
        return self._jwk_client

    def reset_cache(self) -> None:
        self._jwk_client = None
        self._jwk_client_created_at = 0.0

    def decode(self, token: str) -> dict[str, Any]:
        """Verify signature, issuer, audience and expiry; return the claims."""
        try:
            signing_key = self._client().get_signing_key_from_jwt(token)
        except Exception:
            # An unknown kid usually means the realm rotated keys since we cached.
            logger.info("jwks_refresh_on_unknown_kid")
            self.reset_cache()
            try:
                signing_key = self._client().get_signing_key_from_jwt(token)
            except Exception as exc:
                raise AuthenticationError("Unable to resolve the token signing key.") from exc

        try:
            return jwt.decode(
                token,
                signing_key.key,
                algorithms=self._config.algorithms,
                issuer=self._config.issuer,
                audience=self._config.audience if self._config.verify_audience else None,
                leeway=self._config.leeway_seconds,
                options={
                    "verify_aud": self._config.verify_audience,
                    "require": ["exp", "iat", "sub", "iss"],
                },
            )
        except InvalidTokenError as exc:
            raise AuthenticationError(f"Invalid access token: {exc}") from exc

    def principal_from_token(self, token: str) -> Principal:
        return principal_from_claims(self.decode(token), client_id=self._config.client_id)


def principal_from_claims(claims: dict[str, Any], *, client_id: str) -> Principal:
    """Project Keycloak's claim layout onto a `Principal`."""
    realm_roles = frozenset(claims.get("realm_access", {}).get("roles", []))
    resource_access = claims.get("resource_access", {})
    client_roles = frozenset(resource_access.get(client_id, {}).get("roles", []))

    subject = claims.get("sub")
    if not subject:
        raise AuthenticationError("Access token is missing the `sub` claim.")

    return Principal(
        subject=subject,
        username=claims.get("preferred_username"),
        email=claims.get("email"),
        full_name=claims.get("name"),
        realm_roles=realm_roles,
        client_roles=client_roles,
        claims=claims,
    )


verifier = JWTVerifier()
