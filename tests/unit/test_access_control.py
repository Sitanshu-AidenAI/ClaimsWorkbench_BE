"""Role-based access control: the matrix, its cache, and what it refuses.

Four layers, tested where each one can actually be wrong:

* **The domain** — that the documented defaults say what the personas were specified
  to say, and that holding two roles unions rather than ranks.
* **The service** — caching, invalidation, and the two fallbacks. These are the parts
  that decide whether a revoke takes effect and whether a desk goes dark when Redis
  or Postgres blinks, so they are worth more than the repository's SQL.
* **The dependency** — that `require_capability` admits and refuses on the matrix
  rather than on a role.
* **The routes** — the catalogue's shape, the change set, and the refusals.

The repository's own SQL is covered against a real database in
`tests/integration/test_access_matrix_flow.py`. `ON CONFLICT DO NOTHING` and a
delete-then-insert inside one transaction are exactly what a dict cannot tell the
truth about, so faking them here would be testing the fake.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.deps.auth import get_current_principal, require_capability
from app.api.deps.db import get_session
from app.core.security import Principal
from app.domain.capabilities import (
    ADMINISTRATOR,
    CAPABILITY_LABELS,
    DEFAULT_MATRIX,
    PERSONA_LABELS,
    PERSONAS,
    Capability,
    capabilities_for,
)
from app.domain.enums import Role
from app.services.access.service import AccessService
from tests.unit.fakes import FakeAccessRepository, FakeRedis, FakeSession

pytestmark = pytest.mark.anyio


# --- The domain ---------------------------------------------------------------
#
# These assert the specification rather than the implementation: if somebody widens a
# persona by accident, this is the file that should complain.


def test_every_persona_has_a_documented_default() -> None:
    for role in PERSONAS:
        assert role in DEFAULT_MATRIX, f"{role} has no default grants"


def test_every_persona_and_capability_has_a_label() -> None:
    """A matrix cell with no heading is a cell nobody can act on."""
    for role in PERSONAS:
        assert PERSONA_LABELS.get(role), f"{role} has no label"
    for capability in Capability:
        assert CAPABILITY_LABELS.get(capability), f"{capability} has no label"


def test_the_administrator_holds_everything() -> None:
    assert DEFAULT_MATRIX[ADMINISTRATOR] == frozenset(Capability)


def test_the_specified_personas_reach_exactly_what_was_asked_for() -> None:
    """The five persona lists, as specified, capability by capability.

    Spelled out rather than derived, deliberately. The point of this test is to fail
    when the matrix changes, so it must not be written in terms of the thing it is
    checking.
    """
    assert DEFAULT_MATRIX[Role.FNOL_OFFICER] == frozenset(
        {
            Capability.PAGE_INTAKE,
            Capability.PAGE_POLICIES,
            Capability.PAGE_INTELLIGENCE,
            Capability.PAGE_ASSISTANT,
            Capability.PAGE_SETTINGS,
        }
    )
    assert DEFAULT_MATRIX[Role.CLAIMS_HANDLER] == frozenset(
        {
            Capability.PAGE_CLAIMS,
            Capability.PAGE_POLICIES,
            Capability.PAGE_INTELLIGENCE,
            Capability.PAGE_CONNECTORS,
            Capability.PAGE_ASSISTANT,
            Capability.PAGE_SETTINGS,
        }
    )
    assert DEFAULT_MATRIX[Role.LOSS_ADJUSTER] == frozenset(
        {
            Capability.PAGE_INSPECTION,
            Capability.PAGE_INTELLIGENCE,
            Capability.PAGE_ASSISTANT,
            Capability.PAGE_SETTINGS,
        }
    )
    assert DEFAULT_MATRIX[Role.CLAIMS_MANAGER] == frozenset(
        {
            Capability.PAGE_CLAIMS,
            Capability.PAGE_POLICIES,
            Capability.PAGE_APPROVALS,
            Capability.PAGE_INTELLIGENCE,
            Capability.PAGE_TEAM,
            Capability.PAGE_ANALYTICS,
            Capability.PAGE_ASSISTANT,
            Capability.PAGE_ADMIN,
            Capability.PAGE_CONNECTORS,
            Capability.PAGE_SETTINGS,
        }
    )


def test_the_ai_assistant_reaches_every_persona() -> None:
    """Specified explicitly, so it is worth asserting explicitly."""
    for role in PERSONAS:
        assert Capability.PAGE_ASSISTANT in DEFAULT_MATRIX[role]


def test_connectors_reach_the_claims_handler() -> None:
    """A decision rather than an obvious default.

    Narrower gates argued this board was administrator-only because the credentials
    it holds open a path into the claims core. Granting it to handlers was confirmed
    deliberately, so a later reader should find that recorded rather than infer it was
    a slip.
    """
    assert Capability.PAGE_CONNECTORS in DEFAULT_MATRIX[Role.CLAIMS_HANDLER]


def test_holding_two_personas_unions_rather_than_ranks() -> None:
    """The property that makes granting a second role predictable.

    A precedence order would mean a manager who also holds the officer role *loses*
    intake, which nobody would guess from the words "and also an FNOL officer".
    """
    both = capabilities_for({Role.CLAIMS_MANAGER.value, Role.FNOL_OFFICER.value})

    assert Capability.PAGE_INTAKE in both, "the officer's intake was lost"
    assert Capability.PAGE_APPROVALS in both, "the manager's approvals were lost"
    assert both == DEFAULT_MATRIX[Role.CLAIMS_MANAGER] | DEFAULT_MATRIX[Role.FNOL_OFFICER]


def test_business_admin_is_aliased_onto_the_administrator() -> None:
    """A legacy realm role, kept working rather than given a sixth column.

    It is still granted in the realm and to the seeded `demo` user. Dropping it from
    the enum would break `EXTRACTION_ADMIN_ROLES` and orphan live grants at once.
    """
    assert capabilities_for({Role.BUSINESS_ADMIN.value}) == frozenset(Capability)
    assert Role.BUSINESS_ADMIN not in PERSONAS, "the matrix must still show five columns"


def test_a_realm_role_this_product_does_not_model_grants_nothing() -> None:
    """`offline_access`, or a role an administrator added for another system.

    Silently ignored rather than raised: a request path is the wrong place to discover
    that somebody configured a role over in Keycloak.
    """
    assert capabilities_for({"offline_access", "some-other-system"}) == frozenset()


def test_no_roles_grants_nothing() -> None:
    assert capabilities_for(set()) == frozenset()


# --- The service --------------------------------------------------------------


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    fake = FakeRedis()
    monkeypatch.setattr("app.services.access.service.get_redis", lambda: fake)
    return fake


def _service(repository: FakeAccessRepository) -> AccessService:
    """An `AccessService` over a fake repository.

    Constructed then patched rather than injected, because the service takes a session
    and builds its own repository — which is the right shape for the routes and an
    awkward one for a test. Reaching into the private attribute is the smaller evil
    compared with adding a constructor parameter that exists only for tests.
    """
    service = AccessService.__new__(AccessService)
    service._repository = repository  # type: ignore[attr-defined]
    return service


async def test_the_matrix_is_read_from_the_repository_and_then_cached(
    redis: FakeRedis,
) -> None:
    repository = FakeAccessRepository({"claims-handler": {"page:claims"}})
    service = _service(repository)

    first = await service.matrix()
    repository.grants["claims-handler"].add("page:policies")
    second = await service.matrix()

    assert first == {"claims-handler": {"page:claims"}}
    # The second read is served from the cache, so the change behind it is not seen.
    # That is the behaviour a write has to invalidate, which the next test covers.
    assert second == first


async def test_a_write_invalidates_the_cache(redis: FakeRedis) -> None:
    """The reason invalidation is explicit rather than left to a TTL.

    What is cached is an authorisation decision. "We removed their access and it took
    five minutes" is not an acceptable answer about access, so the TTL is only a
    backstop for an invalidation that was lost.
    """
    repository = FakeAccessRepository({"claims-handler": {"page:claims", "page:policies"}})
    service = _service(repository)
    await service.matrix()

    await service.replace_role("claims-handler", {"page:claims"}, granted_by="admin-1")

    assert await service.matrix() == {"claims-handler": {"page:claims"}}
    assert repository.replaced == [("claims-handler", {"page:claims"}, "admin-1")]


async def test_capabilities_are_resolved_from_the_table_not_the_defaults(
    redis: FakeRedis,
) -> None:
    """The point of the whole feature: the table wins over the code.

    The handler is given `page:analytics`, which no default grants them, and denied
    `page:claims`, which every default does.
    """
    repository = FakeAccessRepository({"claims-handler": {"page:analytics"}})

    held = await _service(repository).capabilities_for_roles(frozenset({"claims-handler"}))

    assert held == frozenset({Capability.PAGE_ANALYTICS})
    assert Capability.PAGE_CLAIMS not in held


async def test_an_unknown_capability_in_the_table_is_dropped(redis: FakeRedis) -> None:
    """A row naming something this build has removed.

    Ignored rather than raised — a release that deletes a capability should not make
    every request fail until somebody cleans the table.
    """
    repository = FakeAccessRepository(
        {"claims-handler": {"page:claims", "page:something-we-deleted"}}
    )

    held = await _service(repository).capabilities_for_roles(frozenset({"claims-handler"}))

    assert held == frozenset({Capability.PAGE_CLAIMS})


async def test_an_empty_table_falls_back_to_the_documented_defaults(
    redis: FakeRedis,
) -> None:
    """A desk whose seed failed should behave as documented, not go dark.

    Every board denied for everybody is a worse failure than the defaults, and it is
    the one a naive empty-table read would produce.
    """
    service = _service(FakeAccessRepository({}))

    held = await service.capabilities_for_roles(frozenset({Role.CLAIMS_MANAGER.value}))

    assert held == DEFAULT_MATRIX[Role.CLAIMS_MANAGER]


async def test_the_defaults_are_not_cached_as_if_they_were_the_table(
    redis: FakeRedis,
) -> None:
    """So the next read notices once the seed lands.

    Caching the fallback would make a failed seed persist for the cache lifetime even
    after it succeeded.
    """
    repository = FakeAccessRepository({})
    service = _service(repository)
    await service.matrix()

    repository.grants["claims-handler"] = {"page:claims"}

    assert await service.matrix() == {"claims-handler": {"page:claims"}}


async def test_an_unreadable_database_falls_back_rather_than_denying(
    redis: FakeRedis,
) -> None:
    """Postgres down must not black out every screen."""
    repository = FakeAccessRepository({"claims-handler": {"page:claims"}})
    repository.fail = True

    held = await _service(repository).capabilities_for_roles(frozenset({Role.LOSS_ADJUSTER.value}))

    assert held == DEFAULT_MATRIX[Role.LOSS_ADJUSTER]


async def test_redis_being_down_reads_through_to_the_database(redis: FakeRedis) -> None:
    """The cache is an optimisation; losing it must not lose the answer."""
    repository = FakeAccessRepository({"claims-handler": {"page:claims"}})
    redis.fail = True

    assert await _service(repository).matrix() == {"claims-handler": {"page:claims"}}


async def test_a_write_survives_a_cache_that_cannot_be_cleared(redis: FakeRedis) -> None:
    """Never fail the write for the sake of the cache.

    A change that rolled back because Redis blinked would be worse than a matrix that
    is briefly stale — the TTL bounds the staleness, and nothing bounds a lost edit.
    """
    repository = FakeAccessRepository({"claims-handler": {"page:claims"}})
    service = _service(repository)
    redis.fail = True

    await service.replace_role("claims-handler", set(), granted_by="admin-1")

    assert repository.replaced == [("claims-handler", set(), "admin-1")]


async def test_corrupt_cached_json_is_ignored(redis: FakeRedis) -> None:
    repository = FakeAccessRepository({"claims-handler": {"page:claims"}})
    redis.values["access:matrix:v1"] = "{not json"

    assert await _service(repository).matrix() == {"claims-handler": {"page:claims"}}


async def test_the_seed_runs_only_into_an_empty_table(redis: FakeRedis) -> None:
    """The correction the integration suite forced.

    Seeding unconditionally with `ON CONFLICT DO NOTHING` looked safe and was not: a
    revoked grant does not exist, so it does not conflict, so the next boot put it
    back — silently re-granting access somebody had deliberately removed.

    The first version of this test asserted only that the column grew, which is why it
    passed while the behaviour was wrong. It now asserts the thing that matters.
    """
    populated = FakeAccessRepository({Role.CLAIMS_MANAGER.value: {"page:claims"}})
    service = _service(populated)

    await service.seed()

    assert populated.seed_calls == 0, "a populated table must not be seeded"
    assert populated.grants[Role.CLAIMS_MANAGER.value] == {"page:claims"}


async def test_the_seed_installs_the_defaults_into_an_empty_table(redis: FakeRedis) -> None:
    empty = FakeAccessRepository({})
    service = _service(empty)

    total = await service.seed()

    assert empty.seed_calls == 1
    assert total == sum(len(caps) for caps in DEFAULT_MATRIX.values())
    assert empty.grants[Role.LOSS_ADJUSTER.value] == {
        c.value for c in DEFAULT_MATRIX[Role.LOSS_ADJUSTER]
    }


async def test_a_revoked_grant_survives_a_restart(redis: FakeRedis) -> None:
    """The property the whole change exists for, stated end to end.

    An administrator revokes a capability; the application restarts and seeds; the
    capability must still be revoked.
    """
    repository = FakeAccessRepository({})
    await _service(repository).seed()

    remaining = {
        c.value for c in DEFAULT_MATRIX[Role.CLAIMS_MANAGER] if c is not Capability.PAGE_ANALYTICS
    }
    await _service(repository).replace_role(
        Role.CLAIMS_MANAGER.value, remaining, granted_by="admin-1"
    )

    # Restart.
    await _service(repository).seed()

    assert "page:analytics" not in repository.grants[Role.CLAIMS_MANAGER.value]
    assert "page:approvals" in repository.grants[Role.CLAIMS_MANAGER.value]


async def test_the_cache_key_is_versioned(redis: FakeRedis) -> None:
    """So a change to the cached shape cannot be read as the old one.

    Bumping the key is the whole migration for a cache; without a version in it the
    only options are a flush or a subtly wrong read.
    """
    await _service(FakeAccessRepository({"claims-handler": {"page:claims"}})).matrix()

    assert "access:matrix:v1" in redis.values
    assert json.loads(redis.values["access:matrix:v1"]) == {"claims-handler": ["page:claims"]}


# --- The dependency -----------------------------------------------------------


@pytest.fixture
def gated_app(redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A tiny application with one capability-gated route.

    Built here rather than reusing the real app so the test is about the dependency
    and not about whichever feature route happened to be convenient.
    """
    app = FastAPI()
    repository = FakeAccessRepository(
        {
            "claims-handler": {"page:claims"},
            "claims-manager": {"page:claims", "page:admin"},
        }
    )
    monkeypatch.setattr(
        AccessService, "__init__", lambda self, session: setattr(self, "_repository", repository)
    )

    @app.get("/needs-admin", dependencies=[Depends(require_capability(Capability.PAGE_ADMIN))])
    async def needs_admin() -> dict[str, bool]:
        return {"ok": True}

    async def _no_session() -> AsyncIterator[None]:
        yield None

    app.dependency_overrides[get_session] = _no_session
    app.state.principal_roles = {"claims-handler"}

    def _principal() -> Principal:
        return Principal(
            subject="sub-1", realm_roles=frozenset(app.state.principal_roles), claims={}
        )

    app.dependency_overrides[get_current_principal] = _principal
    from app.core.errors import register_exception_handlers

    register_exception_handlers(app)
    return app


