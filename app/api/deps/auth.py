"""Authentication dependencies.

`CurrentPrincipal` requires a valid token; `OptionalPrincipal` allows anonymous
access. `require_roles(...)` builds a dependency that additionally enforces role
membership.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import settings
from app.core.devauth import is_dev_token, principal_from_dev_token
from app.core.errors import AuthenticationError
from app.core.security import JWTVerifier, Principal, verifier

_bearer = HTTPBearer(auto_error=False, description="Keycloak-issued access token")
_optional_bearer = HTTPBearer(auto_error=False, description="Keycloak-issued access token")


def get_verifier() -> JWTVerifier:
    """Overridable in tests via `app.dependency_overrides`."""
    return verifier


def _resolve(token: str, jwt_verifier: JWTVerifier) -> Principal:
    """Verify a token, allowing the development format where it is enabled.

    The branch is on the token's own shape rather than on configuration alone, so
    a Keycloak token is still verified against the realm JWKS in an environment
    where development tokens happen to be accepted.
    """
    if is_dev_token(token):
        if not settings.dev_auth_enabled:
            raise AuthenticationError("Development tokens are not accepted in this environment.")
        return principal_from_dev_token(token, settings)
    return jwt_verifier.principal_from_token(token)


async def get_current_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    jwt_verifier: Annotated[JWTVerifier, Depends(get_verifier)],
) -> Principal:
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("An Authorization: Bearer <token> header is required.")
    if credentials.scheme.lower() != "bearer":
        raise AuthenticationError("Authorization scheme must be Bearer.")
    return _resolve(credentials.credentials, jwt_verifier)


async def get_optional_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_optional_bearer)],
    jwt_verifier: Annotated[JWTVerifier, Depends(get_verifier)],
) -> Principal | None:
    if credentials is None or not credentials.credentials:
        return None
    return _resolve(credentials.credentials, jwt_verifier)


CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]
OptionalPrincipal = Annotated[Principal | None, Depends(get_optional_principal)]


def require_roles(*roles: str) -> Callable[[Principal], Principal]:
    """Dependency factory enforcing that the caller holds at least one role."""

    def _dependency(principal: CurrentPrincipal) -> Principal:
        principal.require_role(*roles)
        return principal

    return _dependency
