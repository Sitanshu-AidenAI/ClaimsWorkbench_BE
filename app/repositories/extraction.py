"""Reads and writes for datasets, runs and extracted values.

Kept apart from `FNOLRepository` for the reason `DocumentChunkRepository` is: the
access patterns differ. A schema is small, cached and read on almost every
request; a case's values are read as a set and written as a set. Mixing them into
the repository the review screen already uses would make it easy to load a
dataset's whole field table to render one row.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.domain.enums import ExtractionRunStatus, ExtractionSchemaStatus
from app.models.extraction import (
    ExtractedValue,
    ExtractedValueCitation,
    ExtractionRun,
    ExtractionSchema,
    ExtractionSchemaField,
)

if TYPE_CHECKING:
    # Type-only: the citation writer lives in the service layer, and importing it
    # here for real would close the loop `services.extraction` → `engine` →
    # `repositories.extraction` back to itself.
    from app.services.extraction.corroborate import CitationDraft


class ExtractionSchemaRepository:
    """Dataset definitions."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def flush(self) -> None:
        await self._session.flush()

    async def list(self, *, include_archived: bool = False) -> Sequence[ExtractionSchema]:
        statement = select(ExtractionSchema).options(selectinload(ExtractionSchema.fields))
        if not include_archived:
            statement = statement.where(ExtractionSchema.status != ExtractionSchemaStatus.ARCHIVED)
        statement = statement.order_by(ExtractionSchema.is_default.desc(), ExtractionSchema.name)
        return (await self._session.execute(statement)).scalars().unique().all()

    async def get(self, schema_id: uuid.UUID) -> ExtractionSchema | None:
        return await self._session.get(ExtractionSchema, schema_id)

    async def get_by_key(self, key: str) -> ExtractionSchema | None:
        statement = (
            select(ExtractionSchema)
            .where(ExtractionSchema.key == key)
            .options(selectinload(ExtractionSchema.fields))
        )
        return (await self._session.execute(statement)).scalars().unique().first()

    async def get_default(self) -> ExtractionSchema | None:
        """The dataset the pipeline runs when nothing names one.

        Falls back to the only active schema when no row is flagged, so a desk
        that has exactly one dataset never has to know the flag exists.
        """
        statement = (
            select(ExtractionSchema)
            .where(
                ExtractionSchema.is_default.is_(True),
                ExtractionSchema.status == ExtractionSchemaStatus.ACTIVE,
            )
            .options(selectinload(ExtractionSchema.fields))
        )
        found = (await self._session.execute(statement)).scalars().unique().first()
        if found is not None:
            return found

        active = (
            (
                await self._session.execute(
                    select(ExtractionSchema)
                    .where(ExtractionSchema.status == ExtractionSchemaStatus.ACTIVE)
                    .options(selectinload(ExtractionSchema.fields))
                    .order_by(ExtractionSchema.created_at)
                    .limit(2)
                )
            )
            .scalars()
            .unique()
            .all()
        )
        return active[0] if len(active) == 1 else None

    def add(self, schema: ExtractionSchema) -> ExtractionSchema:
        self._session.add(schema)
        return schema

    async def clear_default(self, *, except_id: uuid.UUID | None = None) -> None:
        """Unflag every other default.

        Called before flagging one, because the partial unique index means two
        defaults is an integrity error rather than a last-write-wins.
        """
        statement = select(ExtractionSchema).where(ExtractionSchema.is_default.is_(True))
        if except_id is not None:
            statement = statement.where(ExtractionSchema.id != except_id)
        for row in (await self._session.execute(statement)).scalars().all():
            row.is_default = False
        await self._session.flush()

    async def delete(self, schema: ExtractionSchema) -> None:
        await self._session.delete(schema)

    async def replace_fields(
        self, schema: ExtractionSchema, fields: Sequence[ExtractionSchemaField]
    ) -> None:
        """Swap a dataset's whole field list.

        Replace rather than merge: the configuration UI edits the list as a list —
        reordering, renaming and removing in one save — and reconciling that
        field by field would need a client-supplied identity for a row the client
        just invented.
        """
        await self._session.execute(
            delete(ExtractionSchemaField).where(ExtractionSchemaField.schema_id == schema.id)
        )
        for position, row in enumerate(fields):
            row.schema_id = schema.id
            row.position = position
            self._session.add(row)
        await self._session.flush()
        await self._session.refresh(schema, attribute_names=["fields"])


