"""FNOL case reads and writes.

The child collections are all *upserted by natural key* rather than deleted and
re-inserted. That is the difference between a pipeline that can be re-run safely
and one that loses an officer's work every time a document arrives: re-running
extraction must update the value of `loss.date_of_loss`, not replace the row that
recorded who corrected it and why.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy import case as sql_case
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    REVIEW_FNOL_STATUSES,
    AnalysisKind,
    ExceptionStatus,
    FNOLStatus,
)
from app.models.fnol import (
    FNOLAIAnalysis,
    FNOLCase,
    FNOLDocument,
    FNOLDuplicateCandidate,
    FNOLException,
    FNOLExtractedField,
    FNOLNote,
    FNOLParty,
    FNOLPolicyMatch,
)

#: How far back the duplicate scan looks. A notice repeating a loss from three
#: years ago is a coincidence; one repeating last month's is a duplicate.
DUPLICATE_LOOKBACK_DAYS = 180

#: Worst first, unscored last.
_SEVERITY_ORDER = sql_case(
    {"critical": 0, "high": 1, "medium": 2, "low": 3},
    value=FNOLCase.severity,
    else_=4,
)


class FNOLRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- Cases ---------------------------------------------------------------

    def add(self, case: FNOLCase) -> FNOLCase:
        self._session.add(case)
        return case

    async def flush(self) -> None:
        """Push pending inserts so generated ids are available. Not a commit."""
        await self._session.flush()

    async def get(self, case_id: uuid.UUID) -> FNOLCase | None:
        return await self._session.get(FNOLCase, case_id)

    async def get_by_reference(self, reference: str) -> FNOLCase | None:
        statement = select(FNOLCase).where(FNOLCase.reference == reference.strip().upper())
        return (await self._session.execute(statement)).scalars().first()

    async def get_for_update(self, case_id: uuid.UUID) -> FNOLCase | None:
        """Lock the case row for the duration of the caller's transaction.

        Used by claim creation. Two clicks on "Create claim" arrive as two
        requests; the second one blocks here and then finds the claim the first
        one made, rather than racing it.
        """
        statement = select(FNOLCase).where(FNOLCase.id == case_id).with_for_update()
        return (await self._session.execute(statement)).scalars().first()

    async def find_by_idempotency_key(self, key: str) -> FNOLCase | None:
        statement = select(FNOLCase).where(FNOLCase.idempotency_key == key)
        return (await self._session.execute(statement)).scalars().first()

    async def find_by_message_id(self, message_id: str) -> FNOLCase | None:
        statement = select(FNOLCase).where(FNOLCase.message_id == message_id)
        return (await self._session.execute(statement)).scalars().first()

    async def list_cases(
        self,
        *,
        statuses: Sequence[str] | None = None,
        channels: Sequence[str] | None = None,
        severities: Sequence[str] | None = None,
        search: str | None = None,
        needs_review: bool = False,
        unassigned: bool = False,
        order: str = "received_at",
        limit: int = 25,
        offset: int = 0,
    ) -> tuple[Sequence[FNOLCase], int]:
        """A page of the intake queue, and the total behind it."""
        statement = self._filtered(
            select(FNOLCase),
            statuses=statuses,
            channels=channels,
            severities=severities,
            search=search,
            needs_review=needs_review,
            unassigned=unassigned,
        )

        total = int(
            (
                await self._session.execute(
                    self._filtered(
                        select(func.count(FNOLCase.id)),
                        statuses=statuses,
                        channels=channels,
                        severities=severities,
                        search=search,
                        needs_review=needs_review,
                        unassigned=unassigned,
                    )
                )
            ).scalar_one()
        )

        if order == "severity":
            # Severity is stored as text, so the ordering has to be stated:
            # alphabetically "critical" sorts below "low", which is the wrong way
            # round on the one board where severity decides what happens next.
            statement = statement.order_by(_SEVERITY_ORDER, FNOLCase.received_at.desc())
        else:
            statement = statement.order_by(FNOLCase.received_at.desc())

        rows = (await self._session.execute(statement.offset(offset).limit(limit))).scalars().all()
        return rows, total

    def _filtered[T](
        self,
        statement: Select[T],
        *,
        statuses: Sequence[str] | None,
        channels: Sequence[str] | None,
        severities: Sequence[str] | None,
        search: str | None,
        needs_review: bool,
        unassigned: bool,
    ) -> Select[T]:
        if statuses:
            statement = statement.where(FNOLCase.status.in_(list(statuses)))
        if channels:
            statement = statement.where(FNOLCase.channel.in_(list(channels)))
        if severities:
            statement = statement.where(FNOLCase.severity.in_(list(severities)))
        if needs_review:
            statement = statement.where(
                FNOLCase.status.in_([s.value for s in REVIEW_FNOL_STATUSES])
            )
        if unassigned:
            statement = statement.where(FNOLCase.assigned_to.is_(None))
        if search:
            pattern = f"%{search.strip()}%"
            statement = statement.where(
                or_(
                    FNOLCase.reference.ilike(pattern),
                    FNOLCase.insured_name.ilike(pattern),
                    FNOLCase.policy_number.ilike(pattern),
                    FNOLCase.loss_location.ilike(pattern),
                    FNOLCase.loss_description.ilike(pattern),
                    FNOLCase.reporter_name.ilike(pattern),
                )
            )
        return statement

    async def status_counts(self) -> dict[str, int]:
        statement = select(FNOLCase.status, func.count(FNOLCase.id)).group_by(FNOLCase.status)
        return dict((await self._session.execute(statement)).all())

    async def channel_counts(self) -> dict[str, int]:
        statement = select(FNOLCase.channel, func.count(FNOLCase.id)).group_by(FNOLCase.channel)
        return dict((await self._session.execute(statement)).all())

    async def severity_counts(self) -> dict[str, int]:
        statement = (
            select(FNOLCase.severity, func.count(FNOLCase.id))
            .where(FNOLCase.severity.is_not(None))
            .group_by(FNOLCase.severity)
        )
        return dict((await self._session.execute(statement)).all())

    async def count_received_since(self, since: datetime) -> int:
        statement = select(func.count(FNOLCase.id)).where(FNOLCase.received_at >= since)
        return int((await self._session.execute(statement)).scalar_one())

    async def count_open_exceptions(self) -> int:
        statement = (
            select(func.count(func.distinct(FNOLException.fnol_case_id)))
            .select_from(FNOLException)
            .where(FNOLException.status == ExceptionStatus.OPEN)
        )
        return int((await self._session.execute(statement)).scalar_one())

    async def count_open_exceptions_by_code(self) -> dict[str, int]:
        statement = (
            select(FNOLException.code, func.count(func.distinct(FNOLException.fnol_case_id)))
            .where(FNOLException.status == ExceptionStatus.OPEN)
            .group_by(FNOLException.code)
        )
        return dict((await self._session.execute(statement)).all())

    async def duplicate_scan_pool(self, case: FNOLCase) -> Sequence[FNOLCase]:
        """Other notices worth comparing this one against.

        Narrowed by recency and by sharing at least one strong signal — policy,
        insured or location — so the scorer walks tens of rows rather than the
        whole intake book. A notice sharing none of those is not a duplicate under
        any weighting the scorer uses.
        """
        since = datetime.now(UTC) - timedelta(days=DUPLICATE_LOOKBACK_DAYS)
        clauses = []
        if case.policy_number:
            clauses.append(FNOLCase.policy_number.ilike(f"%{case.policy_number.strip()}%"))
        if case.policy_id:
            clauses.append(FNOLCase.policy_id == case.policy_id)
        if case.insured_name:
            clauses.append(FNOLCase.insured_name.ilike(f"%{_lead(case.insured_name)}%"))
        if case.external_reference:
            clauses.append(FNOLCase.external_reference == case.external_reference)
        if case.date_of_loss:
            clauses.append(
                FNOLCase.date_of_loss.between(
                    case.date_of_loss - timedelta(days=3), case.date_of_loss + timedelta(days=3)
                )
            )

        if not clauses:
            return []

        statement = (
            select(FNOLCase)
            .where(
                FNOLCase.id != case.id,
                FNOLCase.received_at >= since,
                FNOLCase.status.not_in([FNOLStatus.CANCELLED, FNOLStatus.REJECTED]),
                or_(*clauses),
            )
            .order_by(FNOLCase.received_at.desc())
            .limit(50)
        )
        return (await self._session.execute(statement)).scalars().all()

    # -- Documents -----------------------------------------------------------

    async def get_document(self, document_id: uuid.UUID) -> FNOLDocument | None:
        return await self._session.get(FNOLDocument, document_id)

    async def find_document_by_checksum(
        self, case_id: uuid.UUID, checksum: str
    ) -> FNOLDocument | None:
        statement = select(FNOLDocument).where(
            FNOLDocument.fnol_case_id == case_id,
            FNOLDocument.checksum_sha256 == checksum,
        )
        return (await self._session.execute(statement)).scalars().first()

    async def list_documents(self, case_id: uuid.UUID) -> Sequence[FNOLDocument]:
        statement = (
            select(FNOLDocument)
            .where(FNOLDocument.fnol_case_id == case_id)
            .order_by(FNOLDocument.created_at)
        )
        return (await self._session.execute(statement)).scalars().all()

    def add_document(self, document: FNOLDocument) -> FNOLDocument:
        self._session.add(document)
        return document

    async def delete_document(self, document: FNOLDocument) -> None:
        await self._session.delete(document)

    async def count_documents(self, case_id: uuid.UUID) -> int:
        statement = select(func.count(FNOLDocument.id)).where(FNOLDocument.fnol_case_id == case_id)
        return int((await self._session.execute(statement)).scalar_one())

    # -- Extracted fields ----------------------------------------------------

    async def list_fields(self, case_id: uuid.UUID) -> Sequence[FNOLExtractedField]:
        statement = (
            select(FNOLExtractedField)
            .where(FNOLExtractedField.fnol_case_id == case_id)
            .order_by(FNOLExtractedField.section, FNOLExtractedField.field_path)
        )
        return (await self._session.execute(statement)).scalars().all()

    async def get_field(self, case_id: uuid.UUID, field_path: str) -> FNOLExtractedField | None:
        statement = select(FNOLExtractedField).where(
            FNOLExtractedField.fnol_case_id == case_id,
            FNOLExtractedField.field_path == field_path,
        )
        return (await self._session.execute(statement)).scalars().first()

    async def upsert_field(
        self,
        case_id: uuid.UUID,
        *,
        field_path: str,
        section: str,
        label: str,
        value_text: str | None,
        confidence: float | None,
        source: str,
        source_document_id: uuid.UUID | None = None,
        evidence_snippet: str | None = None,
    ) -> FNOLExtractedField:
        """Record what a value is and where it came from.

        A field a human has already corrected is *not* overwritten by a later
        model run — only its evidence is refreshed. Losing an officer's correction
        to a re-extraction is the single most damaging thing this module could do.
        """
        existing = await self.get_field(case_id, field_path)
        if existing is None:
            field = FNOLExtractedField(
                fnol_case_id=case_id,
                field_path=field_path,
                section=section,
                label=label,
                value_text=value_text,
                confidence=confidence,
                source=source,
                source_document_id=source_document_id,
                evidence_snippet=evidence_snippet,
            )
            self._session.add(field)
            return field

        existing.label = label
        existing.section = section
        if existing.human_modified:
            existing.evidence_snippet = evidence_snippet or existing.evidence_snippet
            if source_document_id:
                existing.source_document_id = source_document_id
            return existing

        existing.value_text = value_text
        existing.confidence = confidence
        existing.source = source
        existing.source_document_id = source_document_id
        existing.evidence_snippet = evidence_snippet
        return existing

    # -- Parties -------------------------------------------------------------

    async def list_parties(self, case_id: uuid.UUID) -> Sequence[FNOLParty]:
        statement = (
            select(FNOLParty)
            .where(FNOLParty.fnol_case_id == case_id)
            .order_by(FNOLParty.role, FNOLParty.name)
        )
        return (await self._session.execute(statement)).scalars().all()

    async def get_party(self, party_id: uuid.UUID) -> FNOLParty | None:
        return await self._session.get(FNOLParty, party_id)

    async def upsert_party(
        self,
        case_id: uuid.UUID,
        *,
        role: str,
        name: str,
        organisation: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        address: str | None = None,
        source: str,
        confidence: float | None = None,
        is_primary: bool = False,
    ) -> FNOLParty:
        """Keyed on role plus name, so a re-run does not duplicate the claimant."""
        statement = select(FNOLParty).where(
            FNOLParty.fnol_case_id == case_id,
            FNOLParty.role == role,
            func.lower(FNOLParty.name) == name.strip().lower(),
        )
        existing = (await self._session.execute(statement)).scalars().first()
        if existing is not None:
            existing.organisation = organisation or existing.organisation
            existing.email = email or existing.email
            existing.phone = phone or existing.phone
            existing.address = address or existing.address
            existing.is_primary = is_primary or existing.is_primary
            return existing

        party = FNOLParty(
            fnol_case_id=case_id,
            role=role,
            name=name.strip(),
            organisation=organisation,
            email=email,
            phone=phone,
            address=address,
            source=source,
            confidence=confidence,
            is_primary=is_primary,
        )
        self._session.add(party)
        return party

    async def delete_party(self, party: FNOLParty) -> None:
        await self._session.delete(party)

    # -- Policy candidates ---------------------------------------------------

    async def list_policy_matches(self, case_id: uuid.UUID) -> Sequence[FNOLPolicyMatch]:
        statement = (
            select(FNOLPolicyMatch)
            .where(FNOLPolicyMatch.fnol_case_id == case_id)
            .order_by(FNOLPolicyMatch.rank)
        )
        return (await self._session.execute(statement)).scalars().all()

    async def get_policy_match(
        self, case_id: uuid.UUID, policy_id: uuid.UUID
    ) -> FNOLPolicyMatch | None:
        statement = select(FNOLPolicyMatch).where(
            FNOLPolicyMatch.fnol_case_id == case_id,
            FNOLPolicyMatch.policy_id == policy_id,
        )
        return (await self._session.execute(statement)).scalars().first()

    async def replace_policy_matches(
        self, case_id: uuid.UUID, candidates: Sequence[dict[str, Any]]
    ) -> Sequence[FNOLPolicyMatch]:
        """Rescore the candidate list, preserving whichever one a human chose.

        Candidates are the model's working, not the officer's decision — so they
        are replaced wholesale on every run, except that a selected candidate
        keeps its flag.
        """
        existing = {match.policy_id: match for match in await self.list_policy_matches(case_id)}
        selected_id = next(
            (policy_id for policy_id, match in existing.items() if match.selected), None
        )
        selected_by = existing[selected_id].selected_by if selected_id else None

        incoming_ids = {candidate["policy_id"] for candidate in candidates}
        for policy_id, match in existing.items():
            if policy_id not in incoming_ids and policy_id != selected_id:
                await self._session.delete(match)

        results: list[FNOLPolicyMatch] = []
        for index, candidate in enumerate(candidates):
            policy_id = candidate["policy_id"]
            match = existing.get(policy_id)
            if match is None:
                match = FNOLPolicyMatch(fnol_case_id=case_id, policy_id=policy_id)
                self._session.add(match)
            match.match_strength = candidate["match_strength"]
            match.score = candidate["score"]
            match.rank = index
            match.matched_on = candidate.get("matched_on", {})
            match.reasoning = candidate.get("reasoning")
            match.selected = policy_id == selected_id
            match.selected_by = selected_by if match.selected else None
            results.append(match)
        return results

    # -- Duplicates ----------------------------------------------------------

    async def list_duplicates(self, case_id: uuid.UUID) -> Sequence[FNOLDuplicateCandidate]:
        statement = (
            select(FNOLDuplicateCandidate)
            .where(FNOLDuplicateCandidate.fnol_case_id == case_id)
            .order_by(FNOLDuplicateCandidate.score.desc())
        )
        return (await self._session.execute(statement)).scalars().all()

    async def get_duplicate(self, duplicate_id: uuid.UUID) -> FNOLDuplicateCandidate | None:
        return await self._session.get(FNOLDuplicateCandidate, duplicate_id)

    async def upsert_duplicate(
        self,
        case_id: uuid.UUID,
        *,
        candidate_kind: str,
        candidate_reference: str,
        score: float,
        reasons: list[dict[str, Any]],
        candidate_fnol_id: uuid.UUID | None = None,
        candidate_claim_id: uuid.UUID | None = None,
    ) -> FNOLDuplicateCandidate:
        """A rescored candidate keeps whatever the officer already decided about it."""
        statement = select(FNOLDuplicateCandidate).where(
            FNOLDuplicateCandidate.fnol_case_id == case_id,
            FNOLDuplicateCandidate.candidate_reference == candidate_reference,
        )
        existing = (await self._session.execute(statement)).scalars().first()
        if existing is not None:
            existing.score = score
            existing.reasons = reasons
            return existing

        candidate = FNOLDuplicateCandidate(
            fnol_case_id=case_id,
            candidate_kind=candidate_kind,
            candidate_reference=candidate_reference,
            candidate_fnol_id=candidate_fnol_id,
            candidate_claim_id=candidate_claim_id,
            score=score,
            reasons=reasons,
        )
        self._session.add(candidate)
        return candidate

    async def prune_duplicates(self, case_id: uuid.UUID, keep_references: Sequence[str]) -> None:
        """Drop unresolved candidates that no longer score. Decided ones stay.

        A candidate an officer marked "not a duplicate" must not come back on the
        next run, and one they marked "duplicate" is part of the record.
        """
        keep = set(keep_references)
        for candidate in await self.list_duplicates(case_id):
            if candidate.candidate_reference in keep:
                continue
            if candidate.resolution == "unresolved":
                await self._session.delete(candidate)

    # -- Exceptions ----------------------------------------------------------

    async def list_exceptions(
        self, case_id: uuid.UUID, *, only_open: bool = False
    ) -> Sequence[FNOLException]:
        statement = select(FNOLException).where(FNOLException.fnol_case_id == case_id)
        if only_open:
            statement = statement.where(FNOLException.status == ExceptionStatus.OPEN)
        return (
            (await self._session.execute(statement.order_by(FNOLException.created_at)))
            .scalars()
            .all()
        )

    async def get_exception(self, exception_id: uuid.UUID) -> FNOLException | None:
        return await self._session.get(FNOLException, exception_id)

    async def get_exception_by_code(self, case_id: uuid.UUID, code: str) -> FNOLException | None:
        statement = select(FNOLException).where(
            FNOLException.fnol_case_id == case_id, FNOLException.code == code
        )
        return (await self._session.execute(statement)).scalars().first()

    async def upsert_exception(
        self,
        case_id: uuid.UUID,
        *,
        code: str,
        severity: str,
        title: str,
        detail: str | None,
        blocking: bool,
        context: dict[str, Any] | None = None,
    ) -> FNOLException:
        """Raise an exception, or refresh one that is already open.

        A resolved exception is reopened only if its cause is *worse* than when it
        was resolved — otherwise an officer who dismissed "possible duplicate"
        would see it return on every re-run, which teaches them to ignore the
        list.
        """
        existing = await self.get_exception_by_code(case_id, code)
        if existing is None:
            exception = FNOLException(
                fnol_case_id=case_id,
                code=code,
                severity=severity,
                title=title,
                detail=detail,
                blocking=blocking,
                context=context or {},
            )
            self._session.add(exception)
            return exception

        existing.severity = severity
        existing.title = title
        existing.detail = detail
        existing.blocking = blocking
        existing.context = context or {}
        return existing

    async def clear_exception(self, case_id: uuid.UUID, code: str) -> None:
        """Remove an exception whose cause has gone away.

        Deleted rather than resolved: nobody resolved it, the condition simply
        stopped being true, and recording that as a resolution would put a
        decision in the audit trail that no human made. The audit trail keeps the
        event that raised it.
        """
        existing = await self.get_exception_by_code(case_id, code)
        if existing is not None and existing.status == ExceptionStatus.OPEN:
            await self._session.delete(existing)

    # -- Analyses ------------------------------------------------------------

    async def get_analysis(self, case_id: uuid.UUID, kind: str) -> FNOLAIAnalysis | None:
        statement = select(FNOLAIAnalysis).where(
            FNOLAIAnalysis.fnol_case_id == case_id, FNOLAIAnalysis.kind == kind
        )
        return (await self._session.execute(statement)).scalars().first()

    async def list_analyses(self, case_id: uuid.UUID) -> Sequence[FNOLAIAnalysis]:
        statement = select(FNOLAIAnalysis).where(FNOLAIAnalysis.fnol_case_id == case_id)
        return (await self._session.execute(statement)).scalars().all()

    async def record_analysis(
        self,
        case_id: uuid.UUID,
        *,
        kind: AnalysisKind | str,
        provider: str,
        model: str | None,
        input_fingerprint: str,
        result: dict[str, Any],
        confidence: float | None = None,
        status: str = "completed",
        error: str | None = None,
        latency_ms: int | None = None,
    ) -> FNOLAIAnalysis:
        existing = await self.get_analysis(case_id, str(kind))
        if existing is None:
            existing = FNOLAIAnalysis(fnol_case_id=case_id, kind=str(kind))
            self._session.add(existing)

        existing.provider = provider
        existing.model = model
        existing.input_fingerprint = input_fingerprint
        existing.result = result
        existing.confidence = confidence
        existing.status = status
        existing.error = error
        existing.latency_ms = latency_ms
        return existing

    # -- Notes ---------------------------------------------------------------

    async def list_notes(self, case_id: uuid.UUID) -> Sequence[FNOLNote]:
        statement = (
            select(FNOLNote)
            .where(FNOLNote.fnol_case_id == case_id)
            .order_by(FNOLNote.created_at.desc())
        )
        return (await self._session.execute(statement)).scalars().all()

    def add_note(self, case_id: uuid.UUID, *, author: str, body: str) -> FNOLNote:
        note = FNOLNote(fnol_case_id=case_id, author=author, body=body)
        self._session.add(note)
        return note


def _lead(value: str) -> str:
    parts = [part for part in value.strip().split() if len(part) > 2]
    return parts[0] if parts else value.strip()