@pytest.fixture
async def gated(gated_app: Any) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=gated_app), base_url="http://testserver"
    ) as client:
        yield client


async def test_require_capability_refuses_a_persona_without_it(
    gated: AsyncClient, gated_app: Any
) -> None:
    gated_app.state.principal_roles = {"claims-handler"}

    response = await gated.get("/needs-admin")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"
    # The message names what was needed, so a support ticket does not require a log.
    assert "page:admin" in response.json()["error"]["message"]


async def test_require_capability_admits_a_persona_holding_it(
    gated: AsyncClient, gated_app: Any
) -> None:
    gated_app.state.principal_roles = {"claims-manager"}

    response = await gated.get("/needs-admin")

    assert response.status_code == 200


async def test_require_capability_reads_the_matrix_not_the_role_name(
    gated: AsyncClient, gated_app: Any
) -> None:
    """The property that makes the board mean anything.

    The fake table grants `page:admin` to `claims-manager` and not to `claims-admin`,
    which is the opposite of the documented default. The administrator is refused,
    which could only happen if the answer came from the table.
    """
    gated_app.state.principal_roles = {"claims-admin"}

    response = await gated.get("/needs-admin")

    assert response.status_code == 403


# --- The routes ---------------------------------------------------------------


@pytest.fixture
def matrix_app(app: object, redis: FakeRedis, monkeypatch: pytest.MonkeyPatch) -> Any:
    """The real application, with the grant table faked and an admin signed in."""
    from fastapi import FastAPI as RealApp

    assert isinstance(app, RealApp)

    repository = FakeAccessRepository(
        {role.value: {c.value for c in caps} for role, caps in DEFAULT_MATRIX.items()}
    )
    monkeypatch.setattr(
        AccessService, "__init__", lambda self, session: setattr(self, "_repository", repository)
    )

    db = FakeSession()

    async def _fake_session() -> AsyncIterator[FakeSession]:
        yield db

    app.dependency_overrides[get_session] = _fake_session
    app.dependency_overrides[get_current_principal] = lambda: Principal(
        subject="admin-1", realm_roles=frozenset({Role.CLAIMS_ADMIN.value}), claims={}
    )
    app.state.repository = repository
    app.state.db = db
    yield app
    app.dependency_overrides.clear()


