"""The handler directory: who *Assign a handler* is allowed to offer.

The behaviour under test is small and the consequences of getting it wrong are
not. A directory made of fictional people means a claim assigned to somebody
arrives on nobody's queue, and a registration that overwrote the desk's own fields
would reset a manager's decision about somebody's settlement authority every time
that person logged in.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.core.security import Principal
from app.domain.enums import Role
from app.models.reference_data import Handler
from app.services.handlers.directory import UNPLACED_TEAM, HandlerDirectoryService


class FakeHandlerRepo:
    def __init__(self, rows: list[Handler] | None = None) -> None:
        self.rows: list[Handler] = rows or []
        self.added: list[Handler] = []

    async def get_by_subject(self, subject: str) -> Handler | None:
        return next((row for row in self.rows if row.subject == subject), None)

    async def get_by_email(self, email: str) -> Handler | None:
        return next((row for row in self.rows if (row.email or "").lower() == email.lower()), None)

    def add(self, handler: Handler) -> Handler:
        handler.id = uuid.uuid4()
        self.rows.append(handler)
        self.added.append(handler)
        return handler


def principal(*roles: str, **overrides: Any) -> Principal:
    base: dict[str, Any] = {
        "subject": "kc-8f2c41a9",
        "username": "r.marsh",
        "email": "R.Marsh@carrier.com",
        "full_name": "Rebecca Marsh",
        "realm_roles": frozenset(roles or (Role.CLAIMS_HANDLER,)),
    }
    base.update(overrides)
    return Principal(**base)


def seeded(**overrides: Any) -> Handler:
    """A directory row as the seed wrote it: no subject, and real desk attributes."""
    base: dict[str, Any] = {
        "subject": None,
        "full_name": "Rebecca Marsh",
        "email": "r.marsh@carrier.com",
        "team": "Major Loss",
        "job_title": "Senior Claims Handler",
        "skills": ["property", "business interruption"],
        "lines_of_business": ["property"],
        "countries": ["GB"],
        "max_severity": "critical",
        "authority_limit_minor": 50_000_00,
        "open_claims": 12,
        "capacity": 25,
        "active": True,
    }
    base.update(overrides)
    row = Handler(**base)
    row.id = uuid.uuid4()
    return row


def build(rows: list[Handler] | None = None) -> tuple[HandlerDirectoryService, FakeHandlerRepo]:
    repo = FakeHandlerRepo(rows)
    return HandlerDirectoryService(repo), repo  # type: ignore[arg-type]


class TestWhoGetsRegistered:
    @pytest.mark.asyncio
    async def test_a_handler_signing_in_for_the_first_time_joins_the_directory(self) -> None:
        service, repo = build()
        handler = await service.register(principal())

        assert handler is not None
        assert repo.added == [handler]
        assert handler.subject == "kc-8f2c41a9"
        assert handler.full_name == "Rebecca Marsh"
        assert handler.team == UNPLACED_TEAM

    @pytest.mark.asyncio
    async def test_a_new_account_arrives_with_no_settlement_authority(self) -> None:
        """The one default worth arguing about, and it is null on purpose.

        A number here would be a figure nobody approved on the field that decides
        whether this person can release money. Null means *a manager has not said*,
        and the approval blocks already refuse a settlement against an absent limit.
        """
        service, _ = build()
        handler = await service.register(principal())

        assert handler is not None
        assert handler.authority_limit_minor is None
        assert handler.max_severity == "low"

    @pytest.mark.asyncio
    async def test_an_intake_officer_is_not_a_handler(self) -> None:
        """Offering a manager somebody who cannot own a claim is worse than not."""
        service, repo = build()
        assert await service.register(principal(Role.FNOL_OFFICER)) is None
        assert repo.added == []

    @pytest.mark.asyncio
    async def test_a_manager_is_one_because_managers_carry_files(self) -> None:
        service, _ = build()
        assert await service.register(principal(Role.CLAIMS_MANAGER)) is not None

    @pytest.mark.asyncio
    async def test_a_token_with_no_email_is_not_recorded(self) -> None:
        """Nothing useful to put in a list a manager picks from."""
        service, repo = build()
        assert await service.register(principal(email=None)) is None
        assert repo.added == []

    @pytest.mark.asyncio
    async def test_the_address_is_stored_lowercased(self) -> None:
        """Because it is matched against on the next sign-in, and case is not part of it."""
        service, _ = build()
        handler = await service.register(principal())
        assert handler is not None
        assert handler.email == "r.marsh@carrier.com"


class TestAdoption:
    @pytest.mark.asyncio
    async def test_a_real_person_claims_their_seeded_row(self) -> None:
        """The case that stops the directory filling up with duplicates.

        A colleague whose demo record predates their account signs in: the row is
        theirs, and `handlers.email` is unique so inserting would fail anyway.
        """
        row = seeded()
        service, repo = build([row])

        handler = await service.register(principal())

        assert repo.added == []
        assert handler is row
        assert row.subject == "kc-8f2c41a9"

    @pytest.mark.asyncio
    async def test_adoption_keeps_everything_the_desk_decided(self) -> None:
        """Identity is Keycloak's; authority is the manager's. This is the line."""
        row = seeded()
        service, _ = build([row])

        await service.register(principal())

        assert row.team == "Major Loss"
        assert row.authority_limit_minor == 50_000_00
        assert row.max_severity == "critical"
        assert row.skills == ["property", "business interruption"]
        assert row.open_claims == 12

    @pytest.mark.asyncio
    async def test_adoption_matches_the_address_case_insensitively(self) -> None:
        row = seeded(email="Rebecca.Marsh@Carrier.com")
        service, repo = build([row])

        await service.register(principal(email="rebecca.marsh@carrier.com"))

        assert repo.added == []
        assert row.subject == "kc-8f2c41a9"

    @pytest.mark.asyncio
    async def test_somebody_elses_row_is_not_adopted(self) -> None:
        """Adoption is by address and nothing else. A different address is a different person."""
        row = seeded(email="d.okafor@carrier.com", full_name="Daniel Okafor")
        service, repo = build([row])

        handler = await service.register(principal())

        assert len(repo.added) == 1
        assert handler is not row
        assert row.subject is None


class TestReturningHandler:
    @pytest.mark.asyncio
    async def test_signing_in_again_writes_nothing_new(self) -> None:
        row = seeded(subject="kc-8f2c41a9")
        service, repo = build([row])

        await service.register(principal())

        assert repo.added == []

    @pytest.mark.asyncio
    async def test_a_rename_at_the_provider_reaches_the_directory(self) -> None:
        """People are renamed, and a manager should see the name they answer to."""
        row = seeded(subject="kc-8f2c41a9")
        service, _ = build([row])

        await service.register(principal(full_name="Rebecca Marsh-Okonjo"))

        assert row.full_name == "Rebecca Marsh-Okonjo"

    @pytest.mark.asyncio
    async def test_a_rename_does_not_touch_what_they_are_trusted_with(self) -> None:
        row = seeded(subject="kc-8f2c41a9")
        service, _ = build([row])

        await service.register(principal(full_name="Rebecca Marsh-Okonjo"))

        assert row.authority_limit_minor == 50_000_00
        assert row.team == "Major Loss"

    @pytest.mark.asyncio
    async def test_somebody_deactivated_who_comes_back_is_active_again(self) -> None:
        """They are demonstrably working: they just signed in."""
        row = seeded(subject="kc-8f2c41a9", active=False)
        service, _ = build([row])

        await service.register(principal())

        assert row.active is True
