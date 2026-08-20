"""Where the refresh token lives.

The browser never holds one. It holds an opaque handle in an `HttpOnly` cookie,
and this module is the only thing that can turn a handle back into a credential.
That is the whole point of the design: an XSS can use the session while the tab is
open, because it can call the API with the in-memory access token, but it cannot
walk away with anything that outlives the tab.

Three properties are worth relying on.

**Rotation.** Every refresh issues a new handle and retires the old one. A cookie
captured off the wire is therefore only useful until the legitimate tab refreshes
next, which on a live desk is minutes.

**Reuse detection.** A retired handle presented again means two parties hold the
same cookie, and there is no innocent explanation that is worth the risk of being
wrong about. The whole family is destroyed and the person signs in again. This is
what makes rotation worth doing rather than merely re-issuing the same token: a
stolen cookie either goes unused or announces itself.

**Two clocks.** A grant expires after `idle_timeout_seconds` without use *and* at
`absolute_timeout_seconds` from creation whatever happens. A claims desk leaves
tabs open all day, so an idle bound alone would never fire; an absolute bound
alone would sign someone out mid-sentence. Redis enforces the idle bound as the
key TTL, and the absolute deadline is stored in the value and checked on read.

Nothing here is ever logged: not a handle, not a refresh token, not a family id.
What is logged is that a grant was created, rotated or destroyed, and — loudly —
that a reuse was detected.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.config import AuthSettings, settings
from app.core.logging import get_logger
from app.services.cache import get_redis

logger = get_logger(__name__)

#: Two namespaces, because they answer different questions. `grant:` maps a live
#: handle to its credential; `family:` is the set of handles ever issued to one
#: sign-in, which is what makes revoking a compromised session possible without
#: knowing which handle the attacker holds.
_GRANT_PREFIX = "auth:grant:"
_FAMILY_PREFIX = "auth:family:"

#: How long a retired handle stays known after rotation. It has to outlive the
#: window in which a stolen copy might be replayed, or reuse detection silently
#: degrades into "handle not found" and the theft goes unnoticed.
_RETIRED_TTL_SECONDS = 3600


class GrantReuseError(Exception):
    """A retired handle was presented. The family has been destroyed."""


@dataclass(frozen=True, slots=True)
class Grant:
    """A live refresh grant."""

    handle: str
    refresh_token: str
    subject: str
    family: str
    #: The id token from the original sign-in, kept only so sign-out can pass
    #: `id_token_hint` to Keycloak and end the specific SSO session.
    id_token: str | None
    created_at: datetime
    expires_at: datetime
    #: Which door this session came through: `"password"` or `"sso"`.
    #:
    #: Recorded because sign-out behaves differently for each. An SSO session leaves a
    #: cookie on the realm's own origin, and only a browser navigation to Keycloak can
    #: clear it. A password session never touched Keycloak in the browser at all — the
    #: grant was exchanged server to server — so there is nothing there to clear, and
    #: sending the browser on that round trip is a page reload that achieves nothing.
    #:
    #: Last in the list only because a defaulted dataclass field cannot precede an
    #: undefaulted one.
    via: str = "password"

    @property
    def seconds_remaining(self) -> int:
        return max(0, int((self.expires_at - datetime.now(UTC)).total_seconds()))


class RefreshGrantStore:
    """Redis-backed refresh grants, with rotation and reuse detection."""

    def __init__(self, config: AuthSettings | None = None) -> None:
        self._config = config or settings.auth

    async def create(
        self,
        *,
        refresh_token: str,
        subject: str,
        id_token: str | None = None,
        via: str = "password",
    ) -> Grant:
        """Open a new grant family for a fresh sign-in."""
        family = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        expires_at = now.fromtimestamp(
            now.timestamp() + self._config.absolute_timeout_seconds, tz=UTC
        )
        grant = Grant(
            handle=secrets.token_urlsafe(32),
            refresh_token=refresh_token,
            subject=subject,
            family=family,
            id_token=id_token,
            via=via,
            created_at=now,
            expires_at=expires_at,
        )
        await self._write(grant)
        logger.info("auth_grant_created", subject=subject)
        return grant

    async def get(self, handle: str) -> Grant | None:
        """Resolve a handle, or `None` when it names nothing usable.

        Raises `GrantReuseError` when the handle is *known but retired*, which is
        a different thing from unknown and has to be treated as a compromise
        rather than as an expiry.
        """
        redis = get_redis()
        raw = await redis.get(_grant_key(handle))
        if raw is None:
            return None

        payload = _loads(raw)
        if payload is None:
            await redis.delete(_grant_key(handle))
            return None

        if payload.get("retired"):
            family = str(payload.get("family") or "")
            logger.error("auth_grant_reuse_detected", subject=payload.get("subject"))
            await self.destroy_family(family)
            raise GrantReuseError("This session credential has already been used.")

        grant = _grant_from_payload(handle, payload)
        if grant is None:
            await redis.delete(_grant_key(handle))
            return None

        # The absolute deadline is not the key TTL — the key TTL is the idle bound
        # and is pushed forward on every use — so it is checked here.
        if grant.expires_at <= datetime.now(UTC):
            logger.info("auth_grant_expired_absolute", subject=grant.subject)
            await self.destroy_family(grant.family)
            return None

        return grant

    async def rotate(self, grant: Grant, *, refresh_token: str) -> Grant:
        """Retire this handle and issue its successor, keeping the family."""
        rotated = Grant(
            handle=secrets.token_urlsafe(32),
            refresh_token=refresh_token,
            subject=grant.subject,
            family=grant.family,
            id_token=grant.id_token,
            via=grant.via,
            created_at=grant.created_at,
            expires_at=grant.expires_at,
        )
        await self._write(rotated)
        await self._retire(grant)
        return rotated

    async def destroy(self, handle: str) -> None:
        """Drop one handle. Used by sign-out, which also ends the family."""
        await get_redis().delete(_grant_key(handle))

    async def destroy_family(self, family: str) -> None:
        """Revoke every handle ever issued to one sign-in.

        What sign-out calls, and what reuse detection calls. Sign-out could delete
        only the handle it was given, but a session being deliberately ended is
        exactly when any other copy of it should stop working too.
        """
        if not family:
            return
        redis = get_redis()
        handles = await redis.smembers(_family_key(family))
        keys = [_grant_key(handle) for handle in handles]
        if keys:
            await redis.delete(*keys)
        await redis.delete(_family_key(family))
        logger.info("auth_grant_family_destroyed", handles=len(keys))

    async def _write(self, grant: Grant) -> None:
        redis = get_redis()
        # The idle bound, never longer than what is left of the absolute one — so a
        # grant cannot be kept alive past its deadline by being used often.
        ttl = min(self._config.idle_timeout_seconds, grant.seconds_remaining)
        payload = {
            "refresh_token": grant.refresh_token,
            "subject": grant.subject,
            "family": grant.family,
            "id_token": grant.id_token,
            "via": grant.via,
            "created_at": grant.created_at.isoformat(),
            "expires_at": grant.expires_at.isoformat(),
            "retired": False,
        }
        pipe = redis.pipeline()
        pipe.set(_grant_key(grant.handle), json.dumps(payload), ex=max(ttl, 1))
        pipe.sadd(_family_key(grant.family), grant.handle)
        pipe.expire(_family_key(grant.family), self._config.absolute_timeout_seconds)
        await pipe.execute()

    async def _retire(self, grant: Grant) -> None:
        """Keep the old handle known, but poisoned.

        Deleting it would be tidier and worse: the next presentation would read as
        "unknown handle", which is what an expiry looks like, and a stolen cookie
        would expire quietly instead of raising an alarm.
        """
        redis = get_redis()
        await redis.set(
            _grant_key(grant.handle),
            json.dumps({"retired": True, "family": grant.family, "subject": grant.subject}),
            ex=_RETIRED_TTL_SECONDS,
        )


def _grant_key(handle: str) -> str:
    return f"{_GRANT_PREFIX}{handle}"


def _family_key(family: str) -> str:
    return f"{_FAMILY_PREFIX}{family}"


def _loads(raw: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _grant_from_payload(handle: str, payload: dict[str, Any]) -> Grant | None:
    refresh_token = payload.get("refresh_token")
    subject = payload.get("subject")
    if not isinstance(refresh_token, str) or not isinstance(subject, str):
        return None

    try:
        created_at = datetime.fromisoformat(str(payload["created_at"]))
        expires_at = datetime.fromisoformat(str(payload["expires_at"]))
    except (KeyError, TypeError, ValueError):
        return None

    id_token = payload.get("id_token")
    # Defaulted to `sso` rather than `password` for a row written before this field
    # existed. Getting it wrong costs either a needless page reload or a realm session
    # left alive, and only one of those is a security problem.
    via = payload.get("via")
    return Grant(
        handle=handle,
        refresh_token=refresh_token,
        subject=subject,
        family=str(payload.get("family") or ""),
        id_token=id_token if isinstance(id_token, str) else None,
        via=via if isinstance(via, str) else "sso",
        created_at=created_at,
        expires_at=expires_at,
    )