@pytest.fixture
async def matrix(matrix_app: Any) -> AsyncIterator[AsyncClient]:
    async with (
        AsyncClient(transport=ASGITransport(app=matrix_app), base_url="http://testserver") as c,
        matrix_app.router.lifespan_context(matrix_app),
    ):
        yield c


async def test_the_catalogue_names_five_personas_with_the_administrator_locked(
    matrix: AsyncClient,
) -> None:
    response = await matrix.get("/api/v1/access/matrix")

    assert response.status_code == 200
    roles = response.json()["roles"]
    assert [r["key"] for r in roles] == [r.value for r in PERSONAS]
    assert [r["label"] for r in roles][:2] == ["Administrator", "Claims manager"]
    assert roles[0]["locked"] is True
    assert all(r["locked"] is False for r in roles[1:])


async def test_the_catalogue_is_server_driven_and_lists_every_capability(
    matrix: AsyncClient,
) -> None:
    """So a board cannot silently omit what a later release adds.

    On an authorisation surface, a missing row means an administrator believing they
    have seen the whole picture when they have not.
    """
    body = (await matrix.get("/api/v1/access/matrix")).json()

    listed = {p["key"] for group in body["groups"] for p in group["permissions"]}
    assert listed == {c.value for c in Capability}
    assert set(body["grants"]) == {c.value for c in Capability}


