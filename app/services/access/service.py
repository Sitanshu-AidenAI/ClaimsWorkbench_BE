"""What this caller may reach.

The one place that turns a principal's realm roles into capabilities. Everything
that authorises — the `require_capability` dependency, the session response the SPA
renders its rail from, the matrix board — goes through here, so there is a single
answer to "may they?" rather than one per call site.

**Cached in Redis, invalidated on write.** The matrix is a dozen rows read on every
authorised request and changed a few times a year, which is the shape a cache is for.
Invalidation is explicit rather than TTL-only because the thing being cached is an
authorisation decision: a TTL alone would mean a revoked capability keeping working
for the length of the TTL, and "we removed their access and it took five minutes" is
not an acceptable answer about access. The TTL stays as a backstop for a cache that
was never invalidated because the process that wrote died first.

The fallback matters as much as the cache. If the table is unreadable or empty, the
documented defaults in `app.domain.capabilities` apply — a desk whose grant table
failed to seed should behave as documented, not lock every person out of every board.
"""

from __future__ import annotations

import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.domain.capabilities import (
    DEFAULT_MATRIX,
    Capability,
    capabilities_for,
)
from app.repositories.access import AccessRepository
from app.services.cache import get_redis

logger = get_logger(__name__)

_CACHE_KEY = "access:matrix:v1"
#: Long, because the cache is invalidated explicitly on every write. This only
#: bounds how stale a cache can get if the invalidation itself was lost.
_CACHE_TTL_SECONDS = 3600


class AccessService:
    def __init__(self, session: AsyncSession) -> None:
        self._repository = AccessRepository(session)

    # -- Reads -----------------------------------------------------------------

    async def matrix(self) -> dict[str, set[str]]:
        """Every grant, from the cache where possible."""
        cached = await self._cached()
        if cached is not None:
            return cached

        grants = await self._repository.matrix()
        if not grants:
            # Nothing granted at all means the seed has not run. Serve the documented
            # defaults rather than an empty matrix, and do not cache them — the next
            # read should notice once the seed lands.
            logger.warning("access_matrix_empty_using_defaults")
            return {role.value: {c.value for c in caps} for role, caps in DEFAULT_MATRIX.items()}

        await self._store(grants)
        return grants

    async def capabilities_for_roles(self, roles: frozenset[str]) -> frozenset[Capability]:
        """The union of what these roles allow.

        A union, not a precedence order: somebody holding two personas can do what
        either allows, which is what granting a second role means. A ranking would
        make a manager who also holds the officer role *lose* intake, which nobody
        would predict.

        Unknown capability strings are dropped rather than raising. The table may name
        one this build has removed, and a request path is the wrong place to discover
        that.
        """
        try:
            grants = await self.matrix()
        except Exception as exc:
            # Postgres or Redis unreachable. Falling back to the documented defaults
            # keeps the desk working with the access it is supposed to have, which is
            # a better failure than every screen going dark.
            logger.error("access_matrix_unreadable", error=type(exc).__name__, detail=str(exc))
            return capabilities_for(roles)

        granted: set[Capability] = set()
        for role in roles:
            for value in grants.get(role, ()):
                try:
                    granted.add(Capability(value))
                except ValueError:
                    continue
        return frozenset(granted)

    # -- Writes ----------------------------------------------------------------

    async def replace_role(
        self, role: str, capabilities: set[str], *, granted_by: str | None
    ) -> None:
        """Set one persona's whole column, then drop the cache."""
        await self._repository.replace_role(role, capabilities, granted_by=granted_by)
        await self.invalidate()
        logger.info("access_matrix_updated", role=role, capabilities=len(capabilities))

    async def seed(self) -> int:
        """Install the documented defaults, but only into an empty table.

        **Only when empty, and that is a correction rather than a shortcut.** The
        first version of this seeded unconditionally with `ON CONFLICT DO NOTHING`,
        on the reasoning that an additive insert could not undo anything. It could:
        conflict-do-nothing declines to overwrite a row that *exists*, and a grant an
        administrator has revoked does not exist. The next boot found no conflict and
        put it back — silently re-granting access somebody had deliberately removed.
        `tests/integration/test_access_matrix_flow.py` is what caught it.

        The trade this makes is deliberate. A release that adds a new capability no
        longer delivers it to a deployment that has already been seeded, so an
        administrator has to grant it on the board — or a migration has to, which is
        the right place for a decision about who may reach something new. That is an
        inconvenience. Re-granting revoked access is a security regression, and
        between the two there is no contest.

        Returns the number of grants in the table afterwards, seeded or not.
        """
        if not await self._repository.is_empty():
            total = await self._repository.count()
            logger.info("access_matrix_ready", grants=total, seeded=False)
            return total

        matrix = {role.value: {c.value for c in caps} for role, caps in DEFAULT_MATRIX.items()}
        total = await self._repository.seed(matrix)
        await self.invalidate()
        logger.info("access_matrix_ready", grants=total, seeded=True)
        return total

    async def invalidate(self) -> None:
        """Drop the cached matrix.

        Never raises. A cache that could not be cleared must not fail the write that
        cleared it — the TTL bounds the damage, and a write that rolled back because
        Redis blinked would be worse than a matrix that is briefly stale.
        """
        try:
            await get_redis().delete(_CACHE_KEY)
        except Exception as exc:
            logger.warning("access_cache_not_invalidated", error=type(exc).__name__)

    # -- Cache -----------------------------------------------------------------

    async def _cached(self) -> dict[str, set[str]] | None:
        try:
            raw = await get_redis().get(_CACHE_KEY)
        except Exception:
            # Redis down is not an error here; the database is the source of truth and
            # the caller is about to read it.
            return None
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        return {role: set(caps) for role, caps in payload.items() if isinstance(caps, list)}

    async def _store(self, grants: dict[str, set[str]]) -> None:
        try:
            await get_redis().set(
                _CACHE_KEY,
                json.dumps({role: sorted(caps) for role, caps in grants.items()}),
                ex=_CACHE_TTL_SECONDS,
            )
        except Exception as exc:
            logger.warning("access_cache_not_written", error=type(exc).__name__)
