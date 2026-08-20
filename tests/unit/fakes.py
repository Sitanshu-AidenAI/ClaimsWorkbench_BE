"""Test doubles shared by the unit suites.

Extracted rather than duplicated. Two suites now stand a Redis up — the session
store and the access matrix — and a second copy is where the two quietly stop
agreeing about what the real client does.
"""

from __future__ import annotations

from typing import Any


class FakeRedis:
    """Enough of the client for the code under test: strings, sets, TTLs, a pipeline.

    **TTLs are recorded, not enforced.** Nothing here expires on a clock, because a
    test that waited for an expiry would be slow and flaky in exchange for testing
    Redis rather than us. Every expiry the suites care about is asserted either by
    reading `ttls` — "was the right bound written?" — or by moving a deadline stored
    in the value, which is where the absolute session bound actually lives.

    Failure modes are opt-in rather than simulated by default: set `fail` to make
    every command raise, which is how the access service's "Redis is down, fall back
    to the database" path is exercised.
    """

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}
        self.ttls: dict[str, int] = {}
        #: When true every command raises, standing in for an unreachable Redis.
        self.fail = False
        #: Every command issued, in order. Lets a test assert a cache was *not* read
        #: rather than only that the answer was right.
        self.calls: list[str] = []

    def _record(self, name: str) -> None:
        self.calls.append(name)
        if self.fail:
            raise ConnectionError("fake redis is unavailable")

    async def get(self, key: str) -> str | None:
        self._record("get")
        return self.values.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._record("set")
        self.values[key] = value
        if ex is not None:
            self.ttls[key] = ex

    async def delete(self, *keys: str) -> int:
        self._record("delete")
        removed = 0
        for key in keys:
            removed += 1 if self.values.pop(key, None) is not None else 0
            self.sets.pop(key, None)
        return removed

    async def sadd(self, key: str, *members: str) -> None:
        self._record("sadd")
        self.sets.setdefault(key, set()).update(members)

    async def smembers(self, key: str) -> set[str]:
        self._record("smembers")
        return set(self.sets.get(key, set()))

    async def expire(self, key: str, seconds: int) -> None:
        self._record("expire")
        self.ttls[key] = seconds

    async def incr(self, key: str) -> int:
        self._record("incr")
        nxt = int(self.values.get(key, "0")) + 1
        self.values[key] = str(nxt)
        return nxt

    async def ttl(self, key: str) -> int:
        self._record("ttl")
        # -2 is what redis answers for a key that does not exist, which is the value
        # the throttle has to cope with when a window has already rolled.
        return self.ttls.get(key, -2)

    def pipeline(self) -> FakePipeline:
        return FakePipeline(self)


class FakePipeline:
    """Queues the calls and replays them on `execute`, like the real client.

    Queuing rather than executing eagerly matters: the session store relies on the
    set, the sadd and the expire landing together, and a pipeline that ran each call
    as it was queued would pass a test the real one would fail.
    """

    def __init__(self, redis: FakeRedis) -> None:
        self._redis = redis
        self._queued: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._queued.append(("set", (key, value), {"ex": ex}))

    def sadd(self, key: str, *members: str) -> None:
        self._queued.append(("sadd", (key, *members), {}))

    def expire(self, key: str, seconds: int) -> None:
        self._queued.append(("expire", (key, seconds), {}))

    async def execute(self) -> None:
        for name, args, kwargs in self._queued:
            await getattr(self._redis, name)(*args, **kwargs)
        self._queued.clear()


class FakeAccessRepository:
    """The grant table, in a dict.

    Stands in for `AccessRepository` so the service's caching, invalidation and
    fallback behaviour can be tested without Postgres. The repository's own SQL is
    covered against a real database in `tests/integration/test_access_matrix_flow.py`
    — `ON CONFLICT DO NOTHING` and a delete-then-insert inside one transaction are
    exactly the things a dict cannot tell you the truth about.
    """

    def __init__(self, grants: dict[str, set[str]] | None = None) -> None:
        self.grants: dict[str, set[str]] = {k: set(v) for k, v in (grants or {}).items()}
        self.replaced: list[tuple[str, set[str], str | None]] = []
        self.seed_calls = 0
        #: When true every read raises, standing in for an unreachable database.
        self.fail = False

    async def matrix(self) -> dict[str, set[str]]:
        if self.fail:
            raise ConnectionError("fake database is unavailable")
        return {role: set(caps) for role, caps in self.grants.items()}

    async def is_empty(self) -> bool:
        return not self.grants

    async def replace_role(
        self, role: str, capabilities: set[str], *, granted_by: str | None
    ) -> None:
        self.replaced.append((role, set(capabilities), granted_by))
        if capabilities:
            self.grants[role] = set(capabilities)
        else:
            self.grants.pop(role, None)

    async def count(self) -> int:
        return sum(len(caps) for caps in self.grants.values())

    async def seed(self, matrix: dict[str, set[str]], *, granted_by: str | None = None) -> int:
        """Insert-or-ignore per pair, exactly like `ON CONFLICT DO NOTHING`.

        Including the sharp edge: a pair that was *deleted* does not conflict, so this
        puts it back. The fake models that faithfully on purpose — a kinder fake here
        would have hidden the bug the integration suite found, and the whole point of a
        double is that it fails where the real thing fails.
        """
        self.seed_calls += 1
        for role, capabilities in matrix.items():
            self.grants.setdefault(role, set()).update(capabilities)
        return await self.count()


class FakeSession:
    """A stand-in for `AsyncSession` that records its transaction boundaries.

    The routes take a session only to hand it to a service, and the unit suites fake
    the service — so nothing here needs to execute SQL. What it does need is `commit`,
    because a route that forgets to call it is a real bug (`POST /access/matrix` did),
    and a session override of `None` turns that route into an `AttributeError` rather
    than a passing test.

    `commits` is exposed so a unit test can assert the boundary was reached. It cannot
    tell you the rows are durable, which is why the missing commit was caught by an
    integration test reading through a second connection — but it can tell you the call
    was made, which is enough to stop it silently disappearing again.
    """

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def flush(self) -> None:
        return None