async def test_every_cell_is_answered_for_every_persona(matrix: AsyncClient) -> None:
    """Absent means denied, but the grid must still be complete.

    A board that had to treat a missing key as false would render a hole rather than
    an unchecked box.
    """
    body = (await matrix.get("/api/v1/access/matrix")).json()

    for capability, cells in body["grants"].items():
        assert set(cells) == {r.value for r in PERSONAS}, f"{capability} is missing a column"


async def test_a_change_is_applied_and_the_whole_matrix_comes_back(
    matrix: AsyncClient, matrix_app: Any
) -> None:
    response = await matrix.post(
        "/api/v1/access/matrix",
        json={
            "changes": [{"permission": "page:analytics", "role": "loss-adjuster", "granted": True}]
        },
    )

    assert response.status_code == 200
    # The response is the state that landed, which the board adopts rather than
    # promoting its own draft — the server applies a whole column at a time.
    assert response.json()["grants"]["page:analytics"]["loss-adjuster"] is True
    assert "page:analytics" in matrix_app.state.repository.grants["loss-adjuster"]


async def test_a_revoke_removes_the_grant(matrix: AsyncClient, matrix_app: Any) -> None:
    response = await matrix.post(
        "/api/v1/access/matrix",
        json={
            "changes": [{"permission": "page:claims", "role": "claims-handler", "granted": False}]
        },
    )

    assert response.status_code == 200
    assert response.json()["grants"]["page:claims"]["claims-handler"] is False
    assert "page:claims" not in matrix_app.state.repository.grants["claims-handler"]


