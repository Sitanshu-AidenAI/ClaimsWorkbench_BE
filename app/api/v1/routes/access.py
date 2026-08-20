"""The access matrix, read and edited.

Two routes for the Access control board, and both are gated on `page:admin` — the
capability rather than the role, because who administers a deployment is exactly the
kind of thing a deployment should be able to decide.

The catalogue is served rather than enumerated on the client. A board that hardcoded
the capability keys would silently omit whatever a later release adds, and on an
authorisation surface that means an administrator believing they have seen the whole
picture when they have not.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api.deps.auth import CurrentPrincipal, require_capability
from app.api.deps.db import SessionDep
from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.domain.capabilities import (
    ADMINISTRATOR,
    CAPABILITY_GROUPS,
    CAPABILITY_LABELS,
    PERSONA_LABELS,
    PERSONAS,
    Capability,
)
from app.services.access.service import AccessService

logger = get_logger(__name__)

router = APIRouter(prefix="/access", tags=["access"])

AdminOnly = Depends(require_capability(Capability.PAGE_ADMIN))


class CapabilityRow(BaseModel):
    key: str
    label: str
    #: True when no persona but the administrator may hold it. Nothing is marked so
    #: today; the field exists because the board already renders the distinction and
    #: a future capability — signing off a settlement, say — will want it.
    admin_only: bool = False


class CapabilityGroup(BaseModel):
    id: str
    label: str
    permissions: list[CapabilityRow]


class PersonaColumn(BaseModel):
    key: str
    label: str
    #: The administrator's column states a fact rather than a choice, so the board
    #: draws it locked. Enforced on the write route too — this only stops the client
    #: offering a change that would be refused.
    locked: bool


class MatrixResponse(BaseModel):
    roles: list[PersonaColumn]
    groups: list[CapabilityGroup]
    #: `grants[capability][role]`. Absent means denied; the board renders the row
    #: either way, because the catalogue may name something nobody has been granted.
    grants: dict[str, dict[str, bool]]


class MatrixChange(BaseModel):
    permission: str
    role: str
    granted: bool


class MatrixUpdate(BaseModel):
    changes: list[MatrixChange] = Field(min_length=1)


@router.get(
    "/matrix",
    response_model=MatrixResponse,
    dependencies=[AdminOnly],
    summary="The access matrix and its catalogue",
)
async def read_matrix(session: SessionDep) -> MatrixResponse:
    grants = await AccessService(session).matrix()

    return MatrixResponse(
        roles=[
            PersonaColumn(
                key=role.value,
                label=PERSONA_LABELS[role],
                locked=role == ADMINISTRATOR,
            )
            for role in PERSONAS
        ],
        groups=[
            CapabilityGroup(
                id=group_id,
                label=label,
                permissions=[
                    CapabilityRow(key=c.value, label=CAPABILITY_LABELS[c]) for c in members
                ],
            )
            for group_id, label, members in CAPABILITY_GROUPS
        ],
        grants={
            capability.value: {
                role.value: capability.value in grants.get(role.value, set()) for role in PERSONAS
            }
            for capability in Capability
        },
    )


@router.post(
    "/matrix",
    response_model=MatrixResponse,
    dependencies=[AdminOnly],
    summary="Change who may reach what",
)
async def update_matrix(
    update: MatrixUpdate,
    session: SessionDep,
    principal: CurrentPrincipal,
) -> MatrixResponse:
    """Apply a set of cell changes, then answer with the whole matrix.

    The whole matrix back, not just an acknowledgement, for the same reason the FNOL
    mutations answer with the whole case: the board replaces what it holds rather
    than reconciling, and a change to one cell can be refused while its neighbours
    apply.
    """
    service = AccessService(session)
    current = await service.matrix()

    known_roles = {role.value for role in PERSONAS}
    known_capabilities = {c.value for c in Capability}

    #: Accumulated per role, because the repository replaces a role's whole column —
    #: applying changes one cell at a time would issue a delete per cell.
    desired: dict[str, set[str]] = {}

    for change in update.changes:
        if change.role not in known_roles:
            raise ValidationError(f"{change.role!r} is not a persona in this matrix.")
        if change.permission not in known_capabilities:
            raise ValidationError(f"{change.permission!r} is not a capability.")
        if change.role == ADMINISTRATOR.value:
            # Refused rather than silently dropped. A deployment able to revoke the
            # administrator's access to Admin Studio could lock every person out of
            # the one board that would let them undo it.
            raise ValidationError("The administrator's access cannot be changed.")

        column = desired.setdefault(change.role, set(current.get(change.role, set())))
        if change.granted:
            column.add(change.permission)
        else:
            column.discard(change.permission)

    for role, capabilities in desired.items():
        await service.replace_role(role, capabilities, granted_by=principal.subject)

    # Committed here, at the route boundary, like every other write in this API.
    # `get_session` deliberately does not commit — "the handler owns commit/rollback
    # semantics" — so without this the whole edit was rolled back when the request
    # ended. It answered 200 and reported the new matrix, because the read below ran
    # inside the same uncommitted transaction and could see the change nobody else
    # ever would.
    await session.commit()

    # Invalidated *after* the commit, and that ordering is the second half of the same
    # bug. `replace_role` drops the cache while the transaction is still open, so a
    # read between then and the commit repopulates it from the pre-change rows — and a
    # read *after* a rollback would have cached a state that never existed. Dropping
    # it again once the rows are durable is what makes the cache agree with the
    # database rather than with an attempt.
    await service.invalidate()

    logger.info(
        "access_matrix_changed",
        actor=principal.subject,
        roles=sorted(desired),
        changes=len(update.changes),
    )
    return await read_matrix(session)


@router.get(
    "/capabilities",
    response_model=list[str],
    summary="What the calling principal may reach",
)
async def my_capabilities(
    session: SessionDep, principal: CurrentPrincipal
) -> Annotated[list[str], "sorted capability keys"]:
    """The caller's own capabilities.

    Not gated on anything but being signed in: asking what you yourself may do is not
    a privileged question, and the rail needs the answer on every page load. The SPA
    normally takes this from the session response instead — this exists for a client
    that wants to re-check without re-minting a token.
    """
    held = await AccessService(session).capabilities_for_roles(principal.roles)
    return sorted(c.value for c in held)
