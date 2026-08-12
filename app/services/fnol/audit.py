"""Audit writing, with the vocabulary the trail is read in.

A thin service over the repository, and worth having: every call site would
otherwise have to remember the entity type, the reference and the actor type, and
the first one to forget would produce an event nobody can attribute.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.domain.enums import ActorType, AuditEventType
from app.models.audit import AuditEvent
from app.repositories.audit import AuditRepository

ENTITY_FNOL = "fnol"
ENTITY_CLAIM = "claim"

#: The actor recorded when a pipeline stage acts rather than a person.
SYSTEM_ACTOR = "FNOL pipeline"


class AuditService:
    def __init__(self, repository: AuditRepository) -> None:
        self._repository = repository

    def fnol(
        self,
        case: Any,
        *,
        event_type: AuditEventType,
        summary: str,
        actor: str,
        actor_type: ActorType = ActorType.HUMAN,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> AuditEvent:
        return self._repository.record(
            entity_type=ENTITY_FNOL,
            entity_id=case.id,
            entity_reference=case.reference,
            event_type=event_type,
            summary=summary,
            actor=actor,
            actor_type=actor_type,
            before=before,
            after=after,
            context=context,
        )

    def claim(
        self,
        claim: Any,
        *,
        event_type: AuditEventType,
        summary: str,
        actor: str,
        actor_type: ActorType = ActorType.HUMAN,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> AuditEvent:
        return self._repository.record(
            entity_type=ENTITY_CLAIM,
            entity_id=claim.id,
            entity_reference=claim.reference,
            event_type=event_type,
            summary=summary,
            actor=actor,
            actor_type=actor_type,
            before=before,
            after=after,
            context=context,
        )

    def system(
        self,
        case: Any,
        *,
        event_type: AuditEventType,
        summary: str,
        context: dict[str, Any] | None = None,
        actor: str = SYSTEM_ACTOR,
    ) -> AuditEvent:
        """An event the pipeline caused. Marked `ai` so a reader can tell.

        The distinction is not cosmetic: a regulator asking "who decided this
        claim was a possible duplicate" is entitled to the answer "nobody did — a
        rule flagged it and an officer resolved it".
        """
        return self.fnol(
            case,
            event_type=event_type,
            summary=summary,
            actor=actor,
            actor_type=ActorType.AI,
            context=context,
        )

    async def history(
        self, entity_id: uuid.UUID, *, related_ids: tuple[uuid.UUID, ...] = ()
    ) -> list[AuditEvent]:
        return list(await self._repository.list_for_entity(entity_id, related_ids=related_ids))