async def test_several_changes_to_one_persona_are_applied_together(
    matrix: AsyncClient, matrix_app: Any
) -> None:
    """One write per role, not per cell.

    The repository replaces a role's whole column, so applying cells one at a time
    would issue a delete per cell and leave the column briefly wrong in between.
    """
    await matrix.post(
        "/api/v1/access/matrix",
        json={
            "changes": [
                {"permission": "page:analytics", "role": "loss-adjuster", "granted": True},
                {"permission": "page:team", "role": "loss-adjuster", "granted": True},
                {"permission": "page:settings", "role": "loss-adjuster", "granted": False},
            ]
        },
    )

    adjuster = matrix_app.state.repository.grants["loss-adjuster"]
    assert {"page:analytics", "page:team"} <= adjuster
    assert "page:settings" not in adjuster
    replaced_roles = [role for role, _, _ in matrix_app.state.repository.replaced]
    assert replaced_roles == ["loss-adjuster"], "the column should be written once"


async def test_the_administrator_column_cannot_be_changed(matrix: AsyncClient) -> None:
    """Refused rather than silently dropped.

    A deployment able to revoke the administrator's access to Admin Studio could lock
    every person out of the one board that would let them undo it.
    """
    response = await matrix.post(
        "/api/v1/access/matrix",
        json={"changes": [{"permission": "page:admin", "role": "claims-admin", "granted": False}]},
    )

    assert response.status_code == 422
    assert "administrator" in response.json()["error"]["message"].lower()


