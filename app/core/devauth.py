"""Development access tokens.

Keycloak is the only identity provider this service trusts. It is also a
container that has to be running, a realm that has to be bootstrapped and a
redirect flow that has to complete — none of which is available in a unit test,
a CI job or a laptop running only Postgres and Redis.

This module accepts a second, deliberately unsigned token format in those
environments so the API can still be exercised end to end:

    Authorization: Bearer dev.<base64url(json)>

where the JSON carries `sub`, and optionally `username`, `email`, `name` and
`roles`. It is *not* a security mechanism and does not pretend to be one — it is
enabled only when `settings.dev_auth_enabled` is true, which is false in staging
and production regardless of how the flag is set. Authorisation itself is
unchanged: the roles in the token flow through the same `Principal` and the same
`require_roles` checks as a Keycloak token's would, so a permission test written
against a dev token is testing the real rule.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

from app.core.config import Settings, settings
from app.core.errors import AuthenticationError
from app.core.security import Principal

DEV_TOKEN_PREFIX = "dev."


def is_dev_token(token: str) -> bool:
    return token.startswith(DEV_TOKEN_PREFIX)


def _decode_segment(segment: str) -> dict[str, Any]:
    padded = segment + "=" * (-len(segment) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise AuthenticationError("The development token is not valid base64url JSON.") from exc

    if not isinstance(payload, dict):
        raise AuthenticationError("The development token payload must be a JSON object.")
    return payload


def principal_from_dev_token(token: str, config: Settings | None = None) -> Principal:
    """Project a development token onto the same `Principal` Keycloak produces."""
    config = config or settings
    if not config.dev_auth_enabled:
        raise AuthenticationError("Development tokens are not accepted in this environment.")

    payload = _decode_segment(token.removeprefix(DEV_TOKEN_PREFIX))

    subject = payload.get("sub") or payload.get("subject")
    if not isinstance(subject, str) or not subject:
        raise AuthenticationError("The development token is missing a `sub` claim.")

    raw_roles = payload.get("roles")
    roles = (
        frozenset(str(role) for role in raw_roles)
        if isinstance(raw_roles, list) and raw_roles
        else frozenset(config.dev_auth_default_roles)
    )

    return Principal(
        subject=subject,
        username=_optional_str(payload.get("username")),
        email=_optional_str(payload.get("email")),
        full_name=_optional_str(payload.get("name") or payload.get("full_name")),
        realm_roles=roles,
        client_roles=frozenset(),
        claims={"sub": subject, "dev": True},
    )


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
