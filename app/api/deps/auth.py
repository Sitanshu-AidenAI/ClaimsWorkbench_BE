"""Authentication dependencies.

`CurrentPrincipal` requires a valid token; `OptionalPrincipal` allows anonymous
access. `require_roles(...)` builds a dependency that additionally enforces role
membership.

**There is exactly one credential this API accepts: a Keycloak-issued access
token, verified against the realm JWKS.** There is no second format, no
development bypass and no cookie branch — the browser's session cookie is read by
`app.api.v1.routes.auth` alone, and what it hands back is one of these tokens. So
every authenticated route in this API is reached the same way, whether the caller
is a browser, a test, a `curl` or a future service account.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.deps.db import SessionDep
from app.core.errors import AuthenticationError, PermissionDeniedError
from app.core.security import JWTVerifier, Principal, verifier
from app.domain.capabilities import Capability
from app.services.access.service import AccessService

_bearer = HTTPBearer(auto_error=False, description="Keycloak-issued access token")
_optional_bearer = HTTPBearer(auto_error=False, description="Keycloak-issued access token")


def get_verifier() -> JWTVerifier:
    """Overridable in tests via `app.dependency_overrides`."""
    return verifier


def _resolve(token: str, jwt_verifier: JWTVerifier) -> Principal:
    """Verify a token. One path, no alternatives."""
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


def require_capability(*capabilities: Capability) -> Callable[..., Awaitable[Principal]]:
    """Dependency factory enforcing that the caller holds at least one capability.

    The counterpart to `require_roles`, and the one to reach for on anything an
    administrator should be able to re-delegate. `require_roles` names the persona
    directly, which is right for a rule the business does not get to change; this
    names what the caller is trying to *do*, and the matrix decides which personas
    may do it.

    Reads through `AccessService`, so the answer comes from `role_capabilities` with
    the documented defaults as a fallback — and from Redis on all but the first call
    after a change.
    """

    async def _dependency(principal: CurrentPrincipal, session: SessionDep) -> Principal:
        held = await AccessService(session).capabilities_for_roles(principal.roles)
        if not held.intersection(capabilities):
            raise PermissionDeniedError(
                "This action requires one of the following: "
                + ", ".join(sorted(c.value for c in capabilities))
                + "."
            )
        return principal

    return _dependency
