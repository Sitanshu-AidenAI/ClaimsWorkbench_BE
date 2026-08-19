"""The access matrix against a real database.

Marked `integration` because it needs Postgres. The unit tests fake the repository so
they can cover the service's caching, invalidation and fallbacks cheaply; this covers
what a dict cannot tell the truth about:

* a boot-time seed neither failing on an existing row nor resurrecting a grant an
  administrator removed — the second of those was a real bug this file found, because
  `ON CONFLICT DO NOTHING` does not protect a row that has been *deleted*;
* the composite primary key really refusing a duplicate pair;
* `replace_role` really being a delete-then-insert inside one transaction, so a column
  is never briefly empty to a concurrent reader.

Redis is faked, because the cache is not what is under test here and a test that needed
a second service to be up would be one more reason for the suite not to run.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import delete, select

from app.core.config import settings
from app.db.session import dispose_engine, get_session_factory, init_engine
from app.domain.capabilities import DEFAULT_MATRIX, Capability
from app.domain.enums import Role
from app.models.access import RoleCapability
from app.repositories.access import AccessRepository
from app.services.access.service import AccessService
from tests.unit.fakes import FakeRedis

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture(autouse=True)
async def _engine() -> AsyncIterator[None]:
    await init_engine(settings)
    yield
    await dispose_engine()


@pytest.fixture(autouse=True)
def _redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr("app.services.access.service.get_redis", lambda: fake)
    return fake


@pytest.fixture
async def session() -> AsyncIterator[object]:
    """A session on a table emptied either side of the test.

    Emptied *before* so each test starts from a known state, and after so one test
    cannot seed the next.

    A developer running this against their own stack is left with an empty table, and
    that is deliberate rather than careless: `AccessService.seed` installs the
    documented defaults into an empty table, so the next API start restores the matrix.
    Leaving rows behind would be the worse outcome — the seed skips a populated table,
    so a half-finished test run would become a matrix nobody chose and nothing would
    correct it.
    """
    factory = get_session_factory()
    async with factory() as db:
        await db.execute(delete(RoleCapability))
        await db.commit()
        yield db
        await db.execute(delete(RoleCapability))
        await db.commit()


async def _rows(db: object) -> set[tuple[str, str]]:
    result = await db.execute(select(RoleCapability.role, RoleCapability.capability))  # type: ignore[attr-defined]
    return {(role, capability) for role, capability in result}


async def test_a_grant_round_trips_through_postgres(session: object) -> None:
    repository = AccessRepository(session)  # type: ignore[arg-type]

    await repository.replace_role(
        "claims-handler", {"page:claims", "page:policies"}, granted_by="admin-1"
    )
    await session.commit()  # type: ignore[attr-defined]

    assert await repository.matrix() == {"claims-handler": {"page:claims", "page:policies"}}


async def test_the_seed_installs_every_documented_grant(session: object) -> None:
    repository = AccessRepository(session)  # type: ignore[arg-type]
    matrix = {role.value: {c.value for c in caps} for role, caps in DEFAULT_MATRIX.items()}

    total = await repository.seed(matrix)
    await session.commit()  # type: ignore[attr-defined]

    expected = sum(len(caps) for caps in matrix.values())
    assert total == expected
    assert len(await _rows(session)) == expected


async def test_seeding_twice_inserts_nothing_the_second_time(session: object) -> None:
    """`ON CONFLICT DO NOTHING`, proved rather than assumed.

    The seed runs on every boot. If the conflict clause were wrong this would raise a
    unique-violation and take the application down on its second start.
    """
    repository = AccessRepository(session)  # type: ignore[arg-type]
    matrix = {role.value: {c.value for c in caps} for role, caps in DEFAULT_MATRIX.items()}

    first = await repository.seed(matrix)
    await session.commit()  # type: ignore[attr-defined]
    second = await repository.seed(matrix)
    await session.commit()  # type: ignore[attr-defined]

    assert first == second


async def test_a_revoked_grant_survives_a_restart(session: object) -> None:
    """Through `AccessService`, which is where the guard lives.

    The repository's `seed` is deliberately dumb — it issues the insert it is given.
    The decision *not* to issue it against a populated table belongs to the service,
    so this goes through the service, which is also the path the boot sequence takes.

    An earlier version of this test called the repository directly and failed, which is
    what surfaced the bug: `ON CONFLICT DO NOTHING` declines to overwrite a row that
    exists, and a revoked row does not exist, so the seed put it back.
    """
    service = AccessService(session)  # type: ignore[arg-type]
    await service.seed()
    await session.commit()  # type: ignore[attr-defined]

    remaining = {
        c.value for c in DEFAULT_MATRIX[Role.CLAIMS_MANAGER] if c is not Capability.PAGE_ANALYTICS
    }
    await service.replace_role(Role.CLAIMS_MANAGER.value, remaining, granted_by="admin-1")
    await session.commit()  # type: ignore[attr-defined]

    # Restart: the boot sequence seeds again.
    await AccessService(session).seed()  # type: ignore[arg-type]
    await session.commit()  # type: ignore[attr-defined]

    manager = (await AccessRepository(session).matrix())[Role.CLAIMS_MANAGER.value]  # type: ignore[arg-type]
    assert Capability.PAGE_ANALYTICS.value not in manager, "a revoked grant came back"
    # And nothing else in the column was disturbed.
    assert Capability.PAGE_APPROVALS.value in manager


async def test_the_service_will_not_seed_a_populated_table(session: object) -> None:
    """The guard itself, stated directly."""
    repository = AccessRepository(session)  # type: ignore[arg-type]
    await repository.replace_role("claims-handler", {"page:claims"}, granted_by="a")
    await session.commit()  # type: ignore[attr-defined]

    total = await AccessService(session).seed()  # type: ignore[arg-type]
    await session.commit()  # type: ignore[attr-defined]

    assert total == 1
    assert await repository.matrix() == {"claims-handler": {"page:claims"}}


async def test_replacing_a_column_removes_what_is_no_longer_granted(session: object) -> None:
    """Delete-then-insert, scoped to the one role.

    A diff of individual cells would leave a grant standing if two administrators
    edited different cells of the same column at once. "The last save wins for that
    column" is far easier to explain than a merge nobody asked for.
    """
    repository = AccessRepository(session)  # type: ignore[arg-type]
    await repository.replace_role(
        "claims-handler", {"page:claims", "page:policies", "page:settings"}, granted_by="a"
    )
    await session.commit()  # type: ignore[attr-defined]

    await repository.replace_role("claims-handler", {"page:claims"}, granted_by="b")
    await session.commit()  # type: ignore[attr-defined]

    assert await repository.matrix() == {"claims-handler": {"page:claims"}}


async def test_replacing_one_column_leaves_the_others_alone(session: object) -> None:
    repository = AccessRepository(session)  # type: ignore[arg-type]
    await repository.replace_role("claims-handler", {"page:claims"}, granted_by="a")
    await repository.replace_role("loss-adjuster", {"page:inspection"}, granted_by="a")
    await session.commit()  # type: ignore[attr-defined]

    await repository.replace_role("claims-handler", {"page:policies"}, granted_by="b")
    await session.commit()  # type: ignore[attr-defined]

    matrix = await repository.matrix()
    assert matrix["claims-handler"] == {"page:policies"}
    assert matrix["loss-adjuster"] == {"page:inspection"}


async def test_clearing_a_column_leaves_no_rows_for_that_role(session: object) -> None:
    """Absence is the denial, so an empty column is an absent row and not a false one."""
    repository = AccessRepository(session)  # type: ignore[arg-type]
    await repository.replace_role("loss-adjuster", {"page:inspection"}, granted_by="a")
    await session.commit()  # type: ignore[attr-defined]

    await repository.replace_role("loss-adjuster", set(), granted_by="b")
    await session.commit()  # type: ignore[attr-defined]

    assert "loss-adjuster" not in await repository.matrix()


async def test_is_empty_answers_the_seeds_only_question(session: object) -> None:
    repository = AccessRepository(session)  # type: ignore[arg-type]
    assert await repository.is_empty() is True

    await repository.replace_role("claims-handler", {"page:claims"}, granted_by="a")
    await session.commit()  # type: ignore[attr-defined]

    assert await repository.is_empty() is False


async def test_the_grant_records_who_made_it(session: object) -> None:
    """An authorisation change is exactly the kind of edit somebody asks about later."""
    repository = AccessRepository(session)  # type: ignore[arg-type]
    await repository.replace_role("claims-handler", {"page:claims"}, granted_by="admin-subject-1")
    await session.commit()  # type: ignore[attr-defined]

    row = await session.execute(  # type: ignore[attr-defined]
        select(RoleCapability.granted_by, RoleCapability.granted_at).where(
            RoleCapability.role == "claims-handler"
        )
    )
    granted_by, granted_at = row.one()
    assert granted_by == "admin-subject-1"
    assert granted_at is not None


async def test_the_service_resolves_capabilities_from_real_rows(session: object) -> None:
    """The whole chain over a real table: rows in, `Capability` members out."""
    service = AccessService(session)  # type: ignore[arg-type]
    await AccessRepository(session).replace_role(  # type: ignore[arg-type]
        "loss-adjuster", {"page:inspection", "page:settings"}, granted_by="a"
    )
    await session.commit()  # type: ignore[attr-defined]

    held = await service.capabilities_for_roles(frozenset({"loss-adjuster"}))

    assert held == frozenset({Capability.PAGE_INSPECTION, Capability.PAGE_SETTINGS})


async def test_the_service_seed_is_idempotent_against_the_real_table(session: object) -> None:
    service = AccessService(session)  # type: ignore[arg-type]

    first = await service.seed()
    await session.commit()  # type: ignore[attr-defined]
    second = await service.seed()
    await session.commit()  # type: ignore[attr-defined]

    assert first == second
    expected = sum(len(caps) for caps in DEFAULT_MATRIX.values())
    assert first == expected


class TestTheRouteTheBoardCalls:
    """The matrix route over real HTTP, verified from a *different* session.

    This class exists because of a bug both other layers were blind to.

    `POST /access/matrix` never committed. `get_session` deliberately leaves that to
    the handler — "the handler owns commit/rollback semantics" — so every edit was
    rolled back when the request ended. It still answered 200 with the new matrix,
    because the read at the end of the handler ran inside the same open transaction and
    could see a change nobody else ever would. The board showed the toggle as applied,
    Redis cached a state that never existed, and the grant reverted the moment the cache
    went.

    Neither existing layer could catch it. The unit tests fake the repository, so there
    is no transaction to forget to commit. The repository tests above call
    `session.commit()` themselves. **The only shape that catches a missing commit is
    driving the route with a session it owns and then reading the rows back through a
    connection that never saw its transaction** — which is what this does.
    """

    async def _app(self, monkeypatch: pytest.MonkeyPatch) -> object:
        from fastapi import FastAPI
        from prometheus_client import CollectorRegistry

        import app.main as app_main
        from app.api.deps.auth import get_current_principal, get_verifier
        from app.core.security import Principal

        async def _noop(*_args: object, **_kwargs: object) -> None:
            return None

        # The lifespan would open a second engine and re-seed. This test already has an
        # engine from the fixture, and `get_session` is deliberately *not* overridden,
        # so the route opens and closes its own session against it.
        for target in (
            "init_engine",
            "dispose_engine",
            "init_pool",
            "close_pool",
            "init_redis",
            "close_redis",
            "_seed_access_matrix",
            "_seed_extraction_schemas",
        ):
            monkeypatch.setattr(app_main, target, _noop)

        application: FastAPI = app_main.create_app(metrics_registry=CollectorRegistry())
        application.dependency_overrides[get_verifier] = lambda: None
        application.dependency_overrides[get_current_principal] = lambda: Principal(
            subject="admin-subject",
            username="admin",
            realm_roles=frozenset({Role.CLAIMS_ADMIN.value}),
        )
        return application

    async def _post(self, application: object, capability: str, granted: bool) -> int:
        from httpx import ASGITransport, AsyncClient

        async with AsyncClient(
            transport=ASGITransport(app=application),  # type: ignore[arg-type]
            base_url="http://testserver",
        ) as client:
            response = await client.post(
                "/api/v1/access/matrix",
                json={
                    "changes": [
                        {
                            "permission": capability,
                            "role": Role.CLAIMS_MANAGER.value,
                            "granted": granted,
                        }
                    ]
                },
            )
        return response.status_code

    async def _stored_manager_column(self) -> set[str]:
        """Read through a session that never saw the route's transaction."""
        factory = get_session_factory()
        async with factory() as fresh:
            stored = await AccessRepository(fresh).matrix()
        return stored.get(Role.CLAIMS_MANAGER.value, set())

    async def test_a_toggle_is_durable_and_not_merely_reported(
        self, session: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The regression. A 200 has to mean the row is in the database."""
        await AccessService(session).seed()  # type: ignore[arg-type]
        await session.commit()  # type: ignore[attr-defined]

        application = await self._app(monkeypatch)
        status = await self._post(application, Capability.PAGE_ANALYTICS.value, False)

        assert status == 200
        # Before the fix the response said this had happened and the database disagreed.
        assert Capability.PAGE_ANALYTICS.value not in await self._stored_manager_column(), (
            "the toggle was reported but never committed"
        )

    async def test_the_rest_of_the_column_survives_a_single_toggle(
        self, session: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The symptom as reported: one switch off, and the others followed it.

        A single change must rewrite the column as "everything it had, minus this one",
        never as "only this one".
        """
        await AccessService(session).seed()  # type: ignore[arg-type]
        await session.commit()  # type: ignore[attr-defined]
        expected = {c.value for c in DEFAULT_MATRIX[Role.CLAIMS_MANAGER]} - {
            Capability.PAGE_ANALYTICS.value
        }

        application = await self._app(monkeypatch)
        await self._post(application, Capability.PAGE_ANALYTICS.value, False)

        assert await self._stored_manager_column() == expected

    async def test_two_toggles_in_a_row_both_persist(
        self, session: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The compounding case, which the poisoned cache made worst.

        The second request reads the current column to compute the new one. If the first
        was never committed — or if the cache still held the state it merely attempted —
        the second would be built on a column that does not exist.
        """
        await AccessService(session).seed()  # type: ignore[arg-type]
        await session.commit()  # type: ignore[attr-defined]

        application = await self._app(monkeypatch)
        for capability in (Capability.PAGE_ANALYTICS, Capability.PAGE_TEAM):
            assert await self._post(application, capability.value, False) == 200

        manager = await self._stored_manager_column()
        assert Capability.PAGE_ANALYTICS.value not in manager
        assert Capability.PAGE_TEAM.value not in manager
        assert Capability.PAGE_APPROVALS.value in manager
        assert len(manager) == len(DEFAULT_MATRIX[Role.CLAIMS_MANAGER]) - 2
