"""Audit trail writes and reads.

Append-only by construction: there is no update method, and there will not be one.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import get_request_id
from app.domain.enums import ActorType, AuditEventType
from app.models.audit import AuditEvent


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def record(
        self,
        *,
        entity_type: str,
        entity_id: uuid.UUID,
        entity_reference: str | None,
        event_type: AuditEventType | str,
        summary: str,
        actor: str,
        actor_type: ActorType | str = ActorType.HUMAN,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> AuditEvent:
        """Add an event to the caller's transaction.

        Not committed here: an audit event that survived a rolled-back claim
        creation would be a record of something that never happened.
        """
        event = AuditEvent(
            entity_type=entity_type,
            entity_id=entity_id,
            entity_reference=entity_reference,
            event_type=str(event_type),
            summary=summary,
            actor=actor,
            actor_type=str(actor_type),
            before=before,
            after=after,
            context=context or {},
            request_id=get_request_id(),
            occurred_at=datetime.now(UTC),
        )
        self._session.add(event)
        return event

    async def list_for_entity(
        self,
        entity_id: uuid.UUID,
        *,
        related_ids: Sequence[uuid.UUID] = (),
        limit: int = 200,
    ) -> Sequence[AuditEvent]:
        """The trail for one entity, optionally including entities it links to.

        `related_ids` is how the claim workbench shows the notice's history
        alongside the claim's — the two are one narrative to a reader, even though
        they are two entities to the schema.
        """
        identifiers = [entity_id, *related_ids]
        statement = (
            select(AuditEvent)
            .where(AuditEvent.entity_id.in_(identifiers))
            .order_by(AuditEvent.occurred_at.desc())
            .limit(limit)
        )
        return (await self._session.execute(statement)).scalars().all()