async def test_an_unknown_persona_is_refused(matrix: AsyncClient) -> None:
    response = await matrix.post(
        "/api/v1/access/matrix",
        json={"changes": [{"permission": "page:claims", "role": "regional_head", "granted": True}]},
    )

    assert response.status_code == 422


async def test_an_unknown_capability_is_refused(matrix: AsyncClient) -> None:
    response = await matrix.post(
        "/api/v1/access/matrix",
        json={
            "changes": [{"permission": "page:invented", "role": "claims-handler", "granted": True}]
        },
    )

    assert response.status_code == 422


async def test_an_empty_change_set_is_refused(matrix: AsyncClient) -> None:
    """A save with nothing in it is a bug in the caller, not a no-op worth accepting."""
    response = await matrix.post("/api/v1/access/matrix", json={"changes": []})

    assert response.status_code == 422


async def test_one_bad_change_applies_none_of_them(matrix: AsyncClient, matrix_app: Any) -> None:
    """Validated before anything is written.

    A partial apply would leave the board showing a state nobody chose, and on an
    authorisation surface a half-applied change is the worst of the three outcomes.
    """
    await matrix.post(
        "/api/v1/access/matrix",
        json={
            "changes": [
                {"permission": "page:analytics", "role": "loss-adjuster", "granted": True},
                {"permission": "page:admin", "role": "claims-admin", "granted": False},
            ]
        },
    )

    assert matrix_app.state.repository.replaced == []


async def test_a_change_commits_the_transaction(matrix: AsyncClient, matrix_app: Any) -> None:
    """The boundary the route forgot.

    `get_session` leaves commit to the handler, and this route did not call it — so the
    edit was rolled back at the end of the request while the response, read inside the
    same open transaction, reported success.

    This assertion cannot prove the rows are durable; only a second connection can, and
    `tests/integration/test_access_matrix_flow.py::TestTheRouteTheBoardCalls` does that.
    What it can do is fail fast in the suite that runs on every commit if the call
    disappears again.
    """
    await matrix.post(
        "/api/v1/access/matrix",
        json={
            "changes": [{"permission": "page:analytics", "role": "loss-adjuster", "granted": True}]
        },
    )

    assert matrix_app.state.db.commits == 1


async def test_a_refused_change_commits_nothing(matrix: AsyncClient, matrix_app: Any) -> None:
    """Validation runs before the write, so a refusal has nothing to commit."""
    await matrix.post(
        "/api/v1/access/matrix",
        json={"changes": [{"permission": "page:admin", "role": "claims-admin", "granted": False}]},
    )

    assert matrix_app.state.db.commits == 0
    assert matrix_app.state.repository.replaced == []


async def test_reading_the_matrix_commits_nothing(matrix: AsyncClient, matrix_app: Any) -> None:
    """A GET has no business opening and closing a transaction."""
    await matrix.get("/api/v1/access/matrix")

    assert matrix_app.state.db.commits == 0


async def test_a_caller_can_read_their_own_capabilities(matrix: AsyncClient) -> None:
    """Not a privileged question, and the rail needs it on every page load."""
    response = await matrix.get("/api/v1/access/capabilities")

    assert response.status_code == 200
    assert response.json() == sorted(c.value for c in Capability)