class ExtractionRunRepository:
    """Runs and the values they produce."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def flush(self) -> None:
        await self._session.flush()

    # -- Runs ----------------------------------------------------------------

    def add_run(self, run: ExtractionRun) -> ExtractionRun:
        self._session.add(run)
        return run

    async def get_run(self, run_id: uuid.UUID) -> ExtractionRun | None:
        return await self._session.get(ExtractionRun, run_id)

    async def latest_run(self, case_id: uuid.UUID, schema_id: uuid.UUID) -> ExtractionRun | None:
        """The most recent run of one dataset against one notice.

        Ordered by `created_at` and then by `id`: two runs can share a timestamp
        at the resolution Postgres stores, and a tie-break that is not stable
        would make the reuse check flap between them.
        """
        statement = (
            select(ExtractionRun)
            .where(
                ExtractionRun.fnol_case_id == case_id,
                ExtractionRun.schema_id == schema_id,
            )
            .order_by(ExtractionRun.created_at.desc(), ExtractionRun.id.desc())
            .limit(1)
        )
        return (await self._session.execute(statement)).scalars().first()

    async def list_runs(self, case_id: uuid.UUID, *, limit: int = 20) -> Sequence[ExtractionRun]:
        statement = (
            select(ExtractionRun)
            .where(ExtractionRun.fnol_case_id == case_id)
            .order_by(ExtractionRun.created_at.desc())
            .limit(limit)
        )
        return (await self._session.execute(statement)).scalars().all()

    # -- Values --------------------------------------------------------------

    async def list_values(
        self, case_id: uuid.UUID, schema_id: uuid.UUID
    ) -> Sequence[ExtractedValue]:
        statement = (
            select(ExtractedValue)
            .where(
                ExtractedValue.fnol_case_id == case_id,
                ExtractedValue.schema_id == schema_id,
            )
            .order_by(ExtractedValue.group_label, ExtractedValue.field_key)
        )
        return (await self._session.execute(statement)).scalars().all()

    async def get_value(
        self, case_id: uuid.UUID, schema_id: uuid.UUID, field_key: str
    ) -> ExtractedValue | None:
        statement = select(ExtractedValue).where(
            ExtractedValue.fnol_case_id == case_id,
            ExtractedValue.schema_id == schema_id,
            ExtractedValue.field_key == field_key,
        )
        return (await self._session.execute(statement)).scalars().first()

    def add_value(self, value: ExtractedValue) -> ExtractedValue:
        self._session.add(value)
        return value

    async def count_for_case(self, case_id: uuid.UUID) -> dict[str, int]:
        """How many runs and values one notice carries, across every dataset.

        Read by the deletion path, which has to report what it removed and cannot
        ask afterwards. Not scoped to a schema, unlike everything else here: a
        notice read against two datasets holds values under both, and a count that
        saw only the default one would understate what is about to go.
        """
        return {
            "extraction_runs": int(
                (
                    await self._session.execute(
                        select(func.count(ExtractionRun.id)).where(
                            ExtractionRun.fnol_case_id == case_id
                        )
                    )
                ).scalar_one()
            ),
            "extracted_values": int(
                (
                    await self._session.execute(
                        select(func.count(ExtractedValue.id)).where(
                            ExtractedValue.fnol_case_id == case_id
                        )
                    )
                ).scalar_one()
            ),
        }

    # -- Citations -----------------------------------------------------------

    async def list_citations(self, value_id: uuid.UUID) -> Sequence[ExtractedValueCitation]:
        """One value's citations, in the order the source stepper walks them."""
        statement = (
            select(ExtractedValueCitation)
            .where(ExtractedValueCitation.value_id == value_id)
            .order_by(ExtractedValueCitation.rank, ExtractedValueCitation.created_at)
        )
        return (await self._session.execute(statement)).scalars().all()

    async def get_citation(
        self, value_id: uuid.UUID, document_id: uuid.UUID
    ) -> ExtractedValueCitation | None:
        """One value's citation of one document, if it has one."""
        statement = select(ExtractedValueCitation).where(
            ExtractedValueCitation.value_id == value_id,
            ExtractedValueCitation.document_id == document_id,
        )
        return (await self._session.execute(statement)).scalars().first()

    async def count_citations(
        self, case_id: uuid.UUID, schema_id: uuid.UUID
    ) -> dict[uuid.UUID, int]:
        """Citations per value, for one case's values, in one query.

        The review screen shows the count on every row — a value three documents
        agree on is worth more than one only one document states, and that is a
        thing to see before clicking rather than after. One aggregate rather than a
        relationship load per row, because thirty-eight rows would be
        thirty-eight queries for a number.
        """
        statement = (
            select(
                ExtractedValueCitation.value_id,
                func.count(ExtractedValueCitation.id),
            )
            .join(ExtractedValue, ExtractedValue.id == ExtractedValueCitation.value_id)
            .where(
                ExtractedValue.fnol_case_id == case_id,
                ExtractedValue.schema_id == schema_id,
            )
            .group_by(ExtractedValueCitation.value_id)
        )
        return {row[0]: int(row[1]) for row in (await self._session.execute(statement)).all()}

    async def replace_citations(
        self, value: ExtractedValue, drafts: Sequence[CitationDraft]
    ) -> list[ExtractedValueCitation]:
        """Swap a value's whole citation set for the one a run just derived.

        Replace rather than merge, for the same reason `replace_fields` does: the
        set is derived as a set, and the ranks are only meaningful relative to each
        other. Merging would also have to decide what to do with a citation whose
        document no longer states the value — which is "delete it", so the merge
        buys nothing but a way to get that wrong.

        The cached rectangles go with the old rows, deliberately. They were
        measured against a quote this run may have changed, and a rectangle drawn
        round the wrong words is worse than the half-second it costs to measure
        again.
        """
        await self._session.execute(
            delete(ExtractedValueCitation).where(ExtractedValueCitation.value_id == value.id)
        )
        rows = [
            ExtractedValueCitation(
                value_id=value.id,
                role=draft.role,
                rank=draft.rank,
                document_id=draft.document_id,
                chunk_id=draft.chunk_id,
                page_number=draft.page_number,
                section_label=draft.section_label,
                quote=draft.quote,
                char_start=draft.char_start,
                char_end=draft.char_end,
                strategy=draft.strategy,
            )
            for draft in drafts
        ]
        self._session.add_all(rows)
        return rows

    async def prune_values(
        self, case_id: uuid.UUID, schema_id: uuid.UUID, *, keep: Sequence[str]
    ) -> int:
        """Delete values for fields the dataset no longer has.

        A field removed from a schema must stop appearing on the review screen,
        and a value nobody can see is a value nobody can correct. Human-modified
        values are kept regardless: an officer's answer to a question we have
        since stopped asking is still their answer, and deleting it silently is
        the one thing this module must not do.
        """
        statement = select(ExtractedValue).where(
            ExtractedValue.fnol_case_id == case_id,
            ExtractedValue.schema_id == schema_id,
            ExtractedValue.human_modified.is_(False),
        )
        if keep:
            statement = statement.where(ExtractedValue.field_key.not_in(list(keep)))
        rows = (await self._session.execute(statement)).scalars().all()
        for row in rows:
            await self._session.delete(row)
        return len(rows)


#: Statuses a run may be in and still be worth reusing rather than repeating.
REUSABLE_RUN_STATUSES = frozenset({ExtractionRunStatus.COMPLETED})


__all__ = [
    "REUSABLE_RUN_STATUSES",
    "ExtractionRunRepository",
    "ExtractionSchemaRepository",
]
