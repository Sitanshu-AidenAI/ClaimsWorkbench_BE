"""Rate limiting on the sign-in endpoint.

This did not exist before and did not need to. While sign-in was a redirect,
Keycloak's own login page absorbed credential stuffing and its brute-force
detector counted the failures. Moving the form into this application makes
`POST /auth/login` the front door, and a front door with no limit is a free
credential-stuffing oracle and a cheap way to lock every account on the desk.

Keycloak's per-user lockout still applies underneath — it counts failures whatever
grant produced them. What it cannot see is the shape of an attack that never
repeats a username: ten thousand accounts tried once each never trips a per-user
counter. That is the gap this closes.

Two counters, because they catch different attacks:

* **Per IP** — one source spraying many accounts. Keycloak is blind to this.
* **Per email** — many sources hammering one account. Keycloak would eventually
  lock the account, which is the attacker's goal if the account belongs to someone
  who needs to work today. Refusing earlier, without locking, is kinder.

A fixed window rather than a sliding one or a token bucket. A sliding window costs
a sorted set per key and a bucket costs a read-modify-write; the difference only
matters at the window boundary, where the worst case is twice the limit over two
windows. For a sign-in form that is not worth the machinery.

Counting happens on *failure* only, so somebody signing in and out repeatedly is
never throttled.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.core.config import AuthSettings, settings
from app.core.logging import get_logger
from app.services.cache import get_redis

logger = get_logger(__name__)

_PREFIX = "auth:login-attempts:"


@dataclass(frozen=True, slots=True)
class ThrottleVerdict:
    allowed: bool
    #: Seconds until the window rolls. Sent as `Retry-After` so a client that
    #: wants to behave is told how.
    retry_after: int = 0


class LoginThrottle:
    """Fixed-window attempt counters for the sign-in endpoint."""

    def __init__(self, config: AuthSettings | None = None) -> None:
        self._config = config or settings.auth

    async def check(self, *, ip: str | None, email: str) -> ThrottleVerdict:
        """Whether this attempt may proceed. Reads only — never counts."""
        for key in self._keys(ip=ip, email=email):
            redis = get_redis()
            raw = await redis.get(key)
            if raw is None:
                continue
            if int(raw) >= self._config.login_max_attempts:
                ttl = await redis.ttl(key)
                logger.warning("login_throttled", scope=key.rsplit(":", 1)[0])
                return ThrottleVerdict(
                    allowed=False,
                    retry_after=max(int(ttl), 1) if ttl and ttl > 0 else self._window,
                )
        return ThrottleVerdict(allowed=True)

    async def record_failure(self, *, ip: str | None, email: str) -> None:
        """Count a refused attempt against both scopes.

        `INCR` then `EXPIRE` only on the first increment, so the window is fixed
        from the first failure rather than sliding forward on every one — otherwise
        a steady trickle of attempts would keep the key alive for ever and the
        person would never be let back in.
        """
        redis = get_redis()
        for key in self._keys(ip=ip, email=email):
            count = int(await redis.incr(key))
            if count == 1:
                await redis.expire(key, self._window)

    async def clear(self, *, ip: str | None, email: str) -> None:
        """Forget the failures after a success.

        So a person who mistyped their password four times and then got it right
        starts from zero, rather than carrying a nearly-spent budget into a session
        they have already proved they own.
        """
        keys = self._keys(ip=ip, email=email)
        if keys:
            await get_redis().delete(*keys)

    @property
    def _window(self) -> int:
        return self._config.login_window_seconds

    def _keys(self, *, ip: str | None, email: str) -> list[str]:
        keys = [f"{_PREFIX}email:{_digest(email.strip().lower())}"]
        if ip:
            keys.append(f"{_PREFIX}ip:{_digest(ip)}")
        return keys


def _digest(value: str) -> str:
    """Hash the identifier before it becomes a Redis key.

    An email address is personal data and a key name ends up in `MONITOR` output,
    in a `KEYS` scan and in anyone's screenshot of redis-cli. The counter only ever
    needs equality, so the plaintext buys nothing.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]
